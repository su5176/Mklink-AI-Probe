"""MKLink Remote Client — transparent remote Device proxy.

Connects to a remote MKLink server via WebSocket and presents the
same Device API as a local connection.

Usage::

    import mklink

    dev = mklink.connect_remote("ws://lab.example.com:8765", token="secret")
    dev.flash("firmware.hex")
    val = dev.read_variable("sensor_count")
    dev.close()
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import struct
from typing import Any

logger = logging.getLogger(__name__)


class RemoteDeviceError(Exception):
    pass


class RemoteDevice:
    """Proxy that mirrors the Device API over WebSocket RPC."""

    def __init__(self, url: str, *, token: str | None = None):
        self._url = url
        self._token = token
        self._sock: socket.socket | None = None
        self._req_id = 0
        self._connected = False
        self._connect()

    def __enter__(self) -> RemoteDevice:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- connection --

    def _connect(self) -> None:
        from urllib.parse import urlparse
        parsed = urlparse(self._url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 8765

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((host, port))

        # WebSocket handshake
        import hashlib
        import os
        ws_key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(handshake.encode())
        self._sock.recv(4096)  # read upgrade response
        self._connected = True

    def close(self) -> None:
        if self._sock:
            try:
                self._ws_send(b"", opcode=8)
            except Exception:
                pass
            self._sock.close()
        self._sock = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self._sock is not None

    # -- WebSocket framing --

    def _ws_send(self, payload: bytes, opcode: int = 1) -> None:
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
        self._sock.sendall(bytes(frame))

    def _ws_recv(self) -> bytes:
        header = self._sock.recv(2)
        if not header or len(header) < 2:
            raise RemoteDeviceError("Connection closed")
        opcode = header[0] & 0x0F
        masked = (header[1] & 0x80) != 0
        length = header[1] & 0x7F
        offset = 0
        if length == 126:
            raw = self._sock.recv(2)
            length = struct.unpack(">H", raw)[0]
        elif length == 127:
            raw = self._sock.recv(8)
            length = struct.unpack(">Q", raw)[0]
        mask_key = b""
        if masked:
            mask_key = self._sock.recv(4)
        payload = b""
        while len(payload) < length:
            chunk = self._sock.recv(length - len(payload))
            payload += chunk
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return payload

    # -- RPC --

    def _call(self, method: str, **params) -> Any:
        self._req_id += 1
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._req_id,
        }
        if self._token:
            msg["token"] = self._token

        self._ws_send(json.dumps(msg).encode())
        raw = self._ws_recv()
        resp = json.loads(raw.decode("utf-8"))

        if "error" in resp:
            raise RemoteDeviceError(
                f"RPC error [{resp['error']['code']}]: {resp['error']['message']}"
            )
        return resp.get("result")

    # -- Device API mirror --

    @property
    def idcode(self) -> int:
        return self._call("idcode")

    @property
    def mcu_name(self) -> str:
        return self._call("mcu_name")

    @property
    def port(self) -> str | None:
        return f"remote:{self._url}"

    def flash(self, firmware: str, **kw) -> dict:
        return self._call("flash", firmware=firmware, **kw)

    def erase_chip(self) -> bool:
        return self._call("erase_chip")

    def reset(self) -> None:
        self._call("reset")

    def rtt_start(self, addr: str | None = None, **kw) -> dict:
        return self._call("rtt_start", addr=addr, **kw)

    def rtt_read(self, duration: float = 10.0) -> str:
        return self._call("rtt_read", duration=duration)

    def rtt_write(self, data: bytes | str) -> bool:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        return self._call("rtt_write", data=data)

    def rtt_stop(self) -> str:
        return self._call("rtt_stop")

    def wait_for_rtt(self, pattern: str | None = None, *, timeout: float = 10.0) -> str:
        import re
        deadline = __import__("time").time() + timeout
        collected = ""
        remaining = timeout
        while remaining > 0:
            chunk = self.rtt_read(min(remaining, 2.0))
            if chunk:
                collected += chunk
                if pattern and pattern in collected:
                    return collected
                if pattern and re.compile(pattern).search(collected):
                    return collected
            remaining = deadline - __import__("time").time()
        return collected

    def read_memory(self, address: int, size: int) -> bytes:
        result = self._call("read_memory", address=address, size=size)
        if isinstance(result, dict) and "__bytes__" in result:
            return base64.b64decode(result["__bytes__"])
        return b""

    def write_memory(self, address: int, data: bytes) -> None:
        self._call("write_memory", address=address, data_b64=base64.b64encode(data).decode())

    def read_variable(self, name: str) -> Any:
        return self._call("read_variable", name=name)

    def write_variable(self, name: str, value: int) -> None:
        self._call("write_variable", name=name, value=value)

    def read_register(self, name: str) -> int:
        return self._call("read_register", name=name)

    def halt(self) -> dict:
        return self._call("halt")

    def resume(self) -> dict:
        return self._call("resume")

    def step(self) -> dict:
        return self._call("step")

    def set_breakpoint(self, address: int, slot: int | None = None) -> int:
        return self._call("set_breakpoint", address=address, slot=slot)

    def clear_breakpoint(self, slot: int) -> None:
        self._call("clear_breakpoint", slot=slot)

    def read_core_registers(self) -> dict:
        return self._call("read_core_registers")

    def check_hardfault(self) -> dict | None:
        return self._call("check_hardfault")

    def decode_hardfault(self, fault_regs: dict | None = None) -> Any:
        return self._call("decode_hardfault", fault_regs=fault_regs)


def connect_remote(url: str, *, token: str | None = None) -> RemoteDevice:
    """Connect to a remote MKLink server.

    Returns a RemoteDevice with the same API as a local Device.

    Args:
        url: WebSocket URL, e.g. ``ws://lab.example.com:8765``
        token: Authentication token (must match server's ``auth_token``).
    """
    return RemoteDevice(url, token=token)
