"""Lightweight pattern learner — after each closed trade, extract features about WHEN/HOW it
was opened and find which features predict winning trades. No ML library required; uses
deterministic statistical tests (mean comparison, t-stat) so results are reproducible.

For each closed trade we know:
  - symbol, magic, side, vol, entry_price, exit_price, net P/L
  - entry timestamp (UTC weekday, hour-of-day)
  - duration (minutes from open to close)
We optionally enrich with snapshot features from the moment of entry:
  - GOLD spread at entry (from H1 bar)
  - news sentiment at entry (from news_state.json snapshots saved hourly)

For each FEATURE bucket, we compute mean P/L and t-stat-vs-zero. Buckets with t > 2 or t < -2
are flagged as "real" patterns worth filtering on. Output:
  - data_cache/learned_patterns.json
  - Console: top discoveries

This is intentionally simple — we don't fit a complex model on 60 trades. We just identify
which obvious slicings beat random.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import (
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)

OUT = Path(r"C:\Users\swann\OneDrive - BDA\trading-agent\data_cache\learned_patterns.json")


def t_stat(values: list[float]) -> tuple[float, float]:
    """Returns (mean, t-stat-vs-zero)."""
    n = len(values)
    if n < 2:
        return (sum(values), 0.0)
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0
    return (m, t)


def main():
    if not mt5.initialize():
        print(f"mt5.initialize failed: {mt5.last_error()}", file=sys.stderr); sys.exit(1)
    try:
        # Pull all closed trades
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
        by_pos: dict[int, list] = defaultdict(list)
        for d in deals:
            by_pos[d.position_id].append(d)

        trades = []
        for pos_id, ds in by_pos.items():
            ds = sorted(ds, key=lambda x: x.time)
            if len(ds) < 2:
                continue
            entry, exit_ = ds[0], ds[-1]
            entry_ts = datetime.fromtimestamp(entry.time, tz=timezone.utc)
            exit_ts = datetime.fromtimestamp(exit_.time, tz=timezone.utc)
            net = sum(
                d.profit + d.commission + d.swap + float(getattr(d, "fee", 0.0) or 0.0)
                for d in ds
            )
            trades.append({
                "symbol": entry.symbol, "magic": entry.magic,
                "side": "long" if entry.type == 0 else "short", "vol": entry.volume,
                "entry_ts": entry_ts, "exit_ts": exit_ts,
                "entry_price": entry.price, "exit_price": exit_.price,
                "net": net,
                "duration_min": (exit_ts - entry_ts).total_seconds() / 60,
                "entry_hour": entry_ts.hour, "entry_weekday": entry_ts.weekday(),
                "entry_minute": entry_ts.minute,
            })

        if len(trades) < 10:
            print(f"only {len(trades)} closed trades — need at least 10 for pattern learning")
            return

        nets = [t["net"] for t in trades]
        overall_m, overall_t = t_stat(nets)
        print(f"=== OVERALL ({len(trades)} trades) ===")
        print(f"  mean ${overall_m:+.2f}/trade, t-stat {overall_t:+.2f}\n")

        # Slice 1: hour of entry
        print("=== Pattern: by entry hour-of-day (UTC) ===")
        by_hour = defaultdict(list)
        for t in trades:
            by_hour[t["entry_hour"]].append(t["net"])
        for hr in sorted(by_hour):
            vs = by_hour[hr]
            if len(vs) < 3:
                continue
            m, ts = t_stat(vs)
            flag = " <-- SIGNIFICANT" if abs(ts) >= 2.0 else ""
            print(f"  hr {hr:02d}:00  n={len(vs):3d}  mean=${m:+6.2f}/trade  t={ts:+5.2f}{flag}")

        # Slice 2: weekday
        print("\n=== Pattern: by weekday ===")
        wd_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        by_wd = defaultdict(list)
        for t in trades:
            by_wd[t["entry_weekday"]].append(t["net"])
        for wd in sorted(by_wd):
            vs = by_wd[wd]
            if len(vs) < 3:
                continue
            m, ts = t_stat(vs)
            flag = " <-- SIGNIFICANT" if abs(ts) >= 2.0 else ""
            print(f"  {wd_names[wd]:3s}  n={len(vs):3d}  mean=${m:+6.2f}/trade  t={ts:+5.2f}{flag}")

        # Slice 3: side (long vs short)
        print("\n=== Pattern: by side ===")
        by_side = defaultdict(list)
        for t in trades:
            by_side[t["side"]].append(t["net"])
        for side, vs in by_side.items():
            m, ts = t_stat(vs)
            flag = " <-- SIGNIFICANT" if abs(ts) >= 2.0 else ""
            print(f"  {side:5s}  n={len(vs):3d}  mean=${m:+6.2f}/trade  t={ts:+5.2f}{flag}")

        # Slice 4: duration buckets
        print("\n=== Pattern: by hold duration ===")
        dur_buckets = [("<10m", 0, 10), ("10-30m", 10, 30), ("30m-1h", 30, 60),
                       ("1-6h", 60, 360), ("6-24h", 360, 1440), (">24h", 1440, 99999)]
        for label, lo, hi in dur_buckets:
            vs = [t["net"] for t in trades if lo <= t["duration_min"] < hi]
            if len(vs) < 3:
                continue
            m, ts = t_stat(vs)
            flag = " <-- SIGNIFICANT" if abs(ts) >= 2.0 else ""
            print(f"  {label:8s} n={len(vs):3d}  mean=${m:+6.2f}/trade  t={ts:+5.2f}{flag}")

        # Slice 5: per-symbol
        print("\n=== Pattern: by symbol ===")
        by_sym = defaultdict(list)
        for t in trades:
            by_sym[t["symbol"]].append(t["net"])
        for sym, vs in sorted(by_sym.items(), key=lambda x: -sum(x[1])):
            if len(vs) < 3:
                continue
            m, ts = t_stat(vs)
            wr = sum(1 for x in vs if x > 0) / len(vs) * 100
            flag = " <-- SIGNIFICANT" if abs(ts) >= 2.0 else ""
            print(f"  {sym:9s} n={len(vs):3d}  win={wr:5.1f}%  mean=${m:+6.2f}  total=${sum(vs):+7.2f}  t={ts:+5.2f}{flag}")

        # Persist top discoveries
        discoveries: list[dict] = []
        for slicer_name, bucket_map in [("hour", by_hour), ("weekday", by_wd), ("symbol", by_sym), ("side", by_side)]:
            for key, vs in bucket_map.items():
                if len(vs) < 3:
                    continue
                m, ts = t_stat(vs)
                if abs(ts) >= 1.5:
                    discoveries.append({
                        "slicer": slicer_name, "key": str(key), "n": len(vs),
                        "mean_per_trade": round(m, 3), "t_stat": round(ts, 2),
                        "total_net": round(sum(vs), 2),
                    })
        discoveries.sort(key=lambda x: -abs(x["t_stat"]))
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "n_trades_analyzed": len(trades),
            "overall_mean": round(overall_m, 3),
            "overall_t_stat": round(overall_t, 2),
            "discoveries": discoveries[:30],
        }, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote {OUT}")
        print(f"Top discoveries (|t|>=1.5):")
        for d in discoveries[:10]:
            print(f"  {d['slicer']}={d['key']}: n={d['n']}, mean=${d['mean_per_trade']:+.2f}, t={d['t_stat']:+.2f}, net=${d['total_net']:+.2f}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
