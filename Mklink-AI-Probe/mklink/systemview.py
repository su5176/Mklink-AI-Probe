"""
MKLink Serial Bridge — SystemView 会话管理。

RTOS 的 SEGGER_SYSVIEW 集成把跟踪事件包写入 RTT 上行通道 1（"SysView" 缓冲）。
SystemViewSession 通过探针固件的 ``SystemView.start`` 打开转发，完成
SEGGER UART Recorder 的 ``SV`` 握手后进入 ``SYSTEMVIEW_STREAM`` 二进制流模式，
把原始字节交给 SystemViewParser 解码。

依赖: 无外部依赖
内部依赖: mklink.bridge, mklink._types, mklink.rtt（复用解析）,
          mklink.systemview_parser
"""

from __future__ import annotations

import time

from mklink._types import DeviceState
from mklink.bridge import MKLinkSerialBridge
from mklink.rtt import RTTSession


class SystemViewSession:
    """SystemView 跟踪流会话（RTT 通道 1，二进制）。"""

    def __init__(self, bridge: MKLinkSerialBridge, channel: int = 1):
        self._bridge = bridge
        self._channel = channel
        self._running = False
        self._prefetched = bytearray()
        self._needs_failed_start_reset = False

    _HOST_HELLO = b"SV\x01\x00"
    _CLIENT_HELLO_PREFIX = b"SV"
    _COMMAND_START = b"\x01"
    _COMMAND_STOP = b"\x02"
    _COMMAND_GET_TASKLIST = b"\x04"
    _TASKLIST_RETRY_DELAY_S = 0.15
    _STREAM_DRAIN_INTERVAL_S = 0.01

    def start(
        self,
        addr: str,
        search_size: int = 1024,
        project_root: str = ".",
        *,
        mode: int = 0,
    ) -> dict:
        """启动 SystemView 采集：打开探针转发并完成 Recorder 握手。

        地址解析复用 RTTSession（.mklink/rtt_config.json 或默认 0x20000000）。
        CB 存在性用 ``cmd.read_ram`` 直接验证 magic（比探针 RTTView 文本回执
        可靠——**高频通道(1)下探针会立即推流、不回 ``>>>``**，``send_command``
        拿不到 "Find SEGGER RTT addr" 回执，但 RTT 实际已工作）。
        """
        if mode not in (0, 1):
            raise ValueError(f"rtt_storage_mode 必须是 0 或 1，得到 {mode}")

        if not addr:
            addr = RTTSession._find_rtt_addr_from_config(project_root)
            if addr:
                print(f"[OK] 从配置读取 RTT 地址: {addr}")
            else:
                addr = "0x20000000"
                print(f"[WARN] 未找到 RTT 配置，使用默认搜索地址: {addr}")

        # 探针 SystemView.start 用 search_size 字节从 addr 扫描找 magic——
        # 即使 addr 是静态精确地址也必须给非零窗口（size=0 探针不扫描、报 no find）。
        actual_search_size = search_size if search_size else 1024
        cmd = f"SystemView.start({addr},{actual_search_size},{self._channel})"
        result: dict = {"storage_mode": mode, "channel": self._channel}
        cb_addr_int = int(str(addr), 16)

        # 1) 命令模式下直接读 CB magic 验证（retry：reconnect 后 bridge 可能短暂不
        #    稳定，首次 read_ram 解析失败。retry 3 次给 bridge warmup 时间）
        from mklink.memory_access import parse_read_ram_response
        found = False
        for _ in range(5):
            try:
                magic = parse_read_ram_response(
                    self._bridge.send_command(
                        f"cmd.read_ram(0x{cb_addr_int:08X}, 16)", timeout=6.0
                    )
                )
                if magic[:11] == b"SEGGER RTT\x00":
                    result["control_block_addr"] = f"0x{cb_addr_int:08X}"
                    found = True
                    break
            except Exception:
                pass
            time.sleep(0.5)

        # Sending SystemView.start may switch the probe before the final reply is
        # parsed.  Keep the recovery obligation explicit until the Recorder
        # handshake has completed so every failed start restores command mode.
        self._needs_failed_start_reset = True
        try:
            # 2) 专用 SystemView 模式在握手前不会推送事件，可靠等待启动回执。
            resp = self._bridge.send_command(cmd, timeout=20.0)
            result.update(RTTSession._parse_rtt_startup(resp))
            if found:
                result["control_block_addr"] = f"0x{cb_addr_int:08X}"
            if not result.get("control_block_addr"):
                raise RuntimeError(
                    f"SystemView 未找到 RTT 控制块（起始地址 {addr}, "
                    f"搜索范围 {actual_search_size} 字节）"
                )

            # 3) 进入原始流，执行 SEGGER UART Recorder 四字节握手。
            self._bridge._enter_stream(DeviceState.SYSTEMVIEW_STREAM)
            self._bridge._write_raw(self._HOST_HELLO)
            self._prefetched = bytearray(self._read_recorder_hello(timeout=2.0))
            self._bridge._write_raw(self._COMMAND_START)
        except Exception as error:
            try:
                self.reset_failed_start(cause=error)
            except Exception:
                # Never replace the actual startup failure with cleanup noise.
                pass
            raise
        self._running = True
        self._needs_failed_start_reset = False
        self._started_at = time.monotonic()
        self._tasklist_requested = False
        return result

    def _request_tasklist_after_startup(self) -> None:
        """Retry task metadata after the START burst has been drained.

        START already emits the complete target description.  Small RTT buffers can
        overflow during that initial burst, so requesting only TASKLIST once the host
        is actively draining recovers task/stack metadata without duplicating INIT and
        SYSDESC traffic.
        """
        if (
            not self._tasklist_requested
            and time.monotonic() - self._started_at >= self._TASKLIST_RETRY_DELAY_S
        ):
            self._bridge._write_raw(self._COMMAND_GET_TASKLIST)
            self._tasklist_requested = True

    def _read_recorder_hello(self, *, timeout: float) -> bytes:
        received = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._bridge.drain_stream_bytes()
            if chunk:
                received.extend(chunk)
                if len(received) >= 4:
                    if received[:2] != self._CLIENT_HELLO_PREFIX:
                        raise RuntimeError(
                            "SystemView Recorder 握手失败：探针未返回 SV 响应"
                        )
                    return bytes(received[4:])
            time.sleep(0.01)
        raise RuntimeError("SystemView Recorder 握手超时")

    def read_bytes(
        self, duration: float = 2.0, max_bytes: int | None = None
    ) -> bytes:
        """在 duration 秒内持续 drain 二进制流缓冲，返回累积的原始字节。"""
        if not self._running:
            raise RuntimeError("SystemView not started")
        chunks: list[bytes] = []
        total = 0
        if self._prefetched:
            take = len(self._prefetched)
            if max_bytes is not None:
                take = min(take, max(0, int(max_bytes)))
            prefetched = bytes(self._prefetched[:take])
            del self._prefetched[:take]
            if prefetched:
                chunks.append(prefetched)
                total = len(prefetched)
        deadline = time.monotonic() + max(0.0, float(duration))
        while True:
            budget = None if max_bytes is None else max(0, int(max_bytes) - total)
            if budget == 0:
                break
            try:
                chunk = self._bridge.drain_stream_bytes(max_bytes=budget)
            except RuntimeError:
                break
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
            self._request_tasklist_after_startup()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self._STREAM_DRAIN_INTERVAL_S, remaining))
        # 收尾再 drain 一次，避免漏掉最后一段
        try:
            budget = None if max_bytes is None else max(0, int(max_bytes) - total)
            if budget != 0:
                chunk = self._bridge.drain_stream_bytes(max_bytes=budget)
                if chunk:
                    chunks.append(chunk)
        except RuntimeError:
            pass
        return b"".join(chunks)

    def reset_failed_start(self, *, cause: BaseException | None = None) -> str:
        """Best-effort recovery after an incomplete SystemView start.

        Unlike RTT, SystemView does not arm a prompt-to-stream transition in
        the bridge.  Normalizing the bridge to READY and stopping the
        firmware-side forwarder is sufficient.  A command timeout leaves the
        bridge in ERROR, so normalize it once more before the bounded raw-stop
        fallback.  The method is idempotent because ``Device.systemview_start``
        also calls it while releasing a failed session.
        """
        self._running = False
        self._prefetched.clear()
        self._tasklist_requested = False
        if not self._needs_failed_start_reset:
            return ""
        self._needs_failed_start_reset = False

        # send_command uses ERROR both for a prompt timeout (the probe may have
        # entered its stream) and for a serial transport failure.  Only the
        # former is recoverable in place.  Never turn a real disconnect back
        # into a synthetic READY state or write more bytes to a broken port.
        if getattr(self._bridge, "_transport_error", None) is not None:
            return ""
        if (
            self._bridge.state is DeviceState.ERROR
            and not isinstance(cause, TimeoutError)
        ):
            return ""

        remaining = ""
        try:
            remaining = self._bridge._exit_stream()
        except Exception:
            pass

        recover = getattr(
            self._bridge, "_recover_failed_stream_start", None,
        )
        if callable(recover):
            # The old start prompt may arrive after the stop write and falsely
            # satisfy send_command(stop).  Production bridges must always use
            # identity-verified recovery, even when their local state was not
            # ERROR; only simple compatibility bridges use the legacy path.
            try:
                recover(b"SystemView.stop()\n")
            except Exception:
                pass
            return remaining

        try:
            return self._bridge.send_command("SystemView.stop()", timeout=5.0)
        except Exception as stop_error:
            # A stop-command timeout is the same recoverable stream ambiguity
            # as a start timeout.  A ConnectionError/transport ERROR is not.
            if (
                self._bridge.state is DeviceState.ERROR
                and not isinstance(stop_error, TimeoutError)
            ):
                return remaining
            try:
                self._bridge._exit_stream()
            except Exception:
                pass
            try:
                self._bridge._write_raw(b"SystemView.stop()\n")
            except Exception:
                pass
            return remaining

    def stop(self) -> str:
        """停止采集：停止目标记录并退出探针 SystemView 模式。

        用 raw 写停止命令（与 start 的 raw 写对称）——探针在高频流模式下不在
        ``>>>`` 提示符，send_command 等不到回执会把 bridge 置 ERROR。raw 写 +
        小睡让探针停止推流、回到提示符，bridge 保持 READY 供后续命令使用。
        """
        write_error: ConnectionError | None = None
        try:
            if getattr(self._bridge, "_transport_error", None) is None:
                try:
                    self._bridge._write_raw(self._COMMAND_STOP)
                    self._bridge._write_raw(b"SystemView.stop()\n")
                    time.sleep(0.3)
                except ConnectionError as error:
                    write_error = error
                except Exception:
                    pass  # 非传输停止失败仍允许本地会话结束

            remaining = self._bridge._exit_stream()
            transport_error = getattr(self._bridge, "_transport_error", None)
            if transport_error is not None:
                raise ConnectionError(
                    "SystemView stop failed because the serial transport is unavailable"
                ) from transport_error
            if write_error is not None:
                raise write_error
            return remaining
        finally:
            self._prefetched.clear()
            self._running = False
            self._tasklist_requested = False
