"""Private MKST sidechannel for data already obtained by direct MCP tools.

The bridge never polls or opens a device.  MCP tool threads only attempt a
bounded ``put_nowait`` of results they already own; encoding, StreamHub fanout,
WebSocket I/O, and observation publishing stay on daemon sidecar threads.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import math
import queue
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from mklink.observe_bridge import (
    SafeProducer,
    process_producer,
    stream_descriptor,
    stream_stats_facts,
)
from mklink.remote.stream_hub import StreamHub
from mklink.remote.stream_protocol import (
    MAX_MEMORY_CHUNK_BYTES,
    MAX_MEMORY_REGIONS,
    MAX_MEMORY_SAMPLES,
    MEMORY_JSON_V1,
    RTT_RAW_UTF8_LINES,
    SUPERWATCH_METADATA_JSON,
    SUPERWATCH_SAMPLE_MAJOR_FLOAT32,
    WAVEFORM_SAMPLE_MAJOR_FLOAT32,
    Frame,
    RttLine,
    StreamType,
    encode_frame,
    encode_memory_record,
    encode_rtt_lines,
    encode_superwatch_metadata,
    encode_systemview_events,
    encode_waveform_samples,
)

_STREAM_TYPES: Mapping[str, StreamType] = {
    "memory": StreamType.MEMORY,
    "systemview": StreamType.SYSTEMVIEW,
    "vofa": StreamType.WAVEFORM,
    "rtt": StreamType.RTT_RAW,
    "superwatch": StreamType.SUPERWATCH,
}


@dataclass(frozen=True)
class _DataCommand:
    kind: str
    payload: Any = field(repr=False)
    label: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class _RetainedBatch:
    payload: bytes = field(repr=False)
    item_count: int
    flags: int
    stream_type: StreamType
    sequence: int
    timestamp_ns: int


@dataclass(frozen=True)
class _RetainedMemoryOperation:
    """One complete MEMORY operation retained as an indivisible unit."""

    chunks: tuple[_RetainedBatch, ...] = field(repr=False)
    byte_count: int
    sample_count: int


@dataclass
class _PendingMemoryOperation:
    """Bounded prefix of the one MEMORY operation currently being encoded."""

    operation_id: str = field(repr=False)
    operation: str
    sample_count: int
    next_sample_index: int = 0
    chunks: list[_RetainedBatch] = field(default_factory=list, repr=False)
    byte_count: int = 0


@dataclass(frozen=True)
class _MemoryRegion:
    address: int
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class _MemoryTransfer:
    operation_id: str = field(repr=False)
    operation: str
    sample_index: int
    sample_count: int
    regions: tuple[_MemoryRegion, ...]


class McpStreamSidecar:
    """Loopback-only, authenticated server backed by existing StreamHubs."""

    DATA_QUEUE_CAPACITY = 8
    MEMORY_DATA_QUEUE_CAPACITY = MAX_MEMORY_SAMPLES
    DEFAULT_CLIENT_QUEUE_CAPACITY = 64
    MAX_TEXT_CHARS = 500_000
    MAX_RTT_LINES = 65_536
    MAX_SYSTEMVIEW_EVENTS = 65_536
    HEARTBEAT_SECONDS = 1.0
    CHECKPOINT_SECONDS = 2.0
    MEMORY_RETAINED_BATCHES = 16
    MAX_MEMORY_TRANSFER_BYTES = 512 * 1024
    MEMORY_CLIENT_QUEUE_CAPACITY = (
        MAX_MEMORY_TRANSFER_BYTES + MAX_MEMORY_CHUNK_BYTES - 1
    ) // MAX_MEMORY_CHUNK_BYTES + MAX_MEMORY_SAMPLES * MAX_MEMORY_REGIONS - 1

    def __init__(self) -> None:
        self._hubs = {
            name: StreamHub(max_batches_per_client=(
                self.MEMORY_CLIENT_QUEUE_CAPACITY
                if name == "memory"
                else self.DEFAULT_CLIENT_QUEUE_CAPACITY
            ))
            for name in _STREAM_TYPES
        }
        self._data_queue: queue.Queue[_DataCommand] = queue.Queue(
            maxsize=self.DATA_QUEUE_CAPACITY,
        )
        self._memory_queue: queue.Queue[_DataCommand] = queue.Queue(
            maxsize=self.MEMORY_DATA_QUEUE_CAPACITY,
        )
        self._data_stop = threading.Event()
        self._stop_requested = threading.Event()
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._drop_lock = threading.Lock()
        self._input_dropped: dict[str, int] = {}
        self._last_hub_stats: dict[str, Any] = {}
        self._last_checkpoint: dict[str, float] = {}
        self._stream_lock = threading.RLock()
        self._retained: dict[str, _RetainedBatch] = {}
        self._memory_retained: deque[_RetainedMemoryOperation] = deque(
            maxlen=self.MEMORY_RETAINED_BATCHES,
        )
        self._memory_pending: _PendingMemoryOperation | None = None
        self._superwatch_metadata: _RetainedBatch | None = None
        self._registration_lock = threading.Lock()
        self._registered: set[str] = set()
        self._superwatch_version = 1
        self._producer = SafeProducer()
        self._auth_token = secrets.token_urlsafe(32)
        self._bound_port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._server_thread: threading.Thread | None = None
        self._data_thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return self._ready.is_set() and not self._failed.is_set()

    @property
    def auth_token(self) -> str:
        """Private token for local tests/descriptor registration."""
        return self._auth_token

    def endpoint(self, stream_name: str) -> str | None:
        if stream_name not in self._hubs or self._bound_port is None:
            return None
        return f"ws://127.0.0.1:{self._bound_port}/ws/streams/{stream_name}"

    def start(self, wait_timeout: float = 0.75) -> bool:
        with self._lifecycle_lock:
            if self._server_thread is not None:
                self._ready.wait(timeout=max(0.0, float(wait_timeout)))
                return self.available
            self._data_thread = threading.Thread(
                target=self._data_worker,
                name="mklink-mcp-stream-encoder",
                daemon=True,
            )
            self._server_thread = threading.Thread(
                target=self._server_worker,
                name="mklink-mcp-stream-server",
                daemon=True,
            )
            try:
                self._data_thread.start()
                self._server_thread.start()
            except Exception:  # noqa: BLE001 - sidecar startup is best-effort
                self._failed.set()
                self._ready.set()
                self._data_stop.set()
                self._stop_requested.set()
                return False
        self._ready.wait(timeout=max(0.0, float(wait_timeout)))
        return self.available

    def stop(self, timeout: float = 0.5) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        self._data_stop.set()
        data_thread = self._data_thread
        if (
            data_thread is not None
            and data_thread.ident is not None
            and data_thread is not threading.current_thread()
        ):
            data_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if data_thread is None or not data_thread.is_alive():
            with self._stream_lock:
                incomplete_memory = self._memory_pending is not None
                self._memory_pending = None
            if incomplete_memory:
                self.publish_memory_gap("missing_block_count", 1)
        with self._drop_lock:
            dropped_names = tuple(self._input_dropped)
        for name in dropped_names:
            self._report_input_drops(name)
        self._stop_requested.set()
        loop = self._loop
        async_stop = self._async_stop
        if loop is not None and async_stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(async_stop.set)
            except Exception:  # noqa: BLE001, S110 - loop may be closing
                pass
        server_thread = self._server_thread
        if (
            server_thread is not None
            and server_thread.ident is not None
            and server_thread is not threading.current_thread()
        ):
            server_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(
            thread is not None and thread.ident is not None and thread.is_alive()
            for thread in (self._data_thread, self._server_thread)
        )

    def enqueue(self, command: _DataCommand) -> bool:
        data_thread = self._data_thread
        if (
            data_thread is None
            or data_thread.ident is None
            or self._failed.is_set()
            or self._data_stop.is_set()
            or self._stop_requested.is_set()
        ):
            return False
        target_queue = (
            self._memory_queue if command.kind == "memory" else self._data_queue
        )
        try:
            target_queue.put_nowait(command)
            return True
        except queue.Full:
            with self._drop_lock:
                self._input_dropped[command.kind] = (
                    self._input_dropped.get(command.kind, 0) + 1
                )
            return False

    def _server_worker(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception:  # noqa: BLE001 - isolate optional sidecar failure
            self._failed.set()
            self._ready.set()
            self._data_stop.set()
            self._stop_requested.set()

    async def _serve(self) -> None:
        from websockets.server import serve

        self._loop = asyncio.get_running_loop()
        self._async_stop = asyncio.Event()
        if self._stop_requested.is_set():
            self._async_stop.set()
        try:
            async with serve(
                self._handle_connection,
                "127.0.0.1",
                0,
                close_timeout=0.2,
                max_queue=4,
                max_size=64 * 1024,
                ping_interval=20,
                ping_timeout=20,
            ) as server:
                sockets = server.sockets or []
                if not sockets:
                    raise RuntimeError("MCP stream listener has no socket")
                self._bound_port = int(sockets[0].getsockname()[1])
                self._ready.set()
                with self._stream_lock:
                    retained_names = tuple(self._retained)
                    if self._superwatch_metadata is not None:
                        retained_names = (*retained_names, "superwatch")
                    if self._memory_retained:
                        retained_names = (*retained_names, "memory")
                for name in dict.fromkeys(retained_names):
                    self._ensure_registered(name)
                await self._async_stop.wait()
        finally:
            with self._registration_lock:
                registered = tuple(self._registered)
            for name in registered:
                descriptor = stream_descriptor(name, state="closed")
                if descriptor is not None:
                    self._producer.publish(
                        "stream.closed",
                        {"stream": descriptor},
                    )
            self._ready.clear()
            self._bound_port = None
            self._async_stop = None
            self._loop = None

    async def _handle_connection(self, websocket: Any, *path_args: Any) -> None:
        from websockets.exceptions import ConnectionClosed

        headers = getattr(websocket, "request_headers", {})
        authorization = headers.get("Authorization") if headers is not None else None
        try:
            authorized = isinstance(authorization, str) and secrets.compare_digest(
                authorization,
                f"Bearer {self._auth_token}",
            )
        except (TypeError, ValueError):
            authorized = False
        if not authorized:
            await websocket.close(code=1008, reason="Unauthorized")
            return
        path = path_args[0] if path_args else getattr(websocket, "path", "")
        prefix = "/ws/streams/"
        stream_name = path[len(prefix):] if isinstance(path, str) and path.startswith(prefix) else ""
        hub = self._hubs.get(stream_name)
        stream_type = _STREAM_TYPES.get(stream_name)
        if hub is None or stream_type is None:
            await websocket.close(code=1008, reason="Unknown stream")
            return
        subscriber = None
        try:
            await websocket.send(self._encoded_status(stream_type, hub))
            with self._stream_lock:
                subscriber = hub.subscribe()
                retained = self._initial_batches(stream_name)
            for batch in retained:
                await websocket.send(self._encoded_data_batch(stream_type, batch))
            while True:
                try:
                    batch = await asyncio.wait_for(
                        subscriber.get(),
                        timeout=self.HEARTBEAT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await websocket.send(self._encoded_status(stream_type, hub))
                    continue
                try:
                    await websocket.send(self._encoded_data_batch(stream_type, batch))
                finally:
                    subscriber.task_done()
        except ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001, S110 - disconnect/codec isolation
            pass
        finally:
            if subscriber is not None:
                try:
                    hub.unsubscribe(subscriber)
                except Exception:  # noqa: BLE001, S110 - idempotent teardown
                    pass

    @staticmethod
    def _encoded_status(stream_type: StreamType, hub: StreamHub) -> bytes:
        stats = hub.status_frame()
        payload = json.dumps(
            asdict(stats),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return encode_frame(Frame(
            stream_type=StreamType.CONTROL,
            flags=0,
            stream_id=int(stream_type),
            sequence=stats.last_sequence,
            timestamp_ns=time.time_ns(),
            item_count=0,
            payload=payload,
        ))

    @staticmethod
    def _encoded_data_batch(stream_type: StreamType, batch: Any) -> bytes:
        batch_type = batch.stream_type or stream_type
        return encode_frame(Frame(
            stream_type=batch_type,
            flags=batch.flags,
            stream_id=int(stream_type),
            sequence=batch.sequence,
            timestamp_ns=batch.timestamp_ns,
            item_count=batch.item_count,
            payload=batch.payload,
        ))

    def _initial_batches(self, name: str) -> tuple[_RetainedBatch, ...]:
        if name == "memory":
            completed = tuple(
                chunk
                for batch in self._memory_retained
                for chunk in batch.chunks
            )
            pending = self._memory_pending
            if pending is None:
                return completed
            return (*completed, *pending.chunks)
        if name == "superwatch":
            batches = [self._superwatch_metadata, self._retained.get(name)]
            return tuple(batch for batch in batches if batch is not None)
        retained = self._retained.get(name)
        return () if retained is None else (retained,)

    def _data_worker(self) -> None:
        while (
            not self._data_stop.is_set()
            or not self._data_queue.empty()
            or not self._memory_queue.empty()
        ):
            queued = self._next_data_command()
            if queued is None:
                self._data_stop.wait(0.05)
                continue
            command_queue, command = queued
            try:
                if command.kind == "rtt":
                    self._encode_rtt(command.payload, truncated=command.truncated)
                elif command.kind == "systemview":
                    self._encode_systemview(
                        command.payload,
                        truncated=command.truncated,
                    )
                elif command.kind == "superwatch":
                    self._encode_superwatch(command.label, command.payload)
                elif command.kind == "memory":
                    self._encode_memory(command.payload)
            except Exception:  # noqa: BLE001, S110 - isolate one data command
                pass
            finally:
                command_queue.task_done()

    def _next_data_command(
        self,
    ) -> tuple[queue.Queue[_DataCommand], _DataCommand] | None:
        for command_queue in (self._memory_queue, self._data_queue):
            try:
                return command_queue, command_queue.get_nowait()
            except queue.Empty:
                continue
        return None

    def _encode_rtt(self, value: Any, *, truncated: bool = False) -> None:
        if not isinstance(value, str) or not value:
            return
        text_truncated = truncated or len(value) > self.MAX_TEXT_CHARS
        lines = value[:self.MAX_TEXT_CHARS].splitlines()
        lines_truncated = len(lines) > self.MAX_RTT_LINES
        lines = lines[:self.MAX_RTT_LINES]
        if lines:
            now = time.time_ns()
            records = [RttLine(now + index, "raw", line) for index, line in enumerate(lines)]
            self._publish_hub(
                "rtt",
                encode_rtt_lines(records),
                item_count=len(records),
                flags=RTT_RAW_UTF8_LINES,
                stream_type=StreamType.RTT_RAW,
            )
            if text_truncated or lines_truncated:
                self._publish_truncation("rtt")
            self._encode_vofa(lines)

    def _encode_vofa(self, lines: list[str]) -> None:
        from mklink.rtt_viewer import RttLineParser

        candidates = [line.strip() for line in lines if line.strip()]
        truncated = len(candidates) > 4096
        samples = candidates[:4096]
        if not samples:
            return
        parser = RttLineParser.auto_detect(samples[:32])
        channels: tuple[str, ...] | None = None
        rows: list[tuple[float, ...]] = []
        for line in samples:
            parsed = parser.parse(line)
            if not isinstance(parsed, Mapping):
                continue
            names = tuple(sorted(key for key in parsed if not key.startswith("_")))
            if not names:
                continue
            try:
                row = tuple(float(parsed[name]) for name in names)
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(number) for number in row):
                continue
            if channels is None:
                channels = names
            if names == channels:
                rows.append(row)
        if rows:
            self._publish_hub(
                "vofa",
                encode_waveform_samples(rows),
                item_count=len(rows),
                flags=WAVEFORM_SAMPLE_MAJOR_FLOAT32,
                stream_type=StreamType.WAVEFORM,
            )
            if truncated:
                self._publish_truncation("vofa")

    def _encode_systemview(self, value: Any, *, truncated: bool = False) -> None:
        if not isinstance(value, list):
            return
        payloads: list[bytes] = []
        for event in value[:self.MAX_SYSTEMVIEW_EVENTS]:
            if not isinstance(event, Mapping):
                continue
            try:
                payloads.append(encode_systemview_events([dict(event)]))
            except Exception:  # noqa: BLE001, S112 - skip one invalid event
                continue
        if payloads:
            self._publish_hub(
                "systemview",
                b"".join(payloads),
                item_count=len(payloads),
                flags=0,
                stream_type=StreamType.SYSTEMVIEW,
            )
            if truncated or len(payloads) < len(value):
                self._publish_truncation("systemview")

    def _encode_superwatch(self, label: str | None, value: Any) -> None:
        if (
            not isinstance(label, str)
            or not label
            or len(label) > 256
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            return
        self._superwatch_version += 1
        metadata = encode_superwatch_metadata(
            self._superwatch_version,
            [{"name": label}],
        )
        self._publish_hub(
            "superwatch",
            metadata,
            item_count=0,
            flags=SUPERWATCH_METADATA_JSON,
            stream_type=StreamType.SUPERWATCH,
        )
        self._publish_hub(
            "superwatch",
            encode_waveform_samples([(float(value),)]),
            item_count=1,
            flags=SUPERWATCH_SAMPLE_MAJOR_FLOAT32,
            stream_type=StreamType.SUPERWATCH,
        )

    def _encode_memory(self, value: Any) -> None:
        if not isinstance(value, _MemoryTransfer):
            return
        region_count = len(value.regions)
        transfer_bytes = sum(len(region.data) for region in value.regions)
        encoded_chunks: list[bytes] = []
        for region_index, region in enumerate(value.regions):
            total_bytes = len(region.data)
            for offset in range(0, total_bytes, MAX_MEMORY_CHUNK_BYTES):
                chunk = region.data[offset:offset + MAX_MEMORY_CHUNK_BYTES]
                encoded_chunks.append(encode_memory_record(
                    value.operation_id,
                    value.operation,
                    region.address + offset,
                    offset,
                    total_bytes,
                    chunk,
                    sample_index=value.sample_index,
                    sample_count=value.sample_count,
                    region_index=region_index,
                    region_count=region_count,
                ))
        if not encoded_chunks:
            return

        hub = self._hubs["memory"]
        retained_chunks: list[_RetainedBatch] = []
        invalid_operation = False
        published = False
        with self._stream_lock:
            pending = self._memory_pending
            if pending is not None and pending.operation_id != value.operation_id:
                self._memory_pending = None
                pending = None
                invalid_operation = True
            if pending is None:
                if value.sample_index != 0:
                    invalid_operation = True
                else:
                    pending = _PendingMemoryOperation(
                        operation_id=value.operation_id,
                        operation=value.operation,
                        sample_count=value.sample_count,
                    )
                    self._memory_pending = pending
            elif (
                pending.operation != value.operation
                or pending.sample_count != value.sample_count
                or pending.next_sample_index != value.sample_index
            ):
                self._memory_pending = None
                pending = None
                invalid_operation = True

            if pending is not None and (
                pending.byte_count + transfer_bytes > self.MAX_MEMORY_TRANSFER_BYTES
                or len(pending.chunks) + len(encoded_chunks)
                > self.MEMORY_CLIENT_QUEUE_CAPACITY
            ):
                self._memory_pending = None
                pending = None
                invalid_operation = True

            if pending is not None:
                for payload in encoded_chunks:
                    timestamp_ns = time.time_ns()
                    sequence = hub.publish(
                        payload,
                        item_count=1,
                        flags=MEMORY_JSON_V1,
                        stream_type=StreamType.MEMORY,
                    )
                    retained_chunks.append(_RetainedBatch(
                        payload=bytes(payload),
                        item_count=1,
                        flags=MEMORY_JSON_V1,
                        stream_type=StreamType.MEMORY,
                        sequence=sequence,
                        timestamp_ns=timestamp_ns,
                    ))
                pending.chunks.extend(retained_chunks)
                pending.byte_count += transfer_bytes
                pending.next_sample_index += 1
                published = True
                if pending.next_sample_index == pending.sample_count:
                    self._memory_retained.append(_RetainedMemoryOperation(
                        chunks=tuple(pending.chunks),
                        byte_count=pending.byte_count,
                        sample_count=pending.sample_count,
                    ))
                    self._memory_pending = None
        if published:
            self._after_hub_publish("memory", hub)
        if invalid_operation:
            self.publish_memory_gap("invalid_block_count", 1)

    def _ensure_registered(self, name: str) -> None:
        with self._registration_lock:
            if name in self._registered:
                return
            endpoint = self.endpoint(name)
            descriptor = stream_descriptor(name)
            if endpoint is None or descriptor is None:
                return
            if not self._producer.available:
                self._producer = process_producer()
            accepted = self._producer.register_stream(
                endpoint,
                descriptor,
                headers={"Authorization": f"Bearer {self._auth_token}"},
            )
            if accepted:
                self._registered.add(name)
                self._producer.publish("stream.checkpoint", {
                    "stream": descriptor,
                    "facts": [{
                        "name": "retained_replay",
                        "value": True,
                    }],
                })

    def _publish_truncation(self, name: str) -> None:
        descriptor = stream_descriptor(name)
        if descriptor is not None:
            self._producer.publish("stream.gap", {
                "stream": descriptor,
                "facts": [{"name": "sidecar_truncated", "value": True}],
            })

    def _report_input_drops(self, name: str) -> None:
        with self._drop_lock:
            input_dropped = self._input_dropped.pop(name, 0)
        if not input_dropped:
            return
        self._ensure_registered(name)
        descriptor = stream_descriptor(
            name,
            state="degraded" if name == "memory" else None,
        )
        if descriptor is not None:
            self._producer.publish("stream.gap", {
                "stream": descriptor,
                "facts": [{
                    "name": "sidecar_dropped_batches",
                    "value": input_dropped,
                    "unit": "batches",
                }],
            })

    def publish_memory_gap(self, fact_name: str, count: int = 1) -> bool:
        if fact_name not in {
            "crc_error_count",
            "invalid_block_count",
            "missing_block_count",
            "region_gap_count",
            "firmware_error_count",
            "publish_drop_count",
        }:
            return False
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return False
        self._ensure_registered("memory")
        descriptor = stream_descriptor("memory", state="degraded")
        if descriptor is None:
            return False
        return self._producer.publish("stream.gap", {
            "stream": descriptor,
            "facts": [{"name": fact_name, "value": count, "unit": "count"}],
        })

    def _publish_hub(
        self,
        name: str,
        payload: bytes,
        *,
        item_count: int,
        flags: int,
        stream_type: StreamType,
    ) -> None:
        hub = self._hubs[name]
        timestamp_ns = time.time_ns()
        with self._stream_lock:
            sequence = hub.publish(
                payload,
                item_count=item_count,
                flags=flags,
                stream_type=stream_type,
            )
            retained = _RetainedBatch(
                payload=bytes(payload),
                item_count=item_count,
                flags=flags,
                stream_type=stream_type,
                sequence=sequence,
                timestamp_ns=timestamp_ns,
            )
            if name == "superwatch" and flags == SUPERWATCH_METADATA_JSON:
                self._superwatch_metadata = retained
            elif name == "memory":
                self._memory_retained.append(_RetainedMemoryOperation(
                    chunks=(retained,),
                    byte_count=len(payload),
                    sample_count=1,
                ))
            else:
                self._retained[name] = retained
        self._after_hub_publish(name, hub)

    def _after_hub_publish(self, name: str, hub: StreamHub) -> None:
        self._ensure_registered(name)
        descriptor = stream_descriptor(name)
        if descriptor is None:
            return
        self._report_input_drops(name)
        stats = hub.stats()
        previous = self._last_hub_stats.get(name)
        if previous is not None and stats.dropped_batches > previous.dropped_batches:
            self._producer.publish("stream.gap", {
                "stream": descriptor,
                "facts": [{
                    "name": "dropped_batches",
                    "value": stats.dropped_batches - previous.dropped_batches,
                    "unit": "batches",
                }],
            })
        now = time.monotonic()
        if now - self._last_checkpoint.get(name, 0.0) >= self.CHECKPOINT_SECONDS:
            self._producer.publish("stream.checkpoint", {
                "stream": descriptor,
                "facts": stream_stats_facts(stats),
            })
            self._last_checkpoint[name] = now
        self._last_hub_stats[name] = stats


_SIDECAR_LOCK = threading.Lock()
_SIDECAR: McpStreamSidecar | None = None


def start_mcp_stream_sidecar(wait_timeout: float = 0.75) -> McpStreamSidecar:
    global _SIDECAR
    with _SIDECAR_LOCK:
        if _SIDECAR is None:
            _SIDECAR = McpStreamSidecar()
        sidecar = _SIDECAR
    sidecar.start(wait_timeout)
    return sidecar


def stop_mcp_stream_sidecar(timeout: float = 0.5) -> bool:
    global _SIDECAR
    with _SIDECAR_LOCK:
        sidecar = _SIDECAR
        _SIDECAR = None
    if sidecar is None:
        return True
    return sidecar.stop(timeout)


def _current_sidecar() -> McpStreamSidecar | None:
    with _SIDECAR_LOCK:
        return _SIDECAR


def publish_mcp_rtt(output: Any) -> bool:
    sidecar = _current_sidecar()
    if sidecar is None or not isinstance(output, str) or not output:
        return False
    truncated = len(output) > sidecar.MAX_TEXT_CHARS
    return sidecar.enqueue(_DataCommand(
        "rtt",
        output[:sidecar.MAX_TEXT_CHARS],
        truncated=truncated,
    ))


def publish_mcp_systemview(result: Any) -> bool:
    sidecar = _current_sidecar()
    if sidecar is None or not isinstance(result, Mapping):
        return False
    events = result.get("events")
    if not isinstance(events, list):
        return False
    truncated = len(events) > sidecar.MAX_SYSTEMVIEW_EVENTS
    return sidecar.enqueue(_DataCommand(
        "systemview",
        events[:sidecar.MAX_SYSTEMVIEW_EVENTS],
        truncated=truncated,
    ))


def publish_mcp_superwatch(label: Any, value: Any) -> bool:
    sidecar = _current_sidecar()
    if sidecar is None or not isinstance(label, str):
        return False
    return sidecar.enqueue(_DataCommand("superwatch", value, label=label))


def publish_mcp_memory(
    operation: str,
    address: int,
    data: bytes,
) -> bool:
    """Queue one single-region memory record without touching the device."""
    return publish_mcp_memory_regions(
        operation,
        [(address, data)],
        sample_index=0,
        sample_count=1,
    )


def publish_mcp_memory_regions(
    operation: str,
    regions: Sequence[tuple[int, bytes]],
    *,
    sample_index: int,
    sample_count: int,
    operation_id: str | None = None,
) -> bool:
    """Queue one bounded read/dump sample for the private MEMORY stream."""
    sidecar = _current_sidecar()
    if sidecar is None or operation not in {"read", "dump"}:
        return False
    if (
        isinstance(sample_index, bool)
        or not isinstance(sample_index, int)
        or not 0 <= sample_index <= 0xFFFFFFFFFFFFFFFF
        or isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or not 1 <= sample_count <= MAX_MEMORY_SAMPLES
        or sample_index >= sample_count
    ):
        return False
    if (
        isinstance(regions, (str, bytes, bytearray, memoryview))
        or not isinstance(regions, Sequence)
        or not 1 <= len(regions) <= MAX_MEMORY_REGIONS
    ):
        return False
    copied: list[_MemoryRegion] = []
    total = 0
    for region in regions:
        if not isinstance(region, (tuple, list)) or len(region) != 2:
            return False
        address, data = region
        if (
            isinstance(address, bool)
            or not isinstance(address, int)
            or not 0 <= address <= 0xFFFFFFFFFFFFFFFF
            or not isinstance(data, (bytes, bytearray, memoryview))
        ):
            return False
        payload = bytes(data)
        if not payload or address + len(payload) > 0x10000000000000000:
            return False
        total += len(payload)
        if total > sidecar.MAX_MEMORY_TRANSFER_BYTES:
            return False
        copied.append(_MemoryRegion(address=address, data=payload))
    identifier = (
        f"op-{secrets.token_hex(8)}"
        if operation_id is None
        else operation_id
    )
    if not isinstance(identifier, str) or re.fullmatch(
        r"op-[0-9a-f]{16}", identifier,
    ) is None:
        return False
    transfer = _MemoryTransfer(
        operation_id=identifier,
        operation=operation,
        sample_index=sample_index,
        sample_count=sample_count,
        regions=tuple(copied),
    )
    accepted = sidecar.enqueue(_DataCommand("memory", transfer))
    if not accepted:
        # Flush a tail drop immediately; waiting for the next successful frame
        # would make a final queue overflow invisible to the Dashboard.
        sidecar._report_input_drops("memory")
    return accepted


def publish_mcp_memory_gap(fact_name: str, count: int = 1) -> bool:
    """Publish one bounded degraded-stream counter without private data."""
    sidecar = _current_sidecar()
    if sidecar is None:
        return False
    return sidecar.publish_memory_gap(fact_name, count)


atexit.register(stop_mcp_stream_sidecar, timeout=0.1)
