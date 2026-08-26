$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path $repoRoot "frontend"
$sessionRoot = if ($env:CODEX_SESSION_ROOT) { $env:CODEX_SESSION_ROOT } else { Join-Path $env:USERPROFILE ".codex\sessions" }
$backendPort = if ($env:CODEX_MONITOR_PORT) { [int]$env:CODEX_MONITOR_PORT } else { 8766 }
$vitePort = if ($env:CODEX_MONITOR_VITE_PORT) { [int]$env:CODEX_MONITOR_VITE_PORT } else { 5173 }
$backend = $null
$exitCode = 0

if ($backendPort -lt 1 -or $backendPort -gt 65535 -or $vitePort -lt 1 -or $vitePort -gt 65535) {
    throw "Development ports must be between 1 and 65535: backend=$backendPort, vite=$vitePort"
}

function Get-MonitorHealth([int]$Port) {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Get-ListeningPort([int]$Port) {
    return Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
}

function Test-ViteDevServer([int]$Port) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content -match "Codex Session Monitor"
    }
    catch {
        return $false
    }
}

$pathKeys = @([Environment]::GetEnvironmentVariables("Process").Keys | Where-Object { $_ -ieq "Path" })
if ($pathKeys.Count -gt 1) {
    Remove-Item Env:PATH -ErrorAction SilentlyContinue
}

try {
    $listener = Get-ListeningPort $backendPort | Where-Object { $_.LocalAddress -eq "127.0.0.1" -or $_.LocalAddress -eq "0.0.0.0" } | Select-Object -First 1
    $existingHealth = if ($null -eq $listener) { $null } else { Get-MonitorHealth $backendPort }
    if ($null -eq $listener) {
        $backend = Start-Process -FilePath "uv" -ArgumentList @(
            "run", "python", "-m", "codex_monitor",
            "--project", $repoRoot,
            "--session-root", $sessionRoot,
            "--port", $backendPort,
            "--no-saved-projects"
        ) -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru
    }
    elseif ($null -eq $existingHealth -or $existingHealth.listen_host -ne "127.0.0.1") {
        throw "127.0.0.1:$backendPort is already used by another service; set CODEX_MONITOR_PORT to another port"
    }

    $viteListener = Get-ListeningPort $vitePort | Select-Object -First 1
    if ($null -ne $viteListener) {
        if (Test-ViteDevServer $vitePort) {
            throw "Codex Session Monitor Vite dev server is already running on 127.0.0.1:$vitePort; open it directly or stop the existing server"
        }
        throw "127.0.0.1:$vitePort is already used by another service; set CODEX_MONITOR_VITE_PORT to another port"
    }

    $healthy = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        try {
            $health = Get-MonitorHealth $backendPort
            if ($null -ne $health -and $health.listen_host -eq "127.0.0.1") {
                $healthy = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $healthy) {
        throw "Backend service did not start within 22.5 seconds on 127.0.0.1:$backendPort"
    }

    $env:CODEX_MONITOR_PORT = "$backendPort"
    $env:CODEX_MONITOR_VITE_PORT = "$vitePort"
    Push-Location $frontendRoot
    # Bind browser development to loopback and open the local entry point.
    npm run dev:vite -- --host 127.0.0.1 --open /static/
    $exitCode = $LASTEXITCODE
}
finally {
    if ((Get-Location).Path -eq $frontendRoot) {
        Pop-Location
    }
    if ($null -ne $backend -and -not $backend.HasExited) {
        $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        & $taskkill /PID $backend.Id /T /F | Out-Null
        if (-not $backend.WaitForExit(5000)) {
            Stop-Process -Id $backend.Id -Force
        }
    }
}

exit $exitCode
