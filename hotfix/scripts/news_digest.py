"""Daily news + macro digest for the trading agent.

Pulls everything the bot already collects (unified news_state, FRED macro,
ForexFactory calendar, the day's executed trades) and produces a coherent
one-screen briefing pushed to the operator's phone.

If Ollama is available on the host (i.e. installed via vps_post_install.ps1),
uses Llama-3.2-3B for semantic synthesis. Otherwise falls back to a
template-based digest with deterministic facts only.

Runs daily at 23:00 UTC (after US close) via the MT5-News-Digest scheduled task.

Output:
  - data_cache/news_digest.json    structured digest with sections
  - push notification via notify.py (ntfy + telegram + webhook)

Each section is bounded so the phone push stays small (under 1500 chars total).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mt5_agent.mt5_execution import (
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import (  # noqa: E402
    DATA_CACHE, NEWS_STATE_FILE, CALENDAR_FILE,
    read_json, write_json_atomic,
)

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"
DIGEST_FILE = DATA_CACHE / "news_digest.json"
MACRO_FILE = DATA_CACHE / "macro_state.json"


def ollama_available() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, Exception):
        return False


def ollama_complete(prompt: str, max_tokens: int = 300) -> str:
    """Call local Ollama. Returns "" on any error so we always have a fallback."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": max_tokens},
    }
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
            return (data.get("response") or "").strip()
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, Exception) as e:
        return ""


def collect_realized_today() -> dict:
    """Read this UTC-day's realized P/L from MT5."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"trades": 0, "net_usd": 0.0, "by_symbol": {}}
    if not mt5.initialize():
        return {"trades": 0, "net_usd": 0.0, "by_symbol": {}}
    try:
        now = datetime.now(tz=timezone.utc)
        _, clock = coherent_feed_clock_from_mt5(
            mt5,
            ("BTCUSD", "GOLD", "USDJPY"),
            host_utc=now,
        )
        window = history_window_from_feed_clock(clock, start_of_feed_day=True)
        deals = mt5.history_deals_get(window.start, window.end) or []
        by_pos = defaultdict(list)
        for d in deals:
            by_pos[d.position_id].append(d)
        trades = []
        for pos_id, ds in by_pos.items():
            ds = sorted(ds, key=lambda x: x.time)
            if len(ds) < 2:
                continue
            net = sum(
                d.profit + d.commission + d.swap + float(getattr(d, "fee", 0.0) or 0.0)
                for d in ds
            )
            trades.append({"symbol": ds[0].symbol, "magic": ds[0].magic, "net": net})
        by_sym = defaultdict(lambda: {"n": 0, "net": 0.0})
        for t in trades:
            by_sym[t["symbol"]]["n"] += 1
            by_sym[t["symbol"]]["net"] += t["net"]
        ai = mt5.account_info()
        return {
            "trades": len(trades),
            "net_usd": sum(t["net"] for t in trades),
            "by_symbol": {k: dict(v) for k, v in by_sym.items()},
            "equity": ai.equity if ai else None,
            "balance": ai.balance if ai else None,
        }
    finally:
        mt5.shutdown()


def macro_summary() -> dict:
    state = read_json(MACRO_FILE)
    series = state.get("series", {})
    interp = state.get("interpretation", {})
    out = {}
    if "VIXCLS" in series and isinstance(series["VIXCLS"].get("value"), (int, float)):
        out["vix"] = round(series["VIXCLS"]["value"], 2)
    if "DTWEXBGS" in series and isinstance(series["DTWEXBGS"].get("value"), (int, float)):
        out["dxy"] = round(series["DTWEXBGS"]["value"], 2)
    if "T10Y2Y" in series and isinstance(series["T10Y2Y"].get("value"), (int, float)):
        out["yield_curve_2y10y"] = round(series["T10Y2Y"]["value"], 2)
    if "DFF" in series and isinstance(series["DFF"].get("value"), (int, float)):
        out["fed_funds"] = round(series["DFF"]["value"], 2)
    out["risk_off"] = bool(interp.get("risk_off", False))
    out["yield_inverted"] = bool(interp.get("yield_curve_inverted", False))
    return out


def news_summary() -> dict:
    state = read_json(NEWS_STATE_FILE)
    bullish, bearish, neutral = [], [], []
    blackouts = {}
    for sym, rec in state.items():
        if not isinstance(rec, dict):
            continue
        sent = rec.get("sentiment", 0) or 0
        try:
            sent = float(sent)
        except (TypeError, ValueError):
            sent = 0
        if rec.get("blackout_until"):
            blackouts[sym] = rec["blackout_until"]
        if sent > 0.2:
            bullish.append((sym, sent, (rec.get("headline") or "")[:80]))
        elif sent < -0.2:
            bearish.append((sym, sent, (rec.get("headline") or "")[:80]))
        else:
            neutral.append(sym)
    return {"bullish": bullish, "bearish": bearish, "neutral": neutral, "blackouts": blackouts}


def upcoming_events() -> list:
    cal = read_json(CALENDAR_FILE)
    if not isinstance(cal, list):
        return []
    now = datetime.now(tz=timezone.utc)
    out = []
    for ev in cal[:10]:
        try:
            t = datetime.fromisoformat(ev["scheduled_utc"])
            mins = int((t - now).total_seconds() / 60)
            if mins < 0:
                continue
            if mins > 60 * 36:  # next 36 hours only
                continue
            out.append({
                "in_min": mins,
                "currency": ev.get("currency"),
                "title": (ev.get("title") or "")[:60],
                "scheduled_utc": ev["scheduled_utc"][:16],
            })
        except (ValueError, KeyError):
            continue
    return out


def llm_synth(macro: dict, news: dict, perf: dict, events: list) -> str:
    """Use Ollama to produce a 3-sentence forward look. Returns "" if Ollama unavailable."""
    if not ollama_available():
        return ""
    prompt = f"""You are an experienced retail trader writing a one-paragraph end-of-day briefing
for tomorrow's pre-Asian-open session. Be concise, specific, and skeptical. No filler.
Maximum 3 sentences.

Today's realized: {perf.get('trades', 0)} trades, net ${perf.get('net_usd', 0):+.2f}
Account equity: ${perf.get('equity', 0):.2f}
Macro state: VIX={macro.get('vix')} DXY={macro.get('dxy')} 2y10y={macro.get('yield_curve_2y10y')} fed_funds={macro.get('fed_funds')} risk_off={macro.get('risk_off')}
News sentiment (symbol, score, headline):
  Bullish: {news.get('bullish', [])[:3]}
  Bearish: {news.get('bearish', [])[:3]}
Upcoming events (next 36h):
{json.dumps(events[:5], default=str)}

Write your 3-sentence briefing now:"""
    return ollama_complete(prompt, max_tokens=200)


def build_digest() -> dict:
    now = datetime.now(tz=timezone.utc)
    perf = collect_realized_today()
    macro = macro_summary()
    news = news_summary()
    events = upcoming_events()
    llm_take = llm_synth(macro, news, perf, events)

    digest = {
        "generated_at": now.isoformat(),
        "trading_today": perf,
        "macro": macro,
        "news": news,
        "upcoming_events_36h": events,
        "llm_briefing": llm_take,
        "llm_available": bool(llm_take),
    }
    write_json_atomic(DIGEST_FILE, digest)
    return digest


def format_for_push(digest: dict) -> tuple[str, str]:
    """Returns (title, body) for the push notification."""
    perf = digest["trading_today"]
    net = perf.get("net_usd", 0)
    eq = perf.get("equity", 0)
    title = f"MT5 daily: {perf.get('trades', 0)}t  ${net:+.2f}  eq ${eq:.2f}"
    lines = []
    if perf.get("by_symbol"):
        sym_str = ", ".join(f"{s} {v['n']}t ${v['net']:+.1f}" for s, v in perf["by_symbol"].items())
        lines.append(f"By sym: {sym_str}")
    m = digest["macro"]
    if m:
        risk = "RISK-OFF" if m.get("risk_off") else "calm"
        lines.append(f"Macro: VIX {m.get('vix')} DXY {m.get('dxy')} ({risk})")
    news = digest["news"]
    if news.get("bullish"):
        b = news["bullish"][0]
        lines.append(f"Bull: {b[0]} ({b[1]:+.2f}) {b[2][:50]}")
    if news.get("bearish"):
        b = news["bearish"][0]
        lines.append(f"Bear: {b[0]} ({b[1]:+.2f}) {b[2][:50]}")
    if digest["upcoming_events_36h"]:
        ev = digest["upcoming_events_36h"][0]
        h = ev["in_min"] // 60
        lines.append(f"Next event in {h}h: {ev['currency']} {ev['title']}")
    if digest.get("llm_briefing"):
        lines.append("")
        lines.append("AI take:")
        lines.append(digest["llm_briefing"])
    body = "\n".join(lines)
    if len(body) > 1500:
        body = body[:1497] + "..."
    return title, body


def main():
    digest = build_digest()
    title, body = format_for_push(digest)
    print(f"DIGEST WRITTEN: {DIGEST_FILE}")
    print(f"--- push title: {title}")
    print(f"--- push body ---")
    print(body)
    # Send via notify.py
    try:
        import subprocess
        notify_py = Path(__file__).parent / "notify.py"
        if notify_py.exists():
            subprocess.run([sys.executable, str(notify_py), body], timeout=15, capture_output=True)
    except Exception as e:
        print(f"notify failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
