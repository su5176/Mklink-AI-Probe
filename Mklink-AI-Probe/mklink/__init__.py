"""
MKLink Serial Bridge — 模块化 Python 包。

纯模块在导入时立即可用（无需 pyserial）。
pyserial 依赖的模块通过 lazy __getattr__ 按需加载。
"""

from __future__ import annotations

# 纯模块（无需 pyserial）
from mklink._deps import check_dependencies, require_dependencies
from mklink._types import (
    DEFAULT_BAUDRATE,
    FLM_LOAD_TIMEOUT,
    KNOWN_MKLINK_VID_PIDS,
    PROMPT,
    SYNC_RETRIES,
    IDCODE_RETRY_INTERVAL_MS,
    IDCODE_TIMEOUT_MS,
    DeviceContext,
    DeviceState,
)
from mklink.autostart import generate_autostart_config
from mklink.keil_parser import find_uvprojx, parse_uvprojx
from mklink.profiles import load_mcu_profiles, match_mcu_by_idcode, match_mcu_by_device
from mklink.project_config import (
    load_config, save_config,
    load_keil_project, save_keil_project,
    load_rtt_config, save_rtt_config,
    is_configured, ensure_mklink_dir,
    check_project_config, ProjectConfigStatus,
    format_config_status,
    ensure_rtt_config_updated,
)
from mklink.rtt_addr import find_rtt_addr_from_map
from mklink.rtt_integration import (
    check_rtt_in_project, integrate_rtt_sources,
    check_rtt_sources_bundled, generate_rtt_usage_example,
)
from mklink.utils import (
    format_idcode,
    format_progress_bar,
    format_rtt_info,
    hex_dump,
    parse_download_progress,
    parse_load_result,
)


# SDK API (lazy — depends on pyserial via Device internals)
def __getattr__(name: str):
    """延迟加载 pyserial 依赖模块。"""
    if name == "Device":
        from mklink.device import Device
        return Device
    if name == "connect":
        from mklink.device import connect
        return connect
    if name == "discover_all":
        from mklink.device import discover_all
        return discover_all
    if name == "DeviceError":
        from mklink.device import DeviceError
        return DeviceError
    if name == "DeviceNotConnectedError":
        from mklink.device import DeviceNotConnectedError
        return DeviceNotConnectedError
    if name == "HardFaultReport":
        from mklink.device import HardFaultReport
        return HardFaultReport
    if name == "MKLinkSerialBridge":
        from mklink.bridge import MKLinkSerialBridge
        return MKLinkSerialBridge
    if name == "RTTSession":
        from mklink.rtt import RTTSession
        return RTTSession
    if name == "MKLinkFlash":
        from mklink.flash import MKLinkFlash
        return MKLinkFlash
    if name == "burn_hex_file":
        from mklink.flash import burn_hex_file
        return burn_hex_file
    if name in ("find_mklink_cdc_port", "list_available_ports",
                "find_microkeen_disk", "get_microkeen_flm_path", "check_flm_on_microkeen",
                "resolve_keil_flm_path", "copy_flm_to_microkeen"):
        from mklink import discovery
        return getattr(discovery, name)
    if name == "serve":
        from mklink.remote.server import serve
        return serve
    if name == "connect_remote":
        from mklink.remote.client import connect_remote
        return connect_remote
    raise AttributeError(f"module 'mklink' has no attribute {name!r}")


__all__ = [
    # SDK API (lazy)
    "Device", "connect", "discover_all",
    "DeviceError", "DeviceNotConnectedError", "HardFaultReport",
    # Remote (lazy)
    "serve", "connect_remote",
    # 依赖检查
    "check_dependencies", "require_dependencies",
    # 类型和常量
    "PROMPT", "KNOWN_MKLINK_VID_PIDS", "SYNC_RETRIES",
    "IDCODE_RETRY_INTERVAL_MS", "IDCODE_TIMEOUT_MS", "FLM_LOAD_TIMEOUT",
    "DEFAULT_BAUDRATE",
    "DeviceState", "DeviceContext",
    # 核心类（lazy）
    "MKLinkSerialBridge", "RTTSession", "MKLinkFlash",
    # 工具函数
    "parse_download_progress", "parse_load_result",
    "hex_dump",
    "format_progress_bar", "format_rtt_info", "format_idcode",
    # 端口发现（lazy）
    "find_mklink_cdc_port", "list_available_ports",
    # MICROKEEN 磁盘（lazy）
    "find_microkeen_disk", "get_microkeen_flm_path", "check_flm_on_microkeen",
    "resolve_keil_flm_path", "copy_flm_to_microkeen",
    # RTT 工具
    "find_rtt_addr_from_map", "generate_autostart_config",
    # RTT 集成
    "check_rtt_in_project", "integrate_rtt_sources",
    "check_rtt_sources_bundled", "generate_rtt_usage_example",
    # MCU 配置
    "load_mcu_profiles", "match_mcu_by_idcode", "match_mcu_by_device",
    # Keil 工程解析
    "find_uvprojx", "parse_uvprojx",
    # 项目配置
    "load_config", "save_config",
    "load_keil_project", "save_keil_project",
    "load_rtt_config", "save_rtt_config",
    "is_configured", "ensure_mklink_dir",
    "check_project_config", "ProjectConfigStatus",
    "format_config_status",
    "ensure_rtt_config_updated",
    # 烧录功能（lazy）
    "burn_hex_file",
]
