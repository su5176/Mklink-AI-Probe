from mklink._types import DeviceState
from mklink.device import Device
from mklink.profiles import load_mcu_profiles


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


def test_stm32f1_profile_has_systemview_cpu_clock_default():
    profiles = load_mcu_profiles()

    assert profiles["stm32f1"]["cpu_freq_default"] == 72_000_000
