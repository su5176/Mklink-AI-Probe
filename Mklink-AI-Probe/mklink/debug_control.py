"""Cortex-M CPU debug control via SWD memory-mapped registers.

Provides halt, resume, and FPB hardware breakpoint operations
using the existing write-ram / read-ram bridge commands.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from mklink.bridge import MKLinkSerialBridge
from mklink.memory_access import parse_read_ram_response


# --- Cortex-M Debug Registers (PPB) ---
DHCSR = 0xE000EDF0  # Debug Halting Control and Status Register
DHCSR_KEY = 0xA05F0000
DHCSR_C_DEBUGEN = 1 << 0
DHCSR_C_HALT = 1 << 1
DHCSR_C_STEP = 1 << 2
DHCSR_S_HALT = 1 << 17
DHCSR_S_LOCKUP = 1 << 19

# --- FPB (Flash Patch and Breakpoint) ---
FP_CTRL = 0xE0002000
FP_COMP_BASE = 0xE0002008  # FP_COMP0, stride 4 bytes
FP_CTRL_ENABLE = 1 << 0
FP_CTRL_KEY = 1 << 1

# FPB revision detection
FP_REMAP = 0xE0002004


@dataclass
class BreakpointSlot:
    index: int
    address: int
    enabled: bool = True


@dataclass
class DebugState:
    halted: bool = False
    lockup: bool = False
    dhcsr_raw: int = 0
    num_breakpoints: int = 0
    breakpoints: list[BreakpointSlot] = field(default_factory=list)


def _read_u32(bridge: MKLinkSerialBridge, addr: int) -> int:
    resp = bridge.send_command(f"cmd.read_ram(0x{addr:08X}, 4)", timeout=5.0)
    data = parse_read_ram_response(resp)
    if len(data) < 4:
        raise RuntimeError(f"read_ram(0x{addr:08X}) returned {len(data)} bytes, expected 4")
    return struct.unpack("<I", data[:4])[0]


def _write_u32(bridge: MKLinkSerialBridge, addr: int, value: int) -> None:
    b = struct.pack("<I", value)
    cmd = f"cmd.write_ram(0x{addr:08X}, 0x{b[0]:02X}, 0x{b[1]:02X}, 0x{b[2]:02X}, 0x{b[3]:02X})"
    bridge.send_command(cmd, timeout=5.0)


def read_debug_state(bridge: MKLinkSerialBridge) -> DebugState:
    """Read current CPU debug state."""
    dhcsr = _read_u32(bridge, DHCSR)
    state = DebugState(
        halted=bool(dhcsr & DHCSR_S_HALT),
        lockup=bool(dhcsr & DHCSR_S_LOCKUP),
        dhcsr_raw=dhcsr,
    )

    # Read FP_CTRL to get number of comparators
    fp_ctrl = _read_u32(bridge, FP_CTRL)
    state.num_breakpoints = (fp_ctrl >> 4) & 0x0F  # NUM_CODE field [7:4]

    # Read each comparator
    for i in range(state.num_breakpoints):
        comp_addr = FP_COMP_BASE + i * 4
        comp_val = _read_u32(bridge, comp_addr)
        if comp_val & 0x01:  # ENABLE bit
            # FPBv1: address in bits [28:2], REPLACE in [31:30]
            # FPBv2: address in bits [31:1]
            bp_addr = comp_val & 0x1FFFFFFC
            state.breakpoints.append(BreakpointSlot(index=i, address=bp_addr, enabled=True))

    return state


def halt_cpu(bridge: MKLinkSerialBridge) -> DebugState:
    """Halt the CPU via DHCSR."""
    _write_u32(bridge, DHCSR, DHCSR_KEY | DHCSR_C_DEBUGEN | DHCSR_C_HALT)
    return read_debug_state(bridge)


def resume_cpu(bridge: MKLinkSerialBridge) -> DebugState:
    """Resume CPU execution. If halted at a breakpoint, step past it first."""
    state = read_debug_state(bridge)
    if state.halted and state.breakpoints:
        # Step past the breakpoint instruction before resuming
        _write_u32(bridge, DHCSR, DHCSR_KEY | DHCSR_C_DEBUGEN | DHCSR_C_HALT | DHCSR_C_STEP)
        import time
        time.sleep(0.001)
    _write_u32(bridge, DHCSR, DHCSR_KEY | DHCSR_C_DEBUGEN)
    return read_debug_state(bridge)


def step_cpu(bridge: MKLinkSerialBridge) -> DebugState:
    """Single-step the CPU (execute one instruction)."""
    _write_u32(bridge, DHCSR, DHCSR_KEY | DHCSR_C_DEBUGEN | DHCSR_C_HALT | DHCSR_C_STEP)
    return read_debug_state(bridge)


def get_num_breakpoints(bridge: MKLinkSerialBridge) -> int:
    """Get the number of FPB hardware breakpoint comparators available."""
    fp_ctrl = _read_u32(bridge, FP_CTRL)
    return (fp_ctrl >> 4) & 0x0F


def set_breakpoint(bridge: MKLinkSerialBridge, address: int, slot: int | None = None) -> int:
    """Set a hardware breakpoint at the given Flash address.

    Args:
        bridge: Connected MKLink bridge
        address: Flash address (must be in code region, < 0x20000000)
        slot: Specific comparator slot (0-5), or None for auto-assign

    Returns:
        The slot index used

    Raises:
        ValueError: If address is not in Flash or no free slot available
    """
    if address >= 0x20000000:
        raise ValueError(f"FPB breakpoints only work in Flash region (< 0x20000000), got 0x{address:08X}")

    num_comp = get_num_breakpoints(bridge)
    if num_comp == 0:
        raise RuntimeError("FPB reports 0 comparators — hardware may not support breakpoints")

    # Enable FPB unit if not already
    fp_ctrl = _read_u32(bridge, FP_CTRL)
    if not (fp_ctrl & FP_CTRL_ENABLE):
        _write_u32(bridge, FP_CTRL, FP_CTRL_KEY | FP_CTRL_ENABLE)

    if slot is not None:
        if slot >= num_comp:
            raise ValueError(f"Slot {slot} out of range (max {num_comp - 1})")
    else:
        # Find first free slot
        for i in range(num_comp):
            comp_val = _read_u32(bridge, FP_COMP_BASE + i * 4)
            if not (comp_val & 0x01):
                slot = i
                break
        if slot is None:
            raise ValueError(f"All {num_comp} breakpoint slots are in use")

    # FPBv1 encoding: address[28:2] | REPLACE[31:30] | ENABLE[0]
    # For Thumb instructions: if bit[1] of address is 0 → REPLACE=01 (lower halfword)
    #                         if bit[1] of address is 1 → REPLACE=10 (upper halfword)
    addr_aligned = address & 0x1FFFFFFC
    if address & 0x02:
        replace = 0x80000000  # REPLACE = 10 → upper halfword
    else:
        replace = 0x40000000  # REPLACE = 01 → lower halfword

    comp_val = replace | addr_aligned | 0x01  # ENABLE
    _write_u32(bridge, FP_COMP_BASE + slot * 4, comp_val)

    return slot


def clear_breakpoint(bridge: MKLinkSerialBridge, slot: int) -> None:
    """Clear a specific breakpoint slot."""
    _write_u32(bridge, FP_COMP_BASE + slot * 4, 0x00000000)


def clear_all_breakpoints(bridge: MKLinkSerialBridge) -> int:
    """Clear all breakpoint slots. Returns number cleared."""
    num_comp = get_num_breakpoints(bridge)
    cleared = 0
    for i in range(num_comp):
        comp_val = _read_u32(bridge, FP_COMP_BASE + i * 4)
        if comp_val & 0x01:
            _write_u32(bridge, FP_COMP_BASE + i * 4, 0x00000000)
            cleared += 1
    return cleared


def list_breakpoints(bridge: MKLinkSerialBridge) -> list[BreakpointSlot]:
    """List all active breakpoints."""
    state = read_debug_state(bridge)
    return state.breakpoints


def resolve_function_address(source: str, name: str) -> int | None:
    """Resolve a function name to its Flash address using readelf.

    Args:
        source: Path to ELF/AXF file
        name: Function name to look up

    Returns:
        Address of the function, or None if not found
    """
    import subprocess
    import re

    try:
        result = subprocess.run(
            ["arm-none-eabi-readelf", "-s", source],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        try:
            result = subprocess.run(
                ["readelf", "-s", source],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            raise RuntimeError("readelf not found — install arm-none-eabi-readelf or binutils")

    if result.returncode != 0:
        raise RuntimeError(f"readelf failed: {result.stderr.strip()}")

    # Match FUNC symbols
    line_re = re.compile(
        r'^\s*\d+:\s+([0-9a-fA-F]+)\s+(\d+)\s+FUNC\s+\S+\s+\S+\s+\S+\s+(.+)$'
    )

    for line in result.stdout.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        sym_name = m.group(3).strip()
        if sym_name == name:
            return int(m.group(1), 16)

    return None


def search_functions(source: str, pattern: str, max_results: int = 20) -> list[dict]:
    """Search for function symbols matching a pattern.

    Returns list of dicts with keys: name, address, size
    """
    import subprocess
    import re

    try:
        result = subprocess.run(
            ["arm-none-eabi-readelf", "-s", source],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        result = subprocess.run(
            ["readelf", "-s", source],
            capture_output=True, text=True, timeout=10,
        )

    line_re = re.compile(
        r'^\s*\d+:\s+([0-9a-fA-F]+)\s+(\d+)\s+FUNC\s+\S+\s+\S+\s+\S+\s+(.+)$'
    )
    pat_re = re.compile(pattern, re.IGNORECASE)

    matches = []
    for line in result.stdout.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        sym_name = m.group(3).strip()
        if pat_re.search(sym_name):
            matches.append({
                "name": sym_name,
                "address": int(m.group(1), 16),
                "size": int(m.group(2)),
            })
            if len(matches) >= max_results:
                break

    return matches


# --- Core Register Access (via DCRSR/DCRDR, requires CPU halted) ---
DCRSR = 0xE000EDF4  # Debug Core Register Selector
DCRDR = 0xE000EDF8  # Debug Core Register Data

# Core register indices for DCRSR
CORE_REGS = {
    "r0": 0, "r1": 1, "r2": 2, "r3": 3,
    "r4": 4, "r5": 5, "r6": 6, "r7": 7,
    "r8": 8, "r9": 9, "r10": 10, "r11": 11,
    "r12": 12, "sp": 13, "lr": 14, "pc": 15,
    "xpsr": 16, "msp": 17, "psp": 18,
    "control": 20,
}


def read_core_register(bridge: MKLinkSerialBridge, reg: str | int) -> int:
    """Read a CPU core register (requires CPU halted).

    Args:
        bridge: Connected MKLink bridge
        reg: Register name (r0-r12, sp, lr, pc, xpsr, msp, psp, control) or index

    Returns:
        32-bit register value
    """
    if isinstance(reg, str):
        idx = CORE_REGS.get(reg.lower())
        if idx is None:
            raise ValueError(f"Unknown register '{reg}'. Valid: {', '.join(CORE_REGS.keys())}")
    else:
        idx = reg

    # Write register index to DCRSR with REGWnR=0 (read)
    _write_u32(bridge, DCRSR, idx & 0x1F)
    # Read result from DCRDR
    return _read_u32(bridge, DCRDR)


def read_all_core_registers(bridge: MKLinkSerialBridge) -> dict[str, int]:
    """Read all core registers. Returns dict of name→value."""
    regs = {}
    for name, idx in CORE_REGS.items():
        regs[name] = read_core_register(bridge, idx)
    return regs
