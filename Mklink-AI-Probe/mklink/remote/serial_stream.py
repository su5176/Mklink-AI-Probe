"""Bounded serial byte batching independent of the WebSocket event loop."""

from __future__ import annotations

import threading
from collections.abc import Callable

SERIAL_BATCH_BYTES = 4096
SERIAL_BATCH_INTERVAL = 0.02


class SerialByteBatcher:
    """Preserve byte/direction order while avoiding one WS frame per USB read."""

    def __init__(self, publish: Callable[[bytes, str], None]):
        self._publish = publish
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._direction = "RX"
        self._stop = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="serial-byte-batches", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def feed(self, data: bytes, direction: str) -> None:
        with self._lock:
            if self._closed:
                return
            if direction != self._direction:
                self._flush_locked()
                self._direction = direction
            offset = 0
            while offset < len(data):
                count = min(SERIAL_BATCH_BYTES - len(self._pending), len(data) - offset)
                self._pending.extend(data[offset:offset + count])
                offset += count
                if len(self._pending) == SERIAL_BATCH_BYTES:
                    self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._pending:
            payload = bytes(self._pending)
            self._pending.clear()
            self._publish(payload, self._direction)

    def _run(self) -> None:
        while not self._stop.wait(SERIAL_BATCH_INTERVAL):
            self.flush()

    def close(self) -> None:
        # Call after the serial reader stops so its last chunk is included.
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise TimeoutError("serial byte batch worker did not stop")
        with self._lock:
            self._closed = True
            self._flush_locked()
