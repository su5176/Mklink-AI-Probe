"""
MKLink Serial Bridge — 共享常量与类型定义。

零外部依赖，可被所有模块安全导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PROMPT = ">>>"
KNOWN_MKLINK_VID_PIDS: list[tuple[int, int]] = []  # 已知 VID/PID
SYNC_RETRIES = 3
IDCODE_RETRY_INTERVAL_MS = 500
IDCODE_TIMEOUT_MS = 10000
FLM_LOAD_TIMEOUT = 300.0
DEFAULT_BAUDRATE = 115200


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
class DeviceState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    BUSY = "busy"
    RTT_STREAM = "rtt_stream"
    SYSTEMVIEW_STREAM = "systemview_stream"
    VOFA_STREAM = "vofa_stream"
    DUMP_STREAM = "dump_stream"
    ERROR = "error"


# ---------------------------------------------------------------------------
# 设备上下文
# ---------------------------------------------------------------------------
@dataclass
class DeviceContext:
    state: DeviceState = DeviceState.DISCONNECTED
    flm_loaded: bool = False
    current_mcu: str = ""
    idcode: int = 0
    swd_clock_hz: int = 0
