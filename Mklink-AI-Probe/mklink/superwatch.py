"""SuperWatch polling visualizer support.

SuperWatch reads typed variables and registers with cmd.read_ram. Unlike RTT
and VOFA, the X axis comes from the device-side timestamp emitted as a prefix
in each read_ram response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import subprocess
import struct
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET

from mklink.memory_access import parse_read_ram_response, read_memory
from mklink.watch import decode_value, resolve_variable_path


_TIMESTAMP_RE = re.compile(
    r"(?:timestamp(?:_us)?|time(?:_us)?|ts)\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)",
    re.IGNORECASE,
)
_TIMESTAMP_HEADER_RE = re.compile(
    r"^\s*([0-9a-fA-F]{8})\s+00\s+01\s+02\s+03\s+04\s+05\s+06\s+07\s+08\s+09\s+0A\s+0B\s+0C\s+0D\s+0E\s+0F\s*$",
    re.IGNORECASE,
)
_DUMP_ROW_RE = re.compile(r"^\s*([0-9a-fA-F]{8})\s+((?:[0-9a-fA-F]{2}\s+){0,15}[0-9a-fA-F]{2})\b")


@dataclass
class TimestampedRead:
    timestamp_us: int
    data: bytes
    raw: str


@dataclass
class WatchItem:
    name: str
    address: int
    type_name: str
    size: int
    source: str = "ram"
    enum_values: dict[int, str] | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ReadBlock:
    address: int
    size: int
    items: list[WatchItem]


@dataclass
class SampleResult:
    points: list[dict]
    origin_us: int


@dataclass
class SvdRegister:
    name: str
    address: int
    width: int = 32
    description: str = ""
    fields: list[dict] = field(default_factory=list)


def parse_timestamped_read_ram_response(response: str) -> TimestampedRead:
    match = _TIMESTAMP_RE.search(response)
    timestamp_us = int(match.group(1), 0) if match else None
    data = bytearray()
    for line in response.splitlines():
        header = _TIMESTAMP_HEADER_RE.match(line)
        if header:
            if timestamp_us is None:
                timestamp_us = int(header.group(1), 16)
            continue
        row = _DUMP_ROW_RE.match(line)
        if not row:
            continue
        for token in row.group(2).split():
            data.append(int(token, 16))
    if timestamp_us is None:
        timestamp_us = int(time.time() * 1_000_000)
    return TimestampedRead(
        timestamp_us=timestamp_us,
        data=bytes(data) if data else parse_read_ram_response(response),
        raw=response,
    )


def build_read_blocks(items: list[WatchItem], *, max_gap: int = 16) -> list[ReadBlock]:
    if not items:
        return []
    sorted_items = sorted(items, key=lambda i: (i.source != "ram", i.address))
    blocks: list[ReadBlock] = []
    current_items: list[WatchItem] = []
    current_start = 0
    current_end = 0
    current_source = ""

    def flush() -> None:
        nonlocal current_items, current_start, current_end, current_source
        if current_items:
            blocks.append(ReadBlock(current_start, current_end - current_start, current_items))
        current_items = []
        current_start = 0
        current_end = 0
        current_source = ""

    for item in sorted_items:
        item_end = item.address + max(1, item.size)
        if (
            current_items
            and item.source == current_source == "ram"
            and item.address <= current_end + max_gap
        ):
            current_items.append(item)
            current_end = max(current_end, item_end)
            continue
        flush()
        current_items = [item]
        current_start = item.address
        current_end = item_end
        current_source = item.source
    flush()
    return blocks


def _read_block_via_bridge(bridge, address: int, size: int, *, timeout: float = 10.0) -> tuple[bytes, str]:
    """Read a memory block using an existing bridge connection."""
    addr_s = f"0x{address:08X}"
    cmd = f"cmd.read_ram({addr_s}, {size})"
    raw = bridge.send_command(cmd, timeout=timeout)
    return parse_read_ram_response(raw), raw


def sample_blocks(
    blocks: list[ReadBlock],
    *,
    port: str | None = None,
    read_func=read_memory,
    origin_us: int | None = None,
    bridge=None,
) -> SampleResult:
    points: list[dict] = []
    current_origin = origin_us
    for block in blocks:
        if bridge is not None:
            _data, raw = _read_block_via_bridge(bridge, block.address, block.size)
        else:
            _data, raw = read_func(port, block.address, block.size)
        parsed = parse_timestamped_read_ram_response(raw)
        if current_origin is None:
            current_origin = parsed.timestamp_us
        point: dict = {
            "_t": (parsed.timestamp_us - current_origin) / 1_000_000.0,
            "timestamp_us": parsed.timestamp_us,
        }
        for item in block.items:
            offset = item.address - block.address
            data = parsed.data[offset:offset + item.size]
            point[item.name] = decode_value(data, item.type_name, item.enum_values, known_size=item.size)
        points.append(point)
    return SampleResult(points=points, origin_us=current_origin or 0)


def poll_blocks(
    blocks: list[ReadBlock],
    *,
    port: str | None = None,
    duration: float = 0.0,
    period: float = 0.1,
    read_func=read_memory,
    clock=time.time,
    sleep_func=time.sleep,
) -> list[dict]:
    points: list[dict] = []
    origin_us: int | None = None
    start = clock()
    while True:
        result = sample_blocks(blocks, port=port, read_func=read_func, origin_us=origin_us)
        origin_us = result.origin_us
        points.extend(result.points)
        if duration > 0 and clock() - start >= duration:
            break
        sleep_func(max(0.0, period))
    return points


def resolve_watch_items(
    names: list[str],
    *,
    source: str | None = None,
    dwarf_info=None,
    svd_registers: dict[str, SvdRegister] | None = None,
) -> list[WatchItem]:
    if dwarf_info is None and source:
        from mklink.dwarf_parser import load_dwarf_info

        dwarf_info = load_dwarf_info(source)
    svd_registers = svd_registers or {}
    symbol_sizes = _symbol_size_lookup(source) if source else {}
    items: list[WatchItem] = []
    for raw_name in _normalize_names(names):
        reg_key = raw_name.upper().replace("->", ".")
        svd_match = next((r for k, r in svd_registers.items() if k.upper() == reg_key), None)
        if svd_match:
            items.append(
                WatchItem(
                    name=svd_match.name,
                    address=svd_match.address,
                    type_name="uint32_t",
                    size=max(1, svd_match.width // 8),
                    source="register",
                    metadata={"fields": svd_match.fields, "description": svd_match.description},
                )
            )
            continue
        if dwarf_info is not None:
            address, type_name, size, enum_values = resolve_variable_path(dwarf_info, raw_name)
            if size <= 0 or type_name == "unknown":
                size = int(symbol_sizes.get(raw_name, 0) or 4)
                type_name = {1: "uint8_t", 2: "uint16_t", 4: "uint32_t", 8: "uint64_t"}.get(size, "uint32_t")
            # Warn and skip variables in Flash region (0x00000000-0x1FFFFFFF)
            # ARM Cortex-M SRAM is typically at 0x20000000+
            if 0 <= address < 0x20000000:
                print(f"[WARN] Skipping '{raw_name}': address 0x{address:08X} is outside SRAM region")
                continue
            items.append(
                WatchItem(
                    name=raw_name,
                    address=address,
                    type_name=type_name,
                    size=size,
                    source="ram",
                    enum_values=enum_values,
                )
            )
            continue
        from mklink.registers import resolve_register

        reg = resolve_register(raw_name)
        items.append(
            WatchItem(
                name=reg.name,
                address=reg.address,
                type_name=f"uint{reg.width}_t",
                size=max(1, reg.width // 8),
                source="register",
                metadata={"description": reg.description},
            )
        )
    return items


def _symbol_size_lookup(source: str) -> dict[str, int]:
    try:
        result = subprocess.run(
            ["arm-none-eabi-readelf", "-s", source],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    sizes: dict[str, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            size = int(parts[2], 0)
        except ValueError:
            continue
        name = parts[-1]
        if size > 0:
            sizes[name] = size
    return sizes


def _normalize_names(names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        for part in str(name).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def load_svd_registers(path: str) -> dict[str, SvdRegister]:
    tree = ET.parse(path)
    root = tree.getroot()
    regs: dict[str, SvdRegister] = {}
    for peripheral in root.findall(".//peripheral"):
        pname = _xml_text(peripheral, "name")
        base = int(_xml_text(peripheral, "baseAddress", "0"), 0)
        for reg in peripheral.findall("./registers/register"):
            rname = _xml_text(reg, "name")
            offset = int(_xml_text(reg, "addressOffset", "0"), 0)
            width = int(_xml_text(reg, "size", "32"), 0)
            full_name = f"{pname}.{rname}"
            fields = []
            for field_el in reg.findall("./fields/field"):
                fields.append(
                    {
                        "name": _xml_text(field_el, "name"),
                        "bit_offset": int(_xml_text(field_el, "bitOffset", "0"), 0),
                        "bit_width": int(_xml_text(field_el, "bitWidth", "1"), 0),
                    }
                )
            regs[full_name] = SvdRegister(
                name=full_name,
                address=base + offset,
                width=width,
                description=_xml_text(reg, "description", ""),
                fields=fields,
            )
    return regs


def _xml_text(parent: ET.Element, tag: str, default: str = "") -> str:
    el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else default


def find_project_svd(project_root: str = ".") -> str | None:
    candidates: list[str] = []
    keil_json = os.path.join(project_root, ".mklink", "keil_project.json")
    if os.path.isfile(keil_json):
        try:
            info = json.loads(open(keil_json, "r", encoding="utf-8").read())
            pack_id = str(info.get("pack_id", ""))
            device = str(info.get("device", ""))
            flm_path = str(info.get("flm_path", ""))
            if flm_path:
                pack_root = os.path.dirname(os.path.dirname(flm_path))
                candidates.extend(_find_svd_files(pack_root, device))
            if pack_id:
                candidates.extend(_find_svd_files(os.path.expanduser("~/AppData/Local/Arm/Packs"), device))
        except Exception:
            pass
    candidates.extend(_find_svd_files(project_root, ""))
    return candidates[0] if candidates else None


def _find_svd_files(root: str, device: str) -> list[str]:
    if not root or not os.path.isdir(root):
        return []
    found: list[str] = []
    device_key = device.lower()
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if not filename.lower().endswith(".svd"):
                continue
            path = os.path.join(dirpath, filename)
            if not device_key or device_key in filename.lower() or "svd" in dirpath.lower():
                found.append(path)
    return found


def build_inspector_tree(name: str, type_name: str, data: bytes, dwarf_info) -> dict:
    return _build_node(name, type_name, data, dwarf_info, depth=0)


def _build_node(name: str, type_name: str, data: bytes, dwarf_info, *, depth: int) -> dict:
    if depth > 8:
        return {"name": name, "type": type_name, "truncated": True}
    if type_name in getattr(dwarf_info, "structs", {}):
        st = dwarf_info.structs[type_name]
        return {
            "name": name,
            "type": type_name,
            "kind": "struct",
            "size": st.size,
            "children": [
                _member_node(member, data, dwarf_info, depth=depth + 1)
                for member in st.members
                if member.name
            ],
        }
    if type_name.endswith("[]"):
        elem_type = type_name[:-2]
        elem_size = _sizeof_type(elem_type, dwarf_info) or 1
        return {
            "name": name,
            "type": type_name,
            "kind": "array",
            "children": [
                _build_node(f"[{i}]", elem_type, data[i:i + elem_size], dwarf_info, depth=depth + 1)
                for i in range(0, len(data), elem_size)
            ],
        }
    return {"name": name, "type": type_name, "kind": "value", "value": decode_value(data, type_name, known_size=len(data))}


def _member_node(member, parent_data: bytes, dwarf_info, *, depth: int) -> dict:
    chunk = parent_data[member.offset:member.offset + member.size]
    if member.bit_size is not None:
        raw = int.from_bytes(chunk or b"\x00", "little")
        mask = (1 << member.bit_size) - 1
        value = (raw >> (member.bit_offset or 0)) & mask
        return {
            "name": member.name,
            "type": member.type_name,
            "kind": "bitfield",
            "bit_offset": member.bit_offset or 0,
            "bit_size": member.bit_size,
            "value": bool(value) if member.bit_size == 1 else value,
        }
    return _build_node(member.name, member.type_name, chunk, dwarf_info, depth=depth)


def _sizeof_type(type_name: str, dwarf_info) -> int:
    from mklink.watch import TYPE_FORMATS

    fmt = TYPE_FORMATS.get(type_name.lower())
    if fmt:
        return int(fmt[1])
    if type_name in getattr(dwarf_info, "structs", {}):
        return int(dwarf_info.structs[type_name].size)
    return 0


def make_channel_metadata(items: list[WatchItem]) -> dict[str, dict]:
    metadata: dict[str, dict] = {}
    for item in items:
        meta = {
            "type": item.type_name,
            "size": item.size,
            "unit": "",
            "source": item.source,
            "address": f"0x{item.address:08X}",
        }
        if item.enum_values:
            meta["enumValues"] = {str(k): v for k, v in item.enum_values.items()}
        meta.update(item.metadata)
        metadata[item.name] = meta
    return metadata


class SuperWatchRuntime:
    def __init__(
        self,
        *,
        items: list[WatchItem],
        dwarf_info=None,
        svd_registers: dict[str, SvdRegister] | None = None,
        port: str | None = None,
        read_lock=None,
    ):
        self.items = list(items)
        self.dwarf_info = dwarf_info
        self.svd_registers = svd_registers or {}
        self.port = port
        self.read_lock = read_lock or threading.Lock()
        self.blocks = build_read_blocks(self.items, max_gap=256)
        self.blocks_version = 0

    def search(self, query: str) -> list[dict]:
        q = query.strip().lower()
        results: list[dict] = []
        if self.dwarf_info is not None:
            for name, var in sorted(getattr(self.dwarf_info, "variables", {}).items()):
                if q and q not in name.lower():
                    continue
                # Skip variables outside SRAM (Flash-mapped addresses)
                addr = getattr(var, "address", None)
                if addr is not None and 0 <= addr < 0x20000000:
                    continue
                results.append({
                    "name": name,
                    "kind": "variable",
                    "type": var.type_name,
                    "size": var.size,
                })
                if len(results) >= 50:
                    break
        for name, reg in sorted(self.svd_registers.items()):
            if q and q not in name.lower():
                continue
            results.append({
                "name": name,
                "kind": "register",
                "type": f"uint{reg.width}_t",
                "size": max(1, reg.width // 8),
            })
            if len(results) >= 50:
                break
        return results

    def add(self, name: str) -> dict:
        existing = next((item for item in self.items if item.name == name), None)
        if existing is not None:
            return {"name": existing.name, **make_channel_metadata([existing])[existing.name]}
        try:
            resolved = resolve_watch_items(
                [name],
                dwarf_info=self.dwarf_info,
                svd_registers=self.svd_registers,
            )
        except (KeyError, ValueError) as exc:
            return {"error": f"Cannot resolve '{name}': {exc}"}
        if not resolved:
            return {"error": f"Cannot resolve '{name}': skipped (address outside SRAM or not found)"}
        item = resolved[0]
        self.items.append(item)
        self.blocks = build_read_blocks(self.items, max_gap=256)
        self.blocks_version += 1
        return {"name": item.name, **make_channel_metadata([item])[item.name]}

    def remove(self, name: str) -> dict:
        before = len(self.items)
        self.items = [item for item in self.items if item.name != name]
        if len(self.items) == before:
            return {"removed": False, "name": name}
        self.blocks = build_read_blocks(self.items, max_gap=256)
        self.blocks_version += 1
        return {"removed": True, "name": name}

    def inspect(self, name: str) -> dict | None:
        item = next((candidate for candidate in self.items if candidate.name == name), None)
        if item is None or self.dwarf_info is None:
            return None
        with self.read_lock:
            data, raw = read_memory(self.port, item.address, item.size)
        parsed = parse_timestamped_read_ram_response(raw)
        return build_inspector_tree(item.name, item.type_name, parsed.data[:item.size] or data, self.dwarf_info)

    def memory_read(self, addr: int, size: int) -> dict:
        import base64
        with self.read_lock:
            data, raw = read_memory(self.port, addr, size)
        parsed = parse_timestamped_read_ram_response(raw)
        ts = parsed.timestamp_us if parsed.timestamp_us else int(time.time() * 1e6)
        payload = parsed.data if parsed.data else data
        return {
            "addr": f"0x{addr:08X}",
            "size": len(payload),
            "data": base64.b64encode(payload).decode("ascii"),
            "timestamp_us": ts,
        }

    def memory_write(self, addr: int, value: int, width: int) -> dict:
        from mklink.bridge import MKLinkSerialBridge
        from mklink.cli import _resolve_port
        resolved_port = _resolve_port(self.port)
        bridge = MKLinkSerialBridge(resolved_port)
        if not bridge.connect():
            return {"error": "Bridge connect failed"}
        try:
            cmd = f"cmd.write_ram(0x{addr:08X}, 0x{value:0{width*2}X}, {width})"
            with self.read_lock:
                response = bridge.send_command(cmd, timeout=5.0)
            if "error" in response.lower() or "fail" in response.lower():
                return {"error": response.strip()}
            return {"status": "ok"}
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            bridge.close()

    def memory_symbols(self, query: str) -> list[dict]:
        q = query.strip().lower()
        results: list[dict] = []
        if self.dwarf_info is not None:
            for name, var in sorted(getattr(self.dwarf_info, "variables", {}).items()):
                if q and q not in name.lower():
                    continue
                addr = getattr(var, "address", None)
                if addr is None:
                    continue
                results.append({
                    "name": name,
                    "addr": f"0x{addr:08X}",
                    "size": var.size,
                    "type": var.type_name,
                })
                if len(results) >= 30:
                    break
        return results


def run_superwatch_visualizer(
    *,
    items: list[WatchItem],
    period: float = 0.1,
    port: str | None = None,
    host: str = "127.0.0.1",
    port_http: int = 0,
    no_browser: bool = False,
    max_points: int = 500,
    duration: float = 30.0,
    dwarf_info=None,
    svd_registers: dict[str, SvdRegister] | None = None,
    dump_mem: bool = False,
) -> None:
    from mklink.rtt_viewer import VisualizationServer

    read_lock = threading.Lock()
    runtime = SuperWatchRuntime(
        items=items,
        dwarf_info=dwarf_info,
        svd_registers=svd_registers,
        port=port,
        read_lock=read_lock,
    )
    server = VisualizationServer(
        host=host,
        port=port_http,
        max_points=max_points,
        title="MKLink SuperWatch",
        mode="SuperWatch",
        channel_metadata=make_channel_metadata(runtime.items),
        superwatch_callbacks={
            "search": runtime.search,
            "add": runtime.add,
            "remove": runtime.remove,
            "inspect": runtime.inspect,
        },
        memory_callbacks={
            "read": runtime.memory_read,
            "write": runtime.memory_write,
            "symbols": runtime.memory_symbols,
        },
    )
    server._interval = period
    stop_event = threading.Event()
    _interval_changed = threading.Event()
    origin_us: int | None = None
    actual_port = server.start()
    url = f"http://{host}:{actual_port}"
    print(f"[OK] SuperWatch Viewer started: {url}")
    if not no_browser:
        webbrowser.open(url)

    def _poll_loop() -> None:
        nonlocal origin_us
        from mklink.bridge import MKLinkSerialBridge
        from mklink.cli import _resolve_port

        resolved_port = _resolve_port(port)
        bridge = MKLinkSerialBridge(resolved_port)
        if not bridge.connect():
            server.push_event("error", {"message": "Bridge connect failed"})
            return
        try:
            if dump_mem:
                _dump_mem_poll_loop(bridge, server, runtime, origin_us, stop_event, read_lock)
            else:
                _read_ram_poll_loop(bridge, server, runtime, origin_us, stop_event, read_lock, _interval_changed)
        finally:
            bridge.close()

    def _read_ram_poll_loop(bridge, server, runtime, origin_us_ref, stop_event, read_lock, interval_changed) -> None:
        nonlocal origin_us
        while not stop_event.is_set():
            if server.collecting.is_set():
                t0 = time.monotonic()
                try:
                    with read_lock:
                        result = sample_blocks(runtime.blocks, origin_us=origin_us, bridge=bridge)
                    origin_us = result.origin_us
                    for point in result.points:
                        server.push_data_point(point)
                except Exception as exc:
                    server.push_event("error", {"message": str(exc)})
                elapsed = time.monotonic() - t0
            else:
                elapsed = 0.0
            remaining = max(0.0, server._interval - elapsed)
            interval_changed.clear()
            interval_changed.wait(timeout=remaining)

    def _dump_mem_poll_loop(bridge, server, runtime, origin_us_ref, stop_event, read_lock) -> None:
        nonlocal origin_us
        from mklink._types import DeviceState
        from mklink.dump_memory import DumpMemoryParser, build_dump_mem_command, decode_frame_to_points

        # Build block-to-region mapping
        blocks = runtime.blocks
        if not blocks:
            server.push_event("error", {"message": "No blocks to monitor"})
            return

        region_pairs = [(b.address, b.size) for b in blocks]
        period = server._interval  # already in seconds
        cmd = build_dump_mem_command(region_pairs, period)

        # Probe: try send_command first to check if cmd.dump_memory is supported
        try:
            resp = bridge.send_command(cmd, timeout=5.0)
        except Exception:
            resp = "Error"

        if "Error" in resp or resp.strip() == "-1":
            server.push_event("info", {"message": "cmd.dump_memory not supported, falling back to read_ram polling"})
            _read_ram_poll_loop(bridge, server, runtime, origin_us, stop_event, read_lock, _interval_changed)
            return

        # Device accepted — it's now streaming binary frames.
        # Enter binary stream mode (any text left in buffer will be
        # skipped by the MAGIC sync in the parser).
        bridge._enter_stream(DeviceState.DUMP_STREAM)

        # Build per-region item info for decode_frame_to_points
        block_addresses = []
        for block in blocks:
            items_info = [
                (item.name, item.type_name, item.address - block.address, item.enum_values)
                for item in block.items
            ]
            block_addresses.append((block.address, block.size, items_info))

        parser = DumpMemoryParser(region_sizes=[b.size for b in blocks])
        initial_version = runtime.blocks_version

        try:
            while not stop_event.is_set():
                # Detect blocks change (add/remove from web UI)
                if runtime.blocks_version != initial_version:
                    break  # Exit to reconfigure

                if not server.collecting.is_set():
                    stop_event.wait(timeout=0.05)
                    continue

                try:
                    raw = bridge.drain_stream_bytes()
                    if not raw:
                        time.sleep(0.001)
                        continue
                    frames = parser.feed(raw)
                    for frame in frames:
                        points, origin_us = decode_frame_to_points(
                            frame, block_addresses, origin_us,
                        )
                        for point in points:
                            server.push_data_point(point)
                except Exception as exc:
                    server.push_event("error", {"message": str(exc)})
                    break
        finally:
            # Stop dump_mem streaming by sending period=0
            try:
                bridge._exit_stream()
                stop_cmd = build_dump_mem_command(region_pairs, 0)
                bridge.send_command(stop_cmd, timeout=3.0)
            except Exception:
                pass

        # If we exited due to blocks change, re-enter
        if runtime.blocks_version != initial_version and not stop_event.is_set():
            _dump_mem_poll_loop(bridge, server, runtime, origin_us, stop_event, read_lock)

    thread = threading.Thread(target=_poll_loop, daemon=True)
    thread.start()
    server._on_interval_change = lambda _: _interval_changed.set()
    server._on_stop_requested = stop_event.set
    try:
        start = time.time()
        while duration <= 0 or time.time() - start < duration:
            if stop_event.is_set():
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        thread.join(timeout=2.0)
        server.stop()
