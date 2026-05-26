"""ELF/AXF memory map analysis using readelf text output."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess


@dataclass
class Section:
    name: str
    address: int
    size: int
    flags: str = ""


_SECTION_RE = re.compile(
    r"^\s*\[\s*\d+\]\s+(\S+)\s+\S+\s+([0-9a-fA-F]+)\s+\S+\s+([0-9a-fA-F]+)\s+\S+\s+([A-Z ]+)"
)


def parse_section_headers(output: str) -> list[Section]:
    sections: list[Section] = []
    for line in output.splitlines():
        m = _SECTION_RE.match(line)
        if not m:
            continue
        sections.append(Section(m.group(1), int(m.group(2), 16), int(m.group(3), 16), m.group(4).strip()))
    return sections


def summarize_sections(sections: list[Section], *, flash_size: int = 0, ram_size: int = 0) -> dict:
    flash_names = {".text", ".rodata", ".data", ".ARM.exidx", ".init_array", ".fini_array"}
    ram_names = {".data", ".bss", ".heap", ".stack"}
    flash_used = sum(s.size for s in sections if s.name in flash_names or 0x08000000 <= s.address < 0x10000000)
    ram_used = sum(s.size for s in sections if s.name in ram_names or 0x20000000 <= s.address < 0x40000000)
    return {
        "flash_used": flash_used,
        "flash_size": flash_size,
        "flash_percent": (flash_used / flash_size * 100.0) if flash_size else None,
        "ram_used": ram_used,
        "ram_size": ram_size,
        "ram_percent": (ram_used / ram_size * 100.0) if ram_size else None,
        "sections": [s.__dict__ for s in sections],
    }


def analyze_memmap(source: str, *, flash_size: int = 0, ram_size: int = 0) -> dict:
    result = subprocess.run(["arm-none-eabi-readelf", "-S", source], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "readelf -S failed")
    return summarize_sections(parse_section_headers(result.stdout), flash_size=flash_size, ram_size=ram_size)


def format_memmap(summary: dict) -> str:
    lines = ["Flash Usage", "-----------", f"Total   : {summary['flash_used']} bytes"]
    if summary.get("flash_size"):
        lines[-1] += f" / {summary['flash_size']} bytes ({summary['flash_percent']:.1f}%)"
    lines.extend(["", "RAM Usage", "---------", f"Total   : {summary['ram_used']} bytes"])
    if summary.get("ram_size"):
        lines[-1] += f" / {summary['ram_size']} bytes ({summary['ram_percent']:.1f}%)"
    return "\n".join(lines)


def format_memmap_json(summary: dict) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2)
