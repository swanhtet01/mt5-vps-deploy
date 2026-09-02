"""First tests for the DEPLOYED mt5_agent.mt5_execution, the helper every live trader imports.

hotfix/src/mt5_agent/mt5_execution.py is manifest-pinned onto the VPS and imported by
gold_drift_live, multi_drift_live, intraday_mean_rev, structural_scheduler, the kill switch
and the health check, yet until now it had no tests on either lineage. These pin what the
code DOES today at the boundaries the money path depends on: the 1.0 sizing cap, the
floor-to-step volume rule, the slippage sign convention, the feed-clock coherence rule and
the exact spelling of the live flag.

Where a pinned behaviour looks questionable it is marked ``# CURRENT behaviour -- see report``
and left as is: this file tests hotfix code, it does not change it.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mt5_agent import mt5_execution as mx
from paths import read_json

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def test_module_under_test_is_the_deployed_copy():
    expected = REPO_ROOT / "hotfix" / "src" / "mt5_agent" / "mt5_execution.py"
    assert Path(mx.__file__).resolve() == expected


# --- context_sizing / record_is_fresh --------------------------------------------------------


def _record(minutes_old: float = 10, **extra):
    record = {"as_of": (NOW - timedelta(minutes=minutes_old)).isoformat()}
    record.update(extra)
    return record


class TestContextSizing:
    @pytest.mark.parametrize("raw", [1.0000001, 1.5, 2.0, 10.0, "2.5", 1e9])
    def test_multiplier_above_one_is_capped_at_one(self, raw):
        # The cap CLAUDE.md section 5 says must not be reconciled toward the "2x" docstrings.
        assert mx.context_sizing(_record(sizing_multiplier=raw), now=NOW) == (1.0, True)

    @pytest.mark.parametrize("raw", [-0.5, -1e9, "-3"])
    def test_negative_multiplier_clamps_to_zero(self, raw):
        assert mx.context_sizing(_record(sizing_multiplier=raw), now=NOW) == (0.0, True)

    @pytest.mark.parametrize("raw, expected", [(0.35, 0.35), (0, 0.0), (1, 1.0), ("0.5", 0.5)])
    def test_in_range_multiplier_passes_through(self, raw, expected):
        assert mx.context_sizing(_record(sizing_multiplier=raw), now=NOW) == (expected, True)

    def test_missing_multiplier_defaults_to_one(self):
        assert mx.context_sizing(_record(), now=NOW) == (1.0, True)

    @pytest.mark.parametrize(
        "raw", [None, "abc", "", [], {}, float("nan"), float("inf"), float("-inf")]
    )
    def test_unparseable_or_non_finite_multiplier_falls_back_to_one(self, raw):
        # Includes -inf: a non-finite value is replaced by the neutral 1.0, not clamped to 0.
        assert mx.context_sizing(_record(sizing_multiplier=raw), now=NOW) == (1.0, True)

    def test_stale_record_is_neutral_and_reported_not_fresh(self):
        stale = _record(minutes_old=181, sizing_multiplier=0.2)
        assert mx.context_sizing(stale, now=NOW) == (1.0, False)

    def test_age_exactly_at_the_limit_is_still_fresh(self):
        # `<=` in record_is_fresh: the boundary minute counts as fresh.
        edge = _record(minutes_old=180, sizing_multiplier=0.2)
        assert mx.context_sizing(edge, now=NOW) == (0.2, True)
        assert mx.context_sizing(edge, now=NOW + timedelta(seconds=1)) == (1.0, False)

    def test_caller_max_age_is_honoured(self):
        record = _record(minutes_old=30, sizing_multiplier=0.2)
        assert mx.context_sizing(record, now=NOW, max_age_minutes=60) == (0.2, True)
        assert mx.context_sizing(record, now=NOW, max_age_minutes=20) == (1.0, False)

    def test_record_can_extend_its_own_freshness_window(self):
        # CURRENT behaviour -- see report: the payload's own max_age_minutes overrides the
        # caller's limit, so whoever writes context_score.json decides how long its
        # sizing multiplier stays in force.
        record = _record(minutes_old=600, sizing_multiplier=0.2, max_age_minutes=1000)
        assert mx.context_sizing(record, now=NOW, max_age_minutes=180) == (0.2, True)

    def test_future_dated_record_counts_as_fresh(self):
        # CURRENT behaviour -- see report: a record stamped in the future (clock skew, or
        # a bad writer) is fresh for as long as it stays ahead of the host clock.
        record = _record(minutes_old=-60 * 24 * 365, sizing_multiplier=0.2)
        assert mx.context_sizing(record, now=NOW) == (0.2, True)

    def test_naive_as_of_is_read_as_utc(self):
        naive = (NOW - timedelta(minutes=10)).replace(tzinfo=None).isoformat()
        record = {"as_of": naive, "sizing_multiplier": 0.5}
        assert mx.context_sizing(record, now=NOW) == (0.5, True)

    def test_aware_non_utc_as_of_is_compared_as_an_instant(self):
        plus_two = timezone(timedelta(hours=2))
        record = {
            "as_of": (NOW - timedelta(minutes=10)).astimezone(plus_two).isoformat(),
            "sizing_multiplier": 0.5,
        }
        assert mx.context_sizing(record, now=NOW) == (0.5, True)

    @pytest.mark.parametrize(
        "payload",
        [{}, None, [], "text", {"as_of": None}, {"as_of": ""}, {"as_of": "not-a-date"}, {"as_of": 1725278400}],
    )
    def test_missing_or_malformed_as_of_is_stale_not_an_error(self, payload):
        assert mx.context_sizing(payload, now=NOW) == (1.0, False)

    def test_missing_file_through_the_deployed_reader_is_neutral(self, tmp_path):
        # gold_drift_live does `_read_json(path) or {}` then context_sizing(); pin the pair.
        payload = read_json(tmp_path / "context_score.json") or {}
        assert mx.context_sizing(payload, now=NOW) == (1.0, False)

    def test_malformed_json_through_the_deployed_reader_is_neutral(self, tmp_path):
        path = tmp_path / "context_score.json"
        path.write_text('{"as_of": "2026-09-02T11:50:00+00:00", "sizing_multiplier": 0.', encoding="utf-8")
        payload = read_json(path) or {}
        assert mx.context_sizing(payload, now=NOW) == (1.0, False)

    def test_bom_prefixed_json_through_the_deployed_reader_is_honoured(self, tmp_path):
        # Windows PowerShell 5 writes a BOM for -Encoding utf8; read_json must still parse it.
        path = tmp_path / "context_score.json"
        path.write_text(json.dumps(_record(sizing_multiplier=0.4)), encoding="utf-8-sig")
        assert mx.context_sizing(read_json(path) or {}, now=NOW) == (0.4, True)


class TestRecordIsFresh:
    @pytest.mark.parametrize(
        "override, fresh",
        [
            (None, True),   # absent -> caller's 180
            ("", True),     # falsy -> caller's 180
            (0, True),      # falsy -> caller's 180
            ("abc", True),  # unparseable -> caller's 180
            ("90", False),  # honoured: 90 < 100
            ("0", False),   # truthy string "0" -> int 0 -> floored to 1 minute
            (-5, False),    # floored to 1 minute
            (0.5, False),   # int(0.5) == 0 -> floored to 1 minute
            (120, True),
        ],
    )
    def test_record_level_max_age_override(self, override, fresh):
        record = _record(minutes_old=100)
        if override is not None:
            record["max_age_minutes"] = override
        assert mx.record_is_fresh(record, max_age_minutes=180, now=NOW) is fresh

    def test_caller_limit_is_floored_to_one_minute(self):
        assert mx.record_is_fresh(_record(minutes_old=0.5), max_age_minutes=0, now=NOW) is True
        assert mx.record_is_fresh(_record(minutes_old=2), max_age_minutes=-10, now=NOW) is False

    def test_non_dict_is_never_fresh(self):
        for record in (None, [], "as_of", 42):
            assert mx.record_is_fresh(record, now=NOW) is False


# --- normalize_volume ------------------------------------------------------------------------


def _nv(requested, *, minimum=0.01, maximum=1.0, step=0.01):
    return mx.normalize_volume(requested, minimum=minimum, maximum=maximum, step=step)


class TestNormalizeVolume:
    def test_rounds_down_to_the_broker_step(self):
        assert _nv(0.025) == 0.02
        assert _nv(0.0199) == 0.01

    @pytest.mark.parametrize("requested", [0.030000000000000002, 0.03, 0.07, 0.29, 0.57, 12.34])
    def test_float_residual_never_loses_a_step(self, requested):
        # 0.03/0.01 is 2.9999999999999996 in binary floating point; the epsilon in the
        # implementation must keep that from flooring to 0.02.
        assert _nv(requested, maximum=100.0) == round(requested, 2)

    @pytest.mark.parametrize(
        "requested, step, expected",
        [(0.35, 0.1, 0.3), (0.29, 0.1, 0.2), (2.5, 1.0, 2.0), (0.123456, 0.001, 0.123), (0.8, 0.25, 0.75), (0.7, 0.2, 0.6)],
    )
    def test_step_that_does_not_divide_evenly_floors(self, requested, step, expected):
        assert _nv(requested, minimum=step, maximum=100.0, step=step) == expected

    def test_clamps_to_maximum(self):
        assert _nv(100.0, maximum=0.5) == 0.5
        assert _nv(0.5, maximum=0.5) == 0.5

    def test_maximum_that_is_not_a_step_multiple_is_floored_too(self):
        assert _nv(1.0, maximum=0.055) == 0.05

    @pytest.mark.parametrize("requested", [0.005, 0.0099999, 0.0, -0.01, -1.0])
    def test_below_minimum_is_zero_never_rounded_up(self, requested):
        assert _nv(requested) == 0.0

    def test_exactly_minimum_is_kept(self):
        assert _nv(0.01) == 0.01

    @pytest.mark.parametrize("requested", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_request_is_zero(self, requested):
        assert _nv(requested) == 0.0

    def test_string_request_is_coerced(self):
        assert _nv("0.05") == 0.05

    @pytest.mark.parametrize(
        "limits",
        [dict(minimum=0.0, maximum=1.0, step=0.01), dict(minimum=-0.01, maximum=1.0, step=0.01),
         dict(minimum=0.1, maximum=0.05, step=0.01), dict(minimum=0.01, maximum=1.0, step=0.0),
         dict(minimum=0.01, maximum=1.0, step=-0.01)],
    )
    def test_invalid_broker_limits_raise(self, limits):
        with pytest.raises(ValueError):
            mx.normalize_volume(0.1, **limits)

    def test_integer_step_returns_whole_lots(self):
        assert _nv(3.9, minimum=1.0, maximum=10.0, step=1.0) == 3.0

    def test_result_never_exceeds_request_and_is_always_on_step(self):
        # Sweep a realistic lot range: the result may never increase requested risk and
        # must always be something the broker will accept.
        for thousandths in range(1, 2001):
            requested = thousandths / 1000.0
            got = _nv(requested, maximum=5.0)
            assert got <= requested + 1e-9, requested
            assert got == 0.0 or got >= 0.01, requested
            assert math.isclose(got * 100, round(got * 100), abs_tol=1e-6), requested


# --- adverse_slippage_points ----------------------------------------------------------------


class TestAdverseSlippagePoints:
    def test_buy_filled_above_request_is_adverse_and_positive(self):
        assert mx.adverse_slippage_points("buy", 2000.00, 2000.05, 0.01) == pytest.approx(5.0)

    def test_sell_filled_below_request_is_adverse_and_positive(self):
        assert mx.adverse_slippage_points("sell", 2000.00, 1999.95, 0.01) == pytest.approx(5.0)

    def test_favourable_fills_are_negative(self):
        assert mx.adverse_slippage_points("buy", 2000.00, 1999.97, 0.01) == pytest.approx(-3.0)
        assert mx.adverse_slippage_points("sell", 150.000, 150.004, 0.001) == pytest.approx(-4.0)

    def test_exact_fill_is_zero(self):
        assert mx.adverse_slippage_points("buy", 2000.0, 2000.0, 0.01) == 0.0

    @pytest.mark.parametrize("side", ["BUY", "Buy", "long", "LONG"])
    def test_long_aliases_are_case_insensitive(self, side):
        assert mx.adverse_slippage_points(side, 1.0, 1.1, 0.1) == pytest.approx(1.0)

    @pytest.mark.parametrize("side", ["SELL", "Sell", "short", "SHORT"])
    def test_short_aliases_are_case_insensitive(self, side):
        assert mx.adverse_slippage_points(side, 1.0, 0.9, 0.1) == pytest.approx(1.0)

    @pytest.mark.parametrize("point", [0.0, -0.01])
    def test_zero_or_negative_point_is_guarded_to_zero(self, point):
        assert mx.adverse_slippage_points("buy", 2000.0, 2010.0, point) == 0.0

    def test_unknown_side_raises(self):
        with pytest.raises(ValueError, match="unsupported order side"):
            mx.adverse_slippage_points("hold", 1.0, 1.0, 0.01)

    def test_zero_point_masks_an_invalid_side(self):
        # CURRENT behaviour -- see report: the point guard runs before side validation, so a
        # symbol with point==0 reports "no slippage" for any side, including a bogus one.
        assert mx.adverse_slippage_points("hold", 2000.0, 2010.0, 0.0) == 0.0


# --- feed-clock helpers -----------------------------------------------------------------------


def _tick(epoch):
    return SimpleNamespace(time=epoch)


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


class TestBrokerServerDatetime:
    def test_epoch_becomes_an_aware_utc_datetime(self):
        got = mx.broker_server_datetime(_tick(_epoch(NOW)))
        assert got == NOW and got.tzinfo == timezone.utc

    @pytest.mark.parametrize(
        "tick",
        [None, SimpleNamespace(), _tick(None), _tick("soon"), _tick("12.5"), _tick(0), _tick(-1)],
    )
    def test_missing_invalid_or_non_positive_epoch_is_none(self, tick):
        assert mx.broker_server_datetime(tick) is None

    def test_float_epoch_truncates_and_numeric_string_is_accepted(self):
        assert mx.broker_server_datetime(_tick(1.9)) == datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        assert mx.broker_server_datetime(_tick(str(_epoch(NOW)))) == NOW


class TestFeedClockProvenance:
    def _prov(self, delta: timedelta, **kwargs):
        return mx.feed_clock_provenance(_tick(_epoch(NOW + delta)), host_utc=NOW, **kwargs)

    def test_feed_equal_to_host_is_coherent_with_zero_offset(self):
        clock = self._prov(timedelta(0))
        assert clock.coherent is True
        assert clock.observed_offset_seconds == 0
        assert clock.whole_hour_offset_minutes == 0
        assert clock.residual_seconds == 0
        assert clock.host_utc == NOW and clock.feed_time == NOW

    def test_whole_hour_offset_with_small_residual_is_coherent(self):
        clock = self._prov(timedelta(hours=3, seconds=30))
        assert clock.coherent is True
        assert clock.whole_hour_offset_minutes == 180
        assert clock.residual_seconds == 30
        assert clock.observed_offset_seconds == 3 * 3600 + 30

    def test_negative_whole_hour_offset_is_coherent(self):
        clock = self._prov(timedelta(hours=-2, seconds=-10))
        assert clock.coherent is True
        assert clock.whole_hour_offset_minutes == -120
        assert clock.residual_seconds == -10

    def test_residual_at_the_limit_is_coherent_and_beyond_is_not(self):
        assert self._prov(timedelta(seconds=90)).coherent is True
        assert self._prov(timedelta(seconds=91)).coherent is False
        assert self._prov(timedelta(hours=3, seconds=120)).coherent is False

    def test_offset_at_max_hours_is_coherent_and_beyond_is_not(self):
        assert self._prov(timedelta(hours=14)).coherent is True
        assert self._prov(timedelta(hours=-14)).coherent is True
        assert self._prov(timedelta(hours=15)).coherent is False

    def test_half_hour_zone_offsets_are_never_coherent(self):
        # Only whole-hour offsets are accepted; a 30-minute residual fails either rounding.
        assert self._prov(timedelta(minutes=30)).coherent is False
        assert self._prov(timedelta(hours=5, minutes=30)).coherent is False

    def test_stale_tick_is_incoherent(self):
        assert self._prov(timedelta(hours=-2, minutes=-20)).coherent is False
        assert self._prov(timedelta(minutes=-45)).coherent is False

    def test_tick_stale_by_exactly_whole_hours_looks_like_a_zone_offset(self):
        # CURRENT behaviour -- inherent to measuring against "some whole-hour zone": a feed
        # frozen for exactly N hours is indistinguishable from an N-hour broker offset.
        assert self._prov(timedelta(hours=-1)).coherent is True

    def test_limits_are_floored_at_zero(self):
        assert self._prov(timedelta(hours=1), max_abs_offset_hours=-1).coherent is False
        assert self._prov(timedelta(0), max_abs_offset_hours=-1).coherent is True
        assert self._prov(timedelta(seconds=1), max_residual_seconds=-5).coherent is False
        assert self._prov(timedelta(0), max_residual_seconds=-5).coherent is True

    def test_naive_host_clock_is_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            mx.feed_clock_provenance(_tick(_epoch(NOW)), host_utc=NOW.replace(tzinfo=None))

    def test_aware_non_utc_host_is_normalised_to_utc(self):
        host = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
        clock = mx.feed_clock_provenance(_tick(_epoch(NOW)), host_utc=host)
        assert clock.host_utc == NOW and clock.host_utc.tzinfo == timezone.utc
        assert clock.observed_offset_seconds == 0 and clock.coherent is True

    def test_missing_or_invalid_tick_is_none(self):
        assert mx.feed_clock_provenance(None, host_utc=NOW) is None
        assert mx.feed_clock_provenance(_tick(0), host_utc=NOW) is None

    def test_host_defaults_to_the_wall_clock(self):
        live = datetime.now(tz=timezone.utc)
        clock = mx.feed_clock_provenance(_tick(_epoch(live)))
        assert clock.coherent is True and abs(clock.residual_seconds) < 5

    def test_as_dict_shape_and_rounding(self):
        clock = self._prov(timedelta(hours=3, seconds=20))
        assert clock.as_dict() == {
            "host_utc": NOW.isoformat(),
            "feed_time": (NOW + timedelta(hours=3, seconds=20)).isoformat(),
            "feed_offset_minutes": 180.333,
            "feed_whole_hour_offset_minutes": 180,
            "feed_clock_residual_seconds": 20.0,
            "feed_clock_coherent": True,
        }


class TestHistoryWindowFromFeedClock:
    FEED = NOW + timedelta(hours=3)

    def _clock(self, coherent=True):
        return mx.feed_clock_provenance(_tick(_epoch(self.FEED)), host_utc=NOW) if coherent else (
            mx.feed_clock_provenance(_tick(_epoch(self.FEED + timedelta(minutes=20))), host_utc=NOW)
        )

    def test_lookback_window_is_on_the_feed_clock_not_host_utc(self):
        window = mx.history_window_from_feed_clock(self._clock(), lookback=timedelta(hours=36))
        assert window.start == self.FEED - timedelta(hours=36)
        assert window.end == self.FEED + timedelta(minutes=5)
        assert window.clock.feed_time == self.FEED

    def test_start_of_feed_day_is_feed_midnight(self):
        window = mx.history_window_from_feed_clock(self._clock(), start_of_feed_day=True)
        assert window.start == self.FEED.replace(hour=0, minute=0, second=0, microsecond=0)
        assert window.end == self.FEED + timedelta(minutes=5)

    def test_safety_margin_is_configurable_including_zero(self):
        window = mx.history_window_from_feed_clock(self._clock(), lookback=timedelta(hours=1), safety_minutes=0)
        assert window.end == self.FEED
        window = mx.history_window_from_feed_clock(self._clock(), lookback=timedelta(hours=1), safety_minutes=2.5)
        assert window.end == self.FEED + timedelta(minutes=2, seconds=30)

    def test_none_or_incoherent_clock_fails_closed(self):
        with pytest.raises(RuntimeError, match="coherent"):
            mx.history_window_from_feed_clock(None, lookback=timedelta(hours=1))
        incoherent = self._clock(coherent=False)
        assert incoherent.coherent is False
        with pytest.raises(RuntimeError, match="coherent"):
            mx.history_window_from_feed_clock(incoherent, lookback=timedelta(hours=1))

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({}, "exactly one"),
            ({"lookback": timedelta(hours=1), "start_of_feed_day": True}, "exactly one"),
            ({"lookback": timedelta(0)}, "positive"),
            ({"lookback": timedelta(hours=-1)}, "positive"),
            ({"lookback": timedelta(hours=1), "safety_minutes": -1}, "non-negative"),
        ],
    )
    def test_invalid_bounds_raise_value_error(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            mx.history_window_from_feed_clock(self._clock(), **kwargs)

    def test_as_dict_merges_bounds_with_clock_provenance(self):
        window = mx.history_window_from_feed_clock(self._clock(), lookback=timedelta(hours=2))
        as_dict = window.as_dict()
        assert as_dict["history_start"] == (self.FEED - timedelta(hours=2)).isoformat()
        assert as_dict["history_end"] == (self.FEED + timedelta(minutes=5)).isoformat()
        assert as_dict["feed_whole_hour_offset_minutes"] == 180
        assert as_dict["feed_clock_coherent"] is True


class TestFeedHistoryWindow:
    def test_fresh_tick_yields_a_window(self):
        feed = NOW + timedelta(hours=2)
        window = mx.feed_history_window(_tick(_epoch(feed)), host_utc=NOW, lookback=timedelta(hours=1))
        assert window.start == feed - timedelta(hours=1)
        assert window.end == feed + timedelta(minutes=5)

    def test_stale_tick_fails_closed(self):
        stale = _tick(_epoch(NOW - timedelta(minutes=40)))
        with pytest.raises(RuntimeError, match="coherent"):
            mx.feed_history_window(stale, host_utc=NOW, lookback=timedelta(hours=1))

    def test_missing_tick_fails_closed(self):
        with pytest.raises(RuntimeError, match="coherent"):
            mx.feed_history_window(None, host_utc=NOW, lookback=timedelta(hours=1))


class _FakeMt5:
    """Minimal stand-in: symbol -> tick, exception instance, or None."""

    def __init__(self, ticks: dict):
        self.ticks = ticks
        self.calls: list[str] = []

    def symbol_info_tick(self, symbol):
        self.calls.append(symbol)
        value = self.ticks.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value


class TestCoherentFeedClockFromMt5:
    FRESH = _tick(_epoch(NOW + timedelta(hours=3)))
    STALE = _tick(_epoch(NOW - timedelta(minutes=50)))

    def test_returns_the_first_coherent_symbol_in_order(self):
        mt5 = _FakeMt5({"BTCUSD": self.FRESH, "GOLD": self.FRESH})
        symbol, clock = mx.coherent_feed_clock_from_mt5(mt5, ("BTCUSD", "GOLD"), host_utc=NOW)
        assert symbol == "BTCUSD" and clock.coherent is True
        assert mt5.calls == ["BTCUSD"]

    def test_falls_through_none_stale_and_raising_symbols(self):
        mt5 = _FakeMt5({"BTCUSD": None, "GOLD": self.STALE, "USDJPY": RuntimeError("boom"), "EURUSD": self.FRESH})
        symbol, _ = mx.coherent_feed_clock_from_mt5(mt5, ("BTCUSD", "GOLD", "USDJPY", "EURUSD"), host_utc=NOW)
        assert symbol == "EURUSD"
        assert mt5.calls == ["BTCUSD", "GOLD", "USDJPY", "EURUSD"]

    def test_symbols_are_deduplicated_and_empties_dropped(self):
        mt5 = _FakeMt5({})
        with pytest.raises(RuntimeError):
            mx.coherent_feed_clock_from_mt5(mt5, ("GOLD", "", "GOLD", "USDJPY", "GOLD"), host_utc=NOW)
        assert mt5.calls == ["GOLD", "USDJPY"]

    def test_all_failures_are_reported_with_their_reason(self):
        mt5 = _FakeMt5({"BTCUSD": None, "GOLD": self.STALE, "USDJPY": ValueError("closed")})
        with pytest.raises(RuntimeError) as excinfo:
            mx.coherent_feed_clock_from_mt5(mt5, ("BTCUSD", "GOLD", "USDJPY"), host_utc=NOW)
        message = str(excinfo.value)
        assert message.startswith("No fresh coherent MT5 reference clock is available (")
        assert "BTCUSD: stale, missing, or incoherent tick" in message
        assert "GOLD: stale, missing, or incoherent tick" in message
        assert "USDJPY: closed" in message

    def test_no_symbols_raises_a_bare_message(self):
        with pytest.raises(RuntimeError) as excinfo:
            mx.coherent_feed_clock_from_mt5(_FakeMt5({}), (), host_utc=NOW)
        assert str(excinfo.value) == "No fresh coherent MT5 reference clock is available"

    def test_the_test_stub_cannot_pass_as_a_broker(self):
        # conftest's MetaTrader5 stub raises on symbol_info_tick; the helper swallows that
        # into its failure list, so reaching the stub by accident still fails closed.
        import MetaTrader5 as stub

        with pytest.raises(RuntimeError, match="GOLD: test reached the real MetaTrader5 API"):
            mx.coherent_feed_clock_from_mt5(stub, ("GOLD",), host_utc=NOW)


# --- persistent_user_flag_enabled (the live master switch) -----------------------------------


@pytest.mark.skipif(os.name == "nt", reason="the Windows branch reads HKCU\\Environment, not os.environ")
class TestPersistentUserFlagEnabledOffWindows:
    FLAG = "MT5_TEST_LIVE_FLAG"

    @pytest.mark.parametrize("value", ["1", " 1 ", "1\n"])
    def test_only_a_literal_one_arms(self, monkeypatch, value):
        monkeypatch.setenv(self.FLAG, value)
        assert mx.persistent_user_flag_enabled(self.FLAG) is True

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", "on", "11", "1.0", "01"])
    def test_anything_else_stays_disarmed(self, monkeypatch, value):
        monkeypatch.setenv(self.FLAG, value)
        assert mx.persistent_user_flag_enabled(self.FLAG) is False

    def test_unset_is_disarmed(self, monkeypatch):
        monkeypatch.delenv(self.FLAG, raising=False)
        assert mx.persistent_user_flag_enabled(self.FLAG) is False

    @pytest.mark.parametrize("name", ["", "   "])
    def test_blank_name_is_disarmed(self, name):
        assert mx.persistent_user_flag_enabled(name) is False


# --- successful_deal_retcode --------------------------------------------------------------------


class TestSuccessfulDealRetcode:
    def test_done_and_partial_are_success(self):
        assert mx.successful_deal_retcode(10009, done=10009, done_partial=10010) is True
        assert mx.successful_deal_retcode(10010, done=10009, done_partial=10010) is True

    @pytest.mark.parametrize("retcode", [None, 0, 10004, 10006, 10013, 10018, 10019])
    def test_everything_else_is_failure(self, retcode):
        assert mx.successful_deal_retcode(retcode, done=10009, done_partial=10010) is False
