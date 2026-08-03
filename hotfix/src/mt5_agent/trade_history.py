"""Single source for collecting CLOSED trades from MT5.

Replaces the routine copy-pasted across ~11 scripts (notify, perf_report, drift, killswitch,
dashboard, ...): history_deals_get -> group by position_id -> net = profit+commission+swap+fee.
The grouping/accounting is a PURE function so it is unit-tested without MT5; the thin live
wrapper takes the mt5 module by injection so it is testable with a fake.

Net is always charged honestly (commission + swap + fee included), matching the cost model the
backtests use, so live-vs-backtest comparisons (drift_monitor) are apples-to-apples.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class ClosedTrade:
    position_id: int
    symbol: str
    magic: int
    open_time: datetime
    close_time: datetime
    net: float       # profit + commission + swap + fee (what actually hit the balance)
    gross: float     # profit only (before financing/commission)
    volume: float
    legs: int        # number of deals that made up the position


def _get(d, key):
    return d.get(key) if isinstance(d, dict) else getattr(d, key)


def _get_optional(d, key, default=None):
    return d.get(key, default) if isinstance(d, dict) else getattr(d, key, default)


def _ts(t) -> datetime:
    if isinstance(t, datetime):
        return t
    return datetime.fromtimestamp(int(t), tz=timezone.utc)


def _deal_identity(deal) -> tuple:
    """Stable key used to merge repeated MT5 history-cache reads."""
    ticket = _get(deal, "ticket")
    if ticket not in (None, 0, "0"):
        return ("ticket", int(ticket))
    return (
        "fields",
        int(_get(deal, "position_id") or 0),
        int(_get(deal, "time") or 0),
        int(_get(deal, "type") or 0),
        int(_get(deal, "entry") or 0),
        float(_get(deal, "volume") or 0.0),
        float(_get(deal, "profit") or 0.0),
        float(_get(deal, "commission") or 0.0),
        float(_get(deal, "swap") or 0.0),
        float(_get_optional(deal, "fee", 0.0) or 0.0),
    )


def _stable_history_deals(mt5, start: datetime, end: datetime, max_attempts: int = 3) -> list:
    """Merge repeated reads until MT5's asynchronously-filled history cache stabilizes."""
    merged: dict[tuple, object] = {}
    previous_batch: set[tuple] | None = None
    for _ in range(max(int(max_attempts), 1)):
        batch = list(mt5.history_deals_get(start, end) or [])
        identities = {_deal_identity(deal) for deal in batch}
        for deal in batch:
            merged[_deal_identity(deal)] = deal
        if previous_batch is not None and identities == previous_batch:
            break
        previous_batch = identities
    return list(merged.values())


def closed_trades_from_deals(deals) -> list[ClosedTrade]:
    """Group raw MT5 deals into closed trades. `deals` is any iterable of dicts or objects with
    position_id, symbol, magic, time, profit, commission, swap, optional fee, volume. A position needs >=2
    deals (an entry and an exit) to count as closed."""
    by_pos: dict[int, list] = defaultdict(list)
    for d in deals:
        by_pos[int(_get(d, "position_id"))].append(d)
    out: list[ClosedTrade] = []
    for pos_id, ds in by_pos.items():
        ds = sorted(ds, key=lambda x: _get(x, "time"))
        if len(ds) < 2:
            continue
        entry_markers = [
            _get_optional(deal, "entry")
            for deal in ds
            if _get_optional(deal, "entry") is not None
        ]
        if entry_markers and not any(int(marker) in {1, 2, 3} for marker in entry_markers):
            continue
        net = sum(
            _get(d, "profit")
            + _get(d, "commission")
            + _get(d, "swap")
            + (_get_optional(d, "fee", 0.0) or 0.0)
            for d in ds
        )
        gross = sum(_get(d, "profit") for d in ds)
        first, last = ds[0], ds[-1]
        out.append(ClosedTrade(
            position_id=int(pos_id),
            symbol=str(_get(first, "symbol")),
            magic=int(_get(first, "magic")),
            open_time=_ts(_get(first, "time")),
            close_time=_ts(_get(last, "time")),
            net=float(net), gross=float(gross),
            volume=float(_get(first, "volume")), legs=len(ds)))
    out.sort(key=lambda t: t.close_time)
    return out


def fetch_closed_trades(mt5, start: datetime, end: datetime | None = None,
                        magic: int | None = None, symbol: str | None = None,
                        entry_lookback: timedelta = timedelta(days=30)) -> list[ClosedTrade]:
    """Return trades that closed in ``[start, end]`` with their entry legs backfilled.

    MT5 history queries only return deals inside the requested interval. Pulling a short
    reporting window therefore drops positions opened before the window, so query an entry
    buffer and then filter by close time. ``mt5`` is injected for unit tests.
    """
    if end is None:
        raise ValueError("explicit broker-feed history end is required")
    query_start = start - max(entry_lookback, timedelta(0))
    query_end = end + timedelta(minutes=1)
    deals = _stable_history_deals(mt5, query_start, query_end)
    trades = [
        trade
        for trade in closed_trades_from_deals(deals)
        if start <= trade.close_time <= end
    ]
    if magic is not None:
        trades = [t for t in trades if t.magic == magic]
    if symbol is not None:
        trades = [t for t in trades if t.symbol == symbol]
    return trades


def summarize(trades: list[ClosedTrade]) -> dict:
    """Compact stats used by drift/perf/notify: n, wins, win_rate, net, gross, avg_net."""
    n = len(trades)
    wins = sum(1 for t in trades if t.net > 0)
    net = sum(t.net for t in trades)
    gross = sum(t.gross for t in trades)
    return {
        "n": n, "wins": wins, "losses": n - wins,
        "win_rate": (wins / n) if n else 0.0,
        "net": net, "gross": gross,
        "avg_net": (net / n) if n else 0.0,
    }


def group_by_magic(trades: list[ClosedTrade]) -> dict[int, list[ClosedTrade]]:
    out: dict[int, list[ClosedTrade]] = defaultdict(list)
    for t in trades:
        out[t.magic].append(t)
    return dict(out)
