"""MKLink Modbus RTU 调试模块 — 基于 pymodbus。"""

from mklink.modbus._client import ModbusClient
from mklink.modbus._format import format_registers, parse_register_spec
from mklink.modbus._scanner import scan_slaves
from mklink.modbus._poller import poll_registers, RegisterSpec
from mklink.modbus._monitor import monitor_traffic
from mklink.modbus._profile import load_profile
from mklink.modbus._dashboard import ModbusDashboardServer

__all__ = [
    "ModbusClient",
    "format_registers",
    "parse_register_spec",
    "scan_slaves",
    "poll_registers",
    "RegisterSpec",
    "monitor_traffic",
    "load_profile",
    "ModbusDashboardServer",
]

