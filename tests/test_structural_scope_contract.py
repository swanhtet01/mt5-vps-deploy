from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hotfix" / "scripts"))

import structural_walk_forward as structural  # noqa: E402


def test_only_full_predeclared_symbol_universe_is_paper_eligible():
    family, allowed = structural.research_family_for_symbols(list(structural.DEFAULT_SYMBOLS))
    diagnostic_family, diagnostic_allowed = structural.research_family_for_symbols(["GOLD", "US500Cash"])

    assert (family, allowed) == ("structural_hourweekday", True)
    assert (diagnostic_family, diagnostic_allowed) == ("manual", False)
