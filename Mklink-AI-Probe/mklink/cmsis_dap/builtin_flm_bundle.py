"""Load integrity-checked built-in FLM algorithms."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import List, Mapping, Optional

from .models import TargetRecord


class BuiltinFlmBundleError(ValueError):
    """The built-in FLM bundle is malformed or corrupted."""


@dataclass(frozen=True)
class BuiltinOptionAlgorithm:
    """Integrity metadata for an option-byte FLM kept outside auto Flash selection."""

    target_part: str
    file_name: str
    path: Path
    sha256: str
    ram_start: int
    ram_size: int


_SHA256 = re.compile(r"[0-9a-f]{64}")
_PY32F030_ORDER_CODE = re.compile(
    r"^(PY32F030)[A-Z][A-Z0-9]([4678])[A-Z][67](?:-[A-Z0-9]+)?$",
    re.IGNORECASE,
)
_TARGET_RECORD_CACHE: dict[
    Path, tuple[tuple[int, int], list[Mapping[str, object]]]
] = {}
_ALIAS_INDEX_CACHE: dict[
    int,
    tuple[
        list[Mapping[str, object]],
        dict[str, list[Mapping[str, object]]],
        list[tuple[int, str, tuple[object, ...], re.Pattern[str]]],
    ],
] = {}


def _default_bundle_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")) / "mklink" / "builtin_flm"
    override = os.environ.get("MKLINK_BUILTIN_FLM_ROOT", "").strip()
    if override:
        return Path(override)
    packaged = Path(__file__).resolve().parents[1] / "builtin_flm"
    if (packaged / "manifest.json").is_file():
        return packaged
    local = Path(__file__).resolve().parents[2] / "_maintainer" / "local" / "builtin_flm"
    return local if (local / "manifest.json").is_file() else packaged


def _text(value: object, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuiltinFlmBundleError("{} must be a non-empty string".format(description))
    return value.strip()


def _optional_text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool):
        raise BuiltinFlmBundleError("{} must be an integer".format(description))
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise BuiltinFlmBundleError("{} must be an integer".format(description)) from error
    if result < 0:
        raise BuiltinFlmBundleError("{} must be nonnegative".format(description))
    return result


def _load_manifest(root: Path) -> Mapping[str, object] | None:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuiltinFlmBundleError("builtin FLM manifest is invalid") from error
    if not isinstance(payload, Mapping) or payload.get("schema") != 1:
        raise BuiltinFlmBundleError("builtin FLM manifest schema is unsupported")
    if not isinstance(payload.get("targets"), list):
        raise BuiltinFlmBundleError("builtin FLM manifest targets are invalid")
    return payload


def _blob(root: Path, value: object, digest: object) -> tuple[Path, str]:
    relative = Path(_text(value, "builtin FLM blob"))
    expected = _text(digest, "builtin FLM SHA-256").casefold()
    if _SHA256.fullmatch(expected) is None:
        raise BuiltinFlmBundleError("builtin FLM SHA-256 is invalid")
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise BuiltinFlmBundleError("builtin FLM blob path is unsafe")
    root = root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise BuiltinFlmBundleError("builtin FLM blob escapes the bundle root") from error
    if path.suffix.casefold() != ".flm" or not path.is_file():
        raise BuiltinFlmBundleError("builtin FLM blob is missing")
    return path, expected


def _target_records(root: Path) -> list[Mapping[str, object]]:
    manifest_path = root / "manifest.json"
    try:
        stat = manifest_path.stat()
    except OSError:
        _TARGET_RECORD_CACHE.pop(root.resolve(), None)
        return []
    cache_key = root.resolve()
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _TARGET_RECORD_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    manifest = _load_manifest(root)
    if manifest is None:
        return []
    records = manifest["targets"]
    assert isinstance(records, list)
    if any(not isinstance(record, Mapping) for record in records):
        raise BuiltinFlmBundleError("builtin FLM target is invalid")
    _TARGET_RECORD_CACHE[cache_key] = (signature, records)
    return records


def _programming_signature(raw: Mapping[str, object]) -> tuple[object, ...]:
    algorithms = raw.get("algorithms")
    if not isinstance(algorithms, list):
        return ()
    result = []
    for item in algorithms:
        if not isinstance(item, Mapping) or item.get("automatic", True) is False:
            continue
        raw_sectors = item.get("sector_sizes", [])
        sectors = tuple(
            tuple(pair) for pair in raw_sectors
            if isinstance(pair, list) and len(pair) == 2
        ) if isinstance(raw_sectors, list) else ()
        result.append((
            str(item.get("sha256") or "").casefold(),
            item.get("flash_start"),
            item.get("flash_size"),
            item.get("page_size"),
            sectors,
        ))
    return (raw.get("ram_start"), raw.get("ram_size"), tuple(result))


def _generic_part_pattern(part_number: str) -> Optional[re.Pattern[str]]:
    has_wildcard = "x" in part_number.casefold() or "*" in part_number
    pieces = []
    for character in part_number:
        if character.casefold() == "x":
            pieces.append(r"[A-Z0-9]")
        elif character == "*":
            pieces.append(r"[A-Z0-9]*")
        else:
            pieces.append(re.escape(character))
    trailing = r"[A-Z0-9]{0,4}" if has_wildcard else r"[A-Z0-9]{1,4}"
    return re.compile(r"^{}{}$".format("".join(pieces), trailing), re.IGNORECASE)


def _resolution_index(records: list[Mapping[str, object]]):
    cached = _ALIAS_INDEX_CACHE.get(id(records))
    if cached is not None and cached[0] is records:
        return cached[1], cached[2]
    direct: dict[str, list[Mapping[str, object]]] = {}
    generic = []
    for raw in records:
        candidate = str(raw.get("part_number") or "").strip()
        direct.setdefault(candidate.casefold(), []).append(raw)
        pattern = _generic_part_pattern(candidate)
        if pattern is not None:
            literal_count = len(
                candidate.replace("x", "").replace("X", "").replace("*", "")
            )
            generic.append((literal_count, candidate, _programming_signature(raw), pattern))
    _ALIAS_INDEX_CACHE[id(records)] = (records, direct, generic)
    return direct, generic


def _resolve_part_from_index(
    part_number: str,
    direct: dict[str, list[Mapping[str, object]]],
    generic: list[tuple[int, str, tuple[object, ...], re.Pattern[str]]],
) -> Optional[str]:
    requested = str(part_number or "").strip()
    if not requested:
        return None
    exact = direct.get(requested.casefold(), [])
    if len(exact) == 1:
        return _text(exact[0].get("part_number"), "builtin FLM part number")
    py32 = _PY32F030_ORDER_CODE.fullmatch(requested)
    if py32 is not None:
        # Puya full order codes place the Flash-capacity character after the
        # pin/peripheral variant (for example K28T6 -> x8).  The CMSIS target
        # names omit that extra variant character, so a plain x-placeholder
        # match cannot resolve them.
        canonical = "{}x{}".format(py32.group(1), py32.group(2))
        aliases = direct.get(canonical.casefold(), [])
        if len(aliases) == 1:
            return _text(aliases[0].get("part_number"), "builtin FLM part number")
    candidates = [
        (literal_count, candidate, signature)
        for literal_count, candidate, signature, pattern in generic
        if pattern.fullmatch(requested) is not None
    ]
    if not candidates:
        return None
    best_score = max(item[0] for item in candidates)
    best = [item for item in candidates if item[0] == best_score]
    if len({item[2] for item in best}) != 1:
        return None
    return sorted((item[1] for item in best), key=str.casefold)[0]


def resolve_builtin_flm_parts(
    part_numbers: List[str],
    root: Optional[Path] = None,
) -> dict[str, str]:
    """Batch-resolve exact order codes while loading the large manifest once."""

    bundle_root = Path(root) if root is not None else _default_bundle_root()
    records = _target_records(bundle_root)
    direct, generic = _resolution_index(records)
    resolved = {}
    for part_number in part_numbers:
        canonical = _resolve_part_from_index(part_number, direct, generic)
        if canonical is not None:
            resolved[str(part_number).casefold()] = canonical
    return resolved


def resolve_builtin_flm_part(
    part_number: str,
    root: Optional[Path] = None,
) -> Optional[str]:
    """Resolve an exact order code to one capacity-consistent generic target."""

    return resolve_builtin_flm_parts([part_number], root).get(
        str(part_number or "").strip().casefold()
    )


def load_builtin_flm_targets(root: Optional[Path] = None) -> List[TargetRecord]:
    bundle_root = Path(root) if root is not None else _default_bundle_root()
    records = []
    for raw in _target_records(bundle_root):
        algorithms = raw.get("algorithms")
        if not isinstance(algorithms, list) or not any(
            isinstance(item, Mapping) and item.get("automatic", True) is not False
            for item in algorithms
        ):
            continue
        records.append(TargetRecord(
            part_number=_text(raw.get("part_number"), "builtin FLM part number"),
            vendor=_text(raw.get("manufacturer"), "builtin FLM manufacturer"),
            installed=True,
            source="daplink-builtin",
            family=_optional_text(raw.get("family")),
            series=_optional_text(
                raw.get("series"),
                raw.get("sub_family"),
                raw.get("subfamily"),
            ),
        ))
    return records


def discover_builtin_flm_algorithms(part_number: str, root: Optional[Path] = None):
    target = str(part_number or "").strip()
    if not target:
        return []
    from mklink.hpm_config import is_hpm_target

    if is_hpm_target(target):
        return []
    from .algorithm_catalog import FlashAlgorithm, _encode_target

    bundle_root = Path(root) if root is not None else _default_bundle_root()
    bundle_target = resolve_builtin_flm_part(target, bundle_root)
    if bundle_target is None:
        return []
    matches = [
        raw for raw in _target_records(bundle_root)
        if str(raw.get("part_number") or "").casefold() == bundle_target.casefold()
    ]
    algorithms = []
    for target_index, raw in enumerate(matches):
        raw_algorithms = raw.get("algorithms")
        if not isinstance(raw_algorithms, list):
            raise BuiltinFlmBundleError("builtin FLM target algorithms are invalid")
        ram_start = _integer(raw.get("ram_start"), "builtin FLM RAM start")
        ram_size = _integer(raw.get("ram_size"), "builtin FLM RAM size")
        for index, item in enumerate(raw_algorithms):
            if not isinstance(item, Mapping):
                raise BuiltinFlmBundleError("builtin FLM algorithm is invalid")
            if item.get("automatic", True) is False:
                continue
            path, digest = _blob(bundle_root, item.get("blob"), item.get("sha256"))
            flash_start = _integer(item.get("flash_start"), "builtin FLM flash start")
            flash_size = _integer(item.get("flash_size"), "builtin FLM flash size")
            page_size = _integer(item.get("page_size", 0), "builtin FLM page size")
            raw_sector_sizes = item.get("sector_sizes", [])
            if not isinstance(raw_sector_sizes, list):
                raise BuiltinFlmBundleError("builtin FLM sector sizes are invalid")
            sector_sizes = []
            for pair in raw_sector_sizes:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise BuiltinFlmBundleError("builtin FLM sector size is invalid")
                sector_sizes.append((
                    _integer(pair[0], "builtin FLM sector offset"),
                    _integer(pair[1], "builtin FLM sector size"),
                ))
            identity = "|".join((
                "daplink-builtin", target.casefold(), str(target_index), str(index),
                digest, hex(flash_start), hex(flash_size),
            ))
            algorithm_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            candidate = FlashAlgorithm(
                algorithm_id=algorithm_id,
                target_part=target,
                file_name=_text(item.get("file_name"), "builtin FLM file name"),
                flash_start=flash_start,
                flash_size=flash_size,
                ram_start=ram_start,
                ram_size=ram_size,
                default=index == 0,
                source_kind="daplink-builtin",
                source_name="常用型号内置算法",
                source_token="catalog:daplink:{}:{}".format(_encode_target(target), algorithm_id),
                builtin_blob_path=str(path),
                builtin_blob_sha256=digest,
                page_size=page_size,
                sector_sizes=tuple(sector_sizes),
            )
            identity_key = (
                candidate.file_name.casefold(),
                candidate.flash_start,
                candidate.flash_size,
                candidate.builtin_blob_sha256,
            )
            if not any(
                (
                    existing.file_name.casefold(),
                    existing.flash_start,
                    existing.flash_size,
                    existing.builtin_blob_sha256,
                ) == identity_key
                for existing in algorithms
            ):
                algorithms.append(candidate)
    return algorithms


def discover_builtin_option_algorithm(
    part_number: str,
    root: Optional[Path] = None,
) -> Optional[BuiltinOptionAlgorithm]:
    """Return one exact, integrity-checked option algorithm without auto-selecting it."""

    target = str(part_number or "").strip()
    if not target:
        return None
    bundle_root = Path(root) if root is not None else _default_bundle_root()
    bundle_target = resolve_builtin_flm_part(target, bundle_root)
    if bundle_target is None:
        return None
    matches = [
        raw for raw in _target_records(bundle_root)
        if str(raw.get("part_number") or "").casefold() == bundle_target.casefold()
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise BuiltinFlmBundleError("builtin option algorithm target is ambiguous")
    raw = matches[0]
    option = raw.get("option_algorithm")
    if option is None:
        return None
    if not isinstance(option, Mapping):
        raise BuiltinFlmBundleError("builtin option algorithm is invalid")
    path, digest = _blob(bundle_root, option.get("blob"), option.get("sha256"))
    return BuiltinOptionAlgorithm(
        target_part=_text(raw.get("part_number"), "builtin FLM part number"),
        file_name=_text(option.get("file_name"), "builtin option FLM file name"),
        path=path,
        sha256=digest,
        ram_start=_integer(raw.get("ram_start"), "builtin FLM RAM start"),
        ram_size=_integer(raw.get("ram_size"), "builtin FLM RAM size"),
    )


def extract_builtin_flm(algorithm: object) -> bytes:
    path = Path(_text(getattr(algorithm, "builtin_blob_path", None), "builtin FLM blob path"))
    expected = _text(
        getattr(algorithm, "builtin_blob_sha256", None), "builtin FLM SHA-256"
    ).casefold()
    if _SHA256.fullmatch(expected) is None or not path.is_file():
        raise BuiltinFlmBundleError("builtin FLM blob is unavailable")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise BuiltinFlmBundleError("builtin FLM blob integrity check failed")
    return payload
