#!/usr/bin/env python3
"""Install and validate the local built-in flash-algorithm asset bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


RELEASE_NOTE = "增加了常用型号"
EXPECTED_TARGET_COUNT = 7059
EXPECTED_BLOB_COUNT = 2224


def default_bundle_root(project_root: Path) -> Path:
    """Return the repository-local asset directory used by release builds."""
    import os

    configured = os.environ.get("MKLINK_BUILTIN_FLM_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(project_root).resolve() / "_maintainer" / "local" / "builtin_flm"


def _safe_relative_path(value: object) -> PurePosixPath:
    path = PurePosixPath(str(value))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe built-in algorithm path: {value}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _algorithm_entries(targets: Iterable[object]) -> Iterable[dict[str, object]]:
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("built-in algorithm target must be an object")
        algorithms = target.get("algorithms")
        if not isinstance(algorithms, list) or not algorithms:
            raise ValueError("every built-in target must have a programming algorithm")
        for algorithm in algorithms:
            if not isinstance(algorithm, dict):
                raise ValueError("built-in programming algorithm must be an object")
            yield algorithm
        option_algorithm = target.get("option_algorithm")
        if option_algorithm is not None:
            if not isinstance(option_algorithm, dict):
                raise ValueError("built-in option algorithm must be an object")
            yield option_algorithm


def validate_bundle(root: Path, *, release: bool = True) -> dict[str, object]:
    """Validate the complete catalog and every referenced FLM payload."""
    root = Path(root).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"built-in algorithm manifest does not exist: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("built-in algorithm manifest is invalid") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise ValueError("unsupported built-in algorithm manifest schema")
    if release and manifest.get("release_note") != RELEASE_NOTE:
        raise ValueError(f'built-in algorithm release note must be "{RELEASE_NOTE}"')

    targets = manifest.get("targets")
    blobs = manifest.get("blobs")
    if not isinstance(targets, list) or not isinstance(blobs, list):
        raise ValueError("built-in algorithm manifest targets/blobs must be arrays")
    if release and (
        len(targets) != EXPECTED_TARGET_COUNT
        or manifest.get("target_count") != EXPECTED_TARGET_COUNT
    ):
        raise ValueError(f"release requires {EXPECTED_TARGET_COUNT} built-in targets")
    if release and (
        len(blobs) != EXPECTED_BLOB_COUNT
        or manifest.get("blob_count") != EXPECTED_BLOB_COUNT
    ):
        raise ValueError(f"release requires {EXPECTED_BLOB_COUNT} built-in algorithm blobs")

    known_blobs: dict[str, tuple[PurePosixPath, int]] = {}
    listed_files: set[Path] = set()
    for blob in blobs:
        if not isinstance(blob, dict):
            raise ValueError("built-in algorithm blob must be an object")
        digest = str(blob.get("sha256", "")).lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("built-in algorithm blob has an invalid SHA-256")
        relative = _safe_relative_path(blob.get("file"))
        if relative.suffix.casefold() != ".flm":
            raise ValueError("built-in algorithm blob must use the .flm extension")
        size = blob.get("size")
        if not isinstance(size, int) or size <= 0:
            raise ValueError("built-in algorithm blob size must be positive")
        if digest in known_blobs:
            raise ValueError("duplicate built-in algorithm blob SHA-256")
        path = root.joinpath(*relative.parts)
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError(f"built-in algorithm blob is missing or has the wrong size: {relative}")
        if _sha256(path) != digest:
            raise ValueError(f"built-in algorithm blob failed SHA-256 validation: {relative}")
        known_blobs[digest] = (relative, size)
        listed_files.add(path.resolve())

    referenced: set[str] = set()
    for algorithm in _algorithm_entries(targets):
        digest = str(algorithm.get("sha256", "")).lower()
        relative = _safe_relative_path(algorithm.get("blob"))
        record = known_blobs.get(digest)
        if record is None or record[0] != relative:
            raise ValueError("built-in target references an unknown algorithm blob")
        referenced.add(digest)
    if referenced != set(known_blobs):
        raise ValueError("built-in algorithm manifest contains unreferenced blobs")

    actual_files = {path.resolve() for path in (root / "blobs").rglob("*.flm")}
    if actual_files != listed_files:
        raise ValueError("built-in algorithm directory contains unlisted FLM files")
    return manifest


def install_bundle(source: Path, destination: Path) -> dict[str, object]:
    """Copy an existing complete bundle into the stable local asset directory."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f"built-in algorithm destination already exists: {destination}")
    original = validate_bundle(source, release=False)
    if len(original["targets"]) != EXPECTED_TARGET_COUNT:
        raise ValueError(f"source must contain {EXPECTED_TARGET_COUNT} built-in targets")
    if len(original["blobs"]) != EXPECTED_BLOB_COUNT:
        raise ValueError(f"source must contain {EXPECTED_BLOB_COUNT} built-in algorithm blobs")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / "blobs", destination / "blobs")
    sanitized = {
        "schema": 1,
        "release_note": RELEASE_NOTE,
        "target_count": EXPECTED_TARGET_COUNT,
        "blob_count": EXPECTED_BLOB_COUNT,
        "blobs": original["blobs"],
        "targets": original["targets"],
    }
    (destination / "manifest.json").write_text(
        json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return validate_bundle(destination)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--source", required=True, type=Path)
    install.add_argument("--destination", required=True, type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "install":
        manifest = install_bundle(args.source, args.destination)
    else:
        manifest = validate_bundle(args.root)
    print(json.dumps({
        "release_note": manifest["release_note"],
        "target_count": manifest["target_count"],
        "blob_count": manifest["blob_count"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
