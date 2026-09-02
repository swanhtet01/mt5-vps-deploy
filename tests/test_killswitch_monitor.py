"""Tests for the account-level kill switch — the only backstop on the live account.

killswitch_monitor.py is what actually bounds the blast radius: it sums realized losses
across every live magic and disarms MT5_GOLD_DRIFT_LIVE, which arms all live edges. Until
now it had no tests at all, on either lineage, despite being the last line of defence for
a real balance.

Each threshold is checked at its exact boundary, because every comparison in main() is
inclusive (`<=` / `>=`) and an off-by-one there is the difference between the brake
engaging and not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import killswitch_monitor as ks


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """Run main() against a fake broker, with every side effect captured.

    The kill switch's real effects are a PowerShell env-var write, a phone alert and a
    JSONL append; all three are redirected so a test can assert on them without touching
    the machine.
    """
    events: list[dict] = []
    shell_calls: list[list[str]] = []
    state = {"flag_armed": True}

    monkeypatch.setattr(ks, "LOG", tmp_path / "killswitch.jsonl")
    monkeypatch.setattr(ks, "append", lambda event: events.append(event))
    monkeypatch.setattr(ks, "_live_flag_is_set", lambda: state["flag_armed"])
    monkeypatch.setattr(ks.subprocess, "run", lambda *a, **k: shell_calls.append(a[0] if a else []))

    window = SimpleNamespace(
        clock=SimpleNamespace(feed_time=NOW),
        end=NOW,
        as_dict=lambda: {"feed_window": "stub"},
    )

    def configure(*, equity=600.0, losses=None, history_error=None):
        summary = {
            "n_closed_trades": 0, "n_account_closed_trades": 0,
            "realized_30d_usd": 0.0, "realized_7d_usd": 0.0,
            "account_realized_30d_usd": 0.0, "account_realized_7d_usd": 0.0,
            "current_losing_streak": 0,
        }
        summary.update(losses or {})

        monkeypatch.setattr(ks.mt5, "initialize", lambda: True)
        monkeypatch.setattr(ks.mt5, "shutdown", lambda: None)
        monkeypatch.setattr(ks.mt5, "last_error", lambda: (1, "stub"))
        monkeypatch.setattr(
            ks.mt5, "account_info", lambda: SimpleNamespace(equity=equity, balance=equity)
        )
        if history_error is None:
            monkeypatch.setattr(ks, "recent_history", lambda now: ("GOLD", window, []))
        else:
            def _boom(now):
                raise RuntimeError(history_error)
            monkeypatch.setattr(ks, "recent_history", _boom)
        monkeypatch.setattr(ks, "summarize_losses", lambda deals, w: summary)

    return SimpleNamespace(
        configure=configure, events=events, shell_calls=shell_calls, state=state,
        fired=lambda: [e for e in events if e.get("event") == "KILL_SWITCH_FIRED"],
    )


class TestEachThresholdDisarms:
    def test_all_clear_does_not_disarm(self, harness):
        harness.configure(equity=600.0)

        ks.main()

        assert harness.fired() == []
        assert harness.shell_calls == []

    def test_equity_floor_at_the_boundary_disarms(self, harness):
        harness.configure(equity=ks.THRESH_EQUITY_FLOOR)

        ks.main()

        assert len(harness.fired()) == 1
        assert "equity" in harness.fired()[0]["reason"]

    def test_a_cent_above_the_equity_floor_does_not_disarm(self, harness):
        harness.configure(equity=ks.THRESH_EQUITY_FLOOR + 0.01)

        ks.main()

        assert harness.fired() == []

    def test_agent_30_day_loss_disarms(self, harness):
        harness.configure(losses={"realized_30d_usd": ks.THRESH_30D_LOSS})

        ks.main()

        assert "30-day loss" in harness.fired()[0]["reason"]

    def test_agent_7_day_loss_disarms(self, harness):
        harness.configure(losses={"realized_7d_usd": ks.THRESH_7D_LOSS})

        ks.main()

        assert "7-day loss" in harness.fired()[0]["reason"]

    def test_account_wide_loss_disarms_even_when_the_agent_is_flat(self, harness):
        # The flag arms every live edge, so a drawdown from anything on the account is
        # still a reason to stop — this is the guard the older lineage lacks entirely.
        harness.configure(losses={"account_realized_7d_usd": ks.THRESH_7D_LOSS})

        ks.main()

        assert "account 7-day loss" in harness.fired()[0]["reason"]

    def test_losing_streak_at_the_threshold_disarms(self, harness):
        harness.configure(losses={"current_losing_streak": ks.THRESH_LOSING_STREAK})

        ks.main()

        assert "losing streak" in harness.fired()[0]["reason"]

    def test_one_short_of_the_streak_does_not_disarm(self, harness):
        harness.configure(losses={"current_losing_streak": ks.THRESH_LOSING_STREAK - 1})

        ks.main()

        assert harness.fired() == []

    def test_every_breached_guard_is_named_in_one_disarm(self, harness):
        harness.configure(
            equity=100.0,
            losses={
                "realized_30d_usd": -999.0,
                "realized_7d_usd": -999.0,
                "current_losing_streak": 99,
            },
        )

        ks.main()

        fired = harness.fired()
        assert len(fired) == 1, "one disarm, not one per breached guard"
        for expected in ("30-day loss", "7-day loss", "losing streak", "equity"):
            assert expected in fired[0]["reason"]


class TestFailsClosed:
    def test_history_failure_while_armed_disarms(self, harness):
        # Losing the feed clock means the guards cannot be evaluated at all. Continuing
        # to trade blind is the one outcome a backstop must never allow.
        harness.configure(history_error="history_deals_get failed")

        ks.main()

        assert len(harness.fired()) == 1
        assert harness.fired()[0]["reason"] == "history feed clock unavailable"

    def test_history_failure_while_already_disarmed_is_only_logged(self, harness):
        harness.state["flag_armed"] = False
        harness.configure(history_error="history_deals_get failed")

        ks.main()

        assert harness.fired() == []
        assert [e["event"] for e in harness.events] == ["killswitch_history_unavailable"]


class TestDisarmIsIdempotent:
    def test_disarming_when_already_disarmed_does_not_alert_again(self, harness):
        harness.state["flag_armed"] = False

        ks.disarm("equity floor", {"equity": 100.0})

        assert harness.shell_calls == [], "must not rewrite the env var it already cleared"
        assert [e["event"] for e in harness.events] == ["killswitch_breach_already_disarmed"]

    def test_disarming_while_armed_clears_the_flag(self, harness):
        harness.state["flag_armed"] = True

        ks.disarm("equity floor", {"equity": 100.0})

        assert len(harness.shell_calls) == 1
        command = " ".join(harness.shell_calls[0])
        assert ks.LIVE_ENV_FLAG in command
        assert "SetEnvironmentVariable" in command
        assert [e["event"] for e in harness.events] == ["KILL_SWITCH_FIRED"]


class TestLossSummary:
    """summarize_losses decides what the thresholds see, so its windowing, its magic
    filter and its streak definition are part of the backstop."""

    def _window(self):
        return SimpleNamespace(clock=SimpleNamespace(feed_time=NOW), end=NOW)

    def _trades(self, monkeypatch, trades):
        monkeypatch.setattr(ks, "closed_trades_from_deals", lambda deals: trades)
        # summarize_losses filters raw deals before conversion; one valid-looking deal is
        # enough to get past that filter since the conversion itself is stubbed.
        return [SimpleNamespace(position_id=1, symbol="GOLD")]

    def _trade(self, days_ago, net, magic):
        return SimpleNamespace(close_time=NOW - timedelta(days=days_ago), net=net, magic=magic)

    def test_only_live_magics_count_toward_the_agent_figures(self, monkeypatch):
        trades = [self._trade(1, -50.0, 88001), self._trade(1, -500.0, 12345)]
        deals = self._trades(monkeypatch, trades)

        summary = ks.summarize_losses(deals, self._window())

        assert summary["realized_7d_usd"] == -50.0, "a foreign magic must not count as ours"
        assert summary["account_realized_7d_usd"] == -550.0, "but it does count account-wide"

    def test_losses_outside_the_window_are_excluded(self, monkeypatch):
        trades = [self._trade(3, -20.0, 88001), self._trade(20, -300.0, 88001)]
        deals = self._trades(monkeypatch, trades)

        summary = ks.summarize_losses(deals, self._window())

        assert summary["realized_7d_usd"] == -20.0
        assert summary["realized_30d_usd"] == -320.0

    def test_streak_counts_only_trailing_consecutive_losses(self, monkeypatch):
        trades = [
            self._trade(5, -10.0, 88001),
            self._trade(4, 5.0, 88001),   # a win resets it
            self._trade(3, -10.0, 88001),
            self._trade(2, -10.0, 88001),
        ]
        deals = self._trades(monkeypatch, trades)

        summary = ks.summarize_losses(deals, self._window())

        assert summary["current_losing_streak"] == 2

    def test_intraday_mean_rev_magics_are_covered(self, monkeypatch):
        # 88011-88014 are armed by the same flag; if they were missing from LIVE_MAGICS
        # their drawdown would never reach the cumulative guards.
        for magic in (88011, 88012, 88013, 88014):
            assert magic in ks.LIVE_MAGICS
