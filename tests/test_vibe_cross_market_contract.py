from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cross_market_forward_contract_requires_exact_timestamp_and_is_paper_only():
    source = (ROOT / "hotfix" / "scripts" / "vibe_shadow_forward.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert "cross_market_signal" in names
    assert "confirmation_symbol" in names
    assert "confirmation_frame" in names
    assert "order_send" not in source


def test_cross_market_rule_has_no_forward_fill_dependency():
    source = (ROOT / "hotfix" / "src" / "mt5_agent" / "vibe_rules.py").read_text(encoding="utf-8")

    assert "def cross_market_signal" in source
    assert "cross_market_confirmation" in source
    assert "ffill" not in source
