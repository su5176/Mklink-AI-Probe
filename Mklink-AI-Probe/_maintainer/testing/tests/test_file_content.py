import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mklink.file_content import copy_verified, source_fingerprint, write_verified
from mklink.flash import MKLinkFlash
from mklink.dwarf_parser import DwarfCache, DwarfInfo
from mklink.source_monitor import SourceMonitor
from mklink.symbol_catalog import AxfFingerprint
from mklink.utils import parse_load_result


def replace_preserving_metadata(path, data):
    stat = path.stat()
    path.write_bytes(data)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def test_copy_changed_same_size_and_timestamp(tmp_path):
    source, dest = tmp_path / "image.hex", tmp_path / "disk.hex"
    source.write_bytes(b"AAAA")
    assert copy_verified(source, dest)
    before = source_fingerprint(source)
    replace_preserving_metadata(source, b"BBBB")
    after = source_fingerprint(source)
    assert before["size"] == after["size"] and before["mtime_ns"] == after["mtime_ns"]
    assert before["sha256"] != after["sha256"]
    assert copy_verified(source, dest)
    assert dest.read_bytes() == b"BBBB"
    assert not copy_verified(source, dest)


def test_sync_failure_prevents_program_command(tmp_path, monkeypatch):
    source = tmp_path / "image.hex"
    source.write_bytes(b"image")
    disk = tmp_path / "disk"
    disk.mkdir()
    monkeypatch.setattr("mklink.flash.find_microkeen_disk", lambda: str(disk))
    monkeypatch.setattr("mklink.file_content.os.fsync", Mock(side_effect=OSError("flush failed")))
    bridge = Mock()
    with pytest.raises(Exception, match="拷贝"):
        MKLinkFlash(bridge).burn_hex(str(source))
    bridge.send_command.assert_not_called()


@pytest.mark.parametrize("name", ["../other.hex", "..\\other.hex", "C:other.hex", "._image.hex"])
def test_invalid_probe_file_names_rejected(tmp_path, monkeypatch, name):
    source = tmp_path / "image.hex"
    source.write_bytes(b"a")
    monkeypatch.setattr("mklink.flash.find_microkeen_disk", lambda: str(tmp_path))
    assert MKLinkFlash(Mock())._copy_to_microkeen(str(source), name) is None


@pytest.mark.parametrize("output,success", [
    ("0\n>>>", False),
    ("open failed\n0\n", False),
    ("Download: 100% ,used 100 ms\n0", True),
    ("/image.hex loaded successfully\n0", True),
    ("/image.hex loaded successfully\nVerify failed\n0", False),
    ("Download: 100% ,used 100 ms\nError\n0", False),
    ("/one.hex loaded success\n/two.hex loaded failed", False),
    ("Download: 99% ,used 100 ms\n0", False),
])
def test_programming_completion_requires_evidence(output, success):
    assert parse_load_result(output)["success"] is success


def test_dwarf_and_active_symbol_fingerprints_invalidate_same_metadata(tmp_path):
    path = tmp_path / "image.axf"
    path.write_bytes(b"old!")
    cache = DwarfCache(str(tmp_path / "cache"))
    cache.save(str(path), DwarfInfo(), backend="builtin", parser_version="test")
    fingerprint = AxfFingerprint.from_path(str(path))
    replace_preserving_metadata(path, b"new!")
    assert cache.load(str(path), backend="builtin", parser_version="test") is None
    assert AxfFingerprint.from_path(str(path)) != fingerprint


def test_source_monitor_tracks_content_deletion_recreation_once(tmp_path):
    path = tmp_path / "image.axf"
    path.write_bytes(b"old!")
    device = SimpleNamespace(_axf=str(path), symbol_catalog=None)
    monitor = SourceMonitor()
    assert monitor.changed(device, {}) == []
    replace_preserving_metadata(path, b"new!")
    assert monitor.changed(device, {}) == [str(path)]
    assert monitor.changed(device, {}) == []
    path.unlink()
    assert monitor.changed(device, {}) == [str(path)]
    assert monitor.changed(device, {}) == []
    path.write_bytes(b"back")
    assert monitor.changed(device, {}) == [str(path)]


def test_source_monitor_first_check_detects_stale_loaded_catalog(tmp_path):
    path = tmp_path / "image.axf"
    path.write_bytes(b"old!")
    catalog = SimpleNamespace(axf_path=str(path), fingerprint=AxfFingerprint.from_path(str(path)))
    replace_preserving_metadata(path, b"new!")
    monitor = SourceMonitor()
    assert monitor.changed(SimpleNamespace(_axf=str(path), symbol_catalog=catalog), {}) == [str(path)]


def test_rtt_reload_uses_active_axf_and_drops_obsolete_address(tmp_path, monkeypatch):
    from mklink import project_config as config
    active = tmp_path / 'active.axf'
    active.write_bytes(b'fixture')
    config.save_project_info(str(tmp_path), {'axf_path': str(tmp_path / 'old.axf')})
    config.save_rtt_config(str(tmp_path), {'rtt_addr': '0x20000010', 'rtt_storage_mode': 1})
    seen = []
    monkeypatch.setattr('mklink.rtt_addr.diagnose_rtt_addr',
                        lambda path: seen.append(path) or SimpleNamespace(addr='0x20000040'))
    result = config.ensure_rtt_config_updated(str(tmp_path), source_path=str(active))
    assert result['rtt_addr'] == '0x20000040'
    assert seen == [str(active)]
    monkeypatch.setattr('mklink.rtt_addr.diagnose_rtt_addr', lambda path: SimpleNamespace(addr=None))
    result = config.ensure_rtt_config_updated(str(tmp_path), source_path=str(active))
    assert 'rtt_addr' not in result
    assert result['rtt_storage_mode'] == 1
    from mklink.device import _resolve_rtt_stream_parameters
    with pytest.raises(ValueError, match='addr is required'):
        _resolve_rtt_stream_parameters(None, 0, 1024, None, str(tmp_path), source_path=str(active))
    active.unlink()
    with pytest.raises(FileNotFoundError):
        config.ensure_rtt_config_updated(str(tmp_path), source_path=str(active))
