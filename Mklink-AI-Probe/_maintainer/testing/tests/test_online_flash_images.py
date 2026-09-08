"""Tests for safe, immutable online-flash image inspection."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

import mklink.cmsis_dap.images as images_module
from mklink.cmsis_dap.errors import FlashError, FlashErrorCode
from mklink.cmsis_dap.images import ImageInspector, SectorRecord
from mklink.cmsis_dap.models import ImageSegment, MemoryRegion


FLASH_START = 0x08000000


def flash_region(
    start: int = FLASH_START,
    length: int = 0x10000,
    *,
    writable: bool = True,
    is_flash: bool = True,
    sector_size: Optional[int] = 0x1000,
    name: str = "FLASH",
) -> MemoryRegion:
    return MemoryRegion(name, start, length, is_flash, writable, sector_size)


def ihex_record(address: int, record_type: int, data: bytes = b"") -> str:
    payload = bytes((len(data), address >> 8, address & 0xFF, record_type)) + data
    checksum = (-sum(payload)) & 0xFF
    return ":" + (payload + bytes((checksum,))).hex().upper()


def ihex_image(chunks: List[Tuple[int, bytes]]) -> bytes:
    lines: List[str] = []
    upper = None
    for absolute_address, data in chunks:
        next_upper = absolute_address >> 16
        if next_upper != upper:
            lines.append(ihex_record(0, 4, next_upper.to_bytes(2, "big")))
            upper = next_upper
        lines.append(ihex_record(absolute_address & 0xFFFF, 0, data))
    lines.append(ihex_record(0, 1))
    return ("\n".join(lines) + "\n").encode("ascii")


def assert_flash_error(code: FlashErrorCode, call) -> FlashError:
    with pytest.raises(FlashError) as raised:
        call()
    assert raised.value.code is code
    return raised.value


def test_inspects_bin_and_previews_bytes(tmp_path: Path):
    firmware = tmp_path / "firmware.BIN"
    snapshot_root = tmp_path / "snapshots"
    payload = bytes(range(32))
    firmware.write_bytes(payload)

    inspector = ImageInspector(snapshot_root=snapshot_root)
    inspection = inspector.inspect(firmware, (flash_region(),), base_address=FLASH_START)

    assert re.fullmatch(r"[A-Za-z0-9_-]+", inspection.image_id)
    assert inspection.format == "bin"
    assert inspection.file_name == firmware.name
    snapshot = Path(inspection.file_path)
    assert snapshot.parent == snapshot_root.resolve()
    assert snapshot.name != firmware.name
    assert snapshot.suffix == firmware.suffix
    assert snapshot.read_bytes() == payload
    assert inspection.size == len(payload)
    assert inspection.sha256 == hashlib.sha256(payload).hexdigest()
    assert inspection.start == FLASH_START
    assert inspection.end == FLASH_START + len(payload)
    assert inspection.segments == (ImageSegment(FLASH_START, FLASH_START + len(payload)),)
    assert inspection.base_address == FLASH_START

    preview = inspector.preview(inspection.image_id, FLASH_START, 16)
    assert preview.address == FLASH_START
    assert preview.data == bytes(range(16))
    assert preview.present == (True,) * 16
    with pytest.raises(FrozenInstanceError):
        preview.address = 0


@pytest.mark.parametrize("base_address", [None, -1, True, 1.5, "0x08000000"])
def test_bin_requires_nonnegative_integer_base(tmp_path: Path, base_address):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x01")

    assert_flash_error(
        FlashErrorCode.BIN_ADDRESS_MISSING,
        lambda: ImageInspector().inspect(
            firmware, (flash_region(),), base_address=base_address
        ),
    )


def test_colon_prefixed_non_hex_content_is_valid_bin(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    payload = b":not intel hex at all"
    firmware.write_bytes(payload)
    inspector = ImageInspector(snapshot_root=tmp_path / "snapshots")

    inspection = inspector.inspect(
        firmware, (flash_region(),), base_address=FLASH_START
    )

    assert inspection.format == "bin"
    preview = inspector.preview(inspection.image_id, FLASH_START, len(payload))
    assert preview.data == payload
    assert preview.present == (True,) * len(payload)


def test_hex_detection_limit_is_not_proof_that_bin_contains_valid_hex(
    tmp_path: Path,
):
    firmware = tmp_path / "firmware.bin"
    payload = (
        ihex_record(0, 4, b"\x08\x00").encode("ascii")
        + b"\n"
        + ihex_record(0, 4, b"\x08\x00").encode("ascii")
        + b"\n:NOTHEX\n"
    )
    firmware.write_bytes(payload)
    inspector = ImageInspector(
        snapshot_root=tmp_path / "snapshots",
        max_hex_records=1,
    )

    inspection = inspector.inspect(
        firmware, (flash_region(),), base_address=FLASH_START
    )

    assert inspection.format == "bin"
    assert inspector.preview(inspection.image_id, FLASH_START, len(payload)).data == (
        payload
    )


def test_hex_outside_flash_is_rejected(tmp_path: Path):
    firmware = tmp_path / "outside.hex"
    firmware.write_bytes(ihex_image([(0x09000000, b"\x01\x02")]))

    assert_flash_error(
        FlashErrorCode.IMAGE_OUT_OF_RANGE,
        lambda: ImageInspector().inspect(firmware, (flash_region(),)),
    )


def test_inspects_sparse_hex_and_marks_preview_gaps(tmp_path: Path):
    firmware = tmp_path / "sparse.HEX"
    source = ihex_image(
        [(FLASH_START + 2, b"\x10\xFF"), (FLASH_START + 7, b"\x20\x21")]
    )
    firmware.write_bytes(source)

    inspector = ImageInspector()
    inspection = inspector.inspect(firmware, (flash_region(),))

    assert inspection.format == "hex"
    assert inspection.size == len(source)
    assert inspection.sha256 == hashlib.sha256(source).hexdigest()
    assert inspection.start == FLASH_START + 2
    assert inspection.end == FLASH_START + 9
    assert inspection.segments == (
        ImageSegment(FLASH_START + 2, FLASH_START + 4),
        ImageSegment(FLASH_START + 7, FLASH_START + 9),
    )
    assert inspection.base_address is None

    preview = inspector.preview(inspection.image_id, FLASH_START, 10)
    assert preview.data == b"\xFF\xFF\x10\xFF\xFF\xFF\xFF\x20\x21\xFF"
    assert preview.present == (
        False,
        False,
        True,
        True,
        False,
        False,
        False,
        True,
        True,
        False,
    )


def test_hex_inspection_does_not_use_intelhex_address_dictionary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    firmware = tmp_path / "firmware.hex"
    firmware.write_bytes(ihex_image([(FLASH_START, b"abcd")]))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("production parser must stream Intel HEX")

    monkeypatch.setattr(images_module, "IntelHex", fail_if_called, raising=False)
    inspector = ImageInspector(snapshot_root=tmp_path / "snapshots")

    inspection = inspector.inspect(firmware, (flash_region(),))

    assert inspection.segments == (ImageSegment(FLASH_START, FLASH_START + 4),)
    assert inspector.preview(inspection.image_id, FLASH_START, 4).data == b"abcd"


def test_extended_segment_and_linear_records_compute_absolute_addresses(
    tmp_path: Path,
):
    segment_address = 0x12350
    linear_address = FLASH_START + 0x20
    firmware = tmp_path / "addressing.hex"
    firmware.write_bytes(
        (
            "\n".join(
                (
                    ihex_record(0, 2, b"\x12\x34"),
                    ihex_record(0x10, 0, b"ab"),
                    ihex_record(0, 4, b"\x08\x00"),
                    ihex_record(0x20, 0, b"cd"),
                    ihex_record(0, 1),
                )
            )
            + "\n"
        ).encode("ascii")
    )
    regions = (
        flash_region(start=segment_address, length=0x10, name="segment"),
        flash_region(start=FLASH_START, length=0x100, name="linear"),
    )
    inspector = ImageInspector(snapshot_root=tmp_path / "snapshots")

    inspection = inspector.inspect(firmware, regions)

    assert inspection.segments == (
        ImageSegment(segment_address, segment_address + 2),
        ImageSegment(linear_address, linear_address + 2),
    )


@pytest.mark.parametrize(
    "contents",
    [
        b":0100000001FF\n:00000001FF\n",  # bad checksum
        b":0200000001FC\n:00000001FF\n",  # byte count mismatch
        ihex_record(0, 4, b"\x08\x00").encode("ascii")
        + b"\n"
        + ihex_record(0, 0, b"a").encode("ascii")
        + b"\n",  # missing EOF
        ihex_image([(FLASH_START, b"ab"), (FLASH_START + 1, b"cd")]),
        ihex_record(0, 6).encode("ascii")
        + b"\n"
        + ihex_record(0, 1).encode("ascii")
        + b"\n",  # unknown record type
    ],
)
def test_streaming_hex_parser_rejects_invalid_records(
    tmp_path: Path, contents: bytes
):
    firmware = tmp_path / "invalid.hex"
    firmware.write_bytes(contents)

    assert_flash_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: ImageInspector(snapshot_root=tmp_path / "snapshots").inspect(
            firmware, (flash_region(),)
        ),
    )


@pytest.mark.parametrize(
    ("name", "contents"),
    [
        ("bad.hex", b"not intel hex"),
        ("empty.hex", ihex_record(0, 1).encode("ascii") + b"\n"),
        ("hex-disguised.bin", ihex_image([(FLASH_START, b"\x01")])),
        ("raw-disguised.hex", b"\x00\x01\x02"),
    ],
)
def test_rejects_malformed_empty_or_extension_disguised_content(
    tmp_path: Path, name: str, contents: bytes
):
    firmware = tmp_path / name
    firmware.write_bytes(contents)

    assert_flash_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: ImageInspector().inspect(
            firmware, (flash_region(),), base_address=FLASH_START
        ),
    )


def test_rejects_empty_bin_unknown_extension_and_non_file(tmp_path: Path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    unknown = tmp_path / "firmware.elf"
    unknown.write_bytes(b"ELF")
    directory = tmp_path / "folder.bin"
    directory.mkdir()

    inspector = ImageInspector()
    for path in (empty, unknown):
        assert_flash_error(
            FlashErrorCode.FILE_FORMAT_ERROR,
            lambda path=path: inspector.inspect(
                path, (flash_region(),), base_address=FLASH_START
            ),
        )
    assert_flash_error(
        FlashErrorCode.FILE_NOT_FOUND,
        lambda: inspector.inspect(directory, (flash_region(),), base_address=FLASH_START),
    )
    assert_flash_error(
        FlashErrorCode.FILE_NOT_FOUND,
        lambda: inspector.inspect(
            tmp_path / "missing.bin", (flash_region(),), base_address=FLASH_START
        ),
    )
    with pytest.raises(TypeError):
        inspector.inspect(123, (flash_region(),), base_address=FLASH_START)


@pytest.mark.parametrize(
    "regions",
    [
        (flash_region(length=16),),
        (flash_region(writable=False),),
        (flash_region(is_flash=False),),
        (flash_region(length=0),),
    ],
)
def test_segment_must_fit_one_positive_writable_flash_region(
    tmp_path: Path, regions: Tuple[MemoryRegion, ...]
):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(bytes(range(32)))

    assert_flash_error(
        FlashErrorCode.IMAGE_OUT_OF_RANGE,
        lambda: ImageInspector().inspect(
            firmware, regions, base_address=FLASH_START
        ),
    )


def test_segment_can_cross_adjacent_writable_flash_regions(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(bytes(range(32)))
    regions = (
        flash_region(length=16, name="A"),
        flash_region(start=FLASH_START + 16, length=16, name="B"),
    )

    inspection = ImageInspector().inspect(
        firmware, regions, base_address=FLASH_START
    )

    assert inspection.start == FLASH_START
    assert inspection.end == FLASH_START + 32


def test_overlapping_regions_are_checked_deterministically(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(bytes(range(16)))
    regions = (
        flash_region(length=8, name="too-small"),
        flash_region(length=32, name="fitting"),
    )

    inspection = ImageInspector().inspect(
        firmware, regions, base_address=FLASH_START
    )
    assert inspection.end == FLASH_START + 16


def test_preview_validates_id_range_and_size_cap(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x01")
    inspector = ImageInspector()
    inspection = inspector.inspect(firmware, (flash_region(),), base_address=FLASH_START)

    assert_flash_error(
        FlashErrorCode.FILE_NOT_FOUND,
        lambda: inspector.preview("unknown", FLASH_START, 1),
    )
    for address, length in ((-1, 1), (0, -1), (0, 4097)):
        with pytest.raises(ValueError):
            inspector.preview(inspection.image_id, address, length)
    with pytest.raises(ValueError):
        inspector.preview(inspection.image_id, True, 1)

    outside = inspector.preview(inspection.image_id, FLASH_START + 100, 3)
    assert outside.data == b"\xFF\xFF\xFF"
    assert outside.present == (False, False, False)


def test_snapshot_is_immutable_when_source_changes_and_hash_detects_snapshot_rewrite(
    tmp_path: Path,
):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcd")
    inspector = ImageInspector(snapshot_root=tmp_path / "snapshots")
    inspection = inspector.inspect(firmware, (flash_region(),), base_address=FLASH_START)
    original_stat = firmware.stat()
    snapshot = Path(inspection.file_path)

    assert inspector.validate_unchanged(inspection.image_id) == inspection

    firmware.write_bytes(b"wxyz")
    os.utime(firmware, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert inspector.preview(inspection.image_id, FLASH_START, 4).data == b"abcd"
    assert inspector.validate_unchanged(inspection.image_id) == inspection

    snapshot_stat = snapshot.stat()
    snapshot.write_bytes(b"WXYZ")
    os.utime(snapshot, ns=(snapshot_stat.st_atime_ns, snapshot_stat.st_mtime_ns))
    changed = assert_flash_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: inspector.validate_unchanged(inspection.image_id),
    )
    assert "firmware changed" in changed.message

    snapshot.unlink()
    assert_flash_error(
        FlashErrorCode.FILE_NOT_FOUND,
        lambda: inspector.validate_unchanged(inspection.image_id),
    )
    assert_flash_error(
        FlashErrorCode.FILE_NOT_FOUND,
        lambda: inspector.validate_unchanged("unknown"),
    )


def test_replacing_or_deleting_source_does_not_change_snapshot(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcd")
    inspector = ImageInspector(snapshot_root=tmp_path / "snapshots")
    inspection = inspector.inspect(firmware, (flash_region(),), base_address=FLASH_START)

    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"wxyz")
    firmware.unlink()
    replacement.rename(firmware)

    assert inspector.preview(inspection.image_id, FLASH_START, 4).data == b"abcd"
    assert inspector.validate_unchanged(inspection.image_id) == inspection
    firmware.unlink()
    assert inspector.validate_unchanged(inspection.image_id) == inspection


def test_limits_file_size_and_cleans_failed_snapshot(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcd")
    snapshot_root = tmp_path / "snapshots"
    inspector = ImageInspector(snapshot_root=snapshot_root, max_file_size=3)

    assert_flash_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: inspector.inspect(
            firmware, (flash_region(),), base_address=FLASH_START
        ),
    )

    assert firmware.read_bytes() == b"abcd"
    assert list(snapshot_root.iterdir()) == []


def test_limits_hex_decoded_size_and_cleans_failed_snapshot(tmp_path: Path):
    firmware = tmp_path / "firmware.hex"
    firmware.write_bytes(
        ihex_record(0, 4, b"\x08\x00").encode("ascii")
        + b"\n"
        + ihex_record(0, 0, b"abcd").encode("ascii")
        + b"\n"
        + b":this later line is deliberately invalid\n"
    )
    snapshot_root = tmp_path / "snapshots"
    inspector = ImageInspector(
        snapshot_root=snapshot_root,
        max_hex_decoded_size=3,
    )

    error = assert_flash_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: inspector.inspect(firmware, (flash_region(),)),
    )

    assert "decoded" in error.message
    assert firmware.exists()
    assert list(snapshot_root.iterdir()) == []


def test_limits_hex_record_count_before_reading_later_invalid_line(tmp_path: Path):
    firmware = tmp_path / "many-records.hex"
    firmware.write_bytes(
        ihex_record(0, 4, b"\x08\x00").encode("ascii")
        + b"\n"
        + b"\n".join(
            ihex_record(offset, 0, bytes((offset,))).encode("ascii")
            for offset in range(4)
        )
        + b"\n:this later line is deliberately invalid\n"
    )
    snapshot_root = tmp_path / "snapshots"
    inspector = ImageInspector(snapshot_root=snapshot_root, max_hex_records=3)

    error = assert_flash_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: inspector.inspect(firmware, (flash_region(),)),
    )

    assert "record" in error.message
    assert list(snapshot_root.iterdir()) == []


@pytest.mark.parametrize(
    ("record_type", "metadata", "region"),
    [
        (4, b"\x08\x00", flash_region()),
        (5, b"\x00\x00\x00\x00", flash_region(start=0, length=0x100)),
    ],
)
def test_hex_record_limit_counts_metadata_before_state_changes(
    tmp_path: Path,
    record_type: int,
    metadata: bytes,
    region: MemoryRegion,
):
    firmware = tmp_path / "metadata.hex"
    firmware.write_bytes(
        (
            "\n".join(
                (
                    ihex_record(0, record_type, metadata),
                    ihex_record(0, record_type, metadata),
                    ihex_record(0, 0, b"a"),
                    ihex_record(0, 1),
                )
            )
            + "\n"
        ).encode("ascii")
    )
    snapshot_root = tmp_path / "snapshots"
    inspector = ImageInspector(snapshot_root=snapshot_root, max_hex_records=1)

    error = assert_flash_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: inspector.inspect(firmware, (region,)),
    )

    assert "record" in error.message
    assert list(snapshot_root.iterdir()) == []


def test_limits_sparse_hex_segment_count_and_cleans_snapshot(tmp_path: Path):
    firmware = tmp_path / "many-segments.hex"
    firmware.write_bytes(
        ihex_image(
            [
                (FLASH_START, b"a"),
                (FLASH_START + 2, b"b"),
                (FLASH_START + 4, b"c"),
            ]
        )
    )
    snapshot_root = tmp_path / "snapshots"
    inspector = ImageInspector(snapshot_root=snapshot_root, max_hex_segments=2)

    error = assert_flash_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: inspector.inspect(firmware, (flash_region(),)),
    )

    assert "segment" in error.message
    assert list(snapshot_root.iterdir()) == []


def test_contiguous_hex_records_merge_within_segment_budget(tmp_path: Path):
    firmware = tmp_path / "contiguous.hex"
    firmware.write_bytes(
        ihex_image(
            [
                (FLASH_START, b"a"),
                (FLASH_START + 1, b"b"),
                (FLASH_START + 2, b"c"),
                (FLASH_START + 3, b"d"),
            ]
        )
    )
    inspector = ImageInspector(
        snapshot_root=tmp_path / "snapshots",
        max_hex_records=5,
        max_hex_segments=1,
    )

    inspection = inspector.inspect(firmware, (flash_region(),))

    assert inspection.segments == (ImageSegment(FLASH_START, FLASH_START + 4),)
    assert inspector.preview(inspection.image_id, FLASH_START, 4).data == b"abcd"


def test_sparse_hex_registry_keeps_segment_bytes_not_per_address_dict(tmp_path: Path):
    firmware = tmp_path / "sparse.hex"
    firmware.write_bytes(
        ihex_image(
            [(FLASH_START, b"ab"), (FLASH_START + 0xF000, b"cd")]
        )
    )
    inspector = ImageInspector(snapshot_root=tmp_path / "snapshots")
    inspection = inspector.inspect(firmware, (flash_region(),))

    record = inspector._records[inspection.image_id]
    assert not hasattr(record, "hex_data")
    assert record.hex_segments == (
        (ImageSegment(FLASH_START, FLASH_START + 2), b"ab"),
        (ImageSegment(FLASH_START + 0xF000, FLASH_START + 0xF002), b"cd"),
    )
    assert inspector.preview(inspection.image_id, FLASH_START + 0xEFFF, 4).data == (
        b"\xFFcd\xFF"
    )
    assert inspector.preview(
        inspection.image_id, FLASH_START + 0xEFFF, 4
    ).present == (False, True, True, False)


@pytest.mark.parametrize("unchanged_metadata", [False, True])
def test_source_change_during_copy_is_rejected_without_leaking_snapshot(
    tmp_path: Path,
    monkeypatch,
    unchanged_metadata,
):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"AAAA")
    snapshot_root = tmp_path / "snapshots"
    changed = False

    def change_source(source_path: Path, copied_size: int) -> None:
        nonlocal changed
        if not changed:
            changed = True
            source_path.write_bytes(b"BBBB")

    inspector = ImageInspector(snapshot_root=snapshot_root, copy_hook=change_source)
    if unchanged_metadata:
        # Model low-resolution/retained filesystem timestamps explicitly.
        monkeypatch.setattr(inspector, "_same_stat", lambda before, after: True)

    assert_flash_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: inspector.inspect(
            firmware, (flash_region(),), base_address=FLASH_START
        ),
    )
    assert firmware.exists()
    assert list(snapshot_root.iterdir()) == []


def test_close_removes_only_owned_snapshots(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcd")
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    sentinel = snapshot_root / "keep.txt"
    sentinel.write_text("user file", encoding="utf-8")
    inspector = ImageInspector(snapshot_root=snapshot_root)
    inspection = inspector.inspect(firmware, (flash_region(),), base_address=FLASH_START)
    snapshot = Path(inspection.file_path)

    inspector.close()
    inspector.shutdown()

    assert not snapshot.exists()
    assert snapshot_root.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "user file"
    assert firmware.read_bytes() == b"abcd"


def test_close_removes_default_private_snapshot_root(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcd")
    inspector = ImageInspector()
    inspection = inspector.inspect(firmware, (flash_region(),), base_address=FLASH_START)
    snapshot_root = Path(inspection.file_path).parent

    inspector.close()

    assert not snapshot_root.exists()


def test_covered_sectors_are_unique_aligned_and_clipped(tmp_path: Path):
    firmware = tmp_path / "sparse.hex"
    firmware.write_bytes(
        ihex_image(
            [
                (FLASH_START + 1, b"\x01"),
                (FLASH_START + 7, b"\x02"),
                (FLASH_START + 18, b"\x03"),
            ]
        )
    )
    region = flash_region(length=20, sector_size=8)
    inspector = ImageInspector()
    inspection = inspector.inspect(firmware, (region,))

    coverage = inspector.covered_sectors(inspection.image_id, (region,))

    assert coverage.sector_operations_available is True
    assert coverage.sectors == (
        SectorRecord(FLASH_START, 8),
        SectorRecord(FLASH_START + 16, 4),
    )


def test_contiguous_image_can_cross_adjacent_variable_sector_regions(
    tmp_path: Path,
):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(bytes(range(20)))
    regions = (
        flash_region(length=8, sector_size=4, name="small-sectors"),
        flash_region(
            start=FLASH_START + 8,
            length=16,
            sector_size=8,
            name="large-sectors",
        ),
    )
    inspector = ImageInspector()

    inspection = inspector.inspect(firmware, regions, base_address=FLASH_START)
    coverage = inspector.covered_sectors(inspection.image_id, regions)

    assert coverage.sector_operations_available is True
    assert coverage.sectors == (
        SectorRecord(FLASH_START, 4),
        SectorRecord(FLASH_START + 4, 4),
        SectorRecord(FLASH_START + 8, 8),
        SectorRecord(FLASH_START + 16, 8),
    )


def test_sector_operations_unavailable_when_any_covered_region_lacks_geometry(
    tmp_path: Path,
):
    firmware = tmp_path / "sparse.hex"
    second_start = FLASH_START + 0x100
    firmware.write_bytes(
        ihex_image([(FLASH_START, b"\x01"), (second_start, b"\x02")])
    )
    regions = (
        flash_region(length=0x10, sector_size=8, name="known"),
        flash_region(
            start=second_start,
            length=0x10,
            sector_size=None,
            name="unknown",
        ),
    )
    inspector = ImageInspector()
    inspection = inspector.inspect(firmware, regions)

    coverage = inspector.covered_sectors(inspection.image_id, regions)
    assert coverage.sector_operations_available is False
    assert coverage.sectors == ()


def test_sector_operations_unavailable_when_intersecting_region_lacks_geometry(
    tmp_path: Path,
):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(bytes(range(16)))
    inspected_region = flash_region(length=16, sector_size=8)
    inspector = ImageInspector()
    inspection = inspector.inspect(
        firmware, (inspected_region,), base_address=FLASH_START
    )
    coverage_regions = (
        inspected_region,
        flash_region(
            start=FLASH_START + 4,
            length=8,
            sector_size=None,
            name="overlap-without-geometry",
        ),
    )

    coverage = inspector.covered_sectors(inspection.image_id, coverage_regions)

    assert coverage.sector_operations_available is False
    assert coverage.sectors == ()


def test_sector_operations_unavailable_for_conflicting_overlapping_geometry(
    tmp_path: Path,
):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(bytes(range(16)))
    region = flash_region(length=16, sector_size=8)
    inspector = ImageInspector()
    inspection = inspector.inspect(firmware, (region,), base_address=FLASH_START)
    coverage_regions = (
        region,
        flash_region(length=16, sector_size=16, name="different-geometry"),
    )

    coverage = inspector.covered_sectors(inspection.image_id, coverage_regions)

    assert coverage.sector_operations_available is False
    assert coverage.sectors == ()


def test_identical_overlapping_sector_geometry_is_deduplicated(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(bytes(range(12)))
    region = flash_region(length=16, sector_size=8)
    duplicate = flash_region(length=16, sector_size=8, name="duplicate")
    inspector = ImageInspector()
    inspection = inspector.inspect(firmware, (region,), base_address=FLASH_START)

    coverage = inspector.covered_sectors(inspection.image_id, (duplicate, region))

    assert coverage.sector_operations_available is True
    assert coverage.sectors == (
        SectorRecord(FLASH_START, 8),
        SectorRecord(FLASH_START + 8, 8),
    )


def test_inspector_registries_are_isolated(tmp_path: Path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x01")
    first = ImageInspector()
    second = ImageInspector()
    inspection = first.inspect(firmware, (flash_region(),), base_address=FLASH_START)

    assert_flash_error(
        FlashErrorCode.FILE_NOT_FOUND,
        lambda: second.preview(inspection.image_id, FLASH_START, 1),
    )
    assert first.preview(inspection.image_id, FLASH_START, 1).data == b"\x01"
