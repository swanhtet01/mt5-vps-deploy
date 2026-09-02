"""Position monitor -> rich, XM-app-style phone alerts WITHOUT spam.

Polls MT5 every few minutes and notifies ONLY when something actually changed:
  * a trade OPENED  -> "Bought 0.01 GOLD @ 4140 (Gold Asian drift). SL/TP ..."
  * a trade CLOSED  -> "Gold Asian drift won +$8.50 (held 1.0h). Account now $691."
Unchanged polls are SILENT. State in data_cache/notify_state.json prevents repeat alerts, and
the FIRST run seeds silently (no backfill spam). This gives you XM-app-like visibility -- you
hear about every fill within ~5 min -- without a notification every tick.

Run every 5 min via the MT5-PositionMonitor scheduled task.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA_CACHE, read_json, write_json_atomic  # noqa: E402
try:
    from task_receipt import run_receipt  # noqa: E402
except ImportError:  # helper not delivered: bookkeeping must never stop the task
    print("WARN: task_receipt.py missing; running without a run receipt", file=sys.stderr)
    from contextlib import nullcontext as _nullcontext  # noqa: E402

    def run_receipt(task, directory=None):
        return _nullcontext(None)
import notify as notifier  # noqa: E402
from mt5_agent.mt5_execution import (  # noqa: E402
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)
from mt5_agent.trade_history import closed_trades_from_deals  # noqa: E402

STATE = DATA_CACHE / "notify_state.json"
REFERENCE_SYMBOLS = ("BTCUSD", "GOLD", "USDJPY")

# magic -> human edge name (decoupled from the registry so it works on the VPS immediately)
EDGE_NAMES = {
    88001: "Gold Asian drift", 88002: "USDJPY Monday", 88003: "UK100 Thursday",
    88004: "Gold Friday", 88005: "USDJPY Wednesday", 88006: "Gold Thursday",
    88007: "AUDJPY Monday", 88008: "GBPJPY Thursday", 88009: "Gold Tuesday",
}


def _money(x: float) -> str:
    return f"+${x:,.2f}" if x >= 0 else f"-${abs(x):,.2f}"


def _edge(magic) -> str:
    return EDGE_NAMES.get(int(magic), f"magic {magic}")


def _dur(open_t: datetime, close_t: datetime) -> str:
    mins = max(0.0, (close_t - open_t).total_seconds() / 60.0)
    return f"{mins / 60:.1f}h" if mins >= 60 else f"{int(mins)}m"


def open_alert(pos: dict) -> dict:
    side = "Bought" if int(pos["type"]) == 0 else "Sold"
    sltp = ""
    if pos.get("sl"):
        sltp += f"\nStop @ {pos['sl']}"
    if pos.get("tp"):
        sltp += f"  Target @ {pos['tp']}"
    return {"title": f"Trade opened: {side} {pos['symbol']}", "tags": "green_circle",
            "body": f"{side} {pos['volume']} {pos['symbol']} @ {pos['price_open']}\n"
                    f"Edge: {_edge(pos['magic'])}{sltp}"}


def close_alert(ct, equity: float) -> dict:
    verb = "won" if ct.net >= 0 else "lost"
    return {"title": f"{ct.symbol} closed {_money(ct.net)}",
            "tags": "white_check_mark" if ct.net >= 0 else "x",
            "body": f"{_edge(ct.magic)} {verb} {_money(ct.net)} (held {_dur(ct.open_time, ct.close_time)}).\n"
                    f"Account now ${equity:,.2f}."}


def detect(prev: dict, positions: list[dict], closed_trades: list, equity: float):
    """Pure: compare current MT5 state to the previous snapshot; return (alerts, new_state).
    First run (empty prev) seeds silently so we never backfill-spam old trades."""
    first_run = not prev
    prev_open = set(prev.get("open_ids", []))
    announced = set(prev.get("announced_closed", []))
    cur_open = {int(p["ticket"]): p for p in positions}

    alerts: list[dict] = []
    if first_run:
        # record what already exists so only FUTURE events alert
        announced |= {ct.position_id for ct in closed_trades}
    else:
        for tid in sorted(cur_open.keys() - prev_open):
            alerts.append(open_alert(cur_open[tid]))
        for ct in closed_trades:
            if ct.position_id not in announced and ct.position_id not in cur_open:
                alerts.append(close_alert(ct, equity))
                announced.add(ct.position_id)

    new_state = {"open_ids": sorted(cur_open.keys()),
                 "announced_closed": sorted(announced)[-300:],
                 "updated": None}  # timestamp filled by caller (keeps detect pure/testable)
    return alerts, new_state


def main(receipt=None) -> None:
    import MetaTrader5 as mt5
    initialized = bool(mt5.initialize())
    if receipt is not None:
        receipt.mt5_init = initialized
    if not initialized:
        print(f"position_monitor: mt5.initialize failed: {mt5.last_error()}", file=sys.stderr)
        return
    try:
        ai = mt5.account_info()
        equity = float(ai.equity) if ai else 0.0
        positions = [{
            "ticket": p.ticket, "type": p.type, "magic": p.magic, "volume": p.volume,
            "price_open": p.price_open, "sl": p.sl, "tp": p.tp, "symbol": p.symbol,
        } for p in (mt5.positions_get() or [])]
        now = datetime.now(tz=timezone.utc)
        reference_symbol, clock = coherent_feed_clock_from_mt5(
            mt5,
            REFERENCE_SYMBOLS,
            host_utc=now,
        )
        history_window = history_window_from_feed_clock(
            clock,
            lookback=timedelta(hours=36),
        )
        deals = mt5.history_deals_get(history_window.start, history_window.end)
        if deals is None:
            raise RuntimeError(f"history_deals_get failed: {mt5.last_error()}")
        closed = closed_trades_from_deals(deals)
    finally:
        mt5.shutdown()

    prev = read_json(STATE) or {}
    alerts, new_state = detect(prev, positions, closed, equity)
    new_state["updated"] = datetime.now(tz=timezone.utc).isoformat()
    new_state["history_reference_symbol"] = reference_symbol
    new_state.update(history_window.as_dict())
    write_json_atomic(STATE, new_state)

    for a in alerts:
        notifier.send_ntfy(a["body"], title=a["title"], tags=a.get("tags"))
    print(f"position_monitor: {len(alerts)} alert(s) sent; {len(positions)} open, equity ${equity:,.2f}")


if __name__ == "__main__":
    try:
        # The receipt records the exception (ok=false) and re-raises; the except below then
        # keeps the task itself non-fatal exactly as before.
        with run_receipt("MT5-PositionMonitor") as _receipt:
            main(_receipt)
    except Exception as exc:  # never fail the task
        print(f"position_monitor non-fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
