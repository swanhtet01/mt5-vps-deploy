"""Cost-aware historical rejection screen for deterministic Vibe hypotheses.

This process imports no broker API, cannot place orders, and cannot promote a
candidate. Because the hypotheses were selected from the latest sample, a PASS
only means "not rejected by this historical screen"; paper-forward validation
must establish any usable evidence after discovery.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from mt5_agent.fdr_ledger import benjamini_hochberg  # noqa: E402
from mt5_agent.structural_validation import block_bootstrap_mean_lcb  # noqa: E402
from mt5_agent.vibe_handoff import validate_candidate_handoff  # noqa: E402
from mt5_agent.vibe_rules import (  # noqa: E402
    RULES,
    prepare_frame as _prepare_frame,
    rule_exit as _rule_exit,
    signal as _signal,
)
from vibe_deterministic_research import (  # noqa: E402
    AUDITED_VIBE_COMMIT,
    _instrument_map,
    _within,
    file_sha256,
    load_vibe_frames,
    verify_bundle,
)


SCREEN_SCHEMA = "mt5.vibe_candidate_screen.v1"
MINIMUM_ROWS = 1000
INITIAL_HISTORY_FRACTION = 0.60
FOLDS = 4
MINIMUM_OOS_TRADES = 30
MINIMUM_PROFIT_FACTOR = 1.20
MINIMUM_PROFITABLE_FOLD_RATIO = 0.75
BOOTSTRAP_SAMPLES = 3000
BOOTSTRAP_BLOCK_TRADES = 4
FAMILY_ALPHA = 0.05

def _finite(value: Any, digits: int = 6) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def _contract_values(instrument: Mapping[str, Any], candidate: Mapping[str, Any]) -> tuple[float, float, float, float]:
    tick_size = float(instrument.get("trade_tick_size") or 0)
    tick_value = float(instrument.get("trade_tick_value") or 0)
    minimum_lot = float(instrument.get("volume_min") or 0)
    cost = candidate.get("cost_stress", {}).get("estimated_cost_usd_min_lot")
    if tick_size <= 0 or tick_value <= 0 or minimum_lot <= 0 or cost is None or float(cost) < 0:
        raise ValueError("current instrument snapshot cannot value minimum-lot P/L and stressed costs")
    declared_lot = candidate.get("cost_stress", {}).get("minimum_lot_reference")
    if declared_lot is None or not math.isclose(float(declared_lot), minimum_lot, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError("candidate minimum-lot cost reference does not match instrument snapshot")
    return tick_size, tick_value, minimum_lot, float(cost)


def simulate_candidate(
    frame: pd.DataFrame,
    *,
    family: str,
    direction: str,
    instrument: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Simulate one fixed direction with next-bar execution and no overlap."""
    if family not in RULES or direction not in {"long", "short"}:
        return []
    data = _prepare_frame(frame)
    tick_size, tick_value, minimum_lot, cost = _contract_values(instrument, candidate)
    direction_value = 1 if direction == "long" else -1
    maximum_hold = int(RULES[family]["maximum_hold_bars"])
    stop_atr = float(RULES[family]["stop_atr"])
    trades: list[dict[str, Any]] = []
    signal_index = 0
    while signal_index < len(data) - 1:
        signal_row = data.iloc[signal_index]
        if not _signal(signal_row, family, direction_value):
            signal_index += 1
            continue
        entry_index = signal_index + 1
        entry_price = float(data.iloc[entry_index]["open"])
        atr = float(signal_row["atr14"])
        if not math.isfinite(entry_price) or not math.isfinite(atr) or entry_price <= 0 or atr <= 0:
            signal_index += 1
            continue
        stop_price = entry_price - direction_value * stop_atr * atr
        last_exit_index = min(entry_index + maximum_hold - 1, len(data) - 1)
        exit_index = last_exit_index
        exit_price = float(data.iloc[last_exit_index]["close"])
        exit_reason = "maximum_hold"
        for bar_index in range(entry_index, last_exit_index + 1):
            row = data.iloc[bar_index]
            bar_open = float(row["open"])
            stop_hit = (
                direction_value > 0 and float(row["low"]) <= stop_price
            ) or (
                direction_value < 0 and float(row["high"]) >= stop_price
            )
            if stop_hit:
                exit_index = bar_index
                exit_price = (
                    min(bar_open, stop_price) if direction_value > 0
                    else max(bar_open, stop_price)
                )
                exit_reason = "stop"
                break
            if _rule_exit(row, family, direction_value):
                exit_index = bar_index
                exit_price = float(row["close"])
                exit_reason = "rule_exit"
                break
        gross = (exit_price - entry_price) * direction_value / tick_size * tick_value * minimum_lot
        trades.append(
            {
                "signal_index": signal_index,
                "entry_index": entry_index,
                "exit_index": exit_index,
                "entry_time": data.index[entry_index].isoformat(),
                "exit_time": data.index[exit_index].isoformat(),
                "gross_usd": float(gross),
                "cost_usd": cost,
                "net_usd": float(gross - cost),
                "exit_reason": exit_reason,
            }
        )
        signal_index = max(exit_index, signal_index + 1)
    return trades


def _fold_ranges(row_count: int, purge_bars: int) -> list[dict[str, int]]:
    initial_end = int(row_count * INITIAL_HISTORY_FRACTION)
    remaining = row_count - initial_end
    if initial_end < MINIMUM_ROWS // 2 or remaining < FOLDS * (purge_bars + 1):
        return []
    boundaries = [initial_end + (remaining * index) // FOLDS for index in range(FOLDS + 1)]
    return [
        {
            "fold": index + 1,
            "train_end": boundaries[index],
            "test_start": boundaries[index] + purge_bars,
            "test_end": boundaries[index + 1],
            "purged_bars": purge_bars,
        }
        for index in range(FOLDS)
        if boundaries[index] + purge_bars < boundaries[index + 1]
    ]


def _profit_factor(values: list[float]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def _one_sided_positive_p(values: list[float]) -> float:
    if len(values) < 2:
        return 1.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    if variance <= 0:
        return 0.0 if mean > 0 else 1.0
    statistic = mean / math.sqrt(variance / len(values))
    return 0.5 * math.erfc(statistic / math.sqrt(2.0))


def _metrics(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "trades": 0,
            "net_usd": 0.0,
            "mean_net_usd": None,
            "win_rate": None,
            "profit_factor": None,
            "max_cumulative_drawdown_usd": 0.0,
            "one_sided_positive_p_normal_approx": 1.0,
        }
    cumulative = np.cumsum(values)
    peaks = np.maximum.accumulate(np.concatenate(([0.0], cumulative)))
    drawdowns = peaks[1:] - cumulative
    profit_factor = _profit_factor(values)
    return {
        "trades": len(values),
        "net_usd": _finite(sum(values), 6),
        "mean_net_usd": _finite(sum(values) / len(values), 6),
        "win_rate": _finite(sum(value > 0 for value in values) / len(values), 6),
        "profit_factor": None if math.isinf(profit_factor) else _finite(profit_factor, 6),
        "max_cumulative_drawdown_usd": _finite(drawdowns.max(), 6),
        "one_sided_positive_p_normal_approx": _finite(_one_sided_positive_p(values), 12),
    }


def grade_direction(
    *,
    frame: pd.DataFrame,
    candidate: Mapping[str, Any],
    direction: str,
    instrument: Mapping[str, Any],
) -> dict[str, Any]:
    family = str(candidate["family"])
    screen_id = f"{candidate['candidate_id']}-{direction.upper()}"
    if family not in RULES:
        return {
            "screen_id": screen_id,
            "parent_candidate_id": candidate["candidate_id"],
            "broker_symbol": candidate["broker_symbols"][0],
            "family": family,
            "direction": direction,
            "candidate_stage": "DISCOVERED",
            "historical_screen_verdict": "UNSUPPORTED_RULE",
            "reasons": ["fixed historical simulator does not support this rule family"],
            "folds": [],
            "oos": _metrics([]),
            "paper_candidate": False,
            "live_eligible": False,
        }
    rules = RULES[family]
    purge_bars = int(rules["maximum_hold_bars"]) + 1
    folds = _fold_ranges(len(frame), purge_bars)
    try:
        trades = simulate_candidate(
            frame,
            family=family,
            direction=direction,
            instrument=instrument,
            candidate=candidate,
        )
    except ValueError as exc:
        return {
            "screen_id": screen_id,
            "parent_candidate_id": candidate["candidate_id"],
            "broker_symbol": candidate["broker_symbols"][0],
            "family": family,
            "direction": direction,
            "candidate_stage": "DISCOVERED",
            "historical_screen_verdict": "INSUFFICIENT_CONTRACT_DATA",
            "reasons": [str(exc)],
            "folds": [],
            "oos": _metrics([]),
            "paper_candidate": False,
            "live_eligible": False,
        }
    fold_reports: list[dict[str, Any]] = []
    pooled: list[float] = []
    for fold in folds:
        selected = [
            trade for trade in trades
            if trade["entry_index"] >= fold["test_start"] and trade["exit_index"] < fold["test_end"]
        ]
        values = [float(trade["net_usd"]) for trade in selected]
        pooled.extend(values)
        fold_reports.append({**fold, **_metrics(values)})

    metrics = _metrics(pooled)
    profitable_folds = sum(report["net_usd"] > 0 for report in fold_reports)
    profitable_ratio = profitable_folds / len(fold_reports) if fold_reports else 0.0
    bootstrap_lcb = block_bootstrap_mean_lcb(
        pooled,
        samples=BOOTSTRAP_SAMPLES,
        block_size=BOOTSTRAP_BLOCK_TRADES,
        seed=int.from_bytes(screen_id.encode("utf-8"), "little") % (2**32),
    )
    reasons: list[str] = []
    if len(folds) != FOLDS:
        reasons.append("four purged out-of-sample folds were not available")
    if metrics["trades"] < MINIMUM_OOS_TRADES:
        reasons.append(f"out-of-sample trades {metrics['trades']} < {MINIMUM_OOS_TRADES}")
    if metrics["mean_net_usd"] is None or metrics["mean_net_usd"] <= 0:
        reasons.append("out-of-sample mean net P/L is not positive")
    raw_profit_factor = _profit_factor(pooled)
    if raw_profit_factor < MINIMUM_PROFIT_FACTOR:
        reasons.append(
            f"out-of-sample profit factor {raw_profit_factor:.2f} < {MINIMUM_PROFIT_FACTOR:.2f}"
        )
    if profitable_ratio < MINIMUM_PROFITABLE_FOLD_RATIO:
        reasons.append(
            f"profitable fold ratio {profitable_ratio:.2f} < {MINIMUM_PROFITABLE_FOLD_RATIO:.2f}"
        )
    if bootstrap_lcb is None or bootstrap_lcb <= 0:
        reasons.append("95% block-bootstrap lower bound for mean net P/L is not positive")
    return {
        "screen_id": screen_id,
        "parent_candidate_id": candidate["candidate_id"],
        "broker_symbol": candidate["broker_symbols"][0],
        "family": family,
        "direction": direction,
        "candidate_stage": "DISCOVERED",
        "historical_screen_verdict": "PASS_BEFORE_MULTIPLE_TESTING" if not reasons else "FAIL",
        "reasons": reasons,
        "fixed_rule": {
            **rules,
            "next_bar_open_entry": True,
            "completed_bar_signals_only": True,
            "conservative_intrabar_stop_priority": True,
            "non_overlapping_positions": True,
        },
        "cost_model": {
            "minimum_lot": candidate["cost_stress"]["minimum_lot_reference"],
            "stressed_round_trip_usd": candidate["cost_stress"]["estimated_cost_usd_min_lot"],
            "valuation": "current instrument tick-value snapshot; not historical tick-value evidence",
        },
        "all_simulated_trades": len(trades),
        "folds": fold_reports,
        "oos": {
            **metrics,
            "profitable_fold_ratio": _finite(profitable_ratio, 6),
            "bootstrap_mean_lcb_95_usd": _finite(bootstrap_lcb, 6),
        },
        "paper_candidate": False,
        "live_eligible": False,
    }


def screen_candidates(
    *,
    frames: Mapping[str, pd.DataFrame],
    handoff: Mapping[str, Any],
    instruments: Mapping[str, Mapping[str, Any]],
    generated_at: datetime,
    manifest_sha256: str,
    handoff_sha256: str,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for candidate in handoff["candidates"]:
        directions = ["long", "short"] if candidate["direction"] == "both" else [candidate["direction"]]
        source_symbol = candidate["source_symbols"][0]
        broker_symbol = candidate["broker_symbols"][0]
        for direction in directions:
            results.append(
                grade_direction(
                    frame=frames[source_symbol],
                    candidate=candidate,
                    direction=direction,
                    instrument=instruments.get(broker_symbol, {}),
                )
            )

    pvalues = [float(result["oos"]["one_sided_positive_p_normal_approx"]) for result in results]
    bh_mask = benjamini_hochberg(pvalues, q=FAMILY_ALPHA)
    family_trials = len(results)
    for result, p_value, bh_reject in zip(results, pvalues, bh_mask):
        p_bonferroni = min(1.0, p_value * max(family_trials, 1))
        result["multiple_testing"] = {
            "family": "vibe_deterministic_fixed_rules",
            "family_trials": family_trials,
            "p_raw": _finite(p_value, 12),
            "p_bonferroni": _finite(p_bonferroni, 12),
            "bh_fdr_q_0_05": bool(bh_reject),
        }
        passed = bool(
            result["historical_screen_verdict"] == "PASS_BEFORE_MULTIPLE_TESTING"
            and p_bonferroni < FAMILY_ALPHA
            and bh_reject
        )
        if passed:
            result["historical_screen_verdict"] = "PASS_NOT_REJECTED"
        elif result["historical_screen_verdict"] == "PASS_BEFORE_MULTIPLE_TESTING":
            result["historical_screen_verdict"] = "FAIL_MULTIPLE_TESTING"
            result["reasons"].append("did not survive both Bonferroni and BH-FDR correction")
        result["historical_screen_pass"] = passed

    return {
        "schema": SCREEN_SCHEMA,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "historical_research_only",
        "order_authority": False,
        "automatic_live_promotion": False,
        "forecast_generated": False,
        "source": {
            "kind": "vibe_deterministic_baseline",
            "vibe_commit": AUDITED_VIBE_COMMIT,
            "bundle_manifest_sha256": manifest_sha256,
            "candidate_handoff_sha256": handoff_sha256,
        },
        "method": {
            "hypotheses_fixed_before_screen": True,
            "candidate_selection_used_latest_sample_regime": True,
            "historical_pass_can_reject_but_cannot_validate": True,
            "initial_history_fraction": INITIAL_HISTORY_FRACTION,
            "purged_oos_folds": FOLDS,
            "minimum_oos_trades": MINIMUM_OOS_TRADES,
            "minimum_profit_factor": MINIMUM_PROFIT_FACTOR,
            "minimum_profitable_fold_ratio": MINIMUM_PROFITABLE_FOLD_RATIO,
            "bootstrap_samples": BOOTSTRAP_SAMPLES,
            "bonferroni_and_bh_fdr_required": True,
        },
        "family_trials": family_trials,
        "historical_screen_pass_count": sum(result["historical_screen_pass"] for result in results),
        "paper_candidate_count": 0,
        "live_eligible_count": 0,
        "results": results,
        "limitations": [
            "Candidate selection used the latest portion of this same sample, so a historical PASS is not independent evidence.",
            "The current terminal spread and tick-value snapshot is stressed but is not a historical cost series.",
            "Only post-discovery paper-forward outcomes can provide independent evidence; manual live authorization remains separate.",
        ],
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Vibe Candidate Historical Screen",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "This screen can reject hypotheses. It cannot validate future profit, create a paper candidate, or authorize a live trade.",
        "",
        "| Candidate | Instrument | Rule | Direction | Verdict | OOS trades | Net USD | PF | LCB/trade | Bonferroni p |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in report["results"]:
        oos = result["oos"]
        multiple = result["multiple_testing"]
        lines.append(
            f"| {result['screen_id']} | {result['broker_symbol']} | {result['family']} | {result['direction']} | "
            f"{result['historical_screen_verdict']} | {oos['trades']} | {oos['net_usd']} | "
            f"{oos['profit_factor']} | {oos.get('bootstrap_mean_lcb_95_usd')} | {multiple['p_bonferroni']} |"
        )
    lines.extend(
        [
            "",
            f"Historical screens not rejected: `{report['historical_screen_pass_count']}`.",
            "",
            "Paper candidates: `0`. Live-eligible candidates: `0`.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = args.bundle.resolve()
    output = args.output.resolve()
    if output == bundle or _within(output, bundle):
        raise ValueError("screen output must not modify the immutable export bundle")
    manifest, manifest_sha256 = verify_bundle(bundle)
    handoff_path = args.handoff.resolve()
    handoff = json.loads(handoff_path.read_text(encoding="utf-8-sig"))
    bar_records = [record for record in manifest["files"] if record.get("source_symbol")]
    broker_by_source = {
        str(record["source_symbol"]): str(record["broker_symbol"])
        for record in bar_records
    }
    validate_candidate_handoff(
        handoff,
        allowed_symbols=set(broker_by_source),
        broker_by_source=broker_by_source,
        expected_manifest_sha256=manifest_sha256,
        maximum_candidates=10,
    )
    if handoff["source"]["kind"] != "vibe_deterministic_baseline":
        raise ValueError("only deterministic baseline candidates may enter the fixed historical screen")
    frames, _ = load_vibe_frames(bundle, manifest)
    report = screen_candidates(
        frames=frames,
        handoff=handoff,
        instruments=_instrument_map(bundle),
        generated_at=datetime.now(tz=timezone.utc),
        manifest_sha256=manifest_sha256,
        handoff_sha256=file_sha256(handoff_path),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    markdown = output.with_suffix(".md")
    markdown.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "schema": SCREEN_SCHEMA,
                "status": "completed",
                "report": str(output),
                "report_sha256": file_sha256(output),
                "markdown": str(markdown),
                "family_trials": report["family_trials"],
                "historical_screen_pass_count": report["historical_screen_pass_count"],
                "paper_candidate_count": 0,
                "live_eligible_count": 0,
                "order_authority": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
