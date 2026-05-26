"""串口协议帧解析器 — CRC 引擎与帧边界检测。"""

from __future__ import annotations

import binascii
import struct
import time
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# CRC Engine
# ---------------------------------------------------------------------------


def crc8(data: bytes) -> int:
    result = 0
    for b in data:
        result ^= b
    return result & 0xFF


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    # Swap bytes to match Modbus wire order (low byte first)
    crc &= 0xFFFF
    return ((crc & 0xFF) << 8) | ((crc >> 8) & 0xFF)


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def crc32(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


def checksum8(data: bytes) -> int:
    return sum(data) & 0xFF


def checksum16(data: bytes) -> int:
    return sum(data) & 0xFFFF


_CRC_DISPATCH: dict[str, Any] = {
    "crc8": crc8,
    "crc16_modbus": crc16_modbus,
    "crc16_ccitt": crc16_ccitt,
    "crc32": crc32,
    "checksum8": checksum8,
    "checksum16": checksum16,
}


def compute_crc(algorithm: str, data: bytes) -> int:
    """Dispatch to the correct CRC function by name. Raises ValueError for unknown algorithm."""
    func = _CRC_DISPATCH.get(algorithm)
    if func is None:
        raise ValueError(f"Unknown CRC algorithm: {algorithm!r}")
    return func(data)


# ---------------------------------------------------------------------------
# Frame Parser
# ---------------------------------------------------------------------------

_STRUCT_FORMATS: dict[str, str] = {
    "uint8": "B",
    "int8": "b",
    "uint16": "H",
    "int16": "h",
    "uint32": "I",
    "int32": "i",
    "float32": "f",
}

_TIMEOUT_MS = 50


@dataclass
class ParsedFrame:
    raw: bytes
    fields: dict[str, Any] = field(default_factory=dict)
    crc_valid: bool | None = None
    timestamp: float = 0.0


class FrameParser:
    def __init__(self, profile: dict | None = None):
        """Initialize with optional profile dict (the 'frame' and 'fields' sections).
        If profile is None, operates in line-based mode (split on \\n or timeout).
        """
        self._profile = profile
        self._buffer = bytearray()
        self._last_feed_time: float = 0.0

        if profile and "frame" in profile:
            frame_cfg = profile["frame"]
            self._header = bytes.fromhex(frame_cfg["header"]) if "header" in frame_cfg else None
            self._tail = bytes.fromhex(frame_cfg["tail"]) if "tail" in frame_cfg else None
            self._length_field = frame_cfg.get("length_field")
            self._crc_cfg = frame_cfg.get("crc")
        else:
            self._header = None
            self._tail = None
            self._length_field = None
            self._crc_cfg = None

        self._fields_cfg: list[dict] = profile.get("fields", []) if profile else []
        self._endian = "<"
        if profile and profile.get("frame", {}).get("endian") == "big":
            self._endian = ">"

    def feed(self, data: bytes) -> list[ParsedFrame]:
        """Feed raw bytes, return list of complete parsed frames (may be empty)."""
        now = time.time()
        frames: list[ParsedFrame] = []

        if self._profile is None:
            frames = self._feed_line_mode(data, now)
        else:
            self._buffer.extend(data)
            frames = self._feed_protocol_mode(now)

        self._last_feed_time = now
        return frames

    def reset(self) -> None:
        """Clear internal buffer."""
        self._buffer.clear()
        self._last_feed_time = 0.0

    # ------------------------------------------------------------------
    # Line-based mode
    # ------------------------------------------------------------------

    def _feed_line_mode(self, data: bytes, now: float) -> list[ParsedFrame]:
        frames: list[ParsedFrame] = []

        # Timeout flush: if gap > 50ms and buffer has content, emit it
        if (
            self._buffer
            and self._last_feed_time > 0
            and (now - self._last_feed_time) * 1000 >= _TIMEOUT_MS
        ):
            frames.append(ParsedFrame(
                raw=bytes(self._buffer),
                fields={},
                crc_valid=None,
                timestamp=now,
            ))
            self._buffer.clear()

        self._buffer.extend(data)

        while True:
            # Find newline
            idx_n = self._buffer.find(b"\n")
            if idx_n == -1:
                break

            line = bytes(self._buffer[:idx_n])
            self._buffer = self._buffer[idx_n + 1:]

            # Strip trailing \r
            if line.endswith(b"\r"):
                line = line[:-1]

            frames.append(ParsedFrame(
                raw=line,
                fields={},
                crc_valid=None,
                timestamp=now,
            ))

        return frames

    # ------------------------------------------------------------------
    # Protocol mode
    # ------------------------------------------------------------------

    def _feed_protocol_mode(self, now: float) -> list[ParsedFrame]:
        frames: list[ParsedFrame] = []

        while True:
            frame_bytes = self._try_extract_frame(now)
            if frame_bytes is None:
                break
            frames.append(self._parse_frame(frame_bytes, now))

        return frames

    def _try_extract_frame(self, now: float) -> bytes | None:
        if not self._header:
            return None

        # Scan for header
        header_idx = self._buffer.find(self._header)
        if header_idx == -1:
            # Discard bytes before any potential partial header
            if len(self._buffer) > len(self._header):
                self._buffer = self._buffer[-(len(self._header) - 1):]
            return None

        # Discard garbage before header
        if header_idx > 0:
            self._buffer = self._buffer[header_idx:]

        header_len = len(self._header)

        if self._length_field:
            return self._extract_by_length(header_len)
        elif self._tail:
            return self._extract_by_tail(header_len)
        else:
            return self._extract_by_timeout(now)

    def _extract_by_length(self, header_len: int) -> bytes | None:
        lf = self._length_field
        offset = lf["offset"]
        size = lf["size"]
        includes_header = lf.get("includes_header", False)

        if len(self._buffer) < offset + size:
            return None

        if size == 1:
            frame_len = self._buffer[offset]
        else:
            fmt = f"{self._endian}H"
            frame_len = struct.unpack_from(fmt, self._buffer, offset)[0]

        if includes_header:
            total_len = frame_len
        else:
            total_len = frame_len + header_len

        # Account for CRC bytes at end if configured
        if self._crc_cfg:
            algo = self._crc_cfg["algorithm"]
            if algo in ("crc8", "checksum8"):
                total_len += 1
            elif algo in ("crc16_modbus", "crc16_ccitt", "checksum16"):
                total_len += 2
            elif algo == "crc32":
                total_len += 4

        if len(self._buffer) < total_len:
            return None

        frame = bytes(self._buffer[:total_len])
        self._buffer = self._buffer[total_len:]
        return frame

    def _extract_by_tail(self, header_len: int) -> bytes | None:
        tail_idx = self._buffer.find(self._tail, header_len)
        if tail_idx == -1:
            return None

        end = tail_idx + len(self._tail)
        frame = bytes(self._buffer[:end])
        self._buffer = self._buffer[end:]
        return frame

    def _extract_by_timeout(self, now: float) -> bytes | None:
        if (
            self._last_feed_time > 0
            and (now - self._last_feed_time) * 1000 >= _TIMEOUT_MS
            and len(self._buffer) > 0
        ):
            frame = bytes(self._buffer)
            self._buffer.clear()
            return frame
        return None

    # ------------------------------------------------------------------
    # Frame parsing & field decoding
    # ------------------------------------------------------------------

    def _parse_frame(self, frame_bytes: bytes, now: float) -> ParsedFrame:
        crc_valid = self._validate_crc(frame_bytes)
        fields = self._decode_fields(frame_bytes)

        return ParsedFrame(
            raw=frame_bytes,
            fields=fields,
            crc_valid=crc_valid,
            timestamp=now,
        )

    def _validate_crc(self, frame_bytes: bytes) -> bool | None:
        if not self._crc_cfg:
            return None

        algo = self._crc_cfg["algorithm"]
        offset = self._crc_cfg["offset"]
        scope = self._crc_cfg.get("scope", "all")

        # Determine CRC size
        if algo in ("crc8", "checksum8"):
            crc_size = 1
        elif algo in ("crc16_modbus", "crc16_ccitt", "checksum16"):
            crc_size = 2
        elif algo == "crc32":
            crc_size = 4
        else:
            return None

        # Extract stored CRC value
        if offset < 0:
            crc_pos = len(frame_bytes) + offset
        else:
            crc_pos = offset

        if crc_pos < 0 or crc_pos + crc_size > len(frame_bytes):
            return False

        if crc_size == 1:
            stored_crc = frame_bytes[crc_pos]
        elif crc_size == 2:
            # Modbus CRC is always stored low-byte-first; read as big-endian
            # to match crc16_modbus() return convention
            if algo == "crc16_modbus":
                stored_crc = struct.unpack_from(">H", frame_bytes, crc_pos)[0]
            else:
                stored_crc = struct.unpack_from(f"{self._endian}H", frame_bytes, crc_pos)[0]
        else:
            stored_crc = struct.unpack_from(f"{self._endian}I", frame_bytes, crc_pos)[0]

        # Determine data scope for CRC calculation
        if scope == "payload":
            header_len = len(self._header) if self._header else 0
            crc_data = frame_bytes[header_len:crc_pos]
        else:
            crc_data = frame_bytes[:crc_pos]

        computed = compute_crc(algo, crc_data)
        return computed == stored_crc

    def _decode_fields(self, frame_bytes: bytes) -> dict[str, Any]:
        if not self._fields_cfg:
            return {}

        fields: dict[str, Any] = {}

        for field_cfg in self._fields_cfg:
            name = field_cfg["name"]
            offset = field_cfg["offset"]
            size = field_cfg["size"]
            type_name = field_cfg["type"]
            scale = field_cfg.get("scale")
            unit = field_cfg.get("unit", "")
            enum_map = field_cfg.get("enum")

            # Per-field endian override
            endian = self._endian
            if "endian" in field_cfg:
                endian = ">" if field_cfg["endian"] == "big" else "<"

            fmt_char = _STRUCT_FORMATS.get(type_name)
            if fmt_char is None or offset + size > len(frame_bytes):
                continue

            fmt = f"{endian}{fmt_char}"
            raw_value = struct.unpack_from(fmt, frame_bytes, offset)[0]

            if enum_map:
                hex_key = f"0x{raw_value:02X}" if raw_value < 256 else f"0x{raw_value:04X}"
                value = enum_map.get(hex_key, enum_map.get(str(raw_value), raw_value))
            elif scale:
                value = round(raw_value * scale, 6)
            else:
                value = raw_value

            entry: dict[str, Any] = {"raw": raw_value, "value": value}
            if unit:
                entry["unit"] = unit

            fields[name] = entry

        return fields
