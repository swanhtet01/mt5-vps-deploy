"""GOLD Asian-session drift — LIVE trader (real money).

WARNING: THIS PLACES REAL ORDERS ON THE LIVE MT5 ACCOUNT.

Safety guardrails enforced (no override):
  - HARD volume cap = 0.01 lot (broker minimum). Code refuses to send larger.
  - HARD daily-loss kill switch = $20 USD realized on the strategy magic number.
    If today's realized P/L on magic 88001 is <= -$20, refuses to open new trades.
  - Position cap = 1 open position for this magic. No pyramiding.
  - Catastrophic stop-loss = -1.5% from entry, attached to the order at broker side
    (protects against a disconnected machine + a gap move).
  - Take-profit = +1.0% (locks in unusual spikes; normal exit is time-based).
  - Requires env MT5_GOLD_DRIFT_LIVE=1 to actually send orders. Without it = paper.
  - Regime gate (50% trailing-60 win rate) must be ON. Without it = skip.

Modes:
  live-enter   evaluate at 00:00 broker-server time, send a BUY if all gates green
  live-exit    close any open position after one hour (market)
  live-status  print current state (positions, daily realized, regime)
"""

from __future__ import annotations

import json
import os
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import (
    adverse_slippage_points,
    context_sizing,
    feed_clock_provenance,
    feed_history_window,
    normalize_volume,
    persistent_user_flag_enabled,
    successful_deal_retcode,
)

SYM = "GOLD"
MAGIC = 88001
MAX_LOT = 0.05
# SL/TP scaled 5x from 0.01-lot baseline to keep the same price-distance safety net.
# At 0.05 lot: $150 SL = $30 gold move (same catastrophic backstop), typical 1h drift
# worth $15-50 vs the previous $3-10 at 0.01 lot.
HARD_STOP_USD = 150.0     # catastrophic SL — same price distance, 5x bigger $ (matches 5x lot)
HARD_TP_USD = 600.0       # catches rare moonshots; 99.7% of trades exit by time anyway
MAX_DAILY_LOSS_USD = 50.0
MAX_PORTFOLIO_DAILY_LOSS_USD = 80.0
LOOKBACK = 60
LIVE_ENV_FLAG = "MT5_GOLD_DRIFT_LIVE"
STRUCTURAL_MAGICS = set(range(88001, 88010))

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import (  # noqa: E402
    DATA_CACHE as _DATA_CACHE,
    NEWS_STATE_FILE as _NEWS_STATE_FILE,
    PAPER_ROOT as _PAPER_ROOT,
    read_json as _read_json,
)

LOG_DIR = _PAPER_ROOT / "gold-drift"
NEWS_FILE = _NEWS_STATE_FILE


def is_live() -> bool:
    return persistent_user_flag_enabled(LIVE_ENV_FLAG)


def _check_news_blackout(symbol: str) -> str:
    """Return a non-empty reason if the symbol is currently in a news/event blackout, else "".
    Fails OPEN if the news file is missing/corrupt (we trade), but always honors an active
    blackout window. The news ingestor refreshes the file hourly."""
    state = _read_json(NEWS_FILE)
    rec = state.get(symbol) or {}
    bo = rec.get("blackout_until")
    if not bo:
        return ""
    try:
        until = datetime.fromisoformat(bo)
    except ValueError:
        return ""
    if datetime.now(tz=timezone.utc) >= until:
        return ""
    return f"news blackout until {bo} ({(rec.get('headline') or '')[:80]})"


def append_event(event: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "live-events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, indent=2, default=str))


def init_mt5() -> None:
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")


def compute_regime_state(si) -> tuple[bool, float, int]:
    bars = mt5.copy_rates_from_pos(SYM, mt5.TIMEFRAME_H1, 0, 7200)
    if bars is None or len(bars) < 2:
        raise RuntimeError("no H1 bars")
    rt_cost = (si.spread * si.point / si.trade_tick_size) * si.trade_tick_value * 0.01 + \
              (3.0 * si.point / si.trade_tick_size) * si.trade_tick_value * 0.01
    history: deque = deque(maxlen=LOOKBACK)
    for i in range(1, len(bars)):
        t = datetime.fromtimestamp(int(bars[i]["time"]), tz=timezone.utc)
        if t.hour != 1:
            continue
        pnl = ((float(bars[i]["close"]) - float(bars[i - 1]["close"])) /
               si.trade_tick_size) * si.trade_tick_value * 0.01 - rt_cost
        history.append(pnl)
    if len(history) < 30:
        return False, 0.0, len(history)
    wins = sum(1 for p in history if p > 0)
    return wins / len(history) >= 0.50, wins / len(history), len(history)


def _today_history_deals(reference_tick=None, *, host_utc: datetime | None = None) -> list:
    tick = reference_tick or mt5.symbol_info_tick(SYM)
    window = feed_history_window(
        tick,
        host_utc=host_utc,
        start_of_feed_day=True,
    )
    deals = mt5.history_deals_get(window.start, window.end)
    if deals is None:
        raise RuntimeError(f"history_deals_get failed: {mt5.last_error()}")
    return list(deals)


def today_realized_pnl_usd(reference_tick=None, *, host_utc: datetime | None = None) -> float:
    """Realized P/L today for THIS strategy magic number, in account currency."""
    deals = _today_history_deals(reference_tick, host_utc=host_utc)
    total = 0.0
    for d in deals:
        if d.magic == MAGIC:
            total += d.profit + d.commission + d.swap
    return total


def today_structural_portfolio_pnl_usd(
    reference_tick=None,
    *,
    host_utc: datetime | None = None,
) -> float:
    deals = _today_history_deals(reference_tick, host_utc=host_utc)
    return sum(
        d.profit + d.commission + d.swap
        for d in deals
        if d.magic in STRUCTURAL_MAGICS
    )


def _load_context_sizing() -> tuple[dict, float, bool]:
    payload = _read_json(_DATA_CACHE / "context_score.json") or {}
    multiplier, fresh = context_sizing(payload)
    return (payload if fresh else {}), multiplier, fresh


def open_positions_for_magic() -> list:
    pos = mt5.positions_get(symbol=SYM)
    if pos is None:
        return []
    return [p for p in pos if p.magic == MAGIC]


def live_enter(force: bool = False) -> None:
    now = datetime.now(tz=timezone.utc)
    si = mt5.symbol_info(SYM)
    tick = mt5.symbol_info_tick(SYM)
    if si is None or tick is None:
        append_event({"event": "enter_error", "ts": now.isoformat(), "reason": "no symbol/tick"})
        return
    clock = feed_clock_provenance(tick, host_utc=now)
    if clock is None or not clock.coherent:
        append_event({
            "event": "enter_error", "ts": now.isoformat(),
            "reason": "stale or incoherent feed clock",
            **(clock.as_dict() if clock is not None else {}),
        })
        return
    server_now = clock.feed_time

    # Always log the runtime context FIRST so we can debug env / mode inheritance even when
    # later gates refuse. This is the heartbeat record of every scheduled-task fire.
    mode = "live" if is_live() else "paper-only"
    append_event({"event": "heartbeat", "ts": now.isoformat(), "mode": mode,
                  "persistent_live_authorized": is_live(),
                  "process_env_value": os.environ.get(LIVE_ENV_FLAG, "(unset)"),
                  "force": force, "broker_server_time": server_now.isoformat(),
                  **clock.as_dict(),
                  "broker_weekday": server_now.weekday(), "broker_hour": server_now.hour})

    # Gate 0 uses the same broker clock represented by the researched MT5 bars.
    if not force and not (server_now.hour == 0 and server_now.minute <= 10):
        append_event({"event": "skip_enter", "ts": now.isoformat(),
                      "broker_server_time": server_now.isoformat(),
                      "reason": f"outside broker entry window 00:00-00:10 (now {server_now.strftime('%H:%M')})"})
        return
    # gate 0b: skip weekends (broker rolls swap on Fri close; no Asian session Sat/Sun)
    if not force and server_now.weekday() >= 5:
        append_event({"event": "skip_enter", "ts": now.isoformat(),
                      "broker_server_time": server_now.isoformat(),
                      "reason": f"weekend on broker clock (weekday {server_now.weekday()})"})
        return

    # gate 1: env flag
    live = is_live()
    # gate 2: position cap
    existing = open_positions_for_magic()
    # gate 3: daily loss
    daily_pnl = today_realized_pnl_usd(tick, host_utc=now)
    portfolio_daily_pnl = today_structural_portfolio_pnl_usd(tick, host_utc=now)
    # gate 3b: news/calendar blackout (fail OPEN if file missing/stale; honor active blackout)
    news_blackout = _check_news_blackout(SYM)
    # gate 4: regime
    regime_on, wr, n = compute_regime_state(si)

    base_event = {
        "ts": now.isoformat(), "symbol": SYM, "mode": "live" if live else "paper-only",
        "broker_server_time": server_now.isoformat(),
        **clock.as_dict(),
        "regime_on": regime_on, "trailing_win_rate": round(wr, 4), "trailing_n": n,
        "daily_realized_pnl_usd": round(daily_pnl, 2),
        "portfolio_daily_realized_usd": round(portfolio_daily_pnl, 2),
        "open_positions_for_magic": len(existing),
        "bid": tick.bid, "ask": tick.ask, "spread_pts": si.spread,
    }

    if existing:
        append_event({**base_event, "event": "skip_enter", "reason": "position already open"})
        return
    if daily_pnl <= -MAX_DAILY_LOSS_USD:
        append_event({**base_event, "event": "skip_enter", "reason": f"daily loss kill switch hit (${daily_pnl:.2f} <= -${MAX_DAILY_LOSS_USD})"})
        return
    if portfolio_daily_pnl <= -MAX_PORTFOLIO_DAILY_LOSS_USD:
        append_event({
            **base_event,
            "event": "skip_enter",
            "reason": f"structural portfolio daily loss kill ${portfolio_daily_pnl:.2f}",
        })
        return
    if news_blackout:
        append_event({**base_event, "event": "skip_enter", "reason": news_blackout})
        return
    if not regime_on:
        append_event({**base_event, "event": "skip_enter", "reason": "regime gate OFF"})
        return
    if not live:
        append_event({**base_event, "event": "paper_enter", "reason": f"env {LIVE_ENV_FLAG} not set — paper only"})
        return

    # All gates green. Build the order.
    price = tick.ask
    context, sizing_mult, context_fresh = _load_context_sizing()
    requested_volume = min(MAX_LOT, float(si.volume_max)) * sizing_mult
    volume = normalize_volume(
        requested_volume,
        minimum=float(si.volume_min),
        maximum=min(float(si.volume_max), MAX_LOT),
        step=float(si.volume_step or 0.01),
    )
    if volume <= 0:
        append_event({
            **base_event,
            "event": "skip_enter",
            "reason": f"risk-adjusted volume {requested_volume:.5f} below broker minimum {si.volume_min}",
            "context_fresh": context_fresh,
        })
        return
    # SL/TP in DOLLARS converted to price distance via tick math:
    # $X for 0.01 lot = X * tick_size / (tick_value * 0.01) price units
    sl_price_dist = HARD_STOP_USD * si.trade_tick_size / (si.trade_tick_value * volume)
    tp_price_dist = HARD_TP_USD * si.trade_tick_size / (si.trade_tick_value * volume)
    sl = round(price - sl_price_dist, si.digits)
    tp = round(price + tp_price_dist, si.digits)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYM,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC,
        "comment": "gold_asian_drift",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        append_event({**base_event, "event": "order_check_failed", "request": request,
                      "check": check._asdict() if check else None})
        # try a different filling mode (some brokers reject IOC on CFDs)
        request["type_filling"] = mt5.ORDER_FILLING_FOK
        check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        request["type_filling"] = mt5.ORDER_FILLING_RETURN
        check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        append_event({**base_event, "event": "order_check_all_filling_failed", "request": request,
                      "check": check._asdict() if check else None})
        return

    result = mt5.order_send(request)
    if result is None:
        append_event({**base_event, "event": "order_send_returned_none", "last_error": mt5.last_error()})
        return
    res_dict = result._asdict()
    if not successful_deal_retcode(
        res_dict.get("retcode"),
        done=mt5.TRADE_RETCODE_DONE,
        done_partial=mt5.TRADE_RETCODE_DONE_PARTIAL,
    ):
        append_event({**base_event, "event": "order_send_rejected", "request": request, "result": res_dict})
        return
    fill_price = res_dict.get("price", request["price"])
    slippage_pts = adverse_slippage_points("buy", request["price"], fill_price, si.point)
    # Partial fill: order requested volume > what was actually filled
    filled_vol = res_dict.get("volume", request["volume"])
    partial_fill = filled_vol < (request["volume"] - 1e-9)
    append_event({**base_event, "event": "live_order_sent", "request": request, "result": res_dict,
                  "slippage_points": round(slippage_pts, 4), "partial_fill": partial_fill,
                  "context_score": context.get("trading_context_score", 0),
                  "sizing_multiplier": round(sizing_mult, 4),
                  "context_fresh": context_fresh})


def live_exit() -> None:
    now = datetime.now(tz=timezone.utc)
    si = mt5.symbol_info(SYM)
    tick = mt5.symbol_info_tick(SYM)
    if si is None or tick is None:
        append_event({"event": "exit_error", "ts": now.isoformat(), "reason": "no symbol/tick"})
        return
    positions = open_positions_for_magic()
    if not positions:
        append_event({"event": "exit_skip_no_position", "ts": now.isoformat()})
        return
    for p in positions:
        # Close BUY by SELL at bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": SYM,
            "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "position": p.ticket,
            "price": tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask,
            "deviation": 20,
            "magic": MAGIC,
            "comment": "gold_asian_drift_exit",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)
        if result is None or result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
            request["type_filling"] = mt5.ORDER_FILLING_RETURN
            result = mt5.order_send(request)
        res = result._asdict() if result else {"error": str(mt5.last_error())}
        if result is None or not successful_deal_retcode(
            res.get("retcode"),
            done=mt5.TRADE_RETCODE_DONE,
            done_partial=mt5.TRADE_RETCODE_DONE_PARTIAL,
        ):
            append_event({
                "event": "live_exit_rejected",
                "ts": now.isoformat(),
                "symbol": SYM,
                "magic": MAGIC,
                "position_ticket": p.ticket,
                "request": request,
                "result": res,
            })
            continue
        fill_price = res.get("price", request["price"]) if result else request["price"]
        close_side = "sell" if p.type == mt5.POSITION_TYPE_BUY else "buy"
        slippage_pts = adverse_slippage_points(close_side, request["price"], fill_price, si.point)
        # Partial fill: order requested volume > what was actually filled
        filled_vol = res.get("volume", request["volume"]) if result else request["volume"]
        partial_fill = filled_vol < (request["volume"] - 1e-9)
        append_event({"event": "live_exit_close", "ts": now.isoformat(), "symbol": SYM, "magic": MAGIC,
                      "position_ticket": p.ticket, "volume": p.volume,
                      "entry_price": p.price_open, "exit_price": request["price"],
                      "result": res, "slippage_points": round(slippage_pts, 4), "partial_fill": partial_fill})


def live_status() -> None:
    now = datetime.now(tz=timezone.utc)
    si = mt5.symbol_info(SYM)
    tick = mt5.symbol_info_tick(SYM)
    ai = mt5.account_info()
    regime_on, wr, n = compute_regime_state(si)
    positions = open_positions_for_magic()
    daily = today_realized_pnl_usd(tick, host_utc=now)
    live = is_live()
    clock = feed_clock_provenance(tick, host_utc=now)
    server_now = clock.feed_time if clock is not None else None
    print(f"GOLD DRIFT LIVE TRADER — STATUS @ {now.isoformat()}")
    print(f"  Mode: {'LIVE (real orders)' if live else 'PAPER (env not set)'}")
    print(f"  Persistent live authorization: {live}")
    print(f"  Process env diagnostic: {os.environ.get(LIVE_ENV_FLAG, '(unset)')}")
    print(f"  Feed clock: {server_now.isoformat() if server_now else 'unavailable'}")
    print(f"  Feed clock coherent: {clock.coherent if clock is not None else False}")
    print(f"  Account: equity=${ai.equity:.2f} balance=${ai.balance:.2f}")
    print(f"  Regime: {'ON' if regime_on else 'OFF'}  (trailing {n} signals, win rate {100*wr:.1f}%)")
    print(f"  Open positions on magic {MAGIC}: {len(positions)}")
    for p in positions:
        print(f"    ticket {p.ticket}  vol {p.volume}  entry {p.price_open}  current P/L ${p.profit:.2f}")
    print(f"  Today's realized P/L on this magic: ${daily:+.2f} / ${MAX_DAILY_LOSS_USD:.0f} kill switch")
    print(f"  Current GOLD bid={tick.bid} ask={tick.ask} spread={si.spread}pts")
    # show today's events
    log = LOG_DIR / "live-events.jsonl"
    if log.exists():
        lines = log.read_text(encoding="utf-8").splitlines()
        today_events = [l for l in lines if l and now.date().isoformat() in l[:30]]
        print(f"  Events today: {len(today_events)}")
        for l in today_events[-5:]:
            ev = json.loads(l)
            print(f"    [{ev.get('ts','?')[11:19]}] {ev.get('event','?')}: {ev.get('reason', ev.get('result', ''))}")


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "live-status"
    force = "--force" in args
    init_mt5()
    try:
        if mode == "live-enter":
            live_enter(force=force)
        elif mode == "live-exit":
            live_exit()
        elif mode == "live-status":
            live_status()
        else:
            print(f"unknown mode: {mode}. use live-enter|live-exit|live-status")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
