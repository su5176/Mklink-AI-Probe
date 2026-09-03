"""Load integrity-checked FLM algorithms extracted into the packaged sidecar."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import List, Mapping, Optional

from .models import TargetRecord


class BuiltinFlmBundleError(ValueError):
    """The packaged DAPLinkUtility FLM bundle is malformed or corrupted."""


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _default_bundle_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")) / "mklink" / "builtin_flm"
    override = os.environ.get("MKLINK_BUILTIN_FLM_ROOT", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "builtin_flm"


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
    manifest = _load_manifest(root)
    if manifest is None:
        return []
    records = manifest["targets"]
    assert isinstance(records, list)
    if any(not isinstance(record, Mapping) for record in records):
        raise BuiltinFlmBundleError("builtin FLM target is invalid")
    return records


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
    matches = [
        raw for raw in _target_records(bundle_root)
        if str(raw.get("part_number") or "").casefold() == target.casefold()
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
                source_name="DAPLinkUtility 内置算法",
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
