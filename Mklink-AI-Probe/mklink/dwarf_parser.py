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
    kind: str = "struct"


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
class DwarfArray:
    offset: int
    element_type_offset: int | None = None
    dimensions: tuple[int, ...] = ()
    size: int = 0


@dataclass
class DwarfInfo:
    structs: dict[str, DwarfStruct] = field(default_factory=dict)
    records_by_offset: dict[int, DwarfStruct] = field(default_factory=dict)
    enums: dict[str, DwarfEnum] = field(default_factory=dict)
    enums_by_offset: dict[int, DwarfEnum] = field(default_factory=dict)
    variables: dict[str, DwarfVariable] = field(default_factory=dict)
    base_types: dict[int, tuple[str, int]] = field(default_factory=dict)
    base_type_encodings: dict[int, int] = field(default_factory=dict)
    typedefs: dict[int, tuple[str, int | None]] = field(default_factory=dict)
    pointers: dict[int, tuple[int | None, int]] = field(default_factory=dict)
    arrays: dict[int, DwarfArray | tuple[int | None, int]] = field(default_factory=dict)
    qualifiers: dict[int, int | None] = field(default_factory=dict)


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
            if "DW_AT_encoding" in attrs:
                info.base_type_encodings[off] = _parse_int(attrs["DW_AT_encoding"])
        elif tag == "DW_TAG_typedef" and name:
            info.typedefs[off] = (name, type_ref)
        elif tag == "DW_TAG_pointer_type":
            info.pointers[off] = (type_ref, size or 4)
        elif tag == "DW_TAG_array_type":
            dimensions: list[int] = []
            for child in die["children"]:
                if child["tag"] != "DW_TAG_subrange_type":
                    continue
                cattrs = child["attrs"]
                if "DW_AT_count" in cattrs:
                    count = _parse_int(cattrs["DW_AT_count"])
                elif "DW_AT_upper_bound" in cattrs:
                    lower = _parse_int(cattrs.get("DW_AT_lower_bound", "0"))
                    count = _parse_int(cattrs["DW_AT_upper_bound"]) - lower + 1
                else:
                    count = 0
                if count > 0:
                    dimensions.append(count)
            info.arrays[off] = DwarfArray(
                offset=off,
                element_type_offset=type_ref,
                dimensions=tuple(dimensions),
                size=size,
            )
        elif tag in {
            "DW_TAG_atomic_type",
            "DW_TAG_const_type",
            "DW_TAG_restrict_type",
            "DW_TAG_volatile_type",
        }:
            info.qualifiers[off] = type_ref
        elif tag in {"DW_TAG_structure_type", "DW_TAG_union_type"}:
            record_name = name or f"<anonymous@0x{off:x}>"
            st = DwarfStruct(
                name=record_name,
                offset=off,
                size=size,
                kind="union" if tag == "DW_TAG_union_type" else "struct",
            )
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
            info.records_by_offset[off] = st
            if name:
                info.structs.setdefault(name, st)
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
            info.enums_by_offset[off] = en
            info.enums.setdefault(enum_name, en)
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

    for st in info.records_by_offset.values():
        for m in st.members:
            m.type_name, m.size = resolve_type_name(info, m.type_offset)
    for offset in tuple(info.arrays):
        normalized = get_array_type(info, offset)
        if normalized is None:
            continue
        _finalize_array_size(info, normalized)
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
        array = get_array_type(info, type_offset)
        if array is None:
            return ("unknown[]", 0)
        base, elem_size = resolve_type_name(info, array.element_type_offset)
        _finalize_array_size(info, array, element_size=elem_size)
        suffix = "[]" * max(1, len(array.dimensions))
        return (base + suffix, array.size or elem_size)
    if type_offset in info.qualifiers:
        return resolve_type_name(info, info.qualifiers[type_offset])
    record = get_record_type(info, type_offset)
    if record is not None:
        return (record.name, record.size)
    enum = get_enum_type(info, type_offset)
    if enum is not None:
        return (enum.name, enum.size)
    return ("unknown", 0)


def get_record_type(info: DwarfInfo, type_offset: int | None) -> DwarfStruct | None:
    if type_offset is None:
        return None
    record = info.records_by_offset.get(type_offset)
    if record is not None:
        return record
    return next((item for item in info.structs.values() if item.offset == type_offset), None)


def get_enum_type(info: DwarfInfo, type_offset: int | None) -> DwarfEnum | None:
    if type_offset is None:
        return None
    enum = info.enums_by_offset.get(type_offset)
    if enum is not None:
        return enum
    return next((item for item in info.enums.values() if item.offset == type_offset), None)


def get_array_type(info: DwarfInfo, type_offset: int | None) -> DwarfArray | None:
    if type_offset is None:
        return None
    value = info.arrays.get(type_offset)
    if value is None:
        return None
    if isinstance(value, DwarfArray):
        return value
    element_type_offset, size = value
    array = DwarfArray(
        offset=type_offset,
        element_type_offset=element_type_offset,
        size=int(size or 0),
    )
    info.arrays[type_offset] = array
    _finalize_array_size(info, array)
    return array


def _finalize_array_size(
    info: DwarfInfo,
    array: DwarfArray,
    *,
    element_size: int | None = None,
) -> None:
    if element_size is None:
        _, element_size = resolve_type_name(info, array.element_type_offset)
    element_size = int(element_size or 0)
    if array.dimensions and not array.size and element_size > 0:
        count = 1
        for dimension in array.dimensions:
            count *= dimension
        array.size = count * element_size
    elif not array.dimensions and array.size > 0 and element_size > 0:
        if array.size % element_size == 0:
            array.dimensions = (array.size // element_size,)


class DwarfCache:
    CACHE_SCHEMA_VERSION = 4

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = Path(cache_dir or (Path.home() / ".mklink" / "dwarf_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _source_metadata(self, source: str) -> dict:
        p = Path(source)
        stat = p.stat()
        return {
            "source": str(p.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    def _key(self, source: str, backend: str, parser_version: str) -> Path:
        p = Path(source)
        resolved = str(p.resolve()) if p.exists() else source
        h = hashlib.md5(
            f"{resolved}:{backend}:{parser_version}".encode("utf-8")
        ).hexdigest()
        return self.cache_dir / f"dwarf_{h}.json"

    def load(
        self, source: str, *, backend: str, parser_version: str
    ) -> DwarfInfo | None:
        path = self._key(source, backend, parser_version)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("cache_schema_version") != self.CACHE_SCHEMA_VERSION:
                return None
            if data.get("backend") != backend:
                return None
            if data.get("parser_version") != parser_version:
                return None
            if data.get("source_metadata") != self._source_metadata(source):
                return None
            return _info_from_json(data["info"])
        except Exception:
            return None

    def save(
        self,
        source: str,
        info: DwarfInfo,
        *,
        backend: str,
        parser_version: str,
    ) -> None:
        data = {
            "cache_schema_version": self.CACHE_SCHEMA_VERSION,
            "backend": backend,
            "parser_version": parser_version,
            "source_metadata": self._source_metadata(source),
            "info": _info_to_json(info),
        }
        self._key(source, backend, parser_version).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )


def _default_dwarf_cache_dir(project_root: str | None = None) -> Path:
    """Keep reusable parser data with the user-selected task/project root."""
    explicit = os.environ.get("MKLINK_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve() / "dwarf"
    if project_root:
        return (
            Path(project_root).expanduser().resolve()
            / ".mklink"
            / "cache"
            / "dwarf"
        )
    build_root = os.environ.get("MKLINK_BUILD_ROOT")
    if build_root:
        return Path(build_root).expanduser().resolve() / "cache" / "dwarf"
    return Path.home() / ".mklink" / "dwarf_cache"


def load_dwarf_info(
    source: str,
    *,
    use_cache: bool = True,
    backend: str | None = None,
    project_root: str | None = None,
) -> DwarfInfo:
    from mklink.elf_backend import get_elf_backend

    selected = get_elf_backend(backend, project_root=project_root)
    cache = None
    if use_cache:
        try:
            cache = DwarfCache(str(_default_dwarf_cache_dir(project_root)))
        except OSError:
            # A read-only project must not make AXF parsing fail, and must not
            # trigger a hidden fallback to the user profile/system drive.
            cache = None
    if cache is not None:
        cached = cache.load(
            source,
            backend=selected.name,
            parser_version=selected.parser_version,
        )
        if cached:
            return cached
    info = selected.dwarf_info(source)
    if cache is not None:
        try:
            cache.save(
                source,
                info,
                backend=selected.name,
                parser_version=selected.parser_version,
            )
        except OSError:
            pass
    return info


def _info_to_json(info: DwarfInfo) -> dict:
    arrays = {}
    for offset in info.arrays:
        array = get_array_type(info, offset)
        if array is None:
            continue
        arrays[str(offset)] = {
            "offset": array.offset,
            "element_type_offset": array.element_type_offset,
            "dimensions": list(array.dimensions),
            "size": array.size,
        }
    return {
        "schema_version": 3,
        "structs": {k: {"name": v.name, "offset": v.offset, "size": v.size, "members": [m.__dict__ for m in v.members], "kind": v.kind} for k, v in info.structs.items()},
        "records_by_offset": {str(k): {"name": v.name, "offset": v.offset, "size": v.size, "members": [m.__dict__ for m in v.members], "kind": v.kind} for k, v in info.records_by_offset.items()},
        "enums": {k: {"name": v.name, "offset": v.offset, "size": v.size, "values": v.values} for k, v in info.enums.items()},
        "enums_by_offset": {str(k): {"name": v.name, "offset": v.offset, "size": v.size, "values": v.values} for k, v in info.enums_by_offset.items()},
        "variables": {k: v.__dict__ for k, v in info.variables.items()},
        "base_types": {str(k): v for k, v in info.base_types.items()},
        "base_type_encodings": {str(k): v for k, v in info.base_type_encodings.items()},
        "typedefs": {str(k): v for k, v in info.typedefs.items()},
        "pointers": {str(k): v for k, v in info.pointers.items()},
        "arrays": arrays,
        "qualifiers": {str(k): v for k, v in info.qualifiers.items()},
    }


def _info_from_json(data: dict) -> DwarfInfo:
    if data.get("schema_version") != 3:
        raise ValueError("unsupported DWARF cache schema")
    info = DwarfInfo()
    info.structs = {
        k: DwarfStruct(v["name"], v["offset"], v.get("size", 0), [DwarfMember(**m) for m in v.get("members", [])], v.get("kind", "struct"))
        for k, v in data.get("structs", {}).items()
    }
    info.records_by_offset = {
        int(k): DwarfStruct(v["name"], v["offset"], v.get("size", 0), [DwarfMember(**m) for m in v.get("members", [])], v.get("kind", "struct"))
        for k, v in data.get("records_by_offset", {}).items()
    }
    for name, record in tuple(info.structs.items()):
        info.structs[name] = info.records_by_offset.get(record.offset, record)
    info.enums = {
        k: DwarfEnum(v["name"], v["offset"], v.get("size", 4), {int(kk): vv for kk, vv in v.get("values", {}).items()})
        for k, v in data.get("enums", {}).items()
    }
    info.variables = {k: DwarfVariable(**v) for k, v in data.get("variables", {}).items()}
    info.base_types = {int(k): tuple(v) for k, v in data.get("base_types", {}).items()}
    info.base_type_encodings = {
        int(k): int(v) for k, v in data.get("base_type_encodings", {}).items()
    }
    info.typedefs = {int(k): tuple(v) for k, v in data.get("typedefs", {}).items()}
    info.pointers = {int(k): tuple(v) for k, v in data.get("pointers", {}).items()}
    info.arrays = {
        int(k): DwarfArray(
            offset=v.get("offset", int(k)),
            element_type_offset=v.get("element_type_offset"),
            dimensions=tuple(v.get("dimensions", ())),
            size=v.get("size", 0),
        )
        for k, v in data.get("arrays", {}).items()
    }
    info.enums_by_offset = {
        int(k): DwarfEnum(v["name"], v["offset"], v.get("size", 4), {int(kk): vv for kk, vv in v.get("values", {}).items()})
        for k, v in data.get("enums_by_offset", {}).items()
    }
    for name, enum in tuple(info.enums.items()):
        info.enums[name] = info.enums_by_offset.get(enum.offset, enum)
    info.qualifiers = {int(k): v for k, v in data.get("qualifiers", {}).items()}
    return info
