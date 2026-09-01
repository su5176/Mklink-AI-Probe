"""Bounded fan-out queues for high-throughput stream batches."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set, Tuple, Union


BytesLike = Union[bytes, bytearray, memoryview]
SubscribeCallback = Callable[[Callable[..., int]], None]


@dataclass(frozen=True, eq=False)
class StreamBatch:
    """An immutable payload carrying the hub-assigned batch metadata."""

    payload: bytes = field(repr=False)
    sequence: int
    item_count: int
    timestamp_ns: int = field(default_factory=time.time_ns)
    flags: int = 0
    stream_type: object = None

    def __bytes__(self) -> bytes:
        return self.payload

    def __len__(self) -> int:
        return len(self.payload)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, StreamBatch):
            return self.payload == other.payload
        if isinstance(other, (bytes, bytearray, memoryview)):
            return self.payload == bytes(other)
        return NotImplemented


class _SubscriberQueue(asyncio.Queue):
    def __init__(self, maxsize: int):
        super().__init__(maxsize=maxsize)
        self._protected_initial = None

    def protect_initial(self, batch: StreamBatch) -> None:
        self._protected_initial = batch

    def preserves_oldest(self) -> bool:
        return (
            self._protected_initial is not None
            and bool(self._queue)
            and self._queue[0] is self._protected_initial
        )

    def get_nowait(self):
        item = super().get_nowait()
        if item is self._protected_initial:
            self._protected_initial = None
        return item


@dataclass(frozen=True)
class StreamHubStats:
    produced_batches: int
    produced_items: int
    produced_bytes: int
    delivered_batches: int
    delivered_items: int
    delivered_bytes: int
    dropped_batches: int
    dropped_items: int
    dropped_bytes: int
    active_clients: int
    queue_high_water_mark: int
    last_sequence: int


class StreamHub:
    """Broadcast batches without allowing subscribers to block producers."""

    def __init__(self, max_batches_per_client: int):
        if isinstance(max_batches_per_client, bool) or not isinstance(
            max_batches_per_client, int
        ):
            raise TypeError("max_batches_per_client must be an integer")
        if max_batches_per_client <= 0:
            raise ValueError("max_batches_per_client must be greater than zero")
        self._max_batches_per_client = max_batches_per_client
        self._subscribers: Set[asyncio.Queue] = set()
        self._owner_loop: Optional[asyncio.AbstractEventLoop] = None
        self._generation = 0
        self._pending_by_generation: Dict[int, int] = {}
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._last_sequence = 0
        self._produced_batches = 0
        self._produced_items = 0
        self._produced_bytes = 0
        self._delivered_batches = 0
        self._delivered_items = 0
        self._delivered_bytes = 0
        self._dropped_batches = 0
        self._dropped_items = 0
        self._dropped_bytes = 0
        self._queue_high_water_mark = 0
        self._subscribe_callback: Optional[SubscribeCallback] = None

    def set_subscribe_callback(
        self, callback: Optional[SubscribeCallback],
    ) -> None:
        if callback is not None and not callable(callback):
            raise TypeError("subscribe callback must be callable")
        with self._publish_lock:
            with self._lock:
                self._subscribe_callback = callback

    def subscribe(self) -> asyncio.Queue:
        loop = self._running_loop()
        queue: _SubscriberQueue = _SubscriberQueue(
            maxsize=self._max_batches_per_client
        )
        with self._lock:
            callback = self._subscribe_callback
        activated = False

        def enqueue_initial(
            batch: BytesLike, item_count: int, flags: int = 0,
            stream_type=None,
        ) -> int:
            nonlocal activated
            with self._publish_lock:
                with self._lock:
                    if activated:
                        raise RuntimeError("subscriber is already active")
                    activated = True
                    self._bind_or_require_owner_loop(loop)
                    self._subscribers.add(queue)
                published = self._prepare_batch(
                    batch, item_count, flags, stream_type,
                )
                queue.protect_initial(published)
                self._schedule_delivery(published)
            return published.sequence

        if callback is None:
            with self._publish_lock:
                with self._lock:
                    activated = True
                    self._bind_or_require_owner_loop(loop)
                    self._subscribers.add(queue)
        else:
            try:
                callback(enqueue_initial)
                if not activated:
                    raise RuntimeError(
                        "subscribe callback did not activate the subscriber"
                    )
            except Exception:
                if activated:
                    self.unsubscribe(queue)
                raise
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> bool:
        loop = self._running_loop()
        with self._publish_lock:
            with self._lock:
                if queue not in self._subscribers:
                    return False
                self._require_owner_loop(loop)
                self._subscribers.remove(queue)
            while not queue.empty():
                queue.get_nowait()
                queue.task_done()
            with self._lock:
                self._release_owner_if_idle()
        return True

    def publish(
        self, batch: BytesLike, item_count: int, flags: int = 0,
        stream_type=None,
    ) -> int:
        with self._publish_lock:
            published = self._prepare_batch(batch, item_count, flags, stream_type)
            self._schedule_delivery(published)
        return published.sequence

    def publish_threadsafe(
        self,
        loop: asyncio.AbstractEventLoop,
        batch: BytesLike,
        item_count: int = 0,
        flags: int = 0,
        stream_type=None,
    ) -> int:
        with self._publish_lock:
            with self._lock:
                if self._subscribers:
                    self._require_owner_loop(loop)
                    if loop.is_closed():
                        raise RuntimeError("owner event loop is closed")
            published = self._prepare_batch(batch, item_count, flags, stream_type)
            self._schedule_delivery(published)
        return published.sequence

    def stats(self) -> StreamHubStats:
        with self._lock:
            return StreamHubStats(
                produced_batches=self._produced_batches,
                produced_items=self._produced_items,
                produced_bytes=self._produced_bytes,
                delivered_batches=self._delivered_batches,
                delivered_items=self._delivered_items,
                delivered_bytes=self._delivered_bytes,
                dropped_batches=self._dropped_batches,
                dropped_items=self._dropped_items,
                dropped_bytes=self._dropped_bytes,
                active_clients=len(self._subscribers),
                queue_high_water_mark=self._queue_high_water_mark,
                last_sequence=self._last_sequence,
            )

    def status_frame(self) -> StreamHubStats:
        return self.stats()

    def _prepare_batch(
        self, batch: BytesLike, item_count: int, flags: int = 0,
        stream_type=None,
    ) -> StreamBatch:
        if not isinstance(batch, (bytes, bytearray, memoryview)):
            raise TypeError("batch must be bytes-like")
        if isinstance(item_count, bool) or not isinstance(item_count, int):
            raise TypeError("item_count must be an integer")
        if item_count < 0:
            raise ValueError("item_count must not be negative")
        if isinstance(flags, bool) or not isinstance(flags, int):
            raise TypeError("flags must be an integer")
        if not 0 <= flags <= 0xFF:
            raise ValueError("flags must fit in one byte")
        if stream_type is not None:
            from mklink.remote.stream_protocol import StreamType
            try:
                stream_type = StreamType(stream_type)
            except ValueError as exc:
                raise ValueError("unknown stream type override") from exc
        payload = bytes(batch)
        with self._lock:
            self._last_sequence += 1
            self._produced_batches += 1
            self._produced_items += item_count
            self._produced_bytes += len(payload)
            sequence = self._last_sequence
        return StreamBatch(
            payload, sequence, item_count, flags=flags, stream_type=stream_type,
        )

    def _schedule_delivery(self, batch: StreamBatch) -> None:
        with self._lock:
            loop = self._owner_loop
            subscribers = tuple(self._subscribers)
            generation = self._generation
            if loop is None or not subscribers:
                return
            self._pending_by_generation[generation] = (
                self._pending_by_generation.get(generation, 0) + 1
            )
        if loop.is_closed():
            with self._lock:
                self._finish_delivery(generation)
            raise RuntimeError("owner event loop is closed")
        try:
            loop.call_soon_threadsafe(
                self._deliver, generation, batch, subscribers
            )
        except Exception:
            with self._lock:
                self._finish_delivery(generation)
            raise

    def _deliver(
        self,
        generation: int,
        batch: StreamBatch,
        subscribers: Tuple[asyncio.Queue, ...],
    ) -> None:
        loop = self._running_loop()
        try:
            with self._lock:
                if generation != self._generation or loop is not self._owner_loop:
                    return
                active_subscribers = self._subscribers.copy()
            for queue in subscribers:
                if queue not in active_subscribers:
                    continue
                dropped = None
                enqueue_batch = True
                if queue.full():
                    if queue.preserves_oldest():
                        dropped = batch
                        enqueue_batch = False
                    else:
                        dropped = queue.get_nowait()
                        queue.task_done()
                if enqueue_batch:
                    queue.put_nowait(batch)
                with self._lock:
                    if enqueue_batch:
                        self._delivered_batches += 1
                        self._delivered_items += batch.item_count
                        self._delivered_bytes += len(batch)
                        self._queue_high_water_mark = max(
                            self._queue_high_water_mark, queue.qsize()
                        )
                    if dropped is not None:
                        self._dropped_batches += 1
                        self._dropped_items += dropped.item_count
                        self._dropped_bytes += len(dropped)
        finally:
            with self._lock:
                self._finish_delivery(generation)

    def _finish_delivery(self, generation: int) -> None:
        pending = self._pending_by_generation.get(generation)
        if pending is None:
            return
        if pending <= 1:
            self._pending_by_generation.pop(generation, None)
        else:
            self._pending_by_generation[generation] = pending - 1
        self._release_owner_if_idle()

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop:
        try:
            return asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("operation requires the owner event loop") from exc

    def _bind_or_require_owner_loop(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        if self._owner_loop is None:
            self._bind_owner_loop(loop)
            return
        if loop is self._owner_loop:
            return
        if self._subscribers:
            self._require_owner_loop(loop)
        pending = self._pending_by_generation.get(self._generation, 0)
        if pending and not self._owner_loop.is_closed():
            raise RuntimeError("owner event loop has pending deliveries")
        self._pending_by_generation.pop(self._generation, None)
        self._bind_owner_loop(loop)

    def _bind_owner_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if loop.is_closed():
            raise RuntimeError("owner event loop is closed")
        self._generation += 1
        self._owner_loop = loop
        self._pending_by_generation[self._generation] = 0

    def _release_owner_if_idle(self) -> None:
        if self._subscribers:
            return
        if self._pending_by_generation.get(self._generation, 0):
            return
        self._pending_by_generation.pop(self._generation, None)
        self._owner_loop = None

    def _require_owner_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if loop is not self._owner_loop:
            raise RuntimeError("operation requires the owner event loop")
