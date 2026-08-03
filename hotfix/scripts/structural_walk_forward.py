"""Fresh MT5 structural scan with purged, cost-aware walk-forward validation.

This script is read-only: it pulls completed H1 bars and writes a research report. It
does not edit the live registry, allowlist, environment, tasks, positions, or orders.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.fdr_ledger import benjamini_hochberg
from mt5_agent.mt5_execution import feed_clock_provenance
from mt5_agent.structural_validation import (
    StructuralObservation,
    entry_bucket_for_return_bucket,
    validate_structural_schedule,
)


DEFAULT_SYMBOLS = [
    "GOLD", "SILVER", "OILCash", "BTCUSD", "ETHUSD", "US500Cash",
    "USDJPY", "UK100Cash", "AUDJPY", "GBPJPY", "EURUSD", "GBPUSD",
    "GER40Cash", "JP225Cash",
]
CRYPTO_SYMBOLS = {"BTCUSD", "ETHUSD"}


def _copy_fresh_h1_rates(
    symbol: str,
    bars_count: int,
    feed_now: datetime,
    max_last_bar_age_hours: float,
    *,
    attempts: int = 5,
    retry_delay_seconds: float = 2.0,
):
    """Allow MT5's asynchronous history cache time to refresh a selected symbol."""
    last_reason = "H1 history was unavailable"
    for attempt in range(max(int(attempts), 1)):
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, bars_count)
        if rates is not None and len(rates) >= 1000:
            last_bar = datetime.fromtimestamp(int(rates[-1]["time"]), tz=timezone.utc)
            age_hours = (feed_now - last_bar).total_seconds() / 3600.0
            if -1.0 <= age_hours <= max_last_bar_age_hours:
                return rates, last_bar, age_hours
            last_reason = f"last completed H1 bar age {age_hours:.1f}h is outside limit"
        else:
            last_reason = "insufficient H1 history"
        if attempt + 1 < max(int(attempts), 1):
            mt5.symbol_info_tick(symbol)
            time.sleep(max(float(retry_delay_seconds), 0.0))
    raise RuntimeError(last_reason)


def _reference_tick() -> tuple[str, object]:
    for symbol in ("BTCUSD", "GOLD", "USDJPY"):
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and int(getattr(tick, "time", 0) or 0) > 0:
            clock = feed_clock_provenance(tick)
            if clock is not None and clock.coherent:
                return symbol, tick
    raise RuntimeError("no fresh coherent MT5 reference tick")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int((len(ordered) - 1) * fraction)
    return ordered[index]


def _symbol_observations(
    symbol: str,
    bars_count: int,
    feed_now: datetime,
    max_last_bar_age_hours: float,
    slippage_points: float,
) -> tuple[dict[tuple[int, int], list[StructuralObservation]], dict]:
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError("symbol unavailable")
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError("insufficient symbol info")
    if info.trade_tick_size <= 0 or info.trade_tick_value <= 0 or info.volume_min <= 0:
        raise RuntimeError("invalid tick-value or volume metadata")

    rates, last_bar, last_bar_age_hours = _copy_fresh_h1_rates(
        symbol,
        bars_count,
        feed_now,
        max_last_bar_age_hours,
    )

    historical_spreads = [
        float(row["spread"])
        for row in rates[-2000:]
        if float(row["spread"]) > 0
    ]
    conservative_spread = max(
        float(getattr(info, "spread", 0) or 0),
        _percentile(historical_spreads, 0.90),
    )
    lot = float(info.volume_min)
    round_trip_cost = (
        (conservative_spread + slippage_points)
        * float(info.point)
        / float(info.trade_tick_size)
        * float(info.trade_tick_value)
        * lot
    )
    weekdays = set(range(7)) if symbol in CRYPTO_SYMBOLS else set(range(5))
    buckets: dict[tuple[int, int], list[StructuralObservation]] = defaultdict(list)
    for previous, current in zip(rates, rates[1:]):
        stamp = datetime.fromtimestamp(int(current["time"]), tz=timezone.utc)
        if stamp.weekday() not in weekdays:
            continue
        gross = (
            (float(current["close"]) - float(previous["close"]))
            / float(info.trade_tick_size)
            * float(info.trade_tick_value)
            * lot
        )
        buckets[(stamp.weekday(), stamp.hour)].append(
            StructuralObservation(stamp, gross, round_trip_cost)
        )
    return buckets, {
        "bars": len(rates),
        "first_bar_feed_time": datetime.fromtimestamp(
            int(rates[0]["time"]), tz=timezone.utc
        ).isoformat(),
        "last_bar_feed_time": last_bar.isoformat(),
        "last_bar_age_hours": round(last_bar_age_hours, 3),
        "minimum_lot": lot,
        "conservative_spread_points": conservative_spread,
        "slippage_points_round_trip": slippage_points,
        "round_trip_cost_usd_at_minimum_lot": round(round_trip_cost, 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--bars", type=int, default=30000)
    parser.add_argument("--max-last-bar-age-hours", type=float, default=80.0)
    parser.add_argument("--slippage-points", type=float, default=6.0)
    parser.add_argument(
        "--output", type=Path,
        default=Path("reports/structural-walk-forward-latest.json"),
    )
    args = parser.parse_args()

    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        reference_symbol, reference_tick = _reference_tick()
        clock = feed_clock_provenance(reference_tick)
        if clock is None or not clock.coherent:
            raise RuntimeError("MT5 feed clock provenance failed")
        symbols = [value.strip() for value in args.symbols.split(",") if value.strip()]
        candidates: list[dict] = []
        symbol_reports: dict[str, dict] = {}
        for symbol in symbols:
            try:
                buckets, metadata = _symbol_observations(
                    symbol,
                    max(int(args.bars), 1000),
                    clock.feed_time,
                    max(float(args.max_last_bar_age_hours), 1.0),
                    max(float(args.slippage_points), 0.0),
                )
                symbol_reports[symbol] = metadata
                for (return_weekday, return_hour), observations in sorted(buckets.items()):
                    result = validate_structural_schedule(observations, seed=return_weekday * 24 + return_hour)
                    entry_weekday, entry_hour = entry_bucket_for_return_bucket(
                        return_weekday, return_hour
                    )
                    candidates.append({
                        "spec_id": f"{symbol}|{return_weekday}|{return_hour:02d}",
                        "symbol": symbol,
                        "return_weekday": return_weekday,
                        "return_hour": return_hour,
                        "entry_weekday": entry_weekday,
                        "entry_hour": entry_hour,
                        "exit_weekday": return_weekday,
                        "exit_hour": return_hour,
                        **result,
                    })
            except Exception as exc:
                symbol_reports[symbol] = {"skipped": f"{type(exc).__name__}: {exc}"}

        pvalues = [float(item.get("oos", {}).get("one_sided_positive_p", 1.0)) for item in candidates]
        bh_mask = benjamini_hochberg(pvalues, q=0.05)
        denominator = len(candidates)
        for candidate, bh_reject, p_value in zip(candidates, bh_mask, pvalues):
            candidate["multiple_testing"] = {
                "family": "fresh_structural_hourweekday",
                "family_trials": denominator,
                "p_raw": p_value,
                "p_bonferroni": min(1.0, p_value * denominator),
                "bh_fdr_q_0_05": bool(bh_reject),
            }
            candidate["paper_candidate"] = bool(
                candidate["verdict"] == "PASS"
                and bh_reject
                and p_value * denominator < 0.05
            )
            candidate["live_eligible"] = False

        survivors = [item for item in candidates if item["paper_candidate"]]
        survivors.sort(key=lambda item: item["oos"]["mean_net"], reverse=True)
        report = {
            "generated_at_host_utc": datetime.now(tz=timezone.utc).isoformat(),
            "mode": "read_only_research",
            "orders_sent": 0,
            "time_basis": "MT5 epoch feed clock, measured against host UTC",
            "reference_symbol": reference_symbol,
            "clock": clock.as_dict(),
            "method": {
                "completed_h1_bars_only": True,
                "direction_selected_on_prior_train_only": True,
                "purged_expanding_walk_forward_folds": 4,
                "costs_subtracted_per_trade": True,
                "block_bootstrap_lower_bound_required": True,
                "bonferroni_and_bh_fdr_required": True,
                "auto_promotion": False,
            },
            "symbols": symbol_reports,
            "family_trials": denominator,
            "paper_candidates": survivors,
            "paper_candidate_count": len(survivors),
            "all_candidates": candidates,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps({
            "output": str(args.output),
            "reference_symbol": reference_symbol,
            "clock": clock.as_dict(),
            "symbols_requested": len(symbols),
            "symbols_loaded": sum(1 for item in symbol_reports.values() if "skipped" not in item),
            "family_trials": denominator,
            "paper_candidate_count": len(survivors),
            "paper_candidates": [
                {
                    "spec_id": item["spec_id"],
                    "direction": item["oos"]["direction"],
                    "oos_net": item["oos"]["net"],
                    "oos_profit_factor": item["oos"]["profit_factor"],
                }
                for item in survivors
            ],
        }, indent=2))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
