import os
import time
from pathlib import Path

from mklink.cli import _cli_project_init, _detect_hpm_segger_project
from mklink.project_config import load_config, load_project_info


def _write_cmake_cache(path: Path, *, board: str, project: str = "hello_world"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([
            f"CMAKE_PROJECT_NAME:STATIC={project}",
            f"BOARD:UNINITIALIZED={board}",
            f"SDK_BOARD_DIR:PATH=C:/hpm_sdk/boards/{board}",
            "HPM_BUILD_TYPE:UNINITIALIZED=flash_xip",
            "CMAKE_READELF:FILEPATH=C:/toolchain/bin/riscv32-unknown-elf-readelf.exe",
            "",
        ]),
        encoding="utf-8",
    )


def _write_output_files(output: Path):
    output.mkdir(parents=True)
    for suffix in ("bin", "elf", "map"):
        (output / f"demo.{suffix}").write_bytes(b"demo")


def test_hpm_detect_prefers_newest_cmake_output_over_ses_export(tmp_path: Path):
    ses = tmp_path / "hpm5301evklite_flash_xip_debug" / "segger_embedded_studio"
    exe = ses / "Output" / "Debug" / "Exe"
    exe.mkdir(parents=True)
    for suffix in ("bin", "elf", "map"):
        (exe / f"demo.{suffix}").write_bytes(b"old")
    (ses / "hello_world.json").write_text(
        '{"target":{"board":"hpm5301evklite","target_device_name":"HPM5301xEGx","soc":"HPM5301"}}',
        encoding="utf-8",
    )

    build = tmp_path / "hpm6e00evk_flash_xip_debug"
    _write_cmake_cache(build / "CMakeCache.txt", board="hpm6e00evk")
    _write_output_files(build / "output")
    now = time.time()
    os.utime(build / "output" / "demo.bin", (now + 10, now + 10))

    info = _detect_hpm_segger_project(str(tmp_path))

    assert info is not None
    assert info["board"] == "hpm6e00evk"
    assert info["ide_type"] == "HPM SDK CMake"
    assert info["bin_path"].endswith(str(Path("hpm6e00evk_flash_xip_debug") / "output" / "demo.bin"))
    assert info["axf_path"].endswith(str(Path("hpm6e00evk_flash_xip_debug") / "output" / "demo.elf"))


def test_project_init_preserves_hpm_cmake_ide_type(tmp_path: Path, monkeypatch):
    build = tmp_path / "hpm6e00evk_flash_xip_debug"
    _write_cmake_cache(build / "CMakeCache.txt", board="hpm6e00evk")
    _write_output_files(build / "output")

    monkeypatch.setattr("mklink.discovery.find_mklink_cdc_port", lambda: "COM9")

    _cli_project_init(str(tmp_path))

    assert load_project_info(str(tmp_path))["ide_type"] == "HPM SDK CMake"
    assert load_config(str(tmp_path))["ide_type"] == "HPM SDK CMake"
