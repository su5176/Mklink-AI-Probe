"""Optional, failure-isolated publishing to the local HIL observation bus.

This module deliberately has no import-time dependency on ``hil_core``.  It
projects MKLink results into a small public schema before publishing and keeps
the private WebSocket endpoint/authentication descriptor out of public events.
Observation failures are converted to no-ops so they can never change device
ownership, locking, routing, or the result of an MKLink operation.
"""

from __future__ import annotations

import asyncio
import atexit
import importlib
import inspect
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any
from urllib.parse import urlsplit

SOURCE = {"kind": "mklink", "label": "MKLink"}
PUBLIC_SOURCE = {
    "device": {
        "id": "mklink",
        "kind": "mklink",
        "label": "MKLink",
        "state": "available",
    },
}
OBSERVE_PROTOCOL = "hil-observe-local-v1"
STREAM_PROTOCOL = "mkst-v1"

_EVENT_KINDS = {
    "operation.started",
    "operation.progress",
    "operation.completed",
    "operation.failed",
    "stream.checkpoint",
    "stream.gap",
    "stream.closed",
    "evidence.available",
}
_OPERATIONS = {
    "program.flash",
    "memory.read",
    "memory.write",
    "memory.flush",
    "memory.dump",
    "console.rtt.start",
    "console.rtt.read",
    "console.rtt.write",
    "console.rtt.stop",
    "console.rtt.capture",
    "console.rtt.systemview.start",
    "console.rtt.systemview.read",
    "console.rtt.systemview.stop",
    "console.rtt.systemview.capture",
    "console.rtt.systemview.analyze",
    "console.rtt.systemview.report",
}
_CAPABILITIES = {"program", "target.memory", "console.rtt"}
_ACTION_CLASSES = {"observe", "configure", "emit", "irreversible"}
_STATES = {
    "queued",
    "pending",
    "starting",
    "running",
    "waiting",
    "active",
    "available",
    "degraded",
    "succeeded",
    "completed",
    "failed",
    "cancelled",
    "closing",
    "closed",
    "stale",
    "unavailable",
}
_STREAM_PROTOCOLS = {"mjpeg", "websocket", "sse", "http", "binary", "jsonl"}
_STREAM_STATES = {
    "starting", "active", "available", "degraded", "closed", "unavailable",
}
_PAYLOAD_KEYS = {
    "operation",
    "state",
    "progress",
    "summary",
    "capability",
    "action_class",
    "ok",
    "error_code",
    "facts",
    "artifact",
    "stream",
}
_SENSITIVE_NAME_PARTS = {
    "address",
    "auth",
    "command",
    "credential",
    "crc32",
    "dump",
    "endpoint",
    "id",
    "idcode",
    "password",
    "path",
    "port",
    "raw",
    "secret",
    "serial",
    "token",
    "url",
}
_FACT_UNITS = {
    "batches",
    "bytes",
    "clients",
    "count",
    "Hz",
    "items",
    "lines",
    "ms",
    "percent",
    "s",
    "us",
}
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_FACT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CANONICAL_ADDRESS = re.compile(r"^0x(?:[0-9A-F]{8}|[0-9A-F]{16})$")
_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)
_CREDENTIAL = re.compile(
    r"(?i)(?:authorization|bearer\s+|api[_-]?key|password|passwd|secret|token)"
)


def _canonical_address(value: Any) -> str | None:
    if not isinstance(value, str) or not _CANONICAL_ADDRESS.fullmatch(value):
        return None
    numeric = int(value, 0)
    width = 8 if numeric <= 0xFFFFFFFF else 16
    return value if value == f"0x{numeric:0{width}X}" else None


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _bounded_number(
    value: Any, *, minimum: float = 0.0, maximum: float = 1e15,
) -> Any | None:
    if not _finite_number(value):
        return None
    number = float(value)
    if number < minimum or number > maximum:
        return None
    return value


def _safe_identifier(value: Any, *, allowed: set[str] | None = None) -> str | None:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or _CREDENTIAL.search(value)
    ):
        return None
    if allowed is not None and value not in allowed:
        return None
    return value


def _safe_label(value: Any, *, maximum: int = 48) -> str | None:
    """Return a short display label that cannot be a URL, path, or command."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum or _CREDENTIAL.search(value):
        return None
    if any(marker in value for marker in ("/", "\\", "://", "\r", "\n", "@", "=")):
        return None
    if not all(ch.isalnum() or ch in " _.-" for ch in value):
        return None
    return value


def _safe_fact_name(value: Any) -> str | None:
    if not isinstance(value, str) or not _FACT_NAME.fullmatch(value):
        return None
    # A canonical target address is useful for Dashboard navigation.  It is
    # the sole string-valued memory fact allowed into the public projection;
    # buffers, dumps, commands, and credentials remain private.
    if value == "address":
        return value
    pieces = set(re.split(r"[._-]+", value))
    if pieces & _SENSITIVE_NAME_PARTS:
        return None
    return value


def _fact(
    name: str,
    value: Any,
    *,
    unit: str | None = None,
    ok: bool | None = None,
) -> dict[str, Any] | None:
    safe_name = _safe_fact_name(name)
    if safe_name is None:
        return None
    if isinstance(value, bool):
        safe_value: Any = value
    elif _finite_number(value):
        if abs(float(value)) > 1e15:
            return None
        safe_value = value
    elif safe_name == "address":
        safe_value = _canonical_address(value)
        if safe_value is None:
            return None
    elif safe_name.endswith(".name"):
        safe_value = _safe_label(value)
        if safe_value is None:
            return None
    else:
        return None
    result: dict[str, Any] = {"name": safe_name, "value": safe_value}
    if unit in _FACT_UNITS:
        result["unit"] = unit
    if isinstance(ok, bool):
        result["ok"] = ok
    return result


def _sanitize_facts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    facts: list[dict[str, Any]] = []
    for item in value[:32]:
        if not isinstance(item, Mapping):
            continue
        projected = _fact(
            item.get("name"),
            item.get("value"),
            unit=item.get("unit"),
            ok=item.get("ok"),
        )
        if projected is not None:
            facts.append(projected)
    return facts


def _sanitize_stream(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    stream_id = _safe_identifier(value.get("id"))
    if stream_id is None:
        return None
    stream: dict[str, Any] = {"id": stream_id}
    protocol = _safe_identifier(value.get("protocol"), allowed=_STREAM_PROTOCOLS)
    if protocol is not None:
        stream["protocol"] = protocol
    state = _safe_identifier(value.get("state"), allowed=_STREAM_STATES)
    if state is not None:
        stream["state"] = state
    media_type = value.get("media_type")
    if isinstance(media_type, str) and _MEDIA_TYPE.fullmatch(media_type):
        stream["media_type"] = media_type
    max_rate = _bounded_number(value.get("max_rate"), maximum=100_000.0)
    if max_rate is not None:
        stream["max_rate"] = max_rate
    encoding = _safe_identifier(value.get("encoding"))
    if encoding is not None:
        stream["encoding"] = encoding
    for key in ("width", "height"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 0 < number <= 16_384:
            stream[key] = number
    for key in ("fps", "sample_rate_hz"):
        number = _bounded_number(value.get(key), maximum=1_000_000.0)
        if number is not None:
            stream[key] = number
    return stream


def _sanitize_artifact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    artifact_id = _safe_identifier(value.get("id"))
    if artifact_id is None:
        return None
    artifact: dict[str, Any] = {"id": artifact_id}
    name = _safe_label(value.get("name"), maximum=96)
    if name is not None:
        artifact["name"] = name
    kind = _safe_identifier(value.get("kind"))
    if kind is not None:
        artifact["kind"] = kind
    sha256 = value.get("sha256")
    if isinstance(sha256, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        artifact["sha256"] = sha256.lower()
    size_bytes = value.get("size_bytes")
    if isinstance(size_bytes, int) and not isinstance(size_bytes, bool) and 0 <= size_bytes <= 10**12:
        artifact["size_bytes"] = size_bytes
    media_type = value.get("media_type")
    if isinstance(media_type, str) and _MEDIA_TYPE.fullmatch(media_type):
        artifact["media_type"] = media_type
    return artifact


def project_event_payload(kind: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project an event to the closed, bounded public observation schema."""
    if kind not in _EVENT_KINDS or not isinstance(payload, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in _PAYLOAD_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "operation":
            projected = _safe_identifier(value, allowed=_OPERATIONS)
        elif key == "capability":
            projected = _safe_identifier(value, allowed=_CAPABILITIES)
        elif key == "action_class":
            projected = _safe_identifier(value, allowed=_ACTION_CLASSES)
        elif key == "state":
            projected = _safe_identifier(value, allowed=_STATES)
        elif key in {"summary", "error_code"}:
            projected = _safe_identifier(value)
        elif key == "progress":
            projected = _bounded_number(value, minimum=0.0, maximum=1.0)
            if projected is not None:
                projected = float(projected)
        elif key == "ok":
            projected = value if isinstance(value, bool) else None
        elif key == "facts":
            projected = _sanitize_facts(value)
        elif key == "stream":
            projected = _sanitize_stream(value)
        elif key == "artifact":
            projected = _sanitize_artifact(value)
        else:  # pragma: no cover - _PAYLOAD_KEYS is intentionally closed.
            projected = None
        if projected is not None and projected != []:
            result[key] = projected

    operation_kinds = {
        "operation.started",
        "operation.progress",
        "operation.completed",
        "operation.failed",
    }
    if kind in operation_kinds and "operation" not in result:
        return None
    if kind == "operation.progress" and "progress" not in result:
        return None
    if kind == "operation.completed" and result.get("ok") is not True:
        return None
    if kind == "operation.failed" and (
        result.get("ok") is not False or "error_code" not in result
    ):
        return None
    if kind in {"stream.checkpoint", "stream.closed"} and "stream" not in result:
        return None
    if kind == "stream.gap" and (
        "stream" not in result or not result.get("facts")
    ):
        return None
    if kind == "evidence.available" and "artifact" not in result:
        return None
    return result


def _valid_private_endpoint(endpoint: Any) -> str | None:
    if not isinstance(endpoint, str) or len(endpoint) > 512:
        return None
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except (ValueError, TypeError):
        return None
    if parsed.scheme not in {"ws", "wss"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment or not parsed.path.startswith("/ws/streams/"):
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    if port is None or not 1 <= port <= 65535:
        return None
    return endpoint


def _private_headers(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"Authorization"}:
        return None
    authorization = value.get("Authorization")
    if (
        not isinstance(authorization, str)
        or not authorization.startswith("Bearer ")
        or len(authorization) > 4096
        or "\r" in authorization
        or "\n" in authorization
    ):
        return None
    return {"Authorization": authorization}


@dataclass(frozen=True)
class _QueuedCommand:
    name: str
    args: tuple[Any, ...] = field(default=(), repr=False)
    kwargs: Mapping[str, Any] | None = field(default=None, repr=False)
    done: threading.Event | None = None


class SafeProducer:
    """Bounded, failure-isolated sidecar for one optional observe producer.

    The caller only performs bounded projection plus a non-blocking queue put.
    Importing ``hil_core``, opening the registry, filesystem writes, heartbeat,
    and producer shutdown all happen on a daemon worker.  A wedged observation
    backend can therefore strand only that daemon, never the device operation.
    """

    QUEUE_CAPACITY = 128
    HEARTBEAT_SECONDS = 10.0
    _DROPPABLE_EVENT_KINDS = {
        "operation.progress",
        "stream.checkpoint",
    }

    def __init__(
        self,
        producer: Any = None,
        *,
        factory: Callable[[], Any] | None = None,
        queue_capacity: int | None = None,
    ) -> None:
        if producer is not None and factory is not None:
            raise ValueError("producer and factory are mutually exclusive")
        capacity = queue_capacity if queue_capacity is not None else self.QUEUE_CAPACITY
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            capacity = self.QUEUE_CAPACITY
        self._capacity = min(capacity, 4096)
        self._factory = factory or ((lambda: producer) if producer is not None else None)
        self._condition = threading.Condition()
        self._commands: deque[_QueuedCommand] = deque()
        self._accepting = self._factory is not None
        self._failed = False
        self._stop_requested = False
        self._dropped_commands = 0
        self._stopped = threading.Event()
        self._worker: threading.Thread | None = None
        if self._factory is None:
            self._stopped.set()
            return
        try:
            self._worker = threading.Thread(
                target=self._worker_main,
                name="mklink-observe-writer",
                daemon=True,
            )
            self._worker.start()
        except Exception:
            self._accepting = False
            self._failed = True
            self._stopped.set()

    @property
    def available(self) -> bool:
        with self._condition:
            return self._accepting and not self._failed

    @property
    def dropped_commands(self) -> int:
        with self._condition:
            return self._dropped_commands

    @staticmethod
    def _droppable(command: _QueuedCommand) -> bool:
        if command.name == "heartbeat":
            return True
        return (
            command.name == "publish"
            and bool(command.args)
            and command.args[0] in SafeProducer._DROPPABLE_EVENT_KINDS
        )

    def _enqueue(self, command: _QueuedCommand, *, critical: bool = False) -> bool:
        with self._condition:
            if not self._accepting or self._failed or self._stop_requested:
                return False
            if len(self._commands) >= self._capacity:
                if critical:
                    victim = next(
                        (
                            index
                            for index, queued in enumerate(self._commands)
                            if self._droppable(queued)
                        ),
                        None,
                    )
                    if victim is not None:
                        del self._commands[victim]
                        self._dropped_commands += 1
                    else:
                        self._dropped_commands += 1
                        return False
                else:
                    self._dropped_commands += 1
                    return False
            self._commands.append(command)
            self._condition.notify()
            return True

    def _cancel_pending_locked(self) -> None:
        while self._commands:
            command = self._commands.popleft()
            if command.done is not None:
                command.done.set()
            else:
                self._dropped_commands += 1

    def _worker_main(self) -> None:
        producer = None
        try:
            factory = self._factory
            if factory is None:
                return
            producer = factory()
            if producer is None:
                raise RuntimeError("observe producer unavailable")
            next_heartbeat = time.monotonic() + self.HEARTBEAT_SECONDS
            while True:
                command: _QueuedCommand | None = None
                send_heartbeat = False
                with self._condition:
                    while True:
                        if self._stop_requested and not self._commands:
                            break
                        now = time.monotonic()
                        if not self._stop_requested and now >= next_heartbeat:
                            send_heartbeat = True
                            break
                        if self._commands:
                            command = self._commands.popleft()
                            break
                        timeout = max(0.0, next_heartbeat - now)
                        self._condition.wait(timeout=timeout)
                    if self._stop_requested and command is None and not send_heartbeat:
                        break

                if send_heartbeat:
                    producer.heartbeat()
                    next_heartbeat = time.monotonic() + self.HEARTBEAT_SECONDS
                    continue
                if command is None:
                    continue
                if command.name == "publish":
                    producer.publish(*command.args)
                elif command.name == "register_stream":
                    producer.register_stream(
                        *command.args,
                        **dict(command.kwargs or {}),
                    )
                elif command.name == "heartbeat":
                    producer.heartbeat()
                    next_heartbeat = time.monotonic() + self.HEARTBEAT_SECONDS
                elif command.name == "barrier" and command.done is not None:
                    command.done.set()
        except Exception:
            with self._condition:
                self._failed = True
                self._accepting = False
                self._stop_requested = True
                self._cancel_pending_locked()
                self._condition.notify_all()
        finally:
            if producer is not None:
                try:
                    producer.close()
                except Exception:
                    pass
            with self._condition:
                self._accepting = False
                self._stop_requested = True
                self._cancel_pending_locked()
                self._condition.notify_all()
            self._stopped.set()

    def publish(self, kind: str, payload: Mapping[str, Any]) -> bool:
        projected = project_event_payload(kind, payload)
        if projected is None:
            return False
        command = _QueuedCommand("publish", (kind, projected))
        return self._enqueue(
            command,
            critical=kind not in self._DROPPABLE_EVENT_KINDS,
        )

    def register_stream(
        self,
        endpoint: str,
        stream: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        origin: str | None = None,
    ) -> bool:
        private_endpoint = _valid_private_endpoint(endpoint)
        public_stream = _sanitize_stream(stream)
        private_headers = _private_headers(headers)
        if headers is not None and private_headers is None:
            return False
        if private_endpoint is None or public_stream is None:
            return False
        required = {"id", "protocol", "state", "media_type"}
        if not required.issubset(public_stream):
            return False
        return self._enqueue(
            _QueuedCommand(
                "register_stream",
                (private_endpoint, public_stream),
                {"headers": private_headers, "origin": origin},
            ),
            critical=True,
        )

    def heartbeat(self) -> bool:
        return self._enqueue(_QueuedCommand("heartbeat"))

    def flush(self, timeout: float = 1.0) -> bool:
        """Wait up to ``timeout`` for commands already queued; tests/shutdown only."""
        done = threading.Event()
        if not self._enqueue(_QueuedCommand("barrier", done=done), critical=True):
            return False
        done.wait(timeout=max(0.0, float(timeout)))
        with self._condition:
            return done.is_set() and not self._failed

    def close(self, flush_timeout: float = 0.25) -> bool:
        """Request drain/close and wait only for the caller-supplied finite bound."""
        with self._condition:
            self._accepting = False
            self._stop_requested = True
            self._condition.notify_all()
        self._stopped.wait(timeout=max(0.0, float(flush_timeout)))
        return self._stopped.is_set()


def _open_raw_producer(registry: Any, correlation: str | None) -> Any:
    """Call either spelling used while the local-v1 contract was landing."""
    opener = registry.open_producer
    try:
        parameters = inspect.signature(opener).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "source" in parameters or "private_correlation" in parameters:
        kwargs: dict[str, Any] = {"source": dict(SOURCE)}
        if correlation is not None:
            kwargs["private_correlation"] = correlation
        return opener(**kwargs)
    kwargs = {}
    if correlation is not None:
        kwargs["correlation"] = correlation
    return opener(dict(PUBLIC_SOURCE), **kwargs)


def open_producer(private_correlation: str | None = None) -> SafeProducer:
    """Create an asynchronous producer; even registry open happens off-thread."""
    correlation = private_correlation
    if correlation is not None:
        if not isinstance(correlation, str) or not 1 <= len(correlation) <= 256:
            correlation = None

    def factory() -> Any:
        observe = importlib.import_module("hil_core.observe")
        registry = observe.ObservationRegistry()
        return _open_raw_producer(registry, correlation)

    return SafeProducer(factory=factory)


_PROCESS_PRODUCER_LIMIT = 4
_PROCESS_PRODUCERS_LOCK = threading.Lock()
_PROCESS_PRODUCERS: dict[str, SafeProducer] = {}


def _correlation_key(private_correlation: str | None) -> tuple[str, str | None]:
    correlation = private_correlation
    if correlation is not None:
        if not isinstance(correlation, str) or not 1 <= len(correlation) <= 256:
            correlation = None
    return correlation or "", correlation


def process_producer(private_correlation: str | None = None) -> SafeProducer:
    """Return the bounded process-level producer for one private correlation."""
    key, correlation = _correlation_key(private_correlation)
    with _PROCESS_PRODUCERS_LOCK:
        producer = _PROCESS_PRODUCERS.get(key)
        if producer is not None:
            return producer
        if len(_PROCESS_PRODUCERS) >= _PROCESS_PRODUCER_LIMIT:
            return SafeProducer()
        producer = open_producer(correlation)
        _PROCESS_PRODUCERS[key] = producer
        return producer


def flush_process_observation(timeout: float = 1.0) -> bool:
    """Bounded test/shutdown barrier across current process-level producers."""
    with _PROCESS_PRODUCERS_LOCK:
        producers = list(_PROCESS_PRODUCERS.values())
    deadline = time.monotonic() + max(0.0, float(timeout))
    ok = True
    for producer in producers:
        remaining = max(0.0, deadline - time.monotonic())
        ok = producer.flush(remaining) and ok
    return ok


def release_process_producer(
    private_correlation: str | None,
    producer: SafeProducer,
    *,
    timeout: float = 0.25,
) -> bool:
    """Remove and finitely close one producer without touching other sessions."""
    key, _ = _correlation_key(private_correlation)
    closed = producer.close(timeout)
    with _PROCESS_PRODUCERS_LOCK:
        if closed and _PROCESS_PRODUCERS.get(key) is producer:
            del _PROCESS_PRODUCERS[key]
    return closed


def shutdown_process_observation(timeout: float = 0.5) -> bool:
    """Finitely drain/close only producers owned by this Python process."""
    with _PROCESS_PRODUCERS_LOCK:
        producers = list(_PROCESS_PRODUCERS.values())
        _PROCESS_PRODUCERS.clear()
    deadline = time.monotonic() + max(0.0, float(timeout))
    ok = True
    for producer in producers:
        remaining = max(0.0, deadline - time.monotonic())
        ok = producer.close(remaining) and ok
    return ok


def error_code(error: BaseException) -> str:
    """Return a stable code without exposing an exception message."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, FileNotFoundError):
        return "not_found"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, ConnectionError):
        return "connection_failed"
    if isinstance(error, ValueError):
        return "invalid_argument"
    return "operation_failed"


class OperationObservation:
    """Context-managed lifecycle events for one existing MKLink operation."""

    _MIN_PROGRESS_DELTA = 0.05

    def __init__(
        self,
        operation: str,
        *,
        capability: str,
        action_class: str,
        private_correlation: str | None = None,
    ) -> None:
        self.operation = operation
        self.capability = capability
        self.action_class = action_class
        self.private_correlation = private_correlation
        self._producer = SafeProducer()
        self._ended = False
        self._last_progress = -1.0
        self._progress_scale: float | None = None

    def _base(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "capability": self.capability,
            "action_class": self.action_class,
        }

    def __enter__(self) -> "OperationObservation":
        self._producer = process_producer(self.private_correlation)
        self._producer.publish("operation.started", self._base())
        return self

    def progress(self, value: Any) -> None:
        if not _finite_number(value):
            return
        number = float(value)
        if self._progress_scale is None:
            if number > 1.0:
                self._progress_scale = 100.0
            elif 0.0 < number < 1.0:
                self._progress_scale = 1.0
            elif number == 1.0:
                # A first value of 1 is ambiguous: some backends mean 1%,
                # others mean completion.  The terminal lifecycle event is
                # authoritative, so wait for the next unambiguous sample.
                return
        progress = number / (self._progress_scale or 1.0)
        progress = min(1.0, max(0.0, progress))
        if progress <= self._last_progress:
            return
        if progress < 1.0 and progress - self._last_progress < self._MIN_PROGRESS_DELTA:
            return
        self._last_progress = progress
        payload = self._base()
        payload["progress"] = progress
        self._producer.publish("operation.progress", payload)

    def complete(
        self,
        *,
        facts: Iterable[Mapping[str, Any]] = (),
        artifact: Mapping[str, Any] | None = None,
    ) -> None:
        if self._ended:
            return
        payload = self._base()
        payload.update({"ok": True, "facts": list(facts)[:32]})
        if artifact is not None:
            payload["artifact"] = artifact
        self._producer.publish("operation.completed", payload)
        self._ended = True

    def fail(
        self,
        error: BaseException | str,
        *,
        facts: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        if self._ended:
            return
        if isinstance(error, str):
            code = _safe_identifier(error) or "operation_failed"
        else:
            code = error_code(error)
        payload = self._base()
        payload.update({
            "ok": False,
            "error_code": code,
            "facts": list(facts)[:32],
        })
        self._producer.publish("operation.failed", payload)
        self._ended = True

    def evidence(self, artifact: Mapping[str, Any]) -> None:
        self._producer.publish("evidence.available", {"artifact": artifact})

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        if exc is not None:
            self.fail(exc)
        elif not self._ended:
            self.complete()
        return False


def observe_operation(
    operation: str,
    *,
    capability: str,
    action_class: str,
    private_correlation: str | None = None,
) -> OperationObservation:
    return OperationObservation(
        operation,
        capability=capability,
        action_class=action_class,
        private_correlation=private_correlation,
    )


def flash_facts(
    result: Mapping[str, Any] | None,
    *,
    verify_requested: bool,
    reset_requested: bool,
) -> list[dict[str, Any]]:
    result = result if isinstance(result, Mapping) else {}
    candidates = [
        _fact("verified", result.get("verified"), ok=result.get("verified")),
        _fact("verify_requested", bool(verify_requested)),
        _fact("reset_requested", bool(reset_requested)),
        _fact("duration", result.get("time_ms"), unit="ms"),
        _fact(
            "region_count",
            len(result.get("regions", [])) if isinstance(result.get("regions"), list) else None,
            unit="count",
        ),
    ]
    return [item for item in candidates if item is not None]


def rtt_facts(
    output: Any,
    *,
    duration: Any = None,
    matched: Any = None,
) -> list[dict[str, Any]]:
    if isinstance(output, str):
        byte_count = len(output.encode("utf-8", errors="replace"))
        line_count = len(output.splitlines())
    elif isinstance(output, (bytes, bytearray, memoryview)):
        byte_count = len(output)
        line_count = bytes(output).count(b"\n")
    else:
        byte_count = 0
        line_count = 0
    candidates = [
        _fact("bytes_read", byte_count, unit="bytes"),
        _fact("line_count", line_count, unit="lines"),
        _fact("duration", duration, unit="s"),
        _fact("matched", matched),
    ]
    return [item for item in candidates if item is not None]


def systemview_capture_facts(result: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    result = result if isinstance(result, Mapping) else {}
    events = result.get("events")
    event_count = result.get("event_count")
    if event_count is None and isinstance(events, list):
        event_count = len(events)
    candidates = [
        _fact("event_count", event_count, unit="count"),
        _fact("bytes_read", result.get("bytes_read"), unit="bytes"),
        _fact("synced", result.get("synced"), ok=result.get("synced")),
        _fact("dropped_bytes", result.get("dropped_bytes"), unit="bytes"),
        _fact("dropped_packets", result.get("dropped_packets"), unit="count"),
        _fact("cpu_frequency", result.get("cpu_freq"), unit="Hz"),
    ]
    return [item for item in candidates if item is not None]


def systemview_analysis_facts(report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    report = report if isinstance(report, Mapping) else {}
    summary = report.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    anomalies = report.get("anomalies")
    tasks = report.get("tasks")
    candidates = [
        _fact("event_count", summary.get("event_count"), unit="count"),
        _fact("analyzed_event_count", summary.get("analyzed_event_count"), unit="count"),
        _fact("observed", summary.get("observed_us"), unit="us"),
        _fact("switch_count", summary.get("switch_count"), unit="count"),
        _fact("switch_rate", summary.get("switches_per_sec"), unit="count"),
        _fact("task_count", summary.get("task_count"), unit="count"),
        _fact("idle_cpu", summary.get("idle_pct"), unit="percent"),
        _fact("isr_cpu", summary.get("isr_cpu_pct"), unit="percent"),
        _fact(
            "anomaly_count",
            len(anomalies) if isinstance(anomalies, list) else None,
            unit="count",
        ),
    ]
    facts = [item for item in candidates if item is not None]
    if isinstance(tasks, list):
        for index, task in enumerate(tasks[:5]):
            if not isinstance(task, Mapping):
                continue
            prefix = f"task.{index}"
            name = _safe_label(task.get("name")) or f"task-{index + 1}"
            for item in (
                _fact(f"{prefix}.name", name),
                _fact(f"{prefix}.run", task.get("run_us"), unit="us"),
                _fact(f"{prefix}.cpu", task.get("cpu_pct"), unit="percent"),
                _fact(f"{prefix}.switches", task.get("switches"), unit="count"),
            ):
                if item is not None and len(facts) < 32:
                    facts.append(item)
    return facts


def memory_read_facts(
    address: str,
    *,
    requested_bytes: Any,
    bytes_read: Any,
) -> list[dict[str, Any]]:
    """Project one memory read without exposing the returned bytes."""
    candidates = [
        _fact("address", address),
        _fact("requested_bytes", requested_bytes, unit="bytes"),
        _fact("bytes_read", bytes_read, unit="bytes"),
    ]
    return [item for item in candidates if item is not None]


def memory_write_facts(address: str, *, bytes_written: Any) -> list[dict[str, Any]]:
    """Project one write without exposing the input buffer."""
    candidates = [
        _fact("address", address),
        _fact("bytes_written", bytes_written, unit="bytes"),
    ]
    return [item for item in candidates if item is not None]


def memory_dump_facts(
    address: str,
    *,
    total_bytes: Any,
    region_count: Any,
    sample_count: Any = 1,
) -> list[dict[str, Any]]:
    """Project a bounded dump summary; chunk data and CRC values stay private."""
    candidates = [
        _fact("address", address),
        _fact("total_bytes", total_bytes, unit="bytes"),
        _fact("region_count", region_count, unit="count"),
        _fact("sample_count", sample_count, unit="count"),
    ]
    return [item for item in candidates if item is not None]


def memory_flush_facts(
    address: str | None,
    *,
    total_bytes: Any,
    region_count: Any,
    batch_count: Any,
    successful_batches: Any,
    failed_batches: Any,
) -> list[dict[str, Any]]:
    """Project a flush summary; write buffers never enter observation events."""
    candidates = [
        _fact("address", address),
        _fact("total_bytes", total_bytes, unit="bytes"),
        _fact("region_count", region_count, unit="count"),
        _fact("batch_count", batch_count, unit="batches"),
        _fact("successful_batches", successful_batches, unit="batches"),
        _fact("failed_batches", failed_batches, unit="batches"),
    ]
    return [item for item in candidates if item is not None]


def stream_stats_facts(stats: Any) -> list[dict[str, Any]]:
    if is_dataclass(stats):
        values = asdict(stats)
    elif isinstance(stats, Mapping):
        values = dict(stats)
    else:
        values = {}
    units = {
        "produced_batches": "batches",
        "produced_items": "items",
        "produced_bytes": "bytes",
        "delivered_batches": "batches",
        "delivered_items": "items",
        "delivered_bytes": "bytes",
        "dropped_batches": "batches",
        "dropped_items": "items",
        "dropped_bytes": "bytes",
        "active_clients": "clients",
        "queue_high_water_mark": "batches",
        "last_sequence": "count",
    }
    facts = [_fact(name, values.get(name), unit=unit) for name, unit in units.items()]
    return [item for item in facts if item is not None]


_STREAMS: Mapping[str, Mapping[str, Any]] = {
    "memory": {
        "id": "mklink.memory",
        "protocol": "websocket",
        "state": "available",
        "media_type": "application/vnd.mklink.mkst",
        "encoding": STREAM_PROTOCOL,
    },
    "systemview": {
        "id": "mklink.systemview",
        "protocol": "websocket",
        "state": "available",
        "media_type": "application/vnd.mklink.mkst",
        "encoding": STREAM_PROTOCOL,
    },
    "vofa": {
        "id": "mklink.vofa",
        "protocol": "websocket",
        "state": "available",
        "media_type": "application/vnd.mklink.mkst",
        "encoding": STREAM_PROTOCOL,
    },
    "rtt": {
        "id": "mklink.rtt",
        "protocol": "websocket",
        "state": "available",
        "media_type": "application/vnd.mklink.mkst",
        "encoding": STREAM_PROTOCOL,
    },
    "rtt-terminal": {
        "id": "mklink.rtt-terminal",
        "protocol": "websocket",
        "state": "available",
        "media_type": "application/vnd.mklink.mkst",
        "encoding": STREAM_PROTOCOL,
    },
    "serial": {
        "id": "mklink.serial",
        "protocol": "websocket",
        "state": "available",
        "media_type": "application/vnd.mklink.mkst",
        "encoding": STREAM_PROTOCOL,
    },
    "superwatch": {
        "id": "mklink.superwatch",
        "protocol": "websocket",
        "state": "available",
        "media_type": "application/vnd.mklink.mkst",
        "encoding": STREAM_PROTOCOL,
    },
}


def stream_descriptor(name: str, *, state: str | None = None) -> dict[str, Any] | None:
    descriptor = _STREAMS.get(name)
    if descriptor is None:
        return None
    projected = dict(descriptor)
    if state is not None:
        if state not in _STREAM_STATES:
            return None
        projected["state"] = state
    return projected


class StreamObservation:
    """Observe existing StreamHub stats without starting an acquisition."""

    POLL_SECONDS = 2.0
    IDLE_CHECKPOINT_POLLS = 5

    def __init__(self, registry: Mapping[str, Any]) -> None:
        self._registry = registry
        self._producer = SafeProducer()
        self._endpoints: dict[str, str] = {}
        self._headers: dict[str, str] | None = None
        self._correlation: str | None = None
        self._last_stats: dict[str, dict[str, Any]] = {}
        self._idle_polls: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    def configure(
        self,
        *,
        host: str,
        port: int,
        auth_token: str | None = None,
        private_correlation: str | None = None,
    ) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            return
        normalized = host.strip().lower() if isinstance(host, str) else ""
        if normalized in {"127.0.0.1", "localhost", "0.0.0.0"}:
            authority = f"127.0.0.1:{port}"
        elif normalized in {"::1", "[::1]", "::", "[::]"}:
            authority = f"[::1]:{port}"
        else:
            return
        endpoints = {
            name: f"ws://{authority}/ws/streams/{name}"
            for name in _STREAMS
            if name in self._registry
        }
        if not endpoints or any(_valid_private_endpoint(value) is None for value in endpoints.values()):
            return
        headers = None
        if auth_token:
            headers = _private_headers({"Authorization": f"Bearer {auth_token}"})
            if headers is None:
                return
        self._endpoints = endpoints
        self._headers = headers
        self._correlation = private_correlation

    async def start(self) -> None:
        if not self._endpoints or self._task is not None:
            return
        self._producer = process_producer(self._correlation)
        if not self._producer.available:
            return
        for name, endpoint in self._endpoints.items():
            self._producer.register_stream(
                endpoint,
                _STREAMS[name],
                headers=self._headers,
            )
        if self._producer.available:
            self._task = asyncio.create_task(self._monitor())

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(self.POLL_SECONDS)
            await asyncio.to_thread(self.poll_once)

    def poll_once(self) -> None:
        if not self._producer.available:
            return
        self._producer.heartbeat()
        for name in self._endpoints:
            try:
                hub = self._registry.get(name)
                if hub is None:
                    continue
                stats = hub.stats()
                values = asdict(stats) if is_dataclass(stats) else dict(stats)
            except Exception:
                continue
            previous = self._last_stats.get(name)
            stream = _STREAMS[name]
            dropped_now = int(values.get("dropped_batches") or 0)
            dropped_before = int((previous or {}).get("dropped_batches") or 0)
            if previous is not None and dropped_now > dropped_before:
                gap_facts = [
                    _fact("dropped_batches", dropped_now - dropped_before, unit="batches"),
                    _fact(
                        "dropped_items",
                        int(values.get("dropped_items") or 0)
                        - int(previous.get("dropped_items") or 0),
                        unit="items",
                    ),
                    _fact(
                        "dropped_bytes",
                        int(values.get("dropped_bytes") or 0)
                        - int(previous.get("dropped_bytes") or 0),
                        unit="bytes",
                    ),
                ]
                self._producer.publish(
                    "stream.gap",
                    {"stream": stream, "facts": [fact for fact in gap_facts if fact]},
                )
            changed = previous is None or any(
                values.get(key) != previous.get(key)
                for key in (
                    "produced_batches",
                    "delivered_batches",
                    "dropped_batches",
                    "active_clients",
                    "last_sequence",
                )
            )
            idle_polls = self._idle_polls.get(name, 0) + 1
            if changed or idle_polls >= self.IDLE_CHECKPOINT_POLLS:
                self._producer.publish(
                    "stream.checkpoint",
                    {"stream": stream, "facts": stream_stats_facts(values)},
                )
                idle_polls = 0
            self._idle_polls[name] = idle_polls
            self._last_stats[name] = values

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for name in self._endpoints:
            stream = dict(_STREAMS[name])
            stream["state"] = "closed"
            self._producer.publish("stream.closed", {"stream": stream})
        release_process_producer(
            self._correlation,
            self._producer,
            timeout=0.25,
        )


def install_stream_observation(app: Any, registry: Mapping[str, Any]) -> StreamObservation:
    observation = StreamObservation(registry)
    app.state.mklink_stream_observation = observation
    app.add_event_handler("startup", observation.start)
    app.add_event_handler("shutdown", observation.stop)
    return observation


def configure_stream_observation(
    app: Any,
    *,
    host: str,
    port: int,
    auth_token: str | None,
    private_correlation: str | None,
) -> None:
    observation = getattr(app.state, "mklink_stream_observation", None)
    if not isinstance(observation, StreamObservation):
        return
    try:
        observation.configure(
            host=host,
            port=port,
            auth_token=auth_token,
            private_correlation=private_correlation,
        )
    except Exception:
        pass


atexit.register(shutdown_process_observation, timeout=0.1)
