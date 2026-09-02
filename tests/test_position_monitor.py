"""Tests for the DEPLOYED position monitor's anti-spam change detection.

Ported from the monorepo's tests/test_position_monitor.py so they exercise the copy this
repo actually installs (hotfix/scripts/position_monitor.py), plus pins for the pure
formatting helpers. main() is deliberately not exercised here: it is being wrapped by a
concurrent change, and everything it decides is in detect() anyway.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import position_monitor as pm
from mt5_agent.trade_history import ClosedTrade

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_module_under_test_is_the_deployed_copy():
    assert Path(pm.__file__).resolve() == REPO_ROOT / "hotfix" / "scripts" / "position_monitor.py"


def _pos(ticket, magic=88001, symbol="GOLD", typ=0, vol=0.01, price=4140.0, sl=4110.0, tp=4260.0):
    return {"ticket": ticket, "type": typ, "magic": magic, "volume": vol,
            "price_open": price, "sl": sl, "tp": tp, "symbol": symbol}


def _ct(pos_id, net, magic=88001, symbol="GOLD", held=timedelta(hours=1)):
    base = datetime(2026, 6, 24, 0, 0, tzinfo=timezone.utc)
    return ClosedTrade(position_id=pos_id, symbol=symbol, magic=magic,
                       open_time=base, close_time=base + held,
                       net=net, gross=net, volume=0.01, legs=2)


# --- ported from the monorepo ---------------------------------------------------------------


def test_first_run_is_silent():
    alerts, state = pm.detect({}, [_pos(1)], [_ct(99, 5.0)], equity=700.0)
    assert alerts == []                       # no backfill spam
    assert 99 in state["announced_closed"]    # but recorded so it won't alert later
    assert state["open_ids"] == [1]


def test_new_open_alerts():
    prev = {"open_ids": [], "announced_closed": []}
    alerts, _ = pm.detect(prev, [_pos(5, symbol="USDJPY", magic=88002, typ=0)], [], equity=700.0)
    assert len(alerts) == 1
    assert "opened" in alerts[0]["title"].lower() and "USDJPY" in alerts[0]["title"]
    assert "USDJPY Monday" in alerts[0]["body"]


def test_close_alerts_with_pnl():
    prev = {"open_ids": [7], "announced_closed": []}
    alerts, state = pm.detect(prev, [], [_ct(7, 8.5)], equity=691.0)
    assert len(alerts) == 1
    assert "+$8.50" in alerts[0]["title"]
    assert "won +$8.50" in alerts[0]["body"] and "$691" in alerts[0]["body"]
    assert 7 in state["announced_closed"]


def test_no_change_is_silent():
    prev = {"open_ids": [1], "announced_closed": [99]}
    alerts, _ = pm.detect(prev, [_pos(1)], [_ct(99, 5.0)], equity=700.0)
    assert alerts == []


def test_no_duplicate_close_alert():
    prev = {"open_ids": [], "announced_closed": [7]}
    alerts, _ = pm.detect(prev, [], [_ct(7, 8.5)], equity=691.0)
    assert alerts == []   # already announced


def test_loss_uses_x_tag():
    prev = {"open_ids": [7], "announced_closed": []}
    alerts, _ = pm.detect(prev, [], [_ct(7, -3.0)], equity=679.0)
    assert alerts[0]["tags"] == "x" and "lost -$3.00" in alerts[0]["body"]


def test_fast_open_and_close_between_polls():
    # trade never seen as open (opened+closed within one poll) -> still a close alert
    prev = {"open_ids": [], "announced_closed": []}
    alerts, _ = pm.detect(prev, [], [_ct(12, 4.0)], equity=704.0)
    assert len(alerts) == 1 and "+$4.00" in alerts[0]["title"]


# --- additional pins on the deployed copy -----------------------------------------------------


def test_first_run_is_any_empty_previous_state():
    # An empty dict, not just a missing file, means "seed silently".
    alerts, state = pm.detect({}, [], [_ct(1, 1.0), _ct(2, -1.0)], equity=100.0)
    assert alerts == [] and state["announced_closed"] == [1, 2]


def test_multiple_new_opens_alert_in_ticket_order():
    prev = {"open_ids": [3], "announced_closed": []}
    alerts, state = pm.detect(prev, [_pos(9), _pos(3), _pos(4, symbol="USDJPY", magic=88002)], [], equity=1.0)
    assert [a["title"] for a in alerts] == ["Trade opened: Bought USDJPY", "Trade opened: Bought GOLD"]
    assert state["open_ids"] == [3, 4, 9]


def test_close_of_a_position_still_open_is_not_announced():
    # A partial close produces a ClosedTrade while the position id is still open; the
    # monitor waits until the position is gone before announcing it.
    prev = {"open_ids": [7], "announced_closed": []}
    alerts, state = pm.detect(prev, [_pos(7)], [_ct(7, 2.0)], equity=100.0)
    assert alerts == []
    assert 7 not in state["announced_closed"]


def test_ticket_ids_are_coerced_to_int():
    prev = {"open_ids": [7], "announced_closed": []}
    alerts, state = pm.detect(prev, [_pos("7"), _pos(8.0)], [], equity=100.0)
    assert len(alerts) == 1 and state["open_ids"] == [7, 8]


def test_announced_closed_is_bounded_to_the_last_300():
    prev = {"open_ids": [], "announced_closed": list(range(1000))}
    _, state = pm.detect(prev, [], [_ct(5000, 1.0)], equity=100.0)
    assert len(state["announced_closed"]) == 300
    assert state["announced_closed"][-1] == 5000
    assert state["updated"] is None   # stamped by the caller, keeps detect pure


def test_open_alert_sell_side_without_stops():
    alert = pm.open_alert(_pos(1, typ=1, sl=0.0, tp=0.0, symbol="USDJPY", magic=88005))
    assert alert["title"] == "Trade opened: Sold USDJPY"
    assert alert["tags"] == "green_circle"
    assert "Stop" not in alert["body"] and "Target" not in alert["body"]
    assert "USDJPY Wednesday" in alert["body"]


def test_open_alert_with_stop_and_target():
    body = pm.open_alert(_pos(1))["body"]
    assert "Bought 0.01 GOLD @ 4140.0" in body
    assert "Stop @ 4110.0" in body and "Target @ 4260.0" in body


def test_open_alert_stop_only():
    body = pm.open_alert(_pos(1, tp=0.0))["body"]
    assert "Stop @ 4110.0" in body and "Target" not in body


def test_close_alert_reports_hold_time_and_equity():
    alert = pm.close_alert(_ct(7, 12.345, held=timedelta(minutes=45)), equity=1234.5)
    assert alert["title"] == "GOLD closed +$12.35"
    assert alert["tags"] == "white_check_mark"
    assert "held 45m" in alert["body"] and "Account now $1,234.50." in alert["body"]


def test_money_formatting():
    assert pm._money(1234.5) == "+$1,234.50"
    assert pm._money(0) == "+$0.00"
    assert pm._money(-3) == "-$3.00"


def test_duration_formatting():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert pm._dur(base, base + timedelta(minutes=59)) == "59m"
    assert pm._dur(base, base + timedelta(minutes=60)) == "1.0h"
    assert pm._dur(base, base + timedelta(hours=2, minutes=30)) == "2.5h"
    assert pm._dur(base, base - timedelta(minutes=5)) == "0m"   # clock skew never goes negative


def test_edge_names_fall_back_to_magic():
    assert pm._edge(88001) == "Gold Asian drift"
    assert pm._edge("88009") == "Gold Tuesday"
    assert pm._edge(12345) == "magic 12345"
