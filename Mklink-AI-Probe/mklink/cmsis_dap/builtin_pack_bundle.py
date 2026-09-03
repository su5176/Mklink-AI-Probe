"""Load integrity-checked CMSIS-Pack algorithms bundled with the sidecar."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import List, Mapping, Optional

from .models import TargetRecord


class BuiltinPackBundleError(ValueError):
    """The packaged builtin algorithm bundle is malformed or corrupted."""


def _default_bundle_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")) / "mklink" / "builtin_packs"
    return Path(__file__).resolve().parents[1] / "builtin_packs"


def _text(value: object, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuiltinPackBundleError("{} must be a non-empty string".format(description))
    return value.strip()


def _optional_text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pack_path(root: Path, value: object) -> Path:
    relative_text = _text(value, "builtin pack file")
    relative = Path(relative_text)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise BuiltinPackBundleError("builtin pack file must be a safe relative path")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        raise BuiltinPackBundleError("builtin pack file escapes the bundle root")
    if resolved.suffix.casefold() != ".pack" or not resolved.is_file():
        raise BuiltinPackBundleError("builtin pack file is missing")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_builtin_pack_records(root: Optional[Path] = None) -> List[TargetRecord]:
    bundle_root = Path(root) if root is not None else _default_bundle_root()
    manifest_path = bundle_root / "manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BuiltinPackBundleError("builtin pack manifest is invalid: {}".format(error))
    if not isinstance(payload, Mapping) or payload.get("schema") != 1:
        raise BuiltinPackBundleError("builtin pack manifest schema is unsupported")
    packs = payload.get("packs")
    if not isinstance(packs, list):
        raise BuiltinPackBundleError("builtin pack manifest must contain a packs list")

    records = []  # type: List[TargetRecord]
    for pack_index, raw_pack in enumerate(packs):
        if not isinstance(raw_pack, Mapping):
            raise BuiltinPackBundleError("builtin pack entry must be an object")
        pack_id = _text(raw_pack.get("pack_id"), "builtin pack id")
        version = _text(raw_pack.get("version"), "builtin pack version")
        pack_path = _pack_path(bundle_root, raw_pack.get("file"))
        expected_digest = _text(raw_pack.get("sha256"), "builtin pack sha256").casefold()
        if len(expected_digest) != 64 or any(character not in "0123456789abcdef" for character in expected_digest):
            raise BuiltinPackBundleError("builtin pack sha256 is invalid")
        if _sha256(pack_path) != expected_digest:
            raise BuiltinPackBundleError("builtin pack integrity check failed")
        targets = raw_pack.get("targets")
        if not isinstance(targets, list) or not targets:
            raise BuiltinPackBundleError(
                "builtin pack {} has no target records".format(pack_index)
            )
        for raw_target in targets:
            if not isinstance(raw_target, Mapping):
                raise BuiltinPackBundleError("builtin target entry must be an object")
            records.append(TargetRecord(
                part_number=_text(raw_target.get("part_number"), "builtin target part number"),
                vendor=_text(raw_target.get("vendor"), "builtin target vendor"),
                pack_id=pack_id,
                pack_version=version,
                pack_path=str(pack_path),
                installed=True,
                source="bundle",
                family=_optional_text(raw_target.get("family")),
                series=_optional_text(
                    raw_target.get("series"),
                    raw_target.get("sub_family"),
                    raw_target.get("subfamily"),
                ),
            ))
    return records
