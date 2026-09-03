from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from mklink.hil_lock import HilFileLock, HilLockHeld, _exclusive_guard


def _write_holder(lock: HilFileLock, *, hostname: str, pid: int) -> None:
    lock.root.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps({
            "owner_id": "previous-owner",
            "pid": pid,
            "hostname": hostname,
            "acquired_at": time.time(),
            "lease_s": 3600,
            "expires_at": time.time() + 3600,
        }),
        encoding="utf-8",
    )


def test_acquire_reclaims_unexpired_lock_from_dead_same_host_pid(
    tmp_path, monkeypatch,
):
    lock = HilFileLock("transport_usb-serial_TEST", root=tmp_path)
    _write_holder(lock, hostname=socket.gethostname(), pid=12345)
    monkeypatch.setattr("mklink.local_resources._pid_exists", lambda _pid: False)

    lock.acquire()

    holder = json.loads(lock.path.read_text(encoding="utf-8"))
    assert holder["owner_id"] == lock.owner_id
    assert lock.release() is True


@pytest.mark.parametrize(
    ("hostname", "owner_alive"),
    [(socket.gethostname(), True), ("another-host", False)],
)
def test_acquire_keeps_unexpired_lock_when_owner_may_still_be_valid(
    tmp_path, monkeypatch, hostname, owner_alive,
):
    lock = HilFileLock("transport_usb-serial_TEST", root=tmp_path)
    _write_holder(lock, hostname=hostname, pid=12345)
    monkeypatch.setattr(
        "mklink.local_resources._pid_exists", lambda _pid: owner_alive,
    )

    with pytest.raises(HilLockHeld):
        lock.acquire()


def test_exclusive_guard_serializes_metadata_updates(tmp_path):
    guard_path = tmp_path / "transport.guard"
    attempted = threading.Event()
    entered = threading.Event()

    def contender():
        attempted.set()
        with _exclusive_guard(guard_path):
            entered.set()

    with _exclusive_guard(guard_path):
        thread = threading.Thread(target=contender)
        thread.start()
        assert attempted.wait(1)
        assert not entered.wait(0.1)

    assert entered.wait(1)
    thread.join(timeout=1)
    assert not thread.is_alive()
