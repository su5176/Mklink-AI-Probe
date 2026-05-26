"""Small readelf text parser for DWARF type browsing.

This intentionally avoids extra Python dependencies. It covers the common
DWARF records needed by MKLink's typeinfo/watch commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


@dataclass
class DwarfMember:
    name: str
    offset: int = 0
    type_offset: int | None = None
    type_name: str = ""
    size: int = 0
    bit_offset: int | None = None
    bit_size: int | None = None


@dataclass
class DwarfStruct:
    name: str
    offset: int
    size: int = 0
    members: list[DwarfMember] = field(default_factory=list)


@dataclass
class DwarfEnum:
    name: str
    offset: int
    size: int = 4
    values: dict[int, str] = field(default_factory=dict)


@dataclass
class DwarfVariable:
    name: str
    offset: int
    type_offset: int | None = None
    address: int | None = None
    size: int = 0
    type_name: str = ""


@dataclass
class DwarfInfo:
    structs: dict[str, DwarfStruct] = field(default_factory=dict)
    enums: dict[str, DwarfEnum] = field(default_factory=dict)
    variables: dict[str, DwarfVariable] = field(default_factory=dict)
    base_types: dict[int, tuple[str, int]] = field(default_factory=dict)
    typedefs: dict[int, tuple[str, int | None]] = field(default_factory=dict)
    pointers: dict[int, tuple[int | None, int]] = field(default_factory=dict)
    arrays: dict[int, tuple[int | None, int]] = field(default_factory=dict)


_DIE_RE = re.compile(r"^\s*<(\d+)><([0-9a-fA-F]+)>:\s+.*\((DW_TAG_[^)]+)\)")
_ATTR_RE = re.compile(r"^\s*<[^>]+>\s+((?:DW_AT_|DW_AT_GNU_)\S+)\s*:\s*(.*)$")
_REF_RE = re.compile(r"<0x([0-9a-fA-F]+)>")
_HEX_RE = re.compile(r"0x([0-9a-fA-F]+)")
_DW_OP_PLUS_RE = re.compile(r"DW_OP_plus_uconst:\s*(\d+)")
_DW_OP_ADDR_RE = re.compile(r"DW_OP_addr:\s*([0-9a-fA-F]+)")


def _clean_name(value: str) -> str:
    value = value.strip()
    if ":" in value:
        value = value.split(":")[-1].strip()
    return value.strip('"')


def _parse_int(value: str, default: int = 0) -> int:
    value = value.strip()
    m = _HEX_RE.search(value)
    if m:
        return int(m.group(1), 16)
    m = re.search(r"-?\d+", value)
    return int(m.group(0), 10) if m else default


def _parse_member_offset(value: str) -> int:
    """Parse DW_AT_data_member_location, handling DW_OP_plus_uconst encoding.

    readelf outputs member locations in different forms depending on DWARF
    version and compiler:
      - Simple constant:  "4"
      - Hex constant:     "0x4"
      - DW_OP block:      "2 byte block: 23 4 \\t(DW_OP_plus_uconst: 4)"

    The DW_OP block form has a length prefix (the leading "2") that must NOT
    be interpreted as the offset.
    """
    value = value.strip()
    # DW_OP_plus_uconst is the most common encoding for ARM Compiler 5/6
    m = _DW_OP_PLUS_RE.search(value)
    if m:
        return int(m.group(1), 10)
    # Fallback: simple integer or hex
    return _parse_int(value)


def _parse_ref(value: str) -> int | None:
    m = _REF_RE.search(value)
    return int(m.group(1), 16) if m else None


def parse_dwarf_info_output(output: str) -> DwarfInfo:
    info = DwarfInfo()
    stack: list[dict] = []
    dies: dict[int, dict] = {}

    for line in output.splitlines():
        dm = _DIE_RE.match(line)
        if dm:
            level = int(dm.group(1))
            offset = int(dm.group(2), 16)
            tag = dm.group(3)
            die = {"level": level, "offset": offset, "tag": tag, "attrs": {}, "children": []}
            while stack and stack[-1]["level"] >= level:
                stack.pop()
            if stack:
                stack[-1]["children"].append(die)
            stack.append(die)
            dies[offset] = die
            continue

        am = _ATTR_RE.match(line)
        if am and stack:
            stack[-1]["attrs"][am.group(1)] = am.group(2).strip()

    for die in dies.values():
        attrs = die["attrs"]
        tag = die["tag"]
        off = die["offset"]
        name = _clean_name(attrs.get("DW_AT_name", ""))
        size = _parse_int(attrs.get("DW_AT_byte_size", "0"))
        type_ref = _parse_ref(attrs.get("DW_AT_type", ""))

        if tag == "DW_TAG_base_type" and name:
            info.base_types[off] = (name, size)
        elif tag == "DW_TAG_typedef" and name:
            info.typedefs[off] = (name, type_ref)
        elif tag == "DW_TAG_pointer_type":
            info.pointers[off] = (type_ref, size or 4)
        elif tag == "DW_TAG_array_type":
            info.arrays[off] = (type_ref, size)
        elif tag == "DW_TAG_structure_type" and name:
            st = DwarfStruct(name=name, offset=off, size=size)
            for child in die["children"]:
                if child["tag"] != "DW_TAG_member":
                    continue
                cattrs = child["attrs"]
                m = DwarfMember(
                    name=_clean_name(cattrs.get("DW_AT_name", "")),
                    offset=_parse_member_offset(cattrs.get("DW_AT_data_member_location", "0")),
                    type_offset=_parse_ref(cattrs.get("DW_AT_type", "")),
                    bit_offset=_parse_int(cattrs["DW_AT_bit_offset"]) if "DW_AT_bit_offset" in cattrs else (
                        _parse_int(cattrs["DW_AT_data_bit_offset"]) if "DW_AT_data_bit_offset" in cattrs else None
                    ),
                    bit_size=_parse_int(cattrs["DW_AT_bit_size"]) if "DW_AT_bit_size" in cattrs else None,
                )
                st.members.append(m)
            info.structs[name] = st
        elif tag == "DW_TAG_enumeration_type":
            # Anonymous enums (no DW_AT_name) are common in ARM Compiler output.
            # Use "<anonymous@offset>" as a synthetic name so typedefs can still
            # resolve through them.
            enum_name = name or f"<anonymous@0x{off:x}>"
            en = DwarfEnum(name=enum_name, offset=off, size=size or 4)
            for child in die["children"]:
                if child["tag"] != "DW_TAG_enumerator":
                    continue
                cattrs = child["attrs"]
                en.values[_parse_int(cattrs.get("DW_AT_const_value", "0"))] = _clean_name(cattrs.get("DW_AT_name", ""))
            info.enums[enum_name] = en
        elif tag == "DW_TAG_variable" and name:
            loc = attrs.get("DW_AT_location", "")
            address = None
            # DW_OP_addr: 20000145 (ARM Compiler 5/6 block encoding)
            m = _DW_OP_ADDR_RE.search(loc)
            if m:
                address = int(m.group(1), 16)
            else:
                # Simple hex address: 0x20000145
                m = _HEX_RE.search(loc)
                if m:
                    address = int(m.group(1), 16)
            info.variables[name] = DwarfVariable(name=name, offset=off, type_offset=type_ref, address=address)

    for st in info.structs.values():
        for m in st.members:
            m.type_name, m.size = resolve_type_name(info, m.type_offset)
    for var in info.variables.values():
        var.type_name, var.size = resolve_type_name(info, var.type_offset)
    return info


def resolve_type_name(info: DwarfInfo, type_offset: int | None) -> tuple[str, int]:
    if type_offset is None:
        return ("unknown", 0)
    if type_offset in info.base_types:
        return info.base_types[type_offset]
    if type_offset in info.typedefs:
        name, ref = info.typedefs[type_offset]
        _, size = resolve_type_name(info, ref)
        return (name, size)
    if type_offset in info.pointers:
        ref, size = info.pointers[type_offset]
        base, _ = resolve_type_name(info, ref)
        return (base + "*", size)
    if type_offset in info.arrays:
        ref, size = info.arrays[type_offset]
        base, elem_size = resolve_type_name(info, ref)
        return (base + "[]", size or elem_size)
    for st in info.structs.values():
        if st.offset == type_offset:
            return (st.name, st.size)
    for en in info.enums.values():
        if en.offset == type_offset:
            return (en.name, en.size)
    return ("unknown", 0)


class DwarfCache:
    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = Path(cache_dir or (Path.home() / ".mklink" / "dwarf_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, source: str) -> Path:
        p = Path(source)
        mtime = os.path.getmtime(source) if p.exists() else 0
        h = hashlib.md5(f"{p.resolve() if p.exists() else source}:{mtime}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"dwarf_{h}.json"

    def load(self, source: str) -> DwarfInfo | None:
        path = self._key(source)
        if not path.exists():
            return None
        try:
            return _info_from_json(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def save(self, source: str, info: DwarfInfo) -> None:
        self._key(source).write_text(json.dumps(_info_to_json(info), ensure_ascii=False), encoding="utf-8")


def load_dwarf_info(source: str, *, use_cache: bool = True) -> DwarfInfo:
    cache = DwarfCache()
    if use_cache:
        cached = cache.load(source)
        if cached:
            return cached
    result = subprocess.run(
        ["arm-none-eabi-readelf", "--debug-dump=info", source],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "readelf --debug-dump=info failed")
    info = parse_dwarf_info_output(result.stdout)
    if use_cache:
        cache.save(source, info)
    return info


def _info_to_json(info: DwarfInfo) -> dict:
    return {
        "structs": {k: {"name": v.name, "offset": v.offset, "size": v.size, "members": [m.__dict__ for m in v.members]} for k, v in info.structs.items()},
        "enums": {k: {"name": v.name, "offset": v.offset, "size": v.size, "values": v.values} for k, v in info.enums.items()},
        "variables": {k: v.__dict__ for k, v in info.variables.items()},
        "base_types": {str(k): v for k, v in info.base_types.items()},
        "typedefs": {str(k): v for k, v in info.typedefs.items()},
        "pointers": {str(k): v for k, v in info.pointers.items()},
        "arrays": {str(k): v for k, v in info.arrays.items()},
    }


def _info_from_json(data: dict) -> DwarfInfo:
    info = DwarfInfo()
    info.structs = {
        k: DwarfStruct(v["name"], v["offset"], v.get("size", 0), [DwarfMember(**m) for m in v.get("members", [])])
        for k, v in data.get("structs", {}).items()
    }
    info.enums = {
        k: DwarfEnum(v["name"], v["offset"], v.get("size", 4), {int(kk): vv for kk, vv in v.get("values", {}).items()})
        for k, v in data.get("enums", {}).items()
    }
    info.variables = {k: DwarfVariable(**v) for k, v in data.get("variables", {}).items()}
    info.base_types = {int(k): tuple(v) for k, v in data.get("base_types", {}).items()}
    info.typedefs = {int(k): tuple(v) for k, v in data.get("typedefs", {}).items()}
    info.pointers = {int(k): tuple(v) for k, v in data.get("pointers", {}).items()}
    info.arrays = {int(k): tuple(v) for k, v in data.get("arrays", {}).items()}
    return info
