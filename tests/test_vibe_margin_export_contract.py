from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_margin_export_uses_read_only_order_calc_margin():
    source = (ROOT / "hotfix" / "scripts" / "export_vibe_research_bundle.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "order_calc_margin" in called_attributes
    assert "order_send" not in called_attributes
    assert "order_check" not in called_attributes
