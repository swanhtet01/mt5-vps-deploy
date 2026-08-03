"""Sanitized, one-way export helpers for the Vibe Trading research sidecar.

This module has no order or account-login capability. It only serializes completed
bars, redacted account telemetry, and MT5-native closed-trade P/L for offline research.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from mt5_agent.models import Bar
from mt5_agent.trade_history import ClosedTrade


AUDITED_VIBE_REPOSITORY = "https://github.com/HKUDS/Vibe-Trading"
AUDITED_VIBE_COMMIT = "652917e74e2b2e1f767ef596623bae7f098a53c4"
AUDITED_VIBE_VERSION = "0.1.13"
BUNDLE_SCHEMA = "mt5.vibe_research_bundle.v1"


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def broker_wall_iso(value: datetime) -> str:
    """Render MT5's broker-server wall clock without falsely claiming it is UTC."""
    return value.replace(tzinfo=None).isoformat(timespec="seconds")


def safe_source_symbol(symbol: str, timeframe: str) -> str:
    """Return a stable Vibe local-loader key without path or YAML metacharacters."""
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", f"XM_{symbol}_{timeframe}")
    return normalized.strip("_").upper()


def sanitized_account_snapshot(account_info: object, *, captured_at: datetime) -> dict:
    """Keep risk telemetry while omitting identity, login, server, and credentials."""
    keys = (
        "balance",
        "equity",
        "margin",
        "margin_free",
        "margin_level",
        "profit",
        "credit",
        "leverage",
        "currency",
        "trade_mode",
        "trade_allowed",
        "trade_expert",
    )
    payload = {
        key: getattr(account_info, key)
        for key in keys
        if getattr(account_info, key, None) is not None
    }
    return {
        "schema": "mt5.redacted_account_snapshot.v1",
        "captured_at": utc_iso(captured_at),
        "identity_redacted": True,
        **payload,
    }


def sanitized_instrument_snapshot(symbol: str, symbol_info: object) -> dict:
    """Whitelist execution metadata needed to interpret an MT5 CFD lot."""
    keys = (
        "point",
        "digits",
        "spread",
        "trade_tick_size",
        "trade_tick_value",
        "trade_contract_size",
        "volume_min",
        "volume_max",
        "volume_step",
        "swap_mode",
        "swap_long",
        "swap_short",
        "currency_base",
        "currency_profit",
        "currency_margin",
    )
    values = {
        key: getattr(symbol_info, key)
        for key in keys
        if getattr(symbol_info, key, None) is not None
    }
    return {
        "schema": "mt5.instrument_snapshot.v1",
        "symbol": symbol,
        "spread_basis": "current_terminal_snapshot_not_historical_cost_series",
        **values,
    }


def write_bar_csv(path: Path, bars: Iterable[Bar]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    first: str | None = None
    last: str | None = None
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("date", "open", "high", "low", "close", "volume"))
        for bar in bars:
            timestamp = broker_wall_iso(bar.time)
            first = first or timestamp
            last = timestamp
            writer.writerow(
                (
                    timestamp,
                    float(bar.open),
                    float(bar.high),
                    float(bar.low),
                    float(bar.close),
                    int(bar.tick_volume),
                )
            )
            count += 1
    return {"rows": count, "first": first, "last": last}


def write_trade_csv(path: Path, trades: Sequence[ClosedTrade]) -> dict:
    """Write broker-native P/L without exposing account or MT5 ticket identifiers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(trades, key=lambda trade: (trade.close_time, trade.position_id))
    net = 0.0
    with path.open("w", newline="", encoding="utf-8") as handle:
        fields = (
            "trade_ref",
            "symbol",
            "strategy_magic",
            "open_time",
            "close_time",
            "volume_lots",
            "gross_usd",
            "costs_usd",
            "net_usd",
            "deal_legs",
        )
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for index, trade in enumerate(ordered, start=1):
            costs = float(trade.net) - float(trade.gross)
            net += float(trade.net)
            writer.writerow(
                {
                    "trade_ref": f"T{index:06d}",
                    "symbol": trade.symbol,
                    "strategy_magic": trade.magic,
                    "open_time": broker_wall_iso(trade.open_time),
                    "close_time": broker_wall_iso(trade.close_time),
                    "volume_lots": float(trade.volume),
                    "gross_usd": round(float(trade.gross), 8),
                    "costs_usd": round(costs, 8),
                    "net_usd": round(float(trade.net), 8),
                    "deal_legs": trade.legs,
                }
            )
    return {"rows": len(ordered), "net_usd": round(net, 8)}


def write_data_bridge_config(path: Path, sources: Sequence[Mapping[str, str]]) -> None:
    """Write dependency-free YAML accepted by Vibe Trading's local data loader."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["sources:"]
    for source in sources:
        lines.extend(
            [
                f"  - symbol: {json.dumps(source['symbol'])}",
                "    type: csv",
                f"    path: {json.dumps(str(Path(source['path']).resolve()))}",
                "    columns:",
                '      date: "date"',
                '      open: "open"',
                '      high: "high"',
                '      low: "low"',
                '      close: "close"',
                '      volume: "volume"',
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, root: Path, metadata: Mapping | None = None) -> dict:
    record = {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": file_sha256(path),
    }
    if metadata:
        record.update(dict(metadata))
    return record


def write_json(path: Path, payload: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def manifest_payload(
    *,
    generated_at: datetime,
    files: Sequence[Mapping],
    symbols: Sequence[str],
    timeframe: str,
    history_days: int,
    feed_clock: Mapping,
) -> dict:
    return {
        "schema": BUNDLE_SCHEMA,
        "generated_at": utc_iso(generated_at),
        "mode": "research_only",
        "contains_credentials": False,
        "order_authority": False,
        "automatic_live_promotion": False,
        "source": {
            "platform": "MetaTrader5",
            "bars": "completed_bars_only",
            "trade_pnl": "broker_deal_ledger_net_of_reported_costs",
            "market_time_encoding": "broker_server_wall_clock_as_naive_iso8601",
            "market_time_warning": (
                "Vibe will parse this wall clock as UTC internally; preserve the displayed "
                "weekday/hour as the broker schedule and do not align it to external UTC "
                "events without an independently verified historical offset map."
            ),
            "feed_clock_sample": dict(feed_clock),
        },
        "vibe_trading": {
            "repository": AUDITED_VIBE_REPOSITORY,
            "audited_commit": AUDITED_VIBE_COMMIT,
            "audited_version": AUDITED_VIBE_VERSION,
            "role": "hypothesis_generation_and_reporting_only",
        },
        "research_scope": {
            "symbols": list(symbols),
            "timeframe": timeframe,
            "closed_trade_history_days": int(history_days),
        },
        "performance_contract": {
            "canonical_column": "net_usd",
            "instruction": "Do not recompute CFD P/L as price multiplied by quantity.",
        },
        "promotion_contract": {
            "initial_stage": "DISCOVERED",
            "required_local_gates": [
                "cost_aware_backtest",
                "purged_walk_forward",
                "multiple_testing_control",
                "paper_forward_minimum",
                "manual_live_authorization",
            ],
        },
        "files": list(files),
    }
