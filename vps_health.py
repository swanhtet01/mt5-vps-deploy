"""VPS health monitor — runs on the VPS and watches itself.

Checks:
  1. MT5 terminal: reachable, logged in, account responsive
  2. Disk space: warn if free < 2GB, critical if < 500MB
  3. Memory: warn if available < 500MB
  4. Scheduled tasks: count MT5-* tasks; warn if any in error/failed state
  5. Critical files: news_state.json freshness, blacklist.json present
  6. Log directory size: warn if > 1GB (daily maintenance rotates individual logs)
  7. Optional Vibe sidecar: audited commit, HALT sentinel, and weekly run freshness

Outputs:
  - data_cache/vps_health.json
  - On any WARN or CRITICAL state: notify.py sends a push notification

Runs every 30 minutes via MT5-VPS-Health scheduled task."""

from __future__ import annotations

import json
import hashlib
import re
import os
import shutil
import sys
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import persistent_user_flag_enabled
from mt5_agent.profit_funded_scaling import SCHEMA as PROFIT_SCALING_SCHEMA

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
SCHEDULER_EVENTS = PAPER_ROOT / "analytics" / "structural-scheduler.jsonl"
SCHEDULER_ALLOWLIST = DATA_CACHE / "structural_live_allowlist.json"
VIBE_ROOT = Path(os.environ.get("MT5_VIBE_ROOT", r"C:\mt5-vibe-research"))
AUDITED_VIBE_COMMIT = "652917e74e2b2e1f767ef596623bae7f098a53c4"
VIBE_BASELINE_MAX_AGE_HOURS = 36.0
VIBE_AGENT_MAX_AGE_HOURS = 8.0 * 24.0
VIBE_SHADOW_STATE = DATA_CACHE / "vibe_shadow_forward_state.json"
VIBE_SHADOW_REPORT = DATA_CACHE / "vibe_shadow_forward_report.json"
VIBE_SHADOW_MAX_AGE_MINUTES = 20.0
PROFIT_SCALING_FILE = DATA_CACHE / "position_sizing.json"
VIBE_DENIED_TOOL_FRAGMENTS = (
    "order", "trading_", "connector", "mandate", "bash", "shell", "write", "background",
)


def check_mt5():
    try:
        if not mt5.initialize():
            return {"status": "CRITICAL", "reason": f"mt5.initialize: {mt5.last_error()}"}
        ai = mt5.account_info()
        if ai is None:
            return {"status": "CRITICAL", "reason": "account_info returned None"}
        return {
            "status": "OK",
            "login_last4": str(ai.login)[-4:], "balance": ai.balance, "equity": ai.equity,
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


# LastTaskResult values that are NOT failures: success, currently running, and never yet run
# (0x41301 / 0x41303). A fresh install legitimately reports "not yet run" for a while.
_BENIGN_TASK_RESULTS = {0, 267009, 267011}


def task_last_results():
    """Map MT5-* task name -> LastTaskResult (its last EXIT CODE).

    schtasks /query reports Ready/Running/Disabled -- the SCHEDULE state, not the outcome.
    A task that has exited non-zero on every run for a week still reads Ready, which is
    exactly how a broken safety script stays invisible: killswitch_monitor.py and this file
    both exit 1 on failure now, and nothing was reading those exit codes.

    Get-ScheduledTaskInfo is used rather than `schtasks /query /v`, whose column HEADERS are
    localised -- matching on the string "Last Result" would silently find nothing on a
    non-English box, which is the same class of silent failure being fixed here. status.ps1
    already reads LastTaskResult this way.
    """
    script = (
        "Get-ScheduledTask -TaskName 'MT5-*' -ErrorAction SilentlyContinue | "
        "Get-ScheduledTaskInfo | "
        "Select-Object TaskName, LastTaskResult | ConvertTo-Json -Compress"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=25,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    payload = json.loads(proc.stdout)
    # ConvertTo-Json emits a bare object when there is exactly one task, a list otherwise.
    if isinstance(payload, dict):
        payload = [payload]
    results = {}
    for row in payload:
        name = str(row.get("TaskName", "")).lstrip("\\")
        code = row.get("LastTaskResult")
        if name and isinstance(code, int):
            results[name] = code
    return results


def check_scheduled_tasks():
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/fo", "CSV", "/nh"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return {"status": "WARN", "reason": "schtasks query failed"}
        # Legacy structural entry/exit tasks are deliberately disabled. One broker-clock
        # scheduler replaces them and must exist; missing tasks count as down too.
        critical = {
            "MT5-StructuralScheduler", "MT5-GoldDrift-KillSwitch", "MT5-Heartbeat",
            "MT5-Watchdog", "MT5-PositionMonitor", "MT5-ContextIngest",
            "MT5-LLMThesis", "MT5-ApplyThesis", "MT5-AutoDeploy", "MT5-VPS-Health",
            "MT5-Maintenance",
        }
        if (VIBE_ROOT / "install.json").exists():
            critical.add("MT5-VibeBaseline")
            critical.add("MT5-VibeShadow")
        if read_json(PROFIT_SCALING_FILE).get("schema") == PROFIT_SCALING_SCHEMA:
            critical.add("MT5-ProfitFundedScaling")
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
        ready = {item["name"] for item in mt5_tasks if item["status"] in ("Ready", "Running")}
        critical_down = sorted(critical - ready)

        # Being scheduled is not the same as working. Read the last exit code too, so a
        # critical task that fires on time and fails every single time is not reported OK.
        try:
            last_results = task_last_results()
        except Exception as exc:  # never let the outcome probe break the state check
            last_results = {}
            failing = []
            probe_error = f"{type(exc).__name__}: {exc}"
        else:
            probe_error = None
            failing = sorted(
                name for name, code in last_results.items()
                if name in critical and code not in _BENIGN_TASK_RESULTS
            )

        problems = []
        if probe_error:
            # Could not read the outcomes at all. Reporting OK here would be the same
            # mistake this probe exists to correct -- "we did not check" is not "it is fine".
            problems.append("cannot read task exit codes")
        if critical_down:
            problems.append("critical task(s) not Ready: " + ", ".join(critical_down))
        if failing:
            problems.append("critical task(s) failing their last run: " + ", ".join(
                f"{name} (exit {last_results[name]})" for name in failing))

        if problems:
            out = {
                "status": "WARN",
                "total": len(mt5_tasks),
                "reason": "; ".join(problems),
                "critical_down": critical_down,
                "critical_failing": failing,
                "disabled": not_ready,
            }
            if probe_error:
                out["last_result_probe_error"] = probe_error
            return out
        return {
            "status": "OK",
            "total": len(mt5_tasks),
            "disabled_noncritical": not_ready,
            "last_results_read": len(last_results),
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
    # MT5-SymbolScanner is a 120-minute weekly task whose output nothing was checking, so a
    # scan that stopped producing results was invisible. That is not hypothetical: the scan
    # pulls history from Yahoo via yfinance, and Yahoo rate-limits datacenter IPs -- it 429s
    # from this project's sandbox, and the VPS is a datacenter IP too. A silently dead scan
    # means no new edges are ever discovered while the task keeps reporting Ready.
    # Threshold is 8 days: the task runs weekly, so anything older has MISSED a run. On a
    # fresh box the file is legitimately absent until the first Sunday; that WARNs, which is
    # honest, and clears itself after one scan.
    scan_summary = DATA_CACHE / "edge_discovery_summary.json"
    if scan_summary.exists():
        try:
            age_days = (now.timestamp() - scan_summary.stat().st_mtime) / 86400.0
            out["edge_scan_age_days"] = round(age_days, 1)
            out["edge_scan_status"] = "WARN" if age_days > 8 else "OK"
        except OSError as exc:
            out["edge_scan_status"] = "WARN"
            out["edge_scan_error"] = str(exc)
    else:
        out["edge_scan_status"] = "WARN"
        out["edge_scan_error"] = "edge_discovery_summary.json missing (scan has never produced output)"

    out["blacklist_present"] = BLACKLIST_FILE.exists()
    # maybe_notify only inspects a top-level "status", so without this the whole check was
    # unalertable: a missing or stale news_state.json set news_status and contributed nothing
    # to severity. The news gate fails OPEN on a missing file, so nobody finding out is
    # precisely the case worth paging about.
    problems = []
    if out.get("news_status") == "WARN":
        problems.append("news state " + str(out.get("news_error", "is stale")))
    if not out["blacklist_present"]:
        problems.append("blacklist.json missing")
    if out.get("edge_scan_status") == "WARN":
        problems.append("weekly edge scan " + str(out.get("edge_scan_error", "output is stale")))
    if problems:
        out["status"] = "WARN"
        out["reason"] = "; ".join(problems)
    else:
        out["status"] = "OK"
    return out


def check_structural_scheduler():
    live_flag = persistent_user_flag_enabled("MT5_GOLD_DRIFT_LIVE")
    scheduler_flag = persistent_user_flag_enabled("MT5_STRUCTURAL_SCHEDULER_LIVE")
    force_paper_only = os.environ.get("MT5_STRUCTURAL_FORCE_PAPER_ONLY", "").strip() == "1"
    allowlist = read_json(SCHEDULER_ALLOWLIST, default={})
    enabled = []
    if isinstance(allowlist, dict):
        for value in allowlist.get("enabled_magics", []):
            try:
                magic = int(value)
            except (TypeError, ValueError):
                continue
            if 88001 <= magic <= 88009:
                enabled.append(magic)

    result = {
        "status": "OK",
        "mode": "FORCED_PAPER" if force_paper_only else ("LIVE" if live_flag and scheduler_flag else "PAPER"),
        "allowlisted_magics": sorted(set(enabled)),
        "gold_live_flag": live_flag,
        "scheduler_live_flag": scheduler_flag,
        "force_paper_only": force_paper_only,
    }
    if not force_paper_only and live_flag != scheduler_flag:
        result.update(status="WARN", reason="structural live flags are only partially armed")
    elif not force_paper_only and live_flag and scheduler_flag and not enabled:
        result.update(status="WARN", reason="scheduler is armed but no strategy magic is allowlisted")

    if not SCHEDULER_EVENTS.exists():
        result.update(status="WARN", reason="structural scheduler has not written an event heartbeat")
        return result
    try:
        age_minutes = (
            datetime.now(tz=timezone.utc).timestamp() - SCHEDULER_EVENTS.stat().st_mtime
        ) / 60.0
        result["event_age_min"] = round(age_minutes, 1)
        if age_minutes > 20:
            # The age must NOT go in the reason: maybe_notify hashes the reason strings to
            # dedup, so a number that changes every run made a new key every run and the 6h
            # cooldown never applied -- 48 pushes/day from one chronic warning. The value is
            # already reported as event_age_min, which is not part of the key.
            result.update(status="WARN", reason="structural scheduler event heartbeat is stale")
    except OSError as exc:
        result.update(status="WARN", reason=f"cannot stat structural scheduler events: {exc}")
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _state_age_hours(value, reference: datetime) -> float | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return max((reference - stamp.astimezone(timezone.utc)).total_seconds(), 0.0) / 3600.0
    except ValueError:
        return None


def check_vibe_sidecar(now: datetime | None = None):
    install_path = VIBE_ROOT / "install.json"
    if not install_path.exists():
        return {"status": "OK", "installed": False, "mode": "not_installed"}
    install = read_json(install_path, default={})
    result = {
        "status": "OK",
        "installed": True,
        "mode": "research_only",
        "commit": install.get("commit"),
        "halt_present": (VIBE_ROOT / "live" / "HALT").exists(),
        "order_authority": bool(install.get("order_authority", True)),
        "deterministic_baseline": bool(install.get("deterministic_baseline", False)),
    }
    blockers = []
    if install.get("commit") != AUDITED_VIBE_COMMIT:
        blockers.append("audited Vibe commit mismatch")
    if not result["halt_present"]:
        blockers.append("Vibe global HALT sentinel missing")
    if result["order_authority"]:
        blockers.append("Vibe install metadata grants order authority")
    if not result["deterministic_baseline"]:
        blockers.append("Vibe install metadata predates the deterministic baseline")

    tool_audit = read_json(VIBE_ROOT / "tool-audit.json", default={})
    audited_tools = tool_audit.get("tools") if isinstance(tool_audit, dict) else None
    if (
        tool_audit.get("schema") != "mt5.vibe_tool_audit.v1"
        or tool_audit.get("order_authority") is not False
        or not isinstance(audited_tools, list)
        or not audited_tools
        or tool_audit.get("tool_count") != len(audited_tools)
    ):
        blockers.append("Vibe safe-tool audit is missing or invalid")
    else:
        denied_tools = sorted(
            str(tool)
            for tool in audited_tools
            if any(fragment in str(tool).casefold() for fragment in VIBE_DENIED_TOOL_FRAGMENTS)
        )
        if denied_tools:
            blockers.append("Vibe safe-tool audit contains authority surfaces")
            result["denied_tools"] = denied_tools
        result["tool_count"] = len(audited_tools)

    reference = now or datetime.now(tz=timezone.utc)
    run = read_json(VIBE_ROOT / "last-run.json", default={})
    result["last_run_status"] = run.get("status")
    if run.get("status") == "failed":
        blockers.append("last Vibe invocation failed")

    baseline = read_json(VIBE_ROOT / "last-baseline.json", default={})
    result["baseline_status"] = baseline.get("status")
    baseline_age = _state_age_hours(baseline.get("finished_at"), reference)
    if baseline_age is None:
        blockers.append("Vibe deterministic baseline has never completed")
    else:
        result["baseline_age_hours"] = round(baseline_age, 1)
        if baseline_age > VIBE_BASELINE_MAX_AGE_HOURS:
            blockers.append("Vibe deterministic baseline is older than 36 hours")
    if baseline.get("status") != "completed":
        blockers.append("Vibe deterministic baseline status is not completed")
    if baseline.get("order_authority") is not False:
        blockers.append("Vibe baseline state grants order authority")

    reports_root = VIBE_ROOT / "reports"
    report_path = Path(str(baseline.get("report") or ""))
    handoff_path = Path(str(baseline.get("handoff") or ""))
    screen_path = Path(str(baseline.get("candidate_screen") or ""))
    chart_path = Path(str(baseline.get("chart") or ""))
    if not report_path.is_file() or not _within(report_path, reports_root):
        blockers.append("Vibe baseline report is missing or outside the reports root")
    else:
        if _file_sha256(report_path) != baseline.get("report_sha256"):
            blockers.append("Vibe baseline report hash mismatch")
        report = read_json(report_path, default={})
        if (
            report.get("schema") != "mt5.vibe_deterministic_research.v1"
            or report.get("mode") != "research_only"
            or report.get("order_authority") is not False
            or report.get("data_quality_status") != "PASS"
            or report.get("trade_ledger", {}).get("automated_monthly_history", {}).get("interpretation")
            != "historical_resampling_not_forecast"
        ):
            blockers.append("Vibe baseline report contract is invalid")
        result["symbols_loaded"] = report.get("vibe_loader", {}).get("symbols_loaded")
    if not handoff_path.is_file() or not _within(handoff_path, reports_root):
        blockers.append("Vibe deterministic handoff is missing or outside the reports root")
    else:
        if _file_sha256(handoff_path) != baseline.get("handoff_sha256"):
            blockers.append("Vibe deterministic handoff hash mismatch")
        handoff = read_json(handoff_path, default={})
        candidates = handoff.get("candidates") if isinstance(handoff, dict) else None
        if (
            handoff.get("schema") != "mt5.vibe_candidate_handoff.v1"
            or handoff.get("research_only") is not True
            or handoff.get("order_authority") is not False
            or handoff.get("automatic_live_promotion") is not False
            or not isinstance(candidates, list)
            or any(
                not isinstance(item, dict)
                or item.get("stage") != "DISCOVERED"
                or item.get("live_eligible") is not False
                for item in (candidates or [])
            )
        ):
            blockers.append("Vibe deterministic handoff contract is invalid")
        result["candidate_count"] = len(candidates or [])
    if not screen_path.is_file() or not _within(screen_path, reports_root):
        blockers.append("Vibe candidate screen is missing or outside the reports root")
    else:
        if _file_sha256(screen_path) != baseline.get("candidate_screen_sha256"):
            blockers.append("Vibe candidate screen hash mismatch")
        screen = read_json(screen_path, default={})
        screen_results = screen.get("results") if isinstance(screen, dict) else None
        if (
            screen.get("schema") != "mt5.vibe_candidate_screen.v1"
            or screen.get("mode") != "historical_research_only"
            or screen.get("order_authority") is not False
            or screen.get("automatic_live_promotion") is not False
            or screen.get("forecast_generated") is not False
            or screen.get("paper_candidate_count") != 0
            or screen.get("live_eligible_count") != 0
            or not isinstance(screen_results, list)
            or any(
                not isinstance(item, dict)
                or item.get("candidate_stage") != "DISCOVERED"
                or item.get("paper_candidate") is not False
                or item.get("live_eligible") is not False
                for item in (screen_results or [])
            )
        ):
            blockers.append("Vibe candidate screen contract is invalid")
        result["historical_screen_trials"] = screen.get("family_trials")
        result["historical_screen_pass_count"] = screen.get("historical_screen_pass_count")
        result["paper_candidate_count"] = screen.get("paper_candidate_count")
        result["live_eligible_count"] = screen.get("live_eligible_count")
    if not chart_path.is_file() or not _within(chart_path, reports_root):
        blockers.append("Vibe baseline chart is missing or outside the reports root")

    provider_ready = (VIBE_ROOT / "secrets" / "anthropic.dpapi").is_file()
    result["provider_ready"] = provider_ready
    agent = read_json(VIBE_ROOT / "last-agent-run.json", default={})
    result["agent_status"] = agent.get("status") or "not_configured"
    if provider_ready:
        agent_age = _state_age_hours(agent.get("finished_at"), reference)
        if agent.get("status") != "completed" or agent_age is None:
            blockers.append("Vibe provider is configured but no agent handoff has completed")
        else:
            result["agent_age_hours"] = round(agent_age, 1)
            if agent_age > VIBE_AGENT_MAX_AGE_HOURS:
                blockers.append("Vibe agent handoff is older than 8 days")
    if blockers:
        result.update(status="WARN", reason="; ".join(blockers), blockers=blockers)
    return result


def check_vibe_shadow(now: datetime | None = None):
    """Validate the autonomous quote-only shadow heartbeat and authority boundary."""
    if not (VIBE_ROOT / "install.json").exists():
        return {"status": "OK", "installed": False, "mode": "not_installed"}
    result = {
        "status": "OK",
        "installed": True,
        "mode": "quote_only_shadow",
        "order_authority": False,
        "live_eligible_count": 0,
    }
    blockers: list[str] = []
    reference = now or datetime.now(tz=timezone.utc)
    state = read_json(VIBE_SHADOW_STATE, default={})
    report = read_json(VIBE_SHADOW_REPORT, default={})
    if (
        state.get("schema") != "mt5.vibe_shadow_forward_state.v1"
        or state.get("mode") != "quote_only_shadow"
        or state.get("order_authority") is not False
        or state.get("automatic_live_promotion") is not False
        or not isinstance(state.get("open_positions"), list)
        or not isinstance(state.get("closed_trades"), list)
    ):
        blockers.append("Vibe shadow state is missing or invalid")
    else:
        invalid_open = [
            item for item in state["open_positions"]
            if not isinstance(item, dict)
            or item.get("paper_only") is not True
            or item.get("order_authority") is not False
        ]
        if invalid_open:
            blockers.append("Vibe shadow state contains a position outside the paper boundary")
        result["open_position_count"] = len(state["open_positions"])
        result["closed_trade_count"] = len(state["closed_trades"])
    if (
        report.get("schema") != "mt5.vibe_shadow_forward_report.v1"
        or report.get("mode") != "quote_only_shadow"
        or report.get("order_authority") is not False
        or report.get("automatic_live_promotion") is not False
        or report.get("manual_live_authorization_present") is not False
        or report.get("live_eligible_count") != 0
        or not isinstance(report.get("experiments"), list)
        or any(
            not isinstance(item, dict)
            or item.get("live_eligible") is not False
            or item.get("automatic_live_promotion") is not False
            for item in (report.get("experiments") or [])
        )
    ):
        blockers.append("Vibe shadow report is missing or invalid")
    else:
        report_age = _state_age_hours(report.get("generated_at_host_utc"), reference)
        if report_age is None:
            blockers.append("Vibe shadow heartbeat timestamp is invalid")
        else:
            result["heartbeat_age_minutes"] = round(report_age * 60.0, 1)
            if report_age * 60.0 > VIBE_SHADOW_MAX_AGE_MINUTES:
                blockers.append("Vibe shadow heartbeat is older than 20 minutes")
        result["artifact_status"] = report.get("artifact_status")
        result["experiment_count"] = report.get("experiment_count")
        result["active_entry_experiment_count"] = report.get("active_entry_experiment_count")
        result["paper_net_pnl_usd"] = report.get("paper_net_pnl_usd")
        result["paper_unrealized_net_if_closed_usd"] = report.get(
            "paper_unrealized_net_if_closed_usd"
        )
        result["paper_total_net_if_closed_usd"] = report.get(
            "paper_total_net_if_closed_usd"
        )
        result["paper_evidence_gate_pass_count"] = report.get("paper_evidence_gate_pass_count")
        if report.get("artifact_status") != "PASS":
            blockers.append("Vibe shadow entries are blocked by invalid or stale research artifacts")
    if blockers:
        result.update(status="WARN", reason="; ".join(blockers), blockers=blockers)
    return result


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


def check_profit_funded_scaling(now: datetime | None = None) -> dict:
    reference = now or datetime.now(tz=timezone.utc)
    payload = read_json(PROFIT_SCALING_FILE) or {}
    result = {
        "status": "OK",
        "schema": payload.get("schema"),
        "artifact": str(PROFIT_SCALING_FILE),
    }
    blockers: list[str] = []
    if payload.get("schema") != PROFIT_SCALING_SCHEMA:
        blockers.append("profit-funded sizing artifact is missing or invalid")
    else:
        max_age_hours = min(max(float(payload.get("max_age_hours") or 0.0), 1.0), 26.0)
        age_hours = _state_age_hours(payload.get("as_of"), reference)
        if age_hours is None:
            blockers.append("profit-funded sizing timestamp is invalid")
        else:
            result["age_hours"] = round(age_hours, 2)
            result["max_age_hours"] = max_age_hours
            if age_hours > max_age_hours:
                blockers.append("profit-funded sizing artifact is stale")

        guard = payload.get("account_guard") or {}
        try:
            account_cap = float(guard.get("multiplier_cap"))
        except (TypeError, ValueError):
            account_cap = 0.0
        result["account_guard_status"] = guard.get("status")
        result["account_multiplier_cap"] = account_cap
        if not 1.0 <= account_cap <= 3.0:
            blockers.append("profit-funded account cap is outside 1x to 3x")

        recommendations = payload.get("recommendations") or {}
        result["recommendation_count"] = len(recommendations)
        invalid = []
        promoted = []
        for magic, recommendation in recommendations.items():
            try:
                multiplier = float(recommendation.get("lot_multiplier"))
            except (AttributeError, TypeError, ValueError):
                invalid.append(str(magic))
                continue
            if not 1.0 <= multiplier <= 3.0:
                invalid.append(str(magic))
            elif multiplier > 1.0 and recommendation.get("promotion_authorized") is not True:
                invalid.append(str(magic))
            elif multiplier > 1.0:
                promoted.append(str(magic))
        if not recommendations:
            blockers.append("profit-funded sizing has no strategy recommendations")
        if invalid:
            blockers.append("invalid or unauthorized promoted sizing records")
        result["promoted_magics"] = promoted
        result["invalid_magics"] = invalid
    if blockers:
        result.update(status="WARN", reason="; ".join(blockers), blockers=blockers)
    return result


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

    # Digits are normalised out of the KEY (never out of the pushed message) so that a reason
    # carrying a measured value -- an age, a byte count, an errno -- dedups on the KIND of
    # problem rather than reading as a brand-new alert every run. Without this the cooldown
    # below silently does nothing for exactly the chronic warnings it exists to damp.
    fingerprint = re.sub(r"\d+", "#", severity + "|" + "|".join(sorted(reasons)))
    key = hashlib.md5(fingerprint.encode()).hexdigest()
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
        "structural_scheduler": check_structural_scheduler(),
        "vibe_sidecar": check_vibe_sidecar(),
        "vibe_shadow": check_vibe_shadow(),
        "profit_funded_scaling": check_profit_funded_scaling(),
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
    except Exception as exc:
        # The old comment here said the monitor must not fail its own task because "it alerts
        # via ntfy" -- but a throw out of main() means maybe_notify was never reached, so it
        # alerted via nothing and still exited 0. A health monitor that cannot report its own
        # failure is not a monitor. Push first (best effort), then fail the task honestly.
        print(f"vps_health FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            _push_health(f"[VPS CRITICAL] health monitor itself failed: {type(exc).__name__}: {exc}")
        except Exception:
            pass
        sys.exit(1)
