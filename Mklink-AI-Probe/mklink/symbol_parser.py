"""ELF/AXF/MAP symbol parser for variable browsing.

Parses arm-none-eabi-readelf -s output to extract global/static variable
symbols within the RAM address range, enabling name-based variable lookup
for VOFA+ visualization and debugging.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional


# Default RAM address range for N32G43x (Cortex-M4, 32KB SRAM)
DEFAULT_RAM_START = 0x20000000
DEFAULT_RAM_END = 0x20008000


def parse_readelf_output(
    output: str,
    ram_start: int = DEFAULT_RAM_START,
    ram_end: int = DEFAULT_RAM_END,
) -> list[dict]:
    """Parse readelf -s output, return OBJECT symbols in RAM range.

    Each returned symbol dict has keys: name, address (hex string), type, size.

    Args:
        output: Raw stdout from arm-none-eabi-readelf -s <elf>
        ram_start: Start of RAM address range (inclusive)
        ram_end: End of RAM address range (exclusive)

    Returns:
        List of symbol dicts with keys: name, address, type, size
    """
    if not output or not output.strip():
        return []

    symbols = []

    line_re = re.compile(
        r'^\s*\d+:\s+([0-9a-fA-F]+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$'
    )

    for line in output.splitlines():
        m = line_re.match(line)
        if not m:
            continue

        addr_hex = m.group(1)
        size_str = m.group(2)
        sym_type = m.group(3)
        name = m.group(7).strip()

        if sym_type != "OBJECT":
            continue

        try:
            addr = int(addr_hex, 16)
        except ValueError:
            continue

        if addr < ram_start or addr >= ram_end:
            continue

        size = int(size_str)

        symbols.append({
            "name": name,
            "address": f"0x{addr:08x}",
            "type": sym_type,
            "size": size,
        })

    return symbols


# Default Flash address range for N32G43x (512KB Flash)
DEFAULT_FLASH_START = 0x08000000
DEFAULT_FLASH_END = 0x08080000


def parse_readelf_functions(
    output: str,
    flash_start: int = DEFAULT_FLASH_START,
    flash_end: int = DEFAULT_FLASH_END,
) -> list[dict]:
    """Parse readelf -s output, return FUNC symbols in Flash range.

    Each returned symbol dict has keys: name, address (hex string), type, size.
    """
    if not output or not output.strip():
        return []

    symbols = []

    line_re = re.compile(
        r'^\s*\d+:\s+([0-9a-fA-F]+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$'
    )

    for line in output.splitlines():
        m = line_re.match(line)
        if not m:
            continue

        addr_hex = m.group(1)
        size_str = m.group(2)
        sym_type = m.group(3)
        name = m.group(7).strip()

        if sym_type != "FUNC":
            continue

        try:
            addr = int(addr_hex, 16)
        except ValueError:
            continue

        if addr < flash_start or addr >= flash_end:
            continue

        size = int(size_str)
        if size == 0:
            continue

        symbols.append({
            "name": name,
            "address": f"0x{addr:08x}",
            "type": sym_type,
            "size": size,
        })

    return symbols


def resolve_function_address(output: str, name: str) -> int | None:
    """Resolve a function name to its Flash address.

    Returns the address as int, or None if not found.
    """
    funcs = parse_readelf_functions(output)
    for f in funcs:
        if f["name"] == name:
            return int(f["address"], 16)
    return None


def filter_symbols(symbols: list[dict], pattern: str) -> list[dict]:
    """Filter symbols by regex pattern on the name field.

    Args:
        symbols: List of symbol dicts from parse_readelf_output
        pattern: Python regex pattern to match against symbol names

    Returns:
        Filtered list of symbol dicts matching the pattern
    """
    try:
        regex = re.compile(pattern)
    except re.error:
        return []

    return [sym for sym in symbols if regex.search(sym["name"])]


def resolve_symbol_names(symbols: list[dict], names: list[str]) -> list[dict]:
    """Resolve symbol names to their full info (address, type, size).

    Args:
        symbols: Full list of symbol dicts to search in
        names: List of symbol names to resolve

    Returns:
        List of symbol dicts that were found (preserves request order)
    """
    if not names:
        return []

    # Build name -> symbol lookup
    by_name = {sym["name"]: sym for sym in symbols}

    result = []
    for name in names:
        if name in by_name:
            result.append(by_name[name])

    return result


def suggest_similar_symbols(
    symbols: list[dict],
    name: str,
    max_suggestions: int = 5,
) -> dict:
    """Suggest similar symbol names using Levenshtein edit distance.

    Args:
        symbols: Full list of symbol dicts to search in
        name: The unknown name to find suggestions for
        max_suggestions: Maximum number of suggestions to return

    Returns:
        Dict with keys:
          - error: str describing the unknown symbol
          - suggestions: list of symbol name strings, ranked by distance
    """
    distances = []
    for sym in symbols:
        d = _levenshtein(name, sym["name"])
        distances.append((d, sym["name"]))

    # Sort by distance (closest first)
    distances.sort(key=lambda x: x[0])

    suggestions = [name for _, name in distances[:max_suggestions]]

    return {
        "error": f"Symbol '{name}' not found",
        "suggestions": suggestions,
    }


def _levenshtein(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # j+1 instead of j since previous_row and current_row are one longer
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


class SymbolCache:
    """JSON-based cache for parsed symbol tables.

    Each ELF file's symbols are cached as a separate JSON file, keyed by
    a hash of the ELF file path. This avoids re-running readelf for the
    same binary across multiple invocations.
    """

    def __init__(self, cache_dir: str | None = None):
        if cache_dir is None:
            cache_dir = str(Path.home() / ".mklink" / "symbol_cache")
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_key(self, elf_path: str) -> str:
        """Generate a cache filename from the ELF path."""
        h = hashlib.md5(elf_path.encode("utf-8")).hexdigest()
        return f"symbols_{h}.json"

    def save(self, elf_path: str, symbols: list[dict]) -> None:
        """Save symbols list to cache."""
        cache_file = self._cache_dir / self._cache_key(elf_path)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(symbols, f, indent=2, ensure_ascii=False)

    def load(self, elf_path: str) -> Optional[list[dict]]:
        """Load symbols from cache. Returns None if not cached."""
        cache_file = self._cache_dir / self._cache_key(elf_path)
        if not cache_file.exists():
            return None
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None


# Backward-compatible aliases for contract compliance
parse_symbols_from_elf = parse_readelf_output
