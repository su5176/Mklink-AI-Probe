from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

import pytest

from mklink import mcp_server, mcp_stream_bridge, observe_bridge


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


def test_mcp_stdio_continues_when_optional_sidecar_start_fails(monkeypatch):
    calls = []

    class FakeMcp:
        def run(self, *, transport):
            calls.append(("run", transport))

    def fail_start(*, wait_timeout):
        assert wait_timeout == 0.75
        raise RuntimeError("synthetic sidecar failure")

    monkeypatch.setattr(mcp_server, "mcp", FakeMcp())
    monkeypatch.setattr(mcp_server, "_reset_device", lambda: calls.append("reset"))
    monkeypatch.setattr(mcp_stream_bridge, "start_mcp_stream_sidecar", fail_start)
    monkeypatch.setattr(
        mcp_stream_bridge,
        "stop_mcp_stream_sidecar",
        lambda timeout: calls.append(("stop", timeout)),
    )

    mcp_server.run()

    assert calls == [("run", "stdio"), "reset", ("stop", 0.5)]


def test_real_fastmcp_dump_memory_jsonrpc_result_snapshot(monkeypatch):
    fastmcp = pytest.importorskip("fastmcp")
    bridge = object()
    monkeypatch.setattr(
        mcp_server,
        "_connected_device",
        lambda: SimpleNamespace(_bridge=bridge),
    )
    monkeypatch.setattr(
        "mklink.dump_memory.read_dump_memory_regions_once",
        lambda actual_bridge, _pairs, *, timeout: (
            b"AB" if actual_bridge is bridge and timeout == 0.1 else b"",
        ),
    )
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory_regions",
        lambda *_args, **_kwargs: True,
    )
    server = fastmcp.FastMCP("memory-contract")
    mcp_server._register_memory_tools(server)

    async def exchange():
        async with fastmcp.Client(server) as client:
            tools = await client.list_tools()
            tool = next(item for item in tools if item.name == "dump_memory")
            result = await client.call_tool("dump_memory", {
                "regions": [{"address": 0x20000000, "size": 2}],
                "sample_count": 1,
                "timeout": 0.1,
            })
            return tool, result

    try:
        tool, result = asyncio.run(exchange())
    finally:
        observe_bridge.shutdown_process_observation(timeout=1.0)

    assert tool.inputSchema == {
        "additionalProperties": False,
        "properties": {
            "regions": {
                "items": {"additionalProperties": True, "type": "object"},
                "type": "array",
            },
            "sample_count": {"default": 1, "type": "integer"},
            "timeout": {"default": 10.0, "type": "number"},
        },
        "required": ["regions"],
        "type": "object",
    }
    assert {
        "content": [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in result.content
        ],
        "structuredContent": result.structured_content,
        "isError": result.is_error,
    } == {
        "content": [{
            "type": "text",
            "text": (
                '{"sample_count":1,"region_count":1,"total_bytes":2,'
                '"samples":[{"sample_index":0,"regions":[{'
                '"address":"0x20000000","size":2,"data_hex":"4142"}]}]}'
            ),
        }],
        "structuredContent": {
            "sample_count": 1,
            "region_count": 1,
            "total_bytes": 2,
            "samples": [{
                "sample_index": 0,
                "regions": [{
                    "address": "0x20000000",
                    "size": 2,
                    "data_hex": "4142",
                }],
            }],
        },
        "isError": False,
    }
