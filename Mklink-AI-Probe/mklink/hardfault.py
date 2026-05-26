"""Cortex-M HardFault register and stack-frame decoding."""

from __future__ import annotations

import subprocess
import struct


FAULT_REGISTERS = ["SCB.CFSR", "SCB.HFSR", "SCB.MMFAR", "SCB.BFAR", "SCB.AFSR"]


CFSR_FLAGS = [
    (0, "IACCVIOL", "MemManage: instruction access violation"),
    (1, "DACCVIOL", "MemManage: data access violation"),
    (3, "MUNSTKERR", "MemManage: unstacking error"),
    (4, "MSTKERR", "MemManage: stacking error"),
    (5, "MLSPERR", "MemManage: lazy FP state preservation error"),
    (7, "MMARVALID", "MemManage fault address valid"),
    (8, "IBUSERR", "BusFault: instruction bus error"),
    (9, "PRECISERR", "BusFault: precise data bus error"),
    (10, "IMPRECISERR", "BusFault: imprecise data bus error"),
    (11, "UNSTKERR", "BusFault: unstacking error"),
    (12, "STKERR", "BusFault: stacking error"),
    (13, "LSPERR", "BusFault: lazy FP state preservation error"),
    (15, "BFARVALID", "BusFault address valid"),
    (16, "UNDEFINSTR", "UsageFault: undefined instruction"),
    (17, "INVSTATE", "UsageFault: invalid EPSR state"),
    (18, "INVPC", "UsageFault: invalid PC load"),
    (19, "NOCP", "UsageFault: no coprocessor"),
    (24, "UNALIGNED", "UsageFault: unaligned access"),
    (25, "DIVBYZERO", "UsageFault: divide by zero"),
]

HFSR_FLAGS = [
    (1, "VECTTBL", "Fault during vector table read"),
    (30, "FORCED", "Configurable fault escalated to HardFault"),
    (31, "DEBUGEVT", "Debug event"),
]


def decode_cfsr(value: int) -> list[str]:
    return [f"{name}: {desc}" for bit, name, desc in CFSR_FLAGS if value & (1 << bit)]


def decode_hfsr(value: int) -> list[str]:
    return [f"{name}: {desc}" for bit, name, desc in HFSR_FLAGS if value & (1 << bit)]


def parse_exception_stack_frame(data: bytes) -> dict[str, int]:
    if len(data) < 32:
        raise ValueError("exception stack frame requires at least 32 bytes")
    names = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"]
    values = struct.unpack("<8I", data[:32])
    return dict(zip(names, values))


def addr2line(source: str, *addresses: int) -> dict[int, str]:
    if not source or not addresses:
        return {}
    cmd = ["arm-none-eabi-addr2line", "-e", source, "-f", "-p"]
    cmd.extend(f"0x{a:08X}" for a in addresses)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return {addr: line for addr, line in zip(addresses, lines)}


def format_hardfault_report(
    fault_regs: dict[str, int],
    *,
    frame: dict[str, int] | None = None,
    locations: dict[int, str] | None = None,
) -> str:
    locations = locations or {}
    lines = ["========== Hard Fault Analysis ==========", "--- Fault Status ---"]
    cfsr = fault_regs.get("SCB.CFSR", 0)
    hfsr = fault_regs.get("SCB.HFSR", 0)
    lines.append(f"CFSR: 0x{cfsr:08X}")
    for item in decode_cfsr(cfsr) or ["no configurable fault bits set"]:
        lines.append(f"  - {item}")
    lines.append(f"HFSR: 0x{hfsr:08X}")
    for item in decode_hfsr(hfsr) or ["no hard fault status bits set"]:
        lines.append(f"  - {item}")
    for name in ("SCB.MMFAR", "SCB.BFAR", "SCB.AFSR"):
        if name in fault_regs:
            lines.append(f"{name}: 0x{fault_regs[name]:08X}")
    if frame:
        lines.extend(["", "--- Stack Frame ---"])
        for name in ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"]:
            value = frame[name]
            suffix = f"  {locations[value]}" if value in locations else ""
            lines.append(f"{name.upper():>4} = 0x{value:08X}{suffix}")
    return "\n".join(lines)
