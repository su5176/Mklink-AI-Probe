"""串口调试子包 — 通用 UART 收发、协议解析、多端口监控。"""

from __future__ import annotations

from mklink.serial._port import SerialPort, is_mklink_port, list_uart_ports
from mklink.serial._frame import FrameParser, ParsedFrame, compute_crc
from mklink.serial._profile import load_profile, save_profile, find_profile, validate_profile, ProfileError
from mklink.serial._autoreply import AutoReplyEngine, AutoReplyRule
from mklink.serial._logger import FileLogger
from mklink.serial._monitor import SerialMonitor, SerialEvent
from mklink.serial._cli_mode import CLIMode
from mklink.serial._dashboard import SerialDashboardServer
from mklink.serial._profile_from_c import generate_profile_from_c

__all__ = [
    "SerialPort", "is_mklink_port", "list_uart_ports",
    "FrameParser", "ParsedFrame", "compute_crc",
    "load_profile", "save_profile", "find_profile", "validate_profile", "ProfileError",
    "AutoReplyEngine", "AutoReplyRule",
    "FileLogger",
    "SerialMonitor", "SerialEvent",
    "CLIMode",
    "SerialDashboardServer",
    "generate_profile_from_c",
]
