from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_auto_deploy_receipt_is_best_effort_and_after_completion_marker():
    script = (ROOT / "update.ps1").read_text(encoding="utf-8")

    marker = 'Set-Content "$deploy\\last_update_complete.txt" $deployRef -NoNewline'
    receipt = "$message = if ($env:MT5_AUTODEPLOY)"
    assert marker in script
    assert receipt in script
    assert script.index(marker) < script.index(receipt)
    assert "live authorization unchanged" in script

    receipt_block = script[script.index(receipt) :]
    assert "SetEnvironmentVariable('MT5_GOLD_DRIFT_LIVE'" not in receipt_block


def test_durable_deployment_receipt_precedes_success_marker():
    script = (ROOT / "update.ps1").read_text(encoding="utf-8")

    durable = "$deploymentReceipt = [ordered]@{"
    marker = 'Set-Content "$deploy\\last_update_complete.txt" $deployRef -NoNewline'
    assert durable in script
    assert script.index(durable) < script.index(marker)
    assert "mt5.deployment_receipt.v1" in script
    assert "manifest_sha256 = $hotfixManifestSha256" in script
    assert "live_authorization_changed = $false" in script


def test_manifest_sync_failure_leaves_a_durable_diagnostic_receipt():
    script = (ROOT / "update.ps1").read_text(encoding="utf-8")

    assert "last_hotfix_sync_failure.json" in script
    assert "function Invoke-VerifiedHotfixSync" in script
    assert "Invoke-VerifiedHotfixSync -Phase 'pre_bundle'" in script
    assert "Invoke-VerifiedHotfixSync -Phase 'post_bundle'" in script
    assert "mt5.hotfix_sync_failure.v1" in script
