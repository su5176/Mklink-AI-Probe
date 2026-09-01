from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from mklink.modbus._session import (
    ModbusWorker,
    frame_crc_ok,
    modbus_crc16,
    rtu_frame_length,
    validate_transaction,
)
from mklink.remote.api import create_app
from mklink.remote.dashboards import get_managers


class _RecordingClient:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.thread_ids: set[int] = set()
        self.lock = threading.Lock()

    def read_holding_registers(self, address, count, slave):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.thread_ids.add(threading.get_ident())
        time.sleep(0.005)
        with self.lock:
            self.active -= 1
        return list(range(address, address + count))


def test_worker_serializes_concurrent_callers_on_one_io_thread():
    client = _RecordingClient()
    worker = ModbusWorker(client, slave=1)
    worker.start()
    results: list[list[int | bool]] = []

    threads = [
        threading.Thread(
            target=lambda start=start: results.append(
                worker.execute(3, start, quantity=2)
            )
        )
        for start in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    worker.stop()

    assert len(results) == 8
    assert client.max_active == 1
    assert len(client.thread_ids) == 1


@pytest.mark.parametrize(
    ("fc", "quantity", "values", "message"),
    [
        (3, 126, None, "1..125"),
        (1, 2001, None, "1..2000"),
        (6, None, [1, 2], "exactly one"),
        (16, None, [0] * 124, "at most 123"),
    ],
)
def test_transaction_limits(fc, quantity, values, message):
    with pytest.raises(ValueError, match=message):
        validate_transaction(fc, 0, quantity=quantity, values=values)


def test_modbus_crc_matches_standard_read_request():
    payload = bytes.fromhex("01 03 00 00 00 0A")
    crc = modbus_crc16(payload)
    frame = payload + bytes((crc & 0xFF, crc >> 8))

    assert frame.hex(" ").upper() == "01 03 00 00 00 0A C5 CD"
    assert frame_crc_ok(frame) is True
    assert frame_crc_ok(frame[:-1] + b"\x00") is False


def test_rtu_frame_length_distinguishes_partial_read_response():
    request = bytes.fromhex("01 04 00 00 00 10 F1 C6")
    partial = bytes.fromhex("01 04 20 00 02 00 00")

    assert rtu_frame_length(True, request) == 8
    assert rtu_frame_length(False, partial) == 37
    assert len(partial) < rtu_frame_length(False, partial)


def test_workbench_api_runs_transactions_and_finite_loop(monkeypatch, tmp_path):
    class FakeModbusClient:
        def __init__(self, **kwargs):
            self.trace_packet = kwargs.get("trace_packet")

        def open(self):
            return True

        def close(self):
            pass

        def read_input_registers(self, address, count, slave):
            return list(range(address, address + count))

    manager = get_managers()["modbus"]
    if manager.running:
        manager.stop()
    monkeypatch.setattr("mklink.modbus._client.ModbusClient", FakeModbusClient)
    app = create_app(auth_token=None, project_root=str(tmp_path))

    with TestClient(app) as client:
        started = client.post(
            "/api/dash/modbus/start",
            json={
                "port": "TEST",
                "slave": 1,
                "baudrate": 115200,
                "registers": [],
                "retries": 0,
            },
        )
        assert started.status_code == 200

        response = client.post(
            "/api/dash/modbus/transaction",
            json={"fc": 4, "start": 10, "quantity": 3},
        )
        assert response.status_code == 200
        assert response.json()["values"] == [10, 11, 12]

        invalid = client.post(
            "/api/dash/modbus/transaction",
            json={"fc": 4, "start": 0, "quantity": 126},
        )
        assert invalid.status_code == 400

        loop = client.post(
            "/api/dash/modbus/loop/start",
            json={
                "fc": 4,
                "start": 0,
                "quantity": 1,
                "interval": 0.02,
                "count": 3,
            },
        )
        assert loop.status_code == 200
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = client.get("/api/dash/modbus/status").json()["loop"]
            if not status["running"]:
                break
            time.sleep(0.01)
        assert status["completed"] == 3
        assert status["errors"] == 0

        stopped = client.post("/api/dash/modbus/stop")
        assert stopped.status_code == 200
