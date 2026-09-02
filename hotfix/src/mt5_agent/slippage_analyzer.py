"""Slippage analysis for the MT5 live event logs (deploy copy -- this is what runs on the box).

Reads ``live_order_sent`` and ``live_exit_close`` events from the JSONL event logs written
by gold_drift_live.py, multi_drift_live.py and intraday_mean_rev.py, extracts the slippage
each fill paid, and aggregates it.

Slippage is what the traders already log via ``mt5_execution.adverse_slippage_points``:
positive = adverse fill (cost to us), negative = price improvement. This module only reads.

Grouping:
  * by symbol, by UTC hour, and by symbol-by-hour (``by_symbol_by_hour`` -- the shape the
    dashboard's ``_compute_slippage_by_hour`` reads: ``{symbol: {"HH": {"mean_slippage_pts"}}}``
    with zero-padded hour keys);
  * by spread regime RELATIVE to the symbol's own median spread in the file:
    tight < 0.75x median, normal 0.75x-1.5x, wide > 1.5x. The previous absolute buckets
    (<2 / 2-4 / >4 points) put every XM fill in "wide", which told us nothing;
  * partial-fill rate.

Accepted spread keys: ``spread_points`` (current) and ``spread_pts`` (legacy); when neither
is present but ``bid``, ``ask`` and ``point`` are, the spread is estimated from the quote.

CLI (for a scheduled task; invoke by file path so the deploy copy runs regardless of which
``mt5_agent`` an interpreter would otherwise resolve -- this module has no package imports)::

    python C:\\trading-agent\\src\\mt5_agent\\slippage_analyzer.py
        --log-file C:\\mt5-paper\\intraday-mr\\events.jsonl
        --log-file C:\\mt5-paper\\gold-drift\\live-events.jsonl
        --output C:\\trading-agent\\data_cache\\slippage_analysis.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

FILL_EVENTS = ("live_order_sent", "live_exit_close")

# Regime bounds as multiples of the symbol's median spread in the analysed file.
TIGHT_BELOW = 0.75
WIDE_ABOVE = 1.5
REGIME_TIGHT = "tight"
REGIME_NORMAL = "normal"
REGIME_WIDE = "wide"
REGIME_DEFINITION = (
    f"relative to each symbol's median spread in this file: "
    f"{REGIME_TIGHT} < {TIGHT_BELOW}x, {REGIME_NORMAL} {TIGHT_BELOW}-{WIDE_ABOVE}x, "
    f"{REGIME_WIDE} > {WIDE_ABOVE}x"
)


def read_events(events_jsonl_path: Path | str) -> list[dict[str, Any]]:
    """Read a JSONL file leniently: missing file or unreadable file -> [], bad lines skipped."""
    path = Path(events_jsonl_path)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    events.append(parsed)
    except (IOError, OSError):
        return []
    return events


def _number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _event_symbol(event: dict[str, Any]) -> str | None:
    # intraday_mean_rev's _log() stamps every line with signal=<symbol>; live_order_sent
    # also carries symbol explicitly. Prefer the explicit key.
    symbol = event.get("symbol") or event.get("signal")
    return str(symbol) if symbol else None


def _event_spread_points(event: dict[str, Any]) -> float | None:
    """spread_points (current) > spread_pts (legacy) > estimate from bid/ask/point."""
    for key in ("spread_points", "spread_pts"):
        spread = _number(event.get(key))
        if spread is not None:
            return spread
    bid, ask, point = _number(event.get("bid")), _number(event.get("ask")), _number(event.get("point"))
    if bid is not None and ask is not None and point is not None and point > 0:
        return (ask - bid) / point
    return None


def _event_hour_utc(event: dict[str, Any]) -> int | None:
    ts = event.get("ts")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.hour


def _stats(slips: list[float]) -> dict[str, Any]:
    return {
        "mean_pts": round(mean(slips), 4),
        "std_dev_pts": round(stdev(slips) if len(slips) >= 2 else 0.0, 4),
        "n": len(slips),
        "min_pts": round(min(slips), 4),
        "max_pts": round(max(slips), 4),
    }


def _regime(spread: float, symbol_median: float) -> str | None:
    if symbol_median <= 0:
        return None
    ratio = spread / symbol_median
    if ratio < TIGHT_BELOW:
        return REGIME_TIGHT
    if ratio > WIDE_ABOVE:
        return REGIME_WIDE
    return REGIME_NORMAL


def analyze_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate slippage over already-parsed events (see ``analyze_slippage`` for files)."""
    fill_events = [e for e in events if e.get("event") in FILL_EVENTS]
    if not fill_events:
        return _empty_analysis()

    # Pass 1: per-symbol median spread, so regimes are relative to what the symbol
    # normally quotes rather than to a fixed point count.
    spreads_by_symbol: dict[str, list[float]] = defaultdict(list)
    for event in fill_events:
        symbol = _event_symbol(event)
        spread = _event_spread_points(event)
        if symbol and spread is not None:
            spreads_by_symbol[symbol].append(spread)
    median_spread = {s: float(median(v)) for s, v in spreads_by_symbol.items() if v}

    # Pass 2: slippage aggregation.
    slippages: list[float] = []
    by_symbol: dict[str, list[float]] = defaultdict(list)
    by_hour: dict[int, list[float]] = defaultdict(list)
    by_symbol_by_hour: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_spread_regime: dict[str, list[float]] = defaultdict(list)
    partial_fills = 0
    total_fills = 0
    event_counts: dict[str, int] = defaultdict(int)

    for event in fill_events:
        event_counts[str(event.get("event", "unknown"))] += 1
        total_fills += 1

        slippage = _number(event.get("slippage_points"))
        if slippage is None:
            slippage = _number(event.get("slippage_pts"))  # legacy intraday key
        if slippage is None:
            continue

        slippages.append(slippage)
        if event.get("partial_fill"):
            partial_fills += 1

        symbol = _event_symbol(event)
        hour = _event_hour_utc(event)
        if symbol:
            by_symbol[symbol].append(slippage)
        if hour is not None:
            by_hour[hour].append(slippage)
            if symbol:
                by_symbol_by_hour[symbol][hour].append(slippage)

        spread = _event_spread_points(event)
        if symbol and spread is not None:
            regime = _regime(spread, median_spread.get(symbol, 0.0))
            if regime:
                by_spread_regime[regime].append(slippage)

    if not slippages:
        result = _empty_analysis()
        result["events_by_type"] = dict(event_counts)
        return result

    slippages_sorted = sorted(slippages)
    n = len(slippages)
    result: dict[str, Any] = {
        "mean_slippage_pts": round(mean(slippages), 4),
        "std_dev_pts": round(stdev(slippages) if n >= 2 else 0.0, 4),
        "min_slippage_pts": round(min(slippages), 4),
        "max_slippage_pts": round(max(slippages), 4),
        "median_slippage_pts": round(slippages_sorted[n // 2], 4),
        "n_fills": n,
        "partial_fill_count": partial_fills,
        "partial_fill_rate": round(partial_fills / total_fills, 4) if total_fills > 0 else 0.0,
        "events_by_type": dict(event_counts),
        "by_symbol": {s: _stats(by_symbol[s]) for s in sorted(by_symbol) if by_symbol[s]},
        "by_hour_utc": {h: _stats(by_hour[h]) for h in sorted(by_hour) if by_hour[h]},
        "by_symbol_by_hour": {},
        "by_spread_regime": {
            r: _stats(by_spread_regime[r]) for r in (REGIME_TIGHT, REGIME_NORMAL, REGIME_WIDE)
            if by_spread_regime.get(r)
        },
        "spread_regime_definition": REGIME_DEFINITION,
        "median_spread_points_by_symbol": {s: round(v, 4) for s, v in sorted(median_spread.items())},
    }

    # Dashboard shape: {symbol: {"HH": {"mean_slippage_pts": x, "stdev": y, "n": k}}}.
    # dashboard_metrics._compute_slippage_by_hour looks up f"{hour:02d}" and reads
    # "mean_slippage_pts"; it was never produced before, so the panel was always zeros.
    for symbol in sorted(by_symbol_by_hour):
        hours = by_symbol_by_hour[symbol]
        result["by_symbol_by_hour"][symbol] = {
            f"{hour:02d}": {
                "mean_slippage_pts": round(mean(hours[hour]), 4),
                "stdev": round(stdev(hours[hour]) if len(hours[hour]) >= 2 else 0.0, 4),
                "n": len(hours[hour]),
            }
            for hour in sorted(hours)
            if hours[hour]
        }
    return result


def analyze_slippage(events_jsonl_path: Path | str) -> dict[str, Any]:
    """Analyze slippage from one JSONL event log (missing/unreadable file -> empty analysis)."""
    return analyze_events(read_events(events_jsonl_path))


def compare_to_cost_model(
    analysis: dict[str, Any],
    cost_model_for_symbol: Any,
) -> dict[str, Any]:
    """Compare measured slippage against the backtest cost model's assumptions.

    ``cost_model_for_symbol`` is a callable ``symbol -> object`` whose result has
    ``entry_slippage_points`` and ``stop_slippage_points`` attributes (a
    ``backtest.CostModel`` works; injected as a callable so this module stays
    free of package dependencies and easy to unit-test).

    A symbol is flagged ``optimistic`` when the measured mean slippage exceeds
    the configured entry-slippage assumption: every backtest, walk-forward, and
    chamber verdict for that symbol is then charging less friction than the
    live account actually pays.
    """
    per_symbol: dict[str, Any] = {}
    any_optimistic = False
    for symbol, stats in (analysis.get("by_symbol") or {}).items():
        model = cost_model_for_symbol(symbol)
        modeled_entry = float(getattr(model, "entry_slippage_points", 0.0))
        modeled_stop = float(getattr(model, "stop_slippage_points", 0.0))
        measured_mean = float(stats.get("mean_pts", 0.0))
        measured_max = float(stats.get("max_pts", 0.0))
        optimistic = measured_mean > modeled_entry
        any_optimistic = any_optimistic or optimistic
        per_symbol[symbol] = {
            "n_fills": stats.get("n", 0),
            "measured_mean_pts": round(measured_mean, 4),
            "measured_max_pts": round(measured_max, 4),
            "modeled_entry_slippage_points": round(modeled_entry, 4),
            "modeled_stop_slippage_points": round(modeled_stop, 4),
            "optimistic": optimistic,
            "shortfall_pts": round(max(0.0, measured_mean - modeled_entry), 4),
        }
    return {
        "cost_model_optimistic": any_optimistic,
        "by_symbol": per_symbol,
        "note": (
            "optimistic=true means live fills pay more slippage than validation charges; "
            "raise entry_slippage_points in [risk] (or the symbol override) to at least the measured mean"
        ),
    }


def write_analysis_json(analysis: dict[str, Any], output_path: Path | str) -> None:
    """Write analysis results to a JSON file (temp file + replace, so readers never see a torn file)."""
    import os

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2, default=str)
    os.replace(tmp, path)


def _empty_analysis() -> dict[str, Any]:
    """Return an empty analysis structure (when no data or file missing)."""
    return {
        "mean_slippage_pts": 0.0,
        "std_dev_pts": 0.0,
        "min_slippage_pts": 0.0,
        "max_slippage_pts": 0.0,
        "median_slippage_pts": 0.0,
        "n_fills": 0,
        "partial_fill_count": 0,
        "partial_fill_rate": 0.0,
        "events_by_type": {},
        "by_symbol": {},
        "by_hour_utc": {},
        "by_symbol_by_hour": {},
        "by_spread_regime": {},
        "spread_regime_definition": REGIME_DEFINITION,
        "median_spread_points_by_symbol": {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate fill slippage from MT5 live event JSONL logs into one JSON report."
    )
    parser.add_argument("--log-file", action="append", required=True, metavar="PATH",
                        help="events JSONL to read (repeatable; missing files are skipped)")
    parser.add_argument("--output", required=True, metavar="PATH",
                        help="where to write the analysis JSON (data_cache/slippage_analysis.json)")
    args = parser.parse_args(argv)

    events: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for log_file in args.log_file:
        chunk = read_events(log_file)
        sources.append({"path": str(log_file), "events": len(chunk)})
        events.extend(chunk)

    analysis = analyze_events(events)
    analysis["generated_utc"] = datetime.now(tz=timezone.utc).isoformat()
    analysis["sources"] = sources
    write_analysis_json(analysis, args.output)
    print(f"slippage_analyzer: {analysis['n_fills']} fills from {len(events)} events "
          f"({len(sources)} file(s)) -> {args.output}; "
          f"mean={analysis['mean_slippage_pts']} pts, "
          f"partial_fill_rate={analysis['partial_fill_rate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
