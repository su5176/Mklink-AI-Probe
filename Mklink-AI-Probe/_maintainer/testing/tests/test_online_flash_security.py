from pathlib import Path

import pytest

from mklink.cmsis_dap.builtin_flm_bundle import BuiltinOptionAlgorithm
from mklink.cmsis_dap.errors import FlashError, FlashErrorCode
from mklink.cmsis_dap.security import (
    GD32F30X_OPTION_FLM_SHA256,
    PY32F030_OPTION_FLM_SHA256,
    STM32F1_OPTION_FLM_SHA256,
    STM32F4_OPTION_FLM_SHA256,
    STM32G4_OPTION_FLM_SHA256,
    STM32H7_OPTION_FLM_SHA256,
    STM32L0_OPTION_FLM_SHA256,
    require_security_capability,
    security_capability,
)


def _algorithm(tmp_path: Path, digest: str = STM32F1_OPTION_FLM_SHA256):
    return BuiltinOptionAlgorithm(
        target_part="STM32F103xE",
        file_name="STM32F10x_OPT.FLM",
        path=tmp_path / "STM32F10x_OPT.FLM",
        sha256=digest,
        ram_start=0x20000000,
        ram_size=0x10000,
    )


def test_stm32f103_is_enabled_only_with_pinned_option_algorithm(monkeypatch, tmp_path):
    algorithm = _algorithm(tmp_path)
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda part: algorithm if part == "STM32F103xE" else None,
    )

    capability = security_capability("STM32F103RE")

    assert capability.supported is True
    assert capability.family == "stm32f103-rdp1"
    assert capability.unlock_erases_flash is True
    assert capability.reversible_lock is True
    assert capability.option_address == 0x1FFFF800
    assert capability.option_size == 16
    assert security_capability("STM32F103VE").supported is True
    assert security_capability("STM32F103xE").supported is True


@pytest.mark.parametrize(
    ("part_number", "bundle_part"),
    [
        ("STM32F100C8", "STM32F100x8"),
        ("STM32F101ZGT6", "STM32F101xG"),
        ("STM32F102CBT6", "STM32F102xB"),
        ("STM32F103RCT6", "STM32F103xC"),
        ("STM32F105R8T6", "STM32F105x8"),
        ("STM32F105RCT6", "STM32F105xC"),
        ("STM32F107VCT6", "STM32F107xC"),
    ],
)
def test_stm32f1_order_codes_share_the_validated_option_architecture(
    monkeypatch, tmp_path, part_number, bundle_part
):
    algorithm = _algorithm(tmp_path)
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda part: algorithm if part == bundle_part else None,
    )

    capability = security_capability(part_number)

    assert capability.supported is True
    assert capability.family == "stm32f103-rdp1"


def test_gd32f303xe_order_codes_use_only_pinned_option_algorithm(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="GD32F303CE",
        file_name="GD32F10x_OPT.FLM",
        path=tmp_path / "GD32F10x_OPT.FLM",
        sha256=GD32F30X_OPTION_FLM_SHA256,
        ram_start=0x20000000,
        ram_size=0x10000,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda part: algorithm if part == "GD32F303CE" else None,
    )

    capability = security_capability("GD32F303CET6")

    assert capability.supported is True
    assert capability.family == "gd32f303xe-spc"
    assert capability.option_address == 0x1FFFF800
    assert capability.option_size == 16
    assert capability.unlock_erases_flash is True
    assert capability.reversible_lock is True
    assert security_capability("GD32F303CE").supported is True


def test_other_gd32_remains_disabled_until_hardware_validation():
    capability = security_capability("GD32F103RE")

    assert capability.supported is False
    assert "真机验证" in capability.reason
    with pytest.raises(FlashError) as raised:
        require_security_capability("GD32F103RE")
    assert raised.value.code is FlashErrorCode.SECURITY_NOT_SUPPORTED


def test_gd32f303_fails_closed_when_option_algorithm_hash_changes(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="GD32F303CE",
        file_name="GD32F10x_OPT.FLM",
        path=tmp_path / "GD32F10x_OPT.FLM",
        sha256="0" * 64,
        ram_start=0x20000000,
        ram_size=0x10000,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda _part: algorithm,
    )

    capability = security_capability("GD32F303CET6")

    assert capability.supported is False
    assert "白名单" in capability.reason


def test_py32f030k28t6_uses_only_pinned_option_algorithm(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="PY32F030x8",
        file_name="PY061xx_OB.FLM",
        path=tmp_path / "PY061xx_OB.FLM",
        sha256=PY32F030_OPTION_FLM_SHA256,
        ram_start=0x20000000,
        ram_size=0x2000,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda part: algorithm if part == "PY32F030x8" else None,
    )

    capability = security_capability("PY32F030K28T6")

    assert capability.supported is True
    assert capability.family == "py32f030x8-rdp1"
    assert capability.option_address == 0x1FFF0E80
    assert capability.option_size == 16
    assert capability.unlock_erases_flash is True
    assert capability.reversible_lock is True
    assert security_capability("PY32F030K18T6").supported is False
    assert security_capability("PY32F030K28T7").supported is False


def test_py32f030_fails_closed_when_option_algorithm_hash_changes(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="PY32F030x8",
        file_name="PY061xx_OB.FLM",
        path=tmp_path / "PY061xx_OB.FLM",
        sha256="0" * 64,
        ram_start=0x20000000,
        ram_size=0x2000,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda _part: algorithm,
    )

    capability = security_capability("PY32F030K28T6")

    assert capability.supported is False
    assert "白名单" in capability.reason


def test_stm32f103_fails_closed_when_option_algorithm_hash_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda _part: _algorithm(tmp_path, "0" * 64),
    )

    capability = security_capability("STM32F103RE")

    assert capability.supported is False
    assert "白名单" in capability.reason


def test_stm32f413_exact_order_code_uses_only_pinned_option_algorithm(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="STM32F413xG",
        file_name="STM32F413xx_423xx_OPT.FLM",
        path=tmp_path / "STM32F413xx_423xx_OPT.FLM",
        sha256=STM32F4_OPTION_FLM_SHA256,
        ram_start=0x20000000,
        ram_size=0x50000,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda part: algorithm if part == "STM32F413xG" else None,
    )

    capability = security_capability("STM32F413VGHx")

    assert capability.supported is True
    assert capability.family == "stm32f413-rdp1"
    assert capability.option_address == 0x1FFFC000
    assert capability.option_size == 4


def test_stm32g474_512k_order_codes_use_only_pinned_option_algorithm(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="STM32G474xE",
        file_name="STM32G4xx_DB_OPT.FLM",
        path=tmp_path / "STM32G4xx_DB_OPT.FLM",
        sha256=STM32G4_OPTION_FLM_SHA256,
        ram_start=0x20000000,
        ram_size=0x20000,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda part: algorithm if part == "STM32G474xE" else None,
    )

    capability = security_capability("STM32G474RETx")

    assert capability.supported is True
    assert capability.family == "stm32g474-rdp1"
    assert capability.option_address == 0x1FFF7800
    assert capability.option_size == 84
    assert security_capability("STM32G474xE").supported is True
    assert security_capability("STM32G474RCTx").supported is False


def test_stm32g474_fails_closed_when_option_algorithm_hash_changes(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="STM32G474xE",
        file_name="STM32G4xx_DB_OPT.FLM",
        path=tmp_path / "STM32G4xx_DB_OPT.FLM",
        sha256="0" * 64,
        ram_start=0x20000000,
        ram_size=0x20000,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda _part: algorithm,
    )

    capability = security_capability("STM32G474RETx")

    assert capability.supported is False
    assert "白名单" in capability.reason


def test_stm32h743_2m_order_codes_use_only_pinned_option_algorithm(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="STM32H743xI",
        file_name="STM32H7xx_OPT.FLM",
        path=tmp_path / "STM32H7xx_OPT.FLM",
        sha256=STM32H7_OPTION_FLM_SHA256,
        ram_start=0x24000000,
        ram_size=0x80000,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda part: algorithm if part == "STM32H743xI" else None,
    )

    capability = security_capability("STM32H743IIT6")

    assert capability.supported is True
    assert capability.family == "stm32h743-rdp1"
    assert capability.option_address == 0xFFFFFFFF
    assert capability.option_size == 36
    assert security_capability("STM32H743xI").supported is True
    assert security_capability("STM32H743IGT6").supported is False


def test_stm32h743_fails_closed_when_option_algorithm_hash_changes(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="STM32H743xI",
        file_name="STM32H7xx_OPT.FLM",
        path=tmp_path / "STM32H7xx_OPT.FLM",
        sha256="0" * 64,
        ram_start=0x24000000,
        ram_size=0x80000,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda _part: algorithm,
    )

    capability = security_capability("STM32H743IIT6")

    assert capability.supported is False
    assert "白名单" in capability.reason


def test_stm32l010x4_order_codes_use_only_pinned_option_algorithm(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="STM32L010x4",
        file_name="STM32L0xx_OPT.FLM",
        path=tmp_path / "STM32L0xx_OPT.FLM",
        sha256=STM32L0_OPTION_FLM_SHA256,
        ram_start=0x20000000,
        ram_size=0x800,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda part: algorithm if part == "STM32L010x4" else None,
    )

    capability = security_capability("STM32L010F4P6")

    assert capability.supported is True
    assert capability.family == "stm32l010x4-rdp1"
    assert capability.option_address == 0x1FF80000
    assert capability.option_size == 20
    assert capability.unlock_erases_flash is True
    assert capability.unlock_erases_eeprom is True
    assert capability.unlock_erases_backup_registers is True
    assert security_capability("STM32L010x4").supported is True
    assert security_capability("STM32L010F3P6").supported is False


def test_stm32l010_fails_closed_when_option_algorithm_hash_changes(monkeypatch, tmp_path):
    algorithm = BuiltinOptionAlgorithm(
        target_part="STM32L010x4",
        file_name="STM32L0xx_OPT.FLM",
        path=tmp_path / "STM32L0xx_OPT.FLM",
        sha256="0" * 64,
        ram_start=0x20000000,
        ram_size=0x800,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.security.discover_builtin_option_algorithm",
        lambda _part: algorithm,
    )

    capability = security_capability("STM32L010F4P6")

    assert capability.supported is False
    assert "白名单" in capability.reason
