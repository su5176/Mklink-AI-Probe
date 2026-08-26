"""串口通信封装 — 通用 UART 读写，非 MKLink 调试探针。"""

from __future__ import annotations

import os
import threading
from typing import Optional

import serial
import serial.tools.list_ports

from mklink.local_resources import serial_lock_path
from mklink.usb_interfaces import (
    MKLINK_COMMAND_INTERFACE,
    is_mklink_usb_port,
    usb_interface_number,
)


# ---------------------------------------------------------------------------
# Cross-process port lock (adapted from modbus/_client.py)
# ---------------------------------------------------------------------------
class _PortLock:
    """Cross-process advisory lock for one serial port."""

    _guard = threading.Lock()

    def __init__(self, port: str):
        self._path = serial_lock_path(port)
        self._fd: Optional[object] = None
        self._locked = False

    def acquire(self) -> bool:
        if self._locked:
            return True
        with self._guard:
            try:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                self._fd = open(self._path, "a+")
            except OSError:
                self._fd = None
                return False
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fd.seek(0)
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._fd.close()
                self._fd = None
                return False
            self._fd.seek(0)
            self._fd.truncate()
            self._fd.write(str(os.getpid()))
            self._fd.flush()
            self._locked = True
            return True

    def release(self) -> None:
        if not self._locked or self._fd is None:
            return
        try:
            self._fd.seek(0)
            self._fd.truncate()
            self._fd.write("0")
            self._fd.flush()
            if os.name == "nt":
                import msvcrt
                self._fd.seek(0)
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            self._fd.close()
            self._fd = None
            self._locked = False


# ---------------------------------------------------------------------------
# MKLink port detection
# ---------------------------------------------------------------------------
def is_mklink_port(port: str) -> bool:
    """判断指定 COM 口是否为 MKLink 命令接口。"""
    for info in serial.tools.list_ports.comports():
        if info.device.upper() != port.upper():
            continue
        return (
            is_mklink_usb_port(info)
            and usb_interface_number(info) == MKLINK_COMMAND_INTERFACE
        )
    return False


def list_uart_ports() -> list[dict]:
    """列出通用串口，排除 MKLink 的 MI_04 命令接口。"""
    results: list[dict] = []
    for info in serial.tools.list_ports.comports():
        is_command = (
            is_mklink_usb_port(info)
            and usb_interface_number(info) == MKLINK_COMMAND_INTERFACE
        )
        if not is_command:
            results.append({
                "device": info.device,
                "description": info.description or "",
                "is_mklink": False,
            })
    return results


# ---------------------------------------------------------------------------
# SerialPort
# ---------------------------------------------------------------------------
class SerialPort:
    """通用串口通信类，支持跨进程端口锁与线程安全读写。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        databits: int = 8,
        stopbits: int = 1,
        parity: str = "N",
        timeout: float = 0.05,
    ):
        self._port = port
        self._baudrate = baudrate
        self._databits = databits
        self._stopbits = stopbits
        self._parity = parity
        self._timeout = timeout

        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._port_lock = _PortLock(port)

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> bool:
        """获取端口锁并打开串口，成功返回 True。"""
        if self.is_open:
            return True
        if not self._port_lock.acquire():
            return False
        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=self._databits,
                stopbits=self._stopbits,
                parity=self._parity,
                timeout=self._timeout,
            )
            return True
        except serial.SerialException:
            self._port_lock.release()
            self._serial = None
            return False

    def close(self) -> None:
        """关闭串口并释放端口锁。"""
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
        self._port_lock.release()

    def write(self, data: bytes) -> None:
        """线程安全写入。"""
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.write(data)

    def read_available(self) -> bytes:
        """非阻塞读取所有可用字节。"""
        with self._lock:
            if not self._serial or not self._serial.is_open:
                return b""
            waiting = self._serial.in_waiting
            if waiting > 0:
                return self._serial.read(waiting)
            return b""

    def __enter__(self) -> "SerialPort":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
