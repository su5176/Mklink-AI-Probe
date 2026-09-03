from types import SimpleNamespace

from mklink.serial import _port as serial_port
from mklink.usb_interfaces import usb_interface_number


def port(device, *, location="", hwid="", vid=None, pid=None):
    return SimpleNamespace(
        device=device,
        description=f"USB Serial Device ({device})",
        manufacturer="Microsoft",
        interface=None,
        location=location,
        hwid=hwid,
        vid=vid,
        pid=pid,
    )


def test_usb_interface_number_accepts_windows_location_and_mi_metadata():
    assert usb_interface_number(port("A", location="1-2:x.4")) == 0x04
    assert usb_interface_number(port("B", hwid=r"USB\VID_0D28&PID_0202&MI_0A")) == 0x0A
    assert usb_interface_number(port("C", location="")) is None


def test_generic_serial_list_hides_only_mklink_command_interface(monkeypatch):
    ports = [
        port("COM54", location="1-2:x.2", vid=0x0D28, pid=0x0202),
        port("COM55", location="1-2:x.4", vid=0x0D28, pid=0x0202),
        port("COM56", location="1-2:x.6", vid=0x0D28, pid=0x0202),
        port("COM9", location="1-3:x.4", vid=0x1234, pid=0x5678),
    ]
    monkeypatch.setattr(serial_port.serial.tools.list_ports, "comports", lambda: ports)

    assert [item["device"] for item in serial_port.list_uart_ports()] == [
        "COM54",
        "COM56",
        "COM9",
    ]
    assert serial_port.is_mklink_port("COM55") is True
    assert serial_port.is_mklink_port("COM54") is False
    assert serial_port.is_mklink_port("COM56") is False
