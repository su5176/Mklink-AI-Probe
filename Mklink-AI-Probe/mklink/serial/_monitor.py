"""多串口监控线程管理器。"""

from __future__ import annotations

import collections
import io
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from mklink.serial._autoreply import AutoReplyEngine
from mklink.serial._frame import FrameParser, ParsedFrame
from mklink.serial._logger import FileLogger
from mklink.serial._port import SerialPort


@dataclass
class SerialEvent:
    timestamp: float
    port: str
    direction: str
    raw: bytes
    parsed: ParsedFrame | None = None


class SerialMonitor:
    def __init__(
        self,
        ports: list[dict],
        profile: dict | None = None,
        auto_reply_rules: list[dict] | None = None,
        logger: FileLogger | None = None,
        event_callback: Callable[[SerialEvent], None] | None = None,
        chunk_callback: Callable[[str, str, bytes, float], None] | None = None,
        protocol_callback: Callable[[str, str, bytes, float], None] | None = None,
    ):
        self._port_configs = ports
        self._profile = profile
        self._auto_reply_rules = auto_reply_rules
        self._logger = logger
        self._event_callback = event_callback
        self._chunk_callback = chunk_callback
        self._protocol_callback = protocol_callback

        self._events: collections.deque[SerialEvent] = collections.deque(maxlen=10000)
        self._stop_event = threading.Event()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._serial_ports: dict[str, SerialPort] = {}
        self._port_statuses: dict[str, str] = {cfg["port"]: "closed" for cfg in ports}
        self._lock = threading.Lock()
        self._protocol_lock = threading.Lock()
        self._protocol_queues: dict[str, queue.Queue[bytes]] = {}
        # Completed protocols hand unread terminal bytes back to the normal
        # reader here.  Only the reader thread emits them, preserving parser,
        # event and callback ordering without a second serial consumer.
        self._protocol_handoffs: dict[str, bytearray] = {}

        self._auto_reply_engine: AutoReplyEngine | None = None
        if auto_reply_rules:
            self._auto_reply_engine = AutoReplyEngine()
            self._auto_reply_engine.load_rules(auto_reply_rules)

        self._parsers: dict[str, FrameParser] = {}
        if profile:
            for cfg in ports:
                self._parsers[cfg["port"]] = FrameParser(profile)

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._running = True

        for cfg in self._port_configs:
            t = threading.Thread(
                target=self._reader_loop,
                args=(cfg,),
                daemon=True,
                name=f"serial-reader-{cfg['port']}",
            )
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads.clear()

        with self._lock:
            for sp in self._serial_ports.values():
                sp.close()
            self._serial_ports.clear()
            for port_name in self._port_statuses:
                self._port_statuses[port_name] = "closed"

        self._running = False

    def send(self, port: str, data: bytes) -> bool:
        with self._protocol_lock:
            if port in self._protocol_queues:
                return False
            with self._lock:
                sp = self._serial_ports.get(port)
                if sp is None or not sp.is_open:
                    return False
                try:
                    sp.write(data)
                except Exception:
                    return False

        timestamp = time.time()
        self._emit_chunk(port, "TX", data, timestamp)
        evt = SerialEvent(
            timestamp=timestamp,
            port=port,
            direction="TX",
            raw=data,
        )
        self._emit_event(evt)
        return True

    def send_ymodem(
        self,
        port: str,
        data: bytes,
        filename: str,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback=None,
    ) -> None:
        """Send one in-memory file while exclusively consuming *port* RX bytes.

        The normal reader thread keeps ownership of the open serial port.
        Protocol RX/TX bytes use a separate trace callback instead of ordinary
        terminal/log streams. Framing and auto-reply remain paused so control
        bytes are consumed exactly once.
        """
        from mklink.serial._ymodem import YModemCancelled, YModemSender

        receive_queue: queue.Queue[bytes] = queue.Queue()
        with self._protocol_lock:
            if port in self._protocol_queues:
                raise RuntimeError(f"serial port {port} already has an active transfer")
            with self._lock:
                serial_port = self._serial_ports.get(port)
                if serial_port is None or not serial_port.is_open:
                    raise RuntimeError(f"serial port {port} is not open")
            self._protocol_queues[port] = receive_queue

        cancellation = cancel_event or threading.Event()

        def read_protocol(timeout: float) -> bytes:
            deadline = time.monotonic() + timeout
            while True:
                if cancellation.is_set() or self._stop_event.is_set():
                    raise YModemCancelled("YMODEM transfer cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                try:
                    return receive_queue.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue

        def write_protocol(payload: bytes) -> None:
            if cancellation.is_set() or self._stop_event.is_set():
                raise YModemCancelled("YMODEM transfer cancelled")
            with self._lock:
                current = self._serial_ports.get(port)
                if current is not serial_port or not current.is_open:
                    raise RuntimeError(f"serial port {port} closed during YMODEM transfer")
                current.write(payload)
            self._emit_protocol_chunk(port, "TX", payload, time.time())

        sender = YModemSender(
            read_protocol,
            write_protocol,
            cancel_event=cancellation,
            progress_callback=progress_callback,
        )
        completed = False
        try:
            sender.send(io.BytesIO(data), filename, len(data))
            completed = True
        finally:
            with self._protocol_lock:
                if self._protocol_queues.get(port) is receive_queue:
                    pending = bytearray()
                    if completed:
                        take_pending = getattr(sender, "take_pending_rx", None)
                        if callable(take_pending):
                            pending.extend(take_pending())
                        # Reader queue writes also hold _protocol_lock, so once
                        # this lock is acquired no late put can race the drain.
                        while True:
                            try:
                                pending.extend(receive_queue.get_nowait())
                            except queue.Empty:
                                break
                        if pending:
                            self._protocol_handoffs.setdefault(
                                port, bytearray(),
                            ).extend(pending)
                    self._protocol_queues.pop(port, None)

    def send_all(self, data: bytes) -> None:
        for cfg in self._port_configs:
            self.send(cfg["port"], data)

    def get_events(self, max_count: int = 100) -> list[SerialEvent]:
        results: list[SerialEvent] = []
        for _ in range(max_count):
            try:
                results.append(self._events.popleft())
            except IndexError:
                break
        return results

    def is_running(self) -> bool:
        return self._running

    @property
    def port_status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._port_statuses)

    def __enter__(self) -> SerialMonitor:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def _emit_event(self, evt: SerialEvent) -> None:
        self._events.append(evt)
        if self._event_callback:
            try:
                self._event_callback(evt)
            except Exception:
                pass
        if self._logger:
            decoded = None
            if evt.parsed and evt.parsed.fields:
                decoded = evt.parsed.fields
            try:
                self._logger.log(evt.direction, evt.port, evt.raw, decoded)
            except Exception:
                pass

    def _emit_chunk(
        self,
        port: str,
        direction: str,
        data: bytes,
        timestamp: float,
    ) -> None:
        if not data or self._chunk_callback is None:
            return
        try:
            self._chunk_callback(port, direction, data, timestamp)
        except Exception:
            pass

    def _emit_protocol_chunk(
        self,
        port: str,
        direction: str,
        data: bytes,
        timestamp: float,
    ) -> None:
        if not data or self._protocol_callback is None:
            return
        try:
            self._protocol_callback(port, direction, data, timestamp)
        except Exception:
            pass

    def _reader_loop(self, cfg: dict) -> None:
        port_name = cfg["port"]
        baudrate = cfg.get("baudrate", 115200)
        databits = cfg.get("databits", 8)
        stopbits = cfg.get("stopbits", 1)
        parity = cfg.get("parity", "N")

        while not self._stop_event.is_set():
            sp = SerialPort(
                port=port_name,
                baudrate=baudrate,
                databits=databits,
                stopbits=stopbits,
                parity=parity,
            )
            if not sp.open():
                with self._lock:
                    self._port_statuses[port_name] = "error: port is busy or unavailable"
                self._stop_event.wait(2.0)
                continue

            with self._lock:
                self._serial_ports[port_name] = sp
                self._port_statuses[port_name] = "open"

            parser = self._parsers.get(port_name)
            line_buffer = bytearray()
            protocol_was_active = False

            try:
                while not self._stop_event.is_set():
                    data = sp.read_available()
                    with self._protocol_lock:
                        protocol_queue = self._protocol_queues.get(port_name)
                        if protocol_queue is not None:
                            if data:
                                self._emit_protocol_chunk(
                                    port_name, "RX", data, time.time(),
                                )
                                # Keep the put inside the lock.  Transfer
                                # teardown can now remove+drain atomically.
                                protocol_queue.put(data)
                            handoff = b""
                        else:
                            handoff = bytes(
                                self._protocol_handoffs.pop(port_name, b""),
                            )
                    if protocol_queue is not None:
                        if not protocol_was_active:
                            line_buffer.clear()
                        protocol_was_active = True
                        if not data:
                            self._stop_event.wait(0.01)
                        continue
                    if protocol_was_active:
                        line_buffer.clear()
                        protocol_was_active = False
                    if handoff:
                        # A protocol may start and finish between reader
                        # iterations, so the handoff itself is also a boundary.
                        line_buffer.clear()
                        self._process_rx_data(
                            port_name, handoff, parser, line_buffer,
                        )
                    if data:
                        self._process_rx_data(
                            port_name, data, parser, line_buffer,
                        )
                    elif not handoff:
                        self._stop_event.wait(0.01)

            except Exception as e:
                with self._lock:
                    self._port_statuses[port_name] = f"error: {e}"
            finally:
                sp.close()
                with self._lock:
                    self._serial_ports.pop(port_name, None)

            if not self._stop_event.is_set():
                self._stop_event.wait(2.0)

    def _process_rx_data(
        self,
        port_name: str,
        data: bytes,
        parser: FrameParser | None,
        line_buffer: bytearray,
    ) -> None:
        """Publish one ordinary RX chunk from the sole reader thread."""
        self._emit_chunk(port_name, "RX", data, time.time())

        if parser:
            frames = parser.feed(data)
            for frame in frames:
                evt = SerialEvent(
                    timestamp=time.time(),
                    port=port_name,
                    direction="RX",
                    raw=frame.raw,
                    parsed=frame,
                )
                self._emit_event(evt)
                self._handle_auto_reply(port_name, frame.raw)
            return

        line_buffer.extend(data)
        while b"\n" in line_buffer:
            idx = line_buffer.index(b"\n")
            line = bytes(line_buffer[: idx + 1])
            del line_buffer[: idx + 1]
            evt = SerialEvent(
                timestamp=time.time(),
                port=port_name,
                direction="RX",
                raw=line,
            )
            self._emit_event(evt)
            self._handle_auto_reply(port_name, line)

        if len(line_buffer) > 4096:
            raw = bytes(line_buffer)
            evt = SerialEvent(
                timestamp=time.time(),
                port=port_name,
                direction="RX",
                raw=raw,
            )
            self._emit_event(evt)
            self._handle_auto_reply(port_name, raw)
            line_buffer.clear()

    def _handle_auto_reply(self, port_name: str, data: bytes) -> None:
        if not self._auto_reply_engine:
            return
        replies = self._auto_reply_engine.check(data)
        for reply_data, delay in replies:
            if delay > 0:
                timer = threading.Timer(
                    delay,
                    self._send_auto_reply,
                    args=(port_name, reply_data),
                )
                timer.daemon = True
                timer.start()
            else:
                self._send_auto_reply(port_name, reply_data)

    def _send_auto_reply(self, port_name: str, data: bytes) -> None:
        with self._lock:
            sp = self._serial_ports.get(port_name)
            if sp is None or not sp.is_open:
                return
            try:
                sp.write(data)
            except Exception:
                return

        timestamp = time.time()
        self._emit_chunk(port_name, "TX", data, timestamp)
        evt = SerialEvent(
            timestamp=timestamp,
            port=port_name,
            direction="TX",
            raw=data,
        )
        self._emit_event(evt)
