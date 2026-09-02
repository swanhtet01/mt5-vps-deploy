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

The ping also carries a VERDICT. This used to hit the plain URL and exit 0 even when
initialize() failed, so the one channel that survives the box dying could never say
"unhealthy": a box whose Python still ran but whose terminal had lost the broker read as
fine. Now an unhealthy verdict on two consecutive runs pings the /fail endpoint instead,
which healthchecks.io alerts on immediately. Two runs, not one, so a single initialize()
blip during the broker's daily rollover does not page at 3am.

Also writes a local heartbeat file so we know on the dashboard the last time
we tried — useful when healthchecks is the down party, not us. vps_health.py reads it.
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
from paths import DATA_CACHE, read_json, write_json_atomic  # noqa: E402

try:
    from mt5_agent.mt5_execution import persistent_user_flag_enabled
except Exception:  # noqa: BLE001 - the dead-man's switch must keep beating if the agent package is broken
    persistent_user_flag_enabled = None  # type: ignore[assignment]


HEARTBEAT_FILE = DATA_CACHE / "heartbeat.json"
LIVE_ENV_FLAG = "MT5_GOLD_DRIFT_LIVE"
# Consecutive unhealthy runs before the ping goes to /fail. One run is a blip; two is ten
# minutes with no working terminal, which is worth waking someone for.
FAIL_AFTER_UNHEALTHY_RUNS = 2


def _as_count(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def live_flag_armed() -> bool | None:
    """The persistent live flag, via the same reader every trading script uses.

    None means the flag could not be read at all (the mt5_agent package failed to import),
    which is treated as unhealthy below: if that package is broken, so is every edge."""
    if persistent_user_flag_enabled is None:
        return None
    try:
        return bool(persistent_user_flag_enabled(LIVE_ENV_FLAG))
    except Exception:
        return None


def probe_mt5(live_armed: bool | None) -> dict:
    """Ask the terminal whether trading is actually possible, not just whether it answers.

    Healthy means initialize() succeeded, account_info() is present, the terminal reports
    itself connected to the broker, and -- only while live trading is armed -- AutoTrading
    is on. trade_allowed is deliberately not required in paper mode: a paper box with the
    button off is fine, and paging for it would teach the operator to ignore /fail.
    """
    out = {"mt5_status": "?", "connected": None, "trade_allowed": None, "healthy": False}
    try:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            out["mt5_status"] = "init_failed"
            return out
        try:
            ai = mt5.account_info()
            ti = mt5.terminal_info()
        finally:
            mt5.shutdown()
        out["mt5_login"] = ai.login if ai else None
        out["mt5_equity"] = round(ai.equity, 2) if ai else None
        out["mt5_balance"] = round(ai.balance, 2) if ai else None
        out["connected"] = bool(ti.connected) if ti is not None else None
        out["trade_allowed"] = bool(ti.trade_allowed) if ti is not None else None
    except Exception as e:
        out["mt5_status"] = f"error: {type(e).__name__}: {e}"
        return out

    problems = []
    if ai is None:
        problems.append("no_account")
    if not out["connected"]:
        problems.append("disconnected")
    if live_armed is None:
        problems.append("live_flag_unreadable")
    elif live_armed and not out["trade_allowed"]:
        problems.append("autotrading_off")
    out["mt5_status"] = "ok" if not problems else "+".join(problems)
    out["healthy"] = not problems
    return out


def main() -> int:
    url = os.environ.get("HEALTHCHECK_URL", "").strip()
    started = datetime.now(tz=timezone.utc)
    previous = read_json(HEARTBEAT_FILE, default={})
    if not isinstance(previous, dict):
        previous = {}
    live_armed = live_flag_armed()

    out = {
        "ts": started.isoformat(),
        "url_set": bool(url),
        "healthcheck_url_set": bool(url),  # older key, kept for anything still reading it
        "live_armed": live_armed,
    }
    # Quick MT5 + account check so the ping CARRIES information about whether
    # trading is actually functional, not just whether Python is alive.
    out.update(probe_mt5(live_armed))

    # The streak lives in the local file, so it survives across runs without any other state.
    streak = 0 if out["healthy"] else _as_count(previous.get("unhealthy_streak")) + 1
    out["unhealthy_streak"] = streak
    out["ping_target"] = "fail" if streak >= FAIL_AFTER_UNHEALTHY_RUNS else "base"
    out["ping_ok"] = None
    out["last_ping_utc"] = None
    out["ping_fail_streak"] = _as_count(previous.get("ping_fail_streak"))

    # Always write locally, before the network call, so the file is fresh even if the ping hangs.
    write_json_atomic(HEARTBEAT_FILE, out)

    if not url:
        print(json.dumps(out))
        return 0

    target = url.rstrip("/") + "/fail" if out["ping_target"] == "fail" else url
    # Healthchecks payload supports a short text body; we send compact status
    body = (
        f"mt5={out['mt5_status']} connected={out['connected']} trade_allowed={out['trade_allowed']} "
        f"live={live_armed} streak={streak} equity={out.get('mt5_equity', '?')}"
    )
    out["last_ping_utc"] = datetime.now(tz=timezone.utc).isoformat()
    try:
        req = urllib.request.Request(target, data=body.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=10) as r:
            out["ping_status"] = r.status
            out["ping_ok"] = r.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
        out["ping_ok"] = False
        out["ping_error"] = str(e)
    out["ping_fail_streak"] = 0 if out["ping_ok"] else out["ping_fail_streak"] + 1
    # Update the local file with ping outcome too
    write_json_atomic(HEARTBEAT_FILE, out)
    print(json.dumps(out))
    # Always exit 0: the heartbeat always writes locally, so the task succeeded. A transient
    # network blip to healthchecks.io should NOT flag the task as failed (red in Task Scheduler),
    # and an unhealthy terminal is reported through /fail above, not through this exit code.
    return 0


if __name__ == "__main__":
    sys.exit(main())
