"""多串口监控线程管理器。"""

from __future__ import annotations

import collections
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
    ):
        self._port_configs = ports
        self._profile = profile
        self._auto_reply_rules = auto_reply_rules
        self._logger = logger
        self._event_callback = event_callback

        self._events: collections.deque[SerialEvent] = collections.deque(maxlen=10000)
        self._stop_event = threading.Event()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._serial_ports: dict[str, SerialPort] = {}
        self._port_statuses: dict[str, str] = {cfg["port"]: "closed" for cfg in ports}
        self._lock = threading.Lock()

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
        with self._lock:
            sp = self._serial_ports.get(port)
            if sp is None or not sp.is_open:
                return False
            try:
                sp.write(data)
            except Exception:
                return False

        evt = SerialEvent(
            timestamp=time.time(),
            port=port,
            direction="TX",
            raw=data,
        )
        self._emit_event(evt)
        return True

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
                    self._port_statuses[port_name] = "error: failed to open"
                self._stop_event.wait(2.0)
                continue

            with self._lock:
                self._serial_ports[port_name] = sp
                self._port_statuses[port_name] = "open"

            parser = self._parsers.get(port_name)
            line_buffer = bytearray()

            try:
                while not self._stop_event.is_set():
                    data = sp.read_available()
                    if not data:
                        self._stop_event.wait(0.01)
                        continue

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
                    else:
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
                            evt = SerialEvent(
                                timestamp=time.time(),
                                port=port_name,
                                direction="RX",
                                raw=bytes(line_buffer),
                            )
                            self._emit_event(evt)
                            self._handle_auto_reply(port_name, bytes(line_buffer))
                            line_buffer.clear()

            except Exception as e:
                with self._lock:
                    self._port_statuses[port_name] = f"error: {e}"
            finally:
                sp.close()
                with self._lock:
                    self._serial_ports.pop(port_name, None)

            if not self._stop_event.is_set():
                self._stop_event.wait(2.0)

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

        evt = SerialEvent(
            timestamp=time.time(),
            port=port_name,
            direction="TX",
            raw=data,
        )
        self._emit_event(evt)
