import json
from dataclasses import FrozenInstanceError

import pytest

from mklink.remote.stream_protocol import (
    MAX_MEMORY_JSON_BYTES,
    Frame,
    StreamType,
    decode_frame,
    decode_memory_record,
    encode_frame,
    encode_memory_record,
)


GOLDEN = bytes.fromhex(
    "4d4b535401020024070000000900000000000000e803000000000000"
    "02000000080000000000803f000000c0"
)
MAX_PAYLOAD_SIZE = 4 * 1024 * 1024


def test_waveform_frame_matches_v1_golden_vector():
    frame = Frame(
        StreamType.WAVEFORM,
        0,
        7,
        9,
        1000,
        2,
        bytes.fromhex("0000803f000000c0"),
    )

    assert encode_frame(frame) == GOLDEN
    assert decode_frame(GOLDEN) == frame


def test_frame_is_immutable():
    frame = decode_frame(GOLDEN)

    with pytest.raises(FrozenInstanceError):
        frame.sequence = 10


def test_frame_repr_does_not_expose_payload_bytes():
    frame = Frame(StreamType.MEMORY, 1, 6, 1, 1, 1, b"private-memory-bytes")

    assert "private-memory-bytes" not in repr(frame)


def test_serial_frame_roundtrip_preserves_exact_bytes_and_direction():
    frame = Frame(
        StreamType.SERIAL,
        0x01,
        int(StreamType.SERIAL),
        3,
        123,
        4,
        b"\x00\x7f\x80\xff",
    )

    assert decode_frame(encode_frame(frame)) == frame


@pytest.mark.parametrize(
    ("offset", "replacement", "match"),
    [
        (0, ord("X"), "magic"),
        (4, 2, "version"),
        (5, 99, "stream type"),
        (7, 35, "header size"),
    ],
)
def test_decode_rejects_invalid_header_fields(offset, replacement, match):
    encoded = bytearray(GOLDEN)
    encoded[offset] = replacement

    with pytest.raises(ValueError, match=match):
        decode_frame(bytes(encoded))


@pytest.mark.parametrize("declared_length", [7, 9])
def test_decode_rejects_payload_length_mismatch(declared_length):
    encoded = bytearray(GOLDEN)
    encoded[32:36] = declared_length.to_bytes(4, "little")

    with pytest.raises(ValueError, match="payload length"):
        decode_frame(bytes(encoded))


def test_decode_rejects_payload_larger_than_four_mib_before_reading_it():
    encoded = bytearray(GOLDEN[:36])
    encoded[32:36] = (MAX_PAYLOAD_SIZE + 1).to_bytes(4, "little")

    with pytest.raises(ValueError, match="payload.*4 MiB"):
        decode_frame(bytes(encoded))


def test_encode_rejects_payload_larger_than_four_mib():
    frame = Frame(
        StreamType.RTT_RAW,
        0,
        1,
        1,
        1,
        1,
        b"x" * (MAX_PAYLOAD_SIZE + 1),
    )

    with pytest.raises(ValueError, match="payload.*4 MiB"):
        encode_frame(frame)


def _memory_document(**changes):
    payload = encode_memory_record(
        "op-0123456789abcdef",
        "dump",
        0x20000010,
        0x10,
        0x20,
        b"\xAB\xCD",
        sample_index=7,
        sample_count=8,
        region_index=1,
        region_count=2,
    )
    document = json.loads(payload)
    document.update(changes)
    return document


def test_memory_json_v1_roundtrip_is_closed_canonical_and_bounded():
    payload = encode_memory_record(
        "op-0123456789abcdef",
        "read",
        0x20000000,
        0,
        256,
        bytes(range(256)),
        sample_index=0,
        sample_count=1,
        region_index=0,
        region_count=1,
    )

    assert int(StreamType.MEMORY) == 6
    assert len(payload) <= MAX_MEMORY_JSON_BYTES
    assert decode_memory_record(payload) == {
        "schema_version": 1,
        "operation_id": "op-0123456789abcdef",
        "operation": "read",
        "address": "0x20000000",
        "offset": 0,
        "total_bytes": 256,
        "byte_count": 256,
        "data_hex": bytes(range(256)).hex().upper(),
        "crc32": "29058C73",
        "sample_index": 0,
        "sample_count": 1,
        "region_index": 0,
        "region_count": 1,
    }


def test_memory_decoder_rejects_unknown_and_duplicate_keys():
    unknown = _memory_document(unknown=1)
    with pytest.raises(ValueError, match="closed set"):
        decode_memory_record(json.dumps(unknown).encode())

    valid = json.dumps(_memory_document(), separators=(",", ":"))
    duplicate = valid[:-1] + ',"operation":"dump"}'
    with pytest.raises(ValueError, match="duplicate"):
        decode_memory_record(duplicate.encode())


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": "op-0123456789ABCDEf"},
        {"operation_id": "op-short"},
        {"address": "0x2000abcd"},
        {"address": "0x0000000020000010"},
        {"data_hex": "abcd"},
        {"crc32": "e9ffc9d0"},
        {"crc32": "00000000"},
        {"byte_count": 3},
        {"offset": 31, "byte_count": 2},
        {"region_index": 2, "region_count": 2},
        {"region_count": 9},
        {"sample_count": 0},
        {"sample_count": 65},
        {"sample_index": 8, "sample_count": 8},
        {"address": "0x00000000", "offset": 1},
    ],
)
def test_memory_decoder_rejects_noncanonical_or_inconsistent_records(changes):
    with pytest.raises(ValueError):
        decode_memory_record(json.dumps(_memory_document(**changes)).encode())


@pytest.mark.parametrize(
    "changes",
    [
        {"sample_index": 1, "sample_count": 2},
        {"sample_count": 2},
        {"region_index": 1, "region_count": 2},
        {"region_count": 2},
    ],
)
def test_memory_decoder_rejects_read_with_dump_dimensions(changes):
    document = _memory_document()
    document.update({
        "operation": "read",
        "sample_index": 0,
        "sample_count": 1,
        "region_index": 0,
        "region_count": 1,
    })
    document.update(changes)

    with pytest.raises(ValueError, match="memory read must use"):
        decode_memory_record(json.dumps(document).encode())


@pytest.mark.parametrize("payload", [b"", b"{" + b" " * MAX_MEMORY_JSON_BYTES + b"}"])
def test_memory_decoder_enforces_dedicated_json_wire_limit(payload):
    with pytest.raises(ValueError, match="wire limit"):
        decode_memory_record(payload)


def test_memory_encoder_rejects_chunk_range_region_and_operation_id_errors():
    common = {
        "operation": "dump",
        "address": 0x20000000,
        "offset": 0,
        "total_bytes": 1,
        "data": b"\x00",
        "sample_index": 0,
        "sample_count": 1,
        "region_index": 0,
        "region_count": 1,
    }
    with pytest.raises(ValueError, match="operation_id"):
        encode_memory_record("op-ABCDEF0123456789", **common)
    with pytest.raises(ValueError, match="1..256"):
        encode_memory_record(
            "op-0123456789abcdef",
            **{**common, "total_bytes": 257, "data": b"x" * 257},
        )
    with pytest.raises(ValueError, match="region_count"):
        encode_memory_record(
            "op-0123456789abcdef",
            **{**common, "region_count": 9},
        )
    with pytest.raises(ValueError, match="sample index/count"):
        encode_memory_record(
            "op-0123456789abcdef",
            **{**common, "sample_index": 1},
        )
    with pytest.raises(ValueError, match="valid region"):
        encode_memory_record(
            "op-0123456789abcdef",
            **{**common, "address": 0, "offset": 1, "total_bytes": 2},
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"sample_index": 1, "sample_count": 2},
        {"sample_count": 2},
        {"region_index": 1, "region_count": 2},
        {"region_count": 2},
    ],
)
def test_memory_encoder_rejects_read_with_dump_dimensions(changes):
    common = {
        "operation": "read",
        "address": 0x20000000,
        "offset": 0,
        "total_bytes": 1,
        "data": b"\x00",
        "sample_index": 0,
        "sample_count": 1,
        "region_index": 0,
        "region_count": 1,
    }

    with pytest.raises(ValueError, match="memory read must use"):
        encode_memory_record(
            "op-0123456789abcdef",
            **{**common, **changes},
        )
