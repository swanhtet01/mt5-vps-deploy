from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_structural_runner_is_bounded_read_only_and_records_state():
    source = (ROOT / "hotfix" / "scripts" / "run-structural-walk-forward.ps1").read_text(encoding="utf-8")

    assert "Start-Process" in source
    assert "WaitForExit" in source
    assert "Stop-Process" in source
    assert "$null -ne $exitCode" in source
    assert "structural_walk_forward_state.json" in source
    assert "report_sha256" in source
    assert "order_authority = $false" in source
    assert "order_send" not in source


def test_updater_registers_bounded_weekly_structural_research_task():
    source = (ROOT / "update.ps1").read_text(encoding="utf-8")

    assert "MT5-StructuralWalkForward" in source
    assert "run-structural-walk-forward.ps1" in source
    assert "Set-MT5TaskReliability -TaskName 'MT5-StructuralWalkForward' -ExecutionMinutes 55" in source
