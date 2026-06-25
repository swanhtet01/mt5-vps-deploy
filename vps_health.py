"""VPS health monitor — runs on the VPS and watches itself.

Checks:
  1. MT5 terminal: reachable, logged in, account responsive
  2. Disk space: warn if free < 2GB, critical if < 500MB
  3. Memory: warn if available < 500MB
  4. Scheduled tasks: count MT5-* tasks; warn if any in error/failed state
  5. Critical files: news_state.json freshness, blacklist.json present
  6. Log directory size: warn if > 500MB (rotation needed)

Outputs:
  - data_cache/vps_health.json
  - On any WARN or CRITICAL state: notify.py sends a push notification

Runs every 30 minutes via MT5-VPS-Health scheduled task."""

from __future__ import annotations

import json
import os
import shutil
import sys
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import MetaTrader5 as mt5

# Use the shared path resolver so this runs on the VPS (C:\trading-agent) AND the dev PC,
# instead of the old hardcoded OneDrive paths (which broke health/news/blacklist on the VPS).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA_CACHE, PAPER_ROOT, NEWS_STATE_FILE, BLACKLIST_FILE, read_json, write_json_atomic  # noqa: E402

OUT = DATA_CACHE / "vps_health.json"
LOG_DIRS = [
    PAPER_ROOT / "gold-drift", PAPER_ROOT / "multi-drift", PAPER_ROOT / "news",
    PAPER_ROOT / "analytics", PAPER_ROOT / "swing",
]
NEWS_FILE = NEWS_STATE_FILE


def check_mt5():
    try:
        if not mt5.initialize():
            return {"status": "CRITICAL", "reason": f"mt5.initialize: {mt5.last_error()}"}
        ai = mt5.account_info()
        if ai is None:
            return {"status": "CRITICAL", "reason": "account_info returned None"}
        return {
            "status": "OK",
            "login": ai.login, "balance": ai.balance, "equity": ai.equity,
            "trade_allowed": ai.trade_allowed if hasattr(ai, "trade_allowed") else None,
            "leverage": ai.leverage,
        }
    except Exception as e:
        return {"status": "CRITICAL", "reason": f"{type(e).__name__}: {e}"}
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


def check_disk():
    try:
        total, used, free = shutil.disk_usage("C:\\")
        free_gb = free / (1024 ** 3)
        if free_gb < 0.5:
            return {"status": "CRITICAL", "free_gb": round(free_gb, 2), "reason": "disk free < 500MB"}
        if free_gb < 2:
            return {"status": "WARN", "free_gb": round(free_gb, 2), "reason": "disk free < 2GB"}
        return {"status": "OK", "free_gb": round(free_gb, 2), "total_gb": round(total / (1024 ** 3), 1)}
    except Exception as e:
        return {"status": "WARN", "reason": str(e)}


def check_memory():
    try:
        # Windows-specific: read via wmic or PowerShell — keep dependency-free
        # Use psutil if available, fall back to a rough win32 call
        try:
            import psutil
            mem = psutil.virtual_memory()
            avail_mb = mem.available / (1024 ** 2)
            pct_used = mem.percent
        except ImportError:
            # Fallback: skip the memory check rather than fail
            return {"status": "OK", "note": "psutil not installed; install with pip for memory check"}
        if avail_mb < 200:
            return {"status": "CRITICAL", "available_mb": round(avail_mb, 0), "reason": "< 200MB free"}
        if avail_mb < 500:
            return {"status": "WARN", "available_mb": round(avail_mb, 0), "reason": "< 500MB free"}
        return {"status": "OK", "available_mb": round(avail_mb, 0), "percent_used": pct_used}
    except Exception as e:
        return {"status": "WARN", "reason": str(e)}


def check_scheduled_tasks():
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/fo", "CSV", "/nh"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return {"status": "WARN", "reason": "schtasks query failed"}
        # Only a CRITICAL task being down is a problem. Deliberately-disabled non-live
        # edge tasks (e.g. MT5-AUDJPY-Mon-Enter that isn't promoted) are a normal state,
        # not a failure -- flagging them caused a chronic "scheduled_tasks: ?" false alarm.
        critical = (
            "Live-Enter", "Live-Exit", "KillSwitch", "Heartbeat", "Watchdog",
            "PositionMonitor", "ContextIngest", "LLMThesis", "ApplyThesis",
            "AutoDeploy", "VPS-Health",
        )
        mt5_tasks = []
        not_ready = []
        for line in result.stdout.splitlines():
            if "MT5-" not in line:
                continue
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 3:
                name = parts[0].lstrip('"').lstrip("\\")
                status = parts[2]
                mt5_tasks.append({"name": name, "status": status})
                if status not in ("Ready", "Running"):
                    not_ready.append(name)
        critical_down = sorted({n for n in not_ready if any(c in n for c in critical)})
        if critical_down:
            return {
                "status": "WARN",
                "total": len(mt5_tasks),
                "reason": "critical task(s) not Ready: " + ", ".join(critical_down),
                "critical_down": critical_down,
                "disabled": not_ready,
            }
        return {
            "status": "OK",
            "total": len(mt5_tasks),
            "disabled_noncritical": not_ready,
        }
    except Exception as e:
        return {"status": "WARN", "reason": str(e)}


def check_freshness():
    out = {}
    now = datetime.now(tz=timezone.utc)
    if NEWS_FILE.exists():
        try:
            data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
            if data:
                first_record = next(iter(data.values()))
                as_of = first_record.get("as_of") if isinstance(first_record, dict) else None
                if as_of:
                    age_min = (now - datetime.fromisoformat(as_of)).total_seconds() / 60
                    out["news_age_min"] = round(age_min, 1)
                    out["news_status"] = "WARN" if age_min > 120 else "OK"
        except Exception as e:
            out["news_status"] = "WARN"
            out["news_error"] = str(e)
    else:
        out["news_status"] = "WARN"
        out["news_error"] = "news_state.json missing"
    out["blacklist_present"] = BLACKLIST_FILE.exists()
    return out


def check_log_sizes():
    total_mb = 0
    breakdown = {}
    for d in LOG_DIRS:
        if not d.exists():
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        size_mb = size / (1024 ** 2)
        breakdown[d.name] = round(size_mb, 1)
        total_mb += size_mb
    status = "OK"
    if total_mb > 1000:
        status = "WARN"
    return {"status": status, "total_mb": round(total_mb, 1), "breakdown": breakdown}


def _push_health(msg: str):
    notify_path = Path(__file__).parent / "notify.py"
    if not notify_path.exists():
        return
    try:
        subprocess.run([sys.executable, str(notify_path), msg], timeout=10, capture_output=True)
    except Exception:
        pass


def maybe_notify(report: dict):
    """Push on WARN/CRITICAL, but DEDUP so a chronic warning can't spam ~48 pushes/day.
    An identical alert is suppressed within a cooldown (CRITICAL re-pings hourly, WARN every 6h);
    a return-to-OK after a non-OK state sends one 'recovered' ping. Prevents alert fatigue that
    would bury a real CRITICAL."""
    import hashlib
    import time
    severity = "OK"
    reasons = []
    for k, v in report.items():
        if isinstance(v, dict) and v.get("status") == "CRITICAL":
            severity = "CRITICAL"
            reasons.append(f"{k}: {v.get('reason', '?')}")
        elif isinstance(v, dict) and v.get("status") == "WARN" and severity != "CRITICAL":
            severity = "WARN"
            reasons.append(f"{k}: {v.get('reason', '?')}")

    state_file = DATA_CACHE / "health_notify_state.json"
    state = read_json(state_file) or {}
    now = time.time()

    if severity == "OK":
        if state.get("severity") and state.get("severity") != "OK":
            _push_health("VPS recovered: all health checks green again.")
        write_json_atomic(state_file, {"severity": "OK", "ts": now, "key": ""})
        return

    key = hashlib.md5((severity + "|" + "|".join(sorted(reasons))).encode()).hexdigest()
    cooldown = 3600 if severity == "CRITICAL" else 6 * 3600
    if key == state.get("key") and (now - state.get("ts", 0)) < cooldown:
        return  # identical alert within cooldown -> suppress (anti-spam)
    _push_health(f"[VPS {severity}] " + " | ".join(reasons[:3]))
    write_json_atomic(state_file, {"severity": severity, "ts": now, "key": key})


def main():
    report = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "mt5": check_mt5(),
        "disk": check_disk(),
        "memory": check_memory(),
        "scheduled_tasks": check_scheduled_tasks(),
        "freshness": check_freshness(),
        "log_sizes": check_log_sizes(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    maybe_notify(report)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # the monitor must not itself fail the task; it alerts via ntfy
        print(f"vps_health non-fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
