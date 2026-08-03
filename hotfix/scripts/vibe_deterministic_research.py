"""Run provider-independent research through Vibe Trading's pinned local loader.

The script never imports MetaTrader5 and has no execution path. It verifies the
sanitized bundle, uses Vibe's local DataLoader, produces regime/risk diagnostics,
and emits only DISCOVERED hypotheses under the strict handoff contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from mt5_agent.vibe_handoff import (  # noqa: E402
    AUDITED_VIBE_COMMIT,
    HANDOFF_SCHEMA,
    REQUIRED_VALIDATION_GATES,
    validate_candidate_handoff,
)


REPORT_SCHEMA = "mt5.vibe_deterministic_research.v1"
BUNDLE_SCHEMA = "mt5.vibe_research_bundle.v1"
DEFAULT_MIN_ROWS = 500
CORRELATION_CLUSTER_THRESHOLD = 0.80
MONTHLY_CALENDAR_DAYS = 30.4375
MONTHLY_BOOTSTRAP_SAMPLES = 5000
MONTHLY_BOOTSTRAP_BLOCK_DAYS = 7
MONTHLY_BOOTSTRAP_SEED = 260804


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _bundle_file(bundle: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError(f"unsafe bundle path: {relative!r}")
    path = bundle.joinpath(*pure.parts)
    if not _within(path, bundle):
        raise ValueError(f"bundle path escaped root: {relative!r}")
    return path


def verify_bundle(bundle: Path) -> tuple[dict[str, Any], str]:
    """Verify the immutable export contract and every manifest-listed file."""
    bundle = bundle.resolve()
    manifest_path = bundle / "manifest.json"
    if not bundle.is_dir() or bundle.is_symlink() or not manifest_path.is_file():
        raise ValueError("research bundle or manifest is missing")
    manifest_sha256 = file_sha256(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise ValueError("research bundle schema mismatch")
    if manifest.get("mode") != "research_only":
        raise ValueError("research bundle is not research_only")
    if manifest.get("contains_credentials") is not False:
        raise ValueError("research bundle credential boundary is invalid")
    if manifest.get("order_authority") is not False:
        raise ValueError("research bundle grants order authority")
    if manifest.get("automatic_live_promotion") is not False:
        raise ValueError("research bundle permits automatic live promotion")
    if manifest.get("export_errors"):
        raise ValueError(f"research bundle contains export errors: {manifest['export_errors']}")
    if manifest.get("vibe_trading", {}).get("audited_commit") != AUDITED_VIBE_COMMIT:
        raise ValueError("research bundle Vibe commit mismatch")
    if manifest.get("source", {}).get("feed_clock_sample", {}).get("coherent") is not True:
        raise ValueError("research bundle feed clock is not coherent")

    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise ValueError("research bundle file list is empty")
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"manifest file record {index} is invalid")
        relative = record.get("path")
        if not isinstance(relative, str) or relative in seen:
            raise ValueError(f"manifest file path {relative!r} is invalid or duplicated")
        seen.add(relative)
        path = _bundle_file(bundle, relative)
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"manifest file is missing or linked: {relative}")
        if path.stat().st_size != int(record.get("bytes", -1)):
            raise ValueError(f"manifest byte count mismatch: {relative}")
        if file_sha256(path) != record.get("sha256"):
            raise ValueError(f"manifest hash mismatch: {relative}")

    bar_records = [record for record in records if record.get("source_symbol")]
    declared_symbols = manifest.get("research_scope", {}).get("symbols")
    source_symbols = [str(record["source_symbol"]) for record in bar_records]
    if declared_symbols != source_symbols or len(source_symbols) != len(set(source_symbols)):
        raise ValueError("research scope does not exactly match bar sources")
    required = {
        "trade_history.csv",
        "account_snapshot.redacted.json",
        "instrument_snapshots.json",
        "data-bridge/config.yaml",
    }
    if not required.issubset(seen):
        raise ValueError(f"research bundle missing required files: {sorted(required - seen)}")
    return manifest, manifest_sha256


def load_vibe_frames(
    bundle: Path,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Load every source through the official pinned Vibe local DataLoader."""
    os.environ["VIBE_TRADING_DATA_CACHE"] = "0"
    from backtest.loaders import local_loader

    config_path = bundle / "data-bridge" / "config.yaml"
    local_loader._CONFIG_PATH = config_path
    records = [record for record in manifest["files"] if record.get("source_symbol")]
    start_date = min(str(record["first"])[:10] for record in records)
    end_date = max(str(record["last"])[:10] for record in records)
    symbols = [str(record["source_symbol"]) for record in records]
    loader = local_loader.DataLoader()
    if not loader.is_available():
        raise RuntimeError("Vibe local DataLoader did not accept the bundle config")
    frames = loader.fetch(symbols, start_date, end_date, interval="1H")
    missing = sorted(set(symbols) - set(frames))
    if missing:
        raise RuntimeError(f"Vibe local DataLoader did not return: {missing}")
    return frames, {
        "implementation": "backtest.loaders.local_loader.DataLoader",
        "vibe_commit": AUDITED_VIBE_COMMIT,
        "config": str(config_path.resolve()),
        "interval": "1H",
        "start_date": start_date,
        "end_date": end_date,
        "symbols_requested": len(symbols),
        "symbols_loaded": len(frames),
        "cache_enabled": False,
    }


def _finite(value: Any, digits: int = 6) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _series_last(series: pd.Series) -> float | None:
    clean = series.replace([np.inf, -np.inf], np.nan).dropna()
    return _finite(clean.iloc[-1]) if not clean.empty else None


def _max_drawdown(values: pd.Series) -> float | None:
    clean = values.replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return None
    peak = clean.cummax()
    drawdown = clean / peak - 1.0
    return _finite(drawdown.min() * 100.0)


def analyze_frame(
    *,
    bundle: Path,
    record: Mapping[str, Any],
    frame: pd.DataFrame,
    minimum_rows: int,
) -> dict[str, Any]:
    """Compute descriptive diagnostics without fitting or claiming an edge."""
    raw = pd.read_csv(_bundle_file(bundle, str(record["path"])))
    raw_dates = pd.to_datetime(raw.get("date"), errors="coerce")
    ohlc_names = ["open", "high", "low", "close"]
    numeric = {name: pd.to_numeric(raw.get(name), errors="coerce") for name in ohlc_names}
    missing_ohlc = int(pd.DataFrame(numeric).isna().any(axis=1).sum())
    invalid_ohlc = int(
        (
            (numeric["high"] < numeric["low"])
            | (numeric["high"] < numeric["open"])
            | (numeric["high"] < numeric["close"])
            | (numeric["low"] > numeric["open"])
            | (numeric["low"] > numeric["close"])
            | (pd.DataFrame(numeric) <= 0).any(axis=1)
        ).sum()
    )
    duplicate_raw = int(raw_dates.duplicated().sum())
    invalid_dates = int(raw_dates.isna().sum())

    ordered = frame.sort_index().copy()
    duplicate_loaded = int(ordered.index.duplicated().sum())
    analysis = ordered[~ordered.index.duplicated(keep="last")]
    close = pd.to_numeric(analysis["close"], errors="coerce")
    high = pd.to_numeric(analysis["high"], errors="coerce")
    low = pd.to_numeric(analysis["low"], errors="coerce")
    returns = close.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema100 = close.ewm(span=100, adjust=False).mean()
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr14 = true_range.rolling(14, min_periods=14).mean()
    atr_pct = atr14 / close * 100.0
    realized_vol_24 = returns.rolling(24, min_periods=18).std() * 100.0
    realized_vol_120 = returns.rolling(120, min_periods=80).std() * 100.0
    median_vol = realized_vol_24.tail(500).median()
    latest_vol = _series_last(realized_vol_24)
    volatility_ratio = (
        latest_vol / float(median_vol)
        if latest_vol is not None and pd.notna(median_vol) and float(median_vol) > 0
        else None
    )
    rolling_mean20 = close.rolling(20, min_periods=20).mean()
    rolling_std20 = close.rolling(20, min_periods=20).std()
    zscore20 = (close - rolling_mean20) / rolling_std20.replace(0, np.nan)
    range_low = close.rolling(120, min_periods=80).min()
    range_high = close.rolling(120, min_periods=80).max()
    range_position = (close - range_low) / (range_high - range_low).replace(0, np.nan)
    trend_strength = (ema20 - ema100) / atr14.replace(0, np.nan)
    momentum_24 = (close / close.shift(24) - 1.0) * 100.0
    momentum_120 = (close / close.shift(120) - 1.0) * 100.0

    latest_ema20 = _series_last(ema20)
    latest_ema100 = _series_last(ema100)
    latest_momentum120 = _series_last(momentum_120)
    if (
        latest_ema20 is not None
        and latest_ema100 is not None
        and latest_momentum120 is not None
        and latest_ema20 > latest_ema100
        and latest_momentum120 > 0
    ):
        trend_regime = "up"
    elif (
        latest_ema20 is not None
        and latest_ema100 is not None
        and latest_momentum120 is not None
        and latest_ema20 < latest_ema100
        and latest_momentum120 < 0
    ):
        trend_regime = "down"
    else:
        trend_regime = "mixed"
    if volatility_ratio is None:
        volatility_regime = "unknown"
    elif volatility_ratio >= 1.5:
        volatility_regime = "high"
    elif volatility_ratio <= 0.67:
        volatility_regime = "low"
    else:
        volatility_regime = "normal"

    intervals = analysis.index.to_series().diff().dt.total_seconds().div(3600).dropna()
    quality_reasons: list[str] = []
    checks = {
        "manifest_rows_match_raw": len(raw) == int(record.get("rows", -1)),
        "vibe_rows_match_raw": len(ordered) == len(raw),
        "minimum_rows_met": len(ordered) >= minimum_rows,
        "duplicate_raw_timestamps": duplicate_raw,
        "duplicate_vibe_timestamps": duplicate_loaded,
        "invalid_dates": invalid_dates,
        "missing_ohlc_rows": missing_ohlc,
        "invalid_ohlc_rows": invalid_ohlc,
    }
    if not checks["manifest_rows_match_raw"]:
        quality_reasons.append("manifest/raw row mismatch")
    if not checks["vibe_rows_match_raw"]:
        quality_reasons.append("Vibe loader changed row count")
    if not checks["minimum_rows_met"]:
        quality_reasons.append(f"fewer than {minimum_rows} loaded bars")
    for key in (
        "duplicate_raw_timestamps",
        "duplicate_vibe_timestamps",
        "invalid_dates",
        "missing_ohlc_rows",
        "invalid_ohlc_rows",
    ):
        if checks[key]:
            quality_reasons.append(f"{key}={checks[key]}")

    last_timestamp = analysis.index[-1] if not analysis.empty else None
    return {
        "source_symbol": str(record["source_symbol"]),
        "broker_symbol": str(record["broker_symbol"]),
        "timeframe": str(record["timeframe"]),
        "data_quality": {
            "status": "PASS" if not quality_reasons else "FAIL",
            "reasons": quality_reasons,
            "raw_rows": len(raw),
            "vibe_loaded_rows": len(ordered),
            **checks,
            "median_interval_hours": _finite(intervals.median(), 3),
            "gaps_over_2h": int((intervals > 2.0).sum()),
            "max_gap_hours": _finite(intervals.max(), 3),
        },
        "last_bar": str(last_timestamp.isoformat()) if last_timestamp is not None else None,
        "last_bar_age_hours_at_export": _finite(record.get("last_bar_age_hours"), 3),
        "last_close": _series_last(close),
        "trend_regime": trend_regime,
        "volatility_regime": volatility_regime,
        "momentum_24_pct": _series_last(momentum_24),
        "momentum_120_pct": latest_momentum120,
        "ema20_minus_ema100_atr": _series_last(trend_strength),
        "atr14_pct": _series_last(atr_pct),
        "realized_vol_24_pct_per_bar": latest_vol,
        "realized_vol_120_pct_per_bar": _series_last(realized_vol_120),
        "volatility_ratio_vs_recent_median": _finite(volatility_ratio),
        "zscore_20": _series_last(zscore20),
        "range_position_120": _series_last(range_position),
        "max_drawdown_120_pct": _max_drawdown(close.tail(120)),
    }


def build_correlation_report(
    analyses: list[dict[str, Any]],
    frames: Mapping[str, pd.DataFrame],
) -> dict[str, Any]:
    source_to_broker = {item["source_symbol"]: item["broker_symbol"] for item in analyses}
    return_series = {
        source_to_broker[source]: pd.to_numeric(frame["close"], errors="coerce")
        .pct_change(fill_method=None)
        .replace([np.inf, -np.inf], np.nan)
        for source, frame in frames.items()
    }
    joined = pd.concat(return_series, axis=1).sort_index()
    correlation = joined.corr(min_periods=250)
    names = list(correlation.columns)
    pairs: list[dict[str, Any]] = []
    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, left in enumerate(names):
        for right in names[left_index + 1 :]:
            value = correlation.loc[left, right]
            if pd.isna(value):
                continue
            coefficient = float(value)
            pairs.append(
                {
                    "left": left,
                    "right": right,
                    "correlation": round(coefficient, 6),
                    "overlap_bars": int(joined[[left, right]].dropna().shape[0]),
                }
            )
            if abs(coefficient) >= CORRELATION_CLUSTER_THRESHOLD:
                union(left, right)
    pairs.sort(key=lambda item: abs(item["correlation"]), reverse=True)
    grouped: dict[str, list[str]] = {}
    for name in names:
        grouped.setdefault(find(name), []).append(name)
    clusters = [sorted(values) for values in grouped.values() if len(values) > 1]
    clusters.sort(key=lambda values: (-len(values), values))
    matrix = {
        left: {right: _finite(correlation.loc[left, right]) for right in names}
        for left in names
    }
    return {
        "method": "Pearson correlation of aligned completed H1 close-to-close returns",
        "minimum_overlap_bars": 250,
        "cluster_absolute_threshold": CORRELATION_CLUSTER_THRESHOLD,
        "matrix": matrix,
        "strongest_pairs": pairs[:12],
        "clusters": clusters,
    }


def _ledger_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {
            "trades": 0,
            "net_usd": 0.0,
            "win_rate": None,
            "profit_factor": None,
            "max_cumulative_drawdown_usd": 0.0,
        }
    net = pd.to_numeric(frame["net_usd"], errors="coerce").fillna(0.0)
    gains = float(net[net > 0].sum())
    losses = float(-net[net < 0].sum())
    cumulative = net.cumsum()
    drawdown = cumulative - cumulative.cummax()
    return {
        "trades": len(frame),
        "net_usd": _finite(net.sum(), 2),
        "win_rate": _finite((net > 0).mean(), 6),
        "profit_factor": _finite(gains / losses, 6) if losses > 0 else None,
        "max_cumulative_drawdown_usd": _finite(-drawdown.min(), 2),
    }


def historical_automated_monthly_range(frame: pd.DataFrame) -> dict[str, Any]:
    """Describe resampled historical automated P/L without forecasting returns."""
    automated = frame[frame["strategy_magic"] != 0].copy()
    automated["net_usd"] = pd.to_numeric(automated["net_usd"], errors="coerce")
    automated = automated.dropna(subset=["close_time", "net_usd"]).sort_values("close_time")
    if automated.empty:
        return {
            "status": "INSUFFICIENT_DATA",
            "interpretation": "historical_resampling_not_forecast",
            "reason": "no closed trades with nonzero strategy magic",
            "trades": 0,
        }

    first_close = automated["close_time"].iloc[0]
    last_close = automated["close_time"].iloc[-1]
    active_days = max((last_close - first_close).total_seconds() / 86400.0, 1.0)
    daily_index = pd.date_range(first_close.normalize(), last_close.normalize(), freq="D")
    daily = (
        automated.assign(day=automated["close_time"].dt.normalize())
        .groupby("day")["net_usd"]
        .sum()
        .reindex(daily_index, fill_value=0.0)
        .astype(float)
    )
    values = daily.to_numpy(dtype=float)
    block_size = min(MONTHLY_BOOTSTRAP_BLOCK_DAYS, len(values))
    rng = np.random.default_rng(MONTHLY_BOOTSTRAP_SEED)
    samples = np.empty(MONTHLY_BOOTSTRAP_SAMPLES, dtype=float)
    last_start = len(values) - block_size
    for sample_index in range(MONTHLY_BOOTSTRAP_SAMPLES):
        chunks: list[np.ndarray] = []
        sampled = 0
        while sampled < 30:
            start = int(rng.integers(0, last_start + 1))
            chunk = values[start : start + block_size]
            chunks.append(chunk)
            sampled += len(chunk)
        samples[sample_index] = float(np.concatenate(chunks)[:30].sum())

    all_closes = frame.dropna(subset=["close_time"])["close_time"]
    export_days = (
        max((all_closes.max() - all_closes.min()).total_seconds() / 86400.0, 1.0)
        if not all_closes.empty
        else active_days
    )
    rolling_30 = daily.rolling(30).sum().dropna()
    quantiles = np.quantile(samples, [0.05, 0.25, 0.50, 0.75, 0.95])
    net = float(automated["net_usd"].sum())
    return {
        "status": "AVAILABLE",
        "interpretation": "historical_resampling_not_forecast",
        "method": (
            "Deterministic 7-calendar-day moving-block bootstrap of broker-native daily net USD "
            "during the observed automated active span, resampled into 30-day windows."
        ),
        "important_limitations": [
            "The sample is short and strategy behavior, risk, spreads, and market regimes can change.",
            "Zero-trade days inside the active span are included; inactivity before the first automated close is not.",
            "The interval describes variation in this historical sample and is not a confidence interval for future profit.",
        ],
        "trades": len(automated),
        "first_close": first_close.isoformat(),
        "last_close": last_close.isoformat(),
        "active_span_days": _finite(active_days, 3),
        "observed_net_usd": _finite(net, 2),
        "observed_active_span_monthly_rate_usd": _finite(
            net / active_days * MONTHLY_CALENDAR_DAYS, 2
        ),
        "observed_full_export_span_monthly_rate_usd": _finite(
            net / export_days * MONTHLY_CALENDAR_DAYS, 2
        ),
        "bootstrap": {
            "samples": MONTHLY_BOOTSTRAP_SAMPLES,
            "block_days": block_size,
            "window_days": 30,
            "seed": MONTHLY_BOOTSTRAP_SEED,
            "p05_usd": _finite(quantiles[0], 2),
            "p25_usd": _finite(quantiles[1], 2),
            "median_usd": _finite(quantiles[2], 2),
            "p75_usd": _finite(quantiles[3], 2),
            "p95_usd": _finite(quantiles[4], 2),
            "positive_share": _finite(float((samples > 0).mean()), 6),
        },
        "observed_rolling_30_day": {
            "windows": len(rolling_30),
            "minimum_usd": _finite(rolling_30.min(), 2) if not rolling_30.empty else None,
            "median_usd": _finite(rolling_30.median(), 2) if not rolling_30.empty else None,
            "maximum_usd": _finite(rolling_30.max(), 2) if not rolling_30.empty else None,
        },
    }


def analyze_trade_ledger(bundle: Path) -> dict[str, Any]:
    path = bundle / "trade_history.csv"
    frame = pd.read_csv(path)
    required = {"symbol", "strategy_magic", "close_time", "costs_usd", "net_usd"}
    if not required.issubset(frame.columns):
        raise ValueError(f"trade ledger is missing columns: {sorted(required - set(frame.columns))}")
    frame["strategy_magic"] = pd.to_numeric(frame["strategy_magic"], errors="coerce").fillna(0).astype(int)
    frame["close_time"] = pd.to_datetime(frame["close_time"], errors="coerce")
    frame = frame.dropna(subset=["close_time"]).sort_values("close_time")
    latest = frame["close_time"].max() if not frame.empty else None
    recent = frame[frame["close_time"] >= latest - pd.Timedelta(days=90)] if latest is not None else frame
    by_symbol = []
    for symbol, group in frame.groupby("symbol", sort=True):
        by_symbol.append({"symbol": str(symbol), **_ledger_metrics(group)})
    by_symbol.sort(key=lambda item: (item["net_usd"], item["symbol"]), reverse=True)
    by_magic = []
    for magic, group in frame[frame["strategy_magic"] != 0].groupby("strategy_magic", sort=True):
        by_magic.append({"strategy_magic": int(magic), **_ledger_metrics(group)})
    by_magic.sort(key=lambda item: (item["net_usd"], item["strategy_magic"]), reverse=True)
    return {
        "canonical_performance_column": "net_usd",
        "cfd_pnl_recomputed": False,
        "time_basis": "broker_server_wall_clock",
        "all": _ledger_metrics(frame),
        "automated_magic_nonzero": _ledger_metrics(frame[frame["strategy_magic"] != 0]),
        "manual_or_untagged_magic_zero": _ledger_metrics(frame[frame["strategy_magic"] == 0]),
        "recent_90_days_from_latest_close": _ledger_metrics(recent),
        "reported_costs_usd": _finite(pd.to_numeric(frame["costs_usd"], errors="coerce").sum(), 2),
        "by_symbol": by_symbol,
        "by_automated_magic": by_magic,
        "automated_monthly_history": historical_automated_monthly_range(frame),
    }


def load_structural_validation(path: Path | None, *, now: datetime) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"available": False, "paper_candidates": [], "reason": "report_not_supplied"}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("mode") != "read_only_research" or payload.get("orders_sent") != 0:
        raise ValueError("structural validation report crossed the research boundary")
    candidates = payload.get("paper_candidates")
    if not isinstance(candidates, list):
        raise ValueError("structural validation paper_candidates is invalid")
    summaries = []
    for candidate in candidates:
        if candidate.get("live_eligible") is not False or candidate.get("paper_candidate") is not True:
            raise ValueError("structural validation candidate has invalid promotion flags")
        oos = candidate.get("oos", {})
        summaries.append(
            {
                "spec_id": candidate.get("spec_id"),
                "symbol": candidate.get("symbol"),
                "entry_weekday": candidate.get("entry_weekday"),
                "entry_hour": candidate.get("entry_hour"),
                "exit_weekday": candidate.get("exit_weekday"),
                "exit_hour": candidate.get("exit_hour"),
                "direction": oos.get("direction"),
                "oos_trades": oos.get("trades"),
                "oos_net_minimum_lot_usd": oos.get("net"),
                "oos_profit_factor": oos.get("profit_factor"),
                "bootstrap_mean_lcb_95": oos.get("bootstrap_mean_lcb_95"),
                "p_bonferroni": candidate.get("multiple_testing", {}).get("p_bonferroni"),
                "stage": "PAPER_CANDIDATE",
                "live_eligible": False,
            }
        )
    generated = payload.get("generated_at_host_utc")
    age_days = None
    if generated:
        stamp = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        age_days = max((now - stamp.astimezone(timezone.utc)).total_seconds(), 0) / 86400.0
    return {
        "available": True,
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
        "generated_at": generated,
        "age_days": _finite(age_days, 3),
        "stale": age_days is None or age_days > 14,
        "family_trials": payload.get("family_trials"),
        "paper_candidate_count": len(summaries),
        "paper_candidates": summaries,
        "automatic_live_promotion": False,
    }


def _instrument_map(bundle: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads((bundle / "instrument_snapshots.json").read_text(encoding="utf-8-sig"))
    instruments = payload.get("instruments", [])
    return {
        str(item["symbol"]): item
        for item in instruments
        if isinstance(item, dict) and item.get("symbol")
    }


def _candidate_cost(instrument: Mapping[str, Any] | None) -> dict[str, Any]:
    if not instrument:
        return {
            "basis": "Instrument snapshot unavailable; reject until a cost model is supplied.",
            "spread_multiplier": 2.0,
            "slippage_points_round_trip": 6.0,
            "minimum_lot_reference": None,
            "estimated_cost_usd_min_lot": None,
        }
    spread = max(float(instrument.get("spread") or 0), 0.0)
    point = float(instrument.get("point") or 0)
    tick_size = float(instrument.get("trade_tick_size") or 0)
    tick_value = float(instrument.get("trade_tick_value") or 0)
    minimum_lot = float(instrument.get("volume_min") or 0)
    stress_points = spread * 2.0 + 6.0
    estimated = None
    if point > 0 and tick_size > 0 and tick_value > 0 and minimum_lot > 0:
        estimated = stress_points * point / tick_size * tick_value * minimum_lot
    return {
        "basis": (
            "Research stress only: two times the current terminal spread snapshot plus "
            "6 points round-trip slippage. The snapshot is not historical spread evidence."
        ),
        "spread_multiplier": 2.0,
        "slippage_points_round_trip": 6.0,
        "minimum_lot_reference": _finite(minimum_lot, 6) if minimum_lot > 0 else None,
        "estimated_cost_usd_min_lot": _finite(estimated, 6),
    }


def _candidate_template(
    analysis: Mapping[str, Any],
    *,
    family: str,
    direction: str,
    score: float,
    instrument: Mapping[str, Any] | None,
) -> dict[str, Any]:
    symbol = str(analysis["broker_symbol"])
    source_symbol = str(analysis["source_symbol"])
    if family == "trend_following":
        comparison = ">" if direction == "long" else "<"
        momentum = "> 0" if direction == "long" else "< 0"
        entry = (
            f"At completed broker-wall H1 bar t, signal for bar t+1 only when EMA(20) {comparison} "
            f"EMA(100), 24-bar return {momentum}, and the volatility ratio is below 1.5."
        )
        exit_rule = "Exit at the first of 12 completed H1 bars, an opposite EMA(20/100) cross, or the stop."
        stop_rule = "For validation only, test an initial stop 1.5 times ATR(14) from the next-bar executable entry."
        failure = "Sideways or abruptly reversing markets with high spread and volatility expansion."
    elif family == "breakout":
        comparison = "above" if direction == "long" else "below"
        boundary = "high" if direction == "long" else "low"
        entry = (
            f"At completed broker-wall H1 bar t, signal for bar t+1 only if close[t] is {comparison} "
            f"the prior 120-bar {boundary} computed strictly through t-1 and 24-bar momentum agrees."
        )
        exit_rule = "Exit after 8 completed H1 bars, on a close back inside the prior range, or at the stop."
        stop_rule = "For validation only, test an initial stop 1.25 times ATR(14) from the next-bar executable entry."
        failure = "False breaks during thin liquidity, spread spikes, or immediate range re-entry."
    elif family == "range_reversion":
        threshold = "<= -2" if direction == "long" else ">= 2"
        entry = (
            f"At completed broker-wall H1 bar t, signal for bar t+1 only when the 20-bar close z-score is {threshold} "
            "and absolute EMA(20)-EMA(100) is below 0.75 ATR(14)."
        )
        exit_rule = "Exit on a completed-bar z-score crossing zero, after 10 H1 bars, or at the stop."
        stop_rule = "For validation only, test an initial stop 1.25 times ATR(14) beyond the next-bar executable entry."
        failure = "Persistent trends where an apparent range dislocates rather than reverts."
    else:
        entry = (
            "At completed broker-wall H1 bar t, classify high volatility only when 24-bar realized "
            "volatility is at least 1.5 times its trailing 500-bar median. For bar t+1, test a long "
            "only above the prior 24-bar high or a short only below the prior 24-bar low; grade both "
            "directions as separate hypotheses."
        )
        exit_rule = "Exit after 6 completed H1 bars, on a close back inside the prior 24-bar range, or at the stop."
        stop_rule = "For validation only, test an initial stop 1.0 times ATR(14) from the next-bar executable entry."
        failure = "Volatility spikes that reverse immediately, spread expansion, or unstable direction selection across folds."
    digest = hashlib.sha256(f"{source_symbol}|H1|{family}|{direction}|{entry}".encode()).hexdigest()
    rationale = (
        f"Current descriptive ranking only: trend={analysis['trend_regime']}, "
        f"volatility={analysis['volatility_regime']}, momentum120={analysis['momentum_120_pct']}, "
        f"trend_strength_atr={analysis['ema20_minus_ema100_atr']}, zscore20={analysis['zscore_20']}. "
        "These observations are not evidence of future return."
    )
    return {
        "candidate_id": f"VT-{digest[:12].upper()}",
        "stage": "DISCOVERED",
        "source_symbols": [source_symbol],
        "broker_symbols": [symbol],
        "timeframe": "H1",
        "family": family,
        "direction": direction,
        "session": (
            "All available broker-server wall-clock H1 sessions. Any session filter must be "
            "declared before testing and counted in the multiple-testing family."
        ),
        "entry_rule": entry,
        "exit_rule": exit_rule,
        "stop_rule": stop_rule,
        "cost_stress": _candidate_cost(instrument),
        "rationale": rationale,
        "expected_frequency": "Unknown until cost-aware chronological validation; do not infer it from the current observation.",
        "failure_regime": failure,
        "lookahead_safeguards": [
            "Compute every indicator through completed bar t only.",
            "Use the first executable quote after bar t for entry and include spread and slippage.",
            "Select parameters on training folds only and purge the fold boundary.",
        ],
        "validation_required": list(REQUIRED_VALIDATION_GATES),
        "priority_score": round(min(max(score, 0.0), 100.0), 3),
        "live_eligible": False,
    }


def build_candidate_handoff(
    *,
    analyses: list[dict[str, Any]],
    instruments: Mapping[str, Mapping[str, Any]],
    correlation_report: Mapping[str, Any],
    generated_at: datetime,
    manifest_sha256: str,
    maximum_candidates: int,
) -> dict[str, Any]:
    options: list[dict[str, Any]] = []
    for item in analyses:
        if item["data_quality"]["status"] != "PASS":
            continue
        trend_strength = abs(float(item.get("ema20_minus_ema100_atr") or 0.0))
        momentum120 = float(item.get("momentum_120_pct") or 0.0)
        momentum24 = float(item.get("momentum_24_pct") or 0.0)
        zscore20 = float(item.get("zscore_20") or 0.0)
        range_position = item.get("range_position_120")
        broker_symbol = str(item["broker_symbol"])
        instrument = instruments.get(broker_symbol)
        if (
            item["trend_regime"] in {"up", "down"}
            and item["volatility_regime"] != "high"
            and trend_strength >= 0.5
        ):
            direction = "long" if item["trend_regime"] == "up" else "short"
            score = 45 + min(trend_strength * 12, 30) + min(abs(momentum120), 20)
            options.append(
                _candidate_template(
                    item,
                    family="trend_following",
                    direction=direction,
                    score=score,
                    instrument=instrument,
                )
            )
        if range_position is not None:
            if float(range_position) >= 0.97 and momentum24 > 0:
                direction = "long"
            elif float(range_position) <= 0.03 and momentum24 < 0:
                direction = "short"
            else:
                direction = ""
            if direction:
                score = 40 + min(abs(momentum24) * 5, 30) + min(trend_strength * 5, 15)
                options.append(
                    _candidate_template(
                        item,
                        family="breakout",
                        direction=direction,
                        score=score,
                        instrument=instrument,
                    )
                )
        if item["trend_regime"] == "mixed" and abs(zscore20) >= 1.5:
            direction = "long" if zscore20 < 0 else "short"
            score = 40 + min(abs(zscore20) * 12, 35)
            options.append(
                _candidate_template(
                    item,
                    family="range_reversion",
                    direction=direction,
                    score=score,
                    instrument=instrument,
                )
            )
        if item["volatility_regime"] == "high":
            volatility_ratio = float(item.get("volatility_ratio_vs_recent_median") or 0.0)
            score = 42 + min(max(volatility_ratio - 1.5, 0.0) * 20, 25) + min(abs(momentum24) * 3, 15)
            options.append(
                _candidate_template(
                    item,
                    family="volatility_regime",
                    direction="both",
                    score=score,
                    instrument=instrument,
                )
            )

    options.sort(key=lambda candidate: (-candidate["priority_score"], candidate["candidate_id"]))
    correlation_by_pair: dict[frozenset[str], float] = {}
    matrix = correlation_report.get("matrix", {})
    for left, row in matrix.items():
        for right, value in row.items():
            if left != right and value is not None:
                correlation_by_pair[frozenset({left, right})] = abs(float(value))
    selected: list[dict[str, Any]] = []
    family_counts: dict[str, int] = {}
    selected_symbols: set[str] = set()
    for candidate in options:
        symbol = candidate["broker_symbols"][0]
        family = candidate["family"]
        if symbol in selected_symbols or family_counts.get(family, 0) >= 3:
            continue
        too_correlated = any(
            correlation_by_pair.get(frozenset({symbol, existing["broker_symbols"][0]}), 0.0)
            >= CORRELATION_CLUSTER_THRESHOLD
            and existing["family"] == family
            for existing in selected
        )
        if too_correlated:
            continue
        selected.append(candidate)
        selected_symbols.add(symbol)
        family_counts[family] = family_counts.get(family, 0) + 1
        if len(selected) >= maximum_candidates:
            break

    broker_by_source = {item["source_symbol"]: item["broker_symbol"] for item in analyses}
    payload = {
        "schema": HANDOFF_SCHEMA,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bundle_manifest_sha256": manifest_sha256,
        "research_only": True,
        "order_authority": False,
        "automatic_live_promotion": False,
        "source": {
            "kind": "vibe_deterministic_baseline",
            "vibe_commit": AUDITED_VIBE_COMMIT,
        },
        "summary": (
            f"Ranked {len(selected)} diverse rules for validation from current descriptive regimes. "
            "Priority scores are triage ranks, not probabilities or evidence of profitability."
        ),
        "candidates": selected,
    }
    return validate_candidate_handoff(
        payload,
        allowed_symbols=set(broker_by_source),
        broker_by_source=broker_by_source,
        expected_manifest_sha256=manifest_sha256,
        maximum_candidates=maximum_candidates,
    )


def write_chart(
    path: Path,
    analyses: list[dict[str, Any]],
    frames: Mapping[str, pd.DataFrame],
    correlation: Mapping[str, Any],
    monthly_history: Mapping[str, Any],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    source_to_broker = {item["source_symbol"]: item["broker_symbol"] for item in analyses}
    names = [item["broker_symbol"] for item in analyses]
    matrix = np.array(
        [[correlation["matrix"][left][right] for right in names] for left in names],
        dtype=float,
    )
    figure, axes = plt.subplots(
        3,
        1,
        figsize=(14, 14),
        constrained_layout=True,
        gridspec_kw={"height_ratios": [1.25, 1.0, 0.42]},
    )
    colors = plt.get_cmap("tab20")(np.linspace(0, 1, max(len(frames), 1)))
    for color, (source, frame) in zip(colors, frames.items()):
        close = pd.to_numeric(frame["close"], errors="coerce").dropna().tail(240)
        if close.empty or float(close.iloc[0]) == 0:
            continue
        normalized = close / float(close.iloc[0]) * 100.0
        normalized.loc[normalized.index.to_series().diff() > pd.Timedelta(hours=2)] = np.nan
        axes[0].plot(
            normalized.index,
            normalized,
            color=color,
            linewidth=1.2,
            label=source_to_broker[source],
        )
    axes[0].set_title("Recent normalized H1 price paths (research view, not a forecast)")
    axes[0].set_ylabel("First displayed close = 100")
    axes[0].grid(alpha=0.25)
    axes[0].legend(ncol=4, fontsize=8, loc="upper left")
    locator = mdates.AutoDateLocator(minticks=5, maxticks=8)
    axes[0].xaxis.set_major_locator(locator)
    axes[0].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    image = axes[1].imshow(matrix, vmin=-1, vmax=1, cmap="coolwarm", aspect="auto")
    axes[1].set_title("Aligned H1 return correlation")
    axes[1].set_xticks(range(len(names)), names, rotation=45, ha="right", fontsize=8)
    axes[1].set_yticks(range(len(names)), names, fontsize=8)
    figure.colorbar(image, ax=axes[1], fraction=0.025, pad=0.02)

    axes[2].axvline(0, color="#202020", linewidth=1, alpha=0.8)
    axes[2].set_title("Historical automated 30-day P/L resampling (not a forecast)")
    axes[2].set_xlabel("Net USD at observed historical sizing")
    axes[2].set_yticks([])
    axes[2].grid(axis="x", alpha=0.25)
    if monthly_history.get("status") == "AVAILABLE":
        bootstrap = monthly_history["bootstrap"]
        p05 = float(bootstrap["p05_usd"])
        p25 = float(bootstrap["p25_usd"])
        median = float(bootstrap["median_usd"])
        p75 = float(bootstrap["p75_usd"])
        p95 = float(bootstrap["p95_usd"])
        axes[2].hlines(0, p05, p95, color="#55706d", linewidth=5, label="5th-95th percentile")
        axes[2].hlines(0, p25, p75, color="#009c82", linewidth=13, label="25th-75th percentile")
        axes[2].scatter([median], [0], color="#d64f3f", s=75, zorder=3, label="median")
        axes[2].legend(loc="upper left", ncol=3, fontsize=8, frameon=False)
    else:
        axes[2].text(0.5, 0.5, "No automated trade sample", ha="center", va="center", transform=axes[2].transAxes)
    figure.suptitle("Vibe Trading deterministic baseline", fontsize=16)
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _format_metric(value: Any) -> str:
    return "n/a" if value is None else str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Vibe Trading Deterministic Baseline",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "This is a research report, not a forecast or live-trading instruction. Vibe has no order authority.",
        "",
        "## Data and Regimes",
        "",
        "| Instrument | Data | Trend | Volatility | 24h mom % | 120h mom % | ATR14 % |",
        "| --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for item in report["instruments"]:
        lines.append(
            "| {broker_symbol} | {quality} | {trend_regime} | {volatility_regime} | {m24} | {m120} | {atr} |".format(
                broker_symbol=item["broker_symbol"],
                quality=item["data_quality"]["status"],
                trend_regime=item["trend_regime"],
                volatility_regime=item["volatility_regime"],
                m24=_format_metric(item["momentum_24_pct"]),
                m120=_format_metric(item["momentum_120_pct"]),
                atr=_format_metric(item["atr14_pct"]),
            )
        )
    ledger = report["trade_ledger"]
    lines.extend(
        [
            "",
            "## Broker Ledger",
            "",
            f"- All exported closed trades: `{ledger['all']['trades']}`; net `{ledger['all']['net_usd']} USD`.",
            f"- Automated nonzero magic: `{ledger['automated_magic_nonzero']['trades']}`; net `{ledger['automated_magic_nonzero']['net_usd']} USD`.",
            f"- Manual or untagged magic zero: `{ledger['manual_or_untagged_magic_zero']['trades']}`; net `{ledger['manual_or_untagged_magic_zero']['net_usd']} USD`.",
            "- Performance uses broker-exported `net_usd`; CFD P/L was not recomputed from price and quantity.",
            "",
            "## Historical Monthly Range (Not a Forecast)",
            "",
        ]
    )
    monthly = ledger["automated_monthly_history"]
    if monthly["status"] == "AVAILABLE":
        bootstrap = monthly["bootstrap"]
        lines.extend(
            [
                f"- Observed automated active-span rate: `{monthly['observed_active_span_monthly_rate_usd']} USD/month`.",
                f"- Historical 30-day block-resampling range (5th to 95th percentile): `{bootstrap['p05_usd']} to {bootstrap['p95_usd']} USD`; median `{bootstrap['median_usd']} USD`.",
                f"- Positive resampled windows: `{bootstrap['positive_share']}`. This is historical variation, not an expected return or promise.",
            ]
        )
    else:
        lines.append(f"- Unavailable: {monthly['reason']}.")
    lines.extend(["", "## Correlation Concentration", ""])
    clusters = report["cross_asset_risk"]["clusters"]
    if clusters:
        lines.extend(f"- `{', '.join(cluster)}`" for cluster in clusters)
    else:
        lines.append("- No cluster crossed the configured absolute-correlation threshold.")
    structural = report["structural_validation"]
    lines.extend(["", "## Independent Local Validation", ""])
    if structural.get("available"):
        lines.append(
            f"The latest strict structural scan contains `{structural['paper_candidate_count']}` paper candidates; none is live-eligible."
        )
        for item in structural["paper_candidates"]:
            lines.append(
                f"- `{item['spec_id']}` {item['direction']}: {item['oos_trades']} OOS trades, "
                f"net `{item['oos_net_minimum_lot_usd']} USD` at minimum lot, PF `{item['oos_profit_factor']}`."
            )
    else:
        lines.append("No separate structural validation report was supplied.")
    handoff = report["candidate_handoff"]
    lines.extend(
        [
            "",
            "## Research Queue",
            "",
            f"`{len(handoff['candidates'])}` hypotheses were ranked as `DISCOVERED`. Priority is not a win probability.",
        ]
    )
    for item in handoff["candidates"]:
        lines.append(
            f"- `{item['candidate_id']}` {item['broker_symbols'][0]} {item['family']} {item['direction']} "
            f"(priority `{item['priority_score']}`): {item['entry_rule']}"
        )
    lines.extend(
        [
            "",
            "Every item still requires cost-aware backtesting, purged walk-forward validation, multiple-testing control, paper-forward evidence, and separate manual live authorization.",
            "",
        ]
    )
    return "\n".join(lines)


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--structural-report", type=Path)
    parser.add_argument("--minimum-rows", type=int, default=DEFAULT_MIN_ROWS)
    parser.add_argument("--maximum-candidates", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = args.bundle.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir == bundle or _within(output_dir, bundle):
        raise ValueError("research output must not modify the immutable export bundle")
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest, manifest_sha256 = verify_bundle(bundle)
    frames, loader_provenance = load_vibe_frames(bundle, manifest)
    bar_records = [record for record in manifest["files"] if record.get("source_symbol")]
    analyses = [
        analyze_frame(
            bundle=bundle,
            record=record,
            frame=frames[str(record["source_symbol"])],
            minimum_rows=max(int(args.minimum_rows), 100),
        )
        for record in bar_records
    ]
    correlation = build_correlation_report(analyses, frames)
    generated_at = datetime.now(tz=timezone.utc)
    instruments = _instrument_map(bundle)
    handoff = build_candidate_handoff(
        analyses=analyses,
        instruments=instruments,
        correlation_report=correlation,
        generated_at=generated_at,
        manifest_sha256=manifest_sha256,
        maximum_candidates=min(max(int(args.maximum_candidates), 0), 10),
    )
    account_snapshot = json.loads((bundle / "account_snapshot.redacted.json").read_text(encoding="utf-8-sig"))
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "mode": "research_only",
        "order_authority": False,
        "automatic_live_promotion": False,
        "forecast_generated": False,
        "bundle": {
            "path": str(bundle),
            "manifest_sha256": manifest_sha256,
            "generated_at": manifest.get("generated_at"),
            "feed_clock": manifest.get("source", {}).get("feed_clock_sample"),
            "market_time_warning": manifest.get("source", {}).get("market_time_warning"),
        },
        "vibe_loader": loader_provenance,
        "account_snapshot_redacted": account_snapshot,
        "data_quality_status": (
            "PASS" if all(item["data_quality"]["status"] == "PASS" for item in analyses) else "FAIL"
        ),
        "instruments": analyses,
        "cross_asset_risk": correlation,
        "trade_ledger": analyze_trade_ledger(bundle),
        "structural_validation": load_structural_validation(args.structural_report, now=generated_at),
        "candidate_handoff": handoff,
        "limitations": [
            "Recent regime statistics describe completed bars and do not predict future returns.",
            "Current spread snapshots are not a historical spread series.",
            "Aligned broker-wall H1 correlation is descriptive and can change abruptly.",
            "DISCOVERED hypotheses have no execution or live-promotion authority.",
        ],
    }
    if report["data_quality_status"] != "PASS":
        raise ValueError("one or more Vibe-loaded sources failed the data quality gate")

    report_path = output_dir / "baseline.json"
    handoff_path = output_dir / "candidate-handoff.json"
    markdown_path = output_dir / "baseline.md"
    chart_path = output_dir / "baseline.png"
    write_json(handoff_path, handoff)
    write_json(report_path, report)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    write_chart(
        chart_path,
        analyses,
        frames,
        correlation,
        report["trade_ledger"]["automated_monthly_history"],
    )
    pointer = {
        "schema": "mt5.vibe_baseline_pointer.v1",
        "generated_at": report["generated_at"],
        "report_dir": str(output_dir),
        "report": str(report_path),
        "report_sha256": file_sha256(report_path),
        "handoff": str(handoff_path),
        "handoff_sha256": file_sha256(handoff_path),
        "chart": str(chart_path),
        "candidate_count": len(handoff["candidates"]),
        "symbols_loaded": loader_provenance["symbols_loaded"],
        "order_authority": False,
    }
    pointer_path = output_dir.parent / "latest.json"
    temp_pointer = pointer_path.with_suffix(".tmp")
    write_json(temp_pointer, pointer)
    os.replace(temp_pointer, pointer_path)
    print(json.dumps(pointer, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
