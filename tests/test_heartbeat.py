"""Tests for the dead-man's heartbeat -- the one signal that survives the box dying.

heartbeat.py used to ping the plain healthchecks.io URL and exit 0 even when
MetaTrader5.initialize() failed, so the off-box channel could only ever say "alive", never
"unhealthy". It now carries a verdict: two consecutive unhealthy runs ping /fail. Every
case here drives main() end to end against a fake broker and a captured HTTP layer, and
reads back the state file the way vps_health.py will.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# On the VPS heartbeat.py and paths.py both land in scripts\; in this repo the resolver
# lives under hotfix/scripts, so put it on the path the way the box would see it.
sys.path.insert(0, str(REPO_ROOT / "hotfix" / "scripts"))

import MetaTrader5 as mt5_stub  # noqa: E402  (conftest's stub; every call must be patched)
import heartbeat as hb  # noqa: E402

BASE_URL = "https://hc-ping.com/abc-123"


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """main() with the broker, the live flag, the HTTP layer and the state file all captured."""
    pings: list[dict] = []
    state = {"live": False, "ping": 200}
    hb_file = tmp_path / "heartbeat.json"
    monkeypatch.setattr(hb, "HEARTBEAT_FILE", hb_file)
    monkeypatch.setenv("HEALTHCHECK_URL", BASE_URL)
    monkeypatch.setattr(hb, "live_flag_armed", lambda: state["live"])

    class _Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        pings.append({
            "url": req.full_url, "method": req.get_method(), "body": req.data.decode("utf-8"),
        })
        if isinstance(state["ping"], Exception):
            raise state["ping"]
        return _Response(state["ping"])

    monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

    def configure(*, init=True, account=True, connected=True, trade_allowed=True):
        if isinstance(init, Exception):
            def _boom():
                raise init
            monkeypatch.setattr(mt5_stub, "initialize", _boom)
        else:
            monkeypatch.setattr(mt5_stub, "initialize", lambda: init)
        monkeypatch.setattr(mt5_stub, "shutdown", lambda: None)
        ai = SimpleNamespace(login=12345678, equity=600.0, balance=600.0) if account else None
        monkeypatch.setattr(mt5_stub, "account_info", lambda: ai)
        ti = SimpleNamespace(connected=connected, trade_allowed=trade_allowed)
        monkeypatch.setattr(mt5_stub, "terminal_info", lambda: ti, raising=False)

    def run():
        rc = hb.main()
        return rc, json.loads(hb_file.read_text(encoding="utf-8"))

    return SimpleNamespace(configure=configure, run=run, pings=pings, state=state, file=hb_file)


class TestPingTarget:
    def test_healthy_run_pings_the_base_url(self, harness):
        harness.configure()

        rc, out = harness.run()

        assert rc == 0
        assert [p["url"] for p in harness.pings] == [BASE_URL]
        assert harness.pings[0]["method"] == "POST"
        assert out["healthy"] is True
        assert out["mt5_status"] == "ok"
        assert out["unhealthy_streak"] == 0
        assert out["ping_target"] == "base"
        assert out["ping_ok"] is True
        assert out["url_set"] is True
        assert out["last_ping_utc"]

    def test_first_unhealthy_run_still_pings_the_base_url(self, harness):
        # One failed initialize() is what the broker's daily rollover looks like. Paging
        # on it would wake the operator at 3am for nothing.
        harness.configure(init=False)

        rc, out = harness.run()

        assert rc == 0
        assert harness.pings[0]["url"] == BASE_URL
        assert out["healthy"] is False
        assert out["mt5_status"] == "init_failed"
        assert out["unhealthy_streak"] == 1
        assert out["ping_target"] == "base"

    def test_second_consecutive_unhealthy_run_pings_fail(self, harness):
        harness.configure(init=False)

        harness.run()
        rc, out = harness.run()

        assert rc == 0, "the heartbeat itself must never look like a crashed task"
        assert harness.pings[1]["url"] == BASE_URL + "/fail"
        assert out["unhealthy_streak"] == 2
        assert out["ping_target"] == "fail"
        body = harness.pings[1]["body"]
        assert "mt5=init_failed" in body
        assert "streak=2" in body
        assert "connected=" in body and "trade_allowed=" in body

    def test_fail_url_never_doubles_the_slash(self, harness, monkeypatch):
        monkeypatch.setenv("HEALTHCHECK_URL", BASE_URL + "/")
        harness.configure(init=False)

        harness.run()
        harness.run()

        assert harness.pings[1]["url"] == BASE_URL + "/fail"

    def test_recovery_resets_the_streak(self, harness):
        harness.configure(init=False)
        harness.run()
        harness.run()
        harness.configure()

        rc, out = harness.run()

        assert harness.pings[2]["url"] == BASE_URL
        assert out["unhealthy_streak"] == 0
        assert out["healthy"] is True

    def test_streak_is_carried_by_the_state_file_alone(self, harness):
        # A hand-edited or corrupt file must not throw the streak off or crash the run.
        harness.file.write_text('{"unhealthy_streak": "garbage"}', encoding="utf-8")
        harness.configure(init=False)

        rc, out = harness.run()

        assert rc == 0
        assert out["unhealthy_streak"] == 1


class TestVerdict:
    def test_disconnected_terminal_is_unhealthy(self, harness):
        # account_info answers from cache while the broker link is down; the terminal's
        # own connected flag is what says an order could actually leave the box.
        harness.configure(connected=False)

        _, out = harness.run()

        assert out["healthy"] is False
        assert out["connected"] is False
        assert "disconnected" in out["mt5_status"]

    def test_missing_account_is_unhealthy(self, harness):
        harness.configure(account=False)

        _, out = harness.run()

        assert out["healthy"] is False
        assert "no_account" in out["mt5_status"]
        assert out["mt5_login"] is None

    def test_autotrading_off_is_fine_in_paper_mode(self, harness):
        harness.state["live"] = False
        harness.configure(trade_allowed=False)

        _, out = harness.run()

        assert out["healthy"] is True
        assert out["trade_allowed"] is False

    def test_autotrading_off_is_unhealthy_when_live_is_armed(self, harness):
        # Armed but unable is the state in which every live edge silently does nothing.
        harness.state["live"] = True
        harness.configure(trade_allowed=False)

        _, out = harness.run()

        assert out["healthy"] is False
        assert "autotrading_off" in out["mt5_status"]
        assert out["live_armed"] is True

    def test_autotrading_on_while_armed_is_healthy(self, harness):
        harness.state["live"] = True
        harness.configure(trade_allowed=True)

        _, out = harness.run()

        assert out["healthy"] is True

    def test_unreadable_live_flag_fails_closed(self, harness, monkeypatch):
        # None means the mt5_agent package did not import -- then no edge can trade either.
        monkeypatch.setattr(hb, "live_flag_armed", lambda: None)
        harness.configure()

        _, out = harness.run()

        assert out["healthy"] is False
        assert "live_flag_unreadable" in out["mt5_status"]

    def test_broker_exception_is_a_verdict_not_a_crash(self, harness):
        harness.configure(init=RuntimeError("IPC timeout"))

        rc, out = harness.run()

        assert rc == 0
        assert out["healthy"] is False
        assert out["mt5_status"].startswith("error: RuntimeError")
        assert harness.pings[0]["url"] == BASE_URL


class TestWithoutUrl:
    def test_no_url_writes_the_file_and_touches_no_network(self, harness, monkeypatch):
        monkeypatch.delenv("HEALTHCHECK_URL")
        harness.configure()

        rc, out = harness.run()

        assert rc == 0
        assert harness.pings == []
        assert out["url_set"] is False
        assert out["healthcheck_url_set"] is False
        assert out["ping_ok"] is None
        assert out["last_ping_utc"] is None
        assert out["mt5_status"] == "ok", "the local verdict is still recorded"

    def test_blank_url_counts_as_unset(self, harness, monkeypatch):
        monkeypatch.setenv("HEALTHCHECK_URL", "   ")
        harness.configure()

        _, out = harness.run()

        assert harness.pings == []
        assert out["url_set"] is False


class TestPingOutcome:
    def test_failed_ping_is_counted_and_still_exits_zero(self, harness):
        harness.configure()
        harness.state["ping"] = OSError("connection refused")

        rc, out = harness.run()
        assert rc == 0
        assert out["ping_ok"] is False
        assert "connection refused" in out["ping_error"]
        assert out["ping_fail_streak"] == 1

        rc, out = harness.run()
        assert out["ping_fail_streak"] == 2

        harness.state["ping"] = 200
        _, out = harness.run()
        assert out["ping_ok"] is True
        assert out["ping_fail_streak"] == 0

    def test_non_200_is_a_failed_ping(self, harness):
        harness.configure()
        harness.state["ping"] = 500

        _, out = harness.run()

        assert out["ping_ok"] is False
        assert out["ping_status"] == 500
        assert out["ping_fail_streak"] == 1

    def test_state_file_is_written_before_the_ping(self, harness, monkeypatch):
        # If healthchecks hangs or the process dies mid-ping, the dashboard still sees a
        # fresh local file with the verdict in it.
        harness.configure()
        seen = {}

        def urlopen_reads_file(req, timeout=None):
            seen["on_disk"] = json.loads(harness.file.read_text(encoding="utf-8"))
            raise OSError("boom")

        monkeypatch.setattr(hb.urllib.request, "urlopen", urlopen_reads_file)

        harness.run()

        assert seen["on_disk"]["mt5_status"] == "ok"
        assert seen["on_disk"]["ping_ok"] is None
