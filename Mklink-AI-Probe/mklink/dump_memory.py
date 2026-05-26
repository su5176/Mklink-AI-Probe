"""Dump Memory binary protocol parser for MKLink SuperWatch.

Implements the streaming binary frame parser defined in the Dump Memory
protocol specification. Parses frames containing up to 16 memory regions
with MAGIC sync, CRC32 validation, and automatic resync on corruption.

Zero internal mklink dependencies — only uses struct/binascii from stdlib.
"""

from __future__ import annotations

import binascii
import struct
from typing import Optional

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
MAGIC = b'\x4D\x50\x4D\x44\x4D\x50\x4D\x44'   # "DMPMDMPM"
MAGIC_LEN = 8
HEADER_LEN = 19        # MAGIC(8) + TIMESTAMP_US(8) + FRAME_LENGTH(2) + REGION_COUNT(1)
MIN_FRAME_LEN = 28     # HEADER + 1 region(3+0) + FLAGS(2) + CRC32(4)
MAX_FRAME_LEN = 65535
MAX_REGIONS = 16

# FLAGS bit masks
FLAG_TICK_OVERFLOW    = 0x0001
FLAG_TIMING_VIOLATION = 0x0002
FLAG_REGION_ERROR     = 0x0004
FLAG_SAMPLE_DROPPED   = 0x0008


# Sentinel return values for _try_parse
_NEED_MORE = object()
_RETRY = object()


class DumpMemoryParser:
    """Streaming binary parser for dump_memory frames.

    Follows the same feed() -> list[dict] pattern as JustFloatParser
    and JScopeBinaryParser.
    """

    def __init__(self, region_sizes: list[int] | None = None):
        self._buf = bytearray()
        self._region_sizes = region_sizes or []
        self._expected_count = len(self._region_sizes)
        self._dropped_bytes: int = 0
        self._dropped_frames: int = 0
        self._crc_errors: int = 0

    @property
    def dropped_bytes(self) -> int:
        return self._dropped_bytes

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    @property
    def crc_errors(self) -> int:
        return self._crc_errors

    def feed(self, data: bytes) -> list[dict]:
        """Feed raw bytes, return list of parsed frame dicts.

        Returns: list of {
            "timestamp_us": int,
            "regions": list[tuple[int, bytes]],   # [(index, data), ...]
            "flags": int,
        }
        """
        self._buf.extend(data)
        frames: list[dict] = []
        # Loop until no more complete frames can be extracted.
        # _NEED_MORE is returned when the buffer doesn't have enough data;
        # _RETRY means data was discarded but there may be more to parse.
        while True:
            result = self._try_parse()
            if result is _NEED_MORE:
                break
            if result is not _RETRY:
                frames.append(result)
        return frames

    def _try_parse(self):
        # Step 1: Find MAGIC
        idx = self._buf.find(MAGIC)
        if idx < 0:
            # Keep last MAGIC_LEN-1 bytes (partial MAGIC at boundary)
            drop = len(self._buf) - MAGIC_LEN + 1
            if drop > 0:
                self._dropped_bytes += drop
                del self._buf[:drop]
            return _NEED_MORE

        if idx > 0:
            self._dropped_bytes += idx
            del self._buf[:idx]

        # Step 2: Need at least HEADER_LEN bytes
        if len(self._buf) < HEADER_LEN:
            return _NEED_MORE

        frame_length = struct.unpack_from('<H', self._buf, 16)[0]
        if frame_length < MIN_FRAME_LEN or frame_length > MAX_FRAME_LEN:
            self._dropped_bytes += MAGIC_LEN
            self._dropped_frames += 1
            del self._buf[:MAGIC_LEN]
            return _RETRY

        # Step 3: Collect full frame
        if len(self._buf) < frame_length:
            return _NEED_MORE

        frame_bytes = bytes(self._buf[:frame_length])
        del self._buf[:frame_length]

        # Step 4: CRC32 check
        payload = frame_bytes[:-4]
        expected_crc = struct.unpack('<I', frame_bytes[-4:])[0]
        actual_crc = binascii.crc32(payload) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            self._crc_errors += 1
            self._dropped_frames += 1
            return _RETRY

        # Step 5: Parse fields
        timestamp_us = struct.unpack_from('<Q', frame_bytes, 8)[0]
        region_count = frame_bytes[18]
        flags = struct.unpack_from('<H', frame_bytes, frame_length - 6)[0]

        regions: list[tuple[int, bytes]] = []
        offset = 19
        for _ in range(region_count):
            if offset + 3 > frame_length - 6:
                break
            region_index = frame_bytes[offset]
            region_size = struct.unpack_from('<H', frame_bytes, offset + 1)[0]
            offset += 3
            if region_size > 0 and offset + region_size <= frame_length - 6:
                regions.append((region_index, frame_bytes[offset:offset + region_size]))
                offset += region_size

        return {
            "timestamp_us": timestamp_us,
            "regions": regions,
            "flags": flags,
        }


MAX_TOTAL_DATA_SIZE = 2048


def build_dump_mem_command(
    region_pairs: list[tuple[int, int]],
    period: float,
) -> str:
    """Build the cmd.dump_memory() command string.

    Args:
        region_pairs: list of (address, size) tuples.
        period: sampling period in seconds (float). 0 = single read / stop streaming.

    Returns:
        Command string like "cmd.dump_memory(0x20000054, 4, 0x2000006C, 2, 0.01)"

    Raises:
        ValueError: if total region size exceeds 2048 bytes.
    """
    total_size = sum(size for _, size in region_pairs)
    if total_size > MAX_TOTAL_DATA_SIZE:
        raise ValueError(
            f"Total region size {total_size} exceeds maximum {MAX_TOTAL_DATA_SIZE} bytes"
        )
    parts = []
    for addr, size in region_pairs:
        parts.append(f"0x{addr:08X}")
        parts.append(str(size))
    if period == 0:
        parts.append("0")
    else:
        s = f"{period:.6f}".rstrip('0').rstrip('.')
        if '.' not in s:
            s += ".0"
        parts.append(s)
    return f"cmd.dump_memory({', '.join(parts)})"


def decode_frame_to_points(
    frame: dict,
    block_addresses: list[tuple[int, int, list[tuple[str, str, int, dict | None]]]],
    origin_us: int | None,
) -> tuple[list[dict], int | None]:
    """Decode a parsed frame's region data into per-variable point dicts.

    This bridges binary region data back to the variable name/type system
    used by the SuperWatch visualizer.

    Args:
        frame: Parsed frame from DumpMemoryParser.feed().
        block_addresses: Per-region info list: [(block_addr, block_size, [(name, type_name, item_offset, enum_values), ...])]
        origin_us: Baseline timestamp in microseconds (from first sample).

    Returns:
        (points, origin_us) where points is a list of dicts suitable for
        server.push_data_point().
    """
    from mklink.watch import decode_value

    current_origin = origin_us
    if current_origin is None:
        current_origin = frame["timestamp_us"]

    ts = frame["timestamp_us"]
    relative_t = (ts - current_origin) / 1_000_000.0

    points: list[dict] = []
    for region_index, region_data in frame["regions"]:
        if region_index >= len(block_addresses):
            continue
        block_addr, _block_size, items = block_addresses[region_index]
        point: dict = {"_t": relative_t, "timestamp_us": ts}
        for name, type_name, item_offset, enum_values in items:
            data = region_data[item_offset:item_offset + _item_size(type_name)]
            if data:
                # Store raw numeric value for charting; enum display is handled by frontend
                point[name] = decode_value(data, type_name)
        points.append(point)

    return points, current_origin


def _item_size(type_name: str) -> int:
    """Return byte size for a C type name."""
    _SIZES = {
        "uint8_t": 1, "int8_t": 1, "bool": 1,
        "uint16_t": 2, "int16_t": 2,
        "uint32_t": 4, "int32_t": 4, "float": 4,
        "uint64_t": 8, "int64_t": 8, "double": 8,
    }
    return _SIZES.get(type_name, 4)
