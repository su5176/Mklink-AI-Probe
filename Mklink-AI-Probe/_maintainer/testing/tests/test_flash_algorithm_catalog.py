import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

from mklink.cmsis_dap.algorithm_catalog import (
    deploy_algorithm_to_probe,
    discover_flash_algorithms,
    extract_algorithm,
    resolve_firmware_algorithms,
)
from mklink.cmsis_dap.models import TargetRecord
from mklink.cmsis_dap.paths import PackPaths


def _pack(path: Path, version: str, internal: bytes, external: bytes) -> Path:
    descriptor = f"""<?xml version="1.0" encoding="UTF-8"?>
<package schemaVersion="1.7.0">
  <vendor>Vendor</vendor><name>Device_DFP</name>
  <releases><release version="{version}">Test</release></releases>
  <devices><family Dfamily="Test" Dvendor="Vendor:1">
    <processor Dcore="Cortex-M7" Dfpu="DP_FPU" Dmpu="MPU" Dendian="Little-endian"/>
    <device Dname="DEVICE_A">
      <memory name="IROM1" start="0x08000000" size="0x20000" access="rx" default="1" startup="1"/>
      <memory name="IRAM1" start="0x20000000" size="0x20000" access="rwx" default="1"/>
      <algorithm name="Flash/Internal.FLM" start="0x08000000" size="0x20000" RAMstart="0x20001000" default="1"/>
      <algorithm name="Flash/External.FLM" start="0x90000000" size="0x800000" RAMstart="0x20002000"/>
    </device>
  </family></devices>
</package>"""
    with ZipFile(path, "w") as archive:
        archive.writestr("Vendor.Device_DFP.pdsc", descriptor)
        archive.writestr("Flash/Internal.FLM", internal)
        archive.writestr("Flash/External.FLM", external)
    return path


def _record(pack: Path, version: str, source: str) -> TargetRecord:
    return TargetRecord(
        part_number="DEVICE_A",
        vendor="Vendor",
        pack_id="Vendor.Device_DFP",
        pack_version=version,
        pack_path=str(pack),
        installed=True,
        source=source,
    )


def test_bundle_precedes_installed_pack_and_exposes_all_exact_device_regions(tmp_path: Path):
    paths = PackPaths(tmp_path / "cache")
    paths.data_dir.mkdir(parents=True)
    installed = _pack(paths.data_dir / "installed.pack", "2.0.0", b"new-internal", b"new-external")
    bundled = _pack(tmp_path / "bundled.pack", "1.0.0", b"old-internal", b"old-external")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(json.dumps({
        "installed": {"Vendor.Device_DFP": {"2.0.0": str(installed)}},
    }), encoding="utf-8")

    algorithms = discover_flash_algorithms(
        "DEVICE_A",
        paths=paths,
        builtin_provider=lambda: [_record(bundled, "1.0.0", "bundle")],
    )

    assert [algorithm.source_kind for algorithm in algorithms] == [
        "builtin-pack",
        "builtin-pack",
        "installed-pack",
        "installed-pack",
    ]
    assert {algorithm.source_name for algorithm in algorithms[:2]} == {
        "Vendor.Device_DFP@1.0.0"
    }
    assert extract_algorithm(algorithms[1]) == b"old-external"


def test_daplink_bundle_precedes_installed_pack(tmp_path: Path):
    from mklink.cmsis_dap.algorithm_catalog import FlashAlgorithm

    paths = PackPaths(tmp_path / "cache")
    paths.data_dir.mkdir(parents=True)
    installed = _pack(paths.data_dir / "installed.pack", "2.0.0", b"installed", b"external")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(json.dumps({
        "installed": {"Vendor.Device_DFP": {"2.0.0": str(installed)}},
    }), encoding="utf-8")
    bundled = FlashAlgorithm(
        algorithm_id="d" * 64,
        target_part="DEVICE_A",
        file_name="DAP.FLM",
        flash_start=0x08000000,
        flash_size=0x20000,
        ram_start=0x20000000,
        ram_size=0x1000,
        default=True,
        source_kind="daplink-builtin",
        source_name="常用型号内置算法",
        source_token="daplink:test",
        builtin_blob_path=str(tmp_path / "blob.flm"),
        builtin_blob_sha256="d" * 64,
    )

    algorithms = discover_flash_algorithms(
        "DEVICE_A",
        paths=paths,
        builtin_provider=lambda: [],
        daplink_provider=lambda target: [bundled] if target == "DEVICE_A" else [],
    )

    assert algorithms[0] == bundled
    assert algorithms[0].source_kind == "daplink-builtin"


def test_resolution_falls_through_sources_when_builtin_does_not_cover_range(tmp_path: Path):
    paths = PackPaths(tmp_path / "cache")
    paths.data_dir.mkdir(parents=True)
    installed = _pack(
        paths.data_dir / "installed.pack",
        "2.0.0",
        b"installed-internal",
        b"installed-external",
    )
    bundled = _pack(
        tmp_path / "bundled.pack",
        "1.0.0",
        b"bundled-internal",
        b"bundled-external",
    )
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(json.dumps({
        "installed": {"Vendor.Device_DFP": {"2.0.0": str(installed)}},
    }), encoding="utf-8")

    algorithms = discover_flash_algorithms(
        "DEVICE_A",
        paths=paths,
        builtin_provider=lambda: [_record(bundled, "1.0.0", "bundle")],
    )
    bundled_internal_only = [
        algorithm for algorithm in algorithms
        if algorithm.source_kind == "builtin-pack"
        and algorithm.flash_start == 0x08000000
    ]
    installed_external = [
        algorithm for algorithm in algorithms
        if algorithm.source_kind == "installed-pack"
        and algorithm.flash_start == 0x90000000
    ]

    selected = resolve_firmware_algorithms(
        bundled_internal_only + installed_external,
        ((0x90001000, 0x90002000),),
    )

    assert selected[0].algorithm.source_kind == "installed-pack"
    assert extract_algorithm(selected[0].algorithm) == b"installed-external"


def test_resolution_preserves_installed_pack_version_order(tmp_path: Path):
    paths = PackPaths(tmp_path / "cache")
    paths.data_dir.mkdir(parents=True)
    older = _pack(
        paths.data_dir / "older.pack",
        "2.0.0",
        b"older-internal",
        b"older-external",
    )
    newer = _pack(
        paths.data_dir / "newer.pack",
        "3.0.0",
        b"newer-internal",
        b"newer-external",
    )
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(json.dumps({
        "installed": {
            "Vendor.Device_DFP": {
                "2.0.0": str(older),
                "3.0.0": str(newer),
            }
        },
    }), encoding="utf-8")

    algorithms = discover_flash_algorithms(
        "DEVICE_A",
        paths=paths,
        builtin_provider=lambda: [],
        daplink_provider=lambda _target: [],
    )
    selected = resolve_firmware_algorithms(
        algorithms,
        ((0x08001000, 0x08002000),),
    )

    assert selected[0].algorithm.source_name == "Vendor.Device_DFP@3.0.0"
    assert extract_algorithm(selected[0].algorithm) == b"newer-internal"


def test_automatic_resolution_prefers_builtin_but_explicit_custom_is_honored():
    from mklink.cmsis_dap.algorithm_catalog import FlashAlgorithm

    builtin = FlashAlgorithm(
        algorithm_id="b" * 64,
        target_part="DEVICE_A",
        file_name="Builtin.FLM",
        flash_start=0x08000000,
        flash_size=0x20000,
        ram_start=0x20000000,
        ram_size=0x1000,
        default=True,
        source_kind="builtin-pack",
        source_name="Builtin",
        source_token="builtin:test",
    )
    custom = FlashAlgorithm(
        algorithm_id="c" * 64,
        target_part="DEVICE_A",
        file_name="Custom.FLM",
        flash_start=0x08000000,
        flash_size=0x20000,
        ram_start=0x20000000,
        ram_size=0x1000,
        default=False,
        source_kind="custom-flm",
        source_name="Custom",
        source_token="custom:test",
    )

    automatic = resolve_firmware_algorithms(
        [custom, builtin],
        ((0x08001000, 0x08002000),),
    )
    explicit = resolve_firmware_algorithms(
        [custom, builtin],
        ((0x08001000, 0x08002000),),
        preferred_algorithm_ids=(custom.algorithm_id,),
    )

    assert automatic[0].algorithm == builtin
    assert explicit[0].algorithm == custom


def test_offline_algorithm_list_keeps_lower_priority_explicit_choices(
    tmp_path: Path,
    monkeypatch,
):
    from mklink.cmsis_dap.algorithm_catalog import FlashAlgorithm
    from mklink.remote.offline_download_api import discover_algorithms

    common = {
        "target_part": "DEVICE_A",
        "file_name": "Device.FLM",
        "flash_start": 0x08000000,
        "flash_size": 0x20000,
        "ram_start": 0x20000000,
        "ram_size": 0x1000,
        "default": True,
    }
    builtin = FlashAlgorithm(
        algorithm_id="b" * 64,
        source_kind="builtin-pack",
        source_name="Builtin",
        source_token="catalog:bundle:Vendor.Pack:1:REVWSUNFX0E:0",
        **common,
    )
    custom = FlashAlgorithm(
        algorithm_id="c" * 64,
        source_kind="custom-flm",
        source_name="Custom",
        source_token="custom:REVWSUNFX0E:" + "c" * 64,
        **common,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.algorithm_catalog.discover_flash_algorithms",
        lambda *_args, **_kwargs: [builtin, custom],
    )

    candidates = discover_algorithms(PackPaths(tmp_path / "cache"), "DEVICE_A", None)

    assert [candidate["source_token"] for candidate in candidates] == [
        builtin.source_token,
        custom.source_token,
    ]


def test_state_pack_outside_managed_data_directory_is_ignored(tmp_path: Path):
    paths = PackPaths(tmp_path / "cache")
    paths.root.mkdir(parents=True)
    outside = _pack(tmp_path / "outside.pack", "2.0.0", b"outside", b"outside-ext")
    bundled = _pack(tmp_path / "bundled.pack", "1.0.0", b"builtin", b"builtin-ext")
    paths.state_file.write_text(json.dumps({
        "installed": {"Vendor.Device_DFP": {"2.0.0": str(outside)}},
    }), encoding="utf-8")

    algorithms = discover_flash_algorithms(
        "DEVICE_A",
        paths=paths,
        builtin_provider=lambda: [_record(bundled, "1.0.0", "bundle")],
    )

    assert extract_algorithm(algorithms[0]) == b"builtin"
    assert {algorithm.source_kind for algorithm in algorithms} == {"builtin-pack"}


def test_bundle_is_used_offline_and_firmware_ranges_select_external_flash(tmp_path: Path):
    paths = PackPaths(tmp_path / "cache")
    bundled = _pack(tmp_path / "bundled.pack", "1.0.0", b"internal", b"external")
    algorithms = discover_flash_algorithms(
        "DEVICE_A",
        paths=paths,
        builtin_provider=lambda: [_record(bundled, "1.0.0", "bundle")],
    )

    selected = resolve_firmware_algorithms(
        algorithms,
        ((0x90001000, 0x90002000),),
    )

    assert len(selected) == 1
    assert selected[0].algorithm.file_name == "External.FLM"
    assert selected[0].ranges == ((0x90001000, 0x90002000),)


def test_extract_algorithm_writes_content_addressed_verified_payload(tmp_path: Path):
    pack = _pack(tmp_path / "bundle.pack", "1.0.0", b"internal", b"external")
    algorithm = discover_flash_algorithms(
        "DEVICE_A",
        paths=PackPaths(tmp_path / "cache"),
        builtin_provider=lambda: [_record(pack, "1.0.0", "bundle")],
    )[0]

    destination = extract_algorithm(algorithm, tmp_path / "out")

    assert destination.is_file()
    assert destination.suffix.casefold() == ".flm"
    assert hashlib.sha256(destination.read_bytes()).hexdigest() in destination.name
    assert extract_algorithm(algorithm, tmp_path / "out") == destination

    probe_path = deploy_algorithm_to_probe(algorithm, disk_root=tmp_path / "probe")
    deployed = (tmp_path / "probe").joinpath(*probe_path.strip("/").split("/"))
    assert deployed.read_bytes() == b"internal"
    assert hashlib.sha256(b"internal").hexdigest() in deployed.name


def test_hpm_targets_never_resolve_flm_algorithms(tmp_path: Path):
    def unexpected_builtin_provider():
        raise AssertionError("HPM targets must not inspect Pack algorithms")

    assert discover_flash_algorithms(
        "HPM5301xEGx",
        paths=PackPaths(tmp_path / "cache"),
        builtin_provider=unexpected_builtin_provider,
    ) == []


def test_daplink_bundle_follows_pack_and_precedes_empty_result(tmp_path: Path):
    from mklink.cmsis_dap.algorithm_catalog import FlashAlgorithm

    builtin = FlashAlgorithm(
        algorithm_id="d" * 64,
        target_part="DEVICE_A",
        file_name="DAP.FLM",
        flash_start=0x08000000,
        flash_size=0x20000,
        ram_start=0x20000000,
        ram_size=0x1000,
        default=True,
        source_kind="daplink-builtin",
        source_name="常用型号内置算法",
        source_token="daplink:test",
        builtin_blob_path=str(tmp_path / "blob.flm"),
        builtin_blob_sha256="d" * 64,
    )

    algorithms = discover_flash_algorithms(
        "DEVICE_A",
        paths=PackPaths(tmp_path / "cache"),
        builtin_provider=lambda: [],
        daplink_provider=lambda target: [builtin] if target == "DEVICE_A" else [],
    )

    assert algorithms == [builtin]


def test_daplink_bundle_token_extracts_for_offline_deployment(
    tmp_path: Path, monkeypatch,
):
    from mklink.remote.offline_download_api import _pack_source, discover_algorithms

    payload = b"offline-daplink-flm"
    digest = hashlib.sha256(payload).hexdigest()
    bundle = tmp_path / "builtin-flm"
    blob = bundle / "blobs" / digest[:2] / (digest + ".flm")
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)
    (bundle / "manifest.json").write_text(json.dumps({
        "schema": 1,
        "targets": [{
            "manufacturer": "Vendor",
            "series": "Series",
            "part_number": "DEVICE_A",
            "ram_start": 0x20000000,
            "ram_size": 0x10000,
            "algorithms": [{
                "file_name": "Device.FLM",
                "sha256": digest,
                "blob": "blobs/{}/{}.flm".format(digest[:2], digest),
                "flash_start": 0x08000000,
                "flash_size": 0x20000,
                "automatic": True,
                "page_size": 0x100,
                "sector_sizes": [[0, 0x1000]],
            }],
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("MKLINK_BUILTIN_FLM_ROOT", str(bundle))
    paths = PackPaths(tmp_path / "cache")

    candidates = discover_algorithms(paths, "DEVICE_A", None)
    candidate = next(item for item in candidates if item["origin"] == "常用型号内置算法")
    destination = tmp_path / "offline.flm"

    assert _pack_source(paths, candidate["source_token"], destination) == destination
    assert destination.read_bytes() == payload
