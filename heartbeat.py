"""Heartbeat — pings healthchecks.io every 5 minutes so the operator gets an
alert if the VPS goes silent (crashed, lost internet, Python broken).

Setup: free signup at https://healthchecks.io, create a check with
  Period:  10 minutes
  Grace:   5 minutes
  Name:    "MT5 VPS heartbeat"
Copy the unique Ping URL (looks like https://hc-ping.com/abc-123-def-456).
Set env var HEALTHCHECK_URL=https://hc-ping.com/abc-123-def-456

If the VPS stops pinging for >15 min total, healthchecks emails/sms/pushes you.
A dead VPS is now LOUD, not silent.

Also writes a local heartbeat file so we know on the dashboard the last time
we tried — useful when healthchecks is the down party, not us.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA_CACHE, write_json_atomic  # noqa: E402


HEARTBEAT_FILE = DATA_CACHE / "heartbeat.json"


def main() -> int:
    url = os.environ.get("HEALTHCHECK_URL", "").strip()
    started = datetime.now(tz=timezone.utc)
    out = {"ts": started.isoformat(), "healthcheck_url_set": bool(url)}

    # Quick MT5 + account check so the ping CARRIES information about whether
    # trading is actually functional, not just whether Python is alive.
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            ai = mt5.account_info()
            out["mt5_login"] = ai.login if ai else None
            out["mt5_equity"] = round(ai.equity, 2) if ai else None
            out["mt5_balance"] = round(ai.balance, 2) if ai else None
            mt5.shutdown()
            out["mt5_status"] = "ok"
        else:
            out["mt5_status"] = "init_failed"
    except Exception as e:
        out["mt5_status"] = f"error: {type(e).__name__}: {e}"

    # Always write locally
    write_json_atomic(HEARTBEAT_FILE, out)

    if not url:
        print(json.dumps(out))
        return 0

    # Healthchecks payload supports a short text body; we send compact status
    body = f"mt5={out.get('mt5_status', '?')} equity={out.get('mt5_equity', '?')}"
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=10) as r:
            out["ping_status"] = r.status
            out["ping_ok"] = r.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
        out["ping_ok"] = False
        out["ping_error"] = str(e)
    # Update the local file with ping outcome too
    write_json_atomic(HEARTBEAT_FILE, out)
    print(json.dumps(out))
    # Always exit 0: the heartbeat always writes locally, so the task succeeded. A transient
    # network blip to healthchecks.io should NOT flag the task as failed (red in Task Scheduler).
    return 0


if __name__ == "__main__":
    sys.exit(main())
