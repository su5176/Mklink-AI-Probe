"""Permanent compatibility coverage for the v0.1.4 + direct-Remote union."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from mklink.remote.agent import AgentConfig, AgentDispatchContext, SiteAgent
from mklink.remote.dispatcher import dispatch_capability
from mklink.remote.protocol import PROTOCOL_VERSION, RequestEnvelope, RequestValidationError
from mklink.remote.resource_manager import ResourceError, ResourceGroup, ResourceManager


ROOT = Path(__file__).resolve().parents[3]


def _offline_config() -> dict:
    return {
        "model": "V4",
        "script_name": "offline_download.py",
        "algorithms": [
            {
                "id": "internal",
                "file_name": "Internal.FLM",
                "flash_base": "0x08000000",
                "ram_base": "0x20000000",
                "source_kind": "upload",
                "upload_index": 0,
            }
        ],
        "firmwares": [
            {
                "id": "boot",
                "file_name": "boot.bin",
                "format": "bin",
                "base_address": "0x08000000",
                "algorithm_id": "internal",
            },
            {
                "id": "app",
                "file_name": "app.bin",
                "format": "bin",
                "base_address": "0x08004000",
                "algorithm_id": "internal",
                "upload_index": 7,
            },
        ],
    }


def test_remote_offline_preview_adapts_opaque_uploads_to_v014(monkeypatch):
    from mklink import offline_download

    parsed_payloads = []
    parse = offline_download.parse_offline_config

    def capture(payload):
        parsed_payloads.append(payload)
        return parse(payload)

    monkeypatch.setattr(offline_download, "parse_offline_config", capture)
    monkeypatch.setattr(
        "mklink.remote.dispatcher.capability_available",
        lambda _name: True,
    )

    result = dispatch_capability(
        "offline.preview",
        {"config": _offline_config()},
    )

    assert parsed_payloads[0]["firmwares"][0]["upload_index"] == 0
    assert parsed_payloads[0]["firmwares"][1]["upload_index"] == 7
    assert all(
        "source_path" not in firmware
        for firmware in parsed_payloads[0]["firmwares"]
    )
    assert result["model"] == "V4"
    assert 'load.bin("boot.bin", 0x08000000)' in result["script"]
    assert 'load.bin("app.bin", 0x08004000)' in result["script"]


def test_remote_offline_preview_rejects_field_machine_source_paths(monkeypatch):
    config = _offline_config()
    config["firmwares"][0]["source_path"] = "field-machine-input.bin"
    monkeypatch.setattr(
        "mklink.remote.dispatcher.capability_available",
        lambda _name: True,
    )

    with pytest.raises(RequestValidationError) as rejected:
        dispatch_capability("offline.preview", {"config": config})

    assert rejected.value.data == {"field": "config.firmwares.source_path"}


def test_local_fastapi_and_site_agent_keep_resource_policies_isolated(tmp_path):
    from mklink.remote.api import create_app

    app = create_app(auth_token=None, project_root=str(tmp_path))
    local_manager = app.state.mklink_state["resource_manager"]
    local_manager.acquire(
        ResourceGroup.TARGET_DEBUG,
        "user:dashboard:local",
    )

    def dispatcher(method, _params, context):
        if method == "resource.seed":
            context.resource_manager.acquire(
                ResourceGroup.TARGET_DEBUG,
                "user:dashboard:field",
            )
            return context.resource_manager.get_status()
        if method == "resource.try":
            try:
                context.resource_manager.acquire(
                    ResourceGroup.TARGET_DEBUG,
                    "user:remote:operation",
                )
            except ResourceError as error:
                return {"conflict_owner": error.conflict_owner}
        raise AssertionError(f"unexpected method: {method}")

    agent = SiteAgent(
        AgentConfig(project_root=str(tmp_path)),
        device_factory=lambda: None,
        request_dispatcher=dispatcher,
    )

    assert agent.handshake().protocol_version == PROTOCOL_VERSION
    seeded = asyncio.run(
        agent._dispatch(RequestEnvelope("resource.seed", {}, 1))
    )
    blocked = asyncio.run(
        agent._dispatch(RequestEnvelope("resource.try", {}, 2))
    )
    local_manager.acquire(
        ResourceGroup.TARGET_DEBUG,
        "user:local:operation",
        preempt_user_dashboard=True,
    )

    assert seeded["target_debug"]["owner"] == "user:dashboard:field"
    assert blocked == {"conflict_owner": "user:dashboard:field"}
    assert local_manager.get_active_lease(
        ResourceGroup.TARGET_DEBUG
    ).owner == "user:local:operation"
    assert {
        route.path for route in app.routes if hasattr(route, "path")
    }.issuperset({"/api/health", "/api/device/hardfault"})


def test_v015_metadata_preserves_core_remote_and_separate_optional_surfaces():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 test hosts
        import tomli as tomllib

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    project = metadata["project"]
    scripts = project["scripts"]
    extras = project["optional-dependencies"]

    assert project["version"] == "0.1.7"
    assert {
        "pyelftools==0.32",
        "pycparser>=2.22,<4",
        "websockets>=11.0",
    }.issubset(project["dependencies"])
    assert scripts == {
        "mklink": "mklink.cli:main",
        "mklink-remote": "mklink.remote.cli:main",
        "mklink-remote-agent": "mklink.remote.cli:agent_main",
        "mklink-site-agent": "mklink.remote.package_agent:main",
        "mklink-remote-mcp": "mklink.remote.mcp:main",
    }
    assert extras["remote"] == ["websockets>=11.0", "intelhex>=2.3"]
    assert extras["mcp"] == ["fastmcp>=2.0", "pydantic<2.13"]
    assert {
        "build==1.5.0",
        "pyinstaller==6.18.0",
        "setuptools==80.9.0",
        "wheel==0.45.1",
    } == set(extras["site-agent-build"])
    assert not any("fastmcp" in item.casefold() for item in project["dependencies"])
    assert not any("pyinstaller" in item.casefold() for item in extras["remote"])


def test_dispatcher_matches_v014_device_signatures_and_serializes_richer_results(
    tmp_path,
    monkeypatch,
):
    from mklink.device import Device

    @dataclass(frozen=True)
    class RichResult:
        source: Path
        payload: bytes
        values: tuple[int, ...]

    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x01\x02")
    device = create_autospec(Device, instance=True)
    device.flash.return_value = RichResult(
        source=firmware,
        payload=b"\x01\x02",
        values=(1, 2),
    )
    device.memory_map.return_value = {
        "source": tmp_path / "firmware.axf",
        "payload": b"\x03",
    }
    device.rtt_start.return_value = RichResult(
        source=tmp_path / "rtt.map",
        payload=b"\x04",
        values=(3,),
    )
    device.rtt_stop.return_value = "stopped"
    device.decode_hardfault.return_value = {
        "frames": (
            {
                "source": tmp_path / "fault.c",
                "payload": b"\x05",
            },
        ),
    }
    manager = ResourceManager()
    context = AgentDispatchContext(device=device, resource_manager=manager)
    uploads = SimpleNamespace(resolve=lambda _reference: firmware)
    stream_owners: dict[str, str] = {}
    monkeypatch.setattr(
        "mklink.remote.dispatcher.capability_available",
        lambda _name: True,
    )

    flashed = dispatch_capability(
        "flash.program",
        {
            "firmware": "remote-file:opaque",
            "target_part": "TEST123",
            "verify": True,
            "confirm": True,
        },
        context=context,
        upload_manager=uploads,
    )
    memory_map = dispatch_capability(
        "symbols.memory_map",
        {},
        context=context,
    )
    started = dispatch_capability(
        "rtt.start",
        {
            "addr": 0x20000000,
            "channel": 0,
            "search_size": 4096,
            "mode": "static",
        },
        context=context,
        stream_owners=stream_owners,
    )
    stopped = dispatch_capability(
        "rtt.stop",
        {},
        context=context,
        stream_owners=stream_owners,
    )
    decoded = dispatch_capability(
        "hardfault.decode",
        {"fault_regs": {"cfsr": 1}},
        context=context,
        stream_owners=stream_owners,
    )

    device.flash.assert_called_once_with(
        str(firmware),
        target_part="TEST123",
        verify=True,
    )
    device.memory_map.assert_called_once_with()
    device.rtt_start.assert_called_once_with(
        0x20000000,
        channel=0,
        search_size=4096,
        mode="static",
    )
    device.decode_hardfault.assert_called_once_with({"cfsr": 1})
    device.rtt_stop.assert_called_once_with()
    assert flashed == {
        "source": "firmware.bin",
        "payload": {"__bytes__": "AQI="},
        "values": [1, 2],
    }
    assert memory_map == {
        "source": "firmware.axf",
        "payload": {"__bytes__": "Aw=="},
    }
    assert started == {
        "source": "rtt.map",
        "payload": {"__bytes__": "BA=="},
        "values": [3],
    }
    assert decoded == {
        "frames": [
            {
                "source": "fault.c",
                "payload": {"__bytes__": "BQ=="},
            }
        ]
    }
    assert stopped == "stopped"
    assert manager.get_status() == {}
    assert stream_owners == {}
