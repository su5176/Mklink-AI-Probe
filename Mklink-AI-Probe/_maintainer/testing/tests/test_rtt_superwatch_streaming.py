import asyncio
import binascii
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mklink.remote.dashboards import (
    RttStreamManager,
    SuperWatchStreamManager,
    normalize_superwatch_interval,
)
from mklink.dump_memory import MAGIC
from mklink.superwatch import (
    ReadBlock,
    SuperWatchRuntime,
    WatchItem,
    compile_frame_decoder,
)
from mklink.remote.stream_hub import StreamHub
from mklink.remote.stream_protocol import (
    RTT_RAW_UTF8_LINES,
    RTT_TERMINAL_UTF8,
    SUPERWATCH_METADATA_JSON,
    SUPERWATCH_SAMPLE_MAJOR_FLOAT32,
    SUPERWATCH_TIMESTAMPED_FLOAT32,
    StreamType,
    decode_rtt_lines,
    decode_superwatch_metadata,
    decode_waveform_samples,
)


def _drain(loop, turns=4):
    for _ in range(turns):
        loop.run_until_complete(asyncio.sleep(0))


def test_superwatch_dashboard_template_defaults_interval_to_1ms():
    from mklink.rtt_viewer import _build_dashboard_html

    superwatch_html = _build_dashboard_html(mode="SuperWatch")
    vofa_html = _build_dashboard_html(mode="VOFA")

    assert 'id="interval-input" value="0.001"' in superwatch_html
    assert 'id="interval-input" value="0"' in vofa_html


@pytest.mark.parametrize("interval", [0.00001, 0.001, 60.0])
def test_superwatch_interval_accepts_supported_range(interval):
    assert normalize_superwatch_interval(interval) == pytest.approx(interval)


@pytest.mark.parametrize(
    "interval", [float("nan"), float("inf"), -float("inf"), 0, 0.000009, 60.000001],
)
def test_superwatch_interval_rejects_values_outside_10us_to_60s(interval):
    manager = SuperWatchStreamManager()
    before = manager.get_status()["interval"]

    with pytest.raises(ValueError, match="SuperWatch interval"):
        manager.set_interval(interval)

    assert manager.get_status()["interval"] == before


def _watch_item(name, address):
    return SimpleNamespace(
        name=name, type_name="float", size=4, address=address,
        source="ram", enum_values=None, metadata={},
    )


def test_compiled_superwatch_decoder_reuses_one_numeric_row_without_slices_or_dicts():
    items = [
        WatchItem("signed", 0x20000000, "int16_t", 2, scalar_kind="signed"),
        WatchItem("value", 0x20000004, "float", 4, scalar_kind="float"),
        WatchItem("flag", 0x20000100, "bool", 1, scalar_kind="bool"),
    ]
    blocks = [
        ReadBlock(0x20000000, 8, items[:2]),
        ReadBlock(0x20000100, 1, items[2:]),
    ]
    decoder = compile_frame_decoder(items, blocks)
    first = decoder.decode({
        "regions": [
            (0, struct.pack("<hxxf", -7, 1.25)),
            (1, b"\x01"),
        ],
    })
    second = decoder.decode({
        "regions": [
            (0, struct.pack("<hxxf", 8, 2.5)),
            (1, b"\x00"),
        ],
    })

    assert first is second
    assert second == pytest.approx([8.0, 2.5, 0.0])
    assert decoder.channel_index == {"signed": 0, "value": 1, "flag": 2}
    assert decoder.decode({"regions": [(0, b"\x00" * 8)]}) is None


def test_superwatch_dump_layout_never_spans_address_holes():
    items = [
        WatchItem("a", 0x20000000, "float", 4),
        WatchItem("touching", 0x20000004, "float", 4),
        WatchItem("b", 0x20000014, "float", 4),  # 16-byte hole
        WatchItem("c", 0x20000058, "float", 4),  # 64-byte hole
        WatchItem("d", 0x2000015C, "float", 4),  # 256-byte hole
    ]
    runtime = SuperWatchRuntime(items=items)

    assert [(block.address, block.size) for block in runtime.blocks] == [
        (0x20000000, 8),
        (0x20000014, 4),
        (0x20000058, 4),
        (0x2000015C, 4),
    ]


def test_superwatch_binary_batch_uses_sample_byte_and_latency_limits():
    now = [0.0]
    hub = Mock()
    manager = SuperWatchStreamManager(
        stream_hub=hub,
        batch_samples=1000,
        batch_bytes=1024,
        batch_max_latency=0.020,
        clock=lambda: now[0],
    )
    manager._runtime = SimpleNamespace(items=[SimpleNamespace(name="a")])

    assert manager.publish_sample_points([{"_t": 0.0, "a": 1.0}])
    assert not [call for call in hub.publish.call_args_list if call.kwargs.get("item_count")]
    now[0] = 0.021
    assert manager.publish_sample_points([{"_t": 0.001, "a": 2.0}])
    sample_calls = [call for call in hub.publish.call_args_list if call.kwargs.get("item_count")]
    assert len(sample_calls) == 1
    assert sample_calls[0].kwargs["item_count"] == 2

    hub.reset_mock()
    now[0] = 1.0
    manager = SuperWatchStreamManager(
        stream_hub=hub,
        batch_samples=1000,
        batch_bytes=1024,
        batch_max_latency=1.0,
        clock=lambda: now[0],
    )
    manager._runtime = SimpleNamespace(items=[
        SimpleNamespace(name=f"v{index}") for index in range(16)
    ])
    row = {f"v{index}": float(index) for index in range(16)}
    for sample in range(15):
        assert manager.publish_sample_points([{"_t": sample / 1000, **row}])
    sample_calls = [call for call in hub.publish.call_args_list if call.kwargs.get("item_count")]
    assert len(sample_calls) == 1
    assert sample_calls[0].kwargs["item_count"] == 15


def test_superwatch_low_rate_sample_flushes_immediately():
    hub = Mock()
    manager = SuperWatchStreamManager(
        stream_hub=hub, batch_samples=512, batch_max_latency=0.020,
    )
    manager._runtime = SimpleNamespace(items=[SimpleNamespace(name="a")])
    manager.set_interval(0.1)

    assert manager.publish_sample_points([{"_t": 0.0, "a": 1.0}])
    sample_calls = [call for call in hub.publish.call_args_list if call.kwargs.get("item_count")]
    assert len(sample_calls) == 1
    assert sample_calls[0].kwargs["item_count"] == 1


class _MutableWatchRuntime:
    def __init__(self):
        self.items = [_watch_item("a", 0x20000000)]
        self._rebuild_blocks()

    def _rebuild_blocks(self):
        self.blocks = [ReadBlock(
            address=self.items[0].address,
            size=(self.items[-1].address + self.items[-1].size) - self.items[0].address,
            items=list(self.items),
        )] if self.items else []

    def add(self, name):
        if name == "b" and all(item.name != name for item in self.items):
            self.items.append(_watch_item("b", 0x20000004))
        self._rebuild_blocks()
        return {"name": name}

    def remove(self, name):
        self.items = [item for item in self.items if item.name != name]
        self._rebuild_blocks()
        return {"removed": True, "name": name}


class _SuperWatchDumpBridge:
    def _enter_stream(self, _state):
        pass

    def _write_raw(self, _data):
        pass

    def drain_stream_bytes(self, max_bytes=None):
        return b""

    def _exit_stream(self):
        return ""


class _RecordingHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._sequence = 0
        self.batches = []
        self.callback = None

    def set_subscribe_callback(self, callback):
        self.callback = callback

    def publish(self, payload, *, item_count, flags=0, stream_type=None):
        with self._lock:
            self._sequence += 1
            self.batches.append(SimpleNamespace(
                payload=bytes(payload), item_count=item_count, flags=flags,
                stream_type=stream_type, sequence=self._sequence,
            ))
            return self._sequence

    def snapshot(self):
        with self._lock:
            return list(self.batches)

    def stats(self):
        return SimpleNamespace()


def test_rtt_arbitrary_chunks_preserve_utf8_crlf_and_partial_tail():
    async def scenario():
        hub = StreamHub(max_batches_per_client=8)
        queue = hub.subscribe()
        manager = RttStreamManager(stream_hub=hub, raw_batch_lines=2)

        encoded = "alpha\r\n温度=25\nlast".encode("utf-8")
        split_inside_multibyte = encoded.index("温".encode("utf-8")) + 1
        manager.feed_rtt_bytes(encoded[:split_inside_multibyte])
        manager.feed_rtt_bytes(encoded[split_inside_multibyte:-2])
        manager.feed_rtt_bytes(encoded[-2:])
        manager.flush_pending(final=True)

        first = await queue.get()
        second = await queue.get()
        assert first.stream_type is StreamType.RTT_RAW
        assert first.flags == RTT_RAW_UTF8_LINES
        assert [line.text for line in decode_rtt_lines(first.payload, first.item_count)] == [
            "alpha", "温度=25",
        ]
        assert [line.text for line in decode_rtt_lines(second.payload, second.item_count)] == [
            "last",
        ]
        assert all(line.timestamp_ns > 0 for line in decode_rtt_lines(first.payload, 2))
        hub.unsubscribe(queue)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("encoding", "text"),
    [("gbk", "中文输出"), ("gb18030", "扩展字符𠀀")],
)
def test_rtt_chinese_encoding_preserves_characters_split_across_chunks(
    encoding, text,
):
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        queue = hub.subscribe()
        manager = RttStreamManager(stream_hub=hub, raw_batch_lines=8)
        manager.set_encoding(encoding)
        encoded = f"{text}\n".encode(encoding)

        manager.feed_rtt_bytes(encoded[:1])
        manager.feed_rtt_bytes(encoded[1:-1])
        manager.feed_rtt_bytes(encoded[-1:])
        manager.flush_pending(final=True)

        batch = await queue.get()
        assert [line.text for line in decode_rtt_lines(batch.payload, 1)] == [text]
        assert manager.get_status()["encoding"] == encoding
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_rtt_runtime_encoding_switch_only_changes_future_bytes():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        queue = hub.subscribe()
        manager = RttStreamManager(stream_hub=hub, raw_batch_lines=8)

        manager.feed_rtt_bytes("旧编码\n".encode("utf-8"))
        assert manager.set_encoding("gbk") == "gbk"
        manager.feed_rtt_bytes("新编码\n".encode("gbk"))
        manager.flush_pending(final=True)

        batch = await queue.get()
        assert [line.text for line in decode_rtt_lines(batch.payload, 2)] == [
            "旧编码", "新编码",
        ]
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_rtt_rejects_unsupported_text_encoding():
    manager = RttStreamManager()

    with pytest.raises(ValueError, match="Unsupported RTT encoding"):
        manager.set_encoding("shift-jis")


def test_rtt_invalid_utf8_is_replaced_and_empty_final_tail_is_not_emitted():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        queue = hub.subscribe()
        manager = RttStreamManager(stream_hub=hub, raw_batch_lines=8)
        manager.feed_rtt_bytes(b"bad:\xff\n")
        manager.flush_pending(final=True)
        batch = await queue.get()
        assert [line.text for line in decode_rtt_lines(batch.payload, 1)] == ["bad:\ufffd"]
        assert hub.stats().produced_items == 2
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_rtt_raw_records_preserve_whitespace_and_empty_line_boundaries():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        queue = hub.subscribe()
        manager = RttStreamManager(stream_hub=hub, raw_batch_lines=8)
        manager.feed_rtt_bytes(b"  padded  \r\n\n")
        manager.flush_pending(final=True)
        batch = await queue.get()
        assert [line.text for line in decode_rtt_lines(batch.payload, 2)] == [
            "  padded  ", "",
        ]
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_rtt_poll_preserves_whitespace_only_device_chunks():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        queue = hub.subscribe()
        manager = RttStreamManager(stream_hub=hub, raw_batch_lines=8)
        read_once = threading.Event()

        class Device:
            def rtt_start(self, *_args, **_kwargs):
                pass

            def rtt_read(self, **_kwargs):
                if not read_once.is_set():
                    read_once.set()
                    return b"  \n\n"
                time.sleep(0.001)
                return b""

        manager.start(Device())
        assert await asyncio.to_thread(read_once.wait, 1.0)
        await asyncio.to_thread(manager.stop)
        batch = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert [line.text for line in decode_rtt_lines(batch.payload, 2)] == ["  ", ""]
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_rtt_parsed_numeric_rows_can_publish_waveform_batches():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        queue = hub.subscribe()
        manager = RttStreamManager(
            stream_hub=hub, raw_batch_lines=8, waveform_batch_samples=2,
        )
        manager.feed_rtt_bytes(b"a=1 b=2\na=3 b=4\n")
        raw = await queue.get()
        waveform = await queue.get()
        assert raw.stream_type is StreamType.RTT_RAW
        assert waveform.stream_type is StreamType.WAVEFORM
        assert decode_waveform_samples(waveform.payload, 2, 2) == ((1.0, 2.0), (3.0, 4.0))
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_rtt_auto_detects_csv_rows_before_publishing_waveform_batches():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        queue = hub.subscribe()
        manager = RttStreamManager(
            stream_hub=hub, raw_batch_lines=8, waveform_batch_samples=2,
        )
        manager.feed_rtt_bytes(b"1,2\n3,4\n")
        raw = await asyncio.wait_for(queue.get(), timeout=0.1)
        waveform = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert raw.stream_type is StreamType.RTT_RAW
        assert waveform.stream_type is StreamType.WAVEFORM
        assert decode_waveform_samples(waveform.payload, 2, 2) == (
            (1.0, 2.0), (3.0, 4.0),
        )
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_rtt_auto_detection_keeps_majority_kv_format_when_marker_is_in_probe_window():
    hub = _RecordingHub()
    manager = RttStreamManager(
        stream_hub=hub, raw_batch_lines=32, waveform_batch_samples=32,
    )

    manager.feed_rtt_bytes(b"temp=20,speed=80\n" * 9)
    manager.feed_rtt_bytes(b"M,9,9,0\n")
    manager.feed_rtt_bytes(b"temp=21,speed=81\ntemp=22,speed=82\n")
    manager.flush_pending()

    waveform = [
        batch for batch in hub.snapshot()
        if batch.stream_type is StreamType.WAVEFORM
    ]
    assert manager.get_status()["numeric_channels"] == ["speed", "temp"]
    assert sum(batch.item_count for batch in waveform) == 11
    assert decode_waveform_samples(waveform[0].payload, 11, 2)[-2:] == (
        (81.0, 21.0), (82.0, 22.0),
    )


def test_rtt_default_batches_keep_12khz_dual_stream_below_100_frames_per_second():
    hub = _RecordingHub()
    manager = RttStreamManager(stream_hub=hub)

    manager.feed_rtt_bytes(b"1,2,3,4\n" * 12_000)
    manager.flush_pending()

    batches = hub.snapshot()
    raw = [
        batch for batch in batches
        if batch.stream_type is StreamType.RTT_RAW
        and batch.flags == RTT_RAW_UTF8_LINES
    ]
    waveform = [batch for batch in batches if batch.stream_type is StreamType.WAVEFORM]
    assert sum(batch.item_count for batch in raw) == 12_000
    assert sum(batch.item_count for batch in waveform) == 12_000
    assert len(raw) + len(waveform) <= 100


def test_rtt_marker_lines_do_not_reset_stable_csv_waveform_layout():
    hub = _RecordingHub()
    manager = RttStreamManager(
        stream_hub=hub, raw_batch_lines=256, waveform_batch_samples=256,
    )

    for index in range(3):
        manager.feed_rtt_bytes(b"1,2,3,4\n" * 120)
        manager.feed_rtt_bytes(f"M,{index},{index},0\n".encode())
    manager.flush_pending()

    batches = hub.snapshot()
    raw = [
        batch for batch in batches
        if batch.stream_type is StreamType.RTT_RAW
        and batch.flags == RTT_RAW_UTF8_LINES
    ]
    waveform = [batch for batch in batches if batch.stream_type is StreamType.WAVEFORM]
    assert sum(batch.item_count for batch in raw) == 363
    assert sum(batch.item_count for batch in waveform) == 360
    assert manager.get_status()["numeric_channels"] == ["v0", "v1", "v2", "v3"]


def test_rtt_ignores_a_partial_initial_channel_name_before_locking_layout():
    hub = _RecordingHub()
    manager = RttStreamManager(stream_hub=hub)

    manager.feed_rtt_bytes(
        b"peed=90\ntemp=25,speed=100\ntemp=26,speed=101\n"
    )
    manager.flush_pending()

    waveform = [
        batch for batch in hub.snapshot()
        if batch.stream_type is StreamType.WAVEFORM
    ]
    assert manager.get_status()["numeric_channels"] == ["speed", "temp"]
    assert len(waveform) == 1
    assert decode_waveform_samples(waveform[0].payload, 2, 2) == (
        (100.0, 25.0), (101.0, 26.0),
    )


def test_rtt_default_marker_mix_stays_near_100fps_and_keeps_30hz_waveform():
    hub = _RecordingHub()
    manager = RttStreamManager(stream_hub=hub)

    for index in range(100):
        manager.feed_rtt_bytes(b"1,2,3,4\n" * 110)
        manager.feed_rtt_bytes(f"M,{index},{index},0\n".encode())
    manager.flush_pending()

    batches = hub.snapshot()
    raw = [
        batch for batch in batches
        if batch.stream_type is StreamType.RTT_RAW
        and batch.flags == RTT_RAW_UTF8_LINES
    ]
    waveform = [batch for batch in batches if batch.stream_type is StreamType.WAVEFORM]
    assert sum(batch.item_count for batch in raw) == 11_100
    assert sum(batch.item_count for batch in waveform) == 11_000
    assert len(waveform) >= 30
    assert len(raw) + len(waveform) <= 100


def test_rtt_manager_stop_closes_the_device_stream_session():
    read_started = threading.Event()

    class Device:
        def __init__(self):
            self.stop_calls = 0

        def rtt_start(self, *_args, **_kwargs):
            pass

        def rtt_read(self, **_kwargs):
            read_started.set()
            time.sleep(0.002)
            return b""

        def rtt_stop(self):
            self.stop_calls += 1

    device = Device()
    manager = RttStreamManager()
    manager.start(device)
    assert read_started.wait(timeout=1.0)

    manager.stop()

    assert device.stop_calls == 1


def test_rtt_manager_stops_and_reports_device_error_state():
    from mklink._types import DeviceState

    read_started = threading.Event()

    class Device:
        def __init__(self):
            self.state = DeviceState.READY

        def rtt_start(self, *_args, **_kwargs):
            pass

        def rtt_read(self, **_kwargs):
            self.state = DeviceState.ERROR
            read_started.set()
            return b""

        def rtt_stop(self):
            pass

    manager = RttStreamManager()
    manager.start(Device())
    try:
        assert read_started.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while manager.running and time.monotonic() < deadline:
            time.sleep(0.01)

        status = manager.get_status()
        assert status["running"] is False
        assert "ERROR" in status["error"]
    finally:
        manager.stop()


def test_rtt_manager_write_preserves_bytes_and_exposes_down_buffers():
    read_started = threading.Event()

    class Device:
        def __init__(self):
            self.writes = []

        def rtt_start(self, *_args, **_kwargs):
            return {
                "control_block_addr": "0x20001A40",
                "down_buffers": [
                    {"channel": 0, "size": 32, "active": True, "name": "Terminal"},
                ],
            }

        def rtt_read(self, **_kwargs):
            read_started.set()
            time.sleep(0.002)
            return b""

        def rtt_write(self, data):
            self.writes.append(data)
            return True

        def rtt_stop(self):
            pass

    device = Device()
    manager = RttStreamManager()
    with pytest.raises(RuntimeError, match="not running"):
        manager.write(b"before start")

    manager.start(device, addr="0x20001A40", mode=1, search_size=0)
    assert read_started.wait(timeout=1.0)

    payload = b"\x00\xff\r\n"
    assert manager.write(payload) == len(payload)
    assert device.writes == [payload]
    assert manager.get_status()["down_buffers"] == [
        {"channel": 0, "size": 32, "active": True, "name": "Terminal"},
    ]

    manager.stop()

    assert manager.get_status()["down_buffers"] == []
    with pytest.raises(RuntimeError, match="not running"):
        manager.write(b"after stop")


def test_rtt_manager_write_rejects_missing_down_buffer_and_failed_start():
    read_started = threading.Event()

    class NoDownBufferDevice:
        def rtt_start(self, *_args, **_kwargs):
            return {
                "down_buffers": [
                    {"channel": 0, "size": 0, "active": False},
                ],
            }

        def rtt_read(self, **_kwargs):
            read_started.set()
            time.sleep(0.002)
            return b""

        def rtt_stop(self):
            pass

    manager = RttStreamManager()
    manager.start(NoDownBufferDevice())
    assert read_started.wait(timeout=1.0)
    with pytest.raises(RuntimeError, match="DownBuffer is unavailable"):
        manager.write(b"blocked")
    manager.stop()

    failed = RttStreamManager()
    failed_device = Mock()
    failed_device.rtt_start.side_effect = RuntimeError("init failed")
    failed.start(failed_device)
    failed._thread.join(timeout=1.0)

    assert failed.get_status()["down_buffers"] == []
    with pytest.raises(RuntimeError, match="not running"):
        failed.write(b"stale")


def test_rtt_manager_serializes_concurrent_start_calls(monkeypatch):
    caller_gate = threading.Event()
    first_worker_start = threading.Event()
    release_first_worker = threading.Event()
    start_calls = 0
    start_calls_lock = threading.Lock()

    class Device:
        def rtt_start(self, *_args, **_kwargs):
            nonlocal start_calls
            with start_calls_lock:
                start_calls += 1
            return {"down_buffers": [{"active": True}]}

        def rtt_read(self, **_kwargs):
            time.sleep(0.002)
            return b""

        def rtt_stop(self):
            pass

    manager = RttStreamManager()
    errors = []

    def invoke_start():
        caller_gate.wait()
        try:
            manager.start(Device(), duration=0.1)
        except Exception as exc:
            errors.append(exc)

    callers = [threading.Thread(target=invoke_start) for _ in range(2)]
    for caller in callers:
        caller.start()

    original_thread_start = threading.Thread.start
    intercepted = 0
    intercepted_lock = threading.Lock()

    def controlled_thread_start(thread):
        nonlocal intercepted
        with intercepted_lock:
            intercepted += 1
            current = intercepted
        if current == 1:
            first_worker_start.set()
            assert release_first_worker.wait(timeout=1.0)
        original_thread_start(thread)

    monkeypatch.setattr(threading.Thread, "start", controlled_thread_start)
    caller_gate.set()
    assert first_worker_start.wait(timeout=1.0)
    time.sleep(0.05)
    release_first_worker.set()
    for caller in callers:
        caller.join(timeout=1.0)
        assert not caller.is_alive()

    deadline = time.monotonic() + 1.0
    while start_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.005)

    assert errors == []
    assert start_calls == 1
    manager.stop()


def test_rtt_manager_converts_device_write_exceptions_to_runtime_error():
    read_started = threading.Event()

    class Device:
        def rtt_start(self, *_args, **_kwargs):
            return {"down_buffers": [{"channel": 0, "active": True}]}

        def rtt_read(self, **_kwargs):
            read_started.set()
            time.sleep(0.002)
            return b""

        def rtt_write(self, _data):
            raise OSError("transport disconnected")

        def rtt_stop(self):
            pass

    manager = RttStreamManager()
    manager.start(Device())
    assert read_started.wait(timeout=1.0)
    try:
        with pytest.raises(RuntimeError, match="RTT write failed"):
            manager.write(b"data")
    finally:
        manager.stop()


def test_superwatch_sample_rows_are_aligned_and_metadata_is_versioned():
    async def scenario():
        hub = StreamHub(max_batches_per_client=8)
        queue = hub.subscribe()
        manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=2)
        manager._runtime = SimpleNamespace(items=[
            SimpleNamespace(name="a", type_name="float", size=4, address=0x20000000,
                            source="ram", enum_values=None, metadata={}),
            SimpleNamespace(name="b", type_name="uint32_t", size=4, address=0x20000004,
                            source="ram", enum_values=None, metadata={}),
        ])

        assert manager.publish_metadata() == 2
        assert manager.publish_sample_points([
            {"_t": 0.0, "a": 1.0}, {"_t": 0.0, "b": 2},
        ])
        assert manager.publish_sample_points([
            {"_t": 0.1, "a": 3.0}, {"_t": 0.1, "b": 4},
        ])

        metadata = await queue.get()
        samples = await queue.get()
        assert metadata.stream_type is StreamType.SUPERWATCH
        assert metadata.flags == SUPERWATCH_METADATA_JSON
        decoded_meta = decode_superwatch_metadata(metadata.payload)
        assert decoded_meta["version"] == 2
        assert [channel["name"] for channel in decoded_meta["channels"]] == ["a", "b"]
        assert samples.flags == 0x03
        assert struct.unpack_from("<2d", samples.payload) == (0.0, 100.0)
        assert decode_waveform_samples(samples.payload[16:], 2, 2) == ((1.0, 2.0), (3.0, 4.0))
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_rtt_terminal_chunks_publish_without_waiting_for_newline():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        queue = hub.subscribe()
        manager = RttStreamManager(stream_hub=hub)

        manager.feed_rtt_bytes(b"\x1b[31mwarning\r")
        manager.flush_pending()

        batch = await queue.get()
        assert batch.stream_type is StreamType.RTT_RAW
        assert batch.flags == RTT_TERMINAL_UTF8
        assert batch.item_count == 1
        assert batch.payload.decode("utf-8") == "\x1b[31mwarning\r"
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_rtt_log_and_terminal_batches_use_independent_hubs():
    log_hub = _RecordingHub()
    terminal_hub = _RecordingHub()
    manager = RttStreamManager(stream_hub=log_hub, raw_batch_lines=1)
    manager.set_terminal_stream_hub(terminal_hub)

    manager.feed_rtt_bytes(b"ready\n")
    manager.flush_pending()

    assert len(log_hub.batches) == 1
    assert log_hub.batches[0].flags == RTT_RAW_UTF8_LINES
    assert decode_rtt_lines(log_hub.batches[0].payload, 1)[0].text == "ready"
    assert len(terminal_hub.batches) == 1
    assert terminal_hub.batches[0].flags == RTT_TERMINAL_UTF8
    assert terminal_hub.batches[0].payload == b"ready\n"


def test_rtt_terminal_decoder_preserves_gbk_characters_across_chunks():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        queue = hub.subscribe()
        manager = RttStreamManager(stream_hub=hub)
        manager.set_encoding("gbk")
        encoded = "警告".encode("gbk")

        manager.feed_rtt_bytes(encoded[:1])
        manager.flush_pending()
        manager.feed_rtt_bytes(encoded[1:] + b"\r")
        manager.flush_pending()

        batch = await queue.get()
        assert batch.flags == RTT_TERMINAL_UTF8
        assert batch.payload.decode("utf-8") == "警告\r"
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_superwatch_actual_rate_counts_only_complete_samples_on_a_monotonic_window():
    now = [0.0]
    manager = SuperWatchStreamManager(clock=lambda: now[0])
    manager._runtime = SimpleNamespace(items=[
        SimpleNamespace(name="a"), SimpleNamespace(name="b"),
    ])

    assert not manager.publish_sample_points([{"a": 1.0}])
    assert manager.get_status()["actual_rate"] == 0.0
    for timestamp in (0.0, 0.25, 0.5, 0.75, 1.0):
        now[0] = timestamp
        assert manager.publish_sample_points([{"a": timestamp, "b": timestamp + 1}])

    status = manager.get_status()
    assert status["read_cycles"] == 0
    assert status["actual_rate"] == pytest.approx(4.0)


def test_superwatch_preserves_device_sample_times_across_host_batch_jitter():
    hub = Mock()
    manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=2)
    manager._runtime = SimpleNamespace(items=[SimpleNamespace(name="a")])
    for timestamp in (0.0, 0.00013, 0.00026, 0.025):
        assert manager.publish_sample_points([{"_t": timestamp, "a": timestamp}])
    batches = [call for call in hub.publish.call_args_list if call.kwargs.get("item_count") == 2]
    assert len(batches) == 2
    for call, times in zip(batches, [(0.0, 0.13), (0.26, 25.0)]):
        assert call.kwargs["flags"] == 0x03
        assert struct.unpack_from("<2d", call.args[0]) == pytest.approx(times)
        assert struct.unpack_from("<2f", call.args[0], 16) == pytest.approx(tuple(t / 1000 for t in times))


def test_superwatch_rejects_partial_and_nonfinite_samples_atomically():
    hub = StreamHub(max_batches_per_client=2)
    manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=1)
    manager._runtime = SimpleNamespace(items=[
        SimpleNamespace(name="a"), SimpleNamespace(name="b"),
    ])
    assert not manager.publish_sample_points([{"_t": 0.0, "a": 1.0}])
    assert not manager.publish_sample_points([{"_t": 0.0, "a": 1.0, "b": float("inf")}])
    assert hub.stats().produced_batches == 0


def test_superwatch_layout_changes_flush_old_samples_before_metadata_atomically():
    async def scenario():
        hub = StreamHub(max_batches_per_client=16)
        manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=8)
        manager._runtime = _MutableWatchRuntime()
        manager.publish_metadata()
        queue = hub.subscribe()
        initial = await queue.get()
        assert [channel["name"] for channel in decode_superwatch_metadata(initial.payload)["channels"]] == ["a"]

        assert manager.publish_sample_points([{"a": 1.0}])
        manager.add_watch("b")
        old_sample = await asyncio.wait_for(queue.get(), timeout=0.1)
        added_metadata = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert old_sample.flags == SUPERWATCH_SAMPLE_MAJOR_FLOAT32
        assert decode_waveform_samples(old_sample.payload, 1, 1) == ((1.0,),)
        assert [channel["name"] for channel in decode_superwatch_metadata(added_metadata.payload)["channels"]] == ["a", "b"]

        assert manager.publish_sample_points([{"a": 2.0, "b": 3.0}])
        manager.remove_watch("b")
        two_channel_sample = await asyncio.wait_for(queue.get(), timeout=0.1)
        removed_metadata = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert decode_waveform_samples(two_channel_sample.payload, 1, 2) == ((2.0, 3.0),)
        assert [channel["name"] for channel in decode_superwatch_metadata(removed_metadata.payload)["channels"]] == ["a"]
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_superwatch_layout_change_reports_pending_samples_dropped_without_hub():
    manager = SuperWatchStreamManager(batch_samples=8)
    manager._runtime = _MutableWatchRuntime()
    assert manager.publish_sample_points([{"a": 1.0}])
    manager.add_watch("b")
    assert manager.get_status()["binary_drops"] == {"batches": 1, "items": 1}
    assert manager._pending_sample_count == 0
    assert len(manager._pending_values) == 0


def test_superwatch_stop_flushes_a_partial_batch():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=8)
        manager._runtime = _MutableWatchRuntime()
        manager.publish_metadata()
        queue = hub.subscribe()
        await queue.get()
        assert manager.publish_sample_points([{"a": 7.0}])
        manager.stop()
        sample = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert decode_waveform_samples(sample.payload, 1, 1) == ((7.0,),)
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_superwatch_subscribe_replays_cached_metadata_without_waiting_for_read(monkeypatch):
    async def scenario():
        hub = StreamHub(max_batches_per_client=16)
        manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=1)
        manager._runtime = _MutableWatchRuntime()
        manager.publish_metadata()
        sample_started = threading.Event()
        release_sample = threading.Event()
        sample_finished = threading.Event()

        class BlockingSession:
            stats = {}

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def read_frames(self, **_kwargs):
                sample_started.set()
                try:
                    assert release_sample.wait(2.0)
                    return []
                finally:
                    sample_finished.set()

            def stop(self):
                pass

        monkeypatch.setattr("mklink.dump_memory.DumpMemoryStreamSession", BlockingSession)
        manager.set_interval(1.0)
        manager.start(SimpleNamespace(_bridge=_SuperWatchDumpBridge()))
        try:
            assert await asyncio.to_thread(sample_started.wait, 1.0)
            queue = hub.subscribe()
            await asyncio.sleep(0)
            metadata = await asyncio.wait_for(queue.get(), timeout=0.5)
            assert metadata.flags == SUPERWATCH_METADATA_JSON
            assert decode_superwatch_metadata(metadata.payload)["channels"][0]["name"] == "a"
            assert not sample_finished.is_set()
            hub.unsubscribe(queue)
        finally:
            release_sample.set()
            await asyncio.to_thread(manager.stop)

    asyncio.run(scenario())


def test_superwatch_layout_change_does_not_wait_for_read_and_discards_stale_cycle(monkeypatch):
    hub = _RecordingHub()
    manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=1)
    runtime = _MutableWatchRuntime()
    manager._runtime = runtime
    manager.publish_metadata()
    sample_started = threading.Event()
    release_sample = threading.Event()
    sample_finished = threading.Event()
    add_finished = threading.Event()

    first_read = [True]

    class BlockingSession:
        stats = {}

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def read_frames(self, **_kwargs):
            if first_read[0]:
                first_read[0] = False
                sample_started.set()
                assert release_sample.wait(1.0)
                sample_finished.set()
                return [{"timestamp_us": 1, "regions": [(0, struct.pack("<f", 1.0))]}]
            time.sleep(0.0002)
            return []

        def stop(self):
            pass

    monkeypatch.setattr("mklink.dump_memory.DumpMemoryStreamSession", BlockingSession)
    device = SimpleNamespace(_bridge=_SuperWatchDumpBridge())
    manager.set_interval(1.0)
    manager.start(device)
    assert sample_started.wait(1.0)

    add_thread = threading.Thread(
        target=lambda: (manager.add_watch("b"), add_finished.set()), daemon=True,
    )
    add_thread.start()
    add_completed_without_read = add_finished.wait(0.05)
    release_sample.set()
    assert add_finished.wait(1.0)
    assert sample_finished.wait(1.0)
    manager.stop()
    add_thread.join(timeout=1.0)
    assert not add_thread.is_alive()
    assert add_completed_without_read
    assert not any(
        batch.flags == SUPERWATCH_SAMPLE_MAJOR_FLOAT32
        for batch in hub.snapshot()
    )
    status = manager.get_status()
    assert status["read_cycles"] == 0
    assert status["read_drops"] == 1


def test_superwatch_concurrent_poll_add_remove_pressure_keeps_batches_aligned(monkeypatch):
    hub = _RecordingHub()
    manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=8)
    manager._runtime = _MutableWatchRuntime()
    manager.publish_metadata()
    sampled = 0

    class SamplingSession:
        stats = {}

        def __init__(self, _bridge, regions, _period):
            self.channel_count = regions[0][1] // 4

        def start(self):
            pass

        def read_frames(self, **_kwargs):
            nonlocal sampled
            sampled += 1
            time.sleep(0.0002)
            payload = struct.pack(
                "<" + "f" * self.channel_count,
                *(float(sampled + index) for index in range(self.channel_count)),
            )
            return [{"timestamp_us": sampled, "regions": [(0, payload)]}]

        def stop(self):
            pass

    monkeypatch.setattr("mklink.dump_memory.DumpMemoryStreamSession", SamplingSession)
    manager.set_interval(0.00001)
    manager.start(SimpleNamespace(_bridge=_SuperWatchDumpBridge()))
    try:
        for _ in range(100):
            manager.add_watch("b")
            manager.remove_watch("b")
        target_cycles = manager.get_status()["read_cycles"] + 8
        deadline = time.monotonic() + 1.0
        while manager.get_status()["read_cycles"] < target_cycles and time.monotonic() < deadline:
            time.sleep(0.001)
    finally:
        manager.stop()

    active_channel_count = None
    sample_channel_counts = set()
    for batch in hub.snapshot():
        if batch.flags == SUPERWATCH_METADATA_JSON:
            active_channel_count = len(decode_superwatch_metadata(batch.payload)["channels"])
        elif batch.flags in (SUPERWATCH_SAMPLE_MAJOR_FLOAT32, SUPERWATCH_TIMESTAMPED_FLOAT32):
            assert active_channel_count in (1, 2)
            payload = (
                batch.payload[batch.item_count * 8:]
                if batch.flags == SUPERWATCH_TIMESTAMPED_FLOAT32
                else batch.payload
            )
            decode_waveform_samples(payload, batch.item_count, active_channel_count)
            sample_channel_counts.add(active_channel_count)
    assert sampled > 0
    status = manager.get_status()
    assert sample_channel_counts, {
        key: status[key]
        for key in ("read_cycles", "read_errors", "read_drops", "binary_drops")
    }
    assert manager.get_status()["read_drops"] > 0
    assert manager.get_status()["binary_drops"] == {"batches": 0, "items": 0}


def test_superwatch_republishes_current_metadata_for_late_subscribers():
    async def scenario():
        hub = StreamHub(max_batches_per_client=4)
        manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=1)
        manager._runtime = SimpleNamespace(items=[
            SimpleNamespace(name="a", type_name="float", size=4, address=0x20000000,
                            source="ram", enum_values=None, metadata={}),
        ])
        assert manager.publish_metadata() == 2
        queue = hub.subscribe()
        metadata = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert manager.publish_sample_points([{"a": 1.0}])
        sample = await queue.get()
        assert metadata.flags == SUPERWATCH_METADATA_JSON
        assert decode_superwatch_metadata(metadata.payload)["version"] == 2
        assert sample.flags == SUPERWATCH_SAMPLE_MAJOR_FLOAT32
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_binary_queue_overflow_reports_explicit_rtt_drops_without_blocking_producer():
    async def scenario():
        hub = StreamHub(max_batches_per_client=1)
        queue = hub.subscribe()
        manager = RttStreamManager(stream_hub=hub, raw_batch_lines=1)
        for index in range(100):
            manager.feed_rtt_bytes(f"line-{index}\n".encode())
        await asyncio.sleep(0)
        stats = hub.stats()
        assert stats.produced_items == 100
        assert stats.dropped_batches == 99
        assert stats.dropped_items == 99
        assert queue.qsize() == 1
        hub.unsubscribe(queue)

    asyncio.run(scenario())


def test_app_injects_and_shuts_down_rtt_and_superwatch_hubs(monkeypatch):
    from mklink.remote.api import create_app
    from mklink.remote.dashboards import get_managers

    app = create_app()
    managers = get_managers()
    assert managers["rtt"]._stream_hub is app.state.stream_registry["rtt"]
    assert managers["superwatch"]._stream_hub is app.state.stream_registry["superwatch"]

    stopped = []
    hubs = {}
    for name in ("rtt", "superwatch"):
        manager = managers[name]
        hub = app.state.stream_registry[name]
        hubs[name] = hub
        manager._running = True
        manager._stop_event.clear()
        def stop(name=name, manager=manager):
            stopped.append(name)
            manager._running = False
        monkeypatch.setattr(manager, "stop", stop)
    asyncio.run(app.router.shutdown())
    for name in ("rtt", "superwatch"):
        manager = managers[name]
        assert name in stopped
        assert manager._stream_hub is not hubs[name]
def _dump_frame(timestamp_us, payload):
    region = b"\x00" + struct.pack("<H", len(payload)) + payload
    length = 19 + len(region) + 6
    body = MAGIC + struct.pack("<QHB", timestamp_us, length, 1) + region + b"\x00\x00"
    return body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)


def test_superwatch_uses_dump_stream_and_reports_protocol_integrity():
    item = SimpleNamespace(
        name="value", type_name="float", size=4, address=0x20000000,
        source="ram", enum_values=None, metadata={},
    )
    block = SimpleNamespace(address=0x20000000, size=4, items=[item])
    runtime = SimpleNamespace(items=[item], blocks=[block])

    class Bridge:
        def __init__(self):
            self.chunks = [b"text" + _dump_frame(10, struct.pack("<f", 4.5))]
            self.writes = []

        def _enter_stream(self, state):
            self.state = state

        def _write_raw(self, data):
            self.writes.append(data)

        def drain_stream_bytes(self, max_bytes=None):
            return self.chunks.pop(0) if self.chunks else b""

        def _exit_stream(self):
            return ""

    hub = StreamHub(max_batches_per_client=4)
    manager = SuperWatchStreamManager(stream_hub=hub, batch_samples=1)
    assert manager.get_status()["interval"] == 0.001
    manager._runtime = runtime
    bridge = Bridge()
    manager.start(SimpleNamespace(_bridge=bridge))
    deadline = time.perf_counter() + 1.0
    while manager.get_status()["read_cycles"] < 1 and time.perf_counter() < deadline:
        time.sleep(0.001)
    manager.set_interval(0.002)
    restart_deadline = time.perf_counter() + 1.0
    expected_restart = b"cmd.dump_memory(0x20000000, 4, 0.002)\n"
    while expected_restart not in bridge.writes and time.perf_counter() < restart_deadline:
        time.sleep(0.001)
    manager.stop()

    status = manager.get_status()
    assert status["acquisition_mode"] == "dump-memory"
    assert status["read_cycles"] == 1
    assert status["stream_integrity"]["parser_dropped_bytes"] == 4
    assert status["stream_integrity"]["parser_crc_errors"] == 0
    assert hub.stats().produced_items == 1
    assert bridge.writes[0] == b"cmd.dump_memory(0x20000000, 4, 0.001)\n"
    assert expected_restart in bridge.writes
    assert bridge.writes[-1] == b"cmd.dump_memory(0x20000000, 4, -1.0)\n"
    assert b"cmd.dump_memory(0x20000000, 4, 0)\n" not in bridge.writes


def test_superwatch_rejects_bridge_without_dump_stream_instead_of_read_ram_fallback():
    manager = SuperWatchStreamManager()
    manager._runtime = SuperWatchRuntime(items=[
        WatchItem("value", 0x20000000, "float", 4),
    ])
    events = Mock()
    manager._bridge = events

    manager.start(SimpleNamespace(_bridge=object()))
    manager._thread.join(timeout=1.0)

    assert not manager.running
    assert any(
        call.args[0].get("event") == "error"
        and "read_ram fallback is disabled" in call.args[0].get("message", "")
        for call in events.put.call_args_list
    )
    assert manager.get_status()["acquisition_mode"] != "read-memory"


def test_superwatch_rejects_more_than_safe_dump_region_limit():
    manager = SuperWatchStreamManager()
    manager._runtime = SuperWatchRuntime(items=[
        WatchItem(f"value_{index}", 0x20000000 + index * 0x100, "float", 4)
        for index in range(16)
    ])
    events = Mock()
    manager._bridge = events

    manager.start(SimpleNamespace(_bridge=_SuperWatchDumpBridge()))
    manager._thread.join(timeout=1.0)

    assert not manager.running
    assert any(
        call.args[0].get("event") == "error"
        and "more than 15 dump_memory regions" in call.args[0].get("message", "")
        for call in events.put.call_args_list
    )


def _symbol_write_device(tmp_path, *, write_error=None):
    from mklink.dwarf_parser import DwarfInfo, DwarfVariable
    from mklink.symbol_catalog import SymbolCatalog

    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        base_types={1: ("float", 4)},
        variables={
            "gain": DwarfVariable("gain", 10, 1, 0x20000020, 4, "float"),
        },
    )
    catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )
    operations = []

    def write_memory(address, data):
        operations.append(("write", address, data))
        if write_error:
            raise write_error

    device = SimpleNamespace(
        symbol_catalog=catalog,
        write_memory=write_memory,
    )
    return device, operations


def test_superwatch_write_stops_dump_writes_reads_back_and_restores_running(tmp_path, monkeypatch):
    manager = SuperWatchStreamManager()
    manager._runtime = _MutableWatchRuntime()
    device, operations = _symbol_write_device(tmp_path)
    manager._device = device
    manager._running = True
    manager._stop_event.clear()
    manager._collecting.set()

    def stop():
        operations.append("stop")
        manager._running = False
        manager._collecting.clear()

    def start(_device):
        operations.append("start")
        manager._running = True
        manager._collecting.set()

    monkeypatch.setattr(manager, "stop", stop)
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(
        manager,
        "_readback_once",
        lambda address, size: operations.append(("readback", address, size)) or struct.pack("<f", 1.5),
        raising=False,
    )

    result = manager.write_symbol("gain", generation=1, value=1.5)

    assert operations == [
        "stop",
        ("write", 0x20000020, struct.pack("<f", 1.5)),
        ("readback", 0x20000020, 4),
        "start",
    ]
    assert result["verified"] is True
    assert result["value"] == pytest.approx(1.5)
    assert manager.get_status()["state"] == "running"


def test_superwatch_write_failure_restores_paused_state(tmp_path, monkeypatch):
    manager = SuperWatchStreamManager()
    manager._runtime = _MutableWatchRuntime()
    device, operations = _symbol_write_device(tmp_path, write_error=RuntimeError("flush failed"))
    manager._device = device
    manager._running = True
    manager._stop_event.clear()
    manager._collecting.clear()

    def stop():
        operations.append("stop")
        manager._running = False

    def start(_device):
        operations.append("start")
        manager._running = True
        manager._collecting.set()

    def pause():
        operations.append("pause")
        manager._collecting.clear()

    monkeypatch.setattr(manager, "stop", stop)
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(manager, "pause", pause)

    with pytest.raises(RuntimeError, match="flush failed"):
        manager.write_symbol("gain", generation=1, value=2.0)

    assert operations == [
        "stop",
        ("write", 0x20000020, struct.pack("<f", 2.0)),
        "start",
        "pause",
    ]
    assert manager.get_status()["state"] == "paused"


def test_superwatch_prepare_rebinds_existing_runtime_to_reconnected_device(
    tmp_path, monkeypatch
):
    manager = SuperWatchStreamManager()
    manager._runtime = _MutableWatchRuntime()
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    old_device, old_operations = _symbol_write_device(old_root)
    new_device, new_operations = _symbol_write_device(new_root)
    manager._device = old_device

    manager.prepare(new_device)
    monkeypatch.setattr(
        manager,
        "_readback_once",
        lambda address, size: new_operations.append(("readback", address, size))
        or struct.pack("<f", 1.5),
        raising=False,
    )

    result = manager.write_symbol("gain", generation=1, value=1.5)

    assert old_operations == []
    assert new_operations == [
        ("write", 0x20000020, struct.pack("<f", 1.5)),
        ("readback", 0x20000020, 4),
    ]
    assert result["verified"] is True


def test_superwatch_prepare_rebinds_selected_items_to_new_device_catalog(tmp_path):
    from mklink.dwarf_parser import DwarfInfo, DwarfVariable
    from mklink.superwatch import SuperWatchRuntime, WatchItem
    from mklink.symbol_catalog import SymbolCatalog

    old_axf = tmp_path / "old.axf"
    new_axf = tmp_path / "new.axf"
    old_axf.write_bytes(b"old")
    new_axf.write_bytes(b"new")
    old_info = DwarfInfo(
        base_types={1: ("float", 4)},
        variables={
            "gain": DwarfVariable("gain", 10, 1, 0x20000020, 4, "float"),
            "removed": DwarfVariable("removed", 11, 1, 0x20000024, 4, "float"),
        },
    )
    new_info = DwarfInfo(
        base_types={1: ("float", 4)},
        variables={
            "gain": DwarfVariable("gain", 10, 1, 0x20000040, 4, "float"),
        },
    )
    old_catalog = SymbolCatalog.from_dwarf(
        old_info, axf_path=str(old_axf), ram_ranges=[(0x20000000, 0x20010000)]
    )
    new_catalog = SymbolCatalog.from_dwarf(
        new_info, axf_path=str(new_axf), ram_ranges=[(0x20000000, 0x20010000)]
    )
    manager = SuperWatchStreamManager()
    manager._runtime = SuperWatchRuntime(
        items=[
            WatchItem("gain", 0x20000020, "float", 4),
            WatchItem("removed", 0x20000024, "float", 4),
        ],
        dwarf_info=old_info,
        symbol_catalog=old_catalog,
    )
    device = SimpleNamespace(
        _dwarf_info=new_info,
        symbol_catalog=new_catalog,
        _project_root=str(tmp_path),
        _port=None,
    )

    manager.prepare(device)

    assert [(item.name, item.address) for item in manager._runtime.items] == [
        ("gain", 0x20000040),
    ]
    assert [(block.address, block.size) for block in manager._runtime.blocks] == [
        (0x20000040, 4),
    ]


def test_superwatch_readback_uses_command_mode_after_dump_stream_stops():
    manager = SuperWatchStreamManager()
    device = SimpleNamespace(read_memory=Mock(return_value=b"\x00"))
    manager._device = device

    assert manager._readback_once(0x20000020, 1) == b"\x00"
    device.read_memory.assert_called_once_with(0x20000020, 1)


def test_superwatch_adds_catalog_array_leaves_and_merges_contiguous_reads(tmp_path):
    from mklink.dwarf_parser import DwarfInfo, DwarfVariable
    from mklink.symbol_catalog import SymbolCatalog

    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        base_types={1: ("int16_t", 2)},
        arrays={2: (1, 8)},
        variables={
            "samples": DwarfVariable(
                "samples", 10, 2, 0x20000020, 8, "int16_t[]",
            ),
        },
    )
    catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )
    device = SimpleNamespace(
        _dwarf_info=info,
        symbol_catalog=catalog,
        _project_root=str(tmp_path),
        _port=None,
    )
    manager = SuperWatchStreamManager()

    manager.prepare(device)
    first = manager.add_watch("samples[0]")
    second = manager.add_watch("samples[1]")

    assert first["item"]["address"] == "0x20000020"
    assert second["item"]["address"] == "0x20000022"
    assert manager._runtime.items[0].scalar_kind == "signed"
    assert [(block.address, block.size) for block in manager._runtime.blocks] == [
        (0x20000020, 4),
    ]


def test_superwatch_array_snapshot_reads_only_requested_slice(tmp_path):
    from mklink.dwarf_parser import DwarfInfo, DwarfVariable
    from mklink.symbol_catalog import SymbolCatalog

    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        base_types={1: ("int16_t", 2)},
        arrays={2: (1, 16)},
        variables={
            "samples": DwarfVariable(
                "samples", 10, 2, 0x20000020, 16, "int16_t[]",
            ),
        },
    )
    catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )
    device = SimpleNamespace(
        _dwarf_info=info,
        symbol_catalog=catalog,
        _project_root=str(tmp_path),
        _port=None,
    )
    manager = SuperWatchStreamManager()
    manager.prepare(device)

    selected = manager.select_array_snapshot("samples", start_index=2, count=3)
    scalar_items, sampled_items, dump_blocks = manager._sampling_layout_locked()

    assert selected["snapshot"]["start_index"] == 2
    assert selected["snapshot"]["count"] == 3
    assert scalar_items == ()
    assert [item.name for item in sampled_items] == [
        "samples[2]", "samples[3]", "samples[4]",
    ]
    assert [(block.address, block.size) for block in dump_blocks] == [
        (0x20000024, 6),
    ]
    assert manager._update_array_snapshot_locked([{
        "timestamp_us": 25,
        "samples[2]": -2,
        "samples[3]": -1,
        "samples[4]": 3,
    }])
    snapshot = manager.get_array_snapshot()["snapshot"]
    assert snapshot["values"] == [-2.0, -1.0, 3.0]
    assert manager.clear_array_snapshot() == {"snapshot": None}


def test_dump_stream_decodes_catalog_typedef_with_scalar_kind_and_size():
    from mklink.dump_memory import decode_frame_to_points

    frame = {
        "timestamp_us": 10,
        "regions": [(0, struct.pack("<q", -2))],
    }
    blocks = [
        (0x20000020, 8, [("clock", "clock_t", 0, 8, "signed", None)]),
    ]

    points, origin = decode_frame_to_points(frame, blocks, None)

    assert origin == 10
    assert points == [{"_t": 0.0, "timestamp_us": 10, "clock": -2}]


def test_read_memory_fallback_keeps_enum_samples_numeric():
    from mklink.superwatch import ReadBlock, WatchItem, sample_blocks

    item = WatchItem(
        "mode",
        0x20000020,
        "Mode",
        4,
        enum_values={1: "RUN"},
        scalar_kind="enum",
    )
    block = ReadBlock(0x20000020, 4, [item])

    result = sample_blocks(
        [block],
        read_func=lambda _port, _address, _size: (
            b"\x01\x00\x00\x00",
            "timestamp_us=10\n20000020  01 00 00 00",
        ),
    )

    assert result.points[0]["mode"] == 1


def test_superwatch_reparse_rebinds_selected_names_and_restores_running(tmp_path, monkeypatch):
    from mklink.dwarf_parser import DwarfInfo, DwarfVariable
    from mklink.superwatch import SuperWatchRuntime, WatchItem
    from mklink.symbol_catalog import SymbolCatalog

    first_axf = tmp_path / "first.axf"
    second_axf = tmp_path / "second.axf"
    first_axf.write_bytes(b"first")
    second_axf.write_bytes(b"second")
    old_info = DwarfInfo(
        base_types={1: ("float", 4)},
        variables={"gain": DwarfVariable("gain", 10, 1, 0x20000020, 4, "float")},
    )
    new_info = DwarfInfo(
        base_types={1: ("float", 4)},
        variables={"gain": DwarfVariable("gain", 10, 1, 0x20000040, 4, "float")},
    )
    old_catalog = SymbolCatalog.from_dwarf(
        old_info, axf_path=str(first_axf), generation=1, ram_ranges=[(0x20000000, 0x20010000)]
    )
    new_catalog = SymbolCatalog.from_dwarf(
        new_info, axf_path=str(second_axf), generation=2, ram_ranges=[(0x20000000, 0x20010000)]
    )
    operations = []
    device = SimpleNamespace(
        symbol_catalog=old_catalog,
        _dwarf_info=old_info,
        _project_root=".",
        _port=None,
    )

    def reparse_axf_atomically(axf_path=None):
        operations.append(("reparse", axf_path))
        device.symbol_catalog = new_catalog
        device._dwarf_info = new_info
        return new_catalog

    device.reparse_axf_atomically = reparse_axf_atomically
    manager = SuperWatchStreamManager()
    manager._device = SimpleNamespace(symbol_catalog=old_catalog)
    manager._runtime = SuperWatchRuntime(
        items=[WatchItem("gain", 0x20000020, "float", 4)],
        dwarf_info=old_info,
    )
    manager._running = True
    manager._stop_event.clear()
    manager._collecting.set()

    def stop():
        operations.append("stop")
        manager._running = False
        manager._collecting.clear()

    def start(_device):
        operations.append("start")
        manager._running = True
        manager._collecting.set()

    monkeypatch.setattr(manager, "stop", stop)
    monkeypatch.setattr(manager, "start", start)

    result = manager.reparse_symbols(str(second_axf), device=device)

    assert operations == ["stop", ("reparse", str(second_axf)), "start"]
    assert result == {"preserved": [], "updated": ["gain"], "removed": []}
    assert manager._runtime.items[0].name == "gain"
    assert manager._runtime.items[0].address == 0x20000040
    assert manager._device is device
    assert manager.get_status()["state"] == "running"


def test_superwatch_applies_c_layout_and_restores_paused_collection(tmp_path, monkeypatch):
    from mklink.c_layout import parse_c_layout
    from mklink.dwarf_parser import DwarfInfo, DwarfVariable
    from mklink.superwatch import SuperWatchRuntime
    from mklink.symbol_catalog import SymbolCatalog

    axf = tmp_path / "app.axf"
    axf.write_bytes(b"axf")
    info = DwarfInfo(
        base_types={1: ("uint32_t", 4)},
        variables={"opaque": DwarfVariable("opaque", 10, None, 0x20000020, 8, "Opaque")},
    )
    old_catalog = SymbolCatalog.from_dwarf(
        info, axf_path=str(axf), ram_ranges=[(0x20000000, 0x20010000)]
    )
    layout = parse_c_layout(
        "typedef struct { uint32_t low; uint32_t high; } Opaque;",
        preferred_type="Opaque",
    )
    new_catalog = old_catalog.with_c_layout("opaque", 0x20000020, layout)
    operations = []
    device = SimpleNamespace(symbol_catalog=old_catalog, _dwarf_info=info, _port=None)

    def apply_c_definition(variable, definition, pack):
        operations.append(("apply", variable, pack))
        device.symbol_catalog = new_catalog
        return new_catalog, layout

    device.apply_c_definition = apply_c_definition
    manager = SuperWatchStreamManager()
    manager._runtime = SuperWatchRuntime(items=[], dwarf_info=info, symbol_catalog=old_catalog)
    manager._device = device
    manager._running = True
    manager._stop_event.clear()
    manager._collecting.clear()

    def stop():
        operations.append("stop")
        manager._running = False

    def start(_device):
        operations.append("start")
        manager._running = True
        manager._collecting.set()

    def pause():
        operations.append("pause")
        manager._collecting.clear()

    monkeypatch.setattr(manager, "stop", stop)
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(manager, "pause", pause)

    result = manager.apply_c_definition(
        "opaque", "typedef struct { uint32_t low; uint32_t high; } Opaque;", 4,
        device=device,
    )

    assert operations == ["stop", ("apply", "opaque", 4), "start", "pause"]
    assert result["layout"]["leaf_count"] == 2
    assert manager._runtime.symbol_catalog is new_catalog
    assert manager.running is True
    assert manager._collecting.is_set() is False
