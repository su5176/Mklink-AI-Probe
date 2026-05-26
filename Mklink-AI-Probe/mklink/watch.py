"""Typed one-shot and periodic variable reads."""

from __future__ import annotations

import json
import struct
import time

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
        address, type_name, size, enum_values = resolve_variable_path(info, name)
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
