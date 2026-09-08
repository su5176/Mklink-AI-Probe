"""Safe inspection and preview of BIN and Intel HEX firmware images."""

from __future__ import annotations

import binascii
import hashlib
import os
import secrets
import shutil
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Dict, Iterable, Iterator, Optional, Set, Tuple, Union

from .errors import FlashError, FlashErrorCode
from .models import ImageInspection, ImageSegment, MemoryRegion


_HASH_CHUNK_SIZE = 1024 * 1024
_MAX_PREVIEW_LENGTH = 4096
_MAX_ADDRESS_EXCLUSIVE = 1 << 64
_MISSING_BYTE = 0xFF
_DEFAULT_MAX_FILE_SIZE = 256 * 1024 * 1024
_DEFAULT_MAX_HEX_DECODED_SIZE = 64 * 1024 * 1024
_DEFAULT_MAX_HEX_RECORDS = 262_144
_DEFAULT_MAX_HEX_SEGMENTS = 65_536
_MAX_INTEL_HEX_LINE_SIZE = 1 + 2 * (255 + 5)


@dataclass(frozen=True)
class PreviewPage:
    address: int
    data: bytes
    present: Tuple[bool, ...]


@dataclass(frozen=True)
class SectorRecord:
    address: int
    size: int


@dataclass(frozen=True)
class SectorCoverage:
    sectors: Tuple[SectorRecord, ...]
    sector_operations_available: bool


@dataclass(frozen=True)
class _InspectedRecord:
    inspection: ImageInspection
    snapshot_path: Path
    source_path: Path
    stat_size: int
    stat_mtime_ns: int
    stat_ctime_ns: int
    stat_device: int
    stat_inode: int
    hex_segments: Optional[Tuple[Tuple[ImageSegment, bytes], ...]]


class ImageInspector:
    """Inspect firmware files and keep an isolated registry of safe records."""

    def __init__(
        self,
        snapshot_root: Optional[Union[Path, str]] = None,
        max_file_size: int = _DEFAULT_MAX_FILE_SIZE,
        max_hex_decoded_size: int = _DEFAULT_MAX_HEX_DECODED_SIZE,
        copy_hook: Optional[Callable[[Path, int], None]] = None,
        max_hex_records: int = _DEFAULT_MAX_HEX_RECORDS,
        max_hex_segments: int = _DEFAULT_MAX_HEX_SEGMENTS,
    ) -> None:
        if not self._valid_limit(max_file_size):
            raise ValueError("max_file_size must be a positive integer")
        if not self._valid_limit(max_hex_decoded_size):
            raise ValueError("max_hex_decoded_size must be a positive integer")
        if not self._valid_limit(max_hex_records):
            raise ValueError("max_hex_records must be a positive integer")
        if not self._valid_limit(max_hex_segments):
            raise ValueError("max_hex_segments must be a positive integer")
        if snapshot_root is None:
            self._snapshot_root = Path(
                tempfile.mkdtemp(prefix="mklink-image-snapshots-")
            ).resolve()
            self._owns_snapshot_root = True
        else:
            self._snapshot_root = Path(snapshot_root).expanduser().resolve()
            self._snapshot_root.mkdir(parents=True, exist_ok=True)
            self._owns_snapshot_root = False
        self._max_file_size = max_file_size
        self._max_hex_decoded_size = max_hex_decoded_size
        self._max_hex_records = max_hex_records
        self._max_hex_segments = max_hex_segments
        self._copy_hook = copy_hook
        self._records: Dict[str, _InspectedRecord] = {}
        self._snapshot_files: Set[Path] = set()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._active_operations = 0
        self._closed = False

    def inspect(
        self,
        file_path: Union[Path, str],
        memory_regions: Iterable[MemoryRegion],
        base_address: Optional[int] = None,
    ) -> ImageInspection:
        with self._operation():
            return self._inspect(file_path, memory_regions, base_address)

    def _inspect(
        self,
        file_path: Union[Path, str],
        memory_regions: Iterable[MemoryRegion],
        base_address: Optional[int],
    ) -> ImageInspection:
        source_path = self._validated_source_path(file_path)
        image_format = source_path.suffix.lower().lstrip(".")
        if image_format == "bin":
            if not self._is_nonnegative_int(base_address):
                raise FlashError(
                    FlashErrorCode.BIN_ADDRESS_MISSING,
                    "BIN firmware requires a nonnegative base address",
                )
        snapshot_path: Optional[Path] = None
        registered = False
        try:
            snapshot_path, size, digest, snapshot_stat = self._snapshot_source(
                source_path
            )
            hex_segments: Optional[Tuple[Tuple[ImageSegment, bytes], ...]] = None
            if image_format == "bin":
                if self._looks_like_intel_hex(snapshot_path):
                    raise FlashError(
                        FlashErrorCode.FILE_FORMAT_ERROR,
                        "firmware content does not match the .bin extension",
                    )
                assert base_address is not None
                end = base_address + size
                if end > _MAX_ADDRESS_EXCLUSIVE:
                    raise FlashError(
                        FlashErrorCode.IMAGE_OUT_OF_RANGE,
                        "BIN firmware address range overflows",
                    )
                segments = (ImageSegment(base_address, end),)
            else:
                segments, hex_segments = self._parse_hex(snapshot_path)
                base_address = None

            regions = tuple(memory_regions)
            for segment in segments:
                if not self._segment_is_covered(segment, regions):
                    raise FlashError(
                        FlashErrorCode.IMAGE_OUT_OF_RANGE,
                        "firmware segment is outside writable flash memory",
                        {"start": segment.start, "end": segment.end},
                    )

            current_snapshot_stat = self._stat_file(snapshot_path)
            if not self._same_stat(snapshot_stat, current_snapshot_stat):
                raise FlashError(
                    FlashErrorCode.FILE_FORMAT_ERROR,
                    "firmware snapshot changed during inspection",
                )

            with self._lock:
                image_id = self._new_image_id_locked()
                inspection = ImageInspection(
                    image_id=image_id,
                    file_name=source_path.name,
                    file_path=str(snapshot_path),
                    format=image_format,
                    size=size,
                    sha256=digest,
                    start=segments[0].start,
                    end=segments[-1].end,
                    segments=segments,
                    base_address=base_address,
                )
                self._records[image_id] = _InspectedRecord(
                    inspection=inspection,
                    snapshot_path=snapshot_path,
                    source_path=source_path,
                    stat_size=current_snapshot_stat.st_size,
                    stat_mtime_ns=current_snapshot_stat.st_mtime_ns,
                    stat_ctime_ns=current_snapshot_stat.st_ctime_ns,
                    stat_device=current_snapshot_stat.st_dev,
                    stat_inode=current_snapshot_stat.st_ino,
                    hex_segments=hex_segments,
                )
                self._snapshot_files.add(snapshot_path)
                registered = True
            return inspection
        finally:
            if snapshot_path is not None and not registered:
                self._unlink_snapshot(snapshot_path)

    def preview(self, image_id: str, absolute_address: int, length: int) -> PreviewPage:
        with self._operation():
            return self._preview(image_id, absolute_address, length)

    def _preview(
        self, image_id: str, absolute_address: int, length: int
    ) -> PreviewPage:
        record = self._record(image_id)
        if not self._is_nonnegative_int(absolute_address):
            raise ValueError("absolute_address must be a nonnegative integer")
        if not self._is_nonnegative_int(length):
            raise ValueError("length must be a nonnegative integer")
        if length > _MAX_PREVIEW_LENGTH:
            raise ValueError("preview length exceeds 4096 bytes")
        if absolute_address + length > _MAX_ADDRESS_EXCLUSIVE:
            raise ValueError("preview address range overflows")

        data = bytearray([_MISSING_BYTE]) * length
        present = [False] * length
        if record.inspection.format == "bin":
            self._preview_bin(record, absolute_address, data, present)
        else:
            assert record.hex_segments is not None
            requested_end = absolute_address + length
            for segment, payload in record.hex_segments:
                overlap_start = max(absolute_address, segment.start)
                overlap_end = min(requested_end, segment.end)
                if overlap_start >= overlap_end:
                    continue
                source_offset = overlap_start - segment.start
                output_offset = overlap_start - absolute_address
                count = overlap_end - overlap_start
                data[output_offset : output_offset + count] = payload[
                    source_offset : source_offset + count
                ]
                present[output_offset : output_offset + count] = [True] * count
        return PreviewPage(absolute_address, bytes(data), tuple(present))

    def validate_unchanged(self, image_id: str) -> ImageInspection:
        with self._operation():
            return self._validate_unchanged(image_id)

    def _validate_unchanged(self, image_id: str) -> ImageInspection:
        record = self._record(image_id)
        before = self._stat_file(record.snapshot_path)
        digest = self._stream_sha256(record.snapshot_path)
        after = self._stat_file(record.snapshot_path)
        identity_matches = (
            after.st_dev == record.stat_device and after.st_ino == record.stat_inode
        )
        if (
            not self._same_stat(before, after)
            or after.st_size != record.stat_size
            or after.st_mtime_ns != record.stat_mtime_ns
            or after.st_ctime_ns != record.stat_ctime_ns
            or not identity_matches
            or digest != record.inspection.sha256
        ):
            raise FlashError(
                FlashErrorCode.FILE_FORMAT_ERROR, "firmware changed after inspection"
            )
        return record.inspection

    def covered_sectors(
        self, image_id: str, memory_regions: Iterable[MemoryRegion]
    ) -> SectorCoverage:
        with self._operation():
            return self._covered_sectors(image_id, memory_regions)

    def _covered_sectors(
        self, image_id: str, memory_regions: Iterable[MemoryRegion]
    ) -> SectorCoverage:
        record = self._record(image_id)
        segments = record.inspection.segments
        eligible_regions = self._eligible_regions(memory_regions)
        relevant_regions = tuple(
            region
            for region in eligible_regions
            if any(
                region.start < segment.end and segment.start < region.end
                for segment in segments
            )
        )
        if any(
            not self._segment_is_covered(segment, relevant_regions)
            for segment in segments
        ):
            return SectorCoverage((), False)
        if any(
            not self._valid_sector_size(region.sector_size)
            for region in relevant_regions
        ):
            return SectorCoverage((), False)

        unique_regions = {
            (region.start, region.end, region.sector_size): region
            for region in relevant_regions
        }
        sector_sizes: Dict[int, int] = {}
        for region in unique_regions.values():
            assert region.sector_size is not None
            for segment in segments:
                overlap_start = max(segment.start, region.start)
                overlap_end = min(segment.end, region.end)
                if overlap_start >= overlap_end:
                    continue
                first_index = (overlap_start - region.start) // region.sector_size
                last_index = (overlap_end - 1 - region.start) // region.sector_size
                for index in range(first_index, last_index + 1):
                    address = region.start + index * region.sector_size
                    size = min(region.sector_size, region.end - address)
                    previous_size = sector_sizes.get(address)
                    if previous_size is not None and previous_size != size:
                        return SectorCoverage((), False)
                    sector_sizes[address] = size

        ordered = tuple(
            SectorRecord(address, sector_sizes[address])
            for address in sorted(sector_sizes)
        )
        previous_end = None
        for sector in ordered:
            if previous_end is not None and sector.address < previous_end:
                return SectorCoverage((), False)
            previous_end = sector.address + sector.size
        return SectorCoverage(ordered, True)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            while self._active_operations:
                self._condition.wait()
            snapshot_files = tuple(self._snapshot_files)
            self._snapshot_files.clear()
            self._records.clear()
        for snapshot_path in snapshot_files:
            self._unlink_snapshot(snapshot_path)
        if self._owns_snapshot_root:
            shutil.rmtree(str(self._snapshot_root), ignore_errors=True)

    def shutdown(self) -> None:
        self.close()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        with self._condition:
            if self._closed:
                raise RuntimeError("image inspector is closed")
            self._active_operations += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_operations -= 1
                self._condition.notify_all()

    @staticmethod
    def _validated_source_path(file_path: Union[Path, str]) -> Path:
        if not isinstance(file_path, (Path, str)):
            raise TypeError("file_path must be a path or string")
        path = Path(file_path).expanduser().resolve()
        if path.suffix.lower() not in (".bin", ".hex"):
            raise FlashError(
                FlashErrorCode.FILE_FORMAT_ERROR,
                "firmware file must use a .bin or .hex extension",
            )
        return path

    def _snapshot_source(self, source_path: Path) -> Tuple[Path, int, str, os.stat_result]:
        try:
            source = source_path.open("rb")
        except OSError as exc:
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND, "firmware file was not found"
            ) from exc
        snapshot_path: Optional[Path] = None
        with source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise FlashError(
                    FlashErrorCode.FILE_NOT_FOUND,
                    "firmware path is not a regular file",
                )
            if before.st_size <= 0:
                raise FlashError(
                    FlashErrorCode.FILE_FORMAT_ERROR, "firmware file is empty"
                )
            if before.st_size > self._max_file_size:
                raise FlashError(
                    FlashErrorCode.FILE_FORMAT_ERROR,
                    "firmware file exceeds configured size limit",
                )
            snapshot_path, descriptor = self._create_snapshot_file(source_path.suffix)
            try:
                with os.fdopen(descriptor, "wb") as snapshot:
                    size, digest = self._copy_snapshot_bytes(
                        source, snapshot, source_path
                    )
                    snapshot.flush()
                    os.fsync(snapshot.fileno())
                after = os.fstat(source.fileno())
                if (
                    not self._same_stat(before, after)
                    or size != before.st_size
                    # Windows/remote filesystems may retain timestamps while
                    # a writer replaces same-size bytes. Compare fresh content
                    # too, before accepting a firmware snapshot for erasure.
                    or self._stream_sha256(source_path) != digest
                ):
                    raise FlashError(
                        FlashErrorCode.FILE_FORMAT_ERROR,
                        "firmware changed during snapshot creation",
                    )
                snapshot_stat = self._stat_file(snapshot_path)
                return snapshot_path, size, digest, snapshot_stat
            except Exception:
                self._unlink_snapshot(snapshot_path)
                raise

    def _create_snapshot_file(self, suffix: str) -> Tuple[Path, int]:
        for _ in range(128):
            snapshot_path = self._snapshot_root / (
                secrets.token_urlsafe(24) + suffix
            )
            try:
                descriptor = os.open(
                    str(snapshot_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    stat.S_IRUSR | stat.S_IWUSR,
                )
                return snapshot_path, descriptor
            except FileExistsError:
                continue
            except OSError as exc:
                raise FlashError(
                    FlashErrorCode.FILE_FORMAT_ERROR,
                    "unable to create firmware snapshot",
                ) from exc
        raise FlashError(
            FlashErrorCode.FILE_FORMAT_ERROR,
            "unable to allocate an opaque firmware snapshot name",
        )

    def _copy_snapshot_bytes(
        self, source: BinaryIO, snapshot: BinaryIO, source_path: Path
    ) -> Tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        try:
            while True:
                chunk = source.read(_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > self._max_file_size:
                    raise FlashError(
                        FlashErrorCode.FILE_FORMAT_ERROR,
                        "firmware file exceeds configured size limit",
                    )
                snapshot.write(chunk)
                digest.update(chunk)
                if self._copy_hook is not None:
                    self._copy_hook(source_path, size)
        except FlashError:
            raise
        except OSError as exc:
            raise FlashError(
                FlashErrorCode.FILE_FORMAT_ERROR,
                "unable to create a consistent firmware snapshot",
            ) from exc
        return size, digest.hexdigest()

    @staticmethod
    def _stat_file(path: Path):
        try:
            metadata = path.stat()
        except OSError as exc:
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND, "firmware file was not found"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND, "firmware path is not a regular file"
            )
        return metadata

    @staticmethod
    def _stream_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(_HASH_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND, "firmware file was not found"
            ) from exc
        return digest.hexdigest()

    def _parse_hex(
        self, path: Path
    ) -> Tuple[
        Tuple[ImageSegment, ...],
        Tuple[Tuple[ImageSegment, bytes], ...],
    ]:
        return self.decode_hex(
            path,
            max_records=self._max_hex_records,
            max_decoded_size=self._max_hex_decoded_size,
            max_segments=self._max_hex_segments,
        )

    @staticmethod
    def decode_hex(
        path: Path,
        *,
        max_records: int = _DEFAULT_MAX_HEX_RECORDS,
        max_decoded_size: int = _DEFAULT_MAX_HEX_DECODED_SIZE,
        max_segments: int = _DEFAULT_MAX_HEX_SEGMENTS,
    ) -> Tuple[
        Tuple[ImageSegment, ...],
        Tuple[Tuple[ImageSegment, bytes], ...],
    ]:
        chunks = []
        decoded_size = 0
        record_count = 0
        address_base = 0
        seen_eof = False
        try:
            with path.open("rb") as stream:
                while True:
                    raw_line = stream.readline(_MAX_INTEL_HEX_LINE_SIZE + 2)
                    if not raw_line:
                        break
                    line = raw_line.rstrip(b"\r\n")
                    if not line:
                        continue
                    if len(line) > _MAX_INTEL_HEX_LINE_SIZE:
                        raise FlashError(
                            FlashErrorCode.FILE_FORMAT_ERROR,
                            "Intel HEX record is too long",
                        )
                    if seen_eof:
                        raise FlashError(
                            FlashErrorCode.FILE_FORMAT_ERROR,
                            "Intel HEX contains a record after EOF",
                        )
                    if not line.startswith(b":"):
                        raise FlashError(
                            FlashErrorCode.FILE_FORMAT_ERROR,
                            "Intel HEX record must start with ':'",
                        )
                    encoded = line[1:]
                    if len(encoded) < 10 or len(encoded) % 2:
                        raise FlashError(
                            FlashErrorCode.FILE_FORMAT_ERROR,
                            "Intel HEX record has an invalid encoded length",
                        )
                    try:
                        record = binascii.unhexlify(encoded)
                    except (binascii.Error, ValueError) as exc:
                        raise FlashError(
                            FlashErrorCode.FILE_FORMAT_ERROR,
                            "Intel HEX record contains non-hexadecimal data",
                        ) from exc
                    byte_count = record[0]
                    if len(record) != byte_count + 5:
                        raise FlashError(
                            FlashErrorCode.FILE_FORMAT_ERROR,
                            "Intel HEX byte count does not match record length",
                        )
                    if sum(record) & 0xFF:
                        raise FlashError(
                            FlashErrorCode.FILE_FORMAT_ERROR,
                            "Intel HEX record checksum is invalid",
                        )
                    record_address = (record[1] << 8) | record[2]
                    record_type = record[3]
                    payload = bytes(record[4:-1])
                    if record_type not in (0x00, 0x01, 0x02, 0x03, 0x04, 0x05):
                        raise FlashError(
                            FlashErrorCode.FILE_FORMAT_ERROR,
                            "Intel HEX record type is unsupported",
                        )
                    if record_type != 0x01:
                        record_count += 1
                        if record_count > max_records:
                            raise FlashError(
                                FlashErrorCode.FILE_FORMAT_ERROR,
                                "Intel HEX record limit exceeded",
                            )

                    if record_type == 0x00:
                        if decoded_size + byte_count > max_decoded_size:
                            raise FlashError(
                                FlashErrorCode.FILE_FORMAT_ERROR,
                                "Intel HEX decoded data exceeds configured size limit",
                            )
                        absolute_start = address_base + record_address
                        absolute_end = absolute_start + byte_count
                        if (
                            absolute_start >= (1 << 32)
                            or absolute_end > (1 << 32)
                        ):
                            raise FlashError(
                                FlashErrorCode.FILE_FORMAT_ERROR,
                                "Intel HEX address range overflows 32 bits",
                            )
                        if byte_count:
                            chunks.append((absolute_start, payload))
                            decoded_size += byte_count
                    elif record_type == 0x01:
                        if byte_count != 0 or record_address != 0:
                            raise FlashError(
                                FlashErrorCode.FILE_FORMAT_ERROR,
                                "Intel HEX EOF record is invalid",
                            )
                        seen_eof = True
                    elif record_type == 0x02:
                        if byte_count != 2 or record_address != 0:
                            raise FlashError(
                                FlashErrorCode.FILE_FORMAT_ERROR,
                                "Intel HEX extended-segment record is invalid",
                            )
                        address_base = int.from_bytes(payload, "big") << 4
                    elif record_type == 0x04:
                        if byte_count != 2 or record_address != 0:
                            raise FlashError(
                                FlashErrorCode.FILE_FORMAT_ERROR,
                                "Intel HEX extended-linear record is invalid",
                            )
                        address_base = int.from_bytes(payload, "big") << 16
                    elif record_type in (0x03, 0x05):
                        # Entry-point metadata is not a Flash write address.
                        # Combined boot/application images may contain several
                        # entries; programming and verification use data only.
                        if byte_count != 4 or record_address != 0:
                            raise FlashError(
                                FlashErrorCode.FILE_FORMAT_ERROR,
                                "Intel HEX start-address record is invalid",
                            )
        except FlashError:
            raise
        except OSError as exc:
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND,
                "firmware snapshot was not found",
            ) from exc
        if not seen_eof:
            raise FlashError(
                FlashErrorCode.FILE_FORMAT_ERROR,
                "Intel HEX firmware is missing an EOF record",
            )
        if not chunks:
            raise FlashError(
                FlashErrorCode.FILE_FORMAT_ERROR,
                "Intel HEX firmware contains no data",
            )
        chunks.sort(key=lambda item: item[0])
        merged = []
        for start, payload in chunks:
            if not merged:
                merged.append((start, bytearray(payload)))
                continue
            previous_start, previous_payload = merged[-1]
            previous_end = previous_start + len(previous_payload)
            if start < previous_end:
                raise FlashError(
                    FlashErrorCode.FILE_FORMAT_ERROR,
                    "Intel HEX data records overlap",
                )
            if start == previous_end:
                previous_payload.extend(payload)
            else:
                if len(merged) >= max_segments:
                    raise FlashError(
                        FlashErrorCode.FILE_FORMAT_ERROR,
                        "Intel HEX segment limit exceeded",
                    )
                merged.append((start, bytearray(payload)))
        segment_data = tuple(
            (ImageSegment(start, start + len(payload)), bytes(payload))
            for start, payload in merged
        )
        segments = tuple(segment for segment, _ in segment_data)
        return segments, segment_data

    def _looks_like_intel_hex(self, path: Path) -> bool:
        try:
            with path.open("rb") as stream:
                while True:
                    raw_line = stream.readline(_MAX_INTEL_HEX_LINE_SIZE + 2)
                    if not raw_line:
                        return False
                    first_nonempty = raw_line.rstrip(b"\r\n")
                    if first_nonempty:
                        break
        except OSError as exc:
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND,
                "firmware snapshot was not found",
            ) from exc
        if not first_nonempty.startswith(b":"):
            return False
        try:
            parsed = self._parse_hex(path)
            del parsed
            return True
        except FlashError as exc:
            if exc.code is FlashErrorCode.FILE_NOT_FOUND:
                raise
            return False

    @staticmethod
    def _unlink_snapshot(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    @staticmethod
    def _same_stat(first, second) -> bool:
        return (
            first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns
            and first.st_ctime_ns == second.st_ctime_ns
            and first.st_dev == second.st_dev
            and first.st_ino == second.st_ino
        )

    @staticmethod
    def _is_nonnegative_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _valid_limit(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _valid_sector_size(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _eligible_regions(
        memory_regions: Iterable[MemoryRegion],
    ) -> Tuple[MemoryRegion, ...]:
        return tuple(
            sorted(
                (
                    region
                    for region in memory_regions
                    if region.length > 0 and region.is_flash and region.writable
                ),
                key=lambda region: (
                    region.start,
                    region.end,
                    region.name,
                    -1 if region.sector_size is None else region.sector_size,
                ),
            )
        )

    @classmethod
    def _segment_is_covered(
        cls, segment: ImageSegment, memory_regions: Iterable[MemoryRegion]
    ) -> bool:
        cursor = segment.start
        for region in cls._eligible_regions(memory_regions):
            if region.end <= cursor:
                continue
            if region.start > cursor:
                return False
            cursor = max(cursor, region.end)
            if cursor >= segment.end:
                return True
        return False

    def _record(self, image_id: str) -> _InspectedRecord:
        with self._lock:
            record = self._records.get(image_id)
        if record is None:
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND, "inspected firmware image was not found"
            )
        return record

    def _new_image_id_locked(self) -> str:
        while True:
            image_id = secrets.token_urlsafe(24)
            if image_id not in self._records:
                return image_id

    @staticmethod
    def _preview_bin(
        record: _InspectedRecord,
        absolute_address: int,
        data: bytearray,
        present: list,
    ) -> None:
        image_start = record.inspection.start
        image_end = record.inspection.end
        requested_end = absolute_address + len(data)
        overlap_start = max(absolute_address, image_start)
        overlap_end = min(requested_end, image_end)
        if overlap_start >= overlap_end:
            return
        file_offset = overlap_start - image_start
        output_offset = overlap_start - absolute_address
        count = overlap_end - overlap_start
        try:
            with record.snapshot_path.open("rb") as stream:
                stream.seek(file_offset)
                chunk = stream.read(count)
        except OSError as exc:
            raise FlashError(
                FlashErrorCode.FILE_NOT_FOUND, "firmware file was not found"
            ) from exc
        data[output_offset : output_offset + len(chunk)] = chunk
        present[output_offset : output_offset + len(chunk)] = [True] * len(chunk)
