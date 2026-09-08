from mklink._types import FLM_LOAD_TIMEOUT
from mklink.bridge import quote_probe_string
from mklink.flash import MKLinkFlash


class _Bridge:
    def __init__(self):
        self.commands = []
        self.options = []

    def send_command(self, command, **kwargs):
        self.commands.append(command)
        self.options.append(kwargs)
        return "0\n" if command.startswith("load.flm") else "Download: 100% ,used 10 ms\n0\n"


def test_probe_paths_use_single_quoted_literals():
    assert quote_probe_string("/FLM/STM32F40x.FLM") == "'/FLM/STM32F40x.FLM'"
    assert quote_probe_string("a'b.hex") == "'a\\'b.hex'"

    bridge = _Bridge()
    flasher = MKLinkFlash(bridge)
    assert flasher.load_flm("/FLM/STM32F40x.FLM", "0x08000000", "0x20000000")

    flasher._copy_to_microkeen = lambda *_args: "image.hex"
    assert flasher.burn_hex("ignored.hex")["success"]

    flasher._copy_to_microkeen = lambda *_args: "image.bin"
    assert flasher.burn_bin("ignored.bin", "0x08000000")["success"]

    assert bridge.commands == [
        "load.flm('/FLM/STM32F40x.FLM',0x08000000,0x20000000)",
        "load.hex('image.hex')",
        "load.bin('image.bin',0x08000000)",
    ]
    assert bridge.options[-1]["timeout"] == FLM_LOAD_TIMEOUT
