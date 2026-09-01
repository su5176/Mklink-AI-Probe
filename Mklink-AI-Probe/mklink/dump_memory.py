"""Dump Memory binary protocol parser for MKLink SuperWatch.

Implements the streaming binary frame parser for both OLD and B1 (chunked)
frame formats defined in the dump_memory protocol specification.

Frame formats (per firmware spec, 2026-06-06):

  OLD (total_size <= 2048):
    +0x00  8B magic           "MPMDMPMD"
    +0x08  8B timestamp_us
    +0x10  2B frame_length
    +0x12  1B region_count
    +0x13  ... region[i] = idx(1) + size(2) + data(size)
    +EOF-6 2B flags
    +EOF-4 4B crc32

  B1 (total_size > 2048, chunked):
    +0x00  8B magic           "MPMDMPMD"
    +0x08  8B timestamp_us
    +0x10  2B frame_length
    +0x12  1B region_count
    +0x13  2B flags
    +0x15  4B total_size
    +0x19  2B block_size      (firmware: fixed 2048)
    +0x1B  2B block_index
    +0x1D  2B block_count
    +0x1F  4B block_crc32     (crc32 of region data payload)
    +0x23  ... region[i] = idx(1) + size(2) + data(size)
    +EOF-4 4B crc32            (crc32 of magic..last region data byte)

Maximum single dump_memory call is 32 KiB (32768 bytes). Firmware currently
truncates the last block by ~512B when total_size > 32 KiB, so the safe
upper bound is 32 KiB. Callers needing more must chunk the request at the
host level.

2026-06-07 direct official API retest on MKLink V4.3.1 confirmed:
  - cmd.dump_memory(0x08000000, 256, 0): OLD, full 256B, flags=0
  - cmd.dump_memory(0x20010200, 32, 0): OLD, full 32B, flags=0
  - cmd.dump_memory(0x08020000, 2049, 0): B1, 2048B + 1B, flags=0

Zero internal mklink dependencies — only uses struct/binascii from stdlib.
"""

from __future__ import annotations

import binascii
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
MAGIC = b'\x4D\x50\x4D\x44\x4D\x50\x4D\x44'   # "MPMDMPMD"
MAGIC_LEN = 8

# OLD frame header: magic(8) + ts(8) + frame_length(2) + region_count(1) = 19
OLD_HEADER_LEN = 19
OLD_TRAILER_LEN = 6  # flags(2) + crc32(4)
MIN_OLD_FRAME_LEN = OLD_HEADER_LEN + 3 + OLD_TRAILER_LEN  # 1 region(3) + 6 = 28

# B1 frame header: OLD_HEADER + flags(2) + total_size(4) + block_size(2) +
#                  block_index(2) + block_count(2) + block_crc32(4) = 35
B1_HEADER_LEN = 35
B1_TRAILER_LEN = 4  # crc32(4)
MIN_B1_FRAME_LEN = B1_HEADER_LEN + 3 + B1_TRAILER_LEN  # 1 region(3) + 4 = 42

MAX_FRAME_LEN = 65535
MAX_REGIONS = 16
# V4 firmware advertises 16 regions, but the text API needs 33 positional
# arguments at that point (16 address/size pairs plus period), exactly equal to
# the current PIKA_ARG_NUM_MAX. Field testing on V4.3.8 showed this exact
# parameter-count boundary can wedge the REPL; it is independent of free heap.
# Keep one argument-pair of headroom until firmware exposes a non-varargs/binary
# configuration entry point or raises and validates its VM argument capacity.
MAX_SAFE_REPL_REGIONS = 15
MAX_REPL_COMMAND_BYTES = 511  # PIKA_LINE_BUFF_SIZE is 512 including NUL.
DUMP_MEMORY_STOP_PERIOD = -1.0
EXPECTED_BLOCK_SIZE = 2048  # firmware-fixed per spec

# Maximum total bytes per dump_memory() call.
#
# Default raised to 512 KiB on 2026-06-26: firmware V4.3.3 was retested end-to-end
# on GD32F303CE (512 KiB Flash) and dumps the *entire* 512 KiB Flash cleanly —
# 256 B1 blocks, all flags=0x0000, every block_crc32 + frame_crc32 valid. Full
# retest data in docs/Mklink/2026-06-26-gd32f303-dump-flush-boundary-retest-report.md.
#
# CAUTION for older firmware: pre-V4.3.3 builds may still carry BUG-5 (>64 KiB
# truncates the last block by 512B). On such firmware, pass smaller ADDR:SIZE
# regions (≤32 KiB) from the host instead of relying on the raised default.
# Use set_max_total_data_size() to lower the cap programmatically if needed.
MAX_TOTAL_DATA_SIZE = 512 * 1024  # 524288

_DUMP_MEMORY_CAPTURE_LOCK = threading.RLock()


# FLAGS bit masks
FLAG_TICK_OVERFLOW    = 0x0001
FLAG_TIMING_VIOLATION = 0x0002
FLAG_REGION_ERROR     = 0x0004
FLAG_SAMPLE_DROPPED   = 0x0008


class DumpMemoryReadError(RuntimeError):
    """The dump stream returned a malformed or explicitly failed sample."""

    def __init__(
        self,
        message: str,
        *,
        gap_fact: str = "invalid_block_count",
        gap_count: int = 1,
    ) -> None:
        super().__init__(message)
        self.gap_fact = gap_fact
        self.gap_count = max(1, int(gap_count))


class DumpMemoryUnsupported(DumpMemoryReadError):
    """The connected firmware did not expose a usable dump-memory response."""


class DumpMemoryBusyError(RuntimeError):
    """Another bridge stream currently owns the connected probe."""


@contextmanager
def exclusive_dump_memory_capture() -> Iterator[None]:
    """Serialize dump captures in this process, including multi-sample calls."""
    with _DUMP_MEMORY_CAPTURE_LOCK:
        yield


def _enter_dump_stream(bridge) -> None:
    """Atomically claim DUMP_STREAM, with a safe fallback for narrow adapters."""
    from mklink._types import DeviceState

    try_enter = getattr(bridge, "_try_enter_stream", None)
    if callable(try_enter):
        if try_enter(DeviceState.DUMP_STREAM):
            return
        state = getattr(bridge, "state", None)
        state_name = getattr(state, "value", "unavailable")
        raise DumpMemoryBusyError(
            f"dump_memory requires an idle READY bridge; current state is {state_name}"
        )

    try:
        state = bridge.state
    except AttributeError:
        # Narrow test doubles and older bridge adapters do not expose state.
        bridge._enter_stream(DeviceState.DUMP_STREAM)
        return
    if state is DeviceState.READY:
        bridge._enter_stream(DeviceState.DUMP_STREAM)
        return
    state_name = getattr(state, "value", "unavailable")
    raise DumpMemoryBusyError(
        f"dump_memory requires an idle READY bridge; current state is {state_name}"
    )


# Sentinel return values for _try_parse
_NEED_MORE = object()
_RETRY = object()


class DumpMemoryParser:
    """Streaming binary parser for dump_memory frames (OLD + B1).

    Follows the same feed() -> list[dict] pattern as JustFloatParser
    and JScopeBinaryParser.

    Each returned frame dict has:
        "timestamp_us": int
        "format":       "OLD" or "B1"
        "regions":      list[(region_index, bytes)]
        "flags":        int
        # B1-only fields (absent for OLD):
        "total_size":   int
        "block_size":   int
        "block_index":  int
        "block_count":  int
        "block_crc32":  int   (crc32 of region data payload)
        "block_crc_ok": bool  (B1 region data payload CRC result)
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
        """Feed raw bytes, return list of parsed frame dicts."""
        self._buf.extend(data)
        frames: list[dict] = []
        while True:
            result = self._try_parse()
            if result is _NEED_MORE:
                break
            if result is not _RETRY:
                frames.append(result)
        return frames

    # ---- internal ---------------------------------------------------------

    def _try_parse(self):
        # Step 1: Find MAGIC
        idx = self._buf.find(MAGIC)
        if idx < 0:
            drop = len(self._buf) - MAGIC_LEN + 1
            if drop > 0:
                self._dropped_bytes += drop
                del self._buf[:drop]
            return _NEED_MORE

        if idx > 0:
            self._dropped_bytes += idx
            del self._buf[:idx]

        # Step 2: Need at least header bytes to read frame_length
        if len(self._buf) < OLD_HEADER_LEN:
            return _NEED_MORE

        frame_length = struct.unpack_from('<H', self._buf, 16)[0]
        if frame_length < MIN_OLD_FRAME_LEN or frame_length > MAX_FRAME_LEN:
            self._dropped_bytes += MAGIC_LEN
            self._dropped_frames += 1
            del self._buf[:MAGIC_LEN]
            return _RETRY

        if len(self._buf) < frame_length:
            return _NEED_MORE

        frame_bytes = bytes(self._buf[:frame_length])
        del self._buf[:frame_length]

        # Step 3: CRC32 check (trailing 4 bytes)
        payload = frame_bytes[:-4]
        expected_crc = struct.unpack('<I', frame_bytes[-4:])[0]
        actual_crc = binascii.crc32(payload) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            self._crc_errors += 1
            self._dropped_frames += 1
            return _RETRY

        # Step 4: Detect B1 vs OLD
        region_count = frame_bytes[18]

        if self._looks_like_b1(frame_bytes, frame_length, region_count):
            return self._parse_b1(frame_bytes, frame_length)
        return self._parse_old(frame_bytes, frame_length, region_count)

    @staticmethod
    def _looks_like_b1(frame_bytes: bytes, frame_length: int, region_count: int) -> bool:
        """Heuristic: B1 frames have a recognisable B1 header before the regions.

        In a B1 frame:
          - frame_length >= MIN_B1_FRAME_LEN (42)
          - offset 0x13..0x14 = flags (2B, expected 0 unless error)
          - offset 0x15..0x18 = total_size (4B, 0 < x <= MAX)
          - offset 0x19..0x1A = block_size (2B, expect 2048)
          - offset 0x1B..0x1C = block_index (2B)
          - offset 0x1D..0x1E = block_count (2B)
        """
        if frame_length < MIN_B1_FRAME_LEN:
            return False
        if frame_length < B1_HEADER_LEN + 4:
            return False
        # B1 only triggers when total_size > 2048. Reading total_size at 0x15:
        total_size = struct.unpack_from('<I', frame_bytes, 0x15)[0]
        if total_size == 0 or total_size > MAX_TOTAL_DATA_SIZE:
            return False
        block_size = struct.unpack_from('<H', frame_bytes, 0x19)[0]
        if block_size != EXPECTED_BLOCK_SIZE:
            return False
        block_index = struct.unpack_from('<H', frame_bytes, 0x1B)[0]
        block_count = struct.unpack_from('<H', frame_bytes, 0x1D)[0]
        if block_count == 0 or block_index >= block_count:
            return False
        return True

    @staticmethod
    def _parse_b1(frame_bytes: bytes, frame_length: int) -> dict:
        timestamp_us = struct.unpack_from('<Q', frame_bytes, 8)[0]
        region_count = frame_bytes[18]
        flags = struct.unpack_from('<H', frame_bytes, 0x13)[0]
        total_size = struct.unpack_from('<I', frame_bytes, 0x15)[0]
        block_size = struct.unpack_from('<H', frame_bytes, 0x19)[0]
        block_index = struct.unpack_from('<H', frame_bytes, 0x1B)[0]
        block_count = struct.unpack_from('<H', frame_bytes, 0x1D)[0]
        block_crc32 = struct.unpack_from('<I', frame_bytes, 0x1F)[0]

        regions: list[tuple[int, bytes]] = []
        region_payload = bytearray()
        offset = B1_HEADER_LEN
        for _ in range(region_count):
            if offset + 3 > frame_length - 4:
                break
            region_index = frame_bytes[offset]
            region_size = struct.unpack_from('<H', frame_bytes, offset + 1)[0]
            offset += 3
            if offset + region_size > frame_length - 4:
                break
            region_data = frame_bytes[offset:offset + region_size]
            regions.append((region_index, region_data))
            region_payload.extend(region_data)
            offset += region_size

        actual_block_crc32 = binascii.crc32(bytes(region_payload)) & 0xFFFFFFFF

        return {
            "timestamp_us": timestamp_us,
            "format": "B1",
            "regions": regions,
            "flags": flags,
            "total_size": total_size,
            "block_size": block_size,
            "block_index": block_index,
            "block_count": block_count,
            "block_crc32": block_crc32,
            "block_crc_ok": actual_block_crc32 == block_crc32,
        }

    @staticmethod
    def _parse_old(frame_bytes: bytes, frame_length: int, region_count: int) -> dict:
        timestamp_us = struct.unpack_from('<Q', frame_bytes, 8)[0]
        # OLD frame: regions, then flags(2), then crc32(4)
        flags = struct.unpack_from('<H', frame_bytes, frame_length - 6)[0]

        regions: list[tuple[int, bytes]] = []
        offset = OLD_HEADER_LEN
        for _ in range(region_count):
            if offset + 3 > frame_length - 6:
                break
            region_index = frame_bytes[offset]
            region_size = struct.unpack_from('<H', frame_bytes, offset + 1)[0]
            offset += 3
            if offset + region_size > frame_length - 6:
                break
            regions.append((region_index, frame_bytes[offset:offset + region_size]))
            offset += region_size

        return {
            "timestamp_us": timestamp_us,
            "format": "OLD",
            "regions": regions,
            "flags": flags,
        }


# ---------------------------------------------------------------------------
# Configurable max total data size per dump_memory batch.
# Default = MAX_TOTAL_DATA_SIZE (32 KiB). Override with set_max_total_data_size
# only if the connected firmware has been confirmed to support a larger value.
# ---------------------------------------------------------------------------


def get_max_total_data_size() -> int:
    """Return current max total data size for dump_memory batches (bytes)."""
    return MAX_TOTAL_DATA_SIZE


def set_max_total_data_size(size: int) -> None:
    """Override the max total data size for dump_memory batches.

    Args:
        size: New max in bytes. Default is 512 KiB (firmware V4.3.3 validated).
              Must not exceed 512 KiB — that is the largest total validated to
              date (see docs/Mklink/2026-06-26-gd32f303-dump-flush-boundary-retest-report.md).
              Use this to *lower* the cap (e.g. on older firmware carrying BUG-5).
    """
    global MAX_TOTAL_DATA_SIZE
    if size > 512 * 1024:
        raise ValueError(
            f"size {size} exceeds the 512 KiB validated ceiling. "
            f"See docs/Mklink/2026-06-26-gd32f303-dump-flush-boundary-retest-report.md."
        )
    MAX_TOTAL_DATA_SIZE = size


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
        ValueError: if total region size exceeds MAX_TOTAL_DATA_SIZE (default 512 KiB).
                    Pass smaller ADDR:SIZE regions to split large requests at the host level
                    (e.g. on older firmware that truncates >64 KiB dumps — BUG-5).
    """
    import math

    if not region_pairs:
        raise ValueError("dump-memory requires at least one region")
    if len(region_pairs) > MAX_SAFE_REPL_REGIONS:
        raise ValueError(
            f"dump-memory text API safely supports at most "
            f"{MAX_SAFE_REPL_REGIONS} regions; firmware protocol capacity is "
            f"{MAX_REGIONS}, but the 16-region Pika varargs boundary is unsafe"
        )
    if not math.isfinite(float(period)) or float(period) < -1:
        raise ValueError("dump-memory period must be -1, 0, or a positive finite value")
    for index, pair in enumerate(region_pairs):
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError(f"region_pairs[{index}] must be an (address, size) pair")
        addr, size = pair
        if type(addr) is not int or not 0 <= addr <= 0xFFFFFFFF:
            raise ValueError(f"region_pairs[{index}] address is outside 32-bit range")
        if type(size) is not int or size <= 0:
            raise ValueError(f"region_pairs[{index}] size must be a positive integer")
        if addr + size > 0x100000000:
            raise ValueError(f"region_pairs[{index}] exceeds the 32-bit address space")

    total_size = sum(size for _, size in region_pairs)
    if total_size > MAX_TOTAL_DATA_SIZE:
        raise ValueError(
            f"Total region size {total_size} exceeds maximum {MAX_TOTAL_DATA_SIZE} bytes "
            f"(512 KiB). Split the request into smaller ADDR:SIZE regions at the host level."
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
    command = f"cmd.dump_memory({', '.join(parts)})"
    if len(command.encode("utf-8")) > MAX_REPL_COMMAND_BYTES:
        raise ValueError(
            f"dump-memory command is longer than the safe "
            f"{MAX_REPL_COMMAND_BYTES}-byte Pika REPL line"
        )
    return command


def read_dump_memory_once(
    bridge,
    address: int,
    size: int,
    *,
    timeout: float = 2.0,
    poll_interval: float = 0.0005,
) -> bytes:
    """Read one complete dump-memory sample and explicitly leave stream mode."""
    if size <= 0:
        raise ValueError("dump-memory size must be greater than zero")
    from mklink._types import DeviceState

    parser = DumpMemoryParser(region_sizes=[size])
    command = build_dump_mem_command([(address, size)], 0)
    stop_command = build_dump_mem_command(
        [(address, 1)], DUMP_MEMORY_STOP_PERIOD,
    )
    deadline = time.monotonic() + max(0.001, float(timeout))
    bridge._enter_stream(DeviceState.DUMP_STREAM)
    try:
        bridge._write_raw((command + "\n").encode("utf-8"))
        while time.monotonic() < deadline:
            raw = bridge.drain_stream_bytes(max_bytes=1024 * 1024)
            for frame in parser.feed(raw) if raw else ():
                for region_index, payload in frame.get("regions", ()):
                    if region_index == 0 and len(payload) >= size:
                        return payload[:size]
            if poll_interval:
                time.sleep(poll_interval)
        raise TimeoutError("timed out waiting for one dump-memory sample")
    finally:
        try:
            # ``RTTView.stop`` does not stop dump_memory and period=0 would
            # request another one-shot sample.  Use the firmware's explicit
            # dump stop value even when the one-shot read failed.
            bridge._write_raw((stop_command + "\n").encode("utf-8"))
            try:
                bridge.drain_stream_bytes()
            except Exception:
                pass
        finally:
            bridge._exit_stream()


def read_dump_memory_range_once(
    bridge,
    address: int,
    size: int,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.0005,
) -> bytes:
    """Read one complete region, including B1 multi-block responses.

    ``read_dump_memory_once`` is retained for the streaming consumers that
    only request a small sample.  Online Flash reads need the complete
    payload: current firmware emits one B1 frame per 2048-byte block when a
    request is larger than 2048 bytes.  This helper collects and validates all
    blocks before returning, and never sends a text stream-stop command that
    could be interpreted as target data.
    """
    if type(address) is not int or address < 0:
        raise ValueError("dump-memory address must be a non-negative integer")
    if type(size) is not int or size <= 0 or size > MAX_TOTAL_DATA_SIZE:
        raise ValueError(
            f"dump-memory size must be between 1 and {MAX_TOTAL_DATA_SIZE} bytes"
        )
    from mklink._types import DeviceState

    parser = DumpMemoryParser(region_sizes=[size])
    command = build_dump_mem_command([(address, size)], 0)
    stop_command = build_dump_mem_command(
        [(address, 1)], DUMP_MEMORY_STOP_PERIOD,
    )
    deadline = time.monotonic() + max(0.001, float(timeout))
    blocks: dict[int, bytes] = {}
    expected_blocks: int | None = None
    raw_tail = bytearray()
    bridge._enter_stream(DeviceState.DUMP_STREAM)
    try:
        # ``period=0`` emits one complete sample before the firmware returns
        # to idle.  The previous request is stopped in the finally block with
        # ``period=-1``; if a caller was interrupted, bridge.connect() also
        # uses that state-machine stop value during stream recovery.
        bridge._write_raw((command + "\n").encode("utf-8"))
        while time.monotonic() < deadline:
            raw = bridge.drain_stream_bytes(max_bytes=1024 * 1024)
            if raw:
                raw_tail.extend(raw[-4096:])
            for frame in parser.feed(raw) if raw else ():
                flags = int(frame.get("flags", 0))
                if flags:
                    raise DumpMemoryReadError(
                        f"dump_memory returned error flags 0x{flags:04X}"
                    )
                regions = frame.get("regions", ())
                payload = b"".join(
                    data for region_index, data in regions if region_index == 0
                )
                if frame.get("format") == "OLD":
                    # OLD frames are valid only for a single <=2048-byte
                    # request.  Any such frame left over from another caller
                    # must be discarded rather than returned as this range.
                    if size > 2048:
                        continue
                    if len(payload) != size:
                        continue
                    return payload

                total_size = int(frame.get("total_size", 0))
                # A prior request may have already placed B1 frames in the
                # USB queue.  The protocol has no address field, so total size
                # is the only safe discriminator; discard mismatched samples
                # until the current request's first block arrives.
                if total_size != size:
                    continue
                block_size = int(frame.get("block_size", 0))
                block_index = int(frame.get("block_index", -1))
                block_count = int(frame.get("block_count", 0))
                if (
                    total_size != size
                    or block_size <= 0
                    or block_count <= 0
                    or block_index < 0
                    or block_index >= block_count
                    or not bool(frame.get("block_crc_ok", False))
                ):
                    raise DumpMemoryReadError("dump_memory block metadata is invalid")
                expected_blocks = block_count if expected_blocks is None else expected_blocks
                if expected_blocks != block_count:
                    raise DumpMemoryReadError("dump_memory block count changed")
                expected_size = min(block_size, size - block_index * block_size)
                if expected_size <= 0 or len(payload) != expected_size:
                    raise DumpMemoryReadError(
                        f"dump_memory block {block_index} returned {len(payload)} bytes"
                    )
                blocks[block_index] = payload
                if len(blocks) == expected_blocks:
                    result = b"".join(blocks[index] for index in range(expected_blocks))
                    if len(result) != size:
                        raise DumpMemoryReadError(
                            f"dump_memory assembled {len(result)} bytes for {size}"
                        )
                    return result
            if poll_interval:
                time.sleep(poll_interval)
        diagnostic = bytes(raw_tail).decode("utf-8", errors="replace").lower()
        if (
            "command not found" in diagnostic
            or "attributeerror" in diagnostic
            or "dump_memory fail" in diagnostic
        ):
            raise DumpMemoryUnsupported("cmd.dump_memory is not supported by the probe")
        raise TimeoutError("timed out waiting for one complete dump-memory range")
    finally:
        try:
            # Stop immediately instead of requesting another one-shot sample;
            # the latter would leave a complete frame queued for the next
            # operation on firmware that implements period=0 literally.
            bridge._write_raw((stop_command + "\n").encode("utf-8"))
            stop_deadline = time.monotonic() + 0.2
            while time.monotonic() < stop_deadline:
                bridge.drain_stream_bytes(max_bytes=1024 * 1024)
                time.sleep(0.002)
        except Exception:
            pass
        bridge._exit_stream()


def read_dump_memory_regions_once(
    bridge,
    region_pairs: list[tuple[int, int]],
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.0005,
) -> tuple[bytes, ...]:
    """Read one sample while holding the process-wide dump capture lock."""
    with exclusive_dump_memory_capture():
        return _read_dump_memory_regions_once_locked(
            bridge,
            region_pairs,
            timeout=timeout,
            poll_interval=poll_interval,
        )


def _read_dump_memory_regions_once_locked(
    bridge,
    region_pairs: list[tuple[int, int]],
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.0005,
) -> tuple[bytes, ...]:
    """Read one validated multi-region sample on an already-owned bridge.

    B1 blocks may split one region or cross a region boundary.  They are
    collected by block index, checked for continuity, and only exposed after
    every declared region has exact byte coverage.
    """
    if not isinstance(region_pairs, list) or not 1 <= len(region_pairs) <= MAX_REGIONS:
        raise ValueError(f"dump-memory requires 1..{MAX_REGIONS} regions")
    normalized: list[tuple[int, int]] = []
    total_size = 0
    for index, pair in enumerate(region_pairs):
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError(f"dump-memory region {index} must be (address, size)")
        address, size = pair
        if (
            type(address) is not int
            or address < 0
            or address > 0xFFFFFFFFFFFFFFFF
            or type(size) is not int
            or size <= 0
            or address + size > 0x10000000000000000
        ):
            raise ValueError(f"dump-memory region {index} has an invalid range")
        total_size += size
        if total_size > MAX_TOTAL_DATA_SIZE:
            raise ValueError(
                f"dump-memory total size must not exceed {MAX_TOTAL_DATA_SIZE} bytes"
            )
        normalized.append((address, size))

    region_sizes = [size for _, size in normalized]
    parser = DumpMemoryParser(region_sizes=region_sizes)
    command = build_dump_mem_command(normalized, 0)
    stop_command = build_dump_mem_command([(normalized[0][0], 1)], -1)
    deadline = time.monotonic() + max(0.001, float(timeout))
    blocks: dict[int, dict[int, bytes]] = {}
    expected_blocks: int | None = None
    expected_block_size: int | None = None
    incomplete_region_count = 0
    raw_tail = bytearray()
    _enter_dump_stream(bridge)
    try:
        bridge._write_raw((command + "\n").encode("utf-8"))
        while time.monotonic() < deadline:
            raw = bridge.drain_stream_bytes(max_bytes=1024 * 1024)
            if raw:
                raw_tail.extend(raw[-4096:])
            frames = parser.feed(raw) if raw else ()
            if parser.crc_errors:
                raise DumpMemoryReadError(
                    "dump_memory frame CRC validation failed",
                    gap_fact="crc_error_count",
                    gap_count=parser.crc_errors,
                )
            for frame in frames:
                flags = int(frame.get("flags", 0))
                if flags:
                    raise DumpMemoryReadError(
                        "dump_memory returned firmware error flags",
                        gap_fact="firmware_error_count",
                    )
                regions = frame.get("regions", ())
                by_region: dict[int, bytes] = {}
                valid_regions = True
                for region_index, payload in regions:
                    if (
                        type(region_index) is not int
                        or not 0 <= region_index < len(normalized)
                        or region_index in by_region
                        or not isinstance(payload, (bytes, bytearray, memoryview))
                    ):
                        valid_regions = False
                        break
                    by_region[region_index] = bytes(payload)
                if not valid_regions:
                    raise DumpMemoryReadError(
                        "dump_memory region coverage is invalid",
                        gap_fact="region_gap_count",
                    )

                if frame.get("format") == "OLD":
                    if len(by_region) != len(normalized):
                        incomplete_region_count = max(
                            incomplete_region_count,
                            len(normalized) - len(by_region),
                        )
                        continue
                    mismatched_regions = sum(
                        len(by_region.get(index, b"")) != size
                        for index, size in enumerate(region_sizes)
                    )
                    if mismatched_regions:
                        incomplete_region_count = max(
                            incomplete_region_count,
                            mismatched_regions,
                        )
                        continue
                    return tuple(by_region[index] for index in range(len(normalized)))

                if int(frame.get("total_size", 0)) != total_size:
                    # The bridge may still contain a complete sample left by a
                    # prior request; total size is the protocol's discriminator.
                    continue
                block_size = int(frame.get("block_size", 0))
                block_index = int(frame.get("block_index", -1))
                block_count = int(frame.get("block_count", 0))
                calculated_count = (
                    (total_size + block_size - 1) // block_size
                    if block_size > 0
                    else 0
                )
                if (
                    block_size <= 0
                    or block_count != calculated_count
                    or block_index < 0
                    or block_index >= block_count
                ):
                    raise DumpMemoryReadError(
                        "dump_memory block metadata is invalid",
                        gap_fact="invalid_block_count",
                    )
                if not bool(frame.get("block_crc_ok", False)):
                    raise DumpMemoryReadError(
                        "dump_memory block CRC validation failed",
                        gap_fact="crc_error_count",
                    )
                if expected_blocks is None:
                    expected_blocks = block_count
                    expected_block_size = block_size
                elif (
                    expected_blocks != block_count
                    or expected_block_size != block_size
                ):
                    raise DumpMemoryReadError(
                        "dump_memory block layout changed",
                        gap_fact="invalid_block_count",
                    )
                expected_payload = min(
                    block_size,
                    total_size - block_index * block_size,
                )
                if sum(len(data) for data in by_region.values()) != expected_payload:
                    raise DumpMemoryReadError(
                        "dump_memory block coverage is incomplete",
                        gap_fact="region_gap_count",
                    )
                if block_index in blocks:
                    raise DumpMemoryReadError(
                        "dump_memory repeated a block index",
                        gap_fact="invalid_block_count",
                    )
                blocks[block_index] = by_region
                if len(blocks) == expected_blocks:
                    if set(blocks) != set(range(expected_blocks)):
                        raise DumpMemoryReadError(
                            "dump_memory block sequence is incomplete",
                            gap_fact="missing_block_count",
                        )
                    assembled = [bytearray() for _ in normalized]
                    for current_index in range(expected_blocks):
                        for region_index, data in blocks[current_index].items():
                            assembled[region_index].extend(data)
                            if len(assembled[region_index]) > region_sizes[region_index]:
                                raise DumpMemoryReadError(
                                    "dump_memory region coverage overflowed",
                                    gap_fact="region_gap_count",
                                )
                    incomplete_regions = sum(
                        len(data) != region_sizes[index]
                        for index, data in enumerate(assembled)
                    )
                    if incomplete_regions:
                        raise DumpMemoryReadError(
                            "dump_memory region coverage is incomplete",
                            gap_fact="region_gap_count",
                            gap_count=incomplete_regions,
                        )
                    return tuple(bytes(data) for data in assembled)
            if poll_interval:
                time.sleep(poll_interval)

        diagnostic = bytes(raw_tail).decode("utf-8", errors="replace").lower()
        if (
            "command not found" in diagnostic
            or "attributeerror" in diagnostic
            or "dump_memory fail" in diagnostic
        ):
            raise DumpMemoryUnsupported(
                "cmd.dump_memory is not supported by the probe",
                gap_fact="firmware_error_count",
            )
        if parser.crc_errors:
            raise DumpMemoryReadError(
                "dump_memory frame CRC validation failed",
                gap_fact="crc_error_count",
                gap_count=parser.crc_errors,
            )
        if incomplete_region_count:
            raise DumpMemoryReadError(
                "dump_memory region coverage is incomplete",
                gap_fact="region_gap_count",
                gap_count=incomplete_region_count,
            )
        missing = (
            max(1, expected_blocks - len(blocks))
            if expected_blocks is not None
            else 1
        )
        raise DumpMemoryReadError(
            "timed out waiting for a complete dump-memory sample",
            gap_fact="missing_block_count",
            gap_count=missing,
        )
    finally:
        try:
            bridge._write_raw((stop_command + "\n").encode("utf-8"))
            stop_deadline = time.monotonic() + 0.2
            while time.monotonic() < stop_deadline:
                bridge.drain_stream_bytes(max_bytes=1024 * 1024)
                time.sleep(0.002)
        except Exception:
            pass
        bridge._exit_stream()


class DumpMemoryStreamSession:
    """Own one MKLink ``cmd.dump_memory`` binary-stream lifecycle.

    Protocol parsing stays in :class:`DumpMemoryParser`; this class only
    coordinates bridge mode, command delivery, draining, and explicit stop.
    """

    def __init__(
        self,
        bridge,
        region_pairs: list[tuple[int, int]],
        period: float,
        *,
        stop_grace_s: float = 0.05,
    ):
        if not region_pairs:
            raise ValueError("dump-memory requires at least one region")
        if len(region_pairs) > MAX_SAFE_REPL_REGIONS:
            raise ValueError(
                f"too many regions for the safe text API: {len(region_pairs)} > "
                f"{MAX_SAFE_REPL_REGIONS}"
            )
        if period <= 0:
            raise ValueError("streaming period must be greater than zero")
        self.bridge = bridge
        self.region_pairs = list(region_pairs)
        self.period = float(period)
        self.stop_grace_s = max(0.0, float(stop_grace_s))
        self.parser = DumpMemoryParser(region_sizes=[size for _, size in region_pairs])
        self.started = False
        self._protocol_frames = 0
        self._complete_samples = 0
        self._firmware_flagged_frames = 0
        self._firmware_sample_drop_flags = 0

    def start(self) -> None:
        if self.started:
            return
        from mklink._types import DeviceState

        command = build_dump_mem_command(self.region_pairs, self.period)
        self.bridge._enter_stream(DeviceState.DUMP_STREAM)
        try:
            self.bridge._write_raw((command + "\n").encode("utf-8"))
        except Exception:
            self.bridge._exit_stream()
            raise
        self.started = True

    def read_frames(self, max_bytes: int | None = None) -> list[dict]:
        if not self.started:
            raise RuntimeError("dump-memory stream is not started")
        raw = self.bridge.drain_stream_bytes(max_bytes=max_bytes)
        frames = self.parser.feed(raw) if raw else []
        for frame in frames:
            self._protocol_frames += 1
            flags = int(frame.get("flags", 0))
            if flags:
                self._firmware_flagged_frames += 1
            if flags & FLAG_SAMPLE_DROPPED:
                self._firmware_sample_drop_flags += 1
            if (
                frame.get("format") != "B1"
                or frame.get("block_index", 0) + 1 >= frame.get("block_count", 1)
            ):
                self._complete_samples += 1
        return frames

    def stop(self) -> None:
        if not self.started:
            return
        try:
            command = build_dump_mem_command(
                self.region_pairs, DUMP_MEMORY_STOP_PERIOD,
            )
            self.bridge._write_raw((command + "\n").encode("utf-8"))
            if self.stop_grace_s:
                time.sleep(self.stop_grace_s)
            try:
                self.bridge.drain_stream_bytes()
            except Exception:
                pass
        finally:
            self.bridge._exit_stream()
            self.started = False

    @property
    def stats(self) -> dict[str, int]:
        return {
            "protocol_frames": self._protocol_frames,
            "complete_samples": self._complete_samples,
            "parser_dropped_bytes": self.parser.dropped_bytes,
            "parser_dropped_frames": self.parser.dropped_frames,
            "parser_crc_errors": self.parser.crc_errors,
            "firmware_flagged_frames": self._firmware_flagged_frames,
            "firmware_sample_drop_flags": self._firmware_sample_drop_flags,
        }


def decode_frame_to_points(
    frame: dict,
    block_addresses: list[tuple[int, int, list[tuple]]],
    origin_us: int | None,
) -> tuple[list[dict], int | None]:
    """Decode a parsed frame's region data into per-variable point dicts.

    This bridges binary region data back to the variable name/type system
    used by the SuperWatch visualizer.

    Args:
        frame: Parsed frame from DumpMemoryParser.feed().
        block_addresses: Per-region info list with item name, type, offset,
            byte size, scalar kind, and enum values.
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
        for item in items:
            if len(item) == 4:
                name, type_name, item_offset, enum_values = item
                item_size = _item_size(type_name)
                scalar_kind = None
            else:
                name, type_name, item_offset, item_size, scalar_kind, enum_values = item
            data = region_data[item_offset:item_offset + item_size]
            if data:
                # Store raw numeric value for charting; enum display is handled by frontend
                point[name] = decode_value(
                    data,
                    type_name,
                    known_size=item_size,
                    scalar_kind=scalar_kind,
                )
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
