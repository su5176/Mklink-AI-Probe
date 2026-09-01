import ast
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from mklink.cmsis_dap import pyocd_runtime


class ObservableRLock:
    def __init__(self):
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._owner = None
        self._depth = 0
        self.waiter_blocked = threading.Event()

    def __enter__(self):
        identity = threading.get_ident()
        with self._state_lock:
            if self._owner not in (None, identity):
                self.waiter_blocked.set()
        self._lock.acquire()
        with self._state_lock:
            if self._owner == identity:
                self._depth += 1
            else:
                self._owner = identity
                self._depth = 1
        return self

    def __exit__(self, *_args):
        with self._state_lock:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
        self._lock.release()


def test_online_flash_dependencies_are_importable():
    import intelhex
    import pyocd
    import cmsis_pack_manager

    assert intelhex is not None
    assert pyocd.__version__ == version("pyocd")
    assert cmsis_pack_manager.Cache is not None


def test_lazy_pyocd_imports_are_serialized(monkeypatch):
    observed_lock = ObservableRLock()
    first_entered = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    calls = []
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def fake_import(name):
        nonlocal active, max_active
        with state_lock:
            calls.append(name)
            active += 1
            max_active = max(max_active, active)
        try:
            if name == "pyocd.first":
                first_entered.set()
                assert release_first.wait(2)
            return name
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(pyocd_runtime, "_PYOCD_IMPORT_LOCK", observed_lock)
    monkeypatch.setattr(pyocd_runtime, "_import_module", fake_import)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(pyocd_runtime.import_pyocd_module, "pyocd.first")
        assert first_entered.wait(2)

        def import_second():
            second_started.set()
            return pyocd_runtime.import_pyocd_module("pyocd.second")

        second = executor.submit(import_second)
        assert second_started.wait(2)
        assert observed_lock.waiter_blocked.wait(2)
        assert calls == ["pyocd.first"]
        release_first.set()
        assert first.result(timeout=2) == "pyocd.first"
        assert second.result(timeout=2) == "pyocd.second"

    assert calls == ["pyocd.first", "pyocd.second"]
    assert max_active == 1


def test_lazy_pyocd_import_lock_recovers_after_failure(monkeypatch):
    sentinel = object()
    attempts = 0

    def flaky_import(_name):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ImportError("first import failed")
        return sentinel

    monkeypatch.setattr(pyocd_runtime, "_import_module", flaky_import)
    with pytest.raises(ImportError, match="first import failed"):
        pyocd_runtime.import_pyocd_module("pyocd.target")

    assert pyocd_runtime.import_pyocd_module("pyocd.target") is sentinel
    assert attempts == 2


def test_serialized_importer_rejects_non_pyocd_modules():
    with pytest.raises(ValueError, match="Only pyOCD"):
        pyocd_runtime.import_pyocd_module("usb.core")


def test_production_code_does_not_bypass_serialized_pyocd_importer():
    root = Path(__file__).resolve().parents[3]
    direct_imports = []
    for path in (root / "mklink").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "pyocd" or name.startswith("pyocd.") for name in names):
                direct_imports.append("{}:{}".format(path.relative_to(root), node.lineno))

    assert direct_imports == []


def test_real_pyocd_first_imports_are_safe_in_parallel_subprocess():
    script = """
from concurrent.futures import ThreadPoolExecutor
from mklink.cmsis_dap.pyocd_runtime import import_pyocd_attr

with ThreadPoolExecutor(max_workers=2) as executor:
    probe = executor.submit(
        import_pyocd_attr,
        'pyocd.probe.aggregator',
        'DebugProbeAggregator',
    )
    target = executor.submit(import_pyocd_attr, 'pyocd.target', 'TARGET')
    assert probe.result(timeout=20).__name__ == 'DebugProbeAggregator'
    assert target.result(timeout=20) is not None
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)
