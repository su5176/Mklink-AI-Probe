"""Modbus point-table detection and profile/doc generation."""

from __future__ import annotations

import ast
import csv
import json
import re
from dataclasses import asdict, dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Iterable


@dataclass
class ModbusPoint:
    addr: int
    name: str
    type: str = "uint16"
    access: str = "ro"
    label: str = ""
    unit: str = ""
    scale: float | int = 1
    kind: str = ""
    min: int | None = None
    max: int | None = None
    default: int | None = None
    bits: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    note: str = ""


@dataclass
class PointMap:
    source_format: str
    source_files: list[str]
    points: list[ModbusPoint]
    commands: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        writable = sum(1 for p in self.points if p.access == "rw")
        bitfields = sum(1 for p in self.points if p.bits)
        return {
            "source_format": self.source_format,
            "source_files": self.source_files,
            "points": len(self.points),
            "writable": writable,
            "bitfields": bitfields,
            "commands": len(self.commands),
            "warnings": self.warnings,
        }

    def to_jsonable(self) -> dict[str, Any]:
        data = self.summary()
        data["registers"] = [asdict(p) for p in self.points]
        data["commands"] = self.commands
        return data


_NAME_ALIASES = {
    "addr": {"addr", "address", "地址", "寄存器地址"},
    "name": {"name", "register", "寄存器", "名称", "变量", "变量名"},
    "type": {"type", "类型", "data_type", "datatype"},
    "access": {"access", "权限", "读写", "rw"},
    "range": {"range", "范围", "minmax"},
    "default": {"default", "默认", "默认值", "def"},
    "unit": {"unit", "单位"},
    "scale": {"scale", "倍率", "比例"},
    "kind": {"kind", "类别", "类型标记"},
    "bit": {"bit", "bits", "位", "位定义", "bitfield"},
}


def detect_pointmap(
    project_root: str | Path = ".",
    source: str | Path | None = None,
    fmt: str = "auto",
) -> PointMap:
    """Detect a Modbus point table.

    C sources are the automatic source of truth. Markdown/CSV are parsed only
    when an explicit source path is supplied.
    """
    root = Path(project_root)
    if source:
        src = Path(source)
        if not src.is_absolute():
            src = root / src
        resolved_fmt = _infer_format(src, fmt)
        if resolved_fmt == "c":
            paths = [src]
            if src.suffix.lower() == ".h":
                sibling = src.with_suffix(".c")
                if sibling.is_file():
                    paths.append(sibling)
            return parse_c_sources(paths, project_root=root)
        text = src.read_text(encoding="utf-8")
        if resolved_fmt == "markdown":
            return parse_markdown_table(text, source_name=str(src))
        if resolved_fmt == "csv":
            return parse_csv_table(text, source_name=str(src))
        raise ValueError(f"Unsupported pointmap format: {resolved_fmt}")

    paths = _find_c_pointmap_sources(root)
    if not paths:
        return PointMap("c", [], [], warnings=["No *ModbusRegs.h or *ModbusRegs.c files found"])
    return parse_c_sources(paths, project_root=root)


def parse_c_sources(paths: Iterable[str | Path], project_root: str | Path = ".") -> PointMap:
    root = Path(project_root)
    files = [Path(p) for p in paths]
    texts: list[tuple[Path, str]] = []
    for path in files:
        if path.is_file():
            texts.append((path, path.read_text(encoding="utf-8", errors="ignore")))

    macros: dict[str, int] = {}
    macro_comments: dict[str, str] = {}
    warnings: list[str] = []
    for _, text in texts:
        for name, expr, comment in _iter_defines(text):
            val = _eval_expr(expr, macros)
            if val is not None:
                macros[name] = val
                macro_comments[name] = comment

    by_addr: dict[int, ModbusPoint] = {}
    for path, text in texts:
        for entry in _iter_modbus_reg_defines(text):
            addr = _eval_expr(entry["index"], macros)
            if addr is None:
                warnings.append(f"Could not evaluate {entry['name']} index {entry['index']} in {path.name}")
                continue
            is_signed = entry["field"] == "svalue"
            name = _normalize_reg_name(entry["name"])
            comment = _clean_comment(entry["comment"])
            point = ModbusPoint(
                addr=addr,
                name=name,
                type="int16" if is_signed else "uint16",
                access="rw" if addr >= macros.get("MODBUS_REG_PARAM_START", 200) else "ro",
                label=comment or _labelize(name),
                unit=_guess_unit(comment),
                kind=_guess_kind(name, comment, addr),
                source=str(_rel(path, root)),
                note=comment,
            )
            if addr in by_addr:
                continue
            by_addr[addr] = point

        for entry in _iter_modbus_regs(text):
            addr = _eval_expr(entry["index"], macros)
            if addr is None:
                warnings.append(f"Could not evaluate ModbusRegs index {entry['index']} in {path.name}")
                continue
            body = entry["body"]
            comment = _clean_comment(entry["comment"])
            is_signed = ".svalue" in body
            name = _extract_pointer_name(body) or _to_identifier(comment) or f"reg_{addr}"
            point = ModbusPoint(
                addr=addr,
                name=name,
                type="int16" if is_signed else "uint16",
                access="ro",
                label=comment or _labelize(name),
                unit=_guess_unit(comment),
                kind=_guess_kind(name, comment, addr),
                source=str(_rel(path, root)),
                note=comment,
            )
            if addr in by_addr:
                warnings.append(f"Duplicate address {addr} in {path.name}; keeping first definition")
                continue
            by_addr[addr] = point

        for range_entry in _iter_param_ranges(text):
            addr = _eval_expr(range_entry["index"], macros)
            if addr is None:
                continue
            vals = [_eval_expr(v.strip(), macros) for v in range_entry["values"].split(",")[:3]]
            if len(vals) < 3 or any(v is None for v in vals):
                continue
            comment = _clean_comment(range_entry["comment"])
            point = by_addr.get(addr)
            if point is None:
                point = ModbusPoint(
                    addr=addr,
                    name=_to_identifier(comment) or f"param_{addr}",
                    label=comment or f"Parameter {addr}",
                    kind="param",
                    source=str(_rel(path, root)),
                )
                by_addr[addr] = point
            point.access = "rw"
            point.kind = point.kind or "param"
            point.min = int(vals[0])
            point.max = int(vals[1])
            point.default = int(vals[2])
            if comment:
                point.label = comment
                point.name = point.name or _to_identifier(comment)

    macro_addr_map = _build_macro_addr_map(texts, macros)
    bit_aliases = _extract_bit_aliases(texts, macro_addr_map)
    _attach_bit_aliases(by_addr, bit_aliases)
    bit_defs = _extract_bit_defines(macros, macro_comments)
    _attach_bits(by_addr, bit_defs)
    commands = _build_commands_from_bits(by_addr)

    points = sorted(by_addr.values(), key=lambda p: p.addr)
    return PointMap(
        source_format="c",
        source_files=[str(_rel(p, root)) for p, _ in texts],
        points=points,
        commands=commands,
        warnings=warnings,
    )


def parse_markdown_table(text: str, source_name: str = "markdown") -> PointMap:
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return PointMap("markdown", [source_name], [], warnings=["No Markdown table found"])
    header, data_rows = rows[0], rows[1:]
    records = []
    for row in data_rows:
        if len(row) < len(header):
            row += [""] * (len(header) - len(row))
        records.append(dict(zip(header, row)))
    return _parse_records(records, "markdown", source_name)


def parse_csv_table(text: str, source_name: str = "csv") -> PointMap:
    reader = csv.DictReader(StringIO(text))
    return _parse_records(list(reader), "csv", source_name)


def generate_pointmap_artifacts(
    pointmap: PointMap,
    output: str | Path,
    doc: str | Path,
) -> tuple[Path, Path]:
    profile_path = Path(output)
    doc_path = Path(doc)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.parent.mkdir(parents=True, exist_ok=True)

    profile = pointmap_to_profile(pointmap)
    profile_path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    doc_path.write_text(render_pointmap_doc(pointmap, profile), encoding="utf-8")
    return profile_path, doc_path


def pointmap_to_profile(pointmap: PointMap) -> dict[str, Any]:
    runtime: list[dict[str, Any]] = []
    control: list[dict[str, Any]] = []
    params: list[dict[str, Any]] = []
    alarms: list[dict[str, Any]] = []

    for point in pointmap.points:
        item = _point_to_profile_reg(point)
        if point.kind == "alarm":
            alarms.append(item)
        elif point.kind == "bitfield" or item["access"] == "rw" and point.addr < 200:
            control.append(item)
        elif item["access"] == "rw" or point.kind == "param":
            params.append(item)
        else:
            runtime.append(item)

    groups = []
    if runtime:
        groups.append({"id": "runtime", "name": "Runtime", "poll_group": "fast", "registers": runtime})
    if control or alarms:
        groups.append({"id": "control", "name": "Control", "poll_group": "fast", "registers": control + alarms})
    if params:
        groups.append({"id": "parameters", "name": "Parameters", "poll_group": "slow", "writable": True, "registers": params})

    chart_regs = [r for r in runtime if r.get("type") in ("uint16", "int16")][:6]
    return {
        "schema_version": 1,
        "profile_id": "generated-pointmap",
        "name": "Generated Modbus Point Map",
        "slave": 1,
        "baudrate": 9600,
        "source": {
            "format": pointmap.source_format,
            "files": pointmap.source_files,
            "warnings": pointmap.warnings,
        },
        "groups": groups,
        "commands": pointmap.commands,
        "poll_groups": {
            "fast": {"interval": 1.0},
            "slow": {"interval": 5.0},
        },
        "dashboard": {
            "title": "Generated Modbus Dashboard",
            "overview_metrics": [
                {"addr": r["addr"], "label": r.get("label", r["name"]), "unit": r.get("unit", ""), "scale": r.get("scale", 1)}
                for r in chart_regs
            ],
            "control_buttons": [
                {"action": c["action"], "label": c.get("label", c["action"]), "css_class": ""}
                for c in pointmap.commands[:8]
            ],
        },
    }


def render_pointmap_doc(pointmap: PointMap, profile: dict[str, Any] | None = None) -> str:
    lines = [
        "# Modbus Point Map",
        "",
        f"- Source format: `{pointmap.source_format}`",
        f"- Source files: {', '.join(pointmap.source_files) if pointmap.source_files else 'none'}",
        f"- Registers: {len(pointmap.points)}",
        f"- Commands: {len(pointmap.commands)}",
        "",
    ]
    if pointmap.warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {w}" for w in pointmap.warnings)
        lines.append("")

    lines.extend([
        "## Registers",
        "",
        "| Address | Name | Type | Access | Unit/Scale | Range/Default | Kind | Bits/Notes |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for p in pointmap.points:
        bits = "; ".join(f"{b['bit']}:{b.get('label') or b.get('name')}" for b in p.bits)
        rng = ""
        if p.min is not None or p.max is not None or p.default is not None:
            rng = f"{p.min}..{p.max} / {p.default}"
        lines.append(
            f"| {p.addr} | {p.name} | {p.type} | {p.access} | {p.unit or ''} / {p.scale} | {rng} | {p.kind or ''} | {bits or p.note or ''} |"
        )

    if pointmap.commands:
        lines.extend(["", "## Commands", ""])
        for cmd in pointmap.commands:
            lines.append(
                f"- `{cmd['action']}` writes bit {cmd.get('bit')} at register {cmd.get('addr', cmd.get('write_addr'))}: {cmd.get('label', '')}"
            )
    lines.append("")
    return "\n".join(lines)


def _find_c_pointmap_sources(root: Path) -> list[Path]:
    paths: list[Path] = []
    for pattern in ("*ModbusRegs.h", "*ModbusRegs.c"):
        paths.extend(root.rglob(pattern))
    return sorted(set(paths))


def _infer_format(path: Path, fmt: str) -> str:
    if fmt != "auto":
        return {"md": "markdown"}.get(fmt, fmt)
    suffix = path.suffix.lower()
    if suffix in (".c", ".h"):
        return "c"
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix == ".csv":
        return "csv"
    raise ValueError(f"Cannot infer pointmap format from {path}")


def _iter_defines(text: str):
    pat = re.compile(r"^\s*#define\s+([A-Za-z_]\w*)\s+(.+?)(?://(.*))?$", re.MULTILINE)
    for match in pat.finditer(text):
        name, expr, comment = match.groups()
        if "(" in name:
            continue
        yield name, expr.strip(), _clean_comment(comment or "")


def _eval_expr(expr: str, macros: dict[str, int]) -> int | None:
    expr = expr.split("/*", 1)[0].strip()
    expr = re.sub(r"//.*$", "", expr).strip()
    expr = re.sub(r"\b([A-Za-z_]\w*)\b", lambda m: str(macros[m.group(1)]) if m.group(1) in macros else m.group(0), expr)
    expr = expr.replace("U", "").replace("u", "").replace("L", "").replace("l", "")
    try:
        node = ast.parse(expr, mode="eval")
        return int(_safe_eval(node.body))
    except Exception:
        return None


def _safe_eval(node: ast.AST) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return int(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        val = _safe_eval(node.operand)
        return -val if isinstance(node.op, ast.USub) else val
    if isinstance(node, ast.BinOp):
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.FloorDiv) or isinstance(node.op, ast.Div):
            return left // right
        if isinstance(node.op, ast.LShift):
            return left << right
        if isinstance(node.op, ast.RShift):
            return left >> right
        if isinstance(node.op, ast.BitOr):
            return left | right
        if isinstance(node.op, ast.BitAnd):
            return left & right
    raise ValueError("unsupported expression")


def _iter_modbus_regs(text: str):
    pat = re.compile(r"\[([^\]]+)\]\s*=\s*\{([^;\n]*?(?:\.svalue|\.value)[^;\n]*?)\}\s*,?\s*(?://(.*))?")
    for match in pat.finditer(text):
        yield {"index": match.group(1), "body": match.group(2), "comment": match.group(3) or ""}


def _iter_modbus_reg_defines(text: str):
    lines = text.splitlines()
    pending_comment = ""
    pat = re.compile(
        r"^\s*#define\s+([A-Za-z_]\w*)\s+ModbusRegs\[(.+?)\](?:\.(value|svalue))?.*?(?://(.*))?$"
    )
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("//"):
            pending_comment = _clean_comment(stripped[2:])
            continue
        match = pat.match(line)
        if not match:
            if stripped:
                pending_comment = ""
            continue
        name, index, field, inline_comment = match.groups()
        yield {
            "name": name,
            "index": index,
            "field": field or "value",
            "comment": _clean_comment(inline_comment or pending_comment),
        }
        pending_comment = ""


def _iter_param_ranges(text: str):
    pat = re.compile(r"\[([^\]]+)\]\s*=\s*\{\s*([^{};\n]+?)\s*\}\s*,?\s*(?://(.*))?")
    in_ranges = "ModbusParamRanges" in text
    if not in_ranges:
        return
    for match in pat.finditer(text):
        values = match.group(2)
        if "," not in values:
            continue
        yield {"index": match.group(1), "values": values, "comment": match.group(3) or ""}
    seq_pat = re.compile(r"/\*\s*(\d+)\s*\*/\s*\{\s*([^{};\n]+?)\s*\}\s*,?\s*(?://(.*))?")
    for match in seq_pat.finditer(text):
        values = match.group(2)
        if "," not in values:
            continue
        yield {
            "index": f"MODBUS_REG_PARAM_START + {match.group(1)}",
            "values": values,
            "comment": match.group(3) or "",
        }


def _extract_pointer_name(body: str) -> str:
    match = re.search(r"&\s*([A-Za-z_]\w*)", body)
    return match.group(1) if match else ""


def _extract_bit_defines(macros: dict[str, int], comments: dict[str, str]) -> list[dict[str, Any]]:
    bits = []
    for name, value in macros.items():
        if value <= 0 or value & (value - 1):
            continue
        upper = name.upper()
        if not any(key in upper for key in ("CMD", "COMMAND", "ALARM", "FAULT", "ERR")):
            continue
        bit = value.bit_length() - 1
        bits.append({
            "name": name,
            "label": comments.get(name) or _labelize(name),
            "bit": bit,
            "category": "alarm" if any(k in upper for k in ("ALARM", "FAULT", "ERR")) else "command",
        })
    return bits


def _attach_bits(points: dict[int, ModbusPoint], bit_defs: list[dict[str, Any]]) -> None:
    command_points = [p for p in points.values() if p.kind == "bitfield"]
    alarm_points = [p for p in points.values() if p.kind == "alarm"]
    if not command_points:
        command_points = [p for p in points.values() if "command" in (p.name + p.label).lower()]
    if not alarm_points:
        alarm_points = [p for p in points.values() if "alarm" in (p.name + p.label).lower()]

    if command_points:
        target = sorted(command_points, key=lambda p: p.addr)[0]
        target.kind = "bitfield"
        target.access = "rw"
        target.bits.extend(_dedupe_bits([b for b in bit_defs if b["category"] == "command"]))
    if alarm_points:
        target = sorted(alarm_points, key=lambda p: p.addr)[0]
        target.kind = "alarm"
        target.bits.extend(_dedupe_bits([b for b in bit_defs if b["category"] == "alarm"]))


def _build_macro_addr_map(texts: list[tuple[Path, str]], macros: dict[str, int]) -> dict[str, int]:
    result = {}
    pat = re.compile(r"^\s*#define\s+([A-Za-z_]\w*)\s+ModbusRegs\[(.+?)\]", re.MULTILINE)
    for _, text in texts:
        for match in pat.finditer(text):
            addr = _eval_expr(match.group(2), macros)
            if addr is not None:
                result[match.group(1)] = addr
    return result


def _extract_bit_aliases(
    texts: list[tuple[Path, str]],
    macro_addr_map: dict[str, int],
) -> list[dict[str, Any]]:
    aliases = []
    pat = re.compile(
        r"^\s*#define\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\.bits\.bit(\d+)\s*(?://(.*))?$"
    )
    for _, text in texts:
        pending_comment = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("//"):
                pending_comment = _clean_comment(stripped[2:])
                continue
            match = pat.match(line)
            if not match:
                if stripped:
                    pending_comment = ""
                continue
            name, target, bit_s, inline_comment = match.groups()
            addr = macro_addr_map.get(target)
            if addr is None:
                pending_comment = ""
                continue
            upper = name.upper()
            aliases.append({
                "addr": addr,
                "bit": int(bit_s),
                "name": _normalize_bit_name(name),
                "label": _clean_comment(inline_comment or pending_comment) or _labelize(_normalize_bit_name(name)),
                "category": "alarm" if "ALARM" in upper or "FAULT" in upper else "command",
            })
            pending_comment = ""
    return aliases


def _attach_bit_aliases(points: dict[int, ModbusPoint], aliases: list[dict[str, Any]]) -> None:
    by_addr: dict[int, list[dict[str, Any]]] = {}
    for alias in aliases:
        by_addr.setdefault(alias["addr"], []).append(alias)
    for addr, aliases_for_addr in by_addr.items():
        point = points.get(addr)
        if point is None:
            point = ModbusPoint(
                addr=addr,
                name=f"reg_{addr}",
                label=f"Register {addr}",
                kind="bitfield",
            )
            points[addr] = point
        if any(a["category"] == "alarm" for a in aliases_for_addr):
            point.kind = "alarm"
        else:
            point.kind = "bitfield"
            point.access = "rw"
        point.bits.extend(_dedupe_bits([
            {"bit": a["bit"], "name": a["name"], "label": a["label"]}
            for a in aliases_for_addr
        ]))


def _dedupe_bits(bits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for bit in sorted(bits, key=lambda b: b["bit"]):
        if bit["bit"] in seen:
            continue
        seen.add(bit["bit"])
        out.append({"bit": bit["bit"], "name": bit["name"], "label": bit["label"]})
    return out


def _build_commands_from_bits(points: dict[int, ModbusPoint]) -> list[dict[str, Any]]:
    commands = []
    for point in sorted(points.values(), key=lambda p: p.addr):
        if point.kind != "bitfield":
            continue
        for bit in point.bits:
            action = bit["name"].lower()
            action = re.sub(r"^(cmd|command)_", "", action)
            commands.append({
                "action": action,
                "label": bit.get("label", bit["name"]),
                "addr": point.addr,
                "bit": bit["bit"],
            })
    return commands


def _parse_records(records: list[dict[str, Any]], source_format: str, source_name: str) -> PointMap:
    points = []
    warnings = []
    for raw in records:
        rec = {_canonical_key(k): (v or "").strip() for k, v in raw.items() if k is not None}
        addr_s = rec.get("addr", "")
        if not addr_s:
            continue
        try:
            addr = int(addr_s, 0)
        except ValueError:
            warnings.append(f"Invalid address: {addr_s}")
            continue
        min_v, max_v = _parse_range(rec.get("range", ""))
        default = _parse_optional_int(rec.get("default", ""))
        point = ModbusPoint(
            addr=addr,
            name=_to_identifier(rec.get("name", "")) or f"reg_{addr}",
            label=rec.get("name", "") or f"Register {addr}",
            type=rec.get("type", "") or "uint16",
            access=(rec.get("access", "") or "ro").lower(),
            unit=rec.get("unit", ""),
            scale=_parse_scale(rec.get("scale", "")),
            kind=rec.get("kind", ""),
            min=min_v,
            max=max_v,
            default=default,
            bits=_parse_bits(rec.get("bit", "")),
            source=source_name,
        )
        if point.bits and not point.kind:
            point.kind = "bitfield"
        points.append(point)
    points.sort(key=lambda p: p.addr)
    pm = PointMap(source_format, [source_name], points, warnings=warnings)
    pm.commands = _build_commands_from_bits({p.addr: p for p in points})
    return pm


def _canonical_key(key: str) -> str:
    normalized = key.strip().lower()
    for canon, aliases in _NAME_ALIASES.items():
        if normalized in {a.lower() for a in aliases}:
            return canon
    return normalized


def _parse_range(value: str) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    match = re.search(r"(-?\d+)\s*(?:\.\.|~|-|至|到)\s*(-?\d+)", value)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _parse_optional_int(value: str) -> int | None:
    if value == "":
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _parse_scale(value: str) -> float | int:
    if not value:
        return 1
    try:
        parsed = float(value)
    except ValueError:
        return 1
    return int(parsed) if parsed.is_integer() else parsed


def _parse_bits(value: str) -> list[dict[str, Any]]:
    bits = []
    if not value:
        return bits
    for part in re.split(r"[;；,，]", value):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"(\d+)\s*[:：]\s*(.+)", part)
        if match:
            bit = int(match.group(1))
            label = match.group(2).strip()
        else:
            bit = int(part, 0)
            label = f"bit{bit}"
        bits.append({"bit": bit, "name": _to_identifier(label) or f"bit{bit}", "label": label})
    return bits


def _point_to_profile_reg(point: ModbusPoint) -> dict[str, Any]:
    item: dict[str, Any] = {
        "addr": point.addr,
        "type": point.type,
        "name": point.name,
        "label": point.label or _labelize(point.name),
        "access": point.access,
    }
    for key in ("unit", "kind", "bits"):
        value = getattr(point, key)
        if value:
            item[key] = value
    if point.scale != 1:
        item["scale"] = point.scale
    if point.min is not None:
        item["min"] = point.min
    if point.max is not None:
        item["max"] = point.max
    if point.default is not None:
        item["default"] = point.default
    if point.type in ("uint16", "int16") and point.access == "ro" and point.kind not in ("alarm", "bitfield"):
        item["chart"] = True
    return item


def _guess_kind(name: str, comment: str, addr: int) -> str:
    text = f"{name} {comment}".lower()
    if addr in (101, 112) or name.lower() in ("alarm", "alarm2") or name.lower().endswith("_alarm_reg"):
        return "alarm"
    if addr == 100 or ("command" in text and addr < 200) or name.lower() == "control_command":
        return "bitfield"
    return ""


def _normalize_reg_name(name: str) -> str:
    name = re.sub(r"^MODBUS_REG_", "", name)
    return _to_identifier(name.lower())


def _normalize_bit_name(name: str) -> str:
    name = re.sub(r"^MODBUS_REG_(COMMAND|ALARM)_", "", name)
    return _to_identifier(name.lower())


def _guess_unit(comment: str) -> str:
    text = comment.lower()
    if "0.1v" in text:
        return "0.1V"
    if re.search(r"\b(c|℃)\b", text):
        return "C"
    if "%" in text:
        return "%"
    if re.search(r"\bms\b", text):
        return "ms"
    if re.search(r"\bs\b", text):
        return "s"
    return ""


def _clean_comment(value: str | None) -> str:
    return (value or "").strip().strip("/* ").strip()


def _to_identifier(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    value = re.sub(r"[^0-9A-Za-z_]+", "_", value).strip("_")
    if not value:
        return ""
    if value[0].isdigit():
        value = f"reg_{value}"
    return value


def _labelize(name: str) -> str:
    return re.sub(r"[_\-]+", " ", name).strip().title()


def _rel(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return path
