from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from websockets.sync.client import connect

from mklink import mcp_stream_bridge, observe_bridge
from mklink.remote.stream_protocol import (
    MEMORY_JSON_V1,
    RTT_RAW_UTF8_LINES,
    SUPERWATCH_METADATA_JSON,
    SUPERWATCH_SAMPLE_MAJOR_FLOAT32,
    StreamType,
    decode_frame,
    decode_memory_record,
    decode_rtt_lines,
    decode_superwatch_metadata,
    decode_waveform_samples,
)


@pytest.fixture(autouse=True)
def _sidecar_lifecycle():
    mcp_stream_bridge.stop_mcp_stream_sidecar(timeout=1.0)
    observe_bridge.shutdown_process_observation(timeout=1.0)
    yield
    mcp_stream_bridge.stop_mcp_stream_sidecar(timeout=1.0)
    observe_bridge.shutdown_process_observation(timeout=1.0)


class _Producer:
    def __init__(self):
        self.events = []
        self.streams = []
        self.closed = threading.Event()

    def publish(self, kind, payload):
        self.events.append((kind, payload))

    def register_stream(self, endpoint, stream, *, headers=None, origin=None):
        self.streams.append((endpoint, stream, headers, origin))

    def heartbeat(self):
        return None

    def close(self):
        self.closed.set()


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _receive_data_frame(websocket, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = decode_frame(websocket.recv(timeout=max(0.01, deadline - time.monotonic())))
        if frame.stream_type is not StreamType.CONTROL:
            return frame
    pytest.fail("timed out waiting for an MKST data frame")


def test_mcp_sidecar_thread_start_failure_is_a_clean_noop(monkeypatch):
    original_start = threading.Thread.start

    def fail_server_start(thread):
        if thread.name == "mklink-mcp-stream-server":
            raise RuntimeError("synthetic thread start failure")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_server_start)
    sidecar = mcp_stream_bridge.McpStreamSidecar()

    assert sidecar.start(wait_timeout=0.01) is False
    assert sidecar.enqueue(mcp_stream_bridge._DataCommand("rtt", "lost")) is False
    assert sidecar.stop(timeout=1.0) is True


def test_mcp_sidecar_retains_data_while_listener_is_still_starting(monkeypatch):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()
    original_serve = sidecar._serve

    async def delayed_serve():
        await asyncio.sleep(0.1)
        await original_serve()

    monkeypatch.setattr(sidecar, "_serve", delayed_serve)
    try:
        assert sidecar.start(wait_timeout=0.01) is False
        assert sidecar.enqueue(mcp_stream_bridge._DataCommand(
            "rtt",
            "first before ready\n",
        )) is True
        assert _wait_for(lambda: sidecar.available)
        assert _wait_for(lambda: bool(raw.streams))
        assert producer.flush(timeout=1.0) is True

        endpoint = sidecar.endpoint("rtt")
        assert endpoint is not None
        with connect(
            endpoint,
            additional_headers={
                "Authorization": f"Bearer {sidecar.auth_token}",
            },
            open_timeout=5.0,
            close_timeout=0.2,
        ) as websocket:
            assert decode_frame(websocket.recv(timeout=5.0)).stream_type is StreamType.CONTROL
            retained = _receive_data_frame(websocket)
        assert [line.text for line in decode_rtt_lines(
            retained.payload,
            retained.item_count,
        )] == ["first before ready"]
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_mcp_sidecar_lazily_registers_and_replays_first_real_rtt_batch(monkeypatch):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        assert raw.streams == []

        assert sidecar.enqueue(mcp_stream_bridge._DataCommand(
            "rtt",
            "plain target output\n",
        )) is True
        assert _wait_for(lambda: bool(raw.streams))
        assert producer.flush(timeout=1.0) is True
        assert [stream[1]["id"] for stream in raw.streams] == ["mklink.rtt"]

        endpoint = sidecar.endpoint("rtt")
        assert endpoint is not None
        with connect(
            endpoint,
            additional_headers={
                "Authorization": f"Bearer {sidecar.auth_token}",
            },
            open_timeout=5.0,
            close_timeout=0.2,
        ) as websocket:
            control = decode_frame(websocket.recv(timeout=5.0))
            retained = _receive_data_frame(websocket)

        assert control.stream_type is StreamType.CONTROL
        assert retained.stream_type is StreamType.RTT_RAW
        assert retained.flags == RTT_RAW_UTF8_LINES
        assert [line.text for line in decode_rtt_lines(
            retained.payload,
            retained.item_count,
        )] == ["plain target output"]
        assert any(
            kind == "stream.checkpoint"
            and payload.get("facts") == [{
                "name": "retained_replay",
                "value": True,
            }]
            for kind, payload in raw.events
        )
        assert "plain target output" not in repr(raw.events)
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_retained_replay_is_sent_only_to_the_new_subscriber(monkeypatch):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    monkeypatch.setattr(mcp_stream_bridge.McpStreamSidecar, "HEARTBEAT_SECONDS", 5.0)
    sidecar = mcp_stream_bridge.McpStreamSidecar()

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        assert sidecar.enqueue(mcp_stream_bridge._DataCommand(
            "rtt",
            "one retained batch\n",
        )) is True
        assert _wait_for(lambda: bool(raw.streams))
        endpoint = sidecar.endpoint("rtt")
        assert endpoint is not None
        headers = {"Authorization": f"Bearer {sidecar.auth_token}"}
        with (
            connect(endpoint, additional_headers=headers, open_timeout=5.0) as first,
            connect(endpoint, additional_headers=headers, open_timeout=5.0) as second,
        ):
            assert decode_frame(first.recv(timeout=5.0)).stream_type is StreamType.CONTROL
            first_retained = _receive_data_frame(first)
            assert decode_frame(second.recv(timeout=5.0)).stream_type is StreamType.CONTROL
            second_retained = _receive_data_frame(second)
            with pytest.raises(TimeoutError):
                first.recv(timeout=0.2)

        assert first_retained.payload == second_retained.payload
        assert first_retained.sequence == second_retained.sequence
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_mcp_sidecar_replays_superwatch_metadata_before_latest_sample(monkeypatch):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        assert sidecar.enqueue(mcp_stream_bridge._DataCommand(
            "superwatch",
            42.25,
            label="motor_speed",
        )) is True
        assert _wait_for(lambda: bool(raw.streams))

        endpoint = sidecar.endpoint("superwatch")
        assert endpoint is not None
        with connect(
            endpoint,
            additional_headers={
                "Authorization": f"Bearer {sidecar.auth_token}",
            },
            open_timeout=5.0,
            close_timeout=0.2,
        ) as websocket:
            control = decode_frame(websocket.recv(timeout=5.0))
            metadata = _receive_data_frame(websocket)
            sample = _receive_data_frame(websocket)

        assert control.stream_type is StreamType.CONTROL
        assert metadata.flags == SUPERWATCH_METADATA_JSON
        assert decode_superwatch_metadata(metadata.payload)["channels"] == [
            {"name": "motor_speed"},
        ]
        assert sample.flags == SUPERWATCH_SAMPLE_MAJOR_FLOAT32
        assert decode_waveform_samples(sample.payload, 1, 1) == ((42.25,),)
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_mcp_sidecar_real_observe_descriptor_is_private_and_scrubbed(monkeypatch, tmp_path):
    observe = pytest.importorskip("hil_core.observe")
    monkeypatch.setenv("HIL_OBSERVE_ROOT", str(tmp_path))
    sidecar = mcp_stream_bridge.McpStreamSidecar()

    assert sidecar.start(wait_timeout=1.0) is True
    assert sidecar.enqueue(mcp_stream_bridge._DataCommand(
        "rtt",
        "plain target output\n",
    )) is True
    assert _wait_for(lambda: "rtt" in sidecar._registered)
    assert observe_bridge.flush_process_observation(timeout=20.0) is True

    registry = observe.ObservationRegistry()
    session = next(
        item
        for item in registry.snapshot()["sessions"]
        if item["payload"].get("stream")
    )
    public_text = json.dumps(session, ensure_ascii=False)
    assert sidecar.auth_token not in public_text
    assert "127.0.0.1:" not in public_text
    private = registry.read_private_session(session["session_id"])
    assert private["streams"][0]["url"] == sidecar.endpoint("rtt")
    assert private["streams"][0]["headers"] == {
        "Authorization": f"Bearer {sidecar.auth_token}",
    }

    assert sidecar.stop(timeout=1.0) is True
    assert observe_bridge.flush_process_observation(timeout=20.0) is True
    assert observe_bridge.shutdown_process_observation(timeout=20.0) is True
    scrubbed = registry.read_private_session(session["session_id"])
    assert scrubbed["correlation"] is None
    assert scrubbed["streams"] == []


def test_memory_stream_chunks_one_read_with_one_private_operation_id(monkeypatch):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()
    monkeypatch.setattr(mcp_stream_bridge, "_SIDECAR", sidecar)
    data = bytes(index % 251 for index in range(300))

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        assert mcp_stream_bridge.publish_mcp_memory(
            "read", 0x20000000, data,
        ) is True
        assert _wait_for(lambda: len(sidecar._memory_retained) == 1)
        assert len(sidecar._memory_retained[0].chunks) == 2
        assert _wait_for(lambda: bool(raw.streams))

        endpoint = sidecar.endpoint("memory")
        assert endpoint is not None
        with connect(
            endpoint,
            additional_headers={
                "Authorization": f"Bearer {sidecar.auth_token}",
            },
            open_timeout=5.0,
            close_timeout=0.2,
        ) as websocket:
            assert decode_frame(websocket.recv(timeout=5.0)).stream_type is StreamType.CONTROL
            frames = [_receive_data_frame(websocket) for _ in range(2)]

        records = [decode_memory_record(frame.payload) for frame in frames]
        assert all(frame.stream_type is StreamType.MEMORY for frame in frames)
        assert all(frame.flags == MEMORY_JSON_V1 for frame in frames)
        assert all(frame.item_count == 1 for frame in frames)
        assert {record["operation_id"] for record in records} == {
            records[0]["operation_id"],
        }
        assert [record["operation"] for record in records] == ["read", "read"]
        assert [record["address"] for record in records] == [
            "0x20000000", "0x20000100",
        ]
        assert [record["offset"] for record in records] == [0, 256]
        assert [record["total_bytes"] for record in records] == [300, 300]
        assert [record["byte_count"] for record in records] == [256, 44]
        assert [record["region_index"] for record in records] == [0, 0]
        assert [record["region_count"] for record in records] == [1, 1]
        assert [record["sample_index"] for record in records] == [0, 0]
        assert [record["sample_count"] for record in records] == [1, 1]
        assert bytes.fromhex("".join(record["data_hex"] for record in records)) == data
        assert raw.streams[0][1]["id"] == "mklink.memory"
        public_text = repr(raw.events)
        assert records[0]["operation_id"] not in public_text
        assert data[:16].hex().upper() not in public_text
        assert records[0]["crc32"] not in public_text
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_memory_late_subscriber_replays_complete_batch_larger_than_client_legacy_limit(
    monkeypatch,
):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()
    monkeypatch.setattr(mcp_stream_bridge, "_SIDECAR", sidecar)
    chunk_count = sidecar.DEFAULT_CLIENT_QUEUE_CAPACITY + 1
    data = b"".join(bytes([index % 251]) * 256 for index in range(chunk_count))

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        assert mcp_stream_bridge.publish_mcp_memory(
            "read", 0x20000000, data,
        ) is True
        assert _wait_for(lambda: len(sidecar._memory_retained) == 1)
        assert sidecar._memory_queue.unfinished_tasks == 0
        retained = sidecar._memory_retained[0]
        assert len(retained.chunks) == chunk_count
        assert retained.byte_count == len(data)

        endpoint = sidecar.endpoint("memory")
        assert endpoint is not None
        with connect(
            endpoint,
            additional_headers={
                "Authorization": f"Bearer {sidecar.auth_token}",
            },
            open_timeout=5.0,
            close_timeout=0.2,
        ) as websocket:
            assert decode_frame(websocket.recv(timeout=5.0)).stream_type is StreamType.CONTROL
            frames = [
                _receive_data_frame(websocket)
                for _ in range(chunk_count)
            ]

        records = [decode_memory_record(frame.payload) for frame in frames]
        assert [record["offset"] for record in records] == [
            index * 256 for index in range(chunk_count)
        ]
        assert [frame.sequence for frame in frames] == list(
            range(frames[0].sequence, frames[0].sequence + chunk_count),
        )
        assert len({record["operation_id"] for record in records}) == 1
        assert bytes.fromhex(
            "".join(record["data_hex"] for record in records)
        ) == data
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_memory_mid_operation_subscriber_gets_prefix_then_live_suffix_and_late_replay(
    monkeypatch,
):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()
    operation_id = "op-0123456789abcdef"
    sample_count = 20

    def encode_sample(sample_index):
        sidecar._encode_memory(mcp_stream_bridge._MemoryTransfer(
            operation_id=operation_id,
            operation="dump",
            sample_index=sample_index,
            sample_count=sample_count,
            regions=(mcp_stream_bridge._MemoryRegion(
                0x20000000,
                bytes([sample_index]),
            ),),
        ))

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        for sample_index in range(7):
            encode_sample(sample_index)
        assert len(sidecar._memory_retained) == 0
        assert sidecar._memory_pending is not None
        assert sidecar._memory_pending.next_sample_index == 7

        endpoint = sidecar.endpoint("memory")
        assert endpoint is not None
        headers = {"Authorization": f"Bearer {sidecar.auth_token}"}
        with connect(
            endpoint,
            additional_headers=headers,
            open_timeout=5.0,
            close_timeout=0.2,
        ) as websocket:
            assert decode_frame(websocket.recv(timeout=5.0)).stream_type is StreamType.CONTROL
            prefix = [_receive_data_frame(websocket) for _ in range(7)]
            for sample_index in range(7, sample_count):
                encode_sample(sample_index)
            suffix = [
                _receive_data_frame(websocket)
                for _ in range(sample_count - 7)
            ]

        records = [
            decode_memory_record(frame.payload)
            for frame in (*prefix, *suffix)
        ]
        assert [record["sample_index"] for record in records] == list(
            range(sample_count),
        )
        assert {record["operation_id"] for record in records} == {operation_id}
        assert sidecar._memory_pending is None
        assert len(sidecar._memory_retained) == 1
        assert sidecar._memory_retained[0].sample_count == sample_count

        with connect(
            endpoint,
            additional_headers=headers,
            open_timeout=5.0,
            close_timeout=0.2,
        ) as late_websocket:
            assert decode_frame(
                late_websocket.recv(timeout=5.0)
            ).stream_type is StreamType.CONTROL
            replay = [
                decode_memory_record(
                    _receive_data_frame(late_websocket).payload
                )
                for _ in range(sample_count)
            ]
        assert [record["sample_index"] for record in replay] == list(
            range(sample_count),
        )
        assert {record["operation_id"] for record in replay} == {operation_id}
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_memory_queue_accepts_one_burst_of_sixty_four_samples(monkeypatch):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()
    monkeypatch.setattr(mcp_stream_bridge, "_SIDECAR", sidecar)
    original_encode = sidecar._encode_memory
    encoder_started = threading.Event()
    release_encoder = threading.Event()

    def block_first_sample(value):
        if value.sample_index == 0:
            encoder_started.set()
            assert release_encoder.wait(timeout=2.0)
        original_encode(value)

    monkeypatch.setattr(sidecar, "_encode_memory", block_first_sample)
    operation_id = "op-0123456789abcdef"

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        assert mcp_stream_bridge.publish_mcp_memory_regions(
            "dump",
            [(0x20000000, b"0")],
            sample_index=0,
            sample_count=64,
            operation_id=operation_id,
        ) is True
        assert encoder_started.wait(timeout=1.0)
        accepted = [
            mcp_stream_bridge.publish_mcp_memory_regions(
                "dump",
                [(0x20000000, bytes([sample_index]))],
                sample_index=sample_index,
                sample_count=64,
                operation_id=operation_id,
            )
            for sample_index in range(1, 64)
        ]
        assert all(accepted)
        assert sidecar._memory_queue.unfinished_tasks == 64
        assert sidecar._input_dropped == {}

        release_encoder.set()
        assert _wait_for(
            lambda: sidecar._memory_queue.unfinished_tasks == 0,
            timeout=5.0,
        )
        assert len(sidecar._memory_retained) == 1
        retained = sidecar._memory_retained[0]
        assert retained.sample_count == 64
        assert len(retained.chunks) == 64
        assert [
            decode_memory_record(chunk.payload)["sample_index"]
            for chunk in retained.chunks
        ] == list(range(64))
    finally:
        release_encoder.set()
        sidecar.stop(timeout=2.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_memory_retention_evicts_only_complete_old_batches():
    sidecar = mcp_stream_bridge.McpStreamSidecar()

    for index in range(sidecar.MEMORY_RETAINED_BATCHES + 2):
        sidecar._encode_memory(mcp_stream_bridge._MemoryTransfer(
            operation_id=f"op-{index:016x}",
            operation="read",
            sample_index=0,
            sample_count=1,
            regions=(mcp_stream_bridge._MemoryRegion(
                0x20000000,
                bytes([index]) * 300,
            ),),
        ))

    assert len(sidecar._memory_retained) == sidecar.MEMORY_RETAINED_BATCHES
    assert all(len(batch.chunks) == 2 for batch in sidecar._memory_retained)
    retained_operation_ids = [
        [
            decode_memory_record(chunk.payload)["operation_id"]
            for chunk in batch.chunks
        ]
        for batch in sidecar._memory_retained
    ]
    expected_ids = [
        f"op-{index:016x}"
        for index in range(2, sidecar.MEMORY_RETAINED_BATCHES + 2)
    ]
    assert retained_operation_ids == [[operation_id] * 2 for operation_id in expected_ids]
    assert [
        decode_memory_record(chunk.payload)["offset"]
        for chunk in sidecar._initial_batches("memory")
    ] == [0, 256] * sidecar.MEMORY_RETAINED_BATCHES


def test_memory_batch_retention_and_client_queue_have_exact_resource_bounds():
    sidecar = mcp_stream_bridge.McpStreamSidecar()
    sample_count = 62
    per_sample_bytes = sidecar.MAX_MEMORY_TRANSFER_BYTES // sample_count
    final_region_size = per_sample_bytes - mcp_stream_bridge.MAX_MEMORY_REGIONS + 1
    regions = tuple(
        mcp_stream_bridge._MemoryRegion(0x20000000 + index, b"x")
        for index in range(mcp_stream_bridge.MAX_MEMORY_REGIONS - 1)
    ) + (mcp_stream_bridge._MemoryRegion(0x21000000, b"x" * final_region_size),)

    for sample_index in range(sample_count):
        sidecar._encode_memory(mcp_stream_bridge._MemoryTransfer(
            operation_id="op-0123456789abcdef",
            operation="dump",
            sample_index=sample_index,
            sample_count=sample_count,
            regions=regions,
        ))

    assert sidecar.MEMORY_CLIENT_QUEUE_CAPACITY == 2559
    assert sidecar._memory_retained.maxlen == sidecar.MEMORY_RETAINED_BATCHES == 16
    assert len(sidecar._memory_retained) == 1
    retained = sidecar._memory_retained[0]
    assert retained.sample_count == sample_count
    assert retained.byte_count == per_sample_bytes * sample_count
    assert retained.byte_count <= sidecar.MAX_MEMORY_TRANSFER_BYTES
    assert len(retained.chunks) == 2542
    assert len(retained.chunks) <= sidecar.MEMORY_CLIENT_QUEUE_CAPACITY
    assert sidecar._memory_queue.maxsize == mcp_stream_bridge.MAX_MEMORY_SAMPLES
    assert sidecar._data_queue.maxsize == sidecar.DATA_QUEUE_CAPACITY == 8

    async def queue_limits():
        memory_client = sidecar._hubs["memory"].subscribe()
        other_clients = [
            sidecar._hubs[name].subscribe()
            for name in sidecar._hubs
            if name != "memory"
        ]
        try:
            assert memory_client.maxsize == sidecar.MEMORY_CLIENT_QUEUE_CAPACITY
            assert all(
                client.maxsize == sidecar.DEFAULT_CLIENT_QUEUE_CAPACITY
                for client in other_clients
            )
        finally:
            sidecar._hubs["memory"].unsubscribe(memory_client)
            for name, client in zip(
                (name for name in sidecar._hubs if name != "memory"),
                other_clients,
            ):
                sidecar._hubs[name].unsubscribe(client)

    asyncio.run(queue_limits())


def test_memory_dump_uses_region_local_offsets_and_explicit_operation_id(monkeypatch):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()
    monkeypatch.setattr(mcp_stream_bridge, "_SIDECAR", sidecar)

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        assert mcp_stream_bridge.publish_mcp_memory_regions(
            "dump",
            [(0x20000000, b"private")],
            sample_index=5,
            sample_count=6,
            operation_id="op-ABCDEF0123456789",
        ) is False
        assert mcp_stream_bridge.publish_mcp_memory_regions(
            "dump",
            [(0x20000000, b"private")],
            sample_index=6,
            sample_count=6,
            operation_id="op-0123456789abcdef",
        ) is False
        assert mcp_stream_bridge.publish_mcp_memory_regions(
            "dump",
            [(0x20000000, b"private")],
            sample_index=0,
            sample_count=65,
            operation_id="op-0123456789abcdef",
        ) is False
        assert mcp_stream_bridge.publish_mcp_memory_regions(
            "dump",
            [(0x20000000, b"A" * 300), (0x1_00000000, b"B")],
            sample_index=0,
            sample_count=1,
            operation_id="op-0123456789abcdef",
        ) is True
        assert _wait_for(lambda: len(sidecar._memory_retained) == 1)
        assert len(sidecar._memory_retained[0].chunks) == 3

        endpoint = sidecar.endpoint("memory")
        assert endpoint is not None
        with connect(
            endpoint,
            additional_headers={
                "Authorization": f"Bearer {sidecar.auth_token}",
            },
            open_timeout=5.0,
            close_timeout=0.2,
        ) as websocket:
            assert decode_frame(websocket.recv(timeout=5.0)).stream_type is StreamType.CONTROL
            records = [
                decode_memory_record(_receive_data_frame(websocket).payload)
                for _ in range(3)
            ]

        assert [record["operation_id"] for record in records] == [
            "op-0123456789abcdef",
        ] * 3
        assert [record["sample_index"] for record in records] == [0, 0, 0]
        assert [record["sample_count"] for record in records] == [1, 1, 1]
        assert [record["region_index"] for record in records] == [0, 0, 1]
        assert [record["region_count"] for record in records] == [2, 2, 2]
        assert [record["offset"] for record in records] == [0, 256, 0]
        assert [record["total_bytes"] for record in records] == [300, 300, 1]
        assert [record["address"] for record in records] == [
            "0x20000000",
            "0x20000100",
            "0x0000000100000000",
        ]
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_memory_tail_queue_drop_emits_gap_without_a_later_success(monkeypatch):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()
    monkeypatch.setattr(mcp_stream_bridge, "_SIDECAR", sidecar)

    def full(_command):
        raise mcp_stream_bridge.queue.Full

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        monkeypatch.setattr(sidecar._memory_queue, "put_nowait", full)
        assert mcp_stream_bridge.publish_mcp_memory(
            "read", 0x20000000, b"tail-drop",
        ) is False
        assert producer.flush(timeout=1.0) is True
        gaps = [payload for kind, payload in raw.events if kind == "stream.gap"]
        assert gaps == [{
            "stream": {
                "id": "mklink.memory",
                "protocol": "websocket",
                "state": "degraded",
                "media_type": "application/vnd.mklink.mkst",
                "encoding": "mkst-v1",
            },
            "facts": [{
                "name": "sidecar_dropped_batches",
                "value": 1,
                "unit": "batches",
            }],
        }]
        assert "tail-drop" not in repr(raw.events)
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_memory_publish_drop_gap_is_degraded_and_contains_only_a_count(monkeypatch):
    raw = _Producer()
    producer = observe_bridge.SafeProducer(raw)
    monkeypatch.setattr(mcp_stream_bridge, "process_producer", lambda: producer)
    sidecar = mcp_stream_bridge.McpStreamSidecar()

    try:
        assert sidecar.start(wait_timeout=1.0) is True
        assert sidecar.publish_memory_gap("publish_drop_count", 2) is True
        assert producer.flush(timeout=1.0) is True

        gaps = [payload for kind, payload in raw.events if kind == "stream.gap"]
        assert gaps == [{
            "stream": {
                "id": "mklink.memory",
                "protocol": "websocket",
                "state": "degraded",
                "media_type": "application/vnd.mklink.mkst",
                "encoding": "mkst-v1",
            },
            "facts": [{
                "name": "publish_drop_count",
                "value": 2,
                "unit": "count",
            }],
        }]
    finally:
        sidecar.stop(timeout=1.0)
        producer.flush(timeout=1.0)
        producer.close(flush_timeout=1.0)


def test_memory_commands_hide_private_bytes_from_repr():
    region = mcp_stream_bridge._MemoryRegion(0x20000000, b"private-target-bytes")
    transfer = mcp_stream_bridge._MemoryTransfer(
        "op-0123456789abcdef",
        "read",
        0,
        1,
        (region,),
    )
    command = mcp_stream_bridge._DataCommand("memory", transfer)

    assert "private-target-bytes" not in repr(region)
    assert "op-0123456789abcdef" not in repr(transfer)
    assert "private-target-bytes" not in repr(command)
