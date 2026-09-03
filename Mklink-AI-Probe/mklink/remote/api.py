"""MKLink FastAPI Server — REST + WebSocket API for GUI and remote debugging.

Extends the existing DeviceDispatcher with a proper REST API for configuration,
device discovery, and lifecycle management. Keeps WebSocket JSON-RPC for
low-level device operations.

Usage (CLI)::

    mklink serve --port 8765 --token my-secret --backend fastapi

Usage (Python)::

    from mklink.remote.api import create_app, run_server
    app = create_app()
    run_server(app, port=8765)
"""

from __future__ import annotations
# WARNING: Because of `from __future__ import annotations`, FastAPI cannot resolve
# type hints in closure functions via typing.get_type_hints().  Any new closure route
# (e.g. inside create_app()) MUST explicitly import and annotate its parameter types.
# See the eager-import block below for the required FastAPI/Pydantic types.

import asyncio
from contextlib import asynccontextmanager, contextmanager
import contextvars
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from typing import Annotated, Any
import weakref

from mklink.symbol_catalog import SymbolCatalogError

logger = logging.getLogger(__name__)

_FILE_SOURCE_UPLOAD_LIMIT = 256 * 1024 * 1024
_FILE_SOURCE_UPLOAD_CHUNK = 1024 * 1024
_YMODEM_UPLOAD_LIMIT = 32 * 1024 * 1024
_YMODEM_FILENAME_LIMIT = 31


class BrowserSessionLease:
    """Track browser tabs and request shutdown after the last tab disappears."""

    def __init__(
        self,
        timeout: float,
        *,
        close_grace: float = 2.0,
        startup_grace: float = 60.0,
        clock=time.monotonic,
    ) -> None:
        if timeout <= 0:
            raise ValueError("browser session timeout must be positive")
        self.timeout = float(timeout)
        self.close_grace = max(0.0, float(close_grace))
        self.startup_grace = max(0.0, float(startup_grace))
        self._clock = clock
        self._started_at = clock()
        self._clients: dict[str, float] = {}
        self._registered = False
        self._empty_since: float | None = None
        self._lock = threading.Lock()

    def renew(self, client_id: str) -> int:
        now = self._clock()
        with self._lock:
            self._clients[client_id] = now + self.timeout
            self._registered = True
            self._empty_since = None
            return len(self._clients)

    def release(self, client_id: str) -> int:
        now = self._clock()
        with self._lock:
            self._clients.pop(client_id, None)
            self._discard_expired(now)
            if self._registered and not self._clients and self._empty_since is None:
                self._empty_since = now
            return len(self._clients)

    def should_exit(self) -> bool:
        now = self._clock()
        with self._lock:
            self._discard_expired(now)
            if not self._registered:
                return now - self._started_at >= self.startup_grace
            if self._clients:
                return False
            if self._empty_since is None:
                self._empty_since = now
                return False
            return now - self._empty_since >= self.close_grace

    def _discard_expired(self, now: float) -> None:
        expired = [
            client_id
            for client_id, deadline in self._clients.items()
            if deadline <= now
        ]
        for client_id in expired:
            self._clients.pop(client_id, None)


def _bind_desktop_server_socket(host: str, port: int, port_end: int):
    """Bind one loopback port and keep it reserved for Uvicorn."""
    import socket

    if host != "127.0.0.1":
        raise ValueError("desktop automatic ports require host 127.0.0.1")
    if not (1 <= port <= port_end <= 65535):
        raise ValueError("invalid desktop port range")

    last_error: OSError | None = None
    for candidate in range(port, port_end + 1):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((host, candidate))
            listener.listen(2048)
            return listener, candidate
        except OSError as exc:
            last_error = exc
            listener.close()
    raise OSError(
        f"no available desktop backend port in {port}..{port_end}"
    ) from last_error


def _write_desktop_runtime_info(
    path: str,
    *,
    port: int,
    instance_id: str,
) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps({"port": port, "instanceId": instance_id}),
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _project_root_drives(
    *, system: str | None = None, exists: Any = os.path.exists,
) -> list[str]:
    import platform
    import string

    system = system or platform.system()
    if system != "Windows":
        return ["/"]
    drives = []
    for letter in string.ascii_uppercase:
        if exists(f"{letter}:\\"):
            drives.append(f"{letter}:")
    return drives


def _same_file_source_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )


def _store_uploaded_file_source(
    upload: Any,
    project_root: Path | str,
    allowed_suffixes: tuple[str, ...],
) -> dict[str, object]:
    original_name = str(upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(original_name).suffix.casefold()
    if suffix not in allowed_suffixes:
        raise ValueError("file must use one of: {}".format(", ".join(allowed_suffixes)))

    uploads = (
        Path(project_root).resolve() / ".mklink" / "uploads" / "file-sources"
    ).resolve()
    uploads.mkdir(parents=True, exist_ok=True)
    temporary = (uploads / (secrets.token_hex(24) + ".tmp")).resolve()
    if temporary.parent != uploads:
        raise ValueError("invalid upload path")

    digest = hashlib.sha256()
    total = 0
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = upload.file.read(_FILE_SOURCE_UPLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > _FILE_SOURCE_UPLOAD_LIMIT:
                    raise ValueError("file exceeds 256 MiB upload limit")
                digest.update(chunk)
                output.write(chunk)
        if total == 0:
            raise ValueError("file is empty")
        checksum = digest.hexdigest()
        destination = (uploads / (checksum + suffix)).resolve()
        if destination.parent != uploads:
            raise ValueError("invalid upload path")
        if destination.exists():
            temporary.unlink()
        else:
            os.replace(temporary, destination)
        return {
            "path": str(destination),
            "name": original_name,
            "size": total,
            "sha256": checksum,
        }
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

_NATIVE_TARGET_COORDINATOR = threading.RLock()
_NATIVE_TARGET_CONTEXT = threading.local()
_ASYNC_TARGET_CONTEXT = contextvars.ContextVar("mklink_async_target_context", default=())
_ASYNC_TARGET_LOCKS = weakref.WeakKeyDictionary()
_ASYNC_TARGET_LOCKS_GUARD = threading.Lock()

_DASHBOARD_OWNER_TO_MANAGER = {
    "user:dashboard:rtt": "rtt",
    "user:dashboard:superwatch": "superwatch",
    "user:dashboard:serial": "serial",
    "user:dashboard:modbus": "modbus",
    "user:dashboard:vofa": "vofa",
    "user:dashboard:systemview": "systemview",
}

_RESOURCE_TO_FALLBACK_OWNER = {
    "serial_port": "user:dashboard:serial",
    "modbus_port": "user:dashboard:modbus",
}

_OWNER_REQUIRES_STOP_EVEN_IF_NOT_RUNNING = {
    "user:dashboard:serial",
    "user:dashboard:modbus",
}

_TARGET_DEBUG_RPC_METHODS = {
    "flash", "erase_chip", "reset", "rtt_start", "rtt_read", "rtt_write",
    "rtt_stop", "read_memory", "write_memory", "read_variable",
    "write_variable", "read_register", "halt", "resume", "step",
    "set_breakpoint", "clear_breakpoint", "read_core_registers",
    "check_hardfault", "decode_hardfault",
}


class DashboardStopPending(Exception):
    def __init__(self, dashboard: str):
        self.dashboard = dashboard
        self.detail = {"code": "stop_pending", "dashboard": dashboard}
        super().__init__(f"Dashboard {dashboard} worker is still active")


def _dashboard_worker_alive(manager) -> bool:
    thread = getattr(manager, "_thread", None)
    if thread is not None:
        return bool(thread.is_alive())
    return bool(getattr(manager, "running", False))


def _resource_group_from_name(resource: str):
    from mklink.remote.resource_manager import ResourceGroup

    for group in ResourceGroup:
        if resource == group.value:
            return group
    raise ValueError(f"Unknown resource: {resource}")


def _stop_dashboard_for_owner(owner: str) -> list[str]:
    manager_name = _DASHBOARD_OWNER_TO_MANAGER.get(owner)
    if not manager_name:
        return []

    from mklink.remote.dashboards import get_managers

    managers = get_managers()
    manager = managers.get(manager_name)
    should_stop = (
        (manager is not None and _dashboard_worker_alive(manager))
        or owner in _OWNER_REQUIRES_STOP_EVEN_IF_NOT_RUNNING
    )
    if manager and should_stop:
        manager.stop()
        return [manager_name]
    return []


def release_resource_owner(
    state: dict[str, Any],
    owner: str,
    *,
    stop_active: bool = True,
) -> dict:
    """Stop dashboard activity for an owner and release its resource leases."""
    stopped = _stop_dashboard_for_owner(owner) if stop_active else []
    released = state["resource_manager"].release(owner)
    return {
        "owner": owner,
        "resources": [resource.value for resource in released],
        "stopped": stopped,
    }


def release_resource_by_name(
    state: dict[str, Any],
    resource: str,
    *,
    stop_active: bool = True,
) -> dict:
    """Release the current owner of a named resource, if any."""
    group = _resource_group_from_name(resource)
    lease = state["resource_manager"].get_active_lease(group)
    if lease is None:
        fallback_owner = _RESOURCE_TO_FALLBACK_OWNER.get(resource)
        if fallback_owner:
            return release_resource_owner(
                state, fallback_owner, stop_active=stop_active,
            )
        return {"owner": None, "resources": [], "stopped": []}
    return release_resource_owner(state, lease.owner, stop_active=stop_active)


def remember_device_connection(
    state: dict[str, Any],
    device,
    *,
    mcu: str | None = None,
) -> dict[str, Any] | None:
    """Remember the successful inputs used by an explicit quick reconnect."""
    if device is None or not getattr(device, "connected", False):
        return None

    try:
        axf_status = dict(getattr(device, "axf_status", {}) or {})
    except Exception:
        axf_status = {}
    device_state = getattr(device, "__dict__", {})
    details = {
        "port": getattr(device, "port", None),
        "axf": axf_status.get("axf_path") or device_state.get("_axf"),
        "mcu": mcu if mcu is not None else device_state.get("_mcu_hint"),
        "elf_backend": (
            axf_status.get("elf_backend")
            or device_state.get("_elf_backend")
            or device_state.get("_elf_backend_requested")
        ),
    }
    state["last_device_connection"] = details
    return details


def prepare_online_flash_connect(state: dict[str, Any], request) -> None:
    """Release the shared CDC Device before an HPM online-flash connection."""
    from mklink.hpm_config import is_hpm_target

    if not is_hpm_target(request.target_part):
        return

    from mklink.remote.dashboards import stop_bridge_dashboards

    stop_bridge_dashboards(resource_manager=state["resource_manager"])
    device = state.get("device")
    if device is None:
        return
    device.close()
    if state.get("device") is device:
        state["device"] = None
        state["dispatcher"] = None


def _resource_error_detail(error) -> dict[str, str]:
    return {
        "code": "PROBE_BUSY",
        "resource": error.resource.value,
        "conflict_owner": error.conflict_owner,
    }


@contextmanager
def target_debug_lease(state: dict[str, Any], operation: str):
    """Lease native target access for one API operation."""
    from mklink.remote.resource_manager import ResourceGroup

    owner = f"user:api:{operation}"
    manager = state["resource_manager"]
    with _NATIVE_TARGET_COORDINATOR:
        stack = getattr(_NATIVE_TARGET_CONTEXT, "stack", None)
        if stack is None:
            stack = []
            _NATIVE_TARGET_CONTEXT.stack = stack
        nested = bool(stack and stack[-1][0] is manager)
        lease_owner = stack[-1][1] if nested else owner
        if not nested:
            manager.acquire(
                ResourceGroup.TARGET_DEBUG,
                owner,
                preempt=True,
                preempt_user_dashboard=True,
            )
        stack.append((manager, lease_owner, not nested))
        try:
            yield lease_owner
        finally:
            _manager, active_owner, acquired = stack.pop()
            if acquired:
                manager.release(active_owner)
            if not stack:
                del _NATIVE_TARGET_CONTEXT.stack


def _async_target_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _ASYNC_TARGET_LOCKS_GUARD:
        lock = _ASYNC_TARGET_LOCKS.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _ASYNC_TARGET_LOCKS[loop] = lock
        return lock


@asynccontextmanager
async def async_target_debug_lease(state: dict[str, Any], operation: str):
    """Task-aware target lease for async handlers spanning awaits."""
    from mklink.remote.resource_manager import ResourceGroup

    owner = f"user:api:{operation}"
    manager = state["resource_manager"]
    task = asyncio.current_task()
    stack = _ASYNC_TARGET_CONTEXT.get()
    same_task = bool(stack and stack[-1][0] is task)
    nested = bool(same_task and stack[-1][1] is manager)

    if nested:
        lease_owner = stack[-1][2]
        token = _ASYNC_TARGET_CONTEXT.set(
            stack + ((task, manager, lease_owner, False),)
        )
        try:
            yield lease_owner
        finally:
            _ASYNC_TARGET_CONTEXT.reset(token)
        return

    async_lock = _async_target_lock()
    acquired_async_lock = False
    if not same_task:
        await async_lock.acquire()
        acquired_async_lock = True
    _NATIVE_TARGET_COORDINATOR.acquire()
    try:
        manager.acquire(
            ResourceGroup.TARGET_DEBUG,
            owner,
            preempt=True,
            preempt_user_dashboard=True,
        )
    except Exception:
        _NATIVE_TARGET_COORDINATOR.release()
        if acquired_async_lock:
            async_lock.release()
        raise

    token = _ASYNC_TARGET_CONTEXT.set(stack + ((task, manager, owner, True),))
    try:
        yield owner
    finally:
        _ASYNC_TARGET_CONTEXT.reset(token)
        manager.release(owner)
        _NATIVE_TARGET_COORDINATOR.release()
        if acquired_async_lock:
            async_lock.release()


def acquire_dashboard_resources(state: dict[str, Any], dashboard: str) -> list[str]:
    """Stop bridge peers, then atomically lease resources for a dashboard."""
    from mklink.remote.dashboards import stop_bridge_dashboards
    from mklink.remote.resource_manager import ResourceGroup

    owner = f"user:dashboard:{dashboard}"
    manager = state["resource_manager"]
    stopped = stop_bridge_dashboards(
        exclude=dashboard,
        resource_manager=manager,
    )
    manager.acquire_many(
        [ResourceGroup.MKLINK_BRIDGE, ResourceGroup.TARGET_DEBUG],
        owner,
        preempt=True,
    )
    return stopped


def _dashboard_start_lock(state: dict[str, Any]) -> asyncio.Lock:
    lock = state.get("_dashboard_start_lock")
    if lock is None:
        lock = asyncio.Lock()
        state["_dashboard_start_lock"] = lock
    return lock


async def start_dashboard_manager(
    state: dict[str, Any], dashboard: str, manager, start_call
) -> tuple[str, list[str]]:
    async with _dashboard_start_lock(state):
        return await _start_dashboard_manager_transaction(
            state, dashboard, manager, start_call,
        )


async def _start_dashboard_manager_transaction(
    state: dict[str, Any], dashboard: str, manager, start_call
) -> tuple[str, list[str]]:
    """Start a dashboard as a cancellation-safe acquire/start transaction.

    Cancellation is delivered only after the in-flight executor phase settles
    and its lease/manager effects have been rolled back.  Waiting remains
    asynchronous; the event loop never joins a hardware worker directly.
    """
    if _dashboard_worker_alive(manager):
        if manager.running:
            return "already_running", []
        raise DashboardStopPending(dashboard)
    loop = asyncio.get_running_loop()
    owner = f"user:dashboard:{dashboard}"
    release_lock = threading.Lock()
    owner_released = False

    def release_owner_once():
        nonlocal owner_released
        with release_lock:
            if owner_released:
                return []
            owner_released = True
            return state["resource_manager"].release(owner)

    def rollback_started_manager():
        if not _dashboard_worker_alive(manager):
            release_owner_once()
            return
        error = None
        try:
            manager.stop()
        except Exception as exc:
            error = exc
        if _dashboard_worker_alive(manager):
            raise DashboardStopPending(dashboard) from error
        release_owner_once()
        if error is not None:
            raise error

    async def rollback_start_effects():
        if _dashboard_worker_alive(manager):
            cleanup_future = loop.run_in_executor(
                None, rollback_started_manager,
            )
            cleanup_phase = asyncio.create_task(
                _capture_executor_outcome(cleanup_future)
            )
            await _wait_executor_completion(cleanup_phase)
        else:
            release_owner_once()

    acquire_future = loop.run_in_executor(
        None, acquire_dashboard_resources, state, dashboard,
    )
    acquire_phase = asyncio.create_task(_capture_executor_outcome(acquire_future))
    try:
        acquire_ok, acquire_result = await asyncio.shield(acquire_phase)
    except asyncio.CancelledError:
        await _wait_executor_completion(acquire_phase)
        release_owner_once()
        raise
    if not acquire_ok:
        release_owner_once()
        raise acquire_result
    stopped = acquire_result
    setter = getattr(manager, "set_start_failure_callback", None)
    if callable(setter):
        generation = object()
        manager._api_start_generation = generation

        def release_failed_start(_error):
            if getattr(manager, "_api_start_generation", None) is generation:
                release_owner_once()

        setter(release_failed_start)
    start_future = loop.run_in_executor(None, start_call)
    start_phase = asyncio.create_task(_capture_executor_outcome(start_future))
    try:
        start_ok, start_result = await asyncio.shield(start_phase)
    except asyncio.CancelledError:
        await _wait_executor_completion(start_phase)
        await rollback_start_effects()
        raise
    if not start_ok:
        await rollback_start_effects()
        raise start_result
    return "started", stopped


async def _capture_executor_outcome(future):
    """Normalize executor completion so a cancelled shield cannot log errors."""
    try:
        return True, await future
    except BaseException as exc:
        return False, exc


async def _wait_executor_completion(future):
    """Await an executor future despite repeated cancellation requests."""
    while True:
        if future.done():
            return future.result()
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.done():
                return future.result()


def stop_dashboard_manager(state: dict[str, Any], dashboard: str, manager) -> None:
    """Stop a dashboard, retaining leases while its worker remains active."""
    error = None
    try:
        manager.stop()
    except Exception as exc:
        error = exc
    if _dashboard_worker_alive(manager):
        raise DashboardStopPending(dashboard) from error
    state["resource_manager"].release(f"user:dashboard:{dashboard}")
    if error is not None:
        raise error


async def stop_dashboard_manager_transaction(
    state: dict[str, Any], dashboard: str, manager,
) -> None:
    """Serialize stop with start and keep blocking joins off the event loop."""
    async with _dashboard_start_lock(state):
        await asyncio.to_thread(
            stop_dashboard_manager, state, dashboard, manager,
        )

# Eager-import FastAPI types so that typing.get_type_hints() can resolve
# annotations in closures (e.g. the /ws handler).  The module can still be
# imported without FastAPI — _check_fastapi() gates actual usage.
try:
    from fastapi import (                      # noqa: F401
        FastAPI, WebSocket, WebSocketDisconnect,
        HTTPException, Query, Body, Request, File, UploadFile,
    )
    from fastapi.middleware.cors import CORSMiddleware  # noqa: F401
    from pydantic import BaseModel, StrictInt         # noqa: F401
except ImportError:
    pass


def _check_fastapi():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        return True
    except ImportError:
        return False


def create_app(
    *,
    auth_token: str | None = None,
    project_root: str = ".",
    desktop_instance_id: str | None = None,
    browser_session_timeout: float | None = None,
):
    """Create the FastAPI application.

    Args:
        auth_token: Required token for client authentication.
        project_root: Project root for .mklink/ config lookup.
        desktop_instance_id: Owning Tauri instance identifier, when packaged.
        browser_session_timeout: Browser-tab lease timeout for Web-entry servers.
    """
    if not _check_fastapi():
        raise ImportError(
            "FastAPI backend requires 'gui' extras. "
            "Install with: pip install mklink[gui]"
        )

    # Re-import at function level for type-checker support; the module-level
    # imports above are needed so that from __future__ import annotations
    # does not break closure type hints (especially the WebSocket parameter
    # in the /ws handler).
    from fastapi import (
        FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Body,
        Request, File, UploadFile,
    )
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, StrictInt

    from mklink.remote.server import DeviceDispatcher, make_response, make_error
    from mklink.project_config import (
        load_config, save_config, check_project_config, format_config_status,
        load_project_info, load_rtt_config, save_rtt_config, save_project_info,
        load_project_history, add_to_project_history, remove_from_project_history,
    )

    app = FastAPI(title="MKLink API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "Content-Disposition",
            "X-MKLink-Firmware-Name",
            "X-MKLink-Firmware-Version",
            "X-MKLink-Firmware-Source",
        ],
    )

    # --- Shared state ---
    from mklink.remote.resource_manager import ResourceError, ResourceManager, ResourceGroup
    _state = {
        "device": None,
        "dispatcher": None,
        "last_device_connection": None,
        "auth_token": auth_token,
        "project_root": project_root,
        "desktop_instance_id": desktop_instance_id,
        "resource_manager": ResourceManager(),
    }
    _state["resource_manager"].on_preempt(
        lambda lease, _new_owner: release_resource_owner(_state, lease.owner)
    )
    # Expose shared state on the app so out-of-closure callers (e.g.
    # run_server(auto_connect=True)) can populate it without rebuilding the
    # closure. Route handlers keep using the same ``_state`` dict directly.
    app.state.mklink_state = _state

    browser_sessions = (
        BrowserSessionLease(browser_session_timeout)
        if browser_session_timeout is not None
        else None
    )
    app.state.browser_sessions = browser_sessions
    app.state.request_browser_session_exit = None
    app.state.request_desktop_exit = None
    # A pagehide beacon and a normal disconnect request can arrive together.
    # Serialize the shared-device teardown so only one coroutine can stop
    # dashboards and close the physical probe at a time.
    device_disconnect_lock = asyncio.Lock()

    async def monitor_browser_sessions() -> None:
        interval = min(1.0, max(0.1, browser_sessions.timeout / 4.0))
        while True:
            await asyncio.sleep(interval)
            request_exit = app.state.request_browser_session_exit
            if callable(request_exit) and browser_sessions.should_exit():
                request_exit()
                return

    async def startup_browser_sessions() -> None:
        if browser_sessions is not None:
            app.state.browser_session_task = asyncio.create_task(
                monitor_browser_sessions()
            )

    async def shutdown_browser_sessions() -> None:
        task = getattr(app.state, "browser_session_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app.add_event_handler("startup", startup_browser_sessions)
    app.add_event_handler("shutdown", shutdown_browser_sessions)

    from mklink.remote.embedded_agent import (
        EmbeddedAgentSettings,
        EmbeddedSiteAgentController,
    )

    def reconnect_shared_device(config):
        """Reconnect the one GUI-owned device for a remote lifecycle request."""
        import mklink

        previous = _state.get("last_device_connection") or {}
        port = config.device_port or previous.get("port")
        axf = config.axf or previous.get("axf")
        mcu = previous.get("mcu")
        elf_backend = previous.get("elf_backend")
        with target_debug_lease(_state, "site-agent-reconnect"):
            current = _state.get("device")
            if current is not None:
                remember_device_connection(_state, current, mcu=mcu)
                current.close()
                _state["device"] = None
                _state["dispatcher"] = None
            device = mklink.connect(
                port=port,
                axf=axf,
                mcu=mcu,
                project_root=_state["project_root"],
                elf_backend=elf_backend,
            )
            _state["device"] = device
            _state["dispatcher"] = DeviceDispatcher(device)
            remember_device_connection(_state, device, mcu=mcu)
            return device

    site_agent = EmbeddedSiteAgentController(
        EmbeddedAgentSettings.from_environment(),
        project_root=project_root,
        resource_manager=_state["resource_manager"],
        device_getter=lambda: _state.get("device"),
        device_reconnector=reconnect_shared_device,
    )
    app.state.site_agent = site_agent

    async def startup_site_agent() -> None:
        site_agent.project_root = _state["project_root"]
        await site_agent.start()

    async def shutdown_site_agent() -> None:
        await site_agent.stop()

    app.add_event_handler("startup", startup_site_agent)
    app.add_event_handler("shutdown", shutdown_site_agent)

    from mklink.remote import stream_api
    from mklink.remote.dashboards import SuperWatchTransactionError, get_managers

    stream_registry = stream_api.create_stream_registry()
    stream_types = dict(stream_api.STREAM_TYPES)
    app.state.stream_registry = stream_registry
    app.state.stream_types = stream_types
    dashboard_managers = get_managers()
    _state["dashboard_managers"] = dashboard_managers
    systemview_manager = dashboard_managers["systemview"]
    set_stream_hub = getattr(systemview_manager, "set_stream_hub", None)
    if callable(set_stream_hub):
        set_stream_hub(stream_registry["systemview"])
    vofa_manager = dashboard_managers["vofa"]
    set_vofa_stream_hub = getattr(vofa_manager, "set_stream_hub", None)
    if callable(set_vofa_stream_hub):
        set_vofa_stream_hub(stream_registry["vofa"])
    for stream_name in ("rtt", "superwatch", "serial"):
        manager = dashboard_managers[stream_name]
        setter = getattr(manager, "set_stream_hub", None)
        if callable(setter):
            setter(stream_registry[stream_name])
    rtt_terminal_setter = getattr(
        dashboard_managers["rtt"], "set_terminal_stream_hub", None,
    )
    if callable(rtt_terminal_setter):
        rtt_terminal_setter(stream_registry["rtt-terminal"])
    app.include_router(stream_api.create_stream_router(
        stream_registry, stream_types, auth_token,
    ))
    from mklink.observe_bridge import install_stream_observation
    install_stream_observation(app, stream_registry)

    from starlette.concurrency import run_in_threadpool
    from mklink.remote import online_flash_api

    async def _reparse_active_symbols(
        axf: str | None = None,
        elf_backend: str | None = None,
        *,
        error_status: int = 500,
    ) -> dict:
        device = _state.get("device")
        if not device or not device.connected:
            raise HTTPException(status_code=400, detail="Device not connected")

        manager = get_managers()["superwatch"]
        try:
            if (
                manager._runtime is not None
                and getattr(device, "symbol_catalog", None) is not None
            ):
                if elf_backend is None:
                    args = () if axf is None else (axf,)
                    summary = await run_in_threadpool(
                        manager.reparse_symbols, *args, device=device
                    )
                else:
                    summary = await run_in_threadpool(
                        manager.reparse_symbols,
                        axf,
                        elf_backend,
                        device=device,
                    )
                result = dict(device.axf_status)
                result["rebind"] = summary
            else:
                result = await run_in_threadpool(
                    device.parse_axf, axf, elf_backend=elf_backend
                )
                if manager._runtime is not None and not result.get("error"):
                    await run_in_threadpool(manager.prepare, device)
        except SuperWatchTransactionError as exc:
            raise HTTPException(status_code=409, detail=exc.to_detail()) from exc

        if result.get("error"):
            raise HTTPException(status_code=error_status, detail=result["error"])
        active_axf = result.get("axf_path")
        if axf and not _same_file_source_path(axf, active_axf):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "symbol_source_mismatch",
                    "message": "Symbol parsing completed without activating the requested AXF",
                    "requested_axf": axf,
                    "active_axf": active_axf,
                },
            )
        return result

    online_flash = online_flash_api.create_default_online_flash_services(
        _state["resource_manager"],
        prepare_connect=lambda request: prepare_online_flash_connect(_state, request),
    )
    app.state.online_flash = online_flash
    app.include_router(online_flash_api.create_online_flash_router(online_flash))
    from mklink.remote import offline_download_api
    app.include_router(
        offline_download_api.create_offline_download_router(
            online_flash,
            _state["resource_manager"],
            lambda: _state.get("device"),
        )
    )

    async def shutdown_online_flash() -> None:
        await run_in_threadpool(
            online_flash_api.shutdown_online_flash_services,
            online_flash,
        )

    app.add_event_handler("shutdown", shutdown_online_flash)

    async def shutdown_stream_producers() -> None:
        for stream_name in ("vofa", "rtt", "superwatch", "serial", "systemview"):
            manager = dashboard_managers[stream_name]
            hub = stream_registry[stream_name]
            if getattr(manager, "_stream_hub", None) is not hub:
                continue
            if getattr(manager, "running", False):
                await run_in_threadpool(manager.stop)
            detach = getattr(manager, "detach_stream_hub", None)
            if callable(detach):
                detach(hub)
        rtt_terminal_hub = stream_registry["rtt-terminal"]
        detach_terminal = getattr(
            dashboard_managers["rtt"], "detach_terminal_stream_hub", None,
        )
        if callable(detach_terminal):
            detach_terminal(rtt_terminal_hub)

    app.add_event_handler("shutdown", shutdown_stream_producers)

    async def shutdown_device_and_resources() -> None:
        modbus_manager = dashboard_managers["modbus"]
        if getattr(modbus_manager, "running", False):
            try:
                await run_in_threadpool(modbus_manager.stop)
            except Exception:
                logger.exception("Failed to stop Modbus during shutdown")
        owners = {
            info["owner"]
            for info in _state["resource_manager"].get_status().values()
        }
        for owner in owners:
            try:
                await run_in_threadpool(release_resource_owner, _state, owner)
            except Exception:
                logger.exception("Failed to release resource owner %s", owner)
        _state["resource_manager"].release_all()

        device = _state.get("device")
        if device is not None:
            try:
                await run_in_threadpool(device.close)
            except Exception:
                logger.exception("Failed to close MKLink device during shutdown")
            finally:
                _state["device"] = None
                _state["dispatcher"] = None

    app.add_event_handler("shutdown", shutdown_device_and_resources)

    @app.exception_handler(ResourceError)
    async def resource_error_handler(_request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=409,
            content={"detail": _resource_error_detail(error)},
        )

    @app.exception_handler(DashboardStopPending)
    async def dashboard_stop_pending_handler(_request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": error.detail})

    async def dispatch_rpc(dispatcher, method: str, params: dict, req_id):
        loop = asyncio.get_event_loop()
        if method not in _TARGET_DEBUG_RPC_METHODS:
            return await loop.run_in_executor(
                None, dispatcher.dispatch, method, params, req_id
            )
        try:
            async with async_target_debug_lease(_state, method):
                return await loop.run_in_executor(
                    None, dispatcher.dispatch, method, params, req_id
                )
        except ResourceError as error:
            return make_error(
                -32009,
                json.dumps(_resource_error_detail(error)),
                req_id,
            )

    # Auto-restore last project from history on startup（仅当未显式指定 project_root）
    if project_root == ".":
        try:
            _hist = load_project_history()
            _last = _hist.get("last_project")
            if _last and os.path.isdir(_last):
                _state["project_root"] = _last
        except Exception:
            pass

    # --- Auth middleware ---
    @app.middleware("http")
    async def check_token(request, call_next):
        if _state["auth_token"] and request.url.path.startswith("/api/"):
            token = request.headers.get("X-Auth-Token", "")
            if token != _state["auth_token"]:
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        return await call_next(request)

    # ===================================================================
    # REST API — Configuration
    # ===================================================================

    @app.get("/api/project-root")
    async def get_project_root():
        return {"project_root": _state["project_root"]}

    @app.put("/api/project-root")
    async def set_project_root(path: str = Body(..., embed=True)):
        import os
        p = os.path.abspath(path)
        if not os.path.isdir(p):
            raise HTTPException(status_code=400, detail=f"目录不存在: {p}")
        _state["project_root"] = p
        if site_agent.settings.enabled and site_agent.project_root != p:
            await site_agent.stop()
            site_agent.project_root = p
            await site_agent.start()
        return {"project_root": p}

    @app.get("/api/project-root/browse")
    async def browse_project_root(path: str = ""):
        import os
        p = os.path.abspath(path) if path else os.getcwd()
        if not os.path.isdir(p):
            p = os.path.dirname(p) or os.path.sep
        parent = os.path.dirname(p)
        entries = []
        try:
            for name in sorted(os.listdir(p)):
                full = os.path.join(p, name)
                if os.path.isdir(full) and not name.startswith('.'):
                    entries.append({"name": name, "path": full})
        except PermissionError:
            pass
        available_drives = _project_root_drives()
        return {"current": p, "parent": parent, "dirs": entries, "drives": available_drives}

    @app.get("/api/project-history")
    async def get_project_history():
        return load_project_history()

    @app.post("/api/project-history")
    async def add_project_history(body: dict = Body(default={})):
        path = body.get("path", "")
        if not path:
            raise HTTPException(status_code=400, detail="缺少 path 参数")
        if not os.path.isdir(os.path.abspath(path)):
            raise HTTPException(status_code=400, detail=f"目录不存在: {path}")
        return add_to_project_history(path)

    @app.delete("/api/project-history")
    async def delete_project_history(path: str = ""):
        if not path:
            raise HTTPException(status_code=400, detail="缺少 path 参数")
        return remove_from_project_history(path)

    class ConfigUpdate(BaseModel):
        com_port: str | None = None
        mcu_key: str | None = None
        swd_clock: str | None = None

    @app.get("/api/config")
    async def get_config():
        config = load_config(_state["project_root"])
        return config or {}

    @app.put("/api/config")
    async def update_config(
        com_port: str | None = Body(default=None),
        mcu_key: str | None = Body(default=None),
        swd_clock: str | None = Body(default=None),
    ):
        config = load_config(_state["project_root"]) or {}
        if com_port is not None:
            config["com_port"] = com_port
        if mcu_key is not None:
            config["mcu_key"] = mcu_key
        if swd_clock is not None:
            try:
                parsed_swd_clock = int(swd_clock, 0)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail="SWD 时钟必须是 1 Hz 到 10 MHz 之间的整数",
                )
            if parsed_swd_clock < 1 or parsed_swd_clock > 10_000_000:
                raise HTTPException(
                    status_code=422,
                    detail="SWD 时钟必须是 1 Hz 到 10 MHz 之间的整数",
                )
            config["swd_clock"] = swd_clock
        save_config(_state["project_root"], config)
        return config

    @app.get("/api/config/status")
    async def get_config_status():
        status = check_project_config(_state["project_root"])
        return {
            "is_valid": status.is_valid,
            "has_config": status.has_config,
            "has_project": status.has_keil_project,
            "has_rtt_config": status.has_rtt_config,
            "errors": status.errors,
            "warnings": status.warnings,
            "flm_on_microkeen": status.flm_on_microkeen,
        }

    @app.get("/api/project")
    async def get_project_info():
        info = load_project_info(_state["project_root"])
        return info or {}

    @app.get("/api/rtt-config")
    async def get_rtt_config():
        config = load_rtt_config(_state["project_root"])
        return config or {}

    @app.put("/api/rtt-config")
    async def update_rtt_config(
        rtt_config: dict = Body(default={}),
    ):
        # 校验 rtt_storage_mode（值 ∈ {0, 1}）
        if "rtt_storage_mode" in rtt_config:
            mode = rtt_config["rtt_storage_mode"]
            if mode not in (0, 1):
                raise HTTPException(
                    status_code=400,
                    detail=f"rtt_storage_mode 必须是 0 或 1，得到 {mode}",
                )
        save_rtt_config(_state["project_root"], rtt_config)
        return rtt_config

    @app.post("/api/rtt-find")
    async def rtt_find(
        source_path: str | None = Body(default=None, embed=True),
    ):
        """Auto-detect RTT control block address from ELF/MAP file.

        Scans the project for ELF/MAP files and resolves _SEGGER_RTT address.
        If found, updates rtt_config automatically.
        """
        from mklink.project_config import (
            load_rtt_config, load_keil_project,
        )
        from mklink.rtt_addr import diagnose_rtt_addr

        if source_path:
            result = await asyncio.to_thread(diagnose_rtt_addr, source_path)
            return {
                "found": bool(result.addr),
                "addr": result.addr,
                "source": result.source,
                "source_path": source_path,
                "details": result.details,
                "warnings": result.warnings,
            }

        project_root = _state["project_root"]
        root = Path(project_root)
        project_info = load_keil_project(project_root) or {}

        configured_binary = []
        configured_maps = []
        for key in ("axf_path", "out_path", "map_path"):
            value = project_info.get(key)
            if not value:
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            if not candidate.is_file():
                continue
            target = configured_maps if key == "map_path" else configured_binary
            if candidate not in target:
                target.append(candidate)

        last_response = None

        async def diagnose_sources(source_files):
            nonlocal last_response
            for source_file in source_files:
                source_path = str(source_file)
                result = await asyncio.to_thread(diagnose_rtt_addr, source_path)
                response = {
                    "found": bool(result.addr),
                    "addr": result.addr,
                    "source": result.source,
                    "source_path": source_path,
                    "details": result.details,
                    "warnings": result.warnings,
                }
                if source_file.suffix.lower() == ".map":
                    response["map_path"] = source_path
                last_response = response
                if result.addr:
                    cfg = load_rtt_config(project_root) or {}
                    cfg["rtt_addr"] = result.addr
                    save_rtt_config(project_root, cfg)
                    return response
            return None

        response = await diagnose_sources(configured_binary)
        if response is not None:
            return response

        discovered_binary = []
        discovered_maps = []
        known_files = set(configured_binary + configured_maps)
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate in known_files:
                continue
            suffix = candidate.suffix.lower()
            if suffix in {".axf", ".elf", ".out"}:
                discovered_binary.append(candidate)
            elif suffix == ".map":
                discovered_maps.append(candidate)

        for source_files in (
            sorted(discovered_binary),
            configured_maps,
            sorted(discovered_maps),
        ):
            response = await diagnose_sources(source_files)
            if response is not None:
                return response
        if last_response is not None:
            return last_response
        return {
            "found": False,
            "addr": None,
            "source": "",
            "source_path": None,
            "details": ["未找到 AXF/ELF/OUT/MAP 文件"],
            "warnings": [],
        }

    @app.post("/api/project-init")
    async def project_init():
        """Auto-detect and parse Keil/IAR project, match MCU, save config.

        Scans the project root for .uvprojx or .ewp files, parses project
        info, matches MCU profile, and saves config + project_info + rtt_config.
        """
        import io
        import contextlib
        from mklink.cli import _cli_project_init

        project_root = _state["project_root"]
        # Capture print output from _cli_project_init
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: _cli_project_init(project_root))
            output = buf.getvalue()

            # Reload updated config and project info
            config = load_config(project_root) or {}
            project_info = load_project_info(project_root) or {}
            config_status = check_project_config(project_root)

            # 探针固件版本检查（异步执行，避免阻塞事件循环）
            firmware_check_result: dict = {"status": "skipped"}
            try:
                from mklink import firmware_check as _fc
                port = None
                # Prefer the device's port if currently connected
                dev = _state.get("device")
                if dev is not None and getattr(dev, "port", None):
                    port = dev.port
                root = _fc._resolve_firmware_root()
                check = await loop.run_in_executor(
                    None, _fc.check_probe_firmware, port, root
                )
                firmware_check_result = check.to_dict()
            except Exception as e:
                firmware_check_result = {"status": "skipped", "error": str(e)}

            return {
                "success": True,
                "output": output,
                "config": config,
                "project_info": project_info,
                "config_status": {
                    "is_valid": config_status.is_valid,
                    "has_config": config_status.has_config,
                    "has_project": config_status.has_keil_project,
                    "errors": config_status.errors,
                    "warnings": config_status.warnings,
                },
                "firmware_check": firmware_check_result,
            }
        except Exception as e:
            return {
                "success": False,
                "output": buf.getvalue(),
                "error": str(e),
            }

    @app.post("/api/mcu-detect")
    async def mcu_detect(body: dict = Body(default={})):
        """Detect/create an MCU profile and resolve/copy its FLM file."""
        from mklink.mcu_detect import detect_mcu_profile

        loop = asyncio.get_event_loop()
        project_root = _state["project_root"]
        device = body.get("device")
        flm = body.get("flm")
        port = body.get("port")
        write_profile = bool(body.get("write_profile", True))
        copy_flm = bool(body.get("copy_flm", True))
        read_idcode = bool(body.get("read_idcode", bool(port)))

        def _detect():
            return detect_mcu_profile(
                project_root=project_root,
                device=device,
                flm=flm,
                port=port,
                write_profile=write_profile,
                copy_flm=copy_flm,
                read_idcode=read_idcode,
            )

        if port and read_idcode:
            async with async_target_debug_lease(_state, "mcu-detect"):
                return await loop.run_in_executor(None, _detect)
        return await loop.run_in_executor(None, _detect)

    # ===================================================================
    # REST API — Device Discovery
    # ===================================================================

    @app.get("/api/ports")
    async def list_ports():
        from mklink.discovery import list_available_ports
        return list_available_ports()

    @app.get("/api/ports/discover")
    async def discover_mklink_port():
        from mklink.discovery import find_mklink_cdc_port
        loop = asyncio.get_running_loop()
        port = await loop.run_in_executor(None, find_mklink_cdc_port)
        return {"port": port}

    @app.get("/api/profiles")
    async def list_mcu_profiles():
        from mklink.profiles import load_mcu_profiles
        profiles = load_mcu_profiles()
        return [
            {"key": k, "name": v.get("device_name", k), **v}
            for k, v in profiles.items()
        ]

    @app.get("/api/microkeen")
    async def get_microkeen_info():
        from mklink.discovery import find_microkeen_disk, get_microkeen_flm_path
        disk = find_microkeen_disk()
        flm_dir = get_microkeen_flm_path()
        return {
            "disk_path": disk,
            "flm_dir": flm_dir,
            "available": disk is not None,
        }

    async def _upload_file_source(
        file: UploadFile,
        allowed_suffixes: tuple[str, ...],
    ) -> dict[str, object]:
        try:
            return await run_in_threadpool(
                _store_uploaded_file_source,
                file,
                _state["project_root"],
                allowed_suffixes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await file.close()

    @app.post("/api/files/symbol")
    async def upload_symbol_file(file: UploadFile = File(...)):
        return await _upload_file_source(file, (".axf", ".elf", ".out"))

    @app.post("/api/files/map")
    async def upload_map_file(file: UploadFile = File(...)):
        return await _upload_file_source(file, (".map",))

    # ===================================================================
    # REST API — Device Lifecycle
    # ===================================================================

    class ConnectRequest(BaseModel):
        port: str | None = None
        axf: str | None = None
        mcu: str | None = None
        elf_backend: str | None = None

    @app.post("/api/device/connect")
    async def connect_device(
        port: str | None = Body(default=None),
        axf: str | None = Body(default=None),
        mcu: str | None = Body(default=None),
        elf_backend: str | None = Body(default=None),
        restore_last: bool = Body(default=False),
    ):
        preferred_port = None
        if restore_last:
            previous = _state.get("last_device_connection") or {}
            if port is None:
                preferred_port = previous.get("port")
            axf = axf if axf is not None else previous.get("axf")
            mcu = mcu if mcu is not None else previous.get("mcu")
            elf_backend = (
                elf_backend
                if elf_backend is not None
                else previous.get("elf_backend")
            )
        if _state["device"] and _state["device"].connected:
            dev = _state["device"]
            manager = get_managers()["superwatch"]
            active_axf = axf or (getattr(dev, "axf_status", {}) or {}).get("axf_path")
            if active_axf and (axf is not None or elf_backend is not None):
                await _reparse_active_symbols(axf, elf_backend)
            elif manager._device is not dev:
                await run_in_threadpool(manager.prepare, dev)
            remember_device_connection(_state, dev, mcu=mcu)
            return {
                "status": "already_connected",
                "mcu": dev.mcu_name,
                "idcode": hex(dev.idcode) if dev.idcode else "0x0",
                "port": dev.port,
                "axf_loaded": bool(getattr(dev, "_dwarf_info", None)),
                "elf_backend": dev.axf_status.get("elf_backend"),
                "axf": dev.axf_status,
            }

        import mklink

        # Open the command port first. Target SWD/IDCODE initialization is
        # intentionally deferred so a successful serial connection returns
        # immediately; it is synchronized in the background below.
        loop = asyncio.get_event_loop()

        def _connect():
            return mklink.connect(
                port=port,
                preferred_port=preferred_port,
                axf=axf,
                mcu=mcu,
                project_root=_state["project_root"],
                elf_backend=elf_backend,
                initialize_target_now=False,
            )

        async with async_target_debug_lease(_state, "connect"):
            try:
                device = await loop.run_in_executor(None, _connect)
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        _state["device"] = device
        _state["dispatcher"] = DeviceDispatcher(device)
        await run_in_threadpool(get_managers()["superwatch"].prepare, device)
        remember_device_connection(_state, device, mcu=mcu)

        async def _initialize_target_later():
            try:
                from mklink.device import initialize_target
                async with async_target_debug_lease(_state, "connect-init"):
                    if _state.get("device") is not device or not device.connected:
                        return
                    await run_in_threadpool(
                        initialize_target,
                        device._bridge,
                        device._flash,
                        mcu_hint=mcu,
                        project_root=_state["project_root"],
                    )
                    remember_device_connection(_state, device, mcu=mcu)
            except Exception:
                # Target initialization is best effort; the command session is
                # already usable and later status polling will expose 0/empty
                # until a target becomes available.
                return

        asyncio.create_task(_initialize_target_later())
        return {
            "status": "connected",
            "mcu": device.mcu_name,
            "idcode": hex(device.idcode) if device.idcode else "0x0",
            "port": device.port,
            "axf_loaded": bool(getattr(device, "_dwarf_info", None)),
            "elf_backend": device.axf_status.get("elf_backend"),
            "target_initializing": True,
        }

    async def _disconnect_shared_device() -> dict[str, object]:
        """Stop bridge dashboards and release the GUI-owned Device."""
        async with _dashboard_start_lock(_state):
            async with device_disconnect_lock:
                from mklink.remote.dashboards import BRIDGE_DASHBOARD_TYPES

                stopped = []
                for name in BRIDGE_DASHBOARD_TYPES:
                    manager = dashboard_managers.get(name)
                    if manager is not None and _dashboard_worker_alive(manager):
                        await run_in_threadpool(manager.stop)
                        if _dashboard_worker_alive(manager):
                            raise DashboardStopPending(name)
                        stopped.append(name)
                    _state["resource_manager"].release(f"user:dashboard:{name}")

                device = _state["device"]
                if device:
                    remember_device_connection(_state, device)
                    await run_in_threadpool(device.close)
                    _state["device"] = None
                    _state["dispatcher"] = None
                return {"status": "disconnected", "stopped": stopped}

    @app.post("/api/device/disconnect")
    async def disconnect_device():
        return await _disconnect_shared_device()

    @app.get("/api/device/status")
    async def device_status():
        if not _state["device"]:
            return {"connected": False, "state": "disconnected", "axf": {"loaded": False}}
        dev = _state["device"]
        return {
            "connected": dev.connected,
            "state": dev.state.name if dev.state else "disconnected",
            "mcu": dev.mcu_name if dev.connected else None,
            "idcode": hex(dev.idcode) if dev.connected else None,
            "port": dev.port,
            "axf": dev.axf_status,
        }

    @app.get("/api/probe/firmware-check")
    async def probe_firmware_check():
        """Re-run probe firmware check (no project init required).

        Used by GUI's "重新检测" (recheck) button to verify the user has
        successfully upgraded the probe after seeing the upgrade modal.
        """
        from mklink import firmware_check as _fc
        try:
            port = None
            dev = _state.get("device")
            if dev is not None and getattr(dev, "port", None):
                port = dev.port
            root = _fc._resolve_firmware_root()
            loop = asyncio.get_event_loop()
            check = await loop.run_in_executor(
                None, _fc.check_probe_firmware, port, root
            )
            return check.to_dict()
        except Exception as e:
            return {"status": "skipped", "error": str(e)}

    @app.post("/api/probe/firmware-upgrade")
    async def probe_firmware_upgrade(confirm: bool = Body(default=False)):
        """Upgrade the probe through its UF2 bootloader drive.

        The MICROKEEN volume is the source of truth for this operation.  A
        debug-session connection is optional; when no session is connected the
        endpoint still performs the disk/version check and returns the manual
        UF2 details instead of rejecting the request at the API boundary.
        """
        if confirm is not True:
            raise HTTPException(status_code=400, detail="firmware upgrade requires confirm=true")
        from mklink import firmware_check as _fc

        device = None
        try:
            root = _fc._resolve_firmware_root()
            if _state.get("device") and _state["device"].connected:
                async with _exclusive_probe_control("firmware-upgrade") as (device, stopped):
                    result = await run_in_threadpool(
                        _fc.upgrade_probe_firmware,
                        device,
                        root,
                        confirm=True,
                    )
                    result["stopped"] = stopped
                    return result
            def _upgrade_without_debug_session():
                from mklink.bridge import MKLinkSerialBridge
                from mklink.discovery import find_mklink_cdc_port

                port = find_mklink_cdc_port()
                if not port:
                    return _fc.upgrade_probe_firmware(None, root, confirm=True)
                bridge = MKLinkSerialBridge(port)
                if not bridge.connect():
                    bridge.close()
                    return _fc.upgrade_probe_firmware(None, root, confirm=True)
                try:
                    return _fc.upgrade_probe_firmware(bridge, root, confirm=True)
                finally:
                    bridge.close()

            return await run_in_threadpool(_upgrade_without_debug_session)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        finally:
            if device is not None and not device.connected:
                _state["device"] = None
                _state["dispatcher"] = None

    @app.get("/api/probe/firmware-download")
    async def probe_firmware_download(
        model: str = Query(...),
        family: str = Query(default="microlink"),
    ):
        """Download the newest model/family UF2 with provider fallback."""
        from fastapi.responses import Response
        from mklink import firmware_check as _fc

        normalized_model = model.strip().upper()
        if normalized_model not in {"V3", "V4"}:
            raise HTTPException(status_code=400, detail="firmware model must be V3 or V4")
        normalized_family = family.strip().lower()
        if normalized_family not in {"microlink", "hpmlink"}:
            raise HTTPException(
                status_code=400,
                detail="firmware family must be microlink or hpmlink",
            )
        if normalized_family == "hpmlink" and normalized_model != "V4":
            raise HTTPException(
                status_code=400,
                detail="HPMLink firmware is only available for V4",
            )
        root = _fc._resolve_firmware_root()
        candidate = await run_in_threadpool(
            _fc.latest_firmware,
            normalized_model,
            root,
            family=normalized_family,
        )
        if candidate is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "在线固件通道暂不可用，或尚未发布 "
                    f"{normalized_family} {normalized_model} 固件"
                ),
            )
        temporary = False
        path = None
        try:
            path, temporary, source = await run_in_threadpool(
                _fc._materialize_firmware,
                candidate,
            )
            content = await run_in_threadpool(path.read_bytes)
        except OSError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        finally:
            if temporary and path is not None:
                path.unlink(missing_ok=True)
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{candidate.name}"',
                "Cache-Control": "no-store",
                "X-MKLink-Firmware-Name": candidate.name,
                "X-MKLink-Firmware-Version": candidate.version_str,
                "X-MKLink-Firmware-Source": source,
                "X-MKLink-Firmware-Family": candidate.family,
            },
        )

    # ===================================================================
    # REST API — Device Operations (convenience wrappers)
    # ===================================================================

    @app.post("/api/device/parse-axf")
    async def parse_axf(
        axf: str | None = Body(default=None, embed=True),
        elf_backend: str | None = Body(default=None, embed=True),
    ):
        """手动触发 AXF/ELF 符号表解析。"""
        return await _reparse_active_symbols(axf, elf_backend)

    class FlashRequest(BaseModel):
        firmware: str
        verify: bool = True
        reset_after: bool = True

    @app.post("/api/device/flash")
    async def flash_device(
        firmware: str = Body(...),
        verify: bool = Body(default=True),
        reset_after: bool = Body(default=True),
    ):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        async with async_target_debug_lease(_state, "flash"):
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: _state["device"].flash(
                        firmware, verify=verify, reset_after=reset_after
                    ),
                )
                return result
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/device/reset")
    async def reset_device():
        async with _exclusive_probe_control("reset") as (device, stopped):
            await run_in_threadpool(device.reset)
        return {"status": "ok", "stopped": stopped}

    @asynccontextmanager
    async def _exclusive_probe_control(operation: str):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        from mklink.remote.dashboards import stop_bridge_dashboards

        async with _dashboard_start_lock(_state):
            try:
                stopped = await run_in_threadpool(
                    stop_bridge_dashboards,
                    resource_manager=_state["resource_manager"],
                )
            except Exception as error:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "DASHBOARD_STOP_FAILED",
                        "message": str(error),
                    },
                ) from error
            async with async_target_debug_lease(_state, operation):
                yield _state["device"], stopped

    @app.post("/api/device/power")
    async def set_probe_power(
        voltage_mv: int = Body(...),
        confirm_5v: bool = Body(default=False),
    ):
        try:
            async with _exclusive_probe_control("set-power") as (device, stopped):
                await run_in_threadpool(
                    device.set_power_on,
                    voltage_mv,
                    confirm_5v=confirm_5v,
                )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "status": "ok",
            "power_on": True,
            "voltage_mv": voltage_mv,
            "stopped": stopped,
        }

    @app.post("/api/device/reboot")
    async def reboot_probe():
        device = None
        stopped: list[str] = []
        try:
            async with _exclusive_probe_control("reboot-probe") as (device, stopped):
                remember_device_connection(_state, device)
                await run_in_threadpool(device.reboot)
        finally:
            if device is not None and not device.connected:
                _state["device"] = None
                _state["dispatcher"] = None
        return {"status": "rebooted", "connected": False, "stopped": stopped}

    @app.post("/api/device/erase")
    async def erase_device():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        with target_debug_lease(_state, "erase"):
            try:
                ok = _state["device"].erase_chip()
                return {"success": ok}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/device/halt")
    async def halt_device():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        with target_debug_lease(_state, "halt"):
            s = _state["device"].halt()
        return {"halted": s.halted}

    @app.post("/api/device/resume")
    async def resume_device():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        with target_debug_lease(_state, "resume"):
            s = _state["device"].resume()
        return {"halted": s.halted}

    @app.get("/api/device/hardfault")
    async def check_hardfault():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        with target_debug_lease(_state, "hardfault"):
            return _state["device"].check_hardfault()

    # ===================================================================
    # WebSocket — JSON-RPC (reuses DeviceDispatcher)
    # ===================================================================

    @app.websocket("/ws")
    async def websocket_rpc(websocket: WebSocket):
        await websocket.accept()

        # Auth check
        if _state["auth_token"]:
            try:
                auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
                auth_data = json.loads(auth_msg)
                token = auth_data.get("token") or auth_data.get("params", {}).get("token")
                if token != _state["auth_token"]:
                    await websocket.send_text(make_error(-32001, "Unauthorized"))
                    await websocket.close()
                    return
                # If auth message is also an RPC request, process it
                if auth_data.get("method"):
                    dispatcher = _state.get("dispatcher")
                    if dispatcher:
                        # Strip the token from params before dispatching
                        rpc_params = {
                            k: v for k, v in auth_data.get("params", {}).items()
                            if k != "token"
                        }
                        result = await dispatch_rpc(
                            dispatcher,
                            auth_data["method"],
                            rpc_params,
                            auth_data.get("id"),
                        )
                        await websocket.send_text(result)
            except asyncio.TimeoutError:
                await websocket.close()
                return
            except json.JSONDecodeError:
                await websocket.close()
                return

        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError as e:
                    await websocket.send_text(
                        make_error(-32700, f"Parse error: {e}")
                    )
                    continue

                method = msg.get("method", "")
                params = msg.get("params", {})
                req_id = msg.get("id")

                dispatcher = _state.get("dispatcher")
                if not dispatcher:
                    await websocket.send_text(
                        make_error(-32002, "Device not connected", req_id)
                    )
                    continue

                result_json = await dispatch_rpc(
                    dispatcher, method, params, req_id
                )
                await websocket.send_text(result_json)
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as e:
            logger.error("WebSocket error: %s", e)

    # ===================================================================
    # Integrated Dashboard SSE — all dashboards
    # ===================================================================

    from mklink.remote.dashboards import get_managers

    @app.get("/api/dash/conflict-check")
    async def dash_conflict_check(type: str):
        """检查启动指定 Dashboard 是否会与运行中的 Dashboard 冲突。"""
        from mklink.remote.dashboards import BRIDGE_DASHBOARD_TYPES
        if type not in BRIDGE_DASHBOARD_TYPES:
            return {"conflicts": [], "running": []}
        managers = get_managers()
        running = [
            n for n in BRIDGE_DASHBOARD_TYPES
            if n != type and managers.get(n) and managers[n].running
        ]
        return {"conflicts": running, "running": running}

    @app.get("/api/dash/rtt/stream")
    async def rtt_sse_stream():
        """SSE endpoint for real-time RTT data streaming."""
        from starlette.responses import StreamingResponse
        managers = get_managers()
        rtt = managers["rtt"]
        if not rtt.running:
            raise HTTPException(status_code=400, detail="RTT stream not started")
        return StreamingResponse(
            rtt.sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/dash/rtt/start")
    async def rtt_start(
        addr: str | None = Body(default=None),
        channel: Annotated[StrictInt, Body()] = 0,
        mode: Annotated[StrictInt, Body()] = 0,
        search_size: Annotated[StrictInt, Body()] = 1024,
        encoding: str = Body(default="utf-8"),
    ):
        from mklink.remote.dashboards import normalize_rtt_encoding

        if mode not in (0, 1):
            raise HTTPException(
                status_code=400,
                detail=f"mode 必须是 0 或 1，得到 {mode}",
            )
        try:
            encoding = normalize_rtt_encoding(encoding)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(
                status_code=400,
                detail="Device not connected",
            )
        try:
            from mklink.device import DeviceError, _resolve_rtt_stream_parameters

            addr, channel, search_size, mode = _resolve_rtt_stream_parameters(
                addr,
                channel,
                search_size,
                mode,
                _state["project_root"],
            )
            validate_request = getattr(
                _state["device"], "validate_rtt_stream_request", None,
            )
            if callable(validate_request):
                validate_request(addr, search_size=search_size, mode=mode)
        except (ValueError, DeviceError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        managers = get_managers()
        rtt = managers["rtt"]
        status, stopped = await start_dashboard_manager(
            _state,
            "rtt",
            rtt,
            lambda: rtt.start(
                _state["device"],
                addr=addr,
                channel=channel,
                mode=mode,
                search_size=search_size,
                encoding=encoding,
            ),
        )
        return {"status": status, "stopped": stopped}

    @app.post("/api/dash/rtt/encoding")
    async def rtt_encoding(encoding: str = Body(..., embed=True)):
        managers = get_managers()
        try:
            selected = managers["rtt"].set_encoding(encoding)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"encoding": selected}

    @app.post("/api/dash/rtt/stop")
    async def rtt_stop():
        managers = get_managers()
        await stop_dashboard_manager_transaction(
            _state, "rtt", managers["rtt"],
        )
        return {"status": "stopped"}

    @app.post("/api/dash/rtt/write")
    async def rtt_write(
        data_hex: str = Body(..., embed=True),
    ):
        if len(data_hex) > 65536 * 2:
            raise HTTPException(
                status_code=422,
                detail="data_hex payload must contain 1..65536 bytes",
            )
        if (
            not data_hex
            or len(data_hex) % 2
            or any(char not in "0123456789abcdefABCDEF" for char in data_hex)
        ):
            raise HTTPException(
                status_code=422,
                detail="data_hex must be even hexadecimal",
            )
        data = bytes.fromhex(data_hex)
        managers = get_managers()
        try:
            sent_bytes = await asyncio.to_thread(managers["rtt"].write, data)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"sent_bytes": sent_bytes}

    @app.post("/api/dash/rtt/pause")
    async def rtt_pause():
        managers = get_managers()
        managers["rtt"].pause()
        return {"status": "paused"}

    @app.post("/api/dash/rtt/resume")
    async def rtt_resume():
        managers = get_managers()
        managers["rtt"].resume()
        return {"status": "running"}

    @app.get("/api/dash/rtt/status")
    async def rtt_status():
        managers = get_managers()
        return managers["rtt"].get_status()

    @app.get("/api/dash/rtt/history")
    async def rtt_history():
        managers = get_managers()
        return {"points": managers["rtt"].get_history()}

    # ===================================================================
    # Integrated Dashboard SSE — SystemView（RTOS 跟踪）
    # ===================================================================

    @app.get("/api/dash/systemview/stream")
    async def systemview_sse_stream():
        """SSE endpoint for real-time SystemView RTOS-trace events."""
        from starlette.responses import StreamingResponse
        managers = get_managers()
        sv = managers["systemview"]
        if not sv.running:
            raise HTTPException(status_code=400, detail="SystemView stream not started")
        return StreamingResponse(
            sv.sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/dash/systemview/start")
    async def systemview_start(
        addr: str | None = Body(default=None),
        channel: Annotated[StrictInt, Body()] = 1,
        mode: Annotated[StrictInt, Body()] = 0,
        search_size: Annotated[StrictInt, Body()] = 1024,
    ):
        if mode not in (0, 1):
            raise HTTPException(
                status_code=400,
                detail=f"mode 必须是 0 或 1，得到 {mode}",
            )
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        try:
            from mklink.device import DeviceError, _resolve_rtt_stream_parameters

            addr, channel, search_size, mode = _resolve_rtt_stream_parameters(
                addr,
                channel,
                search_size,
                mode,
                _state["project_root"],
            )
            validate_request = getattr(
                _state["device"], "validate_rtt_stream_request", None,
            )
            if callable(validate_request):
                validate_request(addr, search_size=search_size, mode=mode)
        except (ValueError, DeviceError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        managers = get_managers()
        sv = managers["systemview"]
        status, stopped = await start_dashboard_manager(
            _state,
            "systemview",
            sv,
            lambda: sv.start(
                _state["device"],
                addr=addr,
                channel=channel,
                mode=mode,
                search_size=search_size,
            ),
        )
        return {"status": status, "stopped": stopped}

    @app.post("/api/dash/systemview/stop")
    async def systemview_stop():
        managers = get_managers()
        await stop_dashboard_manager_transaction(
            _state, "systemview", managers["systemview"],
        )
        return {"status": "stopped"}

    @app.post("/api/dash/systemview/pause")
    async def systemview_pause():
        managers = get_managers()
        managers["systemview"].pause()
        return {"status": "paused"}

    @app.post("/api/dash/systemview/resume")
    async def systemview_resume():
        managers = get_managers()
        managers["systemview"].resume()
        return {"status": "running"}

    @app.get("/api/dash/systemview/status")
    async def systemview_status():
        managers = get_managers()
        return managers["systemview"].get_status()

    @app.post("/api/dash/systemview/recording/start")
    async def systemview_recording_start():
        managers = get_managers()
        try:
            return managers["systemview"].start_recording()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/dash/systemview/recording/stop")
    async def systemview_recording_stop():
        managers = get_managers()
        return managers["systemview"].stop_recording()

    @app.get("/api/dash/systemview/history")
    async def systemview_history():
        managers = get_managers()
        return {"points": managers["systemview"].get_history()}

    @app.get("/api/dash/systemview/logs")
    async def systemview_logs():
        from mklink.systemview_logs import list_systemview_logs

        return {"logs": list_systemview_logs(_state["project_root"])}

    @app.get("/api/dash/systemview/logs/download")
    async def systemview_log_download(path: str = Query(...)):
        from fastapi.responses import FileResponse
        from mklink.systemview_logs import (
            SystemViewLogPathError,
            resolve_systemview_log_download,
        )

        try:
            resolved = resolve_systemview_log_download(
                _state["project_root"], path,
            )
        except SystemViewLogPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="SystemView log not found")

        media_type = (
            "application/x-ndjson"
            if resolved.suffix.lower() == ".jsonl"
            else "text/plain; charset=utf-8"
        )
        return FileResponse(
            resolved,
            media_type=media_type,
            filename=resolved.name,
        )

    # ===================================================================
    # Integrated Dashboard SSE — SuperWatch
    # ===================================================================

    @app.get("/api/dash/superwatch/stream")
    async def superwatch_sse_stream():
        from starlette.responses import StreamingResponse
        managers = get_managers()
        sw = managers["superwatch"]
        # Allow SSE connection even when not started — client will receive
        # data once Start is clicked and the poll thread begins pushing.
        return StreamingResponse(
            sw.sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/dash/superwatch/start")
    async def superwatch_start():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        managers = get_managers()
        sw = managers["superwatch"]
        status, stopped = await start_dashboard_manager(
            _state,
            "superwatch",
            sw,
            lambda: sw.start(_state["device"]),
        )
        return {"status": status, "stopped": stopped}

    @app.post("/api/dash/superwatch/stop")
    async def superwatch_stop():
        managers = get_managers()
        await stop_dashboard_manager_transaction(
            _state, "superwatch", managers["superwatch"],
        )
        return {"status": "stopped"}

    @app.post("/api/dash/superwatch/add")
    async def superwatch_add(name: str = Body(..., embed=True)):
        managers = get_managers()
        sw = managers["superwatch"]
        def prepare_and_add():
            if sw._runtime is None and _state["device"] and _state["device"].connected:
                sw.prepare(_state["device"])
            return sw.add_watch(name)

        return await run_in_threadpool(prepare_and_add)

    @app.post("/api/dash/superwatch/remove")
    async def superwatch_remove(name: str = Body(..., embed=True)):
        managers = get_managers()
        sw = managers["superwatch"]
        def prepare_and_remove():
            if sw._runtime is None and _state["device"] and _state["device"].connected:
                sw.prepare(_state["device"])
            return sw.remove_watch(name)

        return await run_in_threadpool(prepare_and_remove)

    @app.post("/api/dash/superwatch/write")
    async def superwatch_write(
        path: str = Body(...),
        generation: int = Body(...),
        value: object = Body(...),
    ):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        from mklink.remote.dashboards import SuperWatchTransactionError

        manager = get_managers()["superwatch"]
        if manager._device is None:
            manager.prepare(_state["device"])
        try:
            return await run_in_threadpool(
                manager.write_symbol,
                path,
                generation=generation,
                value=value,
            )
        except SuperWatchTransactionError as exc:
            raise HTTPException(status_code=409, detail=exc.to_detail()) from exc

    @app.get("/api/dash/superwatch/items")
    async def superwatch_items():
        managers = get_managers()
        items = await run_in_threadpool(managers["superwatch"].list_watches)
        return {"items": items}

    @app.get("/api/dash/superwatch/array-snapshot")
    async def superwatch_array_snapshot():
        managers = get_managers()
        return await run_in_threadpool(
            managers["superwatch"].get_array_snapshot,
        )

    @app.post("/api/dash/superwatch/array-snapshot/select")
    async def superwatch_array_snapshot_select(
        name: str = Body(..., embed=True),
        start_index: int = Body(..., embed=True),
        count: int = Body(..., embed=True),
    ):
        managers = get_managers()
        manager = managers["superwatch"]

        def prepare_and_select():
            if manager._runtime is None and _state["device"] and _state["device"].connected:
                manager.prepare(_state["device"])
            return manager.select_array_snapshot(
                name,
                start_index=start_index,
                count=count,
            )

        from mklink.symbol_catalog import SymbolCatalogError

        try:
            result = await run_in_threadpool(prepare_and_select)
        except SymbolCatalogError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.post("/api/dash/superwatch/array-snapshot/clear")
    async def superwatch_array_snapshot_clear():
        managers = get_managers()
        return await run_in_threadpool(
            managers["superwatch"].clear_array_snapshot,
        )

    @app.get("/api/dash/superwatch/inspect")
    async def superwatch_inspect(name: str):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        managers = get_managers()
        sw = managers["superwatch"]
        if not sw.running:
            raise HTTPException(status_code=400, detail="SuperWatch not running")
        loop = asyncio.get_event_loop()
        tree = await loop.run_in_executor(None, lambda: sw.inspect(name))
        if tree is None:
            return {"tree": None}
        return {"tree": tree}

    @app.post("/api/dash/superwatch/pause")
    async def superwatch_pause():
        managers = get_managers()
        managers["superwatch"].pause()
        return {"status": "paused"}

    @app.post("/api/dash/superwatch/resume")
    async def superwatch_resume():
        managers = get_managers()
        managers["superwatch"].resume()
        return {"status": "running"}

    @app.post("/api/dash/superwatch/interval")
    async def superwatch_interval(interval: float = Body(..., embed=True)):
        managers = get_managers()
        try:
            actual = managers["superwatch"].set_interval(interval)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"interval": actual}

    @app.get("/api/dash/superwatch/status")
    async def superwatch_status():
        managers = get_managers()
        return await run_in_threadpool(managers["superwatch"].get_status)

    # ===================================================================
    # Integrated Dashboard SSE — Serial Monitor
    # ===================================================================

    @app.get("/api/dash/serial/stream")
    async def serial_sse_stream():
        from starlette.responses import StreamingResponse
        managers = get_managers()
        sm = managers["serial"]
        if not sm.running:
            raise HTTPException(status_code=400, detail="Serial monitor not started")
        return StreamingResponse(
            sm.sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/dash/serial/start")
    async def serial_start(
        ports: list[dict] = Body(default=[]),
        baudrate: int = Body(default=115200),
        databits: int = Body(default=8),
        stopbits: int = Body(default=1),
        parity: str = Body(default="N"),
    ):
        """Start serial monitoring on one or more ports.

        Each port config can specify its own baudrate etc., or use the
        top-level defaults for ports that only specify a port name.
        """
        managers = get_managers()
        sm = managers["serial"]
        if sm.running:
            _state["resource_manager"].acquire(
                ResourceGroup.SERIAL_PORT,
                "user:dashboard:serial",
                preempt=True,
            )
            return {"status": "already_running"}

        # Normalize port configs
        port_configs = []
        for p in ports:
            if isinstance(p, str):
                port_configs.append({
                    "port": p, "baudrate": baudrate,
                    "databits": databits, "stopbits": stopbits, "parity": parity,
                })
            elif isinstance(p, dict):
                port_configs.append({
                    "port": p.get("port", ""),
                    "baudrate": p.get("baudrate", baudrate),
                    "databits": p.get("databits", databits),
                    "stopbits": p.get("stopbits", stopbits),
                    "parity": p.get("parity", parity),
                })

        if not port_configs:
            raise HTTPException(status_code=400, detail="No ports specified")

        rm = _state["resource_manager"]
        owner = "user:dashboard:serial"
        try:
            rm.acquire(ResourceGroup.SERIAL_PORT, owner, preempt=True)
        except Exception as e:
            resource = getattr(e, "resource", ResourceGroup.SERIAL_PORT)
            conflict_owner = getattr(e, "conflict_owner", str(e))
            raise HTTPException(
                status_code=409,
                detail={"conflict": conflict_owner, "resource": resource.value},
            )

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: sm.start(port_configs))
        except Exception:
            release_resource_owner(_state, owner, stop_active=True)
            raise
        return {"status": "started"}

    @app.post("/api/dash/serial/stop")
    async def serial_stop():
        result = release_resource_owner(_state, "user:dashboard:serial")
        return {"status": "stopped", **result}

    @app.post("/api/dash/serial/send")
    async def serial_send(
        port: str = Body(...),
        data: str = Body(...),
        hex: bool = Body(default=False),
    ):
        managers = get_managers()
        sm = managers["serial"]
        if not sm.running:
            raise HTTPException(status_code=400, detail="Serial monitor not running")
        if sm.get_ymodem_status()["active"]:
            raise HTTPException(
                status_code=409,
                detail="Serial input is locked by an active YMODEM transfer",
            )
        if hex:
            try:
                data_bytes = bytes.fromhex(data.replace(" ", ""))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid hex string")
        else:
            data_bytes = data.encode("utf-8")
        success = sm.send(port, data_bytes)
        if success:
            return {"ok": True}
        if sm.get_ymodem_status()["active"]:
            raise HTTPException(
                status_code=409,
                detail="Serial input is locked by an active YMODEM transfer",
            )
        raise HTTPException(status_code=500, detail=f"Failed to send to {port}")

    @app.post("/api/dash/serial/ymodem/start")
    async def serial_ymodem_start(
        port: str = Query(...),
        file: UploadFile = File(...),
    ):
        """Upload one bounded file and transfer it over the already-open port."""
        managers = get_managers()
        sm = managers["serial"]
        try:
            if not sm.running:
                raise HTTPException(status_code=400, detail="Serial monitor not running")
            if sm.get_ymodem_status()["active"]:
                raise HTTPException(
                    status_code=409,
                    detail="a YMODEM transfer is already active",
                )
            filename = str(file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
            if not filename:
                raise HTTPException(status_code=400, detail="YMODEM filename is required")
            if any(ord(character) < 0x20 or ord(character) == 0x7F for character in filename):
                raise HTTPException(
                    status_code=400,
                    detail="YMODEM filename contains control characters",
                )
            content = await file.read(_YMODEM_UPLOAD_LIMIT + 1)
        finally:
            await file.close()
        if not content:
            raise HTTPException(status_code=400, detail="YMODEM file is empty")
        if len(content) > _YMODEM_UPLOAD_LIMIT:
            raise HTTPException(
                status_code=413,
                detail="YMODEM file exceeds the 32 MiB upload limit",
            )
        filename_size = len(filename.encode("utf-8"))
        if filename_size > _YMODEM_FILENAME_LIMIT:
            raise HTTPException(
                status_code=400,
                detail="YMODEM filename exceeds the safe 31-byte limit",
            )
        header_size = filename_size + 1 + len(str(len(content))) + 1
        if header_size > 128:
            raise HTTPException(
                status_code=400,
                detail="YMODEM filename is too long for the protocol header",
            )
        try:
            return sm.start_ymodem(port, content, filename)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error))

    @app.get("/api/dash/serial/ymodem/status")
    async def serial_ymodem_status():
        return get_managers()["serial"].get_ymodem_status()

    @app.get("/api/dash/serial/ymodem/trace")
    async def serial_ymodem_trace(after: int = 0, limit: int = 128):
        return get_managers()["serial"].get_ymodem_trace(after, limit)

    @app.post("/api/dash/serial/ymodem/cancel")
    async def serial_ymodem_cancel():
        return get_managers()["serial"].cancel_ymodem()

    @app.get("/api/dash/serial/status")
    async def serial_status():
        managers = get_managers()
        return managers["serial"].get_status()

    # ===================================================================
    # Integrated Dashboard SSE — Modbus
    # ===================================================================

    @app.get("/api/dash/modbus/stream")
    async def modbus_sse_stream():
        from starlette.responses import StreamingResponse
        managers = get_managers()
        mm = managers["modbus"]
        if not mm.running:
            raise HTTPException(status_code=400, detail="Modbus stream not started")
        return StreamingResponse(
            mm.sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/dash/modbus/start")
    async def modbus_start(
        port: str = Body(...),
        slave: int = Body(default=1),
        baudrate: int = Body(default=9600),
        bytesize: int = Body(default=8),
        parity: str = Body(default="N"),
        stopbits: int = Body(default=1),
        timeout: float = Body(default=1.0),
        retries: int = Body(default=0),
        local_echo: bool = Body(default=False),
        registers: list[dict] | None = Body(default=None),
        interval: float = Body(default=1.0),
    ):
        managers = get_managers()
        mm = managers["modbus"]
        if mm.running:
            _state["resource_manager"].acquire(
                ResourceGroup.MODBUS_PORT,
                "user:dashboard:modbus",
                preempt=True,
            )
            return {"status": "already_running"}

        rm = _state["resource_manager"]
        owner = "user:dashboard:modbus"
        try:
            rm.acquire(ResourceGroup.MODBUS_PORT, owner, preempt=True)
        except Exception as e:
            resource = getattr(e, "resource", ResourceGroup.MODBUS_PORT)
            conflict_owner = getattr(e, "conflict_owner", str(e))
            raise HTTPException(
                status_code=409,
                detail={"conflict": conflict_owner, "resource": resource.value},
            )

        try:
            port = str(port).strip()
            parity = str(parity).strip().upper()
            if not port:
                raise ValueError("Serial port is required")
            if isinstance(slave, bool) or not 1 <= int(slave) <= 247:
                raise ValueError("Slave address must be in the range 1..247")
            if isinstance(baudrate, bool) or not 300 <= int(baudrate) <= 4000000:
                raise ValueError("Baud rate must be in the range 300..4000000")
            if bytesize not in (7, 8):
                raise ValueError("Data bits must be 7 or 8")
            if parity not in ("N", "E", "O"):
                raise ValueError("Parity must be N, E or O")
            if stopbits not in (1, 2):
                raise ValueError("Stop bits must be 1 or 2")
            if not 0.05 <= float(timeout) <= 10.0:
                raise ValueError("Timeout must be in the range 0.05..10 seconds")
            if isinstance(retries, bool) or not 0 <= int(retries) <= 5:
                raise ValueError("Retries must be in the range 0..5")
            if not 0.02 <= float(interval) <= 3600.0:
                raise ValueError("Polling interval must be in the range 0.02..3600 seconds")
            from mklink.modbus._client import ModbusClient
            client = ModbusClient(
                port=port,
                baudrate=int(baudrate),
                bytesize=int(bytesize),
                parity=parity,
                stopbits=int(stopbits),
                timeout=float(timeout),
                retries=int(retries),
                handle_local_echo=bool(local_echo),
                trace_packet=mm.trace_packet,
            )
            if not client.open():
                raise HTTPException(
                    status_code=409,
                    detail={
                        "conflict": f"serial port {port} is busy or unavailable",
                        "resource": ResourceGroup.MODBUS_PORT.value,
                    },
                )
        except HTTPException:
            release_resource_owner(_state, owner, stop_active=False)
            raise
        except ValueError as e:
            release_resource_owner(_state, owner, stop_active=False)
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            release_resource_owner(_state, owner, stop_active=False)
            raise HTTPException(status_code=500, detail=f"Modbus connect failed: {e}")

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: mm.start(
                    client,
                    int(slave),
                    registers,
                    float(interval),
                    {
                        "port": port,
                        "baudrate": int(baudrate),
                        "bytesize": int(bytesize),
                        "parity": parity,
                        "stopbits": int(stopbits),
                        "timeout": float(timeout),
                        "retries": int(retries),
                        "local_echo": bool(local_echo),
                    },
                ),
            )
        except Exception:
            release_resource_owner(_state, owner, stop_active=True)
            raise
        return {"status": "started"}

    @app.post("/api/dash/modbus/stop")
    async def modbus_stop():
        result = release_resource_owner(_state, "user:dashboard:modbus")
        return {"status": "stopped", **result}

    @app.post("/api/dash/modbus/write")
    async def modbus_write(
        addr: int = Body(...),
        value: int = Body(...),
    ):
        managers = get_managers()
        mm = managers["modbus"]
        if not mm.running:
            raise HTTPException(status_code=400, detail="Modbus stream not running")
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: mm.write_register(addr, value)
            )
            return result
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/dash/modbus/read")
    async def modbus_read(
        fc: int = Body(default=3),
        start: int = Body(...),
        quantity: int = Body(default=1),
    ):
        managers = get_managers()
        mm = managers["modbus"]
        if not mm.running:
            raise HTTPException(status_code=400, detail="Modbus stream not running")
        try:
            loop = asyncio.get_event_loop()
            values = await loop.run_in_executor(
                None, lambda: mm.read_debug(fc, start, quantity)
            )
            return {"ok": True, "fc": fc, "start": start, "values": values}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/dash/modbus/transaction")
    async def modbus_transaction(
        fc: int = Body(...),
        start: int = Body(...),
        quantity: int | None = Body(default=None),
        values: list[int | bool] | None = Body(default=None),
    ):
        mm = get_managers()["modbus"]
        if not mm.running:
            raise HTTPException(status_code=400, detail="Modbus not connected")
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: mm.transaction(
                    fc, start, quantity=quantity, values=values
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error))

    @app.post("/api/dash/modbus/loop/start")
    async def modbus_loop_start(
        fc: int = Body(...),
        start: int = Body(...),
        quantity: int | None = Body(default=None),
        values: list[int | bool] | None = Body(default=None),
        interval: float = Body(default=1.0),
        count: int = Body(default=0),
    ):
        mm = get_managers()["modbus"]
        if not mm.running:
            raise HTTPException(status_code=400, detail="Modbus not connected")
        try:
            # Validate before starting the background loop so callers get an
            # immediate 400 response instead of a delayed SSE error.
            from mklink.modbus._session import validate_transaction
            validate_transaction(
                fc, start, quantity=quantity, values=values
            )
            return mm.start_loop(
                fc,
                start,
                quantity=quantity,
                values=values,
                interval=interval,
                count=count,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error))

    @app.post("/api/dash/modbus/loop/stop")
    async def modbus_loop_stop():
        return get_managers()["modbus"].stop_loop()

    @app.get("/api/dash/modbus/status")
    async def modbus_status():
        managers = get_managers()
        return managers["modbus"].get_status()

    # ===================================================================
    # Integrated Dashboard SSE — VOFA+ JustFloat
    # ===================================================================

    @app.get("/api/dash/vofa/stream")
    async def vofa_sse_stream():
        from starlette.responses import StreamingResponse
        managers = get_managers()
        vm = managers["vofa"]
        if not vm.running:
            raise HTTPException(status_code=400, detail="VOFA stream not started")
        return StreamingResponse(
            vm.sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/dash/vofa/start")
    async def vofa_start(
        channels: list[dict] | None = Body(default=None),
        interval: float = Body(default=0.1),
    ):
        """Start VOFA JustFloat streaming.

        channels: list of {name, addr, type?, size?} dicts.
        addr can be hex string or int. type defaults to "float", size to 4.
        """
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        from mklink.remote.dashboards import normalize_vofa_interval
        try:
            interval = normalize_vofa_interval(interval)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        managers = get_managers()
        vm = managers["vofa"]
        if not channels:
            channels = list(getattr(vm, "_channels", []) or [])
        if not channels:
            raise HTTPException(
                status_code=400,
                detail="VOFA channels are required before starting",
            )
        from mklink.vofa_viewer import normalize_vofa_channels
        try:
            channels = normalize_vofa_channels(channels)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        status, stopped = await start_dashboard_manager(
            _state,
            "vofa",
            vm,
            lambda: vm.start(_state["device"], channels, interval),
        )
        return {"status": status, "stopped": stopped, "channels": channels}

    @app.post("/api/dash/vofa/stop")
    async def vofa_stop():
        managers = get_managers()
        await stop_dashboard_manager_transaction(
            _state, "vofa", managers["vofa"],
        )
        return {"status": "stopped"}

    @app.post("/api/dash/vofa/pause")
    async def vofa_pause():
        managers = get_managers()
        managers["vofa"].pause()
        return {"status": "paused"}

    @app.post("/api/dash/vofa/resume")
    async def vofa_resume():
        managers = get_managers()
        managers["vofa"].resume()
        return {"status": "running"}

    @app.get("/api/dash/vofa/status")
    async def vofa_status():
        managers = get_managers()
        return managers["vofa"].get_status()

    @app.post("/api/dash/vofa/interval")
    async def vofa_interval(interval: float = Body(..., embed=True)):
        managers = get_managers()
        try:
            actual = managers["vofa"].set_interval(interval)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"interval": actual}

    # ===================================================================
    # Device Memory / Symbols / Registers REST API
    # ===================================================================

    @app.post("/api/device/read-memory")
    async def read_memory(
        address: str = Body(...),
        size: int = Body(...),
    ):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        async with async_target_debug_lease(_state, "read-memory"):
            try:
                loop = asyncio.get_event_loop()
                addr = int(address, 0) if isinstance(address, str) else address
                data = await loop.run_in_executor(
                    None, lambda: _state["device"].read_memory(addr, size)
                )
                import base64
                return {
                    "address": hex(addr),
                    "size": size,
                    "data_base64": base64.b64encode(data).decode(),
                    "data_hex": data.hex(),
                }
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/device/write-memory")
    async def write_memory(
        address: str = Body(...),
        data_hex: str = Body(...),
    ):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        async with async_target_debug_lease(_state, "write-memory"):
            try:
                loop = asyncio.get_event_loop()
                addr = int(address, 0) if isinstance(address, str) else address
                data = bytes.fromhex(data_hex)
                await loop.run_in_executor(
                    None, lambda: _state["device"].write_memory(addr, data)
                )
                return {"status": "ok", "address": hex(addr), "bytes_written": len(data)}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/device/read-variable")
    async def read_variable(name: str = Body(..., embed=True)):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        async with async_target_debug_lease(_state, "read-variable"):
            try:
                loop = asyncio.get_event_loop()
                value = await loop.run_in_executor(
                    None, lambda: _state["device"].read_variable(name)
                )
                return {"name": name, "value": value}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/device/write-variable")
    async def write_variable(
        name: str = Body(...),
        value: int = Body(...),
    ):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        async with async_target_debug_lease(_state, "write-variable"):
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, lambda: _state["device"].write_variable(name, value)
                )
                return {"status": "ok", "name": name, "value": value}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/device/read-register")
    async def read_register(name: str = Body(..., embed=True)):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        async with async_target_debug_lease(_state, "read-register"):
            try:
                loop = asyncio.get_event_loop()
                value = await loop.run_in_executor(
                    None, lambda: _state["device"].read_register(name)
                )
                return {"name": name, "value": value, "hex": hex(value)}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/device/core-registers")
    async def core_registers():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        async with async_target_debug_lease(_state, "core-registers"):
            try:
                loop = asyncio.get_event_loop()
                regs = await loop.run_in_executor(
                    None, _state["device"].read_core_registers
                )
                return {"registers": regs}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/device/hardfault-detail")
    async def hardfault_detail():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        async with async_target_debug_lease(_state, "hardfault-detail"):
            try:
                loop = asyncio.get_event_loop()
                report = await loop.run_in_executor(
                    None, _state["device"].decode_hardfault
                )
                if report is None:
                    return {"fault": None, "summary": "No HardFault detected"}
                return {
                    "fault": True,
                    "cfsr": report.cfsr,
                    "hfsr": report.hfsr,
                    "cfsr_flags": report.cfsr_flags,
                    "hfsr_flags": report.hfsr_flags,
                    "stack_frame": report.stack_frame,
                    "source_locations": report.source_locations,
                    "summary": report.summary,
                    "fault_function": report.fault_function,
                    "fault_location": report.fault_location,
                    "exception_stack": report.exception_stack,
                    "call_stack": report.call_stack,
                    "core_registers": report.core_registers,
                }
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/device/memory-map")
    async def memory_map():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, _state["device"].memory_map
            )
            return result
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ===================================================================
    # Symbols / DWARF API
    # ===================================================================

    def _require_symbol_catalog():
        device = _state.get("device")
        if not device:
            raise HTTPException(status_code=400, detail="No device instance")
        catalog = getattr(device, "symbol_catalog", None)
        if catalog is None:
            raise HTTPException(status_code=400, detail="No DWARF info loaded (need AXF/ELF)")
        return catalog

    @app.get("/api/symbols/status")
    async def symbols_status():
        catalog = _require_symbol_catalog()
        return {
            "loaded": True,
            "generation": catalog.generation,
            "axf_path": catalog.axf_path,
            "parsed_at": catalog.parsed_at,
            "fingerprint": catalog.fingerprint.to_dict(),
            "stale": catalog.is_stale(),
            "total": len(catalog.items),
            "container_count": len(catalog.containers),
            "truncated_roots": list(catalog.truncated_roots),
        }

    @app.get("/api/symbols/catalog")
    async def symbols_catalog(
        q: str = "",
        writable: bool = False,
        offset: int = 0,
        limit: int = 200,
    ):
        catalog = _require_symbol_catalog()
        return catalog.to_page(
            query=q,
            writable=writable,
            offset=offset,
            limit=limit,
        )

    @app.get("/api/symbols/browse")
    async def symbols_browse(
        path: str = "",
        offset: int | None = None,
        limit: int = 256,
    ):
        catalog = _require_symbol_catalog()
        try:
            nodes = (
                catalog.browse_children(path, offset=offset, limit=limit)
                if path
                else catalog.browse_roots()
            )
        except SymbolCatalogError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "generation": catalog.generation,
            "axf_path": catalog.axf_path,
            "fingerprint": catalog.fingerprint.to_dict(),
            "parent": path or None,
            "nodes": [node.to_dict() for node in nodes],
        }

    @app.post("/api/symbols/reparse")
    async def symbols_reparse():
        result = await _reparse_active_symbols(error_status=422)
        return result.get("rebind", result)

    @app.post("/api/symbols/c-layout")
    async def symbols_apply_c_layout(
        variable: str = Body(...),
        definition: str = Body(...),
        pack: int | None = Body(default=None),
    ):
        device = _state.get("device")
        if not device or not device.connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        manager = get_managers()["superwatch"]
        try:
            result = await run_in_threadpool(
                manager.apply_c_definition,
                variable.strip(),
                definition,
                pack,
                device=device,
            )
        except SuperWatchTransactionError as exc:
            status_code = 422 if exc.phase == "c_layout" else 409
            raise HTTPException(status_code=status_code, detail=exc.to_detail()) from exc
        catalog = _require_symbol_catalog()
        return {
            **result,
            "generation": catalog.generation,
            "axf_path": catalog.axf_path,
            "total": len(catalog.items),
            "container_count": len(catalog.containers),
        }

    @app.get("/api/symbols/search")
    async def symbols_search(q: str = ""):
        try:
            catalog = _require_symbol_catalog()
            results = [
                {
                    "name": item.path,
                    "address": item.address,
                    "type": item.type_name,
                    "size": item.size,
                    "descriptor": item.to_dict(),
                }
                for item in catalog.search(q, limit=50)
            ]
            return {"results": results}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/symbols/typeinfo")
    async def symbols_typeinfo(name: str = ""):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        try:
            descriptor = _require_symbol_catalog().by_path(name)
            if descriptor is None:
                return {"name": name, "found": False}
            return {
                "name": name,
                "found": True,
                "type": descriptor.type_name,
                "size": descriptor.size,
                "address": descriptor.address,
                "members": [],
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ===================================================================
    # Health check
    # ===================================================================

    @app.get("/api/health")
    async def health():
        dev = _state["device"]
        from mklink.elf_backend import elf_status

        payload = {
            "status": "ok",
            "device_connected": dev.connected if dev else False,
            **elf_status(project_root=_state["project_root"]),
        }
        if _state["desktop_instance_id"]:
            payload["desktop_instance_id"] = _state["desktop_instance_id"]
        return payload

    @app.post("/api/desktop/shutdown")
    async def desktop_shutdown(instance_id: str = Body(..., embed=True)):
        expected = _state["desktop_instance_id"]
        if not expected:
            raise HTTPException(status_code=404, detail="Desktop shutdown is unavailable")
        if instance_id != expected:
            raise HTTPException(status_code=403, detail="Desktop instance does not match")
        request_exit = app.state.request_desktop_exit
        if not callable(request_exit):
            raise HTTPException(status_code=503, detail="Desktop shutdown is not ready")
        request_exit()
        return {"status": "shutting_down"}

    @app.get("/api/site-agent/status")
    async def site_agent_status():
        return site_agent.status()

    # ===================================================================
    # Static frontend (Vue 3 dist) — catch-all, lowest priority
    # Registered AFTER all /api/* and /ws routes so they take precedence.
    # ===================================================================
    # AI Session Management
    # ===================================================================

    @app.get("/api/resources/status")
    async def resources_status():
        return _state["resource_manager"].get_status()

    @app.post("/api/resources/release")
    async def resources_release(
        owner: str | None = Body(default=None),
        resource: str | None = Body(default=None),
        stop_active: bool = Body(default=True),
    ):
        if owner:
            return {
                "status": "released",
                **release_resource_owner(_state, owner, stop_active=stop_active),
            }
        if resource:
            try:
                result = release_resource_by_name(
                    _state, resource, stop_active=stop_active,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return {"status": "released", **result}
        raise HTTPException(status_code=400, detail="owner or resource is required")

    @app.post("/api/resources/release-serial")
    async def resources_release_serial(stop_active: bool = Body(default=True, embed=True)):
        result = release_resource_by_name(
            _state, ResourceGroup.SERIAL_PORT.value, stop_active=stop_active,
        )
        return {"status": "released", **result}

    @app.post("/api/resources/release-all")
    async def resources_release_all(stop_active: bool = Body(default=True, embed=True)):
        owners = []
        for info in _state["resource_manager"].get_status().values():
            owner = info["owner"]
            if owner not in owners:
                owners.append(owner)
        if stop_active:
            for owner in _DASHBOARD_OWNER_TO_MANAGER:
                if owner not in owners:
                    owners.append(owner)
        results = [
            release_resource_owner(_state, owner, stop_active=stop_active)
            for owner in owners
        ]
        _state["resource_manager"].release_all()
        return {"status": "released", "results": results}

    def _browser_session_client(client_id: str) -> str:
        client_id = client_id.strip()
        if not client_id or len(client_id) > 128:
            raise HTTPException(status_code=400, detail="invalid browser client id")
        return client_id

    @app.post("/api/browser-session/heartbeat")
    async def browser_session_heartbeat(client_id: str = Body(..., embed=True)):
        if browser_sessions is None:
            return {"enabled": False}
        clients = browser_sessions.renew(_browser_session_client(client_id))
        return {"enabled": True, "clients": clients}

    @app.post("/api/browser-session/release")
    async def browser_session_release(client_id: str = Body(..., embed=True)):
        if browser_sessions is None:
            return {"enabled": False}
        clients = browser_sessions.release(_browser_session_client(client_id))
        # pagehide sends this request before the tab disappears. Release the
        # physical probe immediately instead of waiting for the backend's
        # close-grace timer; unexpected websocket loss still keeps the grace
        # period for transient browser reconnects.
        if clients == 0:
            await _disconnect_shared_device()
        return {"enabled": True, "clients": clients}

    @app.websocket("/ws/browser-session")
    async def browser_session_socket(websocket: WebSocket, client_id: str = Query(...)):
        await websocket.accept()
        if browser_sessions is None:
            await websocket.close(code=1008, reason="browser session lease disabled")
            return
        client_id = _browser_session_client(client_id)
        browser_sessions.renew(client_id)
        renew_interval = min(3.0, max(0.1, browser_sessions.timeout / 3.0))
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        websocket.receive_text(), timeout=renew_interval,
                    )
                except asyncio.TimeoutError:
                    browser_sessions.renew(client_id)
        except WebSocketDisconnect:
            pass
        finally:
            browser_sessions.release(client_id)

    @app.post("/api/session/acquire")
    async def session_acquire(
        session_id: str = Body(...),
        resources: list[str] = Body(default=["mklink_bridge"]),
        ttl: float = Body(default=60.0),
    ):
        """AI agent acquires resource lease(s)."""
        from mklink.remote.resource_manager import ResourceGroup, ResourceError as RErr
        rm = _state["resource_manager"]
        group_map = {
            "mklink_bridge": ResourceGroup.MKLINK_BRIDGE,
            "target_debug": ResourceGroup.TARGET_DEBUG,
            "serial_port": ResourceGroup.SERIAL_PORT,
            "modbus_port": ResourceGroup.MODBUS_PORT,
        }
        owner = f"ai:session:{session_id}"
        requested = [(name, group_map[name]) for name in resources if name in group_map]
        try:
            rm.acquire_many(
                [group for _name, group in requested],
                owner,
                ttl=ttl,
                preempt=False,
            )
            return {
                "status": "acquired",
                "owner": owner,
                "resources": [name for name, _group in requested],
            }
        except RErr as e:
            raise HTTPException(
                status_code=409,
                detail={"conflict": e.conflict_owner, "resource": e.resource.value},
            )

    @app.post("/api/session/release")
    async def session_release(session_id: str = Body(...)):
        """AI agent releases its lease(s)."""
        rm = _state["resource_manager"]
        released = rm.release(f"ai:session:{session_id}")
        return {"status": "released", "resources": [r.value for r in released]}

    @app.get("/api/session/status")
    async def session_status():
        """Current resource allocation status."""
        rm = _state["resource_manager"]
        return rm.get_status()

    # ===================================================================

    from fastapi.responses import FileResponse
    from pathlib import Path as _Path

    _gui_dist = _Path(__file__).resolve().parent.parent.parent / "gui" / "dist"

    if _gui_dist.is_dir():
        import mimetypes

        # Windows registry MIME overrides must not stop ES modules or CSS loading.
        _gui_mime_types = {
            ".js": "application/javascript",
            ".mjs": "application/javascript",
            ".css": "text/css",
        }

        def _gui_media_type(path: _Path) -> str:
            return (
                _gui_mime_types.get(path.suffix.lower())
                or mimetypes.guess_type(str(path))[0]
                or "application/octet-stream"
            )

        _app_shell_headers = {
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        _asset_headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
        }
        _static_headers = {
            "Cache-Control": "no-cache",
        }

        @app.get("/")
        async def serve_index():
            return FileResponse(
                _gui_dist / "index.html",
                headers=_app_shell_headers,
            )

        @app.get("/assets/{file_path:path}")
        async def serve_assets(file_path: str):
            f = _gui_dist / "assets" / file_path
            if f.is_file():
                return FileResponse(
                    f,
                    media_type=_gui_media_type(f),
                    headers=_asset_headers,
                )
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=404, content={"error": "not found"})

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            candidate = _gui_dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(
                    candidate,
                    media_type=_gui_media_type(candidate),
                    headers=_static_headers,
                )
            return FileResponse(
                _gui_dist / "index.html",
                headers=_app_shell_headers,
            )

    return app


def run_server(
    app=None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str | None = None,
    device_port: str | None = None,
    axf: str | None = None,
    project_root: str = ".",
    auto_connect: bool = False,
    desktop_port_end: int | None = None,
    desktop_runtime_info: str | None = None,
    desktop_instance_id: str | None = None,
):
    """Start the FastAPI server.

    Args:
        app: Pre-created FastAPI app (created if not provided).
        host: Bind address.
        port: Bind port.
        auth_token: Required token for authentication.
        device_port: MKLink COM port (auto-detect if None).
        axf: AXF/ELF file for symbol resolution.
        project_root: Project root for .mklink/ config lookup.
        auto_connect: Automatically connect to device on startup.
        desktop_port_end: Last packaged-desktop fallback port.
        desktop_runtime_info: Atomic runtime endpoint handshake file.
        desktop_instance_id: Owning Tauri instance identifier.
    """
    import uvicorn

    if app is None:
        app = create_app(
            auth_token=auth_token,
            project_root=project_root,
            desktop_instance_id=desktop_instance_id,
        )

    if auto_connect:
        import mklink
        from mklink.remote.resource_manager import ResourceManager

        mks = getattr(app.state, "mklink_state", None)
        if mks is None:
            mks = {
                "device": None,
                "dispatcher": None,
                "last_device_connection": None,
                "resource_manager": ResourceManager(),
            }
            app.state.mklink_state = mks
        elif "resource_manager" not in mks:
            mks["resource_manager"] = ResourceManager()
        try:
            with target_debug_lease(mks, "auto-connect"):
                device = mklink.connect(
                    port=device_port,
                    axf=axf,
                    project_root=project_root,
                )
            # mklink.connect() now initializes idcode/MCU inside Device._connect,
            # so device.idcode is valid here. Store the device in the app's shared
            # state so the API endpoints actually serve it (previously this
            # connected then orphaned the device via a dead loop).
            from mklink.remote.server import DeviceDispatcher
            mks["device"] = device
            mks["dispatcher"] = DeviceDispatcher(device)
            remember_device_connection(mks, device)
            logger.info("Auto-connected device: MCU=%s IDCODE=0x%08X",
                        device.mcu_name, device.idcode)
        except Exception as e:
            logger.warning("Auto-connect failed: %s", e)

    from mklink.observe_bridge import configure_stream_observation

    mklink_state = getattr(app.state, "mklink_state", {})
    observation_token = (
        auth_token
        if auth_token is not None
        else mklink_state.get("auth_token")
    )
    observation_correlation = (
        desktop_instance_id
        or mklink_state.get("desktop_instance_id")
    )

    if desktop_port_end is None:
        configure_stream_observation(
            app,
            host=host,
            port=port,
            auth_token=observation_token,
            private_correlation=observation_correlation,
        )
        browser_sessions = getattr(app.state, "browser_sessions", None)
        if browser_sessions is None:
            uvicorn.run(app, host=host, port=port, log_level="info")
            return
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        app.state.request_browser_session_exit = lambda: setattr(
            server, "should_exit", True
        )
        server.run()
        return

    if not desktop_runtime_info or not desktop_instance_id:
        raise ValueError("desktop runtime info and instance id are required")
    listener, selected_port = _bind_desktop_server_socket(
        host, port, desktop_port_end,
    )
    try:
        configure_stream_observation(
            app,
            host=host,
            port=selected_port,
            auth_token=observation_token,
            private_correlation=observation_correlation,
        )
        _write_desktop_runtime_info(
            desktop_runtime_info,
            port=selected_port,
            instance_id=desktop_instance_id,
        )
        config = uvicorn.Config(app, log_level="info")
        server = uvicorn.Server(config)
        app.state.request_desktop_exit = lambda: setattr(server, "should_exit", True)
        server.run(sockets=[listener])
    finally:
        listener.close()
