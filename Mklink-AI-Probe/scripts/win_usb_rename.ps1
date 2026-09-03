[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Restore,
    [switch]$Json
)

# Rename only verified, currently connected MKLink V2/V3/V4 CDC interfaces.
# The default mode is read-only. Registry writes require -Apply and elevation.

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

if ($Apply -and $Restore) {
    throw "-Apply and -Restore are mutually exclusive."
}

$Vid = "0D28"
$PidValue = "0202"
$RegistryBase = "HKLM:\SYSTEM\CurrentControlSet\Enum\USB"
$InterfaceNames = [ordered]@{
    "MI_02" = "MKLink USB to UART"
    "MI_04" = "MKLink Python Console"
    "MI_06" = "MKLink USB to RS485"
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Get-PnpUtilPath {
    $path = Join-Path $env:WINDIR "System32\pnputil.exe"
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        $sysnative = Join-Path $env:WINDIR "Sysnative\pnputil.exe"
        if (Test-Path -LiteralPath $sysnative) {
            return $sysnative
        }
    }
    return $path
}

function Get-PropertyValue {
    param(
        [string]$Properties,
        [string]$Key,
        [string]$Type = "String"
    )

    $pattern = "(?ms)$([regex]::Escape($Key)) \[$Type\]:\s*(?<Value>[^\r\n]+)"
    $match = [regex]::Match($Properties, $pattern)
    if ($match.Success) {
        return $match.Groups["Value"].Value.Trim()
    }
    return $null
}

function Get-ConnectedUsbInventory {
    $pnpUtil = Get-PnpUtilPath
    $output = (& $pnpUtil /enum-devices /connected /bus USB /properties 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) {
        throw "Windows PnP device enumeration failed."
    }

    $pattern = "(?ms)DEVPKEY_Device_InstanceId \[String\]:\s*(?<Id>USB\\[^\r\n]+)(?<Props>.*?)(?=DEVPKEY_Device_InstanceId \[String\]:|\z)"
    return @(
        foreach ($match in [regex]::Matches($output, $pattern)) {
            $properties = $match.Groups["Props"].Value
            [pscustomobject]@{
                InstanceId = $match.Groups["Id"].Value.Trim()
                BusDescription = Get-PropertyValue $properties "DEVPKEY_Device_BusReportedDeviceDesc"
                DeviceDescription = Get-PropertyValue $properties "DEVPKEY_Device_DeviceDesc"
                ContainerId = Get-PropertyValue $properties "DEVPKEY_Device_ContainerId" "GUID"
                Parent = Get-PropertyValue $properties "DEVPKEY_Device_Parent"
            }
        }
    )
}

function Get-RegistryPaths {
    param([string]$InstanceId)

    $parts = $InstanceId -split "\\", 3
    if ($parts.Count -ne 3 -or $parts[0] -ne "USB") {
        throw "Cannot parse USB device instance ID: $InstanceId"
    }
    $instancePath = Join-Path (Join-Path $RegistryBase $parts[1]) $parts[2]
    return [pscustomobject]@{
        InstancePath = $instancePath
        DeviceParametersPath = Join-Path $instancePath "Device Parameters"
        ExportKey = "HKLM\SYSTEM\CurrentControlSet\Enum\USB\$($parts[1])\$($parts[2])"
    }
}

function Get-Candidates {
    param([object[]]$Inventory)

    $rootPattern = "^USB\\VID_${Vid}&PID_${PidValue}\\(?<Serial>[^\\]+)$"
    $roots = @(
        $Inventory | Where-Object {
            $_.InstanceId -match $rootPattern -and
            $_.BusDescription -match '^MicroKeen\s*V(?<Version>[234])\s+CMSIS-DAP$' -and
            $_.ContainerId
        }
    )
    $candidates = @()

    foreach ($root in $roots) {
        $null = $root.InstanceId -match $rootPattern
        $serialNumber = $Matches["Serial"]
        $null = $root.BusDescription -match '^MicroKeen\s*V(?<Version>[234])\s+CMSIS-DAP$'
        $version = [int]$Matches["Version"]
        $expectedInterfaces = if ($version -eq 4) {
            @("MI_02", "MI_04", "MI_06")
        } else {
            @("MI_02", "MI_04")
        }
        $deviceCandidates = @()
        $complete = $true

        foreach ($mi in $expectedInterfaces) {
            $matches = @(
                $Inventory | Where-Object {
                    $_.InstanceId -like "USB\VID_${Vid}&PID_${PidValue}&${mi}\*" -and
                    $_.Parent -ieq $root.InstanceId -and
                        $_.ContainerId -ieq $root.ContainerId
                }
            )
            if ($matches.Count -ne 1) {
                Write-Warning "Skipping $($root.BusDescription) serial ${serialNumber}: expected one verified $mi interface, found $($matches.Count)."
                $complete = $false
                break
            }

            $paths = Get-RegistryPaths $matches[0].InstanceId
            if (-not (Test-Path -LiteralPath $paths.DeviceParametersPath)) {
                Write-Warning "Skipping $($root.BusDescription): Device Parameters is missing for $mi."
                $complete = $false
                break
            }
            $deviceParameters = Get-ItemProperty -LiteralPath $paths.DeviceParametersPath
            $portProperty = $deviceParameters.PSObject.Properties["PortName"]
            if ($null -eq $portProperty -or -not $portProperty.Value) {
                Write-Warning "Skipping $($root.BusDescription): PortName is missing for $mi."
                $complete = $false
                break
            }
            $instanceProperties = Get-ItemProperty -LiteralPath $paths.InstancePath
            $friendlyProperty = $instanceProperties.PSObject.Properties["FriendlyName"]
            $currentName = if ($null -ne $friendlyProperty) {
                [string]$friendlyProperty.Value
            } else {
                $null
            }
            $targetName = "$($InterfaceNames[$mi]) ($($portProperty.Value))"
            $deviceCandidates += [pscustomobject]@{
                Version = "V$version"
                Product = $root.BusDescription
                Serial = $serialNumber
                ContainerId = $root.ContainerId
                Parent = $root.InstanceId
                MI = $mi
                PortName = [string]$portProperty.Value
                CurrentName = $currentName
                TargetName = $targetName
                ChangeRequired = $currentName -cne $targetName
                BusDescription = $matches[0].BusDescription
                DeviceDescription = $matches[0].DeviceDescription
                InstanceId = $matches[0].InstanceId
                InstancePath = $paths.InstancePath
                DeviceParametersPath = $paths.DeviceParametersPath
                ExportKey = $paths.ExportKey
            }
        }

        if ($complete) {
            $candidates += $deviceCandidates
        }
    }
    return $candidates
}

function Write-Result {
    param(
        [string]$Status,
        [object[]]$Candidates,
        [string]$BackupDirectory = ""
    )

    if ($Json) {
        [pscustomobject]@{
            Status = $Status
            Apply = [bool]$Apply
            ChangesRequired = @($Candidates | Where-Object ChangeRequired).Count
            BackupDirectory = $BackupDirectory
            Ports = @(
                $Candidates | Select-Object Version, Product, Serial, ContainerId,
                    MI, PortName, CurrentName, TargetName, ChangeRequired, InstanceId
            )
        } | ConvertTo-Json -Depth 4
        return
    }

    if ($Candidates.Count -eq 0) {
        Write-Warning "No complete, descriptor-verified MKLink V2/V3/V4 device was found."
        return
    }
    $Candidates |
        Select-Object Version, Product, MI, PortName, CurrentName, TargetName, ChangeRequired |
        Format-Table -AutoSize -Wrap
    if ($Status -eq "Preview") {
        Write-Warning "Preview only; no registry values were changed. After explicit confirmation, run this script as administrator with -Apply."
    } elseif ($BackupDirectory) {
        Write-Host "Rename complete. Registry backup: $BackupDirectory"
        Write-Host "Reconnect the probes or refresh Device Manager if a displayed name is cached."
    }
}

function Invoke-Restore {
    param([object[]]$Candidates)

    if (-not (Test-IsAdministrator)) {
        throw "-Restore requires an elevated Administrator PowerShell session."
    }

    $restoreCandidates = @($Candidates)
    if ($restoreCandidates.Count -eq 0) {
        Write-Result "AlreadyRestored" $Candidates
        return
    }

    # Re-enumerate immediately before mutation so a device replacement cannot
    # cause a stale registry path to be edited.
    $currentCandidates = @(Get-Candidates @(Get-ConnectedUsbInventory))
    foreach ($candidate in $restoreCandidates) {
        $current = @(
            $currentCandidates | Where-Object {
                $_.InstanceId -ieq $candidate.InstanceId -and
                $_.ContainerId -ieq $candidate.ContainerId -and
                $_.Parent -ieq $candidate.Parent -and
                $_.Serial -ceq $candidate.Serial -and
                $_.MI -ceq $candidate.MI -and
                $_.PortName -ceq $candidate.PortName
            }
        )
        if ($current.Count -ne 1) {
            throw "Device identity or name changed; no registry values were restored: $($candidate.InstanceId)"
        }
    }

    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $backupDirectory = [IO.Path]::GetFullPath(
        (Join-Path $temporaryRoot ("MKLinkUsbRestore_" + (Get-Date -Format "yyyyMMdd_HHmmss")))
    )
    if (-not $backupDirectory.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Resolved backup path is outside the system temporary directory."
    }
    [IO.Directory]::CreateDirectory($backupDirectory) | Out-Null

    foreach ($candidate in $restoreCandidates) {
        $safeName = $candidate.InstanceId -replace "[^A-Za-z0-9._-]", "_"
        $backupFile = Join-Path $backupDirectory "$safeName.reg"
        & reg.exe export $candidate.ExportKey $backupFile /y | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $backupFile)) {
            throw "Registry backup failed before any restore: $($candidate.InstanceId)"
        }
    }

    foreach ($candidate in $restoreCandidates) {
        $description = if ([string]::IsNullOrWhiteSpace($candidate.DeviceDescription)) {
            "USB Serial Device"
        } else {
            $candidate.DeviceDescription.Trim()
        }
        $defaultName = "$description ($($candidate.PortName))"
        New-ItemProperty -LiteralPath $candidate.InstancePath -Name "FriendlyName" `
            -PropertyType String -Value $defaultName -Force | Out-Null
        New-ItemProperty -LiteralPath $candidate.DeviceParametersPath -Name "FriendlyName" `
            -PropertyType String -Value $defaultName -Force | Out-Null
        $root = Get-ItemProperty -LiteralPath $candidate.InstancePath
        $parameters = Get-ItemProperty -LiteralPath $candidate.DeviceParametersPath
        if ($root.FriendlyName -cne $defaultName -or $parameters.FriendlyName -cne $defaultName) {
            throw "Registry restore verification failed: $($candidate.InstanceId)"
        }
    }

    Write-Result "Restored" $Candidates $backupDirectory
}

$inventory = @(Get-ConnectedUsbInventory)
$candidates = @(Get-Candidates $inventory)
if ($candidates.Count -eq 0) {
    Write-Result "NoDevices" $candidates
    exit 0
}
if (-not $Apply) {
    if ($Restore) {
        Invoke-Restore $candidates
    } else {
        Write-Result "Preview" $candidates
    }
    exit 0
}
if (-not (Test-IsAdministrator)) {
    throw "-Apply requires an elevated Administrator PowerShell session."
}

# Re-enumerate and compare every identity field immediately before mutation.
$currentCandidates = @(Get-Candidates @(Get-ConnectedUsbInventory))
foreach ($candidate in $candidates) {
    $current = @(
        $currentCandidates | Where-Object {
            $_.InstanceId -ieq $candidate.InstanceId -and
            $_.ContainerId -ieq $candidate.ContainerId -and
            $_.Parent -ieq $candidate.Parent -and
            $_.Serial -ceq $candidate.Serial -and
            $_.MI -ceq $candidate.MI -and
            $_.PortName -ceq $candidate.PortName
        }
    )
    if ($current.Count -ne 1) {
        throw "Device identity changed after preview; no registry values were written: $($candidate.InstanceId)"
    }
}

$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$backupDirectory = [IO.Path]::GetFullPath(
    (Join-Path $temporaryRoot ("MKLinkUsbRename_" + (Get-Date -Format "yyyyMMdd_HHmmss")))
)
if (-not $backupDirectory.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Resolved backup path is outside the system temporary directory."
}
[IO.Directory]::CreateDirectory($backupDirectory) | Out-Null

# Complete every backup before the first registry write.
foreach ($candidate in $candidates) {
    $safeName = $candidate.InstanceId -replace "[^A-Za-z0-9._-]", "_"
    $backupFile = Join-Path $backupDirectory "$safeName.reg"
    & reg.exe export $candidate.ExportKey $backupFile /y | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $backupFile)) {
        throw "Registry backup failed before any write: $($candidate.InstanceId)"
    }
}

foreach ($candidate in $candidates) {
    New-ItemProperty -LiteralPath $candidate.InstancePath -Name "FriendlyName" `
        -PropertyType String -Value $candidate.TargetName -Force | Out-Null
    New-ItemProperty -LiteralPath $candidate.DeviceParametersPath -Name "FriendlyName" `
        -PropertyType String -Value $candidate.TargetName -Force | Out-Null
}

foreach ($candidate in $candidates) {
    $rootValue = (Get-ItemProperty -LiteralPath $candidate.InstancePath).FriendlyName
    $parameterValue = (Get-ItemProperty -LiteralPath $candidate.DeviceParametersPath).FriendlyName
    if ($rootValue -cne $candidate.TargetName -or $parameterValue -cne $candidate.TargetName) {
        throw "Registry write verification failed: $($candidate.InstanceId)"
    }
}

Write-Result "Applied" $candidates $backupDirectory
