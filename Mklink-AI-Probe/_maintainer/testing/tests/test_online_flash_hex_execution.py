"""Inspection, actual pyOCD file staging, and verify share one HEX decoder."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from mklink.cmsis_dap.backend import PyOcdBackend
from mklink.cmsis_dap.errors import FlashError, FlashErrorCode
from mklink.cmsis_dap.images import ImageInspector
from mklink.cmsis_dap.models import ImageInspection, MemoryRegion
from mklink.cmsis_dap.pyocd_runtime import import_pyocd_module


def record(address, kind, data=b""):
    raw = bytes((len(data), address >> 8, address & 255, kind)) + data
    return ":" + (raw + bytes((-sum(raw) & 255,))).hex().upper()


@pytest.mark.parametrize("entries", [(5, 5), (3, 3), (3, 5)])
def test_combined_hex_entry_points_preserve_data_and_gaps(tmp_path, monkeypatch, entries):
    path = tmp_path / "combined.hex"
    text = "\n".join((
        record(0, 4, b"\x08\x00"), record(0, 0, b"boot"),
        record(0, entries[0], bytes.fromhex("08000131")),
        record(0x8000, 0, b"application"),
        record(0, entries[1], bytes.fromhex("08008131")), record(0, 1),
    )) + "\n"
    path.write_text(text, encoding="ascii")
    inspector = ImageInspector(tmp_path / "snapshots")
    staged = []
    events = []

    class Loader:
        def __init__(self, session, progress, chip_erase, *args):
            assert chip_erase == "sector"

        def add_data(self, address, data):
            staged.append((address, bytes(data)))

        def commit(self):
            events.append("commit")

    module = import_pyocd_module("pyocd.flash.file_programmer")
    monkeypatch.setattr(module, "FlashLoader", Loader)
    backend = PyOcdBackend()
    backend._session = SimpleNamespace(target=SimpleNamespace(memory_map=[]))
    try:
        image = inspector.inspect(path, (MemoryRegion("Flash", 0x08000000, 0x10000, True, True),))
        backend.program(image)
        expected = [(0x08000000, b"boot"), (0x08008000, b"application")]
        assert staged == expected
        assert events == ["commit"]
        assert list(backend._iter_image_chunks(image)) == expected
        assert Path(image.file_path).read_bytes() == path.read_bytes()
        assert path.read_text(encoding="ascii") == text
    finally:
        inspector.close()


@pytest.mark.parametrize("bad", [
    [record(0, 0, b"a"), record(0, 0, b"a"), record(0, 1)],
    [record(0, 0, b"a")[:-2] + "00", record(0, 1)],
    [record(0, 0, b"a"), record(0, 5, b"bad"), record(0, 1)],
    [record(0, 0, b"a")],
])
def test_invalid_hex_never_starts_algorithm(tmp_path, bad):
    path = tmp_path / "invalid.hex"
    path.write_text("\n".join(bad), encoding="ascii")
    events = []
    backend = PyOcdBackend()
    backend._session = SimpleNamespace(
        target=SimpleNamespace(reset_and_halt=lambda: events.append("reset")),
        close=lambda: None,
    )
    backend._algorithm_reset_required = True
    with pytest.raises(FlashError) as raised:
        backend.program(ImageInspection("invalid", file_path=str(path), format="hex"))
    assert raised.value.code is FlashErrorCode.FILE_FORMAT_ERROR
    assert events == []
