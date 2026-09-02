"""notify.py's CLI must fail the task when nothing was delivered.

vps_health._push_health runs `python notify.py <msg>` and now reads the exit code as its
delivery receipt, and Task Scheduler's LastTaskResult is that same exit code. Exit 0 with
zero channels delivered was how every alert could quietly reach nobody while the whole
system reported green. The library senders are unchanged and are pinned here as such.
"""

from __future__ import annotations

import sys

import pytest

import notify


@pytest.fixture()
def channels(monkeypatch):
    """Stub all three senders; the dict decides which of them 'delivers'."""
    sent = {"ntfy": False, "webhook": False, "telegram": False}
    monkeypatch.setattr(notify, "send_ntfy", lambda *a, **k: sent["ntfy"])
    monkeypatch.setattr(notify, "send_webhook", lambda *a, **k: sent["webhook"])
    monkeypatch.setattr(notify, "send_telegram", lambda *a, **k: sent["telegram"])
    return sent


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _refuse(*a, **k):
        raise AssertionError("test reached the network -- stub the sender instead")
    monkeypatch.setattr(notify.urllib.request, "urlopen", _refuse)


def test_no_channel_delivered_exits_one(channels, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["notify.py", "hello"])

    assert notify.main() == 1

    captured = capsys.readouterr()
    assert "no channel delivered" in captured.err
    assert "ntfy=skip  webhook=skip  telegram=skip" in captured.out


@pytest.mark.parametrize("channel", ["ntfy", "webhook", "telegram"])
def test_any_single_channel_delivered_exits_zero(channels, monkeypatch, channel):
    channels[channel] = True
    monkeypatch.setattr(sys, "argv", ["notify.py", "hello"])

    assert notify.main() == 0


def test_all_channels_are_still_attempted_even_after_one_delivers(monkeypatch):
    # The exit code is derived from the results; it must not short-circuit the fan-out.
    calls = []
    monkeypatch.setattr(notify, "send_ntfy", lambda *a, **k: calls.append("ntfy") or True)
    monkeypatch.setattr(notify, "send_webhook", lambda *a, **k: calls.append("webhook") or False)
    monkeypatch.setattr(notify, "send_telegram", lambda *a, **k: calls.append("telegram") or False)
    monkeypatch.setattr(sys, "argv", ["notify.py", "hello", "world"])

    assert notify.main() == 0
    assert calls == ["ntfy", "webhook", "telegram"]


def test_usage_error_exits_one(channels, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["notify.py"])

    assert notify.main() == 1
    assert "usage:" in capsys.readouterr().out


def test_library_senders_return_false_when_unconfigured(monkeypatch):
    # Library behaviour is untouched: an unconfigured channel says False and sends nothing.
    for name in ("NTFY_TOPIC", "WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)

    assert notify.send_ntfy("x") is False
    assert notify.send_webhook({"content": "x"}) is False
    assert notify.send_telegram("x") is False
