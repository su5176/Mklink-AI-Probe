"""从 C 源码 struct 定义自动生成串口协议 Profile。"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "generate_profile_from_c",
    "find_packed_structs",
    "find_frame_constants",
    "find_crc_hints",
    "c_type_to_profile_type",
]

_TYPE_MAP: dict[str, tuple[str, int]] = {
    "uint8_t": ("uint8", 1),
    "int8_t": ("int8", 1),
    "uint16_t": ("uint16", 2),
    "int16_t": ("int16", 2),
    "uint32_t": ("uint32", 4),
    "int32_t": ("int32", 4),
    "float": ("float32", 4),
    "double": ("float32", 8),
    "unsigned char": ("uint8", 1),
    "char": ("uint8", 1),
    "unsigned short": ("uint16", 2),
    "short": ("int16", 2),
    "unsigned int": ("uint32", 4),
    "unsigned long": ("uint32", 4),
    "int": ("int32", 4),
    "long": ("int32", 4),
}

_HEADER_KEYWORDS = re.compile(r"head|header|sof|start", re.IGNORECASE)
_TAIL_KEYWORDS = re.compile(r"tail|eof|end", re.IGNORECASE)
_LENGTH_KEYWORDS = re.compile(r"len|length", re.IGNORECASE)
_CRC_KEYWORDS = re.compile(r"crc|check|checksum", re.IGNORECASE)

_SCALE_PATTERN = re.compile(r"[xX×*]\s*(\d+(?:\.\d+)?)|/\s*(\d+(?:\.\d+)?)")
_UNIT_PATTERN = re.compile(r"\b(RPM|rpm|℃|°C|mV|mA|V|A|W|kW|Hz|kHz|Pa|kPa|%)\b")

_DEFINE_HEX_PATTERN = re.compile(
    r"#define\s+(\w+)\s+0x([0-9A-Fa-f]+)"
)

_PRAGMA_PACK_STRUCT = re.compile(
    r"#pragma\s+pack\s*\(\s*1\s*\)\s*\n(.*?)#pragma\s+pack\s*\(\s*\)",
    re.DOTALL,
)

_ATTRIBUTE_PACKED_STRUCT = re.compile(
    r"typedef\s+struct\s*\{(.*?)\}\s*(__attribute__\s*\(\s*\(\s*packed\s*\)\s*\)\s*)?(\w+)\s*;",
    re.DOTALL,
)

_STRUCT_TYPEDEF = re.compile(
    r"typedef\s+struct\s*(?:\w+)?\s*\{(.*?)\}\s*(\w+)\s*;",
    re.DOTALL,
)

_MEMBER_PATTERN = re.compile(
    r"^\s*([\w\s]+?)\s+(\w+)\s*(?:\[(\d+)\])?\s*;"
    r"\s*(?://\s*(.*)$|/\*\s*(.*?)\s*\*/)?",
    re.MULTILINE,
)


def c_type_to_profile_type(c_type: str) -> tuple[str, int]:
    """Map C type to profile type and size."""
    normalized = " ".join(c_type.split())
    if normalized in _TYPE_MAP:
        return _TYPE_MAP[normalized]
    for key, val in _TYPE_MAP.items():
        if key in normalized:
            return val
    raise ValueError(f"Unknown C type: {c_type!r}")


def find_frame_constants(source: str) -> dict:
    """Find #define constants that look like frame headers/tails."""
    result: dict[str, str] = {}
    for match in _DEFINE_HEX_PATTERN.finditer(source):
        name = match.group(1)
        hex_val = match.group(2).upper()
        if _HEADER_KEYWORDS.search(name) and "header" not in result:
            result["header"] = hex_val
        elif _TAIL_KEYWORDS.search(name) and "tail" not in result:
            result["tail"] = hex_val
    return result


def find_crc_hints(source: str) -> dict | None:
    """Look for CRC function calls or definitions to determine CRC algorithm."""
    crc_patterns = [
        (r"crc16[_\s]*modbus|modbus[_\s]*crc", "crc16_modbus"),
        (r"crc16[_\s]*ccitt|ccitt", "crc16_ccitt"),
        (r"crc32", "crc32"),
        (r"crc16", "crc16_modbus"),
        (r"crc8", "crc8"),
        (r"checksum16|check_sum16", "checksum16"),
        (r"checksum|check_sum", "checksum8"),
    ]
    for pattern, algorithm in crc_patterns:
        if re.search(pattern, source, re.IGNORECASE):
            return {"algorithm": algorithm, "offset": -2, "scope": "all"}
    return None


def _parse_members(body: str) -> list[dict]:
    """Parse struct body into member list."""
    members: list[dict] = []
    for match in _MEMBER_PATTERN.finditer(body):
        raw_type = match.group(1).strip()
        name = match.group(2)
        array_size_str = match.group(3)
        comment = match.group(4) or match.group(5) or ""

        # Skip nested structs or unions
        if raw_type in ("struct", "union") or "{" in raw_type:
            continue

        try:
            profile_type, base_size = c_type_to_profile_type(raw_type)
        except ValueError:
            continue

        array_size = int(array_size_str) if array_size_str else None
        total_size = base_size * (array_size or 1)

        members.append({
            "name": name,
            "type": raw_type,
            "profile_type": profile_type,
            "size": total_size,
            "base_size": base_size,
            "array": array_size,
            "comment": comment.strip(),
        })
    return members


def find_packed_structs(source: str) -> list[dict]:
    """Find all packed structs in source code."""
    results: list[dict] = []

    # Strategy 1: #pragma pack(1) ... #pragma pack()
    for region_match in _PRAGMA_PACK_STRUCT.finditer(source):
        region = region_match.group(1)
        region_start = region_match.start()
        for struct_match in _STRUCT_TYPEDEF.finditer(region):
            body = struct_match.group(1)
            struct_name = struct_match.group(2)
            line = source[:region_start + struct_match.start()].count("\n") + 1
            members = _parse_members(body)
            if members:
                results.append({
                    "name": struct_name,
                    "members": [
                        {
                            "name": m["name"],
                            "type": m["type"],
                            "size": m["size"],
                            "array": m["array"],
                            "comment": m["comment"],
                        }
                        for m in members
                    ],
                    "line": line,
                    "_parsed_members": members,
                })

    # Strategy 2: __attribute__((packed)) or __packed
    if not results:
        for struct_match in re.finditer(
            r"typedef\s+struct\s*\{(.*?)\}\s*"
            r"(?:__attribute__\s*\(\s*\(\s*packed\s*\)\s*\)\s*|__packed\s+)"
            r"(\w+)\s*;",
            source,
            re.DOTALL,
        ):
            body = struct_match.group(1)
            struct_name = struct_match.group(2)
            line = source[: struct_match.start()].count("\n") + 1
            members = _parse_members(body)
            if members:
                results.append({
                    "name": struct_name,
                    "members": [
                        {
                            "name": m["name"],
                            "type": m["type"],
                            "size": m["size"],
                            "array": m["array"],
                            "comment": m["comment"],
                        }
                        for m in members
                    ],
                    "line": line,
                    "_parsed_members": members,
                })

    return results


def _extract_scale(comment: str) -> float | None:
    """Extract scale factor from comment like 'x10', '/100', '*0.1'."""
    m = _SCALE_PATTERN.search(comment)
    if not m:
        return None
    if m.group(1):
        factor = float(m.group(1))
        return 1.0 / factor if factor != 0 else None
    if m.group(2):
        factor = float(m.group(2))
        return 1.0 / factor if factor != 0 else None
    return None


def _extract_unit(comment: str) -> str:
    """Extract unit from comment."""
    m = _UNIT_PATTERN.search(comment)
    return m.group(1) if m else ""


def _struct_name_to_profile_name(name: str) -> str:
    """Convert struct name to kebab-case profile name.
    UartFrame_t -> uart-frame
    """
    # Strip common suffixes
    stripped = re.sub(r"_t$|_T$|_s$|_S$", "", name)
    # CamelCase to separated
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", stripped)
    # Underscores to hyphens
    result = separated.replace("_", "-").lower()
    return result


def generate_profile_from_c(source_path: str, struct_name: str | None = None) -> dict:
    """Parse a C source file and generate a serial protocol profile dict.

    source_path: path to .h or .c file
    struct_name: specific struct to parse (if None, use the first packed struct found)

    Returns a profile dict matching the schema used by _profile.py
    Raises ValueError if no suitable struct found.
    """
    path = Path(source_path)

    # Read with encoding fallback
    source: str = ""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            source = path.read_text(encoding=encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not source:
        raise ValueError(f"Cannot read file: {source_path}")

    structs = find_packed_structs(source)
    if not structs:
        raise ValueError(f"No packed struct found in {source_path}")

    # Select target struct
    target = None
    if struct_name:
        for s in structs:
            if s["name"] == struct_name:
                target = s
                break
        if target is None:
            raise ValueError(f"Struct '{struct_name}' not found in {source_path}")
    else:
        target = structs[0]

    members = target["_parsed_members"]
    constants = find_frame_constants(source)
    crc_hints = find_crc_hints(source)

    # Classify members by role
    header_idx: int | None = None
    tail_idx: int | None = None
    length_idx: int | None = None
    crc_idx: int | None = None

    for i, m in enumerate(members):
        name_lower = m["name"].lower()
        if i == 0 and _HEADER_KEYWORDS.search(name_lower):
            header_idx = i
        elif i == len(members) - 1 and _TAIL_KEYWORDS.search(name_lower):
            tail_idx = i
        elif _LENGTH_KEYWORDS.search(name_lower) and length_idx is None:
            length_idx = i
        elif _CRC_KEYWORDS.search(name_lower) and crc_idx is None:
            crc_idx = i

    # Also check tail at second-to-last if CRC is last
    if tail_idx is None and crc_idx is None and len(members) >= 2:
        last_name = members[-1]["name"].lower()
        second_last_name = members[-2]["name"].lower()
        if _TAIL_KEYWORDS.search(last_name):
            tail_idx = len(members) - 1
        elif _CRC_KEYWORDS.search(last_name):
            crc_idx = len(members) - 1
            if _TAIL_KEYWORDS.search(second_last_name):
                tail_idx = len(members) - 2

    # Build frame section
    frame: dict = {}

    if constants.get("header"):
        frame["header"] = constants["header"]
    elif header_idx is not None:
        frame["header"] = "AA55"

    if constants.get("tail"):
        frame["tail"] = constants["tail"]
    elif tail_idx is not None:
        frame["tail"] = "55AA"

    # Calculate offsets
    offsets: list[int] = []
    offset = 0
    for m in members:
        offsets.append(offset)
        offset += m["size"]

    if length_idx is not None:
        lm = members[length_idx]
        frame["length_field"] = {
            "offset": offsets[length_idx],
            "size": lm["base_size"],
            "includes_header": False,
        }

    if crc_idx is not None:
        crc_offset_from_end = offsets[crc_idx] - offset
        algorithm = "crc16_modbus"
        scope = "payload"
        if crc_hints:
            algorithm = crc_hints["algorithm"]
            scope = crc_hints.get("scope", "all")
        frame["crc"] = {
            "algorithm": algorithm,
            "offset": crc_offset_from_end,
            "scope": scope,
        }

    # Build fields (exclude structural members)
    structural_indices = {i for i in (header_idx, tail_idx, length_idx, crc_idx) if i is not None}
    fields: list[dict] = []

    for i, m in enumerate(members):
        if i in structural_indices:
            continue
        field: dict = {
            "name": m["name"],
            "offset": offsets[i],
            "size": m["size"],
            "type": m["profile_type"],
        }
        if m["array"] is not None:
            field["array"] = m["array"]

        scale = _extract_scale(m["comment"])
        if scale is not None:
            field["scale"] = scale

        unit = _extract_unit(m["comment"])
        if unit:
            field["unit"] = unit

        fields.append(field)

    profile_name = _struct_name_to_profile_name(target["name"])

    return {
        "name": profile_name,
        "version": "1.0",
        "frame": frame,
        "fields": fields,
        "auto_reply": [],
    }
