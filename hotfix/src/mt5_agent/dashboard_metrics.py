"""Quantitative dashboard metrics computation.

Pure functions to compute per-edge and global trading metrics from closed trades.
All metrics are computed from first principles: no external libraries except numpy/pandas.

Functions:
  - compute_metrics: Main entry point, returns dict with all metrics
  - _compute_per_edge: Per-magic-number metrics
  - _compute_global: Account-level metrics
  - _compute_correlation_matrix: Symbol-to-symbol correlation
  - _compute_slippage_by_hour: Aggregated slippage by UTC hour
  - _compute_rolling_metrics: 7d/30d Sharpe and max DD

Returns a dict structure suitable for rendering to HTML dashboard.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from mt5_agent.edge_registry import EdgeRegistry


@dataclass(frozen=True)
class ClosedTrade:
    """Minimal representation of a closed trade for metrics computation."""
    magic: int
    symbol: str
    entry_time: str  # ISO format "2026-06-24T12:30:00Z"
    exit_time: str   # ISO format "2026-06-24T13:30:00Z"
    profit: float    # USD P&L (positive=win, negative=loss)
    volume: float    # lot size
    entry_price: float
    exit_price: float
    status: str = "closed"  # "closed" | "open"


def compute_metrics(
    trades: list[ClosedTrade],
    registry: EdgeRegistry,
    heartbeat: dict[str, Any] | None = None,
    slippage_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute all dashboard metrics from closed trades.

    Args:
        trades: List of closed trades
        registry: EdgeRegistry with edge metadata + backtested stats
        heartbeat: Optional, {"as_of": "...", "balance": 10000, "equity": 10150, ...}
        slippage_analysis: Optional, {"by_symbol_by_hour": {...}}

    Returns:
        Dict with keys:
        {
            "as_of": "2026-06-24T16:30:00Z",
            "account": {
                "equity": 10150.75,
                "balance": 10000.00,
                "daily_pnl": 150.75,
                "drawdown_pct": 0.5,
            },
            "global": {
                "total_trades": 42,
                "total_pnl": 450.50,
                "win_rate": 0.535,
                "sharpe": 1.62,
                "sortino": 1.80,
                "max_dd_pct": 2.1,
                "avg_win": 12.50,
                "avg_loss": -8.30,
            },
            "per_edge": {
                "88001": {
                    "name": "GOLD_DRIFT",
                    "symbol": "GOLD",
                    "n_trades": 12,
                    "pnl": 85.00,
                    "win_rate": 0.583,
                    "sharpe": 1.62,
                    "sortino": 1.80,
                    "max_dd_pct": 2.1,
                    "avg_win": 12.50,
                    "avg_loss": -8.30,
                    "status": "LIVE",
                },
                ...
            },
            "correlation": {
                "symbols": ["GOLD", "USDJPY"],
                "matrix": [[1.0, 0.28], [0.28, 1.0]],  # 2x2
            },
            "slippage_by_hour": {
                "0": 2.1,
                "1": 1.9,
                ...,
                "23": 2.5,
            },
            "equity_curve": {
                "dates": ["2026-06-20", "2026-06-21", ...],
                "equity": [10000, 10050, ...],
            },
            "rolling_metrics": {
                "7d": {
                    "dates": ["2026-06-20", ...],
                    "sharpe": [1.5, 1.6, ...],
                    "max_dd_pct": [-1.5, -1.2, ...],
                },
                "30d": {...},
            },
        }
    """
    if not trades:
        return _empty_metrics()

    as_of = (heartbeat or {}).get("as_of") or datetime.now(tz=timezone.utc).isoformat()

    # Filter to closed trades only
    closed_trades = [t for t in trades if t.status == "closed"]
    if not closed_trades:
        return _empty_metrics(as_of=as_of)

    # Compute component metrics
    account = _compute_account_metrics(closed_trades, heartbeat or {})
    global_metrics = _compute_global_metrics(closed_trades)
    per_edge = _compute_per_edge_metrics(closed_trades, registry)
    correlation = _compute_correlation_matrix(closed_trades)
    slippage_by_hour = _compute_slippage_by_hour(slippage_analysis or {})
    equity_curve = _compute_equity_curve(closed_trades)
    rolling = _compute_rolling_metrics(closed_trades)

    return {
        "as_of": as_of,
        "account": account,
        "global": global_metrics,
        "per_edge": per_edge,
        "correlation": correlation,
        "slippage_by_hour": slippage_by_hour,
        "equity_curve": equity_curve,
        "rolling_metrics": rolling,
    }


def _empty_metrics(as_of: str | None = None) -> dict[str, Any]:
    """Return empty metrics structure when no trades exist."""
    if as_of is None:
        as_of = datetime.now(tz=timezone.utc).isoformat()

    return {
        "as_of": as_of,
        "account": {
            "equity": 0.0,
            "balance": 0.0,
            "daily_pnl": 0.0,
            "drawdown_pct": 0.0,
        },
        "global": {
            "total_trades": 0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_dd_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
        },
        "per_edge": {},
        "correlation": {"symbols": [], "matrix": []},
        "slippage_by_hour": {str(h): 0.0 for h in range(24)},
        "equity_curve": {"dates": [], "equity": []},
        "rolling_metrics": {"7d": {"dates": [], "sharpe": [], "max_dd_pct": []},
                           "30d": {"dates": [], "sharpe": [], "max_dd_pct": []}},
    }


def _compute_account_metrics(trades: list[ClosedTrade], heartbeat: dict[str, Any]) -> dict[str, Any]:
    """Compute account-level metrics from heartbeat."""
    return {
        "equity": heartbeat.get("equity", 0.0),
        "balance": heartbeat.get("balance", 0.0),
        "daily_pnl": heartbeat.get("daily_realized_pnl", 0.0),
        "drawdown_pct": heartbeat.get("drawdown_pct", 0.0),
    }


def _compute_global_metrics(trades: list[ClosedTrade]) -> dict[str, Any]:
    """Compute metrics across all trades."""
    if not trades:
        return {
            "total_trades": 0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_dd_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
        }

    n = len(trades)
    total_pnl = sum(t.profit for t in trades)

    # Win rate
    wins = sum(1 for t in trades if t.profit > 0)
    wr = wins / n if n > 0 else 0.0

    # Daily returns (for Sharpe/Sortino/Max DD)
    daily_returns = defaultdict(float)
    for t in trades:
        day = t.exit_time[:10]  # YYYY-MM-DD
        daily_returns[day] += t.profit

    ret_list = np.array(list(daily_returns.values()))

    # Sharpe ratio
    sharpe = _compute_sharpe(ret_list)

    # Sortino (penalize downside volatility)
    sortino = _compute_sortino(ret_list)

    # Max drawdown
    max_dd = _compute_max_drawdown(ret_list)

    # Avg win / loss
    profits = np.array([t.profit for t in trades])
    wins_arr = profits[profits > 0]
    losses_arr = profits[profits <= 0]

    avg_win = float(np.mean(wins_arr)) if len(wins_arr) > 0 else 0.0
    avg_loss = float(np.mean(losses_arr)) if len(losses_arr) > 0 else 0.0

    return {
        "total_trades": n,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(wr, 4),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
    }


def _compute_per_edge_metrics(
    trades: list[ClosedTrade],
    registry: EdgeRegistry,
) -> dict[str, dict[str, Any]]:
    """Compute metrics for each magic number (edge)."""
    by_magic = defaultdict(list)
    for t in trades:
        by_magic[t.magic].append(t)

    result = {}
    for magic, edge_trades in by_magic.items():
        if not edge_trades:
            continue

        # Look up edge metadata
        edge_rec = registry.by_magic(magic)
        name = edge_rec.key if edge_rec else f"MAGIC_{magic}"
        symbol = edge_rec.symbol if edge_rec else (edge_trades[0].symbol if edge_trades else "?")
        stage = edge_rec.stage if edge_rec else "UNKNOWN"

        # Map stage to display status
        if stage == "LIVE":
            status = "LIVE"
        elif stage == "BLACKLIST":
            status = "BLACKLISTED"
        elif stage == "WATCH":
            status = "PAUSED"
        else:
            status = stage

        # Metrics
        n = len(edge_trades)
        pnl = sum(t.profit for t in edge_trades)

        wins = sum(1 for t in edge_trades if t.profit > 0)
        wr = wins / n if n > 0 else 0.0

        # Daily returns for this edge
        daily_ret = defaultdict(float)
        for t in edge_trades:
            day = t.exit_time[:10]
            daily_ret[day] += t.profit

        ret_arr = np.array(list(daily_ret.values()))
        sharpe = _compute_sharpe(ret_arr)
        sortino = _compute_sortino(ret_arr)
        max_dd = _compute_max_drawdown(ret_arr)

        # Avg win / loss
        profits = np.array([t.profit for t in edge_trades])
        wins_arr = profits[profits > 0]
        losses_arr = profits[profits <= 0]

        avg_win = float(np.mean(wins_arr)) if len(wins_arr) > 0 else 0.0
        avg_loss = float(np.mean(losses_arr)) if len(losses_arr) > 0 else 0.0

        result[str(magic)] = {
            "name": name,
            "symbol": symbol,
            "n_trades": n,
            "pnl": round(pnl, 2),
            "win_rate": round(wr, 4),
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "max_dd_pct": round(max_dd * 100, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "status": status,
        }

    return result


def _compute_correlation_matrix(trades: list[ClosedTrade]) -> dict[str, Any]:
    """Compute correlation matrix of per-trade returns by symbol."""
    if not trades:
        return {"symbols": [], "matrix": []}

    # Group by symbol
    by_symbol = defaultdict(list)
    for t in trades:
        by_symbol[t.symbol].append(t)

    symbols = sorted(by_symbol.keys())
    if not symbols:
        return {"symbols": [], "matrix": []}

    n = len(symbols)
    if n == 1:
        # Single symbol: correlation matrix is [[1.0]]
        return {
            "symbols": symbols,
            "matrix": [[1.0]],
        }

    # Build returns arrays (aligned by trade order)
    returns_by_symbol = {}
    for sym in symbols:
        sym_trades = sorted(by_symbol[sym], key=lambda t: t.exit_time)
        returns_by_symbol[sym] = [t.profit for t in sym_trades]

    # Compute pairwise correlations
    corr_matrix = np.zeros((n, n))
    for i, sym_i in enumerate(symbols):
        for j, sym_j in enumerate(symbols):
            if i == j:
                corr_matrix[i, j] = 1.0
            else:
                ret_i = returns_by_symbol[sym_i]
                ret_j = returns_by_symbol[sym_j]

                # Align lengths (shorter of the two)
                min_len = min(len(ret_i), len(ret_j))
                if min_len > 1:
                    corr = np.corrcoef(ret_i[:min_len], ret_j[:min_len])
                    if corr.shape == (2, 2) and not np.isnan(corr[0, 1]):
                        corr_matrix[i, j] = corr[0, 1]
                    else:
                        corr_matrix[i, j] = 0.0
                else:
                    corr_matrix[i, j] = 0.0

    # Convert to list of lists
    matrix_list = corr_matrix.tolist()

    return {
        "symbols": symbols,
        "matrix": matrix_list,
    }


def _compute_slippage_by_hour(slippage_analysis: dict[str, Any]) -> dict[str, float]:
    """Extract mean slippage per UTC hour from slippage_analysis.json."""
    result = {str(h): 0.0 for h in range(24)}

    if not slippage_analysis:
        return result

    by_hour_by_symbol = slippage_analysis.get("by_symbol_by_hour", {})
    if not by_hour_by_symbol:
        return result

    # For each hour, average slippage across symbols
    for hour in range(24):
        hour_str = f"{hour:02d}"
        slips = []
        for symbol, by_hour in by_hour_by_symbol.items():
            if hour_str in by_hour:
                slip = by_hour[hour_str].get("mean_slippage_pts", 0.0)
                if slip > 0:
                    slips.append(slip)

        if slips:
            result[str(hour)] = round(float(np.mean(slips)), 2)

    return result


def _compute_equity_curve(trades: list[ClosedTrade]) -> dict[str, Any]:
    """Compute daily equity curve from cumulative P&L."""
    if not trades:
        return {"dates": [], "equity": []}

    # Daily cumulative P&L
    daily_pnl = defaultdict(float)
    for t in trades:
        day = t.exit_time[:10]  # YYYY-MM-DD
        daily_pnl[day] += t.profit

    # Sort by date and compute cumulative
    sorted_days = sorted(daily_pnl.keys())
    dates = sorted_days
    equity = []
    cumul = 0.0
    for day in sorted_days:
        cumul += daily_pnl[day]
        equity.append(round(cumul, 2))

    return {
        "dates": dates,
        "equity": equity,
    }


def _compute_rolling_metrics(trades: list[ClosedTrade]) -> dict[str, dict[str, Any]]:
    """Compute 7d and 30d rolling Sharpe/Max DD metrics."""
    if not trades:
        return {
            "7d": {"dates": [], "sharpe": [], "max_dd_pct": []},
            "30d": {"dates": [], "sharpe": [], "max_dd_pct": []},
        }

    # Sort trades by exit time
    sorted_trades = sorted(trades, key=lambda t: t.exit_time)

    # Daily returns
    daily_pnl = defaultdict(float)
    for t in sorted_trades:
        day = t.exit_time[:10]
        daily_pnl[day] += t.profit

    sorted_days = sorted(daily_pnl.keys())
    if len(sorted_days) < 2:
        return {
            "7d": {"dates": [], "sharpe": [], "max_dd_pct": []},
            "30d": {"dates": [], "sharpe": [], "max_dd_pct": []},
        }

    # Compute rolling metrics
    def compute_rolling(window_days: int) -> tuple[list[str], list[float], list[float]]:
        result_dates = []
        result_sharpe = []
        result_dd = []

        for i in range(len(sorted_days)):
            end_day = sorted_days[i]

            # Look back window_days
            start_idx = max(0, i - window_days + 1)
            window = sorted_days[start_idx:i+1]

            ret_arr = np.array([daily_pnl[d] for d in window])

            sharpe = _compute_sharpe(ret_arr)
            dd = _compute_max_drawdown(ret_arr)

            result_dates.append(end_day)
            result_sharpe.append(round(sharpe, 2))
            result_dd.append(round(dd * 100, 2))

        return result_dates, result_sharpe, result_dd

    dates_7d, sharpe_7d, dd_7d = compute_rolling(7)
    dates_30d, sharpe_30d, dd_30d = compute_rolling(30)

    return {
        "7d": {"dates": dates_7d, "sharpe": sharpe_7d, "max_dd_pct": dd_7d},
        "30d": {"dates": dates_30d, "sharpe": sharpe_30d, "max_dd_pct": dd_30d},
    }


# ---- Helper math functions ----

def _compute_sharpe(returns: np.ndarray) -> float:
    """Annualized Sharpe ratio (assuming 252 trading days per year)."""
    if len(returns) < 2:
        return 0.0

    mean_ret = float(np.mean(returns))
    std_ret = float(np.std(returns))

    if std_ret == 0.0 or np.isnan(std_ret):
        return 0.0

    sharpe = (mean_ret / std_ret) * math.sqrt(252)
    return 0.0 if np.isnan(sharpe) or np.isinf(sharpe) else float(sharpe)


def _compute_sortino(returns: np.ndarray) -> float:
    """Sortino ratio (penalize only downside volatility)."""
    if len(returns) < 2:
        return 0.0

    mean_ret = float(np.mean(returns))

    # Downside deviation: only negative returns
    downside = returns[returns < 0]
    if len(downside) == 0:
        # No downside: return Sharpe as proxy
        return _compute_sharpe(returns)

    std_downside = float(np.std(downside))
    if std_downside == 0.0 or np.isnan(std_downside):
        return 0.0

    sortino = (mean_ret / std_downside) * math.sqrt(252)
    return 0.0 if np.isnan(sortino) or np.isinf(sortino) else float(sortino)


def _compute_max_drawdown(returns: np.ndarray) -> float:
    """Max drawdown as a fraction (e.g., 0.05 = 5% DD)."""
    if len(returns) == 0:
        return 0.0

    cumul = np.cumsum(returns)
    running_max = np.maximum.accumulate(cumul)

    # Drawdown at each point
    drawdowns = (running_max - cumul) / running_max

    # Handle division by zero and NaN
    drawdowns = np.nan_to_num(drawdowns, nan=0.0, posinf=0.0, neginf=0.0)

    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
    return 0.0 if np.isnan(max_dd) or np.isinf(max_dd) else max(0.0, max_dd)
