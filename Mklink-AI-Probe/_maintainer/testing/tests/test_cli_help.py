import subprocess
import sys
from pathlib import Path

import pytest


def test_top_level_help_renders_systemview_commands():
    root = Path(__file__).resolve().parents[3]

    result = subprocess.run(
        [sys.executable, "-m", "mklink", "--help"],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "systemview-analyze" in result.stdout
    assert "web-entry" in result.stdout
    assert "security" in result.stdout


def test_web_entry_help_exposes_install_html_and_lifecycle_commands():
    root = Path(__file__).resolve().parents[3]

    result = subprocess.run(
        [sys.executable, "-m", "mklink", "web-entry", "--help"],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    for command in ("install", "uninstall", "html", "start", "stop", "status"):
        assert command in result.stdout


@pytest.mark.parametrize("action", ["lock", "unlock"])
def test_security_help_requires_exact_target_voltage_and_confirmation(action):
    root = Path(__file__).resolve().parents[3]

    result = subprocess.run(
        [sys.executable, "-m", "mklink", "security", action, "--help"],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "--target-part" in result.stdout
    assert "--voltage-mv {1800,3300,5000}" in result.stdout
    assert "--confirm" in result.stdout
    assert ("--confirm-data-loss" in result.stdout) is (action == "unlock")
    assert ("--firmware" in result.stdout) is (action == "lock")


@pytest.mark.parametrize(
    "command",
    ["symbols", "hardfault", "typeinfo", "memmap", "watch", "superwatch", "vofa", "break"],
)
def test_elf_commands_expose_explicit_backend_choice(command):
    root = Path(__file__).resolve().parents[3]

    result = subprocess.run(
        [sys.executable, "-m", "mklink", command, "--help"],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "--elf-backend {builtin,external}" in result.stdout
