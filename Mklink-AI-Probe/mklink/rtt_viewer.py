"""RTT data visualization module.

Provides real-time web-dashboard visualization for SEGGER RTT output.
Zero new Python dependencies — uses stdlib http.server + threading + queue.
Browser side uses Chart.js loaded from CDN.
"""

from __future__ import annotations

import atexit
import json
import queue
import re
import signal
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class RttLineParser:
    """Parse RTT text lines into dicts of {name: float}.

    Supports three strategies plus auto-detection:
      - kv:   key-value pairs (key: value or key=value)
      - csv:  comma/tab/space-separated numbers
      - regex: user-supplied named-group pattern
    """

    def __init__(
        self,
        strategy: str = "kv",
        regex_pattern: str | None = None,
        csv_headers: list[str] | None = None,
        delimiter: str | None = None,
    ):
        self.strategy = strategy
        self.regex_pattern = regex_pattern
        self.csv_headers = csv_headers
        self.delimiter = delimiter
        self._compiled_regex: re.Pattern | None = None
        self._header_sniffed = False

    # -- public API --

    def parse(self, line: str) -> dict[str, float] | None:
        """Parse one line. Returns {name: value} or None if unparseable."""
        line = line.strip()
        if not line:
            return None

        if self.strategy == "kv":
            return self._parse_kv(line)
        elif self.strategy == "csv":
            return self._parse_csv(line)
        elif self.strategy == "regex":
            return self._parse_regex(line)
        else:
            return None

    @classmethod
    def auto_detect(cls, sample_lines: list[str]) -> "RttLineParser":
        """Auto-detect parser strategy from a sample of output lines."""
        non_empty = [l.strip() for l in sample_lines if l.strip()]
        if not non_empty:
            return cls("kv")

        # 1) Try key-value
        kv_count = 0
        for line in non_empty:
            if re.search(r"\w+\s*[:=]\s*[-\d.]+", line):
                kv_count += 1
        if kv_count >= max(1, len(non_empty) * 0.5):
            return cls("kv")

        # 2) Try CSV / delimiter
        for line in non_empty:
            # Comma, tab, or multiple spaces
            if "," in line:
                parts = [p.strip() for p in line.split(",")]
            elif "\t" in line:
                parts = [p.strip() for p in line.split("\t")]
            else:
                # Try whitespace
                parts = line.split()
            numeric = sum(1 for p in parts if cls._is_numeric(p.strip("[]()<>{}")))
            if numeric >= 2 and numeric >= len(parts) * 0.6:
                return cls("csv")
        else:
            # No clear CSV lines found — fallback to kv
            pass

        # 3) Fallback: generic number extraction
        return cls("kv")

    # -- internal --

    @staticmethod
    def _is_numeric(s: str) -> bool:
        """Check if a string represents a finite number."""
        # Strip common unit suffixes and brackets
        s = s.strip().rstrip("CcmVAdB%Hzs")
        if not s:
            return False
        try:
            v = float(s)
            import math
            return math.isfinite(v)
        except (ValueError, TypeError):
            return False

    def _parse_kv(self, line: str) -> dict[str, float] | None:
        """Parse key=value or key: value pairs from a line."""
        result: dict[str, float] = {}
        # Match patterns like "key: 123", "key=123", "key: -1.23", "key: 1.23e-4"
        matches = re.findall(
            r"(\w+)\s*[:=]\s*([-\d.]+(?:[eE][+-]?\d+)?)",
            line,
        )
        if not matches:
            return None
        for name, val_str in matches:
            val_str = val_str.strip()
            try:
                v = float(val_str)
                import math
                if math.isfinite(v):
                    result[name] = v
            except (ValueError, TypeError):
                continue
        return result if result else None

    def _parse_csv(self, line: str) -> dict[str, float] | None:
        """Parse delimiter-separated numeric line."""
        # Determine delimiter from first parseable line
        if self.delimiter:
            parts = line.split(self.delimiter)
        elif "," in line:
            parts = line.split(",")
            self.delimiter = ","
        elif "\t" in line:
            parts = line.split("\t")
            self.delimiter = "\t"
        else:
            parts = line.split()
            self.delimiter = " "

        values: list[float] = []
        for p in parts:
            p = p.strip().strip("[]()<>{}")
            if self._is_numeric(p):
                values.append(float(p))
            else:
                values.append(float("nan"))

        if not values:
            return None

        # If no headers yet and first line is all-numeric, auto-generate names
        if not self._header_sniffed:
            all_numeric = all(
                self._is_numeric(p.strip().strip("[]()<>{}")) for p in parts
            )
            if self.csv_headers:
                pass  # user provided
            elif all_numeric:
                self.csv_headers = [f"v{i}" for i in range(len(values))]
            else:
                # This line has text — treat as header row
                self.csv_headers = [p.strip().strip("[]()<>{}") for p in parts]
                self._header_sniffed = True
                return None  # header row is not data
            self._header_sniffed = True

        headers = self.csv_headers or [f"v{i}" for i in range(len(values))]
        result: dict[str, float] = {}
        for i, v in enumerate(values):
            if i < len(headers):
                import math
                if math.isfinite(v):
                    result[headers[i]] = v
        return result if result else None

    def _parse_regex(self, line: str) -> dict[str, float] | None:
        """Parse using a user-supplied regex with named groups."""
        if not self.regex_pattern:
            return None
        if self._compiled_regex is None:
            self._compiled_regex = re.compile(self.regex_pattern)
        m = self._compiled_regex.search(line)
        if not m:
            return None
        result: dict[str, float] = {}
        for name, val_str in m.groupdict().items():
            if val_str is not None:
                try:
                    v = float(val_str)
                    result[name] = v
                except (ValueError, TypeError):
                    continue
        return result if result else None


# ---------------------------------------------------------------------------
# HTTP / SSE server
# ---------------------------------------------------------------------------

class VisualizationServer:
    """Lightweight HTTP server with SSE endpoint for real-time chart data.

    Broadcaster pattern: data is fanned out to per-client queues so a
    slow client never blocks the producer or other clients.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, max_points: int = 500,
                 idle_timeout: float = 300.0, title: str = "MKLink RTT View",
                 mode: str = "RTT", channel_metadata: dict[str, dict] | None = None,
                 superwatch_callbacks: dict[str, object] | None = None,
                 memory_callbacks: dict[str, object] | None = None):
        self._host = host
        self._port = port
        self._max_points = max_points
        self._title = title
        self._mode = mode
        self._channel_metadata = channel_metadata or {}
        self._initial_metadata = dict(self._channel_metadata)  # snapshot for SSE connect
        self._superwatch_callbacks = superwatch_callbacks or {}
        self._memory_callbacks = memory_callbacks or {}
        self._idle_timeout = idle_timeout  # 0 = disabled
        self._idle_since: float | None = None
        self._idle_timer: threading.Thread | None = None
        self._idle_stop = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._clients: list[queue.Queue] = []
        self._clients_lock = threading.Lock()
        self._history: list[dict[str, float]] = []  # ring buffer for replay
        self._stats = {
            "parsed_lines": 0,
            "dropped_lines": 0,
            "start_time": time.time(),
        }
        self._estimated_interval = 0.0
        self._estimated_rate = 0.0
        # Collection control state
        self._collecting = threading.Event()
        self._collecting.set()  # start in "collecting" state
        self._interval = 0.0
        self._collection_state = "running"  # "running" | "paused" | "stopped"
        self._control_lock = threading.Lock()
        self._on_interval_change = None  # Callable[[float], None]
        self._on_stop_requested = None   # Callable[[], None]

    # -- public API --

    @property
    def port(self) -> int:
        if self._httpd:
            return self._httpd.server_address[1]
        return self._port

    @property
    def collecting(self) -> threading.Event:
        """Event that parser threads check before pushing data."""
        return self._collecting

    def start(self) -> int:
        """Start HTTP server in a daemon thread. Returns the actual port."""
        if self._httpd is not None:
            return self.port

        # Bind handler class to this instance
        server = self
        _max_points = self._max_points
        _title = self._title
        _mode = self._mode

        class _Handler(BaseHTTPRequestHandler):
            def log_message(this, fmt, *args):
                pass  # suppress access logs

            def handle(this):
                try:
                    super().handle()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                    pass

            def do_GET(this):
                if this.path == "/" or this.path == "/index.html":
                    this._send_html()
                elif this.path == "/stream":
                    this._handle_sse()
                elif this.path == "/stats":
                    this._send_json(server._stats)
                elif this.path == "/history":
                    this._send_json(list(server._history))
                elif this.path == "/api/status":
                    with server._clients_lock:
                        client_count = len(server._clients)
                    this._send_json({
                        "state": server._collection_state,
                        "interval": server._interval,
                        "estimated_interval": server._estimated_interval,
                        "estimated_rate": server._estimated_rate,
                        "mode": server._mode,
                        "channel_metadata": server._channel_metadata,
                        "clients": client_count,
                    })
                elif this.path.startswith("/static/"):
                    from mklink._static import serve_static
                    if not serve_static(this, this.path[8:]):
                        this.send_error(404)
                else:
                    import urllib.parse
                    parsed = urllib.parse.urlparse(this.path)
                    if parsed.path == "/api/superwatch/search":
                        query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
                        callback = server._superwatch_callbacks.get("search")
                        results = callback(query) if callback else []
                        this._send_json({"results": results})
                    elif parsed.path == "/api/superwatch/inspect":
                        name = urllib.parse.parse_qs(parsed.query).get("name", [""])[0]
                        callback = server._superwatch_callbacks.get("inspect")
                        tree = callback(name) if callback else None
                        this._send_json({"tree": tree})
                    elif parsed.path == "/api/memory/read":
                        qs = urllib.parse.parse_qs(parsed.query)
                        addr_s = qs.get("addr", [""])[0]
                        size_s = qs.get("size", ["256"])[0]
                        try:
                            addr = int(addr_s, 16) if addr_s.startswith("0x") or addr_s.startswith("0X") else int(addr_s, 16)
                            size = int(size_s)
                        except (ValueError, TypeError):
                            this.send_error(400, "Invalid addr or size")
                            return
                        if size < 1 or size > 2048:
                            this.send_error(400, "Size must be 1-2048")
                            return
                        callback = server._memory_callbacks.get("read")
                        if not callback:
                            this.send_error(501, "Memory read not available")
                            return
                        try:
                            result = callback(addr, size)
                            this._send_json(result)
                        except Exception as exc:
                            this._send_json({"error": str(exc)})
                    elif parsed.path == "/api/memory/symbols":
                        query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
                        callback = server._memory_callbacks.get("symbols")
                        results = callback(query) if callback else []
                        this._send_json({"results": results})
                    else:
                        this.send_error(404)

            def do_POST(this):
                import urllib.parse
                path = urllib.parse.urlparse(this.path).path
                if path == "/api/pause":
                    with server._control_lock:
                        server._collecting.clear()
                        server._collection_state = "paused"
                    server.push_event("state_change", {"state": "paused"})
                    this._send_json({"status": "paused"})
                elif path == "/api/resume":
                    with server._control_lock:
                        server._collecting.set()
                        server._collection_state = "running"
                    server.push_event("state_change", {"state": "running"})
                    this._send_json({"status": "running"})
                elif path == "/api/stop":
                    with server._control_lock:
                        server._collecting.clear()
                        server._collection_state = "stopped"
                    if server._on_stop_requested:
                        server._on_stop_requested()
                    this._send_json({"status": "stopping"})
                elif path == "/api/interval":
                    length = int(this.headers.get('Content-Length', 0))
                    body = json.loads(this.rfile.read(length)) if length else {}
                    new_interval = float(body.get("interval", 0))
                    if new_interval < 0 or new_interval > 60:
                        this.send_error(400, "Interval must be 0-60 seconds")
                        return
                    with server._control_lock:
                        server._interval = new_interval
                    if server._on_interval_change:
                        server._on_interval_change(new_interval)
                    server.push_event("interval_change", {"interval": new_interval})
                    this._send_json({"status": "ok", "interval": new_interval})
                elif path == "/api/superwatch/add":
                    length = int(this.headers.get('Content-Length', 0))
                    body = json.loads(this.rfile.read(length)) if length else {}
                    name = str(body.get("name", "")).strip()
                    callback = server._superwatch_callbacks.get("add")
                    if not name or not callback:
                        this.send_error(400, "Missing SuperWatch add callback or name")
                        return
                    try:
                        item = callback(name)
                    except Exception as exc:
                        this._send_json({"item": {"error": str(exc)}})
                        return
                    if isinstance(item, dict) and item.get("name"):
                        server._channel_metadata[item["name"]] = item
                        server.push_event("channel_metadata", {"channels": server._channel_metadata})
                    this._send_json({"item": item})
                elif path == "/api/superwatch/remove":
                    length = int(this.headers.get('Content-Length', 0))
                    body = json.loads(this.rfile.read(length)) if length else {}
                    name = str(body.get("name", "")).strip()
                    callback = server._superwatch_callbacks.get("remove")
                    if not name or not callback:
                        this.send_error(400, "Missing SuperWatch remove callback or name")
                        return
                    result = callback(name)
                    if result.get("removed") and name in server._channel_metadata:
                        del server._channel_metadata[name]
                        server.push_event("channel_metadata", {"channels": server._channel_metadata})
                    this._send_json(result)
                elif path == "/api/lang":
                    length = int(this.headers.get('Content-Length', 0))
                    body = json.loads(this.rfile.read(length)) if length else {}
                    lang = str(body.get("lang", "zh")).strip()
                    if lang not in ("zh", "en"):
                        lang = "zh"
                    _save_lang_preference(lang)
                    this._send_json({"status": "ok", "lang": lang})
                elif path == "/api/memory/write":
                    length = int(this.headers.get('Content-Length', 0))
                    body = json.loads(this.rfile.read(length)) if length else {}
                    addr_s = str(body.get("addr", "")).strip()
                    value_s = str(body.get("value", "")).strip()
                    width = int(body.get("width", 1))
                    try:
                        addr = int(addr_s, 16) if addr_s.startswith(("0x", "0X")) else int(addr_s, 16)
                        value = int(value_s, 16)
                    except (ValueError, TypeError):
                        this.send_error(400, "Invalid addr or value")
                        return
                    if width not in (1, 2, 4, 8):
                        this.send_error(400, "Width must be 1, 2, 4, or 8")
                        return
                    callback = server._memory_callbacks.get("write")
                    if not callback:
                        this.send_error(501, "Memory write not available")
                        return
                    try:
                        result = callback(addr, value, width)
                        this._send_json(result)
                    except Exception as exc:
                        this._send_json({"error": str(exc)})
                else:
                    this.send_error(404)

            def _send_html(this):
                html_content = None

                # 1. Project-level .mklink/rtt_viewer.html (complete custom)
                project_html = os.path.join(".mklink", "rtt_viewer.html")
                if os.path.isfile(project_html):
                    with open(project_html, "r", encoding="utf-8") as f:
                        html_content = f.read()

                # 2. Template-based (user template or built-in)
                if html_content is None:
                    html_content = _build_dashboard_html(_max_points, title=_title, mode=_mode)

                html = html_content
                data = html.encode("utf-8")
                this.send_response(200)
                this.send_header("Content-Type", "text/html; charset=utf-8")
                this.send_header("Content-Length", str(len(data)))
                this.end_headers()
                this.wfile.write(data)

            def _send_json(this, obj):
                data = json.dumps(obj).encode("utf-8")
                this.send_response(200)
                this.send_header("Content-Type", "application/json")
                this.send_header("Content-Length", str(len(data)))
                this.end_headers()
                this.wfile.write(data)

            def _handle_sse(this):
                """Server-Sent Events endpoint — one long-lived connection."""
                this.send_response(200)
                this.send_header("Content-Type", "text/event-stream")
                this.send_header("Cache-Control", "no-cache")
                this.send_header("Connection", "keep-alive")
                this.send_header("Access-Control-Allow-Origin", "*")
                this.end_headers()

                client_queue: queue.Queue = queue.Queue(maxsize=50)
                with server._clients_lock:
                    server._clients.append(client_queue)

                # Send initial metadata to newly connected client
                try:
                    meta = server._initial_metadata if server._mode == "SuperWatch" else server._channel_metadata
                    if meta:
                        client_queue.put_nowait({
                            "_event": "channel_metadata",
                            "channels": meta,
                        })
                    # Only replay history in RTT mode (SuperWatch starts live)
                    if server._mode != "SuperWatch":
                        for point in server._history:
                            client_queue.put_nowait(point)
                except queue.Full:
                    pass

                try:
                    last_heartbeat = time.time()
                    while server._running.is_set():
                        try:
                            point = client_queue.get(timeout=1.0)
                            line = f"data: {json.dumps(point)}\n\n"
                            this.wfile.write(line.encode("utf-8"))
                            this.wfile.flush()
                        except queue.Empty:
                            # Send heartbeat comment every 15s
                            now = time.time()
                            if now - last_heartbeat > 15:
                                this.wfile.write(b":ping\n\n")
                                this.wfile.flush()
                                last_heartbeat = now
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with server._clients_lock:
                        if client_queue in server._clients:
                            server._clients.remove(client_queue)

        self._running.set()
        self._idle_stop.clear()
        self._httpd = ThreadingHTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()
        if self._idle_timeout > 0:
            self._idle_timer = threading.Thread(
                target=self._idle_watchdog, daemon=True
            )
            self._idle_timer.start()
        return self.port

    def push_data_point(self, data: dict[str, float]) -> None:
        """Push a parsed data point to all connected SSE clients."""
        if not self._running.is_set():
            return

        self._update_sample_estimate(data)

        # Maintain history ring buffer
        self._history.append(data)
        if len(self._history) > self._max_points:
            self._history = self._history[-self._max_points:]

        # Fan out to clients
        with self._clients_lock:
            dead: list[queue.Queue] = []
            for cq in self._clients:
                try:
                    cq.put_nowait(data)
                except queue.Full:
                    dead.append(cq)
            for dq in dead:
                self._clients.remove(dq)

    def _update_sample_estimate(self, data: dict[str, float]) -> None:
        """Estimate sample interval/rate from recent timestamped data points."""
        t = data.get("_t")
        if t is None:
            return
        try:
            current_t = float(t)
        except (TypeError, ValueError):
            return
        if not (current_t == current_t and current_t not in (float("inf"), float("-inf"))):
            return
        previous_t = None
        for point in reversed(self._history):
            prev = point.get("_t")
            if prev is None:
                continue
            try:
                previous_t = float(prev)
            except (TypeError, ValueError):
                continue
            break
        if previous_t is None:
            return
        delta = current_t - previous_t
        if delta <= 0:
            return
        if self._estimated_interval > 0:
            self._estimated_interval = (self._estimated_interval * 0.8) + (delta * 0.2)
        else:
            self._estimated_interval = delta
        self._estimated_rate = 1.0 / self._estimated_interval if self._estimated_interval > 0 else 0.0

    def push_event(self, event_type: str, data: dict | None = None) -> None:
        """Push a typed event to all connected SSE clients."""
        if not self._running.is_set():
            return
        payload: dict = {"_event": event_type, "_t": time.time()}
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
        """Shut down the HTTP server."""
        if self._idle_stop:
            self._idle_stop.set()
        if self._running.is_set():
            self.push_event("shutdown")
            time.sleep(0.5)
        self._running.clear()
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
# Dashboard HTML template
# ---------------------------------------------------------------------------

import os

_TEMPLATE_DIR = os.path.dirname(os.path.abspath(__file__))
_BUILTIN_RTT_TEMPLATE = os.path.join(_TEMPLATE_DIR, "_rtt_viewer_template.html")

_MARKER_MAX_PTS = "__MAX_POINTS__"
_MARKER_TITLE = "__TITLE__"
_MARKER_MODE = "__MODE__"
_MARKER_LANG = "__LANG__"


def _load_lang_preference() -> str:
    """Load language preference from .mklink/lang.json, default 'zh'."""
    lang_file = os.path.join(".mklink", "lang.json")
    if os.path.isfile(lang_file):
        try:
            with open(lang_file, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
                return data.get("lang", "zh")
        except Exception:
            pass
    return "zh"


def _save_lang_preference(lang: str) -> None:
    """Save language preference to .mklink/lang.json."""
    mklink_dir = ".mklink"
    os.makedirs(mklink_dir, exist_ok=True)
    lang_file = os.path.join(mklink_dir, "lang.json")
    with open(lang_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({"lang": lang}))


def _find_rtt_template() -> str:
    """Find the best RTT viewer template file path.

    Priority:
      1. Project-level .mklink/rtt_viewer_template.html
      2. Built-in _rtt_viewer_template.html (next to this .py file)
    """
    project_template = os.path.join(".mklink", "rtt_viewer_template.html")
    if os.path.isfile(project_template):
        return project_template
    if os.path.isfile(_BUILTIN_RTT_TEMPLATE):
        return _BUILTIN_RTT_TEMPLATE
    raise FileNotFoundError(
        f"RTT viewer template not found. Searched:\n"
        f"  {os.path.abspath(project_template)}\n"
        f"  {_BUILTIN_RTT_TEMPLATE}"
    )


def _build_dashboard_html(max_points: int = 500, title: str = "MKLink RTT View",
                          mode: str = "RTT") -> str:
    """Return the self-contained dashboard HTML page. Zero external dependencies."""
    template_path = _find_rtt_template()
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace(_MARKER_MAX_PTS, str(max_points))
    html = html.replace(_MARKER_TITLE, title)
    html = html.replace(_MARKER_MODE, mode)
    html = html.replace(_MARKER_LANG, _load_lang_preference())
    return html


# Keep the old f-string as a fallback in case template file is missing
def _build_dashboard_html_fallback(max_points: int = 500) -> str:
    """Fallback: inline HTML for when template file is not available."""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MKLink RTT View</title>
<style>
:root {{
  --bg: #1a1a2e;
  --surface: #16213e;
  --border: #2a2a4a;
  --accent: #00d4aa;
  --text: #e0e0e0;
  --dim: #8888aa;
  --danger: #ff6b6b;
  --warn: #ffd93d;
  --panel-header-h: 34px;
  --raw-log-h: 180px;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  background:var(--bg); color:var(--text);
  font-family:'Segoe UI',system-ui,sans-serif;
  height:100vh; display:flex; flex-direction:column;
  overflow:hidden;
}}
header {{
  background:var(--surface); border-bottom:1px solid var(--border);
  padding:8px 16px; display:flex; align-items:center; gap:16px;
  flex-shrink:0;
}}
header h1 {{ font-size:16px; color:var(--accent); }}
header .badge {{ font-size:12px; padding:2px 8px; border-radius:10px; }}
.badge-ok {{ background:#0d3320; color:var(--accent); }}
.badge-warn {{ background:#332a0d; color:var(--warn); }}
.badge-info {{ background:#0d1f33; color:#4da6ff; }}

#var-selector {{ display:flex; flex-wrap:wrap; gap:6px; padding:8px 16px; flex-shrink:0; }}
.chip {{
  padding:4px 12px; border-radius:14px; font-size:13px;
  cursor:pointer; border:1px solid var(--border); background:var(--surface);
  color:var(--dim); user-select:none; transition:all .15s;
}}
.chip.active {{ border-color:var(--accent); color:var(--accent); background:#0d3320; }}

/* === main layout: grid with chart + collapsible bottom panel === */
#debug-main {{
  flex:1 1 auto; min-height:0;
  display:grid;
  grid-template-rows: minmax(0, 1fr) auto;
  overflow:hidden;
  background:var(--bg);
}}

#chart-wrap {{
  min-height:0; position:relative; overflow:hidden;
  background:linear-gradient(180deg, rgba(255,255,255,0.02), transparent 36px), var(--surface);
  border-top:1px solid var(--border);
}}
#chart-wrap canvas {{ display:block; width:100%; height:100%; }}

#tooltip {{
  position:absolute; pointer-events:none; display:none;
  background:rgba(0,0,0,0.85); color:var(--text); font-size:12px;
  padding:6px 10px; border-radius:4px; white-space:nowrap;
  border:1px solid var(--border); z-index:10;
}}

/* === bottom panel === */
#raw-log-panel {{
  height:var(--panel-header-h);
  min-height:var(--panel-header-h);
  max-height:min(55vh, 420px);
  display:grid;
  grid-template-rows: var(--panel-header-h) minmax(0, 1fr);
  background:#11182f;
  border-top:1px solid var(--border);
  box-shadow:0 -10px 28px rgba(0,0,0,0.28);
  overflow:hidden;
  transition:height 160ms ease;
}}
#raw-log-panel[data-open="true"] {{ height:var(--raw-log-h); }}

.panel-resizer {{
  display:none; height:6px; margin-top:-3px;
  cursor:ns-resize; background:transparent;
  position:relative; z-index:3;
}}
#raw-log-panel[data-open="true"] .panel-resizer {{
  display:block; grid-row:1;
  position:absolute; left:0; right:0;
}}
.panel-resizer::after {{
  content:""; position:absolute; left:50%; top:2px;
  width:44px; height:2px; transform:translateX(-50%);
  border-radius:999px; background:var(--border);
}}
.panel-resizer:hover::after {{ background:var(--accent); }}

.panel-header {{
  height:var(--panel-header-h);
  display:flex; align-items:center; justify-content:space-between;
  gap:12px; padding:0 10px 0 12px;
  background:#141c36; border-bottom:1px solid var(--border);
  color:var(--text); user-select:none;
}}
.panel-title {{
  display:flex; align-items:center; gap:8px;
  min-width:0; font-size:12px; font-weight:600;
}}
.panel-dot {{
  width:7px; height:7px; border-radius:50%;
  background:var(--accent);
  box-shadow:0 0 10px rgba(0,212,170,0.55);
}}
.panel-actions {{ display:flex; align-items:center; gap:6px; flex:0 0 auto; }}
.panel-count {{ color:var(--dim); font-size:11px; padding-right:6px; }}

.panel-btn {{
  height:24px; min-width:28px; padding:0 8px;
  border:1px solid var(--border); border-radius:4px;
  background:#192344; color:var(--text);
  font:inherit; font-size:11px; line-height:1; cursor:pointer;
}}
.panel-btn:hover {{ border-color:var(--accent); color:var(--accent); }}
.panel-btn-close {{ width:24px; padding:0; font-size:14px; }}

#raw-log {{
  min-height:0; margin:0; padding:10px 12px;
  overflow:auto; background:#0d1328; color:#b8f7ea;
  font-family:Consolas,"Courier New",monospace;
  font-size:11px; line-height:1.45;
  white-space:pre-wrap; overflow-wrap:anywhere;
}}
#raw-log-panel[data-open="false"] #raw-log {{ display:none; }}

footer {{
  background:var(--surface); border-top:1px solid var(--border);
  padding:6px 16px; font-size:12px; color:var(--dim);
  display:flex; gap:20px; flex-shrink:0; flex-wrap:wrap;
}}
.stat {{ display:flex; gap:4px; }}
.stat .label {{ opacity:.7; }}
.stat .value {{ color:var(--text); font-weight:600; }}

#shutdown-overlay {{
  display:none; position:fixed; top:0; left:0; right:0; bottom:0;
  background:rgba(0,0,0,0.85); z-index:9999;
  justify-content:center; align-items:center; flex-direction:column;
  color:var(--text); font-size:18px; text-align:center;
}}
#shutdown-overlay.visible {{ display:flex; }}
#shutdown-overlay h2 {{ color:var(--warn); margin-bottom:8px; }}
#shutdown-overlay p {{ color:var(--dim); font-size:14px; }}
</style>
</head>
<body>
<header>
  <h1>MKLink RTT View</h1>
  <span id="conn-status" class="badge badge-ok">live</span>
  <span id="pts-count" class="badge badge-info">0 pts</span>
</header>
<div id="var-selector"></div>

<main id="debug-main">
  <section id="chart-wrap">
    <canvas id="chart"></canvas>
    <div id="tooltip"></div>
  </section>

  <section id="raw-log-panel" data-open="false">
    <div class="panel-resizer" title="拖拽调整高度"></div>
    <div class="panel-header">
      <div class="panel-title">
        <span class="panel-dot"></span>
        <span>Raw Log</span>
      </div>
      <div class="panel-actions">
        <span id="raw-log-count" class="panel-count">0 行</span>
        <button id="raw-log-clear" class="panel-btn" title="清空日志">清空</button>
        <button id="raw-log-close" class="panel-btn panel-btn-close" title="关闭面板">&#x2715;</button>
      </div>
    </div>
    <pre id="raw-log"></pre>
  </section>
</main>

<footer id="stats-footer"></footer>
<div id="shutdown-overlay">
  <h2>Server Shut Down</h2>
  <p>The visualization server has been stopped.</p>
  <p>You can close this tab.</p>
</div>

<script>
// -- colors --
var COLORS = ['#00d4aa','#4da6ff','#ffd93d','#ff6b6b','#c084fc','#fb923c','#2dd4bf','#f472b6','#a78bfa','#60a5fa'];
var GRID_COLOR = '#2a2a4a';
var TEXT_DIM = '#8888aa';
var MAX_POINTS = {max_points};

// -- state --
var FIELDS = {{}};
var colorIdx = 0;
var paused = false;
var tStart = 0;
var rawLogLineCount = 0;

// -- canvas setup --
var canvas = document.getElementById('chart');
var ctx = canvas.getContext('2d');
var tooltip = document.getElementById('tooltip');
var wrap = document.getElementById('chart-wrap');

function resize() {{
  var r = wrap.getBoundingClientRect();
  var w = r.width || wrap.clientWidth;
  var h = r.height || wrap.clientHeight;
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return false;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(w * dpr));
  canvas.height = Math.max(1, Math.round(h * dpr));
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.setTransform(1,0,0,1,0,0);
  ctx.scale(dpr, dpr);
  return true;
}}
window.addEventListener('resize', resize);
resize();

// Auto-resize when panel toggles
var debugMain = document.getElementById('debug-main');
new ResizeObserver(function() {{ resize(); drawChart(); }}).observe(debugMain);
document.getElementById('raw-log-panel').addEventListener('transitionend', function(e) {{
  if (e.propertyName === 'height') {{ resize(); drawChart(); }}
}});

// -- SSE connection --
var es = new EventSource('/stream');
es.onmessage = function(e) {{
  try {{
    var data = JSON.parse(e.data);
    if (data._event === 'shutdown') {{
      es.close();
      document.getElementById('shutdown-overlay').classList.add('visible');
      document.getElementById('conn-status').textContent = 'stopped';
      document.getElementById('conn-status').className = 'badge badge-warn';
      return;
    }}
    processPoint(data);
  }} catch(_){{}}
}};
es.onerror = function() {{
  if (es.readyState === EventSource.CLOSED) {{
    document.getElementById('shutdown-overlay').classList.add('visible');
  }} else {{
    document.getElementById('conn-status').textContent = 'reconnecting...';
    document.getElementById('conn-status').className = 'badge badge-warn';
  }}
}};
es.onopen = function() {{
  document.getElementById('conn-status').textContent = 'live';
  document.getElementById('conn-status').className = 'badge badge-ok';
}};

// -- bottom panel management --
var rawLogPanel = document.getElementById('raw-log-panel');
var rawLogEl = document.getElementById('raw-log');
var rawLogCountEl = document.getElementById('raw-log-count');
var rawLogOpen = false;

function setRawLogOpen(open) {{
  rawLogOpen = open;
  rawLogPanel.dataset.open = open ? 'true' : 'false';
}}

function toggleRawLog() {{ setRawLogOpen(!rawLogOpen); }}
document.getElementById('raw-log-close').addEventListener('click', function() {{ setRawLogOpen(false); }});
document.getElementById('raw-log-clear').addEventListener('click', function() {{
  rawLogEl.textContent = '';
  rawLogLineCount = 0;
  rawLogCountEl.textContent = '0 行';
}});

// -- drag resize --
var panelResizer = document.querySelector('.panel-resizer');
var resizingRawLog = false;

panelResizer.addEventListener('pointerdown', function(e) {{
  resizingRawLog = true;
  panelResizer.setPointerCapture(e.pointerId);
  document.body.style.cursor = 'ns-resize';
  document.body.style.userSelect = 'none';
}});

panelResizer.addEventListener('pointermove', function(e) {{
  if (!resizingRawLog) return;
  var mainRect = debugMain.getBoundingClientRect();
  var h = Math.round(mainRect.bottom - e.clientY);
  h = Math.max(96, Math.min(h, Math.round(mainRect.height * 0.55)));
  rawLogPanel.style.setProperty('--raw-log-h', h + 'px');
  resize(); drawChart();
}});

panelResizer.addEventListener('pointerup', function(e) {{
  resizingRawLog = false;
  panelResizer.releasePointerCapture(e.pointerId);
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
}});

// -- data processing --
var updatePending = false;
function processPoint(point) {{
  var elapsed = point._t || (performance.now() / 1000);
  if (!tStart) tStart = elapsed;
  var t = elapsed - tStart;

  for (var k in point) {{
    if (!point.hasOwnProperty(k) || k[0] === '_') continue;
    var v = point[k];
    if (!FIELDS[k]) {{
      FIELDS[k] = {{
        color: COLORS[colorIdx % COLORS.length],
        points: [], visible: true,
        min: Infinity, max: -Infinity, sum: 0, count: 0
      }};
      colorIdx++;
    }}
    var m = FIELDS[k];
    v = Number(v);
    if (!Number.isFinite(v)) continue;
    m.points.push({{t: t, y: v}});
    if (m.points.length > MAX_POINTS) {{
      m.points.shift();
      // recompute stats after shift to keep them accurate
      m.min = Infinity; m.max = -Infinity; m.sum = 0; m.count = 0;
      for (var si = 0; si < m.points.length; si++) {{
        var yv = m.points[si].y;
        if (Number.isFinite(yv)) {{
          m.min = Math.min(m.min, yv);
          m.max = Math.max(m.max, yv);
          m.sum += yv; m.count++;
        }}
      }}
    }} else {{
      m.min = Math.min(m.min, v);
      m.max = Math.max(m.max, v);
      m.sum += v; m.count++;
    }}
  }}

  // Append to raw log
  rawLogLineCount++;
  rawLogEl.textContent += JSON.stringify(point) + '\\n';
  rawLogCountEl.textContent = rawLogLineCount + ' 行';
  if (rawLogOpen) rawLogEl.scrollTop = rawLogEl.scrollHeight;

  if (!updatePending) {{
    updatePending = true;
    requestAnimationFrame(function() {{
      try {{
      drawChart();
      updateUI();
      }} catch (err) {{
        console.error("RTT render error:", err);
      }} finally {{
        updatePending = false;
      }}
    }});
  }}
}}

// -- canvas chart drawing --
function drawChart() {{
  if (!resize()) return;
  var W = canvas.clientWidth || parseFloat(canvas.style.width);
  var H = canvas.clientHeight || parseFloat(canvas.style.height);
  if (!Number.isFinite(W) || !Number.isFinite(H) || W <= 0 || H <= 0) return;
  ctx.clearRect(0, 0, W, H);

  var ml = 56, mr = 16, mt = 8, mb = 32;
  var pw = W - ml - mr;
  var ph = H - mt - mb;
  if (pw <= 0 || ph <= 0) return;

  var yMin = Infinity, yMax = -Infinity;
  var hasData = false;
  for (var k in FIELDS) {{
    if (!FIELDS[k].visible) continue;
    var pts = FIELDS[k].points;
    if (pts.length < 2) continue;
    hasData = true;
    yMin = Math.min(yMin, FIELDS[k].min);
    yMax = Math.max(yMax, FIELDS[k].max);
  }}
  if (!hasData) return;
  var pad = (yMax - yMin) * 0.1 || 1;
  yMin -= pad; yMax += pad;

  var tMax = 0;
  for (var k in FIELDS) {{
    var pts = FIELDS[k].points;
    if (pts.length > 0 && pts[pts.length-1].t > tMax) tMax = pts[pts.length-1].t;
  }}
  var tMin = tMax;
  for (var k in FIELDS) {{
    var pts = FIELDS[k].points;
    if (pts.length > 0 && pts[0].t < tMin) tMin = pts[0].t;
  }}
  if (tMax - tMin < 1) tMin = tMax - 1;

  function tx(v) {{ return ml + (v - tMin) / (tMax - tMin || 1) * pw; }}
  function ty(v) {{ return mt + ph - (v - yMin) / (yMax - yMin || 1) * ph; }}

  // Grid
  ctx.strokeStyle = GRID_COLOR;
  ctx.lineWidth = 0.5;
  for (var i = 0; i <= 5; i++) {{
    var yv = yMin + (yMax - yMin) * i / 5;
    var yp = Math.round(ty(yv)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(ml, yp); ctx.lineTo(W - mr, yp);
    ctx.stroke();
    ctx.fillStyle = TEXT_DIM;
    ctx.font = '11px monospace';
    ctx.textAlign = 'right';
    ctx.fillText(yv.toFixed(yMax - yMin < 10 ? 2 : 1), ml - 6, yp + 4);
  }}
  for (var i = 0; i <= 5; i++) {{
    var xv = tMin + (tMax - tMin) * i / 5;
    var xp = Math.round(tx(xv)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(xp, mt); ctx.lineTo(xp, mt + ph);
    ctx.stroke();
    ctx.fillStyle = TEXT_DIM;
    ctx.font = '11px monospace';
    ctx.textAlign = 'center';
    ctx.fillText(xv.toFixed(1) + 's', xp, mt + ph + 16);
  }}

  ctx.fillStyle = TEXT_DIM;
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('time', ml + pw/2, H - 4);
  ctx.save();
  ctx.translate(10, mt + ph/2);
  ctx.rotate(-Math.PI/2);
  ctx.fillText('value', 0, 0);
  ctx.restore();

  ctx.save();
  ctx.beginPath();
  ctx.rect(ml, mt, pw, ph);
  ctx.clip();

  var names = Object.keys(FIELDS).sort();
  for (var ni = 0; ni < names.length; ni++) {{
    var meta = FIELDS[names[ni]];
    if (!meta.visible || meta.points.length < 2) continue;
    ctx.strokeStyle = meta.color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    var started = false;
    for (var i = 0; i < meta.points.length; i++) {{
      var p = meta.points[i];
      var sx = tx(p.t), sy = ty(p.y);
      if (sx < ml - 10 || sx > ml + pw + 10) continue;
      if (!started) {{ ctx.moveTo(sx, sy); started = true; }}
      else ctx.lineTo(sx, sy);
    }}
    ctx.stroke();
  }}
  ctx.restore();

  var lx = ml + 8, ly = mt + 8;
  for (var ni = 0; ni < names.length; ni++) {{
    var name = names[ni];
    var meta = FIELDS[name];
    ctx.fillStyle = meta.color;
    ctx.fillRect(lx, ly, 12, 3);
    ctx.fillStyle = meta.visible ? '#e0e0e0' : TEXT_DIM;
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(name, lx + 16, ly + 6);
    ly += 16;
    if (ly > mt + ph - 10) break;
  }}

  canvas.onmousemove = function(e) {{
    var rect = canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var my = e.clientY - rect.top;
    if (mx < ml || mx > ml + pw || my < mt || my > mt + ph) {{
      tooltip.style.display = 'none'; return;
    }}
    var hoverT = tMin + (mx - ml) / pw * (tMax - tMin);
    var lines = [];
    for (var k in FIELDS) {{
      var pts = FIELDS[k].points;
      if (!FIELDS[k].visible || pts.length < 1) continue;
      var best = null, bestDist = Infinity;
      for (var i = 0; i < pts.length; i++) {{
        var d = Math.abs(pts[i].t - hoverT);
        if (d < bestDist) {{ bestDist = d; best = pts[i]; }}
      }}
      if (best && bestDist < (tMax - tMin) / pw * 15) {{
        lines.push('<span style="color:' + FIELDS[k].color + '">' + k + ': ' + best.y.toFixed(2) + '</span>');
      }}
    }}
    if (lines.length) {{
      tooltip.innerHTML = lines.join('<br>');
      tooltip.style.display = 'block';
      tooltip.style.left = Math.min(mx + 12, W - 180) + 'px';
      tooltip.style.top = Math.max(0, my - 8) + 'px';
    }} else {{ tooltip.style.display = 'none'; }}
  }};
  canvas.onmouseleave = function() {{ tooltip.style.display = 'none'; }};
}}

// -- UI updates --
function updateUI() {{
  var total = 0, count = 0;
  for (var k in FIELDS) {{ total += FIELDS[k].points.length; count++; }}
  document.getElementById('pts-count').textContent = (count ? Math.floor(total/count) : 0) + ' pts';

  var sel = document.getElementById('var-selector');
  var existing = {{}};
  sel.querySelectorAll('.chip').forEach(function(c) {{ existing[c.dataset.name] = c; }});
  var names = Object.keys(FIELDS).sort();
  for (var i = 0; i < names.length; i++) {{
    var name = names[i];
    var meta = FIELDS[name];
    var chip = existing[name];
    if (!chip) {{
      chip = document.createElement('span');
      chip.className = 'chip active';
      chip.dataset.name = name;
      chip.textContent = name;
      chip.onclick = (function(n, el) {{ return function() {{ toggleField(n, el); }}; }})(name, chip);
      sel.appendChild(chip);
      existing[name] = chip;
    }}
    chip.classList.toggle('active', meta.visible);

  var footer = document.getElementById('stats-footer');
  var html = '';
  for (var i = 0; i < names.length; i++) {{
    var name = names[i];
    var meta = FIELDS[name];
    var cur = meta.points.length ? meta.points[meta.points.length-1].y.toFixed(2) : '-';
    var avg = meta.count ? (meta.sum/meta.count).toFixed(2) : '-';
    html += '<div class="stat"><span class="label">' + name + ':</span><span class="value" style="color:' + meta.color + '">cur=' + cur + ' min=' + meta.min.toFixed(2) + ' max=' + meta.max.toFixed(2) + ' avg=' + avg + '</span></div>';
  }}
  footer.innerHTML = html;
}}

}}

function toggleField(name, chipEl) {{
  var meta = FIELDS[name];
  if (!meta) return;
  meta.visible = !meta.visible;
  chipEl.classList.toggle('active', meta.visible);
}}

// -- keyboard shortcuts --
document.addEventListener('keydown', function(e) {{
  if (e.key === ' ' || e.code === 'Space') {{
    e.preventDefault();
    paused = !paused;
    document.getElementById('conn-status').textContent = paused ? 'paused' : 'live';
    document.getElementById('conn-status').className = paused ? 'badge badge-warn' : 'badge badge-ok';
  }}
  if ((e.key === 'l' || e.key === 'L') && !e.ctrlKey && !e.metaKey && !e.altKey) {{
    e.preventDefault();
    toggleRawLog();
  }}
}});
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Convenience runner — used by cli.py
# ---------------------------------------------------------------------------

def run_rtt_visualizer(
    session,         # RTTSession
    bridge,          # MKLinkSerialBridge
    *,
    duration: float = 30.0,
    host: str = "127.0.0.1",
    port: int = 0,
    no_browser: bool = False,
    max_points: int = 500,
    parser_strategy: str = "auto",
    regex_pattern: str | None = None,
    csv_headers: str | None = None,
    channel_name: str | None = None,
) -> None:
    """Run the full visualization pipeline in a background thread.

    This is a fire-and-forget runner — it spawns threads and returns
    control to the caller, which should then wait on KeyboardInterrupt.
    Returns only after all threads are joined (on stop_event signal).

    If *channel_name* matches the ``JScope_<fmt>`` pattern, the viewer
    automatically switches to binary mode and uses
    :class:`~mklink.rtt_binary.JScopeBinaryParser` to decode frames.
    """
    stop_event = threading.Event()
    _stopped = threading.Event()  # idempotent cleanup guard

    # -- detect JScope binary mode --
    _binary_mode = False
    _binary_parser = None
    if channel_name:
        from mklink.rtt_binary import (
            JScopeBinaryParser,
            is_jscope_channel,
            parse_format,
        )
        if is_jscope_channel(channel_name):
            fmt_str = channel_name[len("JScope_"):]
            try:
                fmt_desc = parse_format(fmt_str)
                _binary_parser = JScopeBinaryParser(format_desc=fmt_desc,
                                                    format_str=fmt_str)
                _binary_mode = True
                print(f"[AUTO] JScope binary mode detected: {channel_name}")
            except ValueError:
                print(f"[WARN] JScope channel name detected but format parse failed: {channel_name}")

    # -- build parser --
    if _binary_mode:
        # Binary parser already created above; line parser not needed.
        parser = None
        _auto_detect_done = True
    elif parser_strategy == "auto":
        parser = RttLineParser("kv")
        _auto_detect_done = False
    else:
        headers_list = (
            [h.strip() for h in csv_headers.split(",")] if csv_headers else None
        )
        parser = RttLineParser(
            strategy=parser_strategy,
            regex_pattern=regex_pattern,
            csv_headers=headers_list,
        )
        _auto_detect_done = True

    # -- start HTTP server --
    server = VisualizationServer(host=host, port=port, max_points=max_points,
                                 title="MKLink RTT View", mode="RTT")
    actual_port = server.start()

    url = f"http://{host}:{actual_port}"
    print(f"[OK] RTT View 已启动: {url}")
    if not no_browser:
        print(f"[*] 正在打开浏览器...")
        webbrowser.open(url)

    # -- parser thread --
    parsed_count = 0
    dropped_count = 0

    def _parser_loop():
        nonlocal parsed_count, dropped_count
        import math

        while not stop_event.is_set():
            data = session.read_output(0.1)
            if not data:
                continue

            # --- Binary (JScope) mode ---
            if _binary_mode and _binary_parser is not None:
                raw = data.encode("latin-1") if isinstance(data, str) else data
                if not raw:
                    continue
                frames = _binary_parser.feed(raw)
                if not server.collecting.is_set():
                    continue  # keep parser synced but discard
                for frame in frames:
                    frame["_t"] = time.time()
                    server.push_data_point(frame)
                    parsed_count += 1
                dropped_count += _binary_parser.dropped_frames
                continue

            # --- Text mode (existing logic) ---
            if not server.collecting.is_set():
                continue  # keep reading to drain MCU buffer but discard

            for line in data.split("\n"):
                line = line.strip()
                if not line:
                    continue

                # Auto-detect on the fly if needed
                nonlocal _auto_detect_done
                if parser_strategy == "auto" and not _auto_detect_done:
                    if RttLineParser.auto_detect([line]).strategy != parser.strategy:
                        new_p = RttLineParser.auto_detect([line])
                        if new_p.strategy != parser.strategy:
                            parser.strategy = new_p.strategy
                            parser.regex_pattern = new_p.regex_pattern
                            parser.csv_headers = new_p.csv_headers
                            parser.delimiter = new_p.delimiter
                            print(f"[AUTO] 检测到解析器: {parser.strategy}")
                    # Give auto-detect 10 lines before locking in
                    if parsed_count + dropped_count > 10:
                        _auto_detect_done = True

                parsed = parser.parse(line)
                if parsed:
                    # Add elapsed time
                    parsed["_t"] = time.time()
                    server.push_data_point(parsed)
                    parsed_count += 1
                else:
                    dropped_count += 1

    parser_thread = threading.Thread(target=_parser_loop, daemon=True)
    parser_thread.start()

    # Wire up web-control callback
    server._on_stop_requested = lambda: stop_event.set()

    # -- idempotent cleanup --
    def _cleanup():
        if _stopped.is_set():
            return
        _stopped.set()
        stop_event.set()
        if parser_thread and parser_thread.is_alive():
            parser_thread.join(timeout=2.0)
        server.stop()
        session.stop()
        bridge.close()

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

    # -- wait for stop signal --
    print(f"[*] RTT View 运行中，按 Ctrl+C 停止...\n")
    try:
        start_time = time.time()
        while time.time() - start_time < duration:
            if stop_event.is_set():
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] 用户中断")

    # -- orderly shutdown --
    print("[*] 正在停止...")
    _cleanup()
    print(f"[OK] RTT View 已关闭 (解析 {parsed_count} 行，丢弃 {dropped_count} 行)")


# ---------------------------------------------------------------------------
# RTT RAW Terminal Viewer
# ---------------------------------------------------------------------------

def run_rtt_raw_viewer(
    session,
    bridge,
    host: str = "127.0.0.1",
    port: int = 0,
    no_browser: bool = False,
    duration: float = 30.0,
    max_lines: int = 5000,
) -> None:
    """启动 RTT RAW Web 终端，显示原始 RTT 打印信息。"""
    from pathlib import Path

    static_dir = Path(__file__).parent / "static"
    template_path = static_dir / "rtt_raw_template.html"
    if not template_path.exists():
        print(f"[FAIL] RAW 终端模板不存在: {template_path}")
        return

    html_content = template_path.read_text(encoding="utf-8")
    html_content = html_content.replace("__MAX_LINES__", str(max_lines))

    clients: list[queue.Queue] = []
    clients_lock = threading.Lock()

    class RawHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_content.encode("utf-8"))
            elif self.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                q: queue.Queue = queue.Queue(maxsize=1000)
                with clients_lock:
                    clients.append(q)
                try:
                    while True:
                        try:
                            data = q.get(timeout=30)
                            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with clients_lock:
                        if q in clients:
                            clients.remove(q)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer((host, port), RawHandler)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"
    print(f"[OK] RTT RAW 终端: {url}")

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    if not no_browser:
        webbrowser.open(url)

    stop_event = threading.Event()

    def broadcast(text: str):
        payload = json.dumps({"type": "text", "data": text})
        with clients_lock:
            dead = []
            for cq in clients:
                try:
                    cq.put_nowait(payload)
                except queue.Full:
                    dead.append(cq)
            for dq in dead:
                clients.remove(dq)

    def rtt_reader():
        while not stop_event.is_set():
            try:
                data = session.read_output(0.1)
                if data:
                    for line in data.splitlines():
                        if line.strip():
                            print(line, flush=True)
                            broadcast(line)
            except Exception:
                break

    reader_thread = threading.Thread(target=rtt_reader, daemon=True)
    reader_thread.start()

    try:
        start = time.time()
        while time.time() - start < duration:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] 用户中断")
    finally:
        stop_event.set()
        reader_thread.join(timeout=2)
        session.stop()
        bridge.close()
        server.shutdown()
        print("[OK] RTT RAW 终端已关闭")
