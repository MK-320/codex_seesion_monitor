from pathlib import Path

WORKFLOW = Path(".github/workflows/windows-release.yml")


def test_windows_release_workflow_keeps_unsigned_and_formal_release_paths_separate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "tags:" in workflow
    assert '- "v*"' in workflow
    assert "unsigned-verify:" in workflow
    assert "release:" in workflow
    assert "github.event_name != 'push'" in workflow
    assert "github.event_name == 'push'" in workflow
    for marker in (
        "TAURI_SIGNING_PRIVATE_KEY",
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
        "TAURI_UPDATER_PUBLIC_KEY",
        "TAURI_UPDATER_ENDPOINT",
        "WINDOWS_EXPECTED_PUBLISHER",
        "GH_TOKEN: ${{ github.token }}",
        "Missing required release credential",
        "Get-AuthenticodeSignature",
        "SignerCertificate.Subject",
        "TimeStamperCertificate",
        "scripts/build-sidecar.ps1",
        "src-tauri/tauri.release.conf.json",
        "TAURI_CI_RELEASE_CONFIG",
        "CODEX_MONITOR_RECOVERY_URL",
        "PREVIOUS_STABLE_TAG",
        "Validate previous stable release assets",
        "release download",
        '--pattern "*.sig"',
        "Previous stable updater metadata verification failed.",
        "Previous stable checksum verification failed",
        "Split-Path -Leaf",
        "Signed NSIS uninstaller target evidence is missing",
        "Signed main executable target evidence is missing",
        "Signed sidecar target evidence is missing",
        "Signed NSIS setup.exe target evidence is missing",
        'Get-ChildItem $bundleRoot -Filter "*setup.exe"',
        "tauri build",
        "checksums.sha256",
        "release-manifest.json",
        "latest.json",
        '"windows-x86_64"',
        "signature_status",
        "Authenticode verified; Tauri updater signature generated",
        "test_evidence",
        "actions/runs/${{ github.run_id }}",
        "current_stable_tag",
        "previous_stable_tag",
    ):
        assert marker in workflow
    assert workflow.index("scripts/build-sidecar.ps1") < workflow.index("tauri build")
    assert "sha256" in workflow
    assert "*.nsis.zip" not in workflow

    unsigned_workflow = workflow.split("  unsigned-verify:", maxsplit=1)[1].split(
        "  release:", maxsplit=1
    )[0]
    assert "TAURI_UPDATER_PUBLIC_KEY" not in unsigned_workflow
    assert "TAURI_UPDATER_ENDPOINT" not in unsigned_workflow

    release_workflow = workflow.split("  release:", maxsplit=1)[1]
    release_build = release_workflow.index("npm exec tauri build")
    signing_config = release_workflow[:release_build]
    for marker in (
        "signCommand",
        "plugins",
        "endpoints",
        "pubkey",
        "scripts/sign-pfx.ps1",
        '$pwshPath = Join-Path $PSHOME "pwsh.exe"',
        "Test-Path -LiteralPath $pwshPath -PathType Leaf",
        "$signCommand = @{",
        "cmd = $pwshPath",
        '"%1"',
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
    ):
        assert marker in signing_config
    # String form cannot carry "Program Files\\...\\pwsh.exe"; object form is required.
    assert '"`"$pwshPath`"' not in signing_config
    # Tauri restores an unsigned main binary after packaging; verify durable artifacts.
    # NSIS embeds the uninstaller via a temp nst*.tmp signing target.
    verify_config = release_workflow[release_build:]
    for marker in (
        "@($sidecar, $installer)",
        "*setup.exe",
        "codex-session-monitor.exe",
        "codex-monitor-sidecar-x86_64-pc-windows-msvc.exe",
        "^nst.+\\.tmp$",
        "uninstall.exe",
    ):
        assert marker in verify_config


def test_windows_release_workflow_limits_permissions_and_validates_the_tag() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    release_workflow = workflow.split("  release:", maxsplit=1)[1]

    assert "permissions:\n  contents: read" in workflow
    assert "permissions:\n      contents: write" in release_workflow
    assert "id-token: write" in release_workflow
    assert "windows-production-signing" in workflow
    assert "Tag must point to the latest origin/master commit" in workflow
    assert "Tag version" in workflow
    assert "scripts/check-release.ps1" in workflow
    assert "signing-targets.txt" in workflow
    assert "signing-evidence.json" in workflow
    assert 'Copy-Item -LiteralPath $env:CODEX_SIGNING_LOG' not in workflow
    assert "Raw signing target paths must not be published." in workflow
    assert "Updater metadata installer name does not match the packaged installer." in workflow
    assert "Installer and updater signature must be included in the release asset set." in workflow
    assert "@uploadFiles" in workflow
    assert "npm ci;" not in workflow
    assert workflow.count("uv sync --dev --python 3.12") == 2


def test_windows_release_workflow_supports_azure_and_pfx_signing() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    for secret in (
        "WINDOWS_CERTIFICATE",
        "WINDOWS_CERTIFICATE_PASSWORD",
        "WINDOWS_SIGNING_PROVIDER",
        "AZURE_ARTIFACT_SIGNING_ENDPOINT",
        "AZURE_ARTIFACT_SIGNING_ACCOUNT",
        "AZURE_ARTIFACT_SIGNING_PROFILE",
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
    ):
        assert secret in workflow
    assert "Install-Module -Name ArtifactSigning -RequiredVersion 0.1.8" in workflow
    assert "scripts/sign-artifact.ps1" in workflow
    assert "azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43" in workflow
    assert "AZURE_CLIENT_SECRET" not in workflow
    assert "artifact-signing-cli" not in workflow
    assert "WINDOWS_SIGNING_PROVIDER must be azure, pfx, or selfsigned" in workflow
