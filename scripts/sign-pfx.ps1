param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath
)

$ErrorActionPreference = "Stop"

foreach ($name in @("WINDOWS_CERTIFICATE_PATH", "WINDOWS_CERTIFICATE_PASSWORD")) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Missing PFX signing setting: $name"
    }
}

$signtool = $null
$pathCommand = Get-Command signtool.exe -CommandType Application -ErrorAction SilentlyContinue
if ($null -ne $pathCommand) {
    $signtool = $pathCommand.Path
}

if ([string]::IsNullOrWhiteSpace($signtool)) {
    $programFilesRoots = @(
        [Environment]::GetEnvironmentVariable("ProgramFiles(x86)"),
        [Environment]::GetEnvironmentVariable("ProgramFiles")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique

    $sdkBinRoots = @()
    if (-not [string]::IsNullOrWhiteSpace($env:WindowsSdkDir)) {
        $sdkBinRoots += Join-Path $env:WindowsSdkDir "bin"
    }
    foreach ($programFilesRoot in $programFilesRoots) {
        $sdkBinRoots += Join-Path $programFilesRoot "Windows Kits\10\bin"
        $sdkBinRoots += Join-Path $programFilesRoot "Windows Kits\11\bin"
    }

    foreach ($sdkBinRoot in ($sdkBinRoots | Select-Object -Unique)) {
        $versionDirectories = Get-ChildItem -LiteralPath $sdkBinRoot -Directory -ErrorAction SilentlyContinue |
            Sort-Object -Property Name -Descending
        foreach ($versionDirectory in $versionDirectories) {
            $candidate = Join-Path $versionDirectory.FullName "x64\signtool.exe"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $signtool = (Resolve-Path -LiteralPath $candidate).Path
                break
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($signtool)) { break }
    }
}

if ([string]::IsNullOrWhiteSpace($signtool)) {
    throw "signtool.exe was not found in PATH or the installed Windows SDK."
}

$resolvedFile = Resolve-Path -LiteralPath $FilePath
& $signtool sign `
    /fd SHA256 `
    /tr http://timestamp.digicert.com `
    /td SHA256 `
    /f $env:WINDOWS_CERTIFICATE_PATH `
    /p $env:WINDOWS_CERTIFICATE_PASSWORD `
    $resolvedFile.Path
if ($LASTEXITCODE -ne 0) {
    throw "Authenticode signing failed for $($resolvedFile.Path)."
}

if (-not [string]::IsNullOrWhiteSpace($env:CODEX_SIGNING_LOG)) {
    $resolvedFile.Path | Add-Content -LiteralPath $env:CODEX_SIGNING_LOG -Encoding utf8
}
