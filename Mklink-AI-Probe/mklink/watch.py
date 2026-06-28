"""Typed one-shot and periodic variable reads."""

from __future__ import annotations

import json
import re
import struct
import time
from pathlib import Path

from mklink.dwarf_parser import DwarfInfo, DwarfStruct, load_dwarf_info
from mklink.memory_access import read_memory


TYPE_FORMATS = {
    "uint8_t": ("<B", 1), "uint8": ("<B", 1), "uchar": ("<B", 1), "bool": ("<?", 1),
    "int8_t": ("<b", 1), "int8": ("<b", 1), "char": ("<b", 1),
    "uint16_t": ("<H", 2), "uint16": ("<H", 2), "ushort": ("<H", 2),
    "int16_t": ("<h", 2), "int16": ("<h", 2), "short": ("<h", 2),
    "uint32_t": ("<I", 4), "uint32": ("<I", 4), "uint": ("<I", 4),
    "int32_t": ("<i", 4), "int32": ("<i", 4), "int": ("<i", 4),
    "float": ("<f", 4), "fp32": ("<f", 4),
}

_C_TYPE_ALIASES = {
    "float": "float",
    "uint8_t": "uint8_t",
    "int8_t": "int8_t",
    "uint16_t": "uint16_t",
    "int16_t": "int16_t",
    "uint32_t": "uint32_t",
    "int32_t": "int32_t",
    "unsigned char": "uint8_t",
    "signed char": "int8_t",
    "char": "char",
    "unsigned short": "uint16_t",
    "short": "int16_t",
    "unsigned int": "uint32_t",
    "int": "int32_t",
    "bool": "bool",
}


def decode_value(data: bytes, type_name: str, enum_values: dict[int, str] | None = None, *, known_size: int = 0):
    """Decode raw bytes into a value based on type name.

    Args:
        known_size: When set (> 0), overrides the format-derived size for
            types not in TYPE_FORMATS (e.g. typedefs / enums).
    """
    key = type_name.strip().lower()
    fmt_size = TYPE_FORMATS.get(key)
    if fmt_size:
        fmt, size = fmt_size
    elif known_size > 0:
        # typedef / enum: use known_size to pick the right unsigned format
        fmt = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}.get(known_size, "<I")
        size = known_size
    else:
        fmt, size = "<I", min(4, max(1, len(data)))
    if len(data) < size:
        raise ValueError(f"not enough bytes for {type_name}: need {size}, got {len(data)}")
    value = struct.unpack(fmt, data[:size])[0]
    if enum_values and isinstance(value, int) and value in enum_values:
        return f"{value} ({enum_values[value]})"
    return value


def _candidate_map_paths(source: str) -> list[Path]:
    p = Path(source)
    candidates = []
    if p.suffix:
        candidates.append(p.with_suffix(".map"))
    candidates.append(p.parent / "demo.map")
    return [c for i, c in enumerate(candidates) if c not in candidates[:i]]


def _parse_map_symbol(map_path: Path, name: str) -> tuple[int, int, str | None] | None:
    name_re = re.escape(name)
    by_name = re.compile(
        rf"^\s*{name_re}\s+0x(?P<addr>[0-9a-fA-F]+)\s+\S+\s+(?P<size>\d+)\s+\d+\s+.*?(?P<object>\S+\.o)?\s*$"
    )
    by_range = re.compile(
        rf"^\s*(?P<start>[0-9a-fA-F]{{8}})-(?P<end>[0-9a-fA-F]{{8}})\s+{name_re}\s+(?P<size>\d+)\s+\d+.*?(?P<object>\S+\.o)?\s*$"
    )
    gcc_alloc = re.compile(
        r"^\s*0x(?P<addr>[0-9a-fA-F]+)\s+0x(?P<size>[0-9a-fA-F]+)\s+(?P<object>\S+\.(?:o|obj))\s*$"
    )
    gcc_symbol = re.compile(
        rf"^\s*0x(?P<addr>[0-9a-fA-F]+)\s+{name_re}\s*$"
    )
    try:
        lines = map_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    previous_alloc: tuple[int, int, str | None] | None = None
    for line in lines:
        m = by_name.match(line)
        if m:
            return int(m.group("addr"), 16), int(m.group("size")), m.group("object")
        m = by_range.match(line)
        if m:
            return int(m.group("start"), 16), int(m.group("size")), m.group("object")
        m = gcc_alloc.match(line)
        if m:
            previous_alloc = (
                int(m.group("addr"), 16),
                int(m.group("size"), 16),
                m.group("object"),
            )
            continue
        m = gcc_symbol.match(line)
        if m:
            address = int(m.group("addr"), 16)
            if previous_alloc:
                alloc_addr, alloc_size, object_name = previous_alloc
                if alloc_addr <= address < alloc_addr + max(alloc_size, 1):
                    return address, max(alloc_size - (address - alloc_addr), 0), object_name
            return address, 0, None
    return None


def _iter_source_roots(source: str) -> list[Path]:
    roots = []
    p = Path(source).resolve()
    for parent in [p.parent, *p.parents]:
        roots.append(parent)
    return roots


def _find_declared_type(source: str, name: str, object_name: str | None) -> str | None:
    source_names = []
    if object_name:
        obj = Path(object_name).name
        if obj.endswith(".o"):
            source_names.append(obj[:-2])
        elif obj.endswith(".obj"):
            source_names.append(obj[:-4])
    source_names.extend(["*.c", "*.h"])

    seen: set[Path] = set()
    name_re = re.escape(name)
    type_words = "|".join(sorted((re.escape(k) for k in _C_TYPE_ALIASES), key=len, reverse=True))
    decl_re = re.compile(
        rf"\b(?:static\s+|extern\s+|volatile\s+|const\s+)*"
        rf"(?P<type>{type_words})\s+(?:\*+\s*)?{name_re}\b"
    )
    for root in _iter_source_roots(source):
        if not root.exists() or root.is_file():
            continue
        for pattern in source_names:
            for path in root.rglob(pattern):
                if path in seen or path.suffix.lower() not in {".c", ".h"}:
                    continue
                seen.add(path)
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                m = decl_re.search(text)
                if m:
                    return _C_TYPE_ALIASES.get(m.group("type").lower())
    return None


def resolve_map_source_variable(source: str, name: str) -> tuple[int, str, int] | None:
    """Resolve a simple global variable using a sibling MAP file and C source.

    This is a fallback for toolchains whose DWARF output is too sparse for the
    lightweight parser. It intentionally supports only top-level basic globals.
    """
    if "." in name:
        return None
    for map_path in _candidate_map_paths(source):
        symbol = _parse_map_symbol(map_path, name)
        if not symbol:
            continue
        address, map_size, object_name = symbol
        type_name = _find_declared_type(source, name, object_name) or "unknown"
        fmt_size = TYPE_FORMATS.get(type_name.lower())
        size = fmt_size[1] if fmt_size else map_size
        return address, type_name, size
    return None


def resolve_variable_path(info: DwarfInfo, path: str) -> tuple[int, str, int, dict[int, str] | None]:
    parts = path.split(".")
    var = info.variables.get(parts[0])
    if not var or var.address is None:
        raise KeyError(f"variable '{parts[0]}' not found or has no address")
    address = var.address
    type_name = var.type_name
    size = var.size
    enum_values = None
    for field_name in parts[1:]:
        st = info.structs.get(type_name)
        if not st:
            raise KeyError(f"'{type_name}' is not a known struct")
        member = next((m for m in st.members if m.name == field_name), None)
        if not member:
            raise KeyError(f"field '{field_name}' not found in {type_name}")
        address += member.offset
        type_name = member.type_name
        size = member.size
    if type_name in info.enums:
        enum_values = info.enums[type_name].values
        size = info.enums[type_name].size
    return address, type_name, size, enum_values


def read_watch_values(
    names: list[str],
    *,
    source: str,
    port: str | None = None,
) -> list[dict]:
    info = load_dwarf_info(source)
    rows = []
    for name in names:
        try:
            address, type_name, size, enum_values = resolve_variable_path(info, name)
        except KeyError:
            fallback = resolve_map_source_variable(source, name)
            if not fallback:
                raise
            address, type_name, size = fallback
            enum_values = None
        if (not size or type_name == "unknown") and "." not in name:
            fallback = resolve_map_source_variable(source, name)
            if fallback:
                address, type_name, size = fallback
        data, raw = read_memory(port, address, size)
        value = decode_value(data, type_name, enum_values, known_size=size) if data else raw.strip()
        rows.append({"name": name, "address": f"0x{address:08X}", "type": type_name, "size": size, "value": value})
    return rows


def format_watch_rows(rows: list[dict], *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(rows, ensure_ascii=False, indent=2)
    if not rows:
        return "No variables"
    name_w = max(len(r["name"]) for r in rows)
    lines = []
    for r in rows:
        lines.append(f"{r['name']:<{name_w}} = {r['value']}  {r['type']} @ {r['address']}")
    return "\n".join(lines)


def run_watch(names: list[str], *, source: str, port: str | None = None, period: float | None = None, as_json: bool = False) -> str:
    if period is None or period <= 0:
        return format_watch_rows(read_watch_values(names, source=source, port=port), as_json=as_json)
    try:
        while True:
            print(format_watch_rows(read_watch_values(names, source=source, port=port), as_json=as_json))
            time.sleep(period)
    except KeyboardInterrupt:
        return ""
