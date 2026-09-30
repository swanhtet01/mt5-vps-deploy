from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mt5_agent.vibe_handoff import HANDOFF_SCHEMA, REQUIRED_VALIDATION_GATES, validate_candidate_handoff


MANIFEST_HASH = "a" * 64
BROKERS = {"XM_GOLD_H1": "GOLD", "XM_GBPJPY_H1": "GBPJPY"}


def _candidate() -> dict:
    return {
        "candidate_id": "VT-ABCDEF12", "stage": "DISCOVERED", "source_symbols": ["XM_GOLD_H1"],
        "broker_symbols": ["GOLD"], "timeframe": "H1", "family": "trend_following", "direction": "long",
        "session": "All H1 sessions.", "entry_rule": "Completed bar signal.", "exit_rule": "Fixed exit.",
        "stop_rule": "Fixed stop.", "cost_stress": {"basis": "test", "spread_multiplier": 2.0,
            "slippage_points_round_trip": 6.0, "minimum_lot_reference": 0.01, "estimated_cost_usd_min_lot": 1.0},
        "rationale": "Test hypothesis.", "expected_frequency": "Unknown.", "failure_regime": "Test.",
        "lookahead_safeguards": ["Completed bars only."], "validation_required": list(REQUIRED_VALIDATION_GATES),
        "priority_score": 50.0, "live_eligible": False,
    }


def _handoff(candidate: dict) -> dict:
    return {"schema": HANDOFF_SCHEMA, "generated_at": datetime(2026, 9, 30, tzinfo=timezone.utc).isoformat(),
        "bundle_manifest_sha256": MANIFEST_HASH, "research_only": True, "order_authority": False,
        "automatic_live_promotion": False, "source": {"kind": "vibe_deterministic_baseline", "vibe_commit": "cc54832cb50de29d14bb10097b18e08f0a843650"},
        "summary": "Test.", "candidates": [candidate]}


def test_handoff_family_source_cardinality_is_fail_closed():
    ordinary = _candidate()
    ordinary["source_symbols"] = ["XM_GOLD_H1", "XM_GBPJPY_H1"]
    ordinary["broker_symbols"] = ["GOLD", "GBPJPY"]
    with pytest.raises(ValueError, match="exactly 1 source"):
        validate_candidate_handoff(_handoff(ordinary), allowed_symbols=set(BROKERS), broker_by_source=BROKERS, expected_manifest_sha256=MANIFEST_HASH)

    cross = _candidate()
    cross["family"] = "cross_market_confirmation"
    with pytest.raises(ValueError, match="exactly 2 source"):
        validate_candidate_handoff(_handoff(cross), allowed_symbols=set(BROKERS), broker_by_source=BROKERS, expected_manifest_sha256=MANIFEST_HASH)
