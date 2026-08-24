import ctypes
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mklink._types import DeviceState
from mklink.bridge import MKLinkSerialBridge
from mklink.device import Device
import mklink.local_resources as local_resources
import mklink.mcp_server as mcp_server


class _Callable:
    def __init__(self, callback):
        self._callback = callback

    def __call__(self, *args):
        return self._callback(*args)


class _Kernel32:
    def __init__(
        self,
        *,
        handle=123,
        exit_code=259,
        exit_query_ok=True,
    ):
        self.closed = []
        self.OpenProcess = _Callable(lambda *_args: handle)

        def get_exit_code(_handle, output):
            output._obj.value = exit_code
            return exit_query_ok

        self.GetExitCodeProcess = _Callable(get_exit_code)
        self.CloseHandle = _Callable(lambda value: self.closed.append(value) or True)


def _install_windows_process_api(monkeypatch, kernel32, *, last_error=0):
    monkeypatch.setattr(local_resources.os, "name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)


def test_windows_pid_exists_checks_exit_code_and_closes_handle(monkeypatch):
    live = _Kernel32(exit_code=259)
    _install_windows_process_api(monkeypatch, live)

    assert local_resources._pid_exists(99101) is True
    assert live.closed == [123]

    exited = _Kernel32(exit_code=0)
    _install_windows_process_api(monkeypatch, exited)

    assert local_resources._pid_exists(99102) is False
    assert exited.closed == [123]


def test_windows_pid_exists_only_treats_invalid_pid_open_failure_as_dead(monkeypatch):
    invalid_pid = _Kernel32(handle=0)
    _install_windows_process_api(monkeypatch, invalid_pid, last_error=87)
    assert local_resources._pid_exists(99103) is False

    access_denied = _Kernel32(handle=0)
    _install_windows_process_api(monkeypatch, access_denied, last_error=5)
    assert local_resources._pid_exists(99104) is True


def test_windows_pid_exists_keeps_lock_when_exit_query_fails(monkeypatch):
    kernel32 = _Kernel32(exit_query_ok=False)
    _install_windows_process_api(monkeypatch, kernel32)

    assert local_resources._pid_exists(99105) is True
    assert kernel32.closed == [123]


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle semantics")
def test_windows_pid_exists_reports_completed_real_process_as_dead():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)

    # Popen still owns a process handle here.  OpenProcess may therefore
    # succeed even though the process has exited, which is the customer case.
    assert local_resources._pid_exists(process.pid) is False


def test_release_serial_resources_removes_exited_auto_connect_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    kernel32 = _Kernel32(exit_code=1)
    _install_windows_process_api(monkeypatch, kernel32)
    path = local_resources.serial_lock_path("MKLINK_AUTO_CONNECT")
    lock_path = tmp_path / "mklink_serial_locks" / "serial_MKLINK_AUTO_CONNECT.lock"
    assert path == str(lock_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("99106", encoding="utf-8")

    result = local_resources.release_serial_resources(
        port="MKLINK_AUTO_CONNECT",
        include_mklink_bridge=False,
    )

    assert result["serial_locks"] == [{
        "resource": "serial_port",
        "path": str(lock_path),
        "exists": True,
        "owner_pid": 99106,
        "owner_alive": False,
        "action": "removed_stale_lock",
    }]
    assert not lock_path.exists()


class _Serial:
    is_open = True

    def __init__(self):
        self.writes = []
        self.flushes = 0

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        self.flushes += 1


def test_bridge_nowait_command_flushes_without_waiting_for_prompt():
    bridge = MKLinkSerialBridge("TEST")
    serial = _Serial()
    bridge._serial = serial
    bridge._ctx.state = DeviceState.READY

    bridge.send_command_nowait("reboot()")

    assert serial.writes == [b"reboot()\n"]
    assert serial.flushes == 1


class _Bridge:
    def __init__(self):
        self.commands = []
        self.nowait_commands = []
        self.closed = False

    def send_command(self, command, timeout):
        self.commands.append((command, timeout))
        return ""

    def send_command_nowait(self, command):
        self.nowait_commands.append(command)

    def close(self):
        self.closed = True


class _HilLock:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


def _connected_device():
    device = Device()
    device._connected = True
    device._bridge = _Bridge()
    return device


def test_device_set_power_on_allows_only_supported_safe_requests():
    device = _connected_device()

    device.set_power_on(1800)
    device.set_power_on(3300)

    with pytest.raises(ValueError, match="1800, 3300, or 5000"):
        device.set_power_on(3301)
    with pytest.raises(ValueError, match="5 V may damage"):
        device.set_power_on(5000)
    assert device._bridge.commands == [
        ("cmd.set_power_on(1800)", 10.0),
        ("cmd.set_power_on(3300)", 10.0),
    ]

    device.set_power_on(5000, confirm_5v=True)
    assert device._bridge.commands[-1] == ("cmd.set_power_on(5000)", 10.0)


def test_device_set_power_on_stops_active_streams_only_after_validation():
    device = _connected_device()
    events = []
    device._rtt_session = SimpleNamespace(_running=True)
    device._systemview_session = SimpleNamespace(_running=True)
    device.rtt_stop = lambda: events.append("rtt-stop")
    device.systemview_stop = lambda: events.append("systemview-stop")

    with pytest.raises(ValueError, match="5 V may damage"):
        device.set_power_on(5000)
    assert events == []

    device.set_power_on(3300)
    assert events == ["rtt-stop", "systemview-stop"]
    assert device._bridge.commands == [("cmd.set_power_on(3300)", 10.0)]


def test_device_reboot_sends_probe_command_then_disconnects_and_releases_hil_lock():
    device = _connected_device()
    bridge = device._bridge
    hil_lock = _HilLock()
    device._hil_lock = hil_lock

    device.reboot()

    assert bridge.nowait_commands == ["reboot()"]
    assert bridge.closed is True
    assert hil_lock.released is True
    assert device.connected is False


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function
        return register


def test_mcp_exposes_guarded_power_and_probe_reboot(monkeypatch):
    mcp = _Mcp()
    device = SimpleNamespace(
        set_power_on=lambda voltage_mv, *, confirm_5v=False: calls.append(
            ("power", voltage_mv, confirm_5v)
        ),
        reboot=lambda: calls.append(("reboot",)),
    )
    calls = []
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: device)
    monkeypatch.setattr(mcp_server, "_reset_device", lambda: calls.append(("reset-holder",)))

    mcp_server._register_flash_tools(mcp)

    with pytest.raises(ValueError, match="explicit user confirmation"):
        mcp.tools["set_power_on"](3300)
    assert calls == []

    assert mcp.tools["set_power_on"](3300, confirm_user=True) == {
        "power_on": True,
        "voltage_mv": 3300,
    }
    assert mcp.tools["set_power_on"](
        5000,
        confirm_user=True,
        confirm_5v=True,
    ) == {
        "power_on": True,
        "voltage_mv": 5000,
    }
    assert mcp.tools["reboot_probe"]() == {"rebooted": True, "connected": False}
    assert calls == [
        ("power", 3300, False),
        ("power", 5000, True),
        ("reboot",),
        ("reset-holder",),
    ]
