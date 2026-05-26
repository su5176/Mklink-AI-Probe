"""Register profile loader, validator, and decoder for Modbus dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_EMBEDDED_DIR = Path(__file__).parent / "profiles"


def load_profile(path: str | None = None) -> dict:
    """Load a register profile JSON.

    Search order: explicit path > .mklink/modbus_profile.json > embedded default.
    """
    if path and path != "auto":
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # Try .mklink/modbus_profile.json
    mklink_cfg = Path(".mklink/modbus_profile.json")
    if mklink_cfg.is_file():
        with open(mklink_cfg, "r", encoding="utf-8") as f:
            return json.load(f)

    # Fall back to embedded diesel_heater.json
    embedded = _EMBEDDED_DIR / "diesel_heater.json"
    if embedded.is_file():
        with open(embedded, "r", encoding="utf-8") as f:
            return json.load(f)

    raise FileNotFoundError(
        "No Modbus profile found. Provide --profile or create .mklink/modbus_profile.json"
    )


def build_addr_index(profile: dict) -> dict[int, dict]:
    """Build a {addr: register_def} lookup from profile groups."""
    index: dict[int, dict] = {}
    for group in profile.get("groups", []):
        for reg in group.get("registers", []):
            index[reg["addr"]] = reg
    return index


def get_writable_addrs(profile: dict) -> set[int]:
    """Return set of addresses that are allowed to be written."""
    addrs: set[int] = set()
    for group in profile.get("groups", []):
        if group.get("writable"):
            for reg in group.get("registers", []):
                addrs.add(reg["addr"])
    for cmd in profile.get("commands", []):
        if "write_addr" in cmd:
            addrs.add(cmd["write_addr"])
        if "addr" in cmd:
            addrs.add(cmd["addr"])
    return addrs


def validate_param(profile: dict, addr: int, value: int) -> tuple[bool, str]:
    """Validate a parameter write against profile ranges.

    Returns (valid, error_message). error_message is empty on success.
    """
    index = build_addr_index(profile)

    # Check addr is writable
    writable = get_writable_addrs(profile)
    if addr not in writable:
        if addr in index:
            return False, f"Register {addr} ({index[addr].get('name', '?')}) is read-only"
        return False, f"Register {addr} not found in profile"

    reg = index[addr]

    # Check value range
    if "min" in reg and "max" in reg:
        min_v = reg["min"]
        max_v = reg["max"]
        if value < min_v or value > max_v:
            return False, f"Value {value} out of range [{min_v}, {max_v}] for {reg.get('label', reg.get('name', addr))}"

    return True, ""


# ---------------------------------------------------------------------------
# Generic discovery — auto-find registers by kind
# ---------------------------------------------------------------------------


def discover_alarm_addrs(profile: dict) -> list[int]:
    """Find all register addresses with kind='alarm'."""
    addrs: list[int] = []
    for group in profile.get("groups", []):
        for reg in group.get("registers", []):
            if reg.get("kind") == "alarm":
                addrs.append(reg["addr"])
    return addrs


def discover_control_addr(profile: dict) -> int | None:
    """Find the first register address with kind='bitfield'."""
    for group in profile.get("groups", []):
        for reg in group.get("registers", []):
            if reg.get("kind") == "bitfield":
                return reg["addr"]
    return None


def get_enum_name(profile: dict, addr: int, value: int) -> str:
    """Look up enum display name from profile register definition."""
    index = build_addr_index(profile)
    reg = index.get(addr)
    if not reg or reg.get("kind") != "enum":
        return f"Unknown({value})"
    values = reg.get("values", {})
    return values.get(str(value), f"Unknown({value})")


# ---------------------------------------------------------------------------
# Decoding functions — now generic (driven by profile, no hardcoded addrs)
# ---------------------------------------------------------------------------


def decode_alarm_bits(profile: dict, alarm1_val: int, alarm2_val: int) -> list[dict]:
    """Decode alarm register bitfields into human-readable alarm list.

    Backward-compatible signature: accepts two positional values for the
    first two alarm registers found in the profile.
    """
    alarm_addrs = discover_alarm_addrs(profile)
    index = build_addr_index(profile)
    vals = [alarm1_val, alarm2_val]

    alarms: list[dict] = []
    for i, addr in enumerate(alarm_addrs[:2]):
        val = vals[i] if i < len(vals) else 0
        reg = index.get(addr)
        if not reg:
            continue
        for bit_def in reg.get("bits", []):
            bit_num = bit_def["bit"]
            active = bool(val & (1 << bit_num))
            alarms.append({
                "bit": bit_num,
                "name": bit_def.get("name", f"bit{bit_num}"),
                "label": bit_def.get("label", f"Alarm bit {bit_num}"),
                "active": active,
                "severity": bit_def.get("severity", "stop"),
                "register": addr,
            })

    return alarms


def decode_control_bits(profile: dict, value: int) -> list[dict]:
    """Decode control register bitfield into command state list."""
    result: list[dict] = []
    index = build_addr_index(profile)

    ctrl_addr = discover_control_addr(profile)
    if ctrl_addr is None:
        return result

    reg = index.get(ctrl_addr)
    if not reg:
        return result

    for bit_def in reg.get("bits", []):
        bit_num = bit_def["bit"]
        result.append({
            "bit": bit_num,
            "name": bit_def.get("name", f"bit{bit_num}"),
            "label": bit_def.get("label", f"Bit {bit_num}"),
            "active": bool(value & (1 << bit_num)),
        })

    return result


def get_state_name(value: int) -> str:
    """Map system status enum value to display string.

    DEPRECATED: use get_enum_name(profile, addr, value) for new code.
    Kept for backward compatibility.
    """
    states = {
        0: "Idle",
        1: "Preheating",
        2: "Ignition",
        3: "Running",
        4: "Shutdown",
        5: "Fault",
    }
    return states.get(value, f"Unknown({value})")


def get_mode_name(value: int) -> str:
    """Map mode enum value to display string.

    DEPRECATED: use get_enum_name(profile, addr, value) for new code.
    Kept for backward compatibility.
    """
    modes = {0: "Manual", 1: "Auto"}
    return modes.get(value, f"Unknown({value})")


def resolve_command(profile: dict, action: str) -> tuple[bool, int, int, str]:
    """Resolve a command action to (ok, write_addr, write_value, error_msg).

    For bitfield commands: writes exact bit mask to the control register.
    For direct commands: writes value to specified register.
    """
    ctrl_addr = discover_control_addr(profile)

    for cmd in profile.get("commands", []):
        if cmd.get("action") != action:
            continue

        if "bit" in cmd:
            bit = cmd["bit"]
            write_value = 1 << bit
            write_addr = ctrl_addr if ctrl_addr is not None else 100
            return True, write_addr, write_value, ""

        if "write_addr" in cmd:
            write_value = cmd.get("write_value", 0)
            return True, cmd["write_addr"], write_value, ""

        if "addr" in cmd:
            method = cmd.get("method", "pulse")
            if method == "write_0":
                return True, cmd["addr"], 0, ""
            return True, cmd["addr"], cmd.get("value", 0), ""

    return False, 0, 0, f"Unknown command: {action}"


def find_command(profile: dict, action: str) -> dict | None:
    """Find a command definition by action name."""
    for cmd in profile.get("commands", []):
        if cmd.get("action") == action:
            return cmd
    return None
