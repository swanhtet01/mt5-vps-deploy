"""Journaling schema + fail-loud tests for the intraday mean-reversion trader.

intraday_mean_rev.py is the only strategy on the box that can go live, and until now it
was unmeasurable: it wrote ``slippage_pts`` while the analyzer reads ``slippage_points``,
exits carried no slippage at all, and mt5.initialize() failing returned exit 0 for weeks.

The one test that matters most here is the request-dict pin: every order parameter the
broker sees (symbol, volume, type, price, sl, tp, deviation, magic, comment, filling) must
be exactly what the pre-journaling code sent. The literals below were produced by running
the PREVIOUS revision against these same stubs; if they ever need changing, the order path
changed, and that needs Swan's sign-off.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "hotfix" / "scripts"))

import intraday_mean_rev as imr  # noqa: E402
from task_receipt import run_receipt  # noqa: E402

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)  # Tuesday, London session
GOLD = imr.SIGNALS[0]
assert GOLD["symbol"] == "GOLD" and GOLD["magic"] == 88011

PINNED_ENTRY_REQUEST = {
    "action": 1, "symbol": "GOLD", "volume": 0.03, "type": 0, "price": 2400.0,
    "sl": 2385.0, "tp": 2410.0, "deviation": 20, "magic": 88011, "comment": "mr_gold",
    "type_time": 0, "type_filling": 1,
}
PINNED_CLOSE_REQUEST = {
    "action": 1, "symbol": "GOLD", "volume": 0.03, "type": 1, "position": 555,
    "price": 2399.7, "deviation": 20, "magic": 88011, "comment": "mr_exit:time_exit_3.0h",
    "type_time": 0, "type_filling": 1,
}


class Result:
    """Stand-in for MT5's OrderSendResult: attributes plus _asdict()."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def _asdict(self):
        return dict(self.__dict__)


def _done(req, *, price=None, volume=None, position_id=None):
    fields = dict(retcode=10009, deal=111, order=222,
                  volume=req["volume"] if volume is None else volume,
                  price=req["price"] if price is None else price,
                  bid=2399.70, ask=2400.00, comment="Request executed", request_id=1,
                  retcode_external=0)
    if position_id is not None:
        fields["position_id"] = position_id
    return Result(**fields)


@pytest.fixture()
def broker(monkeypatch, tmp_path):
    """A synthetic GOLD signal: RSI 0 (16 falling H1 bars) inside the ATR band, live-armed."""
    sent: list[dict] = []
    state = {"positions": [], "check": lambda req: SimpleNamespace(retcode=0, comment="Done"),
             "send": lambda req: _done(req, price=req["price"] + 0.05)}

    monkeypatch.setattr(imr, "LOG_DIR", tmp_path / "intraday-mr")
    monkeypatch.setattr(imr, "_NEWS_STATE_FILE", tmp_path / "news_state.json")
    monkeypatch.setattr(imr, "_is_live", lambda: True)
    # Constants the shared stub does not carry (conftest.py is not ours to edit).
    monkeypatch.setattr(imr.mt5, "TIMEFRAME_H1", 16385, raising=False)
    monkeypatch.setattr(imr.mt5, "TRADE_RETCODE_INVALID_FILL", 10030, raising=False)
    monkeypatch.setattr(imr.mt5, "last_error", lambda: (0, "stub"))
    monkeypatch.setattr(imr.mt5, "symbol_info", lambda s: SimpleNamespace(
        spread=30, point=0.01, digits=2, trade_tick_size=0.01, trade_tick_value=1.0,
        volume_min=0.01, volume_max=100.0, trade_exemode=2))
    monkeypatch.setattr(imr.mt5, "symbol_info_tick",
                        lambda s: SimpleNamespace(bid=2399.70, ask=2400.00, time=1756713600))
    monkeypatch.setattr(imr.mt5, "positions_get", lambda: list(state["positions"]))
    monkeypatch.setattr(imr.mt5, "history_deals_get", lambda a, b: [])
    closes = [2415.0 - i for i in range(16)]  # every bar down: RSI 0, ATR 2.0 (0.083%)
    monkeypatch.setattr(imr.mt5, "copy_rates_from_pos",
                        lambda s, tf, start, n: [{"close": c, "high": c + 1, "low": c - 1} for c in closes])
    monkeypatch.setattr(imr.mt5, "order_check", lambda req: state["check"](req))

    def order_send(req):
        sent.append(dict(req))
        return state["send"](req)
    monkeypatch.setattr(imr.mt5, "order_send", order_send)

    def events():
        path = tmp_path / "intraday-mr" / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def open_position(**overrides):
        pos = SimpleNamespace(ticket=555, magic=88011, type=0, volume=0.03, price_open=2390.0,
                              symbol="GOLD", time=int((NOW - timedelta(hours=3)).timestamp()))
        pos.__dict__.update(overrides)
        state["positions"] = [pos]
        return pos

    return SimpleNamespace(sent=sent, state=state, events=events, open_position=open_position,
                           run=lambda: imr.run_symbol(GOLD, NOW))


def _one(events, name):
    matching = [e for e in events if e["event"] == name]
    assert len(matching) == 1, f"expected exactly one {name}, got {[e['event'] for e in events]}"
    return matching[0]


class TestOrderPathIsUnchanged:
    def test_entry_request_dict_is_pinned(self, broker):
        broker.run()

        assert broker.sent == [PINNED_ENTRY_REQUEST]

    def test_close_request_dict_is_pinned(self, broker):
        broker.open_position()

        broker.run()

        assert broker.sent == [PINNED_CLOSE_REQUEST]
        assert [e["event"] for e in broker.events()] == ["exit", "live_exit_close"], \
            "still holding after the close attempt -> no entry evaluated"

    def test_first_passing_filling_mode_is_sent(self, broker):
        # IOC rejected, FOK accepted: the send must carry FOK, exactly as before.
        broker.state["check"] = lambda req: SimpleNamespace(
            retcode=0 if req["type_filling"] == imr.mt5.ORDER_FILLING_FOK else 10030, comment="")

        broker.run()

        assert len(broker.sent) == 1
        assert broker.sent[0] == {**PINNED_ENTRY_REQUEST, "type_filling": imr.mt5.ORDER_FILLING_FOK}

    def test_all_filling_modes_failing_logs_and_sends_nothing(self, broker):
        # The one deliberate behaviour change: a request no filling mode passes order_check
        # for used to be sent anyway (guaranteed server rejection). Now it is a logged skip.
        broker.state["check"] = lambda req: SimpleNamespace(retcode=10030, comment="Unsupported filling mode")

        broker.run()

        assert broker.sent == [], "must not send an order every order_check rejected"
        failed = _one(broker.events(), "order_check_all_filling_failed")
        assert [c["type_filling"] for c in failed["checks"]] == [1, 0, 2]  # IOC, FOK, RETURN
        assert all(c["retcode"] == 10030 for c in failed["checks"])
        assert failed["request"]["symbol"] == "GOLD" and failed["last_error"] is None

    def test_order_check_returning_none_is_recorded_with_last_error(self, broker):
        broker.state["check"] = lambda req: None

        broker.run()

        assert broker.sent == []
        failed = _one(broker.events(), "order_check_all_filling_failed")
        assert failed["last_error"] == [0, "stub"]
        assert all(c["retcode"] is None for c in failed["checks"])


class TestFillJournal:
    def test_live_order_sent_carries_the_full_fill_record(self, broker):
        broker.state["send"] = lambda req: _done(req, price=req["price"] + 0.05, position_id=222)

        broker.run()

        fill = _one(broker.events(), "live_order_sent")
        assert "slippage_pts" not in fill, "legacy key renamed; the analyzer reads slippage_points"
        assert fill["slippage_points"] == 5.0  # (2400.05 - 2400.00) / 0.01, adverse for a buy
        assert fill["requested_price"] == 2400.0 and fill["fill_price"] == 2400.05
        assert fill["price"] == 2400.05 and fill["volume"] == 0.03  # legacy fields kept
        assert (fill["bid"], fill["ask"], fill["spread_points"], fill["point"]) == (2399.7, 2400.0, 30, 0.01)
        assert fill["filling_mode"] == 1 and fill["deviation"] == 20
        assert fill["retcode"] == 10009 and fill["trade_exemode"] == 2
        assert fill["partial_fill"] is False and fill["filled_volume"] == 0.03
        assert (fill["order_ticket"], fill["deal_ticket"], fill["position_id"]) == (222, 111, 222)
        assert fill["result"]["retcode"] == 10009

    def test_partial_fill_is_flagged_and_missing_position_id_is_null(self, broker):
        broker.state["send"] = lambda req: _done(req, volume=0.02)

        broker.run()

        fill = _one(broker.events(), "live_order_sent")
        assert fill["partial_fill"] is True and fill["filled_volume"] == 0.02
        assert fill["position_id"] is None

    def test_paper_enter_carries_the_quote(self, broker, monkeypatch):
        monkeypatch.setattr(imr, "_is_live", lambda: False)

        broker.run()

        assert broker.sent == []
        paper = _one(broker.events(), "paper_enter")
        assert (paper["bid"], paper["ask"], paper["spread_points"], paper["point"]) == (2399.7, 2400.0, 30, 0.01)
        assert paper["side"] == "long"

    def test_skip_events_carry_the_quote(self, broker, tmp_path):
        # _news_blackout compares against the real clock, not the frozen signal time.
        until = (datetime.now(tz=timezone.utc) + timedelta(hours=2)).isoformat()
        (tmp_path / "news_state.json").write_text(
            json.dumps({"GOLD": {"blackout_until": until}}), encoding="utf-8")

        broker.run()

        skip = _one(broker.events(), "skip")
        assert "news blackout" in skip["reason"]
        assert (skip["bid"], skip["ask"], skip["spread_points"], skip["point"]) == (2399.7, 2400.0, 30, 0.01)
        assert broker.sent == []


class TestExitJournal:
    def test_exit_emits_legacy_event_and_live_exit_close(self, broker):
        broker.open_position()
        broker.state["send"] = lambda req: _done(req, price=2399.60)  # sold 10 pts below the bid

        broker.run()

        events = broker.events()
        legacy = _one(events, "exit")
        assert legacy["reason"] == "time_exit_3.0h" and legacy["result"]["retcode"] == 10009
        assert "slippage_points" not in legacy  # the legacy line is unchanged in shape

        close = _one(events, "live_exit_close")
        assert close["side"] == "sell" and close["position_ticket"] == 555
        assert close["requested_price"] == 2399.7 and close["fill_price"] == 2399.6
        assert close["slippage_points"] == 10.0  # adverse for a sell: (2399.70 - 2399.60) / 0.01
        assert close["entry_price"] == 2390.0 and close["volume"] == 0.03
        assert close["partial_fill"] is False and close["filled_volume"] == 0.03
        assert (close["bid"], close["ask"], close["spread_points"], close["point"]) == (2399.7, 2400.0, 30, 0.01)
        assert close["filling_mode"] == 1 and close["retcode"] == 10009
        assert (close["order_ticket"], close["deal_ticket"]) == (222, 111)
        assert close["reason"] == "time_exit_3.0h" and close["magic"] == 88011

    def test_rejected_exit_emits_live_exit_rejected_not_close(self, broker):
        broker.open_position()
        broker.state["send"] = lambda req: Result(retcode=10004, deal=0, order=0, volume=0.0,
                                                  price=0.0, comment="Requote")

        broker.run()

        events = broker.events()
        assert [e["event"] for e in events] == ["exit", "live_exit_rejected"]
        assert events[1]["retcode"] == 10004 and events[1]["last_error"] is None
        assert broker.sent == [PINNED_CLOSE_REQUEST]

    def test_young_position_is_held_and_blocks_entry(self, broker):
        broker.open_position(time=int((NOW - timedelta(minutes=30)).timestamp()))

        broker.run()

        assert broker.sent == [] and broker.events() == []


class TestMainFailsLoud:
    def test_initialize_failure_exits_1_and_receipt_records_it(self, monkeypatch, tmp_path):
        monkeypatch.setattr(imr.mt5, "initialize", lambda: False)
        monkeypatch.setattr(imr.mt5, "last_error", lambda: (-10005, "IPC timeout"))

        with pytest.raises(SystemExit) as exc:
            with run_receipt("MT5-IntradayMR", tmp_path) as receipt:
                imr.main(receipt)

        assert exc.value.code == 1
        written = json.loads((tmp_path / "MT5-IntradayMR.json").read_text(encoding="utf-8"))
        assert written["mt5_init"] is False and written["exit_code"] == 1 and written["ok"] is False

    def test_symbol_error_is_recorded_and_the_loop_continues(self, monkeypatch, tmp_path):
        monkeypatch.setattr(imr.mt5, "initialize", lambda: True)
        monkeypatch.setattr(imr.mt5, "shutdown", lambda: None)
        monkeypatch.setattr(imr, "LOG_DIR", tmp_path / "intraday-mr")
        ran: list[str] = []

        def run_symbol(spec, now):
            if spec["symbol"] == "GOLD":
                raise RuntimeError("boom")
            ran.append(spec["symbol"])
        monkeypatch.setattr(imr, "run_symbol", run_symbol)

        with run_receipt("MT5-IntradayMR", tmp_path) as receipt:
            imr.main(receipt)

        assert ran == ["USDJPY", "EURUSD", "GBPUSD"]
        assert receipt.errors == [{"type": "RuntimeError", "where": "run_symbol:GOLD"}]
        assert receipt.mt5_init is True
        written = json.loads((tmp_path / "MT5-IntradayMR.json").read_text(encoding="utf-8"))
        assert written["ok"] is False and written["exit_code"] == 0
        error_event = json.loads((tmp_path / "intraday-mr" / "events.jsonl").read_text().splitlines()[0])
        assert error_event["error_type"] == "RuntimeError" and error_event["where"] == "run_symbol:GOLD"

    def test_main_still_runs_without_a_receipt(self, monkeypatch):
        monkeypatch.setattr(imr.mt5, "initialize", lambda: True)
        monkeypatch.setattr(imr.mt5, "shutdown", lambda: None)
        monkeypatch.setattr(imr, "run_symbol", lambda spec, now: None)

        imr.main()  # no receipt argument: the pre-existing call shape
