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
import json
import logging
import math
import threading
import time
from typing import Any, Generator

logger = logging.getLogger(__name__)


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
# RTT SSE Generator
# ---------------------------------------------------------------------------

class RttStreamManager:
    """Manages RTT streaming sessions with SSE output."""

    def __init__(self):
        self._bridge = AsyncBridge()
        self._thread: threading.Thread | None = None
        self._running = False
        self._paused = threading.Event()
        self._paused.set()  # not paused
        self._stop_event = threading.Event()
        self._history: list[dict] = []
        self._max_history = 500
        self._parser = None
        self._interval = 0.0
        self._stats = {"parsed_lines": 0, "raw_lines": 0}

    @property
    def running(self) -> bool:
        return self._running and not self._stop_event.is_set()

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    def start(self, device, *, addr: str | None = None, channel: int = 0,
              duration: float = 86400) -> None:
        """Start RTT polling in a background thread."""
        if self._running:
            return

        self._stop_event.clear()
        self._paused.set()
        self._running = True
        self._history.clear()
        self._stats = {"parsed_lines": 0, "raw_lines": 0}

        # Auto-detect parser strategy from initial RTT output
        from mklink.rtt_viewer import RttLineParser
        self._parser = RttLineParser("kv")  # will auto-detect on first lines

        def _poll():
            try:
                device.rtt_start(addr, channel=channel)
                start_time = time.time()
                sample_lines = []

                while not self._stop_event.is_set():
                    if time.time() - start_time > duration:
                        break
                    if not self._paused.is_set():
                        time.sleep(0.05)
                        continue

                    try:
                        text = device.rtt_read(duration=0.5)
                    except Exception:
                        time.sleep(0.1)
                        continue

                    if not text or not text.strip():
                        continue

                    now = time.time()
                    for line in text.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        self._stats["raw_lines"] += 1

                        # Collect samples for auto-detection
                        if len(sample_lines) < 20:
                            sample_lines.append(line)
                            if len(sample_lines) == 20:
                                self._parser = RttLineParser.auto_detect(sample_lines)

                        parsed = self._parser.parse(line)
                        if parsed is not None:
                            parsed["_t"] = now
                            self._stats["parsed_lines"] += 1
                            self._bridge.put({"event": "data", **parsed})
                            # History ring buffer
                            self._history.append(parsed)
                            if len(self._history) > self._max_history:
                                self._history = self._history[-self._max_history:]
                        else:
                            # Raw line event
                            self._bridge.put({"event": "raw", "line": line, "_t": now})

            except Exception as e:
                logger.error("RTT stream error: %s", e)
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


# ---------------------------------------------------------------------------
# SuperWatch SSE Generator
# ---------------------------------------------------------------------------

class SuperWatchStreamManager:
    """Manages SuperWatch variable polling with SSE output.

    Uses SuperWatchRuntime for DWARF-based variable resolution and
    efficient block-based memory reads.
    """

    def __init__(self):
        self._bridge = AsyncBridge()
        self._thread: threading.Thread | None = None
        self._running = False
        self._collecting = threading.Event()
        self._stop_event = threading.Event()
        self._interval = 0.1  # 100ms default
        self._device = None
        self._runtime = None
        self._read_lock = threading.Lock()
        self._origin_us: int | None = None

    @property
    def running(self) -> bool:
        return self._running

    def prepare(self, device) -> None:
        """Build runtime from DWARF info so search/add work before collection starts."""
        if self._runtime is not None:
            return
        self._device = device
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
            svd_registers=svd_registers,
            port=getattr(device, "_port", None),
            read_lock=self._read_lock,
        )

    def start(self, device) -> None:
        if self._running:
            return
        self.prepare(device)
        self._stop_event.clear()
        self._collecting.set()
        self._running = True
        self._origin_us = None

        def _poll():
            try:
                while not self._stop_event.is_set():
                    if not self._collecting.is_set() or not self._runtime.items:
                        time.sleep(0.5)
                        continue
                    t0 = time.monotonic()
                    try:
                        from mklink.superwatch import sample_blocks
                        with self._read_lock:
                            result = sample_blocks(
                                self._runtime.blocks,
                                origin_us=self._origin_us,
                                bridge=device._bridge,
                            )
                        self._origin_us = result.origin_us
                        for point in result.points:
                            self._bridge.put({"event": "data", **point})
                    except Exception as e:
                        logger.debug("SuperWatch poll error: %s", e)
                        self._bridge.put({"event": "error", "message": str(e)})
                    elapsed = time.monotonic() - t0
                    remaining = max(0.0, self._interval - elapsed)
                    self._stop_event.wait(timeout=remaining)
            except Exception as e:
                logger.error("SuperWatch stream error: %s", e)
                self._bridge.put({"event": "error", "message": str(e)})
            finally:
                self._running = False
                self._bridge.put({"event": "stopped"})
                self._bridge.stop()

        self._thread = threading.Thread(target=_poll, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._collecting.clear()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False

    def add_watch(self, name: str) -> dict:
        if self._runtime is None:
            return {"error": "SuperWatch not started"}
        result = self._runtime.add(name)
        return {"item": result}

    def remove_watch(self, name: str) -> dict:
        if self._runtime is None:
            return {"error": "SuperWatch not started"}
        result = self._runtime.remove(name)
        return {"item": result}

    def search(self, query: str) -> list[dict]:
        if self._runtime is None:
            return []
        return self._runtime.search(query)

    def pause(self) -> None:
        self._collecting.clear()

    def resume(self) -> None:
        self._collecting.set()

    def start_collecting(self) -> None:
        self._collecting.set()

    def set_interval(self, interval: float) -> float:
        self._interval = max(0.0, min(60.0, interval))
        return self._interval

    def get_status(self) -> dict:
        if self._collecting.is_set():
            state = "running"
        elif self._running:
            state = "paused"
        else:
            state = "stopped"
        return {
            "state": state,
            "interval": self._interval,
            "items": self.list_watches(),
        }

    def list_watches(self) -> list[dict]:
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
        if self._runtime and self._runtime.items:
            from mklink.superwatch import make_channel_metadata
            meta = make_channel_metadata(self._runtime.items)
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

    def __init__(self):
        self._bridge = AsyncBridge()
        self._monitor = None
        self._running = False
        self._port_config: dict = {}
        self._profile: dict | None = None
        self._auto_reply_rules: list[dict] | None = None
        self._rx_count = 0
        self._tx_count = 0
        self._rx_bytes = 0
        self._tx_bytes = 0
        self._start_time = 0.0

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

            if event.direction == "RX":
                self._rx_count += 1
                self._rx_bytes += len(event.raw)
            else:
                self._tx_count += 1
                self._tx_bytes += len(event.raw)

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

        self._monitor = SerialMonitor(
            ports=ports,
            profile=profile,
            auto_reply_rules=auto_reply_rules,
            event_callback=_event_callback,
        )
        self._monitor.start()
        self._running = True
        self._bridge.put({"event": "status", "ports": {cfg["port"]: "open" for cfg in ports}})

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
            "stats": {
                "rx_count": self._rx_count,
                "tx_count": self._tx_count,
                "rx_bytes": self._rx_bytes,
                "tx_bytes": self._tx_bytes,
                "bytes_per_sec": round((self._rx_bytes + self._tx_bytes) / elapsed, 1),
            },
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

class VofaStreamManager:
    """Manages VOFA+ JustFloat variable streaming via memory reads.

    Reads device RAM at specified addresses, interprets as floats,
    and streams the data via SSE for the VofaTab chart.
    """

    def __init__(self):
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

    @property
    def running(self) -> bool:
        return self._running and not self._stop_event.is_set()

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    def start(self, device, channels: list[dict], interval: float = 0.1) -> None:
        """Start VOFA polling.

        Args:
            device: Device instance with read_memory()
            channels: List of {name, addr (int or hex str), type?, size?}
            interval: Polling interval in seconds
        """
        if self._running:
            return

        import struct as _struct
        self._channels = []
        for ch in channels:
            addr = ch["addr"]
            if isinstance(addr, str):
                addr = int(addr, 0)
            ch_type = ch.get("type", "float")
            size = ch.get("size", 4)
            self._channels.append({
                "name": ch.get("name", f"0x{addr:08x}"),
                "addr": addr,
                "type": ch_type,
                "size": size,
            })

        self._interval = max(interval, 0.01)
        self._stop_event.clear()
        self._paused.set()
        self._running = True
        self._history.clear()

        _TYPE_UNPACK = {
            "float": ("<f", 4), "fp32": ("<f", 4),
            "int32_t": ("<i", 4), "int32": ("<i", 4),
            "uint32_t": ("<I", 4), "uint32": ("<I", 4),
            "int16_t": ("<h", 2), "int16": ("<h", 2),
            "uint16_t": ("<H", 2), "uint16": ("<H", 2),
            "int8_t": ("<b", 1), "uint8_t": ("<B", 1),
        }

        def _poll():
            try:
                while not self._stop_event.is_set():
                    if not self._paused.is_set():
                        time.sleep(self._interval)
                        continue

                    now = time.time()
                    point: dict[str, Any] = {"_t": now}

                    for ch in self._channels:
                        try:
                            raw = device.read_memory(ch["addr"], ch["size"])
                            fmt_info = _TYPE_UNPACK.get(ch["type"])
                            if fmt_info:
                                val = _struct.unpack(fmt_info[0], raw[:fmt_info[1]])[0]
                            else:
                                val = _struct.unpack("<f", raw[:4])[0]
                            if isinstance(val, float) and not math.isfinite(val):
                                val = 0.0
                            point[ch["name"]] = val
                        except Exception:
                            pass

                    if any(k != "_t" for k in point):
                        self._bridge.put({"event": "data", **point})
                        self._history.append(point)
                        if len(self._history) > self._max_history:
                            self._history = self._history[-self._max_history:]

                    self._stop_event.wait(self._interval)

            except Exception as e:
                logger.error("VOFA stream error: %s", e)
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

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def set_interval(self, interval: float) -> float:
        self._interval = max(0.0, min(60.0, interval))
        return self._interval

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "paused": self.paused,
            "channels": self._channels,
            "interval": self._interval,
            "clients": self._bridge.client_count,
            "history_size": len(self._history),
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

BRIDGE_DASHBOARD_TYPES = ("rtt", "superwatch", "vofa")


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
    return _managers


def stop_bridge_dashboards(exclude: str | None = None) -> list[str]:
    """停止所有使用 MKLink Bridge 的 Dashboard（RTT/SuperWatch/VOFA）。
    返回被停止的 Dashboard 名称列表。"""
    stopped = []
    managers = get_managers()
    for name in BRIDGE_DASHBOARD_TYPES:
        if name == exclude:
            continue
        mgr = managers.get(name)
        if mgr and mgr.running:
            mgr.stop()
            stopped.append(name)
    return stopped
