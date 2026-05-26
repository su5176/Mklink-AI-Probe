"""Memory-mapped register definitions used by MKLink debug commands."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegisterDef:
    name: str
    address: int
    width: int = 32
    description: str = ""


REGISTER_TABLE: dict[str, RegisterDef] = {
    "SCB.CPUID": RegisterDef("SCB.CPUID", 0xE000ED00, description="CPUID Base Register"),
    "SCB.ICSR": RegisterDef("SCB.ICSR", 0xE000ED04, description="Interrupt Control and State"),
    "SCB.VTOR": RegisterDef("SCB.VTOR", 0xE000ED08, description="Vector Table Offset"),
    "SCB.AIRCR": RegisterDef("SCB.AIRCR", 0xE000ED0C, description="Application Interrupt and Reset Control"),
    "SCB.SCR": RegisterDef("SCB.SCR", 0xE000ED10, description="System Control"),
    "SCB.CCR": RegisterDef("SCB.CCR", 0xE000ED14, description="Configuration and Control"),
    "SCB.SHCSR": RegisterDef("SCB.SHCSR", 0xE000ED24, description="System Handler Control and State"),
    "SCB.CFSR": RegisterDef("SCB.CFSR", 0xE000ED28, description="Configurable Fault Status"),
    "SCB.HFSR": RegisterDef("SCB.HFSR", 0xE000ED2C, description="HardFault Status"),
    "SCB.DFSR": RegisterDef("SCB.DFSR", 0xE000ED30, description="Debug Fault Status"),
    "SCB.MMFAR": RegisterDef("SCB.MMFAR", 0xE000ED34, description="MemManage Fault Address"),
    "SCB.BFAR": RegisterDef("SCB.BFAR", 0xE000ED38, description="BusFault Address"),
    "SCB.AFSR": RegisterDef("SCB.AFSR", 0xE000ED3C, description="Auxiliary Fault Status"),
    "SYST.CSR": RegisterDef("SYST.CSR", 0xE000E010, description="SysTick Control and Status"),
    "SYST.RVR": RegisterDef("SYST.RVR", 0xE000E014, description="SysTick Reload Value"),
    "SYST.CVR": RegisterDef("SYST.CVR", 0xE000E018, description="SysTick Current Value"),
    "DWT.CTRL": RegisterDef("DWT.CTRL", 0xE0001000, description="DWT Control"),
    "COREDEBUG.DHCSR": RegisterDef("COREDEBUG.DHCSR", 0xE000EDF0, description="Debug Halting Control and Status"),
    "COREDEBUG.DCRSR": RegisterDef("COREDEBUG.DCRSR", 0xE000EDF4, description="Debug Core Register Selector"),
    "COREDEBUG.DCRDR": RegisterDef("COREDEBUG.DCRDR", 0xE000EDF8, description="Debug Core Register Data"),
    "COREDEBUG.DEMCR": RegisterDef("COREDEBUG.DEMCR", 0xE000EDFC, description="Debug Exception and Monitor Control"),
}


def resolve_register(name_or_addr: str, width: int = 32) -> RegisterDef:
    """Resolve a symbolic register name or numeric address."""
    key = name_or_addr.strip().upper().replace("->", ".")
    if key in REGISTER_TABLE:
        return REGISTER_TABLE[key]
    try:
        address = int(name_or_addr, 0)
    except ValueError as exc:
        known = ", ".join(sorted(REGISTER_TABLE)[:8])
        raise KeyError(f"unknown register '{name_or_addr}'. Examples: {known}") from exc
    return RegisterDef(f"0x{address:08X}", address, width)


def iter_registers(prefix: str | None = None) -> list[RegisterDef]:
    regs = list(REGISTER_TABLE.values())
    if prefix:
        p = prefix.upper()
        regs = [r for r in regs if r.name.upper().startswith(p)]
    return sorted(regs, key=lambda r: r.name)
