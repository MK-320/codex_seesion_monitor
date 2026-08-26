$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path $repoRoot "frontend"
$rustRoot = "E:\Rust"
$cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $rustRoot "cargo" }
$env:CARGO_HOME = $cargoHome
$env:RUSTUP_HOME = if ($env:RUSTUP_HOME) { $env:RUSTUP_HOME } else { Join-Path $rustRoot "rustup" }
$env:CARGO_TARGET_DIR = Join-Path $repoRoot ".cargo-target"
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

$vite = $null
$exitCode = 0

function Test-ViteDevServer([int]$Port) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content -match "Codex Session Monitor"
    }
    catch {
        return $false
    }
}

try {
    $viteListener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 1420 -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $viteListener) {
        $vite = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "dev:desktop") -WorkingDirectory $frontendRoot -WindowStyle Hidden -PassThru
    }
    elseif (-not (Test-ViteDevServer 1420)) {
        throw "127.0.0.1:1420 is already used by another service; desktop development cannot start."
    }
    $viteReady = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        try {
            if (Test-ViteDevServer 1420) {
                $viteReady = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $viteReady) {
        throw "Vite dev server did not start within 22.5 seconds on 127.0.0.1:1420"
    }
    Push-Location $frontendRoot
    npm.cmd exec tauri dev
    $exitCode = $LASTEXITCODE
}
finally {
    if ((Get-Location).Path -eq $frontendRoot) {
        Pop-Location
    }
    if ($null -ne $vite -and -not $vite.HasExited) {
        $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        & $taskkill /PID $vite.Id /T /F | Out-Null
        if (-not $vite.WaitForExit(5000)) {
            Stop-Process -Id $vite.Id -Force
        }
    }
}

exit $exitCode
