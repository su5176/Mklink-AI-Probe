"""Fail-closed security recipes for V3/V4 offline-download scripts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Optional

from mklink.cmsis_dap.security import security_capability


_GD32F303_FAMILY = "gd32f303xe-spc"
_GD32F303_OPTION_FLM = "GD32F10x_OPT.FLM"
_GD32F303_OPTION_SHA256 = (
    "4a2efb1f314a4c70b4b9de9561fd288d3f48c7a363570bae5acc2a2aea72545a"
)
_GD32F303_CONFIG_DIR = "GD32F303xE"
_GD32F303_VOLTAGE_MV = 3300
_STM32F103_FAMILY = "stm32f103-rdp1"
_STM32F103_OPTION_FLM = "STM32F10x_OPT.FLM"
_STM32F103_OPTION_SHA256 = _GD32F303_OPTION_SHA256
_STM32F103_CONFIG_DIR = "STM32F103xE"
_STM32F103_VOLTAGE_MV = 3300
_STM32G474_FAMILY = "stm32g474-rdp1"
_STM32G474_OPTION_FLM = "STM32G4xx_DB_OPT.FLM"
_STM32G474_OPTION_SHA256 = (
    "3ec1306cc4e9f1714eee094a66669edb42a7d9df9300fa5af847df343017dac1"
)
_STM32G474_CONFIG_DIR = "STM32G474xE"
_STM32G474_VOLTAGE_MV = 3300
_PY32F030_FAMILY = "py32f030x8-rdp1"
_PY32F030_OPTION_FLM = "PY061xx_OB.FLM"
_PY32F030_OPTION_SHA256 = (
    "de2460697cc31abfd897570696bd6adb901acd7bbb5401c1dcdac2f15dc3bcbd"
)
_PY32F030_CONFIG_DIR = "PY32F030K28T6"
_PY32F030_VOLTAGE_MV = 3300
_SECURITY_VOLTAGES_MV = (1800, 3300, 5000)


@dataclass(frozen=True)
class OfflineSecurityPlan:
    """All integrity-checked assets and actions needed by one offline task."""

    family: str
    unlock_before_download: bool
    lock_after_download: bool
    voltage_mv: int
    algorithm_file_name: str
    algorithm_path: Path
    algorithm_sha256: str
    option_address: int
    ram_base: int
    config_dir: str
    unlock_config: bytes
    lock_config: bytes


@dataclass(frozen=True)
class _OfflineSecurityProfile:
    family: str
    algorithm_file_name: str
    algorithm_sha256: str
    config_dir: str
    voltage_mv: int
    option_address: int
    ram_base: int
    models: tuple[str, ...]
    config_factory: Callable[[str, int, str], bytes]


class _STM32F1Geometry(NamedTuple):
    bundle_part: str
    device_id: int
    flash_kib: int


_STM32F1_FLASH_KIB = {
    "4": 16,
    "6": 32,
    "8": 64,
    "B": 128,
    "C": 256,
    "D": 384,
    "E": 512,
    "F": 768,
    "G": 1024,
}


def _stm32f1_geometry(part_number: str) -> Optional[_STM32F1Geometry]:
    part = str(part_number or "").strip().upper()
    if len(part) < 11 or not part.startswith("STM32F10"):
        return None
    series = part[:9]
    if series not in {"STM32F100", "STM32F101", "STM32F102", "STM32F103", "STM32F105", "STM32F107"}:
        return None
    suffix = part[9:]
    if len(suffix) < 2:
        return None
    density = suffix[1]
    flash_kib = _STM32F1_FLASH_KIB.get(density)
    if flash_kib is None:
        return None
    if series == "STM32F100":
        device_id = 0x428 if density in "CDE" else 0x420
    elif series in {"STM32F105", "STM32F107"}:
        if density not in "8BC":
            return None
        device_id = 0x418
    elif density in "46":
        device_id = 0x412
    elif density in "8B":
        device_id = 0x410
    elif density in "CDE":
        device_id = 0x414
    elif density in "FG" and series in {"STM32F101", "STM32F103"}:
        device_id = 0x430
    else:
        return None
    return _STM32F1Geometry(f"{series}x{density}", device_id, flash_kib)


def _gd32f303_config(action: str, voltage_mv: int, _part_number: str) -> bytes:
    unlock = action == "unlock"
    values = (
        "format=mklink-security-v2",
        f"action={action}",
        "layout=value_inverse8",
        "allow_erased_pair=1",
        "normalize_inverse=1",
        "option_read=direct",
        "option_base=0x1FFFF800",
        "option_size=16",
        "value_offset=0",
        "unprotected_value=0xA5",
        f"write_value={'0xA5' if unlock else '0x00'}",
        f"goal={'unprotected' if unlock else 'protected'}",
        "id_address=0xE0042000",
        "id_mask=0x00000FFF",
        "id_expected=0x00000414",
        "density_address=0x1FFFF7E0",
        "density_expected=0x00400200",
        "density_fault_policy=only_if_protected",
        "status_address=0x4002201C",
        "status_error_mask=0x00000001",
        "status_protected_mask=0x00000002",
        "connect=halt",
        f"post_connect={'halt' if unlock else 'attach'}",
        "apply=power_cycle",
        f"voltage_mv={voltage_mv}",
        "off_ms=3000",
        "off_max_mv=800",
        f"blank_check_on_unlock_transition={1 if unlock else 0}",
        "flash_base=0x08000000",
        "flash_size=0x00080000",
        "flm_device_base=0x1FFFF800",
        "flm_device_size=16",
        "flm_page_size=16",
    )
    return ("\n".join(values) + "\n").encode("ascii")


def _stm32f1_config(action: str, voltage_mv: int, part_number: str) -> bytes:
    geometry = _stm32f1_geometry(part_number)
    if geometry is None:
        raise ValueError("unsupported STM32F1 order code")
    unlock = action == "unlock"
    values = (
        "format=mklink-security-v2",
        f"action={action}",
        "layout=value_inverse8",
        "allow_erased_pair=1",
        "normalize_inverse=1",
        "option_read=shadow_pairs_when_protected",
        "shadow_word0_address=0x4002201C",
        "shadow_word1_address=0x40022020",
        "shadow_value0=const:0x00",
        "shadow_value1=word0:2:0x07:0xF8",
        "shadow_value2=word0:10:0xFF:0x00",
        "shadow_value3=word0:18:0xFF:0x00",
        "shadow_value4=word1:0:0xFF:0x00",
        "shadow_value5=word1:8:0xFF:0x00",
        "shadow_value6=word1:16:0xFF:0x00",
        "shadow_value7=word1:24:0xFF:0x00",
        "option_base=0x1FFFF800",
        "option_size=16",
        "value_offset=0",
        "unprotected_value=0xA5",
        f"write_value={'0xA5' if unlock else '0x00'}",
        f"goal={'unprotected' if unlock else 'protected'}",
        "id_address=0xE0042000",
        "id_mask=0x00000FFF",
        f"id_expected=0x{geometry.device_id:08X}",
        "density_address=0x1FFFF7E0",
        f"density_expected=0x{0xFFFF0000 | geometry.flash_kib:08X}",
        "density_fault_policy=only_if_protected",
        "status_address=0x4002201C",
        "status_error_mask=0x00000001",
        "status_protected_mask=0x00000002",
        "connect=halt",
        f"post_connect={'halt' if unlock else 'attach'}",
        "apply=power_cycle",
        f"voltage_mv={voltage_mv}",
        "off_ms=3000",
        "off_max_mv=800",
        f"blank_check_on_unlock_transition={1 if unlock else 0}",
        "flash_base=0x08000000",
        f"flash_size=0x{geometry.flash_kib * 1024:08X}",
        "flm_device_base=0x1FFFF800",
        "flm_device_size=16",
        "flm_page_size=16",
    )
    return ("\n".join(values) + "\n").encode("ascii")


_STM32G474_OPTION_REGISTERS = (
    0x40022020,
    0x40022024,
    0x40022028,
    0x4002202C,
    0x40022030,
    0x40022070,
    0x40022044,
    0x40022048,
    0x4002204C,
    0x40022050,
    0x40022074,
)
_STM32G474_OPTION_MASKS = (
    0xFFFFFFFF,
    0x0000FFFF,
    0x8000FFFF,
    0x00FF00FF,
    0x00FF00FF,
    0x000101FF,
    0x0000FFFF,
    0x0000FFFF,
    0x00FF00FF,
    0x00FF00FF,
    0x000100FF,
)


def _stm32g474_config(action: str, voltage_mv: int, _part_number: str) -> bytes:
    unlock = action == "unlock"
    values = [
        "format=mklink-security-v2",
        f"action={action}",
        "layout=word32_list",
        "allow_erased_pair=1",
        "normalize_inverse=1",
        "option_read=word32_list",
    ]
    for index, (address, mask) in enumerate(
        zip(_STM32G474_OPTION_REGISTERS, _STM32G474_OPTION_MASKS)
    ):
        values.append(f"shadow_word{index}_address=0x{address:08X}")
        values.append(f"shadow_word{index}_mask=0x{mask:08X}")
    values.extend((
        "option_base=0x1FFF7800",
        "option_size=44",
        "value_offset=0",
        "unprotected_value=0xAA",
        f"write_value={'0xAA' if unlock else '0xBB'}",
        "forbidden_value=0xCC",
        f"goal={'unprotected' if unlock else 'protected'}",
        "id_address=0xE0042000",
        "id_mask=0x00000FFF",
        "id_expected=0x00000469",
        "density_address=0x1FFF75E0",
        "density_expected=0xFFFF0200",
        "density_fault_policy=only_if_protected",
        "status_address=0x40022010",
        "status_error_mask=0x00018010",
        "status_protected_mask=0x00000000",
        "connect=halt",
        f"post_connect={'halt' if unlock else 'attach'}",
        "apply=power_cycle",
        f"voltage_mv={voltage_mv}",
        "off_ms=3000",
        "off_max_mv=800",
        f"blank_check_on_unlock_transition={1 if unlock else 0}",
        "flash_base=0x08000000",
        "flash_size=0x00080000",
        "flm_device_base=0x1FFF7800",
        "flm_device_size=84",
        "flm_page_size=1024",
    ))
    return ("\n".join(values) + "\n").encode("ascii")


def _py32f030_config(action: str, voltage_mv: int, part_number: str) -> bytes:
    if str(part_number or "").strip().upper() != "PY32F030K28T6":
        raise ValueError("unsupported PY32F030 order code")
    unlock = action == "unlock"
    values = [
        "format=mklink-security-v2",
        f"action={action}",
        "layout=word32_inverse_pairs",
        "allow_erased_pair=1",
        "normalize_inverse=1",
        "option_read=direct",
        "option_base=0x1FFF0E80",
        "option_size=16",
        "value_offset=0",
        "unprotected_value=0xAA",
        f"write_value={'0xAA' if unlock else '0xBB'}",
        "forbidden_value=0xCC",
        f"goal={'unprotected' if unlock else 'protected'}",
        "id_address=0x40015800",
        "id_mask=0xFFFFFFFF",
        "id_expected=0x60001000",
        # UID byte 12 is the validated PY32F030 x8 (64 KiB) capacity marker.
        "density_address=0x1FFF0E0C",
        "density_mask=0x000000FF",
        "density_expected=0x00000078",
        "density_fault_policy=only_if_protected",
        "status_address=0x40022020",
        "status_error_mask=0x00000000",
        "status_protected_mask=0x00000001",
        "connect=halt",
        f"post_connect={'halt' if unlock else 'attach'}",
        "apply=power_cycle",
        f"voltage_mv={voltage_mv}",
        "off_ms=3000",
        "off_max_mv=800",
        f"blank_check_on_unlock_transition={1 if unlock else 0}",
        "flash_base=0x08000000",
        "flash_size=0x00010000",
        "flm_device_base=0x1FFF0E80",
        "flm_device_size=16",
        "flm_page_size=16",
        "allow_program_interrupt_on_transition=1",
    ]
    # This option FLM can complete the physical write while losing its normal
    # return on either RDP transition. Firmware still requires post-cycle
    # identity, RDP and preserved-field checks; unlock additionally requires a
    # full 64 KiB blank check before success.
    return ("\n".join(values) + "\n").encode("ascii")


def _offline_profile(part_number: str, family: str) -> Optional[_OfflineSecurityProfile]:
    if family == _GD32F303_FAMILY:
        return _OfflineSecurityProfile(
            family=family,
            algorithm_file_name=_GD32F303_OPTION_FLM,
            algorithm_sha256=_GD32F303_OPTION_SHA256,
            config_dir=_GD32F303_CONFIG_DIR,
            voltage_mv=_GD32F303_VOLTAGE_MV,
            option_address=0x1FFFF800,
            ram_base=0x20000000,
            models=("V3", "V4"),
            config_factory=_gd32f303_config,
        )
    geometry = _stm32f1_geometry(part_number)
    if family == _STM32F103_FAMILY and geometry is not None:
        return _OfflineSecurityProfile(
            family=family,
            algorithm_file_name=_STM32F103_OPTION_FLM,
            algorithm_sha256=_STM32F103_OPTION_SHA256,
            config_dir=geometry.bundle_part,
            voltage_mv=_STM32F103_VOLTAGE_MV,
            option_address=0x1FFFF800,
            ram_base=0x20000000,
            models=("V3", "V4"),
            config_factory=_stm32f1_config,
        )
    if family == _STM32G474_FAMILY:
        return _OfflineSecurityProfile(
            family=family,
            algorithm_file_name=_STM32G474_OPTION_FLM,
            algorithm_sha256=_STM32G474_OPTION_SHA256,
            config_dir=_STM32G474_CONFIG_DIR,
            voltage_mv=_STM32G474_VOLTAGE_MV,
            option_address=0x1FFF7800,
            ram_base=0x20000000,
            models=("V3",),
            config_factory=_stm32g474_config,
        )
    if (
        family == _PY32F030_FAMILY
        and str(part_number or "").strip().upper() == "PY32F030K28T6"
    ):
        return _OfflineSecurityProfile(
            family=family,
            algorithm_file_name=_PY32F030_OPTION_FLM,
            algorithm_sha256=_PY32F030_OPTION_SHA256,
            config_dir=_PY32F030_CONFIG_DIR,
            voltage_mv=_PY32F030_VOLTAGE_MV,
            option_address=0x1FFF0E80,
            ram_base=0x20000000,
            models=("V3",),
            config_factory=_py32f030_config,
        )
    return None


def offline_security_capability(model: str, part_number: str) -> dict[str, object]:
    """Return the narrower security capability supported by offline firmware."""

    normalized_model = str(model or "").strip().upper()
    part = str(part_number or "").strip()
    base = security_capability(part)
    profile = _offline_profile(part, base.family)
    supported = (
        profile is not None
        and normalized_model in profile.models
        and base.supported
        and base.algorithm_path is not None
        and base.algorithm_sha256 == profile.algorithm_sha256
    )
    if supported:
        reason = ""
    elif normalized_model not in ("V3", "V4"):
        reason = "脱机加锁/解锁仅支持带安全命令的 V3/V4 下载器"
    elif profile is not None and normalized_model not in profile.models:
        reason = "该下载器型号的脱机安全固件尚未支持此配置"
    elif profile is None:
        reason = (
            "该器件已支持在线加锁/解锁，但当前下载器脱机安全配置格式尚未覆盖"
            if base.supported
            else (base.reason or "该器件尚未通过脱机加锁/解锁真机验证")
        )
    else:
        reason = "内置选项字节算法不可用或完整性校验失败"
    return {
        "model": normalized_model,
        "part_number": part,
        "supported": supported,
        "unlock_supported": supported,
        "lock_supported": supported,
        "family": base.family if supported else "",
        "reason": reason,
        "unlock_erases_flash": bool(base.unlock_erases_flash) if supported else False,
        "reversible_lock": bool(base.reversible_lock) if supported else False,
        "voltage_options_mv": list(_SECURITY_VOLTAGES_MV) if supported else [],
        "default_voltage_mv": profile.voltage_mv if supported else None,
    }


def _strict_bool(payload: Mapping[str, object], name: str) -> bool:
    value = payload.get(name, False)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def resolve_offline_security(
    payload: Mapping[str, object],
    *,
    model: str,
    part_number: Optional[str],
) -> Optional[OfflineSecurityPlan]:
    """Validate requested security actions and resolve pinned local assets."""

    unlock = _strict_bool(payload, "unlock_before_download")
    lock = _strict_bool(payload, "lock_after_download")
    if not unlock and not lock:
        return None
    if not part_number:
        raise ValueError("offline security operations require target_part")
    status = offline_security_capability(model, part_number)
    if not status["supported"]:
        raise ValueError(str(status["reason"]))
    base = security_capability(part_number)
    profile = _offline_profile(part_number, base.family)
    assert profile is not None
    raw_voltage = payload.get("security_voltage_mv", profile.voltage_mv)
    if isinstance(raw_voltage, bool):
        raise ValueError("security voltage must be an integer")
    try:
        voltage = int(raw_voltage)
    except (TypeError, ValueError) as error:
        raise ValueError("security voltage must be an integer") from error
    if voltage not in _SECURITY_VOLTAGES_MV:
        raise ValueError("offline security voltage must be 1.8V, 3.3V, or 5V")

    assert base.algorithm_path is not None
    algorithm_path = Path(base.algorithm_path)
    try:
        digest = hashlib.sha256(algorithm_path.read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError("built-in option-byte FLM is unavailable") from error
    if digest != profile.algorithm_sha256:
        raise ValueError("built-in option-byte FLM integrity check failed")
    return OfflineSecurityPlan(
        family=profile.family,
        unlock_before_download=unlock,
        lock_after_download=lock,
        voltage_mv=voltage,
        algorithm_file_name=profile.algorithm_file_name,
        algorithm_path=algorithm_path,
        algorithm_sha256=digest,
        option_address=profile.option_address,
        ram_base=profile.ram_base,
        config_dir=profile.config_dir,
        unlock_config=profile.config_factory("unlock", voltage, part_number),
        lock_config=profile.config_factory("lock", voltage, part_number),
    )
