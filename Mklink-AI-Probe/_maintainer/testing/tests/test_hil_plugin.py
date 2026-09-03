from __future__ import annotations

from types import SimpleNamespace

import pytest

from mklink import hil_plugin


TEST_PORT = "TEST_PORT"


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
                "kind": "usb-serial",
                "port": TEST_PORT,
                "vid": "0x0D28",
                "pid": "0x0202",
                "interface": "MI_04",
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
        device=TEST_PORT,
        vid=0x0D28,
        pid=0x0202,
        serial_number="ABC123",
        hwid="USB VID:PID=0D28:0202 SER=ABC123 LOCATION=1-2:x.4",
        location="1-2:x.4",
        interface=None,
    )
    monkeypatch.setattr(hil_plugin.list_ports, "comports", lambda: [info])


def test_identify_uses_enumerated_identity():
    result = hil_plugin.dispatch(_request("identify"))

    assert result["ok"] is True
    assert result["data"]["identity"] == {
        "port": TEST_PORT,
        "serial": "ABC123",
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
        hil_plugin,
        "_transport",
        lambda _request: pytest.fail("transport must not be touched"),
    )

    result = hil_plugin.dispatch(
        _request(
            "invoke",
            verb="program.flash",
            action_class="emit",
            params={"firmware": "should-never-open.hex"},
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "action-refused"


def test_debug_read_plugin_version_holds_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_CORE_LOCK_ROOT", str(tmp_path / "locks"))

    result = hil_plugin.dispatch(
        _request(
            "invoke",
            verb="debug.read",
            action_class="observe",
            params={"target": "plugin-version"},
        )
    )

    assert result["ok"] is True
    assert not list((tmp_path / "locks").glob("*.lock"))


def test_health_and_safe_state_are_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("HIL_CORE_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setattr(
        "mklink.discovery._probe_port", lambda port: port == TEST_PORT,
    )

    health = hil_plugin.dispatch(_request("health"))
    safe = hil_plugin.dispatch(_request("safe_state"))

    assert health["ok"] is True and health["data"]["verified"] is True
    assert safe["ok"] is True and safe["data"]["verified"] is True
    assert not list((tmp_path / "locks").glob("*.lock"))


@pytest.mark.parametrize(
    "params",
    [
        {"target": "unsupported"},
        {"target": "memory", "address": "0x20000000", "size": 0},
        {"target": "register", "name": ""},
        {"target": "variable", "name": ""},
    ],
)
def test_invalid_debug_read_is_rejected_before_hardware(params, monkeypatch):
    monkeypatch.setattr(
        "mklink.connect",
        lambda **_kwargs: pytest.fail("connect must not be called"),
    )

    result = hil_plugin.dispatch(
        _request(
            "invoke",
            verb="debug.read",
            action_class="observe",
            params=params,
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "invalid-device"


def test_debug_read_rejects_identity_mismatch_before_hardware(monkeypatch):
    request = _request(
        "invoke",
        verb="debug.read",
        action_class="observe",
        params={"target": "memory", "address": "0x20000000", "size": 4},
    )
    request["device"]["transport"]["serial"] = "OTHER"
    monkeypatch.setattr(
        "mklink.connect",
        lambda **_kwargs: pytest.fail("connect must not be called"),
    )

    result = hil_plugin.dispatch(request)

    assert result["ok"] is False
    assert result["error"]["type"] == "invalid-device"


class _FakeDevice:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_memory(self, address, size):
        assert address == 0x20000000
        assert size == 4
        return b"\x01\x02\x03\x04"

    def read_register(self, name):
        assert name == "SCB.CFSR"
        return 0x1234

    def read_variable(self, name):
        assert name == "counter"
        return 42


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (
            {"target": "memory", "address": "0x20000000", "size": 4},
            {"target": "memory", "value": "01020304"},
        ),
        (
            {"target": "register", "name": "SCB.CFSR"},
            {"target": "register", "value": 0x1234},
        ),
        (
            {"target": "variable", "name": "counter"},
            {"target": "variable", "value": 42},
        ),
    ],
)
def test_debug_read_targets_use_validated_device(params, expected, monkeypatch):
    monkeypatch.setattr("mklink.connect", lambda **_kwargs: _FakeDevice())

    result = hil_plugin.dispatch(
        _request(
            "invoke",
            verb="debug.read",
            action_class="observe",
            params=params,
        )
    )

    assert result["ok"] is True
    assert result["data"]["target"] == expected["target"]
    assert result["data"]["value"] == expected["value"]
