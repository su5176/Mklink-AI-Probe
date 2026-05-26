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

import re
import struct
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


class DeviceError(Exception):
    pass


class DeviceNotConnectedError(DeviceError):
    pass


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
        axf: str | None = None,
        mcu: str | None = None,
        project_root: str = ".",
    ):
        self._port = port
        self._axf = axf
        self._mcu_hint = mcu
        self._project_root = project_root
        self._bridge = None
        self._flash = None
        self._rtt_session = None
        self._dwarf_info = None
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
        from mklink.discovery import find_mklink_cdc_port
        from mklink.cli import _resolve_port

        port = self._port
        if port is None:
            port = _resolve_port(None, self._project_root)
            self._port = port

        self._bridge = MKLinkSerialBridge(port)
        if not self._bridge.connect():
            raise DeviceNotConnectedError(
                f"Failed to connect to MKLink on {port}"
            )
        self._connected = True

        from mklink.flash import MKLinkFlash
        self._flash = MKLinkFlash(self._bridge)

        if self._axf:
            self._load_dwarf_info()

    def close(self) -> None:
        if self._rtt_session and self._rtt_session._running:
            try:
                self._rtt_session.stop()
            except Exception:
                pass
        if self._bridge:
            self._bridge.close()
        self._bridge = None
        self._flash = None
        self._connected = False

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
        from mklink.dwarf_parser import load_dwarf_info
        if self._axf and Path(self._axf).exists():
            self._dwarf_info = load_dwarf_info(self._axf)

    def parse_axf(self, axf_path: str | None = None) -> dict:
        """手动触发 AXF 解析。返回解析结果摘要。"""
        if axf_path:
            self._axf = axf_path
        if not self._axf:
            return {"loaded": False, "error": "No AXF path set"}
        if not Path(self._axf).exists():
            return {"loaded": False, "error": f"AXF not found: {self._axf}"}
        try:
            self._load_dwarf_info()
            return self.axf_status
        except Exception as e:
            return {"loaded": False, "error": str(e)}

    @property
    def axf_status(self) -> dict:
        """返回 AXF 解析状态摘要。"""
        if not self._dwarf_info:
            return {"loaded": False, "axf_path": self._axf}
        info = self._dwarf_info
        return {
            "loaded": True,
            "axf_path": self._axf,
            "variable_count": len(info.variables),
            "struct_count": len(info.structs),
            "enum_count": len(info.enums),
        }

    # ------------------------------------------------------------------
    # Flash
    # ------------------------------------------------------------------
    def flash(
        self,
        firmware: str,
        *,
        verify: bool = True,
        reset_after: bool = True,
        progress_callback=None,
    ) -> dict:
        self._require_connected()

        # Resolve MCU profile: prefer self._mcu hint > config mcu_key > idcode match
        from mklink.profiles import load_mcu_profiles, match_mcu_by_idcode, match_mcu_by_device
        profiles = load_mcu_profiles()
        mcu_profile = None
        if self._mcu_hint and self._mcu_hint in profiles:
            mcu_profile = profiles[self._mcu_hint]
        if not mcu_profile and self._project_root:
            from mklink.project_config import load_config
            cfg = load_config(self._project_root) or {}
            cfg_mcu = cfg.get("mcu_key")
            if cfg_mcu and cfg_mcu in profiles:
                mcu_profile = profiles[cfg_mcu]
        if not mcu_profile:
            mcu_profile = self._get_mcu_profile()

        flash_base = "0x08000000"
        ram_base = "0x20000000"
        if mcu_profile:
            flash_base = mcu_profile.get("flash_base", flash_base)
            ram_base = mcu_profile.get("ram_base", ram_base)

        # Setup SWD clock (prefer config, fallback to profile default)
        swd_clock = 1000000
        if self._project_root:
            from mklink.project_config import load_config
            cfg = load_config(self._project_root) or {}
            cfg_clock = cfg.get("swd_clock")
            if cfg_clock:
                swd_clock = int(cfg_clock)
        elif mcu_profile:
            swd_clock = mcu_profile.get("swd_clock_default", swd_clock)
        self._flash.set_swd_clock(swd_clock)

        # Load FLM
        flm_path = None
        if mcu_profile:
            flm_path = mcu_profile.get("flm_path", "")
            if flm_path and not flm_path.startswith("/"):
                flm_path = "/" + flm_path
        if flm_path:
            if not self._flash.load_flm(flm_path, flash_base, ram_base):
                raise DeviceError(f"FLM load failed: {flm_path}")

        ext = Path(firmware).suffix.lower()
        if ext == ".hex":
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

        if reset_after:
            self.reset()

        return result

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
        self._bridge.send_command("cmd.set_beep_on()", timeout=3.0)
        time.sleep(0.05)
        self._bridge.send_command("cmd.set_beep_off()", timeout=3.0)

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

    # ------------------------------------------------------------------
    # RTT
    # ------------------------------------------------------------------
    def rtt_start(
        self,
        addr: str | None = None,
        *,
        channel: int = 0,
        search_size: int = 1024,
    ) -> dict:
        self._require_connected()
        if self._rtt_session and self._rtt_session._running:
            self._rtt_session.stop()
        from mklink.rtt import RTTSession
        self._rtt_session = RTTSession(self._bridge, channel=channel)
        return self._rtt_session.start(
            addr or "", search_size=search_size, project_root=self._project_root
        )

    def rtt_read(self, duration: float = 10.0) -> str:
        self._require_connected()
        if not self._rtt_session or not self._rtt_session._running:
            raise DeviceError("RTT not started. Call rtt_start() first.")
        return self._rtt_session.read_output(duration=duration)

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
        addr_s = f"0x{address:08X}"
        args = ", ".join(f"0x{b:02X}" for b in data)
        cmd = f"cmd.write_ram({addr_s}, {args})"
        self._bridge.send_command(cmd, timeout=10.0)

    # ------------------------------------------------------------------
    # Variables
    # ------------------------------------------------------------------
    def read_variable(self, name: str) -> Any:
        self._require_connected()
        if not self._dwarf_info:
            raise DeviceError(
                "No AXF/ELF loaded. Pass axf= to connect() for variable access."
            )
        from mklink.watch import resolve_variable_path, decode_value
        addr, type_name, size, enum_values = resolve_variable_path(
            self._dwarf_info, name
        )
        raw = self.read_memory(addr, size)
        return decode_value(raw, type_name, enum_values, known_size=size)

    def write_variable(self, name: str, value: int) -> None:
        self._require_connected()
        if not self._dwarf_info:
            raise DeviceError(
                "No AXF/ELF loaded. Pass axf= to connect() for variable access."
            )
        from mklink.watch import resolve_variable_path, TYPE_FORMATS
        addr, type_name, size, _ = resolve_variable_path(self._dwarf_info, name)
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
            parse_exception_stack_frame, addr2line, format_hardfault_report,
        )

        cfsr = fault_regs.get("SCB.CFSR", 0)
        hfsr = fault_regs.get("SCB.HFSR", 0)
        cfsr_flags = decode_cfsr(cfsr)
        hfsr_flags = decode_hfsr(hfsr)

        stack_frame = None
        source_locations = None
        if self._axf:
            try:
                frame_raw = self.read_memory(0xE000EDF8, 32)
                stack_frame = parse_exception_stack_frame(frame_raw)
                if stack_frame:
                    addrs = [stack_frame.get("pc", 0), stack_frame.get("lr", 0)]
                    source_locations = addr2line(self._axf, *addrs)
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
        )

    # ------------------------------------------------------------------
    # Memory map
    # ------------------------------------------------------------------
    def memory_map(self) -> dict:
        self._require_connected()
        if not self._axf:
            raise DeviceError("No AXF/ELF loaded for memory map analysis.")
        from mklink.memmap import parse_sections
        return parse_sections(self._axf)


# ======================================================================
# Module-level factory functions
# ======================================================================

def connect(
    *,
    port: str | None = None,
    axf: str | None = None,
    mcu: str | None = None,
    project_root: str = ".",
) -> Device:
    """Create and connect a Device.

    Returns a connected Device ready for use. Use as a context manager::

        with mklink.connect(axf="build/out.elf") as dev:
            dev.flash("build/out.hex")

    Args:
        port: COM port. Auto-detected if not specified.
        axf: Path to AXF/ELF file for symbol resolution.
        mcu: MCU profile hint (e.g. "stm32f4").
        project_root: Project root for .mklink/ config lookup.
    """
    dev = Device(port=port, axf=axf, mcu=mcu, project_root=project_root)
    dev._connect()
    return dev


def discover_all() -> list[dict]:
    """Find all connected MKLink probes.

    Returns a list of dicts, each with keys:
        port, description, manufacturer
    """
    from mklink.discovery import list_available_ports
    ports = list_available_ports()
    return [
        {
            "port": p["device"],
            "description": p.get("description", ""),
            "manufacturer": p.get("manufacturer", ""),
        }
        for p in ports
    ]
