from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hotfix" / "scripts"))

import vibe_candidate_screen as screen  # noqa: E402


def _frame(rows: int = 1800) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=rows, freq="h")
    close = 100 + np.cumsum(0.08 + 0.025 * np.sin(np.arange(rows) / 11.0))
    return pd.DataFrame(
        {
            "open": close - 0.04,
            "high": close + 0.12,
            "low": close - 0.12,
            "close": close,
        },
        index=index,
    )


def _candidate() -> dict:
    return {
        "candidate_id": "VT-TEST00000001",
        "source_symbols": ["XM_TEST_H1"],
        "broker_symbols": ["TEST"],
        "family": "trend_following",
        "direction": "long",
        "cost_stress": {
            "minimum_lot_reference": 0.01,
            "estimated_cost_usd_min_lot": 0.01,
        },
    }


def _instrument() -> dict:
    return {
        "trade_tick_size": 0.01,
        "trade_tick_value": 1.0,
        "volume_min": 0.01,
    }


def test_tiny_account_fails_minimum_lot_stop_risk_gate():
    result = screen.grade_direction(
        frame=_frame(),
        candidate=_candidate(),
        direction="long",
        instrument=_instrument(),
        account_snapshot={"currency": "USD", "equity": 1.0},
    )

    risk = result["minimum_lot_stop_risk"]
    assert risk["pass"] is False
    assert risk["maximum_initial_stop_risk_fraction"] > 0.02
    assert any("exceeds 2% equity budget" in reason for reason in result["reasons"])
    assert result["historical_screen_verdict"] == "FAIL"


def test_non_usd_account_fails_closed_without_conversion_evidence():
    result = screen.grade_direction(
        frame=_frame(),
        candidate=_candidate(),
        direction="long",
        instrument=_instrument(),
        account_snapshot={"currency": "EUR", "equity": 10_000.0},
    )

    assert result["historical_screen_verdict"] == "INSUFFICIENT_CONTRACT_DATA"
    assert "requires a USD account snapshot" in result["reasons"][0]
