"""Fixed H1 research rules shared by historical and paper-forward evaluation."""
from __future__ import annotations

import numpy as np
import pandas as pd


RULES = {
    "trend_following": {"maximum_hold_bars": 12, "stop_atr": 1.5},
    "breakout": {"maximum_hold_bars": 8, "stop_atr": 1.25},
    "range_reversion": {"maximum_hold_bars": 10, "stop_atr": 1.25},
    "volatility_regime": {"maximum_hold_bars": 6, "stop_atr": 1.0},
}


def prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Calculate rule inputs from an oldest-to-newest OHLC frame."""
    ordered = frame.sort_index().copy()
    ordered = ordered[~ordered.index.duplicated(keep="last")]
    for column in ("open", "high", "low", "close"):
        ordered[column] = pd.to_numeric(ordered[column], errors="coerce")
    close = ordered["close"]
    high = ordered["high"]
    low = ordered["low"]
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    returns = close.pct_change(fill_method=None)
    realized_vol_24 = returns.rolling(24, min_periods=18).std()
    rolling_mean_20 = close.rolling(20, min_periods=20).mean()
    rolling_std_20 = close.rolling(20, min_periods=20).std()
    ordered["ema20"] = close.ewm(span=20, adjust=False).mean()
    ordered["ema100"] = close.ewm(span=100, adjust=False).mean()
    ordered["atr14"] = true_range.rolling(14, min_periods=14).mean()
    ordered["momentum24"] = close / close.shift(24) - 1.0
    ordered["realized_vol24"] = realized_vol_24
    ordered["median_vol500"] = realized_vol_24.rolling(500, min_periods=250).median()
    ordered["zscore20"] = (close - rolling_mean_20) / rolling_std_20.replace(0, np.nan)
    ordered["prior24_high"] = high.shift(1).rolling(24, min_periods=24).max()
    ordered["prior24_low"] = low.shift(1).rolling(24, min_periods=24).min()
    ordered["prior120_high"] = high.shift(1).rolling(120, min_periods=120).max()
    ordered["prior120_low"] = low.shift(1).rolling(120, min_periods=120).min()
    return ordered


def signal(row: pd.Series, family: str, direction: int) -> bool:
    """Return a signal from one completed bar only."""
    required = [row.get("close"), row.get("atr14")]
    if any(pd.isna(value) for value in required):
        return False
    if family == "trend_following":
        values = [
            row["ema20"], row["ema100"], row["momentum24"],
            row["realized_vol24"], row["median_vol500"],
        ]
        if any(pd.isna(value) for value in values) or row["median_vol500"] <= 0:
            return False
        volatility_ok = row["realized_vol24"] / row["median_vol500"] < 1.5
        return bool(
            volatility_ok
            and (
                (direction > 0 and row["ema20"] > row["ema100"] and row["momentum24"] > 0)
                or (direction < 0 and row["ema20"] < row["ema100"] and row["momentum24"] < 0)
            )
        )
    if family == "breakout":
        if pd.isna(row["momentum24"]):
            return False
        return bool(
            (direction > 0 and row["close"] > row["prior120_high"] and row["momentum24"] > 0)
            or (direction < 0 and row["close"] < row["prior120_low"] and row["momentum24"] < 0)
        )
    if family == "range_reversion":
        if pd.isna(row["zscore20"]):
            return False
        trend_distance = abs(row["ema20"] - row["ema100"])
        return bool(
            trend_distance < 0.75 * row["atr14"]
            and (
                (direction > 0 and row["zscore20"] <= -2.0)
                or (direction < 0 and row["zscore20"] >= 2.0)
            )
        )
    if family == "volatility_regime":
        if (
            pd.isna(row["realized_vol24"])
            or pd.isna(row["median_vol500"])
            or row["median_vol500"] <= 0
        ):
            return False
        high_volatility = row["realized_vol24"] >= 1.5 * row["median_vol500"]
        return bool(
            high_volatility
            and (
                (direction > 0 and row["close"] > row["prior24_high"])
                or (direction < 0 and row["close"] < row["prior24_low"])
            )
        )
    return False


def rule_exit(row: pd.Series, family: str, direction: int) -> bool:
    """Return whether the latest completed bar closes an open experiment."""
    if family == "trend_following":
        return bool(
            (direction > 0 and row["ema20"] <= row["ema100"])
            or (direction < 0 and row["ema20"] >= row["ema100"])
        )
    if family == "breakout":
        return bool(
            (direction > 0 and row["close"] <= row["prior120_high"])
            or (direction < 0 and row["close"] >= row["prior120_low"])
        )
    if family == "range_reversion":
        return bool(
            (direction > 0 and row["zscore20"] >= 0)
            or (direction < 0 and row["zscore20"] <= 0)
        )
    if family == "volatility_regime":
        return bool(
            (direction > 0 and row["close"] <= row["prior24_high"])
            or (direction < 0 and row["close"] >= row["prior24_low"])
        )
    return False
