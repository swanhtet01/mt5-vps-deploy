"""Build dashboard metrics JSON from live MT5 history.

Runs daily at 01:00 UTC (before context_ingest at 01:30, before thesis at 02:00).
Writes data_cache/dashboard_metrics.json which thesis_ingest reads.

Fetches all closed deals since 2026-06-01, computes Sharpe/win-rate/per-edge
P&L using dashboard_metrics.py, and writes the result atomically.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))

import MetaTrader5 as mt5  # noqa: E402

from mt5_agent.dashboard_metrics import (  # noqa: E402
    ClosedTrade as DashTrade,
    compute_metrics,
)
from mt5_agent.edge_registry import EdgeRegistry  # noqa: E402
from mt5_agent.mt5_execution import (  # noqa: E402
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)
from mt5_agent.trade_history import closed_trades_from_deals  # noqa: E402
from paths import DATA_CACHE, write_json_atomic  # noqa: E402

REFERENCE_SYMBOLS = ("BTCUSD", "GOLD", "USDJPY")
START_DATE = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _load_registry() -> EdgeRegistry:
    try:
        reg_path = DATA_CACHE / "edge_registry.json"
        if reg_path.exists():
            return EdgeRegistry.load(reg_path)
    except Exception:
        pass
    return EdgeRegistry()


def build() -> dict:
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        now_utc = datetime.now(tz=timezone.utc)
        ai = mt5.account_info()
        if ai is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")

        _, clock = coherent_feed_clock_from_mt5(mt5, REFERENCE_SYMBOLS, host_utc=now_utc)
        window = history_window_from_feed_clock(clock, lookback=clock.feed_time - START_DATE)
        raw_deals = mt5.history_deals_get(window.start, window.end) or []

        # Filter to position-based deals (excludes deposits/withdrawals)
        position_deals = [
            d for d in raw_deals
            if int(getattr(d, "position_id", 0) or 0) > 0
            and bool(str(getattr(d, "symbol", "") or "").strip())
        ]
        trade_hist = closed_trades_from_deals(position_deals)

        # Convert trade_history.ClosedTrade → dashboard_metrics.ClosedTrade
        # entry_price/exit_price not available from deal grouping — set 0.0 (not used in metrics)
        dash_trades: list[DashTrade] = []
        today_start_str = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        daily_pnl = 0.0
        for t in trade_hist:
            close_iso = t.close_time.isoformat()
            open_iso = t.open_time.isoformat()
            dash_trades.append(DashTrade(
                magic=int(t.magic),
                symbol=str(t.symbol),
                entry_time=open_iso,
                exit_time=close_iso,
                profit=float(t.net),
                volume=float(t.volume),
                entry_price=0.0,
                exit_price=0.0,
                status="closed",
            ))
            if close_iso >= today_start_str:
                daily_pnl += float(t.net)

        dd_pct = max(0.0, (float(ai.balance) - float(ai.equity)) / max(float(ai.balance), 1)) * 100
        heartbeat = {
            "as_of": now_utc.isoformat(),
            "equity": float(ai.equity),
            "balance": float(ai.balance),
            "daily_realized_pnl": round(daily_pnl, 2),
            "drawdown_pct": round(dd_pct, 2),
        }

        registry = _load_registry()
        metrics = compute_metrics(dash_trades, registry, heartbeat=heartbeat)
        metrics["generated_at"] = now_utc.isoformat()
        metrics["n_deals_processed"] = len(raw_deals)
        metrics["n_trades_built"] = len(dash_trades)
        return metrics

    finally:
        mt5.shutdown()


def main() -> int:
    try:
        metrics = build()
        write_json_atomic(DATA_CACHE / "dashboard_metrics.json", metrics)
        acct = metrics.get("account", {})
        gl = metrics.get("global", {})
        print(
            f"dashboard_metrics built: "
            f"equity=${acct.get('equity', 0):.2f} "
            f"trades={gl.get('total_trades', 0)} "
            f"pnl=${gl.get('total_pnl', 0):.2f} "
            f"winrate={gl.get('win_rate', 0):.1%} "
            f"sharpe={gl.get('sharpe', 0):.2f}"
        )
        return 0
    except Exception as exc:
        print(f"build_dashboard_metrics FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
