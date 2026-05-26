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
import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Eager-import FastAPI types so that typing.get_type_hints() can resolve
# annotations in closures (e.g. the /ws handler).  The module can still be
# imported without FastAPI — _check_fastapi() gates actual usage.
try:
    from fastapi import (                      # noqa: F401
        FastAPI, WebSocket, WebSocketDisconnect,
        HTTPException, Query, Body, Request,
    )
    from fastapi.middleware.cors import CORSMiddleware  # noqa: F401
    from pydantic import BaseModel                    # noqa: F401
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
):
    """Create the FastAPI application.

    Args:
        auth_token: Required token for client authentication.
        project_root: Project root for .mklink/ config lookup.
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
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Body, Request
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel

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
    )

    # --- Shared state ---
    from mklink.remote.resource_manager import ResourceManager, ResourceGroup
    _state = {
        "device": None,
        "dispatcher": None,
        "auth_token": auth_token,
        "project_root": project_root,
        "resource_manager": ResourceManager(),
    }

    # Auto-restore last project from history on startup
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
        return {"project_root": p}

    @app.get("/api/project-root/browse")
    async def browse_project_root(path: str = ""):
        import os
        import string
        p = os.path.abspath(path) if path else os.getcwd()
        if not os.path.isdir(p):
            p = os.path.dirname(p) or "C:\\"
        parent = os.path.dirname(p)
        entries = []
        try:
            for name in sorted(os.listdir(p)):
                full = os.path.join(p, name)
                if os.path.isdir(full) and not name.startswith('.'):
                    entries.append({"name": name, "path": full})
        except PermissionError:
            pass
        # 检测可用盘符
        available_drives = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                available_drives.append(f"{letter}:")
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
        save_rtt_config(_state["project_root"], rtt_config)
        return rtt_config

    @app.post("/api/rtt-find")
    async def rtt_find():
        """Auto-detect RTT control block address from MAP/ELF file.

        Scans the project for MAP files and resolves _SEGGER_RTT address.
        If found, updates rtt_config automatically.
        """
        from mklink.project_config import (
            ensure_rtt_config_updated, load_rtt_config, load_keil_project,
        )
        project_root = _state["project_root"]
        project_info = load_keil_project(project_root) or {}
        map_path = project_info.get("map_path")
        if not map_path:
            # Try common locations
            from pathlib import Path
            root = Path(project_root)
            candidates = list(root.glob("**/*.map")) + list(root.glob("**/*.MAP"))
            if candidates:
                map_path = str(candidates[0])

        if map_path:
            from mklink.rtt_addr import diagnose_rtt_addr
            result = diagnose_rtt_addr(map_path)
            if result.addr:
                # Update rtt_config with found address
                cfg = load_rtt_config(project_root) or {}
                cfg["rtt_addr"] = result.addr
                save_rtt_config(project_root, cfg)
                return {
                    "found": True,
                    "addr": result.addr,
                    "source": result.source,
                    "map_path": map_path,
                }
            return {
                "found": False,
                "addr": None,
                "details": result.details,
                "map_path": map_path,
            }
        return {"found": False, "addr": None, "details": ["未找到 MAP 文件"]}

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
            }
        except Exception as e:
            return {
                "success": False,
                "output": buf.getvalue(),
                "error": str(e),
            }

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
        port = find_mklink_cdc_port()
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

    # ===================================================================
    # REST API — Device Lifecycle
    # ===================================================================

    class ConnectRequest(BaseModel):
        port: str | None = None
        axf: str | None = None
        mcu: str | None = None

    @app.post("/api/device/connect")
    async def connect_device(
        port: str | None = Body(default=None),
        axf: str | None = Body(default=None),
        mcu: str | None = Body(default=None),
    ):
        if _state["device"] and _state["device"].connected:
            return {"status": "already_connected", "mcu": _state["device"].mcu_name}

        import mklink
        try:
            device = mklink.connect(
                port=port,
                axf=axf,
                mcu=mcu,
                project_root=_state["project_root"],
            )

            # Read IDCODE in thread to avoid blocking async loop
            loop = asyncio.get_event_loop()
            def _read_idcode():
                idcode = device._flash.get_idcode()
                device._bridge._ctx.idcode = idcode
                from mklink.profiles import load_mcu_profiles, match_mcu_by_idcode
                profiles = load_mcu_profiles()
                # Prefer explicit mcu hint (compatible chips share IDCODE)
                if mcu:
                    p = profiles.get(mcu)
                    if p:
                        device._bridge._ctx.current_mcu = p.get("name", mcu)
                        return idcode
                # Fallback: match by config mcu_key
                from mklink.project_config import load_config
                cfg = load_config(_state["project_root"]) or {}
                cfg_mcu = cfg.get("mcu_key")
                if cfg_mcu:
                    p = profiles.get(cfg_mcu)
                    if p:
                        device._bridge._ctx.current_mcu = p.get("name", cfg_mcu)
                        return idcode
                # Last resort: match by idcode
                matched = match_mcu_by_idcode(idcode, profiles)
                if matched:
                    device._bridge._ctx.current_mcu = profiles[matched].get("name", matched)
                return idcode

            try:
                idcode = await loop.run_in_executor(None, _read_idcode)
            except Exception:
                idcode = 0

            _state["device"] = device
            _state["dispatcher"] = DeviceDispatcher(device)
            return {
                "status": "connected",
                "mcu": device.mcu_name,
                "idcode": hex(idcode) if idcode else "0x0",
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/device/disconnect")
    async def disconnect_device():
        # 停止所有 Dashboard StreamManager，防止孤立后台线程
        from mklink.remote.dashboards import get_managers
        for _name, mgr in get_managers().items():
            if mgr.running:
                mgr.stop()
        # 释放所有资源租约
        if "resource_manager" in _state:
            _state["resource_manager"].release_all()
        # 关闭设备
        if _state["device"]:
            _state["device"].close()
            _state["device"] = None
            _state["dispatcher"] = None
        return {"status": "disconnected"}

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

    # ===================================================================
    # REST API — Device Operations (convenience wrappers)
    # ===================================================================

    @app.post("/api/device/parse-axf")
    async def parse_axf(
        axf: str | None = Body(default=None, embed=True),
    ):
        """手动触发 AXF/ELF 符号表解析。"""
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: _state["device"].parse_axf(axf)
        )
        if result.get("error"):
            raise HTTPException(status_code=500, detail=result["error"])
        return result

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
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        _state["device"].reset()
        return {"status": "ok"}

    @app.post("/api/device/erase")
    async def erase_device():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        try:
            ok = _state["device"].erase_chip()
            return {"success": ok}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/device/halt")
    async def halt_device():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        s = _state["device"].halt()
        return {"halted": s.halted}

    @app.post("/api/device/resume")
    async def resume_device():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        s = _state["device"].resume()
        return {"halted": s.halted}

    @app.get("/api/device/hardfault")
    async def check_hardfault():
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
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
                        result = dispatcher.dispatch(
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

                loop = asyncio.get_event_loop()
                result_json = await loop.run_in_executor(
                    None, dispatcher.dispatch, method, params, req_id
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
        channel: int = Body(default=0),
    ):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        from mklink.remote.dashboards import stop_bridge_dashboards
        stopped = stop_bridge_dashboards(exclude="rtt")
        rm = _state["resource_manager"]
        for name in stopped:
            rm.release(f"user:dashboard:{name}")
        rm.acquire(ResourceGroup.MKLINK_BRIDGE, "user:dashboard:rtt", preempt=True)
        managers = get_managers()
        rtt = managers["rtt"]
        if rtt.running:
            return {"status": "already_running"}
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: rtt.start(_state["device"], addr=addr, channel=channel)
        )
        return {"status": "started", "stopped": stopped}

    @app.post("/api/dash/rtt/stop")
    async def rtt_stop():
        managers = get_managers()
        managers["rtt"].stop()
        _state["resource_manager"].release("user:dashboard:rtt")
        return {"status": "stopped"}

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
        from mklink.remote.dashboards import stop_bridge_dashboards
        stopped = stop_bridge_dashboards(exclude="superwatch")
        rm = _state["resource_manager"]
        for name in stopped:
            rm.release(f"user:dashboard:{name}")
        rm.acquire(ResourceGroup.MKLINK_BRIDGE, "user:dashboard:superwatch", preempt=True)
        managers = get_managers()
        sw = managers["superwatch"]
        if sw.running:
            return {"status": "already_running"}
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: sw.start(_state["device"]))
        return {"status": "started", "stopped": stopped}

    @app.post("/api/dash/superwatch/stop")
    async def superwatch_stop():
        managers = get_managers()
        managers["superwatch"].stop()
        _state["resource_manager"].release("user:dashboard:superwatch")
        return {"status": "stopped"}

    @app.post("/api/dash/superwatch/add")
    async def superwatch_add(name: str = Body(..., embed=True)):
        managers = get_managers()
        sw = managers["superwatch"]
        if sw._runtime is None and _state["device"] and _state["device"].connected:
            sw.prepare(_state["device"])
        return sw.add_watch(name)

    @app.post("/api/dash/superwatch/remove")
    async def superwatch_remove(name: str = Body(..., embed=True)):
        managers = get_managers()
        sw = managers["superwatch"]
        if sw._runtime is None and _state["device"] and _state["device"].connected:
            sw.prepare(_state["device"])
        return sw.remove_watch(name)

    @app.get("/api/dash/superwatch/items")
    async def superwatch_items():
        managers = get_managers()
        return {"items": managers["superwatch"].list_watches()}

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
        actual = managers["superwatch"].set_interval(interval)
        return {"interval": actual}

    @app.get("/api/dash/superwatch/status")
    async def superwatch_status():
        managers = get_managers()
        return managers["superwatch"].get_status()

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

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: sm.start(port_configs))
        return {"status": "started"}

    @app.post("/api/dash/serial/stop")
    async def serial_stop():
        managers = get_managers()
        managers["serial"].stop()
        return {"status": "stopped"}

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
        raise HTTPException(status_code=500, detail=f"Failed to send to {port}")

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
        parity: str = Body(default="N"),
        stopbits: int = Body(default=1),
        registers: list[dict] | None = Body(default=None),
        interval: float = Body(default=1.0),
    ):
        managers = get_managers()
        mm = managers["modbus"]
        if mm.running:
            return {"status": "already_running"}

        try:
            from mklink.modbus._client import ModbusClient
            client = ModbusClient(port=port, baudrate=baudrate,
                                  parity=parity, stopbits=stopbits)
            client.connect()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Modbus connect failed: {e}")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: mm.start(client, slave, registers, interval)
        )
        return {"status": "started"}

    @app.post("/api/dash/modbus/stop")
    async def modbus_stop():
        managers = get_managers()
        managers["modbus"].stop()
        return {"status": "stopped"}

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
        channels: list[dict] = Body(...),
        interval: float = Body(default=0.1),
    ):
        """Start VOFA JustFloat streaming.

        channels: list of {name, addr, type?, size?} dicts.
        addr can be hex string or int. type defaults to "float", size to 4.
        """
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        from mklink.remote.dashboards import stop_bridge_dashboards
        stopped = stop_bridge_dashboards(exclude="vofa")
        rm = _state["resource_manager"]
        for name in stopped:
            rm.release(f"user:dashboard:{name}")
        rm.acquire(ResourceGroup.MKLINK_BRIDGE, "user:dashboard:vofa", preempt=True)
        managers = get_managers()
        vm = managers["vofa"]
        if vm.running:
            return {"status": "already_running"}
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: vm.start(_state["device"], channels, interval)
        )
        return {"status": "started", "stopped": stopped}

    @app.post("/api/dash/vofa/stop")
    async def vofa_stop():
        managers = get_managers()
        managers["vofa"].stop()
        _state["resource_manager"].release("user:dashboard:vofa")
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
        actual = managers["vofa"].set_interval(interval)
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

    @app.get("/api/symbols/search")
    async def symbols_search(q: str = ""):
        device = _state.get("device")
        if not device:
            raise HTTPException(status_code=400, detail="No device instance")
        dwarf = getattr(device, "_dwarf_info", None)
        if not dwarf:
            raise HTTPException(status_code=400, detail="No DWARF info loaded (need AXF/ELF)")
        try:
            results = []
            q_lower = q.lower()
            for name, var in dwarf.variables.items():
                if q_lower in name.lower():
                    results.append({
                        "name": name,
                        "address": var.address,
                        "type": var.type_name,
                        "size": var.size,
                    })
                    if len(results) >= 50:
                        break
            return {"results": results}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/symbols/typeinfo")
    async def symbols_typeinfo(name: str = ""):
        if not _state["device"] or not _state["device"].connected:
            raise HTTPException(status_code=400, detail="Device not connected")
        dwarf = _state["device"]._dwarf_info
        if not dwarf:
            raise HTTPException(status_code=400, detail="No DWARF info loaded (need AXF/ELF)")
        try:
            var = dwarf.variables.get(name)
            if not var:
                return {"name": name, "found": False}
            # 查找 struct 成员信息
            members = []
            struct_def = dwarf.structs.get(var.type_name)
            if struct_def:
                members = [
                    {"name": m.name, "offset": m.offset, "type": m.type_name, "size": m.size}
                    for m in struct_def.members
                ]
            return {
                "name": name,
                "found": True,
                "type": var.type_name,
                "size": var.size,
                "address": var.address,
                "members": members,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ===================================================================
    # Health check
    # ===================================================================

    @app.get("/api/health")
    async def health():
        dev = _state["device"]
        return {
            "status": "ok",
            "device_connected": dev.connected if dev else False,
        }

    # ===================================================================
    # Static frontend (Vue 3 dist) — catch-all, lowest priority
    # Registered AFTER all /api/* and /ws routes so they take precedence.
    # ===================================================================
    # AI Session Management
    # ===================================================================

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
            "serial_port": ResourceGroup.SERIAL_PORT,
            "modbus_port": ResourceGroup.MODBUS_PORT,
        }
        owner = f"ai:session:{session_id}"
        acquired = []
        try:
            for r in resources:
                rg = group_map.get(r)
                if not rg:
                    continue
                rm.acquire(rg, owner, ttl=ttl, preempt=False)
                acquired.append(r)
            return {"status": "acquired", "owner": owner, "resources": acquired}
        except RErr as e:
            rm.release(owner)
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

        @app.get("/")
        async def serve_index():
            return FileResponse(_gui_dist / "index.html")

        @app.get("/assets/{file_path:path}")
        async def serve_assets(file_path: str):
            f = _gui_dist / "assets" / file_path
            if f.is_file():
                ct, _ = mimetypes.guess_type(str(f))
                return FileResponse(f, media_type=ct or "application/octet-stream")
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=404, content={"error": "not found"})

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            candidate = _gui_dist / full_path
            if full_path and candidate.is_file():
                ct, _ = mimetypes.guess_type(str(candidate))
                return FileResponse(candidate, media_type=ct or "application/octet-stream")
            return FileResponse(_gui_dist / "index.html")

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
    """
    import uvicorn

    if app is None:
        app = create_app(auth_token=auth_token, project_root=project_root)

    if auto_connect:
        import mklink
        try:
            device = mklink.connect(port=device_port, axf=axf, project_root=project_root)
            # Store in app state
            from mklink.remote.server import DeviceDispatcher
            _state_ref = None
            for middleware in app.user_middleware:
                # Access the state through the app
                pass
            logger.info("Auto-connected device: MCU=%s IDCODE=0x%08X",
                        device.mcu_name, device.idcode)
        except Exception as e:
            logger.warning("Auto-connect failed: %s", e)

    uvicorn.run(app, host=host, port=port, log_level="info")
