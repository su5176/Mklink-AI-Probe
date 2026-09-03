"""
MKLink Serial Bridge — 核心串口通信类。

依赖: pyserial
内部依赖: mklink._types
"""

from __future__ import annotations

import codecs
import re
import threading
import time
from collections.abc import Callable

import serial

from mklink._types import (
    DEFAULT_BAUDRATE,
    FLM_LOAD_TIMEOUT,
    PROMPT,
    DeviceContext,
    DeviceState,
    MKLINK_IDENTITY_COMMAND,
    MKLINK_IDENTITY_TOKEN,
)
from mklink.serial._port import _PortLock

# SystemView 在二进制流中使用 0x02 停止帧；随后发送文本命令让固件状态机
# 回到 Pika REPL。必须先独立尝试这一序列，避免后续 RTT/VOFA 命令在状态机
# 切回命令模式的瞬间拼接成 ``RTTView.stop(RTTView.stop...``。
_SYSTEMVIEW_STOP_COMMANDS = [
    b"\x02",
    b"SystemView.stop()\n",
]
# 其他流模式的文本兜底命令，仅在 SystemView 专用恢复没有得到提示符时发送。
_STREAM_FALLBACK_STOP_COMMANDS = [
    b"RTTView.stop()\n",
    b'vofa.send(0x20000000, "uint8_t", 0)\n',
    b"cmd.dump_memory(0x20000054, 4, -1.0)\n",
]
_STREAM_READ_POLL_INTERVAL = 0.01
_SYNC_TIMEOUTS = (0.3, 0.7)
_RECOVERY_PROMPT_TIMEOUT = 1.0
_FAILED_STREAM_STOP_ATTEMPTS = 2
_FAILED_STREAM_STOP_TIMEOUT = 0.75
_FAILED_STREAM_IDENTITY_TIMEOUT = 2.0


def quote_probe_string(value: str) -> str:
    """Return a single-quoted literal accepted by the MicroKeen REPL.

    Several MicroKeen firmware revisions discard double quotes in commands
    received through the CDC REPL.  A single-quoted, escaped literal works on
    those revisions as well as on the usual Python-compatible interpreter.
    """
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f"'{escaped}'"


class MKLinkSerialBridge:
    """通过虚拟串口与 MKLink 烧录器通信的桥接类。"""

    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE):
        self._port = port
        self._baudrate = baudrate
        self._port_lock = _PortLock(port)
        self._serial: serial.Serial | None = None  # 延迟到 connect() 中打开
        self._ctx = DeviceContext()
        self._reader_thread: threading.Thread | None = None
        self._running = False
        self._response_buffer: list[str | bytes] = []
        self._armed_stream_state: DeviceState | None = None
        self._armed_stream_tail: list[bytes] = []
        self._armed_prompt_prefix = b""
        self._armed_prompt_seen = False
        # 命令响应和兼容文本流共用增量 UTF-8 解码器。RTT 原始字节不在
        # reader 线程中解码，以便上层选择目标固件实际使用的文本编码。
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._prompt_event = threading.Event()
        self._buffer_lock = threading.Lock()
        self._cmd_lock = threading.Lock()  # 命令级互斥，防止并发操作
        self._echo_enabled = False
        self._echo_prefix = "[SERIAL] "
        self._echo_offset = 0
        self._echo_pending = ""
        self._echo_callback: Callable[[str], None] | None = None
        self._transport_error: Exception | None = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def _verify_identity(self) -> bool:
        try:
            response = self.send_command(MKLINK_IDENTITY_COMMAND, timeout=2.0)
        except (ConnectionError, TimeoutError, serial.SerialException):
            return False
        return any(
            line.strip() == MKLINK_IDENTITY_TOKEN
            for line in response.splitlines()
        )

    def connect(self) -> bool:
        """打开串口并同步设备状态（等待 >>> 提示符）。"""
        self._transport_error = None
        # 进程级互斥：获取文件锁
        if not self._port_lock.acquire():
            print(f"[FAIL] 串口 {self._port} 正被其他进程使用")
            print("       请等待其他操作完成，或关闭占用串口的进程后重试。")
            return False

        try:
            self._serial = serial.Serial(self._port, self._baudrate, timeout=0.01)
        except serial.SerialException as e:
            self._port_lock.release()
            msg = str(e).lower()
            if "access" in msg or "denied" in msg or "already open" in msg or "in use" in msg:
                print(f"[FAIL] 端口 {self._port} 被占用: {e}")
                print("       请检查是否有其他程序正在使用该串口。")
            else:
                print(f"[FAIL] 无法打开端口 {self._port}: {e}")
            return False
        self._ctx.state = DeviceState.CONNECTING
        self._running = True

        # 清空缓冲区（丢弃历史数据）
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

        # 启动后台读线程
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True
        )
        self._reader_thread.start()

        # 正常 CMD 口会立即返回提示符。分两级短等待兼顾 USB 调度抖动，
        # 无响应时尽快进入流模式恢复，避免固定阻塞 2 秒三次。
        for sync_timeout in _SYNC_TIMEOUTS:
            self._prompt_event.clear()
            with self._buffer_lock:
                self._response_buffer.clear()
            self._serial.write(b"\n")

            if self._prompt_event.wait(timeout=sync_timeout):
                if self._transport_error is not None:
                    self.close()
                    return False
                self._ctx.state = DeviceState.READY
                if self._is_known_command_port() or self._verify_identity():
                    return True
                self.close()
                return False

            # 重试前清空缓冲区
            self._serial.reset_input_buffer()
            with self._buffer_lock:
                self._response_buffer.clear()

        # --- 正常握手失败，尝试流模式恢复 ---
        print("[WARN] 握手超时，设备可能处于流模式，尝试恢复...")

        # 停止 reader 线程以便直接操作串口
        self._running = False
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
            if self._reader_thread.is_alive():
                # reader 线程未退出，关闭串口强制终止
                self._ctx.state = DeviceState.ERROR
                if self._serial and self._serial.is_open:
                    self._serial.close()
                self._port_lock.release()
                return False

        try:
            # 排空缓冲区，先尝试 SystemView 的二进制停止握手。探针在
            # SystemView 流中不会把普通文本当作 Pika 命令，必须先发 0x02。
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            for stop_cmd in _SYSTEMVIEW_STOP_COMMANDS:
                self._serial.write(stop_cmd)
            time.sleep(0.1)

            # 重启 reader 线程，先验证 SystemView 是否已经回到 REPL。
            self._running = True
            self._ctx.state = DeviceState.CONNECTING
            self._response_buffer.clear()
            self._prompt_event.clear()
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self._reader_thread.start()

            self._serial.write(b"\n")
            if self._prompt_event.wait(timeout=_RECOVERY_PROMPT_TIMEOUT):
                self._ctx.state = DeviceState.READY
                if self._verify_identity():
                    print("[OK] 流模式恢复成功")
                    return True

            # 非 SystemView 流模式（例如 RTT/VOFA）不会响应上述握手，才发送
            # 各自的文本停止命令，再进行一次短提示符同步。
            for stop_cmd in _STREAM_FALLBACK_STOP_COMMANDS:
                try:
                    self._serial.write(stop_cmd)
                except serial.SerialException:
                    break
            time.sleep(0.3)
            self._serial.reset_input_buffer()
            self._prompt_event.clear()
            self._serial.write(b"\n")
            if self._prompt_event.wait(timeout=_RECOVERY_PROMPT_TIMEOUT):
                self._ctx.state = DeviceState.READY
                if self._verify_identity():
                    print("[OK] 流模式恢复成功")
                    return True
        except Exception:
            pass

        # 恢复也失败，释放资源
        self._ctx.state = DeviceState.ERROR
        self._running = False
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        self._port_lock.release()
        return False

    def _is_known_command_port(self) -> bool:
        """MI_04 is the fixed MKLink command interface on V2/V3/V4."""
        try:
            from serial.tools import list_ports
            from mklink.usb_interfaces import (
                MKLINK_COMMAND_INTERFACE,
                is_mklink_usb_port,
                usb_interface_number,
            )
            for info in list_ports.comports():
                if str(getattr(info, "device", "")).casefold() != self._port.casefold():
                    continue
                return (
                    is_mklink_usb_port(info)
                    and usb_interface_number(info) == MKLINK_COMMAND_INTERFACE
                )
        except Exception:
            pass
        return False

    def close(self):
        """关闭串口连接并释放文件锁。"""
        self._running = False
        self._prompt_event.set()  # 唤醒可能等待的线程
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        # 释放进程级文件锁
        self._port_lock.release()
        # 重置上下文
        self._ctx = DeviceContext()
        self._ctx.state = DeviceState.DISCONNECTED

    def enter_bootloader(self) -> None:
        """Ask the probe to switch to its UF2 bootloader and close CDC."""
        if self._ctx.state not in (DeviceState.READY, DeviceState.BUSY):
            raise ConnectionError("设备未就绪，请先连接命令端口")
        try:
            self.send_command_nowait("cmd.enter_bootloader()")
        finally:
            self.close()

    # ------------------------------------------------------------------
    # 命令发送
    # ------------------------------------------------------------------
    def send_command(
        self,
        cmd: str,
        timeout: float = 5.0,
        echo: bool = False,
        echo_prefix: str = "[SERIAL] ",
        on_output: Callable[[str], None] | None = None,
        stream_state: DeviceState | None = None,
    ) -> str:
        """发送 PikaScript 命令，等待 >>> 提示符后返回完整响应。"""
        with self._cmd_lock:
            if self._transport_error is not None:
                self._ctx.state = DeviceState.ERROR
                raise ConnectionError(
                    f"串口连接已失效，无法执行命令: {cmd}"
                ) from self._transport_error
            if self._ctx.state not in (DeviceState.READY, DeviceState.BUSY):
                raise ConnectionError(
                    f"设备未就绪，当前状态: {self._ctx.state.value}。请先连接设备。"
                )

            if stream_state is not None:
                self._arm_stream(stream_state)
            self._prompt_event.clear()
            with self._buffer_lock:
                self._response_buffer.clear()
            self._echo_enabled = echo
            self._echo_prefix = echo_prefix
            self._echo_offset = 0
            self._echo_pending = ""
            self._echo_callback = on_output
            stream_output = echo or on_output is not None

            if echo:
                print(f"[TX] {cmd}", flush=True)

            try:
                self._serial.write((cmd + "\n").encode("utf-8"))
            except serial.SerialException as e:
                self._transport_error = e
                self._ctx.state = DeviceState.ERROR
                self._echo_enabled = False
                self._echo_callback = None
                raise ConnectionError(f"写入串口失败: {e}") from e

            deadline = time.monotonic() + timeout
            while True:
                if self._prompt_event.wait(timeout=0.005):
                    break
                if stream_output:
                    self._flush_echo_buffer()
                if time.monotonic() >= deadline:
                    self._ctx.state = DeviceState.ERROR
                    if stream_output:
                        self._flush_echo_buffer(final=True)
                    self._echo_enabled = False
                    self._echo_callback = None
                    raise TimeoutError(f"命令超时 ({timeout}s): {cmd}")

            if self._transport_error is not None or self._ctx.state is DeviceState.ERROR:
                self._echo_enabled = False
                self._echo_callback = None
                error = self._transport_error
                raise ConnectionError(
                    f"命令期间串口读取失败: {cmd}"
                ) from error

            if stream_output:
                self._flush_echo_buffer(final=True)
            if echo:
                print("[RX] <<<", flush=True)

            with self._buffer_lock:
                response = "".join(self._response_buffer)
            self._echo_enabled = False
            self._echo_callback = None
            return response

    def send_command_nowait(self, cmd: str) -> None:
        """Send one REPL command without waiting for the next prompt.

        This is reserved for commands such as ``reboot()`` that intentionally
        restart the probe and therefore cannot produce a reliable trailing
        prompt.  The serial write is flushed before returning so callers may
        immediately close the bridge and release its process/HIL locks.
        """
        with self._cmd_lock:
            if self._ctx.state not in (DeviceState.READY, DeviceState.BUSY):
                raise ConnectionError(
                    f"设备未就绪，当前状态: {self._ctx.state.value}。请先连接设备。"
                )
            if self._serial is None or not self._serial.is_open:
                raise ConnectionError("设备串口未打开。请先连接设备。")
            try:
                self._serial.write((cmd + "\n").encode("utf-8"))
                self._serial.flush()
            except serial.SerialException as e:
                self._transport_error = e
                self._ctx.state = DeviceState.ERROR
                raise ConnectionError(f"写入串口失败: {e}") from e

    def send_script(self, commands: list[str]) -> list[str]:
        """批量发送命令序列，每条等待完成。"""
        results = []
        for cmd in commands:
            results.append(self.send_command(cmd))
        return results

    # ------------------------------------------------------------------
    # 流式读取
    # ------------------------------------------------------------------
    def read_stream(self, duration: float = 10.0) -> str:
        """兼容文本流读取，按 UTF-8 增量解码 RTT 原始字节。"""
        raw = self.read_stream_bytes(duration=duration)
        return self._utf8_decoder.decode(raw, final=False)

    def read_stream_bytes(self, duration: float = 10.0) -> bytes:
        """读取 RTT 原始字节，持续指定时长。"""
        collected: list[bytes] = []
        deadline = time.monotonic() + duration

        while time.monotonic() < deadline and self._running:
            with self._buffer_lock:
                parts = self._response_buffer
                chunk = b"".join(
                    part if isinstance(part, bytes) else part.encode("utf-8")
                    for part in parts
                )
                self._response_buffer.clear()
            if chunk:
                collected.append(chunk)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(_STREAM_READ_POLL_INTERVAL, remaining))

        return b"".join(collected)

    def stop_stream(self) -> str:
        """停止当前流式读取，返回剩余数据并恢复 READY 状态。"""
        with self._buffer_lock:
            parts = self._response_buffer
            if parts and isinstance(parts[0], bytes):
                remaining = self._utf8_decoder.decode(b"".join(parts), final=True)
            else:
                remaining = "".join(parts)
            self._response_buffer.clear()
        self._utf8_decoder.reset()  # 停止流：丢弃可能残留的半字符
        self._ctx.state = DeviceState.READY
        return remaining

    # ------------------------------------------------------------------
    # FLM 管理
    # ------------------------------------------------------------------
    def require_flm_loaded(
        self, mcu_profile: dict | None = None, timeout: float = FLM_LOAD_TIMEOUT
    ) -> bool:
        """检查 FLM 是否已加载，未加载则自动加载。"""
        if self._ctx.flm_loaded:
            return True

        if mcu_profile is None:
            raise ValueError("未指定 MCU 配置，无法加载 FLM")

        flm_path = self._safe_path(mcu_profile.get("flm_path", ""))
        flash_base = mcu_profile.get("flash_base", "0x08000000")
        ram_base = mcu_profile.get("ram_base", "0x20000000")

        # 验证地址格式
        if not self._validate_addr(flash_base):
            raise ValueError(f"无效的 flash_base: {flash_base}")
        if not self._validate_addr(ram_base):
            raise ValueError(f"无效的 ram_base: {ram_base}")

        cmd = f"load.flm({quote_probe_string(flm_path)},{flash_base},{ram_base})"
        resp = self.send_command(cmd, timeout=timeout)

        # load.flm 返回 0 表示成功
        for line in resp.strip().split("\n"):
            line = line.strip()
            if line == "0":
                self._ctx.flm_loaded = True
                self._ctx.current_mcu = mcu_profile.get("name", "")
                return True

        print(f"[FAIL] FLM 加载失败: {resp.strip()}")
        print("请检查 FLM 文件路径和 MCU 配置")
        return False

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def state(self) -> DeviceState:
        return self._ctx.state

    @property
    def idcode(self) -> int:
        return self._ctx.idcode

    @property
    def flm_loaded(self) -> bool:
        return self._ctx.flm_loaded

    @property
    def current_mcu(self) -> str:
        return self._ctx.current_mcu

    # ------------------------------------------------------------------
    # 流模式控制（供 RTTSession 等使用，避免直接访问 _ctx/_serial）
    # ------------------------------------------------------------------
    def _arm_stream(self, state: DeviceState) -> None:
        """Preserve bytes arriving after a command's prompt for a new stream."""
        with self._buffer_lock:
            self._armed_stream_state = state
            self._armed_stream_tail.clear()
            self._armed_prompt_prefix = b""
            self._armed_prompt_seen = False

    def _cancel_stream_arm(self) -> None:
        """Cancel a pending command-to-stream transition."""
        with self._buffer_lock:
            self._armed_stream_state = None
            self._armed_stream_tail.clear()
            self._armed_prompt_prefix = b""
            self._armed_prompt_seen = False

    def _try_enter_stream(self, state: DeviceState) -> bool:
        """切换到流模式，并保留启动提示符之后已经到达的流字节。"""
        with self._buffer_lock:
            if self._ctx.state is not DeviceState.READY:
                return False
            pending = []
            armed_state = getattr(self, "_armed_stream_state", None)
            prompt_seen = getattr(self, "_armed_prompt_seen", False)
            armed_tail = getattr(self, "_armed_stream_tail", None)
            if armed_state is state and prompt_seen and armed_tail is not None:
                pending = list(armed_tail)
            self._response_buffer.clear()
            self._response_buffer.extend(pending)
            self._armed_stream_state = None
            if armed_tail is None:
                self._armed_stream_tail = []
            else:
                armed_tail.clear()
            self._armed_prompt_prefix = b""
            self._armed_prompt_seen = False
            self._ctx.state = state
        self._utf8_decoder.reset()  # 新流会话从干净状态开始

        return True

    def _enter_stream(self, state: DeviceState) -> None:
        """Atomically enter a stream without overwriting an active stream."""
        if self._try_enter_stream(state):
            return
        raise RuntimeError(
            f"cannot enter {state.value}; bridge state is {self._ctx.state.value}"
        )

    def _capture_armed_stream_tail(self, data: bytes) -> bytes:
        """Capture raw bytes after the prompt and return command-mode bytes.

        The reader still needs to see the prompt itself so it can complete the
        waiting command.  Bytes after that prompt must never pass through the
        UTF-8 command decoder: they are already stream payload and retaining
        them in the reader's command ``line_buf`` would pollute the next REPL
        response after the stream stops.
        """
        prompt = PROMPT.encode("ascii")
        with self._buffer_lock:
            if self._armed_stream_state is None:
                return data
            if self._armed_prompt_seen:
                self._armed_stream_tail.append(data)
                return b""

            combined = self._armed_prompt_prefix + data
            prompt_index = combined.find(prompt)
            if prompt_index >= 0:
                command_end = max(
                    0,
                    prompt_index + len(prompt) - len(self._armed_prompt_prefix),
                )
                command_data = data[:command_end]
                tail = data[command_end:]
                if tail:
                    self._armed_stream_tail.append(tail)
                self._armed_prompt_prefix = b""
                self._armed_prompt_seen = True
                return command_data

            if combined.endswith(prompt[:2]):
                self._armed_prompt_prefix = prompt[:2]
            elif combined.endswith(prompt[:1]):
                self._armed_prompt_prefix = prompt[:1]
            else:
                self._armed_prompt_prefix = b""
            return data

    def _exit_stream(self) -> str:
        """退出流模式并返回剩余缓冲数据。

        A stream-state transition can be repaired locally, but a reader or
        writer transport failure cannot.  Keep such a bridge in ``ERROR`` so
        callers do not reuse a disconnected serial session as ``READY``.
        """
        with self._buffer_lock:
            parts = self._response_buffer
            if parts and isinstance(parts[0], bytes):
                remaining = self._utf8_decoder.decode(b"".join(parts), final=True)
            else:
                remaining = "".join(parts)
            self._response_buffer.clear()
        self._utf8_decoder.reset()  # 退出流：丢弃可能残留的半字符
        self._ctx.state = (
            DeviceState.ERROR
            if self._transport_error is not None
            else DeviceState.READY
        )
        return remaining

    def _recover_failed_stream_start(self, stop_command: bytes) -> bool:
        """Boundedly recover command mode after a stream start timed out.

        A raw stop may return its ``>>>`` well after the write.  Returning as
        soon as that write completes lets the stale prompt satisfy the next
        unrelated ``send_command``.  Consume a prompt from at most two stop
        attempts, then send exactly one identity command and keep consuming
        prompts until the response carrying the identity token arrives.  Since
        serial commands are ordered, that verified response also drains every
        earlier stop reply before this method reports READY.
        """
        with self._cmd_lock:
            self._cancel_stream_arm()
            if self._transport_error is not None:
                self._ctx.state = DeviceState.ERROR
                return False
            self._utf8_decoder.reset()
            self._ctx.state = DeviceState.READY
            with self._buffer_lock:
                self._response_buffer.clear()
                self._prompt_event.clear()

            stop_prompt_seen = False
            for _attempt in range(_FAILED_STREAM_STOP_ATTEMPTS):
                if self._transport_error is not None:
                    self._ctx.state = DeviceState.ERROR
                    return False
                with self._buffer_lock:
                    self._response_buffer.clear()
                    self._prompt_event.clear()
                try:
                    self._serial.write(stop_command)
                except serial.SerialException as error:
                    self._transport_error = error
                    self._ctx.state = DeviceState.ERROR
                    return False
                except AttributeError:
                    break
                if self._prompt_event.wait(timeout=_FAILED_STREAM_STOP_TIMEOUT):
                    if self._transport_error is not None:
                        self._ctx.state = DeviceState.ERROR
                        return False
                    stop_prompt_seen = True
                    break

            if not stop_prompt_seen:
                self._ctx.state = DeviceState.ERROR
                return False

            # Do not use send_command here: a delayed prompt from the second
            # bounded stop attempt could complete it before its own response.
            # One directly-written identity command lets us distinguish and
            # consume such stale prompts without emitting further commands.
            with self._buffer_lock:
                self._response_buffer.clear()
                self._prompt_event.clear()
            try:
                self._serial.write(
                    (MKLINK_IDENTITY_COMMAND + "\n").encode("utf-8")
                )
            except serial.SerialException as error:
                self._transport_error = error
                self._ctx.state = DeviceState.ERROR
                return False
            except AttributeError:
                self._ctx.state = DeviceState.ERROR
                return False

            deadline = time.monotonic() + _FAILED_STREAM_IDENTITY_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._prompt_event.wait(timeout=remaining):
                    self._ctx.state = DeviceState.ERROR
                    return False
                with self._buffer_lock:
                    response = "".join(
                        part.decode("utf-8", errors="replace")
                        if isinstance(part, bytes)
                        else part
                        for part in self._response_buffer
                    )
                    self._prompt_event.clear()
                if self._transport_error is not None:
                    self._ctx.state = DeviceState.ERROR
                    return False
                if any(
                    line.strip() == MKLINK_IDENTITY_TOKEN
                    for line in response.splitlines()
                ):
                    with self._buffer_lock:
                        self._response_buffer.clear()
                    self._utf8_decoder.reset()
                    self._ctx.state = DeviceState.READY
                    return True

    def drain_stream_bytes(self, max_bytes: int | None = None) -> bytes:
        """读取并清空 VOFA / SystemView 二进制流缓冲区。

        仅在 VOFA_STREAM / DUMP_STREAM / SYSTEMVIEW_STREAM 状态下调用。
        """
        if self._ctx.state not in (
            DeviceState.VOFA_STREAM,
            DeviceState.DUMP_STREAM,
            DeviceState.SYSTEMVIEW_STREAM,
        ):
            raise RuntimeError(
                "drain_stream_bytes() 仅在 VOFA_STREAM / DUMP_STREAM / "
                "SYSTEMVIEW_STREAM 状态下可用"
            )
        if max_bytes is not None:
            max_bytes = max(0, int(max_bytes))

        with self._buffer_lock:
            chunks = self._response_buffer
            if not chunks or not isinstance(chunks[0], bytes):
                return b""

            if max_bytes is None:
                out = list(chunks)
                chunks.clear()
                return b"".join(out)

            out: list[bytes] = []
            remaining = max_bytes
            while chunks and remaining > 0:
                chunk = chunks.pop(0)
                if len(chunk) <= remaining:
                    out.append(chunk)
                    remaining -= len(chunk)
                    continue
                out.append(chunk[:remaining])
                chunks.insert(0, chunk[remaining:])
                remaining = 0
            return b"".join(out)

    def _write_raw(self, data: bytes) -> None:
        """直接写入串口（用于 RTT DownBuffer 等场景）。"""
        if self._transport_error is not None:
            self._ctx.state = DeviceState.ERROR
            raise ConnectionError("串口连接已失效，无法写入原始数据") from (
                self._transport_error
            )
        try:
            self._serial.write(data)
        except serial.SerialException as error:
            self._transport_error = error
            self._ctx.state = DeviceState.ERROR
            raise ConnectionError(f"写入串口失败: {error}") from error

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _reader_loop(self):
        """后台读线程：持续从串口读取，检测 >>> 提示符。"""
        line_buf = ""

        while self._running:
            try:
                # ``Serial.read(4096)`` waits for all 4096 bytes or the 10 ms
                # timeout. Read exactly what the driver already has; when it
                # is empty, block for one byte and drain the remainder on the
                # next iteration. This removes timeout-sized aggregation from
                # low/medium-rate binary streams without busy polling.
                waiting = self._serial.in_waiting
                read_size = min(65536, waiting) if isinstance(waiting, int) and waiting > 0 else 1
                data = self._serial.read(read_size)
            except serial.SerialException as error:
                if self._running:
                    self._transport_error = error
                    self._ctx.state = DeviceState.ERROR
                    self._prompt_event.set()  # 唤醒等待者
                break

            if not data:
                continue

            # A stream-producing command can return its final ``>>>`` prompt
            # and the first stream bytes in one USB read.  Preserve those raw
            # bytes (and anything received before _enter_stream runs) instead
            # of decoding them as command output and clearing them.
            data = self._capture_armed_stream_tail(data)
            if not data:
                continue

            # 流模式下不检测 >>>（RTT/SystemView/VOFA 数据可能包含 >>>）
            is_stream = self._ctx.state in (
                DeviceState.RTT_STREAM,
                DeviceState.SYSTEMVIEW_STREAM,
                DeviceState.VOFA_STREAM,
                DeviceState.DUMP_STREAM,
            )

            if is_stream:
                if self._ctx.state in (
                    DeviceState.VOFA_STREAM,
                    DeviceState.DUMP_STREAM,
                    DeviceState.SYSTEMVIEW_STREAM,
                ):
                    # VOFA/DumpMem/SystemView 二进制流：直接存 bytes，不 decode；
                    # 同时清空文本解码器残留，避免 text→binary→text 切换污染
                    self._utf8_decoder.reset()
                    with self._buffer_lock:
                        self._response_buffer.append(data)
                else:
                    # RTT 编码由上层会话选择；桥接层必须保留设备原始字节。
                    with self._buffer_lock:
                        self._response_buffer.append(data)
                line_buf = ""
                continue

            # 命令模式：增量 UTF-8 解码后检测 >>> 提示符
            text = self._utf8_decoder.decode(data, final=False)
            line_buf += text

            if PROMPT in line_buf:
                idx = line_buf.index(PROMPT)
                before = line_buf[:idx]
                # Anything after the prompt belongs to neither this completed
                # command nor a later command.  Stream-producing commands have
                # already split and preserved their raw tail above.
                line_buf = ""

                with self._buffer_lock:
                    self._response_buffer.append(before)
                self._prompt_event.set()
            else:
                # Commit only text that cannot be an incomplete prompt.  The
                # previous implementation appended the trailing ``>``/``>>``
                # immediately and returned those characters in the response
                # when the final ``>`` arrived in a later USB read.
                keep = (
                    2 if line_buf.endswith(">>")
                    else 1 if line_buf.endswith(">")
                    else 0
                )
                committed = line_buf[:-keep] if keep else line_buf
                with self._buffer_lock:
                    if committed:
                        self._response_buffer.append(committed)
                line_buf = line_buf[-keep:] if keep else ""

    def _flush_echo_buffer(self, final: bool = False) -> None:
        """将尚未输出的串口响应增量回显到终端。"""
        with self._buffer_lock:
            parts = self._response_buffer
            if parts and isinstance(parts[0], bytes):
                text = b"".join(parts).decode("utf-8", errors="replace")
            else:
                text = "".join(parts)

        if self._echo_offset >= len(text):
            if final and self._echo_pending:
                self._emit_echo_line(self._echo_pending)
                self._echo_pending = ""
            return

        chunk = text[self._echo_offset:]
        if not final:
            tail_keep = 0
            if chunk.endswith(">>"):
                tail_keep = 2
            elif chunk.endswith(">"):
                tail_keep = 1
            if tail_keep:
                chunk = chunk[:-tail_keep]

        if not chunk:
            return

        normalized = chunk.replace("\r\n", "\n").replace("\r", "\n")
        data = self._echo_pending + normalized
        lines = data.split("\n")
        if final:
            complete_lines = lines
            self._echo_pending = ""
        else:
            complete_lines = lines[:-1]
            self._echo_pending = lines[-1]

        for line in complete_lines:
            if not line:
                continue
            self._emit_echo_line(line)

        self._echo_offset += len(chunk)

    def _emit_echo_line(self, line: str) -> None:
        if self._echo_enabled:
            print(f"{self._echo_prefix}{line}", flush=True)
        if self._echo_callback is not None:
            try:
                self._echo_callback(line)
            except Exception:
                pass

    @staticmethod
    def _safe_path(path: str) -> str:
        """校验文件路径，仅允许安全字符。"""
        if not re.match(r'^[a-zA-Z0-9_./\-]+$', path):
            raise ValueError(f"不安全的文件路径: {path}")
        return path

    @staticmethod
    def _validate_addr(addr: str) -> bool:
        """验证地址是否为合法十六进制 (0x00000000 - 0xFFFFFFFF)。"""
        m = re.match(r'^0x[0-9a-fA-F]{1,8}$', addr)
        if not m:
            return False
        val = int(addr, 16)
        return 0 <= val <= 0xFFFFFFFF
