"""Build a read-only HTML review from the connected MT5 account.

The report separates automated and manual results and renders current H1 market
state. Scenario bands are descriptive ATR levels, not price forecasts.
"""

from __future__ import annotations

import argparse
import html
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import (
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)
from mt5_agent.trade_history import ClosedTrade, fetch_closed_trades


MAIN_MAGIC = 26060508
STRUCTURAL_MAGICS = set(range(88001, 88010))
MARKET_SYMBOLS = ("GOLD", "US100Cash", "BTCUSD", "USDJPY")
DEFAULT_OUTPUT = Path("reports/trading-review.html")


def lane_for_magic(magic: int) -> str:
    if magic == MAIN_MAGIC:
        return "Main agent"
    if magic in STRUCTURAL_MAGICS:
        return "Structural agents"
    if 26060000 <= magic <= 26069999:
        return "Other agent"
    if magic == 0:
        return "Manual / untagged"
    return "Other"


def trade_stats(trades: list[ClosedTrade]) -> dict[str, float | int | None]:
    nets = [trade.net for trade in trades]
    wins = [net for net in nets if net > 0]
    losses = [net for net in nets if net < 0]
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for net in nets:
        cumulative += net
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        "count": len(nets),
        "net": sum(nets),
        "win_rate": (len(wins) / len(nets) * 100.0) if nets else 0.0,
        "profit_factor": (gross_wins / gross_losses) if gross_losses else None,
        "max_drawdown": max_drawdown,
    }


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1.0 - alpha) * output[-1])
    return output


def atr(rows: list, period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    true_ranges: list[float] = []
    for index in range(1, len(rows)):
        high = float(rows[index]["high"])
        low = float(rows[index]["low"])
        previous_close = float(rows[index - 1]["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(true_ranges[-period:]) / min(period, len(true_ranges))


def money(value: float) -> str:
    return f"${value:+,.2f}"


def metric_color(value: float) -> str:
    return "positive" if value >= 0 else "negative"


def svg_series(
    series: list[tuple[str, list[float], str]],
    *,
    width: int = 980,
    height: int = 260,
) -> str:
    populated = [(name, values, color) for name, values, color in series if values]
    if not populated:
        return '<div class="empty">No data in the selected window.</div>'
    all_values = [value for _, values, _ in populated for value in values]
    minimum = min(all_values)
    maximum = max(all_values)
    span = max(maximum - minimum, 1e-9)
    longest = max(len(values) for _, values, _ in populated)
    left, right, top, bottom = 64, 18, 18, 36
    plot_width = width - left - right
    plot_height = height - top - bottom

    def x(index: int) -> float:
        return left + index / max(longest - 1, 1) * plot_width

    def y(value: float) -> float:
        return top + (maximum - value) / span * plot_height

    elements = [
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" class="axis"/>',
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" class="axis"/>',
        f'<text x="8" y="{top+5}" class="axis-label">{maximum:+.2f}</text>',
        f'<text x="8" y="{height-bottom}" class="axis-label">{minimum:+.2f}</text>',
    ]
    if minimum < 0 < maximum:
        zero_y = y(0.0)
        elements.append(
            f'<line x1="{left}" y1="{zero_y:.1f}" x2="{width-right}" y2="{zero_y:.1f}" class="zero"/>'
        )
    legend_x = left
    for name, values, color in populated:
        points = " ".join(f"{x(index):.1f},{y(value):.1f}" for index, value in enumerate(values))
        elements.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        elements.append(f'<rect x="{legend_x}" y="{height-20}" width="12" height="3" fill="{color}"/>')
        elements.append(
            f'<text x="{legend_x+18}" y="{height-14}" class="legend">{html.escape(name)}</text>'
        )
        legend_x += max(120, len(name) * 8 + 45)
    return f'<svg class="chart" viewBox="0 0 {width} {height}" role="img">{"".join(elements)}</svg>'


def svg_bars(items: list[tuple[str, float]], *, width: int = 980, height: int = 260) -> str:
    if not items:
        return '<div class="empty">No automated trades in the selected window.</div>'
    maximum = max(abs(value) for _, value in items) or 1.0
    left, right, top, bottom = 54, 18, 24, 60
    plot_width = width - left - right
    plot_height = height - top - bottom
    center = top + plot_height / 2
    slot = plot_width / max(len(items), 1)
    elements = [f'<line x1="{left}" y1="{center}" x2="{width-right}" y2="{center}" class="zero"/>']
    for index, (label, value) in enumerate(items):
        bar_height = abs(value) / maximum * (plot_height / 2 - 10)
        x = left + index * slot + slot * 0.15
        bar_width = slot * 0.7
        y = center - bar_height if value >= 0 else center
        color = "#16a06d" if value >= 0 else "#d44d5c"
        elements.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}" rx="2"/>'
        )
        label_y = center - bar_height - 7 if value >= 0 else center + bar_height + 17
        elements.append(
            f'<text x="{x+bar_width/2:.1f}" y="{label_y:.1f}" text-anchor="middle" class="bar-value">{value:+.2f}</text>'
        )
        elements.append(
            f'<text x="{x+bar_width/2:.1f}" y="{height-18}" text-anchor="middle" class="bar-label">{html.escape(label)}</text>'
        )
    return f'<svg class="chart" viewBox="0 0 {width} {height}" role="img">{"".join(elements)}</svg>'


def market_snapshot(symbol: str) -> dict | None:
    mt5.symbol_select(symbol, True)
    rows = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 160)
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if rows is None or len(rows) < 100 or tick is None or info is None:
        return None
    closes = [float(row["close"]) for row in rows]
    ema20 = ema(closes, 20)
    ema100 = ema(closes, 100)
    current = closes[-1]
    current_atr = atr(list(rows), 14)
    if current > ema20[-1] > ema100[-1]:
        state = "Bullish alignment"
        state_class = "positive"
    elif current < ema20[-1] < ema100[-1]:
        state = "Bearish alignment"
        state_class = "negative"
    else:
        state = "Mixed / range"
        state_class = "neutral"
    point = float(info.point or 0.0)
    spread_points = ((float(tick.ask) - float(tick.bid)) / point) if point else math.nan
    return {
        "symbol": symbol,
        "current": current,
        "ema20": ema20[-1],
        "ema100": ema100[-1],
        "atr": current_atr,
        "upper": current + current_atr,
        "lower": current - current_atr,
        "spread_points": spread_points,
        "state": state,
        "state_class": state_class,
        "closes": closes[-96:],
        "ema20_series": ema20[-96:],
    }


def cumulative_by_lane(trades: list[ClosedTrade], lane: str) -> list[float]:
    cumulative = 0.0
    values = [0.0]
    for trade in trades:
        if lane_for_magic(trade.magic) == lane:
            cumulative += trade.net
            values.append(cumulative)
    return values


def render_report(output: Path, since: datetime) -> None:
    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")
    host_utc = datetime.now(tz=timezone.utc)
    _, clock = coherent_feed_clock_from_mt5(
        mt5,
        ("BTCUSD", "GOLD", "USDJPY"),
        host_utc=host_utc,
    )
    window = history_window_from_feed_clock(clock, lookback=clock.feed_time - since)
    trades = fetch_closed_trades(mt5, window.start, window.end)
    positions = list(mt5.positions_get() or [])
    lanes: dict[str, list[ClosedTrade]] = defaultdict(list)
    for trade in trades:
        lanes[lane_for_magic(trade.magic)].append(trade)

    lane_order = ("Main agent", "Structural agents", "Other agent", "Manual / untagged", "Other")
    lane_rows = []
    for lane in lane_order:
        stats = trade_stats(lanes.get(lane, []))
        profit_factor = stats["profit_factor"]
        pf_text = f"{profit_factor:.2f}" if isinstance(profit_factor, float) else "n/a"
        lane_rows.append(
            "<tr>"
            f"<td>{html.escape(lane)}</td>"
            f"<td>{stats['count']}</td>"
            f"<td>{stats['win_rate']:.1f}%</td>"
            f"<td>{pf_text}</td>"
            f"<td class=\"{metric_color(float(stats['net']))}\">{money(float(stats['net']))}</td>"
            f"<td>{money(-float(stats['max_drawdown']))}</td>"
            "</tr>"
        )

    automated = [trade for trade in trades if lane_for_magic(trade.magic) in {"Main agent", "Structural agents", "Other agent"}]
    by_symbol: dict[str, float] = defaultdict(float)
    for trade in automated:
        by_symbol[trade.symbol] += trade.net
    symbol_items = sorted(by_symbol.items(), key=lambda item: item[1], reverse=True)

    curve = svg_series(
        [
            ("Main agent", cumulative_by_lane(trades, "Main agent"), "#3f72e5"),
            ("Structural agents", cumulative_by_lane(trades, "Structural agents"), "#16a06d"),
            ("Manual / untagged", cumulative_by_lane(trades, "Manual / untagged"), "#d44d5c"),
        ]
    )

    position_rows = "".join(
        "<tr>"
        f"<td>{html.escape(position.symbol)}</td>"
        f"<td>{'Buy' if position.type == mt5.POSITION_TYPE_BUY else 'Sell'}</td>"
        f"<td>{position.volume:g}</td>"
        f"<td>{position.price_open:.5f}</td>"
        f"<td class=\"{metric_color(float(position.profit))}\">{money(float(position.profit))}</td>"
        f"<td>{position.magic}</td>"
        "</tr>"
        for position in positions
    ) or '<tr><td colspan="6" class="empty-cell">No open positions.</td></tr>'

    market_panels = []
    for symbol in MARKET_SYMBOLS:
        snapshot = market_snapshot(symbol)
        if snapshot is None:
            market_panels.append(
                f'<section class="market-panel"><h3>{html.escape(symbol)}</h3><div class="empty">Market data unavailable.</div></section>'
            )
            continue
        market_chart = svg_series(
            [
                ("H1 close", snapshot["closes"], "#e8edf7"),
                ("EMA 20", snapshot["ema20_series"], "#f3b33d"),
            ],
            width=560,
            height=220,
        )
        market_panels.append(
            '<section class="market-panel">'
            f'<div class="market-title"><h3>{html.escape(symbol)}</h3><span class="state {snapshot["state_class"]}">{snapshot["state"]}</span></div>'
            f'{market_chart}'
            '<dl class="market-metrics">'
            f'<div><dt>Last close</dt><dd>{snapshot["current"]:.5f}</dd></div>'
            f'<div><dt>EMA 20 / 100</dt><dd>{snapshot["ema20"]:.5f} / {snapshot["ema100"]:.5f}</dd></div>'
            f'<div><dt>ATR 14</dt><dd>{snapshot["atr"]:.5f}</dd></div>'
            f'<div><dt>1 ATR observation band</dt><dd>{snapshot["lower"]:.5f} to {snapshot["upper"]:.5f}</dd></div>'
            f'<div><dt>Current spread</dt><dd>{snapshot["spread_points"]:.1f} points</dd></div>'
            '</dl></section>'
        )

    generated = datetime.now(tz=timezone.utc)
    total_auto = trade_stats(automated)
    total_manual = trade_stats(lanes.get("Manual / untagged", []))
    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>MT5 Trading Review</title>
<style>
:root{{--bg:#0e1117;--surface:#171c24;--surface-2:#1d2430;--line:#303947;--text:#edf2f8;--muted:#9aa7b8;--blue:#6d94ff;--green:#3bd19d;--red:#ff7785;--amber:#f3b33d}}
*{{box-sizing:border-box}}html,body{{max-width:100%;overflow-x:hidden}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Segoe UI,Arial,sans-serif;letter-spacing:0}}
main{{width:min(1180px,calc(100% - 28px));margin:0 auto;padding:24px 0 44px}}header{{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;padding:8px 0 22px;border-bottom:1px solid var(--line)}}
header>div:first-child{{min-width:0}}h1{{margin:0;font-size:28px;font-weight:650}}h2{{margin:0 0 14px;font-size:17px}}h3{{margin:0;font-size:15px}}p{{margin:4px 0 0;color:var(--muted);overflow-wrap:anywhere}}
.timestamp{{text-align:right;color:var(--muted);font-size:12px}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);margin:20px 0}}
.metric{{background:var(--surface);padding:16px}}.metric span{{display:block;color:var(--muted);font-size:12px}}.metric strong{{display:block;margin-top:4px;font-size:23px;font-weight:650}}
.positive{{color:var(--green)!important}}.negative{{color:var(--red)!important}}.neutral{{color:var(--amber)!important}}
.section{{padding:22px 0;border-bottom:1px solid var(--line)}}.chart{{display:block;width:100%;height:auto;background:var(--surface);border:1px solid var(--line)}}
.axis{{stroke:#4b5667;stroke-width:1}}.zero{{stroke:#687487;stroke-width:1;stroke-dasharray:5 5}}.axis-label,.legend,.bar-label,.bar-value{{fill:#9aa7b8;font-size:12px}}.bar-value{{fill:#dbe4ef}}
table{{width:100%;border-collapse:collapse;background:var(--surface)}}th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}}th{{color:var(--muted);font-size:12px;font-weight:600}}tbody tr:last-child td{{border-bottom:0}}
.two-column{{display:grid;grid-template-columns:1.05fr .95fr;gap:22px}}.two-column>div{{min-width:0}}.table-wrap{{max-width:100%;overflow-x:auto}}.market-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
.market-panel{{background:var(--surface);border:1px solid var(--line);padding:14px;min-width:0}}.market-title{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}}
.state{{font-size:12px;font-weight:600}}.market-metrics{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px;margin:12px 0 0}}.market-metrics div:last-child{{grid-column:1/-1}}
dt{{color:var(--muted);font-size:11px}}dd{{margin:2px 0 0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}}.note{{border-left:3px solid var(--amber);padding:10px 12px;background:#211d15;color:#e8d7ad;margin:0 0 14px}}
.empty,.empty-cell{{color:var(--muted);text-align:center;padding:36px 12px}}footer{{padding-top:18px;color:var(--muted);font-size:12px}}
@media(max-width:820px){{.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.two-column,.market-grid{{grid-template-columns:1fr}}header{{align-items:stretch;flex-direction:column}}.timestamp{{text-align:left}}}}
@media(max-width:520px){{main{{width:calc(100% - 20px);padding-top:16px}}h1{{font-size:26px}}.metric{{padding:13px 12px}}.metric strong{{font-size:21px}}.table-wrap{{overflow-x:visible}}table{{table-layout:fixed;font-size:11px}}th,td{{padding:8px 4px;white-space:normal;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}}th:first-child,td:first-child{{width:29%}}.market-title{{align-items:flex-start}}}}
</style></head><body><main>
<header><div><h1>MT5 Trading Review</h1><p>Account activity, strategy attribution, and current H1 market state</p></div><div class="timestamp">Generated {generated.strftime('%Y-%m-%d %H:%M UTC')}<br>Window starts {since.strftime('%Y-%m-%d')}</div></header>
<div class="metrics">
<div class="metric"><span>Balance</span><strong>${account.balance:,.2f}</strong></div>
<div class="metric"><span>Equity</span><strong>${account.equity:,.2f}</strong></div>
<div class="metric"><span>Automated net</span><strong class="{metric_color(float(total_auto['net']))}">{money(float(total_auto['net']))}</strong></div>
<div class="metric"><span>Manual / untagged net</span><strong class="{metric_color(float(total_manual['net']))}">{money(float(total_manual['net']))}</strong></div>
</div>
<section class="section"><h2>Cumulative closed-trade P/L by lane</h2>{curve}</section>
<section class="section two-column"><div><h2>Attribution</h2><div class="table-wrap"><table><thead><tr><th>Lane</th><th>Trades</th><th>Win rate</th><th>PF</th><th>Net</th><th>Max DD</th></tr></thead><tbody>{''.join(lane_rows)}</tbody></table></div></div><div><h2>Automated net by symbol</h2>{svg_bars(symbol_items, width=560, height=320)}</div></section>
<section class="section"><h2>Open positions</h2><div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Side</th><th>Volume</th><th>Open price</th><th>Floating P/L</th><th>Magic</th></tr></thead><tbody>{position_rows}</tbody></table></div></section>
<section class="section"><h2>Current H1 market state</h2><p class="note">These charts summarize current price alignment and one-ATR observation bands. They are not future-price predictions or trade instructions.</p><div class="market-grid">{''.join(market_panels)}</div></section>
<footer>Read-only report. Closed-trade net includes profit, commission, swap, and any broker fee exposed by MT5. Manual means MT5 magic 0 and may include trades created outside this agent.</footer>
</main></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_doc, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--since", default="2026-06-01", help="UTC start date, YYYY-MM-DD")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        print(f"Invalid --since date: {exc}", file=sys.stderr)
        return 2
    if not mt5.initialize():
        print(f"MT5 initialization failed: {mt5.last_error()}", file=sys.stderr)
        return 1
    try:
        render_report(args.output, since)
    except Exception as exc:
        print(f"Dashboard generation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        mt5.shutdown()
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
