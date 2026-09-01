from __future__ import annotations

import asyncio
import base64
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mklink.local_resources import local_resource_status, serial_lock_path
from mklink.remote import dashboards as dashboard_module
from mklink.remote.api import create_app
from mklink.remote.dashboards import SerialStreamManager
from mklink.remote.stream_protocol import SERIAL_RX_BYTES, SERIAL_TX_BYTES, StreamType
from mklink.serial import _monitor as monitor_module
from mklink.serial._monitor import SerialEvent, SerialMonitor
from mklink.serial._port import _PortLock


class _RecordingHub:
    def __init__(self):
        self.batches = []

    def publish(self, payload, *, item_count, flags=0, stream_type=None):
        self.batches.append((bytes(payload), item_count, flags, stream_type))
        return len(self.batches)

    def stats(self):
        return type("Stats", (), {})()


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not met before timeout")


def test_serial_port_lock_releases_owner_and_can_be_reacquired(monkeypatch, tmp_path):
    monkeypatch.setenv("TEMP", str(tmp_path))

    for _ in range(2):
        lock = _PortLock("COM6")
        assert lock._path.endswith("serial_COM6.lock")
        assert lock._path == serial_lock_path("com6")
        assert lock.acquire() is True
        lock.release()

        status = local_resource_status("COM6")["serial_locks"][0]
        assert status["owner_pid"] == 0
        assert status["owner_alive"] is False


def test_serial_port_lock_file_open_error_is_reported_as_unavailable(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TEMP", str(tmp_path))
    lock = _PortLock("COM6")

    def fail_open(*_args, **_kwargs):
        raise OSError("lock path unavailable")

    monkeypatch.setattr("builtins.open", fail_open)
    assert lock.acquire() is False


def test_modbus_start_reports_busy_serial_port(monkeypatch, tmp_path):
    class BusyModbusClient:
        def __init__(self, **_kwargs):
            pass

        def open(self):
            return False

    monkeypatch.setattr("mklink.modbus._client.ModbusClient", BusyModbusClient)
    app = create_app(auth_token=None, project_root=str(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/dash/modbus/start", json={"port": "BUSY_PORT"})

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "conflict": "serial port BUSY_PORT is busy or unavailable",
            "resource": "modbus_port",
        },
    }


def test_serial_monitor_emits_partial_rx_chunk_before_line_event(monkeypatch):
    chunk_ready = threading.Event()
    chunks = []
    events = []

    class FakeSerialPort:
        def __init__(self, **_kwargs):
            self.is_open = False
            self._read = False

        def open(self):
            self.is_open = True
            return True

        def close(self):
            self.is_open = False

        def read_available(self):
            if self._read:
                return b""
            self._read = True
            return b"prompt> "

    monkeypatch.setattr(monitor_module, "SerialPort", FakeSerialPort)
    monitor = SerialMonitor(
        ports=[{"port": "TEST"}],
        event_callback=events.append,
        chunk_callback=lambda port, direction, data, timestamp: (
            chunks.append((port, direction, data, timestamp)),
            chunk_ready.set(),
        ),
    )

    monitor.start()
    assert chunk_ready.wait(1.0)
    monitor.stop()

    assert len(chunks) == 1
    assert chunks[0][:3] == ("TEST", "RX", b"prompt> ")
    assert events == []


def test_serial_monitor_exclusively_queues_protocol_rx_and_rejects_normal_send(
    monkeypatch,
):
    protocol_started = threading.Event()
    release_protocol = threading.Event()
    writes = []

    class FakeSerialPort:
        is_open = True

        def write(self, payload):
            writes.append(bytes(payload))

    class BlockingSender:
        def __init__(self, read, write, **_kwargs):
            self.read = read
            self.write = write

        def send(self, _stream, _filename, _size):
            protocol_started.set()
            assert self.read(0.5) == b"C"
            assert release_protocol.wait(1.0)
            self.write(b"PACKET")

    monkeypatch.setattr("mklink.serial._ymodem.YModemSender", BlockingSender)
    monitor = SerialMonitor(ports=[{"port": "TEST"}])
    monitor._serial_ports["TEST"] = FakeSerialPort()
    worker = threading.Thread(
        target=monitor.send_ymodem,
        args=("TEST", b"firmware", "app.bin"),
    )
    worker.start()
    assert protocol_started.wait(1.0)

    protocol_queue = monitor._protocol_queues["TEST"]
    protocol_queue.put(b"C")
    assert monitor.send("TEST", b"ordinary input") is False
    assert writes == []

    release_protocol.set()
    worker.join(1.0)
    assert not worker.is_alive()
    assert "TEST" not in monitor._protocol_queues
    assert writes == [b"PACKET"]
    assert monitor.send("TEST", b"ordinary input") is True
    assert writes[-1] == b"ordinary input"


def test_serial_monitor_does_not_start_protocol_during_an_ordinary_write(
    monkeypatch,
):
    ordinary_write_started = threading.Event()
    release_ordinary_write = threading.Event()
    protocol_started = threading.Event()
    writes = []

    class BlockingSerialPort:
        is_open = True

        def write(self, payload):
            payload = bytes(payload)
            if payload == b"ordinary":
                ordinary_write_started.set()
                assert release_ordinary_write.wait(1.0)
            writes.append(payload)

    class RecordingSender:
        def __init__(self, _read, write, **_kwargs):
            self.write = write

        def send(self, _stream, _filename, _size):
            protocol_started.set()
            self.write(b"protocol")

    monkeypatch.setattr("mklink.serial._ymodem.YModemSender", RecordingSender)
    monitor = SerialMonitor(ports=[{"port": "TEST"}])
    monitor._serial_ports["TEST"] = BlockingSerialPort()
    ordinary = threading.Thread(target=monitor.send, args=("TEST", b"ordinary"))
    protocol = threading.Thread(
        target=monitor.send_ymodem,
        args=("TEST", b"firmware", "app.bin"),
    )

    ordinary.start()
    assert ordinary_write_started.wait(1.0)
    protocol.start()
    assert not protocol_started.wait(0.05)
    release_ordinary_write.set()
    ordinary.join(1.0)
    protocol.join(1.0)

    assert not ordinary.is_alive()
    assert not protocol.is_alive()
    assert writes == [b"ordinary", b"protocol"]


def test_serial_monitor_routes_protocol_rx_away_from_line_events(monkeypatch):
    allow_rx = threading.Event()
    protocol_received = threading.Event()
    release_protocol = threading.Event()
    chunks = []
    events = []

    class FakeSerialPort:
        def __init__(self, **_kwargs):
            self.is_open = False
            self.emitted = False

        def open(self):
            self.is_open = True
            return True

        def close(self):
            self.is_open = False

        def read_available(self):
            if not allow_rx.is_set() or self.emitted:
                return b""
            self.emitted = True
            return b"C\n"

        def write(self, _payload):
            pass

    class ReceivingSender:
        def __init__(self, read, _write, **_kwargs):
            self.read = read

        def send(self, _stream, _filename, _size):
            allow_rx.set()
            assert self.read(1.0) == b"C\n"
            protocol_received.set()
            assert release_protocol.wait(1.0)

    monkeypatch.setattr(monitor_module, "SerialPort", FakeSerialPort)
    monkeypatch.setattr("mklink.serial._ymodem.YModemSender", ReceivingSender)
    monitor = SerialMonitor(
        ports=[{"port": "TEST"}],
        event_callback=events.append,
        chunk_callback=lambda port, direction, data, _timestamp: chunks.append(
            (port, direction, data)
        ),
    )
    monitor.start()
    _wait_until(lambda: monitor.port_status["TEST"] == "open")
    worker = threading.Thread(
        target=monitor.send_ymodem,
        args=("TEST", b"firmware", "app.bin"),
    )
    worker.start()
    assert protocol_received.wait(1.0)
    assert chunks == []
    assert events == []

    release_protocol.set()
    worker.join(1.0)
    monitor.stop()
    assert not worker.is_alive()


def test_serial_monitor_returns_final_ymodem_chunk_banner_to_terminal(monkeypatch):
    from collections import deque

    responses = deque()
    response_lock = threading.Lock()
    writes = []
    events = []
    chunks = []
    banner_seen = threading.Event()
    monitor = None

    class FakeSerialPort:
        def __init__(self, **_kwargs):
            self.is_open = False
            self.initial_request_sent = False
            self.eot_count = 0

        def open(self):
            self.is_open = True
            return True

        def close(self):
            self.is_open = False

        def read_available(self):
            if (
                not self.initial_request_sent
                and monitor is not None
                and "TEST" in monitor._protocol_queues
            ):
                self.initial_request_sent = True
                return b"C"
            with response_lock:
                return responses.popleft() if responses else b""

        def write(self, payload):
            payload = bytes(payload)
            writes.append(payload)
            if payload == bytes((0x04,)):
                self.eot_count += 1
                response = b"\x15" if self.eot_count == 1 else b"\x06C"
            elif payload[0] == 0x01 and not any(payload[3:-2]):
                # The target's last ACK and reboot text arrive in one OS read.
                response = b"\x06Cboot ready\r\n"
            elif payload[0] == 0x01:
                response = b"\x06C"
            else:
                response = b"\x06"
            with response_lock:
                responses.append(response)

    monkeypatch.setattr(monitor_module, "SerialPort", FakeSerialPort)
    monitor = SerialMonitor(
        ports=[{"port": "TEST"}],
        event_callback=events.append,
        chunk_callback=lambda port, direction, data, _timestamp: (
            chunks.append((port, direction, data)),
            banner_seen.set() if data == b"boot ready\r\n" else None,
        ),
    )
    monitor.start()
    _wait_until(lambda: monitor.port_status["TEST"] == "open")

    worker = threading.Thread(
        target=monitor.send_ymodem,
        args=("TEST", b"x", "app.bin"),
    )
    worker.start()
    worker.join(2.0)
    assert not worker.is_alive()
    assert banner_seen.wait(1.0)
    monitor.stop()

    assert chunks == [("TEST", "RX", b"boot ready\r\n")]
    assert [event.raw for event in events] == [b"boot ready\r\n"]
    assert all(chunk[2] not in (b"\x06", b"C", b"\x06C") for chunk in chunks)
    assert len(writes) == 5  # header, data, EOT x2, final empty header


def test_serial_stream_manager_publishes_exact_chunks_and_counts_bytes(monkeypatch):
    class FakeMonitor:
        def __init__(self, **kwargs):
            self.event_callback = kwargs["event_callback"]
            self.chunk_callback = kwargs["chunk_callback"]
            self.port_status = {"TEST": "open"}

        def start(self):
            pass

        def stop(self):
            pass

        def send(self, _port, _data):
            return True

        def send_all(self, _data):
            pass

    monkeypatch.setattr(monitor_module, "SerialMonitor", FakeMonitor)
    manager = SerialStreamManager()
    queue = manager._bridge.add_client()
    config = [{"port": "TEST", "baudrate": 115200}]
    manager.start(config)
    monitor = manager._monitor

    raw = b"\x1b[31mready> \xff"
    monitor.chunk_callback("TEST", "RX", raw, 123.5)
    monitor.event_callback(SerialEvent(123.5, "TEST", "RX", raw))

    opening = queue.get_nowait()
    terminal = queue.get_nowait()
    log_event = queue.get_nowait()
    assert opening["event"] == "status"
    assert terminal == {
        "event": "terminal",
        "timestamp": 123.5,
        "port": "TEST",
        "direction": "RX",
        "data_base64": base64.b64encode(raw).decode("ascii"),
    }
    assert log_event["event"] == "data"
    assert manager.get_status()["config"] == config
    assert manager.get_status()["stats"] == {
        "rx_count": 1,
        "tx_count": 0,
        "rx_bytes": len(raw),
        "tx_bytes": 0,
        "bytes_per_sec": float(len(raw)),
    }
    manager.stop()
    manager._bridge.remove_client(queue)


class _YModemMonitor:
    mode = "complete"
    entered = threading.Event()

    def __init__(self, **kwargs):
        self.port_status = {"TEST": "open"}
        self.protocol_callback = kwargs["protocol_callback"]

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, _port, _data):
        return True

    def send_all(self, _data):
        pass

    def send_ymodem(
        self,
        _port,
        data,
        _filename,
        *,
        cancel_event,
        progress_callback,
    ):
        type(self).entered.set()
        self.protocol_callback("TEST", "RX", b"C", 100.0)
        self.protocol_callback("TEST", "TX", b"PACKET", 100.1)
        progress_callback(SimpleNamespace(
            phase="transferring",
            sent_bytes=min(1024, len(data)),
            total_bytes=len(data),
            percent=min(50, 100),
            block=1,
            retries=0,
        ))
        if type(self).mode == "fail":
            raise RuntimeError("synthetic transfer failure")
        if type(self).mode == "late-progress":
            assert cancel_event.wait(1.0)
            progress_callback(SimpleNamespace(
                phase="transferring",
                sent_bytes=len(data),
                total_bytes=len(data),
                percent=100,
                block=2,
                retries=0,
            ))
            from mklink.serial._ymodem import YModemCancelled

            raise YModemCancelled("synthetic cancellation")
        if type(self).mode == "cancel":
            assert cancel_event.wait(1.0)
            from mklink.serial._ymodem import YModemCancelled

            raise YModemCancelled("synthetic cancellation")


def _ymodem_manager(monkeypatch, mode):
    _YModemMonitor.mode = mode
    _YModemMonitor.entered = threading.Event()
    monkeypatch.setattr(monitor_module, "SerialMonitor", _YModemMonitor)
    manager = SerialStreamManager()
    manager.start([{"port": "TEST", "baudrate": 115200}])
    return manager


def test_serial_stream_manager_tracks_ymodem_progress_and_completion(monkeypatch):
    manager = _ymodem_manager(monkeypatch, "complete")
    initial = manager.start_ymodem("TEST", b"firmware payload", "app.bin")
    assert initial == {
        "transfer_id": 1,
        "state": "running",
        "active": True,
        "phase": "waiting",
        "port": "TEST",
        "filename": "app.bin",
        "sent_bytes": 0,
        "total_bytes": len(b"firmware payload"),
        "percent": 0,
        "block": 0,
        "retries": 0,
        "error": "",
    }
    _wait_until(lambda: manager.get_ymodem_status()["state"] == "completed")
    completed = manager.get_ymodem_status()
    assert completed["active"] is False
    assert completed["phase"] == "completed"
    assert completed["sent_bytes"] == len(b"firmware payload")
    assert completed["percent"] == 100
    assert manager.send("TEST", b"next command") is True
    manager.stop()


def test_serial_stream_manager_pages_raw_ymodem_protocol_trace(monkeypatch):
    manager = _ymodem_manager(monkeypatch, "complete")
    manager.start_ymodem("TEST", b"firmware", "app.bin")
    _wait_until(lambda: manager.get_ymodem_status()["state"] == "completed")

    first = manager.get_ymodem_trace(after=0, limit=1)
    assert first == {
        "transfer_id": 1,
        "entries": [{
            "seq": 1,
            "transfer_id": 1,
            "timestamp": 100.0,
            "port": "TEST",
            "direction": "RX",
            "size": 1,
            "hex": "43",
        }],
        "next_seq": 1,
        "dropped": 0,
    }
    second = manager.get_ymodem_trace(after=first["next_seq"])
    assert [entry["direction"] for entry in second["entries"]] == ["TX"]
    assert second["entries"][0]["hex"] == "50 41 43 4B 45 54"
    manager.stop()


def test_serial_stream_manager_publishes_running_before_fast_ymodem_completion(
    monkeypatch,
):
    manager = _ymodem_manager(monkeypatch, "complete")
    queue = manager._bridge.add_client()
    manager.start_ymodem("TEST", b"firmware", "app.bin")
    _wait_until(lambda: manager.get_ymodem_status()["state"] == "completed")

    first = queue.get_nowait()
    second = queue.get_nowait()
    assert (first["state"], first["active"], first["phase"]) == (
        "running", True, "waiting",
    )
    assert second["event"] == "ymodem"
    assert second["phase"] in {"transferring", "completed"}
    events = [second]
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events[-1]["state"] == "completed"
    assert events[-1]["active"] is False
    manager._bridge.remove_client(queue)
    manager.stop()


def test_serial_stream_manager_resets_finished_ymodem_state_on_new_session(
    monkeypatch,
):
    manager = _ymodem_manager(monkeypatch, "complete")
    manager.start_ymodem("TEST", b"firmware", "app.bin")
    _wait_until(lambda: manager.get_ymodem_status()["state"] == "completed")
    manager.stop()

    manager.start([{"port": "TEST", "baudrate": 115200}])
    assert manager.get_ymodem_status() == manager._idle_ymodem_status()
    manager.stop()


def test_serial_stream_manager_rejects_send_and_cancels_active_ymodem(monkeypatch):
    manager = _ymodem_manager(monkeypatch, "cancel")
    manager.start_ymodem("TEST", b"firmware", "app.bin")
    assert _YModemMonitor.entered.wait(1.0)
    assert manager.send("TEST", b"must not interleave") is False

    cancelling = manager.cancel_ymodem(wait=True)
    assert cancelling["state"] == "cancelled"
    assert cancelling["active"] is False
    assert cancelling["phase"] == "cancelled"
    assert cancelling["error"] == "synthetic cancellation"
    assert manager.send("TEST", b"safe after cancellation") is True
    manager.stop()


def test_serial_stream_manager_does_not_publish_late_progress_after_cancel(
    monkeypatch,
):
    manager = _ymodem_manager(monkeypatch, "late-progress")
    queue = manager._bridge.add_client()
    manager.start_ymodem("TEST", b"firmware", "app.bin")
    assert _YModemMonitor.entered.wait(1.0)
    manager.cancel_ymodem(wait=True)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    cancelling_index = next(
        index for index, event in enumerate(events)
        if event.get("phase") == "cancelling"
    )
    assert not any(
        event.get("phase") == "transferring"
        for event in events[cancelling_index + 1:]
    )
    assert events[-1]["state"] == "cancelled"
    manager._bridge.remove_client(queue)
    manager.stop()


def test_serial_stream_manager_records_ymodem_failure(monkeypatch):
    manager = _ymodem_manager(monkeypatch, "fail")
    manager.start_ymodem("TEST", b"firmware", "app.bin")
    _wait_until(lambda: manager.get_ymodem_status()["state"] == "failed")
    failed = manager.get_ymodem_status()
    assert failed["active"] is False
    assert failed["phase"] == "failed"
    assert failed["error"] == "synthetic transfer failure"
    manager.stop()


def test_serial_stream_manager_stop_retains_lifecycle_while_worker_is_alive(
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()

    class StuckMonitor:
        def __init__(self, **_kwargs):
            self.port_status = {"TEST": "open"}

        def start(self):
            pass

        def stop(self):
            pass

        def send_ymodem(self, *_args, **_kwargs):
            entered.set()
            release.wait(1.0)

    monkeypatch.setattr(monitor_module, "SerialMonitor", StuckMonitor)
    monkeypatch.setattr(dashboard_module, "_YMODEM_STOP_TIMEOUT", 0.01)
    manager = SerialStreamManager()
    manager.start([{"port": "TEST", "baudrate": 115200}])
    manager.start_ymodem("TEST", b"firmware", "app.bin")
    assert entered.wait(1.0)

    with pytest.raises(TimeoutError, match="shutdown timeout"):
        manager.stop()
    assert manager.running is True
    assert manager._monitor is not None
    assert manager.get_ymodem_status()["active"] is True

    release.set()
    _wait_until(lambda: manager.get_ymodem_status()["active"] is False)
    manager.stop()
    assert manager.running is False
    assert manager._monitor is None


def test_serial_stream_manager_stop_cannot_clear_monitor_during_worker_start(
    monkeypatch,
):
    real_thread = threading.Thread
    worker_start_entered = threading.Event()
    release_worker_start = threading.Event()
    stop_returned = threading.Event()

    class DelayedStartThread(real_thread):
        def start(self):
            worker_start_entered.set()
            assert release_worker_start.wait(1.0)
            super().start()

    manager = _ymodem_manager(monkeypatch, "complete")
    monkeypatch.setattr(dashboard_module.threading, "Thread", DelayedStartThread)
    start_call = real_thread(
        target=manager.start_ymodem,
        args=("TEST", b"firmware", "app.bin"),
    )
    stop_call = real_thread(target=lambda: (manager.stop(), stop_returned.set()))

    start_call.start()
    assert worker_start_entered.wait(1.0)
    stop_call.start()
    assert not stop_returned.wait(0.05)
    release_worker_start.set()
    start_call.join(1.0)
    stop_call.join(1.0)

    assert not start_call.is_alive()
    assert not stop_call.is_alive()
    assert stop_returned.is_set()
    assert manager.running is False
    assert manager._monitor is None
    assert manager.get_ymodem_status()["active"] is False


def test_serial_ymodem_api_enforces_upload_boundaries_and_send_lock(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(dashboard_module, "_managers", {})
    app = create_app(auth_token=None, project_root=str(tmp_path))
    manager = app.state.mklink_state["dashboard_managers"]["serial"]
    monkeypatch.setattr(manager, "_running", True)
    monkeypatch.setattr("mklink.remote.api._YMODEM_UPLOAD_LIMIT", 8)
    starts = []
    active = {"value": False}

    def start_ymodem(port, content, filename):
        starts.append((port, content, filename))
        return {
            "transfer_id": 1,
            "state": "running",
            "active": True,
            "phase": "waiting",
        }

    monkeypatch.setattr(manager, "start_ymodem", start_ymodem)
    monkeypatch.setattr(manager, "get_ymodem_status", lambda: {
        "transfer_id": 1,
        "state": "running",
        "active": active["value"],
        "phase": "waiting",
    })

    with TestClient(app) as client:
        empty = client.post(
            "/api/dash/serial/ymodem/start?port=TEST",
            files={"file": ("app.bin", b"", "application/octet-stream")},
        )
        oversized = client.post(
            "/api/dash/serial/ymodem/start?port=TEST",
            files={"file": ("app.bin", b"123456789", "application/octet-stream")},
        )
        long_name = client.post(
            "/api/dash/serial/ymodem/start?port=TEST",
            files={"file": ("\u6d4b" * 11 + ".bin", b"x", "application/octet-stream")},
        )
        boundary = b"ymodem-test-boundary"
        control_name = client.post(
            "/api/dash/serial/ymodem/start?port=TEST",
            content=(
                b"--" + boundary + b"\r\n"
                b'Content-Disposition: form-data; name="file"; '
                b'filename="bad\x00name.bin"\r\n'
                b"Content-Type: application/octet-stream\r\n\r\n"
                b"x\r\n--" + boundary + b"--\r\n"
            ),
            headers={"Content-Type": "multipart/form-data; boundary=ymodem-test-boundary"},
        )
        accepted = client.post(
            "/api/dash/serial/ymodem/start?port=TEST",
            files={"file": ("folder/app.bin", b"firmware", "application/octet-stream")},
        )
        active["value"] = True
        duplicate = client.post(
            "/api/dash/serial/ymodem/start?port=TEST",
            files={"file": ("app.bin", b"firmware", "application/octet-stream")},
        )
        locked_send = client.post(
            "/api/dash/serial/send",
            json={"port": "TEST", "data": "boot\\r", "hex": False},
        )
        active["value"] = False

        def race_send(_port, _data):
            active["value"] = True
            return False

        monkeypatch.setattr(manager, "send", race_send)
        raced_send = client.post(
            "/api/dash/serial/send",
            json={"port": "TEST", "data": "boot\\r", "hex": False},
        )

    assert empty.status_code == 400
    assert empty.json()["detail"] == "YMODEM file is empty"
    assert oversized.status_code == 413
    assert oversized.json()["detail"] == "YMODEM file exceeds the 32 MiB upload limit"
    assert long_name.status_code == 400
    assert long_name.json()["detail"] == "YMODEM filename exceeds the safe 31-byte limit"
    assert control_name.status_code == 400
    assert control_name.json()["detail"] == "YMODEM filename contains control characters"
    assert accepted.status_code == 200
    assert starts == [("TEST", b"firmware", "app.bin")]
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "a YMODEM transfer is already active"
    assert locked_send.status_code == 409
    assert locked_send.json()["detail"] == (
        "Serial input is locked by an active YMODEM transfer"
    )
    assert raced_send.status_code == 409
    assert raced_send.json()["detail"] == (
        "Serial input is locked by an active YMODEM transfer"
    )


def test_serial_binary_stream_skips_legacy_formatting_without_sse_clients(monkeypatch):
    class FakeMonitor:
        def __init__(self, **kwargs):
            self.event_callback = kwargs["event_callback"]
            self.chunk_callback = kwargs["chunk_callback"]
            self.port_status = {"TEST": "open"}

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(monitor_module, "SerialMonitor", FakeMonitor)
    monkeypatch.setattr(
        "mklink.remote.dashboards.base64.b64encode",
        lambda _data: (_ for _ in ()).throw(AssertionError("legacy Base64 formatting ran")),
    )
    hub = _RecordingHub()
    manager = SerialStreamManager(stream_hub=hub)
    manager.start([{"port": "TEST", "baudrate": 115200}])
    monitor = manager._monitor

    rx = b"\x00\x7f\x80\xff"
    tx = b"AT\r\n"
    monitor.chunk_callback("TEST", "RX", rx, 1.0)
    monitor.event_callback(SerialEvent(1.0, "TEST", "RX", rx))
    monitor.chunk_callback("TEST", "TX", tx, 2.0)
    monitor.event_callback(SerialEvent(2.0, "TEST", "TX", tx))

    assert hub.batches == [
        (rx, len(rx), SERIAL_RX_BYTES, StreamType.SERIAL),
        (tx, len(tx), SERIAL_TX_BYTES, StreamType.SERIAL),
    ]
    assert manager.get_status()["stats"] == {
        "rx_count": 1,
        "tx_count": 1,
        "rx_bytes": len(rx),
        "tx_bytes": len(tx),
        "bytes_per_sec": float(len(rx) + len(tx)),
    }
    manager.stop()


def test_serial_sse_reconnect_starts_with_current_status():
    async def first_event():
        manager = SerialStreamManager()
        generator = manager.sse_generator()
        try:
            return await generator.__anext__()
        finally:
            await generator.aclose()

    payload = asyncio.run(first_event())
    assert '"event": "status"' in payload
    assert '"running": false' in payload
