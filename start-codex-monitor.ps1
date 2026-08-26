[CmdletBinding()]
param(
    [string[]]$Project = @($PSScriptRoot),
    [string]$SessionRoot = "$env:USERPROFILE\.codex\sessions",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [ValidateRange(1, [int]::MaxValue)]
    [int]$StuckSeconds = 120
)

$ErrorActionPreference = "Stop"
Write-Verbose "Listening on 127.0.0.1 only"
$localUv = Join-Path $PSScriptRoot ".uv-bootstrap\bin\uv.exe"
$uv = if (Test-Path -LiteralPath $localUv -PathType Leaf) {
    $localUv
}
else {
    (Get-Command uv -ErrorAction Stop).Source
}

$uvArgs = @("run", "--no-sync", "python", "-m", "codex_monitor")
foreach ($path in $Project) {
    $uvArgs += "--project=$path"
}
$uvArgs += @("--session-root", $SessionRoot, "--stuck-seconds", $StuckSeconds, "--port", $Port)
& $uv @uvArgs
