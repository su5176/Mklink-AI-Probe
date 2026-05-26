"""Modbus Web Dashboard — real-time register visualization with interactive controls.

Architecture: HTTP+SSE server plus exactly one Modbus I/O worker thread.
HTTP handlers may run concurrently, but all serial operations must be
submitted to that single worker queue. Never access the Modbus serial port
directly from multiple threads.
Zero new Python dependencies — uses stdlib http.server + threading + queue.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import secrets
import signal
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mklink.modbus._client import ModbusClient, ModbusError
from mklink.modbus._format import RegisterSpec, registers_to_values
from mklink.modbus._poller import _group_consecutive
from mklink.modbus._profile import (
    build_addr_index,
    find_command,
    get_writable_addrs,
    resolve_command,
    validate_param,
)

MAX_BATCH = 125  # Modbus FC03 limit


# ---------------------------------------------------------------------------
# Modbus I/O Worker — serializes all serial operations
# ---------------------------------------------------------------------------


class _ModbusWorker:
    """Single-thread worker that processes read/write requests via a queue."""

    def __init__(self, client: ModbusClient, slave: int):
        self._client = client
        self._slave = slave
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self):
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def submit_read(self, specs: list[RegisterSpec]) -> dict[int, int | float]:
        """Submit a synchronous read request, block until done."""
        result: dict[int, int | float] = {}
        done = threading.Event()
        resp_holder: list[Any] = [None]

        def _do_read():
            try:
                resp_holder[0] = self._batch_read(specs)
            except Exception as e:
                resp_holder[0] = e
            finally:
                done.set()

        self._queue.put(_do_read)
        done.wait(timeout=10.0)
        if isinstance(resp_holder[0], Exception):
            raise resp_holder[0]
        return resp_holder[0] or {}

    def submit_write(self, addr: int, value: int) -> None:
        """Submit a synchronous write request."""
        done = threading.Event()
        error_holder: list = [None]

        def _do_write():
            try:
                self._client.write_register(addr, value, self._slave)
            except Exception as e:
                error_holder[0] = e
            finally:
                done.set()

        self._queue.put(_do_write)
        done.wait(timeout=5.0)
        if error_holder[0]:
            raise error_holder[0]

    def submit_debug_read(self, fc: int, start: int, quantity: int) -> list[int | bool]:
        """Submit a synchronous manual read request for FC01/02/03/04."""
        done = threading.Event()
        resp_holder: list[Any] = [None]

        def _do_read():
            try:
                if fc == 1:
                    resp_holder[0] = self._client.read_coils(start, quantity, self._slave)
                elif fc == 2:
                    resp_holder[0] = self._client.read_discrete_inputs(start, quantity, self._slave)
                elif fc == 3:
                    resp_holder[0] = self._client.read_holding_registers(start, quantity, self._slave)
                elif fc == 4:
                    resp_holder[0] = self._client.read_input_registers(start, quantity, self._slave)
                else:
                    raise ValueError(f"Unsupported read function code: {fc}")
            except Exception as e:
                resp_holder[0] = e
            finally:
                done.set()

        self._queue.put(_do_read)
        done.wait(timeout=10.0)
        if isinstance(resp_holder[0], Exception):
            raise resp_holder[0]
        return list(resp_holder[0] or [])[:quantity]

    def submit_debug_write(self, fc: int, start: int, values: list[int | bool]) -> None:
        """Submit a synchronous manual write request for FC05/06/15/16."""
        done = threading.Event()
        error_holder: list[Any] = [None]

        def _do_write():
            try:
                if fc == 5:
                    self._client.write_coil(start, bool(values[0]), self._slave)
                elif fc == 6:
                    self._client.write_register(start, int(values[0]), self._slave)
                elif fc == 15:
                    self._client.write_coils(start, [bool(v) for v in values], self._slave)
                elif fc == 16:
                    self._client.write_registers(start, [int(v) for v in values], self._slave)
                else:
                    raise ValueError(f"Unsupported write function code: {fc}")
            except Exception as e:
                error_holder[0] = e
            finally:
                done.set()

        self._queue.put(_do_write)
        done.wait(timeout=10.0)
        if error_holder[0]:
            raise error_holder[0]

    def _run(self):
        while self._running.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                task()
            except Exception:
                pass  # errors handled in resp_holder

    def _batch_read(self, specs: list[RegisterSpec]) -> dict[int, int | float]:
        """Read registers in batches of max MAX_BATCH, return {addr: value}."""
        result: dict[int, int | float] = {}
        groups = _group_consecutive(specs)
        for group in groups:
            start_addr = group[0].addr
            count = sum(s.reg_count for s in group)
            # Split oversized batches
            while count > 0:
                n = min(count, MAX_BATCH)
                regs = self._client.read_holding_registers(start_addr, n, self._slave)
                for spec in group:
                    offset = spec.addr - start_addr
                    if offset < 0 or offset + spec.reg_count > len(regs):
                        continue
                    raw = regs[offset: offset + spec.reg_count]
                    vals = registers_to_values(raw, spec.type)
                    if vals:
                        result[spec.addr] = vals[0]
                start_addr += n
                count -= n
        return result


# ---------------------------------------------------------------------------
# Poller — periodic register reads via worker
# ---------------------------------------------------------------------------


class _DashboardPoller:
    """Periodic poller that reads registers in fast/slow groups."""

    def __init__(
        self,
        worker: _ModbusWorker,
        profile: dict,
        on_snapshot,  # callback: (snapshot_dict) -> None
        fast_interval: float = 1.0,
        slow_interval: float = 5.0,
    ):
        self._worker = worker
        self._profile = profile
        self._on_snapshot = on_snapshot
        self._fast_interval = fast_interval
        self._slow_interval = slow_interval
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

        # Build spec lists per poll group
        self._fast_specs: list[RegisterSpec] = []
        self._slow_specs: list[RegisterSpec] = []
        for group in profile.get("groups", []):
            pg = group.get("poll_group", "fast")
            for reg in group.get("registers", []):
                if reg.get("hidden"):
                    continue
                spec = RegisterSpec(
                    addr=reg["addr"],
                    type=reg.get("type", "uint16"),
                    name=reg.get("name", ""),
                )
                if pg == "fast":
                    self._fast_specs.append(spec)
                else:
                    self._slow_specs.append(spec)

        # Sort for consecutive grouping
        self._fast_specs.sort(key=lambda s: s.addr)
        self._slow_specs.sort(key=lambda s: s.addr)

    def start(self):
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _run(self):
        last_fast = 0.0
        last_slow = 0.0
        while self._running.is_set():
            now = time.time()
            snapshot: dict[str, Any] = {"_t": now}

            # Fast group
            if now - last_fast >= self._fast_interval:
                try:
                    data = self._worker.submit_read(self._fast_specs)
                    snapshot["registers"] = data
                    last_fast = now
                except Exception:
                    pass

            # Slow group
            if now - last_slow >= self._slow_interval and self._slow_specs:
                try:
                    data = self._worker.submit_read(self._slow_specs)
                    snapshot.setdefault("registers", {}).update(data)
                    last_slow = now
                except Exception:
                    pass

            if "registers" in snapshot:
                self._on_snapshot(snapshot)

            # Sleep for remainder of fast interval
            elapsed = time.time() - now
            sleep_time = max(0.05, self._fast_interval - elapsed)
            self._running.wait(sleep_time)


# ---------------------------------------------------------------------------
# HTTP / SSE server
# ---------------------------------------------------------------------------


class ModbusDashboardServer:
    """Web dashboard server with SSE real-time data and REST write endpoints."""

    def __init__(
        self,
        client: ModbusClient,
        slave: int,
        profile: dict,
        host: str = "127.0.0.1",
        port: int = 0,
        max_points: int = 500,
        fast_interval: float = 1.0,
        slow_interval: float = 5.0,
        enable_remote_writes: bool = False,
        allow_arbitrary_writes: bool = False,
        html_path: str | None = None,
        idle_timeout: float = 300.0,
    ):
        self._host = host
        self._port = port
        self._max_points = max_points
        self._profile = profile
        self._enable_remote_writes = enable_remote_writes
        self._allow_arbitrary_writes = allow_arbitrary_writes
        self._html_path = html_path
        self._idle_timeout = idle_timeout  # 0 = disabled
        self._idle_since: float | None = None
        self._idle_timer: threading.Thread | None = None
        self._idle_stop = threading.Event()

        # Security
        self._csrf_token = secrets.token_hex(16)

        # Modbus I/O
        self._worker = _ModbusWorker(client, slave)
        self._poller = _DashboardPoller(
            self._worker, profile, self.push_snapshot,
            fast_interval=fast_interval, slow_interval=slow_interval,
        )

        # SSE clients
        self._clients: list[queue.Queue] = []
        self._clients_lock = threading.Lock()
        self._history: list[dict] = []
        self._latest: dict = {}

        # HTTP server
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

        # Addr index for lookups
        self._addr_index = build_addr_index(profile)
        self._writable_addrs = get_writable_addrs(profile)

    @property
    def port(self) -> int:
        if self._httpd:
            return self._httpd.server_address[1]
        return self._port

    def start(self) -> int:
        """Start all services. Returns actual HTTP port."""
        if self._httpd is not None:
            return self.port

        server = self
        _max_points = self._max_points
        _profile = self._profile
        _csrf = self._csrf_token
        _addr_index = self._addr_index
        _writable = self._writable_addrs

        class _Handler(BaseHTTPRequestHandler):
            def log_message(this, fmt, *args):
                pass

            def do_GET(this):
                if this.path == "/" or this.path == "/index.html":
                    this._serve_html()
                elif this.path == "/stream":
                    this._handle_sse()
                elif this.path == "/snapshot":
                    this._serve_json(server._latest)
                elif this.path == "/profile":
                    this._serve_json(_profile)
                elif this.path == "/csrf-token":
                    this._serve_json({"token": _csrf})
                elif this.path.startswith("/static/"):
                    from mklink._static import serve_static
                    if not serve_static(this, this.path[8:]):
                        this.send_error(404)
                else:
                    this.send_error(404)

            def do_POST(this):
                if this.path == "/write":
                    this._handle_write()
                elif this.path == "/command":
                    this._handle_command()
                elif this.path == "/debug/read":
                    this._handle_debug_read()
                elif this.path == "/debug/write":
                    this._handle_debug_write()
                elif this.path == "/api/lang":
                    this._handle_lang()
                else:
                    this.send_error(404)

            # --- GET handlers ---

            def _serve_html(this):
                html_content = None

                # 1. Explicit --html path
                if server._html_path and os.path.isfile(server._html_path):
                    with open(server._html_path, "r", encoding="utf-8") as f:
                        html_content = f.read()

                # 2. Project-level .mklink/modbus_dashboard.html
                if html_content is None:
                    project_html = os.path.join(".mklink", "modbus_dashboard.html")
                    if os.path.isfile(project_html):
                        with open(project_html, "r", encoding="utf-8") as f:
                            html_content = f.read()

                # 3. Fallback: generate from profile
                if html_content is None:
                    from mklink.modbus._dashboard_html import build_html
                    html_content = build_html(_max_points, json.dumps(_profile), _csrf)

                data = html_content.encode("utf-8")
                this.send_response(200)
                this.send_header("Content-Type", "text/html; charset=utf-8")
                this.send_header("Content-Length", str(len(data)))
                this.end_headers()
                this.wfile.write(data)

            def _serve_json(this, obj):
                data = json.dumps(obj).encode("utf-8")
                this.send_response(200)
                this.send_header("Content-Type", "application/json")
                this.send_header("Content-Length", str(len(data)))
                this.end_headers()
                this.wfile.write(data)

            def _handle_sse(this):
                this.send_response(200)
                this.send_header("Content-Type", "text/event-stream")
                this.send_header("Cache-Control", "no-cache")
                this.send_header("Connection", "keep-alive")
                this.end_headers()

                client_q: queue.Queue = queue.Queue(maxsize=100)
                with server._clients_lock:
                    server._clients.append(client_q)

                # Replay history
                try:
                    for pt in server._history[-_max_points:]:
                        client_q.put_nowait(pt)
                except queue.Full:
                    pass

                try:
                    last_hb = time.time()
                    while server._running.is_set():
                        try:
                            pt = client_q.get(timeout=1.0)
                            line = f"data: {json.dumps(pt)}\n\n"
                            this.wfile.write(line.encode("utf-8"))
                            this.wfile.flush()
                        except queue.Empty:
                            now = time.time()
                            if now - last_hb > 15:
                                this.wfile.write(b":ping\n\n")
                                this.wfile.flush()
                                last_hb = now
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with server._clients_lock:
                        if client_q in server._clients:
                            server._clients.remove(client_q)

            # --- POST handlers ---

            def _read_body(this) -> bytes:
                length = int(this.headers.get("Content-Length", 0))
                if length > 4096:
                    return b""
                return this.rfile.read(length) if length > 0 else b""

            def _check_csrf(this, body: dict) -> bool:
                token = body.get("token", "")
                if token != _csrf:
                    return False
                origin = this.headers.get("Origin", "")
                referer = this.headers.get("Referer", "")
                if origin and "127.0.0.1" not in origin and "localhost" not in origin:
                    if not server._enable_remote_writes:
                        return False
                return True

            def _handle_write(this):
                try:
                    body = json.loads(this._read_body())
                except Exception:
                    this._send_error(400, "Invalid JSON")
                    return

                if not this._check_csrf(body):
                    this._send_error(403, "CSRF token mismatch")
                    return

                addr = body.get("addr")
                value = body.get("value")
                if addr is None or value is None:
                    this._send_error(400, "Missing addr or value")
                    return

                addr = int(addr)
                value = int(value)

                # Validate against profile
                ok, msg = validate_param(_profile, addr, value)
                if not ok:
                    this._send_error(422, msg)
                    return

                try:
                    server._worker.submit_write(addr, value)
                    # Push write result as SSE event
                    server.push_event("write_result", {
                        "addr": addr, "value": value, "ok": True,
                        "name": _addr_index.get(addr, {}).get("name", str(addr)),
                    })
                    this._serve_json({"ok": True, "message": "Write successful"})
                except ModbusError as e:
                    server.push_event("write_result", {
                        "addr": addr, "value": value, "ok": False, "error": str(e),
                    })
                    this._send_error(502, f"Modbus error: {e}")

            def _handle_command(this):
                try:
                    body = json.loads(this._read_body())
                except Exception:
                    this._send_error(400, "Invalid JSON")
                    return

                if not this._check_csrf(body):
                    this._send_error(403, "CSRF token mismatch")
                    return

                action = body.get("action", "")
                ok, write_addr, write_value, msg = resolve_command(_profile, action)
                if not ok:
                    this._send_error(422, msg)
                    return

                # Generic parametric command handling
                cmd_def = find_command(_profile, action)
                if cmd_def and "params" in cmd_def:
                    params = body.get("params", {})
                    for param_def in cmd_def["params"]:
                        pval = params.get(param_def["name"])
                        if pval is None:
                            this._send_error(400, f"Missing parameter: {param_def['name']}")
                            return
                        pval = int(pval)
                        pmin = param_def.get("min")
                        pmax = param_def.get("max")
                        if pmin is not None and pval < pmin:
                            this._send_error(422, f"{param_def['name']} must be >= {pmin}")
                            return
                        if pmax is not None and pval > pmax:
                            this._send_error(422, f"{param_def['name']} must be <= {pmax}")
                            return
                        write_value = pval

                try:
                    server._worker.submit_write(write_addr, write_value)
                    server.push_event("command_result", {
                        "action": action, "ok": True,
                        "write_addr": write_addr, "write_value": write_value,
                    })
                    this._serve_json({"ok": True, "message": f"Command '{action}' sent"})
                except ModbusError as e:
                    server.push_event("command_result", {
                        "action": action, "ok": False, "error": str(e),
                    })
                    this._send_error(502, f"Modbus error: {e}")

            def _handle_debug_read(this):
                try:
                    body = json.loads(this._read_body())
                except Exception:
                    this._send_error(400, "Invalid JSON")
                    return

                if not this._check_csrf(body):
                    this._send_error(403, "CSRF token mismatch")
                    return

                try:
                    fc = int(body.get("fc"))
                    start = int(body.get("start"))
                    quantity = int(body.get("quantity", 1))
                except Exception:
                    this._send_error(400, "Invalid fc, start, or quantity")
                    return
                if fc not in (1, 2, 3, 4):
                    this._send_error(422, "Read function code must be 1, 2, 3, or 4")
                    return
                if quantity < 1 or quantity > 125:
                    this._send_error(422, "Quantity must be 1..125")
                    return

                try:
                    values = server._worker.submit_debug_read(fc, start, quantity)
                    payload = {"ok": True, "fc": fc, "start": start, "quantity": quantity, "values": values}
                    server.push_event("debug_result", payload)
                    this._serve_json(payload)
                except ModbusError as e:
                    this._send_error(502, f"Modbus error: {e}")
                except Exception as e:
                    this._send_error(500, str(e))

            def _handle_debug_write(this):
                try:
                    body = json.loads(this._read_body())
                except Exception:
                    this._send_error(400, "Invalid JSON")
                    return

                if not this._check_csrf(body):
                    this._send_error(403, "CSRF token mismatch")
                    return

                try:
                    fc = int(body.get("fc"))
                    start = int(body.get("start"))
                    values = body.get("values", [])
                    if not isinstance(values, list):
                        values = [values]
                    values = [int(v) for v in values]
                except Exception:
                    this._send_error(400, "Invalid fc, start, or values")
                    return
                if fc not in (5, 6, 15, 16):
                    this._send_error(422, "Write function code must be 5, 6, 15, or 16")
                    return
                if not values:
                    this._send_error(400, "Missing values")
                    return

                if not server._allow_arbitrary_writes:
                    for i, value in enumerate(values):
                        addr = start + i
                        ok, msg = validate_param(_profile, addr, value)
                        if not ok:
                            this._send_error(422, msg)
                            return

                try:
                    server._worker.submit_debug_write(fc, start, values)
                    payload = {"ok": True, "fc": fc, "start": start, "values": values}
                    server.push_event("debug_result", payload)
                    this._serve_json(payload)
                except ModbusError as e:
                    this._send_error(502, f"Modbus error: {e}")
                except Exception as e:
                    this._send_error(500, str(e))

            def _handle_lang(this):
                try:
                    body = json.loads(this._read_body())
                except Exception:
                    this._send_error(400, "Invalid JSON")
                    return
                lang = str(body.get("lang", "zh")).strip()
                if lang not in ("zh", "en"):
                    lang = "zh"
                mklink_dir = ".mklink"
                os.makedirs(mklink_dir, exist_ok=True)
                lang_file = os.path.join(mklink_dir, "lang.json")
                with open(lang_file, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"lang": lang}))
                this._serve_json({"status": "ok", "lang": lang})

            def _send_error(this, code: int, message: str):
                data = json.dumps({"ok": False, "error": message}).encode("utf-8")
                this.send_response(code)
                this.send_header("Content-Type", "application/json")
                this.send_header("Content-Length", str(len(data)))
                this.end_headers()
                this.wfile.write(data)

        # Start everything
        self._running.set()
        self._httpd = ThreadingHTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

        self._worker.start()
        self._poller.start()
        self._idle_stop.clear()
        if self._idle_timeout > 0:
            self._idle_timer = threading.Thread(
                target=self._idle_watchdog, daemon=True
            )
            self._idle_timer.start()

        return self.port

    def push_snapshot(self, data: dict) -> None:
        """Push a register snapshot to all SSE clients."""
        if not self._running.is_set():
            return

        # Store latest
        regs = data.get("registers", {})
        self._latest.update(data)
        self._latest["registers"] = regs

        # History ring buffer
        self._history.append(data)
        if len(self._history) > self._max_points:
            self._history = self._history[-self._max_points:]

        # Fan out
        with self._clients_lock:
            dead: list[queue.Queue] = []
            for cq in self._clients:
                try:
                    cq.put_nowait(data)
                except queue.Full:
                    dead.append(cq)
            for dq in dead:
                self._clients.remove(dq)

    def push_event(self, event_type: str, data: dict | None = None) -> None:
        """Push a typed event (write/command result) to SSE clients."""
        if not self._running.is_set():
            return
        payload = {"_event": event_type, "_t": time.time()}
        if data:
            payload.update(data)
        with self._clients_lock:
            dead: list[queue.Queue] = []
            for cq in self._clients:
                try:
                    cq.put_nowait(payload)
                except queue.Full:
                    dead.append(cq)
            for dq in dead:
                self._clients.remove(dq)

    def _idle_watchdog(self) -> None:
        """Auto-stop server after idle timeout with no SSE clients."""
        while not self._idle_stop.wait(timeout=5.0):
            if self._idle_timeout <= 0:
                continue
            with self._clients_lock:
                client_count = len(self._clients)
            if client_count > 0:
                self._idle_since = None
            else:
                if self._idle_since is None:
                    self._idle_since = time.time()
                elif time.time() - self._idle_since >= self._idle_timeout:
                    print(f"[WARN] No clients for {self._idle_timeout}s, auto-stopping server")
                    self.stop()
                    return

    def stop(self) -> None:
        """Shut down everything."""
        if self._idle_stop:
            self._idle_stop.set()
        if self._running.is_set():
            self.push_event("shutdown")
            time.sleep(0.5)
        self._running.clear()
        self._poller.stop()
        self._worker.stop()
        httpd = self._httpd
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._httpd = None
        self._thread = None
        self._idle_timer = None
        with self._clients_lock:
            self._clients.clear()


# ---------------------------------------------------------------------------
# Convenience runner — used by cli.py
# ---------------------------------------------------------------------------


def run_modbus_dashboard(
    client: ModbusClient,
    slave: int,
    profile: dict,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    no_browser: bool = False,
    max_points: int = 500,
    fast_interval: float = 1.0,
    slow_interval: float = 5.0,
    duration: float = 0,
    enable_remote_writes: bool = False,
    allow_arbitrary_writes: bool = False,
    html_path: str | None = None,
) -> None:
    """Run the Modbus dashboard server until KeyboardInterrupt."""
    _stopped = threading.Event()  # idempotent cleanup guard

    server = ModbusDashboardServer(
        client=client,
        slave=slave,
        profile=profile,
        host=host,
        port=port,
        max_points=max_points,
        fast_interval=fast_interval,
        slow_interval=slow_interval,
        enable_remote_writes=enable_remote_writes,
        allow_arbitrary_writes=allow_arbitrary_writes,
        html_path=html_path,
    )
    actual_port = server.start()

    url = f"http://{host}:{actual_port}"
    print(f"[OK] Modbus Dashboard 已启动: {url}")
    if not no_browser:
        print(f"[*] 正在打开浏览器...")
        webbrowser.open(url)

    # -- idempotent cleanup --
    def _cleanup():
        if _stopped.is_set():
            return
        _stopped.set()
        server.stop()
        client.close()

    atexit.register(_cleanup)

    if sys.platform == "win32":
        original_sigbreak = signal.getsignal(signal.SIGBREAK)
        def _sigbreak_handler(signum, frame):
            _cleanup()
            sys.exit(1)
        signal.signal(signal.SIGBREAK, _sigbreak_handler)
    else:
        original_sigterm = signal.getsignal(signal.SIGTERM)
        def _sigterm_handler(signum, frame):
            _cleanup()
            sys.exit(0)
        signal.signal(signal.SIGTERM, _sigterm_handler)

    print(f"[*] Dashboard 运行中，按 Ctrl+C 停止...\n")
    try:
        if duration > 0:
            start = time.time()
            while time.time() - start < duration:
                time.sleep(0.5)
        else:
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[*] 用户中断")

    print("[*] 正在停止...")
    _cleanup()
    print("[OK] Modbus Dashboard 已关闭")
