import hashlib
import json
from pathlib import Path

import pytest

from mklink.cmsis_dap.builtin_flm_bundle import (
    BuiltinFlmBundleError,
    discover_builtin_option_algorithm,
    discover_builtin_flm_algorithms,
    extract_builtin_flm,
    load_builtin_flm_targets,
    resolve_builtin_flm_part,
)


def _bundle(root: Path) -> tuple[Path, bytes]:
    payload = b"builtin-flm"
    digest = hashlib.sha256(payload).hexdigest()
    blob = root / "blobs" / digest[:2] / (digest + ".flm")
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)
    (root / "manifest.json").write_text(json.dumps({
        "schema": 1,
        "release_note": "增加了常用型号",
        "targets": [{
            "manufacturer": "Vendor",
            "series": "Family",
            "part_number": "PART-A",
            "ram_start": 0x20000000,
            "ram_size": 0x1000,
            "algorithms": [{
                "file_name": "EXT.FLM",
                "flash_start": 0x90000000,
                "flash_size": 0x800000,
                "sha256": digest,
                "blob": "blobs/{}/{}.flm".format(digest[:2], digest),
                "page_size": 256,
                "sector_sizes": [[0, 4096], [0x10000, 65536]],
            }],
            "option_algorithm": None,
        }],
    }), encoding="utf-8")
    return root, payload


def test_builtin_flm_bundle_exposes_targets_regions_and_verified_bytes(tmp_path: Path):
    root, payload = _bundle(tmp_path / "bundle")

    targets = load_builtin_flm_targets(root)
    algorithms = discover_builtin_flm_algorithms("part-a", root)

    assert [(item.part_number, item.vendor, item.source) for item in targets] == [
        ("PART-A", "Vendor", "daplink-builtin")
    ]
    assert targets[0].series == "Family"
    assert len(algorithms) == 1
    assert algorithms[0].source_kind == "daplink-builtin"
    assert algorithms[0].flash_start == 0x90000000
    assert algorithms[0].sector_sizes == ((0, 4096), (0x10000, 65536))
    assert extract_builtin_flm(algorithms[0]) == payload


def test_builtin_flm_bundle_rejects_changed_blob(tmp_path: Path):
    root, _payload = _bundle(tmp_path / "bundle")
    algorithm = discover_builtin_flm_algorithms("PART-A", root)[0]
    Path(algorithm.builtin_blob_path).write_bytes(b"changed")

    with pytest.raises(BuiltinFlmBundleError, match="integrity"):
        extract_builtin_flm(algorithm)


def test_builtin_option_algorithm_is_explicit_and_integrity_checked(tmp_path: Path):
    root, _payload = _bundle(tmp_path / "bundle")
    option_payload = b"option-flm"
    digest = hashlib.sha256(option_payload).hexdigest()
    blob = root / "blobs" / digest[:2] / (digest + ".flm")
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(option_payload)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["targets"][0]["option_algorithm"] = {
        "file_name": "PART_OPT.FLM",
        "sha256": digest,
        "blob": "blobs/{}/{}.flm".format(digest[:2], digest),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    option = discover_builtin_option_algorithm("part-a", root)

    assert option is not None
    assert option.file_name == "PART_OPT.FLM"
    assert option.path.read_bytes() == option_payload
    assert option.sha256 == digest


def test_generic_target_aliases_exact_order_codes_only_when_capacity_is_unique(tmp_path: Path):
    root, _payload = _bundle(tmp_path / "bundle")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["targets"][0]["part_number"] = "STM32F413xG"
    second = json.loads(json.dumps(manifest["targets"][0]))
    second["part_number"] = "STM32F413xH"
    second["algorithms"][0]["flash_size"] = 0x180000
    manifest["targets"].append(second)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert resolve_builtin_flm_part("STM32F413VGHx", root) == "STM32F413xG"
    algorithms = discover_builtin_flm_algorithms("STM32F413VGHx", root)
    assert len(algorithms) == 1
    assert algorithms[0].target_part == "STM32F413VGHx"
    assert algorithms[0].flash_size == 0x800000


def test_concrete_target_accepts_only_a_short_ordering_suffix(tmp_path: Path):
    root, _payload = _bundle(tmp_path / "bundle")

    assert resolve_builtin_flm_part("PART-AT6", root) == "PART-A"
    assert resolve_builtin_flm_part("PART-A-UNBOUNDED-SUFFIX", root) is None


def test_uppercase_x_placeholders_resolve_vendor_generic_targets(tmp_path: Path):
    root, _payload = _bundle(tmp_path / "bundle")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["targets"][0]["part_number"] = "ACM32H5XX"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert resolve_builtin_flm_part("ACM32H503CBT6", root) == "ACM32H5XX"


def test_py32_full_order_code_resolves_by_its_capacity_character(tmp_path: Path):
    root, _payload = _bundle(tmp_path / "bundle")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["targets"][0]["part_number"] = "PY32F030x4"
    second = json.loads(json.dumps(manifest["targets"][0]))
    second["part_number"] = "PY32F030x8"
    second["ram_size"] = 0x2000
    second["algorithms"][0]["flash_size"] = 0x10000
    manifest["targets"].append(second)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert resolve_builtin_flm_part("PY32F030K28T6", root) == "PY32F030x8"
    assert resolve_builtin_flm_part("PY32F030K14T6", root) == "PY32F030x4"
    assert resolve_builtin_flm_part("PY32F030K28T6-E", root) == "PY32F030x8"


def test_builtin_flm_bundle_hpm_short_circuits_before_manifest_access(tmp_path: Path):
    missing = tmp_path / "missing"

    assert discover_builtin_flm_algorithms("HPM5301xEGx", missing) == []


def test_development_bundle_root_can_be_overridden(monkeypatch, tmp_path: Path):
    import mklink.cmsis_dap.builtin_flm_bundle as bundle

    monkeypatch.setattr(bundle.sys, "frozen", False, raising=False)
    monkeypatch.setenv("MKLINK_BUILTIN_FLM_ROOT", str(tmp_path / "bundle"))

    assert bundle._default_bundle_root() == tmp_path / "bundle"


def test_nonautomatic_algorithms_do_not_hide_other_target_sources(tmp_path: Path):
    root, _payload = _bundle(tmp_path / "bundle")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["targets"][0]["algorithms"][0]["automatic"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert load_builtin_flm_targets(root) == []
    assert discover_builtin_flm_algorithms("PART-A", root) == []
