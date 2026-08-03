"""Export a sanitized MT5 research bundle for the isolated Vibe Trading sidecar."""
from __future__ import annotations

import argparse
import json
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mt5_agent.broker import MT5Broker
from mt5_agent.config import load_config
from mt5_agent.mt5_execution import (
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)
from mt5_agent.trade_history import fetch_closed_trades
from mt5_agent.vibe_bridge import (
    file_record,
    manifest_payload,
    safe_source_symbol,
    sanitized_account_snapshot,
    sanitized_instrument_snapshot,
    write_bar_csv,
    write_data_bridge_config,
    write_json,
    write_trade_csv,
)


RESEARCH_README = """# Research-only MT5 bundle

This directory is a one-way, credential-free input to Vibe Trading.

- Use only completed OHLCV bars from `market_data/`.
- Use `trade_history.csv.net_usd` as canonical realized P/L. MT5 CFD lots are not
  share quantities, so do not recompute P/L as price multiplied by quantity.
- Treat every generated strategy as `DISCOVERED`, never live-ready.
- Do not add broker credentials or enable Vibe's MT5 trading connector.
- Promotion requires this repository's cost-aware, purged walk-forward,
  multiple-testing, paper-forward, and explicit live-authorization gates.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path("config.research-multi-asset-h1.toml")
    )
    parser.add_argument("--out-root", type=Path, default=Path("research/vibe_exports"))
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--bars", type=int, default=5000)
    parser.add_argument("--history-days", type=int, default=365)
    parser.add_argument("--max-last-bar-age-hours", type=float, default=80.0)
    return parser.parse_args()


def coherent_reference_clock(mt5, symbols: list[str], now: datetime):
    return coherent_feed_clock_from_mt5(
        mt5,
        [*symbols, "BTCUSD", "GOLD", "USDJPY"],
        host_utc=now,
    )


def fresh_completed_bars(
    broker: MT5Broker,
    symbol: str,
    timeframe: str,
    count: int,
    feed_time: datetime,
    max_age_hours: float,
    *,
    attempts: int = 5,
    retry_delay_seconds: float = 2.0,
):
    """Retry while MT5 asynchronously refreshes a newly selected history cache."""
    last_reason = "broker returned no completed bars"
    for attempt in range(max(int(attempts), 1)):
        bars = broker.bars(symbol, timeframe, count)
        if bars:
            age_hours = (feed_time - bars[-1].time).total_seconds() / 3600.0
            if -1.0 <= age_hours <= max_age_hours:
                return bars, age_hours
            last_reason = (
                f"last completed bar age {age_hours:.1f}h is outside "
                f"[-1h, {max_age_hours:.1f}h]"
            )
        if attempt + 1 < max(int(attempts), 1):
            broker.mt5.symbol_info_tick(symbol)
            time.sleep(max(float(retry_delay_seconds), 0.0))
    raise RuntimeError(last_reason)


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    symbols = list(dict.fromkeys(args.symbols or config.symbols))
    if not symbols:
        raise SystemExit("No symbols configured for research export")
    if args.bars < 100 or args.history_days < 1:
        raise SystemExit("--bars must be >= 100 and --history-days must be >= 1")

    now = datetime.now(tz=timezone.utc)
    bundle_name = now.strftime("%Y%m%dT%H%M%SZ")
    root = args.out_root.resolve()
    staging = root / f".{bundle_name}-{uuid.uuid4().hex}.staging"
    final = root / bundle_name
    broker = MT5Broker()
    try:
        staging.mkdir(parents=True, exist_ok=False)
        reference_symbol, feed_clock = coherent_reference_clock(broker.mt5, symbols, now)
        sources: list[dict[str, str]] = []
        bar_records: list[dict] = []
        instrument_snapshots: list[dict] = []
        export_errors: list[dict[str, str]] = []
        for symbol in symbols:
            try:
                bars, age_hours = fresh_completed_bars(
                    broker,
                    symbol,
                    args.timeframe,
                    args.bars,
                    feed_clock.feed_time,
                    args.max_last_bar_age_hours,
                )
                path = staging / "market_data" / f"{symbol}_{args.timeframe}.csv"
                metadata = write_bar_csv(path, bars)
                metadata["last_bar_age_hours"] = round(age_hours, 3)
                source_symbol = safe_source_symbol(symbol, args.timeframe)
                sources.append(
                    {
                        "symbol": source_symbol,
                        "path": str(final / path.relative_to(staging)),
                    }
                )
                symbol_info = broker.mt5.symbol_info(symbol)
                if symbol_info is None:
                    raise RuntimeError("symbol metadata became unavailable")
                instrument_snapshots.append(
                    sanitized_instrument_snapshot(symbol, symbol_info)
                )
                bar_records.append(
                    file_record(
                        path,
                        staging,
                        {
                            **metadata,
                            "broker_symbol": symbol,
                            "source_symbol": source_symbol,
                            "timeframe": args.timeframe,
                        },
                    )
                )
            except Exception as exc:
                export_errors.append({"symbol": symbol, "error": str(exc)})

        if not sources:
            raise RuntimeError("No symbol produced a usable completed-bar export")

        account_info = broker.mt5.account_info()
        if account_info is None:
            raise RuntimeError(f"account_info failed: {broker.mt5.last_error()}")
        account_path = staging / "account_snapshot.redacted.json"
        write_json(account_path, sanitized_account_snapshot(account_info, captured_at=now))

        history_window = history_window_from_feed_clock(
            feed_clock,
            lookback=timedelta(days=args.history_days),
        )
        trades = fetch_closed_trades(
            broker.mt5,
            history_window.start,
            history_window.end,
        )
        trade_path = staging / "trade_history.csv"
        trade_metadata = write_trade_csv(trade_path, trades)
        instruments_path = staging / "instrument_snapshots.json"
        write_json(
            instruments_path,
            {
                "schema": "mt5.instrument_snapshots.v1",
                "captured_at": now.isoformat(),
                "warning": (
                    "Spreads and swaps are a current snapshot, not a historical cost series. "
                    "Final validation must use the local cost-aware MT5 harness."
                ),
                "instruments": instrument_snapshots,
            },
        )

        bridge_path = staging / "data-bridge" / "config.yaml"
        write_data_bridge_config(bridge_path, sources)
        readme_path = staging / "RESEARCH-ONLY.md"
        readme_path.write_text(RESEARCH_README, encoding="utf-8")

        files = [
            *bar_records,
            file_record(trade_path, staging, trade_metadata),
            file_record(account_path, staging),
            file_record(instruments_path, staging),
            file_record(bridge_path, staging),
            file_record(readme_path, staging),
        ]
        manifest = manifest_payload(
            generated_at=now,
            files=files,
            symbols=[source["symbol"] for source in sources],
            timeframe=args.timeframe,
            history_days=args.history_days,
            feed_clock={
                "reference_symbol": reference_symbol,
                "captured_host_utc": now.isoformat(),
                "broker_server_wall_time": feed_clock.feed_time.replace(
                    tzinfo=None
                ).isoformat(),
                "observed_offset_minutes": round(
                    feed_clock.observed_offset_seconds / 60.0, 3
                ),
                "whole_hour_offset_minutes": feed_clock.whole_hour_offset_minutes,
                "residual_seconds": round(feed_clock.residual_seconds, 3),
                "coherent": feed_clock.coherent,
                "history_query_start": history_window.start.isoformat(),
                "history_query_end": history_window.end.isoformat(),
            },
        )
        manifest["export_errors"] = export_errors
        manifest_path = staging / "manifest.json"
        write_json(manifest_path, manifest)

        root.mkdir(parents=True, exist_ok=True)
        if final.exists():
            raise RuntimeError(f"Refusing to replace existing bundle: {final}")
        staging.replace(final)
        pointer = {
            "schema": "mt5.vibe_research_pointer.v1",
            "bundle": str(final),
            "manifest": str(final / "manifest.json"),
        }
        pointer_tmp = root / ".latest.json.tmp"
        pointer_tmp.write_text(json.dumps(pointer, indent=2) + "\n", encoding="utf-8")
        pointer_tmp.replace(root / "latest.json")
        print(json.dumps({"status": "exported", **pointer}, indent=2))
        return 0
    finally:
        broker.shutdown()
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
