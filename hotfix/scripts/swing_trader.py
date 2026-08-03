"""Long-term swing/position trader. Holds for WEEKS not hours.

Reads top momentum candidates from data_cache/stock_momentum.json (produced by
stock_momentum.py weekly screener) and either:
  - swing-status  : show current positions + ranked top picks
  - swing-rebalance: open positions on top N candidates, close any in current
                    portfolio that have fallen out of top-2N (rotation rule)

Risk model — completely different from the intraday calendar edges:
  - Position size = MIN_LOT (don't try to scale on swing trades; concentration
    risk is too high for a $700 account)
  - Stop loss: -10% from entry (much wider than intraday $30 stops because
    daily vol on stocks is 1-3%)
  - Take profit: NONE (run with the momentum, exit only on rotation or stop)
  - Hard cap: max 5 concurrent swing positions
  - Magic 99000-99999 (separate from intraday calendar magics 88001-88006
    and codex autonomous 26060xxx)

Schedule: run swing-rebalance weekly on Sunday, after the stock_momentum.py
screener has updated. Manual review encouraged before scheduling automated
execution since this can hold real money for weeks.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import persistent_user_flag_enabled

MOMENTUM_FILE = Path(r"C:\Users\swann\OneDrive - BDA\trading-agent\data_cache\stock_momentum.json")
LOG_DIR = Path(r"C:\mt5-paper\swing")
LIVE_ENV_FLAG = "MT5_SWING_LIVE"  # SEPARATE arming flag from intraday
TOP_N = 5
STOP_LOSS_PCT = 0.10
MAGIC_BASE = 99000


def is_live() -> bool:
    return persistent_user_flag_enabled(LIVE_ENV_FLAG)


def magic_for(ticker: str) -> int:
    # Deterministic magic from ticker hash; collisions are fine since we always filter by symbol+magic-range
    return MAGIC_BASE + (abs(hash(ticker)) % 1000)


def append_event(event: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    event["ts"] = datetime.now(tz=timezone.utc).isoformat()
    with (LOG_DIR / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, indent=2, default=str))


def open_swing_positions() -> list:
    positions = mt5.positions_get() or []
    return [p for p in positions if MAGIC_BASE <= p.magic < MAGIC_BASE + 1000]


def top_candidates(limit: int = TOP_N) -> list[dict]:
    if not MOMENTUM_FILE.exists():
        return []
    try:
        payload = json.loads(MOMENTUM_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload.get("top", [])[:limit]


def swing_status():
    candidates = top_candidates()
    current = open_swing_positions()
    ai = mt5.account_info()
    print(f"=== SWING TRADER STATUS @ {datetime.now(tz=timezone.utc).isoformat()} ===")
    print(f"  Mode: {'LIVE' if is_live() else 'PAPER (env not set)'}")
    print(f"  Account equity: ${ai.equity:.2f}  balance: ${ai.balance:.2f}")
    print(f"  Open swing positions: {len(current)} (max 5)")
    for p in current:
        pct = (p.price_current / p.price_open - 1) * 100 if p.type == 0 else (1 - p.price_current / p.price_open) * 100
        print(f"    {p.symbol:18s} vol={p.volume} entry={p.price_open:.2f} now={p.price_current:.2f} "
              f"({pct:+.1f}%) profit=${p.profit:+.2f}  magic={p.magic}")
    print(f"\n  Current top {TOP_N} momentum candidates (from data_cache/stock_momentum.json):")
    if not candidates:
        print("    no candidates yet — run scripts/stock_momentum.py first")
    for i, c in enumerate(candidates, 1):
        in_portfolio = any(p.symbol == c["ticker"] for p in current)
        flag = " [HOLDING]" if in_portfolio else ""
        print(f"    {i}. {c['ticker']:18s} 6mo {c['ret_6mo_pct']:+6.1f}%  quality {c['momentum_quality']:5.2f}  spread {c['spread_pct_price']:.2f}%{flag}")


def swing_rebalance():
    """Open positions on top N candidates not already held; close positions that
    have fallen out of top 2N (rotation rule)."""
    candidates = top_candidates(TOP_N)
    if not candidates:
        append_event({"event": "rebalance_aborted", "reason": "no candidates"})
        return
    candidate_tickers = {c["ticker"] for c in candidates}
    rotation_keep = {c["ticker"] for c in top_candidates(TOP_N * 2)}
    current = open_swing_positions()
    live = is_live()

    # 1) CLOSE any current position not in the wider rotation set
    for p in current:
        if p.symbol not in rotation_keep:
            tick = mt5.symbol_info_tick(p.symbol)
            if not tick:
                continue
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
                "type": mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
                "position": p.ticket,
                "price": tick.bid if p.type == 0 else tick.ask,
                "deviation": 50, "magic": p.magic,
                "comment": "swing_rotate_out",
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
            }
            if not live:
                append_event({"event": "paper_close_rotation", "symbol": p.symbol, "ticket": p.ticket,
                              "reason": f"{p.symbol} fell out of top {TOP_N * 2} rotation set"})
                continue
            r = mt5.order_send(req)
            if not r:
                req["type_filling"] = mt5.ORDER_FILLING_FOK; r = mt5.order_send(req)
            append_event({"event": "rotation_close", "symbol": p.symbol,
                          "result": r._asdict() if r else {"err": str(mt5.last_error())}})

    # 2) OPEN positions on top N not already held
    current_symbols = {p.symbol for p in open_swing_positions()}  # reload after closes
    if len(current_symbols) >= TOP_N:
        append_event({"event": "rebalance_skip_full", "n_held": len(current_symbols), "max": TOP_N})
        return
    for c in candidates:
        if c["ticker"] in current_symbols:
            continue
        if len(current_symbols) >= TOP_N:
            break
        si = mt5.symbol_info(c["ticker"])
        tick = mt5.symbol_info_tick(c["ticker"])
        if not si or not tick:
            continue
        if not si.visible:
            mt5.symbol_select(c["ticker"], True)
            si = mt5.symbol_info(c["ticker"])
        volume = si.volume_min
        price = tick.ask
        sl = round(price * (1 - STOP_LOSS_PCT), si.digits)
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": c["ticker"], "volume": volume,
            "type": mt5.ORDER_TYPE_BUY, "price": price, "sl": sl, "tp": 0,
            "deviation": 100, "magic": magic_for(c["ticker"]),
            "comment": "swing_momentum",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if not live:
            append_event({"event": "paper_open", "ticker": c["ticker"],
                          "would_buy_vol": volume, "price": price, "sl": sl,
                          "reason": f"top {TOP_N} momentum, 6mo +{c['ret_6mo_pct']:.1f}%"})
            current_symbols.add(c["ticker"])
            continue
        # Try multiple fill modes
        check = mt5.order_check(req)
        if not check or check.retcode != 0:
            req["type_filling"] = mt5.ORDER_FILLING_FOK; check = mt5.order_check(req)
        if not check or check.retcode != 0:
            req["type_filling"] = mt5.ORDER_FILLING_RETURN; check = mt5.order_check(req)
        if not check or check.retcode != 0:
            append_event({"event": "swing_open_check_failed", "ticker": c["ticker"],
                          "check": check._asdict() if check else None})
            continue
        result = mt5.order_send(req)
        append_event({"event": "swing_open", "ticker": c["ticker"], "vol": volume,
                      "price": price, "sl": sl,
                      "result": result._asdict() if result else {"err": str(mt5.last_error())}})
        if result and result.retcode == 10009:
            current_symbols.add(c["ticker"])


def main():
    if not mt5.initialize():
        print(f"mt5.initialize failed: {mt5.last_error()}", file=sys.stderr); sys.exit(1)
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else "swing-status"
        if mode == "swing-status":
            swing_status()
        elif mode == "swing-rebalance":
            swing_rebalance()
        else:
            print(f"unknown mode: {mode}"); sys.exit(1)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
