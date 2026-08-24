import pytest

from mklink._types import DeviceState
from mklink.systemview import SystemViewSession


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

    assert bridge.raw_writes[-1] == b"SystemView.stop()\n"
    assert bridge.exited == 1
    assert bridge.state is DeviceState.READY


def test_systemview_session_rejects_a_missing_control_block():
    bridge = RecorderBridge()
    bridge.send_command = lambda command, timeout=5.0: (
        "0008E488 " + "00 " * 16 + "\n>>>"
        if command.startswith("cmd.read_ram") else ">>>"
    )
    session = SystemViewSession(bridge, channel=1)

    with pytest.raises(RuntimeError, match="RTT control block|RTT 控制块"):
        session.start("0x0008e488", search_size=1024)

    assert bridge.exited == 0
