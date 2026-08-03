"""Correlation-aware position budget -- stops correlated edges secretly stacking into one bet.

THE HAZARD
----------
Each edge is sized independently (position_sizing.json). But several edges can be the SAME
underlying bet on the same day: USDJPY-Monday-long (88002) and AUDJPY-Monday-long (88007) are
both "short JPY on Monday" and fire in the same hour -- if both auto-upsize to 3x, that is a 6x
concentrated JPY-short bet, not diversification. (Same-symbol edges on DIFFERENT weekdays are
NOT a concurrent risk -- they never hold at once -- so clustering is weekday-aware.)

THE FIX (static, parameter-free, fail-safe)
-------------------------------------------
Map each edge to its currency exposures (long a pair = long base / short quote), cluster edges
that share a (currency, direction, weekday), and cap each cluster's AGGREGATE size multiplier.
Uses a STATIC declarative currency map, never a live-estimated correlation matrix (uncomputable
with n~0 trades). Inert by construction at Tier 0: a 2-edge cluster at 1.0x each sums to the cap,
so nothing is throttled until edges actually upsize. Writes data_cache/portfolio_budget.json
({magic: budget_multiplier}); live traders multiply it AFTER their position_sizing multiplier.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

# Static currency map. Pairs -> (base, quote). Single-currency instruments -> (ccy,).
SYMBOL_CCY: dict[str, tuple[str, ...]] = {
    "GOLD": ("XAU", "USD"), "SILVER": ("XAG", "USD"),
    "USDJPY": ("USD", "JPY"), "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"),
    "AUDUSD": ("AUD", "USD"), "USDCAD": ("USD", "CAD"), "NZDUSD": ("NZD", "USD"),
    "USDCHF": ("USD", "CHF"),
    "EURJPY": ("EUR", "JPY"), "GBPJPY": ("GBP", "JPY"), "AUDJPY": ("AUD", "JPY"),
    "EURGBP": ("EUR", "GBP"), "EURAUD": ("EUR", "AUD"),
    "UK100Cash": ("GBP",), "GER40Cash": ("EUR",), "FRA40Cash": ("EUR",),
    "EUSTX50Cash": ("EUR",), "US500Cash": ("USD",), "JP225Cash": ("JPY",),
    "HK50Cash": ("HKD",),
}

CLUSTER_CAP = 2.0   # max aggregate size multiplier for one (currency, direction, weekday) cluster


def sizing_multipliers(payload: dict) -> dict[int, float]:
    """Extract per-magic lot multipliers from the dynamic-sizing JSON schema."""
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("recommendations") or payload.get("magics") or payload
    if not isinstance(raw, dict):
        return {}
    multipliers: dict[int, float] = {}
    for key, value in raw.items():
        try:
            magic = int(key)
            multiplier = float(value.get("lot_multiplier", 1.0)) if isinstance(value, dict) else float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(multiplier) and multiplier >= 0:
            multipliers[magic] = multiplier
    return multipliers


def budget_multiplier(payload: dict, magic: int) -> float:
    """Return a bounded correlation-budget factor for one magic number."""
    if not isinstance(payload, dict):
        return 1.0
    raw = payload.get("budget") or {}
    if not isinstance(raw, dict):
        return 1.0
    try:
        value = float(raw.get(str(magic), raw.get(magic, 1.0)))
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value):
        return 1.0
    return min(max(value, 0.0), 1.0)


def exposures(symbol: str, side: str) -> dict[str, int]:
    """Currency exposures of an edge as {ccy: +1 long / -1 short}. Long a pair = long base,
    short quote. A single-currency instrument is just long/short that currency."""
    ccys = SYMBOL_CCY.get(symbol, (symbol,))
    sign = 1 if side == "long" else -1
    out: dict[str, int] = {}
    if len(ccys) >= 2:
        out[ccys[0]] = sign
        out[ccys[1]] = -sign
    else:
        out[ccys[0]] = sign
    return out


def _edge_fields(e):
    """Accept either an EdgeRecord-like object or a plain dict."""
    if isinstance(e, dict):
        return e["magic"], e["symbol"], e["side"], e["weekday"]
    return e.magic, e.symbol, e.side, e.weekday


def clusters(edges) -> dict[tuple, list[int]]:
    """(currency, direction, weekday) -> [magics] for clusters with >= 2 members."""
    raw: dict[tuple, list[int]] = defaultdict(list)
    for e in edges:
        magic, symbol, side, weekday = _edge_fields(e)
        for ccy, sign in exposures(symbol, side).items():
            raw[(ccy, sign, weekday)].append(magic)
    return {k: v for k, v in raw.items() if len(v) >= 2}


def compute_budget(edges, base_mult: dict[int, float] | None = None,
                   cluster_cap: float = CLUSTER_CAP) -> dict[int, float]:
    """Pure: per-magic budget multiplier so no (currency, direction, weekday) cluster exceeds
    `cluster_cap` in aggregate. 1.0 = unconstrained. An edge in several over-cap clusters takes
    the tightest scaling."""
    base_mult = base_mult or {}
    magics = [_edge_fields(e)[0] for e in edges]
    scale = {m: 1.0 for m in magics}
    for key, members in clusters(edges).items():
        total = sum(base_mult.get(m, 1.0) for m in members)
        if total > cluster_cap and total > 0:
            factor = cluster_cap / total
            for m in members:
                scale[m] = min(scale[m], factor)
    return {m: round(scale[m], 3) for m in scale}


def _data_cache() -> Path:
    return Path(__file__).resolve().parents[2] / "data_cache"


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from mt5_agent.edge_registry import EdgeRegistry  # noqa
    dc = _data_cache()
    reg = EdgeRegistry()
    edges = [e for e in reg.all() if e.stage in ("LIVE", "WATCH")]

    # current per-magic sizing multipliers (default 1.0)
    sizing: dict[int, float] = {}
    sp = dc / "position_sizing.json"
    if sp.exists():
        try:
            sizing = sizing_multipliers(json.loads(sp.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    budget = compute_budget(edges, sizing)
    out = dc / "portfolio_budget.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cluster_cap": CLUSTER_CAP, "budget": budget,
                               "clusters": {f"{c}/{'long' if s > 0 else 'short'}/wd{w}": m
                                            for (c, s, w), m in clusters(edges).items()}},
                              indent=2, default=str), encoding="utf-8")
    throttled = {m: f for m, f in budget.items() if f < 1.0}
    print(f"Edges: {len(edges)}  correlated clusters: {len(clusters(edges))}")
    for (c, s, w), members in clusters(edges).items():
        print(f"  cluster {c} {'long' if s > 0 else 'short'} wd{w}: magics {members}")
    print(f"Throttled (cluster cap {CLUSTER_CAP}): {throttled or 'none -- inert at current sizing'}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
