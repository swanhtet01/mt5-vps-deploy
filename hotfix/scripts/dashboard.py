"""Live performance dashboard for the GOLD Asian-drift bot.

Reads:
  - C:\\mt5-paper\\gold-drift\\live-events.jsonl (heartbeats, skip reasons, orders)
  - MT5 history_deals for the strategy magic 88001 (actual realized P/L)
  - Current regime state from MT5 H1 history

Prints:
  - Account equity vs. baseline (and since-bot-started change)
  - Trades executed live (count, win rate, realized $)
  - Trades expected-but-skipped (and why)
  - Current regime trajectory (last 10 trailing-60 win rates over time)
  - Recent skip reasons (so you can see if costs/regime is killing entries)
  - 1-line verdict: is the edge holding up vs backtest expectation?
"""

from __future__ import annotations

import json
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import (
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)

MAGIC = 88001
SYM = "GOLD"
LOG = Path(r"C:\mt5-paper\gold-drift\live-events.jsonl")


def load_events() -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    for ln in LOG.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def load_realized_deals():
    """Realized P/L per deal for magic 88001 since bot inception."""
    start = datetime(2026, 6, 18, tzinfo=timezone.utc)  # this branch's start
    host_utc = datetime.now(tz=timezone.utc)
    _, clock = coherent_feed_clock_from_mt5(
        mt5,
        ("BTCUSD", "GOLD", "USDJPY"),
        host_utc=host_utc,
    )
    window = history_window_from_feed_clock(clock, lookback=clock.feed_time - start)
    deals = mt5.history_deals_get(window.start, window.end)
    if deals is None:
        return []
    return [d for d in deals if d.magic == MAGIC]


def regime_trajectory(si, n_points=10):
    """Return a list of (date, win_rate, n) historical regime snapshots — proves the regime
    has been ON consistently, not whipsawing."""
    bars = mt5.copy_rates_from_pos(SYM, mt5.TIMEFRAME_H1, 0, 7200)
    if bars is None:
        return []
    rt_cost = (si.spread * si.point / si.trade_tick_size) * si.trade_tick_value * 0.01 + \
              (3.0 * si.point / si.trade_tick_size) * si.trade_tick_value * 0.01
    history = deque(maxlen=60)
    snapshots = []
    snapshot_every = 30  # every 30 signals = ~6 weeks of weekday trading
    for i in range(1, len(bars)):
        t = datetime.fromtimestamp(int(bars[i]["time"]), tz=timezone.utc)
        if t.hour != 1:
            continue
        pnl = ((float(bars[i]["close"]) - float(bars[i - 1]["close"])) /
               si.trade_tick_size) * si.trade_tick_value * 0.01 - rt_cost
        history.append(pnl)
        if len(history) == 60 and (len(history) % snapshot_every) == 0:
            wins = sum(1 for p in history if p > 0)
            snapshots.append((t.date(), wins / 60, sum(history)))
    return snapshots[-n_points:]


def main():
    if not mt5.initialize():
        raise RuntimeError(mt5.last_error())
    try:
        now = datetime.now(tz=timezone.utc)
        ai = mt5.account_info()
        si = mt5.symbol_info(SYM)
        tick = mt5.symbol_info_tick(SYM)
        events = load_events()
        deals = load_realized_deals()

        print(f"{'=' * 72}")
        print(f"GOLD ASIAN-DRIFT — LIVE DASHBOARD @ {now.isoformat()}")
        print(f"{'=' * 72}")
        print(f"\nACCOUNT")
        print(f"  Equity:  ${ai.equity:.2f}     Balance: ${ai.balance:.2f}     "
              f"Free margin: ${ai.margin_free:.2f}")

        print(f"\nLIVE TRADES EXECUTED (magic {MAGIC})")
        if deals:
            entries = [d for d in deals if d.entry == 0]
            exits = [d for d in deals if d.entry == 1]
            realized = sum(d.profit + d.commission + d.swap for d in exits)
            wins = sum(1 for d in exits if (d.profit + d.commission + d.swap) > 0)
            n = len(exits)
            print(f"  Closed trades: {n}    Wins: {wins} ({100*wins/max(n,1):.1f}%)    "
                  f"Realized: ${realized:+.2f}")
            print(f"  Recent closes:")
            for d in sorted(exits, key=lambda x: x.time)[-5:]:
                ts = datetime.fromtimestamp(d.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                net = d.profit + d.commission + d.swap
                print(f"    [{ts}] vol={d.volume}  net=${net:+.3f}  comment={d.comment}")
        else:
            print(f"  (no live trades yet)")

        print(f"\nSCHEDULED-TASK HEARTBEATS (last 24h)")
        cutoff = now - timedelta(hours=24)
        recent = [e for e in events if datetime.fromisoformat(e["ts"]) >= cutoff]
        hb = [e for e in recent if e.get("event") == "heartbeat"]
        skips = [e for e in recent if e.get("event") == "skip_enter"]
        sent = [e for e in recent if e.get("event") == "live_order_sent"]
        exits = [e for e in recent if e.get("event") == "live_exit_close"]
        print(f"  Heartbeats: {len(hb)}   Skips: {len(skips)}   Orders sent: {len(sent)}   "
              f"Exits: {len(exits)}")
        if skips:
            reasons = Counter(s.get("reason", "")[:60] for s in skips)
            print(f"  Skip reasons:")
            for r, c in reasons.most_common():
                print(f"    {c:3d}x  {r}")
        if sent:
            print(f"  Live orders sent:")
            for o in sent[-5:]:
                req = o.get("request", {})
                res = o.get("result", {})
                print(f"    [{o['ts'][:19]}]  vol={req.get('volume')} price={req.get('price')} "
                      f"sl={req.get('sl')} tp={req.get('tp')} retcode={res.get('retcode')}")

        print(f"\nREGIME TRAJECTORY (last 10 snapshots, 30 signals apart)")
        traj = regime_trajectory(si)
        if traj:
            for d, wr, total in traj:
                bar = "#" * int(wr * 30)
                mark = "ON " if wr >= 0.50 else "OFF"
                print(f"  {d}  {100*wr:5.1f}%  cum_pnl_per_001lot=${total:+6.2f}  [{mark}] {bar}")

        # Current regime
        from collections import deque as _dq
        last_60 = []
        bars = mt5.copy_rates_from_pos(SYM, mt5.TIMEFRAME_H1, 0, 7200)
        if bars is not None:
            rt_cost = (si.spread * si.point / si.trade_tick_size) * si.trade_tick_value * 0.01 + \
                      (3.0 * si.point / si.trade_tick_size) * si.trade_tick_value * 0.01
            for i in range(1, len(bars)):
                t = datetime.fromtimestamp(int(bars[i]["time"]), tz=timezone.utc)
                if t.hour != 1:
                    continue
                last_60.append(((float(bars[i]["close"]) - float(bars[i - 1]["close"])) /
                                si.trade_tick_size) * si.trade_tick_value * 0.01 - rt_cost)
            last_60 = last_60[-60:]

        if last_60:
            wins = sum(1 for p in last_60 if p > 0)
            wr = wins / len(last_60)
            on = wr >= 0.50
            print(f"\nCURRENT REGIME STATE")
            print(f"  Trailing 60 win rate: {100*wr:.1f}%   ({'ON' if on else 'OFF'})")
            print(f"  Trailing 60 cumulative: ${sum(last_60):+.2f} per 0.01 lot")

        print(f"\nCURRENT MARKET")
        print(f"  GOLD bid={tick.bid}  ask={tick.ask}  spread={si.spread}pts")
        print(f"  Cost budget: 80pts spread + 3pt slip = ~$0.83 round-trip per 0.01 lot")
        print(f"  Currently {'WITHIN' if si.spread <= 80 else 'OVER'} cost budget")

        # 1-line verdict
        print(f"\n{'-' * 72}")
        if not deals:
            print("VERDICT: No live trades yet. First trade fires at next 00:00 UTC weekday tick.")
        else:
            exits_d = [d for d in deals if d.entry == 1]
            if not exits_d:
                print("VERDICT: A position is open — exit pending.")
            else:
                n = len(exits_d)
                realized = sum(d.profit + d.commission + d.swap for d in exits_d)
                wins = sum(1 for d in exits_d if (d.profit + d.commission + d.swap) > 0)
                exp_per_trade = 2.0  # backtest expectation
                exp = n * exp_per_trade
                if realized >= exp * 0.5:
                    verdict = "TRACKING BACKTEST"
                elif realized >= 0:
                    verdict = "POSITIVE BUT UNDER EXPECTATION"
                else:
                    verdict = "UNDERPERFORMING — monitor regime"
                print(f"VERDICT: {n} live trades, {100*wins/n:.0f}% win, ${realized:+.2f} realized "
                      f"(expected ~${exp:+.0f}). {verdict}.")
        print(f"{'-' * 72}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
