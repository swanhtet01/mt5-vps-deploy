"""Tests for the deploy copy of the slippage analyzer (hotfix/src/mt5_agent/slippage_analyzer.py).

The monorepo original bucketed spread in absolute points (<2 / 2-4 / >4), which put every
XM fill in 'wide' and made the regime breakdown meaningless; it also never produced the
``by_symbol_by_hour`` block the dashboard reads, so that panel was always zeros.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from mt5_agent import slippage_analyzer as sa
from mt5_agent.dashboard_metrics import _compute_slippage_by_hour


def _fill(symbol="GOLD", hour=8, slippage=3.0, spread=30, event="live_order_sent", **extra):
    return {"event": event, "ts": f"2026-09-01T{hour:02d}:15:00+00:00", "symbol": symbol,
            "slippage_points": slippage, "spread_points": spread, **extra}


def _write(tmp_path: Path, events, name="events.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


class TestRelativeSpreadRegimes:
    def test_buckets_are_relative_to_the_symbol_median(self, tmp_path):
        # Median spread 30: 10 is tight (0.33x), 60 is wide (2x), the 30s are normal.
        events = [_fill(spread=s, slippage=i) for i, s in enumerate((30, 30, 30, 30, 10, 60))]

        result = sa.analyze_slippage(_write(tmp_path, events))

        regimes = result["by_spread_regime"]
        assert {r: regimes[r]["n"] for r in regimes} == {"tight": 1, "normal": 4, "wide": 1}
        assert regimes["tight"]["mean_pts"] == 4.0 and regimes["wide"]["mean_pts"] == 5.0
        assert result["median_spread_points_by_symbol"] == {"GOLD": 30.0}
        assert not any(k.startswith("wide_>") for k in regimes), "absolute buckets are gone"

    def test_a_30_point_xm_spread_is_normal_not_wide(self, tmp_path):
        events = [_fill(spread=30), _fill(spread=32), _fill(spread=28)]

        result = sa.analyze_slippage(_write(tmp_path, events))

        assert list(result["by_spread_regime"]) == ["normal"]

    def test_medians_are_per_symbol(self, tmp_path):
        # EURUSD normally quotes ~1 pt; GOLD ~30. A 2-pt EURUSD spread is wide FOR EURUSD.
        events = [_fill("EURUSD", spread=1.0), _fill("EURUSD", spread=1.0), _fill("EURUSD", spread=2.0),
                  _fill("GOLD", spread=30), _fill("GOLD", spread=30)]

        result = sa.analyze_slippage(_write(tmp_path, events))

        assert result["by_spread_regime"]["wide"]["n"] == 1
        assert result["by_spread_regime"]["normal"]["n"] == 4
        assert result["median_spread_points_by_symbol"] == {"EURUSD": 1.0, "GOLD": 30.0}

    def test_boundaries_are_inclusive_for_normal(self, tmp_path):
        # median 40 -> 0.75x = 30 and 1.5x = 60 both land in 'normal'
        events = [_fill(spread=s) for s in (40, 40, 40, 30, 60)]

        result = sa.analyze_slippage(_write(tmp_path, events))

        assert result["by_spread_regime"]["normal"]["n"] == 5


class TestLegacyKeys:
    def test_spread_pts_and_slippage_pts_are_accepted(self, tmp_path):
        legacy = {"event": "live_order_sent", "ts": "2026-09-01T09:00:00+00:00", "symbol": "GOLD",
                  "slippage_pts": 7.0, "spread_pts": 25}
        current = _fill(slippage=1.0, spread=25)

        result = sa.analyze_slippage(_write(tmp_path, [legacy, current]))

        assert result["n_fills"] == 2 and result["mean_slippage_pts"] == 4.0
        assert result["by_spread_regime"]["normal"]["n"] == 2

    def test_spread_is_estimated_from_bid_ask_point_when_absent(self, tmp_path):
        events = [{"event": "live_order_sent", "ts": "2026-09-01T09:00:00+00:00", "symbol": "GOLD",
                   "slippage_points": 2.0, "bid": 2399.70, "ask": 2400.00, "point": 0.01}]

        result = sa.analyze_slippage(_write(tmp_path, events))

        assert result["median_spread_points_by_symbol"]["GOLD"] == 30.0
        assert result["by_spread_regime"]["normal"]["n"] == 1

    def test_signal_key_is_used_when_symbol_is_missing(self, tmp_path):
        # intraday_mean_rev._log stamps every line with signal=<symbol>
        events = [{"event": "live_exit_close", "ts": "2026-09-01T10:00:00+00:00", "signal": "USDJPY",
                   "slippage_points": 1.5}]

        result = sa.analyze_slippage(_write(tmp_path, events))

        assert list(result["by_symbol"]) == ["USDJPY"]


class TestExitsAreCounted:
    def test_live_exit_close_events_count_as_fills(self, tmp_path):
        events = [_fill(slippage=2.0), _fill(slippage=4.0, event="live_exit_close", partial_fill=True),
                  {"event": "exit", "ts": "2026-09-01T10:00:00+00:00", "signal": "GOLD",
                   "reason": "time_exit_2.1h", "result": {"retcode": 10009}}]  # legacy, no slippage

        result = sa.analyze_slippage(_write(tmp_path, events))

        assert result["events_by_type"] == {"live_order_sent": 1, "live_exit_close": 1}
        assert result["n_fills"] == 2 and result["mean_slippage_pts"] == 3.0
        assert result["partial_fill_count"] == 1 and result["partial_fill_rate"] == 0.5

    def test_non_fill_events_are_ignored(self, tmp_path):
        events = [{"event": "enter_eval", "ts": "2026-09-01T10:00:00+00:00", "symbol": "GOLD",
                   "spread_points": 30},
                  {"event": "order_check_all_filling_failed", "symbol": "GOLD"}]

        result = sa.analyze_slippage(_write(tmp_path, events))

        assert result == sa._empty_analysis()


class TestBySymbolByHour:
    def test_shape_matches_the_dashboard_consumer(self, tmp_path):
        events = [_fill("GOLD", hour=8, slippage=2.0), _fill("GOLD", hour=8, slippage=4.0),
                  _fill("USDJPY", hour=8, slippage=6.0), _fill("GOLD", hour=14, slippage=1.0)]

        result = sa.analyze_slippage(_write(tmp_path, events))

        by_symbol_by_hour = result["by_symbol_by_hour"]
        assert set(by_symbol_by_hour) == {"GOLD", "USDJPY"}
        assert set(by_symbol_by_hour["GOLD"]) == {"08", "14"}, "zero-padded two-digit hour keys"
        gold_08 = by_symbol_by_hour["GOLD"]["08"]
        assert gold_08["mean_slippage_pts"] == 3.0 and gold_08["n"] == 2
        assert gold_08["stdev"] == round(sa.stdev([2.0, 4.0]), 4)
        assert by_symbol_by_hour["GOLD"]["14"] == {"mean_slippage_pts": 1.0, "stdev": 0.0, "n": 1}

        # The real consumer: hour 8 averages GOLD (3.0) and USDJPY (6.0) -> 4.5
        panel = _compute_slippage_by_hour(result)
        assert panel["8"] == 4.5 and panel["14"] == 1.0 and panel["0"] == 0.0

    def test_hours_are_normalised_to_utc(self, tmp_path):
        events = [{"event": "live_order_sent", "ts": "2026-09-01T10:00:00+02:00", "symbol": "GOLD",
                   "slippage_points": 1.0}]

        result = sa.analyze_slippage(_write(tmp_path, events))

        assert list(result["by_symbol_by_hour"]["GOLD"]) == ["08"]
        assert list(result["by_hour_utc"]) == [8]

    def test_empty_analysis_carries_the_key(self, tmp_path):
        assert sa.analyze_slippage(tmp_path / "missing.jsonl")["by_symbol_by_hour"] == {}


class TestCostModelComparison:
    def test_optimistic_flag_when_live_exceeds_model(self, tmp_path):
        result = sa.analyze_slippage(_write(tmp_path, [_fill(slippage=5.0), _fill(slippage=7.0)]))

        report = sa.compare_to_cost_model(
            result, lambda symbol: SimpleNamespace(entry_slippage_points=4.0, stop_slippage_points=8.0))

        assert report["cost_model_optimistic"] is True
        assert report["by_symbol"]["GOLD"]["shortfall_pts"] == 2.0


class TestCli:
    def test_cli_merges_log_files_and_writes_output(self, tmp_path, capsys):
        first = _write(tmp_path, [_fill("GOLD", hour=8, slippage=2.0)], "intraday.jsonl")
        second = _write(tmp_path, [_fill("GOLD", hour=8, slippage=4.0, event="live_exit_close")], "gold.jsonl")
        output = tmp_path / "data_cache" / "slippage_analysis.json"

        code = sa.main(["--log-file", str(first), "--log-file", str(second),
                        "--log-file", str(tmp_path / "absent.jsonl"), "--output", str(output)])

        assert code == 0
        written = json.loads(output.read_text(encoding="utf-8"))
        assert written["n_fills"] == 2 and written["by_symbol_by_hour"]["GOLD"]["08"]["n"] == 2
        assert [s["events"] for s in written["sources"]] == [1, 1, 0]
        assert written["generated_utc"].endswith("+00:00")
        assert not list(output.parent.glob("*.tmp"))
        assert "2 fills" in capsys.readouterr().out

    def test_module_is_runnable_as_a_script(self):
        # `python -m mt5_agent.slippage_analyzer` needs a __main__ guard and sys.exit(main()).
        source = Path(sa.__file__).read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in source and "sys.exit(main())" in source
        assert sys.modules["mt5_agent"].__path__[0].endswith(str(Path("hotfix") / "src" / "mt5_agent"))
