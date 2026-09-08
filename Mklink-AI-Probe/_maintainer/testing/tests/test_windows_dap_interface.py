"""The manual Windows GUID repair must never target another USB function."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts' / 'win_dap_interface.ps1'
PWSH = shutil.which('pwsh')
pytestmark = pytest.mark.skipif(not PWSH, reason='PowerShell 7 required')


def test_dap_guid_plan_rejects_other_devices_and_preserves_existing_guids(tmp_path):
    runner = tmp_path / 'plan.ps1'
    script = str(SCRIPT).replace("'", "''")
    runner.write_text(f"""
$ErrorActionPreference='Stop'
. '{script}' -InstanceId ignored
$id='USB\\VID_0D28&PID_0202&MI_00\\test'
$common=@{{Id=$id;Product='MicroKeenV4 CMSIS-DAP';Service='WINUSB';Present=$true}}
$missing=Get-DapGuidPlan @common -Existing @()
if (-not $missing.MissingDapGuid -or $missing.ProposedGuids.Count -ne 1) {{throw 'missing GUID'}}
$old='{{12345678-1234-1234-1234-123456789ABC}}'
$merged=Get-DapGuidPlan @common -Existing @($old)
if ($old -notin $merged.ProposedGuids -or $merged.ProposedGuids.Count -ne 2) {{throw 'lost old GUID'}}
$existing=Get-DapGuidPlan @common -Existing @($dapGuid)
if ($existing.MissingDapGuid -or $existing.ProposedGuids.Count -ne 1) {{throw 'not idempotent'}}
$cases=@(
 @{{Id='USB\\VID_0D28&PID_0202&MI_04\\test'}},
 @{{Id='USB\\VID_0D28&PID_0202\\test'}},
 @{{Id='USB\\VID_1234&PID_0202&MI_00\\test'}},
 @{{Id='USB\\VID_0D28&PID_0202&MI_00\\test\\subkey'}},
 @{{Id='USB\\VID_0D28&PID_0202&MI_00\\*'}},
 @{{Id='USB\\VID_0D28&PID_0202&MI_00\\test?'}},
 @{{Product='Other CMSIS-DAP'}}, @{{Service='libusbK'}}, @{{Present=$false}}
)
foreach ($change in $cases) {{
 $args=$common.Clone();foreach ($key in $change.Keys) {{$args[$key]=$change[$key]}}
 $rejected=$false
 try {{$null=Get-DapGuidPlan @args -Existing @()}} catch {{$rejected=$true}}
 if (-not $rejected) {{throw 'accepted an unrelated device'}}
}}
@{{passed=12;missing=$missing.MissingDapGuid}} | ConvertTo-Json
""", encoding='utf-8')
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(runner)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'passed': 12, 'missing': True}
