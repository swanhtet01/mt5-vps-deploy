"""Tests for task run receipts (hotfix/scripts/task_receipt.py) and their wiring.

Another lane's health check reads data_cache/task_runs/<Task>.json, so the schema is
asserted key-for-key and in order. The receipt must never take a task down with it.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "hotfix" / "scripts"))

from task_receipt import TaskReceipt, run_receipt  # noqa: E402

SCHEMA_KEYS = ["task", "started_utc", "finished_utc", "ok", "exit_code", "mt5_init",
               "errors", "duration_s"]


def _read(tmp_path: Path, task: str) -> dict:
    return json.loads((tmp_path / f"{task}.json").read_text(encoding="utf-8"))


class TestOkPath:
    def test_clean_run_is_ok(self, tmp_path):
        with run_receipt("MT5-Test", tmp_path) as r:
            r.mt5_init = True

        written = _read(tmp_path, "MT5-Test")
        assert written["ok"] is True and written["exit_code"] == 0
        assert written["mt5_init"] is True and written["errors"] == []
        assert isinstance(written["duration_s"], float) and written["duration_s"] >= 0.0
        started = datetime.fromisoformat(written["started_utc"])
        finished = datetime.fromisoformat(written["finished_utc"])
        assert started.tzinfo is not None and finished.tzinfo is not None, "timezone-aware UTC"
        assert written["started_utc"].endswith("+00:00") and finished >= started

    def test_mt5_init_defaults_to_null(self, tmp_path):
        with run_receipt("MT5-NoBroker", tmp_path):
            pass

        assert _read(tmp_path, "MT5-NoBroker")["mt5_init"] is None

    def test_started_receipt_is_written_on_entry(self, tmp_path):
        with run_receipt("MT5-Test", tmp_path):
            during = _read(tmp_path, "MT5-Test")

        assert during["finished_utc"] is None and during["exit_code"] is None
        assert during["ok"] is False and during["duration_s"] is None
        assert during["started_utc"] is not None


class TestExceptionPath:
    def test_exception_is_recorded_and_reraised(self, tmp_path):
        with pytest.raises(RuntimeError, match="boom"):
            with run_receipt("MT5-Test", tmp_path):
                raise RuntimeError("boom")

        written = _read(tmp_path, "MT5-Test")
        assert written["ok"] is False and written["exit_code"] == 1
        assert written["errors"] == [{"type": "RuntimeError", "where": "unhandled"}]
        assert written["finished_utc"] is not None

    def test_system_exit_1_records_exit_code_1(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            with run_receipt("MT5-Test", tmp_path):
                sys.exit(1)

        assert exc.value.code == 1
        written = _read(tmp_path, "MT5-Test")
        assert written["exit_code"] == 1 and written["ok"] is False and written["errors"] == []

    def test_system_exit_zero_and_none_are_clean(self, tmp_path):
        for code in (0, None):
            with pytest.raises(SystemExit):
                with run_receipt("MT5-Test", tmp_path):
                    sys.exit(code)
            written = _read(tmp_path, "MT5-Test")
            assert written["exit_code"] == 0 and written["ok"] is True

    def test_recorded_handled_error_turns_ok_false(self, tmp_path):
        with run_receipt("MT5-Test", tmp_path) as r:
            r.record_error(ValueError("bad symbol"), "run_symbol:GOLD")

        written = _read(tmp_path, "MT5-Test")
        assert written["ok"] is False and written["exit_code"] == 0
        assert written["errors"] == [{"type": "ValueError", "where": "run_symbol:GOLD"}]


class TestSchemaAndAtomicity:
    def test_schema_keys_are_exact_and_ordered(self, tmp_path):
        with run_receipt("MT5-Test", tmp_path) as r:
            r.mt5_init = False
            r.record_error(KeyError("x"), "somewhere")

        written = _read(tmp_path, "MT5-Test")
        assert list(written) == SCHEMA_KEYS
        assert isinstance(written["task"], str)
        assert isinstance(written["ok"], bool) and isinstance(written["exit_code"], int)
        assert isinstance(written["mt5_init"], bool) and isinstance(written["duration_s"], float)
        assert all(set(e) == {"type", "where"} for e in written["errors"])

    def test_atomic_write_leaves_no_temp_file(self, tmp_path):
        with run_receipt("MT5-Test", tmp_path):
            pass

        assert [p.name for p in tmp_path.iterdir()] == ["MT5-Test.json"]

    def test_default_location_is_data_cache_task_runs(self):
        import task_receipt
        from paths import DATA_CACHE

        assert TaskReceipt("MT5-X").path == DATA_CACHE / "task_runs" / "MT5-X.json"
        assert task_receipt.TASK_RUNS_DIR == DATA_CACHE / "task_runs"

    def test_io_failure_never_raises_into_the_task(self, tmp_path, capsys):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")  # mkdir(parents=True) on a file fails

        with run_receipt("MT5-Test", blocker) as r:
            r.mt5_init = True  # the task body still runs

        assert "task_receipt: could not write" in capsys.readouterr().err


class TestWiring:
    """The scripts wrap main() at __main__ time; what is testable here is that each main()
    still runs with and without a receipt, and reports what the receipt needs."""

    def test_remote_control_exits_1_when_the_fetch_fails(self, monkeypatch, tmp_path):
        import remote_control as rc

        monkeypatch.setattr(rc, "BLACKLIST_FILE", tmp_path / "blacklist.json")

        def fail(url=rc.CONTROL_URL):
            raise urllib.error.URLError("no route")
        monkeypatch.setattr(rc, "fetch_control", fail)

        with pytest.raises(SystemExit) as exc:
            with run_receipt("MT5-RemoteControl", tmp_path):
                rc.main()

        assert exc.value.code == 1
        assert not (tmp_path / "blacklist.json").exists(), "blacklist left untouched on failure"
        assert _read(tmp_path, "MT5-RemoteControl")["exit_code"] == 1

    def test_killswitch_main_reports_mt5_init_on_the_receipt(self, monkeypatch, tmp_path):
        import killswitch_monitor as ks

        events: list[dict] = []
        monkeypatch.setattr(ks, "append", lambda event: events.append(event))
        monkeypatch.setattr(ks.mt5, "initialize", lambda: False)
        monkeypatch.setattr(ks.mt5, "last_error", lambda: (-10005, "IPC timeout"))

        with pytest.raises(SystemExit) as exc:
            with run_receipt("MT5-GoldDrift-KillSwitch", tmp_path) as receipt:
                ks.main(receipt)

        assert exc.value.code == 1  # unchanged: the brake stays loud when inert
        assert [e["event"] for e in events] == ["killswitch_inert"]
        written = _read(tmp_path, "MT5-GoldDrift-KillSwitch")
        assert written["mt5_init"] is False and written["exit_code"] == 1

    def test_position_monitor_reports_mt5_init_on_the_receipt(self, monkeypatch, tmp_path):
        import MetaTrader5 as mt5
        import position_monitor as pm

        monkeypatch.setattr(mt5, "initialize", lambda: False)
        monkeypatch.setattr(mt5, "last_error", lambda: (-10005, "IPC timeout"))

        with run_receipt("MT5-PositionMonitor", tmp_path) as receipt:
            pm.main(receipt)

        assert _read(tmp_path, "MT5-PositionMonitor")["mt5_init"] is False

    @pytest.mark.parametrize("module_name", ["vps_maintenance", "killswitch_monitor", "remote_control"])
    def test_missing_helper_never_stops_the_task(self, monkeypatch, capsys, module_name):
        # If task_receipt.py is not delivered (manifest entry missed, or a task fires
        # mid-update) the script must still run -- the kill switch must not go inert and the
        # remote pause lever must not vanish over bookkeeping. It says so on stderr instead.
        import importlib
        module = importlib.import_module(module_name)
        original = module.run_receipt
        try:
            monkeypatch.setitem(sys.modules, "task_receipt", None)  # makes the import fail
            importlib.reload(module)
            assert module.run_receipt is not original
            assert "task_receipt.py missing" in capsys.readouterr().err
            with module.run_receipt("MT5-Whatever") as receipt:
                assert receipt is None  # every main(receipt=None) tolerates this
        finally:
            monkeypatch.undo()
            importlib.reload(module)
        assert module.run_receipt is not None and module.run_receipt.__module__ == "task_receipt"

    def test_every_task_script_wraps_main_in_a_receipt(self):
        expected = {
            "hotfix/scripts/intraday_mean_rev.py": "MT5-IntradayMR",
            "hotfix/scripts/position_monitor.py": "MT5-PositionMonitor",
            "remote_control.py": "MT5-RemoteControl",
            "hotfix/scripts/vps_maintenance.py": "MT5-Maintenance",
            "killswitch_monitor.py": "MT5-GoldDrift-KillSwitch",
        }
        for rel, task in expected.items():
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert f'run_receipt("{task}"' in source, f"{rel} does not wrap main() in {task}"
