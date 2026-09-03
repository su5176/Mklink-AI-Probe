import binascii
import codecs
import struct
import sys
import threading

import pytest

from mklink._types import DeviceContext, DeviceState
from mklink.bridge import MKLinkSerialBridge
from mklink.dump_memory import (
    FLAG_SAMPLE_DROPPED,
    MAGIC,
    DumpMemoryBusyError,
    DumpMemoryReadError,
    DumpMemoryStreamSession,
    MAX_SAFE_REPL_REGIONS,
    build_dump_mem_command,
    read_dump_memory_once,
    read_dump_memory_range_once,
    read_dump_memory_regions_once,
)


def _old_frame(timestamp_us, payload, *, flags=0):
    region = b"\x00" + struct.pack("<H", len(payload)) + payload
    length = 19 + len(region) + 6
    body = MAGIC + struct.pack("<QHB", timestamp_us, length, 1) + region + struct.pack("<H", flags)
    return body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


def _b1_frame(timestamp_us, payload, *, block_index, block_count, total_size, flags=0):
    block_crc = binascii.crc32(payload) & 0xFFFFFFFF
    body = bytearray()
    body.extend(MAGIC)
    body.extend(struct.pack("<QH", timestamp_us, 0))
    body.extend(struct.pack("<B", 1))
    body.extend(struct.pack("<H", flags))
    body.extend(struct.pack("<IHHHI", total_size, 2048, block_index, block_count, block_crc))
    body.extend(struct.pack("<BH", 0, len(payload)))
    body.extend(payload)
    frame_length = len(body) + 4
    struct.pack_into("<H", body, 16, frame_length)
    return bytes(body) + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


def _old_regions_frame(timestamp_us, regions, *, flags=0):
    encoded_regions = b"".join(
        bytes([index]) + struct.pack("<H", len(payload)) + payload
        for index, payload in regions
    )
    length = 19 + len(encoded_regions) + 6
    body = (
        MAGIC
        + struct.pack("<QHB", timestamp_us, length, len(regions))
        + encoded_regions
        + struct.pack("<H", flags)
    )
    return body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


def _b1_regions_frame(
    timestamp_us,
    regions,
    *,
    block_index,
    block_count,
    total_size,
    flags=0,
    corrupt_block_crc=False,
):
    payload = b"".join(data for _index, data in regions)
    block_crc = binascii.crc32(payload) & 0xFFFFFFFF
    if corrupt_block_crc:
        block_crc ^= 0xFFFFFFFF
    body = bytearray()
    body.extend(MAGIC)
    body.extend(struct.pack("<QH", timestamp_us, 0))
    body.extend(struct.pack("<B", len(regions)))
    body.extend(struct.pack("<H", flags))
    body.extend(struct.pack(
        "<IHHHI", total_size, 2048, block_index, block_count, block_crc,
    ))
    for region_index, data in regions:
        body.extend(struct.pack("<BH", region_index, len(data)))
        body.extend(data)
    struct.pack_into("<H", body, 16, len(body) + 4)
    return bytes(body) + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


class FakeBridge:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.calls = []

    def _enter_stream(self, state):
        self.calls.append(("enter", state))

    def _write_raw(self, data):
        self.calls.append(("write", data))

    def drain_stream_bytes(self, max_bytes=None):
        self.calls.append(("drain", max_bytes))
        return self.chunks.pop(0) if self.chunks else b""

    def _exit_stream(self):
        self.calls.append(("exit",))
        return ""


def test_dump_command_rejects_unsafe_pika_argument_boundary_before_io():
    pairs = [(0x20000000 + index * 4, 4) for index in range(MAX_SAFE_REPL_REGIONS)]
    assert build_dump_mem_command(pairs, 0.001).startswith("cmd.dump_memory(")

    with pytest.raises(ValueError, match="Pika varargs boundary is unsafe"):
        build_dump_mem_command(pairs + [(0x20000100, 4)], 0.001)
    with pytest.raises(ValueError, match="positive integer"):
        build_dump_mem_command([(0x20000000, -1)], 0.001)
    with pytest.raises(ValueError, match="32-bit address space"):
        build_dump_mem_command([(0xFFFFFFFC, 8)], 0.001)
    with pytest.raises(ValueError, match="positive finite"):
        build_dump_mem_command([(0x20000000, 4)], float("inf"))


def test_cli_rejects_unsafe_dump_and_direct_read_before_port_access(monkeypatch, capsys):
    from mklink import cli

    def unexpected_port(_port):
        raise AssertionError("unsafe request reached port discovery")

    monkeypatch.setattr(cli, "_resolve_port", unexpected_port)
    exit_code = cli._cli_dump_memory(
        None,
        [f"0x{0x20000000 + index * 4:08X}:4" for index in range(16)],
    )
    cli._cli_read_ram(None, "0x20000000", 4097, None)

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "safe Pika API boundary" in output
    assert "use dump-memory for larger reads" in output


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["dump-memory"]
            + [f"0x{0x20000000 + index * 4:08X}:4" for index in range(16)],
            "safe Pika API boundary",
        ),
        (
            ["vofa", "--period", "0.001"]
            + [
                value
                for index in range(16)
                for value in (f"0x{0x20000000 + index * 4:08X}", "float")
            ],
            "at most 15",
        ),
    ],
)
def test_cli_main_reports_unsafe_stream_request_as_failure(
    monkeypatch, capsys, arguments, message,
):
    from mklink import cli

    monkeypatch.setattr(sys, "argv", ["mklink", *arguments])
    exit_code = cli.main()
    output = capsys.readouterr().out

    assert exit_code == 2
    assert "[FAIL]" in output
    assert message in output
    assert "Connecting" not in output
    assert "[*] 连接" not in output


def test_cli_dump_stream_finally_sends_explicit_stop_not_one_shot(monkeypatch):
    from mklink import bridge as bridge_module, cli

    class CliBridge(FakeBridge):
        def __init__(self, port):
            super().__init__([])
            self.port = port

        def connect(self):
            return True

        def close(self):
            self.calls.append(("close",))

    created = []

    def make_bridge(port):
        instance = CliBridge(port)
        created.append(instance)
        return instance

    monkeypatch.setattr(cli, "_resolve_port", lambda port: port or "COM_TEST")
    monkeypatch.setattr(cli, "_init_target_bridge", lambda bridge: None)
    monkeypatch.setattr(bridge_module, "MKLinkSerialBridge", make_bridge)

    exit_code = cli._cli_dump_memory(
        None, ["0x20000000:4"], period=0.001, frames=0, duration=0.001,
    )

    assert exit_code == 1
    writes = [call[1] for call in created[0].calls if call[0] == "write"]
    assert writes == [
        b"cmd.dump_memory(0x20000000, 4, 0.001)\n",
        b"cmd.dump_memory(0x20000000, 4, -1.0)\n",
    ]


def test_cli_write_ram_uses_device_path_and_verifies_zero_payload(monkeypatch, capsys):
    from types import SimpleNamespace
    from mklink import cli, device

    writes = []
    fake = SimpleNamespace(
        write_memory=lambda address, data: writes.append((address, data)),
        read_memory=lambda address, size: b"\x00" * size,
        close=lambda: writes.append(("close",)),
    )
    monkeypatch.setattr(cli, "_resolve_port", lambda port: port or "COM_TEST")
    monkeypatch.setattr(device, "connect", lambda **kwargs: fake)

    cli._cli_write_ram(None, "0x24040100", ["0x00"] * 4)

    assert writes == [(0x24040100, b"\x00" * 4), ("close",)]
    assert "[OK] 回读验证通过" in capsys.readouterr().out


def test_cli_write_ram_rejects_invalid_or_oversize_data_before_port_access(monkeypatch, capsys):
    from mklink import cli

    monkeypatch.setattr(
        cli,
        "_resolve_port",
        lambda _port: (_ for _ in ()).throw(AssertionError("port discovery reached")),
    )
    cli._cli_write_ram(None, "0x24040100", ["0x100"])
    cli._cli_write_ram(None, "0xFFFFFFFE", ["0x00"] * 4)
    cli._cli_write_ram(None, "0x24040100", ["0x00"] * 4097)

    output = capsys.readouterr().out
    assert "格式无效" in output
    assert "超出 32 位地址空间" in output
    assert "单次最多写入 4096 字节" in output


def test_superwatch_poll_reuses_one_connection_and_accounts_for_read_time(monkeypatch):
    from unittest.mock import Mock
    from mklink import superwatch

    bridge = Mock()
    bridge.connect.return_value = True
    now = [0.0]
    def send(*args, **kwargs):
        now[0] += 0.04
        return "timestamp=100\n20000000 00 00 80 3f"
    bridge.send_command.side_effect = send
    factory = Mock(return_value=bridge)
    init = Mock()
    monkeypatch.setattr('mklink.bridge.MKLinkSerialBridge', factory)
    monkeypatch.setattr('mklink.device.initialize_target', init)
    sleeps = []
    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    blocks = superwatch.build_read_blocks([superwatch.WatchItem('a', 0x20000000, 'float', 4)])
    points = superwatch.poll_blocks(blocks, port='test', duration=.13, period=.05,
                                   clock=lambda: now[0], sleep_func=sleep)
    assert len(points) == 3
    assert factory.call_count == init.call_count == 1
    assert sleeps == pytest.approx([.01, .01])
    bridge.close.assert_called_once()


@pytest.mark.parametrize("dump_mem", [False, True])
def test_superwatch_cli_always_selects_stream_collector(monkeypatch, dump_mem):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from mklink import cli, superwatch
    collector = Mock(return_value=[])
    monkeypatch.setattr(superwatch, 'poll_blocks_dumpmem', collector, raising=False)
    monkeypatch.setattr(superwatch, 'find_project_svd', lambda _: None)
    monkeypatch.setattr(superwatch, 'resolve_watch_items', lambda *a, **k: [superwatch.WatchItem('a', 0x20000000, 'float', 4)])
    poll = Mock(return_value=[])
    monkeypatch.setattr(superwatch, 'poll_blocks', poll)
    cli._cli_superwatch(SimpleNamespace(svd=None, project_root='.', source=None, variables=['a'],
                                       visualize=False, period=.001, port='test', duration=1, dump_mem=dump_mem))
    collector.assert_called_once()
    poll.assert_not_called()


def test_superwatch_dump_collector_decodes_device_time_and_releases_stream(monkeypatch):
    from unittest.mock import Mock
    from mklink import superwatch

    bridge = FakeBridge([_old_frame(1000, struct.pack('<f', 1.0)) + _old_frame(1130, struct.pack('<f', 2.0))])
    bridge.close = Mock()
    monkeypatch.setattr(superwatch, '_open_sampling_bridge', lambda _: bridge)
    now = iter([0, .1, .2, .3, .4])
    blocks = superwatch.build_read_blocks([superwatch.WatchItem('a', 0x20000000, 'float', 4)])
    points = superwatch.poll_blocks_dumpmem(blocks, duration=.35, clock=lambda: next(now), sleep_func=lambda _: None)
    assert [p['a'] for p in points] == [1, 2]
    assert [p['_t'] for p in points] == pytest.approx([0, .00013])
    assert bridge.calls[0] == ('enter', DeviceState.DUMP_STREAM)
    assert bridge.calls[-1] == ('exit',)
    bridge.close.assert_called_once()


def test_standalone_superwatch_uses_explicit_dump_stop(monkeypatch):
    from mklink import bridge as bridge_module, cli, rtt_viewer, superwatch

    class Server:
        def __init__(self, **kwargs):
            self.collecting = threading.Event()
            self.collecting.set()
            self.events = []

        def start(self):
            return 12345

        def push_event(self, event, payload):
            self.events.append((event, payload))

        def push_data_point(self, point):
            return None

        def stop(self):
            return None

    class Bridge:
        def __init__(self, port):
            self.port = port
            self.commands = []

        def connect(self):
            return True

        def send_command(self, command, timeout=0):
            self.commands.append(command)
            return ""

        def _enter_stream(self, state):
            return None

        def drain_stream_bytes(self):
            return b""

        def _exit_stream(self):
            return None

        def close(self):
            return None

    created = []

    def make_bridge(port):
        instance = Bridge(port)
        created.append(instance)
        return instance

    monkeypatch.setattr(rtt_viewer, "VisualizationServer", Server)
    monkeypatch.setattr(bridge_module, "MKLinkSerialBridge", make_bridge)
    monkeypatch.setattr(cli, "_resolve_port", lambda port: port or "COM_TEST")

    superwatch.run_superwatch_visualizer(
        items=[superwatch.WatchItem("value", 0x20000000, "float", 4)],
        period=0.001,
        no_browser=True,
        duration=0.001,
    )

    assert created[0].commands == [
        "cmd.dump_memory(0x20000000, 4, 0.001)",
        "cmd.dump_memory(0x20000000, 4, -1.0)",
    ]


def test_superwatch_poll_closes_on_read_error_and_keeps_custom_reader(monkeypatch):
    from unittest.mock import Mock
    from mklink import superwatch

    bridge = Mock()
    bridge.send_command.side_effect = RuntimeError('read failed')
    factory = Mock(return_value=bridge)
    monkeypatch.setattr(superwatch, '_open_sampling_bridge', factory)
    blocks = superwatch.build_read_blocks([superwatch.WatchItem('a', 0x20000000, 'float', 4)])
    with pytest.raises(RuntimeError, match='read failed'):
        superwatch.poll_blocks(blocks, duration=.1)
    bridge.close.assert_called_once()
    factory.reset_mock()
    reader = Mock(return_value=(struct.pack('<f', 3.0), 'timestamp=1000\n20000000 00 00 40 40'))
    now = iter([0, 0, 1])
    assert superwatch.poll_blocks(blocks, duration=.1, read_func=reader, clock=lambda: next(now))[0]['a'] == 3.0
    factory.assert_not_called()


def test_dump_session_reuses_parser_and_owns_exact_stream_lifecycle():
    bridge = FakeBridge([b"noise" + _old_frame(123, struct.pack("<f", 2.5))])
    session = DumpMemoryStreamSession(
        bridge, [(0x20000000, 4)], 0.0001, stop_grace_s=0,
    )

    session.start()
    frames = session.read_frames(max_bytes=8192)
    session.stop()

    assert frames[0]["timestamp_us"] == 123
    assert struct.unpack("<f", frames[0]["regions"][0][1]) == (2.5,)
    assert bridge.calls[0] == ("enter", DeviceState.DUMP_STREAM)
    assert bridge.calls[1] == (
        "write", b"cmd.dump_memory(0x20000000, 4, 0.0001)\n",
    )
    assert bridge.calls[-3] == (
        "write", b"cmd.dump_memory(0x20000000, 4, -1.0)\n",
    )
    assert ("write", b"cmd.dump_memory(0x20000000, 4, 0)\n") not in bridge.calls
    assert bridge.calls[-2] == ("drain", None)
    assert bridge.calls[-1] == ("exit",)
    assert session.stats == {
        "protocol_frames": 1,
        "complete_samples": 1,
        "parser_dropped_bytes": 5,
        "parser_dropped_frames": 0,
        "parser_crc_errors": 0,
        "firmware_flagged_frames": 0,
        "firmware_sample_drop_flags": 0,
    }


def test_dump_session_reports_crc_loss_and_firmware_drop_flags_separately():
    corrupt = bytearray(_old_frame(1, b"abcd"))
    corrupt[-1] ^= 0xFF
    bridge = FakeBridge([
        bytes(corrupt) + _old_frame(2, b"efgh", flags=FLAG_SAMPLE_DROPPED),
    ])
    session = DumpMemoryStreamSession(bridge, [(0x20000000, 4)], 0.001, stop_grace_s=0)

    session.start()
    frames = session.read_frames()
    session.stop()

    assert len(frames) == 1
    assert session.stats["parser_crc_errors"] == 1
    assert session.stats["parser_dropped_frames"] == 1
    assert session.stats["firmware_flagged_frames"] == 1
    assert session.stats["firmware_sample_drop_flags"] == 1


def test_one_shot_dump_reads_payload_and_stops_stream_cleanly():
    bridge = FakeBridge([b"noise", _old_frame(123, b"\x01\x02\x03\x04")])

    payload = read_dump_memory_once(
        bridge, 0x20000020, 4, timeout=0.1, poll_interval=0,
    )

    assert payload == b"\x01\x02\x03\x04"
    assert bridge.calls[0] == ("enter", DeviceState.DUMP_STREAM)
    assert bridge.calls[1] == (
        "write", b"cmd.dump_memory(0x20000020, 4, 0)\n",
    )
    assert ("write", b"cmd.dump_memory(0x20000020, 1, -1.0)\n") in bridge.calls
    assert ("write", b"RTTView.stop()\n") not in bridge.calls
    assert sum(
        call == ("write", b"cmd.dump_memory(0x20000020, 4, 0)\n")
        for call in bridge.calls
    ) == 1
    assert bridge.calls[-1] == ("exit",)


def test_one_shot_dump_error_uses_dump_stop_without_requesting_another_sample():
    bridge = FakeBridge([])

    with pytest.raises(TimeoutError, match="timed out"):
        read_dump_memory_once(
            bridge, 0x20000020, 4, timeout=0.001, poll_interval=0,
        )

    writes = [call[1] for call in bridge.calls if call[0] == "write"]
    assert writes == [
        b"cmd.dump_memory(0x20000020, 4, 0)\n",
        b"cmd.dump_memory(0x20000020, 1, -1.0)\n",
    ]
    assert b"RTTView.stop()\n" not in writes


def test_one_shot_dump_collects_all_b1_blocks():
    payload = bytes(range(256)) * 16
    bridge = FakeBridge([
        _b1_frame(1, payload[:2048], block_index=0, block_count=2, total_size=len(payload)),
        _b1_frame(2, payload[2048:], block_index=1, block_count=2, total_size=len(payload)),
    ])

    assert read_dump_memory_range_once(
        bridge, 0x80000000, len(payload), timeout=0.1, poll_interval=0,
    ) == payload
    assert ("write", b"cmd.dump_memory(0x80000000, 1, -1.0)\n") in bridge.calls
    assert bridge.calls[-1] == ("exit",)


def test_one_shot_dump_discards_stale_b1_blocks_from_previous_request():
    payload = b"fresh" * 410
    bridge = FakeBridge([
        _b1_frame(1, b"stale" * 410, block_index=0, block_count=2, total_size=4096),
        _b1_frame(2, payload[:2048], block_index=0, block_count=2, total_size=len(payload)),
        _b1_frame(3, payload[2048:], block_index=1, block_count=2, total_size=len(payload)),
    ])

    assert read_dump_memory_range_once(
        bridge, 0x80000000, len(payload), timeout=0.1, poll_interval=0,
    ) == payload


def test_one_shot_dump_rejects_firmware_error_flags():
    bridge = FakeBridge([_old_frame(1, b"abcd", flags=0x0004)])

    with pytest.raises(DumpMemoryReadError, match="error flags"):
        read_dump_memory_range_once(bridge, 0x80000000, 4, timeout=0.1, poll_interval=0)


def test_multi_region_one_shot_accepts_exact_old_sample_and_reuses_bridge():
    bridge = FakeBridge([_old_regions_frame(1, [
        (0, b"abcd"),
        (1, b"ef"),
    ])])

    result = read_dump_memory_regions_once(
        bridge,
        [(0x20000000, 4), (0x20001000, 2)],
        timeout=0.1,
        poll_interval=0,
    )

    assert result == (b"abcd", b"ef")
    assert bridge.calls[0] == ("enter", DeviceState.DUMP_STREAM)
    assert bridge.calls[1] == (
        "write",
        b"cmd.dump_memory(0x20000000, 4, 0x20001000, 2, 0)\n",
    )
    assert ("write", b"cmd.dump_memory(0x20000000, 1, -1.0)\n") in bridge.calls
    assert bridge.calls[-1] == ("exit",)


@pytest.mark.parametrize(
    "active_state",
    [
        DeviceState.RTT_STREAM,
        DeviceState.SYSTEMVIEW_STREAM,
        DeviceState.VOFA_STREAM,
        DeviceState.DUMP_STREAM,
    ],
)
def test_multi_region_one_shot_rejects_active_stream_without_mutating_bridge(
    active_state,
):
    bridge = FakeBridge([_old_regions_frame(1, [(0, b"abcd")])])
    bridge.state = active_state

    with pytest.raises(DumpMemoryBusyError, match="idle READY bridge"):
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 4)],
            timeout=0.1,
            poll_interval=0,
        )

    assert bridge.state is active_state
    assert bridge.calls == []


def test_multi_region_one_shot_serializes_concurrent_dump_captures():
    first_started = threading.Event()
    release_first = threading.Event()

    class StatefulBridge(FakeBridge):
        def __init__(self, chunks, *, block=False):
            super().__init__(chunks)
            self.state = DeviceState.READY
            self.block = block

        def _enter_stream(self, state):
            super()._enter_stream(state)
            self.state = state

        def _write_raw(self, data):
            super()._write_raw(data)
            if self.block and data.endswith(b", 0)\n") and b"-1.0" not in data:
                first_started.set()
                assert release_first.wait(timeout=2.0)

        def _exit_stream(self):
            result = super()._exit_stream()
            self.state = DeviceState.READY
            return result

    first = StatefulBridge([_old_regions_frame(1, [(0, b"aaaa")])], block=True)
    second = StatefulBridge([_old_regions_frame(2, [(0, b"bbbb")])])
    results = []

    def capture(bridge):
        results.append(read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 4)],
            timeout=0.5,
            poll_interval=0,
        ))

    first_thread = threading.Thread(target=capture, args=(first,))
    second_thread = threading.Thread(target=capture, args=(second,))
    first_thread.start()
    assert first_started.wait(timeout=1.0)
    second_thread.start()

    assert second.calls == []
    release_first.set()
    first_thread.join(timeout=2.0)
    second_thread.join(timeout=2.0)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert len(results) == 2
    assert set(results) == {(b"aaaa",), (b"bbbb",)}
    assert second.calls[0] == ("enter", DeviceState.DUMP_STREAM)


def test_real_bridge_atomically_arbitrates_rtt_enter_against_dump_claim():
    for _attempt in range(25):
        bridge = object.__new__(MKLinkSerialBridge)
        bridge._ctx = DeviceContext(state=DeviceState.READY)
        bridge._buffer_lock = threading.Lock()
        bridge._response_buffer = [b"stale"]
        bridge._utf8_decoder = codecs.getincrementaldecoder("utf-8")()
        start = threading.Barrier(3)
        outcomes = []

        def enter_rtt():
            start.wait(timeout=1.0)
            try:
                bridge._enter_stream(DeviceState.RTT_STREAM)
            except RuntimeError:
                outcomes.append(("rtt", False))
            else:
                outcomes.append(("rtt", True))

        def claim_dump():
            start.wait(timeout=1.0)
            outcomes.append((
                "dump",
                bridge._try_enter_stream(DeviceState.DUMP_STREAM),
            ))

        rtt_thread = threading.Thread(target=enter_rtt)
        dump_thread = threading.Thread(target=claim_dump)
        rtt_thread.start()
        dump_thread.start()
        start.wait(timeout=1.0)
        rtt_thread.join(timeout=1.0)
        dump_thread.join(timeout=1.0)

        assert not rtt_thread.is_alive()
        assert not dump_thread.is_alive()
        assert sum(accepted for _name, accepted in outcomes) == 1
        winner = next(name for name, accepted in outcomes if accepted)
        assert bridge.state is (
            DeviceState.RTT_STREAM if winner == "rtt" else DeviceState.DUMP_STREAM
        )
        assert bridge._response_buffer == []


def test_real_bridge_failed_dump_claim_preserves_active_rtt_buffer_and_state():
    bridge = object.__new__(MKLinkSerialBridge)
    bridge._ctx = DeviceContext(state=DeviceState.RTT_STREAM)
    bridge._buffer_lock = threading.Lock()
    bridge._response_buffer = [b"live-rtt-bytes"]
    bridge._utf8_decoder = codecs.getincrementaldecoder("utf-8")()

    assert bridge._try_enter_stream(DeviceState.DUMP_STREAM) is False
    with pytest.raises(RuntimeError, match="bridge state is rtt_stream"):
        bridge._enter_stream(DeviceState.DUMP_STREAM)
    assert bridge.state is DeviceState.RTT_STREAM
    assert bridge._response_buffer == [b"live-rtt-bytes"]


def test_multi_region_old_incomplete_region_reports_region_gap():
    bridge = FakeBridge([_old_regions_frame(1, [
        (0, b"abcd"),
        (1, b"e"),
    ])])

    with pytest.raises(DumpMemoryReadError) as raised:
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 4), (0x20001000, 2)],
            timeout=0.01,
            poll_interval=0,
        )

    assert raised.value.gap_fact == "region_gap_count"
    assert raised.value.gap_count == 1


def test_multi_region_b1_tracks_blocks_and_exact_region_coverage():
    first = b"A" * 1500
    second = b"B" * 1000
    bridge = FakeBridge([
        _b1_regions_frame(
            1,
            [(0, first), (1, second[:548])],
            block_index=0,
            block_count=2,
            total_size=2500,
        ),
        _b1_regions_frame(
            2,
            [(1, second[548:])],
            block_index=1,
            block_count=2,
            total_size=2500,
        ),
    ])

    assert read_dump_memory_regions_once(
        bridge,
        [(0x20000000, 1500), (0x20001000, 1000)],
        timeout=0.1,
        poll_interval=0,
    ) == (first, second)


def test_multi_region_b1_reassembles_out_of_order_complete_blocks():
    first = b"A" * 2048
    bridge = FakeBridge([
        _b1_regions_frame(
            2,
            [(0, b"B")],
            block_index=1,
            block_count=2,
            total_size=2049,
        ),
        _b1_regions_frame(
            1,
            [(0, first)],
            block_index=0,
            block_count=2,
            total_size=2049,
        ),
    ])

    assert read_dump_memory_regions_once(
        bridge,
        [(0x20000000, 2049)],
        timeout=0.1,
        poll_interval=0,
    ) == (first + b"B",)


def test_multi_region_b1_rejects_bad_block_crc_with_stable_gap_fact():
    bridge = FakeBridge([_b1_regions_frame(
        1,
        [(0, b"A" * 2048)],
        block_index=0,
        block_count=2,
        total_size=2049,
        corrupt_block_crc=True,
    )])

    with pytest.raises(DumpMemoryReadError) as raised:
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 2049)],
            timeout=0.1,
            poll_interval=0,
        )

    assert raised.value.gap_fact == "crc_error_count"
    assert raised.value.gap_count == 1


def test_multi_region_b1_missing_tail_never_returns_partial_sample():
    bridge = FakeBridge([_b1_regions_frame(
        1,
        [(0, b"A" * 2048)],
        block_index=0,
        block_count=2,
        total_size=2049,
    )])

    with pytest.raises(DumpMemoryReadError) as raised:
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 2049)],
            timeout=0.01,
            poll_interval=0,
        )

    assert raised.value.gap_fact == "missing_block_count"
    assert raised.value.gap_count == 1


def test_multi_region_b1_rejects_incomplete_per_region_coverage():
    bridge = FakeBridge([
        _b1_regions_frame(
            1,
            [(0, b"A" * 1499), (1, b"B" * 549)],
            block_index=0,
            block_count=2,
            total_size=2500,
        ),
        _b1_regions_frame(
            2,
            [(1, b"B" * 452)],
            block_index=1,
            block_count=2,
            total_size=2500,
        ),
    ])

    with pytest.raises(DumpMemoryReadError) as raised:
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 1500), (0x20001000, 1000)],
            timeout=0.1,
            poll_interval=0,
        )

    assert raised.value.gap_fact == "region_gap_count"


def test_multi_region_b1_rejects_duplicate_block_even_when_bytes_match():
    first = _b1_regions_frame(
        1,
        [(0, b"A" * 2048)],
        block_index=0,
        block_count=2,
        total_size=2049,
    )
    bridge = FakeBridge([first, first])

    with pytest.raises(DumpMemoryReadError) as raised:
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 2049)],
            timeout=0.1,
            poll_interval=0,
        )

    assert raised.value.gap_fact == "invalid_block_count"


def test_multi_region_b1_rejects_block_count_change():
    bridge = FakeBridge([
        _b1_regions_frame(
            1,
            [(0, b"A" * 2048)],
            block_index=0,
            block_count=2,
            total_size=2049,
        ),
        _b1_regions_frame(
            2,
            [(0, b"B")],
            block_index=1,
            block_count=3,
            total_size=2049,
        ),
    ])

    with pytest.raises(DumpMemoryReadError) as raised:
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 2049)],
            timeout=0.1,
            poll_interval=0,
        )

    assert raised.value.gap_fact == "invalid_block_count"


def test_multi_region_outer_crc_error_is_counted_and_never_published():
    corrupt = bytearray(_b1_regions_frame(
        1,
        [(0, b"A" * 2048)],
        block_index=0,
        block_count=2,
        total_size=2049,
    ))
    corrupt[-1] ^= 0xFF
    bridge = FakeBridge([bytes(corrupt)])

    with pytest.raises(DumpMemoryReadError) as raised:
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 2049)],
            timeout=0.01,
            poll_interval=0,
        )

    assert raised.value.gap_fact == "crc_error_count"
    assert raised.value.gap_count == 1


def test_multi_region_outer_crc_error_poisoning_prevents_later_good_frame():
    corrupt = bytearray(_old_regions_frame(1, [(0, b"bad!")]))
    corrupt[-1] ^= 0xFF
    good = _old_regions_frame(2, [(0, b"good")])
    bridge = FakeBridge([bytes(corrupt) + good])

    with pytest.raises(DumpMemoryReadError) as raised:
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 4)],
            timeout=0.1,
            poll_interval=0,
        )

    assert raised.value.gap_fact == "crc_error_count"
    assert raised.value.gap_count == 1


def test_multi_region_timeout_without_frames_reports_missing_block_gap():
    bridge = FakeBridge([])

    with pytest.raises(DumpMemoryReadError) as raised:
        read_dump_memory_regions_once(
            bridge,
            [(0x20000000, 4)],
            timeout=0.01,
            poll_interval=0,
        )

    assert raised.value.gap_fact == "missing_block_count"
    assert raised.value.gap_count == 1
