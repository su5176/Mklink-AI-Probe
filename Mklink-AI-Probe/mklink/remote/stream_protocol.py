"""Versioned binary frames for high-throughput remote streams."""

from __future__ import annotations

import binascii
import json
import math
import re
import struct
from dataclasses import dataclass, field
from enum import IntEnum


MAGIC = b"MKST"
VERSION = 1
HEADER = struct.Struct("<4sBBBBIQQII")
HEADER_SIZE = HEADER.size
MAX_PAYLOAD_SIZE = 4 * 1024 * 1024

# WAVEFORM payload is little-endian Float32 in sample-major order.
WAVEFORM_SAMPLE_MAJOR_FLOAT32 = 0x01
RTT_RAW_UTF8_LINES = 0x01
RTT_TERMINAL_UTF8 = 0x02
SERIAL_RX_BYTES = 0x01
SERIAL_TX_BYTES = 0x02
SUPERWATCH_SAMPLE_MAJOR_FLOAT32 = 0x01
SUPERWATCH_METADATA_JSON = 0x02
# Float64 sample times (milliseconds), then sample-major Float32 values.
SUPERWATCH_TIMESTAMPED_FLOAT32 = 0x03
MEMORY_JSON_V1 = 0x01
MAX_MEMORY_CHUNK_BYTES = 256
MAX_MEMORY_JSON_BYTES = 2048
MAX_MEMORY_REGIONS = 8
MAX_MEMORY_SAMPLES = 64
_MAX_U64 = 0xFFFFFFFFFFFFFFFF
_MEMORY_OPERATION_ID = re.compile(r"^op-[0-9a-f]{16}$")

_MEMORY_JSON_KEYS = frozenset({
    "schema_version",
    "operation_id",
    "operation",
    "address",
    "offset",
    "total_bytes",
    "byte_count",
    "data_hex",
    "crc32",
    "sample_index",
    "sample_count",
    "region_index",
    "region_count",
})

RTT_LINE_RECORD = struct.Struct("<QBI")
RTT_LEVELS = {"raw": 0, "data": 1, "warning": 2, "error": 3}
RTT_LEVEL_NAMES = {value: name for name, value in RTT_LEVELS.items()}

# SystemView v1 events are fixed-size so producers and browser Workers can
# process batches without JSON allocation or another serialization package.
SYSTEMVIEW_EVENT_RECORD = struct.Struct("<BBHIQdddd")
SYSTEMVIEW_EVENT_RECORD_SIZE = SYSTEMVIEW_EVENT_RECORD.size
SYSTEMVIEW_HAS_TICKS = 0x01
SYSTEMVIEW_HAS_TIME_US = 0x02
SYSTEMVIEW_HAS_DELTA_US = 0x04

SYSTEMVIEW_EVENT_KINDS = {
    "overflow": 1,
    "isr_enter": 2,
    "isr_exit": 3,
    "task_start_exec": 4,
    "task_stop_exec": 5,
    "task_start_ready": 6,
    "task_stop_ready": 7,
    "task_create": 8,
    "task_info": 9,
    "trace_start": 10,
    "trace_stop": 11,
    "systime_cycles": 12,
    "systime_us": 13,
    "sysdesc": 14,
    "user_start": 15,
    "user_stop": 16,
    "idle": 17,
    "isr_to_scheduler": 18,
    "timer_enter": 19,
    "timer_exit": 20,
    "stack_info": 21,
    "moduledesc": 22,
    "raw": 23,
    "init": 24,
    "name_resource": 25,
    "print_formatted": 26,
    "nummodules": 27,
    "end_call": 28,
    "task_terminate": 29,
}
SYSTEMVIEW_EVENT_NAMES = {value: key for key, value in SYSTEMVIEW_EVENT_KINDS.items()}


class StreamType(IntEnum):
    SYSTEMVIEW = 1
    WAVEFORM = 2
    RTT_RAW = 3
    SUPERWATCH = 4
    SERIAL = 5
    MEMORY = 6
    CONTROL = 255


@dataclass(frozen=True)
class Frame:
    stream_type: StreamType
    flags: int
    stream_id: int
    sequence: int
    timestamp_ns: int
    item_count: int
    payload: bytes = field(repr=False)


@dataclass(frozen=True)
class RttLine:
    timestamp_ns: int
    level: str
    text: str


def _unsigned_integer(value, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"memory {name} must be an unsigned integer")
    return value


def canonical_memory_address(address: int) -> str:
    """Format one unsigned target address as canonical 32- or 64-bit hex."""
    value = _unsigned_integer(address, "address", _MAX_U64)
    width = 8 if value <= 0xFFFFFFFF else 16
    return f"0x{value:0{width}X}"


def _validate_memory_dimensions(
    operation: str,
    *,
    sample_index: int,
    sample_count: int,
    region_index: int,
    region_count: int,
) -> None:
    if region_count <= 0 or region_index >= region_count:
        raise ValueError("memory region index/count are inconsistent")
    if sample_count <= 0 or sample_index >= sample_count:
        raise ValueError("memory sample index/count are inconsistent")
    if operation == "read" and (
        sample_index != 0
        or sample_count != 1
        or region_index != 0
        or region_count != 1
    ):
        raise ValueError(
            "memory read must use sample_index=0, sample_count=1, "
            "region_index=0, and region_count=1"
        )


def encode_memory_record(
    operation_id: str,
    operation: str,
    address: int,
    offset: int,
    total_bytes: int,
    data: bytes,
    *,
    sample_index: int,
    sample_count: int,
    region_index: int,
    region_count: int,
) -> bytes:
    """Encode one strict ``MEMORY_JSON_V1`` chunk.

    ``offset`` and ``total_bytes`` are local to the current region.  ``address``
    is the actual start address of this chunk (region base + offset).
    """
    if not isinstance(operation_id, str) or not _MEMORY_OPERATION_ID.fullmatch(
        operation_id,
    ):
        raise ValueError("memory operation_id must be op- plus 16 lowercase hex digits")
    if operation not in {"read", "dump"}:
        raise ValueError("memory operation must be 'read' or 'dump'")
    address_text = canonical_memory_address(address)
    offset = _unsigned_integer(offset, "offset", _MAX_U64)
    total_bytes = _unsigned_integer(
        total_bytes, "total_bytes", _MAX_U64,
    )
    sample_index = _unsigned_integer(
        sample_index, "sample_index", _MAX_U64,
    )
    sample_count = _unsigned_integer(
        sample_count, "sample_count", MAX_MEMORY_SAMPLES,
    )
    region_index = _unsigned_integer(region_index, "region_index", 0xFFFFFFFF)
    region_count = _unsigned_integer(region_count, "region_count", MAX_MEMORY_REGIONS)
    chunk = bytes(data)
    if not 1 <= len(chunk) <= MAX_MEMORY_CHUNK_BYTES:
        raise ValueError(
            f"memory chunk must contain 1..{MAX_MEMORY_CHUNK_BYTES} bytes"
        )
    if total_bytes <= 0 or offset > total_bytes or len(chunk) > total_bytes - offset:
        raise ValueError("memory chunk is outside its declared region range")
    if address < offset or address - offset + total_bytes > _MAX_U64 + 1:
        raise ValueError("memory address/range cannot describe a valid region")
    _validate_memory_dimensions(
        operation,
        sample_index=sample_index,
        sample_count=sample_count,
        region_index=region_index,
        region_count=region_count,
    )

    document = {
        "schema_version": 1,
        "operation_id": operation_id,
        "operation": operation,
        "address": address_text,
        "offset": offset,
        "total_bytes": total_bytes,
        "byte_count": len(chunk),
        "data_hex": chunk.hex().upper(),
        "crc32": f"{binascii.crc32(chunk) & 0xFFFFFFFF:08X}",
        "sample_index": sample_index,
        "sample_count": sample_count,
        "region_index": region_index,
        "region_count": region_count,
    }
    payload = json.dumps(
        document, ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_MEMORY_JSON_BYTES:  # defensive shared wire ceiling
        raise ValueError("memory JSON exceeds the v1 wire limit")
    return payload


def decode_memory_record(payload: bytes) -> dict:
    """Strict reference decoder for tests and non-browser consumers."""
    def reject_duplicate_keys(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise ValueError(f"duplicate memory JSON key: {key}")
            document[key] = value
        return document

    wire_payload = bytes(payload)
    if not 1 <= len(wire_payload) <= MAX_MEMORY_JSON_BYTES:
        raise ValueError("memory JSON payload is outside the v1 wire limit")
    try:
        document = json.loads(
            wire_payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid memory JSON") from exc
    if not isinstance(document, dict) or set(document) != _MEMORY_JSON_KEYS:
        raise ValueError("memory JSON keys do not match the v1 closed set")
    operation_id = document.get("operation_id")
    if not isinstance(operation_id, str) or not _MEMORY_OPERATION_ID.fullmatch(
        operation_id,
    ):
        raise ValueError("invalid memory operation_id")
    operation = document.get("operation")
    if type(document.get("schema_version")) is not int or document.get(
        "schema_version",
    ) != 1 or operation not in {
        "read", "dump",
    }:
        raise ValueError("invalid memory JSON version or operation")
    address = document.get("address")
    if not isinstance(address, str):
        raise ValueError("memory address must be canonical hex")
    try:
        numeric_address = int(address, 0)
    except (ValueError, TypeError):
        raise ValueError("memory address must be canonical hex") from None
    if address != canonical_memory_address(numeric_address):
        raise ValueError("memory address must be canonical hex")
    offset = _unsigned_integer(document.get("offset"), "offset", _MAX_U64)
    total_bytes = _unsigned_integer(
        document.get("total_bytes"), "total_bytes", _MAX_U64,
    )
    byte_count = _unsigned_integer(document.get("byte_count"), "byte_count", 0xFFFFFFFF)
    sample_index = _unsigned_integer(
        document.get("sample_index"), "sample_index", _MAX_U64,
    )
    sample_count = _unsigned_integer(
        document.get("sample_count"), "sample_count", MAX_MEMORY_SAMPLES,
    )
    region_index = _unsigned_integer(
        document.get("region_index"), "region_index", 0xFFFFFFFF,
    )
    region_count = _unsigned_integer(
        document.get("region_count"), "region_count", MAX_MEMORY_REGIONS,
    )
    data_hex = document.get("data_hex")
    crc32 = document.get("crc32")
    if (
        not isinstance(data_hex, str)
        or data_hex != data_hex.upper()
        or len(data_hex) != byte_count * 2
    ):
        raise ValueError("memory data_hex length/case is invalid")
    try:
        data = bytes.fromhex(data_hex)
    except ValueError:
        raise ValueError("memory data_hex is invalid") from None
    if not 1 <= byte_count <= MAX_MEMORY_CHUNK_BYTES or len(data) != byte_count:
        raise ValueError("memory byte_count is invalid")
    if total_bytes <= 0 or offset > total_bytes or byte_count > total_bytes - offset:
        raise ValueError("memory chunk is outside its declared region range")
    if numeric_address < offset or numeric_address - offset + total_bytes > _MAX_U64 + 1:
        raise ValueError("memory address/range cannot describe a valid region")
    _validate_memory_dimensions(
        operation,
        sample_index=sample_index,
        sample_count=sample_count,
        region_index=region_index,
        region_count=region_count,
    )
    expected_crc = f"{binascii.crc32(data) & 0xFFFFFFFF:08X}"
    if not isinstance(crc32, str) or crc32 != expected_crc:
        raise ValueError("memory crc32 is invalid")
    # Keep the original closed document as the wire-level reference result.
    return document


def encode_waveform_samples(samples) -> bytes:
    """Encode complete numeric rows as little-endian sample-major Float32."""
    rows = [tuple(row) for row in samples]
    if not rows:
        return b""
    channel_count = len(rows[0])
    if channel_count <= 0 or any(len(row) != channel_count for row in rows):
        raise ValueError("waveform samples must have one stable channel count")
    values = []
    for row in rows:
        for value in row:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("waveform values must be numeric")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("waveform values must be finite")
            values.append(number)
    return struct.pack(f"<{len(values)}f", *values)


def decode_waveform_samples(payload: bytes, item_count: int, channel_count: int):
    """Test/reference decoder for the common sample-major Float32 layout."""
    if item_count < 0 or channel_count <= 0:
        raise ValueError("invalid waveform dimensions")
    expected = item_count * channel_count * 4
    if len(payload) != expected:
        raise ValueError("waveform payload length does not match dimensions")
    values = struct.unpack(f"<{item_count * channel_count}f", payload)
    return tuple(
        tuple(values[row * channel_count:(row + 1) * channel_count])
        for row in range(item_count)
    )


def encode_rtt_lines(lines) -> bytes:
    """Encode UTF-8 RTT lines with timestamp and level metadata."""
    payload = bytearray()
    for line in lines:
        timestamp_ns = int(line.timestamp_ns)
        if not 0 <= timestamp_ns <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("RTT line timestamp must be an unsigned 64-bit integer")
        try:
            level = RTT_LEVELS[line.level]
        except KeyError as exc:
            raise ValueError(f"unknown RTT line level: {line.level!r}") from exc
        encoded = str(line.text).encode("utf-8")
        payload.extend(RTT_LINE_RECORD.pack(timestamp_ns, level, len(encoded)))
        payload.extend(encoded)
    return bytes(payload)


def decode_rtt_lines(payload: bytes, item_count: int) -> tuple[RttLine, ...]:
    """Decode and strictly validate one RTT_RAW v1 payload."""
    if item_count < 0:
        raise ValueError("RTT item count must not be negative")
    offset = 0
    lines = []
    for _ in range(item_count):
        if len(payload) - offset < RTT_LINE_RECORD.size:
            raise ValueError("truncated RTT line metadata")
        timestamp_ns, level_value, length = RTT_LINE_RECORD.unpack_from(payload, offset)
        offset += RTT_LINE_RECORD.size
        if level_value not in RTT_LEVEL_NAMES or length > len(payload) - offset:
            raise ValueError("invalid RTT line metadata")
        try:
            text = payload[offset:offset + length].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("RTT line payload is not valid UTF-8") from exc
        offset += length
        lines.append(RttLine(timestamp_ns, RTT_LEVEL_NAMES[level_value], text))
    if offset != len(payload):
        raise ValueError("RTT payload has trailing bytes")
    return tuple(lines)


def encode_superwatch_metadata(version: int, channels) -> bytes:
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError("SuperWatch metadata version must be positive")
    document = {"version": version, "channels": list(channels)}
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_superwatch_metadata(payload: bytes) -> dict:
    try:
        document = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid SuperWatch metadata JSON") from exc
    if (
        not isinstance(document, dict)
        or isinstance(document.get("version"), bool)
        or not isinstance(document.get("version"), int)
        or document["version"] <= 0
        or not isinstance(document.get("channels"), list)
    ):
        raise ValueError("invalid SuperWatch metadata document")
    return document


def encode_frame(frame: Frame) -> bytes:
    """Encode a frame using the v1 little-endian wire format."""
    try:
        stream_type = StreamType(frame.stream_type)
    except ValueError as exc:
        raise ValueError(f"unknown stream type: {frame.stream_type}") from exc
    payload = bytes(frame.payload)
    if len(payload) > MAX_PAYLOAD_SIZE:
        raise ValueError("payload exceeds 4 MiB limit")
    header = HEADER.pack(
        MAGIC,
        VERSION,
        stream_type,
        frame.flags,
        HEADER_SIZE,
        frame.stream_id,
        frame.sequence,
        frame.timestamp_ns,
        frame.item_count,
        len(payload),
    )
    return header + payload


def decode_frame(encoded: bytes) -> Frame:
    """Decode a v1 frame, rejecting malformed or unsupported input."""
    if len(encoded) < HEADER_SIZE:
        raise ValueError("frame is shorter than the 36-byte header size")
    (
        magic,
        version,
        stream_type_value,
        flags,
        header_size,
        stream_id,
        sequence,
        timestamp_ns,
        item_count,
        payload_length,
    ) = HEADER.unpack_from(encoded)
    if magic != MAGIC:
        raise ValueError("invalid stream frame magic")
    if version != VERSION:
        raise ValueError(f"unsupported stream frame version: {version}")
    if header_size != HEADER_SIZE:
        raise ValueError(f"invalid header size: {header_size}")
    try:
        stream_type = StreamType(stream_type_value)
    except ValueError as exc:
        raise ValueError(f"unknown stream type: {stream_type_value}") from exc
    if payload_length > MAX_PAYLOAD_SIZE:
        raise ValueError("payload exceeds 4 MiB limit")
    if len(encoded) - HEADER_SIZE != payload_length:
        raise ValueError("payload length does not match frame size")
    return Frame(
        stream_type,
        flags,
        stream_id,
        sequence,
        timestamp_ns,
        item_count,
        bytes(encoded[HEADER_SIZE:]),
    )


def _systemview_number(event: dict, *names: str) -> float:
    for name in names:
        value = event.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError(f"SystemView field {name!r} must be finite")
            return float(value)
    return 0.0


def _systemview_context_id(event: dict) -> int:
    for name in (
        "task_id", "isr_id", "resource_id", "timer_id",
        "user_id", "module_id", "event_id",
    ):
        if name not in event or event[name] is None:
            continue
        value = event[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFFFFFF
        ):
            raise ValueError(
                "SystemView context id must be an unsigned 32-bit integer"
            )
        return value
    return 0


def encode_systemview_events(events) -> bytes:
    """Encode decoded SystemView dictionaries as fixed 48-byte v1 records."""
    payload = bytearray(SYSTEMVIEW_EVENT_RECORD_SIZE * len(events))
    for index, event in enumerate(events):
        kind_name = str(event.get("kind") or "")
        kind = SYSTEMVIEW_EVENT_KINDS.get(kind_name)
        raw_event_id = None
        if kind is None and kind_name.startswith("raw_"):
            try:
                raw_event_id = int(kind_name[4:])
            except ValueError:
                raw_event_id = None
            if raw_event_id is not None and 512 <= raw_event_id <= 4096:
                kind = SYSTEMVIEW_EVENT_KINDS["raw"]
        if kind is None:
            raise ValueError(f"unknown SystemView event kind: {kind_name!r}")
        flags = 0
        ticks = event.get("t_ticks")
        if isinstance(ticks, int) and not isinstance(ticks, bool) and 0 <= ticks <= 0xFFFFFFFFFFFFFFFF:
            flags |= SYSTEMVIEW_HAS_TICKS
        elif ticks is not None:
            raise ValueError("SystemView field 't_ticks' must be an unsigned 64-bit integer")
        else:
            ticks = 0
        time_us = event.get("t_us")
        if isinstance(time_us, (int, float)) and not isinstance(time_us, bool):
            if not math.isfinite(time_us):
                raise ValueError("SystemView field 't_us' must be finite")
            flags |= SYSTEMVIEW_HAS_TIME_US
        else:
            time_us = 0.0
        delta_us = event.get("cpu_delta_us")
        if isinstance(delta_us, (int, float)) and not isinstance(delta_us, bool):
            if not math.isfinite(delta_us):
                raise ValueError("SystemView field 'cpu_delta_us' must be finite")
            flags |= SYSTEMVIEW_HAS_DELTA_US
        else:
            delta_us = 0.0
        context_id = (
            raw_event_id
            if raw_event_id is not None
            else _systemview_context_id(event)
        )
        if not 0 <= context_id <= 0xFFFFFFFF:
            raise ValueError("SystemView context id must be an unsigned 32-bit integer")
        aux0 = _systemview_number(
            event, "prio", "cause", "drop_count", "cpu_freq", "systime",
            "stack_base", "options", "num_modules",
        )
        aux1 = _systemview_number(
            event, "stack_size", "sys_freq", "ram_base", "num_args", "id_shift",
        )
        SYSTEMVIEW_EVENT_RECORD.pack_into(
            payload,
            index * SYSTEMVIEW_EVENT_RECORD_SIZE,
            kind,
            flags,
            0,
            context_id & 0xFFFFFFFF,
            ticks,
            float(time_us),
            float(delta_us),
            aux0,
            aux1,
        )
    return bytes(payload)


def decode_systemview_events(payload: bytes) -> list[dict]:
    """Decode fixed SystemView v1 records for tests and non-browser clients."""
    if len(payload) % SYSTEMVIEW_EVENT_RECORD_SIZE:
        raise ValueError("SystemView payload must be a multiple of the record size")
    events = []
    task_kinds = {4, 5, 6, 7, 8, 9, 21, 29}
    for offset in range(0, len(payload), SYSTEMVIEW_EVENT_RECORD_SIZE):
        kind, flags, reserved, context_id, ticks, time_us, delta_us, aux0, aux1 = (
            SYSTEMVIEW_EVENT_RECORD.unpack_from(payload, offset)
        )
        if not all(
            math.isfinite(value)
            for value in (time_us, delta_us, aux0, aux1)
        ):
            raise ValueError("SystemView numeric fields must be finite")
        if (flags & ~(SYSTEMVIEW_HAS_TICKS | SYSTEMVIEW_HAS_TIME_US | SYSTEMVIEW_HAS_DELTA_US)
                or reserved != 0 or kind not in SYSTEMVIEW_EVENT_NAMES):
            raise ValueError("malformed SystemView event record")
        if kind == SYSTEMVIEW_EVENT_KINDS["raw"]:
            event = {"kind": f"raw_{context_id}"}
        else:
            event = {"kind": SYSTEMVIEW_EVENT_NAMES[kind]}
        if flags & SYSTEMVIEW_HAS_TICKS:
            event["t_ticks"] = ticks
        if flags & SYSTEMVIEW_HAS_TIME_US:
            event["t_us"] = time_us
        if flags & SYSTEMVIEW_HAS_DELTA_US:
            event["cpu_delta_us"] = delta_us
        if kind == SYSTEMVIEW_EVENT_KINDS["raw"]:
            event["event_id"] = context_id
        elif kind in task_kinds:
            event["task_id"] = context_id
        elif kind == 2:
            event["isr_id"] = context_id
        elif context_id:
            event["resource_id"] = context_id
        if kind == 9:
            event["prio"] = int(aux0)
        elif kind == 21:
            event["stack_base"] = int(aux0)
            event["stack_size"] = int(aux1)
        elif kind == 7:
            event["cause"] = int(aux0)
        events.append(event)
    return events
