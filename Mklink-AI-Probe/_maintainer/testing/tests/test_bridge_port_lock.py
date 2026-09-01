"""Cross-process MKLink CMD port lock regression tests."""

from __future__ import annotations

from mklink import bridge as bridge_module
from mklink.bridge import MKLinkSerialBridge
from mklink._types import DeviceState


def test_mklink_bridges_lock_each_cmd_port_independently(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    first = MKLinkSerialBridge("COM201")
    second = MKLinkSerialBridge("COM202")
    duplicate = MKLinkSerialBridge("COM201")

    try:
        assert first._port_lock.acquire()
        assert second._port_lock.acquire()
        assert not duplicate._port_lock.acquire()
    finally:
        duplicate._port_lock.release()
        second._port_lock.release()
        first._port_lock.release()


def test_reader_uses_available_bytes_without_waiting_for_a_fixed_4096_chunk():
    bridge = MKLinkSerialBridge("TEST_STREAM")

    class SerialPort:
        def __init__(self):
            self.waiting = iter((0, 100, 70000))
            self.read_sizes = []

        @property
        def in_waiting(self):
            return next(self.waiting)

        def read(self, size):
            self.read_sizes.append(size)
            if len(self.read_sizes) == 3:
                bridge._running = False
            return b"x"

    serial_port = SerialPort()
    bridge._serial = serial_port
    bridge._ctx.state = DeviceState.DUMP_STREAM
    bridge._running = True

    bridge._reader_loop()

    assert serial_port.read_sizes == [1, 100, 65536]
    assert bridge.drain_stream_bytes() == b"xxx"
def test_connect_uses_staged_fast_timeouts_before_stream_recovery(monkeypatch):
    class PortLock:
        def acquire(self):
            return True

        def release(self):
            pass

    class SerialPort:
        is_open = True

        def __init__(self, *_args, **_kwargs):
            self.writes = []

        def reset_input_buffer(self):
            pass

        def reset_output_buffer(self):
            pass

        def write(self, data):
            self.writes.append(data)

        def close(self):
            self.is_open = False

    class ReaderThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return False

    class PromptEvent:
        def __init__(self):
            self.timeouts = []

        def clear(self):
            pass

        def set(self):
            pass

        def wait(self, timeout):
            self.timeouts.append(timeout)
            return len(self.timeouts) == 3

    serial_port = SerialPort()
    monkeypatch.setattr(bridge_module.serial, "Serial", lambda *_a, **_k: serial_port)
    monkeypatch.setattr(bridge_module.threading, "Thread", ReaderThread)
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    bridge = MKLinkSerialBridge("TEST_CMD")
    bridge._port_lock = PortLock()
    bridge._prompt_event = PromptEvent()
    monkeypatch.setattr(bridge, "_verify_identity", lambda: True)

    assert bridge.connect()
    assert bridge._prompt_event.timeouts == [0.3, 0.7, 1.0]
    assert serial_port.writes[:2] == [b"\n", b"\n"]
    # A probe left in SystemView stream mode must receive its binary STOP
    # frame before the textual fallback commands can restore the REPL.
    assert serial_port.writes[2:4] == [
        b"\x02",
        b"SystemView.stop()\n",
    ]
    # Once SystemView returns a prompt, unrelated fallback commands must not
    # be concatenated into the target's command parser.
    assert b"RTTView.stop()\n" not in serial_port.writes
    assert serial_port.writes[-1] == b"\n"
