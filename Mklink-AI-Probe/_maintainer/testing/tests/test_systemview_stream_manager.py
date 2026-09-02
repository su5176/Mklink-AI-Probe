"""SystemViewStreamManager 轮询循环状态机回归测试。

背景:GUI 长会话曾因三个叠加缺陷在 ~5-14s 内必然死亡(空读即重启、看门狗
从会话起点固定起算、任务名解析反复拆流)。修复后语义:
- 单一空闲看门狗以"最后一次收到数据"为基准,统一裁决空读与读异常;
- 会话总时长(duration)与空闲看门狗分离,不再被任何恢复动作延长;
- 首次启动重试按 Device 连接代次限为一次,并重置解析代际;
- 任务名解析一次空结果即永久禁用,stop/start 失败如实上抛。

用 fake device + 缩短的看门狗超时覆盖关键状态机分支。
"""

import threading
import time

import pytest

from mklink.remote.dashboards import SystemViewStreamManager


class FakeDevice:
    """按脚本吐数据的假设备:bytes=数据、Exception=读异常、callable=动态。"""

    def __init__(self, script=(), *, start_script=(), stop_script=(), names=None):
        self._lock = threading.Lock()
        self._script = list(script)
        self._start_script = list(start_script)
        self._stop_script = list(stop_script)
        self._names = {} if names is None else dict(names)
        self.start_calls = 0
        self.stop_calls = 0
        self.resolve_calls = 0

    def systemview_start(self, addr=None, channel=1, mode=0, search_size=1024):
        self.start_calls += 1
        if self._start_script:
            item = self._start_script.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item()
            return item
        return {"control_block_addr": addr or "0x20000000"}

    def systemview_stop(self):
        self.stop_calls += 1
        if self._stop_script:
            item = self._stop_script.pop(0)
            if isinstance(item, Exception):
                raise item

    def systemview_read_bytes(self, duration=1.0, max_bytes=None):
        with self._lock:
            if self._script:
                item = self._script[0]
                if not callable(item):
                    # bytes/Exception 一次性消费;callable 常驻(每次调用
                    # 重新求值,用于"持续异常/持续数据"场景)。
                    self._script.pop(0)
            else:
                item = b""
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item()
        return item

    def systemview_resolve_task_names(self, task_ids):
        self.resolve_calls += 1
        return {task_id: self._names[task_id]
                for task_id in task_ids if task_id in self._names}


class NoEventParser:
    """State-machine fixture: raw bytes arrive but never imply task IDs."""

    synced = False
    abs_time = 0
    cpu_freq = 0
    dropped_bytes = 0
    dropped_packets = 0
    _ram_base = 0x20000000

    def __init__(self):
        self._task_names = {}
        self._isr_names = {}

    @staticmethod
    def feed(_raw):
        return []


def _make_manager(timeout=0.25, *, real_parser=False):
    manager = SystemViewStreamManager()
    # 缩短看门狗加速测试;禁用"有原始字节但解不出事件"检查(测试喂的是
    # 无法解码的填充字节)。
    manager._startup_no_data_timeout_s = timeout
    manager._startup_progress_timeout_s = 999.0
    if not real_parser:
        manager._create_parser = lambda _device=None: NoEventParser()
    return manager


def _wait_terminal(manager, deadline=3.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if not manager.running:
            return True
        time.sleep(0.02)
    return False


def test_never_any_data_fails_with_startup_hint_and_no_restart():
    """场景(a):错误地址等导致从未收到数据 → 按启动超时报错,不重启。"""
    manager = _make_manager()
    device = FakeDevice()
    manager.start(device, addr="0x20000000", channel=1, duration=30)
    assert _wait_terminal(manager)
    manager.stop()
    assert not manager.running
    assert "启动后未收到数据" in (manager._progress_error or "")
    assert device.start_calls == 1
    # finally 里的一次 stop 之外,不应有恢复性 stop/start。
    assert device.stop_calls == 1


def test_data_then_permanent_silence_reports_stream_stalled():
    """场景(b):健康后永久静 → 自动重试一次后仍静默 → 报"数据流已中断"。"""
    manager = _make_manager()
    device = FakeDevice([b"\x01" * 64])
    manager.start(device, addr="0x20000000", channel=1, duration=30)
    assert _wait_terminal(manager, deadline=5.0)
    manager.stop()
    assert "自动重试后仍未收到数据" in (manager._progress_error or "")
    assert device.start_calls == 2  # 一次自动重试
    assert device.stop_calls == 2
    status = manager.get_status()
    assert status["auto_retry_count"] == 1
    assert status["auto_retry_reason"] == "first_start_data_then_idle"
    assert status["first_start_on_connection"] is True


def test_transient_read_exception_does_not_kill_healthy_stream():
    """场景(c):长会话中单次瞬时读异常不再立即杀流。"""
    manager = _make_manager()
    reads = iter([
        b"\x01" * 64,
        RuntimeError("transient CDC hiccup"),
        b"\x02" * 64,
    ])

    def healthy_after_hiccup():
        try:
            item = next(reads)
        except StopIteration:
            return b"\x03" * 32
        if isinstance(item, Exception):
            raise item
        return item

    device = FakeDevice([healthy_after_hiccup])
    manager.start(device, addr="0x20000000", channel=1, duration=0.35)
    assert _wait_terminal(manager)
    manager.stop()
    assert manager._progress_state == "stopped"
    assert manager._progress_error == ""
    assert manager._stats["bytes"] >= 128
    assert device.start_calls == 1


def test_persistent_read_exceptions_fail_with_real_cause():
    """场景(d):持续读异常 → 如实报读取失败并携带底层异常信息。"""
    manager = _make_manager()

    def _always_raise():
        raise RuntimeError("probe gone")

    device = FakeDevice([_always_raise])
    manager.start(device, addr="0x20000000", channel=1, duration=30)
    assert _wait_terminal(manager)
    manager.stop()
    assert "读取失败" in (manager._progress_error or "")
    assert "probe gone" in (manager._progress_error or "")


def test_short_silences_and_duration_deadline_end_cleanly():
    """场景(d/e):短于阈值的间歇静默不重启;duration 到期正常收尾。"""
    manager = _make_manager(timeout=0.2)
    reads = 0

    def alternating():
        nonlocal reads
        reads += 1
        time.sleep(0.01)
        # 每三次读取制造一次真实空读,连续静默约 10ms,远小于 200ms 看门狗。
        return b"" if reads % 3 == 0 else b"\x03" * 32

    device = FakeDevice([alternating])
    manager.start(device, addr="0x20000000", channel=1, duration=0.35)
    end = time.monotonic() + 3.0
    while time.monotonic() < end and manager.running:
        time.sleep(0.02)
    manager.stop()
    assert not manager.running
    # duration 到期自然收尾(progress_state=stopped),无错误、无恢复性重启。
    assert manager._progress_state == "stopped"
    assert manager._progress_error == ""
    assert device.start_calls == 1
    assert device.stop_calls == 1
    assert manager._stats["bytes"] > 0


def test_first_start_reset_auto_retry_recovers_stream():
    """首次启动触发探针附着复位(START 突发后静默)→ 自动重试一次 → 流恢复。"""
    manager = _make_manager()
    device = FakeDevice()

    def after_retry_streams():
        # 第 2 次 start(自动重试)之后持续供数,模拟复位后 recorder 正常工作。
        return b"\x05" * 32 if device.start_calls >= 2 else b""

    device._script = [b"\x06" * 32, after_retry_streams]
    manager.start(device, addr="0x20000000", channel=1, duration=1.5)
    end = time.monotonic() + 5.0
    while time.monotonic() < end and manager.running:
        time.sleep(0.02)
    manager.stop()
    assert not manager.running
    # 重试救活后跑满 duration,正常收尾、无错误。
    assert manager._progress_state == "stopped"
    assert manager._progress_error == ""
    assert device.start_calls == 2  # 精确一次自动重试
    assert device.stop_calls == 2   # 重试前 stop + finally stop
    assert manager._stats["bytes"] > 64  # 失败突发已清除,只统计重试后的持续数据
    status = manager.get_status()
    assert status["auto_retry_count"] == 1
    assert status["connection_generation"] == 1
    assert status["session_generation"] == 1


def test_later_session_on_same_connection_does_not_auto_retry():
    """同一 Device 的后续会话永久静默时如实失败,不再伪装成首次附着。"""
    manager = _make_manager()
    device = FakeDevice([lambda: b"\x01" * 32])
    manager.start(device, duration=0.15)
    assert _wait_terminal(manager)
    manager.stop()
    assert device.start_calls == 1

    device._script = [b"\x02" * 32]
    manager.start(device, duration=30)
    assert _wait_terminal(manager)
    manager.stop()
    assert "数据流已中断" in manager._progress_error
    assert device.start_calls == 2
    assert manager.get_status()["auto_retry_count"] == 0
    assert manager.get_status()["first_start_on_connection"] is False


def test_new_device_connection_restores_first_start_retry():
    manager = _make_manager()
    first = FakeDevice([lambda: b"\x01" * 32])
    manager.start(first, duration=0.1)
    assert _wait_terminal(manager)
    manager.stop()

    second = FakeDevice([b"\x02" * 32])
    manager.start(second, duration=30)
    assert _wait_terminal(manager)
    manager.stop()
    assert second.start_calls == 2
    assert manager.get_status()["connection_generation"] == 2
    assert manager.get_status()["auto_retry_count"] == 1


def test_retry_stop_failure_is_visible_when_restart_recovers():
    manager = _make_manager()
    device = FakeDevice(stop_script=[RuntimeError("stop transport lost")])

    def after_retry_streams():
        return b"\x03" * 32 if device.start_calls >= 2 else b""

    device._script = [b"\x01" * 32, after_retry_streams]
    manager.start(device, duration=0.8)
    assert _wait_terminal(manager)
    manager.stop()
    status = manager.get_status()
    assert status["progress_error"] == ""
    assert status["auto_retry_count"] == 1
    assert status["auto_retry_stop_error"] == "stop transport lost"


def test_retry_start_failure_preserves_recovery_context():
    manager = _make_manager()
    device = FakeDevice(
        [b"\x01" * 32],
        start_script=[{}, RuntimeError("restart refused")],
        stop_script=[RuntimeError("stop failed")],
    )
    manager.start(device, duration=30)
    assert _wait_terminal(manager)
    manager.stop()
    assert "自动重试失败" in manager._progress_error
    assert "restart refused" in manager._progress_error
    assert "stop failed" in manager._progress_error


def test_retry_resets_undecodable_progress_generation():
    """重试前后的未解码字节不能跨 parser 代次累计。"""
    manager = _make_manager(timeout=0.2)
    manager._startup_progress_timeout_s = 0.05
    manager._startup_progress_min_bytes = 100

    class EmptyParser:
        synced = False
        abs_time = 0
        cpu_freq = 0
        dropped_bytes = 0
        dropped_packets = 0
        _task_names = {}
        _isr_names = {}

        @staticmethod
        def feed(_raw):
            return []

    manager._create_parser = lambda _device=None: EmptyParser()
    device = FakeDevice()

    def one_chunk_per_attempt():
        if device.start_calls == 1:
            return b""
        if device.start_calls == 2 and not getattr(device, "_retry_chunk", False):
            device._retry_chunk = True
            return b"\x02" * 64
        return b""

    device._script = [b"\x01" * 64, one_chunk_per_attempt]
    manager.start(device, duration=30)
    assert _wait_terminal(manager)
    manager.stop()
    assert "no decodable events" not in manager._progress_error
    assert manager._raw_bytes_without_events == 64


def _prepare_name_resolution(manager, device, names=None):
    manager._parser = manager._create_parser()
    manager._parser._task_names.clear()
    manager._last_name_resolution = 0.0
    manager._name_resolution_attempted.clear()
    manager._name_resolution_disabled = False
    if names is not None:
        device._names = dict(names)
    return [{"kind": "task_start_exec", "task_id": 0x20001000}]


def test_empty_task_name_resolution_disables_future_stream_restarts():
    manager = _make_manager(real_parser=True)
    device = FakeDevice()
    events = _prepare_name_resolution(manager, device)
    restarted = manager._maybe_resolve_task_names(
        device, events, addr="0x20000000", channel=1, mode=0,
        search_size=1024,
    )
    assert restarted is True
    assert manager._name_resolution_disabled is True
    assert device.resolve_calls == 1
    assert manager._maybe_resolve_task_names(
        device, events, addr="0x20000000", channel=1, mode=0,
        search_size=1024,
    ) is False


def test_successful_task_name_resolution_keeps_fallback_enabled():
    manager = _make_manager(real_parser=True)
    device = FakeDevice(names={0x20001000: "worker"})
    events = _prepare_name_resolution(manager, device)
    assert manager._maybe_resolve_task_names(
        device, events, addr="0x20000000", channel=1, mode=0,
        search_size=1024,
    ) is True
    assert manager._name_resolution_disabled is False
    assert manager._parser._task_names[0x20001000] == "worker"


def test_task_name_resolution_stop_failure_keeps_real_cause():
    manager = _make_manager(real_parser=True)
    device = FakeDevice(stop_script=[RuntimeError("stop failed")])
    events = _prepare_name_resolution(manager, device)
    with pytest.raises(RuntimeError, match="停止失败: stop failed"):
        manager._maybe_resolve_task_names(
            device, events, addr="0x20000000", channel=1, mode=0,
            search_size=1024,
        )


def test_task_name_resolution_restart_failure_keeps_real_cause():
    manager = _make_manager(real_parser=True)
    device = FakeDevice(start_script=[RuntimeError("start failed")])
    events = _prepare_name_resolution(manager, device)
    with pytest.raises(RuntimeError, match="重启失败: start failed"):
        manager._maybe_resolve_task_names(
            device, events, addr="0x20000000", channel=1, mode=0,
            search_size=1024,
        )
