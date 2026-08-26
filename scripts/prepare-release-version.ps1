param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Root,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = Split-Path -Parent $PSScriptRoot
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Formal release version must be stable SemVer (for example 1.0.0): $Version"
}

function Read-Utf8([string]$Path) {
    return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
}

function Write-Utf8([string]$Path, [string]$Content) {
    if ($DryRun) {
        return
    }
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Replace-Unique([string]$RelativePath, [string]$Pattern, [string]$Replacement) {
    $path = Join-Path $Root $RelativePath
    $content = Read-Utf8 $path
    $matches = [regex]::Matches($content, $Pattern)
    if ($matches.Count -ne 1) {
        throw "Expected exactly one version declaration in $RelativePath, found $($matches.Count)."
    }
    $updated = [regex]::Replace($content, $Pattern, $Replacement, 1)
    Write-Utf8 $path $updated
}

Replace-Unique "src/codex_monitor/__init__.py" '(?m)^__version__[ \t]*=[ \t]*"[^"]+"[ \t]*\r?$' "__version__ = `"$Version`""
Replace-Unique "pyproject.toml" '(?m)^version[ \t]*=[ \t]*"[^"]+"[ \t]*\r?$' "version = `"$Version`""
Replace-Unique "frontend/package.json" '(?m)^  "version":[ \t]*"[^"]+",\r?$' "  `"version`": `"$Version`","
Replace-Unique "frontend/package-lock.json" '(?m)^  "version":[ \t]*"[^"]+",\r?$' "  `"version`": `"$Version`","
Replace-Unique "frontend/src-tauri/Cargo.toml" '(?m)^version[ \t]*=[ \t]*"[^"]+"[ \t]*\r?$' "version = `"$Version`""
Replace-Unique "frontend/src-tauri/tauri.conf.json" '(?m)^  "version":[ \t]*"[^"]+",\r?$' "  `"version`": `"$Version`","

$packageLockPath = Join-Path $Root "frontend/package-lock.json"
$packageLock = Read-Utf8 $packageLockPath
$packageLockPattern = '(?ms)(^    "": \{\r?\n      "name": "codex-session-monitor-ui",\r?\n      "version": )"[^"]+"'
$packageLockMatches = [regex]::Matches($packageLock, $packageLockPattern)
if ($packageLockMatches.Count -ne 1) {
    throw "Expected exactly one root package version declaration in frontend/package-lock.json, found $($packageLockMatches.Count)."
}
$packageLockUpdated = [regex]::Replace($packageLock, $packageLockPattern, ('$1"' + $Version + '"'), 1)
Write-Utf8 $packageLockPath $packageLockUpdated

$lockPath = Join-Path $Root "frontend/src-tauri/Cargo.lock"
$lock = Read-Utf8 $lockPath
$lockPattern = '(?ms)(\[\[package\]\]\s*name = "codex-session-monitor"\s*version = )"[^"]+"'
$lockMatches = [regex]::Matches($lock, $lockPattern)
if ($lockMatches.Count -ne 1) {
    throw "Expected exactly one codex-session-monitor package entry in frontend/src-tauri/Cargo.lock, found $($lockMatches.Count)."
}
$lockUpdated = [regex]::Replace($lock, $lockPattern, ('$1"' + $Version + '"'), 1)
Write-Utf8 $lockPath $lockUpdated

if (-not $DryRun -and -not [string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) {
    "CODEX_RELEASE_VERSION=$Version" | Add-Content -LiteralPath $env:GITHUB_ENV -Encoding utf8
}

Write-Output "Prepared formal release version overlay: $Version"
