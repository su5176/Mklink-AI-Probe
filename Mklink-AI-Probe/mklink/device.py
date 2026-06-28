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
        self._systemview_session = None
        self._systemview_parser = None
        self._dwarf_info = None
        self._axf_error = None  # reason DWARF load was skipped (e.g. readelf missing)
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
            try:
                self._dwarf_info = load_dwarf_info(self._axf)
                self._axf_error = None
            except Exception as e:
                # readelf missing / unreadable ELF / DWARF parse error: never
                # let this crash connect() — the bridge is already up. Record
                # the reason so axf_status / the MCP layer can surface it and
                # guide the user (e.g. install the GNU Arm toolchain).
                self._dwarf_info = None
                self._axf_error = str(e)

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
        """返回 AXF 解析状态摘要（含工具链可用性与失败原因）。"""
        from mklink.toolchain import status as toolchain_status
        tc = toolchain_status()
        if not self._dwarf_info:
            out: dict = {"loaded": False, "axf_path": self._axf,
                         "readelf_available": tc["readelf_available"]}
            if self._axf_error:
                out["error"] = self._axf_error
            elif self._axf and not Path(self._axf).exists():
                out["error"] = f"AXF file not found: {self._axf}"
            return out
        info = self._dwarf_info
        return {
            "loaded": True,
            "axf_path": self._axf,
            "variable_count": len(info.variables),
            "struct_count": len(info.structs),
            "enum_count": len(info.enums),
            "readelf_available": tc["readelf_available"],
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
        if mode is None:
            from mklink.project_config import load_rtt_config, resolve_rtt_storage_mode
            rtt_cfg = load_rtt_config(self._project_root)
            mode = resolve_rtt_storage_mode(rtt_cfg)

        from mklink.rtt import RTTSession
        self._rtt_session = RTTSession(self._bridge, channel=channel)
        return self._rtt_session.start(
            addr or "",
            search_size=search_size,
            project_root=self._project_root,
            mode=mode,
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

        cpu_freq_hint = 0
        if self._dwarf_info:
            try:
                freq = self.read_variable("SystemCoreClock")
                if isinstance(freq, int) and freq > 0:
                    cpu_freq_hint = freq
            except Exception:
                pass

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
        self._systemview_parser._ram_base = 0x20000000
        self._systemview_parser._id_shift = 2
        # SystemCoreClock must be read before SystemView switches the bridge
        # into binary stream mode; command/variable reads are unavailable there.
        if cpu_freq_hint:
            self._systemview_parser._cpu_freq = cpu_freq_hint
            result.setdefault("cpu_freq_hint", cpu_freq_hint)
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
        ``rt_thread`` 继承 ``rt_object``，``name[RT_NAME_MAX]`` 在 ``rt_object``
        内（4.x 典型偏移 12：list[8]+type[1]+color[1]+flag[2]），版本间有差异。
        故扫描候选偏移 [8..28]，对每处取以 ``\\0`` 结尾的 C 标识符（线程名如
        ``idle/main/tshell``）；指针字段不会匹配标识符模式，故能准确锁定 name。
        """
        import re
        self._require_connected()
        name_re = re.compile(rb'^[A-Za-z_][A-Za-z0-9_]*')
        names: dict[int, str] = {}
        for tid in task_ids:
            for off in (0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48):
                try:
                    raw = self.read_memory(int(tid) + off, 8)
                except Exception:
                    break  # bridge 出错则放弃此 tid
                nul = raw.find(b'\x00')
                cand = raw if nul < 0 else raw[:nul]
                m = name_re.match(cand)
                if m and len(m.group()) >= 2:
                    names[int(tid)] = m.group().decode('ascii')
                    break
        return names

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
        from mklink.memmap import analyze_memmap
        return analyze_memmap(self._axf)


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
