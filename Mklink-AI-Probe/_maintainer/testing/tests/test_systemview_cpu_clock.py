from mklink._types import DeviceState
from mklink.device import Device
from mklink.profiles import load_mcu_profiles
from mklink.remote.dashboards import SystemViewStreamManager


def test_systemview_start_reads_system_core_clock_before_stream(monkeypatch):
    calls = []

    class FakeBridge:
        state = DeviceState.READY

    class FakeSystemViewSession:
        _running = False

        def __init__(self, bridge, channel=1):
            self._bridge = bridge

        def start(self, addr, search_size=1024, project_root=".", *, mode=0):
            calls.append(("start", self._bridge.state))
            self._bridge.state = DeviceState.SYSTEMVIEW_STREAM
            self._running = True
            return {"control_block_addr": "0x20000000"}

        def stop(self):
            self._running = False

    monkeypatch.setattr("mklink.systemview.SystemViewSession", FakeSystemViewSession)

    dev = Device(project_root=".")
    dev._bridge = FakeBridge()
    dev._connected = True
    dev._dwarf_info = object()

    def read_variable(name):
        calls.append(("read_variable", dev._bridge.state))
        if dev._bridge.state != DeviceState.READY:
            raise AssertionError("SystemCoreClock must be read before stream mode")
        assert name == "SystemCoreClock"
        return 72_000_000

    monkeypatch.setattr(dev, "read_variable", read_variable)

    result = dev.systemview_start()

    assert result["cpu_freq_hint"] == 72_000_000
    assert dev._systemview_parser.cpu_freq == 72_000_000
    assert calls == [
        ("read_variable", DeviceState.READY),
        ("start", DeviceState.READY),
    ]


def test_systemview_start_reads_hpm_core_clock_before_project_default(tmp_path, monkeypatch):
    calls = []

    class FakeBridge:
        state = DeviceState.READY
        idcode = 0
        current_mcu = ""

    class FakeSystemViewSession:
        _running = False

        def __init__(self, bridge, channel=1):
            self._bridge = bridge

        def start(self, addr, search_size=1024, project_root=".", *, mode=0):
            calls.append(("start", self._bridge.state))
            self._bridge.state = DeviceState.SYSTEMVIEW_STREAM
            self._running = True
            return {"control_block_addr": "0x0008e488"}

        def stop(self):
            self._running = False

    monkeypatch.setattr("mklink.systemview.SystemViewSession", FakeSystemViewSession)

    dev = Device(project_root=str(tmp_path))
    dev._bridge = FakeBridge()
    dev._connected = True
    dev._dwarf_info = object()

    def read_variable(name):
        calls.append((name, dev._bridge.state))
        if dev._bridge.state != DeviceState.READY:
            raise AssertionError("clock variables must be read before stream mode")
        if name == "SystemCoreClock":
            raise KeyError(name)
        if name == "hpm_core_clock":
            return 360_000_000
        raise AssertionError(name)

    monkeypatch.setattr(dev, "read_variable", read_variable)

    result = dev.systemview_start()

    assert result["cpu_freq_hint"] == 360_000_000
    assert result["cpu_freq_source"] == "hpm_core_clock"
    assert dev._systemview_parser.cpu_freq == 360_000_000
    assert calls == [
        ("SystemCoreClock", DeviceState.READY),
        ("hpm_core_clock", DeviceState.READY),
        ("start", DeviceState.READY),
    ]


def test_stm32f1_profile_has_systemview_cpu_clock_default():
    profiles = load_mcu_profiles()

    assert profiles["stm32f1"]["cpu_freq_default"] == 72_000_000


def test_hpm_project_seeds_systemview_id_base_without_guessing_clock(tmp_path, monkeypatch):
    mklink_dir = tmp_path / ".mklink"
    mklink_dir.mkdir()
    (mklink_dir / "project_info.json").write_text(
        '{"vendor":"hpmicro","board":"hpm5301evklite"}',
        encoding="utf-8",
    )

    class FakeBridge:
        state = DeviceState.READY
        idcode = 0
        current_mcu = ""

    class FakeSystemViewSession:
        _running = False

        def __init__(self, bridge, channel=1):
            self._bridge = bridge

        def start(self, addr, search_size=1024, project_root=".", *, mode=0):
            self._running = True
            return {"control_block_addr": "0x0008e488"}

        def stop(self):
            self._running = False

    monkeypatch.setattr("mklink.systemview.SystemViewSession", FakeSystemViewSession)

    dev = Device(project_root=str(tmp_path))
    dev._bridge = FakeBridge()
    dev._connected = True

    result = dev.systemview_start()

    assert "cpu_freq_hint" not in result
    assert result["systemview_ram_base"] == "0x10000000"
    assert dev._systemview_parser.cpu_freq == 0
    assert dev._systemview_parser._ram_base == 0x10000000
    assert dev._systemview_parser._id_shift == 2


def test_dashboard_parser_uses_hpm_project_id_base(tmp_path):
    mklink_dir = tmp_path / ".mklink"
    mklink_dir.mkdir()
    (mklink_dir / "project_info.json").write_text(
        '{"vendor":"hpmicro","board":"hpm5301evklite"}',
        encoding="utf-8",
    )

    dev = Device(project_root=str(tmp_path))
    mgr = SystemViewStreamManager()

    parser = mgr._create_parser(dev)

    assert parser._ram_base == 0x10000000
    assert parser._id_shift == 2


def test_dashboard_preserves_cpu_clock_source_from_start_result():
    mgr = SystemViewStreamManager()
    mgr._parser = mgr._create_parser()

    class FakeDevice:
        _dwarf_info = None

    freq = mgr._apply_cpu_freq_hint(
        FakeDevice(),
        {"cpu_freq_hint": 360_000_000, "cpu_freq_source": "hpm_core_clock"},
    )

    assert freq == 360_000_000
    assert mgr._cpu_freq_source == "hpm_core_clock"


def test_dashboard_runtime_cpu_clock_overrides_project_default_and_init():
    mgr = SystemViewStreamManager()

    class FakeDevice:
        _dwarf_info = object()

        @staticmethod
        def _systemview_defaults():
            return {
                "cpu_freq": 816_000_000,
                "cpu_freq_source": "project_info",
            }

    mgr._parser = mgr._create_parser(FakeDevice())
    assert mgr._parser.cpu_freq == 816_000_000

    freq = mgr._apply_cpu_freq_hint(
        FakeDevice(),
        {"cpu_freq_hint": 360_000_000, "cpu_freq_source": "hpm_core_clock"},
    )

    assert freq == 360_000_000
    assert mgr._parser.cpu_freq == 360_000_000
    assert mgr._cpu_freq_source == "hpm_core_clock"

    mgr._note_init_cpu_freq([{"kind": "init", "cpu_freq": 816_000_000}])
    assert mgr._parser.cpu_freq == 360_000_000
    assert mgr._cpu_freq_source == "hpm_core_clock"
