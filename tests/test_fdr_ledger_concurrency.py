from __future__ import annotations

from mt5_agent.fdr_ledger import FDRLedger


def test_separate_ledger_instances_refresh_shared_denominator_and_release_lock(tmp_path):
    path = tmp_path / "fdr.jsonl"
    first = FDRLedger(path)
    second = FDRLedger(path)

    first.record("structural_hourweekday", "GOLD|0|00", 0.01)
    second.record("structural_hourweekday", "GOLD|1|00", 0.02)

    assert first.family_denominator("structural_hourweekday") == 2
    assert second.family_denominator("structural_hourweekday") == 2
    assert not (tmp_path / "fdr.jsonl.lock").exists()
