import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "firmware" / "publish_firmware.py"


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location("mklink_publish_firmware", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _uf2(path: Path, payload: bytes = b"firmware") -> None:
    block = bytearray(512)
    block[0:4] = (0x0A324655).to_bytes(4, "little")
    block[4:8] = (0x9E5D5157).to_bytes(4, "little")
    block[16:20] = len(payload).to_bytes(4, "little")
    block[20:24] = (0).to_bytes(4, "little")
    block[24:28] = (1).to_bytes(4, "little")
    block[32:32 + len(payload)] = payload
    block[508:512] = (0x0AB16F30).to_bytes(4, "little")
    path.write_bytes(block)


def _urls(entries):
    return {
        str(entry["name"]): {
            "github": f"https://github.example/{entry['name']}",
            "gitee": f"https://gitee.example/{entry['name']}",
        }
        for entry in entries.values()
    }


def test_scan_selects_newest_exact_family_and_model(publisher, tmp_path):
    _uf2(tmp_path / "MicroLink_V3.3.7.uf2", b"v3")
    _uf2(tmp_path / "MicroLink_V4.3.6.uf2", b"v4-old")
    _uf2(tmp_path / "MicroLink_V4.3.8.uf2", b"v4-new")
    _uf2(tmp_path / "HPMLink_V4.3.7.uf2", b"hpm")

    entries = publisher.scan_firmware_directory(tmp_path)

    assert set(entries) == {
        ("microlink", "V3"), ("microlink", "V4"), ("hpmlink", "V4")
    }
    assert entries[("microlink", "V4")]["name"] == "MicroLink_V4.3.8.uf2"
    assert all(entry["uf2_blocks"] == 1 for entry in entries.values())


def test_scan_rejects_invalid_uf2(publisher, tmp_path):
    (tmp_path / "MicroLink_V3.3.7.uf2").write_bytes(b"not uf2")
    with pytest.raises(ValueError, match="multiple of 512"):
        publisher.scan_firmware_directory(tmp_path)


def test_merge_rejects_rollback_and_same_version_mutation(publisher, tmp_path):
    path = tmp_path / "MicroLink_V3.3.7.uf2"
    _uf2(path, b"new-content")
    local = publisher.scan_firmware_directory(tmp_path)
    urls = _urls(local)
    old = {
        "schema": "mklink-firmware-v1",
        "published_at": "old",
        "firmwares": [{
            "family": "microlink", "model": "V3", "version": "V3.3.8",
            "name": "MicroLink_V3.3.8.uf2", "size": 512,
            "sha256": "a" * 64, "uf2_blocks": 1,
            "urls": {"github": "https://old.example/v3"},
        }],
    }
    with pytest.raises(ValueError, match="rollback rejected"):
        publisher.merge_manifest(old, local, urls, published_at="now")

    old["firmwares"][0].update({
        "version": "V3.3.7", "name": "MicroLink_V3.3.7.uf2"
    })
    with pytest.raises(ValueError, match="different content"):
        publisher.merge_manifest(old, local, urls, published_at="now")


def test_merge_is_idempotent(publisher, tmp_path):
    _uf2(tmp_path / "HPMLink_V4.3.7.uf2")
    local = publisher.scan_firmware_directory(tmp_path)
    urls = _urls(local)
    first, changed = publisher.merge_manifest(None, local, urls, published_at="first")
    second, changed_again = publisher.merge_manifest(
        first, local, urls, published_at="second"
    )

    assert changed is True
    assert changed_again is False
    assert second == first


def test_publish_rejects_manifest_before_remote_asset_writes(
    publisher, tmp_path, monkeypatch
):
    _uf2(tmp_path / "MicroLink_V3.3.7.uf2", b"rollback")
    existing = {
        "schema": "mklink-firmware-v1",
        "published_at": "old",
        "firmwares": [{
            "family": "microlink", "model": "V3", "version": "V3.3.8",
            "name": "MicroLink_V3.3.8.uf2", "size": 512,
            "sha256": "a" * 64, "uf2_blocks": 1,
            "urls": {
                "github": "https://old.example/v3",
                "gitee": "https://old.example/v3",
            },
        }],
    }
    writes = []
    monkeypatch.setattr(publisher, "load_github_manifest", lambda _repo: existing)
    monkeypatch.setattr(
        publisher, "ensure_github_assets",
        lambda *_args: writes.append("github"),
    )
    monkeypatch.setattr(
        publisher, "ensure_gitee_assets",
        lambda *_args: writes.append("gitee"),
    )

    with pytest.raises(ValueError, match="rollback rejected"):
        publisher.publish_firmware(
            root=tmp_path,
            github_repo="owner/repo",
            gitee_repo="owner/repo",
            token="secret",
        )

    assert writes == []
