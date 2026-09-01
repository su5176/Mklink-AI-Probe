"""Modbus RTU 客户端 — pymodbus ModbusSerialClient 封装。"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable

from pymodbus.client import ModbusSerialClient
from pymodbus import FramerType, ModbusException


class ModbusError(Exception):
    """Modbus 操作失败。"""


class ModbusSlaveError(ModbusError):
    """从站返回异常响应。"""

    def __init__(self, slave: int, fc: int, response):
        self.slave = slave
        self.fc = fc
        self.response = response
        super().__init__(f"从站 {slave} 返回异常 (FC={fc:#04x}): {response}")


class _PortLock:
    """Cross-process advisory lock for one Modbus serial port."""

    _guard = threading.Lock()

    def __init__(self, port: str):
        safe_port = re.sub(r"[^A-Za-z0-9_.-]+", "_", port.upper())
        lock_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "mklink_modbus_locks")
        # A basename such as ``COM6.lock`` still resolves to the reserved
        # Windows device ``COM6``.  Prefix the filename so it is always a real
        # filesystem entry before applying the byte-range lock.
        self._path = os.path.join(lock_dir, f"port_{safe_port}.lock")
        self._fd = None
        self._locked = False

    def acquire(self) -> bool:
        if self._locked:
            return True
        with self._guard:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            # Use a binary descriptor for msvcrt.locking().  Text/append mode
            # produces EINVAL on real Windows hosts even with a materialized
            # byte, which made every port look permanently busy.
            self._fd = open(self._path, "a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    # Windows byte-range locks cannot reliably lock beyond EOF.
                    # A freshly created lock file is empty, which caused every
                    # first acquisition to be misclassified as "port busy" on
                    # real field hosts.  Materialize the byte before locking it.
                    self._fd.seek(0, os.SEEK_END)
                    if self._fd.tell() == 0:
                        self._fd.write(b"\0")
                        self._fd.flush()
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
            self._fd.write(str(os.getpid()).encode("ascii"))
            self._fd.flush()
            self._locked = True
            return True

    def release(self) -> None:
        if not self._locked or self._fd is None:
            return
        try:
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


class ModbusClient:
    """Modbus RTU 客户端，封装 pymodbus 同步串口通信。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        timeout: float = 1.0,
        retries: int = 3,
        handle_local_echo: bool = False,
        trace_packet: Callable[[bool, bytes], bytes] | None = None,
        trace_connect: Callable[[bool], None] | None = None,
    ):
        self._client = ModbusSerialClient(
            port=port,
            framer=FramerType.RTU,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout,
            retries=retries,
            handle_local_echo=handle_local_echo,
            trace_packet=trace_packet,
            trace_connect=trace_connect,
        )
        self._port = port
        self._lock = _PortLock(port)
        self._is_open = False

    def open(self) -> bool:
        """打开串口连接。"""
        if self._is_open:
            return True
        if not self._lock.acquire():
            print(f"[FAIL] Modbus 串口 {self._port} 已被 mklink 其他进程占用；不要并发访问同一串口")
            return False
        try:
            ok = self._client.connect()
            if not ok:
                self._lock.release()
                return False
            self._is_open = True
            return True
        except Exception as e:
            self._lock.release()
            print(f"[FAIL] 无法打开端口 {self._port}: {e}")
            return False

    def close(self) -> None:
        """关闭串口连接。"""
        if not self._is_open:
            self._lock.release()
            return
        try:
            self._client.close()
        finally:
            self._is_open = False
            self._lock.release()

    def _check(self, result, slave: int, fc: int):
        """检查 pymodbus 响应，异常时抛出 ModbusSlaveError。"""
        if isinstance(result, ModbusException):
            raise ModbusError(f"pymodbus 库异常: {result}")
        if result.isError():
            raise ModbusSlaveError(slave, fc, result)
        return result

    # ---- FC01: Read Coils ----
    def read_coils(self, address: int, count: int, slave: int) -> list[bool]:
        rr = self._check(
            self._client.read_coils(address, count=count, device_id=slave),
            slave, 0x01,
        )
        return rr.bits[:count]

    # ---- FC02: Read Discrete Inputs ----
    def read_discrete_inputs(self, address: int, count: int, slave: int) -> list[bool]:
        rr = self._check(
            self._client.read_discrete_inputs(address, count=count, device_id=slave),
            slave, 0x02,
        )
        return rr.bits[:count]

    # ---- FC03: Read Holding Registers ----
    def read_holding_registers(self, address: int, count: int, slave: int) -> list[int]:
        rr = self._check(
            self._client.read_holding_registers(address, count=count, device_id=slave),
            slave, 0x03,
        )
        return rr.registers

    # ---- FC04: Read Input Registers ----
    def read_input_registers(self, address: int, count: int, slave: int) -> list[int]:
        rr = self._check(
            self._client.read_input_registers(address, count=count, device_id=slave),
            slave, 0x04,
        )
        return rr.registers

    # ---- FC05: Write Single Coil ----
    def write_coil(self, address: int, value: bool, slave: int) -> None:
        self._check(
            self._client.write_coil(address, value, device_id=slave),
            slave, 0x05,
        )

    # ---- FC06: Write Single Register ----
    def write_register(self, address: int, value: int, slave: int) -> None:
        self._check(
            self._client.write_register(address, value, device_id=slave),
            slave, 0x06,
        )

    # ---- FC07: Read Exception Status ----
    def read_exception_status(self, slave: int) -> int:
        rr = self._check(
            self._client.read_exception_status(device_id=slave),
            slave, 0x07,
        )
        return rr.status

    # ---- FC15: Write Multiple Coils ----
    def write_coils(self, address: int, values: list[bool], slave: int) -> None:
        self._check(
            self._client.write_coils(address, values, device_id=slave),
            slave, 0x0F,
        )

    # ---- FC16: Write Multiple Registers ----
    def write_registers(self, address: int, values: list[int], slave: int) -> None:
        self._check(
            self._client.write_registers(address, values, device_id=slave),
            slave, 0x10,
        )

    # ---- FC22: Mask Write Register ----
    def mask_write_register(
        self, address: int, and_mask: int, or_mask: int, slave: int
    ) -> None:
        self._check(
            self._client.mask_write_register(
                address, and_mask=and_mask, or_mask=or_mask, device_id=slave
            ),
            slave, 0x16,
        )

    # ---- FC23: Read/Write Multiple Registers ----
    def read_write_registers(
        self,
        read_address: int,
        read_count: int,
        write_address: int,
        write_values: list[int],
        slave: int,
    ) -> list[int]:
        rr = self._check(
            self._client.readwrite_registers(
                read_address=read_address,
                read_count=read_count,
                write_address=write_address,
                values=write_values,
                device_id=slave,
            ),
            slave, 0x17,
        )
        return rr.registers

    # ---- 数据类型转换 ----
    def convert_from_registers(self, registers: list[int], data_type) -> int | float:
        """将 16 位寄存器列表转换为指定类型值。"""
        return self._client.convert_from_registers(registers, data_type=data_type)

    def convert_to_registers(self, value: int | float, data_type) -> list[int]:
        """将指定类型值转换为 16 位寄存器列表。"""
        return self._client.convert_to_registers(value, data_type=data_type)

    @property
    def DATATYPE(self):
        """pymodbus DATATYPE 枚举，用于类型转换。"""
        return self._client.DATATYPE

    @property
    def raw_client(self) -> ModbusSerialClient:
        """直接访问底层 pymodbus 客户端（用于诊断等高级操作）。"""
        return self._client
