"""
MKLink Serial Bridge — MCU 配置加载。

零外部依赖（仅 stdlib json/pathlib），零内部依赖。
mcu_profiles.json 位于本包同目录下。
"""

from __future__ import annotations

import json
from pathlib import Path


def load_mcu_profiles(profile_path: str | None = None) -> dict:
    """加载 MCU 配置文件。"""
    if profile_path is None:
        profile_path = str(Path(__file__).parent / "mcu_profiles.json")

    with open(profile_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("mcus", {})


def match_mcu_by_idcode(idcode: int, profiles: dict) -> str | None:
    """通过 IDCODE 自动匹配 MCU 配置。"""
    idcode_str = f"0x{idcode:08X}"
    for key, profile in profiles.items():
        pattern = profile.get("idcode_pattern", "")
        if pattern and pattern.upper() == idcode_str.upper():
            return key
    return None


def match_mcu_by_device(device_name: str, profiles: dict) -> str | None:
    """通过设备名称前缀匹配 MCU 配置。

    例如 "N32G435CB" 匹配 device_prefix "N32G43" → 返回 "n32g435"。
    """
    if not device_name:
        return None
    upper = device_name.upper()
    for key, profile in profiles.items():
        prefix = profile.get("device_prefix", "")
        if prefix and upper.startswith(prefix.upper()):
            return key
    return None
