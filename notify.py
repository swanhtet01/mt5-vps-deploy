"""Free push notifications to your phone via ntfy.sh — no signup, no API key needed.

You pick a private random topic name (e.g. "swann-mt5-x7q9k") and subscribe on your phone
via the free ntfy app (https://ntfy.sh) or any web browser. Anyone who knows the topic name
can publish to it, so keep the name secret. Public ntfy is rate-limited but generous for
trade alerts.

Usage:
  python notify.py "Daily P/L summary: +$42"         # send a message
  python notify.py daily-summary                       # build & send daily summary from MT5

Configuration via env var (preferred) or fallback constant below:
  NTFY_TOPIC = swann-mt5-private-topic-name           # CHANGE THIS to your own random topic

Once subscribed on your phone, every notification reaches you within seconds, no matter
where the trading PC is (VPS, cloud, off). Also works for Telegram bots / Discord webhooks
via the WEBHOOK_URL env var if you'd prefer those.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import MetaTrader5 as mt5

DEFAULT_TOPIC = "swann-mt5-trading-private-XYZ"   # CHANGE THIS to your own random topic name
NTFY_BASE = "https://ntfy.sh"


def send_ntfy(message: str, title: str | None = None, tags: str | None = None) -> bool:
    topic = os.environ.get("NTFY_TOPIC", DEFAULT_TOPIC)
    if not topic or "XYZ" in topic:
        print(f"WARN: NTFY_TOPIC not configured. Pick a random topic name like 'swann-mt5-x7q9k'", file=sys.stderr)
        print(f"      Then: set NTFY_TOPIC=swann-mt5-x7q9k    (or edit DEFAULT_TOPIC in notify.py)", file=sys.stderr)
        return False
    url = f"{NTFY_BASE}/{topic}"
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if title:
        headers["Title"] = title
    if tags:
        headers["Tags"] = tags
    try:
        req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
        print(f"ntfy send failed: {e}", file=sys.stderr)
        return False


def send_webhook(payload: dict) -> bool:
    """Generic Discord/Slack webhook sender — set WEBHOOK_URL env var to enable."""
    url = os.environ.get("WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status in (200, 204)
    except Exception as e:
        print(f"webhook send failed: {e}", file=sys.stderr)
        return False


def send_telegram(message: str, title: str | None = None) -> bool:
    """Send via Telegram bot. Setup: message @BotFather, /newbot, save the token.
    Send any message to your bot, then GET https://api.telegram.org/bot<TOKEN>/getUpdates
    and read out the chat id. Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    body = f"*{title}*\n{message}" if title else message
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        data = json.dumps({"chat_id": chat_id, "text": body, "parse_mode": "Markdown"}).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception as e:
        print(f"telegram send failed: {e}", file=sys.stderr)
        return False


def build_daily_summary() -> tuple[str, str]:
    """Returns (title, body) for the daily summary."""
    if not mt5.initialize():
        return "MT5 notify error", "mt5.initialize failed"
    try:
        ai = mt5.account_info()
        positions = mt5.positions_get() or []
        now = datetime.now(tz=timezone.utc)
        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
        deals = mt5.history_deals_get(day_start, now + timedelta(minutes=1)) or []
        by_pos = defaultdict(list)
        for d in deals:
            by_pos[d.position_id].append(d)
        today_trades = []
        for pos_id, ds in by_pos.items():
            ds = sorted(ds, key=lambda x: x.time)
            if len(ds) < 2:
                continue
            net = sum(d.profit + d.commission + d.swap for d in ds)
            today_trades.append({"symbol": ds[0].symbol, "magic": ds[0].magic, "net": net})
        n = len(today_trades)
        wins = sum(1 for t in today_trades if t["net"] > 0)
        net = sum(t["net"] for t in today_trades)
        by_sym = defaultdict(lambda: {"n": 0, "net": 0.0})
        for t in today_trades:
            by_sym[t["symbol"]]["n"] += 1
            by_sym[t["symbol"]]["net"] += t["net"]
        sym_breakdown = ", ".join(f"{s} {v['n']}t ${v['net']:+.1f}" for s, v in by_sym.items()) or "none"
        def money(x):
            return (f"+${x:,.2f}" if x >= 0 else f"-${abs(x):,.2f}")

        if n == 0:
            title = "Trading bot: quiet day"
            today_line = "No trades yet today - that's normal, it only fires at set times."
        else:
            title = f"Trading bot: {money(net)} today"
            verb = "made" if net >= 0 else "lost"
            today_line = f"{n} trade(s) today, {verb} {money(net)} ({wins} win / {n - wins} loss)."
        open_line = (f"{len(positions)} trade(s) open right now."
                     if positions else "Nothing open right now - waiting for the next setup.")
        body = (f"Account: ${ai.balance:,.2f} (live value ${ai.equity:,.2f}).\n"
                f"{today_line}\n"
                f"{open_line}\n"
                f"Per market today: {sym_breakdown}.")
        return title, body
    finally:
        mt5.shutdown()


def main():
    if len(sys.argv) < 2:
        print("usage: notify.py <message>  OR  notify.py daily-summary  OR  notify.py test")
        sys.exit(1)
    arg = sys.argv[1]
    if arg == "daily-summary":
        title, body = build_daily_summary()
    elif arg == "test":
        title, body = "MT5 test ping", f"notify.py is working at {datetime.now(tz=timezone.utc).isoformat()}"
    else:
        title, body = "MT5 alert", " ".join(sys.argv[1:])
    ok_ntfy = send_ntfy(body, title=title, tags="moneybag,robot")
    ok_webhook = send_webhook({"content": f"**{title}**\n{body}"})
    ok_tg = send_telegram(body, title=title)
    print(f"ntfy={'ok' if ok_ntfy else 'skip'}  webhook={'ok' if ok_webhook else 'skip'}  telegram={'ok' if ok_tg else 'skip'}")


if __name__ == "__main__":
    main()
