# Audit/restore the CMSIS-DAP discovery GUID without reinstalling a USB driver.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstanceId,
    [switch]$Apply,
    [string]$BackupDirectory
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$dapGuid = '{CDB3B5AD-293B-4663-AA36-1AAE46463776}'

function Get-DapGuidPlan {
    param([string]$Id, [string]$Product, [string]$Service, [bool]$Present, [string[]]$Existing)
    if ($Id -notmatch '^USB\\VID_0D28&PID_0202&MI_00\\[A-Za-z0-9&._-]+$' -or
        $Product -notmatch '^MicroKeen\s*V[234]\s+CMSIS-DAP$' -or
        $Service -ine 'WINUSB' -or -not $Present) {
        throw 'Refusing repair: expected a present, descriptor-identified MKLink V2/V3/V4 MI_00 WinUSB interface.'
    }
    [pscustomobject]@{
        InstanceId = $Id
        Product = $Product
        ExistingGuids = @($Existing)
        MissingDapGuid = $dapGuid -notin @($Existing)
        ExpectedGuid = $dapGuid
        ProposedGuids = @(@($Existing) + @($dapGuid) | Select-Object -Unique)
    }
}

# Tests may load the pure validation function without touching Windows devices.
if ($MyInvocation.InvocationName -eq '.') { return }
if ($InstanceId -notmatch '^USB\\VID_0D28&PID_0202&MI_00\\[A-Za-z0-9&._-]+$') {
    throw 'Specify the exact MKLink MI_00 device instance; no parent, serial port, or wildcard is allowed.'
}
$pnpUtil = Join-Path $env:SystemRoot 'System32/pnputil.exe'
$null = & (Join-Path $env:SystemRoot 'System32/chcp.com') 65001
$inventory = (& $pnpUtil /enum-devices /instanceid $InstanceId /properties 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'Cannot read the selected PnP device.' }
function Read-PnpProperty([string]$Name, [string]$Kind = 'String') {
    $pattern = [regex]::Escape($Name) + ' \[' + [regex]::Escape($Kind) + '\]:\s*([^\r\n]+)'
    $match = [regex]::Match($inventory, $pattern)
    if ($match.Success) { return $match.Groups[1].Value.Trim() }
    return ''
}
$hardwarePath = 'Registry::HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Enum\' + $InstanceId
$parameterPath = Join-Path $hardwarePath 'Device Parameters'
$parameters = Get-Item -LiteralPath $parameterPath
$oldValue = $parameters.GetValue('DeviceInterfaceGUIDs', $null)
if ($null -ne $oldValue -and $parameters.GetValueKind('DeviceInterfaceGUIDs') -ne [Microsoft.Win32.RegistryValueKind]::MultiString) {
    throw 'Unexpected GUID registry type; preserve it for manual diagnosis.'
}
$plan = Get-DapGuidPlan -Id $InstanceId -Product (Read-PnpProperty 'DEVPKEY_Device_BusReportedDeviceDesc') `
    -Service (Read-PnpProperty 'DEVPKEY_Device_Service') `
    -Present ((Read-PnpProperty 'DEVPKEY_Device_IsPresent' 'Boolean') -eq 'TRUE') -Existing @($oldValue | Where-Object { $_ })
if (-not $Apply -or -not $plan.MissingDapGuid) {
    $plan | ConvertTo-Json -Depth 4
    exit 0
}
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Apply requires an Administrator PowerShell. No changes were made.'
}
if (-not $BackupDirectory -or -not [IO.Path]::IsPathRooted($BackupDirectory)) {
    throw 'Apply requires an absolute BackupDirectory before any registry changes.'
}
$null = New-Item -ItemType Directory -Path $BackupDirectory -Force
$backup = Join-Path $BackupDirectory ('dap-guid-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$null = New-Item -ItemType Directory -Path $backup
& reg.exe export ('HKLM\SYSTEM\CurrentControlSet\Enum\' + $InstanceId) (Join-Path $backup 'device.reg') /y | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Registry backup failed; no changes were made.' }
$plan | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $backup 'before.json') -Encoding UTF8
# Roll back only this value, including the originally absent-value case.
$quotedPath = $parameterPath.Replace("'", "''")
$rollback = if ($null -eq $oldValue) {
    "Remove-ItemProperty -LiteralPath '$quotedPath' -Name DeviceInterfaceGUIDs -ErrorAction Stop"
} else {
    $literals = @($oldValue | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }) -join ','
    "New-ItemProperty -LiteralPath '$quotedPath' -Name DeviceInterfaceGUIDs -PropertyType MultiString -Value @($literals) -Force | Out-Null"
}
$rollback += "`n& pnputil.exe /restart-device '$($InstanceId.Replace("'", "''"))'`n"
Set-Content (Join-Path $backup 'rollback.ps1') $rollback -Encoding UTF8
New-ItemProperty -LiteralPath $parameterPath -Name DeviceInterfaceGUIDs -PropertyType MultiString -Value $plan.ProposedGuids -Force | Out-Null
$readback = (Get-Item -LiteralPath $parameterPath).GetValue('DeviceInterfaceGUIDs', $null)
if ($dapGuid -notin @($readback)) { throw "GUID readback failed; backup: $backup" }
& $pnpUtil /restart-device $InstanceId | Tee-Object -FilePath (Join-Path $backup 'restart.txt')
if ($LASTEXITCODE -ne 0) { throw "GUID restored, but this DAP interface could not restart. Backup/rollback: $backup" }
[pscustomobject]@{ Status = 'guid-restored'; InstanceId = $InstanceId; BackupDirectory = $backup; Note = 'Refresh the Keil/host probe list and verify communication.' } | ConvertTo-Json
