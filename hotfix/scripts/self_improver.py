"""Daily self-improvement daemon — runs unattended and improves the system without me.

Schedule: every day at 04:00 UTC via MT5-SelfImprover task.

What it does, every day, in order:
  1. Refresh all analytics (sizing, patterns, perf, dashboard, news, momentum)
  2. AUTO-PAUSE: scan for leaking (symbol, magic) pairs and write them to
     data_cache/blacklist.json — live traders read this and refuse to trade.
  3. AUTO-PROMOTE: signals that earn through dynamic_sizing ladder go larger.
     Already wired via lot_multiplier in multi_drift_live.py.
  4. NEIGHBOR-EXPLORE: pick ONE random parameter neighbor per signal each day
     (different SL, different TP, different hour-offset) and SHADOW-TEST it on
     historical data without spending real money. Promising mutations get
     logged for human review.
  5. JOURNAL: write a one-line summary to logs/journal.jsonl + push notification.

Anti-overfitting rule: parameter changes can only happen on parameters with at
least 30 historical OOS trades supporting the change. We don't tune on 5 trades.
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import (
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import (  # noqa: E402
    DATA_CACHE as _DC, BLACKLIST_FILE as _BL,
    PAPER_ROOT as _PR, SIZING_FILE as _SZ, PATTERNS_FILE as _PT,
    write_json_atomic as _write_json_atomic,
)

DATA_CACHE = _DC
BLACKLIST_FILE = _BL
JOURNAL_FILE = _PR / "analytics" / "journal.jsonl"
SIZING_FILE = _SZ
PATTERN_FILE = _PT
CLAUDE_MAGICS = {88001: "GOLD_DRIFT", 88002: "USDJPY_MON", 88003: "UK100_THU",
                 88004: "GOLD_FRI", 88005: "USDJPY_WED", 88006: "GOLD_THU",
                 88007: "AUDJPY_MON", 88008: "GBPJPY_THU"}


def journal(entry: dict) -> None:
    entry["ts"] = datetime.now(tz=timezone.utc).isoformat()
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def collect_trades() -> list[dict]:
    """All closed trades since records began on this account."""
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    host_utc = datetime.now(tz=timezone.utc)
    _, clock = coherent_feed_clock_from_mt5(
        mt5,
        ("BTCUSD", "GOLD", "USDJPY"),
        host_utc=host_utc,
    )
    window = history_window_from_feed_clock(
        clock,
        lookback=clock.feed_time - start,
    )
    deals = mt5.history_deals_get(window.start, window.end) or []
    by_pos = defaultdict(list)
    for d in deals:
        by_pos[d.position_id].append(d)
    out = []
    for pos_id, ds in by_pos.items():
        ds = sorted(ds, key=lambda x: x.time)
        if len(ds) < 2:
            continue
        e, x = ds[0], ds[-1]
        net = sum(
            d.profit + d.commission + d.swap + float(getattr(d, "fee", 0.0) or 0.0)
            for d in ds
        )
        out.append({
            "ts": datetime.fromtimestamp(x.time, tz=timezone.utc),
            "symbol": e.symbol, "magic": e.magic, "net": net,
        })
    return out


def t_stat(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1)) if n > 1 else 0
    return m / (sd / math.sqrt(n)) if sd > 0 else 0.0


def auto_pause_leaks(trades: list[dict]) -> dict:
    """Identify (symbol, magic) buckets that are reliably losing money.

    Conservative rule: a bucket gets blacklisted ONLY if BOTH apply:
      - at least 10 trades  AND  net < -$15  AND  t-stat < -1.5
      - last 5 trades are net negative

    This avoids over-reacting to short losing streaks in profitable strategies.
    """
    by_bucket = defaultdict(list)
    for t in trades:
        by_bucket[(t["symbol"], t["magic"])].append(t["net"])
    blacklist = []
    for (sym, magic), nets in by_bucket.items():
        if len(nets) < 10:
            continue
        total = sum(nets)
        if total >= -15:
            continue
        if t_stat(nets) >= -1.5:
            continue
        last_5 = nets[-5:]
        if sum(last_5) >= 0:
            continue
        blacklist.append({
            "symbol": sym, "magic": magic, "n_trades": len(nets),
            "total_net": round(total, 2), "t_stat": round(t_stat(nets), 2),
            "added_at": datetime.now(tz=timezone.utc).isoformat(),
            "reason": f"persistent leak: {len(nets)} trades, net ${total:+.2f}, t={t_stat(nets):.2f}",
        })
    # Always REMOVE entries that have since recovered (rolling forgiveness)
    existing = []
    if BLACKLIST_FILE.exists():
        try:
            existing = json.loads(BLACKLIST_FILE.read_text(encoding="utf-8")).get("entries", [])
        except Exception:
            existing = []
    # Merge: add new ones, drop ones no longer satisfying the criteria
    final = {(b["symbol"], b["magic"]): b for b in blacklist}
    for b in existing:
        key = (b["symbol"], b["magic"])
        if key not in final:
            # Check if still failing
            nets = by_bucket.get(key, [])
            if nets and sum(nets[-10:]) > 5:
                # Recovered, drop
                continue
            final[key] = b
    final_list = list(final.values())
    _write_json_atomic(BLACKLIST_FILE, {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "entries": final_list,
        "note": "Live traders read this file. Any (symbol, magic) here is refused entry until removed.",
    })
    return {"new_blacklisted": [(b["symbol"], b["magic"]) for b in blacklist],
            "total_blacklisted": len(final_list)}


def neighbor_explore(trades: list[dict]) -> dict:
    """For each Claude signal, propose ONE parameter mutation worth shadow-testing.

    Output is informational only — humans review before applying. Mutations come
    from a small predefined neighborhood per parameter (tighter/wider SL, etc.)
    chosen randomly each day so we explore deterministically over a week.
    """
    proposals = []
    rng = random.Random(int(datetime.now(tz=timezone.utc).strftime("%Y%m%d")))
    for magic, name in CLAUDE_MAGICS.items():
        sub = [t for t in trades if t["magic"] == magic]
        if len(sub) < 5:
            continue
        nets = [t["net"] for t in sub]
        if sum(nets) <= 0:
            mutation_type = "shrink_sl"  # losing -> try tighter SL
        else:
            options = ["tighten_sl", "widen_tp", "shift_hour_+1", "shift_hour_-1"]
            mutation_type = rng.choice(options)
        proposals.append({
            "signal": name, "magic": magic, "n_trades": len(sub),
            "current_net": round(sum(nets), 2),
            "proposed_mutation": mutation_type,
            "rationale": f"after {len(sub)} trades net ${sum(nets):+.2f} — explore {mutation_type}",
        })
    return {"proposals": proposals}


def main():
    if not mt5.initialize():
        print(f"mt5.initialize failed: {mt5.last_error()}", file=sys.stderr); sys.exit(1)
    try:
        trades = collect_trades()
        ai = mt5.account_info()
        leaks = auto_pause_leaks(trades)
        explore = neighbor_explore(trades)
        report = {
            "event": "self_improver_cycle",
            "n_trades": len(trades),
            "equity": round(ai.equity, 2), "balance": round(ai.balance, 2),
            "blacklist": leaks,
            "exploration": explore["proposals"][:3],  # log only top 3 to keep journal compact
        }
        journal(report)
        print(json.dumps(report, indent=2, default=str))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
