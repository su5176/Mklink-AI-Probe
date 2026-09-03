"""Regression tests for the shared build workspace launcher."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "scripts" / "build_workspace.ps1"


def _powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_native_fixture(tmp_path: Path, exit_code: int):
    child = tmp_path / f"native-stderr-{exit_code}.py"
    child.write_text(
        "import sys\n"
        "print('fixture stdout')\n"
        "print('fixture diagnostic', file=sys.stderr)\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    build_root = tmp_path / f"build-{exit_code}"
    driver = tmp_path / f"run-{exit_code}.ps1"
    driver.write_text(
        "$arguments = @(" + _powershell_literal(child) + ")\n"
        "& "
        + _powershell_literal(LAUNCHER)
        + " -Action run -BuildRoot "
        + _powershell_literal(build_root)
        + " -Executable "
        + _powershell_literal(sys.executable)
        + " -ArgumentList $arguments\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(driver),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    reports = sorted((build_root / "reports").glob("run-*.log"))
    assert len(reports) == 1
    raw_log = reports[0].read_bytes()
    # Windows PowerShell 5 Tee-Object writes UTF-16; PowerShell 7 writes UTF-8.
    encoding = "utf-16" if raw_log.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    return completed, raw_log.decode(encoding, errors="replace")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell semantics")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_native_stderr_is_logged_without_overriding_process_exit_code(
    tmp_path, exit_code
):
    completed, log = _run_native_fixture(tmp_path, exit_code)

    assert completed.returncode == exit_code, json.dumps(
        {"stdout": completed.stdout, "stderr": completed.stderr},
        ensure_ascii=False,
    )
    assert "fixture stdout" in completed.stdout
    assert "fixture diagnostic" in completed.stdout + completed.stderr
    assert "fixture stdout" in log
    assert "fixture diagnostic" in log


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking semantics")
def test_locked_temporary_file_is_retained_without_overriding_success(tmp_path):
    holder = tmp_path / "hold-open.py"
    holder.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "with (root / 'held.log').open('w', encoding='utf-8') as stream:\n"
        "    stream.write('held')\n"
        "    stream.flush()\n"
        "    (root / 'ready').write_text('ready', encoding='utf-8')\n"
        "    time.sleep(4)\n",
        encoding="utf-8",
    )
    child = tmp_path / "launch-holder.py"
    child.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"holder = Path({str(holder)!r})\n"
        "root = Path(os.environ['TEMP']) / 'locked-fixture'\n"
        "flags = int(getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n"
        "subprocess.Popen([sys.executable, str(holder), str(root)], creationflags=flags)\n"
        "deadline = time.monotonic() + 3\n"
        "while not (root / 'ready').exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise SystemExit('holder did not become ready')\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    build_root = tmp_path / "locked-build"
    driver = tmp_path / "run-locked.ps1"
    driver.write_text(
        "$arguments = @(" + _powershell_literal(child) + ")\n"
        "& "
        + _powershell_literal(LAUNCHER)
        + " -Action run -BuildRoot "
        + _powershell_literal(build_root)
        + " -Executable "
        + _powershell_literal(sys.executable)
        + " -ArgumentList $arguments\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(driver),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    try:
        assert completed.returncode == 0
        assert "Temporary run cleanup failed; retained for safe inspection" in (
            completed.stdout + completed.stderr
        )
        assert len(list((build_root / "runs").glob("run-*"))) == 1
    finally:
        # Let the helper release its handle before pytest removes tmp_path.
        time.sleep(4.2)
