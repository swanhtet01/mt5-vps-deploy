from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_paper_only_candidates_return_before_live_order_paths():
    source = (ROOT / "hotfix" / "scripts" / "structural_scheduler.py").read_text(encoding="utf-8")
    paper_guard = 'if bool(spec.get("paper_only")):'
    live_guard = "live_authorized = _live_armed() and magic in allowlist"
    assert paper_guard in source
    assert live_guard in source
    assert source.index(paper_guard) < source.index(live_guard)


def test_scheduler_uses_only_receipt_bound_paper_challengers():
    source = (ROOT / "hotfix" / "scripts" / "structural_scheduler.py").read_text(encoding="utf-8")
    assert "receipt_paper_specs(STRUCTURAL_RESEARCH_RECEIPT_FILE)" in source
    assert "specs.update(receipt_paper_specs" in source


def test_scheduler_applies_shared_paper_portfolio_protections_before_opening():
    source = (ROOT / "hotfix" / "scripts" / "structural_scheduler.py").read_text(encoding="utf-8")
    assert "paper_admission(" in source
    assert source.index("paper_admission(") < source.index('"event": "paper_position_opened"')
