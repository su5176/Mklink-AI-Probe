param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,
    [switch]$KeepRunning
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$resolvedExecutable = (Resolve-Path -LiteralPath $Executable).Path
$started = @()
$runtimeFiles = @()
$completed = $false

try {
    foreach ($index in 0..1) {
        if ($KeepRunning) {
            $process = Start-Process -FilePath $resolvedExecutable -PassThru
        }
        else {
            $process = Start-Process `
                -FilePath $resolvedExecutable -PassThru -WindowStyle Hidden
        }
        $started += $process
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        $runtime = $null
        while ([DateTime]::UtcNow -lt $deadline) {
            $runtime = Get-ChildItem -LiteralPath $env:TEMP `
                -Filter "mklink-ai-probe-runtime-$($process.Id)-*.json" `
                -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($runtime) {
                break
            }
            Start-Sleep -Milliseconds 100
        }
        if (-not $runtime) {
            throw "Runtime info missing for desktop PID $($process.Id)"
        }
        $runtimeFiles += $runtime.FullName
        $info = Get-Content -Raw -Encoding UTF8 -LiteralPath $runtime.FullName |
            ConvertFrom-Json
        $healthDeadline = [DateTime]::UtcNow.AddSeconds(30)
        $health = $null
        while ([DateTime]::UtcNow -lt $healthDeadline) {
            try {
                $health = Invoke-RestMethod `
                    -Uri "http://127.0.0.1:$($info.port)/api/health" `
                    -TimeoutSec 1
            }
            catch {
                $health = $null
            }
            if ($health.desktop_instance_id -eq $info.instanceId) {
                break
            }
            Start-Sleep -Milliseconds 100
        }
        if (-not $health -or $health.desktop_instance_id -ne $info.instanceId) {
            throw "Health ownership mismatch for port $($info.port)"
        }
    }

    $infos = $runtimeFiles | ForEach-Object {
        Get-Content -Raw -Encoding UTF8 -LiteralPath $_ | ConvertFrom-Json
    }
    if (($infos.port | Sort-Object -Unique).Count -ne 2) {
        throw 'Desktop instances did not receive unique ports'
    }

    if ($KeepRunning) {
        $completed = $true
        [pscustomobject]@{
            FirstPid = $started[0].Id
            FirstPort = $infos[0].port
            SecondPid = $started[1].Id
            SecondPort = $infos[1].port
            KeptRunning = $true
            PreferredPortOwner = (
                Get-NetTCPConnection -State Listen -LocalPort 8765
            ).OwningProcess
        }
        return
    }

    Stop-Process -Id $started[0].Id -Force
    $started[0].WaitForExit(10000) | Out-Null
    Start-Sleep -Milliseconds 500
    $secondHealth = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$($infos[1].port)/api/health" `
        -TimeoutSec 2
    if ($secondHealth.desktop_instance_id -ne $infos[1].instanceId) {
        throw 'Second desktop backend changed ownership'
    }

    [pscustomobject]@{
        FirstPid = $started[0].Id
        FirstPort = $infos[0].port
        SecondPid = $started[1].Id
        SecondPort = $infos[1].port
        SecondSurvivedFirstExit = $true
        PreferredPortOwner = (
            Get-NetTCPConnection -State Listen -LocalPort 8765
        ).OwningProcess
    }
}
finally {
    if (-not ($KeepRunning -and $completed)) {
        foreach ($process in $started) {
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Milliseconds 500
        foreach ($path in $runtimeFiles) {
            if ($path -and (Test-Path -LiteralPath $path)) {
                Remove-Item -LiteralPath $path -Force
            }
        }
    }
}
