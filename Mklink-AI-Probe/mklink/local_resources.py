"""Local resource operations for CLI/skill use without FastAPI.

This module deals with process/file locks used by the local mklink tools.  It
does not require a running REST server; FastAPI can call into this layer when it
needs the same cleanup behavior for dashboard flows.
"""

from __future__ import annotations

import glob
import os
import re
import signal
import subprocess
import time
from typing import Any


def _temp_dir() -> str:
    return os.environ.get("TEMP", "/tmp")


def _safe_port_name(port: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", port.upper())


def serial_lock_path(port: str) -> str:
    lock_dir = os.path.join(_temp_dir(), "mklink_serial_locks")
    return os.path.join(lock_dir, f"serial_{_safe_port_name(port)}.lock")


def serial_lock_paths(port: str | None = None) -> list[str]:
    if port:
        return [serial_lock_path(port)]
    lock_dir = os.path.join(_temp_dir(), "mklink_serial_locks")
    return sorted(glob.glob(os.path.join(lock_dir, "*.lock")))


def mklink_bridge_lock_path() -> str:
    return os.path.join(_temp_dir(), "mklink_serial_lock")


def _read_owner_pid(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read().strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_exists(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            # OpenProcess alone is not proof that a process is still running:
            # Windows can keep a terminated process object alive while another
            # handle references it.  Query the exit code so stale serial-lock
            # owners are not reported as live indefinitely.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            get_exit_code.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL

            process_query_limited_information = 0x1000
            error_invalid_parameter = 87
            still_active = 259
            handle = open_process(process_query_limited_information, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER is the documented result for a PID
                # that no longer identifies a process.  Access-denied and
                # unknown failures stay conservative so a live owner's lock is
                # never removed merely because it cannot be inspected.
                return ctypes.get_last_error() != error_invalid_parameter
            try:
                exit_code = wintypes.DWORD()
                if not get_exit_code(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                close_handle(handle)
        except Exception:
            # Failure to inspect is not proof of death.  Keep the lock rather
            # than risking concurrent access to a live serial owner.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_pid(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        return False

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.05)
    return not _pid_exists(pid)


def _cleanup_lock_file(path: str, *, resource: str, force: bool = False) -> dict[str, Any]:
    info: dict[str, Any] = {
        "resource": resource,
        "path": path,
        "exists": os.path.exists(path),
        "owner_pid": None,
        "owner_alive": False,
        "action": "missing",
    }
    if not info["exists"]:
        return info

    owner_pid = _read_owner_pid(path)
    owner_alive = _pid_exists(owner_pid)
    info["owner_pid"] = owner_pid
    info["owner_alive"] = owner_alive

    if owner_alive:
        if force and owner_pid is not None and owner_pid != os.getpid():
            if _terminate_pid(owner_pid):
                info["owner_alive"] = False
                try:
                    os.remove(path)
                    info["action"] = "terminated_owner"
                except OSError:
                    info["action"] = "owner_terminated_lock_left"
                return info
            info["action"] = "terminate_failed"
            return info
        info["action"] = "live_owner"
        return info

    try:
        os.remove(path)
        info["action"] = "removed_stale_lock"
    except OSError as exc:
        info["action"] = "remove_failed"
        info["error"] = str(exc)
    return info


def _inspect_lock_file(path: str, *, resource: str) -> dict[str, Any]:
    owner_pid = _read_owner_pid(path) if os.path.exists(path) else None
    owner_alive = _pid_exists(owner_pid)
    return {
        "resource": resource,
        "path": path,
        "exists": os.path.exists(path),
        "owner_pid": owner_pid,
        "owner_alive": owner_alive,
    }


def stop_inprocess_serial_dashboard() -> list[str]:
    """Stop an in-process serial SSE dashboard manager if one exists."""
    try:
        import mklink.remote.dashboards as dashboards

        manager = getattr(dashboards, "_managers", {}).get("serial")
        if manager:
            manager.stop()
            return ["serial"]
    except Exception:
        return []
    return []


def local_resource_status(port: str | None = None) -> dict[str, Any]:
    """Inspect local lock files without requiring FastAPI."""
    return {
        "port": port,
        "mklink_bridge": _inspect_lock_file(
            mklink_bridge_lock_path(), resource="mklink_bridge",
        ),
        "serial_locks": [
            _inspect_lock_file(path, resource="serial_port")
            for path in serial_lock_paths(port)
        ],
    }


def release_serial_resources(
    *,
    port: str | None = None,
    force: bool = False,
    include_mklink_bridge: bool = True,
) -> dict[str, Any]:
    """Release local serial resources without starting FastAPI.

    Default behavior is conservative: stale lock files are removed, but live
    owner processes are reported rather than killed.  Use ``force=True`` only
    when the caller explicitly wants to terminate the owner process.
    """
    result: dict[str, Any] = {
        "port": port,
        "force": force,
        "stopped": stop_inprocess_serial_dashboard(),
        "mklink_bridge": None,
        "serial_locks": [],
    }

    if include_mklink_bridge:
        result["mklink_bridge"] = _cleanup_lock_file(
            mklink_bridge_lock_path(),
            resource="mklink_bridge",
            force=force,
        )

    result["serial_locks"] = [
        _cleanup_lock_file(path, resource="serial_port", force=force)
        for path in serial_lock_paths(port)
    ]
    return result
