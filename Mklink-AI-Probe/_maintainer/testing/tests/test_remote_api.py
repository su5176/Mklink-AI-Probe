"""Regression tests for the FastAPI remote API."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from mklink.dwarf_parser import DwarfArray, DwarfInfo, DwarfMember, DwarfStruct, DwarfVariable
from mklink.remote.api import BrowserSessionLease, _project_root_drives, create_app
from mklink.symbol_catalog import SymbolCatalog
from route_utils import find_route


def _route_endpoint(app, path):
    return find_route(app, path).endpoint


def _request(client, path, responses, key):
    try:
        responses[key] = client.get(path)
    except BaseException as exc:  # Preserve failures raised in request threads.
        responses[key] = exc


def test_browser_session_lease_waits_for_the_last_tab_and_never_arms_empty():
    now = [0.0]
    lease = BrowserSessionLease(
        10, close_grace=2, startup_grace=60, clock=lambda: now[0],
    )

    assert lease.should_exit() is False
    now[0] = 59
    assert lease.should_exit() is False
    now[0] = 0
    assert lease.renew("first") == 1
    assert lease.renew("second") == 2
    assert lease.release("first") == 1
    now[0] = 20
    assert lease.should_exit() is False
    now[0] = 22
    assert lease.should_exit() is True


def test_browser_session_endpoints_are_enabled_only_for_browser_owned_server():
    default_app = create_app(auth_token=None, project_root=".")
    browser_app = create_app(
        auth_token=None, project_root=".", browser_session_timeout=15,
    )

    with TestClient(default_app) as default_client:
        assert default_client.post(
            "/api/browser-session/heartbeat", json={"client_id": "tab"},
        ).json() == {"enabled": False}
    with TestClient(browser_app) as browser_client:
        assert browser_client.post(
            "/api/browser-session/heartbeat", json={"client_id": "tab"},
        ).json() == {"enabled": True, "clients": 1}
        assert browser_client.post(
            "/api/browser-session/release", json={"client_id": "tab"},
        ).json() == {"enabled": True, "clients": 0}


def test_last_browser_session_release_closes_the_shared_device():
    app = create_app(
        auth_token=None, project_root=".", browser_session_timeout=15,
    )
    device = SimpleNamespace(connected=True, close=lambda: None)
    app.state.mklink_state["device"] = device
    app.state.mklink_state["dispatcher"] = object()

    with patch.object(device, "close") as close, TestClient(app) as client:
        assert client.post(
            "/api/browser-session/heartbeat", json={"client_id": "tab"},
        ).json() == {"enabled": True, "clients": 1}
        assert client.post(
            "/api/browser-session/release", json={"client_id": "tab"},
        ).json() == {"enabled": True, "clients": 0}

    close.assert_called_once_with()
    assert app.state.mklink_state["device"] is None
    assert app.state.mklink_state["dispatcher"] is None


def test_browser_session_websockets_keep_backend_until_the_last_tab_closes():
    app = create_app(
        auth_token=None, project_root=".", browser_session_timeout=15,
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/browser-session?client_id=first",
        ) as first, client.websocket_connect(
            "/ws/browser-session?client_id=second",
        ) as second:
            assert app.state.browser_sessions.release("first") == 1
            first.close()
            assert app.state.browser_sessions.should_exit() is False
            second.close()


def test_app_shutdown_closes_the_shared_device_and_clears_resource_leases():
    from mklink.remote.resource_manager import ResourceGroup

    app = create_app(auth_token=None, project_root=".")
    state = app.state.mklink_state
    device = SimpleNamespace(close=lambda: None)
    state["device"] = device
    state["dispatcher"] = object()
    state["resource_manager"].acquire(
        ResourceGroup.TARGET_DEBUG, "test:shutdown",
    )

    with patch.object(device, "close") as close, TestClient(app):
        pass

    close.assert_called_once_with()
    assert state["device"] is None
    assert state["dispatcher"] is None
    assert state["resource_manager"].get_status() == {}


def test_desktop_shutdown_requires_the_owning_instance_and_requests_exit():
    app = create_app(
        auth_token=None, project_root=".", desktop_instance_id="instance-a",
    )
    requested = []
    app.state.request_desktop_exit = lambda: requested.append(True)

    with TestClient(app) as client:
        rejected = client.post(
            "/api/desktop/shutdown", json={"instance_id": "instance-b"},
        )
        accepted = client.post(
            "/api/desktop/shutdown", json={"instance_id": "instance-a"},
        )

    assert rejected.status_code == 403
    assert accepted.json() == {"status": "shutting_down"}
    assert requested == [True]


def test_desktop_shutdown_is_hidden_from_non_desktop_servers():
    app = create_app(auth_token=None, project_root=".")
    with TestClient(app) as client:
        response = client.post(
            "/api/desktop/shutdown", json={"instance_id": "instance-a"},
        )
    assert response.status_code == 404


def test_project_browser_exposes_native_roots_on_each_desktop_platform():
    assert _project_root_drives(system="Linux") == ["/"]
    assert _project_root_drives(system="Darwin") == ["/"]
    assert _project_root_drives(
        system="Windows", exists=lambda path: path in {"C:\\", "E:\\"},
    ) == ["C:", "E:"]


def test_port_discovery_does_not_block_health_check():
    discovery_started = threading.Event()
    release_discovery = threading.Event()
    responses = {}

    def blocking_discovery():
        discovery_started.set()
        assert release_discovery.wait(timeout=2)
        return "COM42"

    app = create_app(auth_token=None, project_root=".")
    with patch(
        "mklink.discovery.find_mklink_cdc_port",
        side_effect=blocking_discovery,
    ), TestClient(app, raise_server_exceptions=False) as client:
        discover_thread = threading.Thread(
            target=_request,
            args=(client, "/api/ports/discover", responses, "discover"),
        )
        health_thread = threading.Thread(
            target=_request,
            args=(client, "/api/health", responses, "health"),
        )
        discover_thread.start()
        try:
            assert discovery_started.wait(timeout=1)
            health_thread.start()
            health_thread.join(timeout=0.5)
            assert not health_thread.is_alive(), (
                "health check was blocked by port discovery"
            )
        finally:
            release_discovery.set()
            discover_thread.join(timeout=2)
            if health_thread.ident is not None:
                health_thread.join(timeout=2)

        assert not discover_thread.is_alive()
        assert not health_thread.is_alive()
        assert responses["health"].status_code == 200
        assert responses["health"].json()["status"] == "ok"
        assert responses["discover"].status_code == 200
        assert responses["discover"].json() == {"port": "COM42"}


def test_port_discovery_failure_returns_500_and_server_remains_healthy():
    app = create_app(auth_token=None, project_root=".")
    with patch(
        "mklink.discovery.find_mklink_cdc_port",
        side_effect=RuntimeError("scan failed"),
    ), TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/ports/discover")
        health = client.get("/api/health")

    assert response.status_code == 500
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_config_api_rejects_swd_clock_above_10_mhz(tmp_path):
    app = create_app(auth_token=None, project_root=str(tmp_path))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.put("/api/config", json={"swd_clock": "10000001"})

    assert response.status_code == 422
    assert "10 MHz" in response.json()["detail"]


def test_browser_symbol_upload_persists_in_a_controlled_project_directory(tmp_path):
    app = create_app(auth_token=None, project_root=str(tmp_path))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/files/symbol",
            files={"file": ("firmware.axf", b"\x7fELFtest", "application/octet-stream")},
        )

    assert response.status_code == 200
    payload = response.json()
    stored = Path(payload["path"])
    assert stored.parent == (tmp_path / ".mklink" / "uploads" / "file-sources").resolve()
    assert stored.suffix == ".axf"
    assert stored.read_bytes() == b"\x7fELFtest"
    assert payload["name"] == "firmware.axf"


def test_browser_symbol_upload_rejects_an_unsupported_suffix(tmp_path):
    app = create_app(auth_token=None, project_root=str(tmp_path))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/files/symbol",
            files={"file": ("firmware.txt", b"not an elf", "text/plain")},
        )

    assert response.status_code == 400
    assert not (tmp_path / ".mklink" / "uploads" / "file-sources").exists()


def test_browser_map_upload_uses_the_map_only_endpoint(tmp_path):
    app = create_app(auth_token=None, project_root=str(tmp_path))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/files/map",
            files={"file": ("firmware.map", b"memory map", "text/plain")},
        )

    assert response.status_code == 200
    stored = Path(response.json()["path"])
    assert stored.suffix == ".map"
    assert stored.read_bytes() == b"memory map"


def test_rtt_find_uses_explicit_source_without_persisting_project_config(tmp_path):
    source = tmp_path / "firmware.axf"
    result = SimpleNamespace(
        addr="0x20001A40",
        source="binary:firmware.axf",
        details=["resolved _SEGGER_RTT"],
        warnings=["symbol tool fallback used"],
    )
    with patch(
        "mklink.rtt_addr.diagnose_rtt_addr", return_value=result,
    ) as diagnose, patch(
        "mklink.project_config.load_keil_project",
        side_effect=AssertionError("explicit source must bypass project discovery"),
    ), patch("mklink.project_config.save_rtt_config") as save_rtt_config:
        app = create_app(auth_token=None, project_root=str(tmp_path))
        with TestClient(app) as client:
            response = client.post(
                "/api/rtt-find", json={"source_path": str(source)},
            )

    assert response.status_code == 200
    assert response.json() == {
        "found": True,
        "addr": "0x20001A40",
        "source": "binary:firmware.axf",
        "source_path": str(source),
        "details": ["resolved _SEGGER_RTT"],
        "warnings": ["symbol tool fallback used"],
    }
    diagnose.assert_called_once_with(str(source))
    save_rtt_config.assert_not_called()


def test_rtt_find_runs_symbol_diagnosis_outside_the_event_loop_thread(tmp_path):
    result = SimpleNamespace(
        addr="0x20001A40", source="binary:firmware.axf", details=[], warnings=[],
    )
    call_threads = []

    def diagnose(_path):
        call_threads.append(threading.get_ident())
        return result

    app = create_app(auth_token=None, project_root=str(tmp_path))
    endpoint = _route_endpoint(app, "/api/rtt-find")
    event_loop_thread = threading.get_ident()
    with patch("mklink.rtt_addr.diagnose_rtt_addr", side_effect=diagnose):
        response = asyncio.run(endpoint(source_path="firmware.axf"))

    assert response["found"] is True
    assert call_threads == [call_threads[0]]
    assert call_threads[0] != event_loop_thread


def test_rtt_find_explicit_map_returns_parser_details_without_persisting(tmp_path):
    source = tmp_path / "firmware.map"
    result = SimpleNamespace(
        addr=None,
        source="",
        details=["未找到 _SEGGER_RTT 地址"],
        warnings=["检查链接输出"],
    )
    with patch(
        "mklink.rtt_addr.diagnose_rtt_addr", return_value=result,
    ) as diagnose, patch("mklink.project_config.save_rtt_config") as save_rtt_config:
        app = create_app(auth_token=None, project_root=str(tmp_path))
        with TestClient(app) as client:
            response = client.post(
                "/api/rtt-find", json={"source_path": str(source)},
            )

    assert response.status_code == 200
    assert response.json() == {
        "found": False,
        "addr": None,
        "source": "",
        "source_path": str(source),
        "details": ["未找到 _SEGGER_RTT 地址"],
        "warnings": ["检查链接输出"],
    }
    diagnose.assert_called_once_with(str(source))
    save_rtt_config.assert_not_called()


def test_rtt_find_explicit_missing_file_returns_actionable_details(tmp_path):
    source = tmp_path / "missing.map"
    app = create_app(auth_token=None, project_root=str(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/rtt-find", json={"source_path": str(source)},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["source_path"] == str(source)
    assert any("文件不存在" in detail for detail in body["details"])


def test_rtt_find_without_source_preserves_project_discovery_and_persistence(tmp_path):
    map_path = tmp_path / "firmware.map"
    result = SimpleNamespace(
        addr="0x20001A40",
        source="map:firmware.map",
        details=["resolved _SEGGER_RTT"],
        warnings=[],
    )
    with patch(
        "mklink.project_config.load_keil_project",
        return_value={"map_path": str(map_path)},
    ), patch(
        "mklink.rtt_addr.diagnose_rtt_addr", return_value=result,
    ) as diagnose, patch(
        "mklink.project_config.load_rtt_config", return_value={"mode": 0},
    ), patch("mklink.project_config.save_rtt_config") as save_rtt_config:
        app = create_app(auth_token=None, project_root=str(tmp_path))
        with TestClient(app) as client:
            response = client.post("/api/rtt-find")

    assert response.status_code == 200
    assert response.json() == {
        "found": True,
        "addr": "0x20001A40",
        "source": "map:firmware.map",
        "source_path": str(map_path),
        "details": ["resolved _SEGGER_RTT"],
        "warnings": [],
        "map_path": str(map_path),
    }
    diagnose.assert_called_once_with(str(map_path))
    save_rtt_config.assert_called_once_with(
        str(tmp_path), {"mode": 0, "rtt_addr": "0x20001A40"},
    )


def _connected_symbol_device(tmp_path):
    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    dwarf = DwarfInfo(
        base_types={1: ("float", 4), 2: ("bool", 1)},
        structs={
            "Controller": DwarfStruct(
                "Controller",
                3,
                8,
                [
                    DwarfMember("target", 0, 1, "float", 4),
                    DwarfMember("enabled", 4, 2, "bool", 1),
                ],
            )
        },
        variables={
            "gain": DwarfVariable("gain", 10, 1, 0x20000010, 4, "float"),
            "controller": DwarfVariable("controller", 11, 3, 0x20000020, 8, "Controller"),
        },
    )
    catalog = SymbolCatalog.from_dwarf(
        dwarf,
        axf_path=str(axf),
        ram_ranges=[(0x20000000, 0x20010000)],
    )
    device = SimpleNamespace(
        connected=True,
        state=SimpleNamespace(name="READY"),
        mcu_name="STM32F103RC",
        idcode=0x1234,
        port="redacted",
        axf_status={"loaded": True},
        symbol_catalog=catalog,
        _dwarf_info=dwarf,
        close=lambda: None,
    )
    device.parse_axf = lambda _path=None: {"loaded": True, "catalog_generation": catalog.generation}
    return device, axf


def _connected_large_array_device(tmp_path):
    device, axf = _connected_symbol_device(tmp_path)
    dwarf = device._dwarf_info
    dwarf.base_types[4] = ("uint32_t", 4)
    dwarf.arrays[5] = DwarfArray(
        5, element_type_offset=4, dimensions=(1000,), size=4000,
    )
    dwarf.variables["values"] = DwarfVariable(
        "values", 12, 5, 0x20001000, 4000, "uint32_t[]",
    )
    device.symbol_catalog = SymbolCatalog.from_dwarf(
        dwarf,
        axf_path=str(axf),
        ram_ranges=[(0x20000000, 0x20010000)],
    )
    return device, axf


def test_symbol_catalog_api_lists_valid_variables_immediately(tmp_path):
    device, _axf = _connected_symbol_device(tmp_path)
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device), TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.get("/api/symbols/catalog?limit=100")

    assert response.status_code == 200
    body = response.json()
    assert body["generation"] == 1
    assert [item["path"] for item in body["items"]] == [
        "controller.enabled", "controller.target", "gain",
    ]


def test_symbol_browse_api_loads_any_large_array_range_and_exact_search(tmp_path):
    device, _axf = _connected_large_array_device(tmp_path)
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device), TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        roots = client.get("/api/symbols/browse")
        ranges = client.get("/api/symbols/browse", params={"path": "values"})
        tail = client.get(
            "/api/symbols/browse", params={"path": "values", "offset": 768},
        )
        search = client.get("/api/symbols/search", params={"q": "values[999]"})
        typeinfo = client.get(
            "/api/symbols/typeinfo", params={"name": "values[999]"},
        )

    assert roots.status_code == 200
    values = next(node for node in roots.json()["nodes"] if node["path"] == "values")
    assert values["kind"] == "branch"
    assert values["child_count"] == 1000
    assert [node["label"] for node in ranges.json()["nodes"]] == [
        "[0..255]", "[256..511]", "[512..767]", "[768..999]",
    ]
    tail_nodes = tail.json()["nodes"]
    assert len(tail_nodes) == 232
    assert tail_nodes[0]["descriptor"]["path"] == "values[768]"
    assert tail_nodes[-1]["descriptor"]["path"] == "values[999]"
    assert [item["name"] for item in search.json()["results"]] == ["values[999]"]
    assert typeinfo.json()["address"] == 0x20001000 + 999 * 4


def test_symbol_typeinfo_accepts_flattened_catalog_path(tmp_path):
    device, _axf = _connected_symbol_device(tmp_path)
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device), TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.get("/api/symbols/typeinfo?name=controller.target")

    assert response.status_code == 200
    assert response.json() == {
        "name": "controller.target",
        "found": True,
        "type": "float",
        "size": 4,
        "address": 0x20000020,
        "members": [],
    }


def test_symbol_search_and_typeinfo_expose_only_runtime_catalog_leaves(tmp_path):
    device, _axf = _connected_symbol_device(tmp_path)
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device), TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        search = client.get("/api/symbols/search?q=controller")
        root_type = client.get("/api/symbols/typeinfo?name=controller")

    assert search.status_code == 200
    assert [item["name"] for item in search.json()["results"]] == [
        "controller.enabled",
        "controller.target",
    ]
    assert root_type.status_code == 200
    assert root_type.json() == {"name": "controller", "found": False}


def test_symbol_status_marks_changed_axf_stale(tmp_path):
    device, axf = _connected_symbol_device(tmp_path)
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device), TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        time.sleep(0.01)
        axf.write_bytes(b"changed")
        response = client.get("/api/symbols/status")

    assert response.status_code == 200
    assert response.json()["stale"] is True


def test_symbol_c_layout_uses_shared_superwatch_transaction(tmp_path):
    from mklink.c_layout import parse_c_layout
    from mklink.remote.dashboards import get_managers

    device, _axf = _connected_symbol_device(tmp_path)
    manager = get_managers()["superwatch"]
    app = create_app(auth_token=None, project_root=".")
    definition = "typedef struct { float target; bool enabled; char pad[3]; } Controller;"
    layout = parse_c_layout(definition, preferred_type="Controller")

    def apply(variable, source, pack, *, device: object):
        assert variable == "controller"
        assert source == definition
        assert pack == 4
        device.symbol_catalog = device.symbol_catalog.with_c_layout(
            "controller", 0x20000020, layout
        )
        return {
            "layout": layout.to_dict(),
            "rebind": {"preserved": [], "updated": [], "removed": []},
        }

    with patch("mklink.connect", return_value=device), patch.object(
        manager, "apply_c_definition", side_effect=apply
    ) as apply_mock, TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post("/api/symbols/c-layout", json={
            "variable": " controller ",
            "definition": definition,
            "pack": 4,
        })

    assert response.status_code == 200
    assert response.json()["layout"]["leaf_count"] == 5
    assert response.json()["generation"] == 2
    apply_mock.assert_called_once_with(
        "controller", definition, 4, device=device
    )


def test_symbol_c_layout_reports_validation_failure_as_422(tmp_path):
    from mklink.c_layout import CLayoutError
    from mklink.remote.dashboards import SuperWatchTransactionError, get_managers

    device, _axf = _connected_symbol_device(tmp_path)
    manager = get_managers()["superwatch"]
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device), patch.object(
        manager,
        "apply_c_definition",
        side_effect=SuperWatchTransactionError(
            "c_layout", CLayoutError("layout size mismatch")
        ),
    ), TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post("/api/symbols/c-layout", json={
            "variable": "controller",
            "definition": "typedef struct { int bad; } Controller;",
        })

    assert response.status_code == 422
    assert response.json()["detail"]["phase"] == "c_layout"
    assert response.json()["detail"]["message"] == "layout size mismatch"


def test_superwatch_typed_write_route_passes_path_generation_and_value(tmp_path):
    from mklink.remote.dashboards import get_managers

    device, _axf = _connected_symbol_device(tmp_path)
    manager = get_managers()["superwatch"]
    manager._device = device
    app = create_app(auth_token=None, project_root=".")
    result = {"path": "gain", "generation": 1, "value": 1.5, "verified": True}

    with patch("mklink.connect", return_value=device), patch.object(
        manager, "write_symbol", return_value=result
    ) as write_symbol, TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post(
            "/api/dash/superwatch/write",
            json={"path": "gain", "generation": 1, "value": 1.5},
        )

    assert response.status_code == 200
    assert response.json() == result
    write_symbol.assert_called_once_with("gain", generation=1, value=1.5)


def test_superwatch_typed_write_reports_transaction_phase(tmp_path):
    from mklink.remote.dashboards import SuperWatchTransactionError, get_managers

    device, _axf = _connected_symbol_device(tmp_path)
    manager = get_managers()["superwatch"]
    manager._device = device
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device), patch.object(
        manager,
        "write_symbol",
        side_effect=SuperWatchTransactionError("write", RuntimeError("flush failed")),
    ), TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post(
            "/api/dash/superwatch/write",
            json={"path": "gain", "generation": 1, "value": 2.0},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "superwatch_transaction_failed",
        "phase": "write",
        "message": "flush failed",
    }


def test_symbol_reparse_uses_superwatch_transaction_when_prepared(tmp_path):
    from mklink.remote.dashboards import get_managers

    device, _axf = _connected_symbol_device(tmp_path)
    manager = get_managers()["superwatch"]
    manager._device = device
    manager._runtime = SimpleNamespace(items=[])
    app = create_app(auth_token=None, project_root=".")
    summary = {"preserved": ["gain"], "updated": [], "removed": []}

    with patch("mklink.connect", return_value=device), patch.object(
        manager, "reparse_symbols", return_value=summary
    ) as reparse, TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post("/api/symbols/reparse")

    assert response.status_code == 200
    assert response.json() == summary
    reparse.assert_called_once_with(device=device)


def test_device_parse_axf_rebinds_prepared_superwatch_runtime(tmp_path):
    from mklink.remote.dashboards import get_managers

    device, _axf = _connected_symbol_device(tmp_path)
    next_axf = tmp_path / "next.axf"
    next_axf.write_bytes(b"next")
    device.axf_status = {
        "loaded": True,
        "axf_path": str(next_axf),
        "variable_count": 8,
    }
    manager = get_managers()["superwatch"]
    manager._device = device
    manager._runtime = SimpleNamespace(items=[])
    app = create_app(auth_token=None, project_root=".")
    summary = {"preserved": ["gain"], "updated": [], "removed": []}

    with patch("mklink.connect", return_value=device), patch.object(
        manager, "reparse_symbols", return_value=summary
    ) as reparse, TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post(
            "/api/device/parse-axf",
            json={"axf": str(next_axf)},
        )

    assert response.status_code == 200
    assert response.json()["variable_count"] == 8
    assert response.json()["rebind"] == summary
    reparse.assert_called_once_with(str(next_axf), device=device)


def test_device_parse_axf_rebinds_stale_superwatch_device(tmp_path):
    from mklink.remote.dashboards import get_managers

    current_device, _axf = _connected_symbol_device(tmp_path)
    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    stale_device, _stale_axf = _connected_symbol_device(stale_root)
    next_axf = tmp_path / "next.axf"
    next_axf.write_bytes(b"next")
    manager = get_managers()["superwatch"]
    manager._device = stale_device
    manager._runtime = SimpleNamespace(
        items=[],
        symbol_catalog=stale_device.symbol_catalog,
        svd_registers={},
    )
    app = create_app(auth_token=None, project_root=".")
    summary = {"preserved": [], "updated": [], "removed": []}

    def reparse(path, *, device):
        assert device is current_device
        manager._device = device
        current_device.axf_status = {
            "loaded": True,
            "axf_path": str(next_axf),
            "variable_count": 8,
        }
        return summary

    with patch("mklink.connect", return_value=current_device), patch.object(
        manager, "reparse_symbols", side_effect=reparse
    ) as reparse_mock, TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post(
            "/api/device/parse-axf",
            json={"axf": str(next_axf)},
        )

    assert response.status_code == 200
    assert response.json()["axf_path"] == str(next_axf)
    assert response.json()["variable_count"] == 8
    assert manager._device is current_device
    reparse_mock.assert_called_once_with(str(next_axf), device=current_device)


def test_device_parse_axf_rejects_a_false_success_with_the_old_source(tmp_path):
    from mklink.remote.dashboards import get_managers

    device, current_axf = _connected_symbol_device(tmp_path)
    device.axf_status = {
        "loaded": True,
        "axf_path": str(current_axf),
        "variable_count": 4,
    }
    next_axf = tmp_path / "next.axf"
    next_axf.write_bytes(b"next")
    manager = get_managers()["superwatch"]
    manager._device = device
    manager._runtime = SimpleNamespace(
        items=[],
        symbol_catalog=device.symbol_catalog,
        svd_registers={},
    )
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device), patch.object(
        manager,
        "reparse_symbols",
        return_value={"preserved": [], "updated": [], "removed": []},
    ), TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post(
            "/api/device/parse-axf",
            json={"axf": str(next_axf)},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "symbol_source_mismatch",
        "message": "Symbol parsing completed without activating the requested AXF",
        "requested_axf": str(next_axf),
        "active_axf": str(current_axf),
    }


def test_device_connect_refreshes_symbols_when_already_connected(tmp_path):
    from mklink.remote.dashboards import get_managers

    device, _axf = _connected_symbol_device(tmp_path)
    next_axf = tmp_path / "next.axf"
    next_axf.write_bytes(b"next")
    manager = get_managers()["superwatch"]
    manager._device = device
    manager._runtime = SimpleNamespace(
        items=[],
        symbol_catalog=device.symbol_catalog,
        svd_registers={},
    )
    app = create_app(auth_token=None, project_root=".")

    def reparse(path, *, device: object):
        manager._device = device
        device.axf_status = {
            "loaded": True,
            "axf_path": str(next_axf),
            "variable_count": 8,
        }
        return {"preserved": [], "updated": [], "removed": []}

    with patch("mklink.connect", return_value=device) as connect, patch.object(
        manager, "reparse_symbols", side_effect=reparse
    ) as reparse_mock, TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post(
            "/api/device/connect",
            json={"axf": str(next_axf)},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "already_connected"
    assert response.json()["axf"]["axf_path"] == str(next_axf)
    assert connect.call_count == 1
    reparse_mock.assert_called_once_with(str(next_axf), device=device)


def test_device_restore_last_without_axf_keeps_shared_connection(tmp_path):
    from mklink.remote.dashboards import get_managers

    device, _axf = _connected_symbol_device(tmp_path)
    device.axf_status = {
        "loaded": False,
        "axf_path": None,
        "elf_backend": "builtin",
    }
    device.symbol_catalog = None
    device._dwarf_info = None

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("restore-last must not parse without an AXF path")

    device.parse_axf = unexpected_parse
    manager = get_managers()["superwatch"]
    manager._device = device
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device) as connect, TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post(
            "/api/device/connect",
            json={"restore_last": True},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "already_connected"
    assert response.json()["axf_loaded"] is False
    assert response.json()["elf_backend"] == "builtin"
    assert connect.call_count == 1


def test_device_connect_forwards_explicit_elf_backend(tmp_path):
    device, _axf = _connected_symbol_device(tmp_path)
    app = create_app(auth_token=None, project_root=".")

    with patch("mklink.connect", return_value=device) as connect, TestClient(app) as client:
        response = client.post(
            "/api/device/connect", json={"elf_backend": "external"}
        )

    assert response.status_code == 200
    connect.assert_called_once()
    assert connect.call_args.kwargs["elf_backend"] == "external"


def test_device_parse_axf_forwards_explicit_elf_backend(tmp_path):
    from mklink.remote.dashboards import get_managers

    device, _axf = _connected_symbol_device(tmp_path)
    next_axf = tmp_path / "next.axf"
    next_axf.write_bytes(b"next")
    device.axf_status = {"loaded": True, "axf_path": str(next_axf)}
    manager = get_managers()["superwatch"]
    manager._device = device
    manager._runtime = SimpleNamespace(items=[])
    app = create_app(auth_token=None, project_root=".")
    summary = {"preserved": [], "updated": [], "removed": []}

    with patch("mklink.connect", return_value=device), patch.object(
        manager, "reparse_symbols", return_value=summary
    ) as reparse, TestClient(app) as client:
        assert client.post("/api/device/connect", json={}).status_code == 200
        response = client.post(
            "/api/device/parse-axf",
            json={"axf": str(next_axf), "elf_backend": "external"},
        )

    assert response.status_code == 200
    reparse.assert_called_once_with(str(next_axf), "external", device=device)


def test_health_reports_builtin_elf_capability():
    app = create_app(auth_token=None, project_root=".")

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["elf_backend"] == "builtin"
    assert body["builtin_elf_available"] is True


def test_web_app_shell_is_not_cached_but_hashed_assets_are_immutable():
    app = create_app(auth_token=None, project_root=".")

    with TestClient(app) as client:
        index = client.get("/")
        fallback = client.get("/config")
        asset_path = re.search(r'src="(/assets/[^"]+\.js)"', index.text).group(1)
        asset = client.get(asset_path)

    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-store, max-age=0"
    assert index.headers["pragma"] == "no-cache"
    assert fallback.status_code == 200
    assert fallback.headers["cache-control"] == "no-store, max-age=0"
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
