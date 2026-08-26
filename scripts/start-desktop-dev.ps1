$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path $repoRoot "frontend"
$rustRoot = "E:\Rust"
$cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $rustRoot "cargo" }
$env:CARGO_HOME = $cargoHome
$env:RUSTUP_HOME = if ($env:RUSTUP_HOME) { $env:RUSTUP_HOME } else { Join-Path $rustRoot "rustup" }
$targetRoot = Join-Path $repoRoot ".cargo-target"
$env:CARGO_TARGET_DIR = $targetRoot
$cargoBin = Join-Path $cargoHome "bin"
if (Test-Path -LiteralPath $cargoBin) {
    $env:Path = "$cargoBin;$env:Path"
}
$vcvars = "E:\Tauri\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if (-not (Test-Path -LiteralPath $vcvars)) {
    throw "Visual Studio Build Tools were not found at $vcvars"
}
foreach ($entry in (& $env:ComSpec /d /s /c "`"$vcvars`" && set")) {
    if ($entry -match "^(?<name>[^=]+)=(?<value>.*)$") {
        Set-Item -Path "Env:$($Matches.name)" -Value $Matches.value
    }
}

$pathKeys = @([Environment]::GetEnvironmentVariables("Process").Keys | Where-Object { $_ -ieq "Path" })
if ($pathKeys.Count -gt 1) {
    Remove-Item Env:PATH -ErrorAction SilentlyContinue
}

$sidecar = Join-Path $frontendRoot "src-tauri\binaries\codex-monitor-sidecar-x86_64-pc-windows-msvc\codex-monitor-sidecar-x86_64-pc-windows-msvc.exe"
$sidecarBuild = Join-Path $PSScriptRoot "build-sidecar.ps1"
$exitCode = 0

function Test-SidecarRefreshRequired {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Artifact,
        [Parameter(Mandatory)]
        [string]$RepositoryRoot
    )

    if (-not (Test-Path -LiteralPath $Artifact)) {
        return $true
    }

    $artifactWriteTime = (Get-Item -LiteralPath $Artifact).LastWriteTimeUtc
    $inputs = @(
        (Join-Path $RepositoryRoot "src"),
        (Join-Path $RepositoryRoot "pyproject.toml"),
        (Join-Path $RepositoryRoot "uv.lock"),
        (Join-Path $RepositoryRoot "scripts\build-sidecar.ps1")
    )
    foreach ($input in $inputs) {
        if (-not (Test-Path -LiteralPath $input)) {
            continue
        }
        if ((Get-Item -LiteralPath $input).PSIsContainer) {
            if (Get-ChildItem -LiteralPath $input -File -Recurse | Where-Object { $_.LastWriteTimeUtc -gt $artifactWriteTime } | Select-Object -First 1) {
                return $true
            }
        }
        elseif ((Get-Item -LiteralPath $input).LastWriteTimeUtc -gt $artifactWriteTime) {
            return $true
        }
    }
    return $false
}

try {
    if (Test-SidecarRefreshRequired -Artifact $sidecar -RepositoryRoot $repoRoot) {
        if (Test-Path -LiteralPath $sidecar) {
            & $sidecarBuild -Refresh
        }
        else {
            & $sidecarBuild
        }
    }
    Push-Location $frontendRoot
    npm run desktop:dev
    $exitCode = $LASTEXITCODE
}
finally {
    if ((Get-Location).Path -eq $frontendRoot) {
        Pop-Location
    }
}

exit $exitCode
