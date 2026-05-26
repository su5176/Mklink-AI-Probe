"""串口调试 Web Dashboard 服务器。"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mklink.serial._monitor import SerialMonitor

from mklink.serial._dashboard_html import build_serial_dashboard_html


class _SSEClient:
    """Represents a connected SSE client."""

    def __init__(self, wfile, lock: threading.Lock):
        self._wfile = wfile
        self._lock = lock
        self.alive = True

    def send(self, data: str) -> None:
        try:
            with self._lock:
                self._wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self._wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.alive = False

    def send_comment(self, text: str) -> None:
        try:
            with self._lock:
                self._wfile.write(f": {text}\n\n".encode("utf-8"))
                self._wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.alive = False


class SerialDashboardServer:
    def __init__(
        self,
        monitor: SerialMonitor,
        host: str = "127.0.0.1",
        port: int = 0,
        open_browser: bool = True,
        max_events: int = 500,
    ):
        """Initialize dashboard server attached to a SerialMonitor."""
        self._monitor = monitor
        self._host = host
        self._port = port
        self._open_browser = open_browser
        self._max_events = max_events

        self._clients: list[_SSEClient] = []
        self._clients_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._running = threading.Event()
        self._url: str = ""

        self._events: list[dict] = []
        self._events_lock = threading.Lock()

        self._rx_count = 0
        self._tx_count = 0
        self._rx_bytes = 0
        self._tx_bytes = 0
        self._start_time = 0.0

    def _event_callback(self, event) -> None:
        """Called by SerialMonitor for each serial event."""
        ts = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
        ms = int((event.timestamp % 1) * 1000)
        timestamp_str = f"{ts}.{ms:03d}"

        raw_hex = event.raw.hex().upper()
        try:
            ascii_repr = event.raw.decode("ascii", errors="replace")
        except Exception:
            ascii_repr = ""

        fields = {}
        crc_valid = None
        if event.parsed:
            crc_valid = event.parsed.crc_valid
            if event.parsed.fields:
                for k, v in event.parsed.fields.items():
                    if isinstance(v, dict):
                        fields[k] = {
                            "value": v.get("value", ""),
                            "unit": v.get("unit", ""),
                            "raw": v.get("raw", ""),
                        }
                    else:
                        fields[k] = {"value": str(v), "unit": "", "raw": ""}

        evt_data = {
            "type": "data",
            "timestamp": timestamp_str,
            "port": event.port,
            "direction": event.direction,
            "raw_hex": raw_hex,
            "ascii": ascii_repr,
            "fields": fields,
            "crc_valid": crc_valid,
        }

        if event.direction == "RX":
            self._rx_count += 1
            self._rx_bytes += len(event.raw)
        else:
            self._tx_count += 1
            self._tx_bytes += len(event.raw)

        with self._events_lock:
            self._events.append(evt_data)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events:]

        self._broadcast(json.dumps(evt_data))

    def _broadcast(self, data: str) -> None:
        """Send data to all connected SSE clients."""
        with self._clients_lock:
            dead = []
            for client in self._clients:
                client.send(data)
                if not client.alive:
                    dead.append(client)
            for client in dead:
                self._clients.remove(client)

    def _keepalive_loop(self) -> None:
        """Send keepalive comments to SSE clients every 15 seconds."""
        while self._running.is_set():
            time.sleep(15.0)
            if not self._running.is_set():
                break
            with self._clients_lock:
                dead = []
                for client in self._clients:
                    client.send_comment("keepalive")
                    if not client.alive:
                        dead.append(client)
                for client in dead:
                    self._clients.remove(client)

    def _get_port_statuses(self) -> dict[str, str]:
        """Get current port statuses from monitor."""
        return dict(self._monitor._port_statuses)

    def _get_stats(self) -> dict:
        elapsed = max(time.time() - self._start_time, 1.0)
        return {
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "rx_bytes": self._rx_bytes,
            "tx_bytes": self._tx_bytes,
            "bytes_per_sec": round((self._rx_bytes + self._tx_bytes) / elapsed, 1),
        }

    def _make_handler(self):
        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path == "/":
                    self._serve_html()
                elif self.path == "/events":
                    self._serve_sse()
                elif self.path == "/status":
                    self._serve_status()
                elif self.path == "/auto-reply":
                    self._serve_auto_reply_rules()
                elif self.path == "/profile":
                    self._serve_profile()
                elif self.path.startswith("/static/"):
                    from mklink._static import serve_static
                    if not serve_static(self, self.path[8:]):
                        self.send_error(404)
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.path == "/send":
                    self._handle_send()
                elif self.path == "/send-all":
                    self._handle_send_all()
                elif self.path == "/send-file":
                    self._handle_send_file()
                elif self.path == "/auto-reply":
                    self._handle_auto_reply()
                elif self.path == "/logger":
                    self._handle_logger()
                elif self.path == "/config":
                    self._handle_config()
                else:
                    self.send_error(404)

            def _serve_html(self):
                ports = list(server_ref._monitor._port_statuses.keys())
                profile_name = None
                if server_ref._monitor._profile:
                    profile_name = server_ref._monitor._profile.get("name")
                html = build_serial_dashboard_html(ports, profile_name)
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                write_lock = threading.Lock()
                client = _SSEClient(self.wfile, write_lock)
                with server_ref._clients_lock:
                    server_ref._clients.append(client)

                status_evt = json.dumps({
                    "type": "status",
                    "ports": server_ref._get_port_statuses(),
                })
                client.send(status_evt)

                while client.alive and server_ref._running.is_set():
                    time.sleep(1.0)

                with server_ref._clients_lock:
                    if client in server_ref._clients:
                        server_ref._clients.remove(client)

            def _serve_status(self):
                data = {
                    "ports": server_ref._get_port_statuses(),
                    "stats": server_ref._get_stats(),
                }
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle_send(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length)
                    payload = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "Invalid JSON")
                    return

                port_name = payload.get("port", "")
                data_str = payload.get("data", "")
                is_hex = payload.get("hex", False)

                if is_hex:
                    try:
                        data_bytes = bytes.fromhex(data_str.replace(" ", ""))
                    except ValueError:
                        self._json_response(400, {"error": "Invalid hex string"})
                        return
                else:
                    data_bytes = data_str.encode("utf-8")

                success = server_ref._monitor.send(port_name, data_bytes)
                if success:
                    self._json_response(200, {"ok": True})
                else:
                    self._json_response(500, {"error": f"Failed to send to {port_name}"})

            def _handle_config(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length)
                    payload = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "Invalid JSON")
                    return
                self._json_response(200, {"ok": True, "applied": payload})

            def _serve_auto_reply_rules(self):
                rules = []
                engine = server_ref._monitor._auto_reply_engine
                if engine:
                    for r in engine.rules:
                        rules.append({
                            "match_hex": r.match_hex,
                            "match_regex": r.match_regex,
                            "match_contains": r.match_contains,
                            "reply_hex": r.reply_hex,
                            "reply_ascii": r.reply_ascii,
                            "delay": r.delay,
                            "description": r.description,
                        })
                self._json_response(200, {"rules": rules})

            def _handle_auto_reply(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length)
                    payload = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "Invalid JSON")
                    return

                action = payload.get("action", "")
                engine = server_ref._monitor._auto_reply_engine
                if engine is None:
                    from mklink.serial._autoreply import AutoReplyEngine
                    engine = AutoReplyEngine()
                    server_ref._monitor._auto_reply_engine = engine

                if action == "add":
                    from mklink.serial._autoreply import AutoReplyRule
                    rule_data = payload.get("rule", {})
                    rule = AutoReplyRule(**{
                        k: v for k, v in rule_data.items()
                        if k in AutoReplyRule.__dataclass_fields__
                    })
                    engine.add_rule(rule)
                    self._json_response(200, {"ok": True, "count": len(engine.rules)})
                elif action == "remove":
                    idx = payload.get("index", -1)
                    if 0 <= idx < len(engine.rules):
                        engine.remove_rule(idx)
                        self._json_response(200, {"ok": True, "count": len(engine.rules)})
                    else:
                        self._json_response(400, {"error": "Invalid index"})
                else:
                    self._json_response(400, {"error": f"Unknown action: {action}"})

            def _serve_profile(self):
                profile = server_ref._monitor._profile
                if not profile:
                    self._json_response(200, {"profile": None})
                    return

                info = {
                    "name": profile.get("name", ""),
                    "version": profile.get("version", ""),
                    "frame": {},
                    "fields": [],
                    "ports": [],
                }
                if "frame" in profile:
                    frame_cfg = profile["frame"]
                    info["frame"] = {
                        "header": frame_cfg.get("header", ""),
                        "tail": frame_cfg.get("tail", ""),
                        "crc_algorithm": frame_cfg.get("crc", {}).get("algorithm", ""),
                        "endian": frame_cfg.get("endian", "little"),
                    }
                for f in profile.get("fields", []):
                    info["fields"].append({
                        "name": f["name"],
                        "type": f["type"],
                        "unit": f.get("unit", ""),
                        "offset": f["offset"],
                        "size": f["size"],
                    })
                for cfg in server_ref._monitor._port_configs:
                    info["ports"].append({
                        "port": cfg["port"],
                        "baudrate": cfg.get("baudrate", 115200),
                        "databits": cfg.get("databits", 8),
                        "stopbits": cfg.get("stopbits", 1),
                        "parity": cfg.get("parity", "N"),
                    })
                self._json_response(200, {"profile": info})

            def _handle_send_all(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length)
                    payload = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "Invalid JSON")
                    return

                data_str = payload.get("data", "")
                is_hex = payload.get("hex", False)
                if is_hex:
                    try:
                        data_bytes = bytes.fromhex(data_str.replace(" ", ""))
                    except ValueError:
                        self._json_response(400, {"error": "Invalid hex string"})
                        return
                else:
                    data_bytes = data_str.encode("utf-8")

                server_ref._monitor.send_all(data_bytes)
                self._json_response(200, {"ok": True})

            def _handle_send_file(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length)
                    payload = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "Invalid JSON")
                    return

                port_name = payload.get("port", "")
                file_path = payload.get("path", "")
                is_hex = payload.get("hex", False)

                from pathlib import Path
                p = Path(file_path)
                if not p.is_file():
                    self._json_response(400, {"error": f"File not found: {file_path}"})
                    return

                try:
                    content = p.read_bytes()
                    if not is_hex:
                        data_bytes = content
                    else:
                        data_bytes = bytes.fromhex(content.decode("ascii").strip())
                except Exception as e:
                    self._json_response(400, {"error": str(e)})
                    return

                success = server_ref._monitor.send(port_name, data_bytes)
                if success:
                    self._json_response(200, {"ok": True, "bytes_sent": len(data_bytes)})
                else:
                    self._json_response(500, {"error": f"Failed to send to {port_name}"})

            def _handle_logger(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length)
                    payload = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "Invalid JSON")
                    return

                action = payload.get("action", "")
                if action == "start":
                    path = payload.get("path", "serial_log.txt")
                    fmt = payload.get("format", "txt")
                    max_size = payload.get("max_size", 0)
                    from mklink.serial._logger import FileLogger
                    logger = FileLogger(path=path, format=fmt, max_size=max_size)
                    logger.start()
                    server_ref._monitor._logger = logger
                    self._json_response(200, {"ok": True, "path": path, "format": fmt})
                elif action == "stop":
                    if server_ref._monitor._logger:
                        server_ref._monitor._logger.close()
                        server_ref._monitor._logger = None
                    self._json_response(200, {"ok": True})
                elif action == "status":
                    active = server_ref._monitor._logger is not None
                    info = {"active": active}
                    if active and server_ref._monitor._logger:
                        info["path"] = str(server_ref._monitor._logger._path)
                        info["format"] = server_ref._monitor._logger._format
                    self._json_response(200, info)
                else:
                    self._json_response(400, {"error": f"Unknown action: {action}"})


            def _json_response(self, code: int, data: dict) -> None:
                body = json.dumps(data).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _Handler

    def start(self) -> str:
        """Start the HTTP server. Returns the URL (e.g., 'http://127.0.0.1:8765')."""
        if self._running.is_set():
            return self._url

        self._start_time = time.time()

        original_callback = self._monitor._event_callback

        def _combined_callback(event):
            if original_callback:
                original_callback(event)
            self._event_callback(event)

        self._monitor._event_callback = _combined_callback

        handler_class = self._make_handler()
        self._server = ThreadingHTTPServer((self._host, self._port), handler_class)
        actual_port = self._server.server_address[1]
        self._url = f"http://{self._host}:{actual_port}"

        self._running.set()

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="serial-dashboard-http",
        )
        self._server_thread.start()

        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            daemon=True,
            name="serial-dashboard-keepalive",
        )
        self._keepalive_thread.start()

        if self._open_browser:
            webbrowser.open(self._url)

        return self._url

    def stop(self) -> None:
        """Stop the server."""
        if not self._running.is_set():
            return
        self._running.clear()

        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

        if self._server_thread:
            self._server_thread.join(timeout=3.0)
            self._server_thread = None

        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=3.0)
            self._keepalive_thread = None

        with self._clients_lock:
            self._clients.clear()

    def run_forever(self) -> None:
        """Start and block until Ctrl+C."""
        url = self.start()
        print(f"[*] Serial Dashboard: {url}")
        print("[*] Press Ctrl+C to stop...")
        try:
            while self._running.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[*] Stopping...")
        finally:
            self.stop()
            print("[OK] Serial Dashboard stopped.")
