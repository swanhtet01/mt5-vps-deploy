"""Tests for the vps_health.py monitoring rails added in this lane.

Every check is driven against tmp_path by monkeypatching the module's path constants, so
nothing here reads C:\\ or reaches the broker. Two properties matter beyond the obvious
WARN/OK verdicts and are asserted throughout:

  * a check never raises -- a broken input becomes a WARN with a reason, following the
    file's existing pattern, because an exception in one check would take the whole
    health run (and its push) down with it;
  * measured numbers (ages, counts, exit codes) live in the payload, never in the reason,
    because maybe_notify hashes the reason strings to dedup and a number that changes every
    run would defeat the cooldown.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# vps_health.py imports `paths` as a sibling (both land in scripts\ on the VPS); in the repo
# the resolver lives under hotfix/scripts.
sys.path.insert(0, str(REPO_ROOT / "hotfix" / "scripts"))

import MetaTrader5 as mt5_stub  # noqa: E402  (conftest's stub; every call must be patched)
import vps_health as vh  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _no_measured_digits(reason: str) -> bool:
    """Task names carry the literal 'MT5'; anything else numeric in a reason is a leak."""
    return not re.search(r"\d", reason.replace("MT5", ""))


class TestCheckHeartbeat:
    @pytest.fixture(autouse=True)
    def _file(self, monkeypatch, tmp_path):
        self.path = tmp_path / "heartbeat.json"
        monkeypatch.setattr(vh, "HEARTBEAT_FILE", self.path)

    def _state(self, **overrides):
        state = {
            "ts": _iso(1), "url_set": True, "ping_ok": True, "ping_fail_streak": 0,
            "unhealthy_streak": 0, "mt5_status": "ok", "last_ping_utc": _iso(1),
        }
        state.update(overrides)
        _write_json(self.path, state)

    def test_fresh_and_pinging_is_ok(self):
        self._state()

        result = vh.check_heartbeat(now=NOW)

        assert result["status"] == "OK"
        assert result["age_minutes"] == 1.0
        assert result["url_set"] is True
        assert result["ping_fail_streak"] == 0

    def test_stale_file_warns_with_the_age_in_the_payload(self):
        self._state(ts=_iso(20))

        result = vh.check_heartbeat(now=NOW)

        assert result["status"] == "WARN"
        assert "stale" in result["reason"]
        assert result["age_minutes"] == 20.0
        assert _no_measured_digits(result["reason"])

    def test_fifteen_minutes_exactly_is_not_stale(self):
        self._state(ts=_iso(15))

        assert vh.check_heartbeat(now=NOW)["status"] == "OK"

    def test_missing_url_warns(self):
        self._state(url_set=False)

        result = vh.check_heartbeat(now=NOW)

        assert result["status"] == "WARN"
        assert "HEALTHCHECK_URL" in result["reason"]
        assert result["url_set"] is False

    def test_legacy_url_key_is_honoured(self):
        # Older heartbeat.json files only carry healthcheck_url_set.
        self._state(healthcheck_url_set=True)
        state = json.loads(self.path.read_text())
        del state["url_set"]
        _write_json(self.path, state)

        assert vh.check_heartbeat(now=NOW)["status"] == "OK"

    def test_three_consecutive_failed_pings_warn(self):
        self._state(ping_ok=False, ping_fail_streak=3)

        result = vh.check_heartbeat(now=NOW)

        assert result["status"] == "WARN"
        assert "keep failing" in result["reason"]
        assert result["ping_fail_streak"] == 3
        assert _no_measured_digits(result["reason"])

    def test_two_failed_pings_do_not_warn_yet(self):
        self._state(ping_ok=False, ping_fail_streak=2)

        assert vh.check_heartbeat(now=NOW)["status"] == "OK"

    def test_missing_file_warns(self):
        result = vh.check_heartbeat(now=NOW)

        assert result["status"] == "WARN"
        assert "never written" in result["reason"]

    def test_unreadable_file_warns_instead_of_raising(self):
        self.path.write_text("{not json", encoding="utf-8")

        result = vh.check_heartbeat(now=NOW)

        assert result["status"] == "WARN"
        assert "unreadable" in result["reason"]

    def test_naive_timestamp_is_reported_as_invalid(self):
        self._state(ts="2026-09-01T11:59:00")

        result = vh.check_heartbeat(now=NOW)

        assert result["status"] == "WARN"
        assert "invalid" in result["reason"]

    def test_several_problems_are_joined_into_one_reason(self):
        self._state(ts=_iso(30), url_set=False, ping_fail_streak=5)

        result = vh.check_heartbeat(now=NOW)

        assert result["reason"].count(";") == 2


class TestCheckTaskReceipts:
    @pytest.fixture(autouse=True)
    def _dir(self, monkeypatch, tmp_path):
        self.dir = tmp_path / "task_runs"
        monkeypatch.setattr(vh, "TASK_RUNS_DIR", self.dir)

    def _receipt(self, task, *, minutes_ago=1.0, ok=True, errors=None, finished=True, exit_code=0):
        started = NOW - timedelta(minutes=minutes_ago + 0.5)
        _write_json(self.dir / f"{task}.json", {
            "task": task,
            "started_utc": started.isoformat(),
            "finished_utc": _iso(minutes_ago) if finished else None,
            "ok": ok,
            "exit_code": exit_code,
            "mt5_init": True,
            "errors": errors or [],
            "duration_s": 30.0,
        })

    def _all_fresh(self):
        for task in vh.CADENCE_MINUTES:
            self._receipt(task)

    def test_every_task_fresh_and_ok(self):
        self._all_fresh()

        result = vh.check_task_receipts(now=NOW)

        assert result["status"] == "OK"
        assert set(result["tasks"]) == set(vh.CADENCE_MINUTES)
        assert all(entry["state"] == "ok" for entry in result["tasks"].values())

    def test_cadence_table_covers_the_expected_tasks(self):
        assert vh.CADENCE_MINUTES == {
            "MT5-IntradayMR": 30, "MT5-RemoteControl": 5, "MT5-PositionMonitor": 5,
            "MT5-Heartbeat": 5, "MT5-GoldDrift-KillSwitch": 60, "MT5-Maintenance": 1440,
        }

    def test_missing_receipt_is_never_run(self):
        self._all_fresh()
        (self.dir / "MT5-Heartbeat.json").unlink()

        result = vh.check_task_receipts(now=NOW)

        assert result["status"] == "WARN"
        assert result["never_run"] == ["MT5-Heartbeat"]
        assert "never run: MT5-Heartbeat" in result["reason"]
        assert result["tasks"]["MT5-Heartbeat"]["state"] == "never run"

    def test_no_receipts_at_all_warns_for_every_task(self):
        # Unwired tasks are the point: absence of the receipt is the finding.
        result = vh.check_task_receipts(now=NOW)

        assert result["status"] == "WARN"
        assert set(result["never_run"]) == set(vh.CADENCE_MINUTES)

    def test_receipt_older_than_three_cadences_is_stale(self):
        self._all_fresh()
        self._receipt("MT5-RemoteControl", minutes_ago=16)  # cadence 5 -> limit 15

        result = vh.check_task_receipts(now=NOW)

        assert result["status"] == "WARN"
        assert result["stale"] == ["MT5-RemoteControl"]
        assert "stale: MT5-RemoteControl" in result["reason"]
        assert result["tasks"]["MT5-RemoteControl"]["age_minutes"] == 16.0
        assert _no_measured_digits(result["reason"])

    def test_exactly_three_cadences_is_not_stale(self):
        self._all_fresh()
        self._receipt("MT5-RemoteControl", minutes_ago=15)
        self._receipt("MT5-Maintenance", minutes_ago=1440 * 3)

        assert vh.check_task_receipts(now=NOW)["status"] == "OK"

    def test_failed_receipt_names_the_exception_type_not_the_exit_code(self):
        self._all_fresh()
        self._receipt(
            "MT5-IntradayMR", ok=False, exit_code=7,
            errors=[{"type": "ConnectionError", "where": "mt5.initialize"}],
        )

        result = vh.check_task_receipts(now=NOW)

        assert result["status"] == "WARN"
        assert result["failed"] == ["MT5-IntradayMR (ConnectionError)"]
        assert "failed: MT5-IntradayMR (ConnectionError)" in result["reason"]
        assert result["tasks"]["MT5-IntradayMR"]["exit_code"] == 7
        assert result["tasks"]["MT5-IntradayMR"]["error_types"] == ["ConnectionError"]
        assert _no_measured_digits(result["reason"])

    def test_ok_false_without_errors_still_fails(self):
        self._all_fresh()
        self._receipt("MT5-Maintenance", ok=False, exit_code=1)

        result = vh.check_task_receipts(now=NOW)

        assert result["failed"] == ["MT5-Maintenance"]

    def test_errors_with_ok_true_still_fail(self):
        self._all_fresh()
        self._receipt("MT5-PositionMonitor", ok=True, errors=[{"type": "KeyError", "where": "loop"}])

        result = vh.check_task_receipts(now=NOW)

        assert result["failed"] == ["MT5-PositionMonitor (KeyError)"]

    def test_run_in_flight_is_not_a_failure(self):
        self._all_fresh()
        self._receipt("MT5-IntradayMR", ok=None, finished=False, exit_code=None)

        result = vh.check_task_receipts(now=NOW)

        assert result["status"] == "OK"
        assert result["tasks"]["MT5-IntradayMR"]["state"] == "running"

    def test_run_hung_past_three_cadences_is_stale(self):
        self._all_fresh()
        self._receipt("MT5-RemoteControl", ok=None, finished=False, minutes_ago=16)

        result = vh.check_task_receipts(now=NOW)

        assert result["stale"] == ["MT5-RemoteControl"]

    def test_unreadable_receipt_warns_instead_of_raising(self):
        self._all_fresh()
        (self.dir / "MT5-Heartbeat.json").write_text("{nope", encoding="utf-8")

        result = vh.check_task_receipts(now=NOW)

        assert result["status"] == "WARN"
        assert result["unreadable"] == ["MT5-Heartbeat"]

    def test_naive_timestamp_is_unreadable_not_a_crash(self):
        self._all_fresh()
        _write_json(self.dir / "MT5-Heartbeat.json", {
            "task": "MT5-Heartbeat", "started_utc": "2026-09-01T11:59:00",
            "finished_utc": "2026-09-01T11:59:30", "ok": True, "exit_code": 0,
            "mt5_init": True, "errors": [], "duration_s": 30.0,
        })

        result = vh.check_task_receipts(now=NOW)

        assert result["unreadable"] == ["MT5-Heartbeat"]
        assert result["tasks"]["MT5-Heartbeat"]["state"] == "invalid timestamp"


class TestCheckInstalledTree:
    @pytest.fixture(autouse=True)
    def _tree(self, monkeypatch, tmp_path):
        self.root = tmp_path / "trading-agent"
        self.manifest = tmp_path / "mt5-deploy" / "installed-manifest.json"
        monkeypatch.setattr(vh, "REPO_ROOT", self.root)
        monkeypatch.setattr(vh, "INSTALLED_MANIFEST", self.manifest)
        (self.root / "scripts").mkdir(parents=True)
        self.files = {}
        for name, body in (("scripts/a.py", b"print('a')\n"), ("scripts/b.py", b"print('b')\n")):
            (self.root / name).write_bytes(body)
            self.files[name] = hashlib.sha256(body).hexdigest()

    def _write_manifest(self, files=None, **extra):
        payload = {
            "deploy_sha": "b07d0a8", "applied_utc": _iso(60),
            "files": [
                {"destination": d, "sha256": s}
                for d, s in (self.files if files is None else files).items()
            ],
        }
        payload.update(extra)
        _write_json(self.manifest, payload)

    def test_matching_tree_is_ok(self):
        self._write_manifest()

        result = vh.check_installed_tree()

        assert result["status"] == "OK"
        assert result["checked"] == 2
        assert result["differing_count"] == 0
        assert result["deploy_sha"] == "b07d0a8"

    def test_one_byte_change_warns_with_the_file_in_the_payload(self):
        self._write_manifest()
        (self.root / "scripts/a.py").write_bytes(b"print('A')\n")

        result = vh.check_installed_tree()

        assert result["status"] == "WARN"
        assert result["reason"] == "installed tree differs from deployed manifest"
        assert result["changed"] == ["scripts/a.py"]
        assert result["missing"] == []
        assert result["differing_count"] == 1
        assert _no_measured_digits(result["reason"])

    def test_absent_file_warns_as_missing(self):
        self._write_manifest()
        (self.root / "scripts/b.py").unlink()

        result = vh.check_installed_tree()

        assert result["status"] == "WARN"
        assert result["missing"] == ["scripts/b.py"]
        assert result["changed"] == []
        assert result["differing_count"] == 1

    def test_absent_manifest_warns_never_applied(self):
        result = vh.check_installed_tree()

        assert result["status"] == "WARN"
        assert result["reason"] == "no manifest has ever been applied"

    def test_manifest_without_files_warns(self):
        self._write_manifest(files={})

        result = vh.check_installed_tree()

        assert result["status"] == "WARN"
        assert "no file list" in result["reason"]

    def test_unparseable_manifest_warns_instead_of_raising(self):
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text("{nope", encoding="utf-8")

        result = vh.check_installed_tree()

        assert result["status"] == "WARN"

    def test_absolute_destination_is_hashed_in_place(self):
        absolute = str(self.root / "scripts" / "a.py")
        self._write_manifest(files={absolute: self.files["scripts/a.py"]})

        assert vh.check_installed_tree()["status"] == "OK"

    def test_sha_comparison_is_case_insensitive(self):
        self._write_manifest(files={k: v.upper() for k, v in self.files.items()})

        assert vh.check_installed_tree()["status"] == "OK"

    def test_entry_without_hash_is_reported_not_skipped(self):
        self._write_manifest()
        payload = json.loads(self.manifest.read_text())
        payload["files"].append({"destination": "scripts/c.py"})
        _write_json(self.manifest, payload)

        result = vh.check_installed_tree()

        assert result["status"] == "WARN"
        assert result["invalid_entries"] == ["scripts/c.py"]


class TestCheckMt5Terminal:
    def _broker(self, monkeypatch, *, connected=True, trade_allowed=True, live=False):
        monkeypatch.setattr(mt5_stub, "initialize", lambda: True)
        monkeypatch.setattr(mt5_stub, "shutdown", lambda: None)
        monkeypatch.setattr(mt5_stub, "account_info", lambda: SimpleNamespace(
            login=12345678, balance=600.0, equity=600.0, trade_allowed=True, leverage=500,
        ))
        monkeypatch.setattr(
            mt5_stub, "terminal_info",
            lambda: SimpleNamespace(connected=connected, trade_allowed=trade_allowed),
            raising=False,
        )
        monkeypatch.setattr(vh, "persistent_user_flag_enabled", lambda name: live)

    def test_connected_paper_box_is_ok_even_with_autotrading_off(self, monkeypatch):
        self._broker(monkeypatch, trade_allowed=False, live=False)

        result = vh.check_mt5()

        assert result["status"] == "OK"
        assert result["terminal_connected"] is True
        assert result["terminal_trade_allowed"] is False
        assert result["live_armed"] is False

    def test_disconnected_terminal_warns(self, monkeypatch):
        self._broker(monkeypatch, connected=False)

        result = vh.check_mt5()

        assert result["status"] == "WARN"
        assert "not connected" in result["reason"]

    def test_autotrading_off_while_armed_warns(self, monkeypatch):
        self._broker(monkeypatch, trade_allowed=False, live=True)

        result = vh.check_mt5()

        assert result["status"] == "WARN"
        assert "AutoTrading is off while live trading is armed" in result["reason"]

    def test_autotrading_on_while_armed_is_ok(self, monkeypatch):
        self._broker(monkeypatch, trade_allowed=True, live=True)

        assert vh.check_mt5()["status"] == "OK"

    def test_init_failure_is_still_critical(self, monkeypatch):
        monkeypatch.setattr(mt5_stub, "initialize", lambda: False)
        monkeypatch.setattr(mt5_stub, "shutdown", lambda: None)
        monkeypatch.setattr(mt5_stub, "last_error", lambda: (-10005, "IPC timeout"))

        assert vh.check_mt5()["status"] == "CRITICAL"


class TestCheckAlerting:
    def test_missing_healthcheck_url_warns(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "swann-mt5-x7q9k")
        monkeypatch.delenv("HEALTHCHECK_URL", raising=False)

        result = vh.check_alerting()

        assert result["status"] == "WARN"
        assert "HEALTHCHECK_URL" in result["reason"]
        assert result["healthcheck_url_set"] is False
        assert result["configured"] is True

    def test_both_channels_configured_is_ok(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC", "swann-mt5-x7q9k")
        monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/abc")

        result = vh.check_alerting()

        assert result["status"] == "OK"
        assert result["healthcheck_url_set"] is True

    def test_unset_ntfy_topic_stays_critical(self, monkeypatch):
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
        monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/abc")

        result = vh.check_alerting()

        assert result["status"] == "CRITICAL"
        assert result["healthcheck_url_set"] is True


class TestIntradayMrIsWatched:
    def test_intraday_mr_is_a_critical_task(self, monkeypatch):
        csv = '"\\MT5-Heartbeat","Next","Ready"\n'
        monkeypatch.setattr(
            vh.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=csv, stderr=""),
        )
        monkeypatch.setattr(vh, "task_last_results", lambda: {})

        result = vh.check_scheduled_tasks()

        assert result["status"] == "WARN"
        assert "MT5-IntradayMR" in result["critical_down"]

    def test_intraday_mr_log_dir_is_sized(self):
        assert any(d.name == "intraday-mr" for d in vh.LOG_DIRS)


class TestAlertLedger:
    @pytest.fixture(autouse=True)
    def _ledger(self, monkeypatch, tmp_path):
        self.ledger = tmp_path / "alert_ledger.jsonl"
        monkeypatch.setattr(vh, "ALERT_LEDGER", self.ledger)
        self.sent: list[str] = []
        self.rc = {"code": 1}

        def fake_run(cmd, **kwargs):
            self.sent.append(cmd[-1])
            return SimpleNamespace(returncode=self.rc["code"], stdout=b"", stderr=b"")

        monkeypatch.setattr(vh.subprocess, "run", fake_run)

    def _entries(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]

    def test_delivered_push_is_recorded(self):
        self.rc["code"] = 0

        assert vh._push_health("[VPS WARN] disk", severity="WARN") is True

        (entry,) = self._entries()
        assert entry["delivered"] is True
        assert entry["severity"] == "WARN"
        assert entry["ts"].endswith("+00:00")

    def test_undelivered_push_is_recorded_and_returns_false(self):
        self.rc["code"] = 1

        assert vh._push_health("[VPS WARN] disk", severity="WARN") is False

        (entry,) = self._entries()
        assert entry["delivered"] is False
        assert self.sent == ["[VPS WARN] disk"], "no prefix while nothing has failed yet"

    def test_notify_crash_is_recorded_as_undelivered(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("python missing")
        monkeypatch.setattr(vh.subprocess, "run", boom)

        assert vh._push_health("x", severity="CRITICAL") is False
        assert self._entries()[-1]["delivered"] is False

    def test_next_delivered_push_carries_the_undelivered_count(self):
        self.rc["code"] = 1
        vh._push_health("[VPS WARN] first", severity="WARN")
        vh._push_health("[VPS WARN] second", severity="WARN")
        first_ts = self._entries()[0]["ts"]

        self.rc["code"] = 0
        vh._push_health("[VPS WARN] third", severity="WARN")

        assert self.sent[2] == f"2 alert(s) undelivered since {first_ts} | [VPS WARN] third"
        assert self._entries()[-1]["delivered"] is True

        vh._push_health("[VPS OK] recovered", severity="OK")
        assert self.sent[3] == "[VPS OK] recovered", "the count resets once something lands"

    def test_count_only_covers_entries_since_the_last_delivery(self):
        self.rc["code"] = 1
        vh._push_health("a")
        self.rc["code"] = 0
        vh._push_health("b")
        self.rc["code"] = 1
        vh._push_health("c")
        self.rc["code"] = 0
        vh._push_health("d")

        assert self.sent[3].startswith("1 alert(s) undelivered since ")

    def test_ledger_is_capped_at_five_hundred_lines(self):
        self.ledger.write_text(
            "".join(json.dumps({"ts": str(i), "severity": "WARN", "delivered": True}) + "\n"
                    for i in range(vh.ALERT_LEDGER_MAX_LINES)),
            encoding="utf-8",
        )
        self.rc["code"] = 0

        vh._push_health("x", severity="WARN")

        entries = self._entries()
        assert len(entries) == vh.ALERT_LEDGER_MAX_LINES
        assert entries[0]["ts"] == "1", "the oldest line is the one trimmed"
        assert entries[-1]["severity"] == "WARN"

    def test_corrupt_ledger_line_is_skipped_not_fatal(self):
        self.ledger.write_text('{"ts": "t0", "delivered": false}\nnot json\n', encoding="utf-8")
        self.rc["code"] = 0

        vh._push_health("x")

        assert self.sent[0].startswith("1 alert(s) undelivered since t0 | ")
        assert len(self._entries()) == 2

    def test_maybe_notify_passes_the_severity_through(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vh, "DATA_CACHE", tmp_path)
        pushes = []
        monkeypatch.setattr(vh, "_push_health", lambda msg, severity="INFO": pushes.append((msg, severity)))

        vh.maybe_notify({"ts": "x", "disk": {"status": "WARN", "reason": "disk free < 2GB"}})
        vh.maybe_notify({"ts": "x", "disk": {"status": "OK"}})

        assert [severity for _, severity in pushes] == ["WARN", "OK"]
        assert pushes[1][0].startswith("VPS recovered")
