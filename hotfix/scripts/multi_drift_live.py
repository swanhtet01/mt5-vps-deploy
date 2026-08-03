"""Multi-signal live trader for the 3 NEW structural edges from the multi-instrument hunt.

Each signal is a one-hour drift keyed by the timestamp of the researched RETURN bar.
The executable entry is one hour earlier. Earlier releases entered at the return-bar
timestamp and therefore traded the following, untested hour.

All share the gold-drift's machinery: regime gate (trailing 60 win rate >= 50%), live env
flag, news/calendar blackout, kill switch, hard SL/TP. Per-signal magic numbers prevent
cross-contamination on the broker side.

Usage:
  python multi_drift_live.py USDJPY_MON live-enter
  python multi_drift_live.py UK100_THU  live-exit
  python multi_drift_live.py GOLD_FRI   live-status
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
    context_sizing as _shared_context_sizing,
    feed_clock_provenance,
    feed_history_window,
    normalize_volume,
    persistent_user_flag_enabled,
    record_is_fresh as _shared_record_is_fresh,
    successful_deal_retcode,
)
from mt5_agent.portfolio_budget import budget_multiplier
from mt5_agent.profit_funded_scaling import SCHEMA as PROFIT_SCALING_SCHEMA

# Shared path resolver — works on dev PC AND VPS without code changes.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import (  # noqa: E402
    BLACKLIST_FILE as _BLACKLIST_FILE,
    DATA_CACHE as _DATA_CACHE,
    NEWS_STATE_FILE as _NEWS_STATE_FILE,
    PAPER_ROOT as _PAPER_ROOT,
    SIZING_FILE as _SIZING_FILE,
    read_json as _read_json,
)

LIVE_ENV_FLAG = "MT5_GOLD_DRIFT_LIVE"   # reuse the same arming flag
LOG_DIR = _PAPER_ROOT / "multi-drift"
NEWS_FILE = _NEWS_STATE_FILE


SIGNALS = {
    "USDJPY_MON": dict(
        symbol="USDJPY", weekday=0, entry_hour=0, exit_hour=1,
        research_weekday=0, research_return_hour=1,
        side="long", magic=88002, sl_usd=15.0, tp_usd=60.0, max_lot=0.01,
        description="USDJPY Monday 00:00-01:00 feed time LONG (return bucket t=+8.12, n=248)",
    ),
    "UK100_THU": dict(
        symbol="UK100Cash", weekday=3, entry_hour=0, exit_hour=1,
        research_weekday=3, research_return_hour=1,
        side="short", magic=88003, sl_usd=15.0, tp_usd=60.0, max_lot=0.1,
        description="UK100 Thursday 00:00-01:00 feed time SHORT (return bucket t=-5.48, n=268)",
    ),
    "GOLD_FRI": dict(
        symbol="GOLD", weekday=4, entry_hour=22, exit_hour=23,
        research_weekday=4, research_return_hour=23,
        side="long", magic=88004, sl_usd=30.0, tp_usd=120.0, max_lot=0.01,
        description="GOLD Friday 22:00-23:00 feed time LONG (return bucket t=+5.02, n=234)",
    ),
    "USDJPY_WED": dict(
        symbol="USDJPY", weekday=2, entry_hour=21, exit_hour=22,
        research_weekday=2, research_return_hour=22,
        side="long", magic=88005, sl_usd=15.0, tp_usd=60.0, max_lot=0.01,
        description="USDJPY Wednesday 21:00-22:00 feed time LONG (strict return bucket)",
    ),
    "GOLD_THU": dict(
        symbol="GOLD", weekday=3, entry_hour=0, exit_hour=1,
        research_weekday=3, research_return_hour=1,
        side="long", magic=88006, sl_usd=30.0, tp_usd=120.0, max_lot=0.01,
        description="GOLD Thursday 00:00-01:00 feed time LONG (return bucket t=+4.29, n=261)",
    ),
    # NEW edges from wide_edge_hunt (2026-06-23): JPY-cross Monday surge + GBPJPY Thu fade + Tue gold
    "AUDJPY_MON": dict(
        symbol="AUDJPY", weekday=0, entry_hour=0, exit_hour=1,
        research_weekday=0, research_return_hour=1,
        side="long", magic=88007, sl_usd=12.0, tp_usd=48.0, max_lot=0.01,
        description="AUDJPY Monday 00:00-01:00 feed time LONG (return bucket t=+11.99)",
    ),
    "GBPJPY_THU": dict(
        symbol="GBPJPY", weekday=2, entry_hour=23, exit_hour=0,
        research_weekday=3, research_return_hour=0,
        side="short", magic=88008, sl_usd=12.0, tp_usd=48.0, max_lot=0.01,
        description="GBPJPY Wednesday 23:00-Thursday 00:00 feed time SHORT (return bucket t=-8.16)",
    ),
    "GOLD_TUE": dict(
        symbol="GOLD", weekday=1, entry_hour=0, exit_hour=1,
        research_weekday=1, research_return_hour=1,
        side="long", magic=88009, sl_usd=30.0, tp_usd=120.0, max_lot=0.01,
        description="GOLD Tuesday 00:00-01:00 feed time LONG (return bucket t=+3.31)",
    ),
}

MAX_DAILY_LOSS_USD = 20.0
MAX_PORTFOLIO_DAILY_LOSS_USD = 20.0
LOOKBACK = 60
SIZING_FILE = _SIZING_FILE
PORTFOLIO_BUDGET_FILE = _DATA_CACHE / "portfolio_budget.json"
STRUCTURAL_MAGICS = {88001, *(spec["magic"] for spec in SIGNALS.values())}


def _load_lot_multiplier(magic: int) -> tuple[float, str]:
    """Read the fresh, closed-profit-only promotion artifact; fail to 1x."""
    payload = _read_json(SIZING_FILE)
    if payload.get("schema") != PROFIT_SCALING_SCHEMA:
        return 1.0, "default (unverified sizing schema)"
    max_age_hours = min(max(float(payload.get("max_age_hours") or 0.0), 1.0), 26.0)
    if not _record_is_fresh(payload, max_age_minutes=int(max_age_hours * 60)):
        return 1.0, "default (sizing artifact stale)"
    account_guard = payload.get("account_guard") or {}
    try:
        account_cap = float(account_guard.get("multiplier_cap") or 1.0)
    except (TypeError, ValueError):
        return 1.0, "default (account guard parse error)"
    rec = (payload.get("recommendations") or {}).get(str(magic)) or {}
    try:
        mult = float(rec.get("lot_multiplier") or 1.0)
    except (TypeError, ValueError):
        return 1.0, "default (sizing parse error)"
    tier = int(rec.get("tier") or 0)
    if mult > 1.0 and not bool(rec.get("promotion_authorized")):
        return 1.0, "default (promotion not authorized)"
    reason = "; ".join(rec.get("rollback_reasons") or []) or f"profit-funded tier {tier}"
    return min(max(mult, 1.0), max(min(account_cap, 3.0), 1.0)), reason


def _load_portfolio_budget_multiplier(magic: int) -> float:
    return budget_multiplier(_read_json(PORTFOLIO_BUDGET_FILE), magic)


def _support_throttle(portfolio: float, news: float, context: float) -> float:
    """Supplemental signals may reduce, but never exceed, the evidence ceiling."""
    return min(max(float(portfolio) * float(news) * float(context), 0.0), 1.0)


def _is_blacklisted(symbol: str, magic: int) -> tuple[bool, str]:
    """Read self_improver's blacklist. Refuse trade if (symbol, magic) is on it."""
    entries = (_read_json(_BLACKLIST_FILE) or {}).get("entries", [])
    for e in entries:
        if e.get("symbol") == symbol and int(e.get("magic", 0)) == magic:
            return True, str(e.get("reason", "blacklisted"))
    return False, ""


def _record_is_fresh(
    record: dict,
    *,
    max_age_minutes: int = 180,
    now: datetime | None = None,
) -> bool:
    return _shared_record_is_fresh(
        record,
        max_age_minutes=max_age_minutes,
        now=now,
    )


def _load_news_sentiment(symbol: str) -> tuple[float, str]:
    """Read sentiment for sizing modifier. Returns (sentiment, headline)."""
    state = _read_json(NEWS_FILE)
    rec = state.get(symbol) or {}
    if not _record_is_fresh(rec):
        return 0.0, ""
    try:
        sent = float(rec.get("sentiment") or 0.0)
    except (TypeError, ValueError):
        sent = 0.0
    return sent, str(rec.get("headline") or "")[:80]


def _context_sizing(payload: dict, *, max_age_minutes: int = 180) -> tuple[dict, float, bool]:
    multiplier, fresh = _shared_context_sizing(
        payload,
        max_age_minutes=max_age_minutes,
    )
    return (payload if fresh else {}), multiplier, fresh


def is_live() -> bool:
    return persistent_user_flag_enabled(LIVE_ENV_FLAG)


def load_news_blackout(symbol: str) -> str:
    """Returns "" or a non-empty reason if the symbol is currently in a news/event blackout."""
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


def append_event(name: str, event: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    event["signal"] = name
    with (LOG_DIR / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, indent=2, default=str))


def init_mt5() -> None:
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")


def compute_regime_state(spec: dict) -> tuple[bool, float, int]:
    """Trailing-60 win rate on the researched MT5 return-bar bucket."""
    sym = spec["symbol"]
    si = mt5.symbol_info(sym)
    if si is None:
        return False, 0.0, 0
    bars = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 8000)
    if bars is None or len(bars) < 2:
        return False, 0.0, 0
    # spread + 3pt slip cost in dollars for 0.01 lot of THIS instrument
    rt_cost = ((si.spread + 6) * si.point / si.trade_tick_size) * si.trade_tick_value * spec["max_lot"]
    history: deque = deque(maxlen=LOOKBACK)
    direction = 1 if spec["side"] == "long" else -1
    return_hour = int(spec["research_return_hour"])
    return_weekday = int(spec["research_weekday"])
    for i in range(1, len(bars)):
        t = datetime.fromtimestamp(int(bars[i]["time"]), tz=timezone.utc)
        if t.hour != return_hour or t.weekday() != return_weekday:
            continue
        price_move = direction * (float(bars[i]["close"]) - float(bars[i - 1]["close"]))
        pnl = (price_move / si.trade_tick_size) * si.trade_tick_value * spec["max_lot"] - rt_cost
        history.append(pnl)
    if len(history) < 20:
        return False, 0.0, len(history)
    wins = sum(1 for p in history if p > 0)
    return wins / len(history) >= 0.50, wins / len(history), len(history)


def _today_history_deals(reference_tick=None, *, host_utc: datetime | None = None) -> list:
    tick = reference_tick or mt5.symbol_info_tick("GOLD")
    window = feed_history_window(
        tick,
        host_utc=host_utc,
        start_of_feed_day=True,
    )
    deals = mt5.history_deals_get(window.start, window.end)
    if deals is None:
        raise RuntimeError(f"history_deals_get failed: {mt5.last_error()}")
    return list(deals)


def today_realized_pnl_usd(
    magic: int,
    reference_tick=None,
    *,
    host_utc: datetime | None = None,
) -> float:
    deals = _today_history_deals(reference_tick, host_utc=host_utc)
    return sum(d.profit + d.commission + d.swap for d in deals if d.magic == magic)


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


def open_positions_for_magic(magic: int) -> list:
    pos = mt5.positions_get() or []
    return [p for p in pos if p.magic == magic]


def live_enter(name: str, spec: dict, force: bool = False) -> None:
    now = datetime.now(tz=timezone.utc)
    live = is_live()
    si = mt5.symbol_info(spec["symbol"])
    tick = mt5.symbol_info_tick(spec["symbol"])
    if si is None or tick is None:
        append_event(name, {"event": "skip", "ts": now.isoformat(), "reason": "no symbol/tick"})
        return
    clock = feed_clock_provenance(tick, host_utc=now)
    if clock is None or not clock.coherent:
        append_event(name, {
            "event": "skip", "ts": now.isoformat(),
            "reason": "stale or incoherent feed clock",
            **(clock.as_dict() if clock is not None else {}),
        })
        return
    server_now = clock.feed_time
    base = {
        "event": "enter_eval", "ts": now.isoformat(),
        "symbol": spec["symbol"], "side": spec["side"], "mode": "live" if live else "paper-only",
        "broker_server_time": server_now.isoformat(),
        **clock.as_dict(),
        "weekday_now": server_now.weekday(), "weekday_required": spec["weekday"],
        "hour_now": server_now.hour, "hour_required": spec["entry_hour"],
    }

    # Day gate
    if not force and server_now.weekday() != spec["weekday"]:
        append_event(name, {**base, "event": "skip", "reason": f"wrong broker weekday {server_now.weekday()} != {spec['weekday']}"})
        return
    # Hour gate (allow 0-10 minute window)
    if not force and (server_now.hour != spec["entry_hour"] or server_now.minute > 10):
        append_event(name, {**base, "event": "skip", "reason": f"outside broker entry window {spec['entry_hour']}:00-{spec['entry_hour']}:10"})
        return

    # Existing position
    existing = open_positions_for_magic(spec["magic"])
    if existing:
        append_event(name, {**base, "event": "skip", "reason": "position already open"})
        return

    # Daily loss kill
    daily = today_realized_pnl_usd(spec["magic"], tick, host_utc=now)
    if daily <= -MAX_DAILY_LOSS_USD:
        append_event(name, {**base, "event": "skip", "reason": f"daily loss kill ${daily:.2f}"})
        return
    portfolio_daily = today_structural_portfolio_pnl_usd(tick, host_utc=now)
    base["portfolio_daily_realized_usd"] = round(portfolio_daily, 2)
    if portfolio_daily <= -MAX_PORTFOLIO_DAILY_LOSS_USD:
        append_event(
            name,
            {
                **base,
                "event": "skip",
                "reason": f"structural portfolio daily loss kill ${portfolio_daily:.2f}",
            },
        )
        return

    # Self-improver blacklist (auto-pause for persistent leaks)
    bl, bl_reason = _is_blacklisted(spec["symbol"], spec["magic"])
    if bl:
        append_event(name, {**base, "event": "skip", "reason": f"BLACKLISTED: {bl_reason}"})
        return

    # News/event blackout
    news_block = load_news_blackout(spec["symbol"])
    if news_block:
        append_event(name, {**base, "event": "skip", "reason": news_block})
        return

    # Regime gate
    regime_on, wr, n = compute_regime_state(spec)
    base["regime_on"] = regime_on
    base["trailing_win_rate"] = round(wr, 4)
    base["trailing_n"] = n
    if not regime_on:
        append_event(name, {**base, "event": "skip", "reason": f"regime OFF (wr {100*wr:.1f}% over {n} signals)"})
        return

    if not live:
        append_event(name, {**base, "event": "paper_enter", "bid": tick.bid, "ask": tick.ask,
                            "reason": f"env {LIVE_ENV_FLAG}=1 not set; paper only"})
        return

    # Build the order — apply auto-promotion ladder + news sentiment modifier + context scoring
    side = spec["side"]
    base_lot = max(spec["max_lot"], si.volume_min)
    lot_mult, sizing_reason = _load_lot_multiplier(spec["magic"])
    portfolio_mult = _load_portfolio_budget_multiplier(spec["magic"])
    # News modifier: if sentiment AGREES with our trade direction (positive for long, negative
    # for short) AND is strong (|sent| >= 0.3), give a small additional boost up to 1.5×
    sent, sent_head = _load_news_sentiment(spec["symbol"])
    news_mult = 1.0
    direction_score = sent if side == "long" else -sent
    if abs(direction_score) >= 0.3 and direction_score > 0:
        news_mult = 1.0 + min(direction_score, 0.5)  # cap at 1.5×
    elif abs(direction_score) >= 0.5 and direction_score < 0:
        # News strongly DISAGREES — shrink size (but don't abort)
        news_mult = 0.5
    # Load context scoring and apply sizing multiplier
    context, sizing_mult, context_fresh = _context_sizing(
        _read_json(_DATA_CACHE / "context_score.json") or {}
    )
    support_throttle = _support_throttle(portfolio_mult, news_mult, sizing_mult)
    requested = base_lot * lot_mult * support_throttle
    volume = normalize_volume(
        requested,
        minimum=float(si.volume_min),
        maximum=min(float(si.volume_max), float(spec["max_lot"]) * 3.0),
        step=float(si.volume_step or 0.01),
    )
    base["lot_mult"] = round(lot_mult, 2)
    base["portfolio_mult"] = round(portfolio_mult, 3)
    base["news_mult"] = round(news_mult, 2)
    base["context_mult"] = round(sizing_mult, 4)
    base["support_throttle"] = round(support_throttle, 4)
    base["context_fresh"] = context_fresh
    base["sizing_reason"] = sizing_reason
    base["news_sentiment"] = round(sent, 3)
    base["news_headline"] = sent_head
    base["volume_used"] = volume
    base["magic"] = spec["magic"]
    base["context_score"] = context.get("trading_context_score", 0)
    if volume <= 0:
        append_event(
            name,
            {
                **base,
                "event": "skip",
                "reason": f"risk-adjusted volume {requested:.5f} below broker minimum {si.volume_min}",
            },
        )
        return
    if volume > spec["max_lot"] * 5.0 + 1e-9:
        append_event(name, {**base, "event": "abort", "reason": f"volume {volume} > hard 5x cap"})
        return
    price = tick.ask if side == "long" else tick.bid
    # $ → price distance: $X = X * tick_size / (tick_value * volume)
    sl_dist = spec["sl_usd"] * si.trade_tick_size / (si.trade_tick_value * volume)
    tp_dist = spec["tp_usd"] * si.trade_tick_size / (si.trade_tick_value * volume)
    if side == "long":
        sl = round(price - sl_dist, si.digits)
        tp = round(price + tp_dist, si.digits)
        order_type = mt5.ORDER_TYPE_BUY
    else:
        sl = round(price + sl_dist, si.digits)
        tp = round(price - tp_dist, si.digits)
        order_type = mt5.ORDER_TYPE_SELL

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": spec["symbol"], "volume": volume, "type": order_type,
        "price": price, "sl": sl, "tp": tp, "deviation": 20,
        "magic": spec["magic"], "comment": name.lower(),
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    # Try multiple fill modes (some symbols reject IOC/FOK)
    check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        request["type_filling"] = mt5.ORDER_FILLING_FOK
        check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        request["type_filling"] = mt5.ORDER_FILLING_RETURN
        check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        append_event(name, {**base, "event": "order_check_failed",
                            "check": check._asdict() if check else None, "request": request})
        return

    result = mt5.order_send(request)
    if result is None:
        append_event(name, {**base, "event": "order_send_none", "last_error": mt5.last_error()})
        return
    res_dict = result._asdict()
    if not successful_deal_retcode(
        res_dict.get("retcode"),
        done=mt5.TRADE_RETCODE_DONE,
        done_partial=mt5.TRADE_RETCODE_DONE_PARTIAL,
    ):
        append_event(name, {**base, "event": "order_send_rejected", "request": request, "result": res_dict})
        return
    fill_price = res_dict.get("price", request["price"])
    slippage_pts = adverse_slippage_points(side, request["price"], fill_price, si.point)
    # Partial fill: order requested volume > what was actually filled
    filled_vol = res_dict.get("volume", request["volume"])
    partial_fill = filled_vol < (request["volume"] - 1e-9)
    append_event(name, {**base, "event": "live_order_sent", "request": request,
                        "result": res_dict, "slippage_points": round(slippage_pts, 4), "partial_fill": partial_fill})


def live_exit(name: str, spec: dict) -> None:
    now = datetime.now(tz=timezone.utc)
    si = mt5.symbol_info(spec["symbol"])
    tick = mt5.symbol_info_tick(spec["symbol"])
    positions = open_positions_for_magic(spec["magic"])
    if not positions:
        append_event(name, {"event": "exit_skip", "ts": now.isoformat(),
                            "reason": "no open position on magic"})
        return
    for p in positions:
        is_buy = p.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": spec["symbol"], "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "position": p.ticket, "price": tick.bid if is_buy else tick.ask,
            "deviation": 20, "magic": spec["magic"], "comment": name.lower() + "_exit",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
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
            append_event(name, {
                "event": "live_exit_rejected",
                "ts": now.isoformat(),
                "symbol": spec["symbol"],
                "magic": spec["magic"],
                "ticket": p.ticket,
                "request": request,
                "result": res,
            })
            continue
        fill_price = res.get("price", request["price"]) if result else request["price"]
        close_side = "sell" if is_buy else "buy"
        slippage_pts = adverse_slippage_points(close_side, request["price"], fill_price, si.point)
        # Partial fill: order requested volume > what was actually filled
        filled_vol = res.get("volume", request["volume"]) if result else request["volume"]
        partial_fill = filled_vol < (request["volume"] - 1e-9)
        append_event(name, {"event": "live_exit_close", "ts": now.isoformat(),
                            "symbol": spec["symbol"], "magic": spec["magic"],
                            "ticket": p.ticket, "volume": p.volume,
                            "entry_price": p.price_open, "exit_price": request["price"],
                            "result": res, "slippage_points": round(slippage_pts, 4), "partial_fill": partial_fill})


def live_status(name: str, spec: dict) -> None:
    now = datetime.now(tz=timezone.utc)
    si = mt5.symbol_info(spec["symbol"])
    tick = mt5.symbol_info_tick(spec["symbol"])
    regime_on, wr, n = compute_regime_state(spec)
    positions = open_positions_for_magic(spec["magic"])
    daily = today_realized_pnl_usd(spec["magic"], tick, host_utc=now)
    news_block = load_news_blackout(spec["symbol"])
    clock = feed_clock_provenance(tick, host_utc=now)
    server_now = clock.feed_time if clock is not None else None
    print(f"=== {name} — {spec['description']} ===")
    print(f"  Mode: {'LIVE' if is_live() else 'PAPER (env not set)'}")
    print(f"  Host UTC: {now.strftime('%Y-%m-%d %H:%M')}  feed: {server_now.strftime('%Y-%m-%d %H:%M') if server_now else 'unavailable'}")
    print(f"  Feed clock coherent: {clock.coherent if clock is not None else False}")
    print(f"  Required broker window: weekday {spec['weekday']}, hour {spec['entry_hour']}")
    print(f"  Regime: {'ON' if regime_on else 'OFF'}  (wr {100*wr:.1f}% over {n} signals)")
    print(f"  Positions: {len(positions)}  Daily realized: ${daily:+.2f} / -${MAX_DAILY_LOSS_USD:.0f} kill")
    print(f"  News blackout: {news_block or 'none'}")
    print(f"  Market: {spec['symbol']} bid={tick.bid} ask={tick.ask} spread={si.spread}pts")
    for p in positions:
        print(f"    open ticket={p.ticket} vol={p.volume} entry={p.price_open} unrealized=${p.profit:+.2f}")


def main():
    if len(sys.argv) < 3:
        print("usage: multi_drift_live.py <signal> <live-enter|live-exit|live-status>")
        print(f"  signals: {list(SIGNALS.keys())}")
        sys.exit(1)
    name = sys.argv[1].upper()
    mode = sys.argv[2]
    if name not in SIGNALS:
        print(f"unknown signal {name}; available: {list(SIGNALS.keys())}")
        sys.exit(1)
    spec = SIGNALS[name]
    init_mt5()
    try:
        if mode == "live-enter":
            live_enter(name, spec, force="--force" in sys.argv)
        elif mode == "live-exit":
            live_exit(name, spec)
        elif mode == "live-status":
            live_status(name, spec)
        else:
            print(f"unknown mode {mode}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
