from __future__ import annotations

from types import SimpleNamespace

import pytest

from mklink import hil_plugin


def _request(method: str, *, verb=None, action_class=None, params=None):
    return {
        "protocol": "hil-plugin-json-v1",
        "request_id": "req-test",
        "method": method,
        "job_id": "job-test",
        "run_kind": "infrastructure",
        "device": {
            "id": "probe0",
            "plugin": "Mklink-AI-Probe",
            "capabilities": ["debug"],
            "transport": {
                "kind": "usb-serial", "port": "COM8",
                "vid": "0x0D28", "pid": "0x0202", "interface": "MI_04",
                "serial": "ABC123",
                "locator": "vid_0d28_pid_0202_sn_abc123_mi_04",
            },
        },
        "verb": verb,
        "action_class": action_class,
        "params": params or {},
        "dut": None,
        "artifact_dir": None,
    }


@pytest.fixture(autouse=True)
def fake_port(monkeypatch):
    info = SimpleNamespace(
        device="COM8", vid=0x0D28, pid=0x0202, serial_number="ABC123",
        hwid="USB VID:PID=0D28:0202 SER=ABC123 LOCATION=1-2:x.4",
        location="1-2:x.4", interface=None,
    )
    monkeypatch.setattr(hil_plugin.list_ports, "comports", lambda: [info])


def test_identify_uses_enumerated_identity():
    result = hil_plugin.dispatch(_request("identify"))
    assert result["ok"] is True
    assert result["data"]["identity"] == {
        "port": "COM8", "serial": "ABC123",
        "locator": "vid_0d28_pid_0202_sn_abc123_mi_04",
    }


def test_identify_rejects_bench_identity_mismatch():
    request = _request("identify")
    request["device"]["transport"]["serial"] = "OTHER"
    result = hil_plugin.dispatch(request)
    assert result["ok"] is False
    assert result["error"]["type"] == "invalid-device"


def test_unattended_emit_is_refused_before_hardware(monkeypatch):
    monkeypatch.setattr(
        hil_plugin, "_transport",
        lambda _request: pytest.fail("transport must not be touched"),
    )
    result = hil_plugin.dispatch(_request(
        "invoke", verb="program.flash", action_class="emit",
        params={"firmware": "should-never-open.hex"},
    ))
    assert result["ok"] is False
    assert result["error"]["type"] == "action-refused"


def test_debug_read_plugin_version_holds_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_CORE_LOCK_ROOT", str(tmp_path / "locks"))
    result = hil_plugin.dispatch(_request(
        "invoke", verb="debug.read", action_class="observe",
        params={"target": "plugin-version"},
    ))
    assert result["ok"] is True
    assert not list((tmp_path / "locks").glob("*.lock"))


def test_health_and_safe_state_are_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_CORE_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setattr("mklink.discovery._probe_port", lambda port: port == "COM8")
    health = hil_plugin.dispatch(_request("health"))
    safe = hil_plugin.dispatch(_request("safe_state"))
    assert health["ok"] is True and health["data"]["verified"] is True
    assert safe["ok"] is True and safe["data"]["verified"] is True
    assert not list((tmp_path / "locks").glob("*.lock"))
