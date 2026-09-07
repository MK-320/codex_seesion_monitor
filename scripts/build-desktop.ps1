[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path $repoRoot "frontend"
$buildSidecar = Join-Path $repoRoot "scripts\build-sidecar.ps1"
$staticRoot = Join-Path $repoRoot "src\codex_monitor\static"
$sidecarRoot = Join-Path $repoRoot "frontend\src-tauri\binaries\codex-monitor-sidecar-x86_64-pc-windows-msvc"
$sidecarStaticRoot = Join-Path $sidecarRoot "_internal\codex_monitor\static"

function Invoke-Npm([string[]]$Arguments) {
    & npm.cmd @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "npm command failed with exit code ${LASTEXITCODE}: npm $($Arguments -join ' ')"
    }
}

Push-Location $frontendRoot
try {
    # Tauri bundles frontend/dist, while the Python sidecar serves the tracked
    # static directory. Build both from the same source before packaging.
    Invoke-Npm @("run", "build:desktop")
    Invoke-Npm @("run", "build:static")
}
finally {
    Pop-Location
}

$sourceIndex = Join-Path $staticRoot "index.html"
$bundledIndex = Join-Path $sidecarStaticRoot "index.html"
$needsSidecarRefresh = -not (Test-Path -LiteralPath $sidecarRoot)
if (-not $needsSidecarRefresh) {
    $needsSidecarRefresh = -not (Test-Path -LiteralPath $bundledIndex)
}
if (-not $needsSidecarRefresh) {
    $needsSidecarRefresh = (Get-Content -LiteralPath $sourceIndex -Raw) -ne (Get-Content -LiteralPath $bundledIndex -Raw)
}

if ($needsSidecarRefresh) {
    & $buildSidecar -Refresh
    if ($LASTEXITCODE -ne 0) {
        throw "Sidecar refresh failed with exit code $LASTEXITCODE."
    }
}
