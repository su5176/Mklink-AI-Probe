"""VOFA+ JustFloat visualization module.

Provides a JustFloat binary frame parser and a web-based real-time
visualizer that reuses the RTT VisualizationServer (HTTP/SSE + Canvas).

Zero new Python dependencies.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass
import math
import signal
import struct
import sys
import threading
import time
import webbrowser

from mklink.remote.stream_protocol import WAVEFORM_SAMPLE_MAJOR_FLOAT32

VOFA_TYPE_INFO = {
    "int8_t": {"type": "int8_t", "size": 1},
    "uint8_t": {"type": "uint8_t", "size": 1},
    "int16_t": {"type": "int16_t", "size": 2},
    "uint16_t": {"type": "uint16_t", "size": 2},
    "int32_t": {"type": "int32_t", "size": 4},
    "uint32_t": {"type": "uint32_t", "size": 4},
    "float": {"type": "float", "size": 4},
    "bool": {"type": "bool", "size": 1},
}

_VOFA_TYPE_ALIASES = {
    "char": "int8_t", "int8": "int8_t", "int8_t": "int8_t",
    "uchar": "uint8_t", "uint8": "uint8_t", "uint8_t": "uint8_t",
    "short": "int16_t", "int16": "int16_t", "int16_t": "int16_t",
    "ushort": "uint16_t", "uint16": "uint16_t", "uint16_t": "uint16_t",
    "int": "int32_t", "int32": "int32_t", "int32_t": "int32_t",
    "uint": "uint32_t", "uint32": "uint32_t", "uint32_t": "uint32_t",
    "float": "float", "fp32": "float",
    "bool": "bool", "boolean": "bool",
}

_VOFA_TYPE_TOKENS = set(_VOFA_TYPE_ALIASES)

# WAVEFORM frame flag bit 0: payload is little-endian Float32 values in
# sample-major order: sample0.channel0..N, sample1.channel0..N, ... .
VOFA_SAMPLE_MAJOR_FLOAT32 = WAVEFORM_SAMPLE_MAJOR_FLOAT32
VOFA_MAX_READ_BLOCK = 2048


@dataclass(frozen=True)
class VofaChannelRead:
    channel_index: int
    offset: int
    size: int
    type_name: str


@dataclass(frozen=True)
class VofaReadGroup:
    address: int
    size: int
    channels: tuple[VofaChannelRead, ...]


def encode_vofa_samples(samples) -> bytes:
    """Encode rows of numeric channel values as sample-major Float32."""
    rows = [tuple(row) for row in samples]
    if not rows:
        return b""
    channel_count = len(rows[0])
    if channel_count <= 0 or any(len(row) != channel_count for row in rows):
        raise ValueError("VOFA samples must have one consistent channel count")
    values = []
    for row in rows:
        for value in row:
            number = float(value)
            if not math.isfinite(number):
                number = 0.0
            values.append(number)
    return struct.pack(f"<{len(values)}f", *values)


def decode_vofa_samples(payload: bytes, channel_count: int) -> list[tuple[float, ...]]:
    """Decode a sample-major Float32 payload (used by tests/clients)."""
    if channel_count <= 0:
        raise ValueError("channel_count must be positive")
    row_size = channel_count * 4
    if len(payload) % row_size:
        raise ValueError("VOFA payload is not aligned to complete samples")
    values = struct.unpack(f"<{len(payload) // 4}f", payload) if payload else ()
    return [
        tuple(values[index:index + channel_count])
        for index in range(0, len(values), channel_count)
    ]


def build_vofa_read_groups(
    channels: list[dict], *, max_block_size: int = VOFA_MAX_READ_BLOCK,
) -> list[VofaReadGroup]:
    """Group channels into 4-byte-aligned, bounded target reads."""
    if max_block_size < 4:
        raise ValueError("max_block_size must allow one aligned word")
    channels = normalize_vofa_channels(channels)
    normalized = []
    for index, channel in enumerate(channels):
        address = channel["addr"]
        size = channel["size"]
        aligned_start = address & ~0x3
        aligned_end = (address + size + 3) & ~0x3
        if aligned_end - aligned_start > max_block_size:
            raise ValueError("VOFA channel size exceeds the safe read limit")
        normalized.append((
            aligned_start, aligned_end, address, size, index, channel["type"],
        ))
    normalized.sort(key=lambda item: (item[0], item[2]))
    groups = []
    pending = []
    group_start = group_end = None
    for aligned_start, aligned_end, address, size, index, type_name in normalized:
        can_join = (
            group_start is not None
            and aligned_start <= group_end
            and max(group_end, aligned_end) - group_start <= max_block_size
        )
        if not can_join and pending:
            groups.append(VofaReadGroup(
                group_start, group_end - group_start, tuple(pending),
            ))
            pending = []
            group_start = group_end = None
        if group_start is None:
            group_start, group_end = aligned_start, aligned_end
        else:
            group_end = max(group_end, aligned_end)
        pending.append(VofaChannelRead(index, address - group_start, size, type_name))
    if pending:
        groups.append(VofaReadGroup(
            group_start, group_end - group_start, tuple(pending),
        ))
    return groups


def normalize_vofa_type(type_token: str) -> dict[str, int | str] | None:
    """Normalize a VOFA input type alias to canonical C type metadata."""
    key = type_token.strip().lower()
    canonical = _VOFA_TYPE_ALIASES.get(key)
    if not canonical:
        return None
    info = VOFA_TYPE_INFO[canonical]
    return {"input": type_token, "type": info["type"], "size": info["size"]}


def normalize_vofa_channels(channels: list[dict]) -> list[dict]:
    """Validate and canonicalize one complete VOFA channel snapshot."""
    if not isinstance(channels, list) or not 1 <= len(channels) <= 64:
        raise ValueError("VOFA channel count must be between 1 and 64")
    normalized = []
    names = set()
    for index, channel in enumerate(channels):
        if not isinstance(channel, dict):
            raise ValueError(f"VOFA channel {index} must be an object")
        try:
            raw_address = channel["addr"]
            if isinstance(raw_address, bool):
                raise ValueError
            address = int(raw_address, 0) if isinstance(raw_address, str) else int(raw_address)
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"VOFA channel {index} has an invalid 32-bit address") from None
        type_token = str(channel.get("type", "float"))
        type_info = normalize_vofa_type(type_token)
        if type_info is None:
            raise ValueError(f"VOFA channel {index} uses unsupported type {type_token!r}")
        canonical_size = int(type_info["size"])
        try:
            size = int(channel.get("size", canonical_size))
        except (TypeError, ValueError):
            raise ValueError(f"VOFA channel {index} has an invalid size") from None
        if size != canonical_size:
            raise ValueError(
                f"VOFA channel {index} size {size} does not match "
                f"{type_info['type']} size {canonical_size}"
            )
        if address < 0 or address > 0xFFFFFFFF or address + size > 0x100000000:
            raise ValueError(f"VOFA channel {index} exceeds the 32-bit address space")
        default_name = f"0x{address:08x}"
        name = str(channel.get("name", default_name)).strip()
        if not name:
            raise ValueError(f"VOFA channel {index} name must not be empty")
        if name in names:
            raise ValueError("VOFA channel names must be unique")
        names.add(name)
        normalized.append({
            "name": name,
            "addr": address,
            "type": str(type_info["type"]),
            "size": canonical_size,
        })
    return normalized


# ---------------------------------------------------------------------------
# JustFloat frame parser
# ---------------------------------------------------------------------------

class JustFloatParser:
    """Parse VOFA+ JustFloat binary frames from a raw byte stream.

    Frame structure: [float_ch0][float_ch1]...[float_chN][0x00 0x00 0x80 0x7f]

    Uses channel count for frame-length validation and resync on corruption.
    """

    TAIL = b'\x00\x00\x80\x7f'

    def __init__(self, channel_count: int, channel_names: list[str] | None = None):
        self._buf = bytearray()
        self._channel_count = channel_count
        self._expected_frame_bytes = channel_count * 4  # payload only (no tail)
        self._channel_names = channel_names or [f"ch{i}" for i in range(channel_count)]
        self._dropped_bytes = 0
        self._dropped_frames = 0

    @property
    def dropped_bytes(self) -> int:
        return self._dropped_bytes

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    def feed(self, data: bytes) -> list[dict]:
        """Feed raw bytes, return list of parsed frame dicts.

        Each frame: {"ch0": v0, "ch1": v1, ..., "_t": timestamp}

        Strategy: check expected frame position first (handles +Inf in
        channel data), then fall back to TAIL search for resync.
        """
        self._buf.extend(data)
        frames: list[dict] = []
        frame_size = self._expected_frame_bytes + 4  # payload + TAIL

        while len(self._buf) >= frame_size and self._channel_count > 0:
            # 1. Fast path: check if TAIL is at the expected position
            tail_pos = self._expected_frame_bytes
            if self._buf[tail_pos:tail_pos + 4] == self.TAIL:
                payload = bytes(self._buf[:tail_pos])
                point: dict[str, float] = {"_t": time.time()}
                for i in range(self._channel_count):
                    raw = payload[i * 4:(i + 1) * 4]
                    val = struct.unpack('<f', raw)[0]
                    point[self._channel_names[i]] = val
                frames.append(point)
                del self._buf[:frame_size]
                continue

            # 2. Slow path: TAIL not at expected position — search for it
            idx = self._buf.find(self.TAIL)
            if idx < 0:
                break  # no TAIL found at all, wait for more data

            # Check if there's a valid payload right before this TAIL
            payload_start = idx - self._expected_frame_bytes
            if payload_start >= 0:
                payload = bytes(self._buf[payload_start:idx])
                point: dict[str, float] = {"_t": time.time()}
                for i in range(self._channel_count):
                    raw = payload[i * 4:(i + 1) * 4]
                    val = struct.unpack('<f', raw)[0]
                    point[self._channel_names[i]] = val
                frames.append(point)
                if payload_start > 0:
                    self._dropped_bytes += payload_start
                    self._dropped_frames += 1
                del self._buf[:idx + 4]
            else:
                # Not enough data before TAIL — discard it
                self._dropped_bytes += idx + 4
                self._dropped_frames += 1
                del self._buf[:idx + 4]

        # Handle 0-channel case (just TAIL markers, no meaningful data)
        if self._channel_count == 0:
            idx = self._buf.find(self.TAIL)
            while idx >= 0:
                del self._buf[:idx + 4]
                idx = self._buf.find(self.TAIL)

        return frames


# ---------------------------------------------------------------------------
# Convenience runner — used by cli.py
# ---------------------------------------------------------------------------

VOFA_MAX_FAST_CHANNELS = 16
VOFA_MAX_PRECISE_CHANNELS = 15
VOFA_MAX_REPL_COMMAND_BYTES = 511


def _vofa_address_literal(token: str, channel_index: int) -> str:
    """Validate and canonicalize one 32-bit address before building code."""
    value = str(token).strip()
    try:
        address = int(value, 0)
    except (TypeError, ValueError):
        raise ValueError(
            f"VOFA channel {channel_index} has an invalid 32-bit address"
        ) from None
    if not 0 <= address <= 0xFFFFFFFF:
        raise ValueError(
            f"VOFA channel {channel_index} has an invalid 32-bit address"
        )
    return f"0x{address:08X}"


def validate_vofa_repl_command(command: str) -> None:
    """Reject a VOFA command that cannot fit in the Pika REPL line buffer."""
    command_bytes = len(command.encode("utf-8"))
    if command_bytes > VOFA_MAX_REPL_COMMAND_BYTES:
        raise ValueError(
            f"VOFA command is {command_bytes} UTF-8 bytes; the safe Pika REPL "
            f"limit is {VOFA_MAX_REPL_COMMAND_BYTES} bytes"
        )


def build_vofa_command(
    variables: list[str],
    period: float | int,
) -> tuple[str, str, int, str]:
    """Validate and build one ``vofa.send`` command without touching hardware.

    Fast mode uses three Pika arguments regardless of channel count, but the
    firmware layout is independently limited to 16 contiguous float channels.
    Precise mode uses two Pika arguments per channel plus ``period``; keep it at
    15 channels so the known-unsafe 33-argument boundary is never reached.
    """
    import json

    values = [str(value).strip() for value in variables]
    if not values or any(not value for value in values):
        raise ValueError("VOFA variables must not be empty")

    try:
        numeric_second = int(values[1], 10) if len(values) == 2 else None
    except ValueError:
        numeric_second = None

    fast_mode = numeric_second is not None
    if fast_mode:
        channel_count = numeric_second
        if not 1 <= channel_count <= VOFA_MAX_FAST_CHANNELS:
            raise ValueError(
                "VOFA fast-mode channel count must be between 1 and "
                f"{VOFA_MAX_FAST_CHANNELS}"
            )
        address_literals = [_vofa_address_literal(values[0], 0)]
        argument_literals = [address_literals[0], str(channel_count)]
        mode = "fast"
    else:
        if len(values) % 2:
            raise ValueError(
                "VOFA precise mode requires complete address/type pairs"
            )
        channel_count = len(values) // 2
        if not 1 <= channel_count <= VOFA_MAX_PRECISE_CHANNELS:
            raise ValueError(
                "VOFA precise mode supports at most "
                f"{VOFA_MAX_PRECISE_CHANNELS} address/type pairs; 16 pairs "
                "reach the unsafe Pika argument boundary"
            )
        argument_literals = []
        for channel_index in range(channel_count):
            address_token = values[channel_index * 2]
            type_token = values[channel_index * 2 + 1]
            type_info = normalize_vofa_type(type_token)
            if type_info is None:
                raise ValueError(
                    f"VOFA channel {channel_index} uses unsupported type "
                    f"{type_token!r}"
                )
            argument_literals.extend((
                _vofa_address_literal(address_token, channel_index),
                json.dumps(str(type_info["type"])),
            ))
        mode = "precise"

    try:
        normalized_period = float(period)
    except (TypeError, ValueError):
        raise ValueError("VOFA period must be a finite non-negative number") from None
    if not math.isfinite(normalized_period) or normalized_period < 0:
        raise ValueError("VOFA period must be a finite non-negative number")

    var_args = ", ".join(argument_literals)
    command = f"vofa.send({var_args}, {period})"
    validate_vofa_repl_command(command)
    return command, var_args, channel_count, mode

def _infer_channel_count(variables: list[str]) -> int:
    """Infer channel count from CLI variable arguments.

    Quick mode:   vofa <addr> <count>          -> count
    Precise mode: vofa <addr1> <type1> <addr2> ... -> len / 2
    """
    if len(variables) == 2 and variables[1].isdigit():
        return int(variables[1])
    return len(variables) // 2


def _infer_channel_names(variables: list[str], channel_count: int) -> list[str]:
    """Infer channel names from CLI variable arguments.

    Quick mode:   vofa <addr> <count>          -> addr+0, addr+4, ...
    Precise mode: vofa <addr1> <type1> <addr2> ... -> addr1, addr2, ...
    """
    if len(variables) == 2 and variables[1].isdigit():
        base = int(variables[0], 16)
        return [f"0x{base + i * 4:08x}" for i in range(channel_count)]
    # Precise mode: extract addresses (every other element)
    return [variables[i] for i in range(0, len(variables), 2)][:channel_count]


def infer_channel_metadata(
    variables: list[str],
    channel_names: list[str] | None = None,
) -> dict[str, dict[str, int | str]]:
    """Infer per-channel type/size metadata from VOFA CLI variables."""
    channel_count = _infer_channel_count(variables)
    names = channel_names or _infer_channel_names(variables, channel_count)
    metadata: dict[str, dict[str, int | str]] = {}

    if len(variables) == 2 and variables[1].isdigit():
        for name in names[:channel_count]:
            metadata[name] = {"type": "float", "size": 4, "unit": ""}
        return metadata

    for idx, name in enumerate(names[:channel_count]):
        type_index = idx * 2 + 1
        type_token = variables[type_index] if type_index < len(variables) else ""
        info = normalize_vofa_type(type_token)
        if info:
            metadata[name] = {"type": str(info["type"]), "size": int(info["size"]), "unit": ""}
        else:
            metadata[name] = {"type": type_token or "-", "size": "", "unit": ""}
    return metadata


def infer_channel_metadata_from_dwarf(
    dwarf_info,
    variables: list[str],
    channel_names: list[str],
    original_variables: list[str] | None = None,
) -> dict[str, dict]:
    """Infer channel metadata from VOFA variables and DWARF enum/type records."""
    metadata: dict[str, dict] = {}
    source_variables = original_variables or variables
    try:
        from mklink.watch import resolve_variable_path
    except Exception:
        resolve_variable_path = None

    for idx, name in enumerate(channel_names):
        meta: dict = {}
        address_index = idx * 2
        type_index = idx * 2 + 1
        if (
            resolve_variable_path is not None
            and address_index < len(source_variables)
        ):
            var_name = source_variables[address_index]
            if (
                isinstance(var_name, str)
                and var_name
                and not var_name.startswith(("0x", "0X"))
            ):
                try:
                    _addr, type_name, size, enum_values = resolve_variable_path(dwarf_info, var_name)
                    meta = {"type": type_name, "size": size, "unit": ""}
                    if enum_values:
                        meta["enumValues"] = {str(k): v for k, v in enum_values.items()}
                    metadata[name] = meta
                    continue
                except Exception:
                    pass

        if type_index >= len(variables):
            continue
        type_name = variables[type_index]
        meta = {"type": type_name, "unit": ""}
        enum_info = getattr(dwarf_info, "enums", {}).get(type_name)
        if enum_info is None:
            var_key = str(source_variables[address_index] if address_index < len(source_variables) else name)
            var_key = var_key.replace("_", "").lower()
            for enum_name, candidate in getattr(dwarf_info, "enums", {}).items():
                enum_key = str(enum_name).replace("_", "").lower()
                if (
                    var_key == enum_key
                    and
                    getattr(candidate, "size", 0) in (0, 1)
                    and normalize_vofa_type(type_name)
                    and int(normalize_vofa_type(type_name)["size"]) == 1
                ):
                    enum_info = candidate
                    type_name = enum_name
                    break
        if enum_info is not None:
            meta["type"] = type_name
            type_info = normalize_vofa_type(variables[type_index]) if type_index < len(variables) else None
            meta["size"] = getattr(enum_info, "size", 0) or (int(type_info["size"]) if type_info else 4)
            meta["enumValues"] = {
                str(k): v for k, v in getattr(enum_info, "values", {}).items()
            }
        else:
            type_info = normalize_vofa_type(type_name)
            if type_info:
                meta["type"] = str(type_info["type"])
                meta["size"] = int(type_info["size"])
            else:
                meta["size"] = ""
        metadata[name] = meta
    return metadata


def resolve_variable_names(
    variables: list[str],
    elf_path: str | None = None,
    *,
    backend: str | None = None,
    project_root: str | None = None,
) -> list[str]:
    """Resolve symbolic variable names to addresses using symbol_parser.

    If a variable token is not a hex address (doesn't start with '0x'), attempt
    to resolve it as a symbol name from the ELF file. Returns the variable list
    with names replaced by their hex addresses where resolved.

    Args:
        variables: Original variable args from CLI
        elf_path: Path to ELF/AXF file for symbol lookup

    Returns:
        Updated variables list with resolved addresses
    """
    if not elf_path:
        return variables

    from mklink.symbol_parser import resolve_symbol_names, suggest_similar_symbols

    # Check if any address-position token looks like a name. In precise mode
    # the odd tokens are type names, so do not try to resolve them.
    if len(variables) == 2 and variables[1].isdigit():
        candidate_indices = [0]
    else:
        candidate_indices = list(range(0, len(variables), 2))
    name_like = [
        variables[i] for i in candidate_indices
        if i < len(variables)
        and not variables[i].startswith("0x") and not variables[i].startswith("0X")
        and not variables[i].replace(".", "").replace("-", "").isdigit()
        and variables[i].lower() not in _VOFA_TYPE_TOKENS
    ]

    if not name_like:
        return variables

    # Resolve struct.field paths with DWARF first.
    resolved_vars = list(variables)
    dotted_names = [v for v in name_like if "." in v]
    if dotted_names:
        try:
            from mklink.dwarf_parser import load_dwarf_info
            from mklink.watch import resolve_variable_path
            dwarf_info = load_dwarf_info(
                elf_path, backend=backend, project_root=project_root
            )
            for dotted in dotted_names:
                try:
                    addr, type_name, _size, _enum_values = resolve_variable_path(dwarf_info, dotted)
                    for i, token in enumerate(resolved_vars):
                        if token == dotted:
                            resolved_vars[i] = f"0x{addr:08X}"
                            if i + 1 < len(resolved_vars) and not resolved_vars[i + 1].startswith("0x"):
                                resolved_vars[i + 1] = type_name
                except Exception as e:
                    print(f"[WARN] Field path '{dotted}' not resolved: {e}")
        except Exception as e:
            print(f"[WARN] DWARF field resolution unavailable: {e}")

    variables = resolved_vars
    name_like = [
        variables[i] for i in candidate_indices
        if i < len(variables)
        and not variables[i].startswith("0x") and not variables[i].startswith("0X")
        and not variables[i].replace(".", "").replace("-", "").isdigit()
        and variables[i].lower() not in _VOFA_TYPE_TOKENS
    ]
    if not name_like:
        return variables

    try:
        from mklink.elf_backend import list_writable_object_symbols

        normalized = list_writable_object_symbols(
            elf_path, backend=backend, project_root=project_root
        )
    except Exception:
        return variables
    symbols = [
        {
            "name": symbol.name,
            "address": f"0x{symbol.address:08x}",
            "type": "OBJECT",
            "size": symbol.size,
        }
        for symbol in normalized
    ]
    if not symbols:
        return variables

    # Resolve names
    resolved = resolve_symbol_names(symbols, name_like)
    name_to_addr = {r["name"]: r["address"] for r in resolved}

    # Report unresolved
    found_names = set(name_to_addr.keys())
    for name in name_like:
        if name not in found_names:
            suggestion = suggest_similar_symbols(symbols, name, max_suggestions=3)
            if suggestion["suggestions"]:
                print(f"[WARN] Symbol '{name}' not found. Similar: {', '.join(suggestion['suggestions'])}")
            else:
                print(f"[WARN] Symbol '{name}' not found in ELF")

    # Replace names with addresses
    return [name_to_addr.get(v, v) for v in variables]


def run_vofa_visualizer(
    bridge,          # MKLinkSerialBridge (in VOFA_STREAM state)
    *,
    variables: list[str],
    var_args: str,   # original vofa.send() args string for stop command
    period: float,
    channel_count: int | None = None,
    channel_names: list[str] | None = None,
    duration: float = 30.0,
    host: str = "127.0.0.1",
    port: int = 0,
    no_browser: bool = False,
    max_points: int = 500,
    source: str | None = None,
    original_variables: list[str] | None = None,
    backend: str | None = None,
    project_root: str | None = None,
) -> None:
    """Run the full VOFA+ visualization pipeline."""
    from mklink.rtt_viewer import VisualizationServer

    _command, var_args, validated_channel_count, _mode = build_vofa_command(
        variables, period,
    )
    if channel_count is None:
        channel_count = validated_channel_count

    if channel_count <= 0:
        print("[FAIL] 无法推断通道数，请检查变量参数")
        return

    if channel_names is None:
        channel_names = _infer_channel_names(variables, channel_count)

    parser = JustFloatParser(channel_count, channel_names=channel_names)
    stop_event = threading.Event()
    _stopped = threading.Event()
    _reconfiguring = threading.Event()  # set during interval change

    # Build channel info string for title
    ch_info = ", ".join(channel_names)
    channel_metadata = infer_channel_metadata(variables, channel_names)
    if source:
        try:
            from mklink.dwarf_parser import load_dwarf_info
            dwarf_info = load_dwarf_info(
                source, backend=backend, project_root=project_root
            )
            dwarf_metadata = infer_channel_metadata_from_dwarf(
                dwarf_info,
                variables,
                channel_names,
                original_variables=original_variables,
            )
            for name, meta in dwarf_metadata.items():
                channel_metadata[name] = {**channel_metadata.get(name, {}), **meta}
        except Exception as e:
            print(f"[WARN] DWARF channel metadata unavailable: {e}")

    # Start HTTP server (reuse RTT VisualizationServer with VOFA branding)
    server = VisualizationServer(
        host=host, port=port, max_points=max_points,
        title="MKLink VOFA Viewer",
        mode="VOFA",
        channel_metadata=channel_metadata,
    )
    server._interval = period  # initialize with CLI period
    actual_port = server.start()

    url = f"http://{host}:{actual_port}"
    print(f"[OK] VOFA Viewer 已启动: {url}")
    if not no_browser:
        print(f"[*] 正在打开浏览器...")
        webbrowser.open(url)

    # Parser thread — reads binary bytes from bridge, parses JustFloat frames
    frame_count = 0

    def _parser_loop():
        nonlocal frame_count
        while not stop_event.is_set():
            # Wait during reconfiguration (interval change)
            if _reconfiguring.is_set():
                time.sleep(0.05)
                continue
            try:
                raw = bridge.drain_stream_bytes()
            except Exception:
                break
            if not raw:
                time.sleep(0.01)
                continue

            frames = parser.feed(raw)  # always feed to keep frame sync
            if not server.collecting.is_set():
                continue  # discard parsed results when paused
            for point in frames:
                server.push_data_point(point)
                frame_count += 1

    parser_thread = threading.Thread(target=_parser_loop, daemon=True)
    parser_thread.start()

    # Wire up web-control callbacks
    from mklink._types import DeviceState

    def _handle_interval_change(new_period: float):
        restart_command = f"vofa.send({var_args}, {new_period})"
        validate_vofa_repl_command(restart_command)
        was_collecting = server.collecting.is_set()
        server.collecting.clear()
        _reconfiguring.set()

        time.sleep(0.05)
        try:
            remaining = bridge.drain_stream_bytes()
            if remaining:
                parser.feed(remaining)
        except Exception:
            pass
        # Reset parser buffer to avoid half-frame issues
        parser._buf.clear()

        bridge._exit_stream()
        try:
            bridge._write_raw(f'vofa.send({var_args}, 0)\n'.encode("utf-8"))
            time.sleep(0.1)
            bridge._enter_stream(DeviceState.VOFA_STREAM)
            bridge._write_raw((restart_command + "\n").encode("utf-8"))
            print(f"[OK] VOFA interval changed to {new_period}s")
        except Exception as e:
            print(f"[WARN] Failed to change interval: {e}")
            try:
                bridge._enter_stream(DeviceState.VOFA_STREAM)
            except Exception:
                pass
        finally:
            _reconfiguring.clear()
            if was_collecting:
                server.collecting.set()

    server._on_interval_change = _handle_interval_change
    server._on_stop_requested = lambda: stop_event.set()

    # Idempotent cleanup
    def _cleanup():
        if _stopped.is_set():
            return
        _stopped.set()
        stop_event.set()
        if parser_thread and parser_thread.is_alive():
            parser_thread.join(timeout=2.0)
        server.stop()
        try:
            bridge._exit_stream()
            bridge.send_command(f'vofa.send({var_args}, 0)', timeout=5.0)
        except Exception:
            pass
        bridge.close()

    atexit.register(_cleanup)

    if sys.platform == "win32":
        def _sigbreak_handler(signum, frame):
            _cleanup()
            sys.exit(1)
        signal.signal(signal.SIGBREAK, _sigbreak_handler)
    else:
        def _sigterm_handler(signum, frame):
            _cleanup()
            sys.exit(0)
        signal.signal(signal.SIGTERM, _sigterm_handler)

    # Wait for stop signal
    print(f"[*] VOFA 可视化运行中（{channel_count} 通道），按 Ctrl+C 停止...\n")
    try:
        start_time = time.time()
        while time.time() - start_time < duration:
            if stop_event.is_set():
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] 用户中断")

    # Orderly shutdown
    print("[*] 正在停止...")
    _cleanup()
    drop_info = ""
    if parser.dropped_frames > 0:
        drop_info = f"，丢弃 {parser.dropped_frames} 帧损坏数据"
    print(f"[OK] VOFA Viewer 已关闭 (解析 {frame_count} 帧{drop_info})")


# Backward-compatible aliases for contract compliance
batch_read_variables = resolve_variable_names
