"""
SEGGER SystemView RTT 跟踪流解码器。

把 RTT 上行通道（默认通道 1，SEGGER "SysView" 缓冲）里的 SEGGER SystemView
二进制事件流解码成结构化事件字典。零外部依赖。克隆 JustFloatParser 的
``feed(bytes) -> list[dict]`` API，供 SystemViewSession / SystemViewStreamManager
/ CLI / MCP 复用。

协议来源（逐字节对照实现）：
  * SEGGER SystemView User Guide (UM08027)
  * SEGGER_SYSVIEW.c  (_SendPacket / _EncodeU32 / _SendSyncInfo / _EncodeStr)
  * SEGGER_SYSVIEW.h  (SYSVIEW_EVTID_* 事件 ID 表)

============================== 协议要点 ==============================

传输：RTT up-channel ``SEGGER_SYSVIEW_RTT_CHANNEL``（默认 1）。

可变长 U32（LEB128，小端，7 bit/字节，MSB=0x80 表示后续还有字节）::

    while value > 0x7F:
        emit (value & 0x7F) | 0x80
        value >>= 7
    emit value & 0x7F            # 末字节 MSB=0
    # value 0 -> 单字节 0x00；最大 5 字节（SEGGER_SYSVIEW_QUANTA_U32）

同步序列：10 个 0x00（``_abSync``）。跟踪起始 / post-mortem 周期性重发。
解码器在起始或分帧错误后扫描连续 0x00 重新对齐。

ID 收缩（``SHRINK_ID``）：``shrunken = (Id - RAMBase) >> ID_SHIFT``。还原：
``Id = (shrunken << ID_SHIFT) + RAMBase``。RAMBase / ID_SHIFT 由 INIT 事件带出
（默认 0 / 2）。

时间戳：每个包末尾追加一个 delta 时间戳（varint），``abs = abs + delta``。
delta 先按 ``SEGGER_SYSVIEW_TIMESTAMP_BITS``（默认 32）掩码。时间戳通常是 CPU
周期，可用 INIT 里的 CPUFreq 换算成 µs。

字符串（``_EncodeStr``）：``[count][chars]``；count < 255 时 1 字节计数，否则
count=255 再跟 2 字节小端长度。

数据包分帧（``_SendPacket``），读首字节 b0：
  * b0 & 0x80（MSB 置位）—— EventId 是多字节 varint（模块事件 ≥128），
    读完整 varint 得 EventId，再读 length varint 得载荷字节数。
  * 24 <= b0 < 128 —— EventId = b0，再读 length varint。
  * b0 < 24 —— EventId = b0，**无 length**（载荷结构由 EventId 决定）。
随后按 EventId 规范解码载荷（u32=varint，id=收缩ID还原，str=count 前缀串），
最后读 delta 时间戳。
"""

from __future__ import annotations

import re
import struct
from typing import Any


# ---------------------------------------------------------------------------
# 事件 ID（SEGGER_SYSVIEW.h SYSVIEW_EVTID_*）
# ---------------------------------------------------------------------------
EVTID_NOP = 0
EVTID_OVERFLOW = 1
EVTID_ISR_ENTER = 2
EVTID_ISR_EXIT = 3
EVTID_TASK_START_EXEC = 4
EVTID_TASK_STOP_EXEC = 5
EVTID_TASK_START_READY = 6
EVTID_TASK_STOP_READY = 7
EVTID_TASK_CREATE = 8
EVTID_TASK_INFO = 9
EVTID_TRACE_START = 10
EVTID_TRACE_STOP = 11
EVTID_SYSTIME_CYCLES = 12
EVTID_SYSTIME_US = 13
EVTID_SYSDESC = 14
EVTID_USER_START = 15
EVTID_USER_STOP = 16
EVTID_IDLE = 17
EVTID_ISR_TO_SCHEDULER = 18
EVTID_TIMER_ENTER = 19
EVTID_TIMER_EXIT = 20
EVTID_STACK_INFO = 21
EVTID_MODULEDESC = 22
EVTID_INIT = 24
EVTID_NAME_RESOURCE = 25
EVTID_PRINT_FORMATTED = 26
EVTID_NUMMODULES = 27
EVTID_END_CALL = 28
EVTID_TASK_TERMINATE = 29
EVTID_EX = 31

# 触发 length 前缀的事件 ID 阈值（_SendPacket: if EventId < 24 无 length）
_LENGTH_PREFIX_THRESHOLD = 24

# 内存安全上限（防 corrupt 数据 / 全零流导致缓冲无限增长 → OOM）
_MAX_PACKET_PAYLOAD = 4096   # 单包载荷字节数上限（≈ SEGGER_SYSVIEW_MAX_PACKET_SIZE）
_MAX_BUF = 1_048_576         # 缓冲绝对上限 1MB（兜底；连续模式靠 NOP/事件消费自然排空）


# 每个 EventId 的载荷规范：(kind 名, [(类型, 字段名), ...])
#   类型: 'u32' varint / 'id' 收缩 ID 还原 / 'str' count 前缀串
_EVENT_SPECS: dict[int, tuple[str, list[tuple[str, str]]]] = {
    EVTID_NOP:              ("nop", []),
    EVTID_OVERFLOW:         ("overflow", [("u32", "drop_count")]),
    EVTID_ISR_ENTER:        ("isr_enter", [("u32", "isr_id")]),
    EVTID_ISR_EXIT:         ("isr_exit", []),
    EVTID_TASK_START_EXEC:  ("task_start_exec", [("id", "task_id")]),
    EVTID_TASK_STOP_EXEC:   ("task_stop_exec", []),
    EVTID_TASK_START_READY: ("task_start_ready", [("id", "task_id")]),
    EVTID_TASK_STOP_READY:  ("task_stop_ready", [("id", "task_id"), ("u32", "cause")]),
    EVTID_TASK_CREATE:      ("task_create", [("id", "task_id")]),
    EVTID_TASK_INFO:        ("task_info", [("id", "task_id"), ("u32", "prio"),
                                          ("str", "name")]),
    EVTID_TRACE_START:      ("trace_start", []),
    EVTID_TRACE_STOP:       ("trace_stop", []),
    EVTID_SYSTIME_CYCLES:   ("systime_cycles", [("u32", "systime")]),
    EVTID_SYSTIME_US:       ("systime_us", [("u32", "systime")]),
    EVTID_SYSDESC:          ("sysdesc", [("str", "desc")]),
    EVTID_USER_START:       ("user_start", [("u32", "user_id")]),
    EVTID_USER_STOP:        ("user_stop", [("u32", "user_id")]),
    EVTID_IDLE:             ("idle", []),
    EVTID_ISR_TO_SCHEDULER: ("isr_to_scheduler", []),
    EVTID_TIMER_ENTER:      ("timer_enter", [("u32", "timer_id")]),
    EVTID_TIMER_EXIT:       ("timer_exit", []),
    EVTID_STACK_INFO:       ("stack_info", [("id", "task_id"), ("u32", "stack_base"),
                                            ("u32", "stack_size"), ("u32", "stack_end")]),
    EVTID_MODULEDESC:       ("moduledesc", [("u32", "module_id"), ("str", "desc")]),
    EVTID_INIT:             ("init", [("u32", "sys_freq"), ("u32", "cpu_freq"),
                                     ("u32", "ram_base"), ("u32", "id_shift")]),
    EVTID_NAME_RESOURCE:    ("name_resource", [("id", "resource_id"), ("str", "name")]),
    EVTID_PRINT_FORMATTED:  ("print_formatted", [("str", "msg"), ("u32", "options"),
                                                 ("u32", "num_args")]),
    EVTID_NUMMODULES:       ("nummodules", [("u32", "num_modules")]),
    EVTID_END_CALL:         ("end_call", [("u32", "event_id")]),
    EVTID_TASK_TERMINATE:   ("task_terminate", [("id", "task_id")]),
}

# SYSDESC 里 "I#num=name" 形式的中断描述符（对应 menuconfig 的系统描述符）
_ISR_DESC_RE = re.compile(r"I#(\d+)=([^,\s]+)")


class _NeedMore(Exception):
    """缓冲区不足，需要更多字节（跨 feed 边界）。"""


class _FramingError(Exception):
    """分帧错误，需要重新同步。"""

    def __init__(self, message: str, *, discard: int = 1) -> None:
        super().__init__(message)
        self.discard = max(1, discard)


class SystemViewParser:
    """流式 SEGGER SystemView 事件解码器。

    用法::

        parser = SystemViewParser()
        for chunk in rtt_byte_stream:
            for event in parser.feed(chunk):
                handle(event)   # {"kind":..., "t_ticks":..., "task_id":..., ...}

    特性：
      * 跨 ``feed()`` 边界保持半包，连续喂数据即可。
      * 起始 / 分帧错误后扫描同步序列（连续 0x00）自动重对齐。
      * 累计绝对时间戳（tick），用 INIT 的 CPUFreq 换算 µs。
      * 维护 task_id→name / isr_id→name 映射，在事件里附带 ``task_name``。
    """

    #: 视为同步序列的最少连续 0x00 字节数（SEGGER 实发 10 个）
    MIN_SYNC_ZEROS = 5

    def __init__(self, *, timestamp_bits: int = 32) -> None:
        self._buf = bytearray()
        self._dropped_bytes = 0
        self._dropped_packets = 0
        self._synced = False
        self._abs_time = 0                 # 累计绝对时间戳（tick）
        self._ts_mask = (1 << timestamp_bits) - 1 if timestamp_bits < 32 else 0xFFFFFFFF
        self._ram_base = 0                 # INIT 带出
        self._id_shift = 2                 # INIT 带出（SEGGER 默认 2）
        self._cpu_freq = 0                 # INIT 带出，用于 µs 换算
        self._cpu_freq_override: int | None = None
        self._task_names: dict[int, str] = {}
        self._isr_names: dict[int, str] = {}
        self._current_task: int | None = None   # 跟踪当前运行任务（STOP_EXEC 无 task id）
        self._num_modules = 0

    # -- 公开属性 --------------------------------------------------------
    @property
    def dropped_bytes(self) -> int:
        return self._dropped_bytes

    @property
    def dropped_packets(self) -> int:
        return self._dropped_packets

    @property
    def synced(self) -> bool:
        return self._synced

    @property
    def abs_time(self) -> int:
        return self._abs_time

    @property
    def cpu_freq(self) -> int:
        return self._cpu_freq

    def set_cpu_freq(self, freq: int, *, lock: bool = False) -> None:
        """Set a host-side CPU frequency hint.

        A runtime symbol read is more authoritative than the frequency carried
        by a stale INIT packet. ``lock=True`` keeps that value when INIT is
        decoded later in the stream.
        """
        value = int(freq)
        if value <= 0:
            return
        self._cpu_freq = value
        if lock:
            self._cpu_freq_override = value

    def task_name(self, task_id: int) -> str | None:
        return self._task_names.get(task_id)

    def isr_name(self, isr_id: int) -> str | None:
        return self._isr_names.get(isr_id)

    def reset(self) -> None:
        """清空缓冲与累计状态（保留 name 映射与 INIT 配置可选由调用方重建）。"""
        self._buf.clear()
        self._synced = False
        self._abs_time = 0
        self._dropped_bytes = 0
        self._dropped_packets = 0

    # -- 主入口 ----------------------------------------------------------
    def feed(self, data: bytes) -> list[dict]:
        """喂入原始字节，返回本轮解码出的事件字典列表。

        连续模式（continuous，多数实时采集）的 SystemView 流**没有前导同步序列**
        ——首字节就是事件。因此解码器直接从当前位置尝试解析；解析失败（分帧
        错误）则丢 1 字节重试，靠事件 ID 合法性 + 长度上限自然恢复对齐（抓流
        中段也能对上）。post-mortem 模式的同步序列（连续 0x00）会被当作 NOP
        帧消费但不产出事件。
        """
        if data:
            self._buf.extend(data)
        events: list[dict] = []
        while self._buf:
            # 防御性兜底：缓冲异常增长（不应发生）时强制瘦身，杜绝 OOM
            if len(self._buf) > _MAX_BUF:
                self._dropped_bytes += len(self._buf) - self.MIN_SYNC_ZEROS
                del self._buf[: len(self._buf) - self.MIN_SYNC_ZEROS]
                self._synced = False
            try:
                ev = self._parse_packet()
            except _NeedMore:
                break  # 半包，保留缓冲等下一轮
            except _FramingError as exc:
                # 未对齐：丢 1 字节重试（连续流无同步头，靠此在任意位置恢复对齐）
                discard = min(exc.discard, len(self._buf))
                self._dropped_bytes += discard
                del self._buf[:discard]
                self._synced = False
                continue
            self._synced = True
            if ev is not None:
                events.append(ev)
        return events

    # -- 同步 ------------------------------------------------------------
    def _try_sync(self) -> bool:
        """扫描连续 0x00 同步序列。

        仅当零串被非零字节**终止**时才认定同步——这样可以一次性吃掉整段同步
        （SEGGER 实发 10 个 0x00），避免同步跨 ``feed()`` 边界时把残余 0x00
        误当成 NOP 包。同步前的字节视为垃圾计入 ``dropped_bytes``。
        """
        n = len(self._buf)
        i = 0
        while i < n:
            if self._buf[i] != 0x00:
                i += 1
                continue
            j = i
            while j < n and self._buf[j] == 0x00:
                j += 1
            run_len = j - i
            if run_len >= self.MIN_SYNC_ZEROS and j < n:
                # 完整同步串（被非零字节终止）：丢弃前导垃圾 + 整段 0x00
                self._dropped_bytes += i
                del self._buf[:j]
                self._synced = True
                return True
            if run_len >= self.MIN_SYNC_ZEROS:
                # 零串延伸到缓冲末尾（可能还有更多 0x00 要来）。只保留尾部
                # MIN_SYNC_ZEROS 字节等待终止符——否则全零流（通道未集成 / 空
                # RTT 缓冲）会让缓冲无限增长导致 OOM。
                keep_from = n - self.MIN_SYNC_ZEROS
                if keep_from > 0:
                    self._dropped_bytes += keep_from
                    del self._buf[:keep_from]
                return False
            i = j  # 零串太短，继续扫描
        # 未发现合格同步串；限制缓冲无限增长，保留尾部
        if n > 1024:
            self._dropped_bytes += n - self.MIN_SYNC_ZEROS
            del self._buf[: n - self.MIN_SYNC_ZEROS]
        return False

    # -- 单包解析 --------------------------------------------------------
    def _parse_packet(self) -> dict | None:
        pos = 0
        # 1. 读 EventId（可能 varint）
        first = self._peek(pos)
        if first & 0x80:
            event_id, pos = self._read_varint(pos)       # 模块事件 ≥128
            event_id_end = pos
            length, pos = self._read_varint(pos)
        elif first >= _LENGTH_PREFIX_THRESHOLD:
            event_id = first
            pos += 1
            event_id_end = pos
            length, pos = self._read_varint(pos)
        else:
            event_id = first
            pos += 1
            event_id_end = pos
            length = None  # 无 length，按 EventId 规范解码

        # 合法事件 ID：核心 0-31，中间件模块 512-4096。其余视为分帧错误（垃圾数据）
        # —— 这让乱码尽快触发重同步，而不是被当成假包一路吃进同步序列。
        if not (event_id <= 31 or 512 <= event_id <= 4096):
            raise _FramingError(f"implausible event id {event_id}")
        # A timestamp tail can look like a valid multi-byte module event while
        # recovering after target overflow.  Module events are impossible until the
        # target has declared at least one module, so reject that false framing and
        # continue the byte-wise resynchronization instead of swallowing following
        # TASK_INFO / STACK_INFO packets as one opaque raw payload.
        if event_id >= 512 and self._num_modules <= 0:
            raise _FramingError(
                f"module event {event_id} before module declaration",
                discard=event_id_end,
            )

        # 载荷长度上限：corrupt 的 length varint 会让缓冲朝 length 无限增长 → OOM
        if length is not None and length > _MAX_PACKET_PAYLOAD:
            raise _FramingError(f"implausible payload length {length}")

        # 2. 解码载荷
        spec = _EVENT_SPECS.get(event_id)
        if spec is None:
            # 未知 EventId（例如某 RTOS 私有事件）—— 若有 length 可跳过，否则报错重同步
            if length is None:
                raise _FramingError(f"unknown event id {event_id} without length")
            payload_bytes, pos = self._read_exact(pos, length)
            ev: dict[str, Any] = {"kind": f"raw_{event_id}", "event_id": event_id,
                                  "payload_hex": bytes(payload_bytes).hex()}
        else:
            kind, fields = spec
            if length is not None:
                payload_end = pos + length
                if payload_end > len(self._buf):
                    raise _NeedMore
                ev = {"kind": kind}
                ev.update(self._decode_fields(fields, pos, payload_end))
                pos = payload_end
            else:
                ev = {"kind": kind}
                field_vals, pos = self._decode_fields_need(fields, pos)
                ev.update(field_vals)

        invalid_task_info = False
        if ev.get("kind") == "task_info":
            name = ev.get("name")
            invalid_task_info = (
                not isinstance(name, str)
                or not name.strip()
                or len(name) > 32
                or "\ufffd" in name
                or not name.isprintable()
            )

        # 3. 读末尾 delta 时间戳
        delta, pos = self._read_varint(pos)
        delta &= self._ts_mask   # 掩到时间源有效位（处理 <32 位硬件时间戳）
        if invalid_task_info:
            self._dropped_packets += 1
            del self._buf[:pos]
            return None
        # abs_time 保持单调（Python 大整数，不掩码）——避免 32 位 CYCCNT 每 ~25s
        # 翻卷导致 abs 时间在翻卷边界两侧乱跳、CPU%/区间计算失真。delta 本身正确，
        # 故累加值即真实流逝时间（跨多次翻卷也对）。
        self._abs_time = self._abs_time + delta

        ev["delta_ticks"] = delta
        ev["t_ticks"] = self._abs_time

        # 4. 后处理：name 映射 / INIT 配置 / µs 换算 / 当前任务跟踪
        self._post_process(ev)

        # 消费已解析字节
        del self._buf[:pos]
        # NOP/同步占位帧：消费（含其 delta，累加 abs_time）但不产出事件，
        # 避免全零流 / post-mortem 同步序列刷屏 NOP。
        return None if event_id == EVTID_NOP else ev

    def _post_process(self, ev: dict) -> None:
        kind = ev["kind"]
        if kind == "init":
            if self._cpu_freq_override is None:
                self._cpu_freq = ev.get("cpu_freq", 0) or self._cpu_freq
            else:
                self._cpu_freq = self._cpu_freq_override
            self._ram_base = ev.get("ram_base", 0)
            # Zero is valid: HPM FreeRTOS reports ram_base=0/id_shift=0.
            id_shift = ev.get("id_shift")
            self._id_shift = 2 if id_shift is None else int(id_shift)
        elif kind == "task_info":
            tid = ev.get("task_id")
            name = ev.get("name")
            if tid is not None and name:
                self._task_names[tid] = name
        elif kind == "name_resource":
            rid = ev.get("resource_id")
            name = ev.get("name")
            if rid is not None and name:
                self._task_names.setdefault(rid, name)
        elif kind == "sysdesc":
            desc = ev.get("desc", "") or ""
            for m in _ISR_DESC_RE.finditer(desc):
                self._isr_names[int(m.group(1))] = m.group(2)
        elif kind == "nummodules":
            self._num_modules = max(0, int(ev.get("num_modules", 0) or 0))
        elif kind == "task_start_exec":
            self._current_task = ev.get("task_id")
        elif kind == "task_stop_exec":
            # SEGGER 的 STOP_EXEC 不带 task id——用跟踪到的当前任务补全，便于上层画甘特
            if self._current_task is not None:
                ev["task_id"] = self._current_task
        elif kind == "overflow":
            # The target dropped one or more records, so the cached task context
            # is no longer authoritative.  Do not let a later task-stop event
            # inherit the task that was running before the loss boundary.
            self._current_task = None

        # 给任务/ISR 事件补 name
        if "task_id" in ev:
            ev["task_name"] = self._task_names.get(ev["task_id"])
        if "isr_id" in ev:
            ev["isr_name"] = self._isr_names.get(ev["isr_id"])

        # µs 换算放在最后——INIT 自身的 cpu_freq 已更新，故 INIT 也能换算
        if self._cpu_freq:
            ev["cpu_delta_us"] = ev["delta_ticks"] * 1_000_000.0 / self._cpu_freq
            ev["t_us"] = ev["t_ticks"] * 1_000_000.0 / self._cpu_freq

    # -- 原语读取 --------------------------------------------------------
    def _peek(self, pos: int) -> int:
        if pos >= len(self._buf):
            raise _NeedMore
        return self._buf[pos]

    def _read_varint(self, pos: int) -> tuple[int, int]:
        result = 0
        shift = 0
        while True:
            if pos >= len(self._buf):
                raise _NeedMore
            b = self._buf[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result, pos
            shift += 7
            if shift > 35:  # 超过 5 字节，非法 varint
                raise _FramingError("varint too long")

    def _read_exact(self, pos: int, n: int) -> tuple[bytearray, int]:
        if pos + n > len(self._buf):
            raise _NeedMore
        return self._buf[pos:pos + n], pos + n

    def _decode_fields(self, fields, pos: int, end: int) -> dict:
        """在有明确 length 边界内解码字段（越界视为分帧错误）。"""
        out: dict[str, Any] = {}
        for ftype, fname in fields:
            if ftype == "str":
                val, pos = self._read_str_bounded(pos, end)
            elif ftype == "id":
                raw, pos = self._read_varint(pos)
                val = (raw << self._id_shift) + self._ram_base
                out[fname + "_raw"] = raw
            else:  # u32
                val, pos = self._read_varint(pos)
            out[fname] = val
        return out

    def _decode_fields_need(self, fields, pos: int) -> tuple[dict, int]:
        """无 length 边界时解码字段（缓冲不足抛 NeedMore）。"""
        out: dict[str, Any] = {}
        for ftype, fname in fields:
            if ftype == "str":
                val, pos = self._read_str(pos)
            elif ftype == "id":
                raw, pos = self._read_varint(pos)
                val = (raw << self._id_shift) + self._ram_base
                out[fname + "_raw"] = raw
            else:
                val, pos = self._read_varint(pos)
            out[fname] = val
        return out, pos

    def _read_str(self, pos: int) -> tuple[str, int]:
        if pos >= len(self._buf):
            raise _NeedMore
        count = self._buf[pos]
        pos += 1
        if count == 255:
            if pos + 2 > len(self._buf):
                raise _NeedMore
            length = struct.unpack_from("<H", self._buf, pos)[0]
            pos += 2
        else:
            length = count
        if pos + length > len(self._buf):
            raise _NeedMore
        s = bytes(self._buf[pos:pos + length]).decode("utf-8", errors="replace")
        return s, pos + length

    def _read_str_bounded(self, pos: int, end: int) -> tuple[str, int]:
        if pos >= end:
            raise _FramingError("string count out of payload bounds")
        count = self._buf[pos]
        pos += 1
        if count == 255:
            if pos + 2 > end:
                raise _FramingError("string length out of payload bounds")
            length = struct.unpack_from("<H", self._buf, pos)[0]
            pos += 2
        else:
            length = count
        if pos + length > end:
            raise _FramingError("string body out of payload bounds")
        s = bytes(self._buf[pos:pos + length]).decode("utf-8", errors="replace")
        return s, pos + length
