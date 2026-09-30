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
