"""Exercise pyOCD's real core-disconnect sequence with register fixtures."""

from types import SimpleNamespace

import pytest

from mklink.cmsis_dap.backend import PyOcdBackend, _TraceStateDelegate
from mklink.cmsis_dap.errors import FlashError


TRCENA = 1 << 24
DEMCR = 0xE000EDFC


class Core:
    def __init__(self, delegate, value=TRCENA | 1):
        self.delegate = delegate
        self.value = value
        self.writes = []
        self.cleanup = []
        self.fail_read = False
        self.fail_restore = False
        self.bp_manager = SimpleNamespace(remove_all_breakpoints=lambda: self.cleanup.append("breakpoints"))
        self.dwt = SimpleNamespace(remove_all_watchpoints=lambda: self.cleanup.append("watchpoints"))

    def call_delegate(self, name, **kwargs):
        callback = getattr(self.delegate, name, None)
        return callback(**kwargs) if callback else None

    def read32(self, address):
        assert address == DEMCR
        if self.fail_read:
            raise RuntimeError("probe read lost")
        return self.value

    def write32(self, address, value):
        self.writes.append((address, value))
        if address == DEMCR:
            if value & TRCENA and self.fail_restore:
                raise RuntimeError("probe write lost")
            self.value = value

    def flush(self):
        self.cleanup.append("flush")

    def resume(self):
        self.cleanup.append("resume")

    def stop_debug_core_hook(self):
        return False

    def disconnect(self, resume=True):
        from pyocd.coresight.cortex_m import CortexM

        CortexM.disconnect(self, resume)


@pytest.mark.parametrize("enabled,initial,expected", [
    (True, TRCENA | 1, TRCENA),
    (True, 1, 0),
    (False, TRCENA | 1, 0),
])
def test_real_disconnect_preserves_only_observed_trace_bit(enabled, initial, expected):
    delegate = _TraceStateDelegate()
    delegate.enabled = enabled
    core = Core(delegate, initial)
    core.disconnect()
    assert core.value == expected
    assert core.cleanup[:3] == ["breakpoints", "watchpoints", "resume"]
    assert not delegate.errors
    # No DWT reset or access to an application variable.
    assert {address for address, _ in core.writes} <= {DEMCR, 0xE000EDF0}


def test_halted_disconnect_does_not_write_trace_or_resume():
    delegate = _TraceStateDelegate()
    delegate.enabled = True
    core = Core(delegate)
    core.disconnect(resume=False)
    assert core.value == TRCENA | 1
    assert core.writes == []
    assert "resume" not in core.cleanup


def test_trace_snapshot_is_per_core_and_consumed_once():
    delegate = _TraceStateDelegate()
    delegate.enabled = True
    first, second = Core(delegate), Core(delegate, 0)
    delegate.will_stop_debug_core(first)
    delegate.will_stop_debug_core(second)
    first.value = second.value = 0
    delegate.did_stop_debug_core(second)
    delegate.did_stop_debug_core(first)
    assert first.value == TRCENA and second.value == 0
    first.value = 0
    delegate.did_stop_debug_core(first)
    assert first.value == 0


def test_existing_delegate_callbacks_and_post_cleanup_bits_are_preserved():
    calls = []
    def after(core):
        calls.append("after")
        core.value = 1 << 16
    previous = SimpleNamespace(
        will_stop_debug_core=lambda core: calls.append("before"),
        did_stop_debug_core=after,
        did_reset=lambda: "existing",
    )
    delegate = _TraceStateDelegate(previous)
    delegate.enabled = True
    core = Core(delegate)
    core.disconnect()
    assert calls == ["before", "after"]
    assert core.value == TRCENA | (1 << 16)
    assert delegate.did_reset() == "existing"


class Session:
    def __init__(self):
        self.delegate = None
        self.core = None
        self.closed = False
        self.fail_reset = False
        self.target = SimpleNamespace(reset=self.reset)

    def open(self):
        self.core = Core(self.delegate)

    def reset(self, *_args):
        if self.fail_reset:
            raise RuntimeError("reset failed")
        self.core.value = TRCENA | 1

    def close(self):
        self.core.disconnect()
        self.closed = True


def backend_session():
    session = Session()
    backend = PyOcdBackend(session_factory=lambda probe, options: session)
    backend.connect(object(), "STM32F103RE", 1_000_000)
    return backend, session


def test_successful_reset_arms_teardown_preservation():
    backend, session = backend_session()
    assert not session.delegate.enabled
    backend.reset_run()
    assert session.delegate.enabled
    backend.disconnect()
    assert session.closed and session.core.value == TRCENA


@pytest.mark.parametrize("scenario", ["no_reset", "failed_reset", "security"])
def test_unqualified_operations_do_not_arm_trace_preservation(scenario):
    backend, session = backend_session()
    if scenario == "failed_reset":
        backend.reset_run()
        session.fail_reset = True
        with pytest.raises(FlashError):
            backend.reset_run()
    elif scenario == "security":
        backend._security_family = "stm32f103-rdp1"
        backend.reset_run()
    backend.disconnect()
    assert session.closed and session.core.value == 0


@pytest.mark.parametrize("failure", ["read", "restore"])
def test_trace_access_failure_is_reported_after_probe_cleanup(failure):
    backend, session = backend_session()
    backend.reset_run()
    session.core.fail_read = failure == "read"
    session.core.fail_restore = failure == "restore"
    with pytest.raises(FlashError, match="target trace state"):
        backend.disconnect()
    assert session.closed
    assert backend._session is None
    backend.disconnect()
