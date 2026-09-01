import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ElementTree
from zipfile import ZipFile

import pytest

import mklink.cmsis_dap.pack_manager as pack_manager_module
import mklink.cmsis_dap.pack_worker as pack_worker_module
from mklink.cmsis_dap.errors import FlashError, FlashErrorCode
from mklink.cmsis_dap.pack_catalog import PackCatalog
from mklink.cmsis_dap.pack_manager import PackManager, SubprocessPackWorker
from mklink.cmsis_dap.pack_worker import ReportingCache, handle_request, run_protocol
from mklink.cmsis_dap.paths import PackPaths


class FakeWorker:
    def __init__(self):
        self.commands = []

    def run(self, command, payload, on_event):
        self.commands.append((command, payload))
        on_event({"type": "progress", "current": 1, "total": 2})
        return {
            "status": "installed",
            "pack_id": "GigaDevice.GD32F30x_DFP",
            "version": "3.0.2",
        }


def _pack_path(paths, vendor, pack, version):
    return pack_manager_module._canonical_pack_path(paths, vendor, pack, version)


def _transaction_result(path):
    return {
        "status": "installed",
        "pack_id": "Test.Pack",
        "version": "1.0.0",
        "pack_path": str(Path(path).resolve()),
    }


def test_install_downloads_selected_part_only(tmp_path):
    worker = FakeWorker()
    manager = PackManager(root=tmp_path, worker=worker)

    result = manager.install("GD32F303RC", lambda event: None)

    assert worker.commands == [("install", {"part_number": "GD32F303RC"})]
    assert result == {
        "status": "installed",
        "pack_id": "GigaDevice.GD32F30x_DFP",
        "version": "3.0.2",
    }


def test_update_index_uses_cancellable_worker_boundary(tmp_path):
    class IndexWorker(FakeWorker):
        def run(self, command, payload, on_event):
            self.commands.append((command, payload))
            on_event({"type": "progress", "current": 1, "total": 1})
            return {"status": "updated", "target_count": 2}

    worker = IndexWorker()
    events = []

    result = PackManager(tmp_path, worker=worker).update_index(events.append)

    assert worker.commands == [("update-index", {})]
    assert events == [{"type": "progress", "current": 1, "total": 1}]
    assert result == {"status": "updated", "target_count": 2}


def test_independent_managers_serialize_fixed_transaction_files(tmp_path):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    errors = []

    class TransactionWorker:
        def __init__(self, entered, release=None):
            self.entered = entered
            self.release = release

        def run(self, command, payload, on_event):
            temporary = tmp_path / "pack-transaction.json.tmp"
            if temporary.exists():
                raise RuntimeError("fixed transaction file collision")
            temporary.write_text(command, encoding="utf-8")
            self.entered.set()
            try:
                if self.release is not None:
                    assert self.release.wait(2)
                return {"status": "updated", "target_count": 1}
            finally:
                temporary.unlink()

    first = PackManager(
        tmp_path, worker=TransactionWorker(first_entered, release_first)
    )
    second = PackManager(tmp_path, worker=TransactionWorker(second_entered))

    def update(manager):
        try:
            manager.update_index(lambda _event: None)
        except BaseException as error:
            errors.append(error)

    first_thread = threading.Thread(target=update, args=(first,))
    second_thread = threading.Thread(target=update, args=(second,))
    first_thread.start()
    assert first_entered.wait(1)
    second_thread.start()
    assert not second_entered.wait(0.1)
    release_first.set()
    first_thread.join(2)
    second_thread.join(2)

    assert errors == []
    assert second_entered.is_set()
    assert not (tmp_path / "pack-transaction.json.tmp").exists()


def test_waiting_pack_manager_can_be_cancelled(tmp_path):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    errors = []

    class BlockingWorker:
        def __init__(self, entered, release=None):
            self.entered = entered
            self.release = release

        def run(self, command, payload, on_event):
            self.entered.set()
            if self.release is not None:
                assert self.release.wait(2)
            return {"status": "updated", "target_count": 1}

        def cancel(self):
            return None

    first = PackManager(tmp_path, worker=BlockingWorker(first_entered, release_first))
    second = PackManager(tmp_path, worker=BlockingWorker(second_entered))

    first_thread = threading.Thread(
        target=lambda: first.update_index(lambda _event: None)
    )

    def second_update():
        try:
            second.update_index(lambda _event: None)
        except BaseException as error:
            errors.append(error)

    second_thread = threading.Thread(target=second_update)
    first_thread.start()
    assert first_entered.wait(1)
    second_thread.start()
    assert not second_entered.wait(0.1)
    second.cancel()
    second_thread.join(1)
    release_first.set()
    first_thread.join(2)

    assert not second_thread.is_alive()
    assert second_entered.is_set() is False
    assert len(errors) == 1
    assert isinstance(errors[0], FlashError)
    assert errors[0].code is FlashErrorCode.USER_ABORT


def test_pack_root_lock_is_released_after_operation_exception(tmp_path):
    class FailingWorker:
        def run(self, command, payload, on_event):
            raise RuntimeError("worker failed")

    class SuccessWorker:
        def run(self, command, payload, on_event):
            return {"status": "updated", "target_count": 1}

    with pytest.raises(RuntimeError, match="worker failed"):
        PackManager(tmp_path, worker=FailingWorker()).update_index(
            lambda _event: None
        )

    result = PackManager(tmp_path, worker=SuccessWorker()).update_index(
        lambda _event: None
    )

    assert result["status"] == "updated"


def test_pack_root_lock_is_released_after_process_exits_abnormally(tmp_path):
    marker = tmp_path / "child-acquired"
    script = (
        "import os,sys; from pathlib import Path; "
        "from mklink.cmsis_dap.pack_lock import PackRootLock; "
        "root=Path(sys.argv[1]); PackRootLock(root).acquire(timeout=1); "
        "(root/'child-acquired').write_text('yes'); os._exit(7)"
    )
    process = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=str(Path(__file__).resolve().parents[3]),
        check=False,
    )
    assert process.returncode == 7
    assert marker.read_text(encoding="utf-8") == "yes"

    class SuccessWorker:
        def run(self, command, payload, on_event):
            return {"status": "updated", "target_count": 1}

    result = PackManager(
        tmp_path, worker=SuccessWorker(), lock_timeout=1
    ).update_index(lambda _event: None)

    assert result["status"] == "updated"


def test_pack_root_lock_serializes_an_independent_process(tmp_path):
    marker = tmp_path / "child-acquired"
    release = tmp_path / "release-child"
    script = (
        "import sys,time; from pathlib import Path; "
        "from mklink.cmsis_dap.pack_lock import PackRootLock; "
        "root=Path(sys.argv[1]); lock=PackRootLock(root); lock.acquire(timeout=1); "
        "(root/'child-acquired').write_text('yes'); "
        "deadline=time.monotonic()+5; "
        "\nwhile not (root/'release-child').exists() and time.monotonic()<deadline: "
        "time.sleep(0.01)\nlock.release()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=str(Path(__file__).resolve().parents[3]),
    )

    class SuccessWorker:
        def run(self, command, payload, on_event):
            return {"status": "updated", "target_count": 1}

    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        with pytest.raises(FlashError) as captured:
            PackManager(
                tmp_path, worker=SuccessWorker(), lock_timeout=0.1
            ).update_index(lambda _event: None)
        assert captured.value.code is FlashErrorCode.PROBE_BUSY
    finally:
        release.write_text("yes", encoding="utf-8")
        process.wait(timeout=2)

    assert PackManager(
        tmp_path, worker=SuccessWorker(), lock_timeout=1
    ).update_index(lambda _event: None)["status"] == "updated"


def test_pack_root_lock_reentrant_release_keeps_outer_lock_held(tmp_path):
    from mklink.cmsis_dap.pack_lock import PackRootLock

    outer = PackRootLock(tmp_path)
    nested = PackRootLock(tmp_path)
    outer.acquire(timeout=1)
    nested.acquire(timeout=1)

    def competitor_result():
        outcomes = []

        def compete():
            lock = PackRootLock(tmp_path)
            try:
                lock.acquire(timeout=0.1)
            except FlashError as error:
                outcomes.append(error.code)
            else:
                outcomes.append("acquired")
                lock.release()

        thread = threading.Thread(target=compete)
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        return outcomes[0]

    try:
        assert competitor_result() is FlashErrorCode.PROBE_BUSY
        nested.release()
        assert competitor_result() is FlashErrorCode.PROBE_BUSY
        outer.release()
        assert competitor_result() == "acquired"
    finally:
        try:
            nested.release()
        except RuntimeError:
            pass
        try:
            outer.release()
        except RuntimeError:
            pass


def test_parent_death_guard_kills_worker_before_competitor_gets_lock(tmp_path):
    marker = tmp_path / "worker-writes"
    pid_file = tmp_path / "worker.pid"
    parent_script = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        "from mklink.cmsis_dap.pack_lock import PackRootLock; "
        "from mklink.cmsis_dap.process_guard import ("
        "attach_and_release_guarded_process,guarded_process_command); "
        "root=Path(sys.argv[1]); lock=PackRootLock(root); lock.acquire(timeout=1); "
        "child_code=\"import sys,time; from pathlib import Path; p=Path(sys.argv[1]); "
        "[(p.open('ab').write(b'x'),time.sleep(0.01)) for _ in iter(int,1)]\"; "
        "child=subprocess.Popen(guarded_process_command([sys.executable,'-c',child_code,str(root/'worker-writes')]),"
        "stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True); guard=attach_and_release_guarded_process(child); "
        "(root/'worker.pid').write_text(str(child.pid)); time.sleep(0.15); os._exit(9)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_script, str(tmp_path)],
        cwd=str(Path(__file__).resolve().parents[3]),
    )
    child_pid = None
    try:
        parent.wait(timeout=3)
        assert parent.returncode == 9
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        from mklink.cmsis_dap.pack_lock import PackRootLock

        competitor = PackRootLock(tmp_path)
        competitor.acquire(timeout=1)
        try:
            before = marker.stat().st_size
            time.sleep(0.25)
            after = marker.stat().st_size
        finally:
            competitor.release()
        assert after == before
        with pytest.raises(OSError):
            os.kill(child_pid, 0)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except OSError:
                pass


def test_guard_wrapper_exits_without_exec_when_parent_dies_before_attach(tmp_path):
    marker = tmp_path / "must-not-write"
    pid_file = tmp_path / "wrapper.pid"
    parent_script = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        "from mklink.cmsis_dap.pack_lock import PackRootLock; "
        "from mklink.cmsis_dap.process_guard import guarded_process_command; "
        "root=Path(sys.argv[1]); PackRootLock(root).acquire(timeout=1); "
        "code=\"from pathlib import Path; import sys; Path(sys.argv[1]).write_text('leak')\"; "
        "wrapper=subprocess.Popen(guarded_process_command([sys.executable,'-c',code,str(root/'must-not-write')]),stdin=subprocess.PIPE,text=True); "
        "(root/'wrapper.pid').write_text(str(wrapper.pid)); time.sleep(0.15); os._exit(11)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_script, str(tmp_path)],
        cwd=str(Path(__file__).resolve().parents[3]),
    )
    wrapper_pid = None
    try:
        parent.wait(timeout=3)
        assert parent.returncode == 11
        wrapper_pid = int(pid_file.read_text(encoding="utf-8"))
        from mklink.cmsis_dap.pack_lock import PackRootLock

        competitor = PackRootLock(tmp_path)
        competitor.acquire(timeout=1)
        competitor.release()
        time.sleep(0.2)
        assert not marker.exists()
        with pytest.raises(OSError):
            os.kill(wrapper_pid, 0)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
        if wrapper_pid is not None:
            try:
                os.kill(wrapper_pid, signal.SIGTERM)
            except OSError:
                pass


def test_guard_wrapper_executes_only_after_attach_and_go(tmp_path):
    from mklink.cmsis_dap.process_guard import (
        attach_and_release_guarded_process,
        guarded_process_command,
    )

    marker = tmp_path / "started-after-go"
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('go')"
    process = subprocess.Popen(
        guarded_process_command([sys.executable, "-c", code, str(marker)]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    guard = None
    try:
        time.sleep(0.15)
        assert not marker.exists()
        guard = attach_and_release_guarded_process(process)
        process.wait(timeout=2)
        assert marker.read_text(encoding="utf-8") == "go"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        if guard is not None:
            guard.close()


@pytest.mark.skipif(os.name != "nt", reason="validates Windows parent Job reuse")
def test_guard_creationflags_use_verified_parent_job_breakaway(
    monkeypatch,
):
    import mklink.cmsis_dap.process_guard as process_guard

    monkeypatch.setenv("MKLINK_PARENT_JOB_BREAKAWAY_OK", "1")
    monkeypatch.setattr(
        process_guard, "_current_job_limit_flags", lambda: 0x00002000 | 0x00000800
    )
    command = [sys.executable, "-c", "pass"]

    assert process_guard.guarded_process_command(command) != command
    assert process_guard.guarded_process_creationflags() == 0x01000000


def test_guard_command_rejects_spoofed_parent_job_marker(monkeypatch):
    import mklink.cmsis_dap.process_guard as process_guard

    monkeypatch.setenv("MKLINK_PARENT_JOB_BREAKAWAY_OK", "1")
    monkeypatch.setattr(
        process_guard, "_current_job_limit_flags", lambda: 0,
        raising=False,
    )
    command = [sys.executable, "-c", "pass"]

    assert process_guard.guarded_process_command(command) != command
    assert process_guard.guarded_process_creationflags() == 0


def test_guard_command_uses_internal_entrypoint_when_frozen(monkeypatch):
    import mklink.cmsis_dap.process_guard as process_guard

    monkeypatch.setattr(process_guard.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        process_guard.sys,
        "executable",
        r"C:\Program Files\MKLink\mklink-sidecar.exe",
    )
    monkeypatch.setattr(process_guard.os, "getpid", lambda: 1234)

    command = process_guard.guarded_process_command(["worker.exe", "--run"])

    assert command == [
        r"C:\Program Files\MKLink\mklink-sidecar.exe",
        "--internal-process-guard",
        "1234",
        "worker.exe",
        "--run",
    ]


@pytest.mark.skipif(os.name != "nt", reason="validates Windows venv launchers")
def test_guard_command_uses_base_interpreter_on_windows(monkeypatch):
    import mklink.cmsis_dap.process_guard as process_guard

    monkeypatch.setattr(process_guard.sys, "frozen", False, raising=False)
    monkeypatch.setattr(process_guard.sys, "executable", r"C:\venv\Scripts\python.exe")
    monkeypatch.setattr(
        process_guard.sys,
        "_base_executable",
        r"C:\Python313\python.exe",
        raising=False,
    )
    monkeypatch.setattr(process_guard.os, "getpid", lambda: 1234)

    command = process_guard.guarded_process_command(["worker.exe", "--run"])

    assert command[0] == r"C:\Python313\python.exe"
    assert command[1] == str(
        Path(process_guard.__file__).with_name("process_guard_exec.py")
    )
    assert command[2:] == ["1234", "worker.exe", "--run"]


def test_main_routes_internal_process_guard_entrypoint(monkeypatch):
    import runpy
    import mklink.cmsis_dap.process_guard_exec as guard_exec

    observed = []

    def fake_main():
        observed.append(list(sys.argv))
        return 17

    monkeypatch.setattr(guard_exec, "main", fake_main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mklink-sidecar.exe", "--internal-process-guard", "1234", "worker.exe"],
    )

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("mklink.__main__", run_name="__main__")

    assert raised.value.code == 17
    assert observed == [["mklink-sidecar.exe", "1234", "worker.exe"]]


def test_guard_attach_failure_is_fail_closed_and_leaves_no_worker(tmp_path):
    from mklink.cmsis_dap.process_guard import (
        attach_and_release_guarded_process,
        guarded_process_command,
    )

    marker = tmp_path / "must-not-start"
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('bad')"
    process = subprocess.Popen(
        guarded_process_command([sys.executable, "-c", code, str(marker)]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    def fail_attach(_process):
        raise OSError("attach failed")

    with pytest.raises(OSError, match="attach failed"):
        attach_and_release_guarded_process(process, guard_factory=fail_attach)

    assert process.poll() is not None
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="validates Windows READY failure")
def test_guard_ready_failure_closes_job_and_reaps_wrapper():
    from mklink.cmsis_dap.process_guard import attach_and_release_guarded_process

    code = "import sys; sys.stdin.readline(); print('WRONG',flush=True); sys.stdin.read()"
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    with pytest.raises(OSError, match="acknowledge"):
        attach_and_release_guarded_process(process)

    assert process.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="validates Windows Job Object cancel")
def test_cancel_closes_job_before_recovery_and_kills_real_worker(
    tmp_path, monkeypatch
):
    from mklink.cmsis_dap.process_guard import guarded_process_command as guard

    marker = tmp_path / "cancel-worker-writes"
    child_pid_file = tmp_path / "cancel-worker.pid"
    child_code = (
        "import json,os,sys,time; from pathlib import Path; "
        "request=json.loads(sys.stdin.readline()); "
        "print(json.dumps({'type':'event','event':'staging','path':request['staging_dir']}),flush=True); "
        "Path(sys.argv[2]).write_text(str(os.getpid())); p=Path(sys.argv[1]); "
        "[(p.open('ab').write(b'x'),time.sleep(0.01)) for _ in iter(int,1)]"
    )

    def command(_original):
        return guard(
            [sys.executable, "-c", child_code, str(marker), str(child_pid_file)]
        )

    monkeypatch.setattr(
        "mklink.cmsis_dap.pack_manager.guarded_process_command", command
    )
    worker = SubprocessPackWorker(PackPaths(tmp_path))
    original_recover = worker._recover_pending_transaction
    recovery_observations = []

    def process_alive(process_id):
        try:
            os.kill(process_id, 0)
            return True
        except OSError:
            return False

    def terminate_process(process_id):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x0001, False, process_id)
        if handle:
            try:
                kernel32.TerminateProcess(wintypes.HANDLE(handle), 1)
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(handle))

    def observed_recovery(*args, **kwargs):
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        before = marker.stat().st_size
        time.sleep(0.05)
        recovery_observations.append(
            (not process_alive(child_pid)) and marker.stat().st_size == before
        )
        return original_recover(*args, **kwargs)

    worker._recover_pending_transaction = observed_recovery
    manager = PackManager(tmp_path, worker=worker, lock_timeout=1)
    errors = []

    def update():
        try:
            manager.update_index(lambda _event: None)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=update, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_file.exists()
    with worker._lock:
        wrapper_process = worker._process
        wrapper_pid = wrapper_process.pid
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    try:
        manager.cancel()
        thread.join(1)
        assert not thread.is_alive()
        assert wrapper_process.poll() is not None
        assert not process_alive(child_pid)
        before = marker.stat().st_size
        time.sleep(0.15)
        assert marker.stat().st_size == before
        assert recovery_observations and all(recovery_observations)
        assert len(errors) == 1
        assert isinstance(errors[0], FlashError)
        assert errors[0].code is FlashErrorCode.USER_ABORT
        with worker._lock:
            assert worker._process is None
            assert worker._process_guard is None

        class SuccessWorker:
            def run(self, command, payload, on_event):
                return {"status": "updated", "target_count": 1}

        assert PackManager(
            tmp_path, worker=SuccessWorker(), lock_timeout=1
        ).update_index(lambda _event: None)["status"] == "updated"
    finally:
        for process_id in (child_pid, wrapper_pid):
            if process_alive(process_id):
                terminate_process(process_id)
        thread.join(2)


def test_remove_recovers_pending_transaction_while_holding_root_lock(tmp_path):
    paths = PackPaths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / "pack-transaction.json").write_text(
        '{"phase":"corrupt"}', encoding="utf-8"
    )

    with pytest.raises(FlashError) as captured:
        PackManager(tmp_path, worker=FakeWorker()).remove(
            "Vendor", "Pack", "1.0.0"
        )

    assert captured.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR


def test_waiting_remove_can_be_cancelled_without_later_deleting_pack(tmp_path):
    paths = PackPaths(tmp_path)
    pack_path = _pack_path(paths, "Vendor", "Pack", "1.0.0")
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(b"keep")
    paths.state_file.write_text(
        json.dumps(
            {"installed": {"Vendor.Pack": {"1.0.0": str(pack_path)}}}
        ),
        encoding="utf-8",
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    remove_errors = []

    class BlockingWorker:
        def run(self, command, payload, on_event):
            first_entered.set()
            assert release_first.wait(2)
            return {"status": "updated", "target_count": 1}

        def cancel(self):
            return None

    holder = PackManager(tmp_path, worker=BlockingWorker())
    remover = PackManager(tmp_path, worker=FakeWorker())
    holder_thread = threading.Thread(
        target=lambda: holder.update_index(lambda _event: None)
    )

    def remove():
        try:
            remover.remove("Vendor", "Pack", "1.0.0")
        except BaseException as error:
            remove_errors.append(error)

    remove_thread = threading.Thread(target=remove)
    holder_thread.start()
    assert first_entered.wait(1)
    remove_thread.start()
    time.sleep(0.1)
    try:
        assert remove_thread.is_alive()
        remover.cancel()
        remove_thread.join(1)
        assert not remove_thread.is_alive()
        assert len(remove_errors) == 1
        assert isinstance(remove_errors[0], FlashError)
        assert remove_errors[0].code is FlashErrorCode.USER_ABORT
        assert pack_path.read_bytes() == b"keep"
    finally:
        release_first.set()
        holder_thread.join(2)
        remove_thread.join(2)
    assert pack_path.read_bytes() == b"keep"


def test_remove_rejects_concurrent_operation_on_same_manager_immediately(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingWorker:
        def run(self, command, payload, on_event):
            entered.set()
            assert release.wait(2)
            return {"status": "updated", "target_count": 1}

        def cancel(self):
            return None

    manager = PackManager(tmp_path, worker=BlockingWorker(), lock_timeout=1)
    thread = threading.Thread(
        target=lambda: manager.update_index(lambda _event: None)
    )
    thread.start()
    assert entered.wait(1)
    try:
        started = time.monotonic()
        with pytest.raises(FlashError) as captured:
            manager.remove("Vendor", "Pack", "1.0.0")
        elapsed = time.monotonic() - started
        assert captured.value.code is FlashErrorCode.PROBE_BUSY
        assert elapsed < 0.1
    finally:
        release.set()
        thread.join(2)


def test_cancel_removes_staging(tmp_path):
    staging = PackPaths(tmp_path).staging_dir
    staging.mkdir(parents=True)
    (staging / "partial.pack").write_bytes(b"partial")
    manager = PackManager(root=tmp_path, worker=FakeWorker())
    manager._phase = "worker"

    manager.cancel()

    assert not staging.exists()


def test_cancel_preserves_unknown_sibling_staging_directory(tmp_path):
    paths = PackPaths(tmp_path)
    active = paths.staging_dir / "active-job"
    sibling = paths.staging_dir / "other-job"
    active.mkdir(parents=True)
    sibling.mkdir()
    (active / "partial.pack").write_bytes(b"active")
    (sibling / "partial.pack").write_bytes(b"sibling")
    direct_partial = paths.staging_dir / "direct.partial"
    direct_partial.write_bytes(b"direct")

    class Worker(FakeWorker):
        active_staging_dir = active

    manager = PackManager(tmp_path, worker=Worker())
    manager._phase = "worker"
    manager.cancel()

    assert not active.exists()
    assert (sibling / "partial.pack").read_bytes() == b"sibling"
    assert direct_partial.read_bytes() == b"direct"


def test_cancel_without_known_job_removes_direct_partials_but_keeps_child(tmp_path):
    paths = PackPaths(tmp_path)
    sibling = paths.staging_dir / "unknown-job"
    sibling.mkdir(parents=True)
    (sibling / "keep.pack").write_bytes(b"keep")
    direct = paths.staging_dir / "partial.pack"
    direct.write_bytes(b"partial")

    manager = PackManager(tmp_path, worker=FakeWorker())
    manager._phase = "worker"
    manager.cancel()

    assert not direct.exists()
    assert (sibling / "keep.pack").read_bytes() == b"keep"


def test_install_trims_part_number_and_forwards_events(tmp_path):
    worker = FakeWorker()
    events = []

    PackManager(tmp_path, worker=worker).install("  GD32F303RC  ", events.append)

    assert worker.commands == [("install", {"part_number": "GD32F303RC"})]
    assert events == [{"type": "progress", "current": 1, "total": 2}]


@pytest.mark.parametrize("part_number", ["", "  ", None, 123])
def test_install_rejects_empty_or_non_string_part_number(tmp_path, part_number):
    worker = FakeWorker()

    with pytest.raises(ValueError, match="part number"):
        PackManager(tmp_path, worker=worker).install(part_number, lambda event: None)

    assert worker.commands == []


def test_install_with_pack_path_merges_state_and_updates_catalog(tmp_path):
    paths = PackPaths(tmp_path)
    paths.index_dir.mkdir(parents=True)
    paths.index_file.write_text(
        json.dumps(
            {
                "GD32F303RC": {
                    "from_pack": {
                        "vendor": "GigaDevice",
                        "pack": "GD32F30x_DFP",
                        "version": "3.0.2",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    paths.data_dir.mkdir()
    old_pack = paths.data_dir / "Old.Pack.1.0.0.pack"
    old_pack.write_bytes(b"old")
    paths.state_file.write_text(
        json.dumps({"installed": {"Old.Pack": {"1.0.0": str(old_pack.resolve())}}}),
        encoding="utf-8",
    )
    installed_pack = _pack_path(
        paths, "GigaDevice", "GD32F30x_DFP", "3.0.2"
    )
    installed_pack.parent.mkdir(parents=True)
    installed_pack.write_bytes(b"pack")

    class InstalledWorker(FakeWorker):
        def run(self, command, payload, on_event):
            result = super().run(command, payload, on_event)
            result["pack_path"] = str(installed_pack)
            return result

    PackManager(tmp_path, worker=InstalledWorker()).install("GD32F303RC", lambda event: None)

    state = json.loads(paths.state_file.read_text(encoding="utf-8"))
    assert state == {
        "installed": {
            "Old.Pack": {"1.0.0": str(old_pack.resolve())},
            "GigaDevice.GD32F30x_DFP": {
                "3.0.2": str(installed_pack.resolve()),
            },
        }
    }
    record = PackCatalog(paths, builtin_provider=lambda: []).search("GD32F303RC")[0]
    assert record.installed is True
    assert record.pack_path == str(installed_pack.resolve())
    assert not list(paths.root.glob("state.json.*.tmp"))


def test_result_without_pack_path_does_not_create_state(tmp_path):
    PackManager(tmp_path, worker=FakeWorker()).install("GD32F303RC", lambda event: None)

    assert not PackPaths(tmp_path).state_file.exists()


def test_failed_result_with_pack_path_does_not_register_state(tmp_path):
    paths = PackPaths(tmp_path)
    pack_path = paths.data_dir / "failed.pack"
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(b"pack")

    class FailedWorker(FakeWorker):
        def run(self, command, payload, on_event):
            return {
                "status": "failed",
                "pack_id": "Vendor.Pack",
                "version": "1.0.0",
                "pack_path": str(pack_path),
            }

    result = PackManager(tmp_path, worker=FailedWorker()).install(
        "DEVICE", lambda event: None
    )

    assert result["status"] == "failed"
    assert not paths.state_file.exists()


def test_corrupt_state_is_not_overwritten_by_install(tmp_path):
    paths = PackPaths(tmp_path)
    paths.root.mkdir(exist_ok=True)
    paths.state_file.write_text("{broken", encoding="utf-8")
    pack_path = _pack_path(paths, "GigaDevice", "GD32F30x_DFP", "3.0.2")
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(b"pack")

    class InstalledWorker(FakeWorker):
        def run(self, command, payload, on_event):
            result = super().run(command, payload, on_event)
            result["pack_path"] = str(pack_path)
            return result

    with pytest.raises(FlashError) as raised:
        PackManager(tmp_path, worker=InstalledWorker()).install(
            "GD32F303RC", lambda event: None
        )

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert paths.state_file.read_text(encoding="utf-8") == "{broken"


def test_import_pack_uses_exact_resolved_path_and_registers_result(tmp_path):
    source = tmp_path / "incoming" / "Device.PACK"
    source.parent.mkdir()
    source.write_bytes(b"pack")
    worker = FakeWorker()

    result = PackManager(tmp_path, worker=worker).import_pack(source, lambda event: None)

    assert worker.commands == [("import", {"path": str(source.resolve())})]
    assert result["status"] == "installed"


@pytest.mark.parametrize("name", ["missing.pack", "not-a-pack.zip"])
def test_import_pack_rejects_missing_or_wrong_extension(tmp_path, name):
    path = tmp_path / name
    if name.endswith(".zip"):
        path.write_bytes(b"zip")
    worker = FakeWorker()

    with pytest.raises(ValueError, match="pack"):
        PackManager(tmp_path, worker=worker).import_pack(path, lambda event: None)

    assert worker.commands == []


def _write_installed_state(paths, installed):
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(json.dumps({"installed": installed}), encoding="utf-8")


def test_remove_deletes_exact_version_and_preserves_other_versions(tmp_path):
    paths = PackPaths(tmp_path)
    version_1 = _pack_path(paths, "Vendor", "Device_DFP", "1.0.0")
    version_2 = _pack_path(paths, "Vendor", "Device_DFP", "2.0.0")
    version_1.parent.mkdir(parents=True)
    version_2.parent.mkdir(parents=True)
    version_1.write_bytes(b"one")
    version_2.write_bytes(b"two")
    _write_installed_state(
        paths,
        {"Vendor.Device_DFP": {"1.0.0": str(version_1), "2.0.0": str(version_2)}},
    )

    PackManager(tmp_path, worker=FakeWorker()).remove(
        "Vendor", "Vendor.Device_DFP", "1.0.0"
    )

    assert not version_1.exists()
    assert version_2.read_bytes() == b"two"
    assert json.loads(paths.state_file.read_text(encoding="utf-8")) == {
        "installed": {"Vendor.Device_DFP": {"2.0.0": str(version_2)}}
    }


def test_remove_refuses_registered_path_outside_data_without_changing_state(tmp_path):
    paths = PackPaths(tmp_path / "root")
    outside = tmp_path / "outside.pack"
    outside.write_bytes(b"user")
    installed = {"Vendor.Device_DFP": {"1.0.0": str(outside)}}
    _write_installed_state(paths, installed)
    before = paths.state_file.read_bytes()

    with pytest.raises(FlashError) as raised:
        PackManager(paths.root, worker=FakeWorker()).remove(
            "Vendor", "Device_DFP", "1.0.0"
        )

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert outside.read_bytes() == b"user"
    assert paths.state_file.read_bytes() == before


def test_remove_refuses_pack_in_use(tmp_path):
    paths = PackPaths(tmp_path)
    pack_path = _pack_path(paths, "Vendor", "Device_DFP", "1.0.0")
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(b"pack")
    _write_installed_state(
        paths, {"Vendor.Device_DFP": {"1.0.0": str(pack_path)}}
    )

    with pytest.raises(FlashError) as raised:
        PackManager(tmp_path, worker=FakeWorker()).remove(
            "Vendor", "Device_DFP", "1.0.0", in_use=lambda pack_id, version: True
        )

    assert raised.value.code is FlashErrorCode.PROBE_BUSY
    assert pack_path.exists()


def test_cancel_calls_active_worker_once_and_only_removes_staging(tmp_path):
    paths = PackPaths(tmp_path)
    for path in (paths.data_dir / "keep.pack", paths.index_file, paths.state_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep", encoding="utf-8")
    paths.staging_dir.mkdir()
    (paths.staging_dir / "partial.pack").write_bytes(b"partial")

    class CancellableWorker:
        def __init__(self):
            self.started = threading.Event()
            self.released = threading.Event()
            self.cancel_calls = 0

        def run(self, command, payload, on_event):
            self.started.set()
            self.released.wait(2)
            return {"status": "cancelled"}

        def cancel(self):
            self.cancel_calls += 1
            self.released.set()

    worker = CancellableWorker()
    manager = PackManager(tmp_path, worker=worker)
    errors = []

    def install():
        try:
            manager.install("GD32F303RC", lambda event: None)
        except FlashError as error:
            errors.append(error)

    thread = threading.Thread(target=install)
    thread.start()
    assert worker.started.wait(1)

    manager.cancel()
    thread.join(2)
    manager.cancel()

    assert not thread.is_alive()
    assert worker.cancel_calls == 1
    assert [error.code for error in errors] == [FlashErrorCode.USER_ABORT]
    assert not paths.staging_dir.exists()
    assert (paths.data_dir / "keep.pack").read_text(encoding="utf-8") == "keep"
    assert paths.index_file.read_text(encoding="utf-8") == "keep"
    assert paths.state_file.read_text(encoding="utf-8") == "keep"


def test_cancelled_invocation_cannot_register_late_success(tmp_path):
    paths = PackPaths(tmp_path)
    pack_path = paths.data_dir / "Vendor" / "Device_DFP" / "1.0.0.pack"
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(b"pack")

    class LateSuccessWorker:
        def __init__(self):
            self.started = threading.Event()
            self.released = threading.Event()

        def run(self, command, payload, on_event):
            self.started.set()
            self.released.wait(2)
            return {
                "status": "installed",
                "pack_id": "Vendor.Device_DFP",
                "version": "1.0.0",
                "pack_path": str(pack_path),
            }

        def cancel(self):
            self.released.set()

    worker = LateSuccessWorker()
    manager = PackManager(tmp_path, worker=worker)
    errors = []

    def install():
        try:
            manager.install("DEVICE", lambda event: None)
        except FlashError as error:
            errors.append(error)

    thread = threading.Thread(target=install)
    thread.start()
    assert worker.started.wait(1)
    manager.cancel()
    thread.join(2)

    assert [error.code for error in errors] == [FlashErrorCode.USER_ABORT]
    assert not paths.state_file.exists()


def test_reporting_cache_tick_emits_structured_current_and_total():
    events = []
    cache = object.__new__(ReportingCache)
    cache._event_emitter = events.append

    cache._verbose_on_tick_fn(12, 5)

    assert events == [{"type": "progress", "current": 5, "total": 12}]


def test_worker_install_downloads_only_pack_for_exact_selected_device(tmp_path):
    class PackRef:
        vendor = "GigaDevice"
        pack = "GD32F30x_DFP"
        version = "3.0.2"

        def get_pack_name(self):
            return str(Path(self.vendor) / self.pack / (self.version + ".pack"))

    class FakeCache:
        instances = []

        def __init__(self, silent, no_timeouts, json_path, data_path, emitter):
            self.silent = silent
            self.json_path = Path(json_path)
            self.data_path = Path(data_path)
            assert self.json_path.is_dir()
            assert self.data_path.is_dir()
            self.emitter = emitter
            self.selected_devices = None
            self.downloaded_refs = None
            self._index = {
                "GD32F303RC": {
                    "from_pack": {
                        "vendor": "GigaDevice",
                        "pack": "GD32F30x_DFP",
                        "version": "3.0.2",
                    }
                },
                "OTHER": {
                    "from_pack": {
                        "vendor": "Other",
                        "pack": "Other_DFP",
                        "version": "9.9.9",
                    }
                },
            }
            self.instances.append(self)

        def cache_descriptors(self):
            (self.json_path / "index.json").write_text(
                json.dumps(self._index), encoding="utf-8"
            )
            (self.json_path / "aliases.json").write_text("{}", encoding="utf-8")

        @property
        def index(self):
            return self._index

        def packs_for_devices(self, devices):
            self.selected_devices = devices
            return [PackRef()]

        def download_pack_list(self, refs):
            self.downloaded_refs = refs
            artifact = self.data_path / refs[0].get_pack_name()
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"downloaded")

    events = []
    result = handle_request(
        {
            "command": "install",
            "payload": {"part_number": "GD32F303RC"},
            "root": str(tmp_path),
            "staging_dir": str((PackPaths(tmp_path).staging_dir / "install-job").resolve()),
        },
        events.append,
        cache_factory=FakeCache,
    )

    cache = FakeCache.instances[0]
    assert events[0]["event"] == "staging"
    assert Path(events[0]["path"]).is_absolute()
    assert cache.silent is False
    assert cache.selected_devices == [cache.index["GD32F303RC"]]
    assert len(cache.downloaded_refs) == 1
    assert result["pack_id"] == "GigaDevice.GD32F30x_DFP"
    assert result["version"] == "3.0.2"
    pack_path = Path(result["pack_path"])
    assert pack_path.read_bytes() == b"downloaded"
    assert pack_path == _pack_path(
        PackPaths(tmp_path), "GigaDevice", "GD32F30x_DFP", "3.0.2"
    )
    parent = SubprocessPackWorker(PackPaths(tmp_path))
    parent._active_staging_dir = Path(events[0]["path"])
    parent.acknowledge_commit(result)
    assert not PackPaths(tmp_path).staging_dir.exists()


def test_worker_install_falls_back_to_https_when_cache_omits_pack(
    tmp_path, monkeypatch
):
    class PackRef:
        vendor = "Vendor"
        pack = "Device_DFP"
        version = "1.2.3"

        def get_pack_name(self):
            return str(Path(self.vendor) / self.pack / (self.version + ".pack"))

        def get_pdsc_name(self):
            return "Vendor.Device_DFP.1.2.3.pdsc"

    class MissingArtifactCache:
        instances = []

        def __init__(self, silent, no_timeouts, json_path, data_path, emitter):
            self.json_path = Path(json_path)
            self.data_path = Path(data_path)
            self._index = {
                "DEVICE": {
                    "from_pack": {
                        "vendor": "Vendor",
                        "pack": "Device_DFP",
                        "version": "1.2.3",
                    }
                }
            }
            self.instances.append(self)

        def cache_descriptors(self):
            self.json_path.joinpath("index.json").write_text(
                json.dumps(self._index), encoding="utf-8"
            )
            self.json_path.joinpath("aliases.json").write_text(
                "{}", encoding="utf-8"
            )
            self.data_path.joinpath(
                "Vendor.Device_DFP.1.2.3.pdsc"
            ).write_text(
                "<package><url>https://packs.example/vendor/</url></package>",
                encoding="utf-8",
            )

        @property
        def index(self):
            return self._index

        def packs_for_devices(self, devices):
            return [PackRef()]

        def download_pack_list(self, refs):
            return None

    fallback_calls = []
    validated = []

    def fake_https_fallback(pdsc_path, staged_pack, expected, emit):
        fallback_calls.append((pdsc_path, staged_pack, expected))
        staged_pack.parent.mkdir(parents=True, exist_ok=True)
        staged_pack.write_bytes(b"fallback-pack")

    monkeypatch.setattr(
        pack_worker_module,
        "_download_pack_over_https",
        fake_https_fallback,
        raising=False,
    )
    monkeypatch.setattr(
        pack_worker_module,
        "_validate_pack_archive_identity",
        lambda path, expected: validated.append((path, expected)),
        raising=False,
    )

    events = []
    result = handle_request(
        {
            "command": "install",
            "payload": {"part_number": "DEVICE"},
            "root": str(tmp_path),
            "staging_dir": str(
                (PackPaths(tmp_path).staging_dir / "fallback-job").resolve()
            ),
        },
        events.append,
        cache_factory=MissingArtifactCache,
    )

    cache = MissingArtifactCache.instances[0]
    assert len(fallback_calls) == 1
    assert fallback_calls[0][0].name == "Vendor.Device_DFP.1.2.3.pdsc"
    assert fallback_calls[0][2] == ("Vendor", "Device_DFP", "1.2.3")
    assert validated == [
        (fallback_calls[0][1], ("Vendor", "Device_DFP", "1.2.3"))
    ]
    assert result["pack_id"] == "Vendor.Device_DFP"
    assert Path(result["pack_path"]).read_bytes() == b"fallback-pack"


def test_worker_install_uses_last_known_device_when_refresh_omits_it(
    tmp_path, monkeypatch
):
    paths = PackPaths(tmp_path)
    paths.index_dir.mkdir(parents=True)
    last_known = {
        "from_pack": {
            "vendor": "Vendor",
            "pack": "Device_DFP",
            "version": "1.2.3",
            "url": "https://packs.example/vendor/",
        }
    }
    paths.index_file.write_text(
        json.dumps({"DEVICE": last_known}), encoding="utf-8"
    )
    paths.aliases_file.write_text("{}", encoding="utf-8")

    class PackRef:
        vendor = "Vendor"
        pack = "Device_DFP"
        version = "1.2.3"

        def get_pack_name(self):
            return str(Path(self.vendor) / self.pack / (self.version + ".pack"))

    class IncompleteRefreshCache:
        def __init__(self, silent, no_timeouts, json_path, data_path, emitter):
            self.json_path = Path(json_path)
            self.data_path = Path(data_path)
            self._index = {}

        def cache_descriptors(self):
            self.json_path.joinpath("index.json").write_text("{}", encoding="utf-8")
            self.json_path.joinpath("aliases.json").write_text("{}", encoding="utf-8")

        @property
        def index(self):
            return self._index

        def packs_for_devices(self, devices):
            assert devices == [last_known]
            return [PackRef()]

        def download_pack_list(self, refs):
            raise AssertionError("missing refreshed descriptor must not be parsed")

    downloads = []
    validations = []

    def fake_base_download(base_url, staged_pack, expected, emit):
        downloads.append((base_url, expected))
        staged_pack.parent.mkdir(parents=True, exist_ok=True)
        staged_pack.write_bytes(b"last-known-pack")

    monkeypatch.setattr(
        pack_worker_module,
        "_download_pack_from_https_base",
        fake_base_download,
        raising=False,
    )
    monkeypatch.setattr(
        pack_worker_module,
        "_validate_pack_archive_identity",
        lambda path, expected: validations.append((path, expected)),
    )

    result = handle_request(
        {
            "command": "install",
            "payload": {"part_number": "DEVICE"},
            "root": str(tmp_path),
            "staging_dir": str((paths.staging_dir / "last-known-job").resolve()),
        },
        lambda event: None,
        cache_factory=IncompleteRefreshCache,
    )

    assert downloads == [
        (
            "https://packs.example/vendor/",
            ("Vendor", "Device_DFP", "1.2.3"),
        )
    ]
    assert len(validations) == 1
    assert Path(result["pack_path"]).read_bytes() == b"last-known-pack"
    installed_index = json.loads(paths.index_file.read_text(encoding="utf-8"))
    assert installed_index["DEVICE"] == last_known


class _FakePackResponse:
    def __init__(self, chunks, final_url, content_length):
        self._chunks = list(chunks)
        self._final_url = final_url
        self.headers = (
            {"Content-Length": str(content_length)}
            if content_length is not None
            else {}
        )
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def geturl(self):
        return self._final_url

    def read(self, size):
        return self._chunks.pop(0) if self._chunks else b""


def test_https_pack_fallback_streams_exact_identity_to_staging(
    tmp_path, monkeypatch
):
    pdsc = tmp_path / "Vendor.Device_DFP.1.2.3.pdsc"
    pdsc.write_text(
        "<package><url>https://packs.example/vendor/</url></package>",
        encoding="utf-8",
    )
    staged = tmp_path / "data" / "Vendor" / "Device_DFP" / "1.2.3.pack"
    opened = []

    def fake_open(request, timeout):
        opened.append((request.full_url, timeout))
        return _FakePackResponse(
            [b"pack", b"-bytes"], request.full_url, len(b"pack-bytes")
        )

    monkeypatch.setattr(
        pack_worker_module, "_open_https_request", fake_open
    )
    events = []

    pack_worker_module._download_pack_over_https(
        pdsc,
        staged,
        ("Vendor", "Device_DFP", "1.2.3"),
        events.append,
    )

    assert opened == [
        (
            "https://packs.example/vendor/Vendor.Device_DFP.1.2.3.pack",
            30,
        )
    ]
    assert staged.read_bytes() == b"pack-bytes"
    assert not staged.with_name(staged.name + ".download").exists()
    assert events[-1] == {"type": "progress", "current": 10, "total": 10}


def test_https_pack_fallback_rejects_http_source(tmp_path, monkeypatch):
    pdsc = tmp_path / "Vendor.Device_DFP.1.2.3.pdsc"
    pdsc.write_text(
        "<package><url>http://packs.example/vendor/</url></package>",
        encoding="utf-8",
    )

    def unexpected_open(request, timeout):
        raise AssertionError("insecure source must not be opened")

    monkeypatch.setattr(
        pack_worker_module, "_open_https_request", unexpected_open
    )

    with pytest.raises(pack_worker_module.WorkerFailure, match="must use HTTPS"):
        pack_worker_module._download_pack_over_https(
            pdsc,
            tmp_path / "1.2.3.pack",
            ("Vendor", "Device_DFP", "1.2.3"),
            lambda event: None,
        )


def test_https_pack_fallback_rejects_redirect_downgrade(tmp_path, monkeypatch):
    pdsc = tmp_path / "Vendor.Device_DFP.1.2.3.pdsc"
    pdsc.write_text(
        "<package><url>https://packs.example/vendor/</url></package>",
        encoding="utf-8",
    )
    staged = tmp_path / "1.2.3.pack"

    def fake_open(request, timeout):
        return _FakePackResponse(
            [b"pack"], "http://cdn.example/pack.pack", len(b"pack")
        )

    monkeypatch.setattr(
        pack_worker_module, "_open_https_request", fake_open
    )

    with pytest.raises(pack_worker_module.WorkerFailure, match="redirected outside HTTPS"):
        pack_worker_module._download_pack_over_https(
            pdsc,
            staged,
            ("Vendor", "Device_DFP", "1.2.3"),
            lambda event: None,
        )

    assert not staged.exists()


@pytest.mark.parametrize(
    "intermediate_url",
    [
        "http://mirror.example/intermediate.pack",
        "https://user:secret@mirror.example/intermediate.pack",
    ],
)
def test_https_redirect_handler_rejects_unsafe_intermediate_hop(
    intermediate_url,
):
    handler = pack_worker_module._HTTPSOnlyRedirectHandler()
    request = pack_worker_module.Request("https://packs.example/start.pack")

    with pytest.raises(
        pack_worker_module.WorkerFailure, match="redirected outside HTTPS"
    ):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            intermediate_url,
        )


def test_https_pack_fallback_rejects_oversized_response(tmp_path, monkeypatch):
    pdsc = tmp_path / "Vendor.Device_DFP.1.2.3.pdsc"
    pdsc.write_text(
        "<package><url>https://packs.example/vendor/</url></package>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pack_worker_module, "_MAX_PACK_DOWNLOAD_BYTES", 4, raising=False
    )

    def fake_open(request, timeout):
        return _FakePackResponse([b"large"], request.full_url, 5)

    monkeypatch.setattr(
        pack_worker_module, "_open_https_request", fake_open
    )

    with pytest.raises(pack_worker_module.WorkerFailure, match="size limit"):
        pack_worker_module._download_pack_over_https(
            pdsc,
            tmp_path / "1.2.3.pack",
            ("Vendor", "Device_DFP", "1.2.3"),
            lambda event: None,
        )


def test_https_pack_fallback_limits_stream_without_content_length(
    tmp_path, monkeypatch
):
    pdsc = tmp_path / "Vendor.Device_DFP.1.2.3.pdsc"
    pdsc.write_text(
        "<package><url>https://packs.example/vendor/</url></package>",
        encoding="utf-8",
    )
    monkeypatch.setattr(pack_worker_module, "_MAX_PACK_DOWNLOAD_BYTES", 4)

    def fake_open(request, timeout):
        return _FakePackResponse([b"123", b"45"], request.full_url, None)

    monkeypatch.setattr(pack_worker_module, "_open_https_request", fake_open)

    with pytest.raises(pack_worker_module.WorkerFailure, match="size limit"):
        pack_worker_module._download_pack_over_https(
            pdsc,
            tmp_path / "1.2.3.pack",
            ("Vendor", "Device_DFP", "1.2.3"),
            lambda event: None,
        )


def test_https_pack_fallback_redacts_paths_and_url_credentials(
    tmp_path, monkeypatch
):
    missing = tmp_path / "SENTINEL-PDSC-PATH.pdsc"

    with pytest.raises(pack_worker_module.WorkerFailure) as missing_error:
        pack_worker_module._download_pack_over_https(
            missing,
            tmp_path / "1.2.3.pack",
            ("Vendor", "Device_DFP", "1.2.3"),
            lambda event: None,
        )

    assert "SENTINEL-PDSC-PATH" not in missing_error.value.message
    assert str(tmp_path) not in missing_error.value.message

    pdsc = tmp_path / "Vendor.Device_DFP.1.2.3.pdsc"
    pdsc.write_text(
        "<package><url>https://packs.example/vendor/</url></package>",
        encoding="utf-8",
    )

    def failing_open(request, timeout):
        raise OSError("https://user:SECRET@packs.example/private")

    monkeypatch.setattr(pack_worker_module, "_open_https_request", failing_open)

    with pytest.raises(pack_worker_module.WorkerFailure) as network_error:
        pack_worker_module._download_pack_over_https(
            pdsc,
            tmp_path / "network.pack",
            ("Vendor", "Device_DFP", "1.2.3"),
            lambda event: None,
        )

    assert "SECRET" not in network_error.value.message
    assert "user:" not in network_error.value.message


def test_https_pack_fallback_redacts_malformed_url_credentials(tmp_path):
    pdsc = tmp_path / "Vendor.Device_DFP.1.2.3.pdsc"
    pdsc.write_text(
        "<package><url>https://user:SECRET@[invalid/</url></package>",
        encoding="utf-8",
    )

    with pytest.raises(pack_worker_module.WorkerFailure) as raised:
        pack_worker_module._download_pack_over_https(
            pdsc,
            tmp_path / "1.2.3.pack",
            ("Vendor", "Device_DFP", "1.2.3"),
            lambda event: None,
        )

    assert raised.value.code is FlashErrorCode.PACK_DOWNLOAD_FAIL
    assert raised.value.message == "pack download fallback must use HTTPS"
    assert "SECRET" not in raised.value.message


def test_pack_archive_identity_accepts_matching_unique_pdsc(tmp_path):
    pack = tmp_path / "1.2.3.pack"
    with ZipFile(pack, "w") as archive:
        archive.writestr(
            "Vendor.Device_DFP.pdsc",
            """<package>
                <vendor>Vendor:123</vendor>
                <name>Device_DFP</name>
                <releases><release version="1.2.3"/></releases>
            </package>""",
        )

    pack_worker_module._validate_pack_archive_identity(
        pack, ("Vendor", "Device_DFP", "1.2.3")
    )


def test_pack_archive_identity_rejects_mismatched_pdsc(tmp_path):
    pack = tmp_path / "1.2.3.pack"
    with ZipFile(pack, "w") as archive:
        archive.writestr(
            "Other.Device_DFP.pdsc",
            """<package>
                <vendor>Other</vendor>
                <name>Device_DFP</name>
                <releases><release version="1.2.3"/></releases>
            </package>""",
        )

    with pytest.raises(
        pack_worker_module.WorkerFailure, match="identity does not match"
    ):
        pack_worker_module._validate_pack_archive_identity(
            pack, ("Vendor", "Device_DFP", "1.2.3")
        )


def test_pack_archive_identity_rejects_historical_expected_version(tmp_path):
    pack = tmp_path / "1.2.3.pack"
    with ZipFile(pack, "w") as archive:
        archive.writestr(
            "Vendor.Device_DFP.pdsc",
            """<package>
                <vendor>Vendor</vendor>
                <name>Device_DFP</name>
                <releases>
                    <release version="2.0.0"/>
                    <release version="1.2.3"/>
                </releases>
            </package>""",
        )

    with pytest.raises(
        pack_worker_module.WorkerFailure, match="current version"
    ):
        pack_worker_module._validate_pack_archive_identity(
            pack, ("Vendor", "Device_DFP", "1.2.3")
        )


def test_worker_update_index_atomically_promotes_complete_metadata(tmp_path):
    paths = PackPaths(tmp_path)
    paths.index_dir.mkdir(parents=True)
    paths.index_file.write_text('{"OLD":{}}', encoding="utf-8")
    paths.aliases_file.write_text('{"old":"OLD"}', encoding="utf-8")

    class FakeIndexCache:
        def __init__(self, silent, no_timeouts, json_path, data_path, emitter):
            self.json_path = Path(json_path)
            self.data_path = Path(data_path)
            self._index = {"DEVICE_A": {}, "DEVICE_B": {}}

        def cache_descriptors(self):
            self.json_path.joinpath("index.json").write_text(
                json.dumps(self._index), encoding="utf-8"
            )
            self.json_path.joinpath("aliases.json").write_text(
                '{"alias":"DEVICE_A"}', encoding="utf-8"
            )

        @property
        def index(self):
            return self._index

    stage = paths.staging_dir / "index-job"
    result = handle_request(
        {
            "command": "update-index",
            "payload": {},
            "root": str(tmp_path),
            "staging_dir": str(stage.resolve()),
        },
        lambda event: None,
        cache_factory=FakeIndexCache,
    )

    assert result == {"status": "updated", "target_count": 2}
    assert json.loads(paths.index_file.read_text(encoding="utf-8")) == {
        "DEVICE_A": {},
        "DEVICE_B": {},
    }
    assert json.loads(paths.aliases_file.read_text(encoding="utf-8")) == {
        "alias": "DEVICE_A"
    }
    parent = SubprocessPackWorker(paths)
    parent._active_staging_dir = stage
    parent.acknowledge_commit(result)
    assert not paths.staging_dir.exists()


def test_parent_accepts_update_index_result_shape(tmp_path):
    worker = SubprocessPackWorker(PackPaths(tmp_path))

    result = worker._finish_result(
        "update-index", 0, {"status": "updated", "target_count": 10}, ""
    )

    assert result == {"status": "updated", "target_count": 10}


def test_worker_import_promotes_exact_artifact_and_metadata(tmp_path):
    source = tmp_path / "incoming.pack"
    with ZipFile(source, "w") as archive:
        archive.writestr(
            "Vendor.Device_DFP.pdsc",
            "<package><vendor>Vendor</vendor><name>Device_DFP</name></package>",
        )

    class FakeImportCache:
        instances = []

        def __init__(self, silent, no_timeouts, json_path, data_path, emitter):
            self.json_path = Path(json_path)
            self.data_path = Path(data_path)
            assert self.json_path.is_dir()
            assert self.data_path.is_dir()
            self.added_path = None
            self._index = {}
            self.instances.append(self)

        def add_pack_from_path(self, path):
            self.added_path = Path(path)
            assert self.added_path.suffix == ".pdsc"
            assert b"Device_DFP" in self.added_path.read_bytes()
            details = {
                "from_pack": {
                    "vendor": "Vendor",
                    "pack": "Device_DFP",
                    "version": "1.2.3",
                }
            }
            self._index = {"DEVICE_A": details, "DEVICE_B": details}
            self.json_path.joinpath("index.json").write_text(
                json.dumps(self._index), encoding="utf-8"
            )
            self.json_path.joinpath("aliases.json").write_text("{}", encoding="utf-8")

        @property
        def index(self):
            return self._index

    result = handle_request(
        {
            "command": "import",
            "payload": {"path": str(source)},
            "root": str(tmp_path),
            "staging_dir": str((PackPaths(tmp_path).staging_dir / "import-job").resolve()),
        },
        lambda event: None,
        cache_factory=FakeImportCache,
    )

    assert result == {
        "status": "installed",
        "pack_id": "Vendor.Device_DFP",
        "version": "1.2.3",
        "pack_path": str(
            _pack_path(PackPaths(tmp_path), "Vendor", "Device_DFP", "1.2.3")
        ),
    }
    assert Path(result["pack_path"]).read_bytes() == source.read_bytes()
    parent = SubprocessPackWorker(PackPaths(tmp_path))
    parent._active_staging_dir = PackPaths(tmp_path).staging_dir / "import-job"
    parent.acknowledge_commit(result)
    assert not PackPaths(tmp_path).staging_dir.exists()


def test_worker_import_indexes_the_descriptor_inside_a_real_pack_archive(tmp_path):
    source = tmp_path / "Vendor.Device_DFP.1.2.3.pack"
    descriptor = """<?xml version="1.0" encoding="UTF-8"?>
<package schemaVersion="1.7.0">
  <vendor>Vendor</vendor>
  <name>Device_DFP</name>
  <description>Test device family</description>
  <url>https://example.com/pack/</url>
  <releases><release version="1.2.3" date="2026-01-01">Test</release></releases>
  <devices>
    <family Dfamily="Test" Dvendor="Vendor:1">
      <processor Dcore="Cortex-M3" Dfpu="NO_FPU" Dmpu="NO_MPU" Dendian="Little-endian" Dclock="1000000"/>
      <device Dname="DEVICE_A">
        <memory name="IROM1" start="0x08000000" size="0x1000" access="rx" default="1" startup="1"/>
        <memory name="IRAM1" start="0x20000000" size="0x1000" access="rwx" default="1"/>
        <algorithm name="Flash/Test.FLM" start="0x08000000" size="0x1000" default="1"/>
      </device>
    </family>
  </devices>
</package>
"""
    with ZipFile(source, "w") as archive:
        archive.writestr("Vendor.Device_DFP.pdsc", descriptor)
        archive.writestr("Flash/Test.FLM", b"algorithm")

    result = handle_request(
        {
            "command": "import",
            "payload": {"path": str(source)},
            "root": str(tmp_path / "cache"),
            "staging_dir": str(
                (PackPaths(tmp_path / "cache").staging_dir / "real-import").resolve()
            ),
        },
        lambda event: None,
    )

    assert result["pack_id"] == "Vendor.Device_DFP"
    assert result["version"] == "1.2.3"
    assert Path(result["pack_path"]).read_bytes() == source.read_bytes()


def test_worker_import_indexes_missing_url_pack_without_rewriting_readonly_source(
    tmp_path,
):
    source = tmp_path / "WHXY.CW32L012_DFP.1.0.2.pack"
    descriptor = """<?xml version="1.0" encoding="utf-8"?>
<package schemaVersion="1.1" xmlns:xs="http://www.w3.org/2001/XMLSchema-instance">
  <vendor>WHXY</vendor>
  <name>CW32L012_DFP</name>
  <description>CW32 ARM Cortex-M0+ Device Family Pack</description>
  <releases>
    <release version="1.0.2" date="2025-09-02">Version 1.0.2</release>
    <release version="1.0.1" date="2025-07-30">Version 1.0.1</release>
  </releases>
  <devices>
    <family Dfamily="CW32L0 Series" Dvendor="WHXY">
      <processor Dcore="Cortex-M0+" Dfpu="0" Dmpu="0" Dendian="Little-endian"/>
      <subFamily DsubFamily="CW32L012">
        <device Dname="CW32L012C8">
          <processor Dclock="96000000"/>
          <memory name="IROM1" access="rx" start="0x00000000" size="0x10000" startup="1" default="1"/>
          <memory name="IRAM1" access="rw" start="0x20000000" size="0x02000" init="0" default="1"/>
          <algorithm name="Flash/FlashCW32L012.FLM" start="0x00000000" size="0x10000" default="1"/>
        </device>
      </subFamily>
    </family>
  </devices>
</package>
"""
    with ZipFile(source, "w") as archive:
        archive.writestr("WHXY.CW32L012_DFP.pdsc", descriptor)
        archive.writestr("Flash/FlashCW32L012.FLM", b"algorithm")
    source_bytes = source.read_bytes()
    source.chmod(stat.S_IREAD)
    cache_root = tmp_path / "cache"
    paths = PackPaths(cache_root)
    stage = paths.staging_dir / "missing-url-import"

    try:
        result = handle_request(
            {
                "command": "import",
                "payload": {"path": str(source)},
                "root": str(cache_root),
                "staging_dir": str(stage.resolve()),
            },
            lambda event: None,
        )

        installed = Path(result["pack_path"])
        index = json.loads(paths.index_file.read_text(encoding="utf-8"))
        assert result["pack_id"] == "WHXY.CW32L012_DFP"
        assert result["version"] == "1.0.2"
        assert index["CW32L012C8"]["from_pack"] == {
            "pack": "CW32L012_DFP",
            "url": "",
            "vendor": "WHXY",
            "version": "1.0.2",
        }
        assert source.read_bytes() == source_bytes
        assert installed.read_bytes() == source_bytes
        assert installed.stat().st_mode & stat.S_IWRITE

        parent = SubprocessPackWorker(paths)
        parent._active_staging_dir = stage
        parent.acknowledge_commit(result)
        assert not paths.staging_dir.exists()
    finally:
        source.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_manager_reimports_over_legacy_readonly_installed_pack(tmp_path):
    source = tmp_path / "Vendor.Device_DFP.1.2.3.pack"
    descriptor = """<?xml version="1.0" encoding="UTF-8"?>
<package schemaVersion="1.7.0">
  <vendor>Vendor</vendor>
  <name>Device_DFP</name>
  <description>Test device family</description>
  <url>https://example.com/pack/</url>
  <releases><release version="1.2.3" date="2026-01-01">Test</release></releases>
  <devices>
    <family Dfamily="Test" Dvendor="Vendor:1">
      <processor Dcore="Cortex-M3" Dfpu="NO_FPU" Dmpu="NO_MPU" Dendian="Little-endian" Dclock="1000000"/>
      <device Dname="DEVICE_A">
        <memory name="IROM1" start="0x08000000" size="0x1000" access="rx" default="1" startup="1"/>
        <memory name="IRAM1" start="0x20000000" size="0x1000" access="rwx" default="1"/>
        <algorithm name="Flash/Test.FLM" start="0x08000000" size="0x1000" default="1"/>
      </device>
    </family>
  </devices>
</package>
"""
    with ZipFile(source, "w") as archive:
        archive.writestr("Vendor.Device_DFP.pdsc", descriptor)
        archive.writestr("Flash/Test.FLM", b"algorithm")

    cache_root = tmp_path / "cache"
    paths = PackPaths(cache_root)
    manager = PackManager(cache_root)
    first = manager.import_pack(source, lambda _event: None)
    installed = Path(first["pack_path"])
    installed.chmod(stat.S_IREAD)

    try:
        second = manager.import_pack(source, lambda _event: None)

        assert second == first
        assert installed.read_bytes() == source.read_bytes()
        assert installed.stat().st_mode & stat.S_IWRITE
        assert not (paths.root / "pack-transaction.json").exists()
        assert not paths.staging_dir.exists()
    finally:
        for candidate in tmp_path.rglob("*"):
            if candidate.is_file():
                candidate.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_local_import_descriptor_preserves_url_and_adds_namespaced_url():
    existing = b"""<?xml version="1.0" encoding="utf-8"?>
<package><vendor>Vendor</vendor><name>Pack</name><url>https://example.com/</url></package>
"""
    assert pack_worker_module._normalize_local_import_descriptor(existing) == existing

    missing = b"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="urn:test"><vendor>Vendor</vendor><name>Pack</name><description>Test</description></package>
"""
    normalized = pack_worker_module._normalize_local_import_descriptor(missing)
    root = ElementTree.fromstring(normalized)
    urls = [child for child in root if child.tag.rsplit("}", 1)[-1] == "url"]

    assert len(urls) == 1
    assert urls[0].tag == "{urn:test}url"


def test_local_import_descriptor_does_not_confuse_extension_url_with_pack_url():
    descriptor = b"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns:ext="urn:extension">
  <vendor>Vendor</vendor>
  <name>Pack</name>
  <ext:description>Extension metadata</ext:description>
  <ext:url>https://example.com/extension/</ext:url>
</package>
"""

    normalized = pack_worker_module._normalize_local_import_descriptor(descriptor)
    root = ElementTree.fromstring(normalized)
    children = list(root)

    assert [child.tag for child in children[:3]] == ["vendor", "name", "url"]
    assert [child.tag for child in children].count("url") == 1
    assert [child.tag for child in children].count("{urn:extension}url") == 1


def test_worker_import_missing_url_pack_without_devices_still_fails(tmp_path):
    source = tmp_path / "Vendor.Empty_DFP.1.0.0.pack"
    descriptor = b"""<?xml version="1.0" encoding="utf-8"?>
<package schemaVersion="1.7.0">
  <vendor>Vendor</vendor>
  <name>Empty_DFP</name>
  <description>No devices</description>
  <releases><release version="1.0.0">Test</release></releases>
  <devices/>
</package>
"""
    with ZipFile(source, "w") as archive:
        archive.writestr("Vendor.Empty_DFP.pdsc", descriptor)
    source_bytes = source.read_bytes()
    cache_root = tmp_path / "cache"

    with pytest.raises(pack_worker_module.WorkerFailure) as raised:
        handle_request(
            {
                "command": "import",
                "payload": {"path": str(source)},
                "root": str(cache_root),
                "staging_dir": str(
                    (PackPaths(cache_root).staging_dir / "empty-import").resolve()
                ),
            },
            lambda event: None,
        )

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert raised.value.message == "imported pack metadata is missing or ambiguous"
    assert source.read_bytes() == source_bytes
    assert not list(PackPaths(cache_root).data_dir.rglob("*.pack"))


def test_worker_protocol_stdout_contains_json_lines_only():
    from io import StringIO

    stdin = StringIO(json.dumps({"command": "fake"}) + "\n")
    stdout = StringIO()

    def fake_handler(request, emit):
        emit({"type": "log", "message": "working"})
        return {"status": "installed", "pack_id": "V.P", "version": "1"}

    exit_code = run_protocol(stdin, stdout, handler=fake_handler)

    lines = stdout.getvalue().splitlines()
    assert exit_code == 0
    assert [json.loads(line) for line in lines] == [
        {"type": "log", "message": "working"},
        {
            "type": "result",
            "result": {"status": "installed", "pack_id": "V.P", "version": "1"},
        },
    ]


def test_subprocess_worker_forwards_progress_before_process_wait(monkeypatch, tmp_path):
    events = []
    confirmed_staging = []

    class FakeInput:
        def __init__(self):
            self.value = ""
            self.flushed = False
            self.closed = False

        def write(self, value):
            self.value += value

        def flush(self):
            self.flushed = True

        def close(self):
            self.closed = True

    class FakeError:
        def __init__(self):
            self.drained = threading.Event()

        def read(self):
            self.drained.set()
            return "diagnostic only"

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeInput()
            self.stderr = FakeError()
            self.wait_called = False
            self.returncode = None
            self.stdout = self._stdout_lines()

        def _stdout_lines(self):
            staging = Path(json.loads(self.stdin.value)["staging_dir"])
            staging.mkdir(parents=True)
            confirmed_staging.append(staging)
            yield json.dumps(
                {"type": "event", "event": "staging", "path": str(staging)}
            ) + "\n"
            assert worker.active_staging_dir == staging.resolve()
            yield json.dumps(
                {"type": "progress", "current": 1, "total": 2}
            ) + "\n"
            assert events == [{"type": "progress", "current": 1, "total": 2}]
            assert self.wait_called is False
            yield json.dumps(
                {
                    "type": "result",
                    "result": {
                        "status": "installed",
                        "pack_id": "Vendor.Device_DFP",
                        "version": "1.2.3",
                        "pack_path": "unused.pack",
                    },
                }
            ) + "\n"

        def wait(self, timeout=None):
            self.wait_called = True
            self.returncode = 0
            return 0

    process = FakeProcess()
    monkeypatch.setattr(
        "mklink.cmsis_dap.pack_manager.subprocess.Popen",
        lambda *args, **kwargs: process,
    )

    worker = SubprocessPackWorker(PackPaths(tmp_path))
    result = worker.run(
        "install", {"part_number": "DEVICE"}, events.append
    )

    assert result["status"] == "installed"
    assert process.stdin.flushed is True
    assert process.stdin.closed is True
    assert process.stderr.drained.is_set()
    assert process.wait_called is True
    assert len(confirmed_staging) == 1
    assert not confirmed_staging[0].exists()
    request = json.loads(process.stdin.value)
    assert request["payload"] == {"part_number": "DEVICE"}


def test_pack_worker_uses_internal_entrypoint_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\MKLink\mklink-sidecar.exe")

    command = SubprocessPackWorker(PackPaths(tmp_path))._worker_command()

    assert command == [
        r"C:\Program Files\MKLink\mklink-sidecar.exe",
        "--internal-pack-worker",
    ]


def test_subprocess_worker_stops_process_on_invalid_json(monkeypatch, tmp_path):
    class Input:
        def write(self, value):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class Error:
        def read(self):
            return "diagnostic"

    class Process:
        def __init__(self):
            self.stdin = Input()
            self.stdout = iter(["not-json\n"])
            self.stderr = Error()
            self.terminated = False
            self.waited = False
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 1

        def wait(self, timeout=None):
            self.waited = True
            return self.returncode

    process = Process()
    monkeypatch.setattr(
        "mklink.cmsis_dap.pack_manager.subprocess.Popen",
        lambda *args, **kwargs: process,
    )

    with pytest.raises(FlashError) as raised:
        SubprocessPackWorker(PackPaths(tmp_path)).run(
            "install", {"part_number": "DEVICE"}, lambda event: None
        )

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert process.terminated is True
    assert process.waited is True


def test_subprocess_cancel_before_run_latches_without_spawning(monkeypatch, tmp_path):
    popen_calls = 0

    def forbidden_popen(*args, **kwargs):
        nonlocal popen_calls
        popen_calls += 1
        raise AssertionError("Popen must not run after a latched cancel")

    monkeypatch.setattr(
        "mklink.cmsis_dap.pack_manager.subprocess.Popen", forbidden_popen
    )
    worker = SubprocessPackWorker(PackPaths(tmp_path))

    worker.cancel()
    with pytest.raises(FlashError) as raised:
        worker.run("install", {"part_number": "DEVICE"}, lambda event: None)

    assert raised.value.code is FlashErrorCode.USER_ABORT
    assert popen_calls == 0


def test_cancel_during_popen_factory_never_allows_success(monkeypatch, tmp_path):
    factory_entered = threading.Event()
    release_factory = threading.Event()
    cancel_done = threading.Event()

    class Input:
        def write(self, value):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class Error:
        def read(self):
            return ""

    class Process:
        def __init__(self):
            self.stdin = Input()
            self.stderr = Error()
            self.returncode = None
            self.terminated = threading.Event()
            self.stdout = self._lines()

        def _lines(self):
            if not self.terminated.wait(0.2):
                yield json.dumps(
                    {
                        "type": "result",
                        "result": {
                            "status": "installed",
                            "pack_id": "Vendor.Pack",
                            "version": "1.0.0",
                            "pack_path": "must-not-succeed.pack",
                        },
                    }
                ) + "\n"

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 1
            self.terminated.set()

        def wait(self, timeout=None):
            return self.returncode if self.returncode is not None else 0

    process = Process()

    def blocked_popen(*args, **kwargs):
        factory_entered.set()
        assert release_factory.wait(1)
        return process

    monkeypatch.setattr(
        "mklink.cmsis_dap.pack_manager.subprocess.Popen", blocked_popen
    )
    worker = SubprocessPackWorker(PackPaths(tmp_path))
    results = []
    errors = []

    def run_worker():
        try:
            results.append(
                worker.run("install", {"part_number": "DEVICE"}, lambda event: None)
            )
        except FlashError as error:
            errors.append(error)

    run_thread = threading.Thread(target=run_worker)
    run_thread.start()
    assert factory_entered.wait(1)

    def cancel_worker():
        worker.cancel()
        cancel_done.set()

    cancel_thread = threading.Thread(target=cancel_worker)
    cancel_thread.start()
    cancel_done.wait(0.1)
    release_factory.set()
    run_thread.join(2)
    cancel_thread.join(2)

    assert not run_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert results == []
    assert [error.code for error in errors] == [FlashErrorCode.USER_ABORT]
    assert process.terminated.is_set()


def test_cancel_before_first_output_cleans_parent_known_staging(
    monkeypatch, tmp_path
):
    paths = PackPaths(tmp_path)
    stage_created = threading.Event()

    class Input:
        def __init__(self, process):
            self.process = process
            self.value = ""

        def write(self, value):
            self.value += value

        def flush(self):
            pass

        def close(self):
            request = json.loads(self.value)
            self.process.request = request
            stage = Path(request["staging_dir"])
            stage.mkdir(parents=True)
            stage_created.set()

    class Error:
        def read(self):
            return ""

    class Process:
        def __init__(self):
            self.stdin = Input(self)
            self.stderr = Error()
            self.returncode = None
            self.terminated = threading.Event()
            self.stdout = self._lines()
            self.request = None

        def _lines(self):
            self.terminated.wait(1)
            return
            yield

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 1
            self.terminated.set()

        def wait(self, timeout=None):
            return self.returncode

    process = Process()
    worker = SubprocessPackWorker(paths)
    observed_before_spawn = []

    def fake_popen(*args, **kwargs):
        observed_before_spawn.append(worker._active_staging_dir)
        return process

    monkeypatch.setattr(
        "mklink.cmsis_dap.pack_manager.subprocess.Popen", fake_popen
    )
    errors = []

    def run_worker():
        try:
            worker.run("install", {"part_number": "DEVICE"}, lambda event: None)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_worker)
    thread.start()
    assert stage_created.wait(1)
    active = worker.active_staging_dir
    assert active is not None
    sibling = paths.staging_dir / "sibling"
    sibling.mkdir()
    (sibling / "keep").write_bytes(b"keep")

    worker.cancel()
    thread.join(2)

    assert observed_before_spawn == [active]
    assert not active.exists()
    assert (sibling / "keep").read_bytes() == b"keep"
    assert [error.code for error in errors if isinstance(error, FlashError)] == [
        FlashErrorCode.USER_ABORT
    ]


def test_child_uses_exact_parent_supplied_staging_directory(tmp_path):
    paths = PackPaths(tmp_path)
    supplied = paths.staging_dir / "parent-job"
    events = []

    with pytest.raises(pack_worker_module.WorkerFailure):
        handle_request(
            {
                "command": "unknown",
                "payload": {},
                "root": str(tmp_path),
                "staging_dir": str(supplied.resolve()),
            },
            events.append,
        )

    assert Path(events[0]["path"]) == supplied.resolve()
    assert not supplied.exists()


@pytest.mark.parametrize("fail_at", [2, 3, 5])
def test_transaction_replace_failure_restores_all_last_good_files(
    tmp_path, fail_at
):
    paths = PackPaths(tmp_path)
    targets = [
        _pack_path(paths, "Vendor", "Pack", "1.0.0"),
        paths.index_file,
        paths.aliases_file,
    ]
    old_values = [b"old-pack", b'{"old":"index"}', b'{"old":"aliases"}']
    new_values = [b"new-pack", b'{"new":"index"}', b'{"new":"aliases"}']
    stage = paths.staging_dir / "failure-job"
    stage.mkdir(parents=True)
    replacements = []
    for index, (target, old, new) in enumerate(zip(targets, old_values, new_values)):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old)
        prepared = stage / "{}.prepared".format(index)
        prepared.write_bytes(new)
        replacements.append((prepared, target))

    calls = 0

    def failing_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("simulated replace failure")
        os.replace(source, destination)

    with pytest.raises(OSError, match="simulated"):
        pack_worker_module._commit_transaction(
            paths,
            replacements,
            stage,
            _transaction_result(targets[0]),
            replace=failing_replace,
        )

    assert [target.read_bytes() for target in targets] == old_values
    assert not list(paths.root.rglob("*.prepared"))
    assert not list(paths.root.rglob("*.backup"))
    assert not (paths.root / "pack-transaction.json").exists()


def test_recover_transaction_restores_interrupted_journal(tmp_path):
    paths = PackPaths(tmp_path)
    target = paths.data_dir / "Vendor" / "Pack" / "1.0.0.pack"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    stage = paths.staging_dir / "recovery-job"
    stage.mkdir(parents=True)
    prepared = stage / "prepared.pack"
    prepared.write_bytes(b"new")
    backup = stage / "target.backup"
    journal = paths.root / "pack-transaction.json"
    journal.write_text(
        json.dumps(
            {
                "phase": "committing",
                "staging_dir": str(stage.resolve()),
                "result": _transaction_result(target),
                "entries": [
                    {
                        "target": str(target.resolve()),
                        "prepared": str(prepared.resolve()),
                        "backup": str(backup.resolve()),
                        "original_exists": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    os.replace(target, backup)
    os.replace(prepared, target)

    pack_worker_module._recover_transaction(paths)

    assert target.read_bytes() == b"old"
    assert not prepared.exists()
    assert not backup.exists()
    assert not journal.exists()


def test_recover_committed_transaction_removes_legacy_readonly_backup(tmp_path):
    paths = PackPaths(tmp_path)
    target = _pack_path(paths, "Test", "Pack", "1.0.0")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"new")
    paths.state_file.write_text(
        json.dumps(
            {
                "installed": {
                    "Test.Pack": {"1.0.0": str(target.resolve())},
                }
            }
        ),
        encoding="utf-8",
    )
    stage = paths.staging_dir / "committed-readonly-job"
    stage.mkdir(parents=True)
    prepared = stage / "prepared.pack"
    backup = stage / "target.backup"
    backup.write_bytes(b"old")
    backup.chmod(stat.S_IREAD)
    journal = paths.root / "pack-transaction.json"
    journal.write_text(
        json.dumps(
            {
                "phase": "committed",
                "staging_dir": str(stage.resolve()),
                "result": _transaction_result(target),
                "entries": [
                    {
                        "target": str(target.resolve()),
                        "prepared": str(prepared.resolve()),
                        "backup": str(backup.resolve()),
                        "original_exists": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        pack_worker_module.recover_pending_transaction(paths)

        assert target.read_bytes() == b"new"
        assert not backup.exists()
        assert not journal.exists()
    finally:
        if backup.exists():
            backup.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_failed_rollback_keeps_journal_for_next_worker_recovery(tmp_path):
    paths = PackPaths(tmp_path)
    first = paths.data_dir / "first.pack"
    second = paths.index_file
    stage = paths.staging_dir / "rollback-job"
    stage.mkdir(parents=True)
    replacements = []
    for index, (target, old, new) in enumerate(
        (
            (first, b"old-first", b"new-first"),
            (second, b"old-second", b"new-second"),
        )
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old)
        temporary = stage / "{}.prepared".format(index)
        temporary.write_bytes(new)
        replacements.append((temporary, target))

    calls = 0

    def unavailable_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("replace unavailable")
        os.replace(source, destination)

    with pytest.raises(OSError, match="unavailable"):
        pack_worker_module._commit_transaction(
            paths,
            replacements,
            stage,
            _transaction_result(first),
            replace=unavailable_replace,
        )

    journal = paths.root / "pack-transaction.json"
    assert journal.is_file()

    pack_worker_module._recover_transaction(paths)

    assert first.read_bytes() == b"old-first"
    assert second.read_bytes() == b"old-second"
    assert not journal.exists()


def test_transaction_journal_write_failure_removes_prepared_files(
    monkeypatch, tmp_path
):
    paths = PackPaths(tmp_path)
    target = paths.data_dir / "target.pack"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    stage = paths.staging_dir / "journal-failure-job"
    stage.mkdir(parents=True)
    prepared = stage / "target.prepared"
    prepared.write_bytes(b"new")

    def fail_journal(path, value):
        raise OSError("journal unavailable")

    monkeypatch.setattr(
        pack_worker_module, "_write_transaction_journal", fail_journal
    )

    with pytest.raises(OSError, match="journal"):
        pack_worker_module._commit_transaction(
            paths, [(prepared, target)], stage, _transaction_result(target)
        )

    assert target.read_bytes() == b"old"
    assert not prepared.exists()
    assert not (paths.root / "pack-transaction.json").exists()


def test_prepared_files_exist_only_inside_unique_staging(tmp_path):
    paths = PackPaths(tmp_path)
    stage = paths.staging_dir / "known-job"
    stage_data = stage / "data"
    stage_index = stage / "index"
    stage_data.mkdir(parents=True)
    stage_index.mkdir()
    staged_pack = stage_data / "source.pack"
    staged_pack.write_bytes(b"new-pack")
    details = {
        "from_pack": {"vendor": "Vendor", "pack": "Pack", "version": "1.0.0"}
    }
    stage_index.joinpath("index.json").write_text(
        json.dumps({"DEVICE": details}), encoding="utf-8"
    )
    stage_index.joinpath("aliases.json").write_text("{}", encoding="utf-8")
    destination = _pack_path(paths, "Vendor", "Pack", "1.0.0")

    replacements = pack_worker_module._prepare_pack_transaction(
        staged_pack,
        stage_data,
        destination,
        stage_index,
        paths,
        ("Vendor", "Pack", "1.0.0"),
    )

    assert replacements
    assert all(
        stage.resolve() in Path(prepared).resolve().parents
        for prepared, _ in replacements
    )
    assert not list(paths.data_dir.rglob("*.prepared"))
    assert not list(paths.index_dir.rglob("*.prepared"))

    worker = SubprocessPackWorker(paths)
    worker._active_staging_dir = stage.resolve()
    worker._cleanup_active_staging()
    assert not stage.exists()


def test_parent_recovery_restores_committing_transaction_immediately(tmp_path):
    paths = PackPaths(tmp_path)
    stage = paths.staging_dir / "interrupted-job"
    stage.mkdir(parents=True)
    old_target = paths.data_dir / "old.pack"
    new_target = paths.index_file
    old_target.parent.mkdir(parents=True)
    new_target.parent.mkdir(parents=True)
    old_target.write_bytes(b"old-bytes")
    old_prepared = stage / "old.prepared"
    old_backup = stage / "old.backup"
    new_prepared = stage / "new.prepared"
    new_backup = stage / "new.backup"
    old_prepared.write_bytes(b"replacement")
    new_prepared.write_bytes(b"new-target")
    os.replace(old_target, old_backup)
    os.replace(old_prepared, old_target)
    os.replace(new_prepared, new_target)
    journal = paths.root / "pack-transaction.json"
    journal.write_text(
        json.dumps(
            {
                "phase": "committing",
                "staging_dir": str(stage.resolve()),
                "result": _transaction_result(old_target),
                "entries": [
                    {
                        "target": str(old_target.resolve()),
                        "prepared": str(old_prepared.resolve()),
                        "backup": str(old_backup.resolve()),
                        "original_exists": True,
                    },
                    {
                        "target": str(new_target.resolve()),
                        "prepared": str(new_prepared.resolve()),
                        "backup": str(new_backup.resolve()),
                        "original_exists": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    pack_worker_module.recover_pending_transaction(paths)

    assert old_target.read_bytes() == b"old-bytes"
    assert not new_target.exists()
    assert not old_backup.exists()
    assert not journal.exists()


def test_subprocess_cancel_recovers_before_cleaning_staging(
    monkeypatch, tmp_path
):
    paths = PackPaths(tmp_path)
    stage = paths.staging_dir / "cancel-job"
    stage.mkdir(parents=True)
    calls = []

    def recover(recovery_paths, **kwargs):
        calls.append(("recover", stage.exists(), recovery_paths.root))

    monkeypatch.setattr(
        pack_worker_module, "recover_pending_transaction", recover, raising=False
    )

    class Process:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 1

        def wait(self, timeout=None):
            return self.returncode

    worker = SubprocessPackWorker(paths)
    worker._active_staging_dir = stage.resolve()
    worker._process = Process()

    worker.cancel()

    assert calls == [("recover", True, paths.root)]
    assert not stage.exists()


def test_pack_manager_cancel_preserves_staging_on_recovery_integrity_error(
    tmp_path,
):
    paths = PackPaths(tmp_path)
    stage = paths.staging_dir / "diagnostic-job"
    stage.mkdir(parents=True)
    (stage / "evidence").write_bytes(b"keep")

    class CorruptRecoveryWorker(FakeWorker):
        active_staging_dir = stage

        def cancel(self):
            raise FlashError(
                FlashErrorCode.PACK_INTEGRITY_ERROR,
                "corrupt transaction journal",
            )

    manager = PackManager(tmp_path, worker=CorruptRecoveryWorker())
    manager._active = True
    manager._phase = "worker"

    with pytest.raises(FlashError) as raised:
        manager.cancel()

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert (stage / "evidence").read_bytes() == b"keep"


def test_committed_result_wins_over_late_cancel(tmp_path):
    result = {
        "status": "installed",
        "pack_id": "Vendor.Pack",
        "version": "1.0.0",
        "pack_path": "committed.pack",
    }
    worker = SubprocessPackWorker(PackPaths(tmp_path))
    worker._committed_result = dict(result)
    worker._cancel_requested = True

    assert worker._finish_result("install", 1, None, "killed") == result
    assert worker._cancel_requested is False


def test_manager_acknowledges_state_or_rolls_back_on_state_failure(tmp_path):
    paths = PackPaths(tmp_path)
    pack_path = _pack_path(paths, "Vendor", "Pack", "1.0.0")
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(b"new")
    result = {
        "status": "installed",
        "pack_id": "Vendor.Pack",
        "version": "1.0.0",
        "pack_path": str(pack_path),
    }

    class HandshakeWorker(FakeWorker):
        def __init__(self):
            super().__init__()
            self.acked = []
            self.rolled_back = []

        def run(self, command, payload, on_event):
            return dict(result)

        def acknowledge_commit(self, value):
            self.acked.append(dict(value))

        def rollback_commit(self, value):
            self.rolled_back.append(dict(value))

    worker = HandshakeWorker()
    PackManager(tmp_path, worker=worker).install("DEVICE", lambda event: None)
    assert worker.acked == [result]
    assert worker.rolled_back == []

    paths.state_file.write_text("{broken", encoding="utf-8")
    failing_worker = HandshakeWorker()
    with pytest.raises(FlashError):
        PackManager(tmp_path, worker=failing_worker).install(
            "DEVICE", lambda event: None
        )
    assert failing_worker.acked == []
    assert failing_worker.rolled_back == [result]


@pytest.mark.parametrize("state_registered", [False, True])
def test_stale_committed_journal_rolls_back_unregistered_or_keeps_registered(
    tmp_path, state_registered
):
    paths = PackPaths(tmp_path)
    stage = paths.staging_dir / "committed-job"
    stage.mkdir(parents=True)
    target = _pack_path(paths, "Vendor", "Pack", "1.0.0")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"new")
    backup = stage / "000.backup"
    backup.write_bytes(b"old")
    prepared = stage / "pack.prepared"
    result = {
        "status": "installed",
        "pack_id": "Vendor.Pack",
        "version": "1.0.0",
        "pack_path": str(target),
    }
    (paths.root / "pack-transaction.json").write_text(
        json.dumps(
            {
                "phase": "committed",
                "staging_dir": str(stage.resolve()),
                "result": result,
                "entries": [
                    {
                        "target": str(target),
                        "prepared": str(prepared),
                        "backup": str(backup),
                        "original_exists": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    if state_registered:
        _write_installed_state(
            paths, {"Vendor.Pack": {"1.0.0": str(target)}}
        )

    pack_worker_module.recover_pending_transaction(paths)

    assert target.read_bytes() == (b"new" if state_registered else b"old")
    assert not (paths.root / "pack-transaction.json").exists()


def test_cancelled_broken_pipe_is_user_abort_and_next_run_succeeds(
    monkeypatch, tmp_path
):
    write_entered = threading.Event()
    release_write = threading.Event()

    class Error:
        def read(self):
            return ""

    class BrokenInput:
        def write(self, value):
            write_entered.set()
            release_write.wait(1)
            raise BrokenPipeError("cancelled pipe")

        def flush(self):
            pass

        def close(self):
            pass

    class GoodInput:
        def write(self, value):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class Process:
        def __init__(self, broken):
            self.stdin = BrokenInput() if broken else GoodInput()
            self.stderr = Error()
            self.returncode = None
            self.broken = broken
            self.stdout = iter([]) if broken else iter([
                json.dumps({"type": "result", "result": {
                    "status": "installed", "pack_id": "Vendor.Pack",
                    "version": "1.0.0", "pack_path": "ok.pack",
                }}) + "\n"
            ])

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 1

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    processes = [Process(True), Process(False)]
    popen_calls = []

    def popen(*args, **kwargs):
        process = processes[len(popen_calls)]
        popen_calls.append(process)
        return process

    monkeypatch.setattr(
        "mklink.cmsis_dap.pack_manager.subprocess.Popen", popen
    )
    worker = SubprocessPackWorker(PackPaths(tmp_path))
    errors = []

    def first_run():
        try:
            worker.run("install", {"part_number": "ONE"}, lambda event: None)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=first_run)
    thread.start()
    assert write_entered.wait(1)
    worker.cancel()
    release_write.set()
    thread.join(2)

    assert len(errors) == 1
    assert isinstance(errors[0], FlashError)
    assert errors[0].code is FlashErrorCode.USER_ABORT
    result = worker.run("install", {"part_number": "TWO"}, lambda event: None)
    assert result["status"] == "installed"
    assert len(popen_calls) == 2


def test_manager_cancel_accepts_committed_result_then_registers_and_acks(tmp_path):
    paths = PackPaths(tmp_path)
    pack_path = _pack_path(paths, "Vendor", "Pack", "1.0.0")
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(b"new-pack")
    stage = paths.staging_dir / "committed-job"
    stage.mkdir(parents=True)
    backup = stage / "000.backup"
    backup.write_bytes(b"old-pack")
    result = {
        "status": "installed",
        "pack_id": "Vendor.Pack",
        "version": "1.0.0",
        "pack_path": str(pack_path),
    }

    class CoordinatedCommittedWorker:
        active_staging_dir = stage

        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.lock = threading.Lock()
            self._committed = None
            self.acked = []

        @property
        def committed_result(self):
            with self.lock:
                return dict(self._committed) if self._committed is not None else None

        def run(self, command, payload, on_event):
            self.started.set()
            assert self.release.wait(2)
            return dict(result)

        def cancel(self):
            with self.lock:
                self._committed = dict(result)
            self.release.set()

        def acknowledge_commit(self, value):
            state = json.loads(paths.state_file.read_text(encoding="utf-8"))
            assert state["installed"]["Vendor.Pack"]["1.0.0"] == str(pack_path)
            assert backup.read_bytes() == b"old-pack"
            self.acked.append(dict(value))
            backup.unlink()
            stage.rmdir()
            paths.staging_dir.rmdir()

    worker = CoordinatedCommittedWorker()
    manager = PackManager(tmp_path, worker=worker)
    results = []
    errors = []

    def install():
        try:
            results.append(manager.install("DEVICE", lambda event: None))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=install)
    thread.start()
    assert worker.started.wait(1)
    manager.cancel()
    thread.join(2)

    assert errors == []
    assert results == [result]
    assert worker.acked == [result]
    assert not paths.staging_dir.exists()


def test_late_cancel_during_registration_is_noop_and_next_install_succeeds(
    tmp_path,
):
    paths = PackPaths(tmp_path)
    pack_path = _pack_path(paths, "Vendor", "Pack", "1.0.0")
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(b"pack")
    result = {
        "status": "installed",
        "pack_id": "Vendor.Pack",
        "version": "1.0.0",
        "pack_path": str(pack_path),
    }

    class LatchingWorker:
        def __init__(self):
            self.lock = threading.Lock()
            self.stale_cancel = False
            self.cancel_calls = 0
            self.acks = 0

        def run(self, command, payload, on_event):
            with self.lock:
                if self.stale_cancel:
                    self.stale_cancel = False
                    raise FlashError(FlashErrorCode.USER_ABORT, "stale cancel")
            return dict(result)

        def cancel(self):
            with self.lock:
                self.cancel_calls += 1
                self.stale_cancel = True

        def acknowledge_commit(self, value):
            self.acks += 1

    worker = LatchingWorker()
    manager = PackManager(tmp_path, worker=worker)
    registration_entered = threading.Event()
    release_registration = threading.Event()
    original_write_state = manager._write_state

    def blocked_write_state(state):
        registration_entered.set()
        assert release_registration.wait(2)
        original_write_state(state)

    manager._write_state = blocked_write_state
    first_results = []
    first_errors = []

    def first_install():
        try:
            first_results.append(manager.install("DEVICE", lambda event: None))
        except BaseException as error:
            first_errors.append(error)

    thread = threading.Thread(target=first_install)
    thread.start()
    assert registration_entered.wait(1)
    manager.cancel()
    release_registration.set()
    thread.join(2)

    assert first_errors == []
    assert first_results == [result]
    assert worker.cancel_calls == 0
    assert worker.acks == 1

    second = manager.install("DEVICE", lambda event: None)
    assert second == result
    assert worker.acks == 2


def test_ack_consumes_worker_latch_from_process_end_transition(
    monkeypatch, tmp_path
):
    paths = PackPaths(tmp_path)
    target = _pack_path(paths, "Vendor", "Pack", "1.0.0")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"new")
    stage = paths.staging_dir / "transition-job"
    stage.mkdir(parents=True)
    backup = stage / "000.backup"
    backup.write_bytes(b"old")
    result = {
        "status": "installed",
        "pack_id": "Vendor.Pack",
        "version": "1.0.0",
        "pack_path": str(target),
    }
    (paths.root / "pack-transaction.json").write_text(
        json.dumps(
            {
                "phase": "committed",
                "staging_dir": str(stage),
                "result": result,
                "entries": [{
                    "target": str(target), "prepared": str(stage / "prepared"),
                    "backup": str(backup), "original_exists": True,
                }],
            }
        ),
        encoding="utf-8",
    )
    worker = SubprocessPackWorker(paths)
    worker._active_staging_dir = stage.resolve()
    process_ended = threading.Event()
    release_result = threading.Event()

    def first_run(command, payload, on_event):
        with worker._lock:
            worker._committed_result = dict(result)
        process_ended.set()
        assert release_result.wait(2)
        return dict(result)

    worker.run = first_run
    manager = PackManager(tmp_path, worker=worker)
    results = []

    thread = threading.Thread(
        target=lambda: results.append(manager.install("DEVICE", lambda event: None))
    )
    thread.start()
    assert process_ended.wait(1)
    manager.cancel()
    release_result.set()
    thread.join(2)

    assert results == [result]
    assert worker._cancel_requested is False

    class Stream:
        def write(self, value): pass
        def flush(self): pass
        def close(self): pass
        def read(self): return ""

    class Process:
        stdin = Stream()
        stderr = Stream()
        returncode = None
        stdout = iter([json.dumps({"type": "result", "result": result}) + "\n"])
        def wait(self, timeout=None): self.returncode = 0; return 0
        def poll(self): return self.returncode

    monkeypatch.setattr(
        "mklink.cmsis_dap.pack_manager.subprocess.Popen",
        lambda *args, **kwargs: Process(),
    )
    del worker.run
    assert worker.run("install", {"part_number": "NEXT"}, lambda event: None) == result


def _make_directory_link(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    except OSError:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip("directory links unavailable: " + completed.stderr)


def test_remove_rejects_canonical_path_escaping_through_directory_link(tmp_path):
    paths = PackPaths(tmp_path / "managed")
    external = tmp_path / "external-vendor"
    external.mkdir()
    _make_directory_link(paths.data_dir / "Vendor", external)
    escaped = external / "Pack" / "1.0.0" / "Vendor.Pack.1.0.0.pack"
    escaped.parent.mkdir(parents=True)
    escaped.write_bytes(b"external")
    _write_installed_state(
        paths, {"Vendor.Pack": {"1.0.0": str(escaped.resolve())}}
    )
    state_before = paths.state_file.read_bytes()

    with pytest.raises(FlashError) as raised:
        PackManager(paths.root, worker=FakeWorker()).remove(
            "Vendor", "Pack", "1.0.0"
        )

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert escaped.read_bytes() == b"external"
    assert paths.state_file.read_bytes() == state_before


def test_register_rejects_result_escaping_through_directory_link(tmp_path):
    paths = PackPaths(tmp_path / "managed")
    external = tmp_path / "external-vendor"
    external.mkdir()
    _make_directory_link(paths.data_dir / "Vendor", external)
    escaped = external / "Pack" / "1.0.0" / "Vendor.Pack.1.0.0.pack"
    escaped.parent.mkdir(parents=True)
    escaped.write_bytes(b"external")

    class EscapedWorker(FakeWorker):
        def run(self, command, payload, on_event):
            return {
                "status": "installed",
                "pack_id": "Vendor.Pack",
                "version": "1.0.0",
                "pack_path": str(escaped.resolve()),
            }

    with pytest.raises(FlashError) as raised:
        PackManager(paths.root, worker=EscapedWorker()).install(
            "DEVICE", lambda event: None
        )

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert escaped.read_bytes() == b"external"
    assert not paths.state_file.exists()


def test_remove_rolls_pack_back_when_state_write_fails(tmp_path):
    paths = PackPaths(tmp_path)
    first = _pack_path(paths, "Vendor", "Pack", "1.0.0")
    second = _pack_path(paths, "Vendor", "Pack", "2.0.0")
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    _write_installed_state(
        paths,
        {"Vendor.Pack": {"1.0.0": str(first), "2.0.0": str(second)}},
    )
    state_before = paths.state_file.read_bytes()
    manager = PackManager(tmp_path, worker=FakeWorker())
    original_write_state = manager._write_state
    calls = 0

    def fail_once(state):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk full")
        original_write_state(state)

    manager._write_state = fail_once

    with pytest.raises(OSError, match="disk full"):
        manager.remove("Vendor", "Pack", "1.0.0")

    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert paths.state_file.read_bytes() == state_before
    assert not list(paths.root.rglob("*.backup"))
    assert not list(paths.root.rglob("*.tmp"))

    manager.remove("Vendor", "Pack", "1.0.0")

    assert not first.exists()
    assert second.read_bytes() == b"second"
    assert json.loads(paths.state_file.read_text(encoding="utf-8")) == {
        "installed": {"Vendor.Pack": {"2.0.0": str(second)}}
    }


def test_worker_validates_all_metadata_before_touching_last_good(tmp_path):
    paths = PackPaths(tmp_path)
    old_pack = _pack_path(paths, "Vendor", "Pack", "1.0.0")
    old_pack.parent.mkdir(parents=True)
    old_pack.write_bytes(b"old-pack")
    paths.index_dir.mkdir()
    paths.index_file.write_bytes(b'{"old":"index"}')
    paths.aliases_file.write_bytes(b'{"old":"aliases"}')

    class PackRef:
        vendor = "Vendor"
        pack = "Pack"
        version = "1.0.0"

        def get_pack_name(self):
            return str(Path("Vendor") / "Pack" / "1.0.0.pack")

    class MissingAliasesCache:
        def __init__(self, silent, no_timeouts, json_path, data_path, emitter):
            self.json_path = Path(json_path)
            self.data_path = Path(data_path)
            self._index = {
                "DEVICE": {
                    "from_pack": {
                        "vendor": "Vendor",
                        "pack": "Pack",
                        "version": "1.0.0",
                    }
                }
            }

        def cache_descriptors(self):
            self.json_path.joinpath("index.json").write_text(
                json.dumps(self._index), encoding="utf-8"
            )

        @property
        def index(self):
            return self._index

        def packs_for_devices(self, devices):
            return [PackRef()]

        def download_pack_list(self, refs):
            staged = self.data_path / refs[0].get_pack_name()
            staged.parent.mkdir(parents=True)
            staged.write_bytes(b"new-pack")

    with pytest.raises(pack_worker_module.WorkerFailure):
        handle_request(
            {
                "command": "install",
                "payload": {"part_number": "DEVICE"},
                "root": str(tmp_path),
                "staging_dir": str((paths.staging_dir / "validation-job").resolve()),
            },
            lambda event: None,
            cache_factory=MissingAliasesCache,
        )

    assert old_pack.read_bytes() == b"old-pack"
    assert paths.index_file.read_bytes() == b'{"old":"index"}'
    assert paths.aliases_file.read_bytes() == b'{"old":"aliases"}'
    assert not (paths.root / "pack-transaction.json").exists()


def test_canonical_pack_path_uses_identity_layout_without_duplicate_vendor(tmp_path):
    paths = PackPaths(tmp_path)

    result = pack_manager_module._canonical_pack_path(
        paths, "Vendor", "Vendor.Device_DFP", "1.2.3"
    )

    assert result == (
        paths.data_dir
        / "Vendor"
        / "Device_DFP"
        / "1.2.3"
        / "Vendor.Device_DFP.1.2.3.pack"
    ).resolve()


@pytest.mark.parametrize(
    "vendor,pack,version",
    [
        ("", "Pack", "1.0.0"),
        (".", "Pack", "1.0.0"),
        ("Vendor", "..", "1.0.0"),
        ("Vendor/escape", "Pack", "1.0.0"),
        ("Vendor", "Pack\\escape", "1.0.0"),
        ("Vendor", "Pack", "C:\\escape"),
    ],
)
def test_canonical_pack_path_rejects_unsafe_identity_segments(
    tmp_path, vendor, pack, version
):
    with pytest.raises(ValueError):
        pack_manager_module._canonical_pack_path(
            PackPaths(tmp_path), vendor, pack, version
        )


def test_manager_rejects_result_path_for_another_in_tree_pack(tmp_path):
    paths = PackPaths(tmp_path)
    wrong = (
        paths.data_dir
        / "Vendor"
        / "Other"
        / "1.0.0"
        / "Vendor.Other.1.0.0.pack"
    )
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(b"unrelated")

    class WrongPathWorker(FakeWorker):
        def run(self, command, payload, on_event):
            return {
                "status": "installed",
                "pack_id": "Vendor.Pack",
                "version": "1.0.0",
                "pack_path": str(wrong),
            }

    with pytest.raises(FlashError) as raised:
        PackManager(tmp_path, worker=WrongPathWorker()).install(
            "DEVICE", lambda event: None
        )

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert wrong.read_bytes() == b"unrelated"
    assert not paths.state_file.exists()


def test_remove_refuses_state_pointing_to_another_in_tree_pack(tmp_path):
    paths = PackPaths(tmp_path)
    expected = (
        paths.data_dir
        / "Vendor"
        / "Pack"
        / "1.0.0"
        / "Vendor.Pack.1.0.0.pack"
    )
    unrelated = (
        paths.data_dir
        / "Vendor"
        / "Other"
        / "1.0.0"
        / "Vendor.Other.1.0.0.pack"
    )
    expected.parent.mkdir(parents=True)
    unrelated.parent.mkdir(parents=True)
    expected.write_bytes(b"expected")
    unrelated.write_bytes(b"unrelated")
    _write_installed_state(
        paths, {"Vendor.Pack": {"1.0.0": str(unrelated)}}
    )
    before = paths.state_file.read_bytes()

    with pytest.raises(FlashError) as raised:
        PackManager(tmp_path, worker=FakeWorker()).remove(
            "Vendor", "Pack", "1.0.0"
        )

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert expected.read_bytes() == b"expected"
    assert unrelated.read_bytes() == b"unrelated"
    assert paths.state_file.read_bytes() == before


def test_remove_never_recursively_deletes_registered_version_directory(tmp_path):
    paths = PackPaths(tmp_path)
    version_dir = paths.data_dir / "Vendor" / "Pack" / "1.0.0"
    version_dir.mkdir(parents=True)
    arbitrary = version_dir / "user-file.txt"
    arbitrary.write_bytes(b"user")
    _write_installed_state(
        paths, {"Vendor.Pack": {"1.0.0": str(version_dir)}}
    )
    before = paths.state_file.read_bytes()

    with pytest.raises(FlashError) as raised:
        PackManager(tmp_path, worker=FakeWorker()).remove(
            "Vendor", "Pack", "1.0.0"
        )

    assert raised.value.code is FlashErrorCode.PACK_INTEGRITY_ERROR
    assert arbitrary.read_bytes() == b"user"
    assert paths.state_file.read_bytes() == before
