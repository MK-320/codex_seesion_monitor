from pathlib import Path


def test_sidecar_build_is_checkout_relative_and_reusable_as_a_release_gate() -> None:
    script = Path("scripts/build-sidecar.ps1").read_text(encoding="utf-8")

    assert "E:\\Tauri" not in script
    assert "$repoRoot" in script
    assert "function Test-SidecarArtifact" in script
    assert "[switch]$VerifyOnly" in script
    assert "codex_monitor\\static\\index.html" in script
    assert "Sidecar artifact is incomplete" in script


def test_azure_artifact_signing_wrapper_is_auditable_and_secret_free() -> None:
    script = Path("scripts/sign-artifact.ps1").read_text(encoding="utf-8")

    assert "Invoke-ArtifactSigning" in script
    assert "AZURE_ARTIFACT_SIGNING_ENDPOINT" in script
    assert "AZURE_ARTIFACT_SIGNING_ACCOUNT" in script
    assert "AZURE_ARTIFACT_SIGNING_PROFILE" in script
    assert "http://timestamp.acs.microsoft.com" in script
    assert "CODEX_SIGNING_LOG" in script
    assert "AZURE_CLIENT_SECRET" not in script


def test_pfx_signing_wrapper_uses_environment_credentials_and_logs_targets() -> None:
    script = Path("scripts/sign-pfx.ps1").read_text(encoding="utf-8")

    assert "signtool sign" in script
    assert "WINDOWS_CERTIFICATE_PATH" in script
    assert "WINDOWS_CERTIFICATE_PASSWORD" in script
    assert "CODEX_SIGNING_LOG" in script
    assert "https://timestamp.digicert.com" in script
    assert "Get-Command signtool.exe" in script
    assert "Windows Kits\\10\\bin" in script
    assert '"x64\\signtool.exe"' in script
