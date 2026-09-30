from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_exporter_defaults_to_multi_year_h1_depth():
    source = (ROOT / "hotfix" / "scripts" / "export_vibe_research_bundle.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    defaults = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument" or not node.args:
            continue
        if isinstance(node.args[0], ast.Constant) and node.args[0].value == "--bars":
            defaults.extend(
                keyword.value.value
                for keyword in node.keywords
                if keyword.arg == "default" and isinstance(keyword.value, ast.Constant)
            )

    assert defaults == [30000]


def test_scheduled_baseline_requests_multi_year_depth():
    script = (ROOT / "hotfix" / "scripts" / "run-vibe-research.ps1").read_text(
        encoding="utf-8"
    )

    assert '"--bars", "30000"' in script
    assert '"--bars", "5000"' not in script
