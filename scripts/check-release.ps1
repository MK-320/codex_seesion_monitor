$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Invoke-Checked([scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE"
    }
}

Push-Location $root
try {
    Invoke-Checked { uv run python -m codex_monitor.release_check --root $root }
    Invoke-Checked { uv run ruff check . }
    Invoke-Checked { uv run ruff format --check . }
    Invoke-Checked { uv run basedpyright }
    Invoke-Checked { uv run pytest --cov=codex_monitor --cov-report=term-missing }
    Push-Location (Join-Path $root "frontend")
    try {
        Invoke-Checked { npm run typecheck }
        Invoke-Checked { git -C $root diff --exit-code -- src/codex_monitor/static }
        Invoke-Checked { npm run build }
        if ([string]::IsNullOrWhiteSpace($env:CODEX_RELEASE_VERSION)) {
            Invoke-Checked { git -C $root diff --exit-code -- src/codex_monitor/static }
        }
        Invoke-Checked { npm run e2e }
    }
    finally {
        Pop-Location
    }
    Invoke-Checked { uv run python -m codex_monitor.release_check --root $root }
}
finally {
    Pop-Location
}
