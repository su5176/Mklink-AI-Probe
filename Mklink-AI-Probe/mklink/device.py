"""MKLink Device — unified SDK API.

Provides a single ``Device`` facade that wraps bridge, flash, RTT,
variable watch, debug control, and HardFault decoding behind a
context-manager-friendly interface.

Usage::

    import mklink

    with mklink.connect() as dev:
        dev.flash("build/out.hex")
        dev.rtt_start()
        log = dev.wait_for_rtt("System ready", timeout=10.0)
        val = dev.read_variable("sensor_count")
"""

from __future__ import annotations

import os
import re
import struct
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mklink._types import DeviceState, DEFAULT_BAUDRATE


@dataclass
class HardFaultReport:
    """Decoded HardFault information."""
    cfsr: int
    hfsr: int
    cfsr_flags: list[str]
    hfsr_flags: list[str]
    stack_frame: dict[str, int] | None
    source_locations: dict[int, str] | None
    summary: str
    fault_function: str | None
    fault_location: str | None
    exception_stack: dict[str, Any] | None
    call_stack: list[dict[str, Any]]
    core_registers: dict[str, int] | None


class DeviceError(Exception):
    pass


class DeviceNotConnectedError(DeviceError):
    pass


_RT_THREAD_NAME_RE = re.compile(rb"[A-Za-z_][A-Za-z0-9_.-]*")
_RT_NAME_MAX_DEFAULT = 8
_SRAM_START = 0x20000000
_SRAM_END = 0x40000000
_FLASH_VERIFY_CHUNK = 1024


def _aligned_object_list_offset(name_max: int) -> int:
    return (name_max + 2 + 3) & ~3


def _is_sram_pointer(value: int) -> bool:
    return _SRAM_START <= value < _SRAM_END and value % 4 == 0


def _decode_rt_thread_name(raw: bytes, name_max: int) -> str | None:
    """Decode a validated inline ``rt_object`` thread name."""
    list_offset = _aligned_object_list_offset(name_max)
    if len(raw) < list_offset + 8:
        return None
    if raw[name_max] & 0x7F != 0x01:  # RT_Object_Class_Thread
        return None
    next_ptr = int.from_bytes(raw[list_offset:list_offset + 4], "little")
    prev_ptr = int.from_bytes(raw[list_offset + 4:list_offset + 8], "little")
    if not _is_sram_pointer(next_ptr) or not _is_sram_pointer(prev_ptr):
        return None
    name_field = raw[:name_max]
    nul = name_field.find(b"\x00")
    candidate = name_field if nul < 0 else name_field[:nul]
    if candidate and _RT_THREAD_NAME_RE.fullmatch(candidate):
        return candidate.decode("ascii")
    return None


def initialize_target(
    bridge: Any,
    flash: Any,
    *,
    mcu_hint: str | None = None,
    project_root: str = ".",
    timeout: float = 10.0,
) -> int:
    """Initialize the target SWD DP, read IDCODE, and match the MCU profile.

    Sends ``cmd.get_idcode()`` (the probe firmware's SWD line-switch + DP
    init + IDCODE read), writes the result into the bridge context, and
    resolves ``current_mcu`` with priority
    ``mcu_hint > .mklink/config.json:mcu_key > idcode match``.

    Call this on a freshly ``bridge.connect()``-ed session for **every path
    that establishes a target *debug* session** — ``Device._connect``,
    direct-bridge CLI ops, ``memory_access.read_memory``. Do NOT call it for
    probe-only paths (``version``, ``firmware_check``, port detection, Modbus,
    generic serial): those must work without a target MCU attached.

    Best-effort / tolerant by design: if IDCODE cannot be read (no target,
    broken SWD, timeout, or a mock bridge in tests), the bridge context keeps
    its default (``idcode`` 0) and ``0`` is returned — the caller stays
    connected. This preserves the historical "connect succeeds even without a
    target" semantics while fixing the bug where ``idcode`` was *always* 0
    even with a target present (e.g. MCP ``connect``'s long-lived session that
    never re-opened the serial port and so missed the firmware's startup DP
    init window).
    """
    from mklink.profiles import load_mcu_profiles, match_mcu_by_idcode
    from mklink.project_config import load_config

    try:
        idcode = flash.get_idcode(timeout=timeout)
    except Exception:
        # No target / broken SWD / timeout / mock bridge: stay connected, idcode 0.
        return 0

    bridge._ctx.idcode = idcode

    try:
        profiles = load_mcu_profiles()
        # 1) explicit hint wins — compatible chips share the same IDCODE
        if mcu_hint and profiles.get(mcu_hint):
            bridge._ctx.current_mcu = profiles[mcu_hint].get("name", mcu_hint)
            return idcode
        # 2) project config mcu_key
        cfg = load_config(project_root) or {}
        cfg_mcu = cfg.get("mcu_key")
        if cfg_mcu and profiles.get(cfg_mcu):
            bridge._ctx.current_mcu = profiles[cfg_mcu].get("name", cfg_mcu)
            return idcode
        # 3) last resort: match by idcode
        matched = match_mcu_by_idcode(idcode, profiles)
        if matched:
            bridge._ctx.current_mcu = profiles[matched].get("name", matched)
    except Exception:
        pass
    return idcode


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _fmt_hex(value: int) -> str:
    return f"0x{value:08X}"


class Device:
    """Unified MKLink device API.

    Wraps all mklink capabilities behind a single object that can be
    used as a context manager::

        with mklink.connect() as dev:
            dev.flash("firmware.hex")
            dev.rtt_start()
            print(dev.rtt_read(5.0))
            dev.rtt_stop()

    Or manually::

        dev = mklink.connect()
        try:
            dev.flash("firmware.hex")
        finally:
            dev.close()
    """

    def __init__(
        self,
        *,
        port: str | None = None,
        preferred_port: str | None = None,
        axf: str | None = None,
        mcu: str | None = None,
        project_root: str = ".",
        elf_backend: str | None = None,
    ):
        self._port = port
        self._preferred_port = preferred_port
        self._axf = axf
        self._mcu_hint = mcu
        self._project_root = project_root
        self._elf_backend_requested = elf_backend
        self._elf_backend = None
        self._bridge = None
        self._flash = None
        self._rtt_session = None
        self._systemview_session = None
        self._systemview_parser = None
        self._dwarf_info = None
        self._symbol_catalog = None
        self._symbol_layout_overrides: dict[tuple[str, int, int], dict[str, tuple[str, int | None]]] = {}
        self._symbol_lock = threading.RLock()
        self._axf_error = None  # reason ELF/DWARF loading was skipped
        self._connected = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------
    def __enter__(self) -> Device:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def _connect(self) -> None:
        from mklink.bridge import MKLinkSerialBridge
        from mklink.discovery import find_mklink_cdc_port, list_available_ports
        from mklink.project_config import load_config, save_config
        from mklink.serial._port import _PortLock

        automatic = self._port is None
        config = load_config(self._project_root) or {}
        saved_port = str(config.get("com_port") or "").strip() or None
        candidate = self._port or self._preferred_port or saved_port
        attempted: set[str] = set()
        discovery_lock = None

        try:
            while True:
                if candidate is None:
                    if discovery_lock is None:
                        discovery_lock = _PortLock("mklink_auto_connect")
                        deadline = time.monotonic() + 60.0
                        while not discovery_lock.acquire():
                            if time.monotonic() >= deadline:
                                break
                            time.sleep(0.05)
                        else:
                            deadline = None
                        if deadline is not None:
                            break

                    candidate = find_mklink_cdc_port(exclude_ports=set(attempted))
                    if candidate is None:
                        break

                attempted.add(candidate.strip().casefold())
                bridge = MKLinkSerialBridge(candidate)
                if bridge.connect():
                    self._bridge = bridge
                    self._port = candidate
                    break
                bridge.close()

                if not automatic:
                    break
                candidate = None
        finally:
            if discovery_lock is not None:
                discovery_lock.release()

        if self._bridge is None:
            ports = ", ".join(sorted(attempted)) or "none"
            raise DeviceNotConnectedError(
                f"Failed to connect to an available MKLink port (tried: {ports})"
            )

        if automatic and self._port != saved_port:
            visible_ports = {
                str(info.get("device") or "").strip().casefold()
                for info in list_available_ports()
            }
            saved_port_is_present = (
                saved_port is not None and saved_port.casefold() in visible_ports
            )
            if not saved_port_is_present:
                updated = dict(config)
                updated["com_port"] = self._port
                try:
                    save_config(self._project_root, updated)
                except Exception:
                    pass
        self._connected = True

        from mklink.flash import MKLinkFlash
        self._flash = MKLinkFlash(self._bridge)

        # SWD DP init + IDCODE read + MCU match. This was previously only done
        # by the remote API layer; every other connect path (MCP, SDK users,
        # legacy socket server, SystemView CLI, pytest fixtures) skipped it, so
        # the DAP was never initialized in long-lived sessions and idcode read
        # 0. Doing it here fixes all of them at once. Tolerant: a missing
        # target leaves idcode at 0 rather than failing connect (see
        # initialize_target docstring).
        initialize_target(
            self._bridge,
            self._flash,
            mcu_hint=self._mcu_hint,
            project_root=self._project_root,
        )

        if self._axf:
            self._load_dwarf_info()

    def close(self) -> None:
        if self._rtt_session and self._rtt_session._running:
            try:
                self._rtt_session.stop()
            except Exception:
                pass
        if self._systemview_session and self._systemview_session._running:
            try:
                self._systemview_session.stop()
            except Exception:
                pass
        if self._bridge:
            self._bridge.close()
        self._bridge = None
        self._flash = None
        self._connected = False
        # HIL-Infra lockd 协议互操作锁（connect 时获取）：断连即释放
        renew_stop = getattr(self, "_hil_renew_stop", None)
        if renew_stop is not None:
            renew_stop.set()
            thread = getattr(self, "_hil_renew_thread", None)
            if thread is not None:
                thread.join(timeout=2)
            self._hil_renew_stop = None
            self._hil_renew_thread = None
        hil_lock = getattr(self, "_hil_lock", None)
        if hil_lock is not None:
            hil_lock.release()
            self._hil_lock = None

    @property
    def connected(self) -> bool:
        return self._connected and self._bridge is not None

    def _require_connected(self) -> None:
        if not self.connected:
            raise DeviceNotConnectedError("Device not connected")

    @property
    def idcode(self) -> int:
        self._require_connected()
        return self._bridge.idcode

    @property
    def mcu_name(self) -> str:
        self._require_connected()
        return self._bridge.current_mcu

    @property
    def port(self) -> str | None:
        return self._port

    @property
    def state(self) -> DeviceState:
        if not self._bridge:
            return DeviceState.DISCONNECTED
        return self._bridge.state

    # ------------------------------------------------------------------
    # DWARF / symbol loading
    # ------------------------------------------------------------------
    def _load_dwarf_info(self) -> None:
        if self._axf and Path(self._axf).exists():
            try:
                self.reparse_axf_atomically()
            except Exception as e:
                # Unreadable ELF / missing DWARF / parser error: never
                # let this crash connect() — the bridge is already up. Record
                # the reason so axf_status / the MCP layer can surface it and
                # guide the user without losing the connected debug session.
                self._axf_error = str(e)

    @property
    def symbol_catalog(self):
        with self._symbol_lock:
            return self._symbol_catalog

    def reparse_axf_atomically(
        self,
        axf_path: str | None = None,
        elf_backend: str | None = None,
    ):
        from mklink.dwarf_parser import load_dwarf_info
        from mklink.elf_backend import resolve_elf_backend
        from mklink.symbol_catalog import AxfFingerprint, SymbolCatalog

        candidate = str(axf_path or self._axf or "")
        if not candidate:
            raise DeviceError("No AXF path set")
        if not Path(candidate).exists():
            raise DeviceError(f"AXF not found: {candidate}")

        requested_backend = (
            elf_backend
            if elf_backend is not None
            else self._elf_backend_requested
        )
        effective_backend = resolve_elf_backend(
            requested_backend, project_root=self._project_root
        )
        fingerprint = AxfFingerprint.from_path(candidate)
        info = load_dwarf_info(
            candidate,
            backend=effective_backend,
            project_root=self._project_root,
        )
        with self._symbol_lock:
            generation = (self._symbol_catalog.generation if self._symbol_catalog else 0) + 1
        from mklink.elf_backend import writable_memory_ranges

        catalog = SymbolCatalog.from_dwarf(
            info,
            axf_path=candidate,
            generation=generation,
            ram_ranges=writable_memory_ranges(
                candidate,
                backend=effective_backend,
                project_root=self._project_root,
                fallback=((_SRAM_START, _SRAM_END),),
            ),
        )
        override_key = (
            str(Path(candidate).resolve()),
            fingerprint.size,
            fingerprint.mtime_ns,
        )
        for variable_name, (definition, pack) in self._symbol_layout_overrides.get(
            override_key, {}
        ).items():
            catalog, _layout = self._catalog_with_c_definition(
                info,
                catalog,
                variable_name,
                definition,
                pack,
                generation=generation,
            )
        if catalog.fingerprint != fingerprint:
            raise DeviceError("AXF changed while symbols were being parsed")

        with self._symbol_lock:
            self._axf = candidate
            self._dwarf_info = info
            self._symbol_catalog = catalog
            self._elf_backend_requested = requested_backend
            self._elf_backend = effective_backend
            self._axf_error = None
        return catalog

    @staticmethod
    def _catalog_with_c_definition(
        info,
        catalog,
        variable_name: str,
        definition: str,
        pack: int | None,
        *,
        generation: int | None = None,
    ):
        from mklink.c_layout import CLayoutError, parse_c_layout

        variable = info.variables.get(variable_name)
        if variable is None or variable.address is None:
            raise CLayoutError(
                f"variable '{variable_name}' was not found or has no fixed address"
            )
        if variable.size <= 0:
            raise CLayoutError(f"variable '{variable_name}' has no known storage size")
        layout = parse_c_layout(
            definition,
            preferred_type=(
                variable.type_name
                if re.fullmatch(r"[A-Za-z_]\w*", variable.type_name or "")
                else None
            ),
            pack=pack,
        )
        if layout.size != variable.size:
            raise CLayoutError(
                f"C layout size {layout.size} does not match AXF variable size "
                f"{variable.size} for '{variable_name}'"
            )
        return (
            catalog.with_c_layout(
                variable_name,
                int(variable.address),
                layout,
                generation=generation,
            ),
            layout,
        )

    def apply_c_definition(
        self,
        variable_name: str,
        definition: str,
        pack: int | None = None,
    ):
        """Apply a bounded C aggregate layout to the active symbol catalog."""
        with self._symbol_lock:
            info = self._dwarf_info
            catalog = self._symbol_catalog
            if info is None or catalog is None:
                raise DeviceError("No AXF symbol catalog is loaded")
            new_catalog, layout = self._catalog_with_c_definition(
                info,
                catalog,
                variable_name,
                definition,
                pack,
            )
            override_key = (
                str(Path(catalog.axf_path).resolve()),
                catalog.fingerprint.size,
                catalog.fingerprint.mtime_ns,
            )
            overrides = dict(self._symbol_layout_overrides.get(override_key, {}))
            overrides[variable_name] = (definition, pack)
            self._symbol_layout_overrides[override_key] = overrides
            self._symbol_catalog = new_catalog
            return new_catalog, layout

    def parse_axf(
        self,
        axf_path: str | None = None,
        elf_backend: str | None = None,
    ) -> dict:
        """手动触发 AXF 解析。返回解析结果摘要。"""
        try:
            self.reparse_axf_atomically(axf_path, elf_backend=elf_backend)
            return self.axf_status
        except Exception as e:
            self._axf_error = str(e)
            return {"loaded": False, "error": str(e), "active": self.axf_status}

    @property
    def axf_status(self) -> dict:
        """返回 AXF 解析状态摘要（含解析后端能力与失败原因）。"""
        from mklink.elf_backend import elf_status

        try:
            tc = elf_status(
                self._elf_backend or self._elf_backend_requested,
                project_root=self._project_root,
            )
        except Exception as exc:
            tc = {
                "elf_backend": self._elf_backend_requested or "invalid",
                "elf_available": False,
                "builtin_elf_available": True,
                "external_elf_available": False,
                "readelf_available": False,
                "addr2line_available": False,
            }
            if not self._axf_error:
                self._axf_error = str(exc)
        if not self._dwarf_info:
            out: dict = {"loaded": False, "axf_path": self._axf, **tc}
            if self._axf_error:
                out["error"] = self._axf_error
            elif self._axf and not Path(self._axf).exists():
                out["error"] = f"AXF file not found: {self._axf}"
            return out
        info = self._dwarf_info
        catalog = self.symbol_catalog
        out = {
            "loaded": True,
            "axf_path": self._axf,
            "variable_count": len(catalog.items) if catalog is not None else 0,
            "struct_count": len(info.structs),
            "enum_count": len(info.enums),
            **tc,
        }
        if catalog is not None:
            out.update({
                "catalog_generation": catalog.generation,
                "catalog_count": len(catalog.items),
                "catalog_container_count": len(catalog.containers),
                "catalog_stale": catalog.is_stale(),
                "parsed_at": catalog.parsed_at,
            })
        if self._axf_error:
            out["last_error"] = self._axf_error
        return out

    # ------------------------------------------------------------------
    # Flash
    # ------------------------------------------------------------------
    def flash(
        self,
        firmware: str,
        *,
        target_part: str | None = None,
        base_address: int | str | None = None,
        board: str | None = None,
        hpm_flash_cfg: list[str] | tuple[str, str, str, str] | None = None,
        swd_clock: int | None = None,
        verify: bool = True,
        reset_after: bool = True,
        progress_callback=None,
    ) -> dict:
        self._require_connected()

        ext = Path(firmware).suffix.lower()
        if ext not in (".hex", ".bin"):
            raise DeviceError(f"Unsupported firmware format: {ext}")

        # Resolve MCU profile: prefer self._mcu hint > config mcu_key > idcode match
        from mklink.profiles import load_mcu_profiles, match_mcu_by_idcode, match_mcu_by_device
        profiles = load_mcu_profiles()
        mcu_profile = None
        resolved_key = None
        cfg = {}
        project_info = {}
        if self._mcu_hint and self._mcu_hint in profiles:
            mcu_profile = profiles[self._mcu_hint]
            resolved_key = self._mcu_hint
        if not mcu_profile and self._project_root:
            from mklink.project_config import load_config
            cfg = load_config(self._project_root) or {}
            cfg_mcu = cfg.get("mcu_key")
            if cfg_mcu and cfg_mcu in profiles:
                mcu_profile = profiles[cfg_mcu]
                resolved_key = cfg_mcu
        elif self._project_root:
            from mklink.project_config import load_config
            cfg = load_config(self._project_root) or {}
        if self._project_root:
            try:
                from mklink.project_config import load_project_info

                project_info = load_project_info(self._project_root) or {}
            except Exception:
                project_info = {}
        requested_target = str(
            target_part
            or project_info.get("device")
            or project_info.get("target_part")
            or ""
        ).strip()
        if not mcu_profile and requested_target:
            target_profile_key = match_mcu_by_device(requested_target, profiles)
            if target_profile_key:
                mcu_profile = profiles[target_profile_key]
                resolved_key = target_profile_key
        if not mcu_profile:
            mcu_profile = self._get_mcu_profile()
        explicit_custom = self._mcu_hint == "custom" or cfg.get("mcu_key") == "custom"
        from mklink.hpm_config import is_hpm_target

        project_hpm = is_hpm_target(
            requested_target,
            vendor=project_info.get("vendor"),
            board=board or project_info.get("board"),
        )
        if not mcu_profile and not explicit_custom and not requested_target and not project_hpm:
            raise DeviceError(
                "Unknown MCU profile; run `python -m mklink mcu-detect` "
                "or `python -m mklink project-init` before flashing"
            )

        flash_base = "0x08000000"
        profile_flash_base = flash_base
        ram_base = "0x20000000"
        is_hpm_profile = False
        if mcu_profile:
            flash_base = mcu_profile.get("flash_base", flash_base)
            profile_flash_base = flash_base
            ram_base = mcu_profile.get("ram_base", ram_base)
            profile_key = str(resolved_key or "").lower()
            profile_name = str(mcu_profile.get("name", "")).lower()
            profile_prefix = str(mcu_profile.get("device_prefix", "")).lower()
            is_hpm_profile = (
                profile_key.startswith("hpm")
                or "hpmicro" in profile_name
                or profile_prefix.startswith("hpm")
            )
        if base_address is not None:
            try:
                parsed_base_address = (
                    int(base_address, 0) if isinstance(base_address, str) else int(base_address)
                )
            except (TypeError, ValueError):
                raise DeviceError("Flash base address must be an integer")
            if parsed_base_address < 0:
                raise DeviceError("Flash base address must be nonnegative")
            flash_base = _fmt_hex(parsed_base_address)

        # Setup SWD clock (prefer config, fallback to profile default)
        resolved_swd_clock = 1000000
        if swd_clock is not None:
            resolved_swd_clock = swd_clock
        elif cfg:
            cfg_clock = cfg.get("swd_clock")
            if cfg_clock:
                resolved_swd_clock = int(cfg_clock)
        elif mcu_profile:
            resolved_swd_clock = mcu_profile.get("swd_clock_default", resolved_swd_clock)
        self._flash.set_swd_clock(resolved_swd_clock)

        # Prefer the unified user/builtin Pack catalog when an exact target is
        # available. Legacy profile FLM paths remain a compatibility fallback.
        catalog_algorithm = None
        catalog_selections = ()
        catalog_image = None
        catalog_fallback = False
        resolved_target = requested_target
        if not resolved_target and self._mcu_hint and self._mcu_hint not in profiles:
            resolved_target = self._mcu_hint

        from mklink.hpm_config import (
            default_hpm_board,
        )

        resolved_board = str(board or project_info.get("board") or "").strip()
        hpm_target = is_hpm_target(
            resolved_target,
            vendor=project_info.get("vendor"),
            board=resolved_board,
        ) or is_hpm_profile
        if hpm_target:
            if ext != ".bin":
                raise DeviceError("HPM ROM API only supports BIN firmware")
            raw_address = base_address
            if raw_address is None:
                raw_address = (
                    project_info.get("bin_base")
                    or project_info.get("download_base")
                    or project_info.get("flash_base")
                )
            if raw_address is None:
                raise DeviceError("HPM BIN firmware requires an explicit base address")
            try:
                address = int(raw_address, 0) if isinstance(raw_address, str) else int(raw_address)
            except (TypeError, ValueError):
                raise DeviceError("HPM BIN base address must be an integer")
            if address < 0:
                raise DeviceError("HPM BIN base address must be nonnegative")
            if not resolved_board:
                resolved_board = default_hpm_board(resolved_target) or ""
            resolved_flash_cfg = hpm_flash_cfg or project_info.get("hpm_flash_cfg")
            if not resolved_board and not resolved_flash_cfg:
                raise DeviceError("HPM target requires a board or flash configuration")
            result = self._flash.burn_hpm_bin(
                firmware,
                addr=_fmt_hex(address),
                board=resolved_board or None,
                flash_cfg=resolved_flash_cfg,
                progress_callback=progress_callback,
            )
            if not result.get("success"):
                raise DeviceError(f"Flash failed: {result}")
            result = dict(result)
            result["algorithm_source"] = "hpm-rom-api"
            result["verified"] = False
            if verify:
                self._verify_firmware_readback(firmware, _fmt_hex(address))
                result["verified"] = True
            if reset_after:
                self.reset()
            return result

        if resolved_target:
            from mklink.cmsis_dap.algorithm_catalog import (
                FlashAlgorithmError,
                discover_flash_algorithms,
                deploy_algorithm_to_probe,
                resolve_firmware_algorithms,
            )

            if ext == ".hex":
                from intelhex import IntelHex

                image = IntelHex(str(firmware))
                catalog_image = image
                firmware_ranges = tuple((start, end) for start, end in image.segments())
            else:
                firmware_ranges = ((int(flash_base, 0), int(flash_base, 0) + Path(firmware).stat().st_size),)
            try:
                catalog = discover_flash_algorithms(resolved_target)
                if catalog:
                    catalog_selections = tuple(
                        resolve_firmware_algorithms(catalog, firmware_ranges)
                    )
                    catalog_algorithm = catalog_selections[0].algorithm
                    if len(catalog_selections) == 1:
                        flm_path = deploy_algorithm_to_probe(catalog_algorithm)
                        algorithm_flash_base = _fmt_hex(catalog_algorithm.flash_start)
                        if ext == ".hex":
                            flash_base = algorithm_flash_base
                        selected_ram = catalog_algorithm.ram_start or int(ram_base, 0)
                        if not self._flash.load_flm(
                            flm_path,
                            algorithm_flash_base,
                            _fmt_hex(selected_ram),
                        ):
                            raise DeviceError(f"FLM load failed: {flm_path}")
                elif not (
                    mcu_profile
                    and resolved_key not in (None, "custom")
                    and mcu_profile.get("flm_path")
                ):
                    raise DeviceError(
                        f"Target {resolved_target!r} has no usable Flash algorithm"
                    )
            except (
                DeviceError,
                FlashAlgorithmError,
                ImportError,
                OSError,
                RuntimeError,
            ):
                # A verified profile remains a conservative compatibility
                # fallback when a standalone package omits the catalog or a
                # catalog payload cannot be deployed to the probe's volume.
                # This is intentionally unavailable to custom/unknown targets.
                if not (
                    mcu_profile
                    and resolved_key not in (None, "custom")
                    and not explicit_custom
                    and mcu_profile.get("flm_path")
                ):
                    raise
                catalog_algorithm = None
                catalog_selections = ()
                catalog_fallback = True

        # Load the legacy profile FLM only when the unified catalog did not
        # resolve the exact target and firmware address range.
        flm_path = None
        if mcu_profile and catalog_algorithm is None:
            flm_path = mcu_profile.get("flm_path", "")
            if flm_path and not flm_path.startswith("/"):
                flm_path = "/" + flm_path
        if flm_path and catalog_algorithm is None:
            legacy_flash_base = (
                profile_flash_base
                if resolved_key not in (None, "custom") and not explicit_custom
                else flash_base
            )
            if not self._flash.load_flm(flm_path, legacy_flash_base, ram_base):
                raise DeviceError(f"FLM load failed: {flm_path}")
        elif catalog_algorithm is None and (
            mcu_profile
            and resolved_key != "custom"
            and not explicit_custom
            and not is_hpm_profile
        ):
            raise DeviceError(
                f"MCU profile {resolved_key or mcu_profile.get('name', '')!r} has no FLM path"
            )

        if len(catalog_selections) > 1:
            if ext != ".hex" or catalog_image is None:
                raise DeviceError("Multiple Flash algorithms require an Intel HEX image")
            region_results = []
            with tempfile.TemporaryDirectory(prefix="mklink-flash-regions-") as temporary:
                for index, selection in enumerate(catalog_selections):
                    algorithm = selection.algorithm
                    flm_path = deploy_algorithm_to_probe(algorithm)
                    selected_ram = algorithm.ram_start or int(ram_base, 0)
                    if not self._flash.load_flm(
                        flm_path,
                        _fmt_hex(algorithm.flash_start),
                        _fmt_hex(selected_ram),
                    ):
                        raise DeviceError(f"FLM load failed: {flm_path}")
                    region_image = IntelHex()
                    for start, end in selection.ranges:
                        region_image.puts(
                            start,
                            bytes(catalog_image.tobinarray(start=start, end=end - 1)),
                        )
                    region_path = Path(temporary) / "region-{}.hex".format(index)
                    region_image.write_hex_file(str(region_path))

                    def region_progress(percent: int, region_index: int = index) -> None:
                        if progress_callback is not None:
                            progress_callback(
                                int((region_index * 100 + percent) / len(catalog_selections))
                            )

                    region_result = self._flash.burn_hex(
                        str(region_path),
                        progress_callback=region_progress,
                    )
                    if not region_result.get("success"):
                        raise DeviceError(f"Flash failed: {region_result}")
                    region_results.append(dict(region_result))
            result = {"success": True, "regions": region_results}
        elif ext == ".hex":
            result = self._flash.burn_hex(
                firmware, progress_callback=progress_callback
            )
        elif ext == ".bin":
            result = self._flash.burn_bin(
                firmware, flash_base, progress_callback=progress_callback
            )
        else:
            raise DeviceError(f"Unsupported firmware format: {ext}")

        if not result.get("success"):
            raise DeviceError(f"Flash failed: {result}")

        result = dict(result)
        if catalog_algorithm is not None:
            result["algorithm_source"] = catalog_algorithm.source_kind
            result["algorithm_name"] = catalog_algorithm.file_name
            if len(catalog_selections) > 1:
                result["algorithm_sources"] = [
                    selection.algorithm.source_kind for selection in catalog_selections
                ]
                result["algorithm_names"] = [
                    selection.algorithm.file_name for selection in catalog_selections
                ]
        elif catalog_fallback:
            result["algorithm_source"] = "legacy-profile-fallback"
        result["verified"] = False
        if verify:
            self._verify_firmware_readback(firmware, flash_base)
            result["verified"] = True

        if reset_after:
            self.reset()

        return result

    def _verify_firmware_readback(self, firmware: str, flash_base: str) -> None:
        path = Path(firmware)
        if path.suffix.lower() == ".hex":
            from intelhex import IntelHex

            image = IntelHex(str(path))
            regions = [
                (start, bytes(image.tobinarray(start=start, end=end - 1)))
                for start, end in image.segments()
            ]
        else:
            regions = [(int(flash_base, 0), path.read_bytes())]

        for start, expected in regions:
            for offset in range(0, len(expected), _FLASH_VERIFY_CHUNK):
                chunk = expected[offset:offset + _FLASH_VERIFY_CHUNK]
                address = start + offset
                actual = self.read_memory(address, len(chunk))
                if actual != chunk:
                    raise DeviceError(
                        f"Flash verify failed at 0x{address:08X}"
                    )

    def erase_chip(self) -> bool:
        self._require_connected()
        mcu_profile = self._get_mcu_profile()
        flash_base = "0x08000000"
        if mcu_profile:
            flash_base = mcu_profile.get("flash_base", flash_base)
        return self._flash.erase_chip(flash_base)

    def erase_sector(self, addr: int) -> bool:
        self._require_connected()
        return self._flash.erase_sector(f"0x{addr:08X}")

    def reset(self) -> None:
        self._require_connected()
        self._bridge.send_command("cmd.set_reset()", timeout=10.0)

    def _get_mcu_profile(self) -> dict | None:
        from mklink.profiles import load_mcu_profiles, match_mcu_by_idcode, match_mcu_by_device
        profiles = load_mcu_profiles()
        if self._bridge.idcode:
            key = match_mcu_by_idcode(self._bridge.idcode, profiles)
            if key:
                return profiles[key]
        if self._bridge.current_mcu:
            key = match_mcu_by_device(self._bridge.current_mcu, profiles)
            if key:
                return profiles[key]
        return None

    def _systemview_defaults(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "ram_base": 0x20000000,
            "id_shift": 2,
            "cpu_freq": 0,
            "cpu_freq_source": "",
        }

        profile = None
        try:
            profile = self._get_mcu_profile()
        except Exception:
            profile = None

        if isinstance(profile, dict):
            ram_base = _positive_int(
                profile.get("systemview_ram_base")
                or profile.get("sysview_ram_base")
                or profile.get("ram_base")
            )
            if ram_base:
                defaults["ram_base"] = ram_base
            id_shift = _positive_int(
                profile.get("systemview_id_shift")
                or profile.get("sysview_id_shift")
            )
            if id_shift:
                defaults["id_shift"] = id_shift
            freq = _positive_int(
                profile.get("systemview_cpu_freq")
                or profile.get("sysview_cpu_freq")
                or profile.get("cpu_freq_default")
                or profile.get("system_core_clock")
            )
            if freq:
                defaults["cpu_freq"] = freq
                defaults["cpu_freq_source"] = "mcu_profile_default"

        try:
            from mklink.project_config import load_project_info
            project = load_project_info(self._project_root) or {}
        except Exception:
            project = {}

        if isinstance(project, dict):
            board = str(project.get("board", "")).lower()
            vendor = str(project.get("vendor", "")).lower()
            soc = str(project.get("soc", "")).lower()
            device = str(project.get("device", "")).lower()
            looks_hpm5301 = (
                "hpmicro" in vendor
                and ("hpm5301" in board or "hpm5301" in soc or "hpm5301" in device)
            ) or board == "hpm5301evklite"

            ram_base = _positive_int(
                project.get("systemview_ram_base")
                or project.get("sysview_ram_base")
            )
            if ram_base:
                defaults["ram_base"] = ram_base
            elif looks_hpm5301:
                defaults["ram_base"] = 0x10000000

            id_shift = _positive_int(
                project.get("systemview_id_shift")
                or project.get("sysview_id_shift")
            )
            if id_shift:
                defaults["id_shift"] = id_shift

            freq = _positive_int(
                project.get("systemview_cpu_freq")
                or project.get("sysview_cpu_freq")
                or project.get("cpu_freq_default")
                or project.get("system_core_clock")
            )
            if freq:
                defaults["cpu_freq"] = freq
                defaults["cpu_freq_source"] = "project_info"

        return defaults

    def _symbol_source_path(self) -> str | None:
        if self._axf and Path(self._axf).exists():
            return self._axf
        try:
            from mklink.project_config import load_project_info
            project = load_project_info(self._project_root) or {}
        except Exception:
            project = {}
        for key in ("elf_path", "axf_path", "bin_path", "hex_path"):
            path = project.get(key) if isinstance(project, dict) else None
            if path and Path(path).exists():
                return str(path)
        return None

    def _read_cpu_clock_hint(self) -> tuple[int, str]:
        for name in ("SystemCoreClock", "hpm_core_clock"):
            try:
                freq = self.read_variable(name)
            except Exception:
                continue
            freq = _positive_int(freq)
            if freq:
                return freq, name
        return 0, ""

    # ------------------------------------------------------------------
    # RTT
    # ------------------------------------------------------------------
    def _read_rtt_down_buffers(self, control_block_addr: int) -> list[dict]:
        descriptor_size = 24
        header = self.read_memory(control_block_addr, 24)
        if len(header) < 24 or header[:10] != b"SEGGER RTT":
            return []

        max_up = int.from_bytes(header[16:20], "little")
        max_down = int.from_bytes(header[20:24], "little")
        if not 1 <= max_up <= 16 or not 1 <= max_down <= 16:
            return []

        down_address = control_block_addr + 24 + max_up * descriptor_size
        raw = self.read_memory(down_address, max_down * descriptor_size)
        if len(raw) != max_down * descriptor_size:
            return []

        buffers = []
        for channel in range(max_down):
            offset = channel * descriptor_size
            buffer_address = int.from_bytes(raw[offset + 4:offset + 8], "little")
            size = int.from_bytes(raw[offset + 8:offset + 12], "little")
            flags = int.from_bytes(raw[offset + 20:offset + 24], "little")
            buffers.append({
                "channel": channel,
                "size": size,
                "mode": flags,
                "active": buffer_address != 0 and 0 < size <= 1024 * 1024,
                "name": "",
            })
        return buffers

    def rtt_start(
        self,
        addr: str | None = None,
        *,
        channel: int = 0,
        search_size: int = 1024,
        mode: int | None = None,
    ) -> dict:
        """启动 RTT 会话。

        Args:
            addr: RTT 控制块地址（None 时从 rtt_config.json 读）。
            channel: RTT 通道号。
            search_size: 探针扫描字节数（仅模式 0 生效）。
            mode: 0=动态搜寻 / 1=静态编译。None 时从 rtt_config.json:rtt_storage_mode 读。
        """
        self._require_connected()
        if self._rtt_session and self._rtt_session._running:
            self._rtt_session.stop()

        # 未显式传入时，从 rtt_config.json 解析
        rtt_cfg = None
        if mode is None or not addr:
            from mklink.project_config import load_rtt_config, resolve_rtt_storage_mode
            rtt_cfg = load_rtt_config(self._project_root)
        if mode is None:
            mode = resolve_rtt_storage_mode(rtt_cfg)
        if mode == 1 and not addr and rtt_cfg:
            addr = rtt_cfg.get("rtt_addr")

        fallback_down_buffers = []
        requested_addr = None
        if addr:
            try:
                requested_addr = int(addr, 0)
                fallback_down_buffers = self._read_rtt_down_buffers(requested_addr)
            except (TypeError, ValueError, OSError, DeviceError):
                fallback_down_buffers = []

        from mklink.rtt import RTTSession
        self._rtt_session = RTTSession(self._bridge, channel=channel)
        result = self._rtt_session.start(
            addr or "",
            search_size=search_size,
            project_root=self._project_root,
            mode=mode,
        )
        if mode == 1 and not result.get("control_block_addr") and fallback_down_buffers:
            self._rtt_session.reset_failed_start()
            result = self._rtt_session.start(
                addr or "",
                search_size=4,
                project_root=self._project_root,
                mode=0,
            )
            result["storage_mode"] = 1
            result["probe_compatibility_mode"] = "bounded-scan"
        result["down_buffer_probe_count"] = len(fallback_down_buffers)
        if not result.get("control_block_addr"):
            raise DeviceError("RTT control block was not found")
        reported_addr = result.get("control_block_addr")
        if (
            fallback_down_buffers
            and requested_addr is not None
            and reported_addr
        ):
            try:
                reported_matches = int(reported_addr, 0) == requested_addr
            except (TypeError, ValueError):
                reported_matches = False
            if reported_matches:
                result["down_buffers"] = fallback_down_buffers
                result["down_buffer_source"] = "target-control-block"
        return result

    def rtt_read(self, duration: float = 10.0) -> str:
        self._require_connected()
        if not self._rtt_session or not self._rtt_session._running:
            raise DeviceError("RTT not started. Call rtt_start() first.")
        return self._rtt_session.read_output(duration=duration)

    def rtt_read_bytes(self, duration: float = 10.0) -> bytes:
        self._require_connected()
        if not self._rtt_session or not self._rtt_session._running:
            raise DeviceError("RTT not started. Call rtt_start() first.")
        return self._rtt_session.read_output_bytes(duration=duration)

    def rtt_write(self, data: bytes | str) -> bool:
        self._require_connected()
        if not self._rtt_session or not self._rtt_session._running:
            raise DeviceError("RTT not started. Call rtt_start() first.")
        if isinstance(data, str):
            data = data.encode("utf-8")
        return self._rtt_session.send_input(data)

    def rtt_stop(self) -> str:
        self._require_connected()
        if not self._rtt_session:
            return ""
        result = self._rtt_session.stop()
        self._rtt_session = None
        return result

    def wait_for_rtt(
        self,
        pattern: str | None = None,
        *,
        timeout: float = 10.0,
        start_if_needed: bool = True,
    ) -> str:
        """Wait for RTT output, optionally matching a pattern.

        If RTT is not running and ``start_if_needed`` is True, starts it
        automatically using config or default address.
        """
        self._require_connected()
        if start_if_needed and (
            not self._rtt_session or not self._rtt_session._running
        ):
            self.rtt_start()

        compiled = re.compile(pattern) if pattern else None
        deadline = time.time() + timeout
        collected = ""

        remaining = timeout
        while remaining > 0:
            chunk = self.rtt_read(min(remaining, 2.0))
            if chunk:
                collected += chunk
                if compiled and compiled.search(collected):
                    return collected
                if pattern and pattern in collected:
                    return collected
            remaining = deadline - time.time()

        if pattern and pattern not in collected:
            if compiled and not compiled.search(collected):
                pass
        return collected

    # ------------------------------------------------------------------
    # SystemView（RTOS 跟踪：RTT 通道 1 二进制流 → SEGGER 事件解码）
    # ------------------------------------------------------------------
    def systemview_start(
        self,
        addr: str | None = None,
        *,
        channel: int = 1,
        search_size: int = 1024,
        mode: int | None = None,
    ) -> dict:
        """启动 SystemView 采集（RTT 通道 1，二进制）。

        Args:
            addr: RTT 控制块地址（None 时从 rtt_config.json 读，与 RTT 共用）。
            channel: SystemView 上行通道号（SEGGER 默认 1）。
            search_size: 探针扫描字节数（仅 mode=0 生效）。
            mode: 0=动态搜寻 / 1=静态编译。None 时从 rtt_config.json 读。
        """
        self._require_connected()
        if self._systemview_session and self._systemview_session._running:
            self._systemview_session.stop()

        if mode is None:
            from mklink.project_config import load_rtt_config, resolve_rtt_storage_mode
            rtt_cfg = load_rtt_config(self._project_root)
            mode = resolve_rtt_storage_mode(rtt_cfg)

        # SystemView 与 RTT 共用同一探针 bridge，二者互斥
        if self._rtt_session and self._rtt_session._running:
            self._rtt_session.stop()
            self._rtt_session = None

        sv_defaults = self._systemview_defaults()
        cpu_freq_hint, cpu_freq_source = self._read_cpu_clock_hint()
        if not cpu_freq_hint:
            cpu_freq_hint = _positive_int(sv_defaults.get("cpu_freq"))
            cpu_freq_source = str(sv_defaults.get("cpu_freq_source") or "")

        from mklink.systemview import SystemViewSession
        from mklink.systemview_parser import SystemViewParser
        self._systemview_session = SystemViewSession(self._bridge, channel=channel)
        result = self._systemview_session.start(
            addr or "",
            search_size=search_size,
            project_root=self._project_root,
            mode=mode,
        )
        # 每次 start 重建解码器，累计时间戳与 name 映射
        self._systemview_parser = SystemViewParser()
        # SEGGER ID 还原默认（INIT 包常被 16KB 环形缓冲在高事件率下覆盖）：
        # STM32 SRAM base 0x20000000 + ID_SHIFT=2（SEGGER 默认，4 字节对齐）。
        # 这样 task_id 还原成真实 rt_thread 指针，便于直接读线程名。INIT 若抓到
        # 会覆盖为同值。非 STM32 工程可后续从 MCU profile 取 ram base。
        self._systemview_parser._ram_base = int(sv_defaults["ram_base"])
        self._systemview_parser._id_shift = int(sv_defaults["id_shift"])
        # SystemCoreClock must be read before SystemView switches the bridge
        # into binary stream mode; command/variable reads are unavailable there.
        if cpu_freq_hint:
            self._systemview_parser._cpu_freq = cpu_freq_hint
            result.setdefault("cpu_freq_hint", cpu_freq_hint)
            if cpu_freq_source:
                result.setdefault("cpu_freq_source", cpu_freq_source)
        result.setdefault("systemview_ram_base", _fmt_hex(int(sv_defaults["ram_base"])))
        result.setdefault("systemview_id_shift", int(sv_defaults["id_shift"]))
        return result

    def systemview_read_bytes(
        self, duration: float = 2.0, max_bytes: int | None = None
    ) -> bytes:
        """读取 duration 秒的原始 SystemView 字节（未解码）。"""
        self._require_connected()
        if not self._systemview_session or not self._systemview_session._running:
            raise DeviceError("SystemView not started. Call systemview_start() first.")
        return self._systemview_session.read_bytes(
            duration=duration, max_bytes=max_bytes
        )

    def systemview_read(self, duration: float = 2.0) -> dict:
        """读取并解码 duration 秒的 SystemView 事件。

        用持久化解码器（跨多次 read 累计绝对时间戳与 task/isr name 映射）。
        返回 ``{"events": [...], "synced", "abs_time", "cpu_freq",
        "task_names", "isr_names", "dropped_bytes", "dropped_packets"}``。
        """
        self._require_connected()
        if not self._systemview_session or not self._systemview_session._running:
            raise DeviceError("SystemView not started. Call systemview_start() first.")
        raw = self._systemview_session.read_bytes(duration=duration)
        events = self._systemview_parser.feed(raw) if raw else []
        p = self._systemview_parser
        return {
            "events": events,
            "event_count": len(events),
            "bytes_read": len(raw),
            "synced": p.synced,
            "abs_time": p.abs_time,
            "cpu_freq": p.cpu_freq,
            "task_names": dict(p._task_names),
            "isr_names": dict(p._isr_names),
            "dropped_bytes": p.dropped_bytes,
            "dropped_packets": p.dropped_packets,
        }

    def systemview_stop(self) -> None:
        """停止 SystemView 采集。"""
        self._require_connected()
        if not self._systemview_session:
            return
        self._systemview_session.stop()
        self._systemview_session = None
        self._systemview_parser = None

    def systemview_resolve_task_names(
        self, task_ids: list[int]
    ) -> dict[int, str]:
        """直接读 RT-Thread 线程名（不依赖开机 INIT 包）。

        task_id（解码器已还原为真实指针）即 ``rt_thread*``。RT-Thread 的
        ``rt_thread`` 继承 ``rt_object``，对象以 ``name[RT_NAME_MAX]`` 开头，
        紧随其后的 ``type`` 必须是 Thread（可带 Static 标志）。同时验证对象
        类型和名称，避免把错误对齐产生的 task_id 指向的普通 RAM ASCII 片段
        误报为线程名。
        """
        self._require_connected()
        name_max = Device._systemview_rt_name_max(self)
        list_offset = _aligned_object_list_offset(name_max)
        names: dict[int, str] = {}
        for tid in task_ids:
            task_id = int(tid)
            if not _is_sram_pointer(task_id):
                continue
            try:
                raw = self.read_memory(task_id, list_offset + 8)
            except Exception:
                continue
            name = _decode_rt_thread_name(raw, name_max)
            if name:
                names[task_id] = name
        return names

    def _systemview_rt_name_max(self) -> int:
        root = Path(getattr(self, "_project_root", "") or ".")
        candidates = (
            (
                root / "rtconfig.h",
                re.compile(r"^\s*#define\s+RT_NAME_MAX\s+(\d+)", re.M),
            ),
            (
                root / ".config",
                re.compile(r"^CONFIG_RT_NAME_MAX=(\d+)", re.M),
            ),
        )
        for path, pattern in candidates:
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = pattern.search(content)
            if match:
                value = int(match.group(1))
                if 1 <= value <= 64:
                    return value
        return _RT_NAME_MAX_DEFAULT

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    def read_memory(self, address: int, size: int) -> bytes:
        self._require_connected()
        from mklink.memory_access import parse_read_ram_response
        cmd = f"cmd.read_ram(0x{address:08X}, {size})"
        raw = self._bridge.send_command(cmd, timeout=10.0)
        return parse_read_ram_response(raw)

    def write_memory(self, address: int, data: bytes) -> None:
        self._require_connected()
        if not data:
            return
        # cmd.write_ram 的逐字节参数在当前探针固件不稳定（写入不生效，回读为空）；
        # 改用 cmd.flush_memory 的 bytes 表达式（与 MCP flush_memory 一致）：
        # 全相同字节折叠为短表达式（单条可达 12 KiB），非重复数据按 30B 分块，
        # 保证命令串 < 230（PIKA_LINE_BUFF 上限）。详见 references/flush-memory.md。
        CHUNK = 30
        i = 0
        while i < len(data):
            rest = data[i:]
            if all(b == rest[0] for b in rest):
                seg, step = rest, len(rest)
                expr = f"bytes([0x{seg[0]:02X}])*{len(seg)}"
            else:
                seg, step = rest[:CHUNK], CHUNK
                expr = "bytes([" + ", ".join(f"0x{b:02X}" for b in seg) + "])"
            cmd = f"cmd.flush_memory([(0x{address + i:08X}, {expr})])"
            self._bridge.send_command(cmd, timeout=10.0)
            i += step

    # ------------------------------------------------------------------
    # Variables
    # ------------------------------------------------------------------
    def read_variable(self, name: str) -> Any:
        self._require_connected()
        if not self._dwarf_info:
            return self._read_variable_from_map(name)
        from mklink.watch import resolve_variable_path, decode_value
        try:
            addr, type_name, size, enum_values = resolve_variable_path(
                self._dwarf_info, name
            )
        except KeyError:
            descriptor = self.symbol_catalog.by_path(name) if self.symbol_catalog else None
            if descriptor is None:
                return self._read_variable_from_map(name)
            from mklink.symbol_catalog import decode_descriptor

            return decode_descriptor(
                descriptor, self.read_memory(descriptor.address, descriptor.size),
            )
        raw = self.read_memory(addr, size)
        return decode_value(raw, type_name, enum_values, known_size=size)

    def _read_variable_from_map(self, name: str) -> Any:
        source = self._symbol_source_path()
        if not source:
            raise DeviceError(
                "No AXF/ELF/MAP source available. Pass axf= to connect() for variable access."
            )
        from mklink.watch import resolve_map_source_variable, decode_value
        resolved = resolve_map_source_variable(source, name)
        if not resolved:
            raise KeyError(f"variable '{name}' not found or has no address")
        addr, type_name, size = resolved
        if not size:
            size = 4
        raw = self.read_memory(addr, size)
        return decode_value(raw, type_name, None, known_size=size)

    def write_variable(self, name: str, value: int) -> None:
        self._require_connected()
        if not self._dwarf_info:
            raise DeviceError(
                "No AXF/ELF loaded. Pass axf= to connect() for variable access."
            )
        from mklink.watch import resolve_variable_path, TYPE_FORMATS
        try:
            addr, type_name, size, _ = resolve_variable_path(self._dwarf_info, name)
        except KeyError:
            descriptor = self.symbol_catalog.by_path(name) if self.symbol_catalog else None
            if descriptor is None:
                raise
            from mklink.symbol_catalog import encode_descriptor

            self.write_memory(descriptor.address, encode_descriptor(descriptor, value))
            return
        key = type_name.strip().lower()
        fmt_entry = TYPE_FORMATS.get(key)
        if fmt_entry:
            fmt, _ = fmt_entry
        else:
            fmt = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}.get(size, "<I")
        data = struct.pack(fmt, value)
        self.write_memory(addr, data)

    # ------------------------------------------------------------------
    # Registers
    # ------------------------------------------------------------------
    def read_register(self, name: str) -> int:
        self._require_connected()
        from mklink.registers import resolve_register
        reg = resolve_register(name)
        addr = reg.address
        raw = self.read_memory(addr, 4)
        if len(raw) < 4:
            raise DeviceError(f"Failed to read register {name}")
        return struct.unpack("<I", raw[:4])[0]

    # ------------------------------------------------------------------
    # Debug control
    # ------------------------------------------------------------------
    def halt(self):
        self._require_connected()
        from mklink.debug_control import halt_cpu
        return halt_cpu(self._bridge)

    def resume(self):
        self._require_connected()
        from mklink.debug_control import resume_cpu
        return resume_cpu(self._bridge)

    def step(self):
        self._require_connected()
        from mklink.debug_control import step_cpu
        return step_cpu(self._bridge)

    def set_breakpoint(self, address: int, slot: int | None = None) -> int:
        self._require_connected()
        from mklink.debug_control import set_breakpoint
        return set_breakpoint(self._bridge, address, slot)

    def clear_breakpoint(self, slot: int) -> None:
        self._require_connected()
        from mklink.debug_control import clear_breakpoint
        clear_breakpoint(self._bridge, slot)

    def clear_all_breakpoints(self) -> int:
        self._require_connected()
        from mklink.debug_control import clear_all_breakpoints
        return clear_all_breakpoints(self._bridge)

    def read_core_registers(self) -> dict[str, int]:
        self._require_connected()
        from mklink.debug_control import read_all_core_registers
        return read_all_core_registers(self._bridge)

    # ------------------------------------------------------------------
    # HardFault
    # ------------------------------------------------------------------
    def check_hardfault(self) -> dict[str, int] | None:
        """Read fault registers and return them if a fault occurred."""
        self._require_connected()
        try:
            cfsr = self.read_register("SCB.CFSR")
            hfsr = self.read_register("SCB.HFSR")
            if cfsr == 0 and hfsr == 0:
                return None
            return {"SCB.CFSR": cfsr, "SCB.HFSR": hfsr}
        except Exception:
            return None

    def decode_hardfault(
        self, fault_regs: dict[str, int] | None = None
    ) -> HardFaultReport | None:
        """Decode fault registers into a human-readable report."""
        self._require_connected()
        if fault_regs is None:
            fault_regs = self.check_hardfault()
        if not fault_regs:
            return None

        from mklink.hardfault import (
            decode_cfsr, decode_hfsr,
            addr2line, build_call_stack, find_exception_stack,
        )

        cfsr = fault_regs.get("SCB.CFSR", 0)
        hfsr = fault_regs.get("SCB.HFSR", 0)
        cfsr_flags = decode_cfsr(cfsr)
        hfsr_flags = decode_hfsr(hfsr)

        stack_frame = None
        source_locations = None
        fault_function = None
        fault_location = None
        exception_stack = None
        call_stack: list[dict[str, Any]] = []
        core_registers = None
        executable_ranges = [(0x00000000, 0x20000000)]
        function_symbols = []

        if self._axf:
            try:
                from mklink.elf_backend import list_elf_sections, list_elf_symbols

                executable_ranges = [
                    (section.address, section.address + section.size)
                    for section in list_elf_sections(
                        self._axf,
                        backend=self._elf_backend,
                        project_root=self._project_root,
                    )
                    if section.size > 0 and section.flags & 0x4
                ] or executable_ranges
                function_symbols = list_elf_symbols(
                    self._axf,
                    backend=self._elf_backend,
                    project_root=self._project_root,
                )
            except Exception:
                pass

        try:
            try:
                self.halt()
            except Exception:
                # A locked-up core can already be halted even if the halt helper
                # cannot obtain a complete debug-state snapshot.
                pass
            core_registers = self.read_core_registers()
            stack_regions: dict[str, tuple[int, bytes]] = {}
            for pointer_name in ("msp", "psp"):
                pointer = int(core_registers.get(pointer_name, 0)) & ~3
                if not _is_sram_pointer(pointer):
                    continue
                try:
                    stack_regions[pointer_name] = (pointer, self.read_memory(pointer, 512))
                except Exception:
                    continue

            located = find_exception_stack(
                core_registers,
                stack_regions,
                executable_ranges,
            )
            if located:
                stack_frame = located["frame"]
                stack_data = located.pop("stack_data")
                exception_stack = located
                stack_base = int(exception_stack["pointer_address"])
                call_stack = build_call_stack(
                    stack_frame,
                    frame_address=int(exception_stack["frame_address"]),
                    stack_base=stack_base,
                    stack_data=stack_data,
                    executable_ranges=executable_ranges,
                    symbols=function_symbols,
                )
                if self._axf and call_stack:
                    lookups = [int(item["lookup_address"]) for item in call_stack]
                    resolved = addr2line(
                        self._axf,
                        *lookups,
                        backend=self._elf_backend,
                        project_root=self._project_root,
                    )
                    source_locations = {}
                    for item in call_stack:
                        location = resolved.get(int(item["lookup_address"]))
                        if location:
                            item["location"] = location
                            source_locations[int(item["address"])] = location
                if call_stack:
                    fault_function = call_stack[0].get("function")
                    fault_location = call_stack[0].get("location")
        except Exception:
            pass

        flags = cfsr_flags + hfsr_flags
        summary = "; ".join(flags) if flags else "Unknown fault"

        return HardFaultReport(
            cfsr=cfsr,
            hfsr=hfsr,
            cfsr_flags=cfsr_flags,
            hfsr_flags=hfsr_flags,
            stack_frame=stack_frame,
            source_locations=source_locations,
            summary=summary,
            fault_function=fault_function,
            fault_location=fault_location,
            exception_stack=exception_stack,
            call_stack=call_stack,
            core_registers=core_registers,
        )

    # ------------------------------------------------------------------
    # Memory map
    # ------------------------------------------------------------------
    def memory_map(self) -> dict:
        self._require_connected()
        if not self._axf:
            raise DeviceError("No AXF/ELF loaded for memory map analysis.")
        from mklink.memmap import analyze_memmap
        return analyze_memmap(
            self._axf,
            backend=self._elf_backend,
            project_root=self._project_root,
        )


# ======================================================================
# Module-level factory functions
# ======================================================================

def connect(
    *,
    port: str | None = None,
    preferred_port: str | None = None,
    axf: str | None = None,
    mcu: str | None = None,
    project_root: str = ".",
    elf_backend: str | None = None,
) -> Device:
    """Create and connect a Device.

    Returns a connected Device ready for use. Use as a context manager::

        with mklink.connect(axf="build/out.elf") as dev:
            dev.flash("build/out.hex")

    Args:
        port: Explicit COM port. Auto-detected if not specified.
        preferred_port: Soft preference used before automatic discovery.
        axf: Path to AXF/ELF file for symbol resolution.
        mcu: MCU profile hint (e.g. "stm32f4").
        project_root: Project root for .mklink/ config lookup.
        elf_backend: Explicit ELF parser backend (builtin or external).
    """
    dev = Device(
        port=port,
        preferred_port=preferred_port,
        axf=axf,
        mcu=mcu,
        project_root=project_root,
        elf_backend=elf_backend,
    )
    dev._connect()
    # HIL-Infra lockd 协议互操作：connect 成功即持有 transport_usb-serial_<COM>，
    # close() 释放；与 hil_core.lockd 编排器在同一锁名上互斥（T5 原生对齐）。
    from mklink.hil_lock import HilFileLock, transport_lock_name
    hil_lock = HilFileLock(
        transport_lock_name("usb-serial", dev.port),
        owner_id=f"mklink-{os.getpid()}",
    )
    try:
        hil_lock.acquire()
    except Exception:
        dev.close()
        raise
    dev._hil_lock = hil_lock
    # 长会话自动续租：按 lease/3 心跳防止超时会话的锁被回收；close 停止
    renew_stop = threading.Event()

    def _hil_renew_loop():
        while not renew_stop.wait(hil_lock.lease_s / 3):
            if not hil_lock.renew():
                return  # 所有权丢失（被回收）——停止续租，断连时如实释放

    dev._hil_renew_stop = renew_stop
    dev._hil_renew_thread = threading.Thread(target=_hil_renew_loop,
                                             daemon=True, name="hil-lock-renew")
    dev._hil_renew_thread.start()
    return dev


def discover_all() -> list[dict]:
    """Find all connected MKLink probes.

    Returns a list of dicts, each with keys:
        port, description, manufacturer
    """
    from mklink._types import KNOWN_MKLINK_VID_PIDS
    from mklink.discovery import list_available_ports, _probe_port
    ports = list_available_ports()
    results: list[dict] = []
    for p in ports:
        mfr = (p.get("manufacturer") or "").lower()
        desc = (p.get("description") or "").lower()
        vid_pid = (p.get("vid"), p.get("pid"))
        known_identity = (
            any(kw in mfr for kw in ("microkeen", "microlink", "mklink"))
            or any(kw in desc for kw in ("microkeen", "microlink", "mklink"))
            or vid_pid in KNOWN_MKLINK_VID_PIDS
        )
        if not known_identity and not _probe_port(p["device"]):
            continue
        results.append({
            "port": p["device"],
            "description": p.get("description", ""),
            "manufacturer": p.get("manufacturer", ""),
        })
    return results
