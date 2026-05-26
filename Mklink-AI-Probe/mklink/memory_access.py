"""Shared target memory access helpers for MKLink debug commands."""

from __future__ import annotations

import re


_HEX_DUMP_HEADER_RE = re.compile(
    r"^\s*([0-9a-fA-F]{8})\s+00\s+01\s+02\s+03\s+04\s+05\s+06\s+07\s+08\s+09\s+0A\s+0B\s+0C\s+0D\s+0E\s+0F\s*$",
    re.IGNORECASE,
)
_HEX_DUMP_RE = re.compile(r"^\s*([0-9a-fA-F]{8})\s+((?:[0-9a-fA-F]{2}\s+){0,15}[0-9a-fA-F]{2})\b")


def parse_read_ram_response(response: str) -> bytes:
    """Extract bytes from the hex dump text returned by ``cmd.read_ram``.

    Returns an empty bytes object when the response does not contain a
    parseable dump. Callers should retain and display the raw response.
    """
    data = bytearray()
    for line in response.splitlines():
        if _HEX_DUMP_HEADER_RE.match(line):
            continue
        m = _HEX_DUMP_RE.match(line)
        if not m:
            continue
        for token in m.group(2).split():
            data.append(int(token, 16))
    return bytes(data)


def read_memory(
    port: str | None,
    address: int | str,
    size: int,
    *,
    save: str | None = None,
    timeout: float = 10.0,
    bridge: "MKLinkSerialBridge | None" = None,
) -> tuple[bytes, str]:
    """Read target memory via MKLink ``cmd.read_ram``.

    Returns ``(parsed_bytes, raw_response)``. The parsed bytes may be empty if
    the device firmware returned a format this parser does not understand.

    Args:
        bridge: Optional pre-connected bridge instance. If provided, the
            port parameter is ignored and no new connection is created.
    """
    from mklink.bridge import MKLinkSerialBridge

    addr_s = f"0x{address:08X}" if isinstance(address, int) else address

    if bridge is not None:
        if save:
            cmd = f'cmd.read_ram({addr_s}, {size}, "{save}")'
        else:
            cmd = f"cmd.read_ram({addr_s}, {size})"
        raw = bridge.send_command(cmd, timeout=timeout)
        return parse_read_ram_response(raw), raw

    from mklink.cli import _resolve_port
    port = _resolve_port(port)
    bridge = MKLinkSerialBridge(port)
    if not bridge.connect():
        raise ConnectionError("MKLink connection failed")
    try:
        if save:
            cmd = f'cmd.read_ram({addr_s}, {size}, "{save}")'
        else:
            cmd = f"cmd.read_ram({addr_s}, {size})"
        raw = bridge.send_command(cmd, timeout=timeout)
        return parse_read_ram_response(raw), raw
    finally:
        bridge.close()
