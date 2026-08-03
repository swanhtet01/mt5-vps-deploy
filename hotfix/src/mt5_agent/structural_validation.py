"""Leakage-resistant validation for fixed one-hour structural trading schedules."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StructuralObservation:
    time: datetime
    gross_pnl_long: float
    round_trip_cost: float


def entry_bucket_for_return_bucket(return_weekday: int, return_hour: int) -> tuple[int, int]:
    """Map a completed one-hour return bar to its executable entry weekday/hour."""
    if not 0 <= int(return_weekday) <= 6 or not 0 <= int(return_hour) <= 23:
        raise ValueError("weekday must be 0..6 and hour must be 0..23")
    if int(return_hour) > 0:
        return int(return_weekday), int(return_hour) - 1
    return (int(return_weekday) - 1) % 7, 23


def walk_forward_ranges(
    count: int,
    *,
    folds: int = 4,
    oos_fraction: float = 0.4,
    min_train: int = 60,
    purge: int = 1,
) -> list[tuple[int, int, int, int]]:
    """Return (fold, train_end, test_start, test_end) expanding-window ranges."""
    if folds < 1:
        raise ValueError("folds must be at least 1")
    if not 0.1 <= oos_fraction <= 0.8:
        raise ValueError("oos_fraction must be between 0.1 and 0.8")
    if min_train < 2 or purge < 0:
        raise ValueError("min_train must be >= 2 and purge must be >= 0")
    oos_start = max(min_train + purge, int(count * (1.0 - oos_fraction)))
    remaining = count - oos_start
    if remaining < folds:
        return []
    fold_size = remaining // folds
    ranges: list[tuple[int, int, int, int]] = []
    test_start = oos_start
    for fold in range(1, folds + 1):
        test_end = count if fold == folds else test_start + fold_size
        train_end = test_start - purge
        if train_end < min_train or test_end <= test_start:
            return []
        ranges.append((fold, train_end, test_start, test_end))
        test_start = test_end
    return ranges


def _net(observation: StructuralObservation, direction: int) -> float:
    return direction * observation.gross_pnl_long - observation.round_trip_cost


def _profit_factor(values: list[float]) -> float:
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    if gross_loss <= 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _one_sided_positive_p(values: list[float]) -> float:
    if len(values) < 2:
        return 1.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    if variance <= 0:
        return 0.0 if mean > 0 else 1.0
    t_stat = mean / math.sqrt(variance / len(values))
    return 0.5 * math.erfc(t_stat / math.sqrt(2.0))


def block_bootstrap_mean_lcb(
    values: list[float],
    *,
    confidence: float = 0.95,
    samples: int = 2000,
    block_size: int = 4,
    seed: int = 0,
) -> float | None:
    """Moving-block bootstrap lower bound for mean net P/L."""
    if not values:
        return None
    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be between 0.5 and 1.0")
    if samples < 100:
        raise ValueError("samples must be at least 100")
    size = min(max(int(block_size), 1), len(values))
    rng = random.Random(seed)
    means: list[float] = []
    last_start = len(values) - size
    for _ in range(samples):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.randint(0, last_start)
            sample.extend(values[start:start + size])
        sample = sample[:len(values)]
        means.append(sum(sample) / len(sample))
    means.sort()
    index = max(0, min(len(means) - 1, int((1.0 - confidence) * len(means))))
    return means[index]


def validate_structural_schedule(
    observations: list[StructuralObservation],
    *,
    folds: int = 4,
    oos_fraction: float = 0.4,
    min_train: int = 60,
    purge: int = 1,
    min_oos_trades: int = 60,
    min_profit_factor: float = 1.2,
    min_profitable_fold_ratio: float = 0.75,
    bootstrap_samples: int = 2000,
    bootstrap_block_size: int = 4,
    seed: int = 0,
) -> dict:
    """Select direction on prior data and grade it once on each unseen fold."""
    ordered = sorted(observations, key=lambda item: item.time)
    for previous, current in zip(ordered, ordered[1:]):
        if current.time <= previous.time:
            raise ValueError("observation times must be unique")
    for item in ordered:
        if not math.isfinite(item.gross_pnl_long):
            raise ValueError("gross P/L must be finite")
        if not math.isfinite(item.round_trip_cost) or item.round_trip_cost < 0:
            raise ValueError("round-trip cost must be finite and non-negative")

    ranges = walk_forward_ranges(
        len(ordered), folds=folds, oos_fraction=oos_fraction,
        min_train=min_train, purge=purge,
    )
    if not ranges:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "reasons": ["not enough observations for purged walk-forward folds"],
            "folds": [],
            "oos": {"trades": 0},
        }

    fold_reports: list[dict] = []
    pooled: list[float] = []
    directions: list[str] = []
    for fold, train_end, test_start, test_end in ranges:
        train = ordered[:train_end]
        long_mean = sum(_net(item, 1) for item in train) / len(train)
        short_mean = sum(_net(item, -1) for item in train) / len(train)
        direction = 1 if long_mean >= short_mean else -1
        direction_name = "long" if direction == 1 else "short"
        test_values = [_net(item, direction) for item in ordered[test_start:test_end]]
        pooled.extend(test_values)
        directions.append(direction_name)
        fold_reports.append({
            "fold": fold,
            "train_trades": len(train),
            "purged_trades": test_start - train_end,
            "test_trades": len(test_values),
            "direction": direction_name,
            "train_net_mean": round(max(long_mean, short_mean), 6),
            "test_net": round(sum(test_values), 6),
            "test_net_mean": round(sum(test_values) / len(test_values), 6),
            "test_profit_factor": (
                None if math.isinf(_profit_factor(test_values))
                else round(_profit_factor(test_values), 6)
            ),
        })

    net = sum(pooled)
    mean = net / len(pooled) if pooled else 0.0
    profit_factor_raw = _profit_factor(pooled)
    profitable_folds = sum(1 for report in fold_reports if report["test_net"] > 0)
    profitable_fold_ratio = profitable_folds / len(fold_reports)
    lcb = block_bootstrap_mean_lcb(
        pooled, samples=bootstrap_samples, block_size=bootstrap_block_size, seed=seed,
    )
    reasons: list[str] = []
    if len(pooled) < min_oos_trades:
        reasons.append(f"out-of-sample trades {len(pooled)} < {min_oos_trades}")
    if len(set(directions)) != 1:
        reasons.append("training-selected direction changed between folds")
    if mean <= 0:
        reasons.append("out-of-sample mean net P/L is not positive")
    if profit_factor_raw < min_profit_factor:
        reasons.append(
            f"out-of-sample profit factor {profit_factor_raw:.2f} < {min_profit_factor:.2f}"
        )
    if profitable_fold_ratio < min_profitable_fold_ratio:
        reasons.append(
            f"profitable fold ratio {profitable_fold_ratio:.2f} < {min_profitable_fold_ratio:.2f}"
        )
    if lcb is None or lcb <= 0:
        reasons.append("95% block-bootstrap lower bound is not positive")

    return {
        "verdict": "PASS" if not reasons else "FAIL",
        "reasons": reasons,
        "folds": fold_reports,
        "oos": {
            "trades": len(pooled),
            "net": round(net, 6),
            "mean_net": round(mean, 6),
            "win_rate": round(sum(1 for value in pooled if value > 0) / len(pooled), 6),
            "profit_factor": (
                None if math.isinf(profit_factor_raw) else round(profit_factor_raw, 6)
            ),
            "profitable_fold_ratio": round(profitable_fold_ratio, 6),
            "bootstrap_mean_lcb_95": round(lcb, 6) if lcb is not None else None,
            "one_sided_positive_p": _one_sided_positive_p(pooled),
            "direction": directions[0] if len(set(directions)) == 1 else "mixed",
        },
    }
