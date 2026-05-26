"""J-SCOPE RTT binary protocol parser.

Parses binary RTT channels whose name matches the JScope_<fmt> convention
(e.g. ``JScope_u2f4`` for uint16 + float).  Each channel transmits packed
little-endian binary frames terminated by a 4-byte tail marker
(``0x00 0x00 0x80 0x7f`` — the IEEE 754 representation of positive infinity),
identical to the JustFloat tail convention used by J-Scope / VOFA+.

Public API
----------
parse_format(fmt_str)
    Parse a JScope format string into a list of (type_char, byte_size) tuples.
decode_binary_frame(data, format_desc)
    Decode one binary frame according to a format descriptor.
is_jscope_channel(channel_name)
    Return True if *channel_name* matches ``JScope_<fmt>``.
JScopeBinaryParser
    Streaming binary frame parser with frame-resync on corruption.
"""

from __future__ import annotations

import re
import struct
import warnings
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Format map: type_code -> (struct_format, byte_size, numpy-style name)
# ---------------------------------------------------------------------------
FORMAT_MAP: dict[str, tuple[str, int, str]] = {
    "u1": ("<B", 1, "uint8"),
    "u2": ("<H", 2, "uint16"),
    "u4": ("<I", 4, "uint32"),
    "i1": ("<b", 1, "int8"),
    "i2": ("<h", 2, "int16"),
    "i4": ("<i", 4, "int32"),
    "f4": ("<f", 4, "float"),
}

# Valid single-char type prefixes
_VALID_TYPE_CHARS = {"u", "i", "f"}

# Tail marker: 0x00 0x00 0x80 0x7f  (IEEE 754 +Inf in little-endian)
TAIL_MARKER = b"\x00\x00\x80\x7f"

# Regex for JScope channel name: exactly "JScope_" followed by valid format tokens
_JSCOPE_RE = re.compile(r"^JScope_([uif][1-4])+$")

# Regex for individual format tokens
_FMT_TOKEN_RE = re.compile(r"([uif])([1-4])")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def parse_format(fmt_str: str) -> list[tuple[str, int]]:
    """Parse a JScope format string into a list of (type_char, byte_size) tuples.

    Examples::

        >>> parse_format("u2f4")
        [('u', 2), ('f', 4)]
        >>> parse_format("u1u2u4i1i2i4f4")
        [('u', 1), ('u', 2), ('u', 4), ('i', 1), ('i', 2), ('i', 4), ('f', 4)]

    Args:
        fmt_str: Format string like ``"u2f4"``.

    Returns:
        List of ``(type_char, size)`` tuples, e.g. ``[("u", 2), ("f", 4)]``.

    Raises:
        ValueError: If *fmt_str* is empty or contains unrecognised tokens.
    """
    if not fmt_str:
        raise ValueError("Format string must not be empty")

    tokens = _FMT_TOKEN_RE.findall(fmt_str)

    # Verify the concatenation of tokens matches the original string exactly
    reconstructed = "".join(t + s for t, s in tokens)
    if reconstructed != fmt_str:
        # Find the bad portion
        bad = fmt_str[len(reconstructed):]
        # Check if any token is not in FORMAT_MAP
        for t, s in tokens:
            key = t + s
            if key not in FORMAT_MAP:
                raise ValueError(
                    f"Unrecognised format token '{key}' in '{fmt_str}'"
                )
        raise ValueError(
            f"Invalid format string '{fmt_str}': unexpected characters '{bad}'"
        )

    # Validate each token exists in FORMAT_MAP
    for t, s in tokens:
        key = t + s
        if key not in FORMAT_MAP:
            raise ValueError(
                f"Unrecognised format token '{key}' in '{fmt_str}'"
            )

    return [(t, int(s)) for t, s in tokens]


def decode_binary_frame(
    data: bytes,
    format_desc: list[tuple[str, int]],
) -> list[int | float]:
    """Decode a single binary frame according to *format_desc*.

    Args:
        data: Raw binary bytes (little-endian, *without* tail marker).
        format_desc: List of ``(type_char, size)`` tuples from
            :func:`parse_format`.

    Returns:
        List of decoded values (int or float).

    Raises:
        struct.error: If *data* is too short for the format descriptor.
        ValueError: If a format token is unknown.
    """
    values: list[int | float] = []
    offset = 0
    for type_char, size in format_desc:
        key = type_char + str(size)
        if key not in FORMAT_MAP:
            raise ValueError(f"Unknown format token '{key}'")
        fmt_char, byte_size, _ = FORMAT_MAP[key]
        values.append(struct.unpack_from(fmt_char, data, offset)[0])
        offset += byte_size
    return values


def is_jscope_channel(channel_name: str) -> bool:
    """Return True if *channel_name* matches the ``JScope_<fmt>`` pattern.

    Valid examples: ``JScope_f4``, ``JScope_u2f4``, ``JScope_u2u4f4i2``.

    Returns False for partial matches like ``JScope_``, ``jscope_u2f4``,
    or strings with extra trailing content.
    """
    if not _JSCOPE_RE.match(channel_name):
        return False
    # Additionally verify every token is in FORMAT_MAP
    fmt_part = channel_name[len("JScope_"):]
    try:
        tokens = parse_format(fmt_part)
        return len(tokens) > 0
    except ValueError:
        return False


def _frame_size(format_desc: list[tuple[str, int]]) -> int:
    """Return the byte size of one payload frame (without tail marker)."""
    return sum(size for _, size in format_desc)


# ---------------------------------------------------------------------------
# Streaming parser
# ---------------------------------------------------------------------------

class JScopeBinaryParser:
    """Streaming binary frame parser for JScope_* RTT channels.

    Handles:
    - Accumulating partial data across multiple :meth:`feed` calls.
    - Frame synchronisation via a 4-byte tail marker (``0x00 0x00 0x80 0x7f``).
    - Automatic resync when corrupted bytes are detected.

    Usage::

        parser = JScopeBinaryParser(
            format_desc=[("u", 2), ("f", 4)],
            channel_names=["counter", "voltage"],
        )
        frames = parser.feed(raw_bytes)
        for frame in frames:
            print(frame)  # {"counter": 42, "voltage": 3.14}
    """

    def __init__(
        self,
        format_desc: list[tuple[str, int]],
        channel_names: list[str] | None = None,
        format_str: str | None = None,
    ) -> None:
        """Initialise the parser.

        Args:
            format_desc: Format descriptor list from :func:`parse_format`.
            channel_names: Human-readable channel names.  If ``None``,
                auto-generated names ``ch0, ch1, ...`` are used.
            format_str: Original format string (optional, for diagnostics).
        """
        self.format_desc = format_desc
        self.format_str = format_str
        self._payload_size = _frame_size(format_desc)
        self._frame_size = self._payload_size + len(TAIL_MARKER)  # payload + tail

        if channel_names and len(channel_names) == len(format_desc):
            self._channel_names = channel_names
        else:
            self._channel_names = [f"ch{i}" for i in range(len(format_desc))]

        # Streaming state
        self._buffer = bytearray()
        self.dropped_bytes: int = 0
        self.dropped_frames: int = 0

    def feed(self, data: bytes) -> list[dict[str, int | float]]:
        """Feed raw bytes and return a list of decoded frames.

        Each frame is a ``dict[str, int|float]`` mapping channel names to
        decoded values.
        """
        self._buffer.extend(data)
        frames: list[dict[str, int | float]] = []

        while len(self._buffer) >= self._frame_size:
            # Find the next tail marker
            tail_pos = self._buffer.find(TAIL_MARKER)
            if tail_pos == -1:
                # No tail marker found — keep buffering, but discard excess
                # bytes beyond a reasonable lookahead to prevent unbounded
                # memory growth.  The tail could still arrive in the next
                # feed() call, so keep up to _frame_size bytes of slack.
                excess = len(self._buffer) - (self._frame_size + len(TAIL_MARKER))
                if excess > 0:
                    # Discard bytes from the front but keep enough for a
                    # potential frame that straddles feed() boundaries.
                    # Actually, let's be more careful: if we have way more
                    # data than a frame, the leading bytes are likely garbage.
                    # We scan for tail markers; if none exist and we have
                    # > 2 * frame_size bytes, discard the excess.
                    max_keep = 2 * self._frame_size
                    if len(self._buffer) > max_keep:
                        discard = len(self._buffer) - max_keep
                        self._buffer = self._buffer[discard:]
                        self.dropped_bytes += discard
                break

            # Check if the bytes before the tail marker form a valid payload
            payload_start = tail_pos - self._payload_size

            if payload_start < 0:
                # Tail marker found but not enough bytes before it for a
                # complete payload.  Skip past this tail and keep searching.
                skip_to = tail_pos + len(TAIL_MARKER)
                self.dropped_bytes += skip_to
                self._buffer = self._buffer[skip_to:]
                continue

            if payload_start > 0:
                # There are extra bytes before the payload — they are garbage.
                self.dropped_bytes += payload_start
                self._buffer = self._buffer[payload_start:]
                # Now buffer starts at the payload; tail_pos is no longer
                # valid, recalculate.
                tail_pos = self._payload_size

            # Extract payload
            payload = bytes(self._buffer[:self._payload_size])

            try:
                values = decode_binary_frame(payload, self.format_desc)
            except (struct.error, ValueError):
                # Corrupted payload — skip past the tail marker and continue
                skip_to = tail_pos + len(TAIL_MARKER)
                self.dropped_bytes += skip_to
                self.dropped_frames += 1
                self._buffer = self._buffer[skip_to:]
                continue

            # Build result dict
            frame: dict[str, int | float] = {}
            for i, name in enumerate(self._channel_names):
                if i < len(values):
                    frame[name] = values[i]
            frames.append(frame)

            # Consume payload + tail marker
            consumed = self._payload_size + len(TAIL_MARKER)
            self._buffer = self._buffer[consumed:]

        return frames
