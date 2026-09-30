from __future__ import annotations

import pytest

from mt5_agent.edge_registry import EdgeRecord, EdgeRegistry, PromotionError, Stage


def _validated_edge(stage: str) -> EdgeRecord:
    return EdgeRecord(
        key="TEST", magic=88010, symbol="GOLD", weekday=1, entry_hour=1, exit_hour=2,
        side="long", stage=stage,
        validation={"bonferroni": True, "fdr_discovery": True, "chamber_verdict": "PASS",
                    "clock_verified": True, "forward_trades": 30,
                    "forward_profit_factor": 1.2, "forward_net": 1.0},
    )


def test_discovered_edge_cannot_jump_to_live_even_when_fields_are_populated(tmp_path):
    registry = EdgeRegistry(tmp_path / "registry.json")
    registry.upsert(_validated_edge(Stage.DISCOVERED.value))

    with pytest.raises(PromotionError, match="complete PAPER validation"):
        registry.promote("TEST", Stage.LIVE.value)


def test_paper_edge_with_complete_validation_can_promote(tmp_path):
    registry = EdgeRegistry(tmp_path / "registry.json")
    registry.upsert(_validated_edge(Stage.PAPER.value))

    assert registry.promote("TEST", Stage.LIVE.value).stage == Stage.LIVE.value
