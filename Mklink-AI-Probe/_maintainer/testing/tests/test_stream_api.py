import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mklink.remote.api import create_app
from mklink.remote.stream_protocol import StreamType, decode_frame


def _receive_data_frame(websocket):
    """Ignore protocol heartbeats while waiting for the published batch."""
    while True:
        frame = decode_frame(websocket.receive_bytes())
        if frame.stream_type is not StreamType.CONTROL:
            return frame


@pytest.fixture
def app():
    return create_app(auth_token=None, project_root=".")


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def test_create_app_has_an_independent_typed_stream_registry():
    from mklink.remote.dashboards import get_managers

    first = create_app(auth_token=None, project_root=".")
    second = create_app(auth_token=None, project_root=".")

    assert first.state.stream_registry is not second.state.stream_registry
    assert set(first.state.stream_registry) == {
        "systemview", "vofa", "rtt", "rtt-terminal", "serial", "superwatch",
    }
    assert first.state.stream_types == {
        "systemview": StreamType.SYSTEMVIEW,
        "vofa": StreamType.WAVEFORM,
        "rtt": StreamType.RTT_RAW,
        "rtt-terminal": StreamType.RTT_RAW,
        "serial": StreamType.SERIAL,
        "superwatch": StreamType.SUPERWATCH,
    }
    assert all(
        first.state.stream_registry[name]
        is not second.state.stream_registry[name]
        for name in first.state.stream_registry
    )
    assert get_managers()["systemview"]._stream_hub is second.state.stream_registry["systemview"]


@pytest.mark.parametrize(
    ("stream_name", "stream_type"),
    [
        ("systemview", StreamType.SYSTEMVIEW),
        ("vofa", StreamType.WAVEFORM),
        ("rtt", StreamType.RTT_RAW),
        ("rtt-terminal", StreamType.RTT_RAW),
        ("serial", StreamType.SERIAL),
        ("superwatch", StreamType.SUPERWATCH),
    ],
)
def test_websocket_sends_binary_batch_with_hub_metadata(
    client, app, stream_name, stream_type, monkeypatch,
):
    from mklink.remote import stream_api

    monkeypatch.setattr(stream_api, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    hub = app.state.stream_registry[stream_name]
    with client.websocket_connect(f"/ws/streams/{stream_name}") as websocket:
        assert decode_frame(websocket.receive_bytes()).stream_type is StreamType.CONTROL
        if stream_name == "superwatch":
            metadata = decode_frame(websocket.receive_bytes())
            assert metadata.stream_type is StreamType.SUPERWATCH
            assert metadata.flags == 0x02
        expected_sequence = hub.publish(b"payload", item_count=7)
        frame = _receive_data_frame(websocket)

    assert frame.stream_type is stream_type
    assert frame.stream_id == int(stream_type)
    assert frame.sequence == expected_sequence
    assert frame.item_count == 7
    assert frame.timestamp_ns > 0
    assert frame.payload == b"payload"


def test_idle_websocket_sends_compact_control_telemetry(client, app, monkeypatch):
    from mklink.remote import stream_api

    monkeypatch.setattr(stream_api, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    hub = app.state.stream_registry["vofa"]
    before = hub.stats()

    with client.websocket_connect("/ws/streams/vofa") as websocket:
        frame = decode_frame(websocket.receive_bytes())

    telemetry = json.loads(frame.payload.decode("utf-8"))
    assert frame.stream_type is StreamType.CONTROL
    assert frame.stream_id == int(StreamType.WAVEFORM)
    assert frame.sequence == before.last_sequence
    assert frame.item_count == 0
    assert b" " not in frame.payload
    assert telemetry["produced_batches"] == before.produced_batches
    assert telemetry["dropped_batches"] == before.dropped_batches
    assert hub.stats().produced_batches == before.produced_batches
    assert hub.stats().last_sequence == before.last_sequence


def test_websocket_accepts_existing_server_auth_token(monkeypatch):
    from mklink.remote import stream_api

    monkeypatch.setattr(stream_api, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    app = create_app(auth_token="secret", project_root=".")
    with TestClient(app) as client:
        with client.websocket_connect("/ws/streams/rtt") as websocket:
            websocket.send_json({"token": "secret"})
            assert decode_frame(websocket.receive_bytes()).stream_type is StreamType.CONTROL
            app.state.stream_registry["rtt"].publish(b"ok", item_count=1)
            assert _receive_data_frame(websocket).payload == b"ok"


def test_websocket_accepts_private_authorization_header(monkeypatch):
    from mklink.remote import stream_api

    monkeypatch.setattr(stream_api, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    app = create_app(auth_token="secret", project_root=".")
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/ws/streams/rtt",
            headers={"Authorization": "Bearer secret"},
        ) as websocket,
    ):
        assert decode_frame(websocket.receive_bytes()).stream_type is StreamType.CONTROL
        app.state.stream_registry["rtt"].publish(b"ok", item_count=1)
        assert _receive_data_frame(websocket).payload == b"ok"


@pytest.mark.parametrize(
    "auth_message",
    [
        {"token": "wrong"},
        {},
        [],
        None,
        "secret",
        {"params": []},
        {"params": None},
        {"params": "secret"},
    ],
)
def test_websocket_rejects_invalid_auth_token_with_policy_close(auth_message):
    app = create_app(auth_token="secret", project_root=".")
    with TestClient(app) as client:
        with client.websocket_connect("/ws/streams/rtt") as websocket:
            websocket.send_json(auth_message)
            with pytest.raises(WebSocketDisconnect) as error:
                websocket.receive_bytes()
    assert error.value.code == 1008
    assert app.state.stream_registry["rtt"].stats().active_clients == 0


@pytest.mark.parametrize(
    "auth_payload", [b"", b"binary", b'{"token":"secret"}', b"\x00\xff"],
)
def test_websocket_rejects_binary_auth_frame_with_policy_close(auth_payload):
    app = create_app(auth_token="secret", project_root=".")
    with TestClient(app) as client:
        with client.websocket_connect("/ws/streams/rtt") as websocket:
            websocket.send_bytes(auth_payload)
            with pytest.raises(WebSocketDisconnect) as error:
                websocket.receive_bytes()
    assert error.value.code == 1008
    assert app.state.stream_registry["rtt"].stats().active_clients == 0


def test_fanout_clients_share_publish_timestamp_and_batch_metadata(
    client, app, monkeypatch,
):
    from mklink.remote import stream_api

    monkeypatch.setattr(stream_api, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    hub = app.state.stream_registry["vofa"]
    with (
        client.websocket_connect("/ws/streams/vofa") as first,
        client.websocket_connect("/ws/streams/vofa") as second,
    ):
        assert decode_frame(first.receive_bytes()).stream_type is StreamType.CONTROL
        assert decode_frame(second.receive_bytes()).stream_type is StreamType.CONTROL
        expected_sequence = hub.publish(b"shared", item_count=5)
        first_frame = _receive_data_frame(first)
        second_frame = _receive_data_frame(second)

    assert first_frame.sequence == second_frame.sequence == expected_sequence
    assert first_frame.item_count == second_frame.item_count == 5
    assert first_frame.timestamp_ns == second_frame.timestamp_ns


def test_websocket_rejects_unknown_stream_with_policy_close(client):
    with client.websocket_connect("/ws/streams/unknown") as websocket:
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_bytes()
    assert error.value.code == 1008


def test_websocket_disconnect_unsubscribes_client(client, app, monkeypatch):
    from mklink.remote import stream_api

    monkeypatch.setattr(stream_api, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    hub = app.state.stream_registry["systemview"]
    with client.websocket_connect("/ws/streams/systemview") as websocket:
        websocket.receive_bytes()
        assert hub.stats().active_clients == 1

    deadline = time.monotonic() + 1
    while hub.stats().active_clients and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hub.stats().active_clients == 0


def test_publish_from_external_thread_wakes_websocket(client, app, monkeypatch):
    from mklink.remote import stream_api

    monkeypatch.setattr(stream_api, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    hub = app.state.stream_registry["superwatch"]
    publisher = threading.Thread(
        target=lambda: hub.publish(b"thread", item_count=3), daemon=True,
    )
    with client.websocket_connect("/ws/streams/superwatch") as websocket:
        assert decode_frame(websocket.receive_bytes()).stream_type is StreamType.CONTROL
        metadata = decode_frame(websocket.receive_bytes())
        assert metadata.stream_type is StreamType.SUPERWATCH
        assert metadata.flags == 0x02
        publisher.start()
        frame = _receive_data_frame(websocket)
        publisher.join(timeout=1)

    assert not publisher.is_alive()
    assert frame.payload == b"thread"
    assert frame.item_count == 3


@pytest.mark.parametrize(
    ("request_method", "path", "request_kwargs", "manager_method"),
    [
        ("post", "/api/dash/superwatch/add", {"json": {"name": "a"}}, "add_watch"),
        ("post", "/api/dash/superwatch/remove", {"json": {"name": "a"}}, "remove_watch"),
        ("get", "/api/dash/superwatch/status", {}, "get_status"),
        (
            "post", "/api/dash/superwatch/array-snapshot/select",
            {"json": {"name": "a", "start_index": 0, "count": 1}},
            "select_array_snapshot",
        ),
        (
            "post", "/api/dash/superwatch/array-snapshot/clear",
            {"json": {}}, "clear_array_snapshot",
        ),
        (
            "get", "/api/dash/superwatch/array-snapshot",
            {}, "get_array_snapshot",
        ),
    ],
)
def test_superwatch_locking_api_work_does_not_block_health(
    client, monkeypatch, request_method, path, request_kwargs, manager_method,
):
    from mklink.remote.dashboards import get_managers

    manager = get_managers()["superwatch"]
    operation_started = threading.Event()
    release_operation = threading.Event()
    watchdog_released = threading.Event()
    responses = []

    def blocking_operation(*_args, **_kwargs):
        operation_started.set()
        assert release_operation.wait(2.0)
        return {"state": "done"}

    monkeypatch.setattr(manager, manager_method, blocking_operation)
    operation = threading.Thread(
        target=lambda: responses.append(
            getattr(client, request_method)(path, **request_kwargs)
        ),
        daemon=True,
    )
    operation.start()
    assert operation_started.wait(1.0)

    def release_from_watchdog():
        watchdog_released.set()
        release_operation.set()

    watchdog = threading.Timer(1.0, release_from_watchdog)
    watchdog.start()
    try:
        health = client.get("/api/health")
        health_completed_while_blocked = (
            not watchdog_released.is_set()
            and not release_operation.is_set()
        )
    finally:
        release_operation.set()
        watchdog.cancel()
        operation.join(timeout=1.0)

    assert not operation.is_alive()
    assert health.status_code == 200
    assert health_completed_while_blocked
    assert responses[0].status_code == 200


def test_send_failure_unsubscribes_client(monkeypatch):
    from mklink.remote.stream_api import stream_websocket
    from mklink.remote.stream_hub import StreamHub

    class FailingWebSocket:
        async def accept(self):
            pass

        async def send_bytes(self, _data):
            raise RuntimeError("send failed")

    hub = StreamHub(max_batches_per_client=2)

    async def exercise():
        monkeypatch.setattr(
            "mklink.remote.stream_api.HEARTBEAT_INTERVAL_SECONDS", 0.001,
        )
        await stream_websocket(
            FailingWebSocket(), "systemview", {"systemview": hub},
            {"systemview": StreamType.SYSTEMVIEW}, None,
        )

    asyncio.run(exercise())
    assert hub.stats().active_clients == 0
