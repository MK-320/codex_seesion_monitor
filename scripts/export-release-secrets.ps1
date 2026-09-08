#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $env:USERPROFILE ".codex\codex-session-monitor-release-secrets"),
    [string]$PfxPath = (Join-Path $env:USERPROFILE "codex-session-monitor-selfsigned.pfx"),
    [string]$TauriKeyPath = (Join-Path $env:USERPROFILE ".tauri\codex-session-monitor.key"),
    [string]$UpdaterEndpoint = "https://github.com/MK-320/codex_seesion_monitor/releases/latest/download/latest.json",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Read-SecretText {
    param([Parameter(Mandatory)][string]$Prompt)

    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Write-SecretFile {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Value
    )

    $path = Join-Path $OutputDirectory $Name
    if ((Test-Path -LiteralPath $path) -and -not $Force) {
        throw "Secret file already exists: $path. Use -Force only when you intend to replace it."
    }

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($path, $Value, $utf8)
    return $path
}

if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    throw "USERPROFILE is not available. Provide explicit paths."
}

$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$PfxPath = [IO.Path]::GetFullPath($PfxPath)
$TauriKeyPath = [IO.Path]::GetFullPath($TauriKeyPath)
$tauriPublicKeyPath = "$TauriKeyPath.pub"
$certificatePath = [IO.Path]::ChangeExtension($PfxPath, ".cer")

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

if (-not (Test-Path -LiteralPath $PfxPath -PathType Leaf)) {
    Write-Host "Self-signed PFX was not found; starting the existing certificate generator."
    & (Join-Path $PSScriptRoot "prepare-self-signed-signing.ps1") -OutputPath $PfxPath
    if ($LASTEXITCODE -ne 0) {
        throw "Self-signed certificate generation failed."
    }
}

if (-not (Test-Path -LiteralPath $certificatePath -PathType Leaf)) {
    throw "Public certificate file is missing: $certificatePath"
}

$certificate = Get-PfxCertificate -FilePath $certificatePath
if ($null -eq $certificate -or [string]::IsNullOrWhiteSpace($certificate.Subject)) {
    throw "Unable to read the publisher subject from $certificatePath"
}

if (-not (Test-Path -LiteralPath $TauriKeyPath -PathType Leaf)) {
    $frontendPath = Join-Path $PSScriptRoot "..\frontend"
    if (-not (Test-Path -LiteralPath (Join-Path $frontendPath "package.json") -PathType Leaf)) {
        throw "Tauri frontend was not found. Generate the key manually, then rerun this script."
    }

    Write-Host "Tauri updater key was not found; starting signer generate. Keep the password you enter."
    Push-Location $frontendPath
    try {
        & npm exec tauri signer generate -- -w $TauriKeyPath
        if ($LASTEXITCODE -ne 0) {
            throw "Tauri updater key generation failed."
        }
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $TauriKeyPath -PathType Leaf)) {
    throw "Tauri private key was not created: $TauriKeyPath"
}
if (-not (Test-Path -LiteralPath $tauriPublicKeyPath -PathType Leaf)) {
    throw "Tauri public key is missing: $tauriPublicKeyPath"
}

$pfxPassword = Read-SecretText "Enter the self-signed PFX password used during certificate generation"
$tauriKeyPassword = Read-SecretText "Enter the Tauri updater private-key password"
$endpointInput = Read-Host "Tauri updater endpoint [$UpdaterEndpoint]"
if (-not [string]::IsNullOrWhiteSpace($endpointInput)) {
    $UpdaterEndpoint = $endpointInput.Trim()
}
if ($UpdaterEndpoint -notmatch '^https://') {
    throw "TAURI_UPDATER_ENDPOINT must use HTTPS."
}

$privateKey = [IO.File]::ReadAllText($TauriKeyPath)
$publicKey = [IO.File]::ReadAllText($tauriPublicKeyPath).Trim()
$certificateBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($PfxPath))

$files = @(
    (Write-SecretFile -Name "WINDOWS_SIGNING_PROVIDER" -Value "selfsigned")
    (Write-SecretFile -Name "WINDOWS_CERTIFICATE" -Value $certificateBase64)
    (Write-SecretFile -Name "WINDOWS_CERTIFICATE_PASSWORD" -Value $pfxPassword)
    (Write-SecretFile -Name "WINDOWS_EXPECTED_PUBLISHER" -Value $certificate.Subject)
    (Write-SecretFile -Name "TAURI_SIGNING_PRIVATE_KEY" -Value $privateKey)
    (Write-SecretFile -Name "TAURI_SIGNING_PRIVATE_KEY_PASSWORD" -Value $tauriKeyPassword)
    (Write-SecretFile -Name "TAURI_UPDATER_PUBLIC_KEY" -Value $publicKey)
    (Write-SecretFile -Name "TAURI_UPDATER_ENDPOINT" -Value $UpdaterEndpoint)
)

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $OutputDirectory /inheritance:r /grant:r "${identity}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Unable to tighten the output directory ACL. Confirm manually that only your account can access $OutputDirectory."
    }
} catch {
    Write-Warning "Unable to set the output directory ACL: $($_.Exception.Message)"
}

Write-Host "Generated 8 GitHub Environment Secret files:"
foreach ($file in $files) {
    Write-Host "- $file"
}
Write-Host ""
Write-Host "Open each file and copy its contents to:"
Write-Host "GitHub -> Settings -> Environments -> windows-production-signing -> Add environment secret"
Write-Host "The script does not print secret values or upload files. Delete the output directory after copying the secrets."
