"""Self-contained HTML dashboard — pulls live MT5 data + recent trades + news state
and writes a single HTML file you can open in any browser. No JS framework, just inline
SVG charts. Refreshes from a fresh MT5 query each time it runs.

Charts:
  - Cumulative P/L equity curve (all magic numbers combined)
  - Per-strategy bar chart (net by magic)
  - Per-symbol heatmap (wins vs losses)
  - News sentiment per symbol
  - Today's open positions table

Output: C:\\mt5-paper\\dashboard.html (open in browser)
Run anytime, or schedule it on a 10-minute cron.
"""

from __future__ import annotations

import html
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import (
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)

OUT = Path(r"C:\mt5-paper\dashboard.html")
NEWS_FILE = Path(r"C:\Users\swann\OneDrive - BDA\trading-agent\data_cache\news_state.json")
SIZING_FILE = Path(r"C:\Users\swann\OneDrive - BDA\trading-agent\data_cache\position_sizing.json")
MOMENTUM_FILE = Path(r"C:\Users\swann\OneDrive - BDA\trading-agent\data_cache\stock_momentum.json")

CLAUDE_MAGICS = {88001: "GOLD_DRIFT", 88002: "USDJPY_MON", 88003: "UK100_THU",
                 88004: "GOLD_FRI", 88005: "USDJPY_WED", 88006: "GOLD_THU"}


def fetch_trades():
    start = datetime(2026, 6, 17, tzinfo=timezone.utc)
    host_utc = datetime.now(tz=timezone.utc)
    _, clock = coherent_feed_clock_from_mt5(
        mt5,
        ("BTCUSD", "GOLD", "USDJPY"),
        host_utc=host_utc,
    )
    window = history_window_from_feed_clock(clock, lookback=clock.feed_time - start)
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
        out.append({"ts": datetime.fromtimestamp(x.time, tz=timezone.utc),
                    "symbol": e.symbol, "magic": e.magic, "net": net,
                    "vol": e.volume, "side": "BUY" if e.type == 0 else "SELL"})
    return sorted(out, key=lambda x: x["ts"])


def svg_line(points, w=700, h=200, color="#3498db"):
    if not points:
        return f'<svg width="{w}" height="{h}"><text x="50%" y="50%" text-anchor="middle" fill="#888">no data</text></svg>'
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    yrange = max(ymax - ymin, 1)
    xrange = max(xmax - xmin, 1)
    pad = 30
    coords = []
    for x, y in points:
        px = pad + (x - xmin) / xrange * (w - 2 * pad)
        py = h - pad - (y - ymin) / yrange * (h - 2 * pad)
        coords.append(f"{px:.1f},{py:.1f}")
    path = " ".join(coords)
    zero_y = h - pad - (0 - ymin) / yrange * (h - 2 * pad) if ymin < 0 < ymax else None
    zero_line = f'<line x1="{pad}" y1="{zero_y}" x2="{w-pad}" y2="{zero_y}" stroke="#999" stroke-dasharray="3,3"/>' if zero_y else ""
    return (f'<svg width="{w}" height="{h}" style="background:#fafafa;border:1px solid #ddd">'
            f'{zero_line}'
            f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2"/>'
            f'<text x="5" y="15" font-size="11" fill="#666">${ymin:.0f}</text>'
            f'<text x="5" y="{h-5}" font-size="11" fill="#666">${ymax:.0f}</text>'
            f'</svg>')


def svg_bars(items, w=700, h=200):
    if not items:
        return ""
    max_abs = max(abs(v) for _, v in items) or 1
    bar_w = (w - 60) / max(len(items), 1)
    bars = []
    for i, (label, v) in enumerate(items):
        bar_h = abs(v) / max_abs * (h - 50)
        x = 50 + i * bar_w
        color = "#27ae60" if v >= 0 else "#e74c3c"
        y = (h - 30 - bar_h) if v >= 0 else (h - 30)
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w*0.8:.1f}" height="{bar_h:.1f}" fill="{color}"/>')
        bars.append(f'<text x="{x + bar_w*0.4:.1f}" y="{h-15}" text-anchor="middle" font-size="10" fill="#444">{html.escape(str(label)[:9])}</text>')
        bars.append(f'<text x="{x + bar_w*0.4:.1f}" y="{y-3:.1f}" text-anchor="middle" font-size="10" fill="#222">${v:+.0f}</text>')
    return f'<svg width="{w}" height="{h}" style="background:#fafafa;border:1px solid #ddd">{"".join(bars)}<line x1="50" y1="{h-30}" x2="{w-10}" y2="{h-30}" stroke="#333"/></svg>'


def main():
    if not mt5.initialize():
        print(f"mt5.initialize failed: {mt5.last_error()}", file=sys.stderr); sys.exit(1)
    try:
        ai = mt5.account_info()
        positions = mt5.positions_get() or []
        trades = fetch_trades()

        # Equity curve points (cumulative net by close time)
        cum = 0
        eq_pts = []
        for i, t in enumerate(trades):
            cum += t["net"]
            eq_pts.append((i, cum))

        # Per-magic net
        by_magic = defaultdict(float)
        by_magic_n = defaultdict(int)
        for t in trades:
            by_magic[t["magic"]] += t["net"]
            by_magic_n[t["magic"]] += 1
        per_magic_items = sorted([(f"{CLAUDE_MAGICS.get(m, str(m)[-4:])}\n(n={by_magic_n[m]})", v) for m, v in by_magic.items()], key=lambda x: -x[1])

        # Per-symbol win rates
        sym_stats = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0})
        for t in trades:
            sym_stats[t["symbol"]]["n"] += 1
            sym_stats[t["symbol"]]["net"] += t["net"]
            if t["net"] > 0:
                sym_stats[t["symbol"]]["wins"] += 1

        # News state
        news_state = {}
        if NEWS_FILE.exists():
            try:
                news_state = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Sizing recommendations
        sizing = {}
        if SIZING_FILE.exists():
            try:
                sizing = json.loads(SIZING_FILE.read_text(encoding="utf-8")).get("recommendations", {})
            except Exception:
                pass

        # Top momentum stocks
        momentum_top = []
        if MOMENTUM_FILE.exists():
            try:
                momentum_top = json.loads(MOMENTUM_FILE.read_text(encoding="utf-8")).get("top", [])[:15]
            except Exception:
                pass

        # Render HTML
        now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        gain = ai.balance - 544.19
        gain_color = "#27ae60" if gain >= 0 else "#e74c3c"

        position_rows = "".join(
            f"<tr><td>{p.symbol}</td><td>{'BUY' if p.type==0 else 'SELL'}</td>"
            f"<td>{p.volume}</td><td>{p.price_open:.2f}</td><td>{p.price_current:.2f}</td>"
            f"<td style='color:{'#27ae60' if p.profit>=0 else '#e74c3c'}'>${p.profit:+.2f}</td>"
            f"<td>{p.magic}</td></tr>"
            for p in positions
        ) or "<tr><td colspan='7' style='text-align:center;color:#888'>no open positions</td></tr>"

        sym_rows = "".join(
            f"<tr><td>{html.escape(s)}</td><td>{v['n']}</td>"
            f"<td>{100*v['wins']/v['n']:.0f}%</td>"
            f"<td style='color:{'#27ae60' if v['net']>=0 else '#e74c3c'}'>${v['net']:+.2f}</td></tr>"
            for s, v in sorted(sym_stats.items(), key=lambda x: -x[1]['net'])
        )

        news_rows = "".join(
            f"<tr><td>{html.escape(s)}</td>"
            f"<td style='color:{'#27ae60' if r.get('sentiment',0)>=0 else '#e74c3c'}'>{r.get('sentiment',0):+.3f}</td>"
            f"<td>{html.escape(str(r.get('event_risk','?')))}</td>"
            f"<td>{html.escape(str(r.get('blackout_until') or '—')[:20])}</td>"
            f"<td style='font-size:11px;color:#666'>{html.escape(str(r.get('headline',''))[:80])}</td></tr>"
            for s, r in sorted(news_state.items())
        )

        sizing_rows = "".join(
            f"<tr><td>{html.escape(r.get('name','?'))}</td><td>{m}</td>"
            f"<td>{r.get('n',0)}</td><td>{r.get('t_stat',0):+.2f}</td>"
            f"<td>{r.get('profit_factor','—')}</td>"
            f"<td>Tier {r.get('tier',0)} ({r.get('lot_multiplier',1.0):.1f}×)</td></tr>"
            for m, r in sorted(sizing.items(), key=lambda x: -x[1].get('tier', 0))
        )

        momentum_rows = "".join(
            f"<tr><td>{html.escape(x['ticker'])}</td>"
            f"<td style='color:#27ae60'>+{x['ret_6mo_pct']:.1f}%</td>"
            f"<td>+{x['ret_3mo_pct']:.1f}%</td>"
            f"<td>+{x['ret_1mo_pct']:.1f}%</td>"
            f"<td>{x['ann_vol_pct']:.1f}%</td>"
            f"<td>{x['max_dd_pct']:.1f}%</td>"
            f"<td><b>{x['momentum_quality']:.2f}</b></td></tr>"
            for x in momentum_top
        ) or "<tr><td colspan='7' style='text-align:center;color:#888'>run scripts/stock_momentum.py first</td></tr>"

        html_doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>MT5 Live Dashboard</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:1100px;margin:20px auto;color:#222;background:#f5f5f5}}
.card{{background:white;padding:18px;margin:12px 0;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
h1{{margin:0;font-size:24px}} h2{{margin:0 0 12px;font-size:16px;color:#555}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:5px 8px;text-align:left;border-bottom:1px solid #eee}}
th{{background:#f9f9f9;font-weight:600;color:#444}}
.bignum{{font-size:32px;font-weight:600}}
.label{{color:#888;font-size:12px;text-transform:uppercase;letter-spacing:0.5px}}
.grid{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px}}
</style></head><body>
<div class="card">
<h1>MT5 Live Trading Dashboard</h1>
<div style="color:#888;font-size:13px">{now_str} — auto-refresh by re-running html_dashboard.py</div>
</div>

<div class="card grid">
<div><div class="label">Equity</div><div class="bignum">${ai.equity:.2f}</div></div>
<div><div class="label">Balance</div><div class="bignum">${ai.balance:.2f}</div></div>
<div><div class="label">Realized P/L</div><div class="bignum" style="color:{gain_color}">${gain:+.2f}</div></div>
<div><div class="label">% Gain</div><div class="bignum" style="color:{gain_color}">{100*gain/544.19:+.1f}%</div></div>
</div>

<div class="card"><h2>Cumulative P/L (every closed trade in order)</h2>
{svg_line(eq_pts, w=1040)}
</div>

<div class="card"><h2>Net by Strategy Magic</h2>
{svg_bars(per_magic_items, w=1040)}
</div>

<div class="card"><h2>Per-Symbol Performance</h2>
<table><thead><tr><th>Symbol</th><th>Trades</th><th>Win %</th><th>Net</th></tr></thead><tbody>
{sym_rows}
</tbody></table></div>

<div class="card"><h2>News Sentiment (live)</h2>
<table><thead><tr><th>Symbol</th><th>Sentiment</th><th>Event Risk</th><th>Blackout Until</th><th>Headline</th></tr></thead><tbody>
{news_rows}
</tbody></table></div>

<div class="card"><h2>Open Positions</h2>
<table><thead><tr><th>Symbol</th><th>Side</th><th>Vol</th><th>Entry</th><th>Current</th><th>P/L</th><th>Magic</th></tr></thead><tbody>
{position_rows}
</tbody></table></div>

<div class="card"><h2>Dynamic Position Sizing — auto-promotion ladder</h2>
<p style="font-size:12px;color:#666">Tier 0 = min lot. Auto-promote: n≥10+t&gt;1.5→tier 1, n≥20+t&gt;2→tier 2 (2×), n≥30+t&gt;2.5+PF&gt;1.5→tier 3 (3×), n≥50+t&gt;3+PF&gt;1.7→tier 4 (5×). Auto-demote on loss streak.</p>
<table><thead><tr><th>Signal</th><th>Magic</th><th>n</th><th>t-stat</th><th>PF</th><th>Recommendation</th></tr></thead><tbody>
{sizing_rows or '<tr><td colspan="6" style="text-align:center;color:#888">no data — run scripts/dynamic_sizing.py</td></tr>'}
</tbody></table></div>

<div class="card"><h2>Top Momentum Stocks (long-term swing candidates)</h2>
<p style="font-size:12px;color:#666">Universe: 1,368 stocks + 10 thematic indices. Filters: 6mo&gt;+15%, 1mo&gt;0, above 200-SMA, spread&lt;0.5%. Ranked by quality (return/vol).</p>
<table><thead><tr><th>Ticker</th><th>6mo</th><th>3mo</th><th>1mo</th><th>Vol</th><th>MaxDD</th><th>Quality</th></tr></thead><tbody>
{momentum_rows}
</tbody></table></div>

<div style="text-align:center;color:#888;font-size:11px;margin:20px">
generated {now_str} — refresh anytime
</div>
</body></html>"""

        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(html_doc, encoding="utf-8")
        print(f"Wrote {OUT}")
        print(f"Open in browser: file:///{OUT.as_posix()}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
