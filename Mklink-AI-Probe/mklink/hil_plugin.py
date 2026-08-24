"""HIL-Core v0.2 one-shot JSON adapter for Mklink-AI-Probe.

The unattended surface intentionally exposes lifecycle methods and read-only
debug operations. Existing interactive MCP and CLI workflows are unchanged.
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import redirect_stdout
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from serial.tools import list_ports

from mklink.hil_lock import HilFileLock, HilLockHeld, transport_lock_name


PROTOCOL = "hil-plugin-json-v1"
PLUGIN = "Mklink-AI-Probe"
CAPABILITIES = ["program", "debug", "console.rtt", "console.uart", "bus.modbus"]
READ_TARGETS = {"plugin-version", "probe-status", "memory", "register", "variable"}
MAX_READ_BYTES = 4096


def _success(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def _failure(error_type: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": {"type": error_type, "message": message},
    }


def _package_version() -> str:
    try:
        return version("mklink")
    except PackageNotFoundError:
        return "source-tree"


def _parse_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value, 0)
    raise ValueError(f"{field} must be an integer")


def _transport(request: dict[str, Any]) -> dict[str, Any]:
    device = request.get("device")
    if not isinstance(device, dict) or device.get("plugin") != PLUGIN:
        raise ValueError(f"device.plugin must be {PLUGIN}")
    transport = device.get("transport")
    if not isinstance(transport, dict) or transport.get("kind") != "usb-serial":
        raise ValueError("device.transport.kind must be usb-serial")
    port = transport.get("port")
    if not isinstance(port, str) or not port.strip():
        raise ValueError("device.transport.port is required")
    return transport


def _port_info(port: str):
    return next(
        (
            item
            for item in list_ports.comports()
            if str(item.device).casefold() == port.casefold()
        ),
        None,
    )


def _interface_number(info: Any) -> str | None:
    text = " ".join(
        str(getattr(info, key, "") or "")
        for key in ("hwid", "location", "interface")
    ).strip()
    match = re.search(r"(?i)MI[_-]?(\d+)", text)
    if match is None:
        match = re.search(r"(?i)(?:x\.|\.)(\d+)$", text)
    return f"{int(match.group(1)):02d}" if match else None


def _actual_identity(transport: dict[str, Any]) -> dict[str, str]:
    port = str(transport["port"])
    info = _port_info(port)
    if info is None:
        raise FileNotFoundError(f"configured probe port is not present: {port}")
    actual_vid = getattr(info, "vid", None)
    actual_pid = getattr(info, "pid", None)
    actual_serial = str(getattr(info, "serial_number", "") or "").strip()
    expected_vid = (
        _parse_int(transport["vid"], "transport.vid")
        if transport.get("vid") is not None
        else None
    )
    expected_pid = (
        _parse_int(transport["pid"], "transport.pid")
        if transport.get("pid") is not None
        else None
    )
    expected_serial = str(transport.get("serial") or "").strip()
    if expected_vid is not None and actual_vid != expected_vid:
        raise ValueError("probe VID does not match bench identity")
    if expected_pid is not None and actual_pid != expected_pid:
        raise ValueError("probe PID does not match bench identity")
    if expected_serial and actual_serial.casefold() != expected_serial.casefold():
        raise ValueError("probe serial does not match bench identity")

    parts = []
    if actual_vid is not None and actual_pid is not None:
        parts.append(f"vid_{actual_vid:04x}_pid_{actual_pid:04x}")
    if actual_serial:
        parts.append(f"sn_{actual_serial.casefold()}")
    interface = _interface_number(info)
    expected_interface = str(transport.get("interface") or "").strip()
    if expected_interface:
        expected_number = expected_interface.rsplit("_", 1)[-1].zfill(2)
        if interface is None or interface != expected_number:
            raise ValueError("probe interface does not match bench identity")
    if interface:
        parts.append(f"mi_{interface}")
    locator = "_".join(parts)
    expected_locator = str(transport.get("locator") or "").strip()
    if expected_locator and locator.casefold() != expected_locator.casefold():
        raise ValueError("probe locator does not match bench identity")
    return {
        "port": port,
        "serial": actual_serial,
        "locator": locator or expected_locator,
    }


def _lock(transport: dict[str, Any], purpose: str) -> HilFileLock:
    root = os.environ.get("HIL_CORE_LOCK_ROOT") or None
    return HilFileLock(
        transport_lock_name("usb-serial", str(transport["port"])),
        root=Path(root) if root else None,
        lease_s=30,
        owner_id=f"mklink-hil-{purpose}-{os.getpid()}",
    )


def _health(transport: dict[str, Any]) -> dict[str, Any]:
    identity = _actual_identity(transport)
    from mklink.discovery import _probe_port

    with _lock(transport, "health"):
        verified = _probe_port(str(transport["port"]))
    if not verified:
        raise RuntimeError("probe identity handshake failed")
    return {"healthy": True, "verified": True, "identity": identity}


def _safe_state(transport: dict[str, Any]) -> dict[str, Any]:
    _actual_identity(transport)
    lock = _lock(transport, "safe-state")
    lock.acquire()
    lock.release()
    return {
        "verified": not lock.path.exists(),
        "state": "process-local probe session closed; no sustained output",
    }


def _read(request: dict[str, Any], transport: dict[str, Any]) -> dict[str, Any]:
    params = request.get("params")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    target = str(params.get("target") or "plugin-version")
    if target not in READ_TARGETS:
        raise ValueError(
            "params.target must be plugin-version, probe-status, memory, "
            "register, or variable"
        )
    if target == "plugin-version":
        with _lock(transport, "version"):
            return {"target": target, "value": _package_version()}
    if target == "probe-status":
        return {"target": target, "value": _health(transport)}

    address = None
    size = None
    name = None
    if target == "memory":
        address = _parse_int(params.get("address"), "params.address")
        size = _parse_int(params.get("size"), "params.size")
        if not 1 <= size <= MAX_READ_BYTES:
            raise ValueError(f"params.size must be 1..{MAX_READ_BYTES}")
    else:
        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError(f"params.name is required for {target} read")

    _actual_identity(transport)
    from mklink import connect

    port = str(transport["port"])
    dut = request.get("dut") if isinstance(request.get("dut"), dict) else {}
    project_root = str(dut.get("firmware_project") or ".")
    kwargs = {"port": port, "project_root": project_root}
    if dut.get("mcu"):
        kwargs["mcu"] = str(dut["mcu"])
    if params.get("axf"):
        kwargs["axf"] = str(params["axf"])
    with connect(**kwargs) as device:
        if target == "memory":
            data = device.read_memory(address, size)
            return {
                "target": target,
                "address": address,
                "size": len(data),
                "encoding": "hex",
                "value": data.hex(),
            }
        if target == "register":
            return {
                "target": target,
                "name": name,
                "value": device.read_register(name),
            }
        return {
            "target": target,
            "name": name,
            "value": device.read_variable(name),
        }


def dispatch(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
        return _failure("plugin-protocol", f"protocol must be {PROTOCOL}")
    method = request.get("method")
    if method not in {"identify", "capabilities", "health", "invoke", "safe_state"}:
        return _failure("plugin-protocol", "unsupported method")
    if method == "invoke" and (
        request.get("verb") != "debug.read"
        or request.get("action_class") != "observe"
    ):
        return _failure(
            "action-refused",
            "unattended Mklink v0.2 surface only permits observe/debug.read; "
            "flash, reset, writes and control remain interactive-only",
        )
    try:
        transport = _transport(request)
        if method == "identify":
            return _success(
                {
                    "plugin": PLUGIN,
                    "version": _package_version(),
                    "identity": _actual_identity(transport),
                }
            )
        if method == "capabilities":
            return _success(
                {
                    "capabilities": list(CAPABILITIES),
                    "automation_verbs": ["debug.read"],
                }
            )
        if method == "health":
            return _success(_health(transport))
        if method == "safe_state":
            return _success(_safe_state(transport))
        return _success(_read(request, transport))
    except HilLockHeld as exc:
        return _failure("lock-held", str(exc))
    except (FileNotFoundError, ValueError) as exc:
        return _failure("invalid-device", str(exc))
    except Exception as exc:  # noqa: BLE001 - normalize the protocol boundary
        return _failure(type(exc).__name__, str(exc) or type(exc).__name__)


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
    except (UnicodeError, json.JSONDecodeError) as exc:
        result = _failure("plugin-protocol", f"invalid JSON request: {exc}")
    else:
        with redirect_stdout(sys.stderr):
            result = dispatch(request)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
