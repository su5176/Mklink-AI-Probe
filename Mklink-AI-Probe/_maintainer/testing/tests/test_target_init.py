"""Tests for SWD DP / IDCODE initialization (``mklink.device.initialize_target``).

Regression coverage for the MCP ``connect`` idcode=0 bug: every target debug
session — ``Device._connect``, ``memory_access.read_memory`` fresh-bridge path,
and the CLI one-shot target ops — must call ``get_idcode()``, write
``_ctx.idcode``, and match the MCU, instead of leaving idcode at 0 (which left
the DAP uninitialized in long-lived sessions and made halt/read return 0).
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from mklink.device import Device, DeviceNotConnectedError, initialize_target


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _bridge():
    """A mock bridge with a writable _ctx (idcode/current_mcu)."""
    bridge = MagicMock()
    ctx = MagicMock()
    ctx.idcode = 0
    ctx.current_mcu = ""
    bridge._ctx = ctx
    return bridge


def _flash(*, idcode=None, raises=None):
    flash = MagicMock()
    if raises is not None:
        flash.get_idcode = MagicMock(side_effect=raises)
    else:
        flash.get_idcode = MagicMock(return_value=idcode)
    return flash


def _api_client_and_state():
    """Build a FastAPI TestClient + its shared _state (mirrors the api tests)."""
    from mklink.remote.api import create_app
    app = create_app(auth_token=None, project_root=".")
    return TestClient(app), app.state.mklink_state


# ----------------------------------------------------------------------
# initialize_target logic
# ----------------------------------------------------------------------

class TestInitializeTarget:
    def test_writes_idcode_and_matches_by_idcode(self):
        bridge = _bridge()
        flash = _flash(idcode=0x2BA01477)
        profiles = {"stm32f4": {"name": "STM32F4", "idcode_pattern": "0x2BA01477"}}
        with patch("mklink.profiles.load_mcu_profiles", return_value=profiles), \
             patch("mklink.project_config.load_config", return_value={}):
            rc = initialize_target(bridge, flash, project_root=".")
        assert rc == 0x2BA01477
        assert bridge._ctx.idcode == 0x2BA01477
        assert bridge._ctx.current_mcu == "STM32F4"
        flash.get_idcode.assert_called_once()

    def test_mcu_hint_wins_over_idcode_match(self):
        """Explicit hint beats idcode match (compatible chips share IDCODE)."""
        bridge = _bridge()
        flash = _flash(idcode=0x2BA01477)
        profiles = {
            "stm32f4": {"name": "STM32F4", "idcode_pattern": "0x2BA01477"},
            "hc32f4a0": {"name": "HC32F4A0", "idcode_pattern": "0x2BA01477"},
        }
        with patch("mklink.profiles.load_mcu_profiles", return_value=profiles), \
             patch("mklink.project_config.load_config", return_value={}):
            initialize_target(bridge, flash, mcu_hint="hc32f4a0", project_root=".")
        assert bridge._ctx.current_mcu == "HC32F4A0"

    def test_config_mcu_key_beats_idcode_match(self):
        bridge = _bridge()
        flash = _flash(idcode=0x2BA01477)
        profiles = {"stm32f4": {"name": "STM32F4", "idcode_pattern": "0x2BA01477"}}
        with patch("mklink.profiles.load_mcu_profiles", return_value=profiles), \
             patch("mklink.project_config.load_config", return_value={"mcu_key": "stm32f4"}):
            initialize_target(bridge, flash, project_root=".")
        assert bridge._ctx.current_mcu == "STM32F4"

    def test_no_profile_match_still_sets_idcode(self):
        bridge = _bridge()
        flash = _flash(idcode=0xDEADBEEF)
        with patch("mklink.profiles.load_mcu_profiles", return_value={}), \
             patch("mklink.project_config.load_config", return_value={}):
            rc = initialize_target(bridge, flash, project_root=".")
        assert rc == 0xDEADBEEF
        assert bridge._ctx.idcode == 0xDEADBEEF

    def test_tolerant_when_get_idcode_fails(self):
        """No target / broken SWD / timeout → return 0, do not raise, leave ctx at 0."""
        bridge = _bridge()
        flash = _flash(raises=RuntimeError("IDCODEError: timeout"))
        rc = initialize_target(bridge, flash, project_root=".")
        assert rc == 0
        assert bridge._ctx.idcode == 0  # unchanged


# ----------------------------------------------------------------------
# Device._connect wires it in
# ----------------------------------------------------------------------

class TestDeviceConnectInitializesTarget:
    def test_connect_calls_initialize_target(self):
        dev = Device()
        new_bridge = MagicMock()
        new_bridge.connect.return_value = True
        with patch("mklink.bridge.MKLinkSerialBridge", return_value=new_bridge), \
             patch("mklink.project_config.load_config", return_value={"com_port": "COM6"}), \
             patch("mklink.device.initialize_target") as mock_init:
            dev._port = None
            dev._connect()
        mock_init.assert_called_once()
        args, kwargs = mock_init.call_args
        assert args[0] is new_bridge            # bridge
        assert kwargs.get("project_root") == "."
        assert dev.connected is True

    def test_automatic_connect_falls_back_when_preferred_port_is_claimed(self, tmp_path):
        created = []
        events = []

        class DiscoveryLock:
            def __init__(self, name):
                assert name == "mklink_auto_connect"

            def acquire(self):
                events.append("discovery-lock-acquired")
                return True

            def release(self):
                events.append("discovery-lock-released")

        class Bridge:
            def __init__(self, port):
                self.port = port
                self._ctx = MagicMock()
                created.append(port)

            def connect(self):
                return self.port == "COM228"

            def _verify_identity(self):
                return True

            def close(self):
                pass

        dev = Device(preferred_port="COM46", project_root=str(tmp_path))
        with patch("mklink.bridge.MKLinkSerialBridge", Bridge), \
             patch("mklink.discovery.find_mklink_cdc_port", side_effect=lambda **_kwargs: (
                 events.append("discovered") or "COM228"
             )) as discover, \
             patch("mklink.discovery.list_available_ports", return_value=[
                 {"device": "COM46"}, {"device": "COM228"}
             ]), \
             patch("mklink.project_config.load_config", return_value={"com_port": "COM46"}), \
             patch("mklink.project_config.save_config") as save_config, \
             patch("mklink.serial._port._PortLock", DiscoveryLock), \
             patch("mklink.device.initialize_target"):
            dev._connect()

        assert created == ["COM46", "COM46", "COM228"]
        discover.assert_called_once_with(exclude_ports={"com46"})
        assert events == [
            "discovery-lock-acquired",
            "discovered",
            "discovery-lock-released",
        ]
        save_config.assert_not_called()
        assert dev.port == "COM228"

    def test_explicit_port_does_not_fall_back(self, tmp_path):
        bridge = MagicMock()
        bridge.connect.return_value = False
        dev = Device(port="COM46", project_root=str(tmp_path))

        with patch("mklink.bridge.MKLinkSerialBridge", return_value=bridge), \
             patch("mklink.discovery.find_mklink_cdc_port") as discover, \
             patch("mklink.project_config.load_config", return_value={}):
            with pytest.raises(DeviceNotConnectedError):
                dev._connect()

        discover.assert_not_called()
        assert bridge.connect.call_count == 2


# ----------------------------------------------------------------------
# memory_access fresh-bridge path wires it in
# ----------------------------------------------------------------------

class TestMemoryAccessFreshBridgeInit:
    def test_read_memory_fresh_bridge_initializes_target(self):
        from mklink import memory_access
        fake_bridge = MagicMock()
        fake_bridge.connect.return_value = True
        fake_bridge.send_command.return_value = "20000000  AA BB CC DD"
        with patch("mklink.bridge.MKLinkSerialBridge", return_value=fake_bridge), \
             patch("mklink.cli._resolve_port", return_value="COM7"), \
             patch("mklink.device.initialize_target") as mock_init:
            data, raw = memory_access.read_memory(None, 0x20000000, 4)
        mock_init.assert_called_once()
        assert isinstance(data, bytes)


# ----------------------------------------------------------------------
# API /api/device/connect response shape (now includes idcode/port/axf_loaded)
# ----------------------------------------------------------------------

class TestApiConnectShape:
    def test_connect_response_includes_idcode_port_axf(self):
        client, _ = _api_client_and_state()
        dev = MagicMock()
        dev.connected = True
        dev.mcu_name = "STM32F40x"
        dev.idcode = 0x2BA01477
        dev.port = "COM5"
        dev._dwarf_info = object()  # truthy → axf_loaded True
        with patch("mklink.connect", return_value=dev):
            resp = client.post("/api/device/connect", json={"port": "COM5"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "connected"
        assert data["idcode"] == hex(0x2BA01477)
        assert data["port"] == "COM5"
        assert data["axf_loaded"] is True

    def test_already_connected_response_includes_idcode_port_axf(self):
        client, state = _api_client_and_state()
        dev = MagicMock()
        dev.connected = True
        dev.mcu_name = "STM32F40x"
        dev.idcode = 0x2BA01477
        dev.port = "COM5"
        dev._dwarf_info = None
        state["device"] = dev
        resp = client.post("/api/device/connect", json={"port": "COM5"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "already_connected"
        assert data["idcode"] == hex(0x2BA01477)
        assert data["port"] == "COM5"
        assert data["axf_loaded"] is False
