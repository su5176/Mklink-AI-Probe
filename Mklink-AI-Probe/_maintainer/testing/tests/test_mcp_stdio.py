from __future__ import annotations

import io
import sys

from mklink import mcp_server


def test_mcp_stdio_keeps_prints_out_of_jsonrpc_stdout(monkeypatch):
    protocol_bytes = io.BytesIO()
    protocol_stdout = io.TextIOWrapper(protocol_bytes, encoding="utf-8")
    diagnostics = io.StringIO()

    class FakeMcp:
        def run(self, *, transport):
            assert transport == "stdio"
            print("[TX] cmd.get_idcode()")
            print("[SERIAL] idcode = 0x2BA01477")
            sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
            sys.stdout.buffer.flush()

    monkeypatch.setattr(sys, "stdout", protocol_stdout)
    monkeypatch.setattr(sys, "stderr", diagnostics)
    monkeypatch.setattr(mcp_server, "mcp", FakeMcp())

    mcp_server.run()
    protocol_stdout.flush()

    assert protocol_bytes.getvalue() == b'{"jsonrpc":"2.0","id":1,"result":{}}\n'
    assert diagnostics.getvalue().splitlines() == [
        "[TX] cmd.get_idcode()",
        "[SERIAL] idcode = 0x2BA01477",
    ]
    assert sys.stdout is protocol_stdout


def test_mcp_stdio_resets_device_when_transport_exits(monkeypatch):
    calls = []

    class FakeMcp:
        def run(self, *, transport):
            assert transport == "stdio"

    monkeypatch.setattr(mcp_server, "mcp", FakeMcp())
    monkeypatch.setattr(mcp_server, "_reset_device", lambda: calls.append("reset"))

    mcp_server.run()

    assert calls == ["reset"]
