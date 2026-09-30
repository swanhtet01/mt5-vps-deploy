"""Cost-aware historical rejection screen for deterministic Vibe hypotheses.

This process imports no broker API, cannot place orders, and cannot promote a
candidate. A PASS only means "not rejected by this historical screen";
paper-forward validation must establish any usable evidence after discovery.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from statistics import NormalDist
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
MINIMUM_DEFLATED_SHARPE_PROBABILITY = 0.95
EULER_MASCHERONI = 0.5772156649015329
PBO_PARTITIONS = 8
MINIMUM_PBO_DAYS = 64
MINIMUM_PBO_STRATEGIES = 4
MAXIMUM_PBO = 0.20
MAXIMUM_MINIMUM_LOT_STOP_RISK_FRACTION = 0.02

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


def _account_equity_usd(account_snapshot: Mapping[str, Any]) -> float:
    if account_snapshot.get("currency") != "USD":
        raise ValueError("minimum-lot stop-risk gate requires a USD account snapshot")
    equity = float(account_snapshot.get("equity") or 0)
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("minimum-lot stop-risk gate requires positive captured equity")
    return equity


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
        stop_risk = abs(entry_price - stop_price) / tick_size * tick_value * minimum_lot + cost
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
                "initial_stop_risk_usd": float(stop_risk),
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
            "sharpe_per_trade": None,
            "skewness": None,
            "kurtosis": None,
        }
    series = pd.Series(values, dtype="float64")
    sample_std = float(series.std(ddof=1)) if len(series) > 1 else math.nan
    sharpe = float(series.mean() / sample_std) if sample_std > 0 else None
    skewness = float(series.skew()) if len(series) >= 3 else None
    kurtosis = float(series.kurt()) + 3.0 if len(series) >= 4 else None
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
        "sharpe_per_trade": _finite(sharpe, 12),
        "skewness": _finite(skewness, 12),
        "kurtosis": _finite(kurtosis, 12),
    }


def expected_maximum_sharpe(sharpes: list[float], *, trials: int) -> float | None:
    """Expected best Sharpe from an equally sized no-skill search family."""
    if trials <= 1:
        return 0.0
    finite = [float(value) for value in sharpes if math.isfinite(float(value))]
    if len(finite) < 2:
        return None
    variance = float(np.var(finite, ddof=1))
    if variance <= 0:
        return 0.0
    normal = NormalDist()
    first = normal.inv_cdf(1.0 - 1.0 / trials)
    second = normal.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return math.sqrt(variance) * (
        (1.0 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second
    )


def deflated_sharpe_probability(
    metrics: Mapping[str, Any], *, benchmark: float | None
) -> float | None:
    """Probability that per-trade Sharpe exceeds the search-adjusted benchmark."""
    if benchmark is None:
        return None
    observations = int(metrics.get("trades") or 0)
    sharpe = _finite(metrics.get("sharpe_per_trade"), 15)
    skewness = _finite(metrics.get("skewness"), 15)
    kurtosis = _finite(metrics.get("kurtosis"), 15)
    if observations < 3 or sharpe is None or skewness is None or kurtosis is None:
        return None
    denominator_squared = (
        1.0
        - skewness * sharpe
        + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe
    )
    if denominator_squared <= 0 or not math.isfinite(denominator_squared):
        return None
    statistic = (
        (sharpe - benchmark)
        * math.sqrt(observations - 1)
        / math.sqrt(denominator_squared)
    )
    return NormalDist().cdf(statistic)


def _sharpe_score(values: np.ndarray) -> float | None:
    if values.size < 2:
        return None
    standard_deviation = float(np.std(values, ddof=1))
    if standard_deviation <= 0 or not math.isfinite(standard_deviation):
        return None
    score = float(np.mean(values) / standard_deviation)
    return score if math.isfinite(score) else None


def estimate_probability_backtest_overfitting(
    daily_pnl_by_strategy: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    """Estimate family-level PBO using contiguous CSCV partitions."""
    strategy_ids = sorted(daily_pnl_by_strategy)
    dates = sorted(
        {
            day
            for daily_pnl in daily_pnl_by_strategy.values()
            for day in daily_pnl
        }
    )
    base = {
        "method": "combinatorially_symmetric_cross_validation",
        "partitions": PBO_PARTITIONS,
        "strategies": len(strategy_ids),
        "daily_observations": len(dates),
        "maximum_probability_backtest_overfitting": MAXIMUM_PBO,
    }
    if len(strategy_ids) < MINIMUM_PBO_STRATEGIES or len(dates) < MINIMUM_PBO_DAYS:
        return {
            **base,
            "status": "INSUFFICIENT_DATA",
            "evaluated_splits": 0,
            "probability_backtest_overfitting": None,
            "median_oos_rank_percentile": None,
        }

    matrix = np.asarray(
        [
            [float(daily_pnl_by_strategy[strategy_id].get(day, 0.0)) for day in dates]
            for strategy_id in strategy_ids
        ],
        dtype="float64",
    )
    partitions = [
        indices
        for indices in np.array_split(np.arange(len(dates)), PBO_PARTITIONS)
        if indices.size > 0
    ]
    if len(partitions) != PBO_PARTITIONS:
        return {
            **base,
            "status": "INSUFFICIENT_DATA",
            "evaluated_splits": 0,
            "probability_backtest_overfitting": None,
            "median_oos_rank_percentile": None,
        }

    rank_percentiles: list[float] = []
    logits: list[float] = []
    half = PBO_PARTITIONS // 2
    partition_ids = range(PBO_PARTITIONS)
    for train_partition_ids in combinations(partition_ids, half):
        train_set = set(train_partition_ids)
        test_partition_ids = [index for index in partition_ids if index not in train_set]
        train_indices = np.concatenate([partitions[index] for index in train_partition_ids])
        test_indices = np.concatenate([partitions[index] for index in test_partition_ids])
        train_scores = [_sharpe_score(row[train_indices]) for row in matrix]
        finite_train = [index for index, score in enumerate(train_scores) if score is not None]
        if not finite_train:
            continue
        selected = max(finite_train, key=lambda index: (train_scores[index], -index))
        test_scores = [_sharpe_score(row[test_indices]) for row in matrix]
        selected_score = test_scores[selected]
        finite_test = [float(score) for score in test_scores if score is not None]
        if selected_score is None or len(finite_test) < 2:
            continue
        less = sum(score < selected_score for score in finite_test)
        equal = sum(math.isclose(score, selected_score, rel_tol=1e-12, abs_tol=1e-12) for score in finite_test)
        average_rank = less + (equal + 1.0) / 2.0
        percentile = average_rank / (len(finite_test) + 1.0)
        percentile = min(max(percentile, 1e-12), 1.0 - 1e-12)
        rank_percentiles.append(percentile)
        logits.append(math.log(percentile / (1.0 - percentile)))

    if not rank_percentiles:
        return {
            **base,
            "status": "INSUFFICIENT_DATA",
            "evaluated_splits": 0,
            "probability_backtest_overfitting": None,
            "median_oos_rank_percentile": None,
        }
    probability = sum(value <= 0 for value in logits) / len(logits)
    return {
        **base,
        "status": "AVAILABLE",
        "evaluated_splits": len(rank_percentiles),
        "probability_backtest_overfitting": _finite(probability, 12),
        "median_oos_rank_percentile": _finite(float(np.median(rank_percentiles)), 12),
        "median_logit": _finite(float(np.median(logits)), 12),
    }


def grade_direction(
    *,
    frame: pd.DataFrame,
    candidate: Mapping[str, Any],
    direction: str,
    instrument: Mapping[str, Any],
    account_snapshot: Mapping[str, Any],
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
            "_oos_daily_pnl": {},
            "paper_candidate": False,
            "live_eligible": False,
        }
    rules = RULES[family]
    purge_bars = int(rules["maximum_hold_bars"]) + 1
    folds = _fold_ranges(len(frame), purge_bars)
    try:
        account_equity = _account_equity_usd(account_snapshot)
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
            "_oos_daily_pnl": {},
            "paper_candidate": False,
            "live_eligible": False,
        }
    fold_reports: list[dict[str, Any]] = []
    pooled: list[float] = []
    pooled_trades: list[dict[str, Any]] = []
    for fold in folds:
        selected = [
            trade for trade in trades
            if trade["entry_index"] >= fold["test_start"] and trade["exit_index"] < fold["test_end"]
        ]
        values = [float(trade["net_usd"]) for trade in selected]
        pooled.extend(values)
        pooled_trades.extend(selected)
        fold_reports.append({**fold, **_metrics(values)})

    metrics = _metrics(pooled)
    stop_risks = [float(trade["initial_stop_risk_usd"]) for trade in pooled_trades]
    maximum_stop_risk = max(stop_risks) if stop_risks else None
    stop_risk_p95 = float(np.quantile(stop_risks, 0.95)) if stop_risks else None
    risk_budget = account_equity * MAXIMUM_MINIMUM_LOT_STOP_RISK_FRACTION
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
    if maximum_stop_risk is None:
        reasons.append("minimum-lot initial stop risk is unavailable")
    elif maximum_stop_risk > risk_budget:
        reasons.append(
            f"minimum-lot maximum initial stop risk ${maximum_stop_risk:.2f} exceeds "
            f"{MAXIMUM_MINIMUM_LOT_STOP_RISK_FRACTION:.0%} equity budget ${risk_budget:.2f}"
        )
    daily_pnl: dict[str, float] = defaultdict(float)
    for trade in pooled_trades:
        daily_pnl[str(trade["exit_time"])[:10]] += float(trade["net_usd"])
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
        "minimum_lot_stop_risk": {
            "account_currency": "USD",
            "captured_equity_usd": _finite(account_equity, 2),
            "maximum_risk_fraction": MAXIMUM_MINIMUM_LOT_STOP_RISK_FRACTION,
            "risk_budget_usd": _finite(risk_budget, 2),
            "maximum_initial_stop_risk_usd": _finite(maximum_stop_risk, 6),
            "p95_initial_stop_risk_usd": _finite(stop_risk_p95, 6),
            "maximum_initial_stop_risk_fraction": _finite(
                maximum_stop_risk / account_equity if maximum_stop_risk is not None else None,
                8,
            ),
            "pass": maximum_stop_risk is not None and maximum_stop_risk <= risk_budget,
            "basis": "minimum lot, ATR initial stop, stressed round-trip cost, captured equity",
        },
        "all_simulated_trades": len(trades),
        "folds": fold_reports,
        "oos": {
            **metrics,
            "profitable_fold_ratio": _finite(profitable_ratio, 6),
            "bootstrap_mean_lcb_95_usd": _finite(bootstrap_lcb, 6),
        },
        "_oos_daily_pnl": dict(sorted(daily_pnl.items())),
        "paper_candidate": False,
        "live_eligible": False,
    }


def screen_candidates(
    *,
    frames: Mapping[str, pd.DataFrame],
    handoff: Mapping[str, Any],
    instruments: Mapping[str, Mapping[str, Any]],
    account_snapshot: Mapping[str, Any],
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
                    account_snapshot=account_snapshot,
                )
            )

    pvalues = [float(result["oos"]["one_sided_positive_p_normal_approx"]) for result in results]
    bh_mask = benjamini_hochberg(pvalues, q=FAMILY_ALPHA)
    family_trials = len(results)
    daily_pnl_by_strategy = {
        str(result["screen_id"]): result.pop("_oos_daily_pnl", {})
        for result in results
    }
    pbo = estimate_probability_backtest_overfitting(daily_pnl_by_strategy)
    pbo_probability = pbo.get("probability_backtest_overfitting")
    survived_pbo = bool(
        pbo.get("status") == "AVAILABLE"
        and pbo_probability is not None
        and float(pbo_probability) <= MAXIMUM_PBO
    )
    sharpe_values = [
        float(result["oos"]["sharpe_per_trade"])
        for result in results
        if result["oos"].get("sharpe_per_trade") is not None
    ]
    sharpe_benchmark = expected_maximum_sharpe(sharpe_values, trials=family_trials)
    for result, p_value, bh_reject in zip(results, pvalues, bh_mask):
        p_bonferroni = min(1.0, p_value * max(family_trials, 1))
        dsr_probability = deflated_sharpe_probability(
            result["oos"], benchmark=sharpe_benchmark
        )
        result["multiple_testing"] = {
            "family": "vibe_deterministic_fixed_rules",
            "family_trials": family_trials,
            "p_raw": _finite(p_value, 12),
            "p_bonferroni": _finite(p_bonferroni, 12),
            "bh_fdr_q_0_05": bool(bh_reject),
            "deflated_sharpe_benchmark_per_trade": _finite(sharpe_benchmark, 12),
            "deflated_sharpe_probability": _finite(dsr_probability, 12),
            "minimum_deflated_sharpe_probability": MINIMUM_DEFLATED_SHARPE_PROBABILITY,
            "family_pbo_pass": survived_pbo,
        }
        survived_pvalue_correction = bool(p_bonferroni < FAMILY_ALPHA and bh_reject)
        survived_sharpe_deflation = bool(
            dsr_probability is not None
            and dsr_probability >= MINIMUM_DEFLATED_SHARPE_PROBABILITY
        )
        passed = bool(
            result["historical_screen_verdict"] == "PASS_BEFORE_MULTIPLE_TESTING"
            and survived_pvalue_correction
            and survived_sharpe_deflation
            and survived_pbo
        )
        if passed:
            result["historical_screen_verdict"] = "PASS_NOT_REJECTED"
        elif result["historical_screen_verdict"] == "PASS_BEFORE_MULTIPLE_TESTING":
            result["historical_screen_verdict"] = "FAIL_MULTIPLE_TESTING"
            if not survived_pvalue_correction:
                result["reasons"].append(
                    "did not survive both Bonferroni and BH-FDR correction"
                )
            if not survived_sharpe_deflation:
                result["reasons"].append(
                    "deflated Sharpe probability did not reach 95%"
                )
            if not survived_pbo:
                result["reasons"].append(
                    "strategy family did not pass the CSCV/PBO gate"
                )
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
            "deflated_sharpe_required": True,
            "minimum_deflated_sharpe_probability": MINIMUM_DEFLATED_SHARPE_PROBABILITY,
            "pbo_cscv_required": True,
            "minimum_lot_stop_risk_required": True,
            "maximum_minimum_lot_stop_risk_fraction": MAXIMUM_MINIMUM_LOT_STOP_RISK_FRACTION,
            "maximum_probability_backtest_overfitting": MAXIMUM_PBO,
        },
        "family_trials": family_trials,
        "selection_overfitting": pbo,
        "historical_screen_pass_count": sum(result["historical_screen_pass"] for result in results),
        "paper_candidate_count": 0,
        "live_eligible_count": 0,
        "results": results,
        "limitations": [
            "Candidate selection used the latest portion of this same sample, so a historical PASS is not independent evidence.",
            "The current terminal spread and tick-value snapshot is stressed but is not a historical cost series.",
            "Only post-discovery paper-forward outcomes can provide independent evidence; manual live authorization remains separate.",
            "Deflated Sharpe uses per-trade OOS returns and the raw family trial count; it is an additional rejection test, not proof of independence.",
            "CSCV/PBO aligns realized OOS P/L by UTC exit day across the fixed family and fills no-trade days with zero; unavailable or high PBO fails closed.",
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
        "| Candidate | Instrument | Rule | Direction | Verdict | OOS trades | Net USD | PF | LCB/trade | Bonferroni p | DSR |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in report["results"]:
        oos = result["oos"]
        multiple = result["multiple_testing"]
        lines.append(
            f"| {result['screen_id']} | {result['broker_symbol']} | {result['family']} | {result['direction']} | "
            f"{result['historical_screen_verdict']} | {oos['trades']} | {oos['net_usd']} | "
            f"{oos['profit_factor']} | {oos.get('bootstrap_mean_lcb_95_usd')} | {multiple['p_bonferroni']} | "
            f"{multiple['deflated_sharpe_probability']} |"
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
        account_snapshot=json.loads(
            (bundle / "account_snapshot.redacted.json").read_text(encoding="utf-8-sig")
        ),
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
