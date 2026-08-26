"""USB composite-interface metadata shared by MKLink transports."""

from __future__ import annotations

import re
from typing import Any


MKLINK_USB_VID_PID = (0x0D28, 0x0202)
MKLINK_COMMAND_INTERFACE = 0x04


def usb_interface_number(info: Any) -> int | None:
    """Return a USB interface number from pyserial port metadata."""
    text = " ".join(
        str(getattr(info, key, "") or "")
        for key in ("hwid", "location", "interface")
    ).strip()
    match = re.search(r"(?i)MI[_-]?([0-9a-f]{2})", text)
    if match is not None:
        return int(match.group(1), 16)
    match = re.search(r"(?i)(?:x\.|\.)(\d+)$", text)
    return int(match.group(1), 10) if match else None


def is_mklink_usb_port(info: Any) -> bool:
    return (
        getattr(info, "vid", None),
        getattr(info, "pid", None),
    ) == MKLINK_USB_VID_PID
