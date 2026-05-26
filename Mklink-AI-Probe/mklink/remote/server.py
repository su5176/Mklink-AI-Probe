"""MKLink Remote Server — WebSocket + HTTP server for remote debugging.

Exposes a local MKLink device over the network so remote clients can
flash firmware, read variables, capture RTT output, etc.

Architecture::

    Browser ──► HTTP/SSE Dashboard
                       │
    Python SDK ──► WebSocket RPC ──► Device Manager ──► MKLink Probe ──► Target MCU

Usage (CLI)::

    mklink serve --port 8765 --token my-secret

Usage (Python)::

    mklink.serve(host="0.0.0.0", port=8765, auth_token="my-secret")
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable

from mklink._types import DeviceState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RPC protocol helpers
# ---------------------------------------------------------------------------

class RemoteError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def make_response(result: Any, req_id: int | None = None) -> str:
    return json.dumps({"jsonrpc": "2.0", "result": result, "id": req_id})


def make_error(code: int, message: str, req_id: int | None = None) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": req_id,
    })


# ---------------------------------------------------------------------------
# RPC method dispatcher
# ---------------------------------------------------------------------------

class DeviceDispatcher:
    """Bridges JSON-RPC calls to a local Device instance."""

    def __init__(self, device):
        self._device = device
        self._handlers: dict[str, Callable] = {
            "idcode": self._idcode,
            "mcu_name": self._mcu_name,
            "flash": self._flash,
            "erase_chip": self._erase_chip,
            "reset": self._reset,
            "rtt_start": self._rtt_start,
            "rtt_read": self._rtt_read,
            "rtt_write": self._rtt_write,
            "rtt_stop": self._rtt_stop,
            "read_memory": self._read_memory,
            "write_memory": self._write_memory,
            "read_variable": self._read_variable,
            "write_variable": self._write_variable,
            "read_register": self._read_register,
            "halt": self._halt,
            "resume": self._resume,
            "step": self._step,
            "set_breakpoint": self._set_breakpoint,
            "clear_breakpoint": self._clear_breakpoint,
            "read_core_registers": self._read_core_registers,
            "check_hardfault": self._check_hardfault,
            "decode_hardfault": self._decode_hardfault,
        }

    def dispatch(self, method: str, params: dict, req_id: int | None = None) -> str:
        handler = self._handlers.get(method)
        if not handler:
            return make_error(-32601, f"Method not found: {method}", req_id)
        try:
            result = handler(**params)
            if isinstance(result, bytes):
                import base64
                result = {"__bytes__": base64.b64encode(result).decode()}
            return make_response(result, req_id)
        except Exception as e:
            logger.exception("RPC error in %s", method)
            return make_error(-32603, str(e), req_id)

    # -- individual method implementations --

    def _idcode(self) -> int:
        return self._device.idcode

    def _mcu_name(self) -> str:
        return self._device.mcu_name

    def _flash(self, firmware: str, **kw) -> dict:
        return self._device.flash(firmware, **kw)

    def _erase_chip(self) -> bool:
        return self._device.erase_chip()

    def _reset(self) -> None:
        self._device.reset()

    def _rtt_start(self, addr: str | None = None, **kw) -> dict:
        return self._device.rtt_start(addr, **kw)

    def _rtt_read(self, duration: float = 10.0) -> str:
        return self._device.rtt_read(duration)

    def _rtt_write(self, data: str) -> bool:
        return self._device.rtt_write(data)

    def _rtt_stop(self) -> str:
        return self._device.rtt_stop()

    def _read_memory(self, address, size: int) -> bytes:
        return self._device.read_memory(int(address, 0) if isinstance(address, str) else address, size)

    def _write_memory(self, address, data_b64: str) -> None:
        import base64
        addr = int(address, 0) if isinstance(address, str) else address
        data = base64.b64decode(data_b64)
        self._device.write_memory(addr, data)

    def _read_variable(self, name: str) -> Any:
        return self._device.read_variable(name)

    def _write_variable(self, name: str, value: int) -> None:
        self._device.write_variable(name, value)

    def _read_register(self, name: str) -> int:
        return self._device.read_register(name)

    def _halt(self) -> dict:
        s = self._device.halt()
        return {"halted": s.halted}

    def _resume(self) -> dict:
        s = self._device.resume()
        return {"halted": s.halted}

    def _step(self) -> dict:
        s = self._device.step()
        return {"halted": s.halted}

    def _set_breakpoint(self, address: int, slot: int | None = None) -> int:
        return self._device.set_breakpoint(address, slot)

    def _clear_breakpoint(self, slot: int) -> None:
        self._device.clear_breakpoint(slot)

    def _read_core_registers(self) -> dict:
        return self._device.read_core_registers()

    def _check_hardfault(self) -> dict | None:
        return self._device.check_hardfault()

    def _decode_hardfault(self, fault_regs: dict | None = None) -> dict | None:
        report = self._device.decode_hardfault(fault_regs)
        if report is None:
            return None
        return {
            "cfsr": report.cfsr,
            "hfsr": report.hfsr,
            "cfsr_flags": report.cfsr_flags,
            "hfsr_flags": report.hfsr_flags,
            "summary": report.summary,
            "stack_frame": report.stack_frame,
            "source_locations": report.source_locations,
        }


# ---------------------------------------------------------------------------
# Minimal HTTP + WebSocket server
# ---------------------------------------------------------------------------

def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str | None = None,
    device_port: str | None = None,
    axf: str | None = None,
    dashboard: bool = True,
):
    """Start the MKLink remote debugging server.

    Args:
        host: Bind address.
        port: Bind port.
        auth_token: Required token for client authentication.
        device_port: MKLink COM port (auto-detect if None).
        axf: AXF/ELF file for symbol resolution.
        dashboard: Enable web dashboard.
    """
    import mklink

    logger.info("Connecting to MKLink device...")
    device = mklink.connect(port=device_port, axf=axf)
    dispatcher = DeviceDispatcher(device)
    logger.info("Device connected: MCU=%s IDCODE=0x%08X", device.mcu_name, device.idcode)

    # --- Simple WebSocket-like server using threading + sockets ---
    import socket
    import selectors
    import base64
    import hashlib
    import struct

    sel = selectors.DefaultSelector()
    connections: dict[socket.socket, str] = {}  # socket -> state ('init'|'ws')

    def _ws_accept_key(ws_key: str) -> str:
        GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        sha1 = hashlib.sha1((ws_key + GUID).encode()).digest()
        return base64.b64encode(sha1).decode()

    def _ws_frame_decode(data: bytes) -> tuple[bool, int, bytes]:
        """Decode a single WebSocket frame. Returns (fin, opcode, payload)."""
        if len(data) < 2:
            return False, 0, b""
        opcode = data[0] & 0x0F
        masked = (data[1] & 0x80) != 0
        length = data[1] & 0x7F
        offset = 2
        if length == 126:
            length = struct.unpack(">H", data[2:4])[0]
            offset = 4
        elif length == 127:
            length = struct.unpack(">Q", data[2:10])[0]
            offset = 10
        mask_key = b""
        if masked:
            mask_key = data[offset:offset + 4]
            offset += 4
        payload = data[offset:offset + length]
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return True, opcode, payload

    def _ws_frame_encode(payload: bytes, opcode: int = 1) -> bytes:
        """Encode a WebSocket text/binary frame."""
        frame = bytearray()
        frame.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            frame.append(length)
        elif length < 65536:
            frame.append(126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(payload)
        return bytes(frame)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((host, port))
    server_sock.listen(5)
    server_sock.setblocking(False)
    sel.register(server_sock, selectors.EVENT_READ, "accept")
    logger.info("MKLink remote server listening on %s:%d", host, port)

    buffers: dict[socket.socket, bytearray] = {}

    try:
        while True:
            events = sel.select(timeout=1.0)
            for key, mask in events:
                if key.data == "accept":
                    conn, addr = server_sock.accept()
                    conn.setblocking(False)
                    connections[conn] = "init"
                    buffers[conn] = bytearray()
                    sel.register(conn, selectors.EVENT_READ, "client")
                    logger.info("Connection from %s", addr)
                elif key.data == "client":
                    sock = key.fileobj
                    try:
                        data = sock.recv(65536)
                    except Exception:
                        data = b""
                    if not data:
                        sel.unregister(sock)
                        sock.close()
                        connections.pop(sock, None)
                        buffers.pop(sock, None)
                        continue

                    if connections.get(sock) == "init":
                        # HTTP upgrade or plain HTTP
                        text = data.decode("utf-8", errors="replace")
                        if "Upgrade: websocket" in text:
                            # WebSocket handshake
                            ws_key = ""
                            for line in text.split("\r\n"):
                                if line.lower().startswith("sec-websocket-key:"):
                                    ws_key = line.split(":", 1)[1].strip()
                            accept = _ws_accept_key(ws_key)
                            response = (
                                "HTTP/1.1 101 Switching Protocols\r\n"
                                "Upgrade: websocket\r\n"
                                "Connection: Upgrade\r\n"
                                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                            )
                            sock.sendall(response.encode())
                            connections[sock] = "ws"
                            logger.info("WebSocket upgraded from %s", sock.getpeername())
                        else:
                            # Simple HTTP — serve status page
                            body = json.dumps({
                                "status": "running",
                                "mcu": device.mcu_name,
                                "idcode": hex(device.idcode),
                                "connected": device.connected,
                            })
                            resp = (
                                "HTTP/1.1 200 OK\r\n"
                                "Content-Type: application/json\r\n"
                                f"Content-Length: {len(body)}\r\n\r\n"
                                f"{body}"
                            )
                            sock.sendall(resp.encode())
                            sel.unregister(sock)
                            sock.close()
                            connections.pop(sock, None)
                            buffers.pop(sock, None)
                    elif connections.get(sock) == "ws":
                        buffers[sock].extend(data)
                        try:
                            fin, opcode, payload = _ws_frame_decode(bytes(buffers[sock]))
                        except Exception:
                            continue

                        if opcode == 8:  # close
                            sock.sendall(_ws_frame_encode(b"", opcode=8))
                            sel.unregister(sock)
                            sock.close()
                            connections.pop(sock, None)
                            buffers.pop(sock, None)
                            continue

                        if opcode == 9:  # ping
                            sock.sendall(_ws_frame_encode(payload, opcode=10))
                            buffers[sock].clear()
                            continue

                        if opcode == 1 and fin:  # text
                            buffers[sock].clear()
                            try:
                                msg = json.loads(payload.decode("utf-8"))
                                method = msg.get("method", "")
                                params = msg.get("params", {})
                                req_id = msg.get("id")

                                # Auth check
                                if auth_token:
                                    token = msg.get("token") or params.get("token")
                                    if token != auth_token:
                                        resp = make_error(-32001, "Unauthorized", req_id)
                                        sock.sendall(_ws_frame_encode(resp.encode()))
                                        continue

                                result_json = dispatcher.dispatch(method, params, req_id)
                                sock.sendall(_ws_frame_encode(result_json.encode()))
                            except Exception as e:
                                err = make_error(-32700, f"Parse error: {e}")
                                sock.sendall(_ws_frame_encode(err.encode()))
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    finally:
        device.close()
        server_sock.close()
        sel.close()
