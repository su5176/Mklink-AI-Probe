import asyncio
from contextlib import contextmanager
import ctypes
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from mklink._types import DeviceState
from mklink.bridge import MKLinkSerialBridge
from mklink.device import Device, DeviceError
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


def test_mcp_ping_reports_rtt_write_utf8_limit(monkeypatch):
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "0.1.9")
    monkeypatch.setattr("mklink.toolchain.status", lambda: {})
    monkeypatch.setattr(
        "mklink.update_check.check_for_update",
        lambda: {"checked": False},
    )
    mcp = _Mcp()
    mcp_server._register_health_tools(mcp)

    limits = mcp.tools["ping"]()["limits"]
    assert limits["rtt_write_max_utf8_bytes"] == 256
    assert limits["rtt_pattern_max_utf8_bytes"] == 256


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


def test_device_batch_read_coalesces_only_touching_ranges():
    device = _connected_device()
    calls = []

    def read(address, size):
        calls.append((address, size))
        return bytes((address + offset) & 0xFF for offset in range(size))

    device.read_memory = read
    result = device.read_memory_regions([
        (0x20000004, 4),
        (0x20000000, 4),
        (0x20000100, 2),
        (0x20000002, 2),
    ])

    assert calls == [(0x20000000, 8), (0x20000100, 2)]
    assert result == [b"\x04\x05\x06\x07", b"\x00\x01\x02\x03", b"\x00\x01", b"\x02\x03"]


def test_mcp_memory_limits_and_batch_surface_are_enforced_before_io(monkeypatch):
    mcp = _Mcp()
    calls = []
    device = SimpleNamespace(
        read_memory=lambda address, size: calls.append(("read", address, size)) or b"\x01" * size,
        read_memory_regions=lambda pairs: calls.append(("batch", pairs)) or [b"\x02" * size for _, size in pairs],
        write_memory=lambda address, data: calls.append(("write", address, data)),
    )
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: device)
    mcp_server._register_memory_tools(mcp)

    with pytest.raises(ValueError, match="between 1 and 4096"):
        mcp.tools["read_memory"](0x20000000, 4097)
    with pytest.raises(ValueError, match="at most 16"):
        mcp.tools["read_memory_regions"]([
            {"address": 0x20000000 + index * 4, "size": 4}
            for index in range(17)
        ])
    with pytest.raises(ValueError, match="total requested bytes"):
        mcp.tools["read_memory_regions"]([
            {"address": 0x20000000, "size": 4096},
            {"address": 0x20001000, "size": 1},
        ])
    with pytest.raises(ValueError, match="between 1 and 4096"):
        mcp.tools["write_memory"](0x20000000, "AA" * 4097)
    assert calls == []

    output = mcp.tools["read_memory_regions"]([
        {"address": 0x20000000, "size": 4},
        {"address": 0x20000004, "size": 4},
    ])
    assert calls == [("batch", [(0x20000000, 4), (0x20000004, 4)])]
    assert output["region_count"] == 2
    assert output["total_bytes"] == 8

    calls.clear()
    device.read_memory = lambda address, size: calls.append(("verify", address, size)) or b"\x00" * size
    written = mcp.tools["write_memory"](0x20000100, "00000000")
    assert calls == [
        ("write", 0x20000100, b"\x00" * 4),
        ("verify", 0x20000100, 4),
    ]
    assert written["verified"] is True

    calls.clear()
    with pytest.raises(DeviceError, match="write verification failed"):
        mcp.tools["write_memory"](0x20000100, "AA")
    assert calls == [
        ("write", 0x20000100, b"\xAA"),
        ("verify", 0x20000100, 1),
    ]


def test_mcp_reuses_matching_connection_and_reports_limits(monkeypatch):
    mcp = _Mcp()
    device = SimpleNamespace(
        connected=True,
        port="COM8",
        idcode=0x6BA02477,
        mcu_name="STM32H743",
        _dwarf_info=object(),
        axf_status={"elf_backend": "builtin"},
    )
    kwargs = {
        "port": "COM8", "axf": "app.axf", "mcu": None,
        "project_root": ".", "elf_backend": None,
    }
    monkeypatch.setitem(mcp_server._holder, "device", device)
    monkeypatch.setitem(mcp_server._holder, "kwargs", kwargs)
    mcp_server._register_connection_tools(mcp)

    result = mcp.tools["connect"](port="COM8", axf="app.axf")

    assert result["reused"] is True
    assert result["limits"]["direct_read_max_bytes"] == 4096
    assert result["limits"]["batch_read_max_regions"] == 16
    assert result["limits"]["rtt_write_max_utf8_bytes"] == 256


def test_mcp_capture_duration_and_flush_limits(monkeypatch):
    rtt_mcp = _Mcp()
    systemview_mcp = _Mcp()
    flush_mcp = _Mcp()
    calls = []
    device = SimpleNamespace(
        rtt_read=lambda duration: calls.append(("rtt", duration)) or "",
        rtt_start=lambda *args, **kwargs: calls.append(("rtt-start", args, kwargs)) or {},
        systemview_read=lambda duration: calls.append(("systemview", duration)) or {},
        systemview_start=lambda *args, **kwargs: calls.append(("systemview-start", args, kwargs)) or {},
        _bridge=SimpleNamespace(send_command=lambda *args, **kwargs: calls.append(("flush", args))),
    )
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: device)
    mcp_server._register_rtt_tools(rtt_mcp)
    mcp_server._register_systemview_tools(systemview_mcp)
    mcp_server._register_flush_tools(flush_mcp)

    with pytest.raises(ValueError, match="at most 30 seconds"):
        rtt_mcp.tools["rtt_read"](30.1)
    with pytest.raises(ValueError, match="channel must be between 0 and 2"):
        rtt_mcp.tools["rtt_start"](channel=3)
    with pytest.raises(ValueError, match="search_size must be between"):
        rtt_mcp.tools["rtt_start"](search_size=65537)
    with pytest.raises(ValueError, match="channel must be between 0 and 2"):
        systemview_mcp.tools["systemview_start"](channel=-1)
    with pytest.raises(ValueError, match="at most 8 regions"):
        flush_mcp.tools["flush_memory"]([
            {"address": 0x20000000 + index, "data_hex": "00"}
            for index in range(9)
        ])
    with pytest.raises(ValueError, match="must not exceed 12288"):
        flush_mcp.tools["flush_memory"]([
            {"address": 0x20000000, "data_hex": "00" * 6144},
            {"address": 0x20002000, "data_hex": "01" * 6145},
        ])
    assert calls == []

    assert rtt_mcp.tools["rtt_read"](0.1) == {"output": ""}
    assert systemview_mcp.tools["systemview_read"](0.1) == {}
    assert calls == [("rtt", 0.1), ("systemview", 0.1)]


def test_mcp_rtt_write_enforces_utf8_byte_limit_before_device_lookup(monkeypatch):
    mcp = _Mcp()
    lookups = []
    monkeypatch.setattr(
        mcp_server,
        "_connected_device",
        lambda: lookups.append("device") or pytest.fail("unexpected device lookup"),
    )
    mcp_server._register_rtt_tools(mcp)

    for data in ("", "A" * 257, "测" * 86, "\ud800"):
        with pytest.raises(ValueError, match="UTF-8"):
            mcp.tools["rtt_write"](data)
    with pytest.raises(ValueError, match="probe-reserved RTTView.stop"):
        mcp.tools["rtt_write"]("RTTView.stop()")

    assert lookups == []


def test_mcp_rtt_write_accepts_exact_utf8_byte_boundary(monkeypatch):
    writes = []
    device = SimpleNamespace(
        rtt_write=lambda data: writes.append(data) or True,
    )
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: device)
    mcp = _Mcp()
    mcp_server._register_rtt_tools(mcp)
    data = "测" * 85 + "A"

    assert len(data.encode("utf-8")) == 256
    assert mcp.tools["rtt_write"](data) == {"sent": True}
    assert writes == [data]


def test_device_wait_for_rtt_matches_regex_metacharacters_literally():
    device = _connected_device()
    starts = []

    def start():
        starts.append("start")
        device._rtt_session = SimpleNamespace(_running=True)

    device.rtt_start = start
    device.rtt_read = lambda _duration: "prefix[suffix"

    assert device.wait_for_rtt("[", timeout=0.01) == "prefix[suffix"
    assert starts == ["start"]

    device.rtt_read = lambda _duration: "prefix(a+)+$suffix"
    assert device.wait_for_rtt(
        "(a+)+$", timeout=0.01, start_if_needed=False,
    ) == "prefix(a+)+$suffix"


def test_device_wait_for_rtt_rejects_non_text_before_start_or_connection():
    device = Device()
    starts = []
    device.rtt_start = lambda: starts.append("start")

    with pytest.raises(ValueError, match="pattern must be text or None"):
        device.wait_for_rtt(b"ready")

    assert starts == []


def test_mcp_capture_rtt_validates_literal_pattern_bytes_before_device_lookup(
    monkeypatch,
):
    mcp = _Mcp()
    lookups = []
    monkeypatch.setattr(
        mcp_server,
        "_connected_device",
        lambda: lookups.append("device") or pytest.fail("unexpected device lookup"),
    )
    monkeypatch.setitem(mcp_server._holder, "quarantine", None)
    mcp_server._register_rtt_tools(mcp)

    for pattern in ("", "A" * 257, "测" * 86, "\ud800", b"ready"):
        with pytest.raises(ValueError, match="pattern.*UTF-8"):
            mcp.tools["capture_rtt"](duration=0.01, pattern=pattern)

    assert lookups == []


def test_mcp_capture_rtt_accepts_exact_utf8_pattern_boundary(monkeypatch):
    captures = []
    pattern = "测" * 85 + "A"
    assert len(pattern.encode("utf-8")) == 256
    device = SimpleNamespace(
        wait_for_rtt=lambda value, **kwargs: captures.append(
            (value, kwargs),
        ) or f"prefix:{value}:suffix",
    )
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: device)
    mcp = _Mcp()
    mcp_server._register_rtt_tools(mcp)

    result = mcp.tools["capture_rtt"](duration=0.01, pattern=pattern)

    assert result["matched"] is True
    assert captures == [(
        pattern,
        {"timeout": 0.01, "start_if_needed": True},
    )]


def test_real_fastmcp_rejects_loose_rtt_integers_before_hardware_lock(
    monkeypatch,
):
    fastmcp = pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    hardware_entries = []
    device_lookups = []

    @contextmanager
    def observe_hardware(tool_name, *, recovery=False):
        hardware_entries.append((tool_name, recovery))
        yield

    monkeypatch.setattr(mcp_server, "_hardware_operation", observe_hardware)
    monkeypatch.setattr(
        mcp_server,
        "_connected_device",
        lambda: device_lookups.append("device") or pytest.fail(
            "unexpected device lookup",
        ),
    )
    server = mcp_server.build_server()

    async def exercise():
        async with fastmcp.Client(server) as client:
            invalid_calls = (
                ("rtt_start", {"channel": True}),
                ("rtt_start", {"channel": "1"}),
                ("rtt_start", {"search_size": True}),
                ("rtt_start", {"search_size": "1024"}),
                ("systemview_start", {"channel": False}),
                ("systemview_start", {"channel": "1"}),
                ("systemview_start", {"search_size": False}),
                ("systemview_start", {"search_size": "1024"}),
            )
            for tool_name, arguments in invalid_calls:
                with pytest.raises(ToolError, match="valid integer"):
                    await client.call_tool(tool_name, arguments)

    asyncio.run(exercise())

    assert hardware_entries == []
    assert device_lookups == []


def test_mcp_flush_limits_each_item_and_total_before_device_lookup(monkeypatch):
    mcp = _Mcp()
    lookups = []
    monkeypatch.setattr(
        mcp_server,
        "_connected_device",
        lambda: lookups.append("device") or pytest.fail("unexpected device lookup"),
    )
    monkeypatch.setitem(mcp_server._holder, "quarantine", None)
    mcp_server._register_flush_tools(mcp)

    with pytest.raises(ValueError, match="between 1 and 12288"):
        mcp.tools["flush_memory"]([{
            "address": 0x20000000,
            "data_hex": "AA" * 12289,
        }])
    with pytest.raises(ValueError, match="must not exceed 12288"):
        mcp.tools["flush_memory"]([
            {"address": 0x20000000, "data_hex": "AA" * 8192},
            {"address": 0x20002000, "data_hex": "55" * 4097},
        ])

    assert lookups == []
    limits = mcp_server._capabilities()
    assert limits["flush_memory_max_regions"] == 8
    assert limits["flush_memory_max_item_bytes"] == 12288
    assert limits["flush_memory_max_total_bytes"] == 12288


def test_mcp_flush_accepts_exact_12_kib_boundary_with_fake_bridge(monkeypatch):
    commands = []
    device = SimpleNamespace(
        _bridge=SimpleNamespace(
            send_command=lambda command, timeout: commands.append(
                (command, timeout)
            ) or ""
        )
    )
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: device)
    monkeypatch.setitem(mcp_server._holder, "quarantine", None)
    mcp = _Mcp()
    mcp_server._register_flush_tools(mcp)

    result = mcp.tools["flush_memory"]([{
        "address": 0x20000000,
        "data_hex": "A5" * 12288,
    }])

    assert result["total_bytes"] == 12288
    assert result["batches"] == 1
    assert len(commands) == 1
    assert "bytes([0xA5])*12288" in commands[0][0]
    assert commands[0][1] == 10.0


def test_mcp_rejects_parallel_probe_operations_without_queueing():
    entered = threading.Event()
    release = threading.Event()

    @mcp_server._exclusive_hardware_tool
    def operation(value):
        if value == "hold":
            entered.set()
            assert release.wait(2)
        return value

    worker = threading.Thread(target=operation, args=("hold",))
    worker.start()
    assert entered.wait(1)
    try:
        with pytest.raises(RuntimeError, match="Do not issue parallel calls"):
            operation("parallel")
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_mcp_timeout_quarantines_hardware_until_disconnect(monkeypatch):
    class FakeBridge:
        def __init__(self, *, timeout):
            self.timeout = timeout
            self.io_calls = []

        def read(self, address, size):
            self.io_calls.append((address, size))
            if self.timeout:
                raise TimeoutError("fake bridge command timed out")
            return b"\x5a" * size

    class FakeDevice:
        def __init__(self, bridge):
            self.bridge = bridge
            self.connected = True
            self.port = "FAKE_MCP"
            self.idcode = 0x2BA01477
            self.mcu_name = "FAKE_MCU"
            self.state = DeviceState.READY
            self._dwarf_info = None
            self.axf_status = {"elf_backend": "builtin"}
            self.closed = False

        def read_memory(self, address, size):
            return self.bridge.read(address, size)

        def close(self):
            self.closed = True
            self.connected = False

    first_bridge = FakeBridge(timeout=True)
    first_device = FakeDevice(first_bridge)
    monkeypatch.setitem(mcp_server._holder, "device", first_device)
    monkeypatch.setitem(mcp_server._holder, "kwargs", {})
    monkeypatch.setitem(mcp_server._holder, "quarantine", None)
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)
    mcp_server._register_connection_tools(mcp)

    with pytest.raises(TimeoutError, match="fake bridge"):
        mcp.tools["read_memory"](0x20000000, 4)
    assert first_bridge.io_calls == [(0x20000000, 4)]

    with pytest.raises(RuntimeError, match="quarantined"):
        mcp.tools["read_memory"](0x20000004, 4)
    assert first_bridge.io_calls == [(0x20000000, 4)]

    status = mcp.tools["device_status"]()
    assert status["quarantined"] is True
    assert status["quarantine"] == {
        "active": True,
        "tool": "read_memory",
        "reason": "fake bridge command timed out",
    }
    assert first_bridge.io_calls == [(0x20000000, 4)]

    assert mcp.tools["disconnect"]() == {
        "disconnected": True,
        "was_connected": True,
    }
    assert first_device.closed is True
    assert mcp.tools["device_status"]()["quarantined"] is False

    recovered_bridge = FakeBridge(timeout=False)
    recovered_device = FakeDevice(recovered_bridge)
    import mklink
    monkeypatch.setattr(mklink, "connect", lambda **_kwargs: recovered_device)

    assert mcp.tools["connect"](port="FAKE_MCP")["connected"] is True
    assert mcp.tools["read_memory"](0x20000008, 4)["hex"] == "5a5a5a5a"
    assert recovered_bridge.io_calls == [(0x20000008, 4)]
    mcp.tools["disconnect"]()


def test_mcp_detect_profile_guards_only_idcode_hardware_path(monkeypatch):
    calls = []

    def detect(**kwargs):
        calls.append(kwargs["read_idcode"])
        if kwargs["read_idcode"]:
            raise TimeoutError("fake IDCODE timeout")
        return {"status": "file-only"}

    monkeypatch.setattr("mklink.mcu_detect.detect_mcu_profile", detect)
    monkeypatch.setitem(mcp_server._holder, "device", None)
    monkeypatch.setitem(mcp_server._holder, "kwargs", {})
    monkeypatch.setitem(mcp_server._holder, "quarantine", None)
    mcp = _Mcp()
    mcp_server._register_project_tools(mcp)

    assert mcp.tools["detect_mcu_profile"](read_idcode=False) == {
        "status": "file-only"
    }
    with pytest.raises(TimeoutError, match="IDCODE"):
        mcp.tools["detect_mcu_profile"](read_idcode=True)
    with pytest.raises(RuntimeError, match="quarantined"):
        mcp.tools["detect_mcu_profile"](read_idcode=True)
    assert calls == [False, True]

    # File-only project inspection remains available because it performs no
    # probe I/O, even while the hardware session awaits explicit recovery.
    assert mcp.tools["detect_mcu_profile"](read_idcode=False) == {
        "status": "file-only"
    }
    assert calls == [False, True, False]
    mcp_server._reset_device()
