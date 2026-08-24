import binascii
import struct

import pytest

from mklink._types import DeviceState
from mklink.dump_memory import (
    DumpMemoryStreamSession,
    DumpMemoryReadError,
    FLAG_SAMPLE_DROPPED,
    MAGIC,
    read_dump_memory_range_once,
    read_dump_memory_once,
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
        "write", b"cmd.dump_memory(0x20000000, 4, 0)\n",
    )
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
    assert ("write", b"RTTView.stop()\n") in bridge.calls
    assert bridge.calls[-1] == ("exit",)


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
