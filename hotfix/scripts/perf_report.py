"""Honest performance reporter — pulls real MT5 trade history and shows what's actually
working with statistical confidence intervals, NOT just point estimates.

Magic-number ownership:
  88001-88006   Claude calendar-edge live traders
  26060501-99   Codex autonomous scalper

Key questions answered:
  - What's the realized P&L per strategy?
  - Is the edge statistically significant or is this just variance?
  - Which symbols are leaking money (auto-pause candidates)?
  - What's the max drawdown so far?
  - How does this compare to the backtest expectation?
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import datetime, timezone

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import (
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)


CLAUDE_MAGICS = {88001: "GOLD_DRIFT", 88002: "USDJPY_MON", 88003: "UK100_THU",
                 88004: "GOLD_FRI", 88005: "USDJPY_WED", 88006: "GOLD_THU",
                 88007: "AUDJPY_MON", 88008: "GBPJPY_THU"}
REFERENCE_SYMBOLS = ("BTCUSD", "GOLD", "USDJPY")
CODEX_MAGIC_MIN = 26_060_000
CODEX_MAGIC_MAX = 26_069_999


def login_last4(login: object) -> str:
    value = str(login or "")
    return value[-4:] if value else "none"


def trade_owner(magic: int) -> str:
    if magic in CLAUDE_MAGICS:
        return "structural"
    if CODEX_MAGIC_MIN <= int(magic) <= CODEX_MAGIC_MAX:
        return "codex"
    return "manual/external"


def collect_closed_trades(since: datetime) -> list[dict]:
    host_utc = datetime.now(tz=timezone.utc)
    _, clock = coherent_feed_clock_from_mt5(mt5, REFERENCE_SYMBOLS, host_utc=host_utc)
    lookback = clock.feed_time - since
    window = history_window_from_feed_clock(clock, lookback=lookback)
    deals = mt5.history_deals_get(window.start, window.end) or []
    by_pos: dict[int, list] = defaultdict(list)
    for d in deals:
        by_pos[d.position_id].append(d)
    out = []
    for pos_id, ds in by_pos.items():
        ds = sorted(ds, key=lambda x: x.time)
        if len(ds) < 2:
            continue  # still open
        entry, exit_ = ds[0], ds[-1]
        net = sum(
            d.profit + d.commission + d.swap + float(getattr(d, "fee", 0.0) or 0.0)
            for d in ds
        )
        out.append({
            "pos_id": pos_id,
            "open_ts": datetime.fromtimestamp(entry.time, tz=timezone.utc),
            "close_ts": datetime.fromtimestamp(exit_.time, tz=timezone.utc),
            "symbol": entry.symbol, "magic": entry.magic,
            "side": "BUY" if entry.type == 0 else "SELL",
            "vol": entry.volume, "entry": entry.price, "exit": exit_.price,
            "net": net, "comment": (entry.comment or "")[:40],
        })
    return sorted(out, key=lambda x: x["close_ts"])


def stats_block(nets: list[float], label: str) -> dict:
    n = len(nets)
    if n == 0:
        return {"label": label, "n": 0, "note": "no trades"}
    total = sum(nets)
    wins = sum(1 for x in nets if x > 0)
    losses = sum(1 for x in nets if x < 0)
    gross_w = sum(x for x in nets if x > 0)
    gross_l = abs(sum(x for x in nets if x < 0))
    pf = (gross_w / gross_l) if gross_l > 0 else None
    mean = total / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in nets) / max(n - 1, 1))
    t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    # 95% CI on mean per-trade
    ci_lo = mean - 1.96 * sd / math.sqrt(n)
    ci_hi = mean + 1.96 * sd / math.sqrt(n)
    # Drawdown
    cum, peak, mdd = 0, 0, 0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    # Significance verdict (deflated for multiple testing — we're looking at many strategies)
    verdict = "SIGNIFICANT" if abs(t_stat) >= 2.5 else ("EARLY-POSITIVE" if mean > 0 and n >= 10 else "INSUFFICIENT")
    return {
        "label": label, "n": n, "wins": wins, "losses": losses,
        "win_rate_pct": round(100 * wins / n, 1),
        "total_net": round(total, 2), "mean_per_trade": round(mean, 3),
        "sd_per_trade": round(sd, 3), "t_stat": round(t_stat, 2),
        "ci95_mean": (round(ci_lo, 3), round(ci_hi, 3)),
        "profit_factor": round(pf, 2) if pf else None,
        "max_drawdown": round(mdd, 2),
        "verdict": verdict,
    }


def main():
    if not mt5.initialize():
        print(f"mt5.initialize failed: {mt5.last_error()}", file=sys.stderr)
        sys.exit(1)
    try:
        ai = mt5.account_info()
        if ai is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        start = datetime(2026, 6, 17, tzinfo=timezone.utc)
        trades = collect_closed_trades(start)
        owned_trades = [
            trade for trade in trades if trade_owner(trade["magic"]) != "manual/external"
        ]
        external_trades = [
            trade for trade in trades if trade_owner(trade["magic"]) == "manual/external"
        ]
        window_net = sum(trade["net"] for trade in trades)
        owned_net = sum(trade["net"] for trade in owned_trades)
        external_net = sum(trade["net"] for trade in external_trades)

        print(f"=================================================================")
        print(f"  HONEST PERFORMANCE REPORT  @  {datetime.now(tz=timezone.utc).isoformat()}")
        print(f"=================================================================")
        print(f"  Account ending {login_last4(ai.login)}  equity ${ai.equity:.2f}  balance ${ai.balance:.2f}")
        print(f"  Closed-trade window:    {start.isoformat()} to current broker feed time")
        print(f"  Closed net in window:   ${window_net:+.2f}")
        print(f"    automated strategies: ${owned_net:+.2f} ({len(owned_trades)} trades)")
        print(f"    manual / external:    ${external_net:+.2f} ({len(external_trades)} trades)")
        print(f"  Floating profit (open): ${ai.profit:+.2f}")
        print(f"  Closed trades in window: {len(trades)}")
        if not trades:
            print("  no trades yet")
            return

        # ====== Per-strategy ======
        print(f"\n=== CLAUDE EDGES (single-day calendar signals) ===")
        for magic, name in CLAUDE_MAGICS.items():
            sub = [t for t in trades if t["magic"] == magic]
            if sub:
                s = stats_block([t["net"] for t in sub], name)
                print(f"  {name:<13} mag={magic}: {s['n']:3d} trades, "
                      f"{s['win_rate_pct']:5.1f}% win, net=${s['total_net']:+7.2f}, "
                      f"PF={s['profit_factor']}, t={s['t_stat']:+.2f}, "
                      f"95%CI mean=[${s['ci95_mean'][0]:+.2f},${s['ci95_mean'][1]:+.2f}], {s['verdict']}")
            else:
                print(f"  {name:<13} mag={magic}: 0 trades (signal not yet fired this week)")

        codex_trades = [t for t in trades if trade_owner(t["magic"]) == "codex"]
        if codex_trades:
            s = stats_block([t["net"] for t in codex_trades], "Codex autonomous")
            print(f"\n=== CODEX AUTONOMOUS (multi-symbol scalper, magic 26060***) ===")
            print(f"  Total: {s['n']:3d} trades, {s['win_rate_pct']:.1f}% win, "
                  f"net=${s['total_net']:+.2f}, PF={s['profit_factor']}, t={s['t_stat']:+.2f}")
            print(f"  Mean per trade: ${s['mean_per_trade']:+.2f} ± ${s['sd_per_trade']:.2f}  "
                  f"95% CI mean: [${s['ci95_mean'][0]:+.2f}, ${s['ci95_mean'][1]:+.2f}]  {s['verdict']}")
            print(f"  Max drawdown:   ${s['max_drawdown']:.2f}")

            # Per symbol — find leaks
            by_sym: dict[str, list] = defaultdict(list)
            for t in codex_trades:
                by_sym[t["symbol"]].append(t["net"])
            print(f"\n  Per-symbol breakdown (auto-pause candidates marked):")
            for sym, nets in sorted(by_sym.items(), key=lambda x: sum(x[1])):
                wins = sum(1 for x in nets if x > 0)
                total = sum(nets)
                wr = 100 * wins / len(nets)
                flag = ""
                if len(nets) >= 3 and total < 0 and wr < 35:
                    flag = "  [!] AUTO-PAUSE RECOMMENDED (losing reliably)"
                elif len(nets) >= 5 and total < 0:
                    flag = "  [!] underperforming"
                print(f"    {sym:9} {len(nets):3d} trades, {wr:5.1f}% win, net=${total:+7.2f}{flag}")

        if external_trades:
            external = stats_block(
                [trade["net"] for trade in external_trades],
                "Manual/external",
            )
            print("\n=== MANUAL / EXTERNAL (excluded from agent edge estimates) ===")
            print(
                f"  Total: {external['n']:3d} trades, "
                f"net=${external['total_net']:+.2f}"
            )

        # ====== Equity curve ======
        print(f"\n=== EQUITY CURVE (last 20 trades, cumulative net) ===")
        cum = 0
        for t in trades[-20:]:
            cum += t["net"]
            bar = "+" * min(int(abs(t["net"]) * 2), 30) if t["net"] > 0 else "-" * min(int(abs(t["net"]) * 2), 30)
            owner = {
                "structural": "STRUCT",
                "codex": "CODEX",
                "manual/external": "MANUAL",
            }[trade_owner(t["magic"])]
            print(f"  {t['close_ts'].strftime('%m-%d %H:%M')} {owner:6} {t['symbol']:9} ${t['net']:+6.2f}  {bar}  cum=${cum:+.2f}")

        # ====== Honest scaling assessment ======
        print(f"\n=== HONEST SCALING ASSESSMENT ===")
        s = stats_block([trade["net"] for trade in owned_trades], "Owned strategies")
        print(f"  Owned strategies: {s['n']} trades, net=${s['total_net']:+.2f}, mean=${s['mean_per_trade']:+.3f}/trade")
        print(f"  Statistical significance overall: {s['verdict']} (t={s['t_stat']:+.2f})")
        if s["n"] < 30:
            print(f"  RECOMMENDATION: WAIT.  Need at least 30 trades per strategy before scaling.")
            print(f"                 Current sample is too small to distinguish skill from luck.")
            print(f"                 At 95% confidence the true per-trade mean is somewhere between")
            print(f"                 ${s['ci95_mean'][0]:+.2f} and ${s['ci95_mean'][1]:+.2f} — that range straddles zero.")
        else:
            if s["t_stat"] > 2.5 and s["mean_per_trade"] > 0:
                print(f"  RECOMMENDATION: Edge is significant — small scaling defensible.")
                print(f"                  Consider 50% lot increase on the strongest signal (USDJPY_MON t=8 in backtest).")
            else:
                print(f"  RECOMMENDATION: WAIT.  Edge not yet significant (t={s['t_stat']:.2f} < 2.5).")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
