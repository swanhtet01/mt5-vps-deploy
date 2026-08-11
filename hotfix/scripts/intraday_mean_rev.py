"""Intraday RSI mean-reversion trader — GOLD and USDJPY.

Runs every 30 minutes during London (07:00-11:30 UTC) and NY (13:00-17:30 UTC) sessions.
Uses H1 RSI(14) as the primary signal: RSI < 35 -> BUY (oversold fade), RSI > 65 -> SELL.
ATR filter prevents trading during extreme volatility or dead markets.
Timed exit: positions held max 2 hours, then closed regardless of P&L.

This strategy trades MORE FREQUENTLY than the structural weekly edges, providing
active intraday participation without touching the validated structural positions.

Safety guardrails:
  - Uses MT5_GOLD_DRIFT_LIVE env flag (same as structural edges)
  - Daily loss limit per magic: $30
  - Max 1 open position per magic at a time
  - Hard SL $45 / TP $30 (tighter than structural — stop is real, not just safety net)
  - Regime gate: RSI extreme + ATR in normal band + session filter

Magics: 88011 (GOLD mean-rev), 88012 (USDJPY mean-rev)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import (
    adverse_slippage_points,
    normalize_volume,
    persistent_user_flag_enabled,
    successful_deal_retcode,
)

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import (  # noqa: E402
    DATA_CACHE as _DATA_CACHE,
    PAPER_ROOT as _PAPER_ROOT,
    NEWS_STATE_FILE as _NEWS_STATE_FILE,
    read_json as _read_json,
)

LIVE_ENV_FLAG = "MT5_GOLD_DRIFT_LIVE"
LOG_DIR = _PAPER_ROOT / "intraday-mr"

SIGNALS = [
    dict(symbol="GOLD",   magic=88011, side_bias="both", max_lot=0.03,
         sl_usd=45.0, tp_usd=30.0, daily_loss_limit=-30.0,
         rsi_buy=35, rsi_sell=65, min_atr_pct=0.03, max_atr_pct=1.2,
         description="GOLD H1 RSI mean-reversion 0.03 lot"),
    dict(symbol="USDJPY", magic=88012, side_bias="both", max_lot=0.05,
         sl_usd=37.5, tp_usd=25.0, daily_loss_limit=-25.0,
         rsi_buy=35, rsi_sell=65, min_atr_pct=0.02, max_atr_pct=0.8,
         description="USDJPY H1 RSI mean-reversion 0.05 lot"),
]

LONDON_OPEN_UTC  = (7,   0)
LONDON_CLOSE_UTC = (11, 30)
NY_OPEN_UTC      = (13,  0)
NY_CLOSE_UTC     = (17, 30)

MAX_HOLD_HOURS = 2


def _log(name: str, event: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    event["signal"] = name
    with (LOG_DIR / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, indent=2, default=str))


def _is_live() -> bool:
    return persistent_user_flag_enabled(LIVE_ENV_FLAG)


def _in_session(utc_now: datetime) -> bool:
    h, m = utc_now.hour, utc_now.minute
    mins = h * 60 + m
    london = LONDON_OPEN_UTC[0] * 60 + LONDON_OPEN_UTC[1]
    london_end = LONDON_CLOSE_UTC[0] * 60 + LONDON_CLOSE_UTC[1]
    ny = NY_OPEN_UTC[0] * 60 + NY_OPEN_UTC[1]
    ny_end = NY_CLOSE_UTC[0] * 60 + NY_CLOSE_UTC[1]
    return (london <= mins < london_end) or (ny <= mins < ny_end)


def _compute_rsi_atr(symbol: str, period: int = 14) -> tuple[float, float, float]:
    bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, period + 2)
    if bars is None or len(bars) < period + 1:
        return float("nan"), float("nan"), float("nan")

    closes = [float(b["close"]) for b in bars]
    highs  = [float(b["high"])  for b in bars]
    lows   = [float(b["low"])   for b in bars]

    # RSI
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    # ATR
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[-period:]) / period
    last_close = closes[-1]
    return rsi, atr, last_close


def _news_blackout(symbol: str) -> str:
    state = _read_json(_NEWS_STATE_FILE) or {}
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
    return f"news blackout until {bo}"


def _today_pnl(magic: int, symbol: str) -> float:
    now_utc = datetime.now(tz=timezone.utc)
    start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(start, now_utc) or []
    return sum(d.profit + d.commission + d.swap for d in deals if d.magic == magic)


def _open_positions(magic: int) -> list:
    return [p for p in (mt5.positions_get() or []) if p.magic == magic]


def _close_position(pos, symbol: str, name: str, reason: str) -> None:
    now = datetime.now(tz=timezone.utc)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        _log(name, {"event": "exit_skip", "ts": now.isoformat(), "reason": "no tick"})
        return
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": pos.ticket, "price": tick.bid if is_buy else tick.ask,
        "deviation": 20, "magic": pos.magic, "comment": f"mr_exit:{reason[:20]}",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(req)
    if result is None or result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
        req["type_filling"] = mt5.ORDER_FILLING_FOK
        result = mt5.order_send(req)
    if result is None or result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
        req["type_filling"] = mt5.ORDER_FILLING_RETURN
        result = mt5.order_send(req)
    res = result._asdict() if result else {}
    _log(pos.symbol, {"event": "exit", "ts": now.isoformat(), "reason": reason, "result": res})


def run_symbol(spec: dict, utc_now: datetime) -> None:
    symbol  = spec["symbol"]
    magic   = spec["magic"]
    name    = symbol

    si   = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if si is None or tick is None:
        return

    # --- 1. Manage existing position: time exit ---
    for pos in _open_positions(magic):
        open_time = datetime.fromtimestamp(pos.time, tz=timezone.utc)
        age_h = (utc_now - open_time).total_seconds() / 3600.0
        if age_h >= MAX_HOLD_HOURS:
            _close_position(pos, symbol, name, f"time_exit_{age_h:.1f}h")
    if _open_positions(magic):
        return  # still holding, skip entry logic

    # --- 2. Entry logic ---
    if not _in_session(utc_now):
        return

    nb = _news_blackout(symbol)
    if nb:
        _log(name, {"event": "skip", "ts": utc_now.isoformat(), "reason": nb})
        return

    pnl_today = _today_pnl(magic, symbol)
    if pnl_today <= spec["daily_loss_limit"]:
        _log(name, {"event": "skip", "ts": utc_now.isoformat(),
                    "reason": f"daily loss limit hit {pnl_today:.2f}"})
        return

    rsi, atr, last_close = _compute_rsi_atr(symbol)
    if any(v != v for v in (rsi, atr, last_close)) or last_close <= 0:
        return

    atr_pct = (atr / last_close) * 100.0
    if atr_pct < spec["min_atr_pct"] or atr_pct > spec["max_atr_pct"]:
        _log(name, {"event": "skip", "ts": utc_now.isoformat(),
                    "reason": f"ATR% {atr_pct:.3f} outside [{spec['min_atr_pct']},{spec['max_atr_pct']}]"})
        return

    side = None
    if rsi < spec["rsi_buy"]:
        side = "long"
    elif rsi > spec["rsi_sell"]:
        side = "short"
    if side is None:
        return

    live = _is_live()
    base = {
        "event": "enter_eval", "ts": utc_now.isoformat(), "symbol": symbol,
        "side": side, "rsi": round(rsi, 1), "atr_pct": round(atr_pct, 3),
        "mode": "live" if live else "paper-only",
    }

    if not live:
        _log(name, {**base, "event": "paper_enter", "reason": f"env {LIVE_ENV_FLAG}=1 not set"})
        return

    volume = max(spec["max_lot"], float(si.volume_min))
    volume = min(volume, float(si.volume_max))

    price = tick.ask if side == "long" else tick.bid
    sl_dist = spec["sl_usd"] * si.trade_tick_size / (si.trade_tick_value * volume)
    tp_dist = spec["tp_usd"] * si.trade_tick_size / (si.trade_tick_value * volume)
    if side == "long":
        sl = round(price - sl_dist, si.digits)
        tp = round(price + tp_dist, si.digits)
        otype = mt5.ORDER_TYPE_BUY
    else:
        sl = round(price + sl_dist, si.digits)
        tp = round(price - tp_dist, si.digits)
        otype = mt5.ORDER_TYPE_SELL

    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
        "type": otype, "price": price, "sl": sl, "tp": tp, "deviation": 20,
        "magic": magic, "comment": f"mr_{name.lower()[:8]}",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    for fill in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        req["type_filling"] = fill
        chk = mt5.order_check(req)
        if chk is not None and chk.retcode == 0:
            break

    result = mt5.order_send(req)
    if result is None:
        _log(name, {**base, "event": "order_none", "last_error": mt5.last_error()})
        return
    res = result._asdict()
    if not successful_deal_retcode(res.get("retcode"), mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
        _log(name, {**base, "event": "order_rejected", "result": res})
        return

    fill_price = res.get("price", price)
    slip = adverse_slippage_points(side, price, fill_price, si.point)
    _log(name, {**base, "event": "live_order_sent", "volume": volume,
                "price": fill_price, "sl": sl, "tp": tp, "slippage_pts": round(slip, 4)})


def main() -> None:
    if not mt5.initialize():
        print(f"mt5 init failed: {mt5.last_error()}", file=sys.stderr)
        return
    try:
        now = datetime.now(tz=timezone.utc)
        for spec in SIGNALS:
            try:
                run_symbol(spec, now)
            except Exception as exc:
                _log(spec["symbol"], {"event": "error", "ts": now.isoformat(), "error": str(exc)})
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
