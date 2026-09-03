# Run build/test tools with one ignored, non-system-drive storage root.
[CmdletBinding()]
param(
    [ValidateSet('run', 'paths', 'clean')][string]$Action = 'paths',
    [string]$BuildRoot,
    [string]$WorkingDirectory = (Split-Path $PSScriptRoot),
    [string]$Executable,
    [string[]]$ArgumentList = @()
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

if (-not $BuildRoot -and $env:MKLINK_BUILD_ROOT) {
    $BuildRoot = $env:MKLINK_BUILD_ROOT
}
if (-not $BuildRoot) {
    $commonDir = git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir
    if ($LASTEXITCODE -ne 0) { throw 'A Git source checkout is required.' }
    $BuildRoot = Join-Path (Split-Path $commonDir.Trim()) '.build'
}
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
$drive = [IO.Path]::GetPathRoot($BuildRoot)
if ($drive -eq 'C:\' -or ($env:SystemDrive -and $drive -eq ($env:SystemDrive + '\'))) {
    throw 'Build storage must not be on C: or the Windows system drive.'
}
if ($BuildRoot -eq $drive) { throw 'A dedicated build subdirectory is required.' }

function Assert-BuildPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($BuildRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Outside build root: $full" }
    $cursor = $full
    while ($cursor -and $cursor -ne $drive) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Build path must not traverse a link: $cursor" }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $full
}

$cacheRoot = Assert-BuildPath (Join-Path $BuildRoot 'cache')
$runsRoot = Assert-BuildPath (Join-Path $BuildRoot 'runs')
$artifactsRoot = Assert-BuildPath (Join-Path $BuildRoot 'artifacts')
$reportsRoot = Assert-BuildPath (Join-Path $BuildRoot 'reports')
if ($Action -eq 'paths') {
    [pscustomobject]@{root=$BuildRoot;cache=$cacheRoot;runs=$runsRoot;artifacts=$artifactsRoot;reports=$reportsRoot} | ConvertTo-Json
    exit 0
}
if ($Action -eq 'run' -and -not $Executable) { throw '-Executable is required for run.' }
[IO.Directory]::CreateDirectory($BuildRoot) > $null
$marker = Join-Path $BuildRoot '.mklink-build-workspace'
if ($Action -eq 'clean' -and (-not (Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker -Raw).Trim() -ne 'mklink-build-workspace-v1')) {
    throw 'Refusing cleanup of an unrecognized build directory.'
}
if ($Action -eq 'run') { Set-Content -LiteralPath $marker -Value 'mklink-build-workspace-v1' -Encoding utf8 }
# One build/test command at a time prevents cache and cleanup races.
$lock = [IO.File]::Open((Join-Path $BuildRoot '.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
$savedEnvironment = @{}
$runDir = $null
$exitCode = 1
try {
    if ($Action -eq 'clean') {
        if (Test-Path -LiteralPath $runsRoot) {
            $null = Assert-BuildPath $runsRoot
            if (@(Get-ChildItem -LiteralPath $runsRoot -Force | Where-Object { -not $_.PSIsContainer -or $_.Name -notlike 'run-*' }).Count) {
                throw 'Unexpected content in temporary runs; inspect before cleanup.'
            }
            if (@(Get-ChildItem -LiteralPath $runsRoot -Recurse -Force -Attributes ReparsePoint).Count) {
                throw 'Temporary runs contain links; inspect them before manual cleanup.'
            }
            Remove-Item -LiteralPath $runsRoot -Recurse -Force
        }
        Write-Output 'Temporary runs removed; reusable caches, artifacts and reports preserved.'
        $exitCode = 0
    } else {
        $runName = 'run-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
        $runDir = Assert-BuildPath (Join-Path $runsRoot $runName)
        $temporary = Join-Path $runDir 'tmp'
        $work = Join-Path $runDir 'work'
        $environment = @{
            MKLINK_BUILD_ROOT=$BuildRoot; MKLINK_BUILD_WORK_DIR=$work; MKLINK_BUILD_OUTPUT_DIR=$artifactsRoot
            TEMP=$temporary; TMP=$temporary; TMPDIR=$temporary
            PIP_CACHE_DIR=(Join-Path $cacheRoot 'pip'); npm_config_cache=(Join-Path $cacheRoot 'npm')
            PYINSTALLER_CONFIG_DIR=(Join-Path $cacheRoot 'pyinstaller'); CARGO_TARGET_DIR=(Join-Path $cacheRoot 'cargo')
            CARGO_HOME=(Join-Path $cacheRoot 'cargo-home')
            GOCACHE=(Join-Path $cacheRoot 'go-build'); GOMODCACHE=(Join-Path $cacheRoot 'go-mod')
            UV_CACHE_DIR=(Join-Path $cacheRoot 'uv'); MKLINK_VITE_CACHE_DIR=(Join-Path $cacheRoot 'vite')
        }
        foreach ($path in @($environment.Values) + @($reportsRoot)) {
            if ($path -eq $BuildRoot) { continue }
            $null = Assert-BuildPath $path
            [IO.Directory]::CreateDirectory($path) > $null
        }
        $environment.PYTHONDONTWRITEBYTECODE = '1'
        $environment.PYTEST_ADDOPTS = ($env:PYTEST_ADDOPTS + ' --basetemp="' + (Join-Path $runDir 'pytest') + '" -o cache_dir="' + (Join-Path $runDir 'pytest-cache') + '"').Trim()
        foreach ($name in $environment.Keys) {
            $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, $environment[$name], 'Process')
        }
        Push-Location -LiteralPath $WorkingDirectory
        try {
            Write-Output "Build storage: $BuildRoot"
            # Windows PowerShell converts native stderr records into
            # NativeCommandError objects.  With the script-wide Stop policy,
            # harmless diagnostics from Vite, Tauri, Cargo, and npm used to
            # abort the launcher before their real process exit code could be
            # observed.  Keep logging both streams, but let the native exit
            # code remain the verdict for this narrowly scoped invocation.
            $savedErrorActionPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                & $Executable @ArgumentList 2>&1 | Tee-Object -FilePath (Join-Path $reportsRoot ($runName + '.log'))
                $nativeExitCode = $LASTEXITCODE
                $commandSucceeded = $?
            } finally {
                $ErrorActionPreference = $savedErrorActionPreference
            }
            if ($null -ne $nativeExitCode) {
                $exitCode = $nativeExitCode
            } elseif ($commandSucceeded) {
                $exitCode = 0
            } else {
                $exitCode = 1
            }
        } finally { Pop-Location }
    }
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    try {
        if ($runDir -and (Test-Path -LiteralPath $runDir)) {
            $null = Assert-BuildPath $runDir
            if (@(Get-ChildItem -LiteralPath $runDir -Recurse -Force -Attributes ReparsePoint).Count) {
                Write-Warning "Temporary run contains test links; retained for safe inspection: $runDir"
            } else {
                try {
                    Remove-Item -LiteralPath $runDir -Recurse -Force -ErrorAction Stop
                } catch {
                    Write-Warning (
                        "Temporary run cleanup failed; retained for safe inspection: " +
                        "$runDir ($($_.Exception.Message))"
                    )
                }
            }
        }
    } finally { $lock.Dispose() }
}
exit $exitCode
