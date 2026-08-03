"""Drift monitor: compare each edge's LIVE results to its registry expectation and flag decay.

Generalizes the proven GOLD trailing-win-rate gate to every magic, via the edge registry's
stored expectations and the consolidated trade_history collector.

HONESTY / FAIL-SAFE DESIGN (the live book is ~empty: n<=2 on one magic, 0 elsewhere):
* INERT below a hard trade floor -- with almost no live data, drift stats are meaningless, so
  the monitor does NOTHING until an edge has enough closed trades. This is the common case for
  MONTHS and is correct, not a bug.
* DEGRADED (win rate materially below expectation) -> recommend WATCH (size-down/review). This
  is the useful side and leads.
* BROKEN (persistently losing) -> recommend BLACKLIST, but CONSERVATIVELY: needs more trades,
  a clearly negative window, AND no recent recovery (forgiveness), because killing a real edge
  on a bad streak is costly.

assess_drift() is pure (trades + registry in, verdicts out) so it is fully unit-tested.
main() pulls live trades and writes an advisory report; it demotes DEGRADED edges to WATCH in
the registry (advisory until live traders read the registry) but never auto-blacklists.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .edge_registry import EdgeRegistry, Stage
from .trade_history import group_by_magic, summarize

MIN_TRADES_DRIFT = 10        # below this -> INERT (not enough live data to judge)
MIN_TRADES_BLACKLIST = 20    # blacklist needs even more evidence
WIN_RATE_MARGIN = 0.15       # how far below expected win rate counts as DEGRADED
BROKEN_WIN_RATE = 0.35       # absolute floor for BROKEN
DEFAULT_EXPECTED_WR = 0.50   # used when the registry has no expectation yet (low confidence)


@dataclass
class DriftVerdict:
    magic: int
    key: str
    n: int
    win_rate: float
    avg_net: float
    net: float
    expected_win_rate: float
    status: str            # INERT / OK / DEGRADED / BROKEN
    action: str            # none / watch / blacklist
    detail: str


def assess_drift(trades_by_magic, registry: EdgeRegistry,
                 min_trades: int = MIN_TRADES_DRIFT,
                 min_trades_blacklist: int = MIN_TRADES_BLACKLIST) -> list[DriftVerdict]:
    verdicts: list[DriftVerdict] = []
    for rec in registry.all():
        trades = list(trades_by_magic.get(rec.magic, []))
        s = summarize(trades)
        n = s["n"]
        exp_wr = rec.expected_win_rate if rec.expected_win_rate is not None else DEFAULT_EXPECTED_WR

        if n < min_trades:
            verdicts.append(DriftVerdict(rec.magic, rec.key, n, s["win_rate"], s["avg_net"],
                                         s["net"], exp_wr, "INERT", "none",
                                         f"only {n} closed trades (<{min_trades}); not enough to judge"))
            continue

        degraded = s["win_rate"] < exp_wr - WIN_RATE_MARGIN
        # recent recovery check (forgiveness): last 5 trades net
        recent = trades[-5:]
        recent_net = sum(t.net for t in recent)
        broken = (n >= min_trades_blacklist and s["net"] < 0
                  and s["win_rate"] < BROKEN_WIN_RATE and recent_net < 0)

        if broken:
            verdicts.append(DriftVerdict(rec.magic, rec.key, n, s["win_rate"], s["avg_net"],
                                         s["net"], exp_wr, "BROKEN", "blacklist",
                                         f"net ${s['net']:.2f} over {n}, win {s['win_rate']:.0%} "
                                         f"(<{BROKEN_WIN_RATE:.0%}), no recent recovery"))
        elif degraded:
            verdicts.append(DriftVerdict(rec.magic, rec.key, n, s["win_rate"], s["avg_net"],
                                         s["net"], exp_wr, "DEGRADED", "watch",
                                         f"win {s['win_rate']:.0%} vs expected {exp_wr:.0%} "
                                         f"(>{WIN_RATE_MARGIN:.0%} below)"))
        else:
            verdicts.append(DriftVerdict(rec.magic, rec.key, n, s["win_rate"], s["avg_net"],
                                         s["net"], exp_wr, "OK", "none",
                                         f"win {s['win_rate']:.0%} vs expected {exp_wr:.0%}; net ${s['net']:.2f}"))
    return verdicts


def _report_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data_cache" / "drift_report.json"


def main(days: int = 120) -> None:
    import MetaTrader5 as mt5
    from .mt5_execution import coherent_feed_clock_from_mt5, history_window_from_feed_clock
    from .trade_history import fetch_closed_trades
    if not mt5.initialize():
        print(f"mt5.initialize failed: {mt5.last_error()}", file=sys.stderr)
        return
    try:
        host_utc = datetime.now(tz=timezone.utc)
        _, clock = coherent_feed_clock_from_mt5(
            mt5,
            ("BTCUSD", "GOLD", "USDJPY"),
            host_utc=host_utc,
        )
        window = history_window_from_feed_clock(
            clock,
            lookback=timedelta(days=days),
        )
        trades = fetch_closed_trades(mt5, window.start, window.end)
    finally:
        mt5.shutdown()

    registry = EdgeRegistry()
    verdicts = assess_drift(group_by_magic(trades), registry)

    # Apply the safe side: demote DEGRADED -> WATCH in the registry (advisory). Never auto-blacklist.
    demoted = []
    for v in verdicts:
        if v.action == "watch":
            rec = registry.get(v.key)
            if rec and rec.stage == Stage.LIVE.value:
                registry.demote(v.key, Stage.WATCH.value, note=f"drift: {v.detail}")
                demoted.append(v.key)
    if demoted:
        registry.save()

    report = {"generated_at": datetime.now(tz=timezone.utc).isoformat(),
              "window_days": days, "demoted_to_watch": demoted,
              "verdicts": [asdict(v) for v in verdicts]}
    p = _report_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    for v in sorted(verdicts, key=lambda x: x.magic):
        print(f"  {v.key:14s} magic={v.magic} n={v.n:3d} {v.status:9s} -> {v.action:9s} {v.detail}")
    print(f"\nDemoted to WATCH: {demoted or 'none'}.  Report: {p}")


if __name__ == "__main__":
    main()
