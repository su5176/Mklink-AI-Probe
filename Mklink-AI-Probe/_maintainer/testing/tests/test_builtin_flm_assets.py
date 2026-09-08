import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = (
    PROJECT_ROOT
    / "skills"
    / "tauri-gui-builder"
    / "scripts"
    / "builtin_flm_assets.py"
)


@pytest.fixture
def assets_module():
    spec = importlib.util.spec_from_file_location("mklink_builtin_flm_assets", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_bundle(root: Path, *, include_release_note: bool = True) -> dict[str, object]:
    payload = b"\x7fELFbuilt-in-algorithm"
    digest = hashlib.sha256(payload).hexdigest()
    relative = f"blobs/{digest[:2]}/{digest}.flm"
    blob = root / relative
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)
    manifest = {
        "schema": 1,
        "target_count": 1,
        "blob_count": 1,
        "blobs": [{"file": relative, "sha256": digest, "size": len(payload)}],
        "targets": [{
            "part_number": "DEVICE_A",
            "manufacturer": "Vendor",
            "ram_start": 0x20000000,
            "ram_size": 0x1000,
            "algorithms": [{"blob": relative, "sha256": digest}],
            "option_algorithm": None,
        }],
    }
    if include_release_note:
        manifest["release_note"] = "增加了常用型号"
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def test_validate_bundle_checks_complete_catalog(assets_module, monkeypatch, tmp_path):
    monkeypatch.setattr(assets_module, "EXPECTED_TARGET_COUNT", 1)
    monkeypatch.setattr(assets_module, "EXPECTED_BLOB_COUNT", 1)
    make_bundle(tmp_path)

    manifest = assets_module.validate_bundle(tmp_path)

    assert manifest["release_note"] == "增加了常用型号"
    assert manifest["target_count"] == 1
    assert manifest["blob_count"] == 1


def test_validate_bundle_rejects_changed_algorithm(assets_module, monkeypatch, tmp_path):
    monkeypatch.setattr(assets_module, "EXPECTED_TARGET_COUNT", 1)
    monkeypatch.setattr(assets_module, "EXPECTED_BLOB_COUNT", 1)
    manifest = make_bundle(tmp_path)
    blob = tmp_path / manifest["blobs"][0]["file"]
    blob.write_bytes(b"changed")

    with pytest.raises(ValueError, match="wrong size|SHA-256"):
        assets_module.validate_bundle(tmp_path)


def test_install_bundle_removes_source_provenance(assets_module, monkeypatch, tmp_path):
    monkeypatch.setattr(assets_module, "EXPECTED_TARGET_COUNT", 1)
    monkeypatch.setattr(assets_module, "EXPECTED_BLOB_COUNT", 1)
    source = tmp_path / "source"
    original = make_bundle(source, include_release_note=False)
    original["source"] = {"product": "temporary input", "version": "unused"}
    (source / "manifest.json").write_text(json.dumps(original), encoding="utf-8")
    destination = tmp_path / "installed"

    installed = assets_module.install_bundle(source, destination)

    assert installed["release_note"] == "增加了常用型号"
    assert "source" not in installed
    assert set(installed) == {
        "schema", "release_note", "target_count", "blob_count", "blobs", "targets"
    }


def test_repository_local_bundle_is_complete_when_available(assets_module):
    root = PROJECT_ROOT / "_maintainer" / "local" / "builtin_flm"
    if not root.is_dir():
        pytest.skip("repository-local built-in algorithms are not installed")

    manifest = assets_module.validate_bundle(root)

    assert manifest["target_count"] == 7059
    assert manifest["blob_count"] == 2224
