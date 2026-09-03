import queue
import threading

import pytest
import serial

from mklink._types import (
    MKLINK_IDENTITY_COMMAND,
    MKLINK_IDENTITY_TOKEN,
    DeviceState,
)
from mklink.bridge import MKLinkSerialBridge
from mklink.device import Device
from mklink.systemview import SystemViewSession


@pytest.fixture(autouse=True)
def _default_trusted_systemview_test_ram(monkeypatch):
    monkeypatch.setattr(
        Device,
        "_get_mcu_profile",
        lambda _self: {
            "ram_base": "0x20000000",
            "regions": [{
                "name": "ram",
                "start": "0x20000000",
                "size": "0x10000",
            }],
        },
    )


def _magic_dump() -> str:
    data = b"SEGGER RTT\x00" + b"\x00" * 5
    return "0008E488 " + " ".join(f"{value:02X}" for value in data) + "\n>>>"


class RecorderBridge:
    def __init__(self, hello=b"SV\x06\x07\xAA\xBB"):
        self.state = DeviceState.READY
        self.hello = hello
        self.stream = bytearray()
        self.commands = []
        self.raw_writes = []
        self.exited = 0

    def send_command(self, command, timeout=5.0):
        self.commands.append(command)
        if command.startswith("cmd.read_ram"):
            return _magic_dump()
        if command.startswith("SystemView.start"):
            return "Find SEGGER RTT addr 0x8e488\n>>>"
        if command == "SystemView.stop()":
            return "stopped\n>>>"
        if command == "probe.ping()":
            return "pong\n>>>"
        raise AssertionError(command)

    def _enter_stream(self, state):
        self.state = state

    def _write_raw(self, data):
        self.raw_writes.append(data)
        if data == SystemViewSession._HOST_HELLO:
            self.stream.extend(self.hello)

    def drain_stream_bytes(self, max_bytes=None):
        take = len(self.stream) if max_bytes is None else min(len(self.stream), max_bytes)
        data = bytes(self.stream[:take])
        del self.stream[:take]
        return data

    def _exit_stream(self):
        self.exited += 1
        self.state = DeviceState.READY
        return "tail"


def test_systemview_session_uses_recorder_handshake_and_control_commands():
    bridge = RecorderBridge()
    session = SystemViewSession(bridge, channel=1)

    result = session.start("0x0008e488", search_size=1024)

    assert result["control_block_addr"] == "0x0008E488"
    assert "SystemView.start(0x0008e488,1024,1)" in bridge.commands
    assert bridge.raw_writes[:2] == [b"SV\x01\x00", b"\x01"]
    assert session.read_bytes(duration=0) == b"\xAA\xBB"
    assert session.stop() == "tail"
    assert bridge.raw_writes[-2:] == [b"\x02", b"SystemView.stop()\n"]
    assert bridge.state is DeviceState.READY


def test_systemview_stop_does_not_write_after_transport_error():
    bridge = MKLinkSerialBridge("TEST_SYSTEMVIEW_STOP_TRANSPORT_ERROR")
    writes = []

    class SerialPort:
        def write(self, data):
            writes.append(data)

    transport_error = serial.SerialException("probe disconnected")
    bridge._serial = SerialPort()
    bridge._ctx.state = DeviceState.SYSTEMVIEW_STREAM
    bridge._transport_error = transport_error
    session = SystemViewSession(bridge)
    session._running = True
    session._prefetched.extend(b"prefetched")
    session._tasklist_requested = True

    with pytest.raises(ConnectionError) as caught:
        session.stop()

    assert caught.value.__cause__ is transport_error
    assert bridge.state is DeviceState.ERROR
    assert writes == []
    assert session._running is False
    assert session._prefetched == b""
    assert session._tasklist_requested is False


def test_systemview_session_retries_only_tasklist_after_startup_burst():
    bridge = RecorderBridge()
    session = SystemViewSession(bridge, channel=1)
    session.start("0x0008e488", search_size=1024)
    session._started_at -= 1.0

    session.read_bytes(duration=0)
    session.read_bytes(duration=0)

    assert bridge.raw_writes.count(SystemViewSession._COMMAND_GET_TASKLIST) == 1
    assert b"\x03" not in bridge.raw_writes
    assert b"\x05" not in bridge.raw_writes


def test_systemview_short_reads_do_not_wait_in_fixed_50ms_steps(monkeypatch):
    bridge = RecorderBridge(hello=b"")
    session = SystemViewSession(bridge, channel=1)
    session._running = True
    session._started_at = 0.0
    session._tasklist_requested = True
    now = 0.0
    sleeps = []

    def monotonic():
        return now

    def sleep(duration):
        nonlocal now
        sleeps.append(duration)
        now += duration

    monkeypatch.setattr("mklink.systemview.time.monotonic", monotonic)
    monkeypatch.setattr("mklink.systemview.time.sleep", sleep)

    assert session.read_bytes(duration=0.033) == b""
    assert sum(sleeps) == pytest.approx(0.033)
    assert max(sleeps) <= session._STREAM_DRAIN_INTERVAL_S


def test_systemview_session_releases_stream_when_recorder_handshake_fails():
    bridge = RecorderBridge(hello=b"NOPE")
    session = SystemViewSession(bridge, channel=1)

    with pytest.raises(RuntimeError, match="SV"):
        session.start("0x0008e488", search_size=1024)

    assert bridge.commands[-1] == "SystemView.stop()"
    assert bridge.exited == 1
    assert bridge.state is DeviceState.READY


def test_systemview_session_rejects_a_missing_control_block_and_recovers(monkeypatch):
    bridge = RecorderBridge()

    def send_command(command, timeout=5.0):
        bridge.commands.append(command)
        if command.startswith("cmd.read_ram"):
            return "0008E488 " + "00 " * 16 + "\n>>>"
        if command.startswith("SystemView.start"):
            return ">>>"
        if command == "SystemView.stop()":
            return "stopped\n>>>"
        if command == "probe.ping()":
            return "pong\n>>>"
        raise AssertionError(command)

    bridge.send_command = send_command
    monkeypatch.setattr("mklink.systemview.time.sleep", lambda _duration: None)
    session = SystemViewSession(bridge, channel=1)

    with pytest.raises(RuntimeError, match="RTT control block|RTT 控制块"):
        session.start("0x0008e488", search_size=1024)

    assert bridge.commands[-1] == "SystemView.stop()"
    assert bridge.exited == 1
    assert bridge.state is DeviceState.READY
    assert bridge.send_command("probe.ping()") == "pong\n>>>"


def test_systemview_session_recovers_when_start_reply_parsing_fails(monkeypatch):
    bridge = RecorderBridge()
    session = SystemViewSession(bridge, channel=1)
    parse_error = ValueError("malformed startup reply")

    def fail_parse(_response):
        raise parse_error

    monkeypatch.setattr(
        "mklink.systemview.RTTSession._parse_rtt_startup", fail_parse,
    )

    with pytest.raises(ValueError) as caught:
        session.start("0x0008e488", search_size=1024)

    assert caught.value is parse_error
    assert bridge.commands[-1] == "SystemView.stop()"
    assert bridge.state is DeviceState.READY
    assert bridge.send_command("probe.ping()") == "pong\n>>>"


def test_systemview_failed_start_uses_raw_stop_when_command_stop_times_out():
    start_error = TimeoutError("start reply timed out")

    class RawFallbackBridge(RecorderBridge):
        def send_command(self, command, timeout=5.0):
            self.commands.append(command)
            if command.startswith("cmd.read_ram"):
                return _magic_dump()
            if command.startswith("SystemView.start"):
                self.state = DeviceState.ERROR
                raise start_error
            if command == "SystemView.stop()":
                self.state = DeviceState.ERROR
                raise TimeoutError("stop command timed out")
            raise AssertionError(command)

    bridge = RawFallbackBridge()
    session = SystemViewSession(bridge, channel=1)

    with pytest.raises(TimeoutError) as caught:
        session.start("0x0008e488", search_size=1024)

    assert caught.value is start_error
    assert bridge.commands[-1] == "SystemView.stop()"
    assert bridge.raw_writes[-1] == b"SystemView.stop()\n"
    assert bridge.exited == 2
    assert bridge.state is DeviceState.READY
    assert session._running is False


def test_systemview_failed_start_preserves_transport_error_without_raw_stop():
    transport_error = ConnectionError("serial transport disconnected")

    class DisconnectedBridge(RecorderBridge):
        def send_command(self, command, timeout=5.0):
            self.commands.append(command)
            if command.startswith("cmd.read_ram"):
                return _magic_dump()
            if command.startswith("SystemView.start"):
                self.state = DeviceState.ERROR
                raise transport_error
            raise AssertionError(command)

    bridge = DisconnectedBridge()
    session = SystemViewSession(bridge, channel=1)

    with pytest.raises(ConnectionError) as caught:
        session.start("0x0008e488", search_size=1024)

    assert caught.value is transport_error
    assert bridge.state is DeviceState.ERROR
    assert bridge.exited == 0
    assert bridge.raw_writes == []
    assert bridge.commands[-1].startswith("SystemView.start")


def test_systemview_failed_start_verifies_identity_after_late_start_prompt(
    monkeypatch,
):
    monkeypatch.setattr("mklink.bridge._FAILED_STREAM_STOP_TIMEOUT", 0.1)
    monkeypatch.setattr("mklink.bridge._FAILED_STREAM_IDENTITY_TIMEOUT", 0.2)
    bridge = MKLinkSerialBridge("TEST_SYSTEMVIEW_FAILED_START_RECOVERY")
    incoming: queue.Queue[bytes] = queue.Queue()
    writes = []
    timers = []
    identity_command = (MKLINK_IDENTITY_COMMAND + "\n").encode("utf-8")

    class SerialPort:
        @property
        def in_waiting(self):
            return 0

        def write(self, data):
            writes.append(data)
            if data == b"SystemView.stop()\n":
                # A late prompt from the failed start arrives before the real
                # stop response.  A normal send_command(stop) would accept it.
                for delay, response in (
                    (0.01, b"LATE_START\n>>>"),
                    (0.04, b"STOPPED\n>>>"),
                ):
                    timer = threading.Timer(
                        delay, incoming.put, args=(response,),
                    )
                    timers.append(timer)
                    timer.start()
            elif data == identity_command:
                timer = threading.Timer(
                    0.06,
                    incoming.put,
                    args=((MKLINK_IDENTITY_TOKEN + "\n>>>").encode("utf-8"),),
                )
                timers.append(timer)
                timer.start()
            elif data == b"next()\n":
                timer = threading.Timer(
                    0.08, incoming.put, args=(b"NEXT_OK>>>",),
                )
                timers.append(timer)
                timer.start()

        def read(self, _size):
            try:
                return incoming.get(timeout=0.01)
            except queue.Empty:
                return b""

    bridge._serial = SerialPort()
    bridge._ctx.state = DeviceState.READY
    bridge._running = True
    reader = threading.Thread(target=bridge._reader_loop)
    reader.start()
    session = SystemViewSession(bridge)
    session._needs_failed_start_reset = True
    try:
        assert session.reset_failed_start() == ""
        assert bridge.state is DeviceState.READY
        assert bridge.send_command("next()", timeout=0.2) == "NEXT_OK"
    finally:
        bridge._running = False
        for timer in timers:
            timer.join(timeout=0.2)
        reader.join(timeout=1.0)

    assert not reader.is_alive()
    assert writes[:3] == [
        b"SystemView.stop()\n",
        identity_command,
        b"next()\n",
    ]


class _DeviceSystemViewSession:
    calls = []
    _running = False

    def __init__(self, _bridge, channel=1):
        self.channel = channel

    def start(self, addr, search_size=1024, project_root=".", *, mode=0):
        self.calls.append({
            "addr": addr,
            "search_size": search_size,
            "project_root": project_root,
            "mode": mode,
            "channel": self.channel,
        })
        self._running = True
        return {"control_block_addr": addr}

    def stop(self):
        self._running = False


def _systemview_control_block_memory(max_up=2):
    header = (
        b"SEGGER RTT" + b"\x00" * 6
        + max_up.to_bytes(4, "little")
        + (0).to_bytes(4, "little")
    )

    def descriptor(address):
        return (
            (0).to_bytes(4, "little")
            + address.to_bytes(4, "little")
            + (256).to_bytes(4, "little")
            + (0).to_bytes(4, "little") * 3
        )

    return header, b"".join(
        descriptor(0x20001000 + channel * 0x100)
        for channel in range(max_up)
    )


def _systemview_device(monkeypatch, read_memory):
    class Bridge:
        state = DeviceState.READY

    _DeviceSystemViewSession.calls = []
    monkeypatch.setattr(
        "mklink.systemview.SystemViewSession", _DeviceSystemViewSession,
    )
    device = Device(project_root=".")
    device._bridge = Bridge()
    device._connected = True
    device.read_memory = read_memory
    device._read_cpu_clock_hint = lambda: (0, "")
    device._systemview_defaults = lambda: {
        "ram_base": 0x20000000,
        "id_shift": 2,
        "cpu_freq": 0,
        "cpu_freq_source": "",
    }
    return device


def test_device_systemview_stop_clears_session_when_stop_raises():
    stop_error = ConnectionError("SystemView transport failed")

    class Session:
        _running = True

        def stop(self):
            raise stop_error

    class Bridge:
        state = DeviceState.READY
        _transport_error = None

    device = Device()
    device._connected = True
    device._bridge = Bridge()
    device._systemview_session = Session()
    device._systemview_parser = object()

    with pytest.raises(ConnectionError) as caught:
        device.systemview_stop()

    assert caught.value is stop_error
    assert device._systemview_session is None
    assert device._systemview_parser is None


def test_device_systemview_start_clears_old_session_before_validation(
    monkeypatch,
):
    stopped = []
    validation_error = RuntimeError("validation failed")

    class OldSession:
        _running = True

        def stop(self):
            self._running = False
            stopped.append(True)

    device = _systemview_device(
        monkeypatch,
        lambda _address, _size: pytest.fail("unexpected target read"),
    )
    old_session = OldSession()
    device._systemview_session = old_session
    device._systemview_parser = object()

    def fail_validation(*_args, **_kwargs):
        assert device._systemview_session is None
        assert device._systemview_parser is None
        raise validation_error

    device.validate_rtt_stream_request = fail_validation

    with pytest.raises(RuntimeError) as caught:
        device.systemview_start("0x20000000", mode=1, search_size=0)

    assert caught.value is validation_error
    assert stopped == [True]
    assert device._systemview_session is None
    assert device._systemview_parser is None


def test_device_systemview_start_clears_old_session_when_stop_raises(
    monkeypatch,
):
    stop_error = ConnectionError("old SystemView stop failed")

    class OldSession:
        _running = True

        def stop(self):
            raise stop_error

    device = _systemview_device(
        monkeypatch,
        lambda _address, _size: pytest.fail("unexpected target read"),
    )
    device._systemview_session = OldSession()
    device._systemview_parser = object()

    with pytest.raises(ConnectionError) as caught:
        device.systemview_start("0x20000000", mode=1, search_size=0)

    assert caught.value is stop_error
    assert device._systemview_session is None
    assert device._systemview_parser is None


@pytest.mark.parametrize(
    ("offset", "requested_search_size", "effective_search_size"),
    ((0, 0, 1024), (0x10, 64, 64)),
)
def test_device_dynamic_systemview_primes_host_scan_and_uses_found_address(
    monkeypatch,
    offset,
    requested_search_size,
    effective_search_size,
):
    requested_addr = 0x20000000
    actual_addr = requested_addr + offset
    scan_size = effective_search_size + len(b"SEGGER RTT") - 1
    scan = bytearray(scan_size)
    scan[offset:offset + len(b"SEGGER RTT")] = b"SEGGER RTT"
    header, descriptors = _systemview_control_block_memory()
    reads = []

    def read_memory(address, size):
        reads.append((address, size))
        if (address, size) == (requested_addr, scan_size):
            return bytes(scan)
        if (address, size) == (actual_addr, 24):
            return header
        if (address, size) == (actual_addr + 24, len(descriptors)):
            return descriptors
        raise AssertionError((address, size))

    device = _systemview_device(monkeypatch, read_memory)

    result = device.systemview_start(
        f"0x{requested_addr:08X}",
        mode=0,
        search_size=requested_search_size,
    )

    assert reads == [
        (requested_addr, scan_size),
        (actual_addr, 24),
        (actual_addr + 24, len(descriptors)),
    ]
    assert _DeviceSystemViewSession.calls == [{
        "addr": f"0x{actual_addr:08X}",
        "search_size": 4,
        "project_root": ".",
        "mode": 0,
        "channel": 1,
    }]
    assert result["control_block_addr"] == f"0x{actual_addr:08X}"


def test_device_static_systemview_validates_exact_control_block_without_scan(
    monkeypatch,
):
    requested_addr = "0x20000040"
    header, descriptors = _systemview_control_block_memory()
    reads = []

    def read_memory(address, size):
        reads.append((address, size))
        return header if size == 24 else descriptors

    device = _systemview_device(monkeypatch, read_memory)

    result = device.systemview_start(
        requested_addr, mode=1, search_size=0,
    )

    assert _DeviceSystemViewSession.calls[0]["addr"] == requested_addr
    assert _DeviceSystemViewSession.calls[0]["search_size"] == 4
    assert _DeviceSystemViewSession.calls[0]["mode"] == 1
    assert result["control_block_addr"] == requested_addr
    assert reads == [
        (int(requested_addr, 0), 24),
        (int(requested_addr, 0) + 24, len(descriptors)),
    ]


def test_device_dynamic_systemview_rejects_when_host_scan_fails(
    monkeypatch,
):
    requested_addr = "0x20000080"

    def failed_read(_address, _size):
        raise TimeoutError("bounded host scan unavailable")

    device = _systemview_device(monkeypatch, failed_read)

    with pytest.raises(TimeoutError, match="bounded host scan unavailable"):
        device.systemview_start(
            requested_addr, mode=0, search_size=64,
        )

    assert _DeviceSystemViewSession.calls == []


def test_device_systemview_start_clears_failed_session_and_preserves_error(
    monkeypatch,
):
    start_error = RuntimeError("original SystemView start failure")
    instances = []

    class FailingSession:
        _running = False

        def __init__(self, _bridge, channel=1):
            self.channel = channel
            self.reset_calls = 0
            instances.append(self)

        def start(self, *_args, **_kwargs):
            raise start_error

        def reset_failed_start(self):
            self.reset_calls += 1
            raise OSError("cleanup failure")

    monkeypatch.setattr("mklink.systemview.SystemViewSession", FailingSession)
    device = Device(project_root=".")
    device._bridge = type("Bridge", (), {"state": DeviceState.READY})()
    device._connected = True
    device._read_cpu_clock_hint = lambda: (0, "")
    device._systemview_defaults = lambda: {
        "ram_base": 0x20000000,
        "id_shift": 2,
        "cpu_freq": 0,
        "cpu_freq_source": "",
    }
    header, descriptors = _systemview_control_block_memory()
    device.read_memory = (
        lambda _address, size: header if size == 24 else descriptors
    )

    with pytest.raises(RuntimeError) as caught:
        device.systemview_start("0x20000000", mode=1, search_size=0)

    assert caught.value is start_error
    assert instances[0].reset_calls == 1
    assert device._systemview_session is None
    assert device._systemview_parser is None
