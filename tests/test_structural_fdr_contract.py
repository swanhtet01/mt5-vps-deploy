from __future__ import annotations

import ast
from pathlib import Path

from mt5_agent.fdr_ledger import FDRLedger


ROOT = Path(__file__).resolve().parents[1]


def test_structural_family_is_registered_for_cumulative_fdr(tmp_path):
    ledger = FDRLedger(tmp_path / "fdr.jsonl")
    ledger.record("structural_hourweekday", "GOLD|0|00", 0.001, n=100)

    assert ledger.family_denominator("structural_hourweekday") == 1


def test_structural_walk_forward_records_and_requires_cumulative_fdr():
    source = (ROOT / "hotfix" / "scripts" / "structural_walk_forward.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert "FDRLedger" in names
    assert "cumulative_discovery" in names
    assert "cumulative_fdr_ledger_required" in source
