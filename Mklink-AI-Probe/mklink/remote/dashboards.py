"""Integrated dashboard SSE endpoints for FastAPI.

Provides real-time data streaming (RTT, VOFA, SuperWatch, Serial, Modbus)
without launching subprocess dashboards. The FastAPI process holds the single
device connection and streams data via SSE.

Architecture::

    Vue Component ──SSE──► FastAPI /api/dash/*/stream
                                  │
                            thread pool executor
                                  │
                            Device (single connection)
                                  │
                            MKLink Probe ──► Target MCU
"""

from __future__ import annotations

import asyncio
import base64
import codecs
from collections import deque
import json
import logging
import math
import struct
import threading
import time
from typing import Any, Generator

from mklink.remote.stream_protocol import (
    RTT_RAW_UTF8_LINES,
    RTT_TERMINAL_UTF8,
    SERIAL_RX_BYTES,
    SERIAL_TX_BYTES,
    SUPERWATCH_METADATA_JSON,
    SUPERWATCH_SAMPLE_MAJOR_FLOAT32,
    WAVEFORM_SAMPLE_MAJOR_FLOAT32,
    RttLine,
    StreamType,
    encode_rtt_lines,
    encode_superwatch_metadata,
    encode_systemview_events,
    encode_waveform_samples,
)


def _sum_counter_snapshots(base: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    """Combine completed-session counters with one active session snapshot."""
    return {
        key: int(base.get(key, 0)) + int(current.get(key, 0))
        for key in base.keys() | current.keys()
    }

logger = logging.getLogger(__name__)
_RTT_DELIVERY_INTERVAL = 1.0 / 50.0
_SYSTEMVIEW_DELIVERY_INTERVAL = 1.0 / 30.0
_RUNTIME_CPU_FREQ_SOURCES = {"SystemCoreClock", "hpm_core_clock"}
SUPPORTED_RTT_ENCODINGS = ("utf-8", "gb2312", "gbk", "gb18030", "big5")


def normalize_rtt_encoding(value: str) -> str:
    encoding = str(value).strip().lower().replace("_", "-")
    if encoding not in SUPPORTED_RTT_ENCODINGS:
        raise ValueError(
            f"Unsupported RTT encoding: {value}. "
            f"Expected one of {', '.join(SUPPORTED_RTT_ENCODINGS)}"
        )
    return encoding


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _device_in_error_state(device) -> bool:
    state = getattr(device, "state", None)
    return (
        str(getattr(state, "name", "")).upper() == "ERROR"
        or str(getattr(state, "value", "")).lower() == "error"
    )


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse_format(data: str, event: str | None = None) -> str:
    """Format a single SSE message."""
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {data}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _sse_json(data: Any, event: str | None = None) -> str:
    return _sse_format(json.dumps(data, default=str), event)


# ---------------------------------------------------------------------------
# AsyncBridge — thread ↔ async queue bridge
# ---------------------------------------------------------------------------

class AsyncBridge:
    """Bridges a synchronous polling thread to an async SSE generator.

    Usage:
        bridge = AsyncBridge()
        # In a background thread:
        bridge.put({"temp": 25.3})
        # In an async SSE generator:
        async for data in bridge:
            yield data
    """

    def __init__(self, maxsize: int = 200):
        self._queue: asyncio.Queue | None = None
        self._maxsize = maxsize
        self._stopped = False
        self._lock = threading.Lock()
        self._clients: list[asyncio.Queue] = []
        self._clients_lock = threading.Lock()

    def _get_queue(self) -> asyncio.Queue:
        """Get or create a queue for the current async context."""
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
        return self._queue

    def put(self, data: Any) -> None:
        """Put data from a sync thread into all client queues."""
        with self._clients_lock:
            for q in self._clients:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    # Drop oldest to make room
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(data)
                    except asyncio.QueueFull:
                        pass

    def add_client(self) -> asyncio.Queue:
        """Register a new SSE client and return its queue."""
        q = asyncio.Queue(maxsize=self._maxsize)
        with self._clients_lock:
            self._clients.append(q)
        return q

    def remove_client(self, q: asyncio.Queue) -> None:
        """Unregister an SSE client."""
        with self._clients_lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def stop(self) -> None:
        self._stopped = True
        with self._clients_lock:
            for q in self._clients:
                try:
                    q.put_nowait(None)  # sentinel
                except asyncio.QueueFull:
                    pass


# ---------------------------------------------------------------------------
# RTT streaming
# ---------------------------------------------------------------------------


class _RttLineAssembler:
    """Incrementally decode RTT text and split LF/CRLF without losing tails."""

    def __init__(self, encoding: str = "utf-8"):
        self.reset(encoding)

    def feed(self, chunk: bytes, *, final: bool = False) -> list[str]:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("RTT chunks must be bytes-like")
        self._text += self._decoder.decode(bytes(chunk), final=final)
        lines = []
        while True:
            newline = self._text.find("\n")
            if newline < 0:
                break
            line = self._text[:newline]
            self._text = self._text[newline + 1:]
            lines.append(line[:-1] if line.endswith("\r") else line)
        if final:
            if self._text:
                lines.append(self._text[:-1] if self._text.endswith("\r") else self._text)
            self.reset()
        return lines

    def reset(self, encoding: str | None = None) -> None:
        if encoding is not None:
            self.encoding = normalize_rtt_encoding(encoding)
        self._decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        self._text = ""

class RttStreamManager:
    """Manages RTT streaming sessions with SSE output."""

    def __init__(
        self, stream_hub=None, *, raw_batch_lines: int = 512,
        waveform_batch_samples: int = 256,
    ):
        self._bridge = AsyncBridge()
        self._thread: threading.Thread | None = None
        self._running = False
        self._paused = threading.Event()
        self._paused.set()  # not paused
        self._stop_event = threading.Event()
        self._history: list[dict] = []
        self._max_history = 500
        from mklink.rtt_viewer import RttLineParser
        self._parser = RttLineParser("kv")
        self._parser_auto_detect_done = False
        self._parser_auto_detect_attempts = 0
        self._parser_auto_detect_samples: list[str] = []
        self._interval = 0.0
        self._stats = {"parsed_lines": 0, "raw_lines": 0}
        self._error: str | None = None
        self._start_failure_callback = None
        self._stream_hub = stream_hub
        self._terminal_stream_hub = stream_hub
        self._raw_batch_lines = max(1, int(raw_batch_lines))
        self._waveform_batch_samples = max(1, int(waveform_batch_samples))
        self._line_assembler = _RttLineAssembler()
        self._terminal_decoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )
        self._decode_lock = threading.RLock()
        self._pending_raw: list[RttLine] = []
        self._pending_terminal: list[str] = []
        self._pending_numeric: list[tuple[float, ...]] = []
        self._numeric_channels: tuple[str, ...] = ()
        self._numeric_candidate_channels: tuple[str, ...] = ()
        self._numeric_candidate_rows: list[tuple[float, ...]] = []
        self._device = None
        self._start_info: dict = {}
        self._active_generation = None
        self._write_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self._running and not self._stop_event.is_set()

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    def set_start_failure_callback(self, callback) -> None:
        self._start_failure_callback = callback

    def set_stream_hub(self, stream_hub) -> None:
        self._stream_hub = stream_hub

    def set_terminal_stream_hub(self, stream_hub) -> None:
        self._terminal_stream_hub = stream_hub

    def detach_stream_hub(self, stream_hub) -> None:
        if self._stream_hub is stream_hub:
            self._stream_hub = None

    def detach_terminal_stream_hub(self, stream_hub) -> None:
        if self._terminal_stream_hub is stream_hub:
            self._terminal_stream_hub = None

    def _clear_active_session(self, generation=None) -> None:
        with self._write_lock:
            if generation is not None and self._active_generation is not generation:
                return
            self._device = None
            self._start_info = {}
            self._active_generation = None

    def feed_rtt_bytes(self, chunk: bytes, *, final: bool = False) -> None:
        with self._decode_lock:
            terminal_text = self._terminal_decoder.decode(
                bytes(chunk), final=final
            )
            lines = self._line_assembler.feed(chunk, final=final)
            if final:
                self._terminal_decoder = codecs.getincrementaldecoder(
                    self._line_assembler.encoding
                )(errors="replace")
        if terminal_text:
            self._pending_terminal.append(terminal_text)
        for raw_line in lines:
            line = raw_line.strip()
            timestamp_ns = time.time_ns()
            if line and not self._parser_auto_detect_done:
                from mklink.rtt_viewer import RttLineParser
                self._parser_auto_detect_samples.append(line)
                detected = RttLineParser.auto_detect(
                    self._parser_auto_detect_samples
                )
                if detected.strategy != self._parser.strategy:
                    self._parser = detected
                self._parser_auto_detect_attempts += 1
                self._parser_auto_detect_done = self._parser_auto_detect_attempts >= 10
            parsed = self._parser.parse(line) if line and self._parser is not None else None
            self._stats["raw_lines"] += 1
            self._pending_raw.append(RttLine(
                timestamp_ns, "data" if parsed else "raw", raw_line,
            ))
            if len(self._pending_raw) >= self._raw_batch_lines:
                self._flush_raw_batch()
            if parsed:
                parsed["_t"] = timestamp_ns / 1_000_000_000.0
                self._stats["parsed_lines"] += 1
                self._history.append(parsed)
                if len(self._history) > self._max_history:
                    del self._history[:-self._max_history]
                channels = tuple(sorted(key for key in parsed if not key.startswith("_")))
                if channels and all(math.isfinite(float(parsed[key])) for key in channels):
                    if not self._numeric_channels:
                        row = tuple(float(parsed[key]) for key in channels)
                        # Stream attachment can begin in the middle of a line.
                        if self._numeric_candidate_channels != channels:
                            self._numeric_candidate_channels = channels
                            self._numeric_candidate_rows = [row]
                            continue
                        self._numeric_candidate_rows.append(row)
                        self._numeric_channels = channels
                        self._pending_numeric.extend(self._numeric_candidate_rows)
                        self._numeric_candidate_channels = ()
                        self._numeric_candidate_rows = []
                    elif self._numeric_channels == channels:
                        self._pending_numeric.append(
                            tuple(float(parsed[key]) for key in channels)
                        )
                    if self._numeric_channels == channels:
                        if len(self._pending_numeric) >= self._waveform_batch_samples:
                            self._flush_raw_batch()
                            self._flush_numeric_batch()

    def _flush_raw_batch(self) -> None:
        if not self._pending_raw:
            return
        pending = self._pending_raw
        self._pending_raw = []
        if self._stream_hub is not None:
            self._stream_hub.publish(
                encode_rtt_lines(pending), item_count=len(pending),
                flags=RTT_RAW_UTF8_LINES, stream_type=StreamType.RTT_RAW,
            )

    def _flush_terminal_batch(self) -> None:
        if not self._pending_terminal:
            return
        text = "".join(self._pending_terminal)
        self._pending_terminal = []
        if self._terminal_stream_hub is not None:
            self._terminal_stream_hub.publish(
                text.encode("utf-8"), item_count=1,
                flags=RTT_TERMINAL_UTF8, stream_type=StreamType.RTT_RAW,
            )

    def _flush_numeric_batch(self) -> None:
        if not self._pending_numeric:
            return
        pending = self._pending_numeric
        self._pending_numeric = []
        if self._stream_hub is not None:
            self._stream_hub.publish(
                encode_waveform_samples(pending), item_count=len(pending),
                flags=WAVEFORM_SAMPLE_MAJOR_FLOAT32,
                stream_type=StreamType.WAVEFORM,
            )

    def flush_pending(self, *, final: bool = False) -> None:
        if final:
            self.feed_rtt_bytes(b"", final=True)
        self._flush_raw_batch()
        self._flush_numeric_batch()
        self._flush_terminal_batch()

    def start(self, device, *, addr: str | None = None, channel: int = 0,
              mode: int = 0, search_size: int = 1024,
              duration: float = 86400, encoding: str = "utf-8") -> None:
        with self._lifecycle_lock:
            self._start_locked(
                device,
                addr=addr,
                channel=channel,
                mode=mode,
                search_size=search_size,
                duration=duration,
                encoding=encoding,
            )

    def _start_locked(self, device, *, addr: str | None = None, channel: int = 0,
                      mode: int = 0, search_size: int = 1024,
                      duration: float = 86400, encoding: str = "utf-8") -> None:
        """Start RTT polling in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            if self.running:
                return
            raise RuntimeError("RTT worker thread is still active")

        self._clear_active_session()
        stop_event = threading.Event()
        generation = object()
        self._stop_event = stop_event
        self._generation = generation
        self._paused.set()
        self._running = True
        self._history.clear()
        self._stats = {"parsed_lines": 0, "raw_lines": 0}
        self._error = None
        with self._decode_lock:
            self._line_assembler.reset(encoding)
            self._terminal_decoder = codecs.getincrementaldecoder(
                self._line_assembler.encoding
            )(errors="replace")
        self._pending_raw.clear()
        self._pending_terminal.clear()
        self._pending_numeric.clear()
        self._numeric_channels = ()
        self._numeric_candidate_channels = ()
        self._numeric_candidate_rows = []

        # Auto-detect parser strategy from initial RTT output
        from mklink.rtt_viewer import RttLineParser
        self._parser = RttLineParser("kv")  # will auto-detect on first lines
        self._parser_auto_detect_done = False
        self._parser_auto_detect_attempts = 0
        self._parser_auto_detect_samples.clear()
        failure_callback = self._start_failure_callback

        def _poll():
            initialized = False
            start_failure = None
            terminal_failure = None
            try:
                start_info = device.rtt_start(
                    addr, channel=channel, mode=mode, search_size=search_size,
                )
                initialized = True
                with self._write_lock:
                    if (
                        getattr(self, "_generation", None) is generation
                        and not stop_event.is_set()
                    ):
                        self._device = device
                        self._start_info = (
                            dict(start_info) if isinstance(start_info, dict) else {}
                        )
                        self._start_info["channel"] = channel
                        self._active_generation = generation
                start_time = time.time()
                while not stop_event.is_set():
                    if time.time() - start_time > duration:
                        break
                    if not self._paused.is_set():
                        time.sleep(0.05)
                        continue

                    try:
                        read_bytes = getattr(device, "rtt_read_bytes", None)
                        if callable(read_bytes):
                            text = read_bytes(duration=_RTT_DELIVERY_INTERVAL)
                        else:
                            text = device.rtt_read(duration=_RTT_DELIVERY_INTERVAL)
                    except Exception as exc:
                        if _device_in_error_state(device):
                            terminal_failure = exc
                            break
                        time.sleep(0.1)
                        continue

                    if _device_in_error_state(device):
                        terminal_failure = RuntimeError(
                            "RTT device entered ERROR state"
                        )
                        break

                    if text is None or text == b"" or text == "":
                        continue

                    chunk = text if isinstance(text, bytes) else str(text).encode("utf-8")
                    self.feed_rtt_bytes(chunk)
                    self.flush_pending()

            except Exception as e:
                logger.error("RTT stream error: %s", e)
                self._bridge.put({"event": "error", "message": str(e)})
                if not initialized:
                    start_failure = e
                else:
                    terminal_failure = e
            finally:
                with self._write_lock:
                    try:
                        device.rtt_stop()
                    except Exception as e:
                        logger.warning("RTT device stop failed: %s", e)
                    if self._active_generation is generation:
                        self._device = None
                        self._start_info = {}
                        self._active_generation = None
                self.flush_pending(final=True)
                try:
                    if getattr(self, "_generation", None) is generation:
                        self._running = False
                        try:
                            self._bridge.put({"event": "stopped"})
                        finally:
                            self._bridge.stop()
                finally:
                    failure = start_failure or terminal_failure
                    if failure is not None:
                        self._error = str(failure)
                    if failure is not None and failure_callback is not None:
                        try:
                            failure_callback(failure)
                        except Exception:
                            pass

        self._thread = threading.Thread(target=_poll, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lifecycle_lock:
            thread = self._thread
            self._stop_event.set()
            self._clear_active_session(getattr(self, "_generation", None))
            if thread:
                thread.join(timeout=timeout)
                if thread.is_alive():
                    raise TimeoutError("RTT worker thread is still active")
            self._running = False
            self.flush_pending(final=True)
            if self._thread is thread:
                self._thread = None
            return True

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def set_encoding(self, encoding: str) -> str:
        normalized = normalize_rtt_encoding(encoding)
        with self._decode_lock:
            self._line_assembler.reset(normalized)
            self._terminal_decoder = codecs.getincrementaldecoder(normalized)(
                errors="replace"
            )
        self._pending_terminal.clear()
        return normalized

    def get_history(self) -> list[dict]:
        return list(self._history)

    def write(self, data: bytes) -> int:
        with self._write_lock:
            if (
                not self.running
                or self._device is None
                or self._active_generation is not getattr(self, "_generation", None)
            ):
                raise RuntimeError("RTT is not running")
            down_buffers = self._start_info.get("down_buffers", [])
            channel = int(self._start_info.get("channel", 0))
            if not any(
                isinstance(item, dict)
                and item.get("channel") == channel
                and item.get("active")
                for item in down_buffers
            ):
                raise RuntimeError("RTT DownBuffer is unavailable")
            try:
                written = self._device.rtt_write(data)
            except Exception as exc:
                raise RuntimeError(f"RTT write failed: {exc}") from exc
            if not written:
                raise RuntimeError("RTT write failed")
            return len(data)

    def get_status(self) -> dict:
        with self._write_lock:
            down_buffers = [
                dict(item)
                for item in self._start_info.get("down_buffers", [])
                if isinstance(item, dict)
            ]
            control_block_addr = self._start_info.get("control_block_addr")
            down_buffer_source = self._start_info.get("down_buffer_source")
            down_buffer_probe_count = self._start_info.get(
                "down_buffer_probe_count", 0
            )
        return {
            "running": self.running,
            "paused": self.paused,
            "clients": self._bridge.client_count,
            "stats": self._stats,
            "error": self._error,
            "history_size": len(self._history),
            "numeric_channels": list(self._numeric_channels),
            "encoding": self._line_assembler.encoding,
            "down_buffers": down_buffers,
            "control_block_addr": control_block_addr,
            "down_buffer_source": down_buffer_source,
            "down_buffer_probe_count": down_buffer_probe_count,
            "stream": self._stream_hub.stats().__dict__ if self._stream_hub else None,
        }

    async def sse_generator(self):
        """Async SSE generator for FastAPI StreamingResponse."""
        q = self._bridge.add_client()
        # Send initial state
        yield _sse_json({"event": "status", **self.get_status()})
        # Send history replay
        if self._history:
            yield _sse_json({"event": "history", "points": self._history[-100:]})

        try:
            while self.running or self.paused:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield _sse_format("", event="ping")
                    continue
                if data is None:
                    break
                yield _sse_json(data)
                if data.get("event") == "stopped":
                    break
        finally:
            self._bridge.remove_client(q)


class SystemViewStreamManager:
    """Manages SEGGER SystemView RTOS-trace streaming via RTT channel 1.

    Reads raw bytes from the target's RTT "SysView" up-buffer, decodes them
    with a persistent SystemViewParser (accumulating timestamps + name maps),
    and streams decoded RTOS events (task switches, ISR, CPU%, kernel objects)
    over SSE for the RTOS-Trace dashboard.
    """

    def __init__(self, stream_hub=None):
        self._bridge = AsyncBridge()
        self._thread: threading.Thread | None = None
        self._running = False
        self._paused = threading.Event()
        self._paused.set()  # not paused
        self._stop_event = threading.Event()
        self._history: list[dict] = []
        self._max_history = 100_000
        self._history_buffer_us = 60_000_000
        self._history_replay_limit = 500
        self._live_batch_limit = 500
        self._stream_hub = stream_hub
        self._last_status_publish = 0.0
        self._parser = None
        self._stats = {"events": 0, "bytes": 0}
        self._resolved_task_names: dict[int, str] = {}
        self._name_resolution_attempted: set[int] = set()
        self._last_name_resolution = 0.0
        self._cpu_freq_source = ""
        self._recording_lock = threading.RLock()
        self._recording = None
        self._recording_path = ""
        self._recording_summary_path = ""
        self._recording_error = ""
        self._recording_device = None
        self._recording_meta: dict[str, Any] = {}
        self._target_overflow_events = 0
        self._target_drop_count_baseline: int | None = None
        self._target_drop_count: int | None = None
        self._start_failure_callback = None
        self._startup_progress_timeout_s = 3.0
        self._startup_progress_min_bytes = 4096
        self._startup_no_data_timeout_s = 5.0
        self._progress_state = "idle"
        self._progress_error = ""
        self._raw_bytes_without_events = 0

    @property
    def running(self) -> bool:
        return self._running and not self._stop_event.is_set()

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    def set_start_failure_callback(self, callback) -> None:
        self._start_failure_callback = callback

    def set_stream_hub(self, stream_hub) -> None:
        self._stream_hub = stream_hub

    def start(self, device, *, addr: str | None = None, channel: int = 1,
              mode: int = 0, search_size: int = 1024,
              duration: float = 86400) -> None:
        """Start SystemView polling in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            if self.running:
                return
            raise RuntimeError("SystemView worker thread is still active")

        stop_event = threading.Event()
        generation = object()
        self._stop_event = stop_event
        self._generation = generation
        self._paused.set()
        self._running = True
        self._history.clear()
        self._stats = {"events": 0, "bytes": 0}
        self._resolved_task_names.clear()
        self._name_resolution_attempted.clear()
        self._last_name_resolution = 0.0
        self._cpu_freq_source = ""
        with self._recording_lock:
            self._recording = None
            self._recording_path = ""
            self._recording_summary_path = ""
            self._recording_error = ""
            self._recording_device = device
            self._recording_meta = {
                "addr": addr, "channel": channel, "mode": mode,
            }
        self._target_overflow_events = 0
        self._target_drop_count_baseline = None
        self._target_drop_count = None
        self._progress_state = "starting"
        self._progress_error = ""
        self._raw_bytes_without_events = 0
        self._parser = self._create_parser(device)
        failure_callback = self._start_failure_callback

        def _poll():
            initialized = False
            start_failure = None
            try:
                start_result = device.systemview_start(
                    addr, channel=channel, mode=mode, search_size=search_size,
                )
                initialized = True
                self._apply_cpu_freq_hint(device, start_result)
                start_time = time.time()
                empty_cycles = 0
                no_event_started = None
                last_read_error = None
                while not stop_event.is_set():
                    if time.time() - start_time > duration:
                        break
                    if not self._paused.is_set():
                        time.sleep(0.05)
                        continue
                    try:
                        raw = device.systemview_read_bytes(
                            duration=_SYSTEMVIEW_DELIVERY_INTERVAL,
                            max_bytes=64 * 1024,
                        )
                    except Exception as exc:
                        last_read_error = exc
                        if time.time() - start_time >= self._startup_no_data_timeout_s:
                            raise RuntimeError(
                                f"SystemView 读取失败: {exc}"
                            ) from exc
                        time.sleep(0.1)
                        continue
                    if not raw:
                        empty_cycles += 1
                        if empty_cycles == 4:
                            try:
                                device.systemview_stop()
                                time.sleep(0.5)
                                start_result = device.systemview_start(
                                    addr, channel=channel, mode=mode,
                                    search_size=search_size,
                                )
                                self._apply_cpu_freq_hint(device, start_result)
                            except Exception as exc:
                                last_read_error = exc
                        if time.time() - start_time >= self._startup_no_data_timeout_s:
                            detail = (
                                f"：{last_read_error}" if last_read_error is not None else ""
                            )
                            raise RuntimeError(
                                "SystemView 启动后未收到数据，请检查 RTT 地址、"
                                f"通道和下位机 SystemView 配置{detail}"
                            )
                        continue
                    empty_cycles = 0
                    last_read_error = None
                    self._stats["bytes"] += len(raw)
                    now = time.time()
                    evs = self._parser.feed(raw)
                    if evs:
                        self._progress_state = "streaming"
                        self._raw_bytes_without_events = 0
                        no_event_started = None
                    else:
                        self._raw_bytes_without_events += len(raw)
                        if no_event_started is None:
                            no_event_started = time.monotonic()
                        no_event_elapsed = time.monotonic() - no_event_started
                        if (
                            no_event_elapsed >= self._startup_progress_timeout_s
                            and self._raw_bytes_without_events
                            >= self._startup_progress_min_bytes
                        ):
                            raise RuntimeError(
                                "SystemView raw bytes are arriving but no decodable "
                                "events were found; the target sync packet may have "
                                "been overwritten"
                            )
                    self._note_init_cpu_freq(evs)
                    self._ensure_event_time_fields(evs)
                    self._maybe_resolve_task_names(
                        device, evs, addr=addr, channel=channel,
                        mode=mode, search_size=search_size,
                    )
                    self._apply_task_names(evs)
                    self._process_events(evs, now=now)
            except Exception as e:
                logger.error("SystemView stream error: %s", e)
                self._progress_state = "error"
                self._progress_error = str(e)
                self._bridge.put({"event": "error", "message": str(e)})
                start_failure = e
            finally:
                try:
                    device.systemview_stop()
                except Exception:
                    pass
                try:
                    self._close_recording()
                except Exception:
                    pass
                try:
                    if getattr(self, "_generation", None) is generation:
                        self._running = False
                        self._recording_device = None
                        if self._progress_state != "error":
                            self._progress_state = "stopped"
                        try:
                            self._bridge.put({"event": "stopped"})
                        finally:
                            self._bridge.stop()
                finally:
                    if start_failure is not None and failure_callback is not None:
                        try:
                            failure_callback(start_failure)
                        except Exception:
                            pass

        self._thread = threading.Thread(target=_poll, daemon=True)
        self._thread.start()

    def _process_events(self, events: list[dict], *, now: float) -> None:
        """Record every event, then publish bounded live binary batches."""
        if not events:
            return
        self._observe_target_overflows(events)
        self._stats["events"] += len(events)
        # Durable recording is deliberately ahead of all bounded live paths.
        self._record_events(events)
        self._history.extend(events)
        self._trim_history()

        if self._stream_hub is not None:
            for offset in range(0, len(events), self._live_batch_limit):
                batch = events[offset:offset + self._live_batch_limit]
                self._stream_hub.publish(
                    encode_systemview_events(batch), item_count=len(batch)
                )

        if now - self._last_status_publish >= 1.0:
            self._last_status_publish = now
            self._bridge.put({
                "event": "status",
                "stats": dict(self._stats),
                "history_size": len(self._history),
                **self._status_meta(),
            })

    def _observe_target_overflows(self, events: list[dict]) -> None:
        for event in events:
            if event.get("kind") != "overflow":
                continue
            drop_count = event.get("drop_count")
            if not isinstance(drop_count, int) or isinstance(drop_count, bool):
                continue
            drop_count &= 0xFFFFFFFF
            self._target_overflow_events += 1
            if self._target_drop_count_baseline is None:
                self._target_drop_count_baseline = drop_count
            self._target_drop_count = drop_count

    def _create_parser(self, device=None):
        from mklink.systemview_parser import SystemViewParser

        parser = SystemViewParser()
        defaults = {}
        getter = getattr(device, "_systemview_defaults", None)
        if callable(getter):
            try:
                defaults = getter()
            except Exception:
                defaults = {}
        parser._ram_base = _positive_int(defaults.get("ram_base")) or 0x20000000
        parser._id_shift = _positive_int(defaults.get("id_shift")) or 2
        freq = _positive_int(defaults.get("cpu_freq"))
        if freq:
            parser._cpu_freq = freq
            self._cpu_freq_source = str(defaults.get("cpu_freq_source") or "systemview_default")
        return parser

    def _apply_cpu_freq_hint(self, device, start_result: dict | None = None) -> int:
        p = self._parser
        if not p:
            return 0

        result = start_result or {}
        freq = 0
        source = ""
        for key in ("cpu_freq", "cpu_freq_hint"):
            freq = _positive_int(result.get(key))
            if freq:
                source = str(result.get("cpu_freq_source") or "systemview_start")
                break

        # A runtime symbol read is authoritative even when the parser was
        # seeded from a project default such as a stale RT_SYSVIEW_CPU_FREQ.
        # Explicit hints without a source retain the historical behavior too.
        runtime_hint = bool(freq) and (
            source in _RUNTIME_CPU_FREQ_SOURCES
            or ("cpu_freq_hint" in result and not result.get("cpu_freq_source"))
        )
        if runtime_hint:
            set_cpu_freq = getattr(p, "set_cpu_freq", None)
            if callable(set_cpu_freq):
                set_cpu_freq(freq, lock=True)
            else:
                p._cpu_freq = freq
            self._cpu_freq_source = source
            self._ensure_event_time_fields(self._history)
            return freq

        if p.cpu_freq:
            if p and p.cpu_freq and not self._cpu_freq_source:
                self._cpu_freq_source = "parser_default"
            return p.cpu_freq

        if not freq:
            device_parser = getattr(device, "_systemview_parser", None)
            freq = _positive_int(getattr(device_parser, "cpu_freq", 0))
            if freq:
                source = "device_parser"

        if not freq and getattr(device, "_dwarf_info", None):
            try:
                freq = _positive_int(device.read_variable("SystemCoreClock"))
                if freq:
                    source = "SystemCoreClock"
            except Exception:
                freq = 0

        if not freq:
            freq = self._profile_cpu_freq_default(device)
            if freq:
                source = "mcu_profile_default"

        if freq:
            set_cpu_freq = getattr(p, "set_cpu_freq", None)
            if callable(set_cpu_freq):
                set_cpu_freq(freq)
            else:
                p._cpu_freq = freq
            self._cpu_freq_source = source
            self._ensure_event_time_fields(self._history)
        return freq

    def _profile_cpu_freq_default(self, device) -> int:
        getter = getattr(device, "_systemview_defaults", None)
        if callable(getter):
            try:
                defaults = getter()
                freq = _positive_int(defaults.get("cpu_freq"))
                if freq:
                    return freq
            except Exception:
                pass
        profile = None
        try:
            getter = getattr(device, "_get_mcu_profile", None)
            if callable(getter):
                profile = getter()
        except Exception:
            profile = None
        if not isinstance(profile, dict):
            return 0
        for key in ("cpu_freq_default", "system_core_clock", "systemview_cpu_freq"):
            freq = _positive_int(profile.get(key))
            if freq:
                return freq
        return 0

    def _note_init_cpu_freq(self, events: list[dict]) -> None:
        if self._cpu_freq_source in _RUNTIME_CPU_FREQ_SOURCES:
            return
        for ev in events:
            if ev.get("kind") == "init" and _positive_int(ev.get("cpu_freq")):
                self._cpu_freq_source = "INIT"
                return

    def _ensure_event_time_fields(self, events: list[dict]) -> None:
        p = self._parser
        freq = _positive_int(getattr(p, "cpu_freq", 0))
        if not freq:
            return
        for ev in events:
            ticks = ev.get("t_ticks")
            if "t_us" not in ev and isinstance(ticks, (int, float)):
                ev["t_us"] = ticks * 1_000_000.0 / freq
            delta = ev.get("delta_ticks")
            if "cpu_delta_us" not in ev and isinstance(delta, (int, float)):
                ev["cpu_delta_us"] = delta * 1_000_000.0 / freq

    def _trim_history(self) -> None:
        if self._history_buffer_us > 0:
            latest_us = None
            for ev in reversed(self._history):
                t_us = ev.get("t_us")
                if isinstance(t_us, (int, float)):
                    latest_us = t_us
                    break
            if latest_us is not None:
                cutoff = latest_us - self._history_buffer_us
                self._history = [
                    ev for ev in self._history
                    if not isinstance(ev.get("t_us"), (int, float)) or ev["t_us"] >= cutoff
                ]
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    def _start_recording(self, device, extra_meta: dict | None = None) -> None:
        with self._recording_lock:
            try:
                from mklink.systemview_logger import SystemViewJsonlLogger

                project_root = getattr(device, "_project_root", None) or "."
                meta = {
                    **self._status_meta(),
                    **(extra_meta or {}),
                }
                self._recording = SystemViewJsonlLogger(project_root, meta)
                self._recording_path = str(self._recording.path)
                self._recording_summary_path = str(self._recording.summary_path)
                self._recording_error = ""
            except Exception as e:
                self._recording = None
                self._recording_error = str(e)

    def start_recording(self) -> dict:
        if not self.running:
            raise RuntimeError("SystemView stream is not running")
        with self._recording_lock:
            if self._recording is None:
                if self._recording_device is None:
                    raise RuntimeError("SystemView device is unavailable")
                self._start_recording(
                    self._recording_device, dict(self._recording_meta),
                )
            if self._recording is None:
                raise RuntimeError(
                    self._recording_error or "Unable to start recording"
                )
            return self.get_status()

    def stop_recording(self) -> dict:
        self._close_recording()
        return self.get_status()

    def _record_events(self, events: list[dict]) -> None:
        if not events:
            return
        with self._recording_lock:
            if not self._recording:
                return
            try:
                self._recording.write_events(events)
            except Exception as e:
                self._recording_error = str(e)
                try:
                    self._recording.close({"events": self._stats.get("events", 0)})
                except Exception:
                    pass
                self._recording = None

    def _close_recording(self) -> None:
        with self._recording_lock:
            if not self._recording:
                return
            try:
                self._recording.close({
                    "events": self._stats.get("events", 0),
                    "bytes": self._stats.get("bytes", 0),
                    "history_size": len(self._history),
                    "cpu_freq": _positive_int(getattr(self._parser, "cpu_freq", 0)),
                    "cpu_freq_source": self._cpu_freq_source,
                    "dropped_bytes": getattr(self._parser, "dropped_bytes", 0),
                    "dropped_packets": getattr(self._parser, "dropped_packets", 0),
                    **self._target_overflow_status(),
                })
            except Exception as e:
                self._recording_error = str(e)
            finally:
                self._recording = None

    def _target_overflow_status(self) -> dict:
        baseline = self._target_drop_count_baseline
        current = self._target_drop_count
        since_baseline = 0
        if baseline is not None and current is not None:
            since_baseline = (current - baseline) & 0xFFFFFFFF
        return {
            "target_overflow_events": self._target_overflow_events,
            "target_drop_count_baseline": baseline,
            "target_drop_count": current,
            "target_dropped_packets_since_baseline": since_baseline,
        }

    def _status_meta(self) -> dict:
        p = self._parser
        if not p:
            return {
                "synced": False,
                "abs_time": 0,
                "cpu_freq": 0,
                "cpu_freq_source": "",
                "dropped_bytes": 0,
                "dropped_packets": 0,
                "parser_dropped_bytes": 0,
                "parser_dropped_packets": 0,
                "progress_state": self._progress_state,
                "progress_error": self._progress_error,
                "raw_bytes_without_events": self._raw_bytes_without_events,
                **self._target_overflow_status(),
                "task_names": {},
                "isr_names": {},
                "recording": self._recording is not None,
                "recording_path": self._recording_path,
                "recording_summary_path": self._recording_summary_path,
                "recording_error": self._recording_error,
            }
        return {
            "synced": p.synced,
            "abs_time": p.abs_time,
            "cpu_freq": p.cpu_freq,
            "cpu_freq_source": self._cpu_freq_source,
            "dropped_bytes": p.dropped_bytes,
            "dropped_packets": p.dropped_packets,
            "parser_dropped_bytes": p.dropped_bytes,
            "parser_dropped_packets": p.dropped_packets,
            "progress_state": self._progress_state,
            "progress_error": self._progress_error,
            "raw_bytes_without_events": self._raw_bytes_without_events,
            **self._target_overflow_status(),
            "task_names": dict(p._task_names),
            "isr_names": dict(p._isr_names),
            "recording": self._recording is not None,
            "recording_path": self._recording_path,
            "recording_summary_path": self._recording_summary_path,
            "recording_error": self._recording_error,
        }

    def _unknown_task_ids(self, events: list[dict]) -> set[int]:
        p = self._parser
        if not p:
            return set()
        # TASK_INFO already provides non-disruptive names. Do not stop and
        # restart a live high-rate trace for later unknown IDs; those may be
        # false-alignment candidates and are safer to display as hex values.
        # RAM resolution remains a fallback only when startup names were lost.
        if p._task_names:
            return set()
        ram_base = int(getattr(p, "_ram_base", 0x20000000))
        ids: set[int] = set()
        for ev in events:
            tid = ev.get("task_id")
            if not isinstance(tid, int):
                continue
            if tid < ram_base:
                continue
            if tid in p._task_names or tid in self._name_resolution_attempted:
                continue
            ids.add(tid)
        return ids

    def _apply_task_names(self, events: list[dict]) -> None:
        p = self._parser
        if not p:
            return
        for ev in events:
            tid = ev.get("task_id")
            if isinstance(tid, int) and not ev.get("task_name"):
                name = p._task_names.get(tid)
                if name:
                    ev["task_name"] = name

    def _resolve_task_names(self, device, task_ids: set[int]) -> dict[int, str]:
        p = self._parser
        ram_base = int(getattr(p, "_ram_base", 0x20000000))
        ids = {int(tid) for tid in task_ids if int(tid) >= ram_base}
        if not ids:
            return {}
        names = device.systemview_resolve_task_names(sorted(ids)) or {}
        if p:
            for tid, name in names.items():
                if name:
                    p._task_names[int(tid)] = str(name)
        for ev in self._history:
            tid = ev.get("task_id")
            if isinstance(tid, int) and tid in names and names[tid]:
                ev["task_name"] = names[tid]
        self._resolved_task_names.update({int(k): str(v) for k, v in names.items() if v})
        return names

    def _maybe_resolve_task_names(
        self,
        device,
        events: list[dict],
        *,
        addr: str | None,
        channel: int,
        mode: int,
        search_size: int,
    ) -> None:
        ids = self._unknown_task_ids(events)
        if not ids:
            return
        now = time.time()
        if now - self._last_name_resolution < 3.0:
            return
        self._last_name_resolution = now
        self._name_resolution_attempted.update(ids)

        try:
            device.systemview_stop()
            try:
                self._resolve_task_names(device, ids)
            finally:
                start_result = device.systemview_start(
                    addr, channel=channel, mode=mode, search_size=search_size,
                )
                self._apply_cpu_freq_hint(device, start_result)
        except Exception as e:
            logger.debug("SystemView task-name resolution failed: %s", e)

    def stop(self, timeout: float = 5.0) -> bool:
        thread = self._thread
        self._stop_event.set()
        if thread:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError("SystemView worker thread is still active")
        self._running = False
        if self._thread is thread:
            self._thread = None
        return True

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def get_history(self) -> list[dict]:
        return list(self._history)

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "paused": self.paused,
            "clients": self._bridge.client_count,
            "stats": self._stats,
            "history_size": len(self._history),
            "stream": self._stream_hub.stats().__dict__ if self._stream_hub else None,
            **self._status_meta(),
        }

    async def sse_generator(self):
        """Async SSE generator for FastAPI StreamingResponse."""
        q = self._bridge.add_client()
        yield _sse_json({"event": "status", **self.get_status()})
        try:
            while self.running or self.paused:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield _sse_format("", event="ping")
                    continue
                if data is None:
                    break
                yield _sse_json(data)
                if data.get("event") == "stopped":
                    break
        finally:
            self._bridge.remove_client(q)


# ---------------------------------------------------------------------------
# SuperWatch SSE Generator
# ---------------------------------------------------------------------------

SUPERWATCH_MIN_INTERVAL = 0.00001


def normalize_superwatch_interval(interval: float) -> float:
    """Return a finite SuperWatch interval supported by the desktop UI."""
    try:
        value = float(interval)
    except (TypeError, ValueError):
        raise ValueError("SuperWatch interval must be a finite number") from None
    if (
        not math.isfinite(value)
        or value < SUPERWATCH_MIN_INTERVAL
        or value > 60.0
    ):
        raise ValueError(
            "SuperWatch interval must be finite and in the range [0.00001, 60]"
        )
    return value


class SuperWatchTransactionError(RuntimeError):
    """Identifies the failed phase of a serialized SuperWatch operation."""

    code = "superwatch_transaction_failed"

    def __init__(self, phase: str, cause: Exception):
        self.phase = phase
        self.cause = cause
        super().__init__(str(cause))

    def to_detail(self) -> dict:
        return {
            "code": self.code,
            "phase": self.phase,
            "message": str(self.cause),
        }


class SuperWatchStreamManager:
    """Manages SuperWatch variable polling with SSE output.

    Uses SuperWatchRuntime for DWARF-based variable resolution and
    efficient block-based memory reads.
    """

    def __init__(
        self, stream_hub=None, *, batch_samples: int = 32,
        clock=time.perf_counter,
    ):
        self._bridge = AsyncBridge()
        self._thread: threading.Thread | None = None
        self._running = False
        self._collecting = threading.Event()
        self._stop_event = threading.Event()
        self._interval = 0.001  # 1ms default
        self._device = None
        self._runtime = None
        # One re-entrant lock owns runtime layout snapshots, pending sample
        # rows, and metadata transitions. Hardware reads happen outside it.
        self._read_lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._origin_us: int | None = None
        self._stream_hub = stream_hub
        self._batch_samples = max(1, int(batch_samples))
        self._clock = clock
        self._pending_samples: list[tuple[float, ...]] = []
        # Empty layout version 1 is the immutable bootstrap replay for clients
        # that subscribe before a runtime is prepared.  Cache replacement is
        # one reference assignment, so readers never observe payload/snapshot
        # from different layouts.
        empty_channels_json = "[]"
        self._metadata_version = 1
        self._published_metadata_signature = empty_channels_json
        self._metadata_cache = (
            encode_superwatch_metadata(1, []),
            empty_channels_json,
            1,
        )
        self._metadata_publish_lock = threading.Lock()
        self._last_metadata_publish_monotonic = 0.0
        self._config_generation = 0
        self._completed_read_cycles = 0
        self._dropped_read_cycles = 0
        self._read_errors = 0
        self._rate_timestamps = deque()
        self._actual_rate = 0.0
        self._binary_dropped_batches = 0
        self._binary_dropped_items = 0
        self._acquisition_mode = "idle"
        self._stream_integrity: dict[str, int] = {}
        self._dump_restart = threading.Event()
        self._array_snapshot: dict[str, Any] | None = None
        self.set_stream_hub(stream_hub)

    def set_stream_hub(self, stream_hub) -> None:
        if self._stream_hub is not None and self._stream_hub is not stream_hub:
            self._stream_hub.set_subscribe_callback(None)
        self._stream_hub = stream_hub
        if stream_hub is not None:
            stream_hub.set_subscribe_callback(self._publish_subscriber_metadata)

    def detach_stream_hub(self, stream_hub) -> None:
        if self._stream_hub is stream_hub:
            stream_hub.set_subscribe_callback(None)
            self._stream_hub = None

    def _publish_subscriber_metadata(self, enqueue_initial) -> None:
        # Called synchronously by StreamHub.subscribe(), potentially from the
        # asyncio event-loop thread. It must never acquire the layout/read lock.
        with self._metadata_publish_lock:
            payload, _snapshot_json, _version = self._metadata_cache
            enqueue_initial(
                payload,
                item_count=0,
                flags=SUPERWATCH_METADATA_JSON,
                stream_type=StreamType.SUPERWATCH,
            )

    @property
    def running(self) -> bool:
        return self._running and not self._stop_event.is_set()

    def prepare(self, device) -> None:
        """Build runtime from DWARF info so search/add work before collection starts."""
        with self._read_lock:
            self._device = device
            if self._runtime is not None:
                runtime = self._runtime
                new_catalog = getattr(device, "symbol_catalog", None)
                if getattr(runtime, "symbol_catalog", None) is not new_catalog:
                    from mklink.superwatch import SuperWatchRuntime

                    register_items = [
                        item for item in runtime.items
                        if getattr(item, "source", "ram") != "ram"
                    ]
                    selected_paths = [
                        item.name for item in runtime.items
                        if getattr(item, "source", "ram") == "ram"
                    ]
                    rebound = SuperWatchRuntime(
                        items=register_items,
                        dwarf_info=getattr(device, "_dwarf_info", None),
                        symbol_catalog=new_catalog,
                        svd_registers=getattr(runtime, "svd_registers", {}),
                        port=getattr(device, "_port", None),
                        read_lock=self._read_lock,
                    )
                    if new_catalog is not None:
                        for path in selected_paths:
                            rebound.add(path)
                    self._runtime = rebound
                    self._rebind_array_snapshot_locked(new_catalog)
                    self._origin_us = None
                    self._flush_binary_batch_locked()
                    self._rebuild_metadata_cache_locked(publish=True)
                else:
                    runtime.port = getattr(device, "_port", None)
                    runtime.dwarf_info = getattr(device, "_dwarf_info", None)
                return
            dwarf_info = getattr(device, "_dwarf_info", None)
            svd_registers = {}
            try:
                from mklink.superwatch import find_project_svd, load_svd_registers
                project_root = getattr(device, "_project_root", ".")
                svd_path = find_project_svd(project_root)
                if svd_path:
                    svd_registers = load_svd_registers(svd_path)
            except Exception:
                pass
            from mklink.superwatch import SuperWatchRuntime
            self._runtime = SuperWatchRuntime(
                items=[],
                dwarf_info=dwarf_info,
                symbol_catalog=getattr(device, "symbol_catalog", None),
                svd_registers=svd_registers,
                port=getattr(device, "_port", None),
                read_lock=self._read_lock,
            )
            self._rebuild_metadata_cache_locked(publish=True)

    def _build_array_snapshot_locked(
        self,
        catalog,
        name: str,
        start_index: int,
        count: int,
    ) -> dict[str, Any]:
        from mklink.superwatch import WatchItem

        descriptors = catalog.array_descriptors(
            name,
            start_index=start_index,
            count=count,
        )
        items = tuple(
            WatchItem(
                descriptor.path,
                descriptor.address,
                descriptor.type_name,
                descriptor.size,
                enum_values={
                    value: label for label, value in descriptor.enum_values.items()
                },
                scalar_kind=(
                    "signed"
                    if descriptor.scalar_kind == "enum" and descriptor.enum_signed
                    else descriptor.scalar_kind
                ),
            )
            for descriptor in descriptors
        )
        return {
            "name": name,
            "type_name": descriptors[0].type_name,
            "address": descriptors[0].address,
            "element_size": descriptors[0].size,
            "start_index": start_index,
            "count": len(descriptors),
            "items": items,
            "sequence": 0,
            "timestamp_us": None,
            "values": [],
        }

    def _rebind_array_snapshot_locked(self, catalog) -> None:
        if self._array_snapshot is None:
            return
        snapshot = self._array_snapshot
        try:
            self._array_snapshot = self._build_array_snapshot_locked(
                catalog,
                snapshot["name"],
                snapshot["start_index"],
                snapshot["count"],
            )
        except Exception:
            self._array_snapshot = None

    def _sampling_layout_locked(self):
        from mklink.superwatch import build_read_blocks

        scalar_items = tuple(self._runtime.items) if self._runtime is not None else ()
        if self._array_snapshot is None:
            blocks = tuple(self._runtime.blocks) if self._runtime is not None else ()
            return scalar_items, scalar_items, blocks
        items_by_name = {item.name: item for item in scalar_items}
        for item in self._array_snapshot["items"]:
            items_by_name.setdefault(item.name, item)
        items = tuple(items_by_name.values())
        return scalar_items, items, tuple(build_read_blocks(items, max_gap=256))

    def start(self, device) -> None:
        if self._thread is not None and self._thread.is_alive():
            if self.running:
                return
            raise RuntimeError("SuperWatch worker thread is still active")
        self.prepare(device)
        stop_event = threading.Event()
        generation = object()
        self._stop_event = stop_event
        self._generation = generation
        self._collecting.set()
        self._dump_restart.clear()
        self._running = True
        with self._read_lock:
            self._origin_us = None
            self._flush_binary_batch_locked()
            self._rate_timestamps.clear()
            self._actual_rate = 0.0

        def _poll():
            try:
                while not stop_event.is_set():
                    with self._read_lock:
                        if (
                            self._collecting.is_set()
                            and self._runtime is not None
                            and bool(self._runtime.items or self._array_snapshot)
                        ):
                            runtime = self._runtime
                            scalar_items, items, blocks = self._sampling_layout_locked()
                            config_generation = self._config_generation
                            origin_us = self._origin_us
                        else:
                            runtime = None
                    if runtime is None:
                        time.sleep(0.5)
                        continue
                    bridge = getattr(device, "_bridge", None)
                    supports_dump = bridge is not None and all(
                        callable(getattr(bridge, name, None))
                        for name in (
                            "_enter_stream", "_write_raw",
                            "drain_stream_bytes", "_exit_stream",
                        )
                    )
                    if supports_dump and 0 < len(blocks) <= 16:
                        from mklink.dump_memory import (
                            DumpMemoryStreamSession,
                            decode_frame_to_points,
                        )

                        self._acquisition_mode = "dump-memory"
                        region_pairs = [(block.address, block.size) for block in blocks]
                        block_addresses = [
                            (
                                block.address,
                                block.size,
                                [
                                    (
                                        item.name,
                                        item.type_name,
                                        item.address - block.address,
                                        item.size,
                                        getattr(item, "scalar_kind", None),
                                        item.enum_values,
                                    )
                                    for item in block.items
                                ],
                            )
                            for block in blocks
                        ]
                        session = DumpMemoryStreamSession(
                            bridge, region_pairs, self._interval,
                        )
                        completed_integrity = dict(self._stream_integrity)
                        try:
                            session.start()
                            while (
                                not stop_event.is_set()
                                and config_generation == self._config_generation
                                and not self._dump_restart.is_set()
                            ):
                                frames = session.read_frames(max_bytes=1024 * 1024)
                                self._stream_integrity = _sum_counter_snapshots(
                                    completed_integrity, session.stats,
                                )
                                if not frames:
                                    stop_event.wait(0.0005)
                                    continue
                                if not self._collecting.is_set():
                                    continue
                                for frame in frames:
                                    points, origin_us = decode_frame_to_points(
                                        frame, block_addresses, origin_us,
                                    )
                                    with self._read_lock:
                                        if config_generation != self._config_generation:
                                            self._dropped_read_cycles += 1
                                            break
                                        self._origin_us = origin_us
                                        published = self._publish_sample_points_locked(
                                            points,
                                            names=tuple(item.name for item in scalar_items),
                                        )
                                        snapshot_updated = self._update_array_snapshot_locked(points)
                                        if snapshot_updated and not published:
                                            self._record_sample_rate_locked()
                                        if published or snapshot_updated:
                                            self._completed_read_cycles += 1
                        finally:
                            session.stop()
                            self._stream_integrity = _sum_counter_snapshots(
                                completed_integrity, session.stats,
                            )
                            self._dump_restart.clear()
                        continue
                    self._acquisition_mode = "read-memory"
                    t0 = time.monotonic()
                    try:
                        from mklink.superwatch import sample_blocks
                        result = sample_blocks(
                            blocks,
                            origin_us=origin_us,
                            bridge=device._bridge,
                        )
                        with self._read_lock:
                            if (
                                runtime is not self._runtime
                                or config_generation != self._config_generation
                            ):
                                self._dropped_read_cycles += 1
                            else:
                                self._origin_us = result.origin_us
                                published = self._publish_sample_points_locked(
                                    result.points,
                                    names=tuple(item.name for item in scalar_items),
                                )
                                snapshot_updated = self._update_array_snapshot_locked(result.points)
                                if snapshot_updated and not published:
                                    self._record_sample_rate_locked()
                                if published or snapshot_updated:
                                    self._completed_read_cycles += 1
                    except Exception as e:
                        with self._read_lock:
                            self._read_errors += 1
                        logger.debug("SuperWatch poll error: %s", e)
                        self._bridge.put({"event": "error", "message": str(e)})
                    elapsed = time.monotonic() - t0
                    remaining = max(0.0, self._interval - elapsed)
                    stop_event.wait(timeout=remaining)
            except Exception as e:
                logger.error("SuperWatch stream error: %s", e)
                self._bridge.put({"event": "error", "message": str(e)})
            finally:
                self._flush_binary_batch()
                if getattr(self, "_generation", None) is generation:
                    self._running = False
                    self._bridge.put({"event": "stopped"})
                    self._bridge.stop()

        self._thread = threading.Thread(target=_poll, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        thread = self._thread
        self._stop_event.set()
        self._collecting.clear()
        if thread:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError("SuperWatch worker thread is still active")
        self._running = False
        self._flush_binary_batch()
        if self._thread is thread:
            self._thread = None
        return True

    def _readback_once(self, address: int, size: int) -> bytes:
        if self._device is None:
            raise RuntimeError("SuperWatch device is unavailable")
        # The continuous dump stream is already stopped. Re-entering dump mode
        # for a one-shot read can leave firmware unable to restart streaming.
        return self._device.read_memory(address, size)

    def write_symbol(self, path: str, *, generation: int, value: object) -> dict:
        from mklink.symbol_catalog import decode_descriptor, encode_descriptor

        with self._operation_lock:
            device = self._device
            if device is None:
                raise RuntimeError("SuperWatch device is unavailable")
            catalog = getattr(device, "symbol_catalog", None)
            if catalog is None:
                raise RuntimeError("No AXF symbol catalog is loaded")
            descriptor = catalog.require(path, generation)
            if not descriptor.writable:
                raise RuntimeError(f"Symbol is read-only: {path}")
            payload = encode_descriptor(descriptor, value)
            was_running = self.running
            was_collecting = self._collecting.is_set()

            try:
                if was_running:
                    try:
                        self.stop()
                    except Exception as exc:
                        raise SuperWatchTransactionError("stop", exc) from exc
                try:
                    device.write_memory(descriptor.address, payload)
                except Exception as exc:
                    raise SuperWatchTransactionError("write", exc) from exc
                try:
                    actual_raw = self._readback_once(descriptor.address, descriptor.size)
                    if actual_raw[: descriptor.size] != payload:
                        raise RuntimeError(f"SuperWatch readback mismatch for {path}")
                    actual = decode_descriptor(descriptor, actual_raw)
                except Exception as exc:
                    raise SuperWatchTransactionError("readback", exc) from exc
                return {
                    "path": path,
                    "generation": catalog.generation,
                    "value": actual,
                    "verified": True,
                }
            finally:
                if was_running:
                    try:
                        self.start(device)
                        if not was_collecting:
                            self.pause()
                    except Exception as exc:
                        raise SuperWatchTransactionError("restore", exc) from exc

    def reparse_symbols(
        self,
        axf_path: str | None = None,
        elf_backend: str | None = None,
        *,
        device=None,
    ) -> dict:
        target_device = device or self._device
        if target_device is None:
            raise RuntimeError("SuperWatch device is unavailable")

        def load_catalog():
            if elf_backend is None:
                return target_device.reparse_axf_atomically(axf_path)
            return target_device.reparse_axf_atomically(
                axf_path, elf_backend=elf_backend
            )

        return self._replace_symbol_catalog(
            target_device,
            load_catalog,
            failure_phase="reparse",
        )

    def apply_c_definition(
        self,
        variable_name: str,
        definition: str,
        pack: int | None = None,
        *,
        device=None,
    ) -> dict:
        target_device = device or self._device
        if target_device is None:
            raise RuntimeError("SuperWatch device is unavailable")
        applied: dict = {}

        def load_catalog():
            new_catalog, layout = target_device.apply_c_definition(
                variable_name,
                definition,
                pack,
            )
            applied["layout"] = layout.to_dict()
            return new_catalog

        rebind = self._replace_symbol_catalog(
            target_device,
            load_catalog,
            failure_phase="c_layout",
        )
        return {"layout": applied["layout"], "rebind": rebind}

    def _replace_symbol_catalog(
        self,
        target_device,
        load_catalog,
        *,
        failure_phase: str,
    ) -> dict:
        from mklink.superwatch import SuperWatchRuntime
        from mklink.symbol_catalog import RebindSummary, rebind_paths

        with self._operation_lock:
            old_catalog = getattr(target_device, "symbol_catalog", None)
            if old_catalog is None:
                raise RuntimeError("No AXF symbol catalog is loaded")
            old_runtime = self._runtime
            symbol_paths = tuple(
                item.name
                for item in getattr(old_runtime, "items", ())
                if getattr(item, "source", "ram") == "ram"
            )
            register_items = [
                item
                for item in getattr(old_runtime, "items", ())
                if getattr(item, "source", "ram") != "ram"
            ]
            was_running = self.running
            was_collecting = self._collecting.is_set()

            try:
                if was_running:
                    try:
                        self.stop()
                    except Exception as exc:
                        raise SuperWatchTransactionError("stop", exc) from exc
                self.prepare(target_device)
                try:
                    new_catalog = load_catalog()
                except Exception as exc:
                    raise SuperWatchTransactionError(failure_phase, exc) from exc
                try:
                    summary = rebind_paths(old_catalog, new_catalog, symbol_paths)
                    new_runtime = SuperWatchRuntime(
                        items=register_items,
                        dwarf_info=getattr(target_device, "_dwarf_info", None),
                        symbol_catalog=new_catalog,
                        svd_registers=getattr(old_runtime, "svd_registers", {}),
                        port=getattr(target_device, "_port", None),
                        read_lock=self._read_lock,
                    )
                    rebound_removed = list(summary.removed)
                    for path in (*summary.preserved, *summary.updated):
                        result = new_runtime.add(path)
                        if result.get("error"):
                            rebound_removed.append(path)
                    if rebound_removed != list(summary.removed):
                        rebound_set = set(rebound_removed)
                        summary = RebindSummary(
                            tuple(path for path in summary.preserved if path not in rebound_set),
                            tuple(path for path in summary.updated if path not in rebound_set),
                            tuple(rebound_removed),
                        )
                except Exception as exc:
                    raise SuperWatchTransactionError("rebind", exc) from exc
                with self._read_lock:
                    self._runtime = new_runtime
                    self._rebind_array_snapshot_locked(new_catalog)
                    self._origin_us = None
                    self._flush_binary_batch_locked()
                    self._rebuild_metadata_cache_locked(publish=True)
                return summary.to_dict()
            finally:
                if was_running:
                    try:
                        self.start(target_device)
                        if not was_collecting:
                            self.pause()
                    except Exception as exc:
                        raise SuperWatchTransactionError("restore", exc) from exc

    def add_watch(self, name: str) -> dict:
        with self._read_lock:
            if self._runtime is None:
                return {"error": "SuperWatch not started"}
            self._flush_binary_batch_locked()
            result = self._runtime.add(name)
            self._rebuild_metadata_cache_locked(publish=True)
            return {"item": result}

    def remove_watch(self, name: str) -> dict:
        with self._read_lock:
            if self._runtime is None:
                return {"error": "SuperWatch not started"}
            self._flush_binary_batch_locked()
            result = self._runtime.remove(name)
            self._rebuild_metadata_cache_locked(publish=True)
            return {"item": result}

    def select_array_snapshot(
        self,
        name: str,
        *,
        start_index: int,
        count: int,
    ) -> dict:
        with self._read_lock:
            if self._runtime is None or self._runtime.symbol_catalog is None:
                return {"error": "SuperWatch symbol catalog is unavailable"}
            snapshot = self._build_array_snapshot_locked(
                self._runtime.symbol_catalog,
                name,
                start_index,
                count,
            )
            self._array_snapshot = snapshot
            self._config_generation += 1
            self._origin_us = None
            self._dump_restart.set()
            return {"snapshot": self._array_snapshot_payload_locked()}

    def clear_array_snapshot(self) -> dict:
        with self._read_lock:
            if self._array_snapshot is not None:
                self._array_snapshot = None
                self._config_generation += 1
                self._origin_us = None
                self._dump_restart.set()
            return {"snapshot": None}

    def get_array_snapshot(self) -> dict:
        with self._read_lock:
            return {"snapshot": self._array_snapshot_payload_locked()}

    def _array_snapshot_payload_locked(self) -> dict | None:
        if self._array_snapshot is None:
            return None
        return {
            key: value
            for key, value in self._array_snapshot.items()
            if key != "items"
        }

    def _update_array_snapshot_locked(self, points) -> bool:
        if self._array_snapshot is None:
            return False
        names = tuple(item.name for item in self._array_snapshot["items"])
        merged: dict[str, Any] = {}
        timestamp_us = None
        for point in points:
            for name in names:
                if name in point:
                    merged[name] = point[name]
            if point.get("timestamp_us") is not None:
                timestamp_us = point["timestamp_us"]
        if any(name not in merged for name in names):
            return False
        try:
            values = [float(merged[name]) for name in names]
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in values):
            return False
        self._array_snapshot = {
            **self._array_snapshot,
            "sequence": self._array_snapshot["sequence"] + 1,
            "timestamp_us": timestamp_us,
            "values": values,
        }
        return True

    def publish_metadata(self, *, force: bool = False) -> int:
        with self._read_lock:
            return self._rebuild_metadata_cache_locked(publish=force)

    def _rebuild_metadata_cache_locked(self, *, publish: bool = False) -> int:
        channels = self._list_watches_locked()
        signature = json.dumps(channels, sort_keys=True, default=str)
        snapshot_json = json.dumps(channels, separators=(",", ":"), default=str)
        with self._metadata_publish_lock:
            changed = signature != self._published_metadata_signature
            if changed:
                self._metadata_version += 1
                self._config_generation += 1
            payload = encode_superwatch_metadata(self._metadata_version, channels)
            self._published_metadata_signature = signature
            self._metadata_cache = (
                payload,
                snapshot_json,
                self._metadata_version,
            )
            if (changed or publish) and self._stream_hub is not None:
                self._stream_hub.publish(
                    payload,
                    item_count=0,
                    flags=SUPERWATCH_METADATA_JSON,
                    stream_type=StreamType.SUPERWATCH,
                )
        self._last_metadata_publish_monotonic = time.monotonic()
        return self._metadata_version

    def _publish_cached_metadata(self) -> int:
        with self._metadata_publish_lock:
            payload, _snapshot_json, version = self._metadata_cache
            stream_hub = self._stream_hub
            if stream_hub is not None:
                stream_hub.publish(
                    payload,
                    item_count=0,
                    flags=SUPERWATCH_METADATA_JSON,
                    stream_type=StreamType.SUPERWATCH,
                )
            self._last_metadata_publish_monotonic = time.monotonic()
            return version

    def publish_sample_points(self, points) -> bool:
        with self._read_lock:
            return self._publish_sample_points_locked(points)

    def _publish_sample_points_locked(self, points, *, names=None) -> bool:
        if names is None:
            if self._runtime is None or not self._runtime.items:
                return False
            names = tuple(item.name for item in self._runtime.items)
        if not names:
            return False
        merged = {}
        for point in points:
            for name in names:
                if name in point:
                    merged[name] = point[name]
        if any(name not in merged for name in names):
            return False
        try:
            row = tuple(float(merged[name]) for name in names)
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in row):
            return False
        if time.monotonic() - self._last_metadata_publish_monotonic >= 1.0:
            self._publish_cached_metadata()
        self._record_sample_rate_locked()
        self._pending_samples.append(row)
        if len(self._pending_samples) >= self._batch_samples:
            self._flush_binary_batch_locked()
        return True

    def _record_sample_rate_locked(self) -> None:
        completed_at = self._clock()
        self._rate_timestamps.append(completed_at)
        cutoff = completed_at - 1.0
        while self._rate_timestamps and self._rate_timestamps[0] < cutoff:
            self._rate_timestamps.popleft()
        if len(self._rate_timestamps) >= 2:
            elapsed = self._rate_timestamps[-1] - self._rate_timestamps[0]
            self._actual_rate = (
                (len(self._rate_timestamps) - 1) / elapsed if elapsed > 0 else 0.0
            )
        else:
            self._actual_rate = 0.0

    def _flush_binary_batch(self) -> bool:
        with self._read_lock:
            return self._flush_binary_batch_locked()

    def _flush_binary_batch_locked(self) -> bool:
        if not self._pending_samples:
            return True
        pending = self._pending_samples
        self._pending_samples = []
        try:
            if self._stream_hub is None:
                raise RuntimeError("SuperWatch binary stream hub is unavailable")
            self._stream_hub.publish(
                encode_waveform_samples(pending), item_count=len(pending),
                flags=SUPERWATCH_SAMPLE_MAJOR_FLOAT32,
                stream_type=StreamType.SUPERWATCH,
            )
        except Exception as exc:
            self._binary_dropped_batches += 1
            self._binary_dropped_items += len(pending)
            logger.warning("SuperWatch binary batch dropped: %s", exc)
            return False
        return True

    def search(self, query: str) -> list[dict]:
        if self._runtime is None:
            return []
        return self._runtime.search(query)

    def pause(self) -> None:
        self._collecting.clear()
        with self._read_lock:
            self._rate_timestamps.clear()
            self._actual_rate = 0.0

    def resume(self) -> None:
        with self._read_lock:
            self._rate_timestamps.clear()
            self._actual_rate = 0.0
        self._collecting.set()

    def start_collecting(self) -> None:
        self._collecting.set()

    def set_interval(self, interval: float) -> float:
        normalized = normalize_superwatch_interval(interval)
        with self._read_lock:
            self._interval = normalized
            self._rate_timestamps.clear()
            self._actual_rate = 0.0
        self._dump_restart.set()
        return self._interval

    def get_status(self) -> dict:
        if self._collecting.is_set():
            state = "running"
        elif self._running:
            state = "paused"
        else:
            state = "stopped"
        _payload, snapshot_json, metadata_version = self._metadata_cache
        return {
            "state": state,
            "interval": self._interval,
            "items": json.loads(snapshot_json),
            "metadata_version": metadata_version,
            "read_cycles": self._completed_read_cycles,
            "read_drops": self._dropped_read_cycles,
            "read_errors": self._read_errors,
            "actual_rate": round(self._actual_rate, 6),
            "binary_drops": {
                "batches": self._binary_dropped_batches,
                "items": self._binary_dropped_items,
            },
            "acquisition_mode": self._acquisition_mode,
            "stream_integrity": dict(self._stream_integrity),
            "stream": self._stream_hub.stats().__dict__ if self._stream_hub else None,
        }

    def list_watches(self) -> list[dict]:
        _payload, snapshot_json, _version = self._metadata_cache
        return json.loads(snapshot_json)

    def _list_watches_locked(self) -> list[dict]:
        if self._runtime is None:
            return []
        from mklink.superwatch import make_channel_metadata
        meta = make_channel_metadata(self._runtime.items)
        return [{"name": item.name, **meta.get(item.name, {})} for item in self._runtime.items]

    def inspect(self, name: str) -> dict | None:
        if self._runtime is None or not self._device:
            return None
        try:
            return self._runtime.inspect(name)
        except Exception as e:
            logger.warning("SuperWatch inspect error for %s: %s", name, e)
            return None

    async def sse_generator(self):
        q = self._bridge.add_client()
        # Send initial channel metadata for already-added variables
        items = self.list_watches()
        if items:
            meta = {
                item["name"]: {
                    key: value for key, value in item.items() if key != "name"
                }
                for item in items
            }
            yield _sse_json({"event": "channel_metadata", "channels": meta})
        state = "running" if self._collecting.is_set() else ("paused" if self._running else "stopped")
        yield _sse_json({"event": "state_change", "state": state, "items": self.list_watches()})
        try:
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield _sse_format("", event="ping")
                    continue
                if data is None:
                    break
                yield _sse_json(data)
                if data.get("event") == "stopped":
                    break
        finally:
            self._bridge.remove_client(q)


# ---------------------------------------------------------------------------
# Serial SSE Generator
# ---------------------------------------------------------------------------

class SerialStreamManager:
    """Manages serial port monitoring with SSE output."""

    def __init__(self, stream_hub=None):
        self._bridge = AsyncBridge()
        self._monitor = None
        self._running = False
        self._port_config: list[dict] = []
        self._profile: dict | None = None
        self._auto_reply_rules: list[dict] | None = None
        self._rx_count = 0
        self._tx_count = 0
        self._rx_bytes = 0
        self._tx_bytes = 0
        self._start_time = 0.0
        self._stream_hub = stream_hub

    def set_stream_hub(self, stream_hub) -> None:
        self._stream_hub = stream_hub

    def detach_stream_hub(self, stream_hub) -> None:
        if self._stream_hub is stream_hub:
            self._stream_hub = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, ports: list[dict], profile: dict | None = None,
              auto_reply_rules: list[dict] | None = None) -> None:
        if self._running:
            return

        from mklink.serial._monitor import SerialMonitor

        self._port_config = ports
        self._profile = profile
        self._auto_reply_rules = auto_reply_rules
        self._rx_count = 0
        self._tx_count = 0
        self._rx_bytes = 0
        self._tx_bytes = 0
        self._start_time = time.time()

        def _event_callback(event):
            if event.direction == "RX":
                self._rx_count += 1
            else:
                self._tx_count += 1
            if self._bridge.client_count == 0:
                return
            ts = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
            ms = int((event.timestamp % 1) * 1000)
            raw_hex = event.raw.hex().upper()
            try:
                ascii_repr = event.raw.decode("ascii", errors="replace")
            except Exception:
                ascii_repr = ""

            fields = {}
            crc_valid = None
            if event.parsed:
                crc_valid = event.parsed.crc_valid
                if event.parsed.fields:
                    for k, v in event.parsed.fields.items():
                        if isinstance(v, dict):
                            fields[k] = {"value": v.get("value", ""), "unit": v.get("unit", "")}
                        else:
                            fields[k] = {"value": str(v), "unit": ""}

            self._bridge.put({
                "event": "data",
                "timestamp": f"{ts}.{ms:03d}",
                "port": event.port,
                "direction": event.direction,
                "raw_hex": raw_hex,
                "ascii": ascii_repr,
                "fields": fields,
                "crc_valid": crc_valid,
            })

        def _chunk_callback(
            port: str,
            direction: str,
            data: bytes,
            timestamp: float,
        ):
            if direction == "RX":
                self._rx_bytes += len(data)
            else:
                self._tx_bytes += len(data)
            if self._stream_hub is not None:
                self._stream_hub.publish(
                    data,
                    item_count=len(data),
                    flags=(SERIAL_RX_BYTES if direction == "RX" else SERIAL_TX_BYTES),
                    stream_type=StreamType.SERIAL,
                )
            if self._bridge.client_count == 0:
                return
            self._bridge.put({
                "event": "terminal",
                "timestamp": timestamp,
                "port": port,
                "direction": direction,
                "data_base64": base64.b64encode(data).decode("ascii"),
            })

        self._monitor = SerialMonitor(
            ports=ports,
            profile=profile,
            auto_reply_rules=auto_reply_rules,
            event_callback=_event_callback,
            chunk_callback=_chunk_callback,
        )
        self._monitor.start()
        self._running = True
        self._bridge.put({"event": "status", **self.get_status()})

    def stop(self) -> None:
        if self._monitor:
            self._monitor.stop()
            self._monitor = None
        self._running = False
        self._bridge.put({"event": "stopped"})
        self._bridge.stop()

    def send(self, port: str, data: bytes) -> bool:
        if not self._monitor:
            return False
        return self._monitor.send(port, data)

    def send_all(self, data: bytes) -> None:
        if self._monitor:
            self._monitor.send_all(data)

    def get_status(self) -> dict:
        elapsed = max(time.time() - self._start_time, 1.0)
        ports = {}
        if self._monitor:
            ports = self._monitor.port_status
        return {
            "running": self._running,
            "ports": ports,
            "config": [dict(config) for config in self._port_config],
            "stats": {
                "rx_count": self._rx_count,
                "tx_count": self._tx_count,
                "rx_bytes": self._rx_bytes,
                "tx_bytes": self._tx_bytes,
                "bytes_per_sec": round((self._rx_bytes + self._tx_bytes) / elapsed, 1),
            },
            "stream": self._stream_hub.stats().__dict__ if self._stream_hub else None,
        }

    async def sse_generator(self):
        q = self._bridge.add_client()
        yield _sse_json({"event": "status", **self.get_status()})
        try:
            while self.running:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield _sse_format("", event="ping")
                    continue
                if data is None:
                    break
                yield _sse_json(data)
                if data.get("event") == "stopped":
                    break
        finally:
            self._bridge.remove_client(q)


# ---------------------------------------------------------------------------
# Modbus SSE Generator
# ---------------------------------------------------------------------------

class ModbusStreamManager:
    """Manages Modbus register polling with SSE output."""

    def __init__(self):
        self._bridge = AsyncBridge()
        self._thread: threading.Thread | None = None
        self._running = False
        self._client = None
        self._slave: int = 1
        self._specs: list = []
        self._interval: float = 1.0
        self._stop_event = threading.Event()
        self._history: list[dict] = []
        self._max_history = 500
        self._latest: dict = {}

    @property
    def running(self) -> bool:
        return self._running

    def start(self, client, slave: int, registers: list[dict] | None = None,
              interval: float = 1.0) -> None:
        """Start Modbus register polling.

        Args:
            client: ModbusClient instance
            slave: Slave address
            registers: List of {addr, type?, name?} dicts. If None, reads 0-9.
            interval: Polling interval in seconds
        """
        if self._running:
            return

        self._client = client
        self._slave = slave
        self._interval = interval
        self._stop_event.clear()
        self._latest = {}

        if registers:
            from mklink.modbus._format import RegisterSpec
            self._specs = [
                RegisterSpec(
                    addr=r["addr"],
                    type=r.get("type", "uint16"),
                    name=r.get("name", ""),
                ) for r in registers
            ]
        else:
            from mklink.modbus._format import RegisterSpec
            self._specs = [RegisterSpec(addr=i, type="uint16", name=f"R{i}")
                           for i in range(10)]

        self._running = True

        def _poll():
            from mklink.modbus._format import registers_to_values
            from mklink.modbus._poller import _group_consecutive
            try:
                while not self._stop_event.is_set():
                    now = time.time()
                    try:
                        result: dict[str, Any] = {"_t": now, "registers": {}}
                        groups = _group_consecutive(self._specs)
                        for group in groups:
                            start_addr = group[0].addr
                            count = sum(s.reg_count for s in group)
                            n = min(count, 125)
                            regs = self._client.read_holding_registers(
                                start_addr, n, self._slave
                            )
                            for spec in group:
                                offset = spec.addr - start_addr
                                if 0 <= offset + spec.reg_count <= len(regs):
                                    raw = regs[offset:offset + spec.reg_count]
                                    vals = registers_to_values(raw, spec.type)
                                    if vals:
                                        result["registers"][spec.addr] = {
                                            "value": vals[0],
                                            "name": spec.name,
                                            "type": spec.type,
                                        }
                        self._latest = result
                        self._bridge.put({"event": "data", **result})
                        self._history.append(result)
                        if len(self._history) > self._max_history:
                            self._history = self._history[-self._max_history:]
                    except Exception as e:
                        logger.debug("Modbus poll error: %s", e)
                        self._bridge.put({"event": "error", "message": str(e)})
                    self._stop_event.wait(self._interval)
            except Exception as e:
                logger.error("Modbus stream error: %s", e)
                self._bridge.put({"event": "error", "message": str(e)})
            finally:
                self._running = False
                self._bridge.put({"event": "stopped"})
                self._bridge.stop()

        self._thread = threading.Thread(target=_poll, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

    def write_register(self, addr: int, value: int) -> dict:
        if not self._client:
            raise RuntimeError("Modbus not connected")
        self._client.write_register(addr, value, self._slave)
        return {"addr": addr, "value": value, "ok": True}

    def read_debug(self, fc: int, start: int, quantity: int) -> list:
        if not self._client:
            raise RuntimeError("Modbus not connected")
        if fc == 3:
            return self._client.read_holding_registers(start, quantity, self._slave)
        elif fc == 4:
            return self._client.read_input_registers(start, quantity, self._slave)
        elif fc == 1:
            return self._client.read_coils(start, quantity, self._slave)
        elif fc == 2:
            return self._client.read_discrete_inputs(start, quantity, self._slave)
        raise ValueError(f"Unsupported FC: {fc}")

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "slave": self._slave,
            "interval": self._interval,
            "register_count": len(self._specs),
            "clients": self._bridge.client_count,
            "latest": self._latest,
        }

    async def sse_generator(self):
        q = self._bridge.add_client()
        yield _sse_json({"event": "status", **self.get_status()})
        if self._history:
            yield _sse_json({"event": "history", "points": self._history[-100:]})
        try:
            while self.running:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield _sse_format("", event="ping")
                    continue
                if data is None:
                    break
                yield _sse_json(data)
                if data.get("event") == "stopped":
                    break
        finally:
            self._bridge.remove_client(q)


# ---------------------------------------------------------------------------
# VOFA+ JustFloat SSE Generator
# ---------------------------------------------------------------------------

def normalize_vofa_interval(interval: float) -> float:
    """Return a finite supported VOFA interval without silently accepting invalid input."""
    try:
        value = float(interval)
    except (TypeError, ValueError):
        raise ValueError("VOFA interval must be a finite number") from None
    if not math.isfinite(value) or value < 0 or value > 60.0:
        raise ValueError("VOFA interval must be finite and in the range [0, 60]")
    return max(0.000001, value)


class VofaStreamManager:
    """Manages VOFA+ JustFloat variable streaming via memory reads.

    Reads device RAM at specified addresses, interprets as floats,
    and streams the data via SSE for the VofaTab chart.
    """

    def __init__(
        self,
        stream_hub=None,
        *,
        batch_samples: int = 32,
        clock=time.perf_counter,
    ):
        self._bridge = AsyncBridge()
        self._thread: threading.Thread | None = None
        self._running = False
        self._paused = threading.Event()
        self._paused.set()
        self._stop_event = threading.Event()
        self._channels: list[dict] = []  # [{name, addr, type, size}]
        self._interval: float = 0.1  # seconds
        self._history: list[dict] = []
        self._max_history = 500
        self._stream_hub = stream_hub
        self._batch_samples = max(1, int(batch_samples))
        self._clock = clock
        self._read_groups = []
        self._pending_samples: list[tuple[float, ...]] = []
        self._completed_samples = 0
        self._completed_reads = 0
        self._read_errors = 0
        self._rate_timestamps = deque()
        self._actual_rate = 0.0
        self._acquisition_mode = "idle"
        self._stream_integrity: dict[str, int] = {}
        self._dump_restart = threading.Event()

    @property
    def running(self) -> bool:
        return self._running and not self._stop_event.is_set()

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    def set_stream_hub(self, stream_hub) -> None:
        self._stream_hub = stream_hub

    def detach_stream_hub(self, stream_hub) -> None:
        if self._stream_hub is stream_hub:
            self._stream_hub = None

    def configure(self, channels: list[dict], interval: float | None = None) -> None:
        from mklink.vofa_viewer import build_vofa_read_groups, normalize_vofa_channels

        normalized_interval = (
            self._interval if interval is None else normalize_vofa_interval(interval)
        )
        normalized = normalize_vofa_channels(channels)
        read_groups = build_vofa_read_groups(normalized)
        self._channels = normalized
        self._interval = normalized_interval
        self._read_groups = read_groups
        self._pending_samples.clear()
        self._completed_samples = 0
        self._completed_reads = 0
        self._read_errors = 0
        self._rate_timestamps.clear()
        self._actual_rate = 0.0
        self._acquisition_mode = "idle"
        self._stream_integrity = {}
        self._dump_restart.clear()

    @staticmethod
    def _unpack_spec(type_name: str) -> tuple[str, int]:
        return {
            "float": ("<f", 4), "fp32": ("<f", 4),
            "int32_t": ("<i", 4), "int32": ("<i", 4),
            "uint32_t": ("<I", 4), "uint32": ("<I", 4),
            "int16_t": ("<h", 2), "int16": ("<h", 2),
            "uint16_t": ("<H", 2), "uint16": ("<H", 2),
            "int8_t": ("<b", 1), "int8": ("<b", 1),
            "uint8_t": ("<B", 1), "uint8": ("<B", 1),
            "bool": ("<?", 1), "boolean": ("<?", 1),
        }.get(type_name, ("<f", 4))

    def _accept_values(self, values: list) -> bool:
        if any(value is None for value in values):
            self._read_errors += 1
            return False
        sample = tuple(
            number if math.isfinite(number) else 0.0
            for number in (float(value) for value in values)
        )
        self._completed_samples += 1
        completed_at = self._clock()
        self._rate_timestamps.append(completed_at)
        cutoff = completed_at - 1.0
        while self._rate_timestamps and self._rate_timestamps[0] < cutoff:
            self._rate_timestamps.popleft()
        if len(self._rate_timestamps) >= 2:
            elapsed = self._rate_timestamps[-1] - self._rate_timestamps[0]
            self._actual_rate = (
                (len(self._rate_timestamps) - 1) / elapsed if elapsed > 0 else 0.0
            )
        else:
            self._actual_rate = 0.0
        point = {"_t": time.time()}
        point.update({
            channel["name"]: sample[index]
            for index, channel in enumerate(self._channels)
        })
        self._bridge.put({"event": "data", **point})
        self._history.append(point)
        if len(self._history) > self._max_history:
            del self._history[:-self._max_history]
        self._pending_samples.append(sample)
        if len(self._pending_samples) >= self._batch_samples:
            self._flush_binary_batch()
        return True

    def _accept_dump_frame(self, frame: dict) -> bool:
        from mklink.dump_memory import FLAG_REGION_ERROR

        if int(frame.get("flags", 0)) & FLAG_REGION_ERROR:
            self._read_errors += 1
            return False
        values = [None] * len(self._channels)
        regions = dict(frame.get("regions", []))
        try:
            for region_index, group in enumerate(self._read_groups):
                raw = regions.get(region_index)
                if raw is None or len(raw) != group.size:
                    raise ValueError("non-exact VOFA dump-memory region")
                for channel in group.channels:
                    fmt, width = self._unpack_spec(channel.type_name)
                    start = channel.offset
                    values[channel.channel_index] = struct.unpack(
                        fmt, raw[start:start + width],
                    )[0]
        except Exception:
            self._read_errors += 1
            return False
        return self._accept_values(values)

    def collect_cycle(self, device) -> bool:
        import struct as _struct

        values = [None] * len(self._channels)
        try:
            for group in self._read_groups:
                raw = device.read_memory(group.address, group.size)
                self._completed_reads += 1
                if len(raw) != group.size:
                    raise ValueError("non-exact VOFA memory read")
                for channel in group.channels:
                    fmt, width = self._unpack_spec(channel.type_name)
                    start = channel.offset
                    values[channel.channel_index] = _struct.unpack(
                        fmt, raw[start:start + width],
                    )[0]
        except Exception:
            self._read_errors += 1
            return False
        return self._accept_values(values)

    def publish_samples(self, samples) -> None:
        from mklink.vofa_viewer import (
            VOFA_SAMPLE_MAJOR_FLOAT32,
            encode_vofa_samples,
        )

        rows = [tuple(row) for row in samples]
        if not rows or self._stream_hub is None:
            return
        self._stream_hub.publish(
            encode_vofa_samples(rows), item_count=len(rows),
            flags=VOFA_SAMPLE_MAJOR_FLOAT32,
        )

    def _flush_binary_batch(self) -> None:
        if not self._pending_samples:
            return
        pending = self._pending_samples
        self._pending_samples = []
        self.publish_samples(pending)

    def start(self, device, channels: list[dict], interval: float = 0.1) -> None:
        """Start VOFA polling.

        Args:
            device: Device instance with read_memory()
            channels: List of {name, addr (int or hex str), type?, size?}
            interval: Polling interval in seconds
        """
        if self._thread is not None and self._thread.is_alive():
            if self.running:
                return
            raise RuntimeError("VOFA worker thread is still active")

        self.configure(channels, interval)
        stop_event = threading.Event()
        generation = object()
        self._stop_event = stop_event
        self._generation = generation
        self._paused.set()
        self._running = True
        self._history.clear()

        def _poll():
            try:
                bridge = getattr(device, "_bridge", None)
                if bridge is not None and 0 < len(self._read_groups) <= 16:
                    from mklink.dump_memory import DumpMemoryStreamSession

                    self._acquisition_mode = "dump-memory"
                    region_pairs = [
                        (group.address, group.size) for group in self._read_groups
                    ]
                    while not stop_event.is_set():
                        self._dump_restart.clear()
                        session = DumpMemoryStreamSession(
                            bridge, region_pairs, self._interval,
                        )
                        completed_integrity = dict(self._stream_integrity)
                        try:
                            session.start()
                            while (
                                not stop_event.is_set()
                                and not self._dump_restart.is_set()
                            ):
                                frames = session.read_frames(max_bytes=1024 * 1024)
                                self._stream_integrity = _sum_counter_snapshots(
                                    completed_integrity, session.stats,
                                )
                                if not frames:
                                    stop_event.wait(0.0005)
                                    continue
                                if not self._paused.is_set():
                                    continue
                                for frame in frames:
                                    self._accept_dump_frame(frame)
                        finally:
                            session.stop()
                            self._stream_integrity = _sum_counter_snapshots(
                                completed_integrity, session.stats,
                            )
                else:
                    self._acquisition_mode = "read-memory"
                    while not stop_event.is_set():
                        if not self._paused.is_set():
                            stop_event.wait(self._interval)
                            continue

                        self.collect_cycle(device)
                        stop_event.wait(self._interval)

            except Exception as e:
                logger.error("VOFA stream error: %s", e)
                self._bridge.put({"event": "error", "message": str(e)})
            finally:
                if getattr(self, "_generation", None) is generation:
                    self._running = False
                    self._bridge.put({"event": "stopped"})
                    self._bridge.stop()

        self._thread = threading.Thread(target=_poll, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        thread = self._thread
        self._stop_event.set()
        if thread:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError("VOFA worker thread is still active")
        self._running = False
        self._flush_binary_batch()
        if self._thread is thread:
            self._thread = None
        return True

    def pause(self) -> None:
        self._paused.clear()
        self._rate_timestamps.clear()
        self._actual_rate = 0.0

    def resume(self) -> None:
        self._rate_timestamps.clear()
        self._actual_rate = 0.0
        self._paused.set()

    def set_interval(self, interval: float) -> float:
        self._interval = normalize_vofa_interval(interval)
        self._dump_restart.set()
        return self._interval

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "paused": self.paused,
            "channels": self._channels,
            "interval": self._interval,
            "clients": self._bridge.client_count,
            "history_size": len(self._history),
            "completed_samples": self._completed_samples,
            "completed_reads": self._completed_reads,
            "read_errors": self._read_errors,
            "actual_rate": round(self._actual_rate, 6),
            "acquisition_mode": self._acquisition_mode,
            "stream_integrity": dict(self._stream_integrity),
            "layout": "sample-major-float32",
            "stream": self._stream_hub.stats().__dict__ if self._stream_hub else None,
        }

    async def sse_generator(self):
        q = self._bridge.add_client()
        yield _sse_json({"event": "status", **self.get_status()})
        if self._history:
            yield _sse_json({"event": "history", "points": self._history[-100:]})
        try:
            while self.running or self.paused:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield _sse_format("", event="ping")
                    continue
                if data is None:
                    break
                yield _sse_json(data)
                if data.get("event") == "stopped":
                    break
        finally:
            self._bridge.remove_client(q)


# ---------------------------------------------------------------------------
# Global stream managers (keyed by state in api.py)
# ---------------------------------------------------------------------------

_managers: dict[str, Any] = {}

BRIDGE_DASHBOARD_TYPES = ("rtt", "superwatch", "vofa", "systemview")


def get_managers() -> dict[str, Any]:
    """Get or create stream manager singletons."""
    if "rtt" not in _managers:
        _managers["rtt"] = RttStreamManager()
    if "superwatch" not in _managers:
        _managers["superwatch"] = SuperWatchStreamManager()
    if "serial" not in _managers:
        _managers["serial"] = SerialStreamManager()
    if "modbus" not in _managers:
        _managers["modbus"] = ModbusStreamManager()
    if "vofa" not in _managers:
        _managers["vofa"] = VofaStreamManager()
    if "systemview" not in _managers:
        _managers["systemview"] = SystemViewStreamManager()
    return _managers


def stop_bridge_dashboards(
    exclude: str | None = None,
    resource_manager=None,
) -> list[str]:
    """停止所有使用 MKLink Bridge 的 Dashboard（RTT/SuperWatch/VOFA）。
    返回被停止的 Dashboard 名称列表。"""
    stopped = []
    managers = get_managers()
    for name in BRIDGE_DASHBOARD_TYPES:
        if name == exclude:
            continue
        mgr = managers.get(name)
        if mgr and mgr.running:
            error = None
            try:
                mgr.stop()
            except Exception as exc:
                error = exc
            thread = getattr(mgr, "_thread", None)
            alive = thread.is_alive() if thread is not None else bool(mgr.running)
            if alive:
                raise TimeoutError(
                    f"{name} worker thread is still active"
                ) from error
            if resource_manager is not None:
                resource_manager.release(f"user:dashboard:{name}")
            if error is not None:
                raise error
            stopped.append(name)
    return stopped
