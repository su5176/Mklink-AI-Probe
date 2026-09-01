"""Build and audit the standalone Windows Site Agent candidate."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import marshal
import os
import platform
import re
import shutil
import subprocess
import sys
import types
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import quote, unquote


PACKAGE_NAME = "mklink-remote-site-agent-windows-x86_64"
GENERATOR_VERSION = "2.0.0"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_FILE_MODE = 0o100644
PROHIBITED_SUFFIXES = {
    ".axf",
    ".bin",
    ".elf",
    ".flm",
    ".hex",
    ".jpg",
    ".jpeg",
    ".log",
    ".pack",
    ".png",
    ".screenshot",
}
EXCLUDED_NAME_PARTS = {
    "__pycache__",
    ".egg-info",
    "fastmcp",
    "pyinstaller",
}
LOCAL_INSTALL_METADATA_NAMES = {
    "direct_url.json",
}
EXCLUDED_ARCHIVE_NAMES = {
    "fastapi",
    "fastmcp",
    "mklink.cmsis_dap.builtin_flm_bundle",
    "mklink.cmsis_dap.builtin_pack_bundle",
    "mklink.mcp_server",
    "mklink.remote.api",
    "mklink.remote.mcp",
    "mklink.remote.stream_api",
    "pyinstaller",
    "starlette",
    "uvicorn",
}
PROHIBITED_TUNNEL_ARCHIVE_PARTS = {
    "frp",
    "frpc",
    "frps",
    "sitetunnel",
}
NO_CREDENTIAL_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)
PRINTABLE_ASCII = re.compile(rb"[\x20-\x7e]{4,}")
PRINTABLE_UTF16_LE = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
ABSOLUTE_FILE_URL = re.compile(
    r"(?i)(?<![a-z0-9])file:/+[^\s\"'<>|\x00]+",
)
DRIVE_LOCAL_PATH = re.compile(
    r"(?i)(?<![a-z0-9])([a-z]:[\\/][a-z0-9 _.\-\\/]+)",
)


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    display = " ".join(
        Path(item).name if Path(item).is_absolute() else item
        for item in command
    )
    print(f"+ {display}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> Iterable[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _source_input_records(
    root: Path,
    script_dir: Path,
) -> tuple[list[dict[str, object]], str]:
    inputs = [root / "pyproject.toml"]
    inputs.extend(_files(root / "mklink"))
    # The GUI wrapper consumes the completed core ZIP and pins its hash.
    # Excluding that downstream packager avoids a circular core hash.
    inputs.extend(
        path for path in _files(script_dir) if path.name != "build_gui.py"
    )
    inputs.extend(
        path
        for path in _files(root / "native" / "stcp_bridge")
        if "build" not in {
            part.casefold()
            for part in path.relative_to(root / "native" / "stcp_bridge").parts
        }
    )
    records: list[dict[str, object]] = []
    digest = hashlib.sha256()
    for path in sorted(
        set(inputs),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        lowered_parts = {part.casefold() for part in path.parts}
        if (
            "__pycache__" in lowered_parts
            or path.suffix.casefold() in {".pyc", ".pyo"}
            or any(part.casefold().endswith(".egg-info") for part in path.parts)
        ):
            continue
        data = path.read_bytes()
        record = {
            "path": relative,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        records.append(record)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\n")
    return records, digest.hexdigest()


def _project_version(root: Path) -> str:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    project_table = re.search(
        r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)",
        pyproject,
    )
    if project_table is None:
        raise RuntimeError("pyproject.toml has no [project] table")
    version = re.search(
        r'(?m)^version\s*=\s*"(?P<value>[^"\r\n]+)"\s*(?:#.*)?$',
        project_table.group("body"),
    )
    if version is None:
        raise RuntimeError("pyproject.toml has no static project version")
    return version.group("value")


def _load_provenance(
    script_dir: Path,
    *,
    product_version: str,
) -> dict[str, object]:
    provenance_path = script_dir / "build-provenance.json"
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid Site Agent build provenance") from exc
    required_hashes = (
        "base_git_sha",
        "upstream_git_sha",
        "upstream_merge_sha",
    )
    for name in required_hashes:
        value = provenance.get(name)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise RuntimeError(f"invalid Site Agent provenance field: {name}")
    if provenance.get("product_version") != product_version:
        raise RuntimeError(
            "Site Agent provenance product version does not match pyproject.toml"
        )
    return provenance


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        raise RuntimeError(f"invalid wheel requirement: {requirement}")
    return match.group(1).replace("_", "-").casefold()


def _wheel_contract(
    wheel: Path,
    *,
    product_version: str,
) -> dict[str, object]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name
            for name in archive.namelist()
            if name.casefold().endswith(".dist-info/metadata")
            and name.casefold().startswith("mklink-")
        ]
        if len(metadata_names) != 1:
            raise RuntimeError("expected exactly one Mklink wheel METADATA")
        metadata_text = archive.read(metadata_names[0]).decode("utf-8")

    fields: dict[str, list[str]] = {}
    current_name: str | None = None
    for line in metadata_text.splitlines():
        if not line:
            current_name = None
            continue
        if line[:1].isspace() and current_name is not None:
            fields[current_name][-1] += line.strip()
            continue
        name, separator, value = line.partition(":")
        if not separator:
            continue
        current_name = name.casefold()
        fields.setdefault(current_name, []).append(value.strip())

    version_values = fields.get("version", [])
    if version_values != [product_version]:
        raise RuntimeError(f"unexpected Mklink wheel version: {version_values}")
    requirements = sorted(fields.get("requires-dist", []), key=str.casefold)
    core = sorted(
        (
            item
            for item in requirements
            if "extra ==" not in item.casefold()
        ),
        key=str.casefold,
    )
    extras: dict[str, list[str]] = {}
    for extra in ("remote", "mcp", "site-agent-build"):
        marker_double = f'extra == "{extra}"'
        marker_single = f"extra == '{extra}'"
        extras[extra] = sorted(
            (
                item
                for item in requirements
                if marker_double in item.casefold()
                or marker_single in item.casefold()
            ),
            key=str.casefold,
        )

    core_names = {_requirement_name(item) for item in core}
    remote_names = {_requirement_name(item) for item in extras["remote"]}
    mcp_names = {_requirement_name(item) for item in extras["mcp"]}
    build_names = {
        _requirement_name(item)
        for item in extras["site-agent-build"]
    }
    if not {"pycparser", "websockets"}.issubset(core_names):
        raise RuntimeError("core runtime must include pycparser and websockets")
    if not {"websockets", "intelhex"}.issubset(remote_names):
        raise RuntimeError("remote extra must include websockets and intelhex")
    if "fastmcp" not in mcp_names:
        raise RuntimeError("mcp extra must remain explicitly separate")
    if not {"build", "pyinstaller", "setuptools", "wheel"}.issubset(build_names):
        raise RuntimeError("site-agent-build extra is incomplete")

    return {
        "product": "mklink",
        "version": version_values[0],
        "requires_python": fields.get("requires-python", [""])[0],
        "core": core,
        "remote_extra": extras["remote"],
        "mcp_extra": extras["mcp"],
        "site_agent_build_extra": extras["site-agent-build"],
    }


def _validated_cleanup(stage: Path, output: Path) -> None:
    stage = stage.resolve()
    output = output.resolve()
    if stage.parent != output or stage.name != ".site-agent-build":
        raise RuntimeError("refusing to clean an unexpected staging path")
    if stage.exists():
        shutil.rmtree(stage)


def _resolve_worktree_root(product_source_root: Path) -> Path:
    product_source_root = product_source_root.expanduser().resolve()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(product_source_root),
                "rev-parse",
                "--show-toplevel",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            return product_source_root
        candidate = Path(lines[0]).expanduser()
        if not candidate.is_absolute():
            return product_source_root
        candidate = candidate.resolve()
        if (
            candidate.is_dir()
            and (
                candidate == product_source_root
                or candidate in product_source_root.parents
            )
        ):
            return candidate
    except (OSError, subprocess.SubprocessError):
        pass
    return product_source_root


def _path_text_variants(path: Path) -> set[str]:
    resolved = path.expanduser().resolve()
    native = str(resolved)
    if native.startswith("\\\\?\\"):
        native = native[4:]
    forward = native.replace("\\", "/")
    path_encoded = quote(forward, safe="/:")
    fully_encoded_forward = quote(forward, safe="")
    fully_encoded_native = quote(native, safe="")
    variants = {
        native,
        native.replace("\\", "\\\\"),
        forward,
        path_encoded,
        fully_encoded_forward,
        fully_encoded_native,
    }
    if len(forward) >= 3 and forward[1:3] == ":/":
        variants.update(
            {
                f"file:/{forward}",
                f"file:///{forward}",
                f"file:/{path_encoded}",
                f"file:///{path_encoded}",
                f"file:/{fully_encoded_forward}",
                f"file:///{fully_encoded_forward}",
                f"file:/{fully_encoded_native}",
                f"file:///{fully_encoded_native}",
            }
        )
    else:
        variants.add(resolved.as_uri())
    return {variant.casefold() for variant in variants if variant}


def _path_policy(forbidden_paths: Iterable[Path]) -> dict[str, object]:
    marker_texts: set[str] = set()
    for path in forbidden_paths:
        marker_texts.update(_path_text_variants(path))
    marker_bytes: set[bytes] = set()
    for marker in marker_texts:
        marker_bytes.add(marker.encode("utf-8"))
        marker_bytes.add(marker.encode("utf-16-le"))

    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    allowed_system_roots = {
        str(system_root).replace("\\", "/").casefold().rstrip("/"),
    }
    return {
        "markers": tuple(sorted(marker_bytes)),
        "system_roots": tuple(sorted(allowed_system_roots)),
    }


def _printable_strings(data: bytes) -> Iterable[str]:
    for match in PRINTABLE_ASCII.finditer(data):
        yield match.group().decode("ascii", errors="ignore")
    for match in PRINTABLE_UTF16_LE.finditer(data):
        yield match.group().decode("utf-16-le", errors="ignore")


def _local_path_from_file_url(value: str) -> str:
    value = unquote(value).replace("\\", "/")
    payload = value.split(":", 1)[1].lstrip("/")
    return payload


def _is_allowed_system_path(value: str, policy: dict[str, object]) -> bool:
    normalized = unquote(value).replace("\\", "/").casefold()
    if normalized.startswith("file:"):
        normalized = _local_path_from_file_url(normalized).casefold()
    normalized = normalized.rstrip(".,;:)]}")
    for root in policy["system_roots"]:
        if normalized == root or normalized.startswith(f"{root}/"):
            return True
    return False


def _audit_content(
    label: str,
    data: bytes,
    policy: dict[str, object],
    *,
    scan_generic_paths: bool = True,
    allow_static_drive_paths: bool = False,
    allow_pe_provenance_paths: bool = False,
) -> None:
    lowered_data = data.lower()
    for marker in policy["markers"]:
        if marker and marker in lowered_data:
            raise RuntimeError(f"PROHIBITED build-machine path in {label}")
    if any(marker in data for marker in NO_CREDENTIAL_MARKERS):
        raise RuntimeError(f"PROHIBITED credential material in {label}")

    if not scan_generic_paths:
        return
    for printable in _printable_strings(data):
        for match in ABSOLUTE_FILE_URL.finditer(printable):
            candidate = match.group()
            if not _local_path_from_file_url(candidate):
                # Standard-library code contains the URI scheme delimiter as
                # a standalone constant. It is not a local origin.
                continue
            if not _is_allowed_system_path(candidate, policy):
                raise RuntimeError(f"PROHIBITED local file URL in {label}")
        for match in DRIVE_LOCAL_PATH.finditer(printable):
            candidate = match.group(1)
            if (
                not _is_allowed_system_path(candidate, policy)
                and not allow_static_drive_paths
                and not allow_pe_provenance_paths
            ):
                raise RuntimeError(f"PROHIBITED drive-local path in {label}")


def _audit_code_object(
    label: str,
    code: types.CodeType,
    policy: dict[str, object],
) -> None:
    filenames: list[str] = []
    strings: list[str] = []
    byte_values: list[bytes] = []

    def visit(item: types.CodeType) -> None:
        filenames.append(item.co_filename)
        strings.extend((item.co_name, item.co_qualname))
        for constant in item.co_consts:
            if isinstance(constant, types.CodeType):
                visit(constant)
            elif isinstance(constant, str):
                strings.append(constant)
            elif isinstance(constant, bytes):
                byte_values.append(constant)

    visit(code)
    _audit_content(
        f"{label} code filenames",
        "\n".join(filenames).encode("utf-8", errors="surrogatepass"),
        policy,
    )
    _audit_content(
        f"{label} repository/runtime string constants",
        "\n".join(strings).encode("utf-8", errors="surrogatepass"),
        policy,
        allow_static_drive_paths=True,
    )
    for index, value in enumerate(byte_values):
        _audit_content(
            f"{label} bytes constant {index}",
            value,
            policy,
            scan_generic_paths=False,
        )


def _try_audit_marshaled_code(
    label: str,
    data: bytes,
    policy: dict[str, object],
    *,
    pyc_header: bool = False,
) -> bool:
    offsets = (16, 12, 8) if pyc_header else (0,)
    for offset in offsets:
        try:
            value = marshal.loads(data[offset:])
        except (EOFError, TypeError, ValueError):
            continue
        if isinstance(value, types.CodeType):
            _audit_code_object(label, value, policy)
            return True
    return False


def _audit_name(relative: str) -> None:
    normalized = relative.replace("\\", "/")
    parts = [part.casefold() for part in normalized.split("/") if part]
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"(?i)^[a-z]:/", normalized)
        or any(part == ".." for part in parts)
    ):
        raise RuntimeError(f"PROHIBITED package path: {relative}")
    if Path(normalized).suffix.casefold() in PROHIBITED_SUFFIXES:
        raise RuntimeError(f"PROHIBITED package suffix: {relative}")
    if any(name in parts for name in LOCAL_INSTALL_METADATA_NAMES):
        raise RuntimeError(f"PROHIBITED local install metadata: {relative}")
    if any(
        excluded in part
        for part in parts
        for excluded in EXCLUDED_NAME_PARTS
    ):
        raise RuntimeError(f"excluded package entry: {relative}")


def _remove_local_install_metadata(bundle: Path) -> list[str]:
    removed: list[str] = []
    for path in _files(bundle):
        relative = path.relative_to(bundle)
        if (
            path.name.casefold() in LOCAL_INSTALL_METADATA_NAMES
            and any(part.casefold().endswith(".dist-info") for part in relative.parts)
        ):
            path.unlink()
            removed.append(relative.as_posix())
    return sorted(removed)


def _record_hash(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    return f"sha256={encoded.rstrip(b'=').decode('ascii')}"


def _record_target(
    record: Path,
    row_path: str,
) -> tuple[str, Path | None, str | None]:
    normalized = row_path.replace("\\", "/")
    if "\x00" in normalized:
        return normalized, None, "invalid"
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or re.match(r"(?i)^[a-z]:/", normalized)
        or any(part == ".." for part in pure.parts)
    ):
        return normalized, None, "out_of_bundle"
    canonical = pure.as_posix()
    if canonical in {"", "."}:
        return canonical, None, "invalid"
    if (
        pure.suffix.casefold() == ".pyc"
        or any(part.casefold() == "__pycache__" for part in pure.parts)
    ):
        return canonical, None, "bytecode_cache"
    if any(
        part.casefold() in LOCAL_INSTALL_METADATA_NAMES
        for part in pure.parts
    ):
        return canonical, None, "local_origin"

    site_root = record.parent.parent.resolve()
    target = site_root.joinpath(*pure.parts).resolve()
    try:
        target.relative_to(site_root)
    except ValueError:
        return canonical, None, "out_of_bundle"
    if not target.is_file():
        return canonical, None, "not_bundled"
    return canonical, target, None


def _serialize_record_rows(rows: Iterable[tuple[str, str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(
        sorted(rows, key=lambda row: (row[0].casefold(), row[0]))
    )
    return output.getvalue().encode("utf-8")


def _normalize_distribution_records(
    bundle: Path,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    record_paths = [
        path
        for path in _files(bundle)
        if path.name.casefold() == "record"
        and path.parent.name.casefold().endswith(".dist-info")
    ]
    for record in record_paths:
        try:
            source_rows = list(
                csv.reader(io.StringIO(record.read_text(encoding="utf-8")))
            )
        except (csv.Error, UnicodeError) as exc:
            raise RuntimeError(f"invalid distribution RECORD: {record}") from exc

        retained: dict[str, tuple[str, str, str]] = {}
        removed = {
            "bytecode_cache": 0,
            "invalid": 0,
            "local_origin": 0,
            "not_bundled": 0,
            "out_of_bundle": 0,
        }
        for row in source_rows:
            if len(row) != 3:
                raise RuntimeError(f"malformed distribution RECORD row: {record}")
            canonical, target, reason = _record_target(record, row[0])
            if reason is not None:
                removed[reason] += 1
                continue
            assert target is not None
            key = canonical.casefold()
            if key in retained:
                raise RuntimeError(
                    f"duplicate distribution RECORD path: {record}: {canonical}"
                )
            if target == record.resolve():
                retained[key] = (canonical, "", "")
            else:
                data = target.read_bytes()
                retained[key] = (
                    canonical,
                    _record_hash(data),
                    str(len(data)),
                )

        site_root = record.parent.parent.resolve()
        self_path = record.resolve().relative_to(site_root).as_posix()
        retained[self_path.casefold()] = (self_path, "", "")
        normalized_data = _serialize_record_rows(retained.values())
        record.write_bytes(normalized_data)
        results.append(
            {
                "path": record.relative_to(bundle).as_posix(),
                "original_rows": len(source_rows),
                "retained_rows": len(retained),
                "removed_rows": removed,
            }
        )
    return results


def _audit_distribution_records(bundle: Path) -> None:
    record_paths = [
        path
        for path in _files(bundle)
        if path.name.casefold() == "record"
        and path.parent.name.casefold().endswith(".dist-info")
    ]
    for record in record_paths:
        data = record.read_bytes()
        try:
            rows = list(
                csv.reader(
                    io.StringIO(data.decode("utf-8"), newline="")
                )
            )
        except (csv.Error, UnicodeError) as exc:
            raise RuntimeError(f"invalid normalized RECORD: {record}") from exc
        if not rows:
            raise RuntimeError(f"empty normalized RECORD: {record}")

        validated: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for row in rows:
            if len(row) != 3:
                raise RuntimeError(f"malformed normalized RECORD row: {record}")
            canonical, target, reason = _record_target(record, row[0])
            if reason is not None or canonical != row[0] or target is None:
                raise RuntimeError(
                    f"unsafe or stale normalized RECORD row: {record}: {row[0]}"
                )
            key = canonical.casefold()
            if key in seen:
                raise RuntimeError(
                    f"duplicate normalized RECORD path: {record}: {canonical}"
                )
            seen.add(key)
            if target == record.resolve():
                expected = (canonical, "", "")
            else:
                target_data = target.read_bytes()
                expected = (
                    canonical,
                    _record_hash(target_data),
                    str(len(target_data)),
                )
            if tuple(row) != expected:
                raise RuntimeError(
                    f"normalized RECORD integrity mismatch: {record}: {canonical}"
                )
            validated.append(expected)
        if data != _serialize_record_rows(validated):
            raise RuntimeError(f"non-canonical normalized RECORD: {record}")


def _audit_bundle(
    bundle: Path,
    *,
    policy: dict[str, object],
) -> list[dict[str, object]]:

    _audit_distribution_records(bundle)
    records: list[dict[str, object]] = []
    for path in _files(bundle):
        relative = path.relative_to(bundle).as_posix()
        _audit_name(relative)
        data = path.read_bytes()
        _audit_content(
            f"bundle file {relative}",
            data,
            policy,
            scan_generic_paths=not relative.casefold().endswith(
                "base_library.zip"
            ),
            allow_pe_provenance_paths=(
                data.startswith(b"MZ")
                and path.suffix.casefold() in {".dll", ".exe", ".pyd"}
            ),
        )
        records.append(
            {
                "mode": "0644",
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    return records


def _audit_pyinstaller_archive(
    executable: Path,
    *,
    site_packages: Path,
    policy: dict[str, object],
) -> dict[str, object]:
    sys.path.insert(0, str(site_packages))
    try:
        from PyInstaller.archive.readers import CArchiveReader

        reader = CArchiveReader(str(executable))
        names: list[str] = []

        def audit_name(name: str) -> None:
            _audit_content(f"archive entry name {name}", name.encode("utf-8"), policy)
            lowered_name = name.casefold()
            if any(excluded in lowered_name for excluded in EXCLUDED_ARCHIVE_NAMES):
                raise RuntimeError(f"excluded archive entry: {name}")
            name_parts = {
                part
                for part in re.split(r"[^a-z0-9_+-]+", lowered_name)
                if part
            }
            if PROHIBITED_TUNNEL_ARCHIVE_PARTS.intersection(name_parts):
                raise RuntimeError(f"prohibited tunnel archive entry: {name}")

        def record(name: str, data: bytes | None) -> None:
            names.append(name)
            audit_name(name)
            if data is not None:
                if not _try_audit_marshaled_code(
                    f"archive entry {name}",
                    data,
                    policy,
                ):
                    _audit_content(f"archive entry {name}", data, policy)

        for name in sorted(reader.toc):
            try:
                data = reader.extract(name)
            except Exception:
                data = None
            try:
                embedded = reader.open_embedded_archive(name)
            except Exception:
                embedded = None
            # An embedded PYZ is a compressed container. Scanning its encoded
            # bytes as printable text produces random drive-like sequences;
            # scan its name and every decompressed module below instead.
            record(
                name,
                (
                    data
                    if embedded is None and isinstance(data, bytes)
                    else None
                ),
            )
            if embedded is not None:
                for child_name in sorted(embedded.toc):
                    child_data = embedded.extract(child_name)
                    child_label = f"{name}::{child_name}"
                    if isinstance(child_data, types.CodeType):
                        names.append(child_label)
                        audit_name(child_label)
                        _audit_code_object(child_label, child_data, policy)
                    else:
                        record(
                            child_label,
                            child_data if isinstance(child_data, bytes) else None,
                        )
            if (
                isinstance(data, bytes)
                and name.casefold().endswith(".zip")
            ):
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as embedded_zip:
                        for info in sorted(
                            embedded_zip.infolist(),
                            key=lambda item: item.filename,
                        ):
                            if info.is_dir():
                                continue
                            child_label = f"{name}::{info.filename}"
                            child_data = embedded_zip.read(info)
                            if (
                                info.filename.casefold().endswith(".pyc")
                                and _try_audit_marshaled_code(
                                    child_label,
                                    child_data,
                                    policy,
                                    pyc_header=True,
                                )
                            ):
                                names.append(child_label)
                                audit_name(child_label)
                            else:
                                record(child_label, child_data)
                except zipfile.BadZipFile:
                    pass
    finally:
        sys.path.remove(str(site_packages))

    module_names = {
        name.split("::", 1)[1].casefold()
        for name in names
        if "::" in name
    }
    required_roots = (
        "elftools",
        "intelhex",
        "pymodbus",
        "pycparser",
        "serial",
        "websockets",
    )
    for root_name in required_roots:
        if not any(
            name == root_name or name.startswith(f"{root_name}.")
            for name in module_names
        ):
            raise RuntimeError(
                f"required Site Agent runtime module is absent: {root_name}"
            )

    encoded_names = "\n".join(names).encode("utf-8")
    return {
        "entries": len(names),
        "names_sha256": hashlib.sha256(encoded_names).hexdigest(),
    }


def _bundle_distributions(
    bundle: Path,
    *,
    product_version: str,
) -> list[dict[str, str]]:
    distributions: list[dict[str, str]] = []
    for metadata in _files(bundle):
        if (
            metadata.name.casefold() != "metadata"
            or not metadata.parent.name.casefold().endswith(".dist-info")
        ):
            continue
        name = ""
        version = ""
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if line.casefold().startswith("name:"):
                name = line.split(":", 1)[1].strip()
            elif line.casefold().startswith("version:"):
                version = line.split(":", 1)[1].strip()
            if name and version:
                break
        if not name or not version:
            raise RuntimeError(f"incomplete bundled distribution metadata: {metadata}")
        distributions.append(
            {
                "name": name,
                "version": version,
            }
        )
    distributions.sort(key=lambda item: (item["name"].casefold(), item["version"]))
    available = {
        (item["name"].replace("_", "-").casefold(), item["version"])
        for item in distributions
    }
    if ("mklink", product_version) not in available:
        raise RuntimeError(
            f"bundled Mklink v{product_version} metadata is absent"
        )
    if not any(name == "pycparser" for name, _version in available):
        raise RuntimeError("bundled pycparser metadata is absent")
    return distributions


def _audit_zip(
    artifact: Path,
    *,
    bundle_name: str,
    records: list[dict[str, object]],
    policy: dict[str, object],
) -> None:
    expected = {
        f"{bundle_name}/{record['path']}": record
        for record in records
    }
    with zipfile.ZipFile(artifact) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos if not info.is_dir()]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise RuntimeError("ZIP and audited bundle path sets differ")
        for info in infos:
            if info.is_dir():
                continue
            _audit_name(info.filename)
            if info.date_time != FIXED_ZIP_TIME:
                raise RuntimeError(f"non-deterministic ZIP timestamp: {info.filename}")
            if info.external_attr >> 16 != FIXED_FILE_MODE:
                raise RuntimeError(f"non-deterministic ZIP mode: {info.filename}")
            data = archive.read(info)
            _audit_content(
                f"ZIP member {info.filename}",
                data,
                policy,
                scan_generic_paths=not info.filename.casefold().endswith(
                    "base_library.zip"
                ),
                allow_pe_provenance_paths=(
                    data.startswith(b"MZ")
                    and Path(info.filename).suffix.casefold()
                    in {".dll", ".exe", ".pyd"}
                ),
            )
            record = expected[info.filename]
            if (
                len(data) != record["size"]
                or hashlib.sha256(data).hexdigest() != record["sha256"]
            ):
                raise RuntimeError(f"ZIP member integrity mismatch: {info.filename}")


def _deterministic_zip(bundle: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in _files(bundle):
            relative = (Path(bundle.name) / path.relative_to(bundle)).as_posix()
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = FIXED_FILE_MODE << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)


def _resolve_stcp_library(root: Path, configured: Path | None) -> Path:
    candidate = configured
    if candidate is None:
        environment_value = os.environ.get("MKLINK_STCP_LIBRARY", "").strip()
        candidate = (
            Path(environment_value)
            if environment_value
            else root / "native" / "stcp_bridge" / "build" / "mklink-stcp.dll"
        )
    candidate = candidate.expanduser().resolve()
    try:
        with candidate.open("rb") as source:
            mz_header = source.read(2)
    except OSError:
        mz_header = b""
    if (
        not candidate.is_file()
        or candidate.suffix.casefold() != ".dll"
        or "frpc" in candidate.name.casefold()
        or mz_header != b"MZ"
    ):
        raise RuntimeError(
            "a valid in-process mklink-stcp.dll is required; frpc.exe is not accepted"
        )
    return candidate


def build(
    output: Path,
    *,
    stcp_library: Path | None = None,
) -> tuple[Path, Path]:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("this build target requires x86_64 Windows")

    script_dir = Path(__file__).resolve().parent
    root = script_dir.parents[1]
    worktree_root = _resolve_worktree_root(root)
    product_version = _project_version(root)
    provenance = _load_provenance(
        script_dir,
        product_version=product_version,
    )
    source_input_records, source_input_sha256 = _source_input_records(
        root,
        script_dir,
    )
    stcp_library = _resolve_stcp_library(root, stcp_library)
    stcp_library_sha256 = _sha256(stcp_library)
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"{PACKAGE_NAME}.zip"
    manifest = output / f"{PACKAGE_NAME}.manifest.json"
    # Never leave a previously invalidated candidate looking current while a
    # replacement build is in progress or has failed.
    artifact.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    stage = output / ".site-agent-build"
    _validated_cleanup(stage, output)
    stage.mkdir()
    runtime_temp = stage / "tmp"
    runtime_temp.mkdir()
    pip_cache = stage / "pip-cache"
    pip_cache.mkdir()
    pyinstaller_config = stage / "pyinstaller-config"
    pyinstaller_config.mkdir()

    source_root = stage / "source"
    source_root.mkdir()
    shutil.copy2(root / "pyproject.toml", source_root / "pyproject.toml")
    shutil.copytree(
        root / "mklink",
        source_root / "mklink",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.egg-info",
            "*.pyc",
            "*.pyo",
        ),
    )
    definition = stage / "definition"
    definition.mkdir()
    shutil.copy2(script_dir / "entry.py", definition / "entry.py")
    shutil.copy2(
        script_dir / "mklink-remote-agent.spec",
        definition / "mklink-remote-agent.spec",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_CACHE_DIR": str(pip_cache),
            "PIP_NO_INPUT": "1",
            "PYINSTALLER_CONFIG_DIR": str(pyinstaller_config),
            "SOURCE_DATE_EPOCH": "315532800",
            "TEMP": str(runtime_temp),
            "TMP": str(runtime_temp),
        }
    )

    virtual_environment = stage / "venv"
    _run(
        [sys.executable, "-m", "venv", str(virtual_environment)],
        cwd=stage,
        env=environment,
    )
    python = virtual_environment / "Scripts" / "python.exe"
    wheel_dir = stage / "wheel"
    wheel_dir.mkdir()

    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            f"{source_root}[site-agent-build]",
        ],
        cwd=stage,
        env=environment,
    )
    _run(
        [
            str(python),
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheel_dir),
        ],
        cwd=source_root,
        env=environment,
    )
    wheels = sorted(wheel_dir.glob("mklink-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("expected exactly one current-repository wheel")
    wheel = wheels[0]
    wheel_contract = _wheel_contract(
        wheel,
        product_version=product_version,
    )
    _run(
        [str(python), "-m", "pip", "uninstall", "--yes", "mklink"],
        cwd=root,
        env=environment,
    )
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            f"{wheel}[remote]",
        ],
        cwd=root,
        env=environment,
    )

    pyinstaller_dist = stage / "dist"
    _run(
        [
            str(python),
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(pyinstaller_dist),
            "--workpath",
            str(stage / "work"),
            str(definition / "mklink-remote-agent.spec"),
        ],
        cwd=stage,
        env=environment,
    )

    bundle = pyinstaller_dist / "mklink-remote-agent"
    executable = bundle / "mklink-remote-agent.exe"
    if not executable.is_file():
        raise RuntimeError("standalone executable was not produced")
    shutil.copy2(script_dir / "README.md", bundle / "README.md")
    shutil.copy2(stcp_library, bundle / "mklink-stcp.dll")
    license_root = bundle / "THIRD-PARTY-LICENSES"
    license_root.mkdir()
    shutil.copy2(
        root / "native" / "stcp_bridge" / "LICENSE",
        license_root / "Apache-2.0.txt",
    )

    removed_origin_metadata = _remove_local_install_metadata(bundle)
    normalized_distribution_records = _normalize_distribution_records(bundle)
    policy = _path_policy(
        (
            root,
            worktree_root,
            output,
            stage,
            source_root,
            definition,
            virtual_environment,
            wheel_dir,
            stage / "work",
            pyinstaller_dist,
            Path.home(),
        )
    )
    archive_audit = _audit_pyinstaller_archive(
        executable,
        site_packages=virtual_environment / "Lib" / "site-packages",
        policy=policy,
    )
    records = _audit_bundle(
        bundle,
        policy=policy,
    )
    bundled_distributions = _bundle_distributions(
        bundle,
        product_version=product_version,
    )
    _deterministic_zip(bundle, artifact)
    _audit_zip(
        artifact,
        bundle_name=bundle.name,
        records=records,
        policy=policy,
    )

    manifest_payload = {
        "schema": "mklink.site-agent.package-manifest.v1",
        "product": {
            "name": "mklink",
            "version": wheel_contract["version"],
        },
        "artifact": {
            "name": artifact.name,
            "sha256": _sha256(artifact),
            "size": artifact.stat().st_size,
        },
        "signing": {
            "status": "unsigned",
            "signature": None,
            "authenticode": "not-requested",
        },
        "wheel": {
            "name": wheel.name,
            "sha256": _sha256(wheel),
            "size": wheel.stat().st_size,
        },
        "build": {
            "command": "python packaging/site_agent/build.py --output <directory>",
            "format": "pyinstaller-onedir-in-deterministic-zip",
            "generator": {
                "name": "mklink-site-agent-builder",
                "version": GENERATOR_VERSION,
            },
            "inputs": {
                "base_git_sha": provenance["base_git_sha"],
                "upstream_git_sha": provenance["upstream_git_sha"],
                "upstream_merge_sha": provenance["upstream_merge_sha"],
                "dirty_content_sha256": source_input_sha256,
                "file_count": len(source_input_records),
                "canonicalization": "UTF-8 relative-path NUL size NUL SHA-256 LF",
            },
            "source": "current-repository-wheel",
        },
        "compatibility": {
            "target": {
                "operating_system": "Windows",
                "architecture": "x86_64",
                "python_runtime": "bundled",
            },
            "protocol": {
                "jsonrpc_version": "2.0",
                "mklink_remote_protocol_version": "1.0",
                "mklink_version": wheel_contract["version"],
            },
            "roles": {
                "field_host": "standalone Site Agent; no Codex, Skill, source checkout, or global Python required",
                "engineer_host": [
                    "repository Skill",
                    "mklink.remote SDK",
                    "mklink remote CLI",
                    "optional mklink-remote-mcp stdio server",
                ],
            },
        },
        "network_policy": {
            "mode": "direct-or-in-process-lan-stcp",
            "allowed": [
                "same LAN",
                "managed VPN with direct IP reachability",
                "LAN-local frps rendezvous using bundled mklink-stcp.dll",
            ],
            "prohibited": [
                "frpc.exe or any renamed/extracted frpc executable",
                "bundled or child-process frps",
                "NAT traversal",
                "public relay or public tunnel",
            ],
            "default_bind": "127.0.0.1",
            "non_loopback_requirements": [
                "--allow-lan",
                "non-empty token from environment or owner-only token file",
            ],
            "lan_stcp_requirements": [
                "Site Agent remains on a loopback IP",
                "operator-supplied frps is reachable only on the LAN",
                "FRP auth token, STCP secret, and Site Agent token are distinct",
                "all secrets enter through environment variables or owner-only files",
            ],
        },
        "entry_commands": {
            "start_loopback": ".\\mklink-remote-agent.exe start --host 127.0.0.1 --port 8766",
            "start_lan_or_vpn": ".\\mklink-remote-agent.exe start --host <LAN_OR_VPN_ADDRESS> --port 8766 --allow-lan",
            "start_lan_stcp": ".\\mklink-remote-agent.exe start --transport lan-stcp --host 127.0.0.1 --port 8766 --stcp-server-addr <LAN_FRPS_ADDRESS> --stcp-proxy-name <SITE_PROXY_NAME>",
            "health": ".\\mklink-remote-agent.exe health --host <SITE_ADDRESS> --port 8766",
            "status": ".\\mklink-remote-agent.exe status --host <SITE_ADDRESS> --port 8766",
            "stop": ".\\mklink-remote-agent.exe stop --host <SITE_ADDRESS> --port 8766",
            "restart": ".\\mklink-remote-agent.exe restart --host <SITE_ADDRESS> --port 8766",
        },
        "dependencies": {
            **wheel_contract,
            "bundled_distributions": bundled_distributions,
            "field_runtime": "bundled in ZIP",
            "engineer_mcp": "separate optional mcp extra; not bundled in field package",
            "in_process_stcp": {
                "library": "mklink-stcp.dll",
                "sha256": stcp_library_sha256,
                "frp_version": "0.69.1",
                "source": "official github.com/fatedier/frp client packages",
                "frpc_executable": False,
            },
        },
        "audit": {
            "schema": "mklink.site-agent.artifact-audit.v2",
            "surfaces": {
                "bundle_files": len(records),
                "zip_members": len(records),
                "manifest": 1,
                "archive_entries": archive_audit["entries"],
            },
            "archive_names_sha256": archive_audit["names_sha256"],
            "removed_local_origin_metadata": removed_origin_metadata,
            "normalized_distribution_records": normalized_distribution_records,
            "local_path_policy": {
                "reject": [
                    "current source/worktree/output/staging/home path variants",
                    "native, forward-slash, JSON-escaped, fully percent-encoded, and file-URL forms",
                    "UTF-8 and UTF-16 exact local paths",
                    "absolute file URLs and drive-local paths",
                ],
                "allow": [
                    "current Windows SystemRoot paths embedded by operating-system runtime files",
                    "third-party PE compiler/debug provenance paths after exact current-build paths and file URLs are rejected",
                    "repository/runtime code string constants after exact current-build paths and file URLs are rejected",
                    "encoded PYZ/base-library containers only when every decompressed member is separately audited",
                ],
                "worktree_resolution": (
                    "git top-level only when it is an existing absolute ancestor "
                    "of the product source; otherwise the product source root"
                ),
            },
        },
        "files": records,
    }
    manifest_data = (
        json.dumps(manifest_payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _audit_content("package manifest", manifest_data, policy)
    manifest.write_bytes(manifest_data)
    _audit_content(
        "written package manifest",
        manifest.read_bytes(),
        policy,
    )
    print(json.dumps(manifest_payload["artifact"], sort_keys=True), flush=True)
    _validated_cleanup(stage, output)
    return artifact, manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("MKLINK_BUILD_OUTPUT_DIR", "release")) / "site-agent",
    )
    parser.add_argument(
        "--stcp-library",
        type=Path,
        help="prebuilt mklink-stcp.dll; defaults to native/stcp_bridge/build",
    )
    args = parser.parse_args()
    if os.name == "nt":
        storage = os.environ.get("MKLINK_BUILD_ROOT")
        if not storage:
            parser.error("run via scripts/build_workspace.ps1 -Action run")
        storage_root = Path(storage).resolve()
        if storage_root.drive.casefold() in {"c:", os.environ.get("SystemDrive", "C:").casefold()}:
            parser.error("build storage must not be on C: or the Windows system drive")
        if not args.output.resolve().is_relative_to(storage_root):
            parser.error("--output must be inside MKLINK_BUILD_ROOT")
    build(args.output, stcp_library=args.stcp_library)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
