from pathlib import Path

from mklink.flash import MKLinkFlash, parse_hpm_program_result


def test_hpm_program_requires_loaded_successfully_or_100_percent():
    assert parse_hpm_program_result(
        'hpm.program("demo.bin",0x80000400)\n'
        "open fileName: demo.bin success,file size: 63384 byte\n"
        " demo.bin loaded successfully.\n"
        "0\n"
    )["success"]

    assert parse_hpm_program_result(
        'hpm.program("demo.bin",0x80000400)\n'
        "Download:  96% ,used 2127 ms\n"
        "Download: 100% ,used 2222 ms\n"
        "0\n"
    )["success"]

    assert not parse_hpm_program_result(
        'hpm.board("hpm5301evklite")\n'
        "board name = hpm5301evklite\n"
        "0\n"
        'hpm.program("demo.bin",0x80000400)\n'
        "0\n"
    )["success"]

    assert not parse_hpm_program_result(
        'hpm.program("demo.bin",0x80000400)\n'
        "Download:  96% ,used 2127 ms\n"
        "0\n"
    )["success"]


class _FakeBridge:
    def send_command(self, cmd, timeout=0, echo=False):
        if cmd.startswith("hpm.board"):
            return "board name = hpm5301evklite\n0\n"
        if cmd.startswith("hpm.program"):
            return "0\n"
        raise AssertionError(cmd)


def test_hpm_burn_bin_does_not_treat_plain_zero_as_success(tmp_path: Path):
    bin_file = tmp_path / "demo.bin"
    bin_file.write_bytes(b"demo")

    flash = MKLinkFlash(_FakeBridge())
    flash._copy_to_microkeen = lambda local_path, microkeen_filename=None: "demo.bin"

    result = flash.burn_hpm_bin(
        str(bin_file),
        addr="0x80000400",
        board="hpm5301evklite",
    )

    assert result["success"] is False
    assert result["loaded_successfully"] is False
    assert result["download_100_percent"] is False
