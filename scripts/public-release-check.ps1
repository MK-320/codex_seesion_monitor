[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Root = (Get-Location).Path,
    [Parameter(Mandatory = $false)]
    [string]$PublicRoot = $Root,
    [Parameter(Mandatory = $false)]
    [string]$ConfirmationPath = (Join-Path (Split-Path -Parent $PublicRoot) "public-release-approval.json")
)

$ErrorActionPreference = "Stop"
uv run python -m codex_monitor.release_check --root $Root --public-root $PublicRoot --confirmation $ConfirmationPath
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "Public export gate passed."
