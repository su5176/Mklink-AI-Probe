"""Persistent, target-scoped user FLM algorithms for online flash."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import uuid
from typing import Callable, Iterable, Sequence, Tuple

from .errors import FlashError, FlashErrorCode
from .models import MemoryRegion
from .pyocd_runtime import import_pyocd_attr


@dataclass(frozen=True)
class CustomFlmRecord:
    algorithm_id: str
    target_part: str
    file_name: str
    file_path: str
    flash_start: int
    flash_size: int
    page_size: int
    sector_sizes: Tuple[Tuple[int, int], ...]


def _parse_flm(path: Path) -> object:
    PackFlashAlgo = import_pyocd_attr(
        "pyocd.target.pack.flash_algo", "PackFlashAlgo"
    )

    return PackFlashAlgo(str(path))


class CustomFlmCatalog:
    """Store validated FLMs by digest without retaining their source paths."""

    def __init__(
        self,
        root: str | Path,
        *,
        parser: Callable[[Path], object] = _parse_flm,
    ) -> None:
        self._root = Path(root)
        self._directory = self._root / "custom-flm"
        self._registry = self._directory / "registry.json"
        self._parser = parser
        self._lock = threading.RLock()

    def list(self, part_number: str) -> Tuple[CustomFlmRecord, ...]:
        key = self._part_key(part_number)
        with self._lock:
            records = [
                record
                for record in self._read_records()
                if record.target_part.casefold() == key
            ]
        return tuple(sorted(records, key=lambda item: (item.file_name.casefold(), item.algorithm_id)))

    def add(
        self,
        source: str | Path,
        file_name: str,
        part_number: str,
        existing_regions: Sequence[MemoryRegion],
    ) -> CustomFlmRecord:
        source_path = Path(source)
        if not source_path.is_file():
            raise FlashError(FlashErrorCode.FILE_NOT_FOUND, "FLM file was not found")
        safe_name = Path(str(file_name)).name
        if not safe_name or Path(safe_name).suffix.casefold() != ".flm":
            raise FlashError(FlashErrorCode.FILE_FORMAT_ERROR, "custom algorithm must be an .flm file")
        target_part = str(part_number).strip()
        self._part_key(target_part)
        digest = self._digest(source_path)
        try:
            parsed = self._metadata(self._parser(source_path))
        except FlashError:
            raise
        except Exception:
            raise FlashError(
                FlashErrorCode.FILE_FORMAT_ERROR,
                "FLM file could not be parsed",
            ) from None
        destination = self._directory / (digest + ".flm")
        candidate = CustomFlmRecord(
            algorithm_id=digest,
            target_part=target_part,
            file_name=safe_name,
            file_path=str(destination.resolve()),
            flash_start=parsed[0],
            flash_size=parsed[1],
            page_size=parsed[2],
            sector_sizes=parsed[3],
        )
        self._reject_overlap(candidate, existing_regions)

        with self._lock:
            records = list(self._read_records())
            for record in records:
                if (
                    record.target_part.casefold() == target_part.casefold()
                    and record.algorithm_id == digest
                ):
                    return record
            self._reject_overlap(candidate, self._regions_for_records(
                record for record in records
                if record.target_part.casefold() == target_part.casefold()
            ))
            self._directory.mkdir(parents=True, exist_ok=True)
            created_payload = not destination.exists()
            if created_payload:
                temporary = self._directory / (digest + "." + uuid.uuid4().hex + ".tmp")
                try:
                    shutil.copyfile(source_path, temporary)
                    if self._digest(temporary) != digest:
                        raise FlashError(FlashErrorCode.PACK_INTEGRITY_ERROR, "FLM copy verification failed")
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            records.append(candidate)
            try:
                self._write_records(records)
            except BaseException:
                if created_payload:
                    destination.unlink(missing_ok=True)
                raise
        return candidate

    def remove(self, part_number: str, algorithm_id: str) -> None:
        key = self._part_key(part_number)
        algorithm_id = str(algorithm_id).strip().casefold()
        with self._lock:
            records = list(self._read_records())
            kept = [
                record for record in records
                if not (
                    record.target_part.casefold() == key
                    and record.algorithm_id.casefold() == algorithm_id
                )
            ]
            if len(kept) == len(records):
                raise KeyError(algorithm_id)
            self._write_records(kept)
            if not any(record.algorithm_id.casefold() == algorithm_id for record in kept):
                (self._directory / (algorithm_id + ".flm")).unlink(missing_ok=True)

    def regions(self, part_number: str) -> Tuple[MemoryRegion, ...]:
        return self._regions_for_records(self.list(part_number))

    def paths(self, part_number: str) -> Tuple[str, ...]:
        return tuple(record.file_path for record in self.list(part_number))

    def fingerprint(self, part_number: str) -> Tuple[str, ...]:
        return tuple(record.algorithm_id for record in self.list(part_number))

    @staticmethod
    def _part_key(part_number: str) -> str:
        value = str(part_number).strip()
        if not value:
            raise ValueError("target part number is required")
        return value.casefold()

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _metadata(parsed: object) -> tuple[int, int, int, Tuple[Tuple[int, int], ...]]:
        try:
            start = int(getattr(parsed, "flash_start"))
            size = int(getattr(parsed, "flash_size"))
            page_size = int(getattr(parsed, "page_size"))
            sector_sizes = tuple(
                (int(offset), int(sector_size))
                for offset, sector_size in getattr(parsed, "sector_sizes")
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise FlashError(FlashErrorCode.FILE_FORMAT_ERROR, "FLM metadata is invalid") from error
        if start < 0 or size <= 0 or start + size > 0x1_0000_0000 or page_size <= 0:
            raise FlashError(FlashErrorCode.FILE_FORMAT_ERROR, "FLM flash range is invalid")
        if not sector_sizes or sector_sizes[0][0] != 0:
            raise FlashError(FlashErrorCode.FILE_FORMAT_ERROR, "FLM sector geometry must start at offset zero")
        previous = -1
        for offset, sector_size in sector_sizes:
            if offset <= previous or offset < 0 or offset >= size or sector_size <= 0:
                raise FlashError(FlashErrorCode.FILE_FORMAT_ERROR, "FLM sector geometry is invalid")
            previous = offset
        return start, size, page_size, sector_sizes

    @staticmethod
    def _reject_overlap(
        candidate: CustomFlmRecord,
        regions: Sequence[MemoryRegion],
    ) -> None:
        candidate_end = candidate.flash_start + candidate.flash_size
        for region in regions:
            if not region.is_flash:
                continue
            if candidate.flash_start < region.end and region.start < candidate_end:
                raise FlashError(
                    FlashErrorCode.TARGET_NOT_SUPPORTED,
                    "custom FLM range overlaps an existing flash algorithm",
                )

    @staticmethod
    def _regions_for_records(records: Iterable[CustomFlmRecord]) -> Tuple[MemoryRegion, ...]:
        result = []
        for record in records:
            sizes = record.sector_sizes
            for index, (offset, sector_size) in enumerate(sizes):
                next_offset = sizes[index + 1][0] if index + 1 < len(sizes) else record.flash_size
                suffix = "" if len(sizes) == 1 else "-{}".format(index)
                result.append(MemoryRegion(
                    "custom-flm-{}{}".format(record.algorithm_id[:12], suffix),
                    record.flash_start + offset,
                    next_offset - offset,
                    True,
                    True,
                    sector_size,
                ))
        return tuple(result)

    def _read_records(self) -> Tuple[CustomFlmRecord, ...]:
        if not self._registry.exists():
            return ()
        try:
            payload = json.loads(self._registry.read_text(encoding="utf-8"))
            items = payload["algorithms"]
            if payload.get("version") != 1 or not isinstance(items, list):
                raise ValueError("unsupported registry format")
            records = []
            for item in items:
                values = dict(item)
                values["sector_sizes"] = tuple(tuple(pair) for pair in values["sector_sizes"])
                record = CustomFlmRecord(**values)
                expected = str((self._directory / (record.algorithm_id + ".flm")).resolve())
                if (
                    record.file_path != expected
                    or not Path(record.file_path).is_file()
                    or self._digest(Path(record.file_path)) != record.algorithm_id
                ):
                    raise ValueError("registered FLM payload is unavailable")
                records.append(record)
            return tuple(records)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise FlashError(FlashErrorCode.PACK_INTEGRITY_ERROR, "custom FLM registry is invalid") from error

    def _write_records(self, records: Sequence[CustomFlmRecord]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = self._directory / ("registry." + uuid.uuid4().hex + ".tmp")
        payload = {
            "version": 1,
            "algorithms": [asdict(record) for record in sorted(
                records,
                key=lambda item: (item.target_part.casefold(), item.file_name.casefold(), item.algorithm_id),
            )],
        }
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self._registry)
        finally:
            temporary.unlink(missing_ok=True)
