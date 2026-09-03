import asyncio
import json
import asyncio
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mklink.cmsis_dap.errors import FlashError, FlashErrorCode
from mklink.cmsis_dap.algorithm_catalog import FlashAlgorithm
from mklink.cmsis_dap.images import SectorCoverage, SectorRecord
from mklink.cmsis_dap.models import (
    ImageInspection,
    ImageSegment,
    JobEvent,
    JobRequest,
    JobSnapshot,
    JobState,
    MemoryRegion,
    TargetRecord,
)
from mklink.cmsis_dap.paths import PackPaths
from mklink.cmsis_dap.pack_catalog import PackCatalog
from mklink.cmsis_dap.jobs import OnlineFlashJobManager
from mklink.remote.online_flash_api import (
    OnlineFlashServices,
    _blocking,
    _pack_memory_regions,
    _put_latest_pack_event,
    _target_flash_configuration,
    create_online_flash_router,
    default_target_memory_provider,
)
from mklink.remote.api import create_app
from mklink.remote.resource_manager import ResourceManager
from mklink.remote.online_flash_api import shutdown_online_flash_services


class Catalog:
    def __init__(self):
        self.calls = []
        self.refresh_count = 0

    def search(self, query, vendor=None, installed=None, limit=100):
        self.calls.append((query, vendor, installed, limit))
        records = [
            TargetRecord("DEVICE_A", "Vendor", "Vendor.Pack", "1.0", "safe.pack", True),
            TargetRecord("Other", "Vendor", installed=False),
        ]
        return [record for record in records if query.casefold() in record.part_number.casefold()][:limit]

    def status(self):
        return {"index_available": True, "target_count": 2, "last_error": None}

    def refresh(self):
        self.refresh_count += 1
        return self.status()


class CustomFlms:
    def __init__(self, root):
        self.root = Path(root)
        self.records = []

    def list(self, part_number):
        return tuple(record for record in self.records if record.target_part.casefold() == part_number.casefold())

    def add(self, path, file_name, part_number, existing_regions):
        assert Path(path).is_file()
        assert tuple(existing_regions) == ()
        stored = self.root / "custom.flm"
        stored.write_bytes(Path(path).read_bytes())
        record = type("Custom", (), {
            "algorithm_id": "algo-1", "target_part": part_number,
            "file_name": file_name, "file_path": str(stored),
            "flash_start": 0x90000000, "flash_size": 0x800000,
            "page_size": 0x1000, "sector_sizes": ((0, 0x1000),),
        })()
        self.records.append(record)
        return record

    def remove(self, part_number, algorithm_id):
        self.records = [r for r in self.records if not (
            r.target_part.casefold() == part_number.casefold() and r.algorithm_id == algorithm_id
        )]

    def regions(self, part_number):
        if not self.list(part_number):
            return ()
        return (MemoryRegion("external", 0x90000000, 0x800000, True, True, 0x1000),)

    def paths(self, part_number):
        return tuple(record.file_path for record in self.list(part_number))

    def fingerprint(self, part_number):
        return tuple(record.algorithm_id for record in self.list(part_number))


class PackManager:
    def __init__(self):
        self.cancelled = False
        self.removed = None
        self.imported_path = None

    def install(self, part_number, on_event):
        on_event({"type": "progress", "progress": 0.5})
        if part_number == "missing":
            raise FlashError(FlashErrorCode.PACK_NOT_FOUND, "missing")
        if part_number == "path-leak":
            raise FlashError(
                FlashErrorCode.PACK_DOWNLOAD_FAIL,
                r"failed C:\Users\alice\cache\Vendor.Pack and /home/alice/cache/pack",
                {
                    "nested": {
                        "path": Path("C:/Users/alice/cache/Vendor.Pack"),
                        "path_keys": {
                            Path("C:/Users/alice/cache/one.pack"): "path-object",
                            r"C:\Users\alice\cache\two.pack": "windows-string",
                            "/home/alice/cache/three.pack": "posix-string",
                        },
                        "collision_keys": {
                            "[redacted-key-1]": "literal-key",
                            Path("C:/Users/alice/cache/four.pack"): "path-key",
                        },
                    }
                },
            )
        return {"status": "installed", "part_number": part_number}

    def import_pack(self, path, on_event):
        self.imported_path = Path(path)
        assert self.imported_path.exists()
        on_event({"type": "log", "message": "ok"})
        return {"status": "installed", "pack_id": "V.P", "version": "1"}

    def cancel(self):
        self.cancelled = True

    def remove(self, vendor, pack, version, in_use=None):
        if in_use is not None and in_use("{}.{}".format(vendor, pack), version):
            raise FlashError(FlashErrorCode.PROBE_BUSY, "in use")
        self.removed = (vendor, pack, version)


class Inspector:
    def __init__(self):
        self.inspection = ImageInspection(
            "image-1", "fw.bin", "C:/secret/snapshot.bin", "bin", 4, "abc", 0x1000, 0x1004
        )
        self.seen_path = None
        self.seen_base = None
        self.preview_length = None
        self.seen_regions = ()

    def inspect(self, path, regions, base_address=None):
        self.seen_path = Path(path)
        self.seen_base = base_address
        assert self.seen_path.exists()
        self.seen_regions = tuple(regions)
        assert self.seen_regions[0].start == 0x1000
        assert base_address == 0x1000
        return self.inspection

    def validate_unchanged(self, image_id):
        if image_id != "image-1":
            raise KeyError(image_id)
        return self.inspection

    def covered_sectors(self, image_id, regions):
        assert image_id == "image-1"
        assert tuple(regions)[0].sector_size == 0x100
        return SectorCoverage((SectorRecord(0x1000, 0x100),), True)

    def preview(self, image_id, address, length):
        if image_id != "image-1":
            raise KeyError(image_id)
        self.preview_length = length
        return Preview(address, b"\x01\xff", (True, False))


@dataclass
class Preview:
    address: int
    data: bytes
    present: tuple


class Jobs:
    def __init__(self):
        self.started = []
        self.busy = False
        self.snapshot = JobSnapshot(
            "job-1", JobState.CONNECTING, ("connect", "disconnect"), None, 1.0, 2.0
        )

    def start(self, request):
        if self.busy:
            raise FlashError(FlashErrorCode.PROBE_BUSY, "busy")
        self.busy = True
        self.started.append(request)
        return "job-1"

    def read_memory(self, request, address, size):
        self.read_request = request
        self.read_range = (address, size)
        return bytes((address + index) & 0xFF for index in range(size))

    def iter_memory(self, request, address, chunk_sizes):
        self.read_request = request
        self.read_range = (address, sum(chunk_sizes))
        self.read_chunks = tuple(chunk_sizes)
        offset = 0
        for size in chunk_sizes:
            yield bytes((address + offset + index) & 0xFF for index in range(size))
            offset += size

    def get(self, job_id):
        if job_id != "job-1":
            raise KeyError(job_id)
        return self.snapshot

    def list(self):
        return [self.snapshot] if self.busy else []

    def stop(self, job_id):
        return self.get(job_id)

    def wait_for_events(self, job_id, after=0, timeout=None):
        self.get(job_id)
        if after < 2:
            self.snapshot = JobSnapshot(
                "job-1", JobState.SUCCEEDED, ("connect", "disconnect"), None, 1.0, 2.0
            )
            return [JobEvent(job_id, 2, 1.0, "state", state=JobState.SUCCEEDED)]
        return []


@pytest.fixture
def services(tmp_path):
    return OnlineFlashServices(
        catalog=Catalog(),
        pack_manager=PackManager(),
        image_inspector=Inspector(),
        job_manager=Jobs(),
        probe_provider=lambda: [
            type("Probe", (), {"unique_id": "mk", "product_name": "MKLink DAP"})(),
            type("Probe", (), {"unique_id": "other", "product_name": "CMSIS-DAP"})(),
        ],
        target_memory_provider=lambda part: [MemoryRegion("flash", 0x1000, 0x1000, True, True, 0x100)],
        paths=PackPaths(tmp_path),
        custom_flms=CustomFlms(tmp_path),
        pack_index_updater=lambda on_event: ({"status": "updated"}),
        heartbeat_interval=0.01,
    )


@pytest.fixture
def app(services):
    result = FastAPI()
    result.include_router(create_online_flash_router(services))
    return result


def request(app, method, path, **kwargs):
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.request(method, path, **kwargs)


def test_probe_target_and_pack_status_routes_use_injected_services(app, services):
    probes = request(app, "GET", "/api/online-flash/probes")
    assert [item["unique_id"] for item in probes.json()] == ["mk"]
    targets = request(app, "GET", "/api/online-flash/targets?q=device&vendor=Vendor&installed=true&limit=7")
    assert targets.json()[0]["part_number"] == "DEVICE_A"
    assert "pack_path" not in targets.json()[0]
    assert services.catalog.calls[-1] == ("device", "Vendor", True, 7)
    status = request(app, "GET", "/api/online-flash/packs/status")
    assert status.json()["index_available"] is True


@pytest.mark.parametrize("query", ["acme", "control family", "value series"])
def test_target_search_route_matches_vendor_family_and_series(
    app, services, tmp_path, query,
):
    paths = PackPaths(tmp_path / "catalog")
    paths.index_dir.mkdir(parents=True)
    paths.index_file.write_text(json.dumps({
        "PART-A": {
            "vendor": "Acme Semiconductor",
            "family": "Control Family",
            "sub_family": "Value Series",
            "from_pack": {
                "vendor": "Acme Semiconductor",
                "pack": "Part_DFP",
                "version": "1.0.0",
            },
        },
    }), encoding="utf-8")
    services.catalog = PackCatalog(paths, builtin_provider=lambda: [])

    response = request(app, "GET", "/api/online-flash/targets", params={"q": query})

    assert response.status_code == 200
    assert response.json() == [{
        "part_number": "PART-A",
        "vendor": "Acme Semiconductor",
        "pack_id": "Acme Semiconductor.Part_DFP",
        "pack_version": "1.0.0",
        "installed": False,
        "source": "index",
        "family": "Control Family",
        "series": "Value Series",
    }]


def test_target_search_route_keeps_limit_validation(app):
    response = request(app, "GET", "/api/online-flash/targets?limit=1001")

    assert response.status_code == 422


def test_target_memory_map_route_returns_flash_sector_geometry(app):
    response = request(app, "GET", "/api/online-flash/targets/DEVICE_A/memory-map")

    assert response.status_code == 200
    assert response.json() == [{
        "name": "flash",
        "start": 0x1000,
        "length": 0x1000,
        "sector_size": 0x100,
    }]


def test_target_memory_map_route_omits_hpm_without_read_geometry(app, services):
    services.catalog.search = lambda *args, **kwargs: []

    response = request(app, "GET", "/api/online-flash/targets/HPM5300/memory-map")

    assert response.status_code == 200
    assert response.json() == []


def test_read_memory_route_returns_bin_for_non_hpm_target(app, services):
    response = request(
        app,
        "POST",
        "/api/online-flash/memory/read",
        json={
            "address": "0x1000",
            "size": 4,
            "probe_id": "mk",
            "target_part": "DEVICE_A",
        },
    )
    assert response.status_code == 200
    assert response.content == bytes([0x00, 0x01, 0x02, 0x03])
    assert response.headers["content-type"] == "application/octet-stream"
    assert "read-0x00001000-4.bin" in response.headers["content-disposition"]
    assert services.job_manager.read_range == (0x1000, 4)


def test_read_memory_stream_keeps_sector_chunks_in_one_response(app, services):
    response = request(
        app,
        "POST",
        "/api/online-flash/memory/read-stream",
        json={
            "address": "0x1000",
            "size": 8,
            "chunk_sizes": [4, 4],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
        },
    )

    assert response.status_code == 200
    assert response.content == bytes(range(8))
    assert response.headers["content-length"] == "8"
    assert services.job_manager.read_range == (0x1000, 8)
    assert services.job_manager.read_chunks == (4, 4)


def test_read_memory_stream_rejects_inconsistent_chunk_plan(app):
    response = request(
        app,
        "POST",
        "/api/online-flash/memory/read-stream",
        json={
            "address": "0x1000",
            "size": 8,
            "chunk_sizes": [4, 2],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
        },
    )

    assert response.status_code == 422
    assert "add up to size" in response.json()["detail"]


def test_read_memory_route_supports_hpm_target(app, services):
    services.catalog.search = lambda *args, **kwargs: []
    response = request(
        app,
        "POST",
        "/api/online-flash/memory/read",
        json={
            "address": "0x80000000",
            "size": 4,
            "probe_id": "mk",
            "target_part": "HPM5300",
        },
    )
    assert response.status_code == 200
    assert response.content == bytes([0x00, 0x01, 0x02, 0x03])
    assert services.job_manager.read_range == (0x80000000, 4)


def test_hpm_image_and_job_use_rom_api_without_pack_or_sector_geometry(app, services):
    services.catalog.search = lambda *args, **kwargs: []
    services.target_memory_provider = lambda _part: pytest.fail("HPM must not load Pack memory")
    hpm_inspection = ImageInspection(
        "hpm-image", "firmware.bin", "snapshot.bin", "bin", 8, "abc",
        0x80000400, 0x80000408, base_address=0x80000400,
    )

    def inspect_hpm(path, regions, base_address=None):
        assert Path(path).is_file()
        assert tuple(regions) == (
            MemoryRegion("hpm-xpi", 0x80000000, 0x10000000, True, True, None),
        )
        assert base_address == 0x80000400
        return hpm_inspection

    services.image_inspector.inspect = inspect_hpm
    services.image_inspector.validate_unchanged = lambda image_id: (
        hpm_inspection if image_id == hpm_inspection.image_id else pytest.fail("wrong image")
    )

    inspected = request(
        app,
        "POST",
        "/api/online-flash/images/inspect",
        data={"part_number": "HPM5300", "base_address": "0x80000400"},
        files={"file": ("firmware.bin", b"firmware")},
    )

    assert inspected.status_code == 200
    assert inspected.json()["sector_operations_available"] is False
    assert inspected.json()["sectors"] == []

    started = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "erase", "program", "verify", "reset", "disconnect"],
            "probe_id": "mk",
            "target_part": "HPM5300",
            "image_id": inspected.json()["image_id"],
            "base_address": 0x80000400,
            "board": "hpm5300evk",
        },
    )

    assert started.status_code == 200
    request_record = services.job_manager.started[-1]
    assert request_record.pack_path is None
    assert request_record.custom_flm_paths == ()
    assert request_record.sector_addresses == ()
    assert request_record.board == "hpm5300evk"


def test_hpm_image_rejects_hex_without_pack_lookup(app, services):
    services.catalog.search = lambda *args, **kwargs: pytest.fail("HPM must not look up Packs")

    response = request(
        app,
        "POST",
        "/api/online-flash/images/inspect",
        data={"part_number": "HPM5300", "base_address": "0x80000400"},
        files={"file": ("firmware.hex", b":00000001FF\n")},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "FILE_FORMAT_ERROR"


def test_hpm_algorithm_api_never_accepts_flm(app, services):
    listed = request(app, "GET", "/api/online-flash/algorithms?part_number=HPM5300")
    available = request(app, "GET", "/api/online-flash/targets/HPM5300/algorithms")
    added = request(
        app,
        "POST",
        "/api/online-flash/algorithms",
        data={"part_number": "HPM5300"},
        files={"file": ("not-used.flm", b"algorithm")},
    )

    assert listed.json() == []
    assert available.json() == [{
        "algorithm_id": "hpm-rom-api",
        "target_part": "HPM5300",
        "file_name": "HPM ROM API",
        "flash_start": 0x80000000,
        "flash_size": 0x10000000,
        "default": True,
        "source_kind": "hpm-rom-api",
        "source_name": "HPM ROM API",
    }]
    assert added.status_code == 422
    assert added.json()["detail"]["code"] == "TARGET_NOT_SUPPORTED"
    assert services.custom_flms.records == []


def test_target_algorithm_route_lists_pack_source_without_paths(app, services, monkeypatch):
    monkeypatch.setattr(
        "mklink.cmsis_dap.algorithm_catalog.discover_flash_algorithms",
        lambda part_number, paths: [FlashAlgorithm(
            algorithm_id="pack-algorithm",
            target_part=part_number,
            file_name="Internal.FLM",
            flash_start=0x08000000,
            flash_size=0x80000,
            ram_start=0x20000000,
            ram_size=0x4000,
            default=True,
            source_kind="installed-pack",
            source_name="Vendor.Pack@1.0",
            source_token="secret-token",
            pack_path="C:/secret/Vendor.Pack.1.0.pack",
        )],
    )

    response = request(app, "GET", "/api/online-flash/targets/DEVICE_A/algorithms")

    assert response.status_code == 200
    assert response.json() == [{
        "algorithm_id": "pack-algorithm",
        "target_part": "DEVICE_A",
        "file_name": "Internal.FLM",
        "flash_start": 0x08000000,
        "flash_size": 0x80000,
        "default": True,
        "source_kind": "installed-pack",
        "source_name": "Vendor.Pack@1.0",
    }]
    assert "secret" not in response.text.casefold()


def test_target_algorithm_route_describes_pyocd_builtin_regions(app, services, monkeypatch):
    builtin = TargetRecord(
        "DEVICE_A", "Vendor", installed=True, source="builtin",
    )
    monkeypatch.setattr(
        services.catalog,
        "search",
        lambda query, **_kwargs: [builtin]
        if query.casefold() in builtin.part_number.casefold() else [],
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.algorithm_catalog.discover_flash_algorithms",
        lambda *_args, **_kwargs: [],
    )
    services.target_memory_provider = lambda _part: (
        MemoryRegion("flash", 0x08000000, 0x80000, True, True, 0x800),
    )

    response = request(app, "GET", "/api/online-flash/targets/DEVICE_A/algorithms")

    assert response.status_code == 200
    assert response.json() == [{
        "algorithm_id": "pyocd-builtin:device_a:08000000",
        "target_part": "DEVICE_A",
        "file_name": "DEVICE_A · flash",
        "flash_start": 0x08000000,
        "flash_size": 0x80000,
        "default": True,
        "source_kind": "pyocd-builtin",
        "source_name": "pyOCD",
    }]


def test_custom_flm_routes_store_list_and_remove_without_exposing_paths(app, services):
    added = request(
        app,
        "POST",
        "/api/online-flash/algorithms",
        data={"part_number": "DEVICE_A"},
        files={"file": ("external.flm", b"algorithm")},
    )

    assert added.status_code == 200
    assert added.json() == {
        "algorithm_id": "algo-1",
        "target_part": "DEVICE_A",
        "file_name": "external.flm",
        "flash_start": 0x90000000,
        "flash_size": 0x800000,
        "page_size": 0x1000,
        "sector_sizes": [[0, 0x1000]],
    }
    assert "file_path" not in added.text
    listed = request(app, "GET", "/api/online-flash/algorithms?part_number=DEVICE_A")
    assert listed.json() == [added.json()]
    removed = request(
        app,
        "DELETE",
        "/api/online-flash/algorithms/algo-1?part_number=DEVICE_A",
    )
    assert removed.json() == {"status": "removed"}
    assert services.custom_flms.records == []


def test_custom_flm_overrides_installed_pack_regions(tmp_path):
    base = MemoryRegion("bundle-external", 0x90000000, 0x800000, True, True, 0x1000)
    custom = MemoryRegion("custom-external", 0x90000000, 0x800000, True, True, 0x1000)

    class ExactCatalog:
        def search(self, query, installed=None, limit=100, **_kwargs):
            return [TargetRecord(
                part_number=query,
                vendor="Vendor",
                pack_id="Vendor.Bundle",
                pack_version="1",
                pack_path="bundle.pack",
                installed=True,
                source="index",
            )]

    class Algorithms:
        def regions(self, _part_number): return (custom,)
        def fingerprint(self, _part_number): return ("digest",)
        def paths(self, _part_number): return (str(tmp_path / "custom.flm"),)

    services = type("Services", (), {
        "catalog": ExactCatalog(),
        "target_memory_provider": staticmethod(lambda _part_number: (base,)),
        "custom_flms": Algorithms(),
    })()

    regions, fingerprint, _paths = _target_flash_configuration(services, "DEVICE_A")

    assert regions == (custom,)
    assert fingerprint == ("digest",)


def test_inspection_binds_custom_flms_and_job_receives_stored_paths(app, services):
    added = request(
        app,
        "POST",
        "/api/online-flash/algorithms",
        data={"part_number": "DEVICE_A"},
        files={"file": ("external.flm", b"algorithm")},
    )
    assert added.status_code == 200
    inspection = inspect_device_image(app)
    assert [region.start for region in services.image_inspector.seen_regions] == [
        0x1000,
        0x90000000,
    ]

    started = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "verify", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
            "image_id": inspection["image_id"],
            "frequency": 10_000_000,
        },
    )

    assert started.status_code == 200
    request_record = services.job_manager.started[-1]
    assert request_record.frequency == 10_000_000
    assert request_record.custom_flm_paths == (
        services.custom_flms.records[0].file_path,
    )
    assert request_record.custom_flm_digests == ("algo-1",)


def test_pyocd_builtin_keeps_internal_algorithm_and_injects_external_custom_flm(
    app, services, tmp_path, monkeypatch,
):
    services.catalog.search = lambda query, **_kwargs: [TargetRecord(
        query,
        "Vendor",
        installed=True,
        source="builtin",
    )]
    custom_path = tmp_path / "external.flm"
    custom_path.write_bytes(b"external")
    services.custom_flms.records.append(type("Custom", (), {
        "algorithm_id": "a0f72b7c2eea0f85a43874d5dba038ee9f81ff7d8e6727765b1491af0f41dc67",
        "target_part": "DEVICE_A",
        "file_name": "external.flm",
        "file_path": str(custom_path),
        "flash_start": 0x90000000,
        "flash_size": 0x800000,
        "ram_start": 0x20001000,
        "ram_size": 0x10000,
        "page_size": 0x1000,
        "sector_sizes": ((0, 0x1000),),
    })())
    services.image_inspector.inspection = ImageInspection(
        "image-1",
        "fw.hex",
        "snapshot.hex",
        "hex",
        8,
        "abc",
        0x08000000,
        0x90000004,
        segments=(
            ImageSegment(0x08000000, 0x08000004),
            ImageSegment(0x90000000, 0x90000004),
        ),
    )
    services.image_targets["image-1"] = (
        "device_a",
        services.custom_flms.fingerprint("DEVICE_A"),
    )
    external = FlashAlgorithm(
        algorithm_id=services.custom_flms.records[0].algorithm_id,
        target_part="DEVICE_A",
        file_name="external.flm",
        flash_start=0x90000000,
        flash_size=0x800000,
        ram_start=0x20001000,
        ram_size=0x10000,
        default=False,
        source_kind="custom-flm",
        source_name="user",
        source_token="external",
        custom_path=str(custom_path),
        custom_sha256=services.custom_flms.records[0].algorithm_id,
    )
    bundled_path = tmp_path / "bundled-external.flm"
    bundled_path.write_bytes(b"bundled-external")
    bundled = FlashAlgorithm(
        algorithm_id="bundled-external",
        target_part="DEVICE_A",
        file_name="bundled-external.flm",
        flash_start=0x90000000,
        flash_size=0x800000,
        ram_start=0x20001000,
        ram_size=0x10000,
        default=True,
        source_kind="builtin-pack",
        source_name="Vendor.Bundle@1.0.0",
        source_token="bundled-external",
        builtin_blob_path=str(bundled_path),
        builtin_blob_sha256="b" * 64,
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.algorithm_catalog.discover_flash_algorithms",
        lambda *_args, **_kwargs: [bundled, external],
    )

    started = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "verify", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
            "image_id": "image-1",
        },
    )

    assert started.status_code == 200
    request_record = services.job_manager.started[-1]
    assert request_record.custom_flm_paths == (str(custom_path),)
    assert request_record.custom_flm_regions == ((0x90000000, 0x800000),)
    assert request_record.custom_flm_ram_start is None
    assert request_record.custom_flm_ram_size is None


def _algorithm(tmp_path, name, start, size, *, default=False):
    path = tmp_path / name
    path.write_bytes(name.encode("ascii"))
    digest = "{:064x}".format(start)
    return FlashAlgorithm(
        algorithm_id=name,
        target_part="DEVICE_A",
        file_name=name,
        flash_start=start,
        flash_size=size,
        ram_start=0x20000000,
        ram_size=0x20000,
        default=default,
        source_kind="daplink-builtin",
        source_name="DAPLinkUtility",
        source_token=name,
        builtin_blob_path=str(path),
        builtin_blob_sha256=digest,
    )


def _use_daplink_target(services, algorithms, monkeypatch):
    services.catalog.search = lambda query, **_kwargs: [TargetRecord(
        query,
        "Vendor",
        installed=True,
        source="daplink-builtin",
    )]
    monkeypatch.setattr(
        "mklink.cmsis_dap.algorithm_catalog.discover_flash_algorithms",
        lambda *_args, **_kwargs: list(algorithms),
    )


def test_daplink_connect_only_loads_one_deterministic_default_algorithm(
    app, services, tmp_path, monkeypatch,
):
    internal = _algorithm(tmp_path, "internal.flm", 0x08000000, 0x200000, default=True)
    external = _algorithm(tmp_path, "external.flm", 0x90000000, 0x800000)
    _use_daplink_target(services, (external, internal), monkeypatch)

    started = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
        },
    )

    assert started.status_code == 200
    request_record = services.job_manager.started[-1]
    assert request_record.custom_flm_paths == (internal.builtin_blob_path,)
    assert request_record.custom_flm_regions == ((0x08000000, 0x200000),)


def test_daplink_sector_erase_loads_only_covering_algorithm(
    app, services, tmp_path, monkeypatch,
):
    internal = _algorithm(tmp_path, "internal.flm", 0x08000000, 0x200000, default=True)
    external = _algorithm(tmp_path, "external.flm", 0x90000000, 0x800000)
    _use_daplink_target(services, (internal, external), monkeypatch)

    started = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "erase", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
            "sector_addresses": [0x90001000],
        },
    )

    assert started.status_code == 200
    request_record = services.job_manager.started[-1]
    assert request_record.custom_flm_paths == (external.builtin_blob_path,)
    assert request_record.custom_flm_regions == ((0x90000000, 0x800000),)


def test_daplink_chip_erase_rejects_multiple_algorithms(
    app, services, tmp_path, monkeypatch,
):
    algorithms = (
        _algorithm(tmp_path, "internal.flm", 0x08000000, 0x200000, default=True),
        _algorithm(tmp_path, "external.flm", 0x90000000, 0x800000),
    )
    _use_daplink_target(services, algorithms, monkeypatch)

    response = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "erase", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TARGET_NOT_SUPPORTED"


def test_daplink_chip_erase_loads_its_only_algorithm(
    app, services, tmp_path, monkeypatch,
):
    algorithm = _algorithm(
        tmp_path,
        "internal.flm",
        0x08000000,
        0x200000,
        default=True,
    )
    _use_daplink_target(services, (algorithm,), monkeypatch)

    started = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "erase", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
        },
    )

    assert started.status_code == 200
    assert services.job_manager.started[-1].custom_flm_paths == (
        algorithm.builtin_blob_path,
    )


def test_algorithm_change_invalidates_an_existing_image_inspection(app, services):
    added = request(
        app,
        "POST",
        "/api/online-flash/algorithms",
        data={"part_number": "DEVICE_A"},
        files={"file": ("external.flm", b"algorithm")},
    ).json()
    inspection = inspect_device_image(app)
    request(
        app,
        "DELETE",
        "/api/online-flash/algorithms/{}?part_number=DEVICE_A".format(
            added["algorithm_id"]
        ),
    )

    response = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "verify", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
            "image_id": inspection["image_id"],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TARGET_NOT_SUPPORTED"


def test_custom_flm_mutation_is_blocked_while_an_online_job_is_active(app, services):
    services.job_manager.busy = True

    added = request(
        app,
        "POST",
        "/api/online-flash/algorithms",
        data={"part_number": "DEVICE_A"},
        files={"file": ("external.flm", b"algorithm")},
    )

    assert added.status_code == 409
    assert added.json()["detail"]["code"] == "PROBE_BUSY"


def test_job_start_and_custom_flm_mutation_share_configuration_lock(app, services):
    class TrackingLock:
        def __init__(self):
            self._lock = threading.RLock()
            self.depth = 0

        def __enter__(self):
            self._lock.acquire()
            self.depth += 1
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.depth -= 1
            self._lock.release()

    lock = TrackingLock()

    class LockedCustomFlms(CustomFlms):
        def add(self, *args, **kwargs):
            assert lock.depth > 0
            return super().add(*args, **kwargs)

        def remove(self, *args, **kwargs):
            assert lock.depth > 0
            return super().remove(*args, **kwargs)

    class LockedJobs(Jobs):
        def start(self, request):
            assert lock.depth > 0
            return super().start(request)

    services.configuration_lock = lock
    services.custom_flms = LockedCustomFlms(services.paths.root)
    services.job_manager = LockedJobs()

    added = request(
        app,
        "POST",
        "/api/online-flash/algorithms",
        data={"part_number": "DEVICE_A"},
        files={"file": ("external.flm", b"algorithm")},
    )
    assert added.status_code == 200
    removed = request(
        app,
        "DELETE",
        "/api/online-flash/algorithms/algo-1?part_number=DEVICE_A",
    )
    assert removed.status_code == 200
    started = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
        },
    )
    assert started.status_code == 200


def test_job_frequency_is_limited_to_ten_megahertz(app):
    response = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "erase", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
            "frequency": 10_000_001,
        },
    )

    assert response.status_code == 422


def test_probe_enumeration_failure_is_actionable_and_does_not_expose_raw_details(app, services):
    services.probe_provider = lambda: (_ for _ in ()).throw(
        RuntimeError(r"backend failed at C:\private\probe")
    )

    response = request(app, "GET", "/api/online-flash/probes")

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "code": "CONNECT_FAIL",
        "title": "连接失败",
        "message": "CMSIS-DAP 枚举失败，请检查 MicroKeen 设备的 WinUSB 驱动后重试",
    }


def test_pack_operations_collect_events_cancel_remove_and_map_errors(app, services):
    refresh_count = services.catalog.refresh_count
    installed = request(app, "POST", "/api/online-flash/packs/install", json={"part_number": "Other"})
    assert installed.json()["events"][0]["progress"] == 0.5
    assert services.catalog.refresh_count == refresh_count + 1
    missing = request(app, "POST", "/api/online-flash/packs/install", json={"part_number": "missing"})
    assert missing.status_code == 404
    updated = request(app, "POST", "/api/online-flash/packs/index/update")
    assert updated.json()["result"] == {"status": "updated"}
    cancelled = request(app, "POST", "/api/online-flash/packs/cancel")
    assert cancelled.status_code == 200 and services.pack_manager.cancelled
    removed = request(app, "DELETE", "/api/online-flash/packs/V.P/1")
    assert removed.status_code == 200
    assert services.pack_manager.removed == ("V", "P", "1")


def test_hpm_pack_install_is_satisfied_without_network_download(app, services):
    services.pack_manager.install = lambda *_args: (_ for _ in ()).throw(
        AssertionError("HPM must not download a Pack")
    )

    response = request(
        app,
        "POST",
        "/api/online-flash/packs/install",
        json={"part_number": "HPM5301xEGx"},
    )

    assert response.status_code == 200
    assert response.json()["result"] == {
        "status": "installed",
        "part_number": "HPM5301xEGx",
    }


def test_pack_install_can_stream_progress_and_terminal_result(app, services):
    refresh_count = services.catalog.refresh_count
    response = request(
        app,
        "POST",
        "/api/online-flash/packs/install",
        json={"part_number": "Other"},
        headers={"Accept": "application/x-ndjson"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    messages = [json.loads(line) for line in response.text.splitlines() if line]
    assert messages[0] == {
        "type": "event",
        "event": {"type": "progress", "phase": "preparing", "progress": 0.01},
    }
    assert any(
        message.get("type") == "event"
        and message.get("event", {}).get("phase") == "downloading"
        and message["event"]["progress"] > 0.01
        for message in messages
    )
    assert messages[-1] == {
        "type": "result",
        "result": {"status": "installed", "part_number": "Other"},
    }
    assert services.catalog.refresh_count == refresh_count + 1


def test_pack_import_can_stream_result_after_catalog_refresh(app, services):
    refresh_count = services.catalog.refresh_count
    response = request(
        app,
        "POST",
        "/api/online-flash/packs/import",
        files={"file": ("a.pack", b"pack")},
        headers={"Accept": "application/x-ndjson"},
    )

    messages = [json.loads(line) for line in response.text.splitlines() if line]
    assert response.status_code == 200
    assert any(message.get("event", {}).get("phase") == "refreshing" for message in messages)
    assert messages[-1] == {
        "type": "result",
        "result": {"status": "installed", "pack_id": "V.P", "version": "1"},
    }
    assert services.catalog.refresh_count == refresh_count + 1
    assert not services.pack_manager.imported_path.exists()


def test_pack_stream_bounds_bursty_progress_without_losing_result(app, services):
    def noisy_install(part_number, on_event):
        for index in range(1000):
            on_event({"type": "progress", "current": index + 1, "total": 1000})
        return {"status": "installed", "part_number": part_number}

    services.pack_manager.install = noisy_install
    response = request(
        app,
        "POST",
        "/api/online-flash/packs/install",
        json={"part_number": "Other"},
        headers={"Accept": "application/x-ndjson"},
    )
    messages = [json.loads(line) for line in response.text.splitlines() if line]

    assert response.status_code == 200
    assert messages[-1] == {
        "type": "result",
        "result": {"status": "installed", "part_number": "Other"},
    }


def test_pack_progress_queue_replaces_old_events_at_capacity():
    queue = asyncio.Queue(maxsize=2)
    _put_latest_pack_event(queue, {"sequence": 1})
    _put_latest_pack_event(queue, {"sequence": 2})
    _put_latest_pack_event(queue, {"sequence": 3})

    assert queue.qsize() == 2
    assert queue.get_nowait() == {"sequence": 2}
    assert queue.get_nowait() == {"sequence": 3}

    queue.put_nowait({"type": "event", "sequence": 4})
    queue.put_nowait({"type": "result"})
    _put_latest_pack_event(queue, {"type": "event", "sequence": 5})
    assert queue.get_nowait() == {"type": "event", "sequence": 4}
    assert queue.get_nowait() == {"type": "result"}


def test_flash_error_redacts_windows_posix_and_nested_path_values(app):
    response = request(
        app,
        "POST",
        "/api/online-flash/packs/install",
        json={"part_number": "path-leak"},
    )

    payload = response.json()["detail"]
    encoded = json.dumps(payload)
    assert response.status_code == 502
    assert payload["code"] == "PACK_DOWNLOAD_FAIL"
    assert "C:\\Users\\alice" not in payload["message"]
    assert "/home/alice" not in encoded
    assert payload["details"]["nested"]["path"] == "[redacted-path]"
    path_keys = payload["details"]["nested"]["path_keys"]
    assert list(path_keys) == [
        "[redacted-key-1]",
        "[redacted-key-2]",
        "[redacted-key-3]",
    ]
    assert list(path_keys.values()) == [
        "path-object",
        "windows-string",
        "posix-string",
    ]
    collision_keys = payload["details"]["nested"]["collision_keys"]
    assert collision_keys == {
        "[redacted-key-1]": "literal-key",
        "[redacted-key-1]#2": "path-key",
    }


def test_pack_status_redacts_paths_but_preserves_addresses_and_slash_text(app, services):
    services.catalog.status = lambda: {
        "index_available": True,
        "target_count": 1,
        "last_error": (
            r"read/write /api/online-flash at C:\Users\alice\index.json "
            "for 0x08000000, /tmp/mklink/index.json, and /workspace/project/index.json"
        ),
    }

    payload = request(app, "GET", "/api/online-flash/packs/status").json()

    assert "C:\\Users\\alice" not in payload["last_error"]
    assert "/tmp/mklink" not in payload["last_error"]
    assert "/workspace/project" not in payload["last_error"]
    assert "read/write" in payload["last_error"]
    assert "/api/online-flash" in payload["last_error"]
    assert "0x08000000" in payload["last_error"]


def test_path_redaction_handles_file_uri_and_path_prefix_without_redacting_routes(
    app, services
):
    services.catalog.status = lambda: {
        "index_available": True,
        "target_count": 1,
        "last_error": (
            "file:///home/alice/secret.bin; path:/home/alice/secret.bin; "
            "https://example.com/download/firmware.bin; /health; /oauth/callback"
        ),
    }

    message = request(app, "GET", "/api/online-flash/packs/status").json()[
        "last_error"
    ]

    assert "file:///home/alice" not in message
    assert "path:/home/alice" not in message
    assert "https://example.com/download/firmware.bin" in message
    assert "/health" in message
    assert "/oauth/callback" in message


@pytest.mark.parametrize(
    "local_path",
    [
        "/dev/ttyUSB0",
        "/proc/self/maps",
        "/sys/class/tty",
        "/bin/bash",
        "/sbin/init",
        "/boot/vmlinuz",
        "/data/private",
        "/workspace/project/Makefile",
        "/run/mklink/socket",
        "/lib/firmware/device",
        "/lib64/ld-linux",
        "/media/user/disk",
        "/snap/mklink/current",
        "/nix/store/package",
    ],
)
def test_path_redaction_covers_standard_posix_local_roots(
    app, services, local_path
):
    services.catalog.status = lambda: {
        "index_available": True,
        "target_count": 1,
        "last_error": (
            f"local={local_path}; https://example.com{local_path}; "
            "/health; /oauth/callback; /api/online-flash; /ws"
        ),
    }

    message = request(app, "GET", "/api/online-flash/packs/status").json()[
        "last_error"
    ]

    assert message.startswith("local=[redacted-path]; ")
    assert f"https://example.com{local_path}" in message
    assert "/health" in message
    assert "/oauth/callback" in message
    assert "/api/online-flash" in message
    assert "/ws" in message


def test_first_pack_index_failure_is_503_and_records_catalog_error(app, services):
    recorded = []
    services.catalog.status = lambda: {
        "index_available": False,
        "target_count": 0,
        "last_error": None,
    }
    services.catalog.note_refresh_failure = recorded.append

    def fail(_on_event):
        raise FlashError(FlashErrorCode.PACK_DOWNLOAD_FAIL, "offline")

    services.pack_index_updater = fail

    response = request(app, "POST", "/api/online-flash/packs/index/update")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "PACK_INDEX_UNAVAILABLE"
    assert len(recorded) == 1


def test_pack_index_failure_keeps_last_good_cache_and_returns_502(app, services):
    services.paths.index_dir.mkdir(parents=True)
    services.paths.index_file.write_text('{"DEVICE":{}}', encoding="utf-8")
    services.catalog = PackCatalog(services.paths, builtin_provider=lambda: [])

    def fail(_on_event):
        raise FlashError(FlashErrorCode.PACK_DOWNLOAD_FAIL, "offline")

    services.pack_index_updater = fail

    response = request(app, "POST", "/api/online-flash/packs/index/update")

    assert response.status_code == 502
    assert services.catalog.status().index_available is True
    assert services.paths.index_file.read_text(encoding="utf-8") == '{"DEVICE":{}}'


def test_successful_index_update_immediately_refreshes_pack_status(app, services):
    services.catalog = PackCatalog(services.paths, builtin_provider=lambda: [])

    def update(_on_event):
        services.paths.index_dir.mkdir(parents=True)
        services.paths.index_file.write_text('{"DEVICE":{}}', encoding="utf-8")
        services.paths.aliases_file.write_text("{}", encoding="utf-8")
        return {"status": "updated", "target_count": 1}

    services.pack_index_updater = update

    updated = request(app, "POST", "/api/online-flash/packs/index/update")
    status = request(app, "GET", "/api/online-flash/packs/status")

    assert updated.status_code == 200
    assert status.json()["index_available"] is True
    assert status.json()["target_count"] == 1


def test_import_and_inspect_stream_uploads_then_delete_temporary_files(app, services):
    refresh_count = services.catalog.refresh_count
    imported = request(
        app, "POST", "/api/online-flash/packs/import", files={"file": ("a.pack", b"pack")}
    )
    assert imported.status_code == 200
    assert services.catalog.refresh_count == refresh_count + 1
    assert not services.pack_manager.imported_path.exists()
    inspected = request(
        app,
        "POST",
        "/api/online-flash/images/inspect",
        data={"part_number": "DEVICE_A", "base_address": "0x1000"},
        files={"file": ("fw.bin", b"abcd")},
    )
    body = inspected.json()
    assert body["image_id"] == "image-1"
    assert body["sector_operations_available"] is True
    assert body["sectors"] == [{"address": 0x1000, "size": 0x100}]
    assert "file_path" not in body
    assert not services.image_inspector.seen_path.exists()


def test_captured_image_can_use_same_pack_flm_range_for_programming(
    app, services, monkeypatch
):
    algorithm = FlashAlgorithm(
        algorithm_id="pack-algorithm",
        target_part="DEVICE_A",
        file_name="device.flm",
        flash_start=0x1000,
        flash_size=0x2000,
        ram_start=0x20000000,
        ram_size=0x1000,
        default=True,
        source_kind="installed-pack",
        source_name="Vendor.Pack@1.0",
        source_token="catalog:installed:test",
        pack_path="safe.pack",
    )
    monkeypatch.setattr(
        "mklink.cmsis_dap.algorithm_catalog.discover_flash_algorithms",
        lambda *_args, **_kwargs: [algorithm],
    )

    inspected = request(
        app,
        "POST",
        "/api/online-flash/images/inspect",
        data={
            "part_number": "DEVICE_A",
            "base_address": "0x1000",
            "captured_from_target": "true",
        },
        files={"file": ("captured.bin", b"abcd")},
    )

    assert inspected.status_code == 200, inspected.text
    assert services.image_inspector.seen_regions[0].length == 0x2000
    assert services.image_flash_overrides["image-1"][1] == ((0x1000, 0x2000),)

    started = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "erase", "program", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
            "image_id": "image-1",
            "sector_addresses": [0x1000],
        },
    )

    assert started.status_code == 200, started.text
    assert services.job_manager.started[0].pack_flm_regions == ((0x1000, 0x2000),)


def test_local_firmware_path_status_and_inspection_track_recompiled_files(app, services):
    source = services.paths.root / "build" / "fw.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"abcd")

    first_status = request(
        app, "GET", "/api/online-flash/images/source-status", params={"path": str(source)}
    )
    inspected = request(
        app,
        "POST",
        "/api/online-flash/images/inspect-path",
        json={"path": str(source), "part_number": "DEVICE_A", "base_address": "0x1000"},
    )
    source.write_bytes(b"abcdefgh")
    second_status = request(
        app, "GET", "/api/online-flash/images/source-status", params={"path": str(source)}
    )

    assert first_status.status_code == 200
    assert first_status.json()["size"] == 4
    assert inspected.status_code == 200, inspected.text
    assert services.image_inspector.seen_path == source.resolve()
    assert source.is_file()
    assert second_status.json()["size"] == 8


def test_local_firmware_path_accepts_numeric_bin_base_from_desktop_clients(app, services):
    source = services.paths.root / "build" / "fw.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"abcd")

    inspected = request(
        app,
        "POST",
        "/api/online-flash/images/inspect-path",
        json={"path": str(source), "part_number": "DEVICE_A", "base_address": 0x1000},
    )

    assert inspected.status_code == 200, inspected.text
    assert services.image_inspector.seen_base == 0x1000


def test_local_firmware_path_rejects_missing_or_unsupported_sources(app, tmp_path):
    unsupported = tmp_path / "firmware.txt"
    unsupported.write_text("data", encoding="ascii")

    missing = request(
        app, "GET", "/api/online-flash/images/source-status", params={"path": str(tmp_path / "missing.bin")}
    )
    wrong_suffix = request(
        app, "GET", "/api/online-flash/images/source-status", params={"path": str(unsupported)}
    )

    assert missing.status_code == 422
    assert wrong_suffix.status_code == 422


def test_inspect_requires_exact_installed_target_and_enforces_upload_limit(app, services):
    absent = request(
        app,
        "POST",
        "/api/online-flash/images/inspect",
        data={"part_number": "Nope", "base_address": "4096"},
        files={"file": ("fw.bin", b"abcd")},
    )
    assert absent.status_code == 422
    services.upload_limit = 3
    too_large = request(
        app,
        "POST",
        "/api/online-flash/images/inspect",
        data={"part_number": "DEVICE_A", "base_address": "4096"},
        files={"file": ("fw.bin", b"abcd")},
    )
    assert too_large.status_code == 422
    assert not list((services.paths.root / "uploads").glob("*"))


def test_inspect_rejects_invalid_base_address_with_422(app):
    response = request(
        app,
        "POST",
        "/api/online-flash/images/inspect",
        data={"part_number": "DEVICE_A", "base_address": "not-an-address"},
        files={"file": ("fw.bin", b"abcd")},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_preview_uses_relative_offset_and_serializes_gaps(app):
    response = request(app, "GET", "/api/online-flash/images/image-1/preview?offset=0&length=2")
    assert response.json() == {
        "address": 4096,
        "length": 2,
        "data_base64": "Af8=",
        "present": [True, False],
    }
    missing = request(app, "GET", "/api/online-flash/images/missing/preview?offset=0&length=2")
    assert missing.status_code == 404


def test_preview_defaults_to_4096_bytes(app, services):
    response = request(app, "GET", "/api/online-flash/images/image-1/preview")

    assert response.status_code == 200
    assert services.image_inspector.preview_length == 4096


def inspect_device_image(app):
    response = request(
        app,
        "POST",
        "/api/online-flash/images/inspect",
        data={"part_number": "DEVICE_A", "base_address": "4096"},
        files={"file": ("fw.bin", b"abcd")},
    )
    assert response.status_code == 200
    return response.json()


def test_jobs_validate_dependencies_and_second_active_job_is_conflict(app):
    inspection = inspect_device_image(app)
    payload = {
        "actions": ["connect", "erase", "program", "disconnect"],
        "probe_id": "mk",
        "target_part": "DEVICE_A",
        "image_id": inspection["image_id"],
        "sector_addresses": [0x1000],
    }
    started = request(app, "POST", "/api/online-flash/jobs", json=payload)
    assert started.status_code == 200 and started.json()["job_id"] == "job-1"
    assert started.json()["job"]["file_path"] is None
    busy = request(app, "POST", "/api/online-flash/jobs", json=payload)
    assert busy.status_code == 409
    active = request(app, "GET", "/api/online-flash/jobs/active")
    assert active.json()["job_id"] == "job-1"
    missing = request(app, "GET", "/api/online-flash/jobs/missing")
    assert missing.status_code == 404


def test_program_job_requires_covered_sector_erase(app):
    inspection = inspect_device_image(app)

    response = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "program", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
            "image_id": inspection["image_id"],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMAGE_OUT_OF_RANGE"


def test_program_job_recomputes_and_requires_exact_covered_sectors(app):
    inspection = inspect_device_image(app)

    response = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "erase", "program", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
            "image_id": inspection["image_id"],
            "sector_addresses": [0x1100],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMAGE_OUT_OF_RANGE"


def test_program_job_rejects_geometry_that_is_no_longer_reliable(app, services):
    inspection = inspect_device_image(app)
    services.image_inspector.covered_sectors = lambda image_id, regions: SectorCoverage((), False)

    response = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "erase", "program", "disconnect"],
            "probe_id": "mk",
            "target_part": "DEVICE_A",
            "image_id": inspection["image_id"],
            "sector_addresses": [0x1000],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMAGE_OUT_OF_RANGE"


def test_job_rejects_image_inspected_for_a_different_target(app, services):
    inspection = inspect_device_image(app)
    services.catalog.search = lambda *args, **kwargs: [
        TargetRecord("Other", "Vendor", installed=True)
    ]

    response = request(
        app,
        "POST",
        "/api/online-flash/jobs",
        json={
            "actions": ["connect", "verify", "disconnect"],
            "probe_id": "mk",
            "target_part": "Other",
            "image_id": inspection["image_id"],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TARGET_NOT_SUPPORTED"


def test_active_job_returns_200_null_when_idle(app):
    response = request(app, "GET", "/api/online-flash/jobs/active")

    assert response.status_code == 200
    assert response.json() is None


def test_stop_route_forwards_job_id_and_returns_snapshot(app, services):
    stopped = []

    def stop(job_id):
        stopped.append(job_id)
        return services.job_manager.get(job_id)

    services.job_manager.stop = stop

    response = request(app, "POST", "/api/online-flash/jobs/job-1/stop")

    assert response.status_code == 200
    assert response.json()["job_id"] == "job-1"
    assert stopped == ["job-1"]


def test_sse_replays_after_cursor_and_closes_at_terminal_state(app):
    response = request(app, "GET", "/api/online-flash/jobs/job-1/events?after=1")
    assert response.status_code == 200
    assert "id: 2\nevent: state\n" in response.text
    assert '"state":"succeeded"' in response.text


def test_sse_emits_heartbeat_and_filters_duplicate_sequences(app, services):
    calls = []

    def wait_for_events(job_id, after=0, timeout=None):
        calls.append((job_id, after, timeout))
        if len(calls) == 1:
            return []
        services.job_manager.snapshot = JobSnapshot(
            "job-1", JobState.SUCCEEDED, ("connect",), None, 1.0, 2.0
        )
        return [JobEvent(job_id, 2, 1.0, "state", state=JobState.SUCCEEDED)]

    services.job_manager.wait_for_events = wait_for_events

    response = request(app, "GET", "/api/online-flash/jobs/job-1/events?after=2")

    assert response.status_code == 200
    assert response.text.count(": heartbeat\n\n") == 1
    assert "id: 2\n" not in response.text
    assert len(calls) == 2
    assert calls[0][2] == services.heartbeat_interval


def test_online_flash_services_default_heartbeat_is_15_seconds(tmp_path):
    defaults = OnlineFlashServices(
        catalog=object(),
        pack_manager=object(),
        image_inspector=object(),
        job_manager=object(),
        probe_provider=lambda: [],
        target_memory_provider=lambda _part: [],
        paths=PackPaths(tmp_path),
    )

    assert defaults.heartbeat_interval == 15.0


def test_sse_event_messages_redact_paths_without_changing_normal_text(app, services):
    def wait_for_events(job_id, after=0, timeout=None):
        services.job_manager.snapshot = JobSnapshot(
            "job-1", JobState.SUCCEEDED, ("connect",), None, 1.0, 2.0
        )
        return [
            JobEvent(
                job_id,
                3,
                1.0,
                "log",
                message=(
                    r"read/write C:\Users\alice\firmware.bin "
                    "and /home/alice/firmware.bin at 0x08000000"
                ),
            )
        ]

    services.job_manager.wait_for_events = wait_for_events

    response = request(app, "GET", "/api/online-flash/jobs/job-1/events?after=2")

    assert "C:\\Users\\alice" not in response.text
    assert "/home/alice" not in response.text
    assert "read/write" in response.text
    assert "0x08000000" in response.text


def test_create_app_mounts_services_once_and_shuts_them_down(monkeypatch, services):
    calls = []

    def shutdown(name):
        return lambda *_args, **_kwargs: calls.append(name)

    services.job_manager.shutdown = shutdown("jobs")
    services.pack_manager.shutdown = shutdown("packs")
    services.image_inspector.shutdown = shutdown("images")
    factory_calls = []

    def factory(resource_manager, prepare_connect=None):
        factory_calls.append((resource_manager, prepare_connect))
        return services

    monkeypatch.setattr(
        "mklink.remote.online_flash_api.create_default_online_flash_services",
        factory,
    )

    mounted = create_app(project_root=".")
    assert mounted.state.online_flash is services
    assert len(factory_calls) == 1
    assert factory_calls[0][0] is mounted.state.mklink_state["resource_manager"]
    assert callable(factory_calls[0][1])

    with TestClient(mounted) as client:
        assert client.get("/api/online-flash/packs/status").status_code == 200

    assert calls == ["jobs", "packs", "images"]


def test_create_app_hpm_online_flash_preparation_releases_shared_device(
    monkeypatch, services
):
    captured = {}

    def factory(resource_manager, prepare_connect=None):
        captured["resource_manager"] = resource_manager
        captured["prepare_connect"] = prepare_connect
        return services

    stopped = []
    monkeypatch.setattr(
        "mklink.remote.online_flash_api.create_default_online_flash_services",
        factory,
    )
    monkeypatch.setattr(
        "mklink.remote.dashboards.stop_bridge_dashboards",
        lambda resource_manager=None: stopped.append(resource_manager) or [],
    )
    mounted = create_app(project_root=".")
    state = mounted.state.mklink_state

    class Device:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    device = Device()
    state["device"] = device
    state["dispatcher"] = object()
    prepare = captured["prepare_connect"]

    prepare(JobRequest(actions=("connect", "disconnect"), target_part="STM32F103RC"))
    assert device.close_calls == 0
    assert state["device"] is device

    prepare(JobRequest(actions=("connect", "disconnect"), target_part="HPM5301xEGx"))
    assert stopped == [captured["resource_manager"]]
    assert device.close_calls == 1
    assert state["device"] is None
    assert state["dispatcher"] is None


def test_service_shutdown_is_bounded_for_blocked_backend_and_cleans_components(
    tmp_path,
):
    class Backend:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def connect(self, **_kwargs):
            self.started.set()
            self.release.wait(1)

        def disconnect(self):
            return None

    class Component:
        def __init__(self, name, calls):
            self.name = name
            self.calls = calls

        def shutdown(self):
            self.calls.append(self.name)

    backend = Backend()
    jobs = OnlineFlashJobManager(lambda: backend, ResourceManager())
    job_id = jobs.start(JobRequest(actions=("connect", "disconnect")))
    assert backend.started.wait(1)
    calls = []
    bounded = OnlineFlashServices(
        catalog=object(),
        pack_manager=Component("packs", calls),
        image_inspector=Component("images", calls),
        job_manager=jobs,
        probe_provider=lambda: [],
        target_memory_provider=lambda _part: [],
        paths=PackPaths(tmp_path),
        shutdown_timeout=0.05,
    )

    started = time.monotonic()
    try:
        shutdown_online_flash_services(bounded)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert jobs.get(job_id).state is JobState.STOPPING
        assert calls == ["packs", "images"]
    finally:
        backend.release.set()
        assert jobs.wait(job_id, timeout=2).state is JobState.STOPPED


def test_service_shutdown_cleans_later_components_when_job_shutdown_raises(
    services,
):
    calls = []

    def fail(*_args, **_kwargs):
        calls.append("jobs")
        raise RuntimeError("shutdown failed")

    services.job_manager.shutdown = fail
    services.pack_manager.shutdown = lambda: calls.append("packs")
    services.image_inspector.shutdown = lambda: calls.append("images")

    with pytest.raises(RuntimeError, match="shutdown failed"):
        shutdown_online_flash_services(services)

    assert calls == ["jobs", "packs", "images"]


def test_cached_pack_memory_provider_uses_exact_flash_algorithm(tmp_path):
    paths = PackPaths(tmp_path)
    paths.index_dir.mkdir(parents=True)
    paths.index_file.write_text(
        '{"DEVICE":{"algorithms":[{"start":"0x08000000",'
        '"size":"0x40000","sector_size":"0x800"}]}}',
        encoding="utf-8",
    )

    regions = default_target_memory_provider("device", paths)

    assert regions == [
        MemoryRegion("flash-0", 0x08000000, 0x40000, True, True, 0x800)
    ]


def test_builtin_registry_name_resolves_sector_geometry():
    regions = default_target_memory_provider("STM32F103RC")

    assert any(
        region.start == 0x08000000 and region.sector_size == 0x800
        for region in regions
    )


def test_installed_pack_memory_map_uses_pyocd_flm_geometry(tmp_path, monkeypatch):
    pack_path = tmp_path / "Vendor.Device.pack"
    pack_path.write_bytes(b"pack")

    class FlashRegion:
        name = "flash"
        start = 0x08000000
        length = 0x40000
        is_flash = True
        is_writable = True
        sector_size = 0
        blocksize = 0

        class flm:
            @staticmethod
            def iter_sector_size_ranges():
                first = type("Range", (), {
                    "start": 0x08000000,
                    "end": 0x0800FFFF,
                })()
                second = type("Range", (), {
                    "start": 0x08010000,
                    "end": 0x0803FFFF,
                })()
                yield first, 0x800
                yield second, 0x1000

    class Device:
        part_number = "DEVICE"
        memory_map = [FlashRegion()]

    class Pack:
        devices = [Device()]

    monkeypatch.setattr(
        "pyocd.target.pack.cmsis_pack.CmsisPack", lambda _path: Pack()
    )

    assert _pack_memory_regions("device", pack_path) == [
        MemoryRegion("flash", 0x08000000, 0x10000, True, True, 0x800),
        MemoryRegion("flash-1", 0x08010000, 0x30000, True, True, 0x1000),
    ]


def test_installed_pack_relocates_relative_flm_geometry(tmp_path, monkeypatch):
    pack_path = tmp_path / "Vendor.Device.pack"
    pack_path.write_bytes(b"pack")

    class FlashRegion:
        name = "IROM1"
        start = 0x00400000
        length = 0x00100000
        is_flash = True
        is_writable = True
        sector_size = 0
        blocksize = 0

        class flm:
            flash_start = 0
            flash_size = 0x00100000

            @staticmethod
            def iter_sector_size_ranges():
                sector_range = type("Range", (), {
                    "start": 0,
                    "end": 0x000FFFFF,
                })()
                yield sector_range, 0x1000

    class Device:
        part_number = "CST92F41KxVxxx"
        memory_map = [FlashRegion()]

    class Pack:
        devices = [Device()]

    monkeypatch.setattr(
        "pyocd.target.pack.cmsis_pack.CmsisPack", lambda _path: Pack()
    )

    assert _pack_memory_regions("CST92F41KxVxxx", pack_path) == [
        MemoryRegion("IROM1", 0x00400000, 0x00100000, True, True, 0x1000),
    ]


def test_memory_provider_prefers_exact_installed_pack_over_dynamic_registry(
    tmp_path, monkeypatch,
):
    import mklink.remote.online_flash_api as module
    import pyocd.target

    pack_path = tmp_path / "exact.pack"
    pack_path.write_bytes(b"pack")
    exact = [MemoryRegion("exact", 0x08000000, 0x20000, True, True, 0x2000)]
    polluted = MemoryRegion("family-external", 0x90000000, 0x8000000, True, True, 0x1000)

    class CatalogWithInstalledPack:
        def __init__(self, paths):
            pass

        def search(self, part_number, installed=None):
            return [
                TargetRecord(
                    part_number,
                    "Vendor",
                    pack_path=str(pack_path),
                    installed=True,
                )
            ]

    class DynamicPackTarget:
        PART_NUMBER = "STM32H7B0VBTx"
        MEMORY_MAP = (polluted,)

    monkeypatch.setattr(
        "mklink.cmsis_dap.pack_catalog.PackCatalog",
        CatalogWithInstalledPack,
    )
    monkeypatch.setattr(
        module,
        "_pack_memory_regions",
        lambda part_number, path: exact,
    )
    monkeypatch.setattr(pyocd.target, "TARGET", {"stm32h7b0vbtx": DynamicPackTarget})
    paths = type("Paths", (), {"index_file": tmp_path / "index.json"})()

    assert default_target_memory_provider("STM32H7B0VBTx", paths) == exact


def test_cached_pack_memory_provider_rejects_missing_memory_map(tmp_path):
    paths = PackPaths(tmp_path)
    paths.index_dir.mkdir(parents=True)
    paths.index_file.write_text('{"DEVICE":{"algorithms":[]}}', encoding="utf-8")

    with pytest.raises(FlashError) as captured:
        default_target_memory_provider("DEVICE", paths)

    assert captured.value.code is FlashErrorCode.TARGET_NOT_SUPPORTED


def test_request_cancellation_is_not_converted_to_http_500(monkeypatch):
    async def cancel(_function, *_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr("mklink.remote.online_flash_api.run_in_threadpool", cancel)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_blocking(lambda: None))
