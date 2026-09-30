import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hotfix" / "src"))
from mt5_agent.structural_challengers import paper_specs


def test_receipt_bound_candidate_is_permanently_paper_only(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"mode": "read_only_research", "orders_sent": 0,
        "research_scope": {"canonical_full_universe": True, "paper_eligibility_allowed": True, "fdr_family": "structural_hourweekday"},
        "paper_candidates": [{"spec_id": "GOLD|1|03", "symbol": "GOLD", "entry_weekday": 1, "entry_hour": 2, "exit_hour": 3, "oos": {"direction": "long"}}]}), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"schema": "mt5.structural_walk_forward_state.v1", "status": "completed", "order_authority": False,
        "output": str(report), "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(), "finished_at_utc": "2026-09-30T12:00:00Z"}), encoding="utf-8")
    specs = paper_specs(state, now=datetime(2026, 9, 30, 13, tzinfo=timezone.utc))
    assert list(specs.values())[0]["paper_only"] is True
