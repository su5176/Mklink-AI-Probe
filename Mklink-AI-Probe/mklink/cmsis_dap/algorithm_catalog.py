"""Unified offline-first Flash algorithm discovery and extraction."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple, Union
import uuid

from .models import TargetRecord
from .paths import PackPaths
from .pyocd_runtime import import_pyocd_attr


class FlashAlgorithmError(ValueError):
    """A target algorithm is unavailable, ambiguous, or changed on disk."""


@dataclass(frozen=True)
class FlashAlgorithm:
    algorithm_id: str
    target_part: str
    file_name: str
    flash_start: int
    flash_size: int
    ram_start: int
    ram_size: int
    default: bool
    source_kind: str
    source_name: str
    source_token: str
    pack_path: Optional[str] = None
    pack_sha256: Optional[str] = None
    pack_algorithm_index: Optional[int] = None
    algorithm_path: Optional[str] = None
    custom_path: Optional[str] = None
    custom_sha256: Optional[str] = None
    builtin_blob_path: Optional[str] = None
    builtin_blob_sha256: Optional[str] = None
    page_size: int = 0
    sector_sizes: Tuple[Tuple[int, int], ...] = ()


@dataclass(frozen=True)
class FirmwareAlgorithmSelection:
    algorithm: FlashAlgorithm
    ranges: Tuple[Tuple[int, int], ...]


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_AUTOMATIC_SOURCE_PRIORITY = {
    "builtin-pack": 0,
    "daplink-builtin": 1,
    "installed-pack": 2,
    "custom-flm": 3,
}


def _encode_target(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def target_from_source_token(token: str) -> str:
    try:
        encoded = token.rsplit(":", 2)[-2] if token.startswith("catalog:") else token.split(":", 2)[1]
        padding = "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (IndexError, UnicodeError, ValueError) as error:
        raise FlashAlgorithmError("algorithm source token is invalid") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _installed_pack_records(paths: PackPaths, part_number: str) -> list[TargetRecord]:
    from .pack_manager import resolve_managed_pack_path

    try:
        payload = json.loads(paths.state_file.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []
    installed = payload.get("installed") if isinstance(payload, Mapping) else None
    if not isinstance(installed, Mapping):
        return []
    records = []
    for pack_id, versions in installed.items():
        if not isinstance(versions, Mapping) or "." not in str(pack_id):
            continue
        vendor = str(pack_id).split(".", 1)[0]
        for version, value in versions.items():
            pack_path = resolve_managed_pack_path(paths, value)
            if pack_path is None:
                continue
            records.append(TargetRecord(
                part_number=part_number,
                vendor=vendor,
                pack_id=str(pack_id),
                pack_version=str(version),
                pack_path=str(pack_path),
                installed=True,
                source="installed",
            ))
    records.sort(key=lambda record: (record.pack_id or "", record.pack_version or ""), reverse=True)
    return records


def _default_ram(device: object) -> Tuple[int, int]:
    for element in getattr(getattr(device, "_info", None), "memories", ()):
        attributes = getattr(element, "attrib", {})
        name = str(attributes.get("name") or attributes.get("id") or "").upper()
        access = str(attributes.get("access") or "")
        if "w" not in access and "RAM" not in name:
            continue
        try:
            return int(attributes["start"], 0), int(attributes["size"], 0)
        except (KeyError, TypeError, ValueError):
            continue
    return 0, 0


def _pack_algorithms(record: TargetRecord, part_number: str) -> list[FlashAlgorithm]:
    if not record.pack_path or not record.pack_id or not record.pack_version:
        return []
    pack_path = Path(record.pack_path).resolve()
    if not pack_path.is_file():
        return []
    CmsisPack = import_pyocd_attr(
        "pyocd.target.pack.cmsis_pack", "CmsisPack"
    )

    pack = CmsisPack(str(pack_path))
    try:
        devices = [
            device for device in pack.devices
            if str(device.part_number).casefold() == part_number.casefold()
        ]
        if len(devices) != 1:
            return []
        device = devices[0]
        default_ram_start, default_ram_size = _default_ram(device)
        pack_digest = _sha256(pack_path)
        algorithms = []
        for index, element in enumerate(getattr(device, "_info").algos):
            name = element.attrib.get("name")
            start = element.attrib.get("start")
            size = element.attrib.get("size")
            if not name or start is None or size is None:
                continue
            try:
                flash_start = int(start, 0)
                flash_size = int(size, 0)
                ram_start = int(element.attrib.get("RAMstart", default_ram_start), 0) \
                    if isinstance(element.attrib.get("RAMstart", default_ram_start), str) \
                    else int(element.attrib.get("RAMstart", default_ram_start))
                raw_ram_size = element.attrib.get("RAMsize", default_ram_size)
                ram_size = int(raw_ram_size, 0) if isinstance(raw_ram_size, str) else int(raw_ram_size)
            except (TypeError, ValueError):
                continue
            identity = "|".join((
                record.source,
                record.pack_id,
                record.pack_version,
                part_number.casefold(),
                str(index),
                str(name),
                hex(flash_start),
                hex(flash_size),
            ))
            algorithm_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            token = ":".join((
                "catalog",
                "installed" if record.source == "installed" else "bundle",
                record.pack_id,
                record.pack_version,
                _encode_target(part_number),
                str(index),
            ))
            algorithms.append(FlashAlgorithm(
                algorithm_id=algorithm_id,
                target_part=part_number,
                file_name=Path(str(name).replace("\\", "/")).name,
                flash_start=flash_start,
                flash_size=flash_size,
                ram_start=ram_start,
                ram_size=ram_size,
                default=element.attrib.get("default") == "1",
                source_kind=(
                    "installed-pack" if record.source == "installed" else "builtin-pack"
                ),
                source_name="{}@{}".format(record.pack_id, record.pack_version),
                source_token=token,
                pack_path=str(pack_path),
                pack_sha256=pack_digest,
                pack_algorithm_index=index,
                algorithm_path=str(name),
            ))
        return algorithms
    finally:
        pack_file = getattr(pack, "_pack_file", None)
        if hasattr(pack_file, "close"):
            pack_file.close()


def _custom_algorithms(paths: PackPaths, part_number: str) -> list[FlashAlgorithm]:
    try:
        from .custom_flm import CustomFlmCatalog

        records = CustomFlmCatalog(paths.root).list(part_number)
    except (OSError, TypeError, ValueError):
        return []
    result = []
    for record in records:
        result.append(FlashAlgorithm(
            algorithm_id=str(record.algorithm_id),
            target_part=part_number,
            file_name=str(record.file_name),
            flash_start=int(record.flash_start),
            flash_size=int(record.flash_size),
            ram_start=int(getattr(record, "ram_start", 0)),
            ram_size=int(getattr(record, "ram_size", 0)),
            default=False,
            source_kind="custom-flm",
            source_name="用户 FLM",
            source_token="custom:{}:{}".format(_encode_target(part_number), record.algorithm_id),
            custom_path=str(record.file_path),
            custom_sha256=str(record.algorithm_id),
        ))
    return result


def discover_flash_algorithms(
    part_number: str,
    *,
    paths: Optional[PackPaths] = None,
    builtin_provider: Optional[Callable[[], Iterable[TargetRecord]]] = None,
    daplink_provider: Optional[Callable[[str], Iterable[FlashAlgorithm]]] = None,
) -> list[FlashAlgorithm]:
    target = str(part_number or "").strip()
    if not target:
        return []
    from mklink.hpm_config import is_hpm_target

    if is_hpm_target(target):
        return []
    resolved_paths = paths or PackPaths()
    custom = _custom_algorithms(resolved_paths, target)
    discovered = []  # type: list[FlashAlgorithm]

    if builtin_provider is None:
        from .builtin_pack_bundle import load_builtin_pack_records

        builtin_provider = load_builtin_pack_records
    matching = [
        record for record in builtin_provider()
        if record.part_number.casefold() == target.casefold()
        and record.source == "bundle"
    ]
    for record in matching:
        discovered.extend(_pack_algorithms(record, target))
    if daplink_provider is None:
        from .builtin_flm_bundle import discover_builtin_flm_algorithms

        daplink_provider = discover_builtin_flm_algorithms
    discovered.extend(daplink_provider(target))
    for record in _installed_pack_records(resolved_paths, target):
        discovered.extend(_pack_algorithms(record, target))
    discovered.extend(custom)
    return discovered


def _extract_bytes(algorithm: FlashAlgorithm) -> bytes:
    if algorithm.custom_path:
        path = Path(algorithm.custom_path).resolve()
        if not path.is_file():
            raise FlashAlgorithmError("custom FLM payload is unavailable")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if algorithm.custom_sha256 and digest != algorithm.custom_sha256.casefold():
            raise FlashAlgorithmError("custom FLM payload integrity check failed")
        return data
    if algorithm.builtin_blob_path:
        from .builtin_flm_bundle import BuiltinFlmBundleError, extract_builtin_flm

        try:
            return extract_builtin_flm(algorithm)
        except BuiltinFlmBundleError as error:
            raise FlashAlgorithmError(str(error)) from error
    if not algorithm.pack_path or algorithm.pack_algorithm_index is None:
        raise FlashAlgorithmError("Pack algorithm source is incomplete")
    pack_path = Path(algorithm.pack_path).resolve()
    if not pack_path.is_file() or _sha256(pack_path) != algorithm.pack_sha256:
        raise FlashAlgorithmError("Pack algorithm source changed after discovery")
    refreshed = _pack_algorithms(TargetRecord(
        part_number=algorithm.target_part,
        vendor="",
        pack_id=algorithm.source_name.rsplit("@", 1)[0],
        pack_version=algorithm.source_name.rsplit("@", 1)[-1],
        pack_path=str(pack_path),
        installed=True,
        source="installed" if algorithm.source_kind == "installed-pack" else "bundle",
    ), algorithm.target_part)
    matches = [item for item in refreshed if item.algorithm_id == algorithm.algorithm_id]
    if len(matches) != 1 or matches[0].algorithm_path != algorithm.algorithm_path:
        raise FlashAlgorithmError("Pack algorithm metadata changed after discovery")

    CmsisPack = import_pyocd_attr(
        "pyocd.target.pack.cmsis_pack", "CmsisPack"
    )

    pack = CmsisPack(str(pack_path))
    try:
        devices = [
            device for device in pack.devices
            if str(device.part_number).casefold() == algorithm.target_part.casefold()
        ]
        if len(devices) != 1:
            raise FlashAlgorithmError("Pack target is unavailable")
        with devices[0].get_file(str(algorithm.algorithm_path)) as source:
            return source.read()
    finally:
        pack_file = getattr(pack, "_pack_file", None)
        if hasattr(pack_file, "close"):
            pack_file.close()


def extract_algorithm(
    algorithm: FlashAlgorithm,
    destination: Optional[Path] = None,
) -> Union[bytes, Path]:
    data = _extract_bytes(algorithm)
    if destination is None:
        return data
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    safe_name = _SAFE_NAME.sub("_", Path(algorithm.file_name).stem)[:48] or "algorithm"
    target = root / "{}_{}.flm".format(safe_name, digest)
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
        return target
    temporary = root / (target.name + ".{}.tmp".format(uuid.uuid4().hex))
    try:
        temporary.write_bytes(data)
        os.replace(str(temporary), str(target))
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def deploy_algorithm_to_probe(
    algorithm: FlashAlgorithm,
    *,
    disk_root: Optional[Path] = None,
) -> str:
    if disk_root is None:
        from mklink.discovery import find_microkeen_disk

        discovered = find_microkeen_disk()
        if not discovered:
            raise FlashAlgorithmError("MICROKEEN disk is unavailable")
        disk_root = Path(discovered)
    root = Path(disk_root).resolve() / "FLM"
    payload = _extract_bytes(algorithm)
    digest = hashlib.sha256(payload).hexdigest()
    safe_name = _SAFE_NAME.sub("_", Path(algorithm.file_name).stem)[:40] or "algorithm"
    target = root / "{}_{}.flm".format(safe_name, digest)
    root.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        temporary = root / (target.name + ".{}.tmp".format(uuid.uuid4().hex))
        try:
            temporary.write_bytes(payload)
            os.replace(str(temporary), str(target))
        finally:
            if temporary.exists():
                temporary.unlink()
    return "/FLM/{}".format(target.name)


def resolve_firmware_algorithms(
    algorithms: Sequence[FlashAlgorithm],
    ranges: Sequence[Tuple[int, int]],
    *,
    allow_uncovered: bool = False,
    preferred_algorithm_ids: Iterable[str] = (),
) -> list[FirmwareAlgorithmSelection]:
    preferred = {str(algorithm_id) for algorithm_id in preferred_algorithm_ids}
    source_order = {}  # type: dict[tuple[str, str], int]
    for algorithm in algorithms:
        source_key = (algorithm.source_kind, algorithm.source_name)
        source_order.setdefault(source_key, len(source_order))
    selected = {}  # type: dict[str, tuple[FlashAlgorithm, list[Tuple[int, int]]]]
    for start, end in ranges:
        if start < 0 or end <= start:
            raise FlashAlgorithmError("firmware range is invalid")
        candidates = [
            algorithm for algorithm in algorithms
            if algorithm.flash_size > 0
            and algorithm.flash_start <= start
            and end <= algorithm.flash_start + algorithm.flash_size
        ]
        if not candidates:
            if allow_uncovered:
                continue
            raise FlashAlgorithmError(
                "no Flash algorithm covers 0x{:08X}-0x{:08X}".format(start, end)
            )
        candidates.sort(key=lambda algorithm: (
            0 if algorithm.algorithm_id in preferred else 1,
            _AUTOMATIC_SOURCE_PRIORITY.get(algorithm.source_kind, 4),
            source_order[(algorithm.source_kind, algorithm.source_name)],
            0 if algorithm.default else 1,
            algorithm.flash_size,
            algorithm.algorithm_id,
        ))
        algorithm = candidates[0]
        entry = selected.setdefault(algorithm.algorithm_id, (algorithm, []))
        entry[1].append((start, end))
    return [
        FirmwareAlgorithmSelection(algorithm, tuple(grouped_ranges))
        for algorithm, grouped_ranges in selected.values()
    ]
