"""Fail-closed target security capabilities for online flashing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .builtin_flm_bundle import discover_builtin_option_algorithm
from .errors import FlashError, FlashErrorCode


STM32F1_OPTION_ADDRESS = 0x1FFFF800
STM32F1_OPTION_SIZE = 16
STM32F1_OPTION_FLM_SHA256 = (
    "4a2efb1f314a4c70b4b9de9561fd288d3f48c7a363570bae5acc2a2aea72545a"
)
STM32F4_OPTION_ADDRESS = 0x1FFFC000
STM32F4_OPTION_SIZE = 4
STM32F4_OPTION_FLM_SHA256 = (
    "4fdd94fb68cf6659a6025639d5cf3b88a01658a4b5ab6b9e89cad87722b6fdf7"
)
STM32G4_OPTION_ADDRESS = 0x1FFF7800
STM32G4_OPTION_SIZE = 84
STM32G4_OPTION_FLM_SHA256 = (
    "3ec1306cc4e9f1714eee094a66669edb42a7d9df9300fa5af847df343017dac1"
)
STM32H7_OPTION_ADDRESS = 0xFFFFFFFF
STM32H7_OPTION_SIZE = 36
STM32H7_OPTION_FLM_SHA256 = (
    "e591ef4d2a2bc0a724f801f585c6be42b0939df5518a776041a94d4faee5833f"
)
STM32L0_OPTION_ADDRESS = 0x1FF80000
STM32L0_OPTION_SIZE = 20
STM32L0_OPTION_FLM_SHA256 = (
    "0930351a8585fe9e74a8786655e412a5eabddd405ff3bb296a0c5a884653edaf"
)
GD32F30X_OPTION_ADDRESS = 0x1FFFF800
GD32F30X_OPTION_SIZE = 16
GD32F30X_OPTION_FLM_SHA256 = (
    "4a2efb1f314a4c70b4b9de9561fd288d3f48c7a363570bae5acc2a2aea72545a"
)
PY32F030_OPTION_ADDRESS = 0x1FFF0E80
PY32F030_OPTION_SIZE = 16
PY32F030_OPTION_FLM_SHA256 = (
    "de2460697cc31abfd897570696bd6adb901acd7bbb5401c1dcdac2f15dc3bcbd"
)
_STM32F1 = re.compile(
    r"^STM32F10(?:0|1|2|3|5|7)(?:[A-Z][468BCDEFG][A-Z0-9X]{0,4}|X[468BCDEFG])$",
    re.IGNORECASE,
)
_STM32F413 = re.compile(r"^STM32F413(?:X[GH]|[A-Z][GH][A-Z0-9X]{2})$", re.IGNORECASE)
_STM32G474_512K = re.compile(r"^STM32G474(?:XE|[A-Z]E[A-Z0-9X]{2})$", re.IGNORECASE)
_STM32H743_2M = re.compile(r"^STM32H743(?:XI|[A-Z]I[A-Z0-9X]{2})$", re.IGNORECASE)
_STM32L010_16K = re.compile(r"^STM32L010(?:X4|[A-Z]4[A-Z0-9X]{2})$", re.IGNORECASE)
_GD32F303_512K = re.compile(
    r"^GD32F303(?:[A-Z]E|[A-Z]E[A-Z0-9]{2})$", re.IGNORECASE
)
_PY32F030K28T6 = re.compile(r"^PY32F030K28T6$", re.IGNORECASE)
_GD32 = re.compile(r"^GD32", re.IGNORECASE)
_PY32 = re.compile(r"^PY32", re.IGNORECASE)


@dataclass(frozen=True)
class SecurityCapability:
    part_number: str
    supported: bool
    family: str = ""
    reason: str = ""
    unlock_erases_flash: bool = False
    unlock_erases_eeprom: bool = False
    unlock_erases_backup_registers: bool = False
    reversible_lock: bool = False
    option_address: int = 0
    option_size: int = 0
    algorithm_path: Optional[Path] = None
    algorithm_sha256: str = ""

    def public(self) -> dict[str, object]:
        return {
            "part_number": self.part_number,
            "supported": self.supported,
            "unlock_supported": self.supported,
            "lock_supported": self.supported,
            "family": self.family,
            "reason": self.reason,
            "unlock_erases_flash": self.unlock_erases_flash,
            "unlock_erases_eeprom": self.unlock_erases_eeprom,
            "unlock_erases_backup_registers": self.unlock_erases_backup_registers,
            "reversible_lock": self.reversible_lock,
        }


_STM32F1_DENSITIES = {
    "STM32F100": frozenset("468BCDE"),
    "STM32F101": frozenset("468BCDEFG"),
    "STM32F102": frozenset("468B"),
    "STM32F103": frozenset("468BCDEFG"),
    "STM32F105": frozenset("8BC"),
    "STM32F107": frozenset("8BC"),
}


def _stm32f1_bundle_part(part_number: str) -> Optional[str]:
    part = part_number.strip().upper()
    if _STM32F1.fullmatch(part) is None:
        return None
    series = part[:9]
    suffix = part[9:]
    density = suffix[1]
    if density not in _STM32F1_DENSITIES.get(series, ()):
        return None
    return f"{series}x{density}"


def _stm32f413_bundle_part(part_number: str) -> Optional[str]:
    part = part_number.strip()
    if _STM32F413.fullmatch(part) is None:
        return None
    suffix = part[len("STM32F413"):]
    return "STM32F413x{}".format(suffix[1].upper())


def _stm32g474_bundle_part(part_number: str) -> Optional[str]:
    part = part_number.strip()
    if _STM32G474_512K.fullmatch(part) is None:
        return None
    return "STM32G474xE"


def _stm32h743_bundle_part(part_number: str) -> Optional[str]:
    part = part_number.strip()
    if _STM32H743_2M.fullmatch(part) is None:
        return None
    return "STM32H743xI"


def _stm32l010_bundle_part(part_number: str) -> Optional[str]:
    part = part_number.strip()
    if _STM32L010_16K.fullmatch(part) is None:
        return None
    return "STM32L010x4"


def _gd32f303_bundle_part(part_number: str) -> Optional[str]:
    part = part_number.strip()
    if _GD32F303_512K.fullmatch(part) is None:
        return None
    suffix = part[len("GD32F303"):].upper()
    return "GD32F303{}E".format(suffix[0])


def _py32f030_bundle_part(part_number: str) -> Optional[str]:
    if _PY32F030K28T6.fullmatch(part_number.strip()) is None:
        return None
    return "PY32F030x8"


def security_capability(part_number: str) -> SecurityCapability:
    """Resolve only hardware-validated families and fail closed on asset mismatch."""

    part = str(part_number or "").strip()
    bundle_part = _stm32f1_bundle_part(part)
    f413_bundle_part = _stm32f413_bundle_part(part)
    g474_bundle_part = _stm32g474_bundle_part(part)
    h743_bundle_part = _stm32h743_bundle_part(part)
    l010_bundle_part = _stm32l010_bundle_part(part)
    gd32f303_bundle_part = _gd32f303_bundle_part(part)
    py32f030_bundle_part = _py32f030_bundle_part(part)
    if py32f030_bundle_part is not None:
        try:
            algorithm = discover_builtin_option_algorithm(py32f030_bundle_part)
        except (OSError, TypeError, ValueError):
            return SecurityCapability(part, False, reason="内置选项字节算法不可用或完整性校验失败")
        if (
            algorithm is None
            or algorithm.file_name.casefold() != "py061xx_ob.flm"
            or algorithm.sha256 != PY32F030_OPTION_FLM_SHA256
        ):
            return SecurityCapability(part, False, reason="内置选项字节算法不匹配安全白名单")
        return SecurityCapability(
            part_number=part,
            supported=True,
            family="py32f030x8-rdp1",
            unlock_erases_flash=True,
            reversible_lock=True,
            option_address=PY32F030_OPTION_ADDRESS,
            option_size=PY32F030_OPTION_SIZE,
            algorithm_path=algorithm.path,
            algorithm_sha256=algorithm.sha256,
        )
    if gd32f303_bundle_part is not None:
        try:
            algorithm = discover_builtin_option_algorithm(gd32f303_bundle_part)
        except (OSError, TypeError, ValueError):
            return SecurityCapability(part, False, reason="内置选项字节算法不可用或完整性校验失败")
        if (
            algorithm is None
            or algorithm.file_name.casefold() != "gd32f10x_opt.flm"
            or algorithm.sha256 != GD32F30X_OPTION_FLM_SHA256
        ):
            return SecurityCapability(part, False, reason="内置选项字节算法不匹配安全白名单")
        return SecurityCapability(
            part_number=part,
            supported=True,
            family="gd32f303xe-spc",
            unlock_erases_flash=True,
            reversible_lock=True,
            option_address=GD32F30X_OPTION_ADDRESS,
            option_size=GD32F30X_OPTION_SIZE,
            algorithm_path=algorithm.path,
            algorithm_sha256=algorithm.sha256,
        )
    if l010_bundle_part is not None:
        try:
            algorithm = discover_builtin_option_algorithm(l010_bundle_part)
        except (OSError, TypeError, ValueError):
            return SecurityCapability(part, False, reason="内置选项字节算法不可用或完整性校验失败")
        if (
            algorithm is None
            or algorithm.file_name.casefold() != "stm32l0xx_opt.flm"
            or algorithm.sha256 != STM32L0_OPTION_FLM_SHA256
        ):
            return SecurityCapability(part, False, reason="内置选项字节算法不匹配安全白名单")
        return SecurityCapability(
            part_number=part,
            supported=True,
            family="stm32l010x4-rdp1",
            unlock_erases_flash=True,
            unlock_erases_eeprom=True,
            unlock_erases_backup_registers=True,
            reversible_lock=True,
            option_address=STM32L0_OPTION_ADDRESS,
            option_size=STM32L0_OPTION_SIZE,
            algorithm_path=algorithm.path,
            algorithm_sha256=algorithm.sha256,
        )
    if h743_bundle_part is not None:
        try:
            algorithm = discover_builtin_option_algorithm(h743_bundle_part)
        except (OSError, TypeError, ValueError):
            return SecurityCapability(part, False, reason="内置选项字节算法不可用或完整性校验失败")
        if (
            algorithm is None
            or algorithm.file_name.casefold() != "stm32h7xx_opt.flm"
            or algorithm.sha256 != STM32H7_OPTION_FLM_SHA256
        ):
            return SecurityCapability(part, False, reason="内置选项字节算法不匹配安全白名单")
        return SecurityCapability(
            part_number=part,
            supported=True,
            family="stm32h743-rdp1",
            unlock_erases_flash=True,
            reversible_lock=True,
            option_address=STM32H7_OPTION_ADDRESS,
            option_size=STM32H7_OPTION_SIZE,
            algorithm_path=algorithm.path,
            algorithm_sha256=algorithm.sha256,
        )
    if g474_bundle_part is not None:
        try:
            algorithm = discover_builtin_option_algorithm(g474_bundle_part)
        except (OSError, TypeError, ValueError):
            return SecurityCapability(part, False, reason="内置选项字节算法不可用或完整性校验失败")
        if (
            algorithm is None
            or algorithm.file_name.casefold() != "stm32g4xx_db_opt.flm"
            or algorithm.sha256 != STM32G4_OPTION_FLM_SHA256
        ):
            return SecurityCapability(part, False, reason="内置选项字节算法不匹配安全白名单")
        return SecurityCapability(
            part_number=part,
            supported=True,
            family="stm32g474-rdp1",
            unlock_erases_flash=True,
            reversible_lock=True,
            option_address=STM32G4_OPTION_ADDRESS,
            option_size=STM32G4_OPTION_SIZE,
            algorithm_path=algorithm.path,
            algorithm_sha256=algorithm.sha256,
        )
    if f413_bundle_part is not None:
        try:
            algorithm = discover_builtin_option_algorithm(f413_bundle_part)
        except (OSError, TypeError, ValueError):
            return SecurityCapability(part, False, reason="内置选项字节算法不可用或完整性校验失败")
        if (
            algorithm is None
            or algorithm.file_name.casefold() != "stm32f413xx_423xx_opt.flm"
            or algorithm.sha256 != STM32F4_OPTION_FLM_SHA256
        ):
            return SecurityCapability(part, False, reason="内置选项字节算法不匹配安全白名单")
        return SecurityCapability(
            part_number=part,
            supported=True,
            family="stm32f413-rdp1",
            unlock_erases_flash=True,
            reversible_lock=True,
            option_address=STM32F4_OPTION_ADDRESS,
            option_size=STM32F4_OPTION_SIZE,
            algorithm_path=algorithm.path,
            algorithm_sha256=algorithm.sha256,
        )
    if bundle_part is None:
        if _GD32.match(part):
            return SecurityCapability(
                part,
                False,
                reason="GD32 加锁/解锁正在等待真机验证",
            )
        if _PY32.match(part):
            return SecurityCapability(
                part,
                False,
                reason="该 PY32 器件尚未通过加锁/解锁真机验证",
            )
        return SecurityCapability(part, False, reason="该器件尚未通过加锁/解锁真机验证")
    try:
        algorithm = discover_builtin_option_algorithm(bundle_part)
    except (OSError, TypeError, ValueError):
        return SecurityCapability(part, False, reason="内置选项字节算法不可用或完整性校验失败")
    if (
        algorithm is None
        or algorithm.file_name.casefold() != "stm32f10x_opt.flm"
        or algorithm.sha256 != STM32F1_OPTION_FLM_SHA256
    ):
        return SecurityCapability(part, False, reason="内置选项字节算法不匹配安全白名单")
    return SecurityCapability(
        part_number=part,
        supported=True,
        # Keep the original internal family key for command/firmware ABI
        # compatibility. The resolver now covers the complete STM32F1
        # option-byte architecture rather than only STM32F103 order codes.
        family="stm32f103-rdp1",
        unlock_erases_flash=True,
        reversible_lock=True,
        option_address=STM32F1_OPTION_ADDRESS,
        option_size=STM32F1_OPTION_SIZE,
        algorithm_path=algorithm.path,
        algorithm_sha256=algorithm.sha256,
    )


def require_security_capability(part_number: str) -> SecurityCapability:
    capability = security_capability(part_number)
    if not capability.supported or capability.algorithm_path is None:
        raise FlashError(
            FlashErrorCode.SECURITY_NOT_SUPPORTED,
            capability.reason or "target security operations are not supported",
        )
    return capability
