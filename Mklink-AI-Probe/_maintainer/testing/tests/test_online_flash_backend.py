from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mklink.cmsis_dap.backend import HpmRomBackend, PyOcdBackend, RoutingFlashBackend
from mklink.cmsis_dap.backend import (
    _expand_pack_flm_regions,
    _install_custom_flm_regions,
    _relocate_pack_flm_regions,
)
from mklink.cmsis_dap.errors import FlashError, FlashErrorCode
from mklink.cmsis_dap.models import ImageInspection, ImageSegment, MemoryRegion


class FakeTarget:
    def __init__(self, memory_map=()) -> None:
        self.reset_and_halt_calls = 0
        self.memory_map = memory_map
        self.reset_calls = []

    def reset_and_halt(self) -> None:
        self.reset_and_halt_calls += 1

    def reset(self, reset_type=None) -> None:
        self.reset_calls.append(reset_type)


class FakeSession:
    def __init__(self, target: FakeTarget | None = None) -> None:
        self.target = target or FakeTarget()
        self.delegate = None
        self.open_calls = 0
        self.close_calls = 0

    def open(self) -> None:
        self.open_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class FakeProbe:
    def __init__(self, unique_id: str) -> None:
        self.unique_id = unique_id


class FakeRegion:
    def __init__(
        self,
        start: int,
        length: int,
        *,
        is_flash: bool = True,
        is_ram: bool = False,
        is_writable: bool = True,
        blocksize: int | None = 0x100,
        name: str = "region",
    ) -> None:
        self.start = start
        self.length = length
        self.end = start + length
        self.is_flash = is_flash
        self.is_ram = is_ram
        self.is_writable = is_writable
        self.blocksize = blocksize
        self.name = name
        self.flash = (
            UniformFlash(start, blocksize)
            if isinstance(blocksize, int) and blocksize > 0
            else None
        )


class FakeSectorInfo:
    def __init__(self, base_addr: int, size: int) -> None:
        self.base_addr = base_addr
        self.size = size


class UniformFlash:
    def __init__(self, start: int, size: int) -> None:
        self.start = start
        self.size = size

    def get_sector_info(self, address: int) -> FakeSectorInfo:
        base = self.start + ((address - self.start) // self.size) * self.size
        return FakeSectorInfo(base, self.size)


def assert_error(code: FlashErrorCode, call) -> FlashError:
    with pytest.raises(FlashError) as raised:
        call()
    assert raised.value.code is code
    return raised.value


def test_hpm_rom_backend_reads_via_dump_memory(monkeypatch) -> None:
    class Device:
        _bridge = object()

        def close(self):
            pass

    device = Device()
    backend = HpmRomBackend(device_factory=lambda **_kwargs: device)
    backend.connect(
        probe="probe",
        target="HPM5300",
        frequency=1_000_000,
        board="hpm5300evk",
    )
    calls = []

    def read_range(bridge, address, size, *, timeout):
        calls.append((bridge, address, size, timeout))
        return bytes((address + index) & 0xFF for index in range(size))

    monkeypatch.setattr("mklink.dump_memory.read_dump_memory_range_once", read_range)
    assert backend.read_memory(0x80000000, 0x8001) == bytes(
        (0x80000000 + index) & 0xFF for index in range(0x8001)
    )
    assert [item[1:3] for item in calls] == [
        (0x80000000, 0x1000),
        (0x80001000, 0x1000),
        (0x80002000, 0x1000),
        (0x80003000, 0x1000),
        (0x80004000, 0x1000),
        (0x80005000, 0x1000),
        (0x80006000, 0x1000),
        (0x80007000, 0x1000),
        (0x80008000, 1),
    ]
    assert backend.memory_regions()[0].start == 0x80000000
    backend.disconnect()


def test_pyocd_backend_reads_exact_bytes_from_target() -> None:
    class Target(FakeTarget):
        def __init__(self):
            super().__init__((FakeRegion(0x08000000, 0x100),))

        def read_memory_block8(self, address, size):
            return bytes((address + offset) & 0xFF for offset in range(size))

    session = FakeSession(Target())
    backend = PyOcdBackend(
        session_factory=lambda _probe, _options: session,
        probe_provider=lambda: [FakeProbe("probe")],
    )
    backend.connect(probe="probe", target="STM32F103C8", frequency=1_000_000)
    assert backend.read_memory(0x08000010, 4) == bytes([0x10, 0x11, 0x12, 0x13])
    backend.disconnect()


def test_hpm_rom_backend_programs_without_flm_and_verifies_by_readback(
    tmp_path: Path,
) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcdefgh")
    calls = []

    class Device:
        def flash(self, path, **kwargs):
            calls.append(("flash", Path(path), kwargs))
            return {"success": True, "algorithm_source": "hpm-rom-api"}

        def read_memory(self, address, size):
            calls.append(("read", address, size))
            return firmware.read_bytes()[address - 0x80000400 : address - 0x80000400 + size]

        def reset(self):
            calls.append(("reset",))

        def close(self):
            calls.append(("close",))

    device = Device()
    backend = HpmRomBackend(
        device_factory=lambda **kwargs: calls.append(("connect", kwargs)) or device,
        port_resolver=lambda probe: calls.append(("resolve", probe)) or "probe-port",
        verify_chunk_size=4,
    )
    backend.connect(
        probe="probe-id",
        target="HPM5300",
        frequency=10_000_000,
        board="hpm5300evk",
    )
    image = ImageInspection(
        "image", file_path=str(firmware), format="bin", base_address=0x80000400
    )

    backend.erase_chip()
    backend.erase_sectors([0x80000000])
    backend.program(image)
    backend.verify(image)
    backend.reset_run()
    backend.disconnect()

    assert calls[0:2] == [("resolve", "probe-id"), ("connect", {"port": "probe-port"})]
    assert calls[2] == (
        "flash",
        firmware,
        {
            "target_part": "HPM5300",
            "base_address": 0x80000400,
            "board": "hpm5300evk",
            "hpm_flash_cfg": None,
            "swd_clock": 10_000_000,
            "verify": False,
            "reset_after": False,
        },
    )
    assert calls[3:] == [
        ("read", 0x80000400, 4),
        ("read", 0x80000404, 4),
        ("reset",),
        ("close",),
    ]


def test_hpm_rom_backend_rejects_hex_and_reports_verify_mismatch(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"expected")

    class Device:
        def read_memory(self, address, size):
            return b"x" * size

        def close(self):
            pass

    backend = HpmRomBackend(
        device_factory=lambda **_kwargs: Device(),
        port_resolver=lambda _probe: "probe-port",
    )
    backend.connect("probe", "HPM5300", 1_000_000, board="hpm5300evk")

    mismatch = assert_error(
        FlashErrorCode.VERIFY_FAIL,
        lambda: backend.verify(ImageInspection(
            "bin", file_path=str(firmware), format="bin", base_address=0x80000000
        )),
    )
    assert mismatch.details["address"] == 0x80000000
    assert_error(
        FlashErrorCode.FILE_FORMAT_ERROR,
        lambda: backend.program(ImageInspection("hex", file_path=str(firmware), format="hex")),
    )


def test_hpm_rom_backend_verify_reports_progress(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcdefgh")
    reported = []

    class Device:
        def read_memory(self, address, size):
            return firmware.read_bytes()[address - 0x80000400 : address - 0x80000400 + size]

        def close(self):
            pass

    backend = HpmRomBackend(
        device_factory=lambda **_kwargs: Device(),
        port_resolver=lambda _probe: "probe-port",
        verify_chunk_size=4,
    )
    backend.connect("probe", "HPM5300", 1_000_000, board="hpm5300evk")
    backend.verify(
        ImageInspection(
            "image",
            file_path=str(firmware),
            format="bin",
            size=8,
            start=0x80000400,
            end=0x80000408,
            segments=(ImageSegment(0x80000400, 0x80000408),),
            base_address=0x80000400,
        ),
        progress_callback=reported.append,
    )

    assert reported == [0.0, 0.5, 1.0]
    backend.disconnect()


def test_routing_backend_selects_hpm_rom_only_for_hpm_targets() -> None:
    calls = []

    class Backend:
        def __init__(self, name):
            self.name = name

        def connect(self, **kwargs):
            calls.append((self.name, kwargs))

        def disconnect(self):
            calls.append((self.name, "disconnect"))

    router = RoutingFlashBackend(
        pyocd_factory=lambda: Backend("pyocd"),
        hpm_factory=lambda: Backend("hpm"),
    )
    router.connect("probe", "HPM5300", 1_000_000, board="hpm5300evk")
    router.disconnect()
    router.connect("probe", "STM32F103RC", 1_000_000, board="ignored")

    assert calls[0][0] == "hpm"
    assert calls[0][1]["board"] == "hpm5300evk"
    assert calls[2][0] == "pyocd"
    assert "board" not in calls[2][1]


def test_connect_halts_target_and_disconnect_closes_session() -> None:
    session = FakeSession()
    backend = PyOcdBackend(session_factory=lambda probe, options: session)

    backend.connect(object(), "HPM5300", 1_000_000)
    backend.disconnect()

    assert session.open_calls == 1
    assert session.target.reset_and_halt_calls == 1
    assert session.close_calls == 1


def test_session_factory_receives_probe_and_options_positionally() -> None:
    session = FakeSession()

    def positional_factory(probe, options, /):
        assert options["target_override"] == "HPM5300"
        return session

    backend = PyOcdBackend(session_factory=positional_factory)
    backend.connect(object(), "HPM5300", 1_000_000)
    backend.disconnect()


def test_disconnect_clears_session_even_when_close_fails() -> None:
    class BrokenClose(FakeSession):
        def close(self):
            self.close_calls += 1
            raise RuntimeError("USB close failed")

    session = BrokenClose()
    backend = PyOcdBackend(session_factory=lambda probe, options: session)
    backend.connect(object(), "HPM5300", 1_000_000)

    assert_error(FlashErrorCode.CONNECT_FAIL, backend.disconnect)
    backend.disconnect()
    assert session.close_calls == 1


def test_import_does_not_load_pyocd_or_usb() -> None:
    script = (
        "import sys; import mklink.cmsis_dap.backend; "
        "assert not any(n == 'pyocd' or n.startswith('pyocd.') for n in sys.modules); "
        "assert not any(n == 'usb' or n.startswith('usb.') for n in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_connect_resolves_only_an_exact_unique_probe_id() -> None:
    first = FakeProbe("FIRST")
    wanted = FakeProbe("WANTED")
    seen = []
    backend = PyOcdBackend(
        session_factory=lambda probe, options: seen.append(probe) or FakeSession(),
        probe_provider=lambda: [first, wanted],
    )

    backend.connect("WANTED", "HPM5300", 1_000_000)

    assert seen == [wanted]
    backend.disconnect()

    missing = PyOcdBackend(
        session_factory=lambda probe, options: pytest.fail("must not fall back"),
        probe_provider=lambda: [first],
    )
    assert_error(
        FlashErrorCode.MKLINK_DAP_NOT_FOUND,
        lambda: missing.connect("WANTED", "HPM5300", 1_000_000),
    )


def test_connect_builds_supported_options_and_reconnect_closes_old(
    tmp_path: Path,
) -> None:
    pack = tmp_path / "target.pack"
    pack.write_bytes(b"pack")
    sessions = [FakeSession(), FakeSession()]
    calls = []

    def factory(probe, options):
        calls.append((probe, options))
        return sessions[len(calls) - 1]

    backend = PyOcdBackend(session_factory=factory)
    first_probe, second_probe = object(), object()
    backend.connect(
        first_probe,
        "HPM5300",
        2_000_000,
        pack=str(pack),
        connect_mode="under-reset",
        reset_mode="hardware",
    )
    backend.connect(second_probe, "HPM5361", 4_000_000)

    assert calls == [
        (
            first_probe,
            {
                "target_override": "HPM5300",
                "frequency": 2_000_000,
                "connect_mode": "under-reset",
                "auto_unlock": False,
                "pack": str(pack.resolve()),
            },
        ),
        (
            second_probe,
            {
                "target_override": "HPM5361",
                "frequency": 4_000_000,
                "connect_mode": "halt",
                "auto_unlock": False,
            },
        ),
    ]
    assert sessions[0].close_calls == 1
    backend.disconnect()


def test_connect_closes_partial_session_when_open_fails() -> None:
    class BrokenSession(FakeSession):
        def open(self) -> None:
            raise RuntimeError("transport failure")

    session = BrokenSession()
    backend = PyOcdBackend(session_factory=lambda probe, options: session)

    assert_error(
        FlashErrorCode.CONNECT_FAIL,
        lambda: backend.connect(object(), "HPM5300", 1_000_000),
    )
    assert session.close_calls == 1
    backend.disconnect()


@pytest.mark.parametrize("frequency", [0, -1, True])
def test_connect_rejects_invalid_frequency(frequency) -> None:
    backend = PyOcdBackend(session_factory=lambda probe, options: FakeSession())
    with pytest.raises(ValueError):
        backend.connect(object(), "HPM5300", frequency)


def test_connect_rejects_missing_or_non_pack_path(tmp_path: Path) -> None:
    backend = PyOcdBackend(session_factory=lambda probe, options: FakeSession())
    assert_error(
        FlashErrorCode.FILE_NOT_FOUND,
        lambda: backend.connect(object(), "HPM5300", 1, pack=str(tmp_path / "x.pack")),
    )
    wrong = tmp_path / "x.txt"
    wrong.write_text("x")
    assert_error(
        FlashErrorCode.TARGET_NOT_SUPPORTED,
        lambda: backend.connect(object(), "HPM5300", 1, pack=str(wrong)),
    )


def test_connect_installs_custom_flm_delegate_before_session_open(
    tmp_path: Path, monkeypatch,
) -> None:
    flm = tmp_path / "external.flm"
    flm.write_bytes(b"flm")
    session = FakeSession()
    installed = []

    class Sequence:
        def insert_before(self, task, item):
            assert task == "create_flash"
            name, callback = item
            assert name == "mklink_custom_flm"
            installed.append(callback)

    monkeypatch.setattr(
        "mklink.cmsis_dap.backend._install_custom_flm_regions",
        lambda target, paths, ram, regions: installed.append(
            (target, paths, ram, regions)
        ),
    )
    original_open = session.open

    def open_session():
        session.delegate.will_init_target(session.target, Sequence())
        installed[0]()
        original_open()

    session.open = open_session
    backend = PyOcdBackend(session_factory=lambda probe, options: session)

    backend.connect(
        object(),
        "HPM5300",
        10_000_000,
        custom_flm_paths=(str(flm),),
        custom_flm_digests=(hashlib.sha256(b"flm").hexdigest(),),
    )

    assert installed[1] == (session.target, (b"flm",), None, ())
    backend.disconnect()


def test_unknown_target_with_builtin_flm_uses_generic_target_and_catalog_ram(
    tmp_path: Path, monkeypatch,
) -> None:
    flm = tmp_path / "builtin.flm"
    flm.write_bytes(b"flm")
    session = FakeSession()
    observed = {}

    class Sequence:
        def insert_before(self, _task, item):
            _name, callback = item
            callback()

    monkeypatch.setattr(
        "mklink.cmsis_dap.backend._install_custom_flm_regions",
        lambda target, payloads, ram, regions: observed.update(
            target=target, payloads=payloads, ram=ram, regions=regions
        ),
    )

    def factory(_probe, options):
        observed["options"] = options
        original_open = session.open

        def open_session():
            session.delegate.will_init_target(session.target, Sequence())
            original_open()

        session.open = open_session
        return session

    backend = PyOcdBackend(session_factory=factory)
    backend.connect(
        object(),
        "VENDOR_UNKNOWN_PART",
        10_000_000,
        custom_flm_paths=(str(flm),),
        custom_flm_digests=(hashlib.sha256(b"flm").hexdigest(),),
        custom_flm_ram_start=0x20000000,
        custom_flm_ram_size=0x10000,
    )

    assert observed["options"]["target_override"] == "cortex_m"
    assert observed["payloads"] == (b"flm",)
    assert observed["ram"] == (0x20000000, 0x10000)
    assert observed["regions"] == ()
    backend.disconnect()


def test_connect_passes_exact_catalog_flash_region_to_delegate(
    tmp_path: Path, monkeypatch,
) -> None:
    flm = tmp_path / "builtin.flm"
    flm.write_bytes(b"flm")
    session = FakeSession()
    observed = {}

    class Sequence:
        def insert_before(self, _task, item):
            _name, callback = item
            callback()

    monkeypatch.setattr(
        "mklink.cmsis_dap.backend._install_custom_flm_regions",
        lambda _target, _payloads, _ram, regions: observed.update(regions=regions),
    )
    original_open = session.open

    def open_session():
        session.delegate.will_init_target(session.target, Sequence())
        original_open()

    session.open = open_session
    backend = PyOcdBackend(session_factory=lambda _probe, _options: session)
    backend.connect(
        object(),
        "VENDOR_UNKNOWN_PART",
        1_000_000,
        custom_flm_paths=(str(flm),),
        custom_flm_digests=(hashlib.sha256(b"flm").hexdigest(),),
        custom_flm_regions=((0x60000000, 0x1000000),),
    )

    assert observed["regions"] == ((0x60000000, 0x1000000),)
    backend.disconnect()


def test_connect_rejects_custom_flm_changed_after_catalog_snapshot(
    tmp_path: Path,
) -> None:
    flm = tmp_path / "external.flm"
    original = b"original-flm"
    flm.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    flm.write_bytes(b"replaced-flm")
    backend = PyOcdBackend(
        session_factory=lambda probe, options: pytest.fail(
            "integrity must be checked before creating a session"
        )
    )

    error = assert_error(
        FlashErrorCode.PACK_INTEGRITY_ERROR,
        lambda: backend.connect(
            object(),
            "HPM5300",
            1_000_000,
            custom_flm_paths=(str(flm),),
            custom_flm_digests=(digest,),
        ),
    )

    assert error.message == "custom FLM integrity check failed"


def test_custom_flm_delegate_preserves_an_existing_session_delegate(
    tmp_path: Path, monkeypatch,
) -> None:
    flm = tmp_path / "external.flm"
    flm.write_bytes(b"flm")
    calls = []

    class ExistingDelegate:
        def will_init_target(self, target, init_sequence):
            calls.append((target, init_sequence))

        def did_reset(self):
            return "existing"

    session = FakeSession()
    existing = ExistingDelegate()
    session.delegate = existing
    backend = PyOcdBackend(session_factory=lambda probe, options: session)
    monkeypatch.setattr(
        "mklink.cmsis_dap.backend._install_custom_flm_regions",
        lambda target, paths, ram, regions: None,
    )

    backend.connect(
        object(),
        "HPM5300",
        1,
        custom_flm_paths=(str(flm),),
        custom_flm_digests=(hashlib.sha256(b"flm").hexdigest(),),
    )
    sequence = type("Sequence", (), {"insert_before": lambda *args: None})()
    session.delegate.will_init_target(session.target, sequence)

    assert calls == [(session.target, sequence)]
    assert session.delegate.did_reset() == "existing"
    backend.disconnect()


def test_custom_flm_regions_do_not_mutate_a_shared_pack_memory_map(
    tmp_path: Path, monkeypatch,
) -> None:
    from pyocd.core.memory_map import FlashRegion, MemoryMap, RamRegion

    shared = MemoryMap(
        FlashRegion(start=0x08000000, length=0x20000, sector_size=0x2000),
        RamRegion(start=0x20000000, length=0x20000),
    )
    targets = [type("Target", (), {"memory_map": shared})() for _ in range(2)]
    flm = tmp_path / "external.flm"
    flm.write_bytes(b"flm")

    class Algorithm:
        flash_start = 0x90000000
        flash_size = 0x800000

    monkeypatch.setattr(
        "pyocd.target.pack.flash_algo.PackFlashAlgo",
        lambda _path: Algorithm(),
    )

    _install_custom_flm_regions(targets[0], (flm.read_bytes(),))
    _install_custom_flm_regions(targets[1], (flm.read_bytes(),))

    assert len(shared.regions) == 2
    assert len(targets[0].memory_map.regions) == 3
    assert len(targets[1].memory_map.regions) == 3


def test_pack_flm_relocation_clones_relative_algorithm() -> None:
    from pyocd.core.memory_map import FlashRegion, MemoryMap

    flash_info = type("FlashInfo", (), {"start": 0})()
    algorithm = type("Algorithm", (), {
        "flash_start": 0,
        "flash_size": 0x100000,
        "flash_info": flash_info,
    })()
    memory_map = MemoryMap(FlashRegion(
        name="IROM1",
        start=0x00400000,
        length=0x100000,
        flm=algorithm,
    ))
    target = type("Target", (), {"memory_map": memory_map})()

    _relocate_pack_flm_regions(target)

    relocated = memory_map.get_region_for_address(0x00400000).flm
    assert relocated is not algorithm
    assert relocated.flash_start == 0x00400000
    assert relocated.flash_info is not flash_info
    assert relocated.flash_info.start == 0x00400000
    assert algorithm.flash_start == 0
    assert algorithm.flash_info.start == 0


def test_pack_flm_relocation_rejects_out_of_bounds_relative_algorithm() -> None:
    from pyocd.core.memory_map import FlashRegion, MemoryMap

    algorithm = type("Algorithm", (), {
        "flash_start": 0x80000,
        "flash_size": 0x100000,
    })()
    memory_map = MemoryMap(FlashRegion(
        name="IROM1",
        start=0x00400000,
        length=0x100000,
        flm=algorithm,
    ))
    target = type("Target", (), {"memory_map": memory_map})()

    _relocate_pack_flm_regions(target)

    assert memory_map.get_region_for_address(0x00400000).flm is algorithm
    assert algorithm.flash_start == 0x80000


def test_pack_flm_expansion_requires_an_algorithm_covering_the_requested_range() -> None:
    from pyocd.core.memory_map import FlashRegion, MemoryMap

    algorithm = type("Algorithm", (), {
        "flash_start": 0x08000000,
        "flash_size": 0x80000,
    })()
    original = FlashRegion(
        name="IROM1",
        start=0x08000000,
        length=0x40000,
        flm=algorithm,
    )
    target = type("Target", (), {"memory_map": MemoryMap(original)})()

    _expand_pack_flm_regions(target, ((0x08000000, 0x80000),))

    expanded = target.memory_map.get_region_for_address(0x0807FFFF)
    assert expanded is not None
    assert expanded.length == 0x80000
    assert expanded.flm is algorithm
    assert original.length == 0x40000

    with pytest.raises(FlashError) as raised:
        _expand_pack_flm_regions(target, ((0x08000000, 0x100000),))
    assert raised.value.code is FlashErrorCode.TARGET_NOT_SUPPORTED


def test_pack_flm_expansion_accepts_an_existing_runtime_flash_range() -> None:
    from pyocd.core.memory_map import FlashRegion, MemoryMap

    region = FlashRegion(
        name="flash",
        start=0x08000000,
        length=0x80000,
        algo={"pc_program_page": 0x20000100},
        blocksize=0x800,
    )
    target = type("Target", (), {"memory_map": MemoryMap(region)})()

    _expand_pack_flm_regions(target, ((0x08000000, 0x80000),))

    accepted = target.memory_map.get_region_for_address(0x0807FFFF)
    assert accepted is not None
    assert accepted.length == 0x80000


def test_pack_connect_relocates_flm_before_create_flash(tmp_path: Path) -> None:
    from pyocd.core.memory_map import FlashRegion, MemoryMap

    pack = tmp_path / "Vendor.Device.pack"
    pack.write_bytes(b"pack")
    algorithm = type("Algorithm", (), {
        "flash_start": 0,
        "flash_size": 0x100000,
    })()
    target = FakeTarget(MemoryMap(FlashRegion(
        name="IROM1",
        start=0x00400000,
        length=0x100000,
        flm=algorithm,
    )))
    session = FakeSession(target)
    backend = PyOcdBackend(session_factory=lambda probe, options: session)

    backend.connect(object(), "CST92F41KxVxxx", 1_000_000, pack=str(pack))
    inserted = []
    sequence = type("Sequence", (), {
        "insert_before": lambda self, task, item: inserted.append((task, item)),
    })()
    session.delegate.will_init_target(target, sequence)

    assert inserted[0][0] == "create_flash"
    assert inserted[0][1][0] == "mklink_pack_flm_relocation"
    inserted[0][1][1]()
    assert target.memory_map.get_region_for_address(0x00400000).flm.flash_start == 0x00400000
    backend.disconnect()


def test_custom_flm_replaces_overlapping_generic_placeholder_region(
    monkeypatch,
) -> None:
    from pyocd.core.memory_map import MemoryMap, RamRegion

    target = type("Target", (), {"memory_map": MemoryMap(
        RamRegion(name="Code", start=0x00000000, length=0x20000000),
        RamRegion(name="SRAM", start=0x20000000, length=0x20000000),
        RamRegion(name="RAM2", start=0x80000000, length=0x20000000),
    )})()

    class Algorithm:
        flash_start = 0x90000000
        flash_size = 0x800000

    monkeypatch.setattr(
        "pyocd.target.pack.flash_algo.PackFlashAlgo",
        lambda _payload: Algorithm(),
    )

    _install_custom_flm_regions(
        target,
        (b"flm",),
        (0x20000000, 0x10000),
    )

    region = target.memory_map.get_region_for_address(0x90000400)
    assert region is not None
    assert region.is_flash
    assert region.name == "mklink_custom_flm_0"
    ram = target.memory_map.get_region_for_address(0x20000000)
    assert ram is not None
    assert ram.is_ram
    assert ram.start == 0x20000000
    assert ram.length == 0x10000
    assert target.memory_map.get_region_for_address(0x1000) is None


def test_custom_flm_region_uses_catalog_range_instead_of_embedded_range(
    monkeypatch,
) -> None:
    from pyocd.core.memory_map import MemoryMap, RamRegion

    target = type("Target", (), {"memory_map": MemoryMap(
        RamRegion(start=0x20000000, length=0x10000),
    )})()

    class Algorithm:
        flash_start = 0x90000000
        flash_size = 0x800000

    monkeypatch.setattr(
        "pyocd.target.pack.flash_algo.PackFlashAlgo",
        lambda _payload: Algorithm(),
    )

    _install_custom_flm_regions(
        target,
        (b"flm",),
        None,
        ((0x60000000, 0x1000000),),
    )

    region = target.memory_map.get_region_for_address(0x60001000)
    assert region is not None
    assert region.is_flash
    assert region.start == 0x60000000
    assert region.length == 0x1000000


def test_custom_flm_replaces_overlapping_builtin_region_on_cloned_map(
    tmp_path: Path, monkeypatch,
) -> None:
    from pyocd.core.memory_map import FlashRegion, MemoryMap, RamRegion

    shared = MemoryMap(
        FlashRegion(start=0x90000000, length=0x800000, sector_size=0x1000),
        RamRegion(start=0x20000000, length=0x20000),
    )
    target = type("Target", (), {"memory_map": shared})()

    class Algorithm:
        flash_start = 0x90000000
        flash_size = 0x800000

    monkeypatch.setattr(
        "pyocd.target.pack.flash_algo.PackFlashAlgo",
        lambda _payload: Algorithm(),
    )

    _install_custom_flm_regions(target, (b"custom",))

    assert len(shared.regions) == 2
    replacement = target.memory_map.get_region_for_address(0x90000000)
    assert replacement.name == "mklink_custom_flm_0"
    assert len(target.memory_map.regions) == 2


def test_custom_flm_flash_calls_optional_verify_entry(tmp_path: Path, monkeypatch) -> None:
    from pyocd.core.memory_map import MemoryMap, RamRegion
    from pyocd.flash.flash import Flash

    flm = tmp_path / "external.flm"
    flm.write_bytes(b"flm")

    class Algorithm:
        flash_start = 0x90000000
        flash_size = 0x800000
        symbols = {"Verify": 0x5D}

        def get_pyocd_flash_algo(self, blocksize, ram_region):
            return {
                "load_address": 0x20001000,
                "instructions": [0] * 64,
                "pc_erase_sector": 0x20001001,
                "pc_program_page": 0x20001003,
                "page_buffers": [0x20000000],
                "begin_stack": 0x20002000,
                "static_base": 0x20001100,
                "analyzer_supported": False,
            }

    monkeypatch.setattr(
        "pyocd.target.pack.flash_algo.PackFlashAlgo",
        lambda _path: Algorithm(),
    )
    target = FakeTarget(MemoryMap(RamRegion(start=0x20000000, length=0x20000)))

    _install_custom_flm_regions(target, (flm.read_bytes(),))

    region = target.memory_map.get_region_for_address(0x90000000)
    algo = region.flm.get_pyocd_flash_algo(0x1000, None)
    flash_target = type(
        "FlashTarget",
        (),
        {
            "session": type(
                "Session",
                (),
                {"options": {"flash.timeout.program": 7.0}},
            )(),
            "write_memory_block8": lambda self, address, data: writes.append(
                (address, bytes(data))
            ),
        },
    )()
    flash = Flash(flash_target, algo)
    flash.region = region
    lifecycle = []
    writes = []
    flash.init = lambda operation: lifecycle.append(("init", operation.name))
    flash.uninit = lambda: lifecycle.append(("uninit",))
    flash._call_function_and_wait = lambda pc, r0, r1, r2, timeout: (
        lifecycle.append(("call", pc, r0, r1, r2, timeout)) or r0 + r1
    )

    result = PyOcdBackend._verify_with_flash_algorithm(
        flash, 0x90000020, b"abcd"
    )

    assert flash.flash_algo["pc_verify"] == 0x20001061
    assert flash.flash_algo["mklink_custom_verify"] is True
    assert writes == [(0x20000000, b"abcd")]
    assert lifecycle == [
        ("init", "VERIFY"),
        ("call", 0x20001061, 0x90000020, 4, 0x20000000, 7.0),
        ("uninit",),
    ]
    assert result == 0x90000024


def connected_backend(*, target=None, programmer_factory=None, eraser_factory=None):
    session = FakeSession(target or FakeTarget())
    backend = PyOcdBackend(
        session_factory=lambda probe, options: session,
        programmer_factory=programmer_factory,
        eraser_factory=eraser_factory,
    )
    backend.connect(object(), "HPM5300", 1_000_000)
    return backend, session


def test_operations_require_a_connected_session() -> None:
    backend = PyOcdBackend()
    for operation in (
        backend.erase_chip,
        lambda: backend.erase_sectors([0x80000000]),
        lambda: backend.program(ImageInspection("missing")),
        lambda: backend.verify(ImageInspection("missing")),
        backend.reset_run,
        backend.memory_regions,
    ):
        assert_error(FlashErrorCode.CONNECT_FAIL, operation)


def test_chip_and_sector_erase_use_exact_modes_and_sorted_unique_addresses() -> None:
    events = []

    class Eraser:
        def __init__(self, session, mode):
            events.append(("create", session, mode.name))

        def erase(self, addresses=None):
            events.append(("erase", addresses))

    target = FakeTarget((FakeRegion(0x80000000, 0x1000, blocksize=0x100),))
    backend, session = connected_backend(target=target, eraser_factory=Eraser)

    backend.erase_chip()
    backend.erase_sectors([0x80000200, 0x80000000, 0x80000200])

    assert events == [
        ("create", session, "CHIP"),
        ("erase", None),
        ("create", session, "SECTOR"),
        ("erase", [0x80000000, 0x80000200]),
    ]
    backend.disconnect()


@pytest.mark.parametrize(
    ("regions", "addresses", "code"),
    [
        ((FakeRegion(0x80000000, 0x1000),), [], FlashErrorCode.IMAGE_OUT_OF_RANGE),
        ((FakeRegion(0x80000000, 0x1000),), [0x80000001], FlashErrorCode.IMAGE_OUT_OF_RANGE),
        ((FakeRegion(0x80000000, 0x1000),), [0x90000000], FlashErrorCode.IMAGE_OUT_OF_RANGE),
        ((FakeRegion(0x80000000, 0x1000, blocksize=None),), [0x80000000], FlashErrorCode.TARGET_NOT_SUPPORTED),
    ],
)
def test_sector_erase_validates_flash_geometry(regions, addresses, code) -> None:
    backend, _ = connected_backend(target=FakeTarget(regions), eraser_factory=pytest.fail)
    assert_error(code, lambda: backend.erase_sectors(addresses))
    backend.disconnect()


def test_sector_erase_accepts_real_flash_region_with_rx_access() -> None:
    from pyocd.core.memory_map import FlashRegion

    erased = []

    class Eraser:
        def __init__(self, session, mode):
            pass

        def erase(self, addresses=None):
            erased.append(addresses)

    region = FlashRegion(start=0x80000000, length=0x1000, blocksize=0x100)
    region.flash = UniformFlash(region.start, region.blocksize)
    assert region.is_writable is False
    backend, _ = connected_backend(
        target=FakeTarget((region,)), eraser_factory=Eraser
    )

    backend.erase_sectors([0x80000000])

    assert erased == [[0x80000000]]
    backend.disconnect()


def test_program_bin_passes_base_and_hex_does_not(tmp_path: Path) -> None:
    calls = []

    class Programmer:
        def __init__(self, session):
            calls.append(("create", session))

        def program(self, path, **kwargs):
            calls.append(("program", path, kwargs))

    binary = tmp_path / "firmware.bin"
    binary.write_bytes(b"bin")
    ihex = tmp_path / "firmware.hex"
    ihex.write_text(":00000001FF\n", encoding="ascii")
    backend, session = connected_backend(programmer_factory=Programmer)

    backend.program(
        ImageInspection("bin", file_path=str(binary), format="bin", base_address=0x80000000)
    )
    backend.program(ImageInspection("hex", file_path=str(ihex), format="hex"))

    assert calls == [
        ("create", session),
        ("program", str(binary), {"base_address": 0x80000000}),
        ("create", session),
        ("program", str(ihex), {}),
    ]
    backend.disconnect()


def test_program_forwards_pyocd_progress_callback(tmp_path: Path) -> None:
    reported = []

    class Programmer:
        def __init__(self, _session, *, progress):
            progress(0.5)

        def program(self, _path, **_kwargs):
            pass

    binary = tmp_path / "firmware.bin"
    binary.write_bytes(b"bin")
    backend, _session = connected_backend(programmer_factory=Programmer)

    backend.program(
        ImageInspection(
            "bin",
            file_path=str(binary),
            format="bin",
            base_address=0x80000000,
        ),
        progress_callback=reported.append,
    )

    assert reported == [0.5]
    backend.disconnect()


def test_program_disables_memory_scans_for_custom_flm_regions(tmp_path: Path) -> None:
    calls = []

    class Programmer:
        def __init__(self, session, **kwargs):
            calls.append(("create", session, kwargs))

        def program(self, path, **kwargs):
            calls.append(("program", path, kwargs))

    firmware = tmp_path / "external.hex"
    firmware.write_text(":00000001FF\n", encoding="ascii")
    target = FakeTarget((FakeRegion(
        0x90000000,
        0x800000,
        name="mklink_custom_flm_0",
    ),))
    backend, session = connected_backend(target=target, programmer_factory=Programmer)

    backend.program(ImageInspection(
        "external",
        file_path=str(firmware),
        format="hex",
        start=0x90000000,
        end=0x90001000,
    ))

    assert calls == [
        ("create", session, {"smart_flash": False, "keep_unwritten": False}),
        ("program", str(firmware), {}),
    ]
    backend.disconnect()


def test_program_maps_locked_error_and_closes_session(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"x")

    class Programmer:
        def __init__(self, session):
            pass

        def program(self, path, **kwargs):
            raise RuntimeError("target locked")

    backend, session = connected_backend(programmer_factory=Programmer)
    assert_error(
        FlashErrorCode.TARGET_LOCKED,
        lambda: backend.program(
            ImageInspection("bin", file_path=str(firmware), format="bin", base_address=0)
        ),
    )
    assert session.close_calls == 1
    assert_error(FlashErrorCode.CONNECT_FAIL, backend.erase_chip)


def test_program_maps_file_disappearance_to_file_not_found(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"x")

    class VanishedProgrammer:
        def __init__(self, session):
            pass

        def program(self, path, **kwargs):
            raise FileNotFoundError("snapshot vanished")

    backend, _ = connected_backend(programmer_factory=VanishedProgrammer)
    assert_error(
        FlashErrorCode.FILE_NOT_FOUND,
        lambda: backend.program(
            ImageInspection("bin", file_path=str(firmware), format="bin", base_address=0)
        ),
    )


def test_sector_geometry_read_failure_maps_to_erase_fail() -> None:
    class BrokenTarget(FakeTarget):
        @property
        def memory_map(self):
            raise RuntimeError("memory map unavailable")

        @memory_map.setter
        def memory_map(self, value):
            pass

    backend, _ = connected_backend(target=BrokenTarget())
    assert_error(
        FlashErrorCode.ERASE_FAIL,
        lambda: backend.erase_sectors([0x80000000]),
    )
    backend.disconnect()


def test_sector_erase_uses_actual_variable_sector_geometry() -> None:
    events = []

    class VariableFlash:
        def get_sector_info(self, address):
            if address < 0x1000:
                return FakeSectorInfo(0, 0x1000)
            return FakeSectorInfo(0x1000, 0x2000)

    class Eraser:
        def __init__(self, session, mode):
            pass

        def erase(self, addresses=None):
            events.append(addresses)

    region = FakeRegion(0, 0x4000, blocksize=0x400)
    region.flash = VariableFlash()
    backend, _ = connected_backend(
        target=FakeTarget((region,)), eraser_factory=Eraser
    )

    assert_error(
        FlashErrorCode.IMAGE_OUT_OF_RANGE,
        lambda: backend.erase_sectors([0x400]),
    )
    backend.erase_sectors([0x1000, 0x1000])

    assert events == [[0x1000]]
    backend.disconnect()


@pytest.mark.parametrize("kind", ["missing", "none", "size", "base"])
def test_sector_erase_rejects_unreliable_sector_info(kind) -> None:
    class Flash:
        def get_sector_info(self, address):
            if kind == "none":
                return None
            if kind == "size":
                return FakeSectorInfo(address, 0)
            return type("Info", (), {"size": 0x100})()

    region = FakeRegion(0, 0x1000)
    if kind == "missing":
        del region.flash
    else:
        region.flash = Flash()
    backend, _ = connected_backend(target=FakeTarget((region,)))

    assert_error(
        FlashErrorCode.TARGET_NOT_SUPPORTED,
        lambda: backend.erase_sectors([0]),
    )
    backend.disconnect()


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("sector query failed", FlashErrorCode.ERASE_FAIL),
        ("flash locked", FlashErrorCode.TARGET_LOCKED),
    ],
)
def test_sector_info_query_exceptions_are_mapped(message, code) -> None:
    class Flash:
        def get_sector_info(self, address):
            raise RuntimeError(message)

    region = FakeRegion(0, 0x1000)
    region.flash = Flash()
    backend, _ = connected_backend(target=FakeTarget((region,)))

    assert_error(code, lambda: backend.erase_sectors([0]))
    backend.disconnect()


def test_sector_info_query_flash_error_is_remapped() -> None:
    class Flash:
        def get_sector_info(self, address):
            raise FlashError(FlashErrorCode.PROGRAM_FAIL, "sector query failed")

    region = FakeRegion(0, 0x1000)
    region.flash = Flash()
    backend, _ = connected_backend(target=FakeTarget((region,)))

    assert_error(FlashErrorCode.ERASE_FAIL, lambda: backend.erase_sectors([0]))
    backend.disconnect()


class MemoryTarget(FakeTarget):
    def __init__(self, data: dict[int, int]) -> None:
        super().__init__()
        self.data = data
        self.read_calls = []

    def read_memory_block8(self, address: int, size: int):
        self.read_calls.append((address, size))
        return [self.data[address + offset] for offset in range(size)]


def ihex_record(address: int, record_type: int, data: bytes = b"") -> str:
    payload = bytes((len(data), address >> 8, address & 0xFF, record_type)) + data
    return ":" + (payload + bytes((-sum(payload) & 0xFF,))).hex().upper()


def test_verify_bin_reads_in_bounded_chunks_and_reports_first_mismatch(
    tmp_path: Path,
) -> None:
    payload = bytes(range(256)) * 20
    base = 0x80000000
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(payload)
    target = MemoryTarget({base + index: value for index, value in enumerate(payload)})
    backend, _ = connected_backend(target=target)
    image = ImageInspection(
        "bin",
        file_path=str(firmware),
        format="bin",
        size=len(payload),
        start=base,
        end=base + len(payload),
        segments=(ImageSegment(base, base + len(payload)),),
        base_address=base,
    )

    backend.verify(image)

    assert target.read_calls == [(base, 4096), (base + 4096, len(payload) - 4096)]
    target.data[base + 4100] ^= 0xFF
    error = assert_error(FlashErrorCode.VERIFY_FAIL, lambda: backend.verify(image))
    assert "0x80001004" in error.message
    backend.disconnect()


def test_verify_short_read_reports_first_missing_address(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcd")

    class ShortTarget(FakeTarget):
        def read_memory_block8(self, address, size):
            return [ord("a"), ord("b")]

    backend, _ = connected_backend(target=ShortTarget())
    image = ImageInspection(
        "bin",
        file_path=str(firmware),
        format="bin",
        size=4,
        start=0x1000,
        end=0x1004,
        segments=(ImageSegment(0x1000, 0x1004),),
        base_address=0x1000,
    )
    error = assert_error(FlashErrorCode.VERIFY_FAIL, lambda: backend.verify(image))
    assert "0x1002" in error.message
    backend.disconnect()


@pytest.mark.parametrize(
    ("payload", "mismatch_address"),
    [(b"ab", 0x1002), (b"abcdef", 0x1004)],
)
def test_verify_bin_rejects_snapshot_size_change(
    tmp_path: Path, payload: bytes, mismatch_address: int
) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(payload)
    target = MemoryTarget(
        {0x1000 + offset: value for offset, value in enumerate(b"abcd")}
    )
    backend, _ = connected_backend(target=target)
    image = ImageInspection(
        "bin",
        file_path=str(firmware),
        format="bin",
        size=4,
        start=0x1000,
        end=0x1004,
        segments=(ImageSegment(0x1000, 0x1004),),
        base_address=0x1000,
    )

    error = assert_error(FlashErrorCode.VERIFY_FAIL, lambda: backend.verify(image))

    assert f"0x{mismatch_address:X}" in error.message
    backend.disconnect()


def test_verify_sparse_hex_reads_only_inspected_segments(tmp_path: Path) -> None:
    first, second = 0x80000000, 0x80010000
    firmware = tmp_path / "firmware.hex"
    firmware.write_text(
        "\n".join(
            (
                ihex_record(0, 4, b"\x80\x00"),
                ihex_record(0, 0, b"ab"),
                ihex_record(0, 4, b"\x80\x01"),
                ihex_record(0, 0, b"cd"),
                ihex_record(0, 1),
            )
        )
        + "\n",
        encoding="ascii",
    )
    target = MemoryTarget(
        {first: ord("a"), first + 1: ord("b"), second: ord("c"), second + 1: ord("d")}
    )
    backend, _ = connected_backend(target=target)
    image = ImageInspection(
        "hex",
        file_path=str(firmware),
        format="hex",
        segments=(ImageSegment(first, first + 2), ImageSegment(second, second + 2)),
    )

    backend.verify(image)

    assert target.read_calls == [(first, 2), (second, 2)]
    backend.disconnect()


def test_verify_uses_custom_flm_for_non_memory_mapped_flash(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcdef")
    calls = []

    class FlmVerifier:
        def verify_data(self, address, data):
            calls.append((address, bytes(data)))
            return address + len(data)

    class ExternalTarget(FakeTarget):
        def read_memory_block8(self, address, size):
            pytest.fail("external flash must be verified by its FLM")

    region = FakeRegion(0x90000000, 0x1000, blocksize=0x100)
    region.page_size = 4
    region.flash = FlmVerifier()
    backend, _ = connected_backend(target=ExternalTarget((region,)))

    backend.verify(
        ImageInspection(
            "bin",
            file_path=str(firmware),
            format="bin",
            size=6,
            start=0x90000000,
            end=0x90000006,
            segments=(ImageSegment(0x90000000, 0x90000006),),
            base_address=0x90000000,
        )
    )

    assert calls == [(0x90000000, b"abcd"), (0x90000004, b"ef")]
    backend.disconnect()


def test_verify_reports_custom_flm_failure_address(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcd")

    class FlmVerifier:
        def verify_data(self, address, data):
            return address + 2

    region = FakeRegion(0x90000000, 0x1000, blocksize=0x100)
    region.page_size = 4
    region.flash = FlmVerifier()
    backend, _ = connected_backend(target=FakeTarget((region,)))
    image = ImageInspection(
        "bin",
        file_path=str(firmware),
        format="bin",
        size=4,
        start=0x90000000,
        end=0x90000004,
        segments=(ImageSegment(0x90000000, 0x90000004),),
        base_address=0x90000000,
    )

    error = assert_error(FlashErrorCode.VERIFY_FAIL, lambda: backend.verify(image))

    assert "0x90000002" in error.message
    backend.disconnect()


def test_verify_supports_word_read_api(tmp_path: Path) -> None:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"abcde")

    class WordTarget(FakeTarget):
        def read_memory_block32(self, address, count):
            assert count == 2
            return [0x64636261, 0x00000065]

    backend, _ = connected_backend(target=WordTarget())
    backend.verify(
        ImageInspection(
            "bin",
            file_path=str(firmware),
            format="bin",
            size=5,
            start=0x1000,
            end=0x1005,
            segments=(ImageSegment(0x1000, 0x1005),),
            base_address=0x1000,
        )
    )
    backend.disconnect()


def test_reset_uses_stored_and_overridden_public_reset_types() -> None:
    target = FakeTarget()
    session = FakeSession(target)
    backend = PyOcdBackend(session_factory=lambda probe, options: session)
    backend.connect(object(), "HPM5300", 1_000_000, reset_mode="hardware")

    backend.reset_run()
    backend.reset_run("default")
    backend.reset_run("software")
    backend.reset_run("core")
    backend.reset_run("system")

    assert [getattr(value, "name", None) for value in target.reset_calls] == [
        "HARDWARE",
        None,
        "DEFAULT",
        "CORE",
        "SYSTEM",
    ]
    with pytest.raises(ValueError):
        backend.reset_run("mystery")
    backend.disconnect()


def test_memory_regions_converts_only_flash_and_ram() -> None:
    regions = (
        FakeRegion(0x80000000, 0x1000, blocksize=0x100, name="flash"),
        FakeRegion(0x10000000, 0x800, is_flash=False, is_ram=True, blocksize=None, name="ram"),
        FakeRegion(0x40000000, 0x100, is_flash=False, blocksize=None, name="device"),
    )
    backend, _ = connected_backend(target=FakeTarget(regions))

    assert backend.memory_regions() == (
        MemoryRegion("flash", 0x80000000, 0x1000, True, True, 0x100),
        MemoryRegion("ram", 0x10000000, 0x800, False, True, None),
    )
    backend.disconnect()


def test_memory_regions_marks_real_rx_flash_as_programmable() -> None:
    from pyocd.core.memory_map import FlashRegion

    region = FlashRegion(
        start=0x80000000,
        length=0x1000,
        blocksize=0x100,
        name="real-flash",
    )
    region.flash = UniformFlash(region.start, region.blocksize)
    assert region.is_writable is False
    backend, _ = connected_backend(target=FakeTarget((region,)))

    assert backend.memory_regions() == (
        MemoryRegion("real-flash", 0x80000000, 0x1000, True, True, 0x100),
    )
    backend.disconnect()


@pytest.mark.parametrize("failure", ["iteration", "property"])
def test_memory_regions_maps_memory_map_failures_without_leaking_details(failure) -> None:
    secret = "INTERNAL transport detail"

    class BrokenMap:
        def __iter__(self):
            raise RuntimeError(secret)

    class BrokenRegion:
        @property
        def is_flash(self):
            raise RuntimeError(secret)

    memory_map = BrokenMap() if failure == "iteration" else (BrokenRegion(),)
    backend, _ = connected_backend(target=FakeTarget(memory_map))

    error = assert_error(FlashErrorCode.TARGET_NOT_SUPPORTED, backend.memory_regions)
    assert error.message == "target memory map is unavailable"
    assert secret not in error.message
    backend.disconnect()


def test_memory_regions_maps_locked_failure_without_leaking_details() -> None:
    class LockedMap:
        def __iter__(self):
            raise RuntimeError("read protection internal detail")

    backend, _ = connected_backend(target=FakeTarget(LockedMap()))

    error = assert_error(FlashErrorCode.TARGET_LOCKED, backend.memory_regions)
    assert error.message == "target memory map is protected"
    backend.disconnect()


def test_memory_regions_remaps_flash_error_without_leaking_details() -> None:
    secret = "INTERNAL flash error detail"

    class BrokenRegion:
        @property
        def is_flash(self):
            raise FlashError(FlashErrorCode.PROGRAM_FAIL, secret)

    backend, _ = connected_backend(target=FakeTarget((BrokenRegion(),)))

    error = assert_error(FlashErrorCode.TARGET_NOT_SUPPORTED, backend.memory_regions)
    assert error.message == "target memory map is unavailable"
    assert secret not in error.message
    backend.disconnect()


def test_public_operations_are_serialized() -> None:
    state = {"active": 0, "maximum": 0}
    state_lock = threading.Lock()

    class SlowEraser:
        def __init__(self, session, mode):
            pass

        def erase(self, addresses=None):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.03)
            with state_lock:
                state["active"] -= 1

    backend, _ = connected_backend(eraser_factory=SlowEraser)
    threads = [threading.Thread(target=backend.erase_chip) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["maximum"] == 1
    backend.disconnect()


def test_connect_and_erase_map_locked_failures() -> None:
    class LockedSession(FakeSession):
        def open(self):
            raise RuntimeError("read protection enabled")

    assert_error(
        FlashErrorCode.TARGET_LOCKED,
        lambda: PyOcdBackend(
            session_factory=lambda probe, options: LockedSession()
        ).connect(object(), "HPM5300", 1),
    )

    class LockedEraser:
        def __init__(self, session, mode):
            pass

        def erase(self, addresses=None):
            raise RuntimeError("device locked")

    backend, _ = connected_backend(eraser_factory=LockedEraser)
    assert_error(FlashErrorCode.TARGET_LOCKED, backend.erase_chip)
    backend.disconnect()


@pytest.mark.parametrize(
    "message",
    [
        "read protection enabled",
        "readout protection enabled",
        "target locked",
        "device is locked",
        "flash locked",
        "RDP level 1",
        "RDP level 2",
        "mass erase disabled due protection",
    ],
)
def test_erase_maps_specific_lock_phrases(message) -> None:
    class LockedEraser:
        def __init__(self, session, mode):
            pass

        def erase(self, addresses=None):
            raise RuntimeError(message)

    backend, _ = connected_backend(eraser_factory=LockedEraser)

    assert_error(FlashErrorCode.TARGET_LOCKED, backend.erase_chip)
    backend.disconnect()


@pytest.mark.parametrize(
    "message", ["security extension unsupported", "protected method missing"]
)
def test_erase_does_not_treat_generic_security_words_as_locked(message) -> None:
    class BrokenEraser:
        def __init__(self, session, mode):
            pass

        def erase(self, addresses=None):
            raise RuntimeError(message)

    backend, _ = connected_backend(eraser_factory=BrokenEraser)

    assert_error(FlashErrorCode.ERASE_FAIL, backend.erase_chip)
    backend.disconnect()
