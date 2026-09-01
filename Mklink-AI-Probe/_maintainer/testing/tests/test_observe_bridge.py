from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from mklink import mcp_server, mcp_stream_bridge, observe_bridge


@pytest.fixture(autouse=True)
def _bounded_process_producer_lifecycle():
    observe_bridge.shutdown_process_observation(timeout=1.0)
    yield
    observe_bridge.shutdown_process_observation(timeout=1.0)


class _Producer:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.events = []
        self.streams = []
        self.heartbeats = 0
        self.heartbeat_event = threading.Event()
        self.closed = 0

    def _check(self):
        if self.fail:
            raise RuntimeError("private failure with token=do-not-leak")

    def publish(self, kind, payload):
        self._check()
        self.events.append((kind, payload))

    def register_stream(self, endpoint, stream, *, headers=None, origin=None):
        self._check()
        self.streams.append((endpoint, stream, headers, origin))

    def heartbeat(self):
        self._check()
        self.heartbeats += 1
        self.heartbeat_event.set()

    def close(self):
        self._check()
        self.closed += 1


class _BlockingProducer(_Producer):
    def __init__(self):
        super().__init__()
        self.publish_started = threading.Event()
        self.release_publish = threading.Event()
        self.close_finished = threading.Event()

    def publish(self, kind, payload):
        self.publish_started.set()
        self.release_publish.wait(timeout=5.0)
        super().publish(kind, payload)

    def close(self):
        super().close()
        self.close_finished.set()


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function
        return register


def test_queued_command_repr_hides_args_kwargs_and_bearer_token():
    command = observe_bridge._QueuedCommand(
        "register_stream",
        ("ws://127.0.0.1/private", {"operation_id": "op-0123456789abcdef"}),
        {"headers": {"Authorization": "Bearer private-token"}},
    )

    rendered = repr(command)
    assert "127.0.0.1" not in rendered
    assert "operation_id" not in rendered
    assert "Bearer" not in rendered
    assert "private-token" not in rendered


def _install_observe(monkeypatch, producer, *, legacy=False):
    calls = []

    if legacy:
        class Registry:
            def open_producer(self, *, source, private_correlation=None):
                calls.append((source, private_correlation))
                return producer
    else:
        class Registry:
            def open_producer(self, public, *, correlation=None):
                calls.append((public, correlation))
                return producer

    module = SimpleNamespace(ObservationRegistry=Registry)
    monkeypatch.setattr(
        observe_bridge.importlib,
        "import_module",
        lambda name: module if name == "hil_core.observe" else None,
    )
    return calls


@pytest.mark.parametrize("legacy", [False, True])
def test_open_producer_supports_contract_spelling_without_import_time_dependency(
    monkeypatch, legacy,
):
    raw = _Producer()
    calls = _install_observe(monkeypatch, raw, legacy=legacy)

    producer = observe_bridge.open_producer("opaque-correlation")

    assert producer.flush(timeout=1.0) is True
    assert producer.available is True
    expected = (
        {"kind": "mklink", "label": "MKLink"}
        if legacy
        else {
            "device": {
                "id": "mklink",
                "kind": "mklink",
                "label": "MKLink",
                "state": "available",
            },
        }
    )
    assert calls == [(expected, "opaque-correlation")]
    assert producer.close(flush_timeout=1.0) is True


def test_missing_observe_dependency_and_producer_failures_are_noops(monkeypatch):
    def missing(_name):
        raise ModuleNotFoundError("hil_core")

    monkeypatch.setattr(observe_bridge.importlib, "import_module", missing)
    producer = observe_bridge.open_producer()
    assert producer.flush(timeout=1.0) is False
    assert producer.available is False
    assert producer.publish("operation.started", {"operation": "program.flash"}) is False
    assert producer.heartbeat() is False
    producer.close()

    failing = observe_bridge.SafeProducer(_Producer(fail=True))
    assert failing.publish("operation.started", {"operation": "program.flash"}) is True
    assert failing.flush(timeout=1.0) is False
    assert failing.available is False
    assert failing.heartbeat() is False
    failing.close()


def test_public_projection_drops_sensitive_and_unbounded_fields():
    payload = observe_bridge.project_event_payload(
        "operation.completed",
        {
            "operation": "console.rtt.capture",
            "capability": "console.rtt",
            "action_class": "observe",
            "ok": True,
            "path": r"D:\secret\firmware.axf",
            "port": "COM9",
            "serial": "probe-id",
            "token": "secret",
            "raw_command": "RTTView.start(0x20000000)",
            "raw_dump": "classified target output",
            "facts": [
                {"name": "bytes_read", "value": 42, "unit": "bytes"},
                {"name": "raw", "value": "classified"},
                {"name": "target.path", "value": "firmware.axf"},
            ] * 20,
        },
    )

    assert payload == {
        "operation": "console.rtt.capture",
        "capability": "console.rtt",
        "action_class": "observe",
        "ok": True,
        "facts": [
            {"name": "bytes_read", "value": 42, "unit": "bytes"},
        ] * 11,
    }
    assert len(payload["facts"]) <= 32
    assert not any(
        secret in repr(payload)
        for secret in ("D:\\secret", "COM9", "probe-id", "classified", "0x20000000")
    )


def test_register_stream_keeps_endpoint_and_auth_private(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    producer = observe_bridge.open_producer()
    stream = {
        "id": "mklink.systemview",
        "protocol": "websocket",
        "state": "available",
        "media_type": "application/vnd.mklink.mkst",
        "encoding": "mkst-v1",
        "token": "must-drop",
    }

    assert producer.register_stream(
        "ws://127.0.0.1:8765/ws/streams/systemview",
        stream,
        headers={"Authorization": "Bearer private-token"},
    ) is True
    assert producer.flush(timeout=1.0) is True
    assert raw.streams == [(
        "ws://127.0.0.1:8765/ws/streams/systemview",
        {
            "id": "mklink.systemview",
            "protocol": "websocket",
            "state": "available",
            "media_type": "application/vnd.mklink.mkst",
            "encoding": "mkst-v1",
        },
        {"Authorization": "Bearer private-token"},
        None,
    )]
    assert raw.events == []

    assert producer.register_stream(
        "ws://remote.example:8765/ws/streams/systemview",
        stream,
        headers={"Authorization": "Bearer private-token"},
    ) is False
    assert producer.register_stream(
        "ws://127.0.0.1:8765/ws/streams/systemview?token=bad",
        stream,
    ) is False
    assert producer.register_stream(
        "ws://127.0.0.1:8765/ws/streams/systemview",
        stream,
        headers={"X-Token": "private-token"},
    ) is False
    assert producer.close(flush_timeout=1.0) is True


def test_operation_context_publishes_safe_lifecycle_and_rethrows(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)

    with (
        pytest.raises(RuntimeError, match="sensitive device text"),
        observe_bridge.observe_operation(
            "console.rtt.capture",
            capability="console.rtt",
            action_class="observe",
        ) as operation,
    ):
        operation.progress(20)
        operation.progress(21)  # Coalesced below the five percent boundary.
        raise RuntimeError("sensitive device text / token=private")

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert raw.events == [
        (
            "operation.started",
            {
                "operation": "console.rtt.capture",
                "capability": "console.rtt",
                "action_class": "observe",
            },
        ),
        (
            "operation.progress",
            {
                "operation": "console.rtt.capture",
                "capability": "console.rtt",
                "action_class": "observe",
                "progress": 0.2,
            },
        ),
        (
            "operation.failed",
            {
                "operation": "console.rtt.capture",
                "capability": "console.rtt",
                "action_class": "observe",
                "ok": False,
                "error_code": "operation_failed",
            },
        ),
    ]
    assert raw.closed == 0
    assert "sensitive device text" not in repr(raw.events)
    assert observe_bridge.shutdown_process_observation(timeout=1.0) is True
    assert raw.closed == 1


def test_long_operation_heartbeats_without_touching_operation_result(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    monkeypatch.setattr(observe_bridge.SafeProducer, "HEARTBEAT_SECONDS", 0.01)

    with observe_bridge.observe_operation(
        "console.rtt.read",
        capability="console.rtt",
        action_class="observe",
    ):
        assert raw.heartbeat_event.wait(timeout=1.0)

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert raw.heartbeats >= 1
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.completed",
    ]


def test_progress_normalizes_integer_percent_without_treating_one_as_complete(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)

    with observe_bridge.observe_operation(
        "program.flash",
        capability="program",
        action_class="emit",
    ) as operation:
        for value in (0, 1, 2, 5, 10, 100, 100):
            operation.progress(value)

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    progress = [
        payload["progress"]
        for kind, payload in raw.events
        if kind == "operation.progress"
    ]
    assert progress == [0.0, 0.05, 0.1, 1.0]


def test_process_producer_is_reused_across_mcp_operations(monkeypatch):
    raw = _Producer()
    calls = _install_observe(monkeypatch, raw)

    for _ in range(2):
        with observe_bridge.observe_operation(
            "console.rtt.read",
            capability="console.rtt",
            action_class="observe",
        ):
            pass

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert len(calls) == 1
    assert [kind for kind, _payload in raw.events] == [
        "operation.started",
        "operation.completed",
        "operation.started",
        "operation.completed",
    ]
    assert raw.closed == 0
    assert observe_bridge.shutdown_process_observation(timeout=1.0) is True
    assert raw.closed == 1


def test_blocked_observe_io_never_blocks_flash_or_unbounds_queue(monkeypatch):
    raw = _BlockingProducer()
    _install_observe(monkeypatch, raw)
    monkeypatch.setattr(observe_bridge.SafeProducer, "QUEUE_CAPACITY", 4)
    expected = {"success": True, "verified": True}

    class Device:
        def flash(self, _firmware, **kwargs):
            for percent in range(0, 101, 5):
                kwargs["progress_callback"](percent)
            return expected

    monkeypatch.setattr(mcp_server, "_connected_device", lambda: Device())
    mcp = _Mcp()
    mcp_server._register_flash_tools(mcp)

    started_at = time.monotonic()
    try:
        assert mcp.tools["flash"]("firmware.bin") is expected
        assert time.monotonic() - started_at < 0.5
        assert raw.publish_started.wait(timeout=1.0)
        producer = observe_bridge.process_producer()
        assert producer.dropped_commands > 0

        close_started = time.monotonic()
        assert observe_bridge.shutdown_process_observation(timeout=0.02) is False
        assert time.monotonic() - close_started < 0.2
    finally:
        raw.release_publish.set()

    assert raw.close_finished.wait(timeout=1.0)
    kinds = [kind for kind, _payload in raw.events]
    assert kinds[0] == "operation.started"
    assert kinds[-1] == "operation.completed"


def test_rtt_and_systemview_projectors_never_include_raw_payloads():
    rtt = observe_bridge.rtt_facts(
        "secret token=abc\nsecond raw line\n",
        duration=2.5,
        matched=True,
    )
    assert rtt == [
        {"name": "bytes_read", "value": 33, "unit": "bytes"},
        {"name": "line_count", "value": 2, "unit": "lines"},
        {"name": "duration", "value": 2.5, "unit": "s"},
        {"name": "matched", "value": True},
    ]

    analysis = observe_bridge.systemview_analysis_facts({
        "summary": {
            "event_count": 100,
            "analyzed_event_count": 95,
            "observed_us": 2000.0,
            "switch_count": 12,
            "switches_per_sec": 6000.0,
            "task_count": 1,
            "idle_pct": 20.0,
            "isr_cpu_pct": 2.0,
        },
        "tasks": [{
            "id": 0x20001234,
            "name": "token_worker",
            "run_us": 800.0,
            "cpu_pct": 80.0,
            "switches": 7,
            "raw": "never publish",
        }],
        "anomalies": [{"detail": "secret raw trace"}],
    })
    assert {fact["name"] for fact in analysis} >= {
        "event_count",
        "task.0.name",
        "task.0.run",
        "task.0.cpu",
        "task.0.switches",
    }
    assert "0x20001234" not in repr(analysis)
    assert "secret raw trace" not in repr(analysis)
    assert "token_worker" not in repr(analysis)


@dataclass(frozen=True)
class _Stats:
    produced_batches: int = 3
    produced_items: int = 30
    produced_bytes: int = 300
    delivered_batches: int = 3
    delivered_items: int = 30
    delivered_bytes: int = 300
    dropped_batches: int = 0
    dropped_items: int = 0
    dropped_bytes: int = 0
    active_clients: int = 1
    queue_high_water_mark: int = 2
    last_sequence: int = 3


class _Hub:
    def __init__(self):
        self.value = _Stats()

    def stats(self):
        return self.value


def test_stream_observation_emits_checkpoint_and_drop_delta(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    hub = _Hub()
    observation = observe_bridge.StreamObservation({"vofa": hub})
    observation.configure(
        host="127.0.0.1",
        port=8765,
        auth_token="private-token",
        private_correlation="desktop-instance",
    )
    observation._producer = observe_bridge.open_producer("desktop-instance")

    observation.poll_once()
    hub.value = replace(
        hub.value,
        produced_batches=5,
        delivered_batches=4,
        dropped_batches=1,
        dropped_items=10,
        dropped_bytes=40,
        last_sequence=5,
    )
    observation.poll_once()

    assert observation._producer.flush(timeout=1.0) is True
    kinds = [kind for kind, _payload in raw.events]
    assert kinds == ["stream.checkpoint", "stream.gap", "stream.checkpoint"]
    gap = raw.events[1][1]
    assert gap["stream"]["id"] == "mklink.vofa"
    assert gap["facts"] == [
        {"name": "dropped_batches", "value": 1, "unit": "batches"},
        {"name": "dropped_items", "value": 10, "unit": "items"},
        {"name": "dropped_bytes", "value": 40, "unit": "bytes"},
    ]
    assert raw.heartbeats == 2
    assert observation._producer.close(flush_timeout=1.0) is True


def test_real_hil_observe_contract_round_trip(monkeypatch, tmp_path):
    observe = pytest.importorskip("hil_core.observe")
    monkeypatch.setenv("HIL_OBSERVE_ROOT", str(tmp_path))

    with observe_bridge.observe_operation(
        "console.rtt.capture",
        capability="console.rtt",
        action_class="observe",
        private_correlation="mcp-call-private",
    ) as operation:
        operation.complete(facts=observe_bridge.rtt_facts(
            "private target output\n", duration=0.1, matched=False,
        ))

    hub = _Hub()
    stream_observation = observe_bridge.StreamObservation({"vofa": hub})
    stream_observation.configure(
        host="127.0.0.1",
        port=8765,
        auth_token="private-token",
        private_correlation="desktop-private",
    )

    async def exercise_stream():
        await stream_observation.start()
        stream_observation.poll_once()
        assert await asyncio.to_thread(
            observe_bridge.flush_process_observation,
            5.0,
        ) is True
        active_registry = observe.ObservationRegistry()
        active_stream = next(
            session
            for session in active_registry.snapshot()["sessions"]
            if session["payload"].get("stream")
        )
        private_before_close = active_registry.read_private_session(
            active_stream["session_id"],
        )
        await stream_observation.stop()
        return private_before_close

    private_before_close = asyncio.run(exercise_stream())
    assert private_before_close["correlation"] == "desktop-private"
    assert private_before_close["streams"] == [{
        "stream_id": "mklink.vofa",
        "url": "ws://127.0.0.1:8765/ws/streams/vofa",
        "headers": {"Authorization": "Bearer private-token"},
        "origin": None,
    }]
    assert observe_bridge.shutdown_process_observation(timeout=5.0) is True

    registry = observe.ObservationRegistry()
    sessions = registry.snapshot()["sessions"]
    assert len(sessions) == 2
    assert all(session["liveness"] == "closed" for session in sessions)

    operation_session = next(
        session for session in sessions
        if session["payload"].get("operation") == "console.rtt.capture"
    )
    operation_kinds = [
        event["kind"]
        for event in registry.read_events(operation_session["session_id"])["events"]
    ]
    assert operation_kinds == [
        "session.opened", "operation.started", "operation.completed", "session.closed",
    ]

    stream_session = next(
        session for session in sessions
        if session["payload"].get("stream")
    )
    public_text = json.dumps(stream_session, ensure_ascii=False)
    assert "127.0.0.1:8765" not in public_text
    assert "private-token" not in public_text
    assert stream_session["payload"]["stream"] == [{
        "id": "mklink.vofa",
        "protocol": "websocket",
        "state": "closed",
        "media_type": "application/vnd.mklink.mkst",
        "encoding": "mkst-v1",
    }]
    private = registry.read_private_session(stream_session["session_id"])
    assert private["correlation"] is None
    assert private["streams"] == []


def test_direct_mcp_flash_publishes_progress_without_changing_result(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    calls = []
    expected = {
        "success": True,
        "verified": True,
        "time_ms": 125,
        "path": r"D:\private\firmware.hex",
        "idcode": "private-id",
    }

    class Device:
        def flash(self, firmware, **kwargs):
            calls.append((firmware, kwargs))
            kwargs["progress_callback"](25)
            kwargs["progress_callback"](100)
            return expected

    monkeypatch.setattr(mcp_server, "_connected_device", lambda: Device())
    mcp = _Mcp()
    mcp_server._register_flash_tools(mcp)

    result = mcp.tools["flash"](
        r"D:\private\firmware.hex", verify=True, reset_after=False,
    )

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert result is expected
    assert calls[0][0] == r"D:\private\firmware.hex"
    assert calls[0][1]["verify"] is True
    assert calls[0][1]["reset_after"] is False
    assert callable(calls[0][1]["progress_callback"])
    assert [kind for kind, _payload in raw.events] == [
        "operation.started",
        "operation.progress",
        "operation.progress",
        "operation.completed",
    ]
    assert raw.events[-1][1]["facts"] == [
        {"name": "verified", "value": True, "ok": True},
        {"name": "verify_requested", "value": True},
        {"name": "reset_requested", "value": False},
        {"name": "duration", "value": 125, "unit": "ms"},
    ]
    assert "firmware.hex" not in repr(raw.events)
    assert "private-id" not in repr(raw.events)


def test_direct_mcp_rtt_and_systemview_publish_summaries_only(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)

    class Device:
        def wait_for_rtt(self, pattern, *, timeout, start_if_needed):
            assert pattern == "READY-private"
            assert timeout == 0.2
            assert start_if_needed is True
            return "READY-private token=secret\n"

        def systemview_read(self, duration):
            assert duration == 0.1
            return {
                "events": [{"task_id": 0x20001234, "raw": "secret trace"}],
                "event_count": 1,
                "bytes_read": 64,
                "synced": True,
                "cpu_freq": 120_000_000,
                "dropped_bytes": 2,
                "dropped_packets": 1,
            }

    device = Device()
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: device)
    mcp = _Mcp()
    mcp_server._register_rtt_tools(mcp)
    mcp_server._register_systemview_tools(mcp)

    rtt_result = mcp.tools["capture_rtt"](
        duration=0.2, pattern="READY-private",
    )
    systemview_result = mcp.tools["systemview_read"](duration=0.1)

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert rtt_result == {
        "output": "READY-private token=secret\n",
        "matched": True,
    }
    assert systemview_result["events"][0]["raw"] == "secret trace"
    published = repr(raw.events)
    assert "READY-private" not in published
    assert "token=secret" not in published
    assert "0x20001234" not in published
    assert "secret trace" not in published
    completed = [
        payload for kind, payload in raw.events if kind == "operation.completed"
    ]
    assert completed[0]["facts"] == [
        {"name": "bytes_read", "value": 27, "unit": "bytes"},
        {"name": "line_count", "value": 1, "unit": "lines"},
        {"name": "duration", "value": 0.2, "unit": "s"},
        {"name": "matched", "value": True},
    ]
    assert completed[1]["facts"] == [
        {"name": "event_count", "value": 1, "unit": "count"},
        {"name": "bytes_read", "value": 64, "unit": "bytes"},
        {"name": "synced", "value": True, "ok": True},
        {"name": "dropped_bytes", "value": 2, "unit": "bytes"},
        {"name": "dropped_packets", "value": 1, "unit": "count"},
        {"name": "cpu_frequency", "value": 120_000_000, "unit": "Hz"},
    ]


def test_observation_failure_cannot_fail_direct_mcp_flash(monkeypatch):
    _install_observe(monkeypatch, _Producer(fail=True))
    expected = {"success": True, "verified": False}

    class Device:
        def flash(self, _firmware, **kwargs):
            kwargs["progress_callback"](50)
            return expected

    monkeypatch.setattr(mcp_server, "_connected_device", lambda: Device())
    mcp = _Mcp()
    mcp_server._register_flash_tools(mcp)

    assert mcp.tools["flash"]("firmware.bin") is expected
    assert observe_bridge.flush_process_observation(timeout=1.0) is False


def test_memory_public_facts_allow_only_numerically_canonical_address():
    projected = observe_bridge.project_event_payload(
        "operation.completed",
        {
            "operation": "memory.read",
            "capability": "target.memory",
            "action_class": "observe",
            "ok": True,
            "hex": "DEADBEEF",
            "data_hex": "DEADBEEF",
            "crc32": "7C9CA35A",
            "operation_id": "op-0123456789abcdef",
            "facts": [
                {"name": "address", "value": "0x20000000"},
                {"name": "address", "value": "0x0000000020000000"},
                {"name": "address", "value": "0x0000000100000000"},
                {"name": "raw", "value": 1},
                {"name": "crc32", "value": 0x7C9CA35A},
                {"name": "bytes_read", "value": 4, "unit": "bytes"},
            ],
        },
    )

    assert projected == {
        "operation": "memory.read",
        "capability": "target.memory",
        "action_class": "observe",
        "ok": True,
        "facts": [
            {"name": "address", "value": "0x20000000"},
            {"name": "address", "value": "0x0000000100000000"},
            {"name": "bytes_read", "value": 4, "unit": "bytes"},
        ],
    }
    public_text = repr(projected)
    assert "DEADBEEF" not in public_text
    assert "7C9CA35A" not in public_text
    assert "operation_id" not in public_text


def test_direct_mcp_memory_read_write_publish_safe_lifecycle_and_private_read(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    published = []
    writes = []

    class Device:
        def read_memory(self, address, size):
            if (address, size) == (0x20000010, 4):
                return b"\xDE\xAD\xBE\xEF"
            assert (address, size) == (0x20000000, 2)
            return b"\xCA\xFE"

        def write_memory(self, address, data):
            writes.append((address, data))

    monkeypatch.setattr(mcp_server, "_connected_device", lambda: Device())
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory",
        lambda operation, address, data: published.append(
            (operation, address, data),
        ) or True,
    )
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)

    read_result = mcp.tools["read_memory"](0x20000010, 4)
    write_result = mcp.tools["write_memory"](0x20000000, "CA FE")

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert read_result == {
        "address": "0x20000010",
        "size": 4,
        "bytes_read": 4,
        "hex": "deadbeef",
    }
    assert write_result == {
        "address": "0x20000000",
        "bytes_written": 2,
        "verified": True,
    }
    assert published == [("read", 0x20000010, b"\xDE\xAD\xBE\xEF")]
    assert writes == [(0x20000000, b"\xCA\xFE")]
    assert [kind for kind, _payload in raw.events] == [
        "operation.started",
        "operation.completed",
        "operation.started",
        "operation.completed",
    ]
    assert raw.events[1][1]["facts"] == [
        {"name": "address", "value": "0x20000010"},
        {"name": "requested_bytes", "value": 4, "unit": "bytes"},
        {"name": "bytes_read", "value": 4, "unit": "bytes"},
    ]
    assert raw.events[3][1]["facts"] == [
        {"name": "address", "value": "0x20000000"},
        {"name": "bytes_written", "value": 2, "unit": "bytes"},
    ]
    assert raw.events[3][1]["action_class"] == "emit"
    public_text = repr(raw.events)
    assert "deadbeef" not in public_text.lower()
    assert "cafe" not in public_text.lower()


@pytest.mark.parametrize("failure_mode", ["return_false", "raise"])
def test_direct_mcp_read_publish_failure_is_degraded_without_failing_read(
    monkeypatch,
    failure_mode,
):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    gaps = []

    class Device:
        def read_memory(self, _address, _size):
            return b"private-read"

    def publish(*_args):
        if failure_mode == "raise":
            raise RuntimeError("private sidechannel failure")
        return False

    monkeypatch.setattr(mcp_server, "_connected_device", lambda: Device())
    monkeypatch.setattr(mcp_stream_bridge, "publish_mcp_memory", publish)
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory_gap",
        lambda fact, count: gaps.append((fact, count)) or True,
    )
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)

    result = mcp.tools["read_memory"](0x20000000, 12)

    assert result["bytes_read"] == 12
    assert result["hex"] == b"private-read".hex()
    assert gaps == [("publish_drop_count", 1)]
    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.completed",
    ]
    assert "private-read" not in repr(raw.events)


def test_direct_mcp_read_accepts_exact_single_transfer_limit(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    published = []
    byte_count = mcp_server.MCP_MAX_DIRECT_READ_BYTES
    assert byte_count == 4096

    class Device:
        def read_memory(self, _address, size):
            assert size == byte_count
            return b"X" * size

    monkeypatch.setattr(mcp_server, "_connected_device", lambda: Device())
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory",
        lambda operation, address, data: published.append(
            (operation, address, len(data)),
        ) or True,
    )
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)

    result = mcp.tools["read_memory"](0x20000000, byte_count)

    assert result["bytes_read"] == byte_count
    assert len(result["hex"]) == byte_count * 2
    assert published == [("read", 0x20000000, byte_count)]
    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.completed",
    ]


def test_direct_mcp_read_rejects_above_single_transfer_limit_before_device(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    byte_count = mcp_server.MCP_MAX_DIRECT_READ_BYTES + 1

    monkeypatch.setattr(
        mcp_server,
        "_connected_device",
        lambda: pytest.fail("oversize memory read must not reach the device"),
    )
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory",
        lambda *_args: pytest.fail("oversize memory read must not be published"),
    )
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory_gap",
        lambda *_args: pytest.fail("rejected reads must not publish a gap"),
    )
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)

    with pytest.raises(ValueError, match="between 1 and 4096"):
        mcp.tools["read_memory"](0x20000000, byte_count)

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.failed",
    ]
    assert raw.events[1][1]["error_code"] == "invalid_argument"


@pytest.mark.parametrize("data", [b"", b"\xAA\xBB\xCC"])
def test_direct_mcp_read_rejects_empty_or_short_device_result_without_publish(
    monkeypatch,
    data,
):
    raw = _Producer()
    _install_observe(monkeypatch, raw)

    class Device:
        def read_memory(self, address, size):
            assert (address, size) == (0x20000000, 4)
            return data

    monkeypatch.setattr(mcp_server, "_connected_device", lambda: Device())
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory",
        lambda *_args: pytest.fail("incomplete memory must not be published"),
    )
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory_gap",
        lambda *_args: pytest.fail("incomplete reads must not publish a gap"),
    )
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)

    with pytest.raises(RuntimeError, match="incomplete data"):
        mcp.tools["read_memory"](0x20000000, 4)

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.failed",
    ]
    assert raw.events[1][1]["error_code"] == "operation_failed"
    assert "completed" not in repr(raw.events)
    assert not data or data.hex() not in repr(raw.events)


def test_direct_mcp_memory_rejects_negative_and_overflow_addresses_before_device(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    monkeypatch.setattr(
        mcp_server,
        "_connected_device",
        lambda: pytest.fail("invalid memory ranges must not reach the device"),
    )
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)

    with pytest.raises(ValueError, match="32-bit non-negative"):
        mcp.tools["read_memory"](-1, 4)
    with pytest.raises(ValueError, match="32-bit non-negative"):
        mcp.tools["write_memory"](0x1_0000000000000000, "00")

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert [kind for kind, _payload in raw.events] == [
        "operation.started",
        "operation.failed",
        "operation.started",
        "operation.failed",
    ]
    assert all(
        payload["error_code"] == "invalid_argument"
        for kind, payload in raw.events
        if kind == "operation.failed"
    )


def test_direct_mcp_dump_is_bounded_and_reuses_operation_id_across_samples(monkeypatch):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    bridge = object()
    calls = []
    sidechannel = []

    def read_regions(actual_bridge, pairs, *, timeout):
        calls.append((actual_bridge, pairs, timeout))
        sample = len(calls)
        return (bytes([sample]) * 4, bytes([sample + 16]) * 2)

    monkeypatch.setattr(mcp_server, "_connected_device", lambda: SimpleNamespace(
        _bridge=bridge,
    ))
    monkeypatch.setattr("mklink.dump_memory.read_dump_memory_regions_once", read_regions)
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory_regions",
        lambda operation, regions, *, sample_index, sample_count, operation_id: (
            sidechannel.append(
                (operation, regions, sample_index, sample_count, operation_id),
            )
        ) or True,
    )
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)

    result = mcp.tools["dump_memory"](
        [
            {"address": 0x20000000, "size": 4},
            {"address": 0x1_00000000, "size": 2},
        ],
        sample_count=2,
        timeout=0.25,
    )

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert calls == [
        (bridge, [(0x20000000, 4), (0x1_00000000, 2)], 0.25),
        (bridge, [(0x20000000, 4), (0x1_00000000, 2)], 0.25),
    ]
    assert result == {
        "sample_count": 2,
        "region_count": 2,
        "total_bytes": 12,
        "samples": [
            {
                "sample_index": 0,
                "regions": [
                    {"address": "0x20000000", "size": 4, "data_hex": "01010101"},
                    {"address": "0x0000000100000000", "size": 2, "data_hex": "1111"},
                ],
            },
            {
                "sample_index": 1,
                "regions": [
                    {"address": "0x20000000", "size": 4, "data_hex": "02020202"},
                    {"address": "0x0000000100000000", "size": 2, "data_hex": "1212"},
                ],
            },
        ],
    }
    assert [item[2] for item in sidechannel] == [0, 1]
    assert [item[3] for item in sidechannel] == [2, 2]
    assert len({item[4] for item in sidechannel}) == 1
    assert sidechannel[0][4].startswith("op-")
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.completed",
    ]
    assert raw.events[-1][1]["facts"] == [
        {"name": "address", "value": "0x20000000"},
        {"name": "total_bytes", "value": 12, "unit": "bytes"},
        {"name": "region_count", "value": 2, "unit": "count"},
        {"name": "sample_count", "value": 2, "unit": "count"},
    ]
    assert "01010101" not in repr(raw.events)
    assert sidechannel[0][4] not in repr(raw.events)


@pytest.mark.parametrize("failure_mode", ["return_false", "raise"])
def test_direct_mcp_dump_private_publish_drop_keeps_response_and_marks_each_sample(
    monkeypatch,
    failure_mode,
):
    raw = _Producer()
    _install_observe(monkeypatch, raw)
    gaps = []
    bridge = object()
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: SimpleNamespace(
        _bridge=bridge,
    ))
    monkeypatch.setattr(
        "mklink.dump_memory.read_dump_memory_regions_once",
        lambda actual_bridge, _pairs, *, timeout: (
            b"private" if actual_bridge is bridge and timeout == 0.1 else b"",
        ),
    )

    def publish(*_args, **_kwargs):
        if failure_mode == "raise":
            raise RuntimeError("private dump sidechannel failure")
        return False

    monkeypatch.setattr(mcp_stream_bridge, "publish_mcp_memory_regions", publish)
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory_gap",
        lambda fact, count: gaps.append((fact, count)) or True,
    )
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)

    result = mcp.tools["dump_memory"](
        [{"address": 0x20000000, "size": 7}],
        sample_count=2,
        timeout=0.1,
    )

    assert result["sample_count"] == 2
    assert [sample["regions"][0]["data_hex"] for sample in result["samples"]] == [
        b"private".hex().upper(),
        b"private".hex().upper(),
    ]
    assert gaps == [("publish_drop_count", 1), ("publish_drop_count", 1)]
    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.completed",
    ]
    assert "private" not in repr(raw.events)


def test_direct_mcp_dump_failure_emits_failed_lifecycle_and_stream_gap(monkeypatch):
    from mklink.dump_memory import DumpMemoryReadError

    raw = _Producer()
    _install_observe(monkeypatch, raw)
    gaps = []
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: SimpleNamespace(
        _bridge=object(),
    ))

    def fail_dump(*_args, **_kwargs):
        raise DumpMemoryReadError(
            "private bad block details",
            gap_fact="crc_error_count",
            gap_count=2,
        )

    monkeypatch.setattr("mklink.dump_memory.read_dump_memory_regions_once", fail_dump)
    monkeypatch.setattr(
        mcp_stream_bridge,
        "publish_mcp_memory_gap",
        lambda fact, count: gaps.append((fact, count)) or True,
    )
    mcp = _Mcp()
    mcp_server._register_memory_tools(mcp)

    with pytest.raises(DumpMemoryReadError, match="private bad block"):
        mcp.tools["dump_memory"]([{"address": 0x20000000, "size": 4}])

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert gaps == [("crc_error_count", 2)]
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.failed",
    ]
    assert raw.events[-1][1]["error_code"] == "operation_failed"
    assert "private bad block" not in repr(raw.events)


def test_direct_mcp_flush_partial_result_is_failed_without_changing_response(monkeypatch):
    from mklink import cli

    raw = _Producer()
    _install_observe(monkeypatch, raw)
    responses = iter(["ok", "partial"])
    bridge = SimpleNamespace(
        send_command=lambda _command, timeout: next(responses),
    )
    monkeypatch.setattr(mcp_server, "_connected_device", lambda: SimpleNamespace(
        _bridge=bridge,
    ))
    monkeypatch.setattr(
        cli,
        "_parse_flush_response",
        lambda response: (response == "ok", response),
    )
    mcp = _Mcp()
    mcp_server._register_flush_tools(mcp)
    writes = [
        {"address": 0x20000000 + index, "data_hex": f"{index:02X}"}
        for index in range(8)
    ]

    result = mcp.tools["flush_memory"](writes)

    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert result == {
        "ok": False,
        "batches": 2,
        "total_bytes": 8,
        "results": [
            {"batch": 1, "items": 6, "bytes": 6, "ok": True, "message": "ok"},
            {
                "batch": 2,
                "items": 2,
                "bytes": 2,
                "ok": False,
                "message": "partial",
            },
        ],
    }
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.failed",
    ]
    failed = raw.events[-1][1]
    assert failed["error_code"] == "memory_flush_failed"
    assert failed["facts"] == [
        {"name": "address", "value": "0x20000000"},
        {"name": "total_bytes", "value": 8, "unit": "bytes"},
        {"name": "region_count", "value": 8, "unit": "count"},
        {"name": "batch_count", "value": 2, "unit": "batches"},
        {"name": "successful_batches", "value": 1, "unit": "batches"},
        {"name": "failed_batches", "value": 1, "unit": "batches"},
    ]
    assert "data_hex" not in repr(raw.events)


def test_direct_mcp_flush_exception_after_success_reports_safe_batch_counts(monkeypatch):
    from mklink import cli

    raw = _Producer()
    _install_observe(monkeypatch, raw)
    failure = RuntimeError("private transport detail raw=DEADBEEF")
    calls = 0

    def send_command(_command, timeout):
        nonlocal calls
        assert timeout == 10.0
        calls += 1
        if calls == 1:
            return "ok"
        raise failure

    monkeypatch.setattr(mcp_server, "_connected_device", lambda: SimpleNamespace(
        _bridge=SimpleNamespace(send_command=send_command),
    ))
    monkeypatch.setattr(cli, "_parse_flush_response", lambda response: (True, response))
    mcp = _Mcp()
    mcp_server._register_flush_tools(mcp)
    writes = [
        {"address": 0x20000000 + index, "data_hex": f"{index:02X}"}
        for index in range(8)
    ]

    with pytest.raises(RuntimeError) as raised:
        mcp.tools["flush_memory"](writes)

    assert raised.value is failure
    assert observe_bridge.flush_process_observation(timeout=1.0) is True
    assert [kind for kind, _payload in raw.events] == [
        "operation.started", "operation.failed",
    ]
    failed = raw.events[-1][1]
    assert failed["error_code"] == "memory_flush_transport_failed"
    assert failed["facts"] == [
        {"name": "address", "value": "0x20000000"},
        {"name": "total_bytes", "value": 8, "unit": "bytes"},
        {"name": "region_count", "value": 8, "unit": "count"},
        {"name": "batch_count", "value": 2, "unit": "batches"},
        {"name": "successful_batches", "value": 1, "unit": "batches"},
        {"name": "failed_batches", "value": 1, "unit": "batches"},
    ]
    assert "DEADBEEF" not in repr(raw.events)
