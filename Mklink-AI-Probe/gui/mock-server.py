#!/usr/bin/env python3
"""Mock MKLink backend for GUI web demo (no hardware).

Serves canned JSON for the /api/* endpoints the Vue frontend calls, so the GUI
can be shown in a browser via `npm run dev` (Vite proxies /api and /ws here on
127.0.0.1:8765). Dependency-free: stdlib only.
"""
import json
import re
import hashlib
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8765
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ---- canned data ----------------------------------------------------------
PORTS = [
    {"device": "COM5", "description": "MKLink CMSIS-DAP Debugger", "manufacturer": "MicroLink", "vid": 1234, "pid": 5678},
    {"device": "COM3", "description": "STLink Virtual COM Port", "manufacturer": "STMicroelectronics", "vid": 1155, "pid": 22336},
    {"device": "COM7", "description": "USB-RS485 Adapter", "manufacturer": "FTDI", "vid": 1027, "pid": 24577},
    {"device": "COM11", "description": "J-Link CDC UART", "manufacturer": "SEGGER", "vid": 1366, "pid": 1015},
]
PROFILES = [
    {"key": "stm32f407vg", "name": "STM32F407VGT6", "vendor": "ST", "core": "Cortex-M4F", "flash_base": "0x08000000", "flash_size": 1048576, "ram_base": "0x20000000", "ram_size": 192 * 1024},
    {"key": "stm32f103c8", "name": "STM32F103C8T6", "vendor": "ST", "core": "Cortex-M3", "flash_base": "0x08000000", "flash_size": 64 * 1024, "ram_base": "0x20000000", "ram_size": 20 * 1024},
    {"key": "gd32f407vk", "name": "GD32F407VKT6", "vendor": "GigaDevice", "core": "Cortex-M4F", "flash_base": "0x08000000", "flash_size": 3 * 1024 * 1024, "ram_base": "0x20000000", "ram_size": 192 * 1024},
    {"key": "hpm5301", "name": "HPM5301EVKLite", "vendor": "HPMicro", "core": "RV32-Andes", "flash_base": "0x80000000", "flash_size": 1024 * 1024, "ram_base": "0x00000000", "ram_size": 32 * 1024},
    {"key": "hc32f460", "name": "HC32F460PETB", "vendor": "HDSC", "core": "Cortex-M4", "flash_base": "0x00000000", "flash_size": 512 * 1024, "ram_base": "0x1FFF8000", "ram_size": 96 * 1024},
]
DEVICE_STATUS = {
    "connected": True,
    "state": "running",
    "mcu": "STM32F407VGT6",
    "idcode": "0x4BA00477",
    "port": "COM5",
    "axf": {
        "loaded": True,
        "axf_path": "D:\\proj\\eternal-chip\\build\\eternal.axf",
        "elf_backend": "builtin",
        "elf_available": True,
        "builtin_elf_available": True,
        "builtin_elf_version": "1.4.0",
        "external_elf_available": False,
        "variable_count": 1284,
        "struct_count": 96,
        "enum_count": 24,
    },
}
CONFIG = {"com_port": "COM5", "mcu_key": "stm32f407vg", "swd_clock": "4000"}
CONFIG_STATUS = {
    "is_valid": True, "has_config": True, "has_project": True, "has_rtt_config": True,
    "errors": [], "warnings": [], "flm_on_microkeen": True,
}
PROJECT = {
    "hex_path": "build/eternal.hex", "map_path": "build/eternal.map",
    "flm_path": "MK-Firmware/flm/STM32F4xx_1024.FLM", "flm_name": "STM32F4xx_1024.FLM",
    "flash_base": "0x08000000", "axf_path": "build/eternal.axf",
}
RTT_CONFIG = {"rtt_addr": "0x20000000", "rtt_storage_mode": 0, "search_size": 1024, "channel": 0, "autostart": False, "integrated": True}
MICROKEEN = {"disk_path": "E:\\", "flm_dir": "D:\\proj\\eternal-chip\\MK-Firmware\\flm", "available": True}
PROJECT_ROOT = {"project_root": "D:\\proj\\eternal-chip"}
PROJECT_HISTORY = {
    "last_project": "D:\\proj\\eternal-chip",
    "history": [
        {"path": "D:\\proj\\eternal-chip", "name": "eternal-chip", "last_used": "2026-08-12 18:04"},
        {"path": "D:\\proj\\genset-gcu", "name": "genset-gcu", "last_used": "2026-08-10 09:30"},
        {"path": "D:\\proj\\bms-master", "name": "bms-master", "last_used": "2026-08-05 14:12"},
    ],
}
PROBE_FW = {
    "status": "ok", "current_version": "V4.3.3", "min_required_version": "V4.3.0",
    "recommended_uf2": {"name": "MKLink-V4", "version": "V4.3.3", "model": "V4", "path": "firmware/V4.3.3.uf2"},
    "all_uf2s": [
        {"name": "MKLink-V4", "version": "V4.3.3", "model": "V4", "path": "firmware/V4.3.3.uf2"},
        {"name": "MKLink-V3", "version": "V3.7.1", "model": "V3", "path": "firmware/V3.7.1.uf2"},
    ],
    "firmware_dir": "firmware",
    "instructions": "Probe firmware is up to date.",
}
SYMBOL_STATUS = {
    "loaded": True, "generation": 1, "axf_path": "build/eternal.axf",
    "parsed_at": 1723478400, "fingerprint": {"sha256": "a1b2c3d4e5", "size": 318294, "mtime": 1723478400},
    "stale": False, "total": 1284, "container_count": 96, "truncated_roots": [],
}
DASH_TYPES = ["rtt", "serial", "modbus", "superwatch", "systemview", "vofa"]
DASH_STATUS = {t: {"running": False, "url": None} for t in DASH_TYPES}
RTT_HISTORY = {
    "lines": [
        "\033[0;32m[boot] Eternal Chip FW v2.1.0 (" + __import__("platform").machine() + ")\033[0m",
        "[init] RT-Thread RTOS starting... \033[0;32mOK\033[0m",
        "[init] Mount SPI flash  \033[0;32mOK\033[0m  (8 MB)",
        "[net] LWIP 2.1.2 up, IP 192.168.1.50",
        "[sens] BMP390 calibrated, P=1013.2 hPa, T=24.6 C",
        "[main] loop @ 1kHz  \033[0;33mcpu 23%\033[0m",
        "[dbg] g_adc.avg = 0x1F3E  gain=1.45",
    ]
}
SYMBOL_SEARCH = [
    {"name": "g_adc", "address": "0x20000AF0", "type": "adc_sample_t", "size": 16},
    {"name": "g_battery_pack", "address": "0x20000B10", "type": "battery_pack_t", "size": 48},
    {"name": "g_sys_tick", "address": "0x20000004", "type": "volatile uint32_t", "size": 4},
    {"name": "fault_flags", "address": "0x20000010", "type": "uint32_t", "size": 4},
    {"name": "g_rtt_buffer", "address": "0x20001000", "type": "char[1024]", "size": 1024},
]
CORE_REGS = {"r0": "0x00000000", "r1": "0x20000AF0", "r2": "0x00000003", "r3": "0x00000000",
             "r12": "0x00000000", "lr": "0x080018A2", "pc": "0x08001234", "xpsr": "0x21000000",
             "msp": "0x2000FFF0", "psp": "0x20009FC0"}
MEMORY_MAP = [
    {"name": "FLASH", "start": "0x08000000", "size": 1048576, "type": "rom"},
    {"name": "RAM", "start": "0x20000000", "size": 192 * 1024, "type": "ram"},
    {"name": "CCM", "start": "0x10000000", "size": 64 * 1024, "type": "ram"},
]
SUPERWATCH_ITEMS = [
    {"path": "g_battery_pack.voltage", "address": "0x20000B10", "type_name": "float", "scalar_kind": "float", "size": 4, "writable": False, "value": 12.42},
    {"path": "g_battery_pack.current", "address": "0x20000B14", "type_name": "float", "scalar_kind": "float", "size": 4, "writable": False, "value": -1.83},
    {"path": "g_adc.temp", "address": "0x20000AF8", "type_name": "float", "scalar_kind": "float", "size": 4, "writable": False, "value": 24.6},
    {"path": "fault_flags", "address": "0x20000010", "type_name": "uint32_t", "scalar_kind": "unsigned", "size": 4, "writable": True, "value": 0},
]
ONLINE_FLASH_ROOT = {"enabled": True, "version": "1.0", "cache_dir": ".mklink/online-flash"}
ONLINE_TARGETS = []
ONLINE_JOBS_ACTIVE = None
HARDFAULT = {"fault": False}

# ---- routing --------------------------------------------------------------
GET_ROUTES = {
    "/api/ports": PORTS,
    "/api/ports/discover": {"port": "COM5"},
    "/api/profiles": PROFILES,
    "/api/config": CONFIG,
    "/api/config/status": CONFIG_STATUS,
    "/api/project": PROJECT,
    "/api/rtt-config": RTT_CONFIG,
    "/api/microkeen": MICROKEEN,
    "/api/project-root": PROJECT_ROOT,
    "/api/project-history": PROJECT_HISTORY,
    "/api/probe/firmware-check": PROBE_FW,
    "/api/device/status": DEVICE_STATUS,
    "/api/device/core-registers": CORE_REGS,
    "/api/device/memory-map": MEMORY_MAP,
    "/api/device/hardfault-detail": HARDFAULT,
    "/api/device/hardfault": HARDFAULT,
    "/api/symbols/status": SYMBOL_STATUS,
    "/api/symbols/search": SYMBOL_SEARCH,
    "/api/online-flash": ONLINE_FLASH_ROOT,
    "/api/online-flash/targets": ONLINE_TARGETS,
    "/api/online-flash/jobs/active": ONLINE_JOBS_ACTIVE,
    "/api/lang": {"lang": "zh"},
}

ARRAY_HINTS = ("search", "items", "jobs", "targets", "history", "logs", "profiles", "ports", "map")


def dash_status_for(path: str):
    m = re.match(r"^/api/dash/([a-z]+)/status$", path)
    return DASH_STATUS if m else None


def dash_history_for(path: str):
    m = re.match(r"^/api/dash/([a-z]+)/history$", path)
    if not m:
        return None
    return RTT_HISTORY if m.group(1) == "rtt" else []


def superwatch_items(path: str):
    if path == "/api/dash/superwatch/items":
        return SUPERWATCH_ITEMS
    return None


def resolve_get(path: str):
    for fn in (dash_status_for, dash_history_for, superwatch_items):
        v = fn(path)
        if v is not None:
            return v
    if path in GET_ROUTES:
        return GET_ROUTES[path]
    # smart catch-all: never 404 an /api GET
    if any(h in path for h in ARRAY_HINTS):
        return []
    return {}


def resolve_mutation(path: str, method: str, body):
    """POST/PUT/DELETE: return a sensible success payload."""
    if path == "/api/device/connect":
        return DEVICE_STATUS
    if path == "/api/device/read-memory":
        return {"data_hex": "DEADBEEF"}
    if path == "/api/device/read-variable":
        return {"value": 0}
    if path == "/api/device/read-register":
        return {"value": "0x00000000"}
    if path == "/api/dash/superwatch/items":
        return SUPERWATCH_ITEMS
    if path == "/api/project-history":
        return PROJECT_HISTORY
    if path == "/api/project-root":
        return PROJECT_ROOT
    if path == "/api/config":
        return body if isinstance(body, dict) else CONFIG
    if path == "/api/rtt-config":
        return body if isinstance(body, dict) else RTT_CONFIG
    if path == "/api/dash/rtt/write":
        return {"sent_bytes": len(body.get("data_hex", "")) // 2} if isinstance(body, dict) else {"sent_bytes": 0}
    if path == "/api/online-flash/jobs":
        return {"job_id": "job/1", "status": "queued"}
    return {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quieter
        pass

    def _send(self, code: int, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # WebSocket upgrade for the browser-session keepalive: accept then close
        # with code 1008 so the client stops retrying (no real device to lease).
        if (self.headers.get("Upgrade", "").lower() == "websocket"
                and self.path.split("?", 1)[0].startswith("/ws/")):
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode()).digest()
            ).decode()
            self.wfile.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
            )
            self.wfile.write(b"\x88\x02\x03\xf0")  # close frame, status 1008
            try:
                self.connection.shutdown(__import__("socket").SHUT_RDWR)
            except OSError:
                pass
            return
        path = self.path.split("?", 1)[0]
        self._send(200, resolve_get(path))

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        self._send(200, resolve_mutation(path, "PUT", body))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        self._send(200, resolve_mutation(path, "POST", body))

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/project-history"):
            self._send(200, PROJECT_HISTORY)
        else:
            self._send(200, {})

    def do_OPTIONS(self):
        self._send(204, {})

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        ct = self.headers.get("Content-Type", "")
        if "application/json" in ct or ct == "":
            try:
                return json.loads(raw.decode() or "{}")
            except Exception:
                return {}
        return {}

    # WebSocket upgrade attempts -> decline gracefully (frontend degrades)
    def do_HEAD(self):
        self._send(200, {})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[mock] MKLink mock backend on http://{HOST}:{PORT} (serving /api/* with demo data)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
