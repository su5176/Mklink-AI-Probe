"""Desktop sidecar port and ownership regression tests."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from mklink.remote.api import (
    _bind_desktop_server_socket,
    _write_desktop_runtime_info,
    create_app,
)


def _blocking_listener() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener, listener.getsockname()[1]


def test_desktop_backend_skips_an_occupied_preferred_port():
    blocker, occupied_port = _blocking_listener()
    selected = None
    listener = None
    try:
        if occupied_port > 65525:
            pytest.skip("ephemeral port is too close to the range limit")
        listener, selected = _bind_desktop_server_socket(
            "127.0.0.1", occupied_port, occupied_port + 10,
        )
        assert occupied_port < selected <= occupied_port + 10
    finally:
        if listener is not None:
            listener.close()
        blocker.close()


def test_desktop_backend_reports_a_full_port_range():
    blocker, occupied_port = _blocking_listener()
    try:
        with pytest.raises(OSError, match="no available desktop backend port"):
            _bind_desktop_server_socket(
                "127.0.0.1", occupied_port, occupied_port,
            )
    finally:
        blocker.close()


def test_desktop_runtime_info_is_published_atomically(tmp_path):
    destination = tmp_path / "runtime.json"
    _write_desktop_runtime_info(
        str(destination), port=8766, instance_id="instance-b",
    )
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "port": 8766,
        "instanceId": "instance-b",
    }
    assert list(tmp_path.iterdir()) == [destination]


def test_health_identifies_the_owning_desktop_instance():
    app = create_app(
        auth_token=None,
        project_root=".",
        desktop_instance_id="instance-b",
    )
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["desktop_instance_id"] == "instance-b"


def _wait_for_json(path, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.05)
    raise AssertionError(f"runtime info was not published: {path}")


def _wait_for_health(port: int, instance_id: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=0.5,
            ) as response:
                payload = json.load(response)
            if payload.get("desktop_instance_id") == instance_id:
                return payload
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"desktop backend {instance_id} did not become healthy")


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_two_desktop_sidecars_get_independent_endpoints(tmp_path):
    probe, preferred_port = _blocking_listener()
    probe.close()
    if preferred_port > 65525:
        pytest.skip("ephemeral port is too close to the range limit")

    processes = []
    endpoints = []
    try:
        for index in range(2):
            instance_id = f"desktop-instance-{index}"
            runtime_info = tmp_path / f"runtime-{index}.json"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mklink",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(preferred_port),
                    "--desktop-port-end",
                    str(preferred_port + 10),
                    "--desktop-runtime-info",
                    str(runtime_info),
                    "--desktop-instance-id",
                    instance_id,
                    "--project-root",
                    str(tmp_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            endpoint = _wait_for_json(runtime_info)
            _wait_for_health(endpoint["port"], instance_id)
            endpoints.append(endpoint)

        assert endpoints[0]["port"] != endpoints[1]["port"]
        _stop_process(processes[0])
        _wait_for_health(endpoints[1]["port"], "desktop-instance-1")
    finally:
        for process in reversed(processes):
            _stop_process(process)
