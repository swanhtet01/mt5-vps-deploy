"""The intraday mean-reversion fade must not be armed by the shared live flag alone.

MT5_GOLD_DRIFT_LIVE arms every validated edge on the account. This strategy has no backtest
artefact — a 0.667 reward-to-risk RSI fade with take-profit near 5x H1 ATR inside a 2-hour
hold — so riding that flag alone meant it went live the moment anything else did, and could
only be stopped by disarming everything. structural_scheduler.py already solves this with a
second flag of its own; these tests pin the same shape here.
"""

from __future__ import annotations

import pytest

import intraday_mean_rev as mr


@pytest.fixture()
def flags(monkeypatch):
    """Control both persistent flags and the force-paper override."""
    enabled: set[str] = set()
    monkeypatch.setattr(mr, "persistent_user_flag_enabled", lambda name: name in enabled)
    monkeypatch.delenv(mr.FORCE_PAPER_ONLY_ENV_FLAG, raising=False)
    return enabled


def test_shared_flag_alone_does_not_arm_it(flags):
    # The regression that matters: arming the validated structural edges must not
    # silently put this unvalidated strategy into the market alongside them.
    flags.add(mr.LIVE_ENV_FLAG)

    assert mr._is_live() is False


def test_own_flag_alone_does_not_arm_it(flags):
    # The shared flag remains the master switch, so this one cannot bypass it.
    flags.add(mr.INTRADAY_LIVE_ENV_FLAG)

    assert mr._is_live() is False


def test_both_flags_arm_it(flags):
    flags.update({mr.LIVE_ENV_FLAG, mr.INTRADAY_LIVE_ENV_FLAG})

    assert mr._is_live() is True


def test_neither_flag_is_paper(flags):
    assert mr._is_live() is False


def test_force_paper_only_overrides_both_flags(flags, monkeypatch):
    flags.update({mr.LIVE_ENV_FLAG, mr.INTRADAY_LIVE_ENV_FLAG})
    monkeypatch.setenv(mr.FORCE_PAPER_ONLY_ENV_FLAG, "1")

    assert mr._is_live() is False


def test_only_an_exact_1_forces_paper(flags, monkeypatch):
    # A stray value must not silently disable the override and leave it live-armed
    # under the operator's belief that it is paper-only... nor block a real arm.
    flags.update({mr.LIVE_ENV_FLAG, mr.INTRADAY_LIVE_ENV_FLAG})
    monkeypatch.setenv(mr.FORCE_PAPER_ONLY_ENV_FLAG, "0")

    assert mr._is_live() is True


def test_force_paper_tolerates_surrounding_whitespace(flags, monkeypatch):
    flags.update({mr.LIVE_ENV_FLAG, mr.INTRADAY_LIVE_ENV_FLAG})
    monkeypatch.setenv(mr.FORCE_PAPER_ONLY_ENV_FLAG, " 1 ")

    assert mr._is_live() is False


def test_it_uses_a_different_flag_from_the_structural_scheduler():
    # Two unvalidated-strategy gates sharing one flag would defeat the purpose.
    assert mr.INTRADAY_LIVE_ENV_FLAG != mr.LIVE_ENV_FLAG
    assert mr.INTRADAY_LIVE_ENV_FLAG == "MT5_INTRADAY_MR_LIVE"
