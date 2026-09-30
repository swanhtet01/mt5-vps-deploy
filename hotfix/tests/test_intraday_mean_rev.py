from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


HOTFIX_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOTFIX_ROOT / "src"))
sys.path.insert(0, str(HOTFIX_ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "intraday_mean_rev_under_test",
    HOTFIX_ROOT / "scripts" / "intraday_mean_rev.py",
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_live_entry_requires_global_and_strategy_authorization(monkeypatch):
    enabled = {runner.LIVE_ENV_FLAG}
    monkeypatch.setattr(
        runner,
        "persistent_user_flag_enabled",
        lambda name: name in enabled,
    )
    assert runner._is_live() is False

    enabled.add(runner.MR_LIVE_ENV_FLAG)
    assert runner._is_live() is True


def test_broker_minimum_above_cap_skips_instead_of_upsizing(monkeypatch):
    events: list[dict] = []
    info = SimpleNamespace(
        volume_min=0.10,
        volume_max=10.0,
        volume_step=0.10,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        digits=2,
    )
    tick = SimpleNamespace(bid=100.0, ask=100.1)
    monkeypatch.setattr(runner.mt5, "symbol_info", lambda _symbol: info)
    monkeypatch.setattr(runner.mt5, "symbol_info_tick", lambda _symbol: tick)
    monkeypatch.setattr(runner.mt5, "account_info", lambda: SimpleNamespace(equity=1000.0))
    monkeypatch.setattr(runner.mt5, "order_send", lambda _request: 1 / 0)
    monkeypatch.setattr(runner, "_open_positions", lambda _magic: [])
    monkeypatch.setattr(runner, "_in_session", lambda _now: True)
    monkeypatch.setattr(runner, "_news_blackout", lambda _symbol: "")
    monkeypatch.setattr(runner, "_today_pnl", lambda _magic, _symbol: 0.0)
    monkeypatch.setattr(runner, "_compute_rsi_atr", lambda _symbol: (20.0, 1.0, 100.0))
    monkeypatch.setattr(runner, "_is_live", lambda: True)
    monkeypatch.setattr(runner, "_log", lambda _name, event: events.append(event))
    strategy = {
        "symbol": "TEST",
        "magic": 88099,
        "max_lot": 0.01,
        "sl_usd": 5.0,
        "tp_usd": 5.0,
        "daily_loss_limit": -5.0,
        "min_atr_pct": 0.0,
        "max_atr_pct": 10.0,
        "rsi_buy": 35.0,
        "rsi_sell": 65.0,
    }

    runner.run_symbol(strategy, datetime.now(tz=timezone.utc))

    assert events[-1]["event"] == "entry_skip"
    assert events[-1]["broker_volume_min"] == 0.10
    assert events[-1]["configured_max_lot"] == 0.01


def test_stop_above_two_percent_equity_budget_skips(monkeypatch):
    events: list[dict] = []
    info = SimpleNamespace(
        volume_min=0.01,
        volume_max=10.0,
        volume_step=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        digits=2,
    )
    tick = SimpleNamespace(bid=100.0, ask=100.1)
    monkeypatch.setattr(runner.mt5, "symbol_info", lambda _symbol: info)
    monkeypatch.setattr(runner.mt5, "symbol_info_tick", lambda _symbol: tick)
    monkeypatch.setattr(runner.mt5, "account_info", lambda: SimpleNamespace(equity=100.0))
    monkeypatch.setattr(runner.mt5, "order_send", lambda _request: 1 / 0)
    monkeypatch.setattr(runner, "_open_positions", lambda _magic: [])
    monkeypatch.setattr(runner, "_in_session", lambda _now: True)
    monkeypatch.setattr(runner, "_news_blackout", lambda _symbol: "")
    monkeypatch.setattr(runner, "_today_pnl", lambda _magic, _symbol: 0.0)
    monkeypatch.setattr(runner, "_compute_rsi_atr", lambda _symbol: (20.0, 1.0, 100.0))
    monkeypatch.setattr(runner, "_is_live", lambda: True)
    monkeypatch.setattr(runner, "_log", lambda _name, event: events.append(event))
    strategy = {
        "symbol": "TEST",
        "magic": 88099,
        "max_lot": 0.01,
        "sl_usd": 5.0,
        "tp_usd": 5.0,
        "daily_loss_limit": -5.0,
        "min_atr_pct": 0.0,
        "max_atr_pct": 10.0,
        "rsi_buy": 35.0,
        "rsi_sell": 65.0,
    }

    runner.run_symbol(strategy, datetime.now(tz=timezone.utc))

    assert events[-1]["event"] == "entry_skip"
    assert "2% equity budget" in events[-1]["reason"]
    assert events[-1]["account_equity"] == 100.0
