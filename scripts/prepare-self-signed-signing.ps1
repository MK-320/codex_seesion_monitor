param(
    [string]$OutputPath = (Join-Path $env:USERPROFILE "codex-session-monitor-selfsigned.pfx"),
    [string]$Subject = "CN=Codex Session Monitor",
    [int]$ValidityDays = 730
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    throw "USERPROFILE is not available. Provide -OutputPath explicitly."
}

$output = [IO.Path]::GetFullPath($OutputPath)
$parent = Split-Path -Parent $output
if (-not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

$password = Read-Host "Enter a password for the self-signed PFX" -AsSecureString
$certificate = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject $Subject `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -HashAlgorithm SHA256 `
    -KeyAlgorithm RSA `
    -KeyLength 3072 `
    -KeyExportPolicy Exportable `
    -NotAfter (Get-Date).AddDays($ValidityDays)

Export-PfxCertificate -Cert $certificate -FilePath $output -Password $password | Out-Null
$publicPath = [IO.Path]::ChangeExtension($output, ".cer")
Export-Certificate -Cert $certificate -FilePath $publicPath | Out-Null

Write-Output "Self-signed release certificate created."
Write-Output "PFX: $output"
Write-Output "Public certificate: $publicPath"
Write-Output "Publisher subject: $($certificate.Subject)"
Write-Output "Thumbprint: $($certificate.Thumbprint)"
Write-Output "This certificate identifies the formal release publisher but is self-signed and is not publicly trusted by Windows by default."
