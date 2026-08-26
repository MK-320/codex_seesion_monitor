[CmdletBinding()]
param(
    [switch]$VerifyOnly,
    [switch]$Refresh
)

$ErrorActionPreference = "Stop"

function Test-SidecarArtifact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ArtifactRoot,
        [Parameter(Mandatory)]
        [string]$Name
    )

    $required = @(
        (Join-Path $ArtifactRoot "$Name.exe"),
        (Join-Path $ArtifactRoot "_internal"),
        (Join-Path $ArtifactRoot "_internal\codex_monitor\static\index.html")
    )
    foreach ($path in $required) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Sidecar artifact is incomplete: $path"
        }
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$name = "codex-monitor-sidecar-x86_64-pc-windows-msvc"
$distRoot = Join-Path $repoRoot "frontend\src-tauri\binaries"
$artifactRoot = Join-Path $distRoot $name
$buildRoot = Join-Path ([System.IO.Path]::GetTempPath()) "codex-session-monitor-sidecar-$PID"
$workRoot = Join-Path $buildRoot "work"
$specRoot = Join-Path $buildRoot "spec"
$refreshDistRoot = Join-Path $buildRoot "refresh-dist"

if ($VerifyOnly) {
    Test-SidecarArtifact -ArtifactRoot $artifactRoot -Name $name
    return
}

if ((Test-Path -LiteralPath $artifactRoot) -and -not $Refresh) {
    throw "Sidecar output already exists: $artifactRoot. Run .\scripts\build-sidecar.ps1 -Refresh to rebuild without deleting the current artifact."
}

$buildDistRoot = if ($Refresh) { $refreshDistRoot } else { $distRoot }
$buildArtifactRoot = Join-Path $buildDistRoot $name
New-Item -ItemType Directory -Force -Path $buildDistRoot, $workRoot, $specRoot | Out-Null
Push-Location $repoRoot
try {
    # --noconsole: Windows GUI subsystem so Tauri-launched sidecar has no black console window.
    # Parent still pipes stdout for ready/diagnostic lines; do not rely on a visible console.
    uv run pyinstaller --noconfirm --noupx --noconsole --onedir --name $name --paths "$repoRoot\src" --collect-all codex_monitor --exclude-module tkinter --exclude-module _tkinter --distpath $buildDistRoot --workpath $workRoot --specpath $specRoot "$repoRoot\src\codex_monitor\__main__.py"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
    Test-SidecarArtifact -ArtifactRoot $buildArtifactRoot -Name $name
    if ($Refresh) {
        Copy-Item -LiteralPath (Join-Path $buildArtifactRoot "$name.exe") -Destination (Join-Path $artifactRoot "$name.exe") -Force
        Copy-Item -LiteralPath (Join-Path $buildArtifactRoot "_internal") -Destination $artifactRoot -Recurse -Force
        Test-SidecarArtifact -ArtifactRoot $artifactRoot -Name $name
    }
}
finally {
    Pop-Location
}
