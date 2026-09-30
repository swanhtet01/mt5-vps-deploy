import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hotfix" / "src"))
from mt5_agent.structural_challengers import (
    CANONICAL_SYMBOLS,
    CANONICAL_UNIVERSE_SHA256,
    paper_challenger_status,
    paper_specs,
)


def _receipt(tmp_path, candidates):
    report_root = tmp_path / "reports"
    report_root.mkdir()
    report = report_root / "report.json"
    report.write_text(json.dumps({"mode": "read_only_research", "orders_sent": 0,
        "research_scope": {"symbols": sorted(CANONICAL_SYMBOLS), "universe_sha256": CANONICAL_UNIVERSE_SHA256,
                           "canonical_full_universe": True,
                           "paper_eligibility_allowed": True, "fdr_family": "structural_hourweekday"},
        "paper_candidates": candidates}), encoding="utf-8")
    state_root = tmp_path / "data_cache"
    state_root.mkdir()
    state = state_root / "state.json"
    state.write_text(json.dumps({"schema": "mt5.structural_walk_forward_state.v1", "status": "completed", "order_authority": False,
        "output": str(report), "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(), "finished_at_utc": "2026-09-30T12:00:00Z"}), encoding="utf-8")
    return state


def test_receipt_bound_candidate_is_permanently_paper_only(tmp_path):
    state = _receipt(tmp_path, [{"spec_id": "GOLD|1|03", "symbol": "GOLD", "entry_weekday": 1, "entry_hour": 2, "exit_hour": 3, "oos": {"direction": "long"}}])
    specs = paper_specs(state, now=datetime(2026, 9, 30, 13, tzinfo=timezone.utc))
    assert list(specs.values())[0]["paper_only"] is True


def test_receipt_rejects_out_of_scope_candidate_as_a_whole(tmp_path):
    state = _receipt(tmp_path, [{"spec_id": "NOT_A_SYMBOL|1|03", "symbol": "NOT_A_SYMBOL", "entry_weekday": 1, "entry_hour": 2, "exit_hour": 3, "oos": {"direction": "long"}}])

    try:
        paper_specs(state, now=datetime(2026, 9, 30, 13, tzinfo=timezone.utc))
    except ValueError as exc:
        assert "identity" in str(exc)
    else:
        raise AssertionError("out-of-scope candidate must fail closed")


def test_zero_candidates_is_healthy_paper_evidence(tmp_path):
    state = _receipt(tmp_path, [])

    status = paper_challenger_status(state, now=datetime(2026, 9, 30, 13, tzinfo=timezone.utc))

    assert status["status"] == "OK"
    assert status["mode"] == "paper_only"
    assert status["paper_challenger_count"] == 0


def test_receipt_rejects_hash_valid_report_outside_project_reports_root(tmp_path):
    state = _receipt(tmp_path, [])
    outside = tmp_path / "outside-report.json"
    outside.write_text((tmp_path / "reports" / "report.json").read_text(encoding="utf-8"), encoding="utf-8")
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["output"] = str(outside)
    payload["report_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    state.write_text(json.dumps(payload), encoding="utf-8")

    try:
        paper_specs(state, now=datetime(2026, 9, 30, 13, tzinfo=timezone.utc))
    except ValueError as exc:
        assert "outside the reports root" in str(exc)
    else:
        raise AssertionError("outside report must fail closed")
