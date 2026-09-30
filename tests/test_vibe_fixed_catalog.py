from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hotfix" / "scripts"))

import vibe_deterministic_research as research  # noqa: E402


def _analysis(
    symbol: str,
    *,
    trend: str = "mixed",
    volatility: str = "normal",
    momentum: float = 0.0,
) -> dict:
    return {
        "source_symbol": f"XM_{symbol}_H1",
        "broker_symbol": symbol,
        "data_quality": {"status": "PASS"},
        "trend_regime": trend,
        "volatility_regime": volatility,
        "ema20_minus_ema100_atr": momentum,
        "momentum_24_pct": momentum,
        "momentum_120_pct": momentum,
        "zscore_20": momentum,
        "range_position_120": 0.99 if momentum > 0 else 0.01,
        "volatility_ratio_vs_recent_median": max(abs(momentum), 1.0),
    }


def _handoff(analyses: list[dict], maximum_candidates: int) -> dict:
    return research.build_candidate_handoff(
        analyses=analyses,
        instruments={},
        correlation_report={"matrix": {}},
        generated_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        manifest_sha256="a" * 64,
        maximum_candidates=maximum_candidates,
    )


def test_catalog_uses_frozen_cross_asset_priority():
    symbols = ["AUDJPY", "BTCUSD", "EURUSD", "GOLD", "OILCash", "US500Cash"]
    handoff = _handoff([_analysis(symbol) for symbol in symbols], 4)

    assert [item["broker_symbols"][0] for item in handoff["candidates"]] == [
        "GOLD",
        "OILCash",
        "BTCUSD",
        "US500Cash",
    ]


def test_catalog_identity_is_independent_of_latest_regime():
    bullish = _handoff(
        [_analysis("GOLD", trend="up", volatility="normal", momentum=3.0)], 4
    )
    bearish = _handoff(
        [_analysis("GOLD", trend="down", volatility="high", momentum=-3.0)], 4
    )

    assert [item["candidate_id"] for item in bullish["candidates"]] == [
        item["candidate_id"] for item in bearish["candidates"]
    ]
    assert all(item["direction"] == "both" for item in bullish["candidates"])


def test_catalog_balances_all_families_before_repeating():
    handoff = _handoff([_analysis("GOLD")], 4)

    assert [item["family"] for item in handoff["candidates"]] == list(
        research.FIXED_CATALOG_FAMILIES
    )
    assert "latest observed regime" in handoff["candidates"][0]["rationale"]


def test_catalog_reserves_slots_for_predeclared_cross_market_pairs():
    symbols = ["GOLD", "OILCash", "BTCUSD", "US500Cash", "USDJPY", "ETHUSD", "UK100Cash"]
    handoff = _handoff([_analysis(symbol) for symbol in symbols], 8)

    pairs = [item["broker_symbols"] for item in handoff["candidates"] if item["family"] == "cross_market_confirmation"]
    assert pairs == [["BTCUSD", "ETHUSD"], ["US500Cash", "UK100Cash"]]
    assert len(handoff["candidates"]) == 8
