"""Lazy, serialised pyOCD backend for online flash operations."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
import time
from copy import copy
from pathlib import Path
from types import MethodType
from typing import Any, Callable, Iterator, Mapping, Optional, Tuple

from .errors import FlashError, FlashErrorCode
from .images import ImageInspector
from .models import ImageInspection, ImageSegment, MemoryRegion
from .pyocd_runtime import import_pyocd_attr


_LOCKED_ERROR_PATTERN = re.compile(
    r"\bread(?:out)?\s+protection\b"
    r"|\b(?:target|device|flash)(?:\s+is)?\s+locked\b"
    r"|\brdp\s+level\s+(?:1(?:\s*/\s*2)?|2)\b"
    r"|\bmass\s+erase\s+disabled\s+due\s+protection\b",
    re.IGNORECASE,
)
_POWER_CYCLE_VOLTAGES_MV = frozenset({1800, 3300, 5000})
_POWER_CYCLE_OFF_SECONDS = 3.0


def _power_cycle_mklink_probe(probe_identifier: str, voltage_mv: int) -> None:
    """Power-cycle exactly one MKLink target through its matching CDC interface."""

    from mklink.bridge import MKLinkSerialBridge
    from mklink.discovery import discover_mklink_command_ports

    identifier = str(probe_identifier or "").strip().casefold()
    if not identifier:
        raise RuntimeError("the selected CMSIS-DAP probe has no stable USB identity")
    matches = [
        port
        for port in discover_mklink_command_ports()
        if str(getattr(port, "serial_number", "") or "").strip().casefold()
        == identifier
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "could not map the selected CMSIS-DAP probe to exactly one MKLink command port"
        )
    bridge = MKLinkSerialBridge(str(matches[0].device))
    if not bridge.connect():
        raise RuntimeError("could not open the selected MKLink command port")
    powered_off = False
    try:
        bridge.send_command("cmd.set_power_off()", timeout=10.0)
        powered_off = True
        time.sleep(_POWER_CYCLE_OFF_SECONDS)
        bridge.send_command(f"cmd.set_power_on({voltage_mv})", timeout=10.0)
    except Exception:
        if powered_off:
            try:
                bridge.send_command(f"cmd.set_power_on({voltage_mv})", timeout=10.0)
            except Exception:
                pass
        raise
    finally:
        bridge.close()


def _pack_flm_address_offset(
    region_start: int,
    region_length: int,
    flm_start: int,
    flm_size: int,
) -> int:
    values = (region_start, region_length, flm_start, flm_size)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        return 0
    if region_start < 0 or region_length <= 0 or flm_start < 0 or flm_size <= 0:
        return 0

    region_end = region_start + region_length
    flm_end = flm_start + flm_size
    if flm_start < region_end and region_start < flm_end:
        return 0
    if flm_end > region_length or region_end > 0x1_0000_0000:
        return 0
    return region_start


def _relocate_pack_flm_regions(target: Any) -> None:
    for region in getattr(target, "memory_map", ()):
        if not bool(getattr(region, "is_flash", False)):
            continue
        algorithm = getattr(region, "flm", None)
        offset = _pack_flm_address_offset(
            getattr(region, "start", None),
            getattr(region, "length", None),
            getattr(algorithm, "flash_start", None),
            getattr(algorithm, "flash_size", None),
        )
        if offset == 0:
            continue

        relocated = copy(algorithm)
        relocated.flash_start = int(algorithm.flash_start) + offset
        flash_info = getattr(algorithm, "flash_info", None)
        if getattr(flash_info, "start", None) == algorithm.flash_start:
            relocated.flash_info = copy(flash_info)
            relocated.flash_info.start = relocated.flash_start
        region.flm = relocated


def _expand_pack_flm_regions(
    target: Any,
    requested_regions: Tuple[Tuple[int, int], ...],
) -> None:
    if not requested_regions:
        return
    target.memory_map = target.memory_map.clone()
    for start, size in requested_regions:
        same_start = [
            region
            for region in target.memory_map
            if bool(getattr(region, "is_flash", False)) and int(region.start) == start
        ]
        if len(same_start) == 1 and size <= int(same_start[0].length):
            continue
        matches = []
        for region in same_start:
            algorithm = getattr(region, "flm", None)
            algorithm_start = getattr(algorithm, "flash_start", None)
            algorithm_size = getattr(algorithm, "flash_size", None)
            if (
                isinstance(algorithm_start, int)
                and not isinstance(algorithm_start, bool)
                and isinstance(algorithm_size, int)
                and not isinstance(algorithm_size, bool)
                and algorithm_start <= start
                and start + size <= algorithm_start + algorithm_size
            ):
                matches.append(region)
        if len(matches) != 1:
            raise FlashError(
                FlashErrorCode.TARGET_NOT_SUPPORTED,
                "requested Flash range is not backed by one Pack algorithm",
            )
        region = matches[0]
        expanded = region.clone_with_changes(length=size)
        target.memory_map.remove_region(region)
        target.memory_map.add_region(expanded)


def _prepare_pack_flm_regions(
    target: Any,
    requested_regions: Tuple[Tuple[int, int], ...],
) -> None:
    _relocate_pack_flm_regions(target)
    _expand_pack_flm_regions(target, requested_regions)


class _TraceStateDelegate:
    """Preserve a running application's trace clock across pyOCD cleanup.

    Armed only after a successful non-security reset/run. pyOCD's normal core
    disconnect clears DEMCR, which otherwise stops firmware-owned DWT timing.
    Capture at teardown (not connection), and restore only an observed TRCENA;
    never restore debugger vector-catch bits or enable a previously disabled
    trace clock. Normal breakpoint cleanup, resume, and probe close still run.
    """

    _DEMCR = 0xE000EDFC
    _TRCENA = 1 << 24

    def __init__(self, next_delegate: Any = None) -> None:
        self._next_delegate = next_delegate
        self.enabled = False
        self.errors: list[str] = []
        self._trace_by_core: dict[int, int] = {}

    def will_stop_debug_core(self, core: Any) -> None:
        callback = getattr(self._next_delegate, "will_stop_debug_core", None)
        if callable(callback):
            callback(core=core)
        self._trace_by_core.pop(id(core), None)
        if not self.enabled:
            return
        try:
            self._trace_by_core[id(core)] = core.read32(self._DEMCR) & self._TRCENA
        except Exception as exc:
            # Do not prevent pyOCD from releasing a broken connection. Report
            # the failure after session.close(), even when pyOCD swallows it.
            self.errors.append(f"read target trace state: {exc}")

    def did_stop_debug_core(self, core: Any) -> None:
        callback = getattr(self._next_delegate, "did_stop_debug_core", None)
        if callable(callback):
            callback(core=core)
        trace = self._trace_by_core.pop(id(core), 0)
        if not trace:
            return
        try:
            current = core.read32(self._DEMCR)
            if not current & trace:
                core.write32(self._DEMCR, current | trace)
                core.flush()
                if not core.read32(self._DEMCR) & trace:
                    raise RuntimeError("TRCENA did not persist")
        except Exception as exc:
            self.errors.append(f"restore target trace state: {exc}")

    def __getattr__(self, name: str) -> Any:
        if self._next_delegate is None:
            raise AttributeError(name)
        return getattr(self._next_delegate, name)


class _PackFlmDelegate:
    def __init__(
        self,
        next_delegate: Any = None,
        expanded_regions: Tuple[Tuple[int, int], ...] = (),
    ) -> None:
        self._next_delegate = next_delegate
        self._expanded_regions = expanded_regions

    def will_init_target(self, target: Any, init_sequence: Any) -> None:
        callback = getattr(self._next_delegate, "will_init_target", None)
        if callable(callback):
            callback(target, init_sequence)
        init_sequence.insert_before(
            "create_flash",
            (
                "mklink_pack_flm_relocation",
                lambda: _prepare_pack_flm_regions(target, self._expanded_regions),
            ),
        )

    def __getattr__(self, name: str) -> Any:
        if self._next_delegate is None:
            raise AttributeError(name)
        return getattr(self._next_delegate, name)


class _CustomFlmDelegate:
    def __init__(
        self,
        payloads: Tuple[bytes, ...],
        next_delegate: Any = None,
        ram_region: Optional[Tuple[int, int]] = None,
        flash_regions: Tuple[Tuple[int, int], ...] = (),
    ) -> None:
        self._payloads = payloads
        self._next_delegate = next_delegate
        self._ram_region = ram_region
        self._flash_regions = flash_regions

    def will_init_target(self, target: Any, init_sequence: Any) -> None:
        callback = getattr(self._next_delegate, "will_init_target", None)
        if callable(callback):
            callback(target, init_sequence)
        init_sequence.insert_before(
            "create_flash",
            (
                "mklink_custom_flm",
                lambda: _install_custom_flm_regions(
                    target,
                    self._payloads,
                    self._ram_region,
                    self._flash_regions,
                ),
            ),
        )

    def __getattr__(self, name: str) -> Any:
        if self._next_delegate is None:
            raise AttributeError(name)
        return getattr(self._next_delegate, name)


class _SecurityFlmDelegate:
    def __init__(
        self,
        payload: bytes,
        next_delegate: Any = None,
        family: str = "",
        region: Tuple[int, int] = (0, 0),
    ) -> None:
        self._payload = payload
        self._next_delegate = next_delegate
        self._family = family
        self._region = region

    def will_init_target(self, target: Any, init_sequence: Any) -> None:
        callback = getattr(self._next_delegate, "will_init_target", None)
        if callable(callback):
            callback(target, init_sequence)
        init_sequence.insert_before(
            "create_flash",
            (
                "mklink_security_flm",
                lambda: _install_security_flm_region(
                    target, self._payload, self._family, self._region
                ),
            ),
        )

    def __getattr__(self, name: str) -> Any:
        if self._next_delegate is None:
            raise AttributeError(name)
        return getattr(self._next_delegate, name)


def _install_security_flm_region(
    target: Any,
    payload: bytes,
    family: str,
    region: Tuple[int, int],
) -> None:
    from .security import (
        GD32F30X_OPTION_ADDRESS,
        GD32F30X_OPTION_SIZE,
        PY32F030_OPTION_ADDRESS,
        PY32F030_OPTION_SIZE,
        STM32F1_OPTION_ADDRESS,
        STM32F1_OPTION_SIZE,
        STM32F4_OPTION_ADDRESS,
        STM32F4_OPTION_SIZE,
        STM32G4_OPTION_ADDRESS,
        STM32G4_OPTION_SIZE,
        STM32H7_OPTION_ADDRESS,
        STM32H7_OPTION_SIZE,
        STM32L0_OPTION_ADDRESS,
        STM32L0_OPTION_SIZE,
    )

    allowed_regions = {
        "gd32f303xe-spc": (GD32F30X_OPTION_ADDRESS, GD32F30X_OPTION_SIZE),
        "py32f030x8-rdp1": (PY32F030_OPTION_ADDRESS, PY32F030_OPTION_SIZE),
        "stm32f103-rdp1": (STM32F1_OPTION_ADDRESS, STM32F1_OPTION_SIZE),
        "stm32f413-rdp1": (STM32F4_OPTION_ADDRESS, STM32F4_OPTION_SIZE),
        "stm32g474-rdp1": (STM32G4_OPTION_ADDRESS, STM32G4_OPTION_SIZE),
        "stm32h743-rdp1": (STM32H7_OPTION_ADDRESS, STM32H7_OPTION_SIZE),
        "stm32l010x4-rdp1": (STM32L0_OPTION_ADDRESS, STM32L0_OPTION_SIZE),
    }
    expected_region = allowed_regions.get(family)
    if expected_region is None or region != expected_region:
        raise FlashError(
            FlashErrorCode.SECURITY_NOT_SUPPORTED,
            "security option-byte region is not in the safety whitelist",
        )
    option_address, option_size = expected_region
    FlashRegion = import_pyocd_attr("pyocd.core.memory_map", "FlashRegion")
    PackFlashAlgo = import_pyocd_attr(
        "pyocd.target.pack.flash_algo", "PackFlashAlgo"
    )
    try:
        algorithm = PackFlashAlgo(io.BytesIO(payload))
    except Exception:
        raise FlashError(
            FlashErrorCode.FILE_FORMAT_ERROR,
            "security option-byte FLM could not be loaded",
        ) from None
    if (
        int(getattr(algorithm, "flash_start", -1)) != option_address
        or int(getattr(algorithm, "flash_size", -1)) != option_size
    ):
        raise FlashError(
            FlashErrorCode.SECURITY_NOT_SUPPORTED,
            "security option-byte FLM metadata is outside the safety whitelist",
        )
    if family == "stm32h743-rdp1":
        # H743 option bytes are register-managed rather than memory mapped.
        # Its pinned legacy FLM intentionally declares the 0xFFFFFFFF sentinel;
        # validating that metadata is sufficient, and no fake memory region is
        # installed for the host-driven reversible RDP procedure.
        return
    target.memory_map = target.memory_map.clone()
    for existing in list(target.memory_map):
        start = int(existing.start)
        end = start + int(existing.length)
        if option_address < end and start < option_address + option_size:
            if start == option_address and int(existing.length) == option_size:
                target.memory_map.remove_region(existing)
            else:
                raise FlashError(
                    FlashErrorCode.SECURITY_NOT_SUPPORTED,
                    "security option-byte region overlaps the target memory map",
                )
    target.memory_map.add_region(FlashRegion(
        name="mklink_security_option_bytes",
        start=option_address,
        length=option_size,
        flm=algorithm,
    ))


def _install_custom_flm_regions(
    target: Any,
    payloads: Tuple[bytes, ...],
    ram_region: Optional[Tuple[int, int]] = None,
    flash_regions: Tuple[Tuple[int, int], ...] = (),
) -> None:
    FlashRegion = import_pyocd_attr("pyocd.core.memory_map", "FlashRegion")
    RamRegion = import_pyocd_attr("pyocd.core.memory_map", "RamRegion")
    PackFlashAlgo = import_pyocd_attr(
        "pyocd.target.pack.flash_algo", "PackFlashAlgo"
    )

    target.memory_map = target.memory_map.clone()
    if ram_region is not None:
        ram_start, ram_size = ram_region
        for region in list(target.memory_map):
            if bool(getattr(region, "is_ram", False)):
                target.memory_map.remove_region(region)
        target.memory_map.add_region(RamRegion(
            name="mklink_algorithm_ram",
            start=ram_start,
            length=ram_size,
        ))
    existing = [region for region in target.memory_map if bool(getattr(region, "is_flash", False))]
    if flash_regions and len(flash_regions) != len(payloads):
        raise FlashError(
            FlashErrorCode.PACK_INTEGRITY_ERROR,
            "custom FLM region metadata is invalid",
        )
    for index, payload in enumerate(payloads):
        try:
            algorithm = PackFlashAlgo(io.BytesIO(payload))
        except Exception:
            raise FlashError(
                FlashErrorCode.FILE_FORMAT_ERROR,
                "custom FLM could not be loaded",
            ) from None
        _enable_custom_flm_verify(algorithm)
        if flash_regions:
            start, size = flash_regions[index]
            algorithm.flash_start = start
            algorithm.flash_size = size
        else:
            start = int(algorithm.flash_start)
            size = int(algorithm.flash_size)
        end = start + size
        for region in list(target.memory_map):
            region_start = int(region.start)
            region_end = region_start + int(region.length)
            if start < region_end and region_start < end:
                if str(getattr(region, "name", "")).startswith("mklink_custom_flm_"):
                    raise FlashError(
                        FlashErrorCode.TARGET_NOT_SUPPORTED,
                        "custom FLM range overlaps another custom algorithm",
                    )
                target.memory_map.remove_region(region)
                if region in existing:
                    existing.remove(region)
        region = FlashRegion(
            name="mklink_custom_flm_{}".format(index),
            start=start,
            length=size,
            flm=algorithm,
        )
        target.memory_map.add_region(region)
        existing.append(region)


def _enable_custom_flm_verify(algorithm: Any) -> None:
    symbols = getattr(algorithm, "symbols", None)
    verify_offset = symbols.get("Verify") if isinstance(symbols, dict) else None
    build_algo = getattr(algorithm, "get_pyocd_flash_algo", None)
    if (
        not isinstance(verify_offset, int)
        or isinstance(verify_offset, bool)
        or verify_offset < 0
        or not callable(build_algo)
    ):
        return

    def build_with_verify(self: Any, blocksize: int, ram_region: Any) -> Any:
        flash_algo = build_algo(blocksize, ram_region)
        if flash_algo is None:
            return None
        flash_algo = dict(flash_algo)
        flash_algo["pc_verify"] = (
            int(flash_algo["load_address"]) + 4 + verify_offset
        )
        flash_algo["mklink_custom_verify"] = True
        return flash_algo

    algorithm.get_pyocd_flash_algo = MethodType(build_with_verify, algorithm)


def _mask_custom_flm_interrupts(target: Any) -> None:
    """Prevent application IRQ state from interrupting RAM flash algorithms."""

    for region in target.memory_map:
        name = str(getattr(region, "name", ""))
        if not (name.startswith("mklink_custom_flm_") or name == "mklink_security_option_bytes"):
            continue
        flash = getattr(region, "flash", None)
        original_call = getattr(flash, "_call_function", None)
        original_uninit = getattr(flash, "uninit", None)
        if not callable(original_call) or not callable(original_uninit):
            continue
        saved: dict[str, Optional[int]] = {"primask": None}

        def call_masked(
            self: Any,
            *args: Any,
            _call=original_call,
            _saved=saved,
            _target=target,
            **kwargs: Any,
        ):
            if _saved["primask"] is None:
                _saved["primask"] = int(_target.read_core_register("primask"))
            _target.write_core_register("primask", 1)
            return _call(*args, **kwargs)

        def uninit_restored(
            self: Any,
            *args: Any,
            _uninit=original_uninit,
            _saved=saved,
            _target=target,
            **kwargs: Any,
        ):
            try:
                return _uninit(*args, **kwargs)
            finally:
                previous = _saved["primask"]
                _saved["primask"] = None
                if previous is not None:
                    _target.write_core_register("primask", previous)

        flash._call_function = MethodType(call_masked, flash)
        flash.uninit = MethodType(uninit_restored, flash)


class HpmRomBackend:
    """Program HPMicro XPI Flash through the MKLink device-side ROM API."""

    # The HPM ROM flash call performs erase and program as one operation. The
    # job manager uses this marker to keep the erase stage pending until the
    # programming stage starts.
    erase_deferred = True
    READ_CHUNK_SIZE = 4 * 1024

    def __init__(
        self,
        device_factory: Optional[Callable[..., Any]] = None,
        port_resolver: Optional[Callable[[Any], Optional[str]]] = None,
        *,
        verify_chunk_size: int = 4096,
    ) -> None:
        if not isinstance(verify_chunk_size, int) or verify_chunk_size <= 0:
            raise ValueError("verify_chunk_size must be a positive integer")
        self._device_factory = device_factory
        self._port_resolver = port_resolver
        self._verify_chunk_size = verify_chunk_size
        self._device: Any = None
        self._target = ""
        self._frequency = 1_000_000
        self._board: Optional[str] = None
        self._flash_cfg: Optional[Tuple[str, str, str, str]] = None
        self._reset_mode = "default"
        self._reset_voltage_mv: Optional[int] = None
        self._lock = threading.RLock()

    def connect(
        self,
        probe: Any,
        target: str,
        frequency: int,
        pack: Optional[str] = None,
        custom_flm_paths: Tuple[str, ...] = (),
        custom_flm_digests: Tuple[str, ...] = (),
        custom_flm_regions: Tuple[Tuple[int, int], ...] = (),
        pack_flm_regions: Tuple[Tuple[int, int], ...] = (),
        custom_flm_ram_start: Optional[int] = None,
        custom_flm_ram_size: Optional[int] = None,
        connect_mode: str = "halt",
        reset_mode: str = "default",
        reset_voltage_mv: Optional[int] = None,
        board: Optional[str] = None,
        hpm_flash_cfg: Optional[Tuple[str, str, str, str]] = None,
    ) -> None:
        del (
            pack,
            custom_flm_paths,
            custom_flm_digests,
            custom_flm_regions,
            pack_flm_regions,
            custom_flm_ram_start,
            custom_flm_ram_size,
            connect_mode,
        )
        from mklink.hpm_config import (
            is_hpm_target,
            normalize_hpm_configuration,
        )

        if not is_hpm_target(target):
            raise FlashError(FlashErrorCode.TARGET_NOT_SUPPORTED, "target is not an HPM device")
        if not isinstance(frequency, int) or isinstance(frequency, bool) or not 1 <= frequency <= 10_000_000:
            raise ValueError("frequency must be between 1 and 10000000 Hz")
        if reset_voltage_mv is not None and reset_voltage_mv not in _POWER_CYCLE_VOLTAGES_MV:
            raise ValueError("reset_voltage_mv must be 1800, 3300, or 5000")
        if reset_mode != "power-cycle" and reset_voltage_mv is not None:
            raise ValueError("reset_voltage_mv is only valid for power-cycle reset")
        resolved_board, resolved_cfg = normalize_hpm_configuration(
            target, board=board, flash_cfg=hpm_flash_cfg
        )
        with self._lock:
            self.disconnect()
            resolver = self._port_resolver
            if resolver is None:
                from mklink.discovery import find_mklink_cdc_port

                resolver = lambda identifier: find_mklink_cdc_port(serial_number=identifier)
            port = resolver(probe)
            if not port:
                raise FlashError(
                    FlashErrorCode.MKLINK_DAP_NOT_FOUND,
                    "MKLink CDC bridge was not found for the selected probe",
                )
            factory = self._device_factory
            if factory is None:
                from mklink.device import connect

                factory = connect
            try:
                self._device = factory(port=port)
                self._target = str(target)
                self._frequency = frequency
                self._board = resolved_board
                self._flash_cfg = resolved_cfg
                self._reset_mode = reset_mode
                self._reset_voltage_mv = reset_voltage_mv
            except FlashError:
                raise
            except Exception as error:
                raise FlashError(FlashErrorCode.CONNECT_FAIL, str(error)) from error

    def disconnect(self) -> None:
        with self._lock:
            device, self._device = self._device, None
            self._reset_voltage_mv = None
            if device is not None:
                try:
                    device.close()
                except Exception as error:
                    raise FlashError(FlashErrorCode.CONNECT_FAIL, str(error)) from error

    def erase_chip(self) -> None:
        self._require_device()

    def erase_sectors(self, addresses: Any) -> None:
        del addresses
        self._require_device()

    def program(
        self,
        image: ImageInspection,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> None:
        with self._lock:
            device = self._require_device()
            path, base = self._validated_bin(image)
            try:
                flash_options = {
                    "target_part": self._target,
                    "base_address": base,
                    "board": self._board,
                    "hpm_flash_cfg": self._flash_cfg,
                    "swd_clock": self._frequency,
                    "verify": False,
                    "reset_after": False,
                }
                if progress_callback is not None:
                    flash_options["progress_callback"] = (
                        lambda percent: progress_callback(float(percent) / 100.0)
                    )
                result = device.flash(
                    str(path),
                    **flash_options,
                )
                if not isinstance(result, Mapping) or result.get("success") is not True:
                    raise FlashError(FlashErrorCode.PROGRAM_FAIL, "HPM ROM programming failed")
            except FlashError:
                raise
            except Exception as error:
                raise FlashError(FlashErrorCode.PROGRAM_FAIL, str(error)) from error

    def verify(
        self,
        image: ImageInspection,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> None:
        with self._lock:
            device = self._require_device()
            path, base = self._validated_bin(image)
            try:
                with path.open("rb") as stream:
                    offset = 0
                    total = path.stat().st_size
                    if progress_callback is not None:
                        progress_callback(0.0)
                    while True:
                        expected = stream.read(self._verify_chunk_size)
                        if not expected:
                            break
                        actual = bytes(device.read_memory(base + offset, len(expected)))
                        if actual != expected:
                            mismatch = next(
                                (index for index, pair in enumerate(zip(expected, actual)) if pair[0] != pair[1]),
                                min(len(expected), len(actual)),
                            )
                            raise FlashError(
                                FlashErrorCode.VERIFY_FAIL,
                                "HPM Flash verification failed",
                                {"address": base + offset + mismatch},
                            )
                        offset += len(expected)
                        if progress_callback is not None:
                            progress_callback(offset / total if total else 1.0)
            except FlashError:
                raise
            except FileNotFoundError:
                raise FlashError(FlashErrorCode.FILE_NOT_FOUND, "firmware snapshot file was not found") from None
            except Exception as error:
                raise FlashError(FlashErrorCode.VERIFY_FAIL, str(error)) from error

    def read_memory(self, address: int, size: int) -> bytes:
        if type(address) is not int or address < 0:
            raise ValueError("address must be a non-negative integer")
        if type(size) is not int or size <= 0:
            raise ValueError("size must be a positive integer")
        self._require_device()
        region = MemoryRegion("hpm-xpi", 0x80000000, 0x10000000, True, True, None)
        if address < region.start or address + size > region.end:
            raise FlashError(
                FlashErrorCode.IMAGE_OUT_OF_RANGE,
                "requested HPM Flash range is outside the XPI memory map",
            )
        bridge = getattr(self._device, "_bridge", None)
        if bridge is None:
            raise FlashError(
                FlashErrorCode.CONNECT_FAIL,
                "HPM device bridge is unavailable",
            )
        from mklink.dump_memory import (
            DumpMemoryUnsupported,
            read_dump_memory_range_once,
        )

        try:
            # HPM XPI Flash is organized in 4 KiB sectors. Keep host requests
            # sector-sized while the dump protocol handles its 2 KiB frames.
            parts = []
            for offset in range(0, size, self.READ_CHUNK_SIZE):
                part_size = min(self.READ_CHUNK_SIZE, size - offset)
                parts.append(
                    read_dump_memory_range_once(
                        bridge, address + offset, part_size, timeout=10.0,
                    )
                )
            return b"".join(parts)
        except DumpMemoryUnsupported:
            # Older probe firmware may expose only the text API.  It is slower,
            # but preserves compatibility for small or diagnostic reads.
            from mklink.memory_access import parse_read_ram_response

            raw = bridge.send_command(
                f"cmd.read_flash(0x{address:08X}, {size})",
                timeout=max(10.0, size / 1200.0),
            )
            data = parse_read_ram_response(raw)
            if len(data) != size:
                raise FlashError(
                    FlashErrorCode.CONNECT_FAIL,
                    f"HPM read_flash returned {len(data)} bytes for {size}",
                )
            return data

    def memory_regions(self) -> Tuple[MemoryRegion, ...]:
        self._require_device()
        return (MemoryRegion("hpm-xpi", 0x80000000, 0x10000000, True, True, None),)

    def reset_run(self, reset_mode: Optional[str] = None) -> None:
        try:
            device = self._require_device()
            mode = self._reset_mode if reset_mode is None else reset_mode
            if mode == "power-cycle":
                if self._reset_voltage_mv not in _POWER_CYCLE_VOLTAGES_MV:
                    raise ValueError(
                        "power-cycle reset requires a validated restore voltage"
                    )
                device.set_power_off()
                try:
                    time.sleep(_POWER_CYCLE_OFF_SECONDS)
                    device.set_power_on(
                        self._reset_voltage_mv,
                        confirm_5v=self._reset_voltage_mv == 5000,
                    )
                except Exception:
                    try:
                        device.set_power_on(
                            self._reset_voltage_mv,
                            confirm_5v=self._reset_voltage_mv == 5000,
                        )
                    except Exception:
                        pass
                    raise
                return
            device.reset()
        except FlashError:
            raise
        except Exception as error:
            raise FlashError(FlashErrorCode.RESET_FAIL, str(error)) from error

    def _require_device(self) -> Any:
        if self._device is None:
            raise FlashError(FlashErrorCode.CONNECT_FAIL, "HPM backend is not connected")
        return self._device

    @staticmethod
    def _validated_bin(image: ImageInspection) -> Tuple[Path, int]:
        if image.format.casefold() != "bin":
            raise FlashError(FlashErrorCode.FILE_FORMAT_ERROR, "HPM ROM API only supports BIN firmware")
        path = Path(image.file_path)
        if not path.is_file():
            raise FlashError(FlashErrorCode.FILE_NOT_FOUND, "firmware snapshot file was not found")
        if image.base_address is None:
            raise FlashError(FlashErrorCode.BIN_ADDRESS_MISSING, "HPM BIN firmware requires a base address")
        from mklink.hpm_config import normalize_hpm_address

        base, _formatted = normalize_hpm_address(image.base_address)
        return path, base


class RoutingFlashBackend:
    """Select the HPM ROM backend or pyOCD once per connection."""

    def __init__(
        self,
        pyocd_factory: Optional[Callable[[], Any]] = None,
        hpm_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._pyocd_factory = pyocd_factory or PyOcdBackend
        self._hpm_factory = hpm_factory or HpmRomBackend
        self._backend: Any = None

    def connect(self, probe: Any, target: str, frequency: int, **kwargs: Any) -> None:
        from mklink.hpm_config import is_hpm_target

        self.disconnect()
        if is_hpm_target(target):
            backend = self._hpm_factory()
        else:
            backend = self._pyocd_factory()
            kwargs.pop("board", None)
            kwargs.pop("hpm_flash_cfg", None)
        backend.connect(probe=probe, target=target, frequency=frequency, **kwargs)
        self._backend = backend

    def disconnect(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            backend.disconnect()

    def __getattr__(self, name: str) -> Any:
        backend = self._backend
        if backend is None:
            raise AttributeError(name)
        return getattr(backend, name)


class PyOcdBackend:
    """Own a single pyOCD session without importing pyOCD at module import time."""

    def __init__(
        self,
        session_factory: Optional[Callable[..., Any]] = None,
        probe_provider: Optional[Callable[[], Any]] = None,
        programmer_factory: Optional[Callable[..., Any]] = None,
        eraser_factory: Optional[Callable[..., Any]] = None,
        power_cycle: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._probe_provider = probe_provider
        self._programmer_factory = programmer_factory
        self._eraser_factory = eraser_factory
        self._power_cycle = power_cycle or _power_cycle_mklink_probe
        self._session: Any = None
        self._reset_mode = "default"
        self._reset_voltage_mv: Optional[int] = None
        self._probe_identifier = ""
        self._security_family = ""
        self._algorithm_reset_required = False
        self._algorithm_reset_done = False
        self._connection_arguments: Optional[dict[str, Any]] = None
        self._py32f030_unlock_transition_pending = False
        self._lock = threading.RLock()

    def connect(
        self,
        probe: Any,
        target: str,
        frequency: int,
        pack: Optional[str] = None,
        custom_flm_paths: Tuple[str, ...] = (),
        custom_flm_digests: Tuple[str, ...] = (),
        custom_flm_regions: Tuple[Tuple[int, int], ...] = (),
        pack_flm_regions: Tuple[Tuple[int, int], ...] = (),
        custom_flm_ram_start: Optional[int] = None,
        custom_flm_ram_size: Optional[int] = None,
        connect_mode: str = "halt",
        reset_mode: str = "default",
        reset_voltage_mv: Optional[int] = None,
        security_family: Optional[str] = None,
        security_flm_path: Optional[str] = None,
        security_flm_digest: Optional[str] = None,
        security_flm_region: Optional[Tuple[int, int]] = None,
    ) -> None:
        with self._lock:
            self.disconnect()
            if not isinstance(frequency, int) or isinstance(frequency, bool) or frequency <= 0:
                raise ValueError("frequency must be a positive integer")
            if reset_voltage_mv is not None and reset_voltage_mv not in _POWER_CYCLE_VOLTAGES_MV:
                raise ValueError("reset_voltage_mv must be 1800, 3300, or 5000")
            if reset_mode != "power-cycle" and reset_voltage_mv is not None:
                raise ValueError(
                    "reset_voltage_mv is only valid for power-cycle reset"
                )
            resolved_pack: Optional[str] = None
            if pack is not None:
                pack_path = Path(pack).expanduser()
                if not pack_path.is_file():
                    raise FlashError(
                        FlashErrorCode.FILE_NOT_FOUND, "CMSIS-Pack file was not found"
                    )
                if pack_path.suffix.lower() != ".pack":
                    raise FlashError(
                        FlashErrorCode.TARGET_NOT_SUPPORTED,
                        "CMSIS-Pack path must name a .pack file",
                    )
                resolved_pack = str(pack_path.resolve())
            if len(custom_flm_paths) != len(custom_flm_digests):
                raise FlashError(
                    FlashErrorCode.PACK_INTEGRITY_ERROR,
                    "custom FLM integrity metadata is invalid",
                )
            if custom_flm_regions and len(custom_flm_regions) != len(custom_flm_paths):
                raise FlashError(
                    FlashErrorCode.PACK_INTEGRITY_ERROR,
                    "custom FLM region metadata is invalid",
                )
            resolved_regions = []
            for raw_region in custom_flm_regions:
                if not isinstance(raw_region, tuple) or len(raw_region) != 2:
                    raise FlashError(
                        FlashErrorCode.PACK_INTEGRITY_ERROR,
                        "custom FLM region metadata is invalid",
                    )
                start, size = raw_region
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or start < 0
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size <= 0
                    or start + size > 0x1_0000_0000
                ):
                    raise FlashError(
                        FlashErrorCode.PACK_INTEGRITY_ERROR,
                        "custom FLM region metadata is invalid",
                    )
                resolved_regions.append((start, size))
            resolved_pack_regions = []
            for raw_region in pack_flm_regions:
                if not isinstance(raw_region, tuple) or len(raw_region) != 2:
                    raise FlashError(
                        FlashErrorCode.PACK_INTEGRITY_ERROR,
                        "Pack FLM region metadata is invalid",
                    )
                start, size = raw_region
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or start < 0
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size <= 0
                    or start + size > 0x1_0000_0000
                ):
                    raise FlashError(
                        FlashErrorCode.PACK_INTEGRITY_ERROR,
                        "Pack FLM region metadata is invalid",
                    )
                resolved_pack_regions.append((start, size))
            resolved_flms = []
            for value, expected_digest in zip(custom_flm_paths, custom_flm_digests):
                flm_path = Path(value).expanduser()
                if not flm_path.is_file():
                    raise FlashError(FlashErrorCode.FILE_NOT_FOUND, "custom FLM file was not found")
                if flm_path.suffix.casefold() != ".flm":
                    raise FlashError(FlashErrorCode.FILE_FORMAT_ERROR, "custom algorithm must be an .flm file")
                digest = str(expected_digest).casefold()
                if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise FlashError(
                        FlashErrorCode.PACK_INTEGRITY_ERROR,
                        "custom FLM integrity metadata is invalid",
                    )
                try:
                    payload = flm_path.read_bytes()
                except FileNotFoundError:
                    raise FlashError(
                        FlashErrorCode.FILE_NOT_FOUND,
                        "custom FLM file was not found",
                    ) from None
                except OSError:
                    raise FlashError(
                        FlashErrorCode.PACK_INTEGRITY_ERROR,
                        "custom FLM integrity check failed",
                    ) from None
                if hashlib.sha256(payload).hexdigest() != digest:
                    raise FlashError(
                        FlashErrorCode.PACK_INTEGRITY_ERROR,
                        "custom FLM integrity check failed",
                    )
                resolved_flms.append(payload)
            security_payload = None
            if security_family is not None:
                from .security import (
                    GD32F30X_OPTION_ADDRESS,
                    GD32F30X_OPTION_FLM_SHA256,
                    GD32F30X_OPTION_SIZE,
                    PY32F030_OPTION_ADDRESS,
                    PY32F030_OPTION_FLM_SHA256,
                    PY32F030_OPTION_SIZE,
                    STM32F1_OPTION_ADDRESS,
                    STM32F1_OPTION_FLM_SHA256,
                    STM32F1_OPTION_SIZE,
                    STM32F4_OPTION_ADDRESS,
                    STM32F4_OPTION_FLM_SHA256,
                    STM32F4_OPTION_SIZE,
                    STM32G4_OPTION_ADDRESS,
                    STM32G4_OPTION_FLM_SHA256,
                    STM32G4_OPTION_SIZE,
                    STM32H7_OPTION_ADDRESS,
                    STM32H7_OPTION_FLM_SHA256,
                    STM32H7_OPTION_SIZE,
                    STM32L0_OPTION_ADDRESS,
                    STM32L0_OPTION_FLM_SHA256,
                    STM32L0_OPTION_SIZE,
                )

                security_whitelist = {
                    "gd32f303xe-spc": (
                        GD32F30X_OPTION_FLM_SHA256,
                        GD32F30X_OPTION_ADDRESS,
                        GD32F30X_OPTION_SIZE,
                    ),
                    "py32f030x8-rdp1": (
                        PY32F030_OPTION_FLM_SHA256,
                        PY32F030_OPTION_ADDRESS,
                        PY32F030_OPTION_SIZE,
                    ),
                    "stm32f103-rdp1": (
                        STM32F1_OPTION_FLM_SHA256,
                        STM32F1_OPTION_ADDRESS,
                        STM32F1_OPTION_SIZE,
                    ),
                    "stm32f413-rdp1": (
                        STM32F4_OPTION_FLM_SHA256,
                        STM32F4_OPTION_ADDRESS,
                        STM32F4_OPTION_SIZE,
                    ),
                    "stm32g474-rdp1": (
                        STM32G4_OPTION_FLM_SHA256,
                        STM32G4_OPTION_ADDRESS,
                        STM32G4_OPTION_SIZE,
                    ),
                    "stm32h743-rdp1": (
                        STM32H7_OPTION_FLM_SHA256,
                        STM32H7_OPTION_ADDRESS,
                        STM32H7_OPTION_SIZE,
                    ),
                    "stm32l010x4-rdp1": (
                        STM32L0_OPTION_FLM_SHA256,
                        STM32L0_OPTION_ADDRESS,
                        STM32L0_OPTION_SIZE,
                    ),
                }
                allowed = security_whitelist.get(security_family)
                if (
                    allowed is None
                    or security_flm_digest != allowed[0]
                    or security_flm_region != (allowed[1], allowed[2])
                    or not security_flm_path
                ):
                    raise FlashError(
                        FlashErrorCode.SECURITY_NOT_SUPPORTED,
                        "security configuration is not in the safety whitelist",
                    )
                security_path = Path(security_flm_path)
                try:
                    security_payload = security_path.read_bytes()
                except OSError:
                    raise FlashError(
                        FlashErrorCode.SECURITY_NOT_SUPPORTED,
                        "security option-byte algorithm is unavailable",
                    ) from None
                if hashlib.sha256(security_payload).hexdigest() != security_flm_digest:
                    raise FlashError(
                        FlashErrorCode.PACK_INTEGRITY_ERROR,
                        "security option-byte algorithm integrity check failed",
                    )
            ram_region = None
            if custom_flm_ram_start is not None or custom_flm_ram_size is not None:
                if (
                    not isinstance(custom_flm_ram_start, int)
                    or isinstance(custom_flm_ram_start, bool)
                    or custom_flm_ram_start < 0
                    or not isinstance(custom_flm_ram_size, int)
                    or isinstance(custom_flm_ram_size, bool)
                    or custom_flm_ram_size <= 0
                    or custom_flm_ram_start + custom_flm_ram_size > 0x1_0000_0000
                ):
                    raise FlashError(
                        FlashErrorCode.PACK_INTEGRITY_ERROR,
                        "custom FLM RAM metadata is invalid",
                    )
                ram_region = (custom_flm_ram_start, custom_flm_ram_size)
            session = None
            try:
                resolved_probe = self._resolve_probe(probe)
                factory = self._session_factory
                if factory is None:
                    Session = import_pyocd_attr("pyocd.core.session", "Session")

                    factory = lambda selected_probe, selected_options: Session(
                        selected_probe, options=selected_options
                    )
                session_target = target
                if security_family == "stm32h743-rdp1":
                    session_target = "stm32h743xx"
                elif (
                    resolved_pack is None
                    and resolved_flms
                    and str(target).casefold().startswith("stm32l010")
                ):
                    # The generic Cortex-M target can attach to L010 but uses
                    # the wrong CoreSight topology, producing false zero reads
                    # and failed RAM algorithm returns. L031 shares the L0
                    # debug/Flash interface; L010-specific geometry and FLMs
                    # are still installed below, and security operations also
                    # verify DEV_ID 0x457 plus the 16 KiB size register.
                    session_target = "stm32l031x6"
                elif resolved_pack is None and resolved_flms:
                    TARGET = import_pyocd_attr("pyocd.target", "TARGET")

                    known = {str(name).casefold() for name in TARGET}
                    known.update(
                        str(getattr(target_type, "PART_NUMBER", "")).casefold()
                        for target_type in TARGET.values()
                    )
                    if str(target).casefold() not in known:
                        session_target = "cortex_m"
                options = {
                    "target_override": session_target,
                    "frequency": frequency,
                    "connect_mode": connect_mode,
                    "auto_unlock": False,
                }
                if reset_mode == "hardware":
                    # Apply the selected reset to algorithm preparation too,
                    # not just to the final reset after verification.
                    options["reset_type"] = "hardware"
                if resolved_pack is not None:
                    options["pack"] = resolved_pack
                session = factory(resolved_probe, options)
                delegate = getattr(session, "delegate", None)
                if resolved_pack is not None:
                    delegate = _PackFlmDelegate(delegate, tuple(resolved_pack_regions))
                if resolved_flms:
                    delegate = _CustomFlmDelegate(
                        tuple(resolved_flms),
                        delegate,
                        ram_region,
                        tuple(resolved_regions),
                    )
                if security_payload is not None:
                    assert security_flm_region is not None
                    delegate = _SecurityFlmDelegate(
                        security_payload,
                        delegate,
                        security_family or "",
                        security_flm_region,
                    )
                session.delegate = _TraceStateDelegate(delegate)
                session.open()
                if resolved_flms or security_payload is not None:
                    _mask_custom_flm_interrupts(session.target)
                self._session = session
                self._reset_mode = reset_mode
                self._reset_voltage_mv = reset_voltage_mv
                self._probe_identifier = str(
                    getattr(resolved_probe, "unique_id", None) or probe or ""
                ).strip()
                self._security_family = security_family or ""
                self._algorithm_reset_required = bool(resolved_flms) or security_family == "stm32f103-rdp1"
                self._algorithm_reset_done = False
                self._connection_arguments = {
                    "probe": probe,
                    "target": target,
                    "frequency": frequency,
                    "pack": pack,
                    "custom_flm_paths": custom_flm_paths,
                    "custom_flm_digests": custom_flm_digests,
                    "custom_flm_regions": custom_flm_regions,
                    "pack_flm_regions": pack_flm_regions,
                    "custom_flm_ram_start": custom_flm_ram_start,
                    "custom_flm_ram_size": custom_flm_ram_size,
                    "connect_mode": connect_mode,
                    "reset_mode": reset_mode,
                    "reset_voltage_mv": reset_voltage_mv,
                    "security_family": security_family,
                    "security_flm_path": security_flm_path,
                    "security_flm_digest": security_flm_digest,
                    "security_flm_region": security_flm_region,
                }
            except FlashError:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                raise
            except Exception as exc:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                raise self._mapped_error(exc, FlashErrorCode.CONNECT_FAIL) from None

    def unlock_security(self) -> str:
        """Disable a whitelisted reversible RDP level; this mass-erases Flash."""

        with self._lock:
            try:
                if self._security_family == "gd32f303xe-spc":
                    changed, _desired = self._write_gd32f303_spc(0xA5)
                    return (
                        "GD32F303 security protection was disabled; main Flash was mass-erased and a power-cycle reset is required"
                        if changed
                        else "GD32F303 security protection was already disabled"
                    )
                if self._security_family == "py32f030x8-rdp1":
                    changed, _desired = self._write_py32f030_rdp(0xAA)
                    if changed:
                        self._verify_py32f030_transition(_desired, unlock=True)
                    return (
                        "PY32F030 read protection was disabled and verified after power cycle; all 64 KiB Flash was verified erased"
                        if changed
                        else "PY32F030 read protection was already disabled"
                    )
                if self._security_family == "stm32l010x4-rdp1":
                    changed, _desired = self._write_stm32l010_rdp(0xAA)
                    return (
                        "STM32L010 read protection was disabled; main Flash, data EEPROM, and backup registers were erased; a power-cycle reset is required"
                        if changed
                        else "STM32L010 read protection was already disabled"
                    )
                if self._security_family == "stm32h743-rdp1":
                    changed, _desired = self._write_stm32h743_rdp(0xAA)
                    return (
                        "STM32H743 read protection was disabled; main Flash was mass-erased and a power-cycle reset is required"
                        if changed
                        else "STM32H743 read protection was already disabled"
                    )
                if self._security_family == "stm32g474-rdp1":
                    changed, _desired = self._write_stm32g474_rdp(0xAA)
                    session = self._require_session()
                    if not self._stm32g474_physical_rdp_matches(session.target, 0xAA):
                        raise RuntimeError(
                            "RDP unlock was not written to the physical option bytes"
                        )
                    return (
                        "STM32G474 read protection disable was written; main Flash was mass-erased and a power-cycle reset is required"
                        if changed
                        else "STM32G474 read protection was already disabled"
                    )
                if self._security_family == "stm32f413-rdp1":
                    changed, desired = self._write_stm32f413_rdp(0xAA)
                    session = self._require_session()
                    session.target.reset_and_halt()
                    actual = self._read_target_bytes(session.target, 0x40023C14, 4)
                    if not self._stm32f413_optcr_matches(actual, desired):
                        raise RuntimeError(
                            "RDP unlock or preserved option bytes did not persist after reset"
                        )
                    return (
                        "STM32F413 read protection was disabled; main Flash was mass-erased"
                        if changed
                        else "STM32F413 read protection was already disabled"
                    )
                if self._security_family != "stm32f103-rdp1":
                    raise FlashError(
                        FlashErrorCode.SECURITY_NOT_SUPPORTED,
                        "security configuration is not in the safety whitelist",
                    )
                changed, desired = self._write_stm32f103_rdp(0xA5, 0x5A)
                session = self._require_session()
                session.target.reset_and_halt()
                actual = self._read_target_bytes(
                    session.target, 0x1FFFF800, 16
                )
                if actual != desired:
                    raise RuntimeError("RDP unlock or preserved option bytes did not persist after reset")
                return (
                    "STM32F103 read protection was disabled; main Flash was mass-erased"
                    if changed
                    else "STM32F103 read protection was already disabled"
                )
            except FlashError:
                raise
            except Exception as exc:
                raise self._mapped_error(exc, FlashErrorCode.UNLOCK_FAIL) from None

    def lock_security(self) -> str:
        """Enable a whitelisted reversible RDP level and reset to activate it."""

        with self._lock:
            try:
                if self._security_family == "gd32f303xe-spc":
                    changed, _desired = self._write_gd32f303_spc(0x00)
                    return (
                        "GD32F303 reversible security protection was written; a power-cycle reset is required to activate it"
                        if changed
                        else "GD32F303 reversible security protection was already active"
                    )
                if self._security_family == "py32f030x8-rdp1":
                    changed, _desired = self._write_py32f030_rdp(0xBB)
                    if changed:
                        self._verify_py32f030_transition(_desired, unlock=False)
                    return (
                        "PY32F030 reversible read protection was activated and verified after power cycle"
                        if changed
                        else "PY32F030 reversible read protection was already active"
                    )
                if self._security_family == "stm32l010x4-rdp1":
                    changed, _desired = self._write_stm32l010_rdp(0xBB)
                    return (
                        "STM32L010 reversible read protection was written; a power-cycle reset is required to activate it"
                        if changed
                        else "STM32L010 reversible read protection was already active"
                    )
                if self._security_family == "stm32h743-rdp1":
                    changed, _desired = self._write_stm32h743_rdp(0xBB)
                    return (
                        "STM32H743 global reversible read protection was written; a power-cycle reset is required to activate it"
                        if changed
                        else "STM32H743 reversible read protection was already active"
                    )
                if self._security_family == "stm32g474-rdp1":
                    changed, _desired = self._write_stm32g474_rdp(0xBB)
                    session = self._require_session()
                    if not self._stm32g474_physical_rdp_matches(session.target, 0xBB):
                        raise RuntimeError(
                            "RDP lock was not written to the physical option bytes"
                        )
                    return (
                        "STM32G474 reversible read protection was written; a power-cycle reset is required to activate it"
                        if changed
                        else "STM32G474 reversible read protection was already active"
                    )
                if self._security_family == "stm32f413-rdp1":
                    changed, desired = self._write_stm32f413_rdp(0xBB)
                    session = self._require_session()
                    session.target.reset_and_halt()
                    optcr = int.from_bytes(
                        self._read_target_bytes(session.target, 0x40023C14, 4),
                        "little",
                    )
                    if not self._stm32f413_optcr_matches(
                        optcr.to_bytes(4, "little"), desired
                    ):
                        raise RuntimeError("RDP lock did not activate after reset")
                    return (
                        "STM32F413 reversible read protection was written and activated after reset"
                        if changed
                        else "STM32F413 reversible read protection was already active"
                    )
                if self._security_family != "stm32f103-rdp1":
                    raise FlashError(
                        FlashErrorCode.SECURITY_NOT_SUPPORTED,
                        "security configuration is not in the safety whitelist",
                    )
                changed, _desired = self._write_stm32f103_rdp(0x00, 0xFF)
                session = self._require_session()
                session.target.reset_and_halt()
                obr = int.from_bytes(
                    self._read_target_bytes(session.target, 0x4002201C, 4),
                    "little",
                )
                if not (obr & 0x2):
                    raise RuntimeError("RDP lock did not activate after reset")
                return (
                    "STM32F103 reversible read protection was written and activated after reset"
                    if changed
                    else "STM32F103 reversible read protection was already active"
                )
            except FlashError:
                raise
            except Exception as exc:
                raise self._mapped_error(exc, FlashErrorCode.LOCK_FAIL) from None

    _STM32L010_OPTION_ADDRESS = 0x1FF80000
    _STM32L010_OPTION_SIZE = 20
    _STM32L010_STATUS_ERROR_MASK = (
        (1 << 17)
        | (1 << 16)
        | (1 << 13)
        | (1 << 11)
        | (1 << 10)
        | (1 << 9)
        | (1 << 8)
    )
    def _write_stm32l010_rdp(self, value: int) -> Tuple[bool, bytes]:
        if value not in {0xAA, 0xBB}:
            raise RuntimeError("invalid STM32L010 reversible RDP value")
        session = self._require_session()
        target = session.target
        device_id = int.from_bytes(
            self._read_target_bytes(target, 0x40015800, 4), "little"
        ) & 0xFFF
        if device_id != 0x457:
            raise RuntimeError("connected target identity does not match STM32L010 category 1")
        flash_size_kib = int.from_bytes(
            self._read_target_bytes(target, 0x1FF8007C, 2), "little"
        )
        if flash_size_kib != 16:
            raise RuntimeError("connected target Flash size does not match STM32L010x4")
        region = self._flash_region_for_address(target, self._STM32L010_OPTION_ADDRESS)
        if (
            region is None
            or str(getattr(region, "name", "")) != "mklink_security_option_bytes"
            or int(region.start) != self._STM32L010_OPTION_ADDRESS
            or int(region.length) != self._STM32L010_OPTION_SIZE
            or getattr(region, "flash", None) is None
        ):
            raise RuntimeError("validated STM32L010 option-byte algorithm is unavailable")

        current = self._read_target_bytes(
            target, self._STM32L010_OPTION_ADDRESS, self._STM32L010_OPTION_SIZE
        )
        if len(current) != self._STM32L010_OPTION_SIZE:
            raise RuntimeError("STM32L010 option-byte snapshot has an unexpected length")
        logical = int.from_bytes(
            self._read_target_bytes(target, 0x4002201C, 4), "little"
        )
        physical_rdp = current[0]
        if current[2] != (physical_rdp ^ 0xFF):
            raise RuntimeError("STM32L010 RDP option-byte complement is invalid")
        if (logical & 0xFF) != physical_rdp:
            raise RuntimeError("STM32L010 logical and physical RDP state is inconsistent")
        if physical_rdp == 0xCC:
            raise RuntimeError("STM32L010 RDP level 2 is irreversible and unsupported")
        if value == 0xAA and physical_rdp == 0xAA:
            return False, current
        if value == 0xBB and physical_rdp not in {0xAA, 0xCC}:
            return False, current

        desired = bytearray(current)
        # RM0451 requires this exact first option word. In particular,
        # 0x015500AA is the only documented Level-1-to-Level-0 request that
        # triggers the mandatory mass erase. Unused bits must not be copied
        # from a prior physical readback.
        desired_word = 0x015500AA if value == 0xAA else 0x014400BB
        desired[:4] = desired_word.to_bytes(4, "little")
        desired_bytes = bytes(desired)
        self._program_stm32l010_option_word(target, desired_word)
        actual = self._read_target_bytes(
            target, self._STM32L010_OPTION_ADDRESS, self._STM32L010_OPTION_SIZE
        )
        if not self._stm32l010_options_match(actual, desired_bytes):
            raise RuntimeError("STM32L010 option fields did not persist")
        return True, desired_bytes

    @classmethod
    def _program_stm32l010_option_word(cls, target: Any, desired_word: int) -> None:
        if desired_word not in {0x015500AA, 0x014400BB}:
            raise RuntimeError("invalid STM32L010 reversible RDP option word")
        write32 = getattr(target, "write32", None)
        if not callable(write32):
            raise RuntimeError("target does not support validated 32-bit option writes")
        status = int.from_bytes(
            cls._read_target_bytes(target, 0x40022018, 4), "little"
        )
        if status & 1:
            raise RuntimeError("STM32L010 Flash interface is busy before option change")
        if status & cls._STM32L010_STATUS_ERROR_MASK:
            raise RuntimeError("STM32L010 Flash status contains a pre-existing error")

        control = int.from_bytes(
            cls._read_target_bytes(target, 0x40022004, 4), "little"
        )
        if control & 1:
            write32(0x4002200C, 0x89ABCDEF)
            write32(0x4002200C, 0x02030405)
        control = int.from_bytes(
            cls._read_target_bytes(target, 0x40022004, 4), "little"
        )
        if control & 1:
            raise RuntimeError("STM32L010 FLASH_PECR did not unlock")
        if control & (1 << 2):
            write32(0x40022014, 0xFBEAD9C8)
            write32(0x40022014, 0x24252627)
        control = int.from_bytes(
            cls._read_target_bytes(target, 0x40022004, 4), "little"
        )
        if control & (1 << 2):
            raise RuntimeError("STM32L010 option bytes did not unlock")

        write32(0x40022018, 1 << 1)
        write32(cls._STM32L010_OPTION_ADDRESS, desired_word)
        flush = getattr(target, "flush", None)
        if callable(flush):
            flush()
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            status = int.from_bytes(
                cls._read_target_bytes(target, 0x40022018, 4), "little"
            )
            if not (status & 1) and (status & (1 << 1)):
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("STM32L010 option operation did not finish")
        if status & cls._STM32L010_STATUS_ERROR_MASK:
            raise RuntimeError("STM32L010 option operation reported an error")

        control = int.from_bytes(
            cls._read_target_bytes(target, 0x40022004, 4), "little"
        )
        write32(0x40022004, control | (1 << 2) | 1)
        if callable(flush):
            flush()

    @staticmethod
    def _stm32l010_options_match(actual: bytes, desired: bytes) -> bool:
        """Compare every defined L010x4 option field, ignoring reserved bytes."""

        if len(actual) != 20 or len(desired) != 20:
            return False
        # Bytes 1 and 3 of the first physical option word are reserved and
        # may read back differently after the vendor FLM normalises them.
        defined = (0, 2, *range(4, 20))
        return all(actual[index] == desired[index] for index in defined)

    _STM32H743_OPTSR_CUR_REGISTERS = (0x5200201C, 0x5200211C)
    _STM32H743_OPTSR_PRG_REGISTERS = (0x52002020, 0x52002120)
    _STM32H743_FLASH_KEY_REGISTERS = (0x52002004, 0x52002104)
    _STM32H743_FLASH_CR_REGISTERS = (0x5200200C, 0x5200210C)
    _STM32H743_FLASH_SR_REGISTERS = (0x52002010, 0x52002110)
    _STM32H743_RDP_MASK = 0x0000FF00
    _STM32H743_OPTCHANGEERR = 1 << 30

    @classmethod
    def _stm32h743_option_words(
        cls, target: Any, registers: Tuple[int, int]
    ) -> Tuple[int, int]:
        return tuple(
            int.from_bytes(cls._read_target_bytes(target, address, 4), "little")
            for address in registers
        )  # type: ignore[return-value]

    def _write_stm32h743_rdp(self, value: int) -> Tuple[bool, Tuple[int, int]]:
        if value not in {0xAA, 0xBB}:
            raise RuntimeError("invalid STM32H743 reversible RDP value")
        session = self._require_session()
        target = session.target
        device_id = int.from_bytes(
            self._read_target_bytes(target, 0x5C001000, 4), "little"
        ) & 0xFFF
        if device_id != 0x450:
            raise RuntimeError("connected target identity does not match STM32H743")
        flash_size_kib = int.from_bytes(
            self._read_target_bytes(target, 0x1FF1E880, 2), "little"
        )
        if flash_size_kib != 2048:
            raise RuntimeError("connected target Flash size does not match STM32H743xI")

        current = self._stm32h743_option_words(
            target, self._STM32H743_OPTSR_CUR_REGISTERS
        )
        programmed = self._stm32h743_option_words(
            target, self._STM32H743_OPTSR_PRG_REGISTERS
        )
        current_rdp = tuple((word >> 8) & 0xFF for word in current)
        programmed_rdp = tuple((word >> 8) & 0xFF for word in programmed)
        if 0xCC in current_rdp or 0xCC in programmed_rdp:
            raise RuntimeError("STM32H743 RDP level 2 is irreversible and unsupported")
        if len(set(current_rdp)) != 1 or current_rdp != programmed_rdp:
            raise RuntimeError("STM32H743 Flash banks have inconsistent RDP state")
        if value == 0xAA and current_rdp[0] == 0xAA:
            return False, programmed
        if value == 0xBB and current_rdp[0] not in {0xAA, 0xCC}:
            return False, programmed

        desired = tuple(
            (word & ~self._STM32H743_RDP_MASK) | (value << 8)
            for word in programmed
        )
        self._program_stm32h743_option_registers(target, desired)
        actual = self._stm32h743_option_words(
            target, self._STM32H743_OPTSR_CUR_REGISTERS
        )
        for before, after in zip(current, actual):
            if after & self._STM32H743_OPTCHANGEERR:
                raise RuntimeError("STM32H743 option change reported an error")
            if ((after >> 8) & 0xFF) != value:
                raise RuntimeError("STM32H743 RDP change did not persist")
            preserved_mask = ~(
                self._STM32H743_RDP_MASK | self._STM32H743_OPTCHANGEERR
            ) & 0xFFFFFFFF
            if after & preserved_mask != before & preserved_mask:
                raise RuntimeError("STM32H743 non-RDP option fields changed unexpectedly")
        return True, desired

    @classmethod
    def _program_stm32h743_option_registers(
        cls, target: Any, desired: Tuple[int, int]
    ) -> None:
        if (
            len(desired) != 2
            or any(((word >> 8) & 0xFF) not in {0xAA, 0xBB} for word in desired)
        ):
            raise RuntimeError("invalid STM32H743 reversible RDP value")
        write32 = getattr(target, "write32", None)
        if not callable(write32):
            raise RuntimeError("target does not support validated 32-bit option writes")

        for key_register, control_register, status_register in zip(
            cls._STM32H743_FLASH_KEY_REGISTERS,
            cls._STM32H743_FLASH_CR_REGISTERS,
            cls._STM32H743_FLASH_SR_REGISTERS,
        ):
            if int.from_bytes(
                cls._read_target_bytes(target, status_register, 4), "little"
            ) & 1:
                raise RuntimeError("STM32H743 Flash bank is busy before option change")
            control = int.from_bytes(
                cls._read_target_bytes(target, control_register, 4), "little"
            )
            if control & 1:
                write32(key_register, 0x45670123)
                write32(key_register, 0xCDEF89AB)

        option_control = int.from_bytes(
            cls._read_target_bytes(target, 0x52002018, 4), "little"
        )
        if option_control & 1:
            write32(0x52002008, 0x08192A3B)
            write32(0x52002008, 0x4C5D6E7F)
        option_control = int.from_bytes(
            cls._read_target_bytes(target, 0x52002018, 4), "little"
        )
        if option_control & 1:
            raise RuntimeError("STM32H743 option registers did not unlock")

        for address, word in zip(cls._STM32H743_OPTSR_PRG_REGISTERS, desired):
            write32(address, word)
        flush = getattr(target, "flush", None)
        if callable(flush):
            flush()
        if cls._stm32h743_option_words(
            target, cls._STM32H743_OPTSR_PRG_REGISTERS
        ) != desired:
            raise RuntimeError("STM32H743 option programming registers rejected RDP")

        write32(0x52002018, 1 << 1)  # FLASH_OPTCR.OPTSTART
        if callable(flush):
            flush()
        deadline = time.monotonic() + 180.0
        while time.monotonic() < deadline:
            status = cls._stm32h743_option_words(
                target, cls._STM32H743_FLASH_SR_REGISTERS
            )
            option_control = int.from_bytes(
                cls._read_target_bytes(target, 0x52002018, 4), "little"
            )
            if not any(word & 1 for word in status) and not (option_control & (1 << 1)):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("STM32H743 option operation did not finish")

        current = cls._stm32h743_option_words(
            target, cls._STM32H743_OPTSR_CUR_REGISTERS
        )
        if any(word & cls._STM32H743_OPTCHANGEERR for word in current):
            raise RuntimeError("STM32H743 option change reported an error")

        option_control = int.from_bytes(
            cls._read_target_bytes(target, 0x52002018, 4), "little"
        )
        write32(0x52002018, option_control | 1)
        for control_register in cls._STM32H743_FLASH_CR_REGISTERS:
            control = int.from_bytes(
                cls._read_target_bytes(target, control_register, 4), "little"
            )
            write32(control_register, control | 1)
        if callable(flush):
            flush()

    _STM32G474_OPTION_REGISTERS = (
        0x40022020,  # OPTR
        0x40022024,  # PCROP1SR
        0x40022028,  # PCROP1ER
        0x4002202C,  # WRP1AR
        0x40022030,  # WRP1BR
        0x40022070,  # SEC1R
        0x40022044,  # PCROP2SR
        0x40022048,  # PCROP2ER
        0x4002204C,  # WRP2AR
        0x40022050,  # WRP2BR
        0x40022074,  # SEC2R
    )
    _STM32G474_OPTION_MASKS = (
        0xFFFFFFFF,
        0x0000FFFF,
        0x8000FFFF,
        0x00FF00FF,
        0x00FF00FF,
        0x000101FF,
        0x0000FFFF,
        0x0000FFFF,
        0x00FF00FF,
        0x00FF00FF,
        0x000100FF,
    )

    @classmethod
    def _stm32g474_option_snapshot(cls, target: Any) -> bytes:
        return b"".join(
            cls._read_target_bytes(target, address, 4)
            for address in cls._STM32G474_OPTION_REGISTERS
        )

    def _write_stm32g474_rdp(self, value: int) -> Tuple[bool, bytes]:
        if value not in {0xAA, 0xBB}:
            raise RuntimeError("invalid STM32G474 reversible RDP value")
        session = self._require_session()
        target = session.target
        device_id = int.from_bytes(
            self._read_target_bytes(target, 0xE0042000, 4), "little"
        ) & 0xFFF
        if device_id != 0x469:
            raise RuntimeError("connected target identity does not match STM32G474")
        flash_size_kib = int.from_bytes(
            self._read_target_bytes(target, 0x1FFF75E0, 2), "little"
        )
        if flash_size_kib != 512:
            raise RuntimeError("connected target Flash size does not match STM32G474xE")
        address = 0x1FFF7800
        current = bytearray(self._stm32g474_option_snapshot(target))
        if len(current) != 44:
            raise RuntimeError("STM32G474 option register snapshot has an unexpected length")
        if current[0] == 0xCC:
            raise RuntimeError("STM32G474 RDP level 2 is irreversible and unsupported")
        if value == 0xAA and current[0] == 0xAA:
            return False, bytes(current)
        if value == 0xBB and current[0] not in {0xAA, 0xCC}:
            return False, bytes(current)
        desired = bytearray(current)
        desired[0] = value
        desired_bytes = bytes(desired)
        region = self._flash_region_for_address(target, address)
        if (
            region is None
            or str(getattr(region, "name", "")) != "mklink_security_option_bytes"
            or int(region.start) != address
            or int(region.length) != 84
        ):
            raise RuntimeError("validated option-byte Flash region is unavailable")
        if getattr(region, "flash", None) is None:
            raise RuntimeError("option-byte Flash algorithm is unavailable")
        self._program_stm32g474_option_registers(target, desired_bytes)
        return True, desired_bytes

    @classmethod
    def _program_stm32g474_option_registers(
        cls, target: Any, desired: bytes
    ) -> None:
        if len(desired) != 44:
            raise RuntimeError("STM32G474 option payload has an unexpected length")
        if desired[0] not in {0xAA, 0xBB}:
            raise RuntimeError("invalid STM32G474 reversible RDP value")
        write32 = getattr(target, "write32", None)
        if not callable(write32):
            raise RuntimeError("target does not support validated 32-bit option writes")

        # This is the host-driven equivalent of the pinned STM32G4 option FLM.
        # It does not execute target RAM code, which remains unavailable while
        # RDP1 is active on some STM32G4 applications. Only reversible AA/BB
        # payloads reach this method, and every non-RDP field is preserved.
        write32(0x40022008, 0x45670123)  # FLASH_KEY1
        write32(0x40022008, 0xCDEF89AB)  # FLASH_KEY2
        write32(0x4002200C, 0x08192A3B)  # FLASH_OPTKEY1
        write32(0x4002200C, 0x4C5D6E7F)  # FLASH_OPTKEY2
        write32(0x40022010, 0x000143FA)  # clear documented error flags
        for address, mask, offset in zip(
            cls._STM32G474_OPTION_REGISTERS,
            cls._STM32G474_OPTION_MASKS,
            range(0, len(desired), 4),
        ):
            value = int.from_bytes(desired[offset : offset + 4], "little") & mask
            write32(address, value)
        write32(0x40022014, 1 << 17)  # FLASH_CR.OPTSTRT
        flush = getattr(target, "flush", None)
        if callable(flush):
            flush()

        deadline = time.monotonic() + 60.0
        last_read_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                flash_sr = int.from_bytes(
                    cls._read_target_bytes(target, 0x40022010, 4), "little"
                )
                last_read_error = None
                if not (flash_sr & (1 << 16)):
                    break
            except Exception as exc:
                last_read_error = exc
            time.sleep(0.05)
        else:
            if last_read_error is not None:
                raise RuntimeError(
                    "STM32G474 option operation status became inaccessible"
                ) from last_read_error
            raise RuntimeError("STM32G474 option operation did not finish")

        # Relock both option and main Flash control without requesting option
        # loading. The job's validated 3.3 V power cycle performs the load.
        write32(0x40022014, 1 << 31)
        write32(0x40022014, 1 << 30)
        if callable(flush):
            flush()

    @classmethod
    def _stm32g474_options_match(cls, actual: bytes, desired: bytes) -> bool:
        if len(actual) != 44 or len(desired) != 44:
            return False
        for offset, mask in enumerate(cls._STM32G474_OPTION_MASKS):
            start = offset * 4
            actual_word = int.from_bytes(actual[start : start + 4], "little")
            desired_word = int.from_bytes(desired[start : start + 4], "little")
            if actual_word & mask != desired_word & mask:
                return False
        return True

    @classmethod
    def _stm32g474_physical_rdp_matches(cls, target: Any, value: int) -> bool:
        physical = cls._read_target_bytes(target, 0x1FFF7800, 8)
        return (
            len(physical) == 8
            and physical[0] == value
            and physical[4] == (value ^ 0xFF)
        )

    def _write_stm32f413_rdp(self, value: int) -> Tuple[bool, bytes]:
        if value not in {0xAA, 0xBB}:
            raise RuntimeError("invalid STM32F413 reversible RDP value")
        session = self._require_session()
        target = session.target
        device_id = int.from_bytes(
            self._read_target_bytes(target, 0xE0042000, 4), "little"
        ) & 0xFFF
        if device_id != 0x463:
            raise RuntimeError(
                "connected target identity does not match STM32F413/423"
            )
        address = 0x1FFFC000
        current = bytearray(self._read_target_bytes(target, 0x40023C14, 4))
        if len(current) != 4:
            raise RuntimeError("FLASH_OPTCR read returned an unexpected length")
        if current[1] == 0xCC:
            raise RuntimeError("STM32F413 RDP level 2 is irreversible and unsupported")
        if value == 0xAA and current[1] == 0xAA:
            return False, bytes(current)
        if value == 0xBB and current[1] not in {0xAA, 0xCC}:
            return False, bytes(current)
        desired_bytes = bytearray(current)
        desired_bytes[1] = value
        desired = bytes(desired_bytes)
        region = self._flash_region_for_address(target, address)
        if (
            region is None
            or str(getattr(region, "name", "")) != "mklink_security_option_bytes"
            or int(region.start) != address
            or int(region.length) != 4
        ):
            raise RuntimeError("validated option-byte Flash region is unavailable")
        flash = getattr(region, "flash", None)
        if flash is None:
            raise RuntimeError("option-byte Flash algorithm is unavailable")
        flash.init(flash.Operation.ERASE)
        try:
            flash.erase_sector(address)
        finally:
            flash.uninit()
        flash.init(flash.Operation.PROGRAM)
        try:
            flash.program_page(address, desired)
        except Exception as exc:
            # Changing F4 RDP can immediately reset/release the core. Some FLM
            # runners then time out or fail while reading IPSR even though the
            # option operation is still running. Never reset during the implicit
            # mass erase: wait for FLASH_SR.BSY to clear, then let the caller
            # reset and authoritatively validate every programmed OPTCR field.
            self._wait_stm32f413_option_operation(target, exc)
        finally:
            try:
                flash.uninit()
            except Exception:
                pass
        return True, desired

    @classmethod
    def _wait_stm32f413_option_operation(
        cls, target: Any, original_error: Exception
    ) -> None:
        deadline = time.monotonic() + 60.0
        last_read_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                flash_sr = int.from_bytes(
                    cls._read_target_bytes(target, 0x40023C0C, 4), "little"
                )
                last_read_error = None
                if not (flash_sr & (1 << 16)):
                    return
            except Exception as exc:
                last_read_error = exc
            time.sleep(0.05)
        if last_read_error is not None:
            raise original_error from last_read_error
        raise RuntimeError("STM32F413 option operation did not finish") from original_error

    @staticmethod
    def _stm32f413_optcr_matches(actual: bytes, desired: bytes) -> bool:
        """Compare the fields programmed by the STM32F413 option FLM."""

        if len(actual) != 4 or len(desired) != 4:
            return False
        # The FLM masks OPTSTRT/OPTLOCK (bits 1:0) and reserved bits 29:28,
        # then writes the complete FLASH_OPTCR value. Compare every other bit,
        # including USER, RDP, and all write-protection fields.
        mask = 0xCFFFFFFC
        return (
            int.from_bytes(actual, "little") & mask
            == int.from_bytes(desired, "little") & mask
        )

    _PY32F030_OPTION_ADDRESS = 0x1FFF0E80
    _PY32F030_OPTION_SIZE = 16
    _PY32F030_DBG_ID = 0x60001000

    def _verify_py32f030_transition(self, desired: bytes, *, unlock: bool) -> None:
        """Finish the security transition before a job may proceed to programming."""
        if self._connection_arguments is None or self._reset_voltage_mv not in _POWER_CYCLE_VOLTAGES_MV:
            raise RuntimeError("PY32F030 security verification requires reconnect and power-cycle configuration")
        reconnect = dict(self._connection_arguments)
        reconnect["connect_mode"] = "halt" if unlock else "attach"
        probe_identifier = self._probe_identifier
        voltage = self._reset_voltage_mv
        # Release the existing debug session before removing target power.
        self._require_session().options["resume_on_disconnect"] = False
        self.disconnect()
        self._power_cycle(probe_identifier, voltage)
        time.sleep(1.0)  # Allow option loading after the rail is restored.
        self.connect(**reconnect)
        target = self._require_session().target
        dbg_id = int.from_bytes(self._read_target_bytes(target, 0x40015800, 4), "little")
        factory_id = int.from_bytes(self._read_target_bytes(target, 0x1FFF0FF8, 4), "little")
        uid = self._read_target_bytes(target, 0x1FFF0E00, 16)
        if dbg_id != self._PY32F030_DBG_ID or factory_id != dbg_id or len(uid) != 16 or uid[12] != 0x78:
            raise RuntimeError("PY32F030 identity verification failed after power cycle")
        actual = self._read_target_bytes(target, self._PY32F030_OPTION_ADDRESS, self._PY32F030_OPTION_SIZE)
        logical = int.from_bytes(self._read_target_bytes(target, 0x40022020, 4), "little") & 0xFF
        if not self._py32f030_option_words_valid(actual) or actual != desired or logical != desired[0]:
            raise RuntimeError("PY32F030 RDP or preserved option bytes did not persist after power cycle")
        if unlock:
            for address in range(0x08000000, 0x08010000, 4096):
                if self._read_target_bytes(target, address, 4096) != b"\xFF" * 4096:
                    raise RuntimeError("PY32F030 main Flash was not completely erased by RDP unlock")
        self._py32f030_unlock_transition_pending = False

    @staticmethod
    def _py32f030_option_words_valid(payload: bytes) -> bool:
        if len(payload) != PyOcdBackend._PY32F030_OPTION_SIZE:
            return False
        for index in range(0, len(payload), 4):
            word = payload[index:index + 4]
            if word == b"\xFF" * 4:
                continue
            if word[0] ^ word[2] != 0xFF or word[1] ^ word[3] != 0xFF:
                return False
        return True

    def _write_py32f030_rdp(self, value: int) -> Tuple[bool, bytes]:
        if value not in {0xAA, 0xBB}:
            raise RuntimeError("invalid PY32F030 reversible RDP value")
        session = self._require_session()
        target = session.target
        dbg_id = int.from_bytes(
            self._read_target_bytes(target, 0x40015800, 4), "little"
        )
        factory_id = int.from_bytes(
            self._read_target_bytes(target, 0x1FFF0FF8, 4), "little"
        )
        if dbg_id != self._PY32F030_DBG_ID or factory_id != dbg_id:
            raise RuntimeError("connected target identity does not match PY32F030x8")
        uid = self._read_target_bytes(target, 0x1FFF0E00, 16)
        if uid[12] != 0x78 or uid in {b"\x00" * 16, b"\xFF" * 16}:
            raise RuntimeError("connected target UID layout does not match PY32F030")
        main_regions = [
            region
            for region in getattr(target, "memory_map", ())
            if bool(getattr(region, "is_flash", False))
            and int(getattr(region, "start", -1)) == 0x08000000
            and int(getattr(region, "length", -1)) == 0x10000
        ]
        if len(main_regions) != 1:
            raise RuntimeError("connected target Flash geometry does not match PY32F030x8")

        status = int.from_bytes(
            self._read_target_bytes(target, 0x40022010, 4), "little"
        )
        if status & ((1 << 16) | (1 << 15) | (1 << 4)):
            raise RuntimeError("PY32F030 Flash status is busy or contains an error")
        current = bytes(
            self._read_target_bytes(
                target,
                self._PY32F030_OPTION_ADDRESS,
                self._PY32F030_OPTION_SIZE,
            )
        )
        if not self._py32f030_option_words_valid(current):
            raise RuntimeError("PY32F030 option-byte complement validation failed")
        physical_rdp = current[0]
        if current[2] != (physical_rdp ^ 0xFF):
            raise RuntimeError("PY32F030 RDP option-byte complement is invalid")
        logical_rdp = int.from_bytes(
            self._read_target_bytes(target, 0x40022020, 4), "little"
        ) & 0xFF
        if logical_rdp != physical_rdp:
            raise RuntimeError("PY32F030 logical and physical RDP state is inconsistent")
        if value == 0xAA and physical_rdp == 0xAA:
            return False, current
        if value == 0xBB and physical_rdp != 0xAA:
            return False, current

        desired = bytearray(current)
        desired[0] = value
        desired[2] = value ^ 0xFF
        desired_bytes = bytes(desired)
        region = self._flash_region_for_address(
            target, self._PY32F030_OPTION_ADDRESS
        )
        if (
            region is None
            or str(getattr(region, "name", "")) != "mklink_security_option_bytes"
            or int(region.start) != self._PY32F030_OPTION_ADDRESS
            or int(region.length) != self._PY32F030_OPTION_SIZE
        ):
            raise RuntimeError("validated PY32F030 option-byte Flash region is unavailable")
        flash = getattr(region, "flash", None)
        if flash is None:
            raise RuntimeError("PY32F030 option-byte Flash algorithm is unavailable")
        flash.init(flash.Operation.ERASE)
        try:
            flash.erase_sector(self._PY32F030_OPTION_ADDRESS)
        finally:
            flash.uninit()
        flash.init(flash.Operation.PROGRAM)
        transition_interrupted = False
        try:
            try:
                flash.program_page(self._PY32F030_OPTION_ADDRESS, desired_bytes)
            except Exception as exc:
                text = str(exc).casefold()
                transition_interrupted = bool(
                    value == 0xAA
                    and physical_rdp != 0xAA
                    and "target was not halted as expected after calling flash algorithm routine"
                    in text
                    and "ipsr=3" in text
                )
                if not transition_interrupted:
                    raise
        finally:
            try:
                flash.uninit()
            except Exception:
                if not transition_interrupted:
                    raise
        if transition_interrupted:
            # RDP1 -> RDP0 starts a hardware mass erase and resets the core
            # before this FLM can return normally.  Defer verification until
            # the mandatory power cycle has re-established a fresh SWD link.
            self._py32f030_unlock_transition_pending = True
            return True, desired_bytes
        actual = bytes(
            self._read_target_bytes(
                target,
                self._PY32F030_OPTION_ADDRESS,
                self._PY32F030_OPTION_SIZE,
            )
        )
        if (
            not self._py32f030_option_words_valid(actual)
            or actual[0] != value
            or actual[2] != (value ^ 0xFF)
            or actual[1] != current[1]
            or actual[3:] != desired_bytes[3:]
        ):
            raise RuntimeError("PY32F030 RDP or preserved option bytes did not persist")
        return True, actual

    _GD32F303_OPTION_ADDRESS = 0x1FFFF800
    _GD32F303_OPTION_SIZE = 16

    @staticmethod
    def _gd32f303_option_pairs_valid(payload: bytes) -> bool:
        if len(payload) != PyOcdBackend._GD32F303_OPTION_SIZE:
            return False
        return all(
            (payload[index] == 0xFF and payload[index + 1] == 0xFF)
            or (payload[index] ^ payload[index + 1]) == 0xFF
            for index in range(0, len(payload), 2)
        )

    def _write_gd32f303_spc(self, value: int) -> Tuple[bool, bytes]:
        if value not in {0xA5, 0x00}:
            raise RuntimeError("invalid GD32F303 reversible SPC value")
        session = self._require_session()
        target = session.target
        dbg_id = int.from_bytes(
            self._read_target_bytes(target, 0xE0042000, 4), "little"
        )
        if dbg_id & 0xFFF != 0x414:
            raise RuntimeError("connected target identity does not match GD32F30x")
        obstat = int.from_bytes(
            self._read_target_bytes(target, 0x4002201C, 4), "little"
        )
        if obstat & 0x1:
            raise RuntimeError("GD32F303 option-byte shadow reports a complement error")
        current = bytes(
            self._read_target_bytes(
                target,
                self._GD32F303_OPTION_ADDRESS,
                self._GD32F303_OPTION_SIZE,
            )
        )
        if not self._gd32f303_option_pairs_valid(current):
            raise RuntimeError("GD32F303 option-byte complement validation failed")
        protected = current[:2] != b"\xA5\x5A"
        if protected != bool(obstat & 0x2):
            raise RuntimeError("GD32F303 logical and physical SPC state is inconsistent")
        try:
            density = int.from_bytes(
                self._read_target_bytes(target, 0x1FFFF7E0, 4), "little"
            )
        except Exception:
            # GD32F30x security protection can block the factory signature
            # range as well as main Flash.  In that state the family ID,
            # pinned option algorithm, and independently matching physical
            # SPC/shadow state remain authoritative.  An unprotected target
            # must always expose and match its immutable density signature.
            if not protected:
                raise RuntimeError(
                    "GD32F303 density signature is unavailable while unprotected"
                ) from None
        else:
            if (density & 0xFFFF, density >> 16) != (512, 64):
                raise RuntimeError(
                    "connected target density does not match GD32F303xE"
                )
        if value == 0xA5 and not protected:
            return False, current
        if value == 0x00 and protected:
            return False, current

        desired = bytearray(current)
        desired[:2] = bytes((value, value ^ 0xFF))
        region = self._flash_region_for_address(
            target, self._GD32F303_OPTION_ADDRESS
        )
        if (
            region is None
            or str(getattr(region, "name", "")) != "mklink_security_option_bytes"
            or int(region.start) != self._GD32F303_OPTION_ADDRESS
            or int(region.length) != self._GD32F303_OPTION_SIZE
        ):
            raise RuntimeError("validated GD32F303 option-byte Flash region is unavailable")
        flash = getattr(region, "flash", None)
        if flash is None:
            raise RuntimeError("GD32F303 option-byte Flash algorithm is unavailable")
        try:
            flash.init(flash.Operation.ERASE)
            flash.erase_sector(self._GD32F303_OPTION_ADDRESS)
            flash.uninit()
            flash.init(flash.Operation.PROGRAM)
            flash.program_page(self._GD32F303_OPTION_ADDRESS, bytes(desired))
        finally:
            # Main-Flash programming reuses the same target RAM. uninit()
            # alone leaves pyOCD believing the option FLM is still loaded,
            # so unlock -> program -> lock would execute overwritten code.
            flash.cleanup()
        actual = bytes(
            self._read_target_bytes(
                target,
                self._GD32F303_OPTION_ADDRESS,
                self._GD32F303_OPTION_SIZE,
            )
        )
        if (
            not self._gd32f303_option_pairs_valid(actual)
            or actual[:2] != bytes((value, value ^ 0xFF))
            or actual[2::2] != desired[2::2]
        ):
            raise RuntimeError("GD32F303 SPC or preserved option bytes did not persist")
        return True, actual

    def _write_stm32f103_rdp(self, value: int, complement: int) -> Tuple[bool, bytes]:
        session = self._require_session()
        address = 0x1FFFF800
        current, directly_readable = self._stm32f103_option_snapshot(session.target)
        current = bytearray(current)
        if len(current) != 16:
            raise RuntimeError("option-byte read returned an unexpected length")
        if any((current[index] ^ current[index + 1]) != 0xFF for index in range(0, 16, 2)):
            raise RuntimeError("option-byte complement validation failed")
        if current[:2] == bytes((value, complement)):
            return False, bytes(current)
        desired = bytes((value, complement)) + bytes(current[2:])
        region = self._flash_region_for_address(session.target, address)
        if (
            region is None
            or str(getattr(region, "name", "")) != "mklink_security_option_bytes"
            or int(region.start) != address
            or int(region.length) != 16
        ):
            raise RuntimeError("validated option-byte Flash region is unavailable")
        flash = getattr(region, "flash", None)
        if flash is None:
            raise RuntimeError("option-byte Flash algorithm is unavailable")
        # A standalone lock may follow read-only verification of a running
        # RTOS. It needs the same privileged MSP context as main-Flash work,
        # before erasing any option bytes.
        self._prepare_algorithm_execution(session.target)
        try:
            flash.init(flash.Operation.ERASE)
            flash.erase_sector(address)
            flash.uninit()
            flash.init(flash.Operation.PROGRAM)
            flash.program_page(address, desired)
        finally:
            # Main and option algorithms share RAM across unlock/program/lock.
            flash.cleanup()
        if directly_readable:
            actual = self._read_target_bytes(session.target, address, 16)
            if actual != desired:
                raise RuntimeError("option-byte write verification failed")
        return True, desired

    @classmethod
    def _stm32f103_option_snapshot(cls, target: Any) -> Tuple[bytes, bool]:
        """Read option bytes, or reconstruct them from safe shadow registers under RDP."""

        address = 0x1FFFF800
        try:
            return cls._read_target_bytes(target, address, 16), True
        except Exception as original_error:
            try:
                obr = int.from_bytes(cls._read_target_bytes(target, 0x4002201C, 4), "little")
                wrpr = int.from_bytes(cls._read_target_bytes(target, 0x40022020, 4), "little")
            except Exception:
                raise original_error
            if not (obr & 0x2):
                raise original_error
            if obr & 0x1:
                raise RuntimeError("STM32F103 option-byte shadow register reports OPTERR")
            user = 0xF8 | ((obr >> 2) & 0x07)
            values = [
                0x00,
                user,
                (obr >> 10) & 0xFF,
                (obr >> 18) & 0xFF,
                *(wrpr >> shift & 0xFF for shift in (0, 8, 16, 24)),
            ]
            reconstructed = bytes(
                byte
                for item in values
                for byte in (item, item ^ 0xFF)
            )
            return reconstructed, False

    def disconnect(self) -> None:
        with self._lock:
            session, self._session = self._session, None
            self._probe_identifier = ""
            self._reset_voltage_mv = None
            self._security_family = ""
            self._algorithm_reset_required = False
            self._algorithm_reset_done = False
            self._connection_arguments = None
            self._py32f030_unlock_transition_pending = False
            if session is not None:
                try:
                    session.close()
                    trace_delegate = getattr(session, "delegate", None)
                    if isinstance(trace_delegate, _TraceStateDelegate) and trace_delegate.errors:
                        raise RuntimeError("; ".join(trace_delegate.errors))
                except Exception as exc:
                    raise self._mapped_error(exc, FlashErrorCode.CONNECT_FAIL) from None

    def erase_chip(self) -> None:
        with self._lock:
            session = self._require_session()
            try:
                self._prepare_algorithm_execution(session.target)
                factory, mode = self._eraser(erase_mode="CHIP")
                factory(session, mode).erase()
            except FlashError:
                raise
            except Exception as exc:
                raise self._mapped_error(exc, FlashErrorCode.ERASE_FAIL) from None

    def erase_sectors(self, addresses: Any) -> None:
        with self._lock:
            session = self._require_session()
            try:
                unique = self._validated_sector_addresses(session.target, addresses)
                self._prepare_algorithm_execution(session.target)
                factory, mode = self._eraser(erase_mode="SECTOR")
                factory(session, mode).erase(unique)
            except FlashError:
                raise
            except Exception as exc:
                raise self._mapped_error(exc, FlashErrorCode.ERASE_FAIL) from None

    def program(
        self,
        image: ImageInspection,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> None:
        with self._lock:
            session = self._require_session()
            try:
                path = Path(image.file_path)
                if not path.is_file():
                    raise FlashError(
                        FlashErrorCode.FILE_NOT_FOUND,
                        "firmware snapshot file was not found",
                    )
                hex_data = (
                    self._decode_hex_image(image)
                    if image.format.lower() == "hex" else None
                )
                self._prepare_algorithm_execution(session.target)
                factory = self._programmer_factory
                if factory is None:
                    FileProgrammer = import_pyocd_attr(
                        "pyocd.flash.file_programmer", "FileProgrammer"
                    )

                    factory = FileProgrammer
                image_start = (
                    image.base_address
                    if image.format.lower() == "bin" and image.base_address is not None
                    else image.start
                )
                region = self._flash_region_for_address(session.target, image_start)
                # A program request only authorizes image-covered sectors, never
                # pyOCD's automatic cost-based escalation to chip erase.
                programmer_options: dict[str, Any] = {"chip_erase": "sector"}
                if progress_callback is not None:
                    programmer_options["progress"] = progress_callback
                if str(getattr(region, "name", "")).startswith("mklink_custom_flm_"):
                    programmer_options.update(smart_flash=False, keep_unwritten=False)
                programmer = factory(session, **programmer_options)
                kwargs = {}
                if image.format.lower() == "bin":
                    kwargs["base_address"] = image.base_address
                if hex_data is None:
                    programmer.program(str(path), **kwargs)
                else:
                    # Use the same validated segments as preview/verification.
                    # Feeding the raw HEX to a second parser rejects harmless
                    # repeated entry points after the target was already erased.
                    # Queue all sparse segments before committing once.
                    for segment, payload in hex_data:
                        programmer.add_file(
                            io.BytesIO(payload), file_format="bin",
                            base_address=segment.start,
                        )
                    programmer.commit()
            except FlashError:
                self._close_after_failure()
                raise
            except FileNotFoundError:
                self._close_after_failure()
                raise FlashError(
                    FlashErrorCode.FILE_NOT_FOUND,
                    "firmware snapshot file was not found",
                ) from None
            except Exception as exc:
                self._close_after_failure()
                logging.getLogger(__name__).exception("Online firmware programming failed")
                raise self._mapped_error(exc, FlashErrorCode.PROGRAM_FAIL) from None

    def _prepare_algorithm_execution(self, target: Any) -> None:
        """Reset custom-FLM targets once, immediately before destructive work.

        Attaching to a running RTOS can retain exception/stack/MPU state that
        is unsuitable for a RAM algorithm. Connection and read-only operations
        remain non-resetting; erase/program need a deterministic execution state.
        """

        if not self._algorithm_reset_required or self._algorithm_reset_done:
            return
        target.reset_and_halt()
        # Some probes/targets acknowledge a software reset while retaining the
        # running RTOS context. Do not erase and then run an FLM on its PSP or
        # from an exception handler. Do not silently substitute a physical reset.
        if (
            int(target.read_core_register("ipsr")) != 0
            or int(target.read_core_register("control")) & 3
        ):
            raise FlashError(
                FlashErrorCode.CONNECT_FAIL,
                "Target reset did not establish a privileged main-stack context; "
                "Flash was not erased. Select hardware reset with NRST connected, "
                "or connect under reset.",
            )
        self._algorithm_reset_done = True

    def verify(
        self,
        image: ImageInspection,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> None:
        with self._lock:
            session = self._require_session()
            try:
                verified = 0
                total = image.size if isinstance(image.size, int) and image.size > 0 else 0
                if progress_callback is not None:
                    progress_callback(0.0)
                for address, expected in self._iter_image_chunks(image):
                    self._verify_expected_bytes(session.target, address, expected)
                    verified += len(expected)
                    if progress_callback is not None:
                        progress_callback(verified / total if total else 1.0)
            except FlashError:
                raise
            except FileNotFoundError:
                raise FlashError(
                    FlashErrorCode.FILE_NOT_FOUND,
                    "firmware snapshot file was not found",
                ) from None
            except Exception as exc:
                raise self._mapped_error(exc, FlashErrorCode.VERIFY_FAIL) from None

    @classmethod
    def _verify_expected_bytes(
        cls, target: Any, address: int, expected: bytes
    ) -> None:
        offset = 0
        while offset < len(expected):
            current = address + offset
            region = cls._flash_region_for_address(target, current)
            size = len(expected) - offset
            if region is not None:
                size = min(size, int(region.start) + int(region.length) - current)
            flash = getattr(region, "flash", None) if region is not None else None
            custom_verify = callable(getattr(flash, "verify_data", None))
            flash_algo = getattr(flash, "flash_algo", None)
            custom_verify = custom_verify or (
                isinstance(flash_algo, dict)
                and flash_algo.get("mklink_custom_verify") is True
            )
            if custom_verify:
                page_size = getattr(region, "page_size", None)
                if (
                    not isinstance(page_size, int)
                    or isinstance(page_size, bool)
                    or page_size <= 0
                ):
                    raise RuntimeError("custom FLM has no valid verification buffer size")
                size = min(size, page_size)
                result = cls._verify_with_flash_algorithm(
                    flash, current, expected[offset : offset + size]
                )
                success = current + size
                if result != success:
                    mismatch = (
                        result
                        if isinstance(result, int)
                        and not isinstance(result, bool)
                        and current <= result < success
                        else current
                    )
                    raise FlashError(
                        FlashErrorCode.VERIFY_FAIL,
                        f"verification mismatch at 0x{mismatch:X}",
                    )
            else:
                actual = cls._read_target_bytes(target, current, size)
                common = min(len(actual), size)
                mismatch = next(
                    (
                        index
                        for index in range(common)
                        if actual[index] != expected[offset + index]
                    ),
                    None,
                )
                if mismatch is None and len(actual) != size:
                    mismatch = common
                if mismatch is not None:
                    raise FlashError(
                        FlashErrorCode.VERIFY_FAIL,
                        f"verification mismatch at 0x{current + mismatch:X}",
                    )
            offset += size

    @staticmethod
    def _flash_region_for_address(target: Any, address: int) -> Any:
        memory_map = getattr(target, "memory_map", ())
        getter = getattr(memory_map, "get_region_for_address", None)
        if callable(getter):
            region = getter(address)
            if region is not None and bool(getattr(region, "is_flash", False)):
                return region
        for region in memory_map:
            if (
                bool(getattr(region, "is_flash", False))
                and int(region.start) <= address < int(region.start) + int(region.length)
            ):
                return region
        return None

    @staticmethod
    def _verify_with_flash_algorithm(flash: Any, address: int, data: bytes) -> Any:
        verifier = getattr(flash, "verify_data", None)
        if callable(verifier):
            return verifier(address, data)

        flash_algo = getattr(flash, "flash_algo", None)
        if (
            not isinstance(flash_algo, dict)
            or flash_algo.get("mklink_custom_verify") is not True
        ):
            raise RuntimeError("custom FLM verification is unavailable")
        pc_verify = flash_algo.get("pc_verify")
        page_buffers = getattr(flash, "page_buffers", ())
        if (
            not isinstance(pc_verify, int)
            or isinstance(pc_verify, bool)
            or not page_buffers
        ):
            raise RuntimeError("custom FLM verification entry is invalid")

        flash.init(flash.Operation.VERIFY)
        try:
            flash.target.write_memory_block8(page_buffers[0], data)
            timeout = flash.target.session.options.get("flash.timeout.program")
            result = flash._call_function_and_wait(
                pc_verify,
                address,
                len(data),
                page_buffers[0],
                timeout=timeout,
            )
            if result == flash.TIMEOUT_ERROR:
                raise RuntimeError("custom FLM verification timed out")
            return result
        finally:
            flash.uninit()

    def reset_run(self, reset_mode: Optional[str] = None) -> None:
        with self._lock:
            session = self._require_session()
            trace_delegate = getattr(session, "delegate", None)
            if isinstance(trace_delegate, _TraceStateDelegate):
                trace_delegate.enabled = False
            mode = self._reset_mode if reset_mode is None else reset_mode
            if mode not in {
                "default", "hardware", "software", "core", "system", "power-cycle"
            }:
                raise ValueError(f"unknown reset mode: {mode}")
            try:
                if mode == "power-cycle":
                    if self._reset_voltage_mv not in _POWER_CYCLE_VOLTAGES_MV:
                        raise ValueError(
                            "power-cycle reset requires a validated restore voltage"
                        )
                    self._power_cycle(
                        self._probe_identifier,
                        self._reset_voltage_mv,
                    )
                    if self._py32f030_unlock_transition_pending:
                        reconnect = self._connection_arguments
                        if reconnect is None:
                            raise RuntimeError(
                                "PY32F030 unlock verification has no reconnect configuration"
                            )
                        reconnect = dict(reconnect)
                        self.connect(**reconnect)
                        session = self._require_session()
                        option = self._read_target_bytes(
                            session.target,
                            self._PY32F030_OPTION_ADDRESS,
                            self._PY32F030_OPTION_SIZE,
                        )
                        logical_rdp = int.from_bytes(
                            self._read_target_bytes(session.target, 0x40022020, 4),
                            "little",
                        ) & 0xFF
                        if (
                            not self._py32f030_option_words_valid(option)
                            or option[0] != 0xAA
                            or option[2] != 0x55
                            or logical_rdp != 0xAA
                        ):
                            raise RuntimeError(
                                "PY32F030 RDP unlock did not persist after power cycle"
                            )
                        for address in range(0x08000000, 0x08010000, 4096):
                            if self._read_target_bytes(
                                session.target, address, 4096
                            ) != b"\xFF" * 4096:
                                raise RuntimeError(
                                    "PY32F030 main Flash was not completely erased by RDP unlock"
                                )
                        self._py32f030_unlock_transition_pending = False
                    return
                if mode == "default":
                    session.target.reset()
                else:
                    Target = import_pyocd_attr("pyocd.core.target", "Target")
                    reset_types = {
                        "hardware": Target.ResetType.HARDWARE,
                        "software": Target.ResetType.DEFAULT,
                        "core": Target.ResetType.CORE,
                        "system": Target.ResetType.SYSTEM,
                    }
                    session.target.reset(reset_types[mode])
                if self._algorithm_reset_done:
                    Target = import_pyocd_attr("pyocd.core.target", "Target")
                    state = session.target.get_state()
                    if state not in (Target.State.RUNNING, Target.State.SLEEPING):
                        # Never resume a stranded RAM algorithm as if it were
                        # the application during session teardown.
                        session.options["resume_on_disconnect"] = False
                        raise FlashError(
                            FlashErrorCode.RESET_FAIL,
                            "Flash operation finished but reset did not start the target "
                            f"(state={state.name}); check the reset connection/method.",
                        )
                if isinstance(trace_delegate, _TraceStateDelegate) and not self._security_family:
                    trace_delegate.enabled = True
            except FlashError:
                raise
            except Exception as exc:
                raise self._mapped_error(exc, FlashErrorCode.RESET_FAIL) from None

    def memory_regions(self) -> Tuple[MemoryRegion, ...]:
        with self._lock:
            session = self._require_session()
            try:
                result = []
                for region in session.target.memory_map:
                    is_flash = bool(getattr(region, "is_flash", False))
                    is_ram = bool(getattr(region, "is_ram", False))
                    if not is_flash and not is_ram:
                        continue
                    blocksize = (
                        getattr(region, "blocksize", None) if is_flash else None
                    )
                    if (
                        not isinstance(blocksize, int)
                        or isinstance(blocksize, bool)
                        or blocksize <= 0
                    ):
                        blocksize = None
                    result.append(
                        MemoryRegion(
                            name=str(getattr(region, "name", "")),
                            start=int(region.start),
                            length=int(region.length),
                            is_flash=is_flash,
                            writable=(
                                self._is_programmable_flash(region)
                                if is_flash
                                else bool(getattr(region, "is_writable", True))
                            ),
                            sector_size=blocksize,
                        )
                    )
                return tuple(result)
            except Exception as exc:
                if self._is_locked_error(exc):
                    raise FlashError(
                        FlashErrorCode.TARGET_LOCKED,
                        "target memory map is protected",
                    ) from None
                raise FlashError(
                    FlashErrorCode.TARGET_NOT_SUPPORTED,
                    "target memory map is unavailable",
                ) from None

    def read_memory(self, address: int, size: int) -> bytes:
        """Read an exact target-memory range while the pyOCD session is open."""
        if type(address) is not int or address < 0:
            raise ValueError("address must be a non-negative integer")
        if type(size) is not int or size <= 0:
            raise ValueError("size must be a positive integer")
        with self._lock:
            session = self._require_session()
            try:
                data = self._read_target_bytes(session.target, address, size)
            except Exception as exc:
                if self._is_locked_error(exc) or self._security_read_protection_active(
                    session.target
                ):
                    raise FlashError(
                        FlashErrorCode.TARGET_LOCKED,
                        "target memory is protected",
                    ) from None
                raise FlashError(
                    FlashErrorCode.CONNECT_FAIL,
                    f"target memory read failed: {exc}",
                ) from None
            if len(data) != size:
                raise FlashError(
                    FlashErrorCode.CONNECT_FAIL,
                    f"target returned {len(data)} bytes for a {size}-byte read",
                )
            return data

    def _security_read_protection_active(self, target: Any) -> bool:
        if self._security_family not in {
            "stm32f103-rdp1",
            "gd32f303xe-spc",
            "py32f030x8-rdp1",
        }:
            return False
        if not any(
            str(getattr(region, "name", "")) == "mklink_security_option_bytes"
            for region in getattr(target, "memory_map", ())
        ):
            return False
        try:
            status_address = (
                0x40022020
                if self._security_family == "py32f030x8-rdp1"
                else 0x4002201C
            )
            obr = int.from_bytes(
                self._read_target_bytes(target, status_address, 4), "little"
            )
        except Exception:
            return False
        if self._security_family == "py32f030x8-rdp1":
            return (obr & 0xFF) != 0xAA
        return bool(obr & 0x2)

    @staticmethod
    def _iter_image_chunks(
        image: ImageInspection,
    ) -> Iterator[Tuple[int, bytes]]:
        path = Path(image.file_path)
        if image.format.lower() == "bin":
            if image.base_address is None:
                raise FlashError(
                    FlashErrorCode.VERIFY_FAIL, "BIN image has no base address"
                )
            if (
                not isinstance(image.size, int)
                or isinstance(image.size, bool)
                or image.size < 0
                or len(image.segments) != 1
            ):
                raise FlashError(
                    FlashErrorCode.VERIFY_FAIL,
                    "BIN image inspection is inconsistent",
                )
            segment = image.segments[0]
            if (
                image.start != image.base_address
                or image.end != image.start + image.size
                or segment.start != image.start
                or segment.end != image.end
                or segment.length != image.size
            ):
                raise FlashError(
                    FlashErrorCode.VERIFY_FAIL,
                    "BIN image inspection is inconsistent",
                )
            snapshot_size = path.stat().st_size
            if snapshot_size != image.size:
                mismatch = image.base_address + min(snapshot_size, image.size)
                raise FlashError(
                    FlashErrorCode.VERIFY_FAIL,
                    f"verification mismatch at 0x{mismatch:X}",
                )
            address = image.base_address
            remaining = image.size
            with path.open("rb") as stream:
                while remaining:
                    requested = min(4096, remaining)
                    payload = stream.read(requested)
                    if len(payload) != requested:
                        raise FlashError(
                            FlashErrorCode.VERIFY_FAIL,
                            f"verification mismatch at 0x{address + len(payload):X}",
                        )
                    yield address, payload
                    address += len(payload)
                    remaining -= len(payload)
                if stream.read(1):
                    raise FlashError(
                        FlashErrorCode.VERIFY_FAIL,
                        f"verification mismatch at 0x{address:X}",
                    )
            return

        for segment, payload in PyOcdBackend._decode_hex_image(image):
            for offset in range(0, len(payload), 4096):
                yield segment.start + offset, payload[offset:offset + 4096]

    @staticmethod
    def _decode_hex_image(
        image: ImageInspection,
    ) -> Tuple[Tuple[ImageSegment, bytes], ...]:
        segments, data = ImageInspector.decode_hex(Path(image.file_path))
        if (
            segments != image.segments
            or segments[0].start != image.start
            or segments[-1].end != image.end
        ):
            raise FlashError(
                FlashErrorCode.FILE_FORMAT_ERROR,
                "HEX image data does not match its inspection",
            )
        return data

    @staticmethod
    def _read_target_bytes(target: Any, address: int, size: int) -> bytes:
        read8 = getattr(target, "read_memory_block8", None)
        if callable(read8):
            result = read8(address, size)
            return bytes(result or ())
        read32 = getattr(target, "read_memory_block32", None)
        if callable(read32):
            words = read32(address, (size + 3) // 4)
            output = bytearray()
            for word in words or ():
                output.extend(int(word).to_bytes(4, "little"))
            return bytes(output[:size])
        raise RuntimeError("target does not support block memory reads")

    def _eraser(self, erase_mode: str) -> Any:
        FlashEraser = import_pyocd_attr("pyocd.flash.eraser", "FlashEraser")

        return self._eraser_factory or FlashEraser, FlashEraser.Mode[erase_mode]

    @staticmethod
    def _validated_sector_addresses(target: Any, addresses: Any) -> list[int]:
        values = list(addresses)
        if not values:
            raise FlashError(
                FlashErrorCode.IMAGE_OUT_OF_RANGE,
                "at least one sector address is required",
            )
        unique = sorted(set(values))
        if any(not isinstance(value, int) or isinstance(value, bool) for value in unique):
            raise FlashError(
                FlashErrorCode.IMAGE_OUT_OF_RANGE, "sector address is invalid"
            )
        regions = tuple(target.memory_map)
        for address in unique:
            containing = [
                region
                for region in regions
                if bool(getattr(region, "is_flash", False))
                and int(region.start) <= address < int(region.start) + int(region.length)
            ]
            if not containing:
                raise FlashError(
                    FlashErrorCode.IMAGE_OUT_OF_RANGE,
                    f"sector address 0x{address:X} is outside flash",
                )
            sizes = set()
            for region in containing:
                flash = getattr(region, "flash", None)
                get_sector_info = getattr(flash, "get_sector_info", None)
                if not callable(get_sector_info):
                    raise FlashError(
                        FlashErrorCode.TARGET_NOT_SUPPORTED,
                        "target does not expose reliable sector geometry",
                    )
                try:
                    info = get_sector_info(address)
                except Exception as exc:
                    raise PyOcdBackend._mapped_error(
                        exc, FlashErrorCode.ERASE_FAIL
                    ) from None
                if info is None:
                    raise FlashError(
                        FlashErrorCode.TARGET_NOT_SUPPORTED,
                        "target does not expose reliable sector geometry",
                    )
                try:
                    base = getattr(info, "base_addr", None)
                    size = getattr(info, "size", None)
                except Exception:
                    raise FlashError(
                        FlashErrorCode.TARGET_NOT_SUPPORTED,
                        "target does not expose reliable sector geometry",
                    ) from None
                if (
                    not isinstance(base, int)
                    or isinstance(base, bool)
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size <= 0
                    or base < int(region.start)
                    or base + size > int(region.start) + int(region.length)
                ):
                    raise FlashError(
                        FlashErrorCode.TARGET_NOT_SUPPORTED,
                        "target does not expose reliable sector geometry",
                    )
                if base != address:
                    raise FlashError(
                        FlashErrorCode.IMAGE_OUT_OF_RANGE,
                        f"address 0x{address:X} is not a sector start",
                    )
                sizes.add(size)
            if len(sizes) != 1:
                raise FlashError(
                    FlashErrorCode.TARGET_NOT_SUPPORTED,
                    "target exposes conflicting sector geometry",
                )
        return unique

    @staticmethod
    def _is_programmable_flash(region: Any) -> bool:
        if getattr(region, "flash", None) is not None:
            return True
        return getattr(region, "algo", None) is not None

    def _require_session(self) -> Any:
        if self._session is None:
            raise FlashError(FlashErrorCode.CONNECT_FAIL, "target is not connected")
        return self._session

    def _close_after_failure(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def _resolve_probe(self, probe: Any) -> Any:
        if not isinstance(probe, str):
            return probe
        provider = self._probe_provider
        if provider is None:
            DebugProbeAggregator = import_pyocd_attr(
                "pyocd.probe.aggregator", "DebugProbeAggregator"
            )

            provider = DebugProbeAggregator.get_all_connected_probes
        for candidate in provider():
            if getattr(candidate, "unique_id", None) == probe:
                return candidate
        raise FlashError(
            FlashErrorCode.MKLINK_DAP_NOT_FOUND,
            "requested MKLink DAP probe was not found",
        )

    @staticmethod
    def _mapped_error(exc: Exception, fallback: FlashErrorCode) -> FlashError:
        text = str(exc)
        if PyOcdBackend._is_locked_error(exc):
            return FlashError(FlashErrorCode.TARGET_LOCKED, text or "target is locked")
        return FlashError(fallback, text or fallback.value)

    @staticmethod
    def _is_locked_error(exc: Exception) -> bool:
        return _LOCKED_ERROR_PATTERN.search(str(exc)) is not None
