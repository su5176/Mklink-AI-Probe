import asyncio
from pathlib import Path
import struct
import time

import pytest

from mklink.remote.dashboards import SystemViewStreamManager
from mklink.remote.stream_hub import StreamHub
from mklink.remote.stream_protocol import (
    SYSTEMVIEW_EVENT_RECORD_SIZE,
    decode_systemview_events,
    encode_systemview_events,
)


def _events(count: int) -> list[dict]:
    return [
        {
            "kind": "task_start_exec" if index % 2 == 0 else "task_stop_exec",
            "task_id": 0x20000000 + index % 4,
            "t_ticks": index * 72,
            "t_us": float(index),
            "cpu_delta_us": 1.0,
        }
        for index in range(count)
    ]


def test_systemview_status_exposes_binary_stream_stats():
    hub = StreamHub(max_batches_per_client=4)
    manager = SystemViewStreamManager(stream_hub=hub)

    assert manager.get_status()["stream"] == hub.stats().__dict__


def test_systemview_fixed_records_round_trip_and_reject_malformed_payload():
    events = _events(3)
    payload = encode_systemview_events(events)

    assert len(payload) == 3 * SYSTEMVIEW_EVENT_RECORD_SIZE
    decoded = decode_systemview_events(payload)
    assert [event["kind"] for event in decoded] == [event["kind"] for event in events]
    assert [event["task_id"] for event in decoded] == [event["task_id"] for event in events]
    assert [event["t_ticks"] for event in decoded] == [event["t_ticks"] for event in events]
    assert [event["t_us"] for event in decoded] == [event["t_us"] for event in events]

    with pytest.raises(ValueError, match="multiple"):
        decode_systemview_events(payload[:-1])

    malformed_flags = bytearray(payload[:SYSTEMVIEW_EVENT_RECORD_SIZE])
    malformed_flags[1] = 0x80
    with pytest.raises(ValueError, match="malformed"):
        decode_systemview_events(malformed_flags)


def test_systemview_stack_metadata_round_trips_in_fixed_records():
    [decoded] = decode_systemview_events(encode_systemview_events([{
        "kind": "stack_info",
        "task_id": 0x825B0,
        "stack_base": 0x83000,
        "stack_size": 1024,
        "t_ticks": 12,
    }]))

    assert decoded["task_id"] == 0x825B0
    assert decoded["stack_base"] == 0x83000
    assert decoded["stack_size"] == 1024


@pytest.mark.parametrize("flags", [0, 0x07], ids=["flags-off", "flags-on"])
@pytest.mark.parametrize("slot_offset", [16, 24, 32, 40], ids=["time", "delta", "aux0", "aux1"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "pos-inf", "neg-inf"])
def test_decoder_rejects_non_finite_double_slots_regardless_of_flags(
    flags, slot_offset, value
):
    payload = bytearray(encode_systemview_events([
        {"kind": "task_info", "task_id": 1, "t_ticks": 1, "t_us": 1.0,
         "cpu_delta_us": 0.5, "prio": 3, "stack_size": 1024}
    ]))
    payload[1] = flags
    struct.pack_into("<d", payload, slot_offset, value)

    with pytest.raises(ValueError, match="finite"):
        decode_systemview_events(payload)


def test_unknown_systemview_kind_is_rejected_instead_of_silently_corrupted():
    with pytest.raises(ValueError, match="unknown SystemView event kind"):
        encode_systemview_events([{"kind": "future_event", "t_ticks": 1}])

    with pytest.raises(ValueError, match="context id must be an unsigned 32-bit integer"):
        encode_systemview_events([{"kind": "task_start_exec", "task_id": 1.5}])



@pytest.mark.parametrize("field", ["t_us", "cpu_delta_us", "prio", "stack_size"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_encoder_rejects_non_finite_double_fields(field, value):
    with pytest.raises(ValueError, match="finite"):
        encode_systemview_events([{"kind": "task_info", "task_id": 1, field: value}])


def test_length_prefixed_parser_events_use_a_generic_fixed_record():
    payload = encode_systemview_events([
        {"kind": "raw_512", "event_id": 512, "t_ticks": 9, "t_us": 1.5}
    ])

    assert decode_systemview_events(payload) == [
        {"kind": "raw_512", "t_ticks": 9, "t_us": 1.5, "event_id": 512}
    ]


def test_recording_precedes_bounded_live_publication_and_keeps_all_events():
    async def scenario():
        hub = StreamHub(max_batches_per_client=8)
        queue = hub.subscribe()
        manager = SystemViewStreamManager(stream_hub=hub)
        recorded: list[dict] = []

        class Recorder:
            def write_events(self, events):
                recorded.extend(events)

        manager._recording = Recorder()
        manager._process_events(_events(1200), now=123.0)
        await asyncio.sleep(0)

        batches = []
        while not queue.empty():
            batches.append(queue.get_nowait())
            queue.task_done()
        hub.unsubscribe(queue)

        assert len(recorded) == 1200
        assert sum(batch.item_count for batch in batches) == 1200
        assert all(batch.item_count <= manager._live_batch_limit for batch in batches)
        assert [batch.sequence for batch in batches] == sorted(
            batch.sequence for batch in batches
        )
        assert all(
            len(batch.payload) == batch.item_count * SYSTEMVIEW_EVENT_RECORD_SIZE
            for batch in batches
        )
        assert sum(len(decode_systemview_events(batch.payload)) for batch in batches) == 1200

    asyncio.run(scenario())


def test_slow_browser_drops_live_batches_without_truncating_recording():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        queue = hub.subscribe()
        manager = SystemViewStreamManager(stream_hub=hub)
        recorded: list[dict] = []

        class Recorder:
            def write_events(self, events):
                recorded.extend(events)

        manager._recording = Recorder()
        manager._process_events(_events(1200), now=123.0)
        await asyncio.sleep(0)

        stats = hub.stats()
        latest = queue.get_nowait()
        queue.task_done()
        hub.unsubscribe(queue)
        assert len(recorded) == 1200
        assert stats.produced_items == 1200
        assert stats.dropped_items == 1200 - latest.item_count
        assert latest.sequence == stats.last_sequence

    asyncio.run(scenario())


def test_recording_is_explicit_and_can_stop_without_stopping_trace(tmp_path):
    manager = SystemViewStreamManager()
    manager._parser = manager._create_parser()
    manager._running = True
    manager._recording_device = type(
        "Device", (), {"_project_root": str(tmp_path)}
    )()
    manager._recording_meta = {"addr": "0x000870A4", "channel": 1, "mode": 0}

    assert manager.get_status()["recording"] is False
    started = manager.start_recording()
    path = Path(started["recording_path"])
    assert started["recording"] is True

    manager._process_events(_events(3), now=123.0)
    stopped = manager.stop_recording()

    assert stopped["recording"] is False
    assert manager.running is True
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.count('"type":"event"') == 3


def test_status_separates_target_overflow_from_parser_framing_drops():
    manager = SystemViewStreamManager()
    manager._parser = manager._create_parser()

    manager._process_events([
        {"kind": "overflow", "drop_count": 100, "t_ticks": 1},
        {"kind": "overflow", "drop_count": 107, "t_ticks": 2},
    ], now=123.0)

    status = manager.get_status()
    assert status["target_overflow_events"] == 2
    assert status["target_drop_count_baseline"] == 100
    assert status["target_drop_count"] == 107
    assert status["target_dropped_packets_since_baseline"] == 7
    assert status["parser_dropped_bytes"] == 0
    assert status["parser_dropped_packets"] == 0


def test_protocol_task_names_disable_disruptive_live_ram_name_resolution():
    manager = SystemViewStreamManager()
    manager._parser = manager._create_parser()
    manager._parser._task_names[0x20001000] = "main"

    unknown = manager._unknown_task_ids([
        {"kind": "task_start_exec", "task_id": 0x20002000},
    ])

    assert unknown == set()


def test_raw_bytes_without_decodable_events_fail_with_sync_diagnostic():
    class Device:
        def __init__(self):
            self.stop_calls = 0
            self.read_durations = []

        def systemview_start(self, *_args, **_kwargs):
            return {}

        def systemview_read_bytes(self, **kwargs):
            self.read_durations.append(kwargs["duration"])
            time.sleep(0.002)
            return b"\x01" * 64

        def systemview_stop(self):
            self.stop_calls += 1

    device = Device()
    manager = SystemViewStreamManager()

    class Parser:
        synced = False
        abs_time = 0
        cpu_freq = 0
        dropped_bytes = 0
        dropped_packets = 0
        _task_names = {}
        _isr_names = {}

        @staticmethod
        def feed(_raw):
            return []

    manager._create_parser = lambda _device=None: Parser()
    manager._startup_progress_timeout_s = 0.01
    manager._startup_progress_min_bytes = 128
    manager._start_recording = lambda *_args, **_kwargs: None
    failure = []
    manager.set_start_failure_callback(failure.append)

    manager.start(device)
    deadline = time.monotonic() + 1.0
    while manager.running and time.monotonic() < deadline:
        time.sleep(0.005)

    status = manager.get_status()
    assert status["running"] is False
    assert status["progress_state"] == "error"
    assert "no decodable events" in status["progress_error"]
    assert "sync" in status["progress_error"].lower()
    assert status["raw_bytes_without_events"] >= 128
    assert device.stop_calls == 1
    assert device.read_durations
    assert all(duration == pytest.approx(1.0 / 30.0)
               for duration in device.read_durations)
    assert failure and failure[0] is not None


def test_no_systemview_bytes_fail_instead_of_staying_in_starting_forever():
    class Device:
        def __init__(self):
            self.stop_calls = 0

        def systemview_start(self, *_args, **_kwargs):
            return {"control_block_addr": "0x0008E488"}

        def systemview_read_bytes(self, **_kwargs):
            return b""

        def systemview_stop(self):
            self.stop_calls += 1

    device = Device()
    manager = SystemViewStreamManager()
    manager._startup_no_data_timeout_s = 0.02
    manager._start_recording = lambda *_args, **_kwargs: None

    manager.start(device)
    deadline = time.monotonic() + 1.0
    while manager.running and time.monotonic() < deadline:
        time.sleep(0.005)

    status = manager.get_status()
    assert status["running"] is False
    assert status["progress_state"] == "error"
    assert "未收到数据" in status["progress_error"]
    assert device.stop_calls >= 1
