import asyncio
from pathlib import Path
import json
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from mklink.cmsis_dap.algorithm_catalog import FlashAlgorithm
from mklink.cmsis_dap.models import MemoryRegion, TargetRecord
from mklink.firmware_check import Version, read_bridge_version, read_device_version
from mklink.offline_download import (
    OfflineDownloadError,
    deploy_offline_bundle,
    generate_offline_script,
    offline_trigger_command,
    parse_offline_config,
    script_filename,
)
from mklink.bridge import MKLinkSerialBridge
from mklink.remote.api import create_app
from mklink.remote.offline_download_api import detect_probe_model
from mklink.remote.resource_manager import ResourceGroup
from route_utils import find_route


def _config(model="V4"):
    return {
        "model": model,
        "script_name": "factory-line-a.py",
        "auto_download_count": 3,
        "wait_idcode_timeout_ms": 10000,
        "swd_clock_hz": 10000000,
        "algorithms": [
            {
                "id": "internal",
                "file_name": "STM32F10x_1024.FLM",
                "flash_base": "0x08000000",
                "ram_base": "0x20000000",
                "source_kind": "upload",
                "upload_index": 0,
            },
            {
                "id": "external",
                "file_name": "External.FLM",
                "flash_base": "0x90000000",
                "ram_base": "0x20010000",
                "source_kind": "upload",
                "upload_index": 1,
            },
        ],
        "firmwares": [
            {
                "id": "boot",
                "file_name": "boot.bin",
                "format": "bin",
                "base_address": "0x08000000",
                "algorithm_id": "internal",
                "upload_index": 0,
            },
            {
                "id": "app",
                "file_name": "rt-thread.hex",
                "format": "hex",
                "base_address": None,
                "algorithm_id": "internal",
                "upload_index": 1,
            },
            {
                "id": "assets",
                "file_name": "assets.bin",
                "format": "bin",
                "base_address": "0x90000000",
                "algorithm_id": "external",
                "upload_index": 2,
            },
        ],
    }


def _gd32_security_config(*, unlock=True, lock=True):
    payload = _config("V3")
    payload.update({
        "auto_download_count": 1,
        "target_part": "GD32F303CET6",
        "unlock_before_download": unlock,
        "lock_after_download": lock,
        "security_voltage_mv": 3300,
    })
    return payload


def _stm32f103_security_config(*, unlock=True, lock=True):
    payload = _config("V3")
    payload.update({
        "auto_download_count": 1,
        "target_part": "STM32F103RE",
        "unlock_before_download": unlock,
        "lock_after_download": lock,
        "security_voltage_mv": 3300,
    })
    return payload


def _stm32g474_security_config(*, unlock=True, lock=True, model="V3"):
    payload = _config(model)
    payload.update({
        "auto_download_count": 1,
        "target_part": "STM32G474RET6",
        "unlock_before_download": unlock,
        "lock_after_download": lock,
        "security_voltage_mv": 3300,
    })
    return payload


def _py32f030_security_config(*, unlock=True, lock=True, model="V3"):
    payload = _config(model)
    payload.update({
        "auto_download_count": 1,
        "target_part": "PY32F030K28T6",
        "unlock_before_download": unlock,
        "lock_after_download": lock,
        "security_voltage_mv": 3300,
    })
    return payload


def _local_stm32_config(source: Path, base_address="0x08005000"):
    return {
        "model": "V4",
        "script_name": "stm32f103re.py",
        "auto_download_count": 1,
        "wait_idcode_timeout_ms": 10000,
        "swd_clock_hz": 10000000,
        "target_part": "STM32F103RE",
        "algorithms": [{
            "id": "internal",
            "file_name": "STM32F10x_512.FLM",
            "flash_base": "0x08000000",
            "ram_base": "0x20000000",
            "source_kind": "existing",
        }],
        "firmwares": [{
            "id": "app",
            "file_name": "rtthread.bin",
            "format": "bin",
            "base_address": base_address,
            "algorithm_id": "internal",
            "source_path": str(source),
        }],
    }


def _configure_offline_target_metadata(
    app,
    monkeypatch,
    *,
    target_part="STM32F103RE",
    regions=None,
    algorithms=None,
):
    services = app.state.online_flash
    target = TargetRecord(
        part_number=target_part,
        vendor="Test",
        installed=True,
        source="installed",
    )
    services.catalog.search = lambda query, **_kwargs: (
        [target] if query.casefold() == target_part.casefold() else []
    )
    services.target_memory_provider = lambda _part: tuple(regions or (
        MemoryRegion("flash", 0x08000000, 0x80000, True, True, 0x800),
    ))
    services.custom_flms = None
    monkeypatch.setattr(
        "mklink.cmsis_dap.algorithm_catalog.discover_flash_algorithms",
        lambda _part, paths: list(algorithms or ()),
    )


def test_probe_model_controls_script_filename():
    assert script_filename("V2", "custom.py") == "offline_download.py"
    assert script_filename("V3", "custom.py") == "offline_download.py"
    assert script_filename("V4", "custom.py") == "custom.py"


def test_v2_rejects_automatic_multi_round_downloads():
    payload = _config("V2")
    with pytest.raises(OfflineDownloadError, match="V2.*1"):
        parse_offline_config(payload)


def test_offline_swd_clock_is_limited_to_10_mhz():
    payload = _config()
    payload["swd_clock_hz"] = 10_000_001
    with pytest.raises(OfflineDownloadError, match="SWD clock.*10000000"):
        parse_offline_config(payload)


def test_bin_requires_an_address_and_hex_uses_embedded_addresses():
    payload = _config()
    payload["firmwares"][0]["base_address"] = None
    with pytest.raises(OfflineDownloadError, match="BIN.*base address"):
        parse_offline_config(payload)

    payload = _config()
    payload["firmwares"][1]["base_address"] = "0x08005000"
    parsed = parse_offline_config(payload)
    assert parsed.firmwares[1].base_address is None


def test_v4_script_supports_multiple_files_addresses_algorithms_and_rounds():
    config = parse_offline_config(_config())
    script = generate_offline_script(config)

    assert "AUTO_DOWNLOAD_COUNT = 3" in script
    assert "WAIT_IDCODE_TIMEOUT = 10000" in script
    assert "cmd.set_swd_clock(10000000)" in script
    assert 'load.flm("FLM/STM32F10x_1024.FLM", 0x08000000, 0x20000000)' in script
    assert 'load.bin("boot.bin", 0x08000000)' in script
    assert 'load.hex("rt-thread.hex")' in script
    assert 'load.flm("FLM/External.FLM", 0x90000000, 0x20010000)' in script
    assert 'load.bin("assets.bin", 0x90000000)' in script
    assert script.index('load.bin("boot.bin"') < script.index('load.hex("rt-thread.hex"')
    assert script.index('load.hex("rt-thread.hex"') < script.index('load.flm("FLM/External.FLM"')


def test_chip_erase_uses_first_firmware_algorithm_before_programming():
    payload = _config("V3")
    payload["auto_download_count"] = 1
    payload["erase_all_before_download"] = True

    config = parse_offline_config(payload)
    script = generate_offline_script(config)

    main_flm = 'load.flm("FLM/STM32F10x_1024.FLM", 0x08000000, 0x20000000)'
    erase = "cmd.erase_chip_flash(0x08000000)"
    program = 'load.bin("boot.bin", 0x08000000)'
    assert config.erase_all_before_download is True
    assert script.count(main_flm) == 1
    assert script.index(main_flm) < script.index(erase) < script.index(program)
    assert 'print("chip erase failed: 0x08000000")' in script


def test_chip_erase_flag_requires_a_boolean_and_rejects_hpm():
    payload = _config()
    payload["erase_all_before_download"] = "true"
    with pytest.raises(OfflineDownloadError, match="must be a boolean"):
        parse_offline_config(payload)

    payload = {
        "model": "V4",
        "script_name": "hpm.py",
        "auto_download_count": 1,
        "wait_idcode_timeout_ms": 10000,
        "swd_clock_hz": 10000000,
        "target_part": "HPM5301xEGx",
        "board": "hpm5301evklite",
        "erase_all_before_download": True,
        "algorithms": [],
        "firmwares": [{
            "id": "app",
            "file_name": "app.bin",
            "format": "bin",
            "base_address": "0x80000400",
            "algorithm_id": "",
            "upload_index": 0,
        }],
    }
    with pytest.raises(OfflineDownloadError, match="HPM ROM API.*chip erase"):
        parse_offline_config(payload)


def test_v3_security_script_orders_unlock_program_and_lock_and_aborts_on_failure():
    config = parse_offline_config(_gd32_security_config())
    script = generate_offline_script(config)
    assert b"connect=halt\n" in config.security.unlock_config
    assert b"connect=halt\n" in config.security.lock_config
    assert b"under_reset" not in config.security.unlock_config
    assert b"off_ms=3000\n" in config.security.unlock_config
    assert b"off_max_mv=800\n" in config.security.unlock_config

    unlock_flm = 'load.flm("FLM/GD32F10x_OPT.FLM", 0x1FFFF800, 0x20000000)'
    unlock = 'cmd.unlock("CFG/GD32F303xE/unlock.cfg")'
    program = 'load.bin("boot.bin", 0x08000000)'
    lock = 'cmd.lock("CFG/GD32F303xE/lock.cfg")'
    assert script.count(unlock_flm) == 2
    assert 'cmd.unlock("CFG/__mklink_security_api_probe_missing__.cfg")' in script
    assert 'cmd.lock("CFG/__mklink_security_api_probe_missing__.cfg")' in script
    assert "security_unlock_api != -101 or security_lock_api != -101" in script
    assert script.index(unlock_flm) < script.index(unlock) < script.index(program)
    assert script.index(program) < script.rindex(unlock_flm) < script.index(lock)
    assert 'print("security unlock failed:", security_unlock_rc)' in script
    assert 'print("security lock failed:", security_lock_rc)' in script
    assert script.index("cmd.set_reset()") < script.index("cmd.cpu_run()")


def test_v3_stm32f1_security_uses_part_geometry_with_pinned_recipe():
    config = parse_offline_config(_stm32f103_security_config())
    script = generate_offline_script(config)

    assert config.security.family == "stm32f103-rdp1"
    assert config.security.algorithm_file_name == "STM32F10x_OPT.FLM"
    assert b"density_expected=0xFFFF0200" in config.security.unlock_config
    assert b"option_read=shadow_pairs_when_protected" in config.security.unlock_config
    assert b"shadow_word0_address=0x4002201C" in config.security.unlock_config
    assert b"shadow_value1=word0:2:0x07:0xF8" in config.security.unlock_config
    assert b"shadow_value7=word1:24:0xFF:0x00" in config.security.unlock_config
    assert b"connect=halt" in config.security.unlock_config
    assert b"off_ms=3000" in config.security.unlock_config
    assert b"off_max_mv=800" in config.security.unlock_config
    assert b"connect=halt" in config.security.lock_config
    assert 'load.flm("FLM/STM32F10x_OPT.FLM", 0x1FFFF800, 0x20000000)' in script
    assert 'cmd.unlock("CFG/STM32F103xE/unlock.cfg")' in script
    assert 'cmd.lock("CFG/STM32F103xE/lock.cfg")' in script

    payload = _stm32f103_security_config()
    payload["target_part"] = "STM32F103RC"
    config = parse_offline_config(payload)
    script = generate_offline_script(config)
    assert b"id_expected=0x00000414" in config.security.unlock_config
    assert b"density_expected=0xFFFF0100" in config.security.unlock_config
    assert b"flash_size=0x00040000" in config.security.unlock_config
    assert 'cmd.unlock("CFG/STM32F103xC/unlock.cfg")' in script
    assert 'cmd.lock("CFG/STM32F103xC/lock.cfg")' in script


@pytest.mark.parametrize(
    ("part_number", "device_id", "flash_kib"),
    [
        ("STM32F100C8", 0x420, 64),
        ("STM32F100ZE", 0x428, 512),
        ("STM32F101C6", 0x412, 32),
        ("STM32F102CB", 0x410, 128),
        ("STM32F103RC", 0x414, 256),
        ("STM32F103ZG", 0x430, 1024),
        ("STM32F105R8", 0x418, 64),
        ("STM32F105RC", 0x418, 256),
        ("STM32F107VC", 0x418, 256),
    ],
)
def test_v3_stm32f1_series_resolves_device_and_capacity(part_number, device_id, flash_kib):
    payload = _stm32f103_security_config()
    payload["target_part"] = part_number

    config = parse_offline_config(payload)

    assert f"id_expected=0x{device_id:08X}\n".encode("ascii") in config.security.unlock_config
    assert f"density_expected=0x{0xFFFF0000 | flash_kib:08X}\n".encode("ascii") in config.security.unlock_config
    assert f"flash_size=0x{flash_kib * 1024:08X}\n".encode("ascii") in config.security.unlock_config


def test_v3_without_post_lock_resets_and_beeps_after_programming():
    script = generate_offline_script(
        parse_offline_config(_gd32_security_config(lock=False))
    )

    assert 'cmd.unlock("CFG/GD32F303xE/unlock.cfg")' in script
    assert 'cmd.lock("CFG/GD32F303xE/lock.cfg")' not in script
    program = script.index('load.bin("assets.bin", 0x90000000)')
    reset = script.index("cmd.set_reset()")
    run = script.index("cmd.cpu_run()")
    beep_on = script.index("cmd.set_beep_on()")
    delay = script.index("time.sleep_ms(1000)")
    beep_off = script.index("cmd.set_beep_off()")
    finished = script.index('print("auto download finished")')
    assert program < finished < reset < run < beep_on < delay < beep_off


def test_v3_stm32g474_security_uses_generic_masked_word_recipe():
    config = parse_offline_config(_stm32g474_security_config())
    script = generate_offline_script(config)

    assert config.security.family == "stm32g474-rdp1"
    assert config.security.algorithm_file_name == "STM32G4xx_DB_OPT.FLM"
    assert config.security.option_address == 0x1FFF7800
    assert b"layout=word32_list" in config.security.unlock_config
    assert b"option_read=word32_list" in config.security.unlock_config
    assert b"shadow_word0_address=0x40022020" in config.security.unlock_config
    assert b"shadow_word0_mask=0xFFFFFFFF" in config.security.unlock_config
    assert b"shadow_word10_address=0x40022074" in config.security.unlock_config
    assert b"shadow_word10_mask=0x000100FF" in config.security.unlock_config
    assert b"forbidden_value=0xCC" in config.security.unlock_config
    assert b"id_expected=0x00000469" in config.security.unlock_config
    assert b"density_expected=0xFFFF0200" in config.security.unlock_config
    assert b"status_protected_mask=0x00000000" in config.security.unlock_config
    assert b"connect=halt" in config.security.unlock_config
    assert b"flash_size=0x00080000" in config.security.unlock_config
    assert 'load.flm("FLM/STM32G4xx_DB_OPT.FLM", 0x1FFF7800, 0x20000000)' in script
    assert 'cmd.unlock("CFG/STM32G474xE/unlock.cfg")' in script
    assert 'cmd.lock("CFG/STM32G474xE/lock.cfg")' in script


def test_stm32g474_offline_security_is_enabled_only_for_updated_v3_firmware():
    app = create_app(auth_token=None, project_root=".")
    with TestClient(app) as client:
        v3 = client.get(
            "/api/offline-download/security",
            params={"model": "V3", "part_number": "STM32G474RET6"},
        )
        v4 = client.get(
            "/api/offline-download/security",
            params={"model": "V4", "part_number": "STM32G474RET6"},
        )

    assert v3.status_code == 200
    assert v3.json()["supported"] is True
    assert v3.json()["family"] == "stm32g474-rdp1"
    assert v4.status_code == 200
    assert v4.json()["supported"] is False
    assert "固件尚未支持" in v4.json()["reason"]


def test_v3_py32f030_security_uses_exact_x8_halt_recipe():
    config = parse_offline_config(_py32f030_security_config())
    script = generate_offline_script(config)

    assert config.security.family == "py32f030x8-rdp1"
    assert config.security.algorithm_file_name == "PY061xx_OB.FLM"
    assert config.security.option_address == 0x1FFF0E80
    assert b"layout=word32_inverse_pairs" in config.security.unlock_config
    assert b"option_read=direct" in config.security.unlock_config
    assert b"forbidden_value=0xCC" in config.security.unlock_config
    assert b"id_address=0x40015800" in config.security.unlock_config
    assert b"id_expected=0x60001000" in config.security.unlock_config
    assert b"density_address=0x1FFF0E0C" in config.security.unlock_config
    assert b"density_mask=0x000000FF" in config.security.unlock_config
    assert b"density_expected=0x00000078" in config.security.unlock_config
    assert b"connect=halt" in config.security.unlock_config
    assert b"allow_program_interrupt_on_transition=1" in config.security.unlock_config
    assert b"allow_program_interrupt_on_transition=1" in config.security.lock_config
    assert b"flash_size=0x00010000" in config.security.unlock_config
    assert b"off_ms=3000" in config.security.unlock_config
    assert b"off_max_mv=800" in config.security.unlock_config
    assert 'load.flm("FLM/PY061xx_OB.FLM", 0x1FFF0E80, 0x20000000)' in script
    assert 'cmd.unlock("CFG/PY32F030K28T6/unlock.cfg")' in script
    assert 'cmd.lock("CFG/PY32F030K28T6/lock.cfg")' in script


def test_py32f030_offline_security_is_exact_part_and_v3_only():
    app = create_app(auth_token=None, project_root=".")
    with TestClient(app) as client:
        exact_v3 = client.get(
            "/api/offline-download/security",
            params={"model": "V3", "part_number": "PY32F030K28T6"},
        )
        exact_v4 = client.get(
            "/api/offline-download/security",
            params={"model": "V4", "part_number": "PY32F030K28T6"},
        )
        nearby = client.get(
            "/api/offline-download/security",
            params={"model": "V3", "part_number": "PY32F030K18T6"},
        )

    assert exact_v3.status_code == 200
    assert exact_v3.json()["supported"] is True
    assert exact_v3.json()["family"] == "py32f030x8-rdp1"
    assert exact_v4.status_code == 200
    assert exact_v4.json()["supported"] is False
    assert "固件尚未支持" in exact_v4.json()["reason"]
    assert nearby.status_code == 200
    assert nearby.json()["supported"] is False


@pytest.mark.parametrize("model", ["V2"])
def test_offline_security_fails_closed_for_firmware_without_security_commands(model):
    payload = _gd32_security_config()
    payload["model"] = model
    if model == "V2":
        payload["auto_download_count"] = 1

    with pytest.raises(OfflineDownloadError, match="V3/V4"):
        parse_offline_config(payload)


def test_v4_security_uses_the_same_pinned_recipe_and_resets_without_post_lock():
    payload = _gd32_security_config(lock=False)
    payload["model"] = "V4"
    payload["script_name"] = "gd32-security.py"

    script = generate_offline_script(parse_offline_config(payload))

    assert 'cmd.unlock("CFG/GD32F303xE/unlock.cfg")' in script
    assert 'cmd.lock("CFG/GD32F303xE/lock.cfg")' not in script
    assert "security_unlock_api != -101 or security_lock_api != -101" in script
    assert "cmd.set_reset()" in script
    assert script.index("cmd.set_reset()") < script.index("cmd.cpu_run()")


def test_offline_security_rejects_unvalidated_target_and_accepts_board_voltage():
    payload = _gd32_security_config()
    payload["target_part"] = "GD32F103RET6"
    with pytest.raises(OfflineDownloadError, match="真机验证"):
        parse_offline_config(payload)

    for voltage in (1800, 5000):
        payload = _gd32_security_config()
        payload["security_voltage_mv"] = voltage
        config = parse_offline_config(payload)
        assert f"voltage_mv={voltage}\n".encode("ascii") in config.security.unlock_config
        assert f"voltage_mv={voltage}\n".encode("ascii") in config.security.lock_config

    payload = _gd32_security_config()
    payload["security_voltage_mv"] = 2500
    with pytest.raises(OfflineDownloadError, match="1.8V, 3.3V, or 5V"):
        parse_offline_config(payload)

def test_hpm_offline_script_uses_rom_api_without_flm():
    payload = {
        "model": "V4",
        "script_name": "hpm-offline.py",
        "auto_download_count": 2,
        "wait_idcode_timeout_ms": 10000,
        "swd_clock_hz": 10000000,
        "target_part": "HPM5301xEGx",
        "board": "hpm5301evklite",
        "algorithms": [],
        "firmwares": [{
            "id": "app",
            "file_name": "app.bin",
            "format": "bin",
            "base_address": "0x80000400",
            "algorithm_id": "",
            "upload_index": 0,
        }],
    }

    config = parse_offline_config(payload)
    script = generate_offline_script(config)

    assert config.algorithms == ()
    assert "import hpm" in script
    assert 'hpm.board("hpm5301evklite")' in script
    assert 'hpm.program("app.bin", 0x80000400)' in script
    assert "load.flm" not in script


def test_non_hpm_offline_config_rejects_hpm_board_settings():
    payload = _config()
    payload["target_part"] = "STM32F103RC"
    payload["board"] = "hpm5301evklite"

    with pytest.raises(OfflineDownloadError, match="only valid for HPM"):
        parse_offline_config(payload)


def test_deploy_copies_script_firmwares_and_flms_to_expected_usb_directories(tmp_path):
    config = parse_offline_config(_config())
    firmware_sources = []
    for name in ("boot.bin", "rt-thread.hex", "assets.bin"):
        path = tmp_path / ("source-" + name)
        path.write_bytes(name.encode("ascii"))
        firmware_sources.append(path)
    algorithm_sources = []
    for name in ("internal.flm", "external.flm"):
        path = tmp_path / name
        path.write_bytes(name.encode("ascii"))
        algorithm_sources.append(path)
    disk = tmp_path / "MICROKEEN"
    disk.mkdir()
    (disk / "keep.txt").write_text("keep", encoding="ascii")

    result = deploy_offline_bundle(
        config,
        disk,
        firmware_sources=firmware_sources,
        algorithm_sources=algorithm_sources,
    )

    assert result["script_name"] == "factory-line-a.py"
    assert (disk / "python" / "factory-line-a.py").is_file()
    assert (disk / "boot.bin").read_bytes() == b"boot.bin"
    assert (disk / "rt-thread.hex").read_bytes() == b"rt-thread.hex"
    assert (disk / "assets.bin").read_bytes() == b"assets.bin"
    assert (disk / "FLM" / "STM32F10x_1024.FLM").read_bytes() == b"internal.flm"
    assert (disk / "FLM" / "External.FLM").read_bytes() == b"external.flm"
    assert (disk / "keep.txt").read_text(encoding="ascii") == "keep"


def test_v3_security_deploys_pinned_option_flm_and_configs(tmp_path):
    config = parse_offline_config(_gd32_security_config())
    firmware_sources = []
    for name in ("boot.bin", "rt-thread.hex", "assets.bin"):
        path = tmp_path / ("source-" + name)
        path.write_bytes(name.encode("ascii"))
        firmware_sources.append(path)
    algorithm_sources = []
    for name in ("internal.flm", "external.flm"):
        path = tmp_path / name
        path.write_bytes(name.encode("ascii"))
        algorithm_sources.append(path)
    disk = tmp_path / "MICROKEEN"
    disk.mkdir()

    result = deploy_offline_bundle(
        config,
        disk,
        firmware_sources=firmware_sources,
        algorithm_sources=algorithm_sources,
    )

    assert "FLM/GD32F10x_OPT.FLM" in result["files"]
    assert "CFG/GD32F303xE/unlock.cfg" in result["files"]
    assert "CFG/GD32F303xE/lock.cfg" in result["files"]
    assert (disk / "FLM" / "GD32F10x_OPT.FLM").read_bytes() == config.security.algorithm_path.read_bytes()
    assert "id_expected=0x00000414" in (disk / "CFG" / "GD32F303xE" / "unlock.cfg").read_text("ascii")
    assert "connect=halt" in (disk / "CFG" / "GD32F303xE" / "unlock.cfg").read_text("ascii")
    assert "density_expected=0x00400200" in (disk / "CFG" / "GD32F303xE" / "lock.cfg").read_text("ascii")
    assert "option_read=direct" in (disk / "CFG" / "GD32F303xE" / "lock.cfg").read_text("ascii")
    assert "connect=halt" in (disk / "CFG" / "GD32F303xE" / "lock.cfg").read_text("ascii")
    assert "off_ms=3000" in (disk / "CFG" / "GD32F303xE" / "lock.cfg").read_text("ascii")
    assert "off_max_mv=800" in (disk / "CFG" / "GD32F303xE" / "lock.cfg").read_text("ascii")


def test_v3_stm32f103xe_security_deploys_separate_assets(tmp_path):
    config = parse_offline_config(_stm32f103_security_config())
    firmware_sources = []
    for name in ("boot.bin", "rt-thread.hex", "assets.bin"):
        path = tmp_path / ("source-" + name)
        path.write_bytes(name.encode("ascii"))
        firmware_sources.append(path)
    algorithm_sources = []
    for name in ("internal.flm", "external.flm"):
        path = tmp_path / name
        path.write_bytes(name.encode("ascii"))
        algorithm_sources.append(path)
    disk = tmp_path / "MICROKEEN"
    disk.mkdir()

    result = deploy_offline_bundle(
        config,
        disk,
        firmware_sources=firmware_sources,
        algorithm_sources=algorithm_sources,
    )

    assert "FLM/STM32F10x_OPT.FLM" in result["files"]
    unlock = (disk / "CFG" / "STM32F103xE" / "unlock.cfg").read_text("ascii")
    assert "id_expected=0x00000414" in unlock
    assert "density_expected=0xFFFF0200" in unlock
    assert "flash_size=0x00080000" in unlock


def test_deploy_never_creates_a_staging_directory_on_the_probe_disk(tmp_path, monkeypatch):
    config = parse_offline_config(_config())
    disk = tmp_path / "MICROKEEN"
    disk.mkdir()
    firmware_sources = []
    for index, name in enumerate(("boot.bin", "rt-thread.hex", "assets.bin")):
        path = tmp_path / f"firmware-{index}"
        path.write_bytes(name.encode("ascii"))
        firmware_sources.append(path)
    algorithm_sources = []
    for index in range(2):
        path = tmp_path / f"algorithm-{index}"
        path.write_bytes(bytes([index]))
        algorithm_sources.append(path)

    real_copy2 = __import__("shutil").copy2

    def assert_clean_probe_disk(source, destination):
        assert not any(
            child.name.startswith(".mklink-offline-staging-")
            for child in disk.iterdir()
        )
        return real_copy2(source, destination)

    monkeypatch.setattr("mklink.offline_download.shutil.copy2", assert_clean_probe_disk)
    deploy_offline_bundle(
        config,
        disk,
        firmware_sources=firmware_sources,
        algorithm_sources=algorithm_sources,
    )


def test_deploy_removes_existing_probe_files_before_copying_replacements(tmp_path, monkeypatch):
    config = parse_offline_config(_config())
    disk = tmp_path / "MICROKEEN"
    (disk / "FLM").mkdir(parents=True)
    (disk / "boot.bin").write_bytes(b"old")
    (disk / "FLM" / "STM32F10x_1024.FLM").write_bytes(b"old")
    firmware_sources = []
    for index, name in enumerate(("boot.bin", "rt-thread.hex", "assets.bin")):
        path = tmp_path / f"firmware-replacement-{index}"
        path.write_bytes(name.encode("ascii"))
        firmware_sources.append(path)
    algorithm_sources = []
    for index in range(2):
        path = tmp_path / f"algorithm-replacement-{index}"
        path.write_bytes(bytes([index]))
        algorithm_sources.append(path)

    real_copy2 = __import__("shutil").copy2

    def reject_in_place_overwrite(source, destination):
        destination = Path(destination)
        if destination.exists() and disk in destination.parents:
            raise PermissionError("probe file must be removed before replacement")
        return real_copy2(source, destination)

    monkeypatch.setattr("mklink.offline_download.shutil.copy2", reject_in_place_overwrite)
    deploy_offline_bundle(
        config,
        disk,
        firmware_sources=firmware_sources,
        algorithm_sources=algorithm_sources,
    )

    assert (disk / "boot.bin").read_bytes() == b"boot.bin"
    assert (disk / "FLM" / "STM32F10x_1024.FLM").read_bytes() == b"\x00"


def test_local_flm_selected_from_probe_usb_is_staged_before_same_path_update(tmp_path):
    disk = tmp_path / "MICROKEEN"
    probe_flm = disk / "FLM" / "STM32F10x_1024.FLM"
    probe_flm.parent.mkdir(parents=True)
    probe_flm.write_bytes(b"selected-from-probe")
    payload = _config()
    payload["algorithms"][0].pop("upload_index")
    payload["algorithms"][0]["source_path"] = str(probe_flm)
    config = parse_offline_config(payload)
    assert config.algorithms[0].source_path == str(probe_flm)

    firmware_sources = []
    for index, name in enumerate(("boot.bin", "rt-thread.hex", "assets.bin")):
        path = tmp_path / f"firmware-probe-source-{index}"
        path.write_bytes(name.encode("ascii"))
        firmware_sources.append(path)
    external_flm = tmp_path / "external.flm"
    external_flm.write_bytes(b"external")

    deploy_offline_bundle(
        config,
        disk,
        firmware_sources=firmware_sources,
        algorithm_sources={"internal": probe_flm, "external": external_flm},
    )

    assert probe_flm.read_bytes() == b"selected-from-probe"


def test_v4_trigger_command_selects_the_configured_script():
    assert offline_trigger_command("V4", "factory-line-a.py") == (
        'load.offline("Python/factory-line-a.py")'
    )
    assert offline_trigger_command("V3", "ignored.py") == "load.offline()"


def test_serial_bridge_echo_callback_receives_complete_lines():
    bridge = object.__new__(MKLinkSerialBridge)
    bridge._buffer_lock = threading.Lock()
    bridge._response_buffer = ["first\r\nsecond\n"]
    bridge._echo_offset = 0
    bridge._echo_pending = ""
    bridge._echo_prefix = "[SERIAL] "
    bridge._echo_enabled = False
    lines = []
    bridge._echo_callback = lines.append

    bridge._flush_echo_buffer(final=True)

    assert lines == ["first", "second"]


def test_v3_deploy_forces_offline_download_script_name(tmp_path):
    payload = _config("V3")
    payload["auto_download_count"] = 1
    config = parse_offline_config(payload)
    firmware_sources = []
    for index, name in enumerate(("boot.bin", "rt-thread.hex", "assets.bin")):
        path = tmp_path / f"firmware-{index}"
        path.write_bytes(name.encode("ascii"))
        firmware_sources.append(path)
    algorithm_sources = []
    for index in range(2):
        path = tmp_path / f"algorithm-{index}"
        path.write_bytes(bytes([index]))
        algorithm_sources.append(path)
    disk = tmp_path / "MICROKEEN"
    disk.mkdir()

    deploy_offline_bundle(
        config,
        disk,
        firmware_sources=firmware_sources,
        algorithm_sources=algorithm_sources,
    )

    assert (disk / "python" / "offline_download.py").is_file()
    assert not (disk / "python" / "factory-line-a.py").exists()


def test_detect_probe_model_uses_cmd_get_version():
    with patch("mklink.discovery.find_mklink_cdc_port", return_value="TEST_CDC"), patch(
        "mklink.firmware_check.read_device_version",
        return_value=Version(4, 3, 3),
    ) as read_version:
        assert detect_probe_model() == {"model": "V4", "version": "V4.3.3"}
    read_version.assert_called_once_with("TEST_CDC")


def test_detect_probe_model_retries_a_transient_empty_response():
    with patch("mklink.discovery.find_mklink_cdc_port", return_value="TEST_CDC"), patch(
        "mklink.firmware_check.read_device_version",
        side_effect=[None, Version(4, 3, 4)],
    ) as read_version, patch("mklink.remote.offline_download_api.time.sleep") as sleep:
        assert detect_probe_model() == {"model": "V4", "version": "V4.3.4"}

    assert read_version.call_count == 2
    sleep.assert_called_once_with(0.5)


def test_read_device_version_imports_the_serial_bridge(monkeypatch):
    calls = []

    class Bridge:
        def __init__(self, port):
            calls.append(("init", port))

        def connect(self):
            calls.append(("connect",))
            return True

        def send_command(self, command, timeout):
            calls.append(("send", command, timeout))
            return "V4.3.3"

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr("mklink.bridge.MKLinkSerialBridge", Bridge)

    assert read_device_version("TEST_CDC") == Version(4, 3, 3)
    assert calls == [
        ("init", "TEST_CDC"),
        ("connect",),
        ("send", "cmd.get_version()", 5.0),
        ("close",),
    ]


def test_read_bridge_version_reuses_an_existing_connection():
    class Bridge:
        def send_command(self, command, timeout):
            assert (command, timeout) == ("cmd.get_version()", 5.0)
            return "V4.3.4"

    assert read_bridge_version(Bridge()) == Version(4, 3, 4)


def test_read_device_version_rejects_failed_serial_connection(monkeypatch):
    calls = []

    class Bridge:
        def __init__(self, port):
            calls.append(("init", port))

        def connect(self):
            calls.append(("connect",))
            return False

        def send_command(self, command, timeout):
            raise AssertionError("send_command must not run without a connection")

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr("mklink.bridge.MKLinkSerialBridge", Bridge)

    with pytest.raises(ConnectionError, match="Unable to connect to MKLink CDC port"):
        read_device_version("TEST_CDC")
    assert calls == [("init", "TEST_CDC"), ("connect",), ("close",)]


def test_preview_api_generates_the_resolved_script():
    app = create_app(auth_token=None, project_root=".")
    with TestClient(app) as client:
        response = client.post("/api/offline-download/preview", json=_config())

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "V4"
    assert payload["script_name"] == "factory-line-a.py"
    assert 'load.hex("rt-thread.hex")' in payload["script"]


def test_offline_security_api_reports_v3_and_v4_validated_target():
    app = create_app(auth_token=None, project_root=".")
    with TestClient(app) as client:
        supported = client.get(
            "/api/offline-download/security",
            params={"model": "V3", "part_number": "GD32F303CET6"},
        )
        v4_supported = client.get(
            "/api/offline-download/security",
            params={"model": "V4", "part_number": "GD32F303CET6"},
        )

    assert supported.status_code == 200
    assert supported.json()["supported"] is True
    assert supported.json()["voltage_options_mv"] == [1800, 3300, 5000]
    assert v4_supported.status_code == 200
    assert v4_supported.json()["supported"] is True
    assert v4_supported.json()["voltage_options_mv"] == [1800, 3300, 5000]


def test_preview_accepts_local_stm32f103re_app_after_bootloader(tmp_path, monkeypatch):
    source = tmp_path / "rtthread.bin"
    source.write_bytes(b"\0" * 0x1C210)
    payload = _local_stm32_config(source)
    algorithm = FlashAlgorithm(
        algorithm_id="stm32f103re-internal",
        target_part="STM32F103RE",
        file_name="STM32F10x_512.FLM",
        flash_start=0x08000000,
        flash_size=0x80000,
        ram_start=0x20000000,
        ram_size=0x10000,
        default=True,
        source_kind="installed-pack",
        source_name="Keil.STM32F1xx_DFP@2.4.1",
        source_token="catalog:test",
    )
    app = create_app(auth_token=None, project_root=".")
    _configure_offline_target_metadata(app, monkeypatch, algorithms=(algorithm,))

    with TestClient(app) as client:
        response = client.post("/api/offline-download/preview", json=payload)

    assert response.status_code == 200, response.text
    assert 'load.bin("rtthread.bin", 0x08005000)' in response.json()["script"]


def test_preview_rejects_local_bin_outside_target_flash(tmp_path, monkeypatch):
    source = tmp_path / "rtthread.bin"
    source.write_bytes(b"\0" * 0x2000)
    payload = _local_stm32_config(source, "0x20000000")
    app = create_app(auth_token=None, project_root=".")
    _configure_offline_target_metadata(app, monkeypatch)

    with TestClient(app) as client:
        response = client.post("/api/offline-download/preview", json=payload)

    assert response.status_code == 422
    assert "outside writable Flash" in response.json()["detail"]


def test_preview_rejects_local_bin_address_overflow(tmp_path, monkeypatch):
    source = tmp_path / "rtthread.bin"
    source.write_bytes(b"\0" * 0x20)
    payload = _local_stm32_config(source, "0xFFFFFFF0")
    app = create_app(auth_token=None, project_root=".")
    _configure_offline_target_metadata(app, monkeypatch)

    with TestClient(app) as client:
        response = client.post("/api/offline-download/preview", json=payload)

    assert response.status_code == 422
    assert "overflows 32-bit address space" in response.json()["detail"]


def test_preview_rejects_local_bin_beyond_selected_algorithm(tmp_path, monkeypatch):
    source = tmp_path / "rtthread.bin"
    source.write_bytes(b"\0" * 0x2000)
    payload = _local_stm32_config(source, "0x0801F000")
    algorithm = FlashAlgorithm(
        algorithm_id="small-internal",
        target_part="STM32F103RE",
        file_name="STM32F10x_512.FLM",
        flash_start=0x08000000,
        flash_size=0x20000,
        ram_start=0x20000000,
        ram_size=0x10000,
        default=True,
        source_kind="installed-pack",
        source_name="test",
        source_token="catalog:test",
    )
    app = create_app(auth_token=None, project_root=".")
    _configure_offline_target_metadata(app, monkeypatch, algorithms=(algorithm,))

    with TestClient(app) as client:
        response = client.post("/api/offline-download/preview", json=payload)

    assert response.status_code == 422
    assert "exceeds selected FLM coverage" in response.json()["detail"]


def test_preview_falls_back_to_target_flash_when_unrelated_pack_is_broken(tmp_path, monkeypatch):
    source = tmp_path / "rtthread.bin"
    source.write_bytes(b"\0" * 0x2000)
    payload = _local_stm32_config(source)
    payload["algorithms"][0].update({
        "source_kind": "pack",
        "source_token": "catalog:installed:selected",
    })
    app = create_app(auth_token=None, project_root=".")
    _configure_offline_target_metadata(app, monkeypatch)

    def fail_discovery(*_args, **_kwargs):
        raise OSError("unrelated installed Pack is unreadable")

    monkeypatch.setattr(
        "mklink.cmsis_dap.algorithm_catalog.discover_flash_algorithms",
        fail_discovery,
    )
    with TestClient(app) as client:
        response = client.post("/api/offline-download/preview", json=payload)

    assert response.status_code == 200, response.text
    assert 'load.bin("rtthread.bin", 0x08005000)' in response.json()["script"]


def test_preview_requires_target_for_local_bin(tmp_path):
    source = tmp_path / "rtthread.bin"
    source.write_bytes(b"app")
    payload = _local_stm32_config(source)
    payload.pop("target_part")
    app = create_app(auth_token=None, project_root=".")

    with TestClient(app) as client:
        response = client.post("/api/offline-download/preview", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == "BIN firmware validation requires target_part"


def test_preview_keeps_local_hpm_bin_outside_cmsis_gate(tmp_path):
    source = tmp_path / "hpm-app.bin"
    source.write_bytes(b"hpm-app")
    payload = {
        "model": "V4",
        "script_name": "hpm-offline.py",
        "auto_download_count": 1,
        "wait_idcode_timeout_ms": 10000,
        "swd_clock_hz": 10000000,
        "target_part": "HPM5301xEGx",
        "board": "hpm5301evklite",
        "algorithms": [],
        "firmwares": [{
            "id": "app",
            "file_name": "hpm-app.bin",
            "format": "bin",
            "base_address": "0x80000400",
            "algorithm_id": "",
            "source_path": str(source),
        }],
    }
    app = create_app(auth_token=None, project_root=".")
    app.state.online_flash.catalog.search = lambda *_args, **_kwargs: pytest.fail(
        "HPM preview must not use the CMSIS target catalog"
    )
    app.state.online_flash.target_memory_provider = lambda *_args: pytest.fail(
        "HPM preview must not use the CMSIS target memory map"
    )

    with TestClient(app) as client:
        response = client.post("/api/offline-download/preview", json=payload)

    assert response.status_code == 200, response.text
    assert 'hpm.program("hpm-app.bin", 0x80000400)' in response.json()["script"]


def test_trigger_api_runs_the_configured_v4_script_with_both_resources_leased(monkeypatch):
    calls = []

    class Bridge:
        def __init__(self, port):
            calls.append(("init", port))

        def connect(self):
            calls.append(("connect",))
            return True

        def send_command(self, command, timeout, echo):
            calls.append(("send", command, timeout, echo))
            return "offline download finished"

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr("mklink.bridge.MKLinkSerialBridge", Bridge)
    monkeypatch.setattr("mklink.discovery.find_mklink_cdc_port", lambda: "TEST_CDC")
    app = create_app(auth_token=None, project_root=".")

    with TestClient(app) as client:
        response = client.post(
            "/api/offline-download/trigger",
            json={"model": "V4", "script_name": "factory-line-a.py"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert calls == [
        ("init", "TEST_CDC"),
        ("connect",),
        ("send", 'load.offline("Python/factory-line-a.py")', 600, True),
        ("close",),
    ]
    assert app.state.mklink_state["resource_manager"].get_status() == {}


def test_trigger_api_preempts_and_stops_the_rtt_dashboard(monkeypatch):
    stopped = []

    class Bridge:
        def __init__(self, _port):
            pass

        def connect(self):
            return True

        def send_command(self, _command, timeout, echo=False):
            assert timeout == 600
            assert echo is True
            return "offline download finished"

        def close(self):
            pass

    class RttManager:
        running = True

        def stop(self):
            self.running = False
            stopped.append("rtt")

    monkeypatch.setattr("mklink.bridge.MKLinkSerialBridge", Bridge)
    monkeypatch.setattr("mklink.discovery.find_mklink_cdc_port", lambda: "TEST_CDC")
    app = create_app(auth_token=None, project_root=".")
    manager = app.state.mklink_state["resource_manager"]
    manager.acquire(ResourceGroup.MKLINK_BRIDGE, "user:dashboard:rtt")

    with patch(
        "mklink.remote.dashboards.get_managers", return_value={"rtt": RttManager()},
    ), TestClient(app) as client:
        response = client.post("/api/offline-download/trigger", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert stopped == ["rtt"]
    assert manager.get_status() == {}


def test_offline_api_requires_manual_model_and_reuses_bridge_only_for_trigger():
    calls = []

    class Bridge:
        def send_command(self, command, timeout, echo=False):
            calls.append((command, timeout, echo))
            if command == "cmd.get_version()":
                return "V4.3.4"
            return "offline download finished"

    class Device:
        connected = True
        port = "TEST_CDC"
        _bridge = Bridge()

        def close(self):
            pass

    app = create_app(auth_token=None, project_root=".")
    app.state.mklink_state["device"] = Device()

    with TestClient(app) as client:
        detected = client.post("/api/offline-download/detect-model", json={})
        preview = client.post(
            "/api/offline-download/preview",
            json=_config("V4"),
        )
        triggered = client.post(
            "/api/offline-download/trigger",
            json={"model": "V4", "script_name": "factory-line-a.py"},
        )

    assert detected.status_code in (404, 405)
    assert preview.status_code == 200, preview.text
    assert preview.json()["model"] == "V4"
    assert triggered.status_code == 200, triggered.text
    assert triggered.json()["status"] == "completed"
    assert calls == [('load.offline("Python/factory-line-a.py")', 600, True)]


def test_trigger_api_streams_device_output_before_the_terminal_result(monkeypatch):
    class Bridge:
        def __init__(self, _port):
            pass

        def connect(self):
            return True

        def send_command(self, command, timeout, echo=False, on_output=None):
            assert command == 'load.offline("Python/factory-line-a.py")'
            assert timeout == 600
            assert echo is False
            on_output("erase started")
            on_output("program finished")
            return "erase started\nprogram finished\noffline download finished"

        def close(self):
            pass

    monkeypatch.setattr("mklink.bridge.MKLinkSerialBridge", Bridge)
    monkeypatch.setattr("mklink.discovery.find_mklink_cdc_port", lambda: "TEST_CDC")
    app = create_app(auth_token=None, project_root=".")

    with TestClient(app) as client:
        response = client.post(
            "/api/offline-download/trigger",
            json={"model": "V4", "script_name": "factory-line-a.py"},
            headers={"Accept": "application/x-ndjson"},
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    messages = [json.loads(line) for line in response.text.splitlines() if line]
    assert messages[:2] == [
        {"type": "line", "line": "erase started"},
        {"type": "line", "line": "program finished"},
    ]
    assert messages[-1]["type"] == "result"
    assert messages[-1]["result"]["status"] == "completed"


def test_trigger_stream_keeps_resources_until_the_serial_thread_finishes():
    allow_finish = threading.Event()

    class Bridge:
        def send_command(self, command, timeout, echo=False, on_output=None):
            assert command == 'load.offline("Python/factory-line-a.py")'
            assert timeout == 600
            assert echo is False
            on_output("program started")
            assert allow_finish.wait(timeout=5.0)
            return "program started\noffline download finished"

    class Device:
        connected = True
        port = "TEST_CDC"
        _bridge = Bridge()

        def close(self):
            pass

    app = create_app(auth_token=None, project_root=".")
    app.state.mklink_state["device"] = Device()
    manager = app.state.mklink_state["resource_manager"]
    route = find_route(app, "/api/offline-download/trigger")

    async def exercise_disconnect():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/offline-download/trigger",
            "headers": [(b"accept", b"application/x-ndjson")],
            "app": app,
        }, receive)
        response = await route.endpoint(
            request=request,
            payload={"model": "V4", "script_name": "factory-line-a.py"},
        )
        iterator = response.body_iterator
        await iterator.__anext__()
        await iterator.aclose()
        await asyncio.sleep(0)
        assert manager.get_active_lease(ResourceGroup.MKLINK_BRIDGE) is not None
        assert manager.get_active_lease(ResourceGroup.TARGET_DEBUG) is not None

        allow_finish.set()
        for _ in range(100):
            if manager.get_status() == {}:
                break
            await asyncio.sleep(0.01)
        assert manager.get_status() == {}

    asyncio.run(exercise_disconnect())


def test_deploy_api_writes_uploaded_bundle_to_microkeen_disk(tmp_path, monkeypatch):
    disk = tmp_path / "MICROKEEN"
    disk.mkdir()
    payload = _config()
    payload["target_part"] = "DEVICE_A"
    app = create_app(auth_token=None, project_root=".")
    _configure_offline_target_metadata(
        app,
        monkeypatch,
        target_part="DEVICE_A",
        regions=(
            MemoryRegion("internal", 0x08000000, 0x80000, True, True, 0x800),
            MemoryRegion("external", 0x90000000, 0x100000, True, True, 0x1000),
        ),
    )
    files = [
        ("firmware_files", ("boot.bin", b"boot", "application/octet-stream")),
        ("firmware_files", ("rt-thread.hex", b":00000001FF", "application/octet-stream")),
        ("firmware_files", ("assets.bin", b"assets", "application/octet-stream")),
        ("flm_files", ("internal.flm", b"internal", "application/octet-stream")),
        ("flm_files", ("external.flm", b"external", "application/octet-stream")),
    ]
    with patch("mklink.discovery.find_microkeen_disk", return_value=str(disk)), TestClient(app) as client:
        response = client.post(
            "/api/offline-download/deploy",
            data={"config_json": json.dumps(payload)},
            files=files,
        )

    assert response.status_code == 200, response.text
    assert (disk / "python" / "factory-line-a.py").is_file()
    assert (disk / "boot.bin").read_bytes() == b"boot"
    assert (disk / "FLM" / "STM32F10x_1024.FLM").read_bytes() == b"internal"


def test_deploy_rejects_uploaded_bin_outside_target_flash(tmp_path, monkeypatch):
    disk = tmp_path / "MICROKEEN"
    disk.mkdir()
    payload = _local_stm32_config(tmp_path / "unused.bin", "0x20000000")
    payload["firmwares"][0].pop("source_path")
    payload["firmwares"][0]["upload_index"] = 0
    app = create_app(auth_token=None, project_root=".")
    _configure_offline_target_metadata(app, monkeypatch)

    with patch("mklink.discovery.find_microkeen_disk", return_value=str(disk)), TestClient(app) as client:
        response = client.post(
            "/api/offline-download/deploy",
            data={"config_json": json.dumps(payload)},
            files=[(
                "firmware_files",
                ("rtthread.bin", b"\0" * 0x2000, "application/octet-stream"),
            )],
        )

    assert response.status_code == 422
    assert "outside writable Flash" in response.json()["detail"]
    assert list(disk.iterdir()) == []


def test_deploy_api_reads_current_local_firmware_paths(tmp_path, monkeypatch):
    disk = tmp_path / "MICROKEEN"
    disk.mkdir()
    payload = _config()
    payload["target_part"] = "DEVICE_A"
    for index, firmware in enumerate(payload["firmwares"]):
        source = tmp_path / firmware["file_name"]
        source.write_bytes(f"current-{index}".encode("ascii"))
        firmware.pop("upload_index")
        firmware["source_path"] = str(source)
    app = create_app(auth_token=None, project_root=".")
    _configure_offline_target_metadata(
        app,
        monkeypatch,
        target_part="DEVICE_A",
        regions=(
            MemoryRegion("internal", 0x08000000, 0x80000, True, True, 0x800),
            MemoryRegion("external", 0x90000000, 0x100000, True, True, 0x1000),
        ),
    )
    files = [
        ("flm_files", ("internal.flm", b"internal", "application/octet-stream")),
        ("flm_files", ("external.flm", b"external", "application/octet-stream")),
    ]

    with patch("mklink.discovery.find_microkeen_disk", return_value=str(disk)), TestClient(app) as client:
        response = client.post(
            "/api/offline-download/deploy",
            data={"config_json": json.dumps(payload)},
            files=files,
        )

    assert response.status_code == 200, response.text
    assert (disk / "boot.bin").read_bytes() == b"current-0"
    assert (disk / "rt-thread.hex").read_bytes() == b"current-1"
    assert (disk / "assets.bin").read_bytes() == b"current-2"


def test_deploy_revalidates_local_bin_after_preview(tmp_path, monkeypatch):
    source = tmp_path / "rtthread.bin"
    source.write_bytes(b"\0" * 0x100)
    payload = _local_stm32_config(source)
    algorithm = FlashAlgorithm(
        algorithm_id="small-internal",
        target_part="STM32F103RE",
        file_name="STM32F10x_512.FLM",
        flash_start=0x08000000,
        flash_size=0x20000,
        ram_start=0x20000000,
        ram_size=0x10000,
        default=True,
        source_kind="installed-pack",
        source_name="test",
        source_token="catalog:test",
    )
    app = create_app(auth_token=None, project_root=".")
    _configure_offline_target_metadata(app, monkeypatch, algorithms=(algorithm,))
    disk = tmp_path / "MICROKEEN"
    disk.mkdir()

    with patch("mklink.discovery.find_microkeen_disk", return_value=str(disk)), TestClient(app) as client:
        preview = client.post("/api/offline-download/preview", json=payload)
        source.write_bytes(b"\0" * 0x20000)
        deployed = client.post(
            "/api/offline-download/deploy",
            data={"config_json": json.dumps(payload)},
        )

    assert preview.status_code == 200, preview.text
    assert deployed.status_code == 422
    assert "exceeds selected FLM coverage" in deployed.json()["detail"]
    assert list(disk.iterdir()) == []

@pytest.mark.parametrize('same_source', [False, True])
def test_deploy_reuses_unchanged_usb_files_even_when_replacement_is_locked(tmp_path, monkeypatch, same_source):
    from mklink.offline_download import _transactional_copy
    disk = tmp_path / 'probe'
    destination = disk / 'FLM' / 'Device.FLM'
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b'algorithm')
    source = destination if same_source else tmp_path / 'upload.flm'
    if not same_source:
        source.write_bytes(destination.read_bytes())
    before = destination.stat().st_mtime_ns
    def reject_remove(path):
        pytest.fail('unchanged USB file must not be removed')
    monkeypatch.setattr('mklink.offline_download._remove_probe_file', reject_remove)
    result = _transactional_copy(disk, [(Path('FLM/Device.FLM'), source, None), (Path('python/test.py'), None, b'script')])
    assert result == ['FLM/Device.FLM', 'python/test.py']
    assert destination.read_bytes() == b'algorithm'
    assert destination.stat().st_mtime_ns == before
    assert (disk / 'python/test.py').read_bytes() == b'script'


def test_locked_changed_file_reports_name_and_rolls_back_earlier_changes(tmp_path, monkeypatch):
    from mklink.offline_download import _transactional_copy
    disk = tmp_path / 'probe'
    disk.mkdir()
    (disk / 'app.hex').write_bytes(b'old-app')
    (disk / 'Device.FLM').write_bytes(b'old-flm')
    from mklink.offline_download import _remove_probe_file as remove
    def locked_remove(path):
        if path.name == 'Device.FLM':
            raise PermissionError('file is in use')
        remove(path)
    monkeypatch.setattr('mklink.offline_download._remove_probe_file', locked_remove)
    with pytest.raises(OfflineDownloadError, match='offline file operation failed: Device.FLM'):
        _transactional_copy(disk, [(Path('app.hex'), None, b'new-app'), (Path('Device.FLM'), None, b'new-flm'), (Path('script.py'), None, b'script')])
    assert (disk / 'app.hex').read_bytes() == b'old-app'
    assert (disk / 'Device.FLM').read_bytes() == b'old-flm'
    assert not (disk / 'script.py').exists()
