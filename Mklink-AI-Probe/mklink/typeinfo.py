"""Formatting helpers for DWARF type information."""

from __future__ import annotations

import json

from mklink.dwarf_parser import DwarfInfo, load_dwarf_info


def format_variable(info: DwarfInfo, name: str) -> str:
    var = info.variables.get(name)
    if not var:
        raise KeyError(f"variable '{name}' not found")
    addr = f" addr=0x{var.address:08X}" if var.address is not None else ""
    return f"{var.name}: {var.type_name} (size={var.size}{addr})"


def format_struct(info: DwarfInfo, name: str) -> str:
    st = info.structs.get(name)
    if not st:
        raise KeyError(f"struct '{name}' not found")
    lines = [f"{st.name} (size={st.size})"]
    for m in st.members:
        lines.append(f"  +0x{m.offset:02X}  {m.name:<24} {m.type_name:<16} ({m.size} bytes)")
    return "\n".join(lines)


def format_enum(info: DwarfInfo, name: str) -> str:
    en = info.enums.get(name)
    if not en:
        raise KeyError(f"enum '{name}' not found")
    lines = [f"{en.name} (size={en.size})"]
    for value, enum_name in sorted(en.values.items()):
        lines.append(f"  {enum_name:<24} = {value}")
    return "\n".join(lines)


def run_typeinfo(args) -> str:
    info = load_dwarf_info(args.source)
    if getattr(args, "json", False):
        if args.list_structs:
            return json.dumps(sorted(info.structs), ensure_ascii=False)
        if args.list_enums:
            return json.dumps(sorted(info.enums), ensure_ascii=False)
    if args.var:
        return format_variable(info, args.var)
    if args.struct:
        return format_struct(info, args.struct)
    if args.enum:
        return format_enum(info, args.enum)
    if args.list_structs:
        return "\n".join(sorted(info.structs))
    if args.list_enums:
        return "\n".join(sorted(info.enums))
    return "No type query specified"
