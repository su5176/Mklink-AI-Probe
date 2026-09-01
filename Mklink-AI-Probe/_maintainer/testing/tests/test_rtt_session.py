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
from mklink.device import Device, DeviceError
from mklink.rtt import RTTSession


@pytest.fixture(autouse=True)
def _default_trusted_rtt_test_ram(monkeypatch):
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


class StopSensitiveBridge:
    def __init__(self):
        self.state = DeviceState.RTT_STREAM
        self.raw_writes = []
        self.commands = []

    def _exit_stream(self):
        self.state = DeviceState.READY
        return "tail"

    def _write_raw(self, data):
        self.raw_writes.append(data)

    def send_command(self, command, timeout=5.0):
        self.commands.append(command)
        if command == "RTTView.stop()":
            self.state = DeviceState.ERROR
            raise TimeoutError("prompt is unavailable while stopping RTT")
        if self.state is not DeviceState.READY:
            raise ConnectionError("bridge is not immediately reusable")
        return (
            "Find SEGGER RTT addr 0x20000000\n"
            "UpBuffer Channel 0 Size: 1024 Mode: 0\n>>>"
        )

    def _enter_stream(self, state):
        self.state = state


def test_bridge_rtt_byte_reader_preserves_non_utf8_payload():
    bridge = object.__new__(MKLinkSerialBridge)
    bridge._running = True
    bridge._buffer_lock = threading.Lock()
    payload = "中文".encode("gbk")
    bridge._response_buffer = [payload[:1], payload[1:]]

    assert bridge.read_stream_bytes(duration=0.001) == payload


def test_bridge_rtt_stream_arm_preserves_prompt_tail_and_following_bytes():
    bridge = MKLinkSerialBridge("TEST_RTT_STREAM")
    command_written = threading.Event()
    first_stream_bytes = b"\xffrtt_tick=1,"
    following_stream_bytes = b"seq=2\r\n"
    chunks = [
        b"Find SEGGER RTT addr 0x20000010\r\n>>",
        b">" + first_stream_bytes,
        following_stream_bytes,
    ]

    class SerialPort:
        @property
        def in_waiting(self):
            return len(chunks[0]) if command_written.is_set() and chunks else 0

        def write(self, _data):
            command_written.set()

        def read(self, _size):
            command_written.wait(timeout=1.0)
            data = chunks.pop(0)
            if not chunks:
                bridge._running = False
            return data

    bridge._serial = SerialPort()
    bridge._ctx.state = DeviceState.READY
    bridge._running = True
    reader = threading.Thread(target=bridge._reader_loop)
    reader.start()

    response = bridge.send_command(
        "RTTView.start(0x20000000,64,0)",
        timeout=1.0,
        stream_state=DeviceState.RTT_STREAM,
    )
    reader.join(timeout=1.0)

    assert not reader.is_alive()
    assert response == "Find SEGGER RTT addr 0x20000010\r\n"
    bridge._enter_stream(DeviceState.RTT_STREAM)
    bridge._running = True
    try:
        assert bridge.read_stream_bytes(duration=0.001) == (
            first_stream_bytes + following_stream_bytes
        )
    finally:
        bridge._running = False


def test_bridge_rtt_stream_tail_does_not_pollute_the_next_command():
    bridge = MKLinkSerialBridge("TEST_RTT_NEXT_COMMAND")
    chunks: queue.Queue[bytes] = queue.Queue()
    first_stream_bytes = b"raw-stream-tail"

    class SerialPort:
        @property
        def in_waiting(self):
            return 0

        def write(self, data):
            if data.startswith(b"RTTView.start"):
                chunks.put(
                    b"Find SEGGER RTT addr 0x20000010\r\n>>>"
                    + first_stream_bytes
                )
            elif data.startswith(b"second"):
                chunks.put(b"SECOND_OK>>>")

        def read(self, _size):
            try:
                return chunks.get(timeout=0.02)
            except queue.Empty:
                return b""

    bridge._serial = SerialPort()
    bridge._ctx.state = DeviceState.READY
    bridge._running = True
    reader = threading.Thread(target=bridge._reader_loop)
    reader.start()
    try:
        response = bridge.send_command(
            "RTTView.start(0x20000000,64,0)",
            timeout=1.0,
            stream_state=DeviceState.RTT_STREAM,
        )
        assert response == "Find SEGGER RTT addr 0x20000010\r\n"
        bridge._enter_stream(DeviceState.RTT_STREAM)
        assert bridge.read_stream_bytes(duration=0.001) == first_stream_bytes
        bridge._exit_stream()

        assert bridge.send_command("second()", timeout=1.0) == "SECOND_OK"
    finally:
        bridge._running = False
        reader.join(timeout=1.0)

    assert not reader.is_alive()


def test_rtt_stop_uses_raw_stop_and_allows_immediate_restart():
    bridge = StopSensitiveBridge()
    session = RTTSession(bridge)
    session._running = True

    assert session.stop() == "tail"
    assert bridge.state is DeviceState.READY
    assert bridge.raw_writes == [b"RTTView.stop()\n"]
    assert "RTTView.stop()" not in bridge.commands

    result = session.start("0x20000000", search_size=1024)
    assert result["control_block_addr"] == "0x20000000"
    assert bridge.state is DeviceState.RTT_STREAM


def test_bridge_exit_stream_preserves_reader_transport_error():
    bridge = MKLinkSerialBridge("TEST_RTT_EXIT_TRANSPORT_ERROR")
    transport_error = serial.SerialException("probe disconnected")
    bridge._ctx.state = DeviceState.RTT_STREAM
    bridge._transport_error = transport_error
    bridge._response_buffer = [b"tail"]

    assert bridge._exit_stream() == "tail"
    assert bridge.state is DeviceState.ERROR
    assert bridge._transport_error is transport_error


def test_rtt_stop_does_not_write_after_transport_error():
    bridge = MKLinkSerialBridge("TEST_RTT_STOP_TRANSPORT_ERROR")
    writes = []

    class SerialPort:
        def write(self, data):
            writes.append(data)

    transport_error = serial.SerialException("probe disconnected")
    bridge._serial = SerialPort()
    bridge._ctx.state = DeviceState.RTT_STREAM
    bridge._transport_error = transport_error
    session = RTTSession(bridge)
    session._running = True
    session._input_guard_tail = b"partial"

    with pytest.raises(ConnectionError) as caught:
        session.stop()

    assert caught.value.__cause__ is transport_error
    assert bridge.state is DeviceState.ERROR
    assert writes == []
    assert session._running is False
    assert session._input_guard_tail == b""


def test_rtt_stop_records_raw_write_transport_error():
    bridge = MKLinkSerialBridge("TEST_RTT_STOP_WRITE_ERROR")
    transport_error = serial.SerialException("write failed")

    class SerialPort:
        def write(self, _data):
            raise transport_error

    bridge._serial = SerialPort()
    bridge._ctx.state = DeviceState.RTT_STREAM
    session = RTTSession(bridge)
    session._running = True

    with pytest.raises(ConnectionError):
        session.stop()

    assert bridge._transport_error is transport_error
    assert bridge.state is DeviceState.ERROR
    assert session._running is False


def test_device_transport_error_is_not_connected_but_close_still_works():
    closed = []

    class BrokenBridge:
        state = DeviceState.ERROR
        _transport_error = serial.SerialException("probe disconnected")

        def close(self):
            closed.append(True)

    device = Device()
    device._connected = True
    device._bridge = BrokenBridge()

    assert device.connected is False
    assert device.state is DeviceState.ERROR

    device.close()

    assert closed == [True]
    assert device.connected is False
    assert device.state is DeviceState.DISCONNECTED


def test_device_rtt_stop_clears_session_when_stop_raises():
    stop_error = ConnectionError("RTT transport failed")

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
    device._rtt_session = Session()
    device._rtt_control_block_info = {"control_block_addr": 0x20000000}
    device._rtt_write_guard_tail = b"partial"

    with pytest.raises(ConnectionError) as caught:
        device.rtt_stop()

    assert caught.value is stop_error
    assert device._rtt_session is None
    assert device._rtt_control_block_info is None
    assert device._rtt_write_guard_tail == b""


def test_rtt_failed_start_reset_uses_raw_fallback_and_restores_ready_state():
    bridge = StopSensitiveBridge()
    session = RTTSession(bridge)
    session._running = True

    assert session.reset_failed_start() == "tail"
    assert session._running is False
    assert bridge.state is DeviceState.READY
    assert bridge.raw_writes == [b"RTTView.stop()\n"]

    # The failed recovery command must not strand the local bridge in ERROR.
    assert "Find SEGGER RTT addr" in bridge.send_command("next()")


def test_rtt_failed_start_recovery_identity_checks_after_late_start_prompt(
    monkeypatch,
):
    monkeypatch.setattr("mklink.bridge._FAILED_STREAM_STOP_TIMEOUT", 0.1)
    monkeypatch.setattr("mklink.bridge._FAILED_STREAM_IDENTITY_TIMEOUT", 0.2)
    bridge = MKLinkSerialBridge("TEST_RTT_FAILED_START_RECOVERY")
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
            if data == b"RTTView.stop()\n":
                # The stale start prompt arrives first and would falsely
                # complete a normal send_command(stop).  The real stop prompt
                # deliberately follows after identity verification has begun.
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
                timer = threading.Timer(0.08, incoming.put, args=(b"NEXT_OK>>>",))
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
    session = RTTSession(bridge)
    session._running = True
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
        b"RTTView.stop()\n",
        identity_command,
        b"next()\n",
    ]


def test_bridge_failed_stream_recovery_allows_one_bounded_stop_retry(monkeypatch):
    monkeypatch.setattr("mklink.bridge._FAILED_STREAM_STOP_TIMEOUT", 0.02)
    monkeypatch.setattr("mklink.bridge._FAILED_STREAM_IDENTITY_TIMEOUT", 0.1)
    bridge = MKLinkSerialBridge("TEST_RTT_SECOND_STOP")
    incoming: queue.Queue[bytes] = queue.Queue()
    stop_count = 0
    identity_command = (MKLINK_IDENTITY_COMMAND + "\n").encode("utf-8")

    class SerialPort:
        @property
        def in_waiting(self):
            return 0

        def write(self, data):
            nonlocal stop_count
            if data == b"RTTView.stop()\n":
                stop_count += 1
                if stop_count == 2:
                    incoming.put(b">>>")
            elif data == identity_command:
                incoming.put((MKLINK_IDENTITY_TOKEN + "\n>>>").encode("utf-8"))

        def read(self, _size):
            try:
                return incoming.get(timeout=0.01)
            except queue.Empty:
                return b""

    bridge._serial = SerialPort()
    bridge._ctx.state = DeviceState.ERROR
    bridge._running = True
    reader = threading.Thread(target=bridge._reader_loop)
    reader.start()
    try:
        assert bridge._recover_failed_stream_start(b"RTTView.stop()\n") is True
    finally:
        bridge._running = False
        reader.join(timeout=1.0)

    assert stop_count == 2
    assert bridge.state is DeviceState.READY


def test_bridge_failed_stream_recovery_stops_after_two_attempts(monkeypatch):
    monkeypatch.setattr("mklink.bridge._FAILED_STREAM_STOP_TIMEOUT", 0.001)
    bridge = MKLinkSerialBridge("TEST_RTT_BOUNDED_STOP")
    writes = []

    class SilentSerialPort:
        def write(self, data):
            writes.append(data)

    bridge._serial = SilentSerialPort()
    bridge._ctx.state = DeviceState.ERROR

    assert bridge._recover_failed_stream_start(b"RTTView.stop()\n") is False
    assert writes == [b"RTTView.stop()\n", b"RTTView.stop()\n"]
    assert bridge.state is DeviceState.ERROR


def test_rtt_start_propagates_reader_transport_error_without_raw_recovery():
    bridge = MKLinkSerialBridge("TEST_RTT_READER_ERROR")
    command_written = threading.Event()
    writes = []

    class FailingSerialPort:
        @property
        def in_waiting(self):
            return 0

        def write(self, data):
            writes.append(data)
            command_written.set()

        def read(self, _size):
            command_written.wait(timeout=1.0)
            raise serial.SerialException("probe disconnected during RTT start")

    bridge._serial = FailingSerialPort()
    bridge._ctx.state = DeviceState.READY
    bridge._running = True
    reader = threading.Thread(target=bridge._reader_loop)
    reader.start()
    session = RTTSession(bridge)
    try:
        with pytest.raises(ConnectionError, match="串口读取失败"):
            session.start("0x20000000", search_size=1024)
        assert bridge.state is DeviceState.ERROR
        assert isinstance(bridge._transport_error, serial.SerialException)

        # Device.rtt_start invokes this cleanup after propagating the original
        # exception.  A real transport fault must not be rewritten to READY or
        # trigger another serial write.
        assert session.reset_failed_start() == ""
        assert bridge.state is DeviceState.ERROR
        assert writes == [b"RTTView.start(0x20000000,1024,0)\n"]
    finally:
        bridge._running = False
        reader.join(timeout=1.0)

    assert not reader.is_alive()


def _rtt_control_block_memory(*, max_up=3, max_down=3):
    header = (
        b"SEGGER RTT" + b"\x00" * 6
        + max_up.to_bytes(4, "little")
        + max_down.to_bytes(4, "little")
    )

    def descriptor(buffer_address, size, write_offset=0, read_offset=0, flags=0):
        return (
            (0).to_bytes(4, "little")
            + buffer_address.to_bytes(4, "little")
            + size.to_bytes(4, "little")
            + write_offset.to_bytes(4, "little")
            + read_offset.to_bytes(4, "little")
            + flags.to_bytes(4, "little")
        )

    up = b"".join(
        descriptor(0x20000100 + channel * 0x100, 256)
        for channel in range(max_up)
    )
    down = (
        descriptor(0x20001000, 16)
        + descriptor(0x20002000, 8, flags=1)
        + descriptor(0, 0)
    )[:max_down * 24]
    return header, up + down


def _trust_test_ram(device):
    device._get_mcu_profile = lambda: {
        "ram_base": "0x20000000",
        "regions": [{
            "name": "ram",
            "start": "0x20000000",
            "size": "0x10000",
        }],
    }


def test_device_reads_down_buffers_from_standard_rtt_control_block():
    header, descriptors = _rtt_control_block_memory()
    device = Device()
    _trust_test_ram(device)
    reads = []

    def read_memory(address, size):
        reads.append((address, size))
        return header if len(reads) == 1 else descriptors

    device.read_memory = read_memory

    assert device._read_rtt_down_buffers(0x20000000) == [
        {"channel": 0, "size": 16, "mode": 0, "active": True, "name": ""},
        {"channel": 1, "size": 8, "mode": 1, "active": True, "name": ""},
        {"channel": 2, "size": 0, "mode": 0, "active": False, "name": ""},
    ]
    assert reads == [
        (0x20000000, 24),
        (0x20000000 + 24, 6 * 24),
    ]


def test_device_marks_wrapping_down_buffer_inactive():
    header, descriptors = _rtt_control_block_memory()
    wrapping_down = bytearray(descriptors)
    first_down = 3 * 24
    wrapping_down[first_down + 4:first_down + 8] = (
        0xFFFFFFF0
    ).to_bytes(4, "little")
    wrapping_down[first_down + 8:first_down + 12] = (32).to_bytes(4, "little")
    device = Device()
    _trust_test_ram(device)
    reads = iter((header, bytes(wrapping_down)))
    device.read_memory = lambda _address, _size: next(reads)

    buffers = device._read_rtt_down_buffers(0x20000000)

    assert buffers[0]["size"] == 32
    assert buffers[0]["active"] is False
    assert buffers[1]["active"] is True


def _device_with_rtt_memory(header, descriptors, bridge=None):
    bridge = bridge or StopSensitiveBridge()
    bridge.state = DeviceState.READY
    device = Device()
    device._connected = True
    device._bridge = bridge
    _trust_test_ram(device)

    def read_memory(_address, size):
        if size == 24:
            return header
        if size == len(descriptors):
            return descriptors
        raise AssertionError(size)

    device.read_memory = read_memory
    return device, bridge


def test_device_allows_output_only_rtt_but_rejects_write_without_downbuffer():
    header, descriptors = _rtt_control_block_memory(max_down=0)
    device, bridge = _device_with_rtt_memory(header, descriptors)

    result = device.rtt_start("0x20000000", mode=1, search_size=0)

    assert result["down_buffers"] == []
    with pytest.raises(DeviceError, match="DownBuffer channel 0 does not exist"):
        device.rtt_write(b"input")
    assert bridge.raw_writes == []


def test_device_rejects_missing_target_up_channel_before_probe_start():
    header, descriptors = _rtt_control_block_memory(max_up=1, max_down=0)
    device, bridge = _device_with_rtt_memory(header, descriptors)

    with pytest.raises(DeviceError, match="UpBuffer channel 1 does not exist"):
        device.rtt_start(
            "0x20000000", channel=1, mode=1, search_size=0,
        )

    assert bridge.commands == []


def test_device_rejects_target_up_count_above_v4_firmware_limit():
    header, descriptors = _rtt_control_block_memory(max_up=4, max_down=0)
    device, bridge = _device_with_rtt_memory(header, descriptors)

    with pytest.raises(DeviceError, match="firmware limit of 3"):
        device.rtt_start("0x20000000", mode=1, search_size=0)

    assert bridge.commands == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("buffer", 0x08000000, "outside known target writable RAM"),
        ("write_offset", 256, "offset is outside"),
        ("read_offset", 256, "offset is outside"),
    ),
)
def test_device_rejects_unsafe_up_descriptor_before_probe_start(
    field, value, message,
):
    header, descriptors = _rtt_control_block_memory(max_down=0)
    corrupt = bytearray(descriptors)
    ranges = {
        "buffer": (4, 8),
        "write_offset": (12, 16),
        "read_offset": (16, 20),
    }
    start, end = ranges[field]
    corrupt[start:end] = value.to_bytes(4, "little")
    device, bridge = _device_with_rtt_memory(header, bytes(corrupt))

    with pytest.raises(DeviceError, match=message):
        device.rtt_start("0x20000000", mode=1, search_size=0)

    assert bridge.commands == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("buffer", 0x08000000, "outside known target writable RAM"),
        ("write_offset", 16, "offset is outside"),
        ("read_offset", 16, "offset is outside"),
        ("size", 0xFFFFFFFF, "size exceeds safety limit"),
    ),
)
def test_device_rtt_write_rejects_unsafe_down_descriptor(
    field, value, message,
):
    header, descriptors = _rtt_control_block_memory(max_down=1)
    corrupt = bytearray(descriptors)
    down = 3 * 24
    ranges = {
        "buffer": (down + 4, down + 8),
        "size": (down + 8, down + 12),
        "write_offset": (down + 12, down + 16),
        "read_offset": (down + 16, down + 20),
    }
    start, end = ranges[field]
    corrupt[start:end] = value.to_bytes(4, "little")
    device, bridge = _device_with_rtt_memory(header, bytes(corrupt))
    device.rtt_start("0x20000000", mode=1, search_size=0)

    with pytest.raises(DeviceError, match=message):
        device.rtt_write(b"input")

    assert bridge.raw_writes == []


def test_device_rtt_write_rejects_reserved_stop_across_calls():
    header, descriptors = _rtt_control_block_memory(max_down=1)
    device, bridge = _device_with_rtt_memory(header, descriptors)
    device.rtt_start("0x20000000", mode=1, search_size=0)

    assert device.rtt_write(b"RTTView.st") is True
    with pytest.raises(DeviceError, match=r"RTTView\.stop"):
        device.rtt_write(b"op()")

    assert bridge.raw_writes == [b"RTTView.st"]


def test_device_failed_rtt_write_does_not_advance_reserved_sequence_guard():
    header, descriptors = _rtt_control_block_memory(max_down=1)
    device, _bridge = _device_with_rtt_memory(header, descriptors)
    info = device._read_rtt_control_block(0x20000000)
    writes = []

    class Session:
        _running = True
        _channel = 0

        def send_input(self, data):
            writes.append(data)
            if len(writes) == 1:
                raise ConnectionError("serial write failed")
            return True

    device._rtt_session = Session()
    device._rtt_control_block_info = info

    with pytest.raises(ConnectionError, match="serial write failed"):
        device.rtt_write(b"RTTView.st")
    assert device.rtt_write(b"op()") is True
    assert writes == [b"RTTView.st", b"op()"]


@pytest.mark.parametrize(
    "addr",
    ("0x20000002", "0x40000000", "0x08000000"),
)
def test_device_rejects_unaligned_or_non_ram_control_block_before_swd(addr):
    device, bridge = _device_with_rtt_memory(b"", b"")
    reads = []
    device.read_memory = lambda address, size: reads.append((address, size))

    with pytest.raises((ValueError, DeviceError)):
        device.rtt_start(addr, mode=1, search_size=0)

    assert reads == []
    assert bridge.commands == []


def test_device_rejects_scan_window_outside_profile_ram_before_swd():
    device, bridge = _device_with_rtt_memory(b"", b"")
    device._get_mcu_profile = lambda: {
        "ram_base": "0x20000000",
        "regions": [{
            "name": "ram",
            "start": "0x20000000",
            "size": "0x100",
        }],
    }
    reads = []
    device.read_memory = lambda address, size: reads.append((address, size))

    with pytest.raises(DeviceError, match="scan window"):
        device.rtt_start(
            "0x20000080", mode=0, search_size=0x80,
        )

    assert reads == []
    assert bridge.commands == []


def test_device_rejects_rtt_when_no_trusted_ram_map_is_available(tmp_path):
    device, bridge = _device_with_rtt_memory(b"", b"")
    device._project_root = str(tmp_path)
    device._get_mcu_profile = lambda: None
    reads = []
    device.read_memory = lambda address, size: reads.append((address, size))

    with pytest.raises(DeviceError, match="No trusted target RAM map"):
        device.rtt_start("0x20000000", mode=1, search_size=0)

    assert reads == []
    assert bridge.commands == []


def test_device_accepts_hpm_rtt_address_from_matched_profile():
    header, descriptors = _rtt_control_block_memory(max_down=0)
    device, _bridge = _device_with_rtt_memory(header, descriptors)
    device._get_mcu_profile = lambda: {
        "ram_base": "0x00080000",
        "regions": [{
            "name": "ram",
            "start": "0x00080000",
            "size": "0x40000",
        }],
    }

    device.validate_rtt_stream_request(
        "0x00080000", search_size=1024, mode=0,
    )


def test_device_exact_rtt_start_uses_control_block_down_buffer_fallback():
    header, down = _rtt_control_block_memory()
    bridge = StopSensitiveBridge()
    bridge.state = DeviceState.READY
    device = Device()
    device._connected = True
    device._bridge = bridge
    device.read_memory = lambda _address, size: header if size == 24 else down

    result = device.rtt_start("0x20000000", mode=1, search_size=0)

    assert result["down_buffer_source"] == "target-control-block"
    assert [item["size"] for item in result["down_buffers"]] == [16, 8, 0]
    assert bridge.state is DeviceState.RTT_STREAM


def test_device_dynamic_exact_address_still_performs_bounded_host_scan():
    control_block_addr = 0x20000000
    header, down = _rtt_control_block_memory()
    scan_size = 1024 + len(b"SEGGER RTT") - 1
    scan = header + b"\x00" * (scan_size - len(header))
    reads = []

    def read_memory(address, size):
        reads.append((address, size))
        if address == control_block_addr and size == 24:
            return header
        if address == control_block_addr and size == scan_size:
            return scan
        if address == control_block_addr + 24 and size == len(down):
            return down
        raise AssertionError((address, size))

    bridge = StopSensitiveBridge()
    bridge.state = DeviceState.READY
    device = Device()
    device._connected = True
    device._bridge = bridge
    device.read_memory = read_memory

    result = device.rtt_start(
        f"0x{control_block_addr:08X}", mode=0, search_size=1024,
    )

    assert result["control_block_addr"] == f"0x{control_block_addr:08x}"
    assert result["down_buffer_source"] == "target-control-block"
    assert (control_block_addr, scan_size) in reads


class CorruptDescriptorBridge(StopSensitiveBridge):
    def send_command(self, command, timeout=5.0):
        self.commands.append(command)
        return (
            "Find SEGGER RTT addr 0x20000000\n"
            "UpBuffer Channel 0 Size: 16384 Mode: 0\n"
            "DownBuffer Channel 0 Size: 0 Mode: 536873680\n"
            "DownBuffer Channel 2 Size: 640616 Mode: 536873680\n>>>"
        )


@pytest.mark.parametrize(("mode", "search_size"), [(0, 1024), (1, 0)])
def test_device_rtt_start_prefers_target_down_buffers_over_corrupt_probe_output(
    mode, search_size,
):
    header, down = _rtt_control_block_memory()
    bridge = CorruptDescriptorBridge()
    bridge.state = DeviceState.READY
    device = Device()
    device._connected = True
    device._bridge = bridge
    def read_memory(address, size):
        if size == 24:
            return header
        if address == 0x20000000 and size > len(down):
            return header + b"\x00" * (size - len(header))
        return down

    device.read_memory = read_memory

    result = device.rtt_start(
        "0x20000000", mode=mode, search_size=search_size,
    )

    assert result["down_buffer_source"] == "target-control-block"
    assert [item["size"] for item in result["down_buffers"]] == [16, 8, 0]


@pytest.mark.parametrize(
    "address_source",
    ("explicit", "configured", "auto-configured", "integer"),
)
@pytest.mark.parametrize("requested_search_size", (0, 64))
def test_device_dynamic_rtt_start_reads_down_buffers_at_reported_shifted_address(
    tmp_path,
    address_source,
    requested_search_size,
):
    requested_addr = 0x20000000
    actual_addr = requested_addr + 0x10
    effective_search_size = requested_search_size or 1024
    header, down = _rtt_control_block_memory()
    scan = bytearray(effective_search_size + len(b"SEGGER RTT") - 1)
    scan[actual_addr - requested_addr:actual_addr - requested_addr + len(header)] = header
    reads = []

    class ShiftedControlBlockBridge(StopSensitiveBridge):
        def send_command(self, command, timeout=5.0):
            self.commands.append(command)
            return (
                f"Find SEGGER RTT addr 0x{actual_addr:08x}\n"
                "UpBuffer Channel 0 Size: 16384 Mode: 0\n"
                "DownBuffer Channel 0 Size: 0 Mode: 536873680\n>>>"
            )

    def read_memory(address, size):
        reads.append((address, size))
        if address == requested_addr and size == 24:
            return b"\x00" * size
        if address == requested_addr and size == len(scan):
            return bytes(scan)
        if address == actual_addr and size == 24:
            return header
        if address == actual_addr + 24 and size == len(down):
            return down
        raise AssertionError((address, size))

    bridge = ShiftedControlBlockBridge()
    bridge.state = DeviceState.READY
    if address_source in ("configured", "auto-configured"):
        from mklink.project_config import save_rtt_config

        save_rtt_config(str(tmp_path), {
            "rtt_addr": f"0x{requested_addr:08x}",
            "rtt_storage_mode": 0,
        })
    device = Device(project_root=str(tmp_path))
    device._connected = True
    device._bridge = bridge
    device.read_memory = read_memory

    start_addr = {
        "explicit": f"0x{requested_addr:08x}",
        "configured": None,
        "auto-configured": None,
        "integer": requested_addr,
    }[address_source]
    start_mode = None if address_source == "auto-configured" else 0
    result = device.rtt_start(
        start_addr, mode=start_mode, search_size=requested_search_size,
    )

    assert result["control_block_addr"] == f"0x{actual_addr:08x}"
    assert result["down_buffer_source"] == "target-control-block"
    assert [item["size"] for item in result["down_buffers"]] == [16, 8, 0]
    assert (actual_addr, 24) in reads
    assert (actual_addr + 24, len(down)) in reads
    assert bridge.commands[0] == (
        f"RTTView.start(0x{actual_addr:08X},4,0)"
    )


@pytest.mark.parametrize("method_name", ("rtt_start", "systemview_start"))
@pytest.mark.parametrize(
    "kwargs",
    (
        {"addr": "0); reboot(); RTTView.start(0", "mode": 0},
        {"addr": "-1", "mode": 0},
        {"addr": "0x100000000", "mode": 0},
        {"addr": "0xFFFFFFFF", "search_size": 1024, "mode": 0},
        {"addr": True, "mode": 0},
        {"addr": "0x20000000", "channel": 16, "mode": 0},
        {"addr": "0x20000000", "search_size": -1, "mode": 0},
        {"addr": "0x20000000", "search_size": 65537, "mode": 0},
        {"addr": "0x20000000", "search_size": True, "mode": 0},
        {"addr": "0x20000000", "mode": True},
    ),
)
def test_device_stream_entry_points_reject_unsafe_probe_parameters(
    method_name,
    kwargs,
):
    bridge = StopSensitiveBridge()
    bridge.state = DeviceState.READY
    device = Device()
    device._connected = True
    device._bridge = bridge

    with pytest.raises(ValueError):
        getattr(device, method_name)(**kwargs)

    assert bridge.commands == []
    assert bridge.raw_writes == []


@pytest.mark.parametrize("method_name", ("rtt_start", "systemview_start"))
def test_device_static_stream_requires_an_explicit_or_configured_address(
    tmp_path,
    method_name,
):
    bridge = StopSensitiveBridge()
    bridge.state = DeviceState.READY
    device = Device(project_root=str(tmp_path))
    device._connected = True
    device._bridge = bridge

    with pytest.raises(ValueError, match="addr is required"):
        getattr(device, method_name)(mode=1)

    assert bridge.commands == []
    assert bridge.raw_writes == []


class ExactModeUnsupportedBridge(StopSensitiveBridge):
    def __init__(self):
        super().__init__()
        self.exact_start_pending = False

    def send_command(self, command, timeout=5.0):
        self.commands.append(command)
        if ",0,0)" in command:
            self.exact_start_pending = True
            return ">>>"
        if command == "RTTView.stop()":
            self.exact_start_pending = False
            return ">>>"
        if ",4,0)" in command:
            if self.exact_start_pending:
                return ">>>"
            return (
                "Find SEGGER RTT addr 0x20000000\n"
                "UpBuffer Channel 0 Size: 1024 Mode: 0\n"
                "DownBuffer Channel 0 Size: 0 Mode: 536873680\n"
                "DownBuffer Channel 2 Size: 640616 Mode: 536873680\n>>>"
            )
        raise AssertionError(command)


def test_device_exact_rtt_start_resets_probe_before_bounded_scan_fallback():
    header, down = _rtt_control_block_memory()
    bridge = ExactModeUnsupportedBridge()
    bridge.state = DeviceState.READY
    device = Device()
    device._connected = True
    device._bridge = bridge
    device.read_memory = lambda _address, size: header if size == 24 else down

    result = device.rtt_start("0x20000000", mode=1, search_size=0)

    assert bridge.commands[:3] == [
        "RTTView.start(0x20000000,0,0)",
        "RTTView.stop()",
        "RTTView.start(0x20000000,4,0)",
    ]
    assert result["probe_compatibility_mode"] == "bounded-scan"
    assert result["storage_mode"] == 1
    assert result["down_buffer_source"] == "target-control-block"
    assert [item["size"] for item in result["down_buffers"]] == [16, 8, 0]


def test_device_output_only_static_rtt_uses_bounded_scan_compatibility_fallback():
    header, descriptors = _rtt_control_block_memory(max_down=0)
    bridge = ExactModeUnsupportedBridge()
    device, _bridge = _device_with_rtt_memory(header, descriptors, bridge)

    result = device.rtt_start("0x20000000", mode=1, search_size=0)

    assert bridge.commands[:3] == [
        "RTTView.start(0x20000000,0,0)",
        "RTTView.stop()",
        "RTTView.start(0x20000000,4,0)",
    ]
    assert result["probe_compatibility_mode"] == "bounded-scan"
    assert result["down_buffers"] == []


def test_device_exact_rtt_start_resolves_configured_address_before_fallback(tmp_path):
    from mklink.project_config import save_rtt_config

    save_rtt_config(str(tmp_path), {
        "rtt_addr": "0x20000000",
        "rtt_storage_mode": 1,
    })
    header, down = _rtt_control_block_memory()
    bridge = ExactModeUnsupportedBridge()
    bridge.state = DeviceState.READY
    device = Device(project_root=str(tmp_path))
    device._connected = True
    device._bridge = bridge
    device.read_memory = lambda _address, size: header if size == 24 else down

    result = device.rtt_start(mode=1, search_size=0)

    assert bridge.commands[:3] == [
        "RTTView.start(0x20000000,0,0)",
        "RTTView.stop()",
        "RTTView.start(0x20000000,4,0)",
    ]
    assert result["control_block_addr"] == "0x20000000"
    assert result["down_buffer_source"] == "target-control-block"


def test_device_rtt_start_rejects_missing_control_block():
    bridge = ExactModeUnsupportedBridge()
    bridge.state = DeviceState.READY
    bridge.send_command = lambda _command, timeout=5.0: ">>>"
    device = Device()
    device._connected = True
    device._bridge = bridge
    device.read_memory = lambda _address, _size: b"\x00" * 24

    with pytest.raises(DeviceError, match="control block"):
        device.rtt_start("0x20000000", mode=1, search_size=0)

    assert device._rtt_session is None


class IncompleteStartBridge:
    """Probe model that starts streaming before returning an incomplete reply."""

    def __init__(self):
        self.state = DeviceState.READY
        self.firmware_streaming = False
        self.commands = []
        self.raw_writes = []
        self.arm_cancelled = 0

    def _arm_stream(self, state):
        self.state = DeviceState.READY
        self.armed_state = state

    def _cancel_stream_arm(self):
        self.arm_cancelled += 1
        self.armed_state = None

    def _enter_stream(self, state):
        self.state = state

    def _exit_stream(self):
        self.state = DeviceState.READY
        return "stream-tail"

    def _write_raw(self, data):
        self.raw_writes.append(data)

    def send_command(self, command, timeout=5.0, stream_state=None):
        if stream_state is not None:
            self._arm_stream(stream_state)
        self.commands.append(command)
        if command.startswith("RTTView.start("):
            self.firmware_streaming = True
            return "RTT stream enabled without address\n"
        if command == "RTTView.stop()":
            self.firmware_streaming = False
            self.state = DeviceState.READY
            return "STOPPED"
        if self.firmware_streaming:
            raise ConnectionError("probe is still in RTT stream mode")
        return "NEXT_OK"


def _incomplete_start_device(bridge):
    header, descriptors = _rtt_control_block_memory()
    device = Device()
    device._connected = True
    device._bridge = bridge
    device.read_memory = (
        lambda _address, size: header if size == 24 else descriptors
    )
    return device


def test_device_recovers_when_probe_streams_but_start_reply_has_no_address():
    bridge = IncompleteStartBridge()
    device = _incomplete_start_device(bridge)

    with pytest.raises(DeviceError, match="control block"):
        device.rtt_start("0x20000000", mode=1, search_size=0)

    assert bridge.commands == [
        "RTTView.start(0x20000000,0,0)",
        "RTTView.stop()",
        "RTTView.start(0x20000000,4,0)",
        "RTTView.stop()",
    ]
    assert bridge.arm_cancelled >= 1
    assert bridge.firmware_streaming is False
    assert bridge.state is DeviceState.READY
    assert device._rtt_session is None
    assert bridge.send_command("next()") == "NEXT_OK"


def test_device_recovers_and_clears_session_when_start_reply_parsing_fails(
    monkeypatch,
):
    bridge = IncompleteStartBridge()
    device = _incomplete_start_device(bridge)

    def fail_parse(_output):
        raise ValueError("malformed RTT start reply")

    monkeypatch.setattr(RTTSession, "_parse_rtt_startup", staticmethod(fail_parse))

    with pytest.raises(ValueError, match="malformed RTT start reply"):
        device.rtt_start("0x20000000", mode=1, search_size=0)

    assert bridge.commands == [
        "RTTView.start(0x20000000,0,0)",
        "RTTView.stop()",
    ]
    assert bridge.firmware_streaming is False
    assert bridge.state is DeviceState.READY
    assert device._rtt_session is None
    assert bridge.send_command("next()") == "NEXT_OK"
