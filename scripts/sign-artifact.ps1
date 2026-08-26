param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath
)

$ErrorActionPreference = "Stop"

foreach ($name in @(
    "AZURE_ARTIFACT_SIGNING_ENDPOINT",
    "AZURE_ARTIFACT_SIGNING_ACCOUNT",
    "AZURE_ARTIFACT_SIGNING_PROFILE"
)) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Missing Azure Artifact Signing setting: $name"
    }
}

$resolvedFile = Resolve-Path -LiteralPath $FilePath
Invoke-ArtifactSigning `
    -Endpoint $env:AZURE_ARTIFACT_SIGNING_ENDPOINT `
    -CodeSigningAccountName $env:AZURE_ARTIFACT_SIGNING_ACCOUNT `
    -CertificateProfileName $env:AZURE_ARTIFACT_SIGNING_PROFILE `
    -Files $resolvedFile.Path `
    -FileDigest SHA256 `
    -TimestampRfc3161 "http://timestamp.acs.microsoft.com" `
    -TimestampDigest SHA256 `
    -ExcludeEnvironmentCredential $true `
    -ExcludeWorkloadIdentityCredential $true `
    -ExcludeManagedIdentityCredential $true `
    -ExcludeSharedTokenCacheCredential $true `
    -ExcludeVisualStudioCredential $true `
    -ExcludeVisualStudioCodeCredential $true `
    -ExcludeAzureCliCredential $false `
    -ExcludeAzurePowerShellCredential $true `
    -ExcludeAzureDeveloperCliCredential $true `
    -ExcludeInteractiveBrowserCredential $true

if (-not [string]::IsNullOrWhiteSpace($env:CODEX_SIGNING_LOG)) {
    $resolvedFile.Path | Add-Content -LiteralPath $env:CODEX_SIGNING_LOG -Encoding utf8
}
