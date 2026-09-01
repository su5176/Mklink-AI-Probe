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
