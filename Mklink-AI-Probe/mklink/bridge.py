"""
MKLink Serial Bridge — 核心串口通信类。

依赖: pyserial
内部依赖: mklink._types
"""

from __future__ import annotations

import atexit
import os
import re
import sys
import threading
import time

import serial

from mklink._types import (
    DEFAULT_BAUDRATE,
    FLM_LOAD_TIMEOUT,
    PROMPT,
    SYNC_RETRIES,
    DeviceContext,
    DeviceState,
)

# 流模式停止命令 — RTT 和 VOFA 互斥，恢复时盲发两个是安全的
_STREAM_STOP_COMMANDS = [
    b"RTTView.stop()\n",
    b'vofa.send(0x20000000, "uint8_t", 0)\n',
    b"cmd.dump_memory(0x20000054, 4, 0)\n",
]


# ---------------------------------------------------------------------------
# 进程级串口互斥锁（文件锁）
# ---------------------------------------------------------------------------
class SerialLock:
    """基于文件的进程级互斥锁，防止多个 CLI 进程同时操作串口。

    锁文件位于 %TEMP%/mklink_serial_lock，内容为持有者 PID。
    支持 stale lock 检测：若持有进程已退出则自动释放旧锁。
    """

    _LOCK_PATH = os.path.join(os.environ.get("TEMP", "/tmp"), "mklink_serial_lock")

    def __init__(self) -> None:
        self._fd = None
        self._locked = False

    def acquire(self, timeout: float = 0.0) -> bool:
        """尝试获取锁。timeout=0 为非阻塞。返回是否成功。"""
        try:
            # 检测 stale lock：如果锁文件存在且持有者进程已退出，清除旧锁
            if os.path.exists(self._LOCK_PATH):
                try:
                    with open(self._LOCK_PATH, "r") as f:
                        old_pid = f.read().strip()
                    if old_pid.isdigit():
                        old_pid_int = int(old_pid)
                        # Windows 上检查进程是否存在
                        try:
                            import ctypes
                            kernel32 = ctypes.windll.kernel32
                            handle = kernel32.OpenProcess(0x100000, False, old_pid_int)
                            if handle == 0:
                                # 进程不存在，清除 stale lock
                                os.remove(self._LOCK_PATH)
                            else:
                                kernel32.CloseHandle(handle)
                        except Exception:
                            pass
                except (OSError, ValueError):
                    pass

            self._fd = open(self._LOCK_PATH, "w")
            self._fd.write(str(os.getpid()))
            self._fd.flush()

            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    self._fd.close()
                    self._fd = None
                    return False
            else:
                import fcntl
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self._fd.close()
                    self._fd = None
                    return False

            self._locked = True
            return True
        except OSError:
            if self._fd:
                self._fd.close()
                self._fd = None
            return False

    def release(self) -> None:
        if not self._locked or self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fd.close()
            self._fd = None
            self._locked = False
            try:
                os.remove(self._LOCK_PATH)
            except OSError:
                pass

    @property
    def locked(self) -> bool:
        return self._locked


# 模块级锁实例，所有 bridge 共享
_serial_lock = SerialLock()


def _cleanup_lock() -> None:
    _serial_lock.release()


atexit.register(_cleanup_lock)


class MKLinkSerialBridge:
    """通过虚拟串口与 MKLink 烧录器通信的桥接类。"""

    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE):
        self._port = port
        self._baudrate = baudrate
        self._serial: serial.Serial | None = None  # 延迟到 connect() 中打开
        self._ctx = DeviceContext()
        self._reader_thread: threading.Thread | None = None
        self._running = False
        self._response_buffer: list[str] = []
        self._prompt_event = threading.Event()
        self._buffer_lock = threading.Lock()
        self._cmd_lock = threading.Lock()  # 命令级互斥，防止并发操作
        self._echo_enabled = False
        self._echo_prefix = "[SERIAL] "
        self._echo_offset = 0
        self._echo_pending = ""

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """打开串口并同步设备状态（等待 >>> 提示符）。"""
        # 进程级互斥：获取文件锁
        if not _serial_lock.acquire():
            print(f"[FAIL] 串口正被其他进程使用（锁文件: {SerialLock._LOCK_PATH}）")
            print("       请等待其他操作完成，或关闭占用串口的进程后重试。")
            return False

        try:
            self._serial = serial.Serial(self._port, self._baudrate, timeout=0.01)
        except serial.SerialException as e:
            _serial_lock.release()
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

        # 发送空行同步，重试 SYNC_RETRIES 次
        for attempt in range(1, SYNC_RETRIES + 1):
            self._prompt_event.clear()
            with self._buffer_lock:
                self._response_buffer.clear()
            self._serial.write(b"\n")

            if self._prompt_event.wait(timeout=2.0):
                self._ctx.state = DeviceState.READY
                return True

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
                _serial_lock.release()
                return False

        try:
            # 排空缓冲区，盲发停止命令
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            for stop_cmd in _STREAM_STOP_COMMANDS:
                try:
                    self._serial.write(stop_cmd)
                except serial.SerialException:
                    break
            time.sleep(0.5)
            self._serial.reset_input_buffer()

            # 重启 reader 线程，最后尝试一次握手
            self._running = True
            self._ctx.state = DeviceState.CONNECTING
            self._response_buffer.clear()
            self._prompt_event.clear()
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self._reader_thread.start()

            self._serial.write(b"\n")
            if self._prompt_event.wait(timeout=3.0):
                self._ctx.state = DeviceState.READY
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
        _serial_lock.release()
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
        _serial_lock.release()
        # 重置上下文
        self._ctx = DeviceContext()
        self._ctx.state = DeviceState.DISCONNECTED

    # ------------------------------------------------------------------
    # 命令发送
    # ------------------------------------------------------------------
    def send_command(
        self,
        cmd: str,
        timeout: float = 5.0,
        echo: bool = False,
        echo_prefix: str = "[SERIAL] ",
    ) -> str:
        """发送 PikaScript 命令，等待 >>> 提示符后返回完整响应。"""
        with self._cmd_lock:
            if self._ctx.state not in (DeviceState.READY, DeviceState.BUSY):
                raise ConnectionError(
                    f"设备未就绪，当前状态: {self._ctx.state.value}。请先连接设备。"
                )

            self._prompt_event.clear()
            with self._buffer_lock:
                self._response_buffer.clear()
            self._echo_enabled = echo
            self._echo_prefix = echo_prefix
            self._echo_offset = 0
            self._echo_pending = ""

            if echo:
                print(f"[TX] {cmd}", flush=True)

            try:
                self._serial.write((cmd + "\n").encode("utf-8"))
            except serial.SerialException as e:
                self._ctx.state = DeviceState.ERROR
                self._echo_enabled = False
                raise ConnectionError(f"写入串口失败: {e}") from e

            deadline = time.monotonic() + timeout
            while True:
                if self._prompt_event.wait(timeout=0.005):
                    break
                if echo:
                    self._flush_echo_buffer()
                if time.monotonic() >= deadline:
                    self._ctx.state = DeviceState.ERROR
                    if echo:
                        self._flush_echo_buffer(final=True)
                    self._echo_enabled = False
                    raise TimeoutError(f"命令超时 ({timeout}s): {cmd}")

            if echo:
                self._flush_echo_buffer(final=True)
                print("[RX] <<<", flush=True)

            with self._buffer_lock:
                response = "".join(self._response_buffer)
            self._echo_enabled = False
            return response

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
        """流式读取（RTT/SystemView/VOFA），持续指定时长。"""
        collected: list[str] = []
        deadline = time.monotonic() + duration

        while time.monotonic() < deadline and self._running:
            with self._buffer_lock:
                chunk = "".join(self._response_buffer)
                self._response_buffer.clear()
            if chunk:
                collected.append(chunk)
            time.sleep(0.05)

        return "".join(collected)

    def stop_stream(self) -> str:
        """停止当前流式读取，返回剩余数据并恢复 READY 状态。"""
        with self._buffer_lock:
            remaining = "".join(self._response_buffer)
            self._response_buffer.clear()
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

        cmd = f'load.flm("{flm_path}",{flash_base},{ram_base})'
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
    def _enter_stream(self, state: DeviceState) -> None:
        """切换到流模式（RTT/SystemView/VOFA）。清空缓冲区避免残留数据泄漏到流中。"""
        with self._buffer_lock:
            self._response_buffer.clear()
        self._ctx.state = state

    def _exit_stream(self) -> str:
        """退出流模式，恢复 READY，返回剩余缓冲数据。"""
        with self._buffer_lock:
            parts = self._response_buffer
            if parts and isinstance(parts[0], bytes):
                remaining = b"".join(parts).decode("utf-8", errors="replace")
            else:
                remaining = "".join(parts)
            self._response_buffer.clear()
        self._ctx.state = DeviceState.READY
        return remaining

    def drain_stream_bytes(self) -> bytes:
        """读取并清空 VOFA 二进制流缓冲区。

        仅在 DeviceState.VOFA_STREAM 状态下调用。
        """
        if self._ctx.state not in (DeviceState.VOFA_STREAM, DeviceState.DUMP_STREAM):
            raise RuntimeError("drain_stream_bytes() 仅在 VOFA_STREAM 或 DUMP_STREAM 状态下可用")
        with self._buffer_lock:
            chunks = list(self._response_buffer)
            self._response_buffer.clear()
        return b"".join(chunks) if chunks and isinstance(chunks[0], bytes) else b""

    def _write_raw(self, data: bytes) -> None:
        """直接写入串口（用于 RTT DownBuffer 等场景）。"""
        self._serial.write(data)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _reader_loop(self):
        """后台读线程：持续从串口读取，检测 >>> 提示符。"""
        line_buf = ""

        while self._running:
            try:
                data = self._serial.read(4096)
            except serial.SerialException:
                if self._running:
                    self._ctx.state = DeviceState.ERROR
                    self._prompt_event.set()  # 唤醒等待者
                break

            if not data:
                continue

            text = data.decode("utf-8", errors="replace")
            line_buf += text

            # 流模式下不检测 >>>（RTT/SystemView/VOFA 数据可能包含 >>>）
            is_stream = self._ctx.state in (
                DeviceState.RTT_STREAM,
                DeviceState.SYSTEMVIEW_STREAM,
                DeviceState.VOFA_STREAM,
                DeviceState.DUMP_STREAM,
            )

            if is_stream:
                if self._ctx.state in (DeviceState.VOFA_STREAM, DeviceState.DUMP_STREAM):
                    # VOFA/DumpMem 二进制流：直接存 bytes，不 decode
                    with self._buffer_lock:
                        self._response_buffer.append(data)
                else:
                    # RTT/SystemView 文本流：存 str
                    with self._buffer_lock:
                        self._response_buffer.append(text)
                line_buf = ""
                continue

            # 命令模式：检测 >>> 提示符
            if PROMPT in line_buf:
                idx = line_buf.index(PROMPT)
                before = line_buf[:idx]
                line_buf = line_buf[idx + len(PROMPT):]

                with self._buffer_lock:
                    self._response_buffer.append(before)
                self._prompt_event.set()
            else:
                # 缓冲输出，但保留尾部可能的不完整 >>>
                # 处理 >>> 跨 read 分割的情况（如 >> + >）
                with self._buffer_lock:
                    self._response_buffer.append(text)

                # 保留尾部可能的不完整提示符
                if line_buf.endswith(">"):
                    line_buf = line_buf[-len(PROMPT):]  # 保留最多 len(>>>) 字符
                elif line_buf.endswith(">>"):
                    line_buf = line_buf[-len(PROMPT):]
                else:
                    line_buf = ""

    def _flush_echo_buffer(self, final: bool = False) -> None:
        """将尚未输出的串口响应增量回显到终端。"""
        with self._buffer_lock:
            parts = self._response_buffer
            if parts and isinstance(parts[0], bytes):
                text = b"".join(parts).decode("utf-8", errors="replace")
            else:
                text = "".join(parts)

        if self._echo_offset >= len(text):
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
            print(f"{self._echo_prefix}{line}", flush=True)

        self._echo_offset += len(chunk)

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
