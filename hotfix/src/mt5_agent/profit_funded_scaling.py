from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import median
from typing import Iterable, Sequence


SCHEMA = "mt5.profit_funded_scaling.v1"


@dataclass(frozen=True)
class PromotionTier:
    tier: int
    multiplier: float
    min_trades: int
    min_t_stat: float
    min_profit_factor: float
    min_recent_profit_factor: float
    min_profit_cushion_units: float


# These are promotion gates, not fitted strategy parameters. Promotion is slow,
# while drawdown rollback below is immediate and asymmetric.
DEFAULT_TIERS: tuple[PromotionTier, ...] = (
    PromotionTier(0, 1.00, 0, 0.0, 0.0, 0.0, 0.0),
    PromotionTier(1, 1.25, 60, 2.0, 1.20, 1.00, 8.0),
    PromotionTier(2, 1.50, 100, 2.5, 1.30, 1.05, 15.0),
    PromotionTier(3, 2.00, 200, 3.0, 1.40, 1.10, 30.0),
    PromotionTier(4, 3.00, 400, 3.5, 1.50, 1.15, 60.0),
)


def _finite(values: Iterable[float]) -> list[float]:
    return [float(value) for value in values if math.isfinite(float(value))]


def _profit_factor(values: Sequence[float]) -> float:
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = abs(sum(value for value in values if value < 0))
    if gross_loss > 0:
        return gross_profit / gross_loss
    return math.inf if gross_profit > 0 else 1.0


def _serial_drawdown(values: Sequence[float]) -> tuple[float, float, float]:
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return cumulative, peak, max_drawdown


def strategy_statistics(nets: Iterable[float], *, recent_window: int = 20) -> dict:
    values = _finite(nets)
    count = len(values)
    if count == 0:
        return {
            "closed_trades": 0,
            "net_profit_usd": 0.0,
            "win_rate_pct": 0.0,
            "mean_per_trade_usd": 0.0,
            "sample_sd_usd": 0.0,
            "t_stat": 0.0,
            "ci95_mean_lower_usd": 0.0,
            "ci95_mean_upper_usd": 0.0,
            "profit_factor": 1.0,
            "recent_window": 0,
            "recent_net_profit_usd": 0.0,
            "recent_profit_factor": 1.0,
            "current_losing_streak": 0,
            "loss_unit_usd": 0.0,
            "profit_cushion_units": 0.0,
            "profit_high_water_usd": 0.0,
            "current_drawdown_usd": 0.0,
            "current_drawdown_units": 0.0,
            "current_drawdown_from_profit_peak_pct": 0.0,
            "max_drawdown_usd": 0.0,
        }

    net_profit, profit_peak, max_drawdown = _serial_drawdown(values)
    mean = net_profit / count
    sample_sd = (
        math.sqrt(sum((value - mean) ** 2 for value in values) / (count - 1))
        if count > 1
        else 0.0
    )
    standard_error = sample_sd / math.sqrt(count) if sample_sd > 0 else 0.0
    t_stat = mean / standard_error if standard_error > 0 else 0.0
    ci_half_width = 1.96 * standard_error
    losses = [abs(value) for value in values if value < 0]
    loss_unit = median(losses) if losses else median(abs(value) for value in values)
    loss_unit = max(float(loss_unit), 1e-9)
    recent_count = min(max(int(recent_window), 1), count)
    recent = values[-recent_count:]
    losing_streak = 0
    for value in reversed(values):
        if value < 0:
            losing_streak += 1
        else:
            break
    current_drawdown = max(profit_peak - net_profit, 0.0)

    return {
        "closed_trades": count,
        "net_profit_usd": round(net_profit, 6),
        "win_rate_pct": round(100.0 * sum(value > 0 for value in values) / count, 4),
        "mean_per_trade_usd": round(mean, 6),
        "sample_sd_usd": round(sample_sd, 6),
        "t_stat": round(t_stat, 6),
        "ci95_mean_lower_usd": round(mean - ci_half_width, 6),
        "ci95_mean_upper_usd": round(mean + ci_half_width, 6),
        "profit_factor": round(_profit_factor(values), 6)
        if math.isfinite(_profit_factor(values))
        else 999.0,
        "recent_window": recent_count,
        "recent_net_profit_usd": round(sum(recent), 6),
        "recent_profit_factor": round(_profit_factor(recent), 6)
        if math.isfinite(_profit_factor(recent))
        else 999.0,
        "current_losing_streak": losing_streak,
        "loss_unit_usd": round(loss_unit, 6),
        "profit_cushion_units": round(max(net_profit, 0.0) / loss_unit, 6),
        "profit_high_water_usd": round(profit_peak, 6),
        "current_drawdown_usd": round(current_drawdown, 6),
        "current_drawdown_units": round(current_drawdown / loss_unit, 6),
        "current_drawdown_from_profit_peak_pct": round(
            100.0 * current_drawdown / profit_peak if profit_peak > 0 else 0.0,
            6,
        ),
        "max_drawdown_usd": round(max_drawdown, 6),
    }


def _tier_failures(statistics: dict, tier: PromotionTier) -> list[str]:
    failures: list[str] = []
    if int(statistics["closed_trades"]) < tier.min_trades:
        failures.append(f"need {tier.min_trades} closed attributable trades")
    if float(statistics["net_profit_usd"]) <= 0:
        failures.append("closed attributable net profit must be positive")
    if float(statistics["ci95_mean_lower_usd"]) <= 0:
        failures.append("95% confidence lower bound for mean trade is not positive")
    if float(statistics["t_stat"]) < tier.min_t_stat:
        failures.append(f"t-stat below {tier.min_t_stat:.2f}")
    if float(statistics["profit_factor"]) < tier.min_profit_factor:
        failures.append(f"profit factor below {tier.min_profit_factor:.2f}")
    if float(statistics["recent_profit_factor"]) < tier.min_recent_profit_factor:
        failures.append(f"recent profit factor below {tier.min_recent_profit_factor:.2f}")
    if float(statistics["profit_cushion_units"]) < tier.min_profit_cushion_units:
        failures.append(
            f"profit cushion below {tier.min_profit_cushion_units:.1f} median-loss units"
        )
    return failures


def evaluate_profit_funded_scaling(
    nets: Iterable[float],
    *,
    multiplier_cap: float = 3.0,
    recent_window: int = 20,
    tiers: Sequence[PromotionTier] = DEFAULT_TIERS,
) -> dict:
    statistics = strategy_statistics(nets, recent_window=recent_window)
    eligible = tiers[0]
    for tier in tiers[1:]:
        if _tier_failures(statistics, tier):
            break
        eligible = tier

    effective_tier = eligible.tier
    rollback_reasons: list[str] = []
    drawdown_units = float(statistics["current_drawdown_units"])
    drawdown_pct = float(statistics["current_drawdown_from_profit_peak_pct"])
    losing_streak = int(statistics["current_losing_streak"])
    recent_profit_factor = float(statistics["recent_profit_factor"])

    if float(statistics["net_profit_usd"]) <= 0:
        effective_tier = 0
        rollback_reasons.append("no retained attributable profit cushion")
    if losing_streak >= 5 or recent_profit_factor < 0.80:
        effective_tier = 0
        rollback_reasons.append("hard rollback: loss streak or recent PF deterioration")
    elif drawdown_units >= 4.0 or drawdown_pct >= 50.0:
        effective_tier = 0
        rollback_reasons.append("hard rollback: profit high-water drawdown")
    elif losing_streak >= 3 or drawdown_units >= 2.0 or drawdown_pct >= 25.0:
        effective_tier = max(effective_tier - 1, 0)
        rollback_reasons.append("one-tier rollback: early drawdown warning")

    effective = next(tier for tier in tiers if tier.tier == effective_tier)
    cap = min(max(float(multiplier_cap), 1.0), 3.0)
    multiplier = min(effective.multiplier, cap)
    promotion_authorized = multiplier > 1.0
    next_tier = next((tier for tier in tiers if tier.tier > effective_tier), None)
    blockers = _tier_failures(statistics, next_tier) if next_tier is not None else []
    blockers.extend(rollback_reasons)
    if cap <= 1.0 and eligible.multiplier > 1.0:
        blockers.append("account-wide guard caps scaling at 1x")

    return {
        "schema": SCHEMA,
        "policy": "closed attributable profit only; no balance, credit, floating P/L, or manual profit",
        "statistics": statistics,
        "eligible_tier_before_rollback": eligible.tier,
        "effective_tier": effective_tier,
        "lot_multiplier": round(multiplier, 4),
        "multiplier_cap": round(cap, 4),
        "promotion_authorized": promotion_authorized,
        "rollback_reasons": rollback_reasons,
        "next_tier": None
        if next_tier is None
        else {
            "tier": next_tier.tier,
            "multiplier": next_tier.multiplier,
            "min_trades": next_tier.min_trades,
        },
        "promotion_blockers": blockers,
    }


def evaluate_account_guard(account_nets: Iterable[float], *, recent_window: int = 20) -> dict:
    statistics = strategy_statistics(account_nets, recent_window=recent_window)
    blockers: list[str] = []
    cap = 3.0
    if int(statistics["closed_trades"]) == 0:
        blockers.append("no closed account history")
        cap = 1.0
    if float(statistics["net_profit_usd"]) <= 0:
        blockers.append("account-wide closed net P/L is not positive")
        cap = 1.0
    if float(statistics["recent_net_profit_usd"]) <= 0:
        blockers.append("recent account-wide closed net P/L is not positive")
        cap = 1.0
    if (
        float(statistics["current_drawdown_units"]) >= 4.0
        or float(statistics["current_drawdown_from_profit_peak_pct"]) >= 50.0
    ):
        blockers.append("account-wide profit high-water drawdown is severe")
        cap = 1.0
    elif (
        float(statistics["current_drawdown_units"]) >= 2.0
        or float(statistics["recent_profit_factor"]) < 1.10
    ):
        blockers.append("account-wide drawdown or recent PF limits scaling")
        cap = min(cap, 1.5)
    return {
        "statistics": statistics,
        "multiplier_cap": cap,
        "status": "clear" if cap > 1.0 else "floor_only",
        "blockers": blockers,
        "manual_losses_constrain_account_risk": True,
        "manual_profits_do_not_establish_strategy_edge": True,
    }


def _max_drawdown(values: Sequence[float]) -> float:
    return _serial_drawdown(values)[2]


def simulate_profit_funded_path(nets: Iterable[float]) -> dict:
    values = _finite(nets)
    observed: list[float] = []
    scaled: list[float] = []
    multipliers: list[float] = []
    for value in values:
        decision = evaluate_profit_funded_scaling(observed)
        multiplier = float(decision["lot_multiplier"])
        multipliers.append(multiplier)
        scaled.append(value * multiplier)
        observed.append(value)
    return {
        "closed_trades": len(values),
        "fixed_1x_net_profit_usd": round(sum(values), 6),
        "adaptive_net_profit_usd": round(sum(scaled), 6),
        "fixed_1x_max_drawdown_usd": round(_max_drawdown(values), 6),
        "adaptive_max_drawdown_usd": round(_max_drawdown(scaled), 6),
        "max_multiplier_used": max(multipliers, default=1.0),
        "trades_above_1x": sum(multiplier > 1.0 for multiplier in multipliers),
        "no_lookahead": True,
    }


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(max(probability, 0.0), 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def bootstrap_scaling_stress(
    nets: Iterable[float],
    *,
    samples: int = 500,
    block_size: int = 5,
    seed: int = 247,
) -> dict:
    values = _finite(nets)
    if not values or samples <= 0:
        return {
            "samples": 0,
            "method": "deterministic moving-block bootstrap",
            "status": "insufficient_history",
        }
    block_size = min(max(int(block_size), 1), len(values))
    rng = random.Random(seed)
    adaptive_nets: list[float] = []
    adaptive_drawdowns: list[float] = []
    fixed_nets: list[float] = []
    fixed_drawdowns: list[float] = []
    for _ in range(int(samples)):
        sampled: list[float] = []
        while len(sampled) < len(values):
            start = rng.randrange(len(values))
            sampled.extend(values[(start + offset) % len(values)] for offset in range(block_size))
        sampled = sampled[: len(values)]
        simulation = simulate_profit_funded_path(sampled)
        fixed_nets.append(float(simulation["fixed_1x_net_profit_usd"]))
        adaptive_nets.append(float(simulation["adaptive_net_profit_usd"]))
        fixed_drawdowns.append(float(simulation["fixed_1x_max_drawdown_usd"]))
        adaptive_drawdowns.append(float(simulation["adaptive_max_drawdown_usd"]))
    return {
        "samples": int(samples),
        "method": "deterministic moving-block bootstrap",
        "block_size": block_size,
        "seed": seed,
        "status": "diagnostic_only_not_a_profit_forecast",
        "fixed_1x": {
            "median_net_profit_usd": round(_quantile(fixed_nets, 0.50), 4),
            "p05_net_profit_usd": round(_quantile(fixed_nets, 0.05), 4),
            "p95_max_drawdown_usd": round(_quantile(fixed_drawdowns, 0.95), 4),
            "probability_of_loss": round(sum(value < 0 for value in fixed_nets) / len(fixed_nets), 4),
        },
        "adaptive": {
            "median_net_profit_usd": round(_quantile(adaptive_nets, 0.50), 4),
            "p05_net_profit_usd": round(_quantile(adaptive_nets, 0.05), 4),
            "p95_max_drawdown_usd": round(_quantile(adaptive_drawdowns, 0.95), 4),
            "probability_of_loss": round(
                sum(value < 0 for value in adaptive_nets) / len(adaptive_nets), 4
            ),
        },
    }
