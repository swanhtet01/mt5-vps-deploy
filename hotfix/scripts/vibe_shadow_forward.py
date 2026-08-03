"""Run one quote-only Vibe paper-forward cycle against completed XM H1 bars."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import MetaTrader5 as mt5
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from mt5_agent.fdr_ledger import FDRLedger  # noqa: E402
from mt5_agent.mt5_execution import FeedClockProvenance, feed_clock_provenance, normalize_volume  # noqa: E402
from mt5_agent.vibe_rules import prepare_frame, rule_exit, signal  # noqa: E402
from mt5_agent.vibe_shadow import (  # noqa: E402
    FDR_FAMILY,
    build_report,
    entry_eligible_experiments,
    forward_metrics,
    load_state,
    load_vibe_artifacts,
    merge_experiment_catalog,
    paper_trade_result,
    register_screen_trials,
)
from paths import DATA_CACHE, PAPER_ROOT, write_json_atomic  # noqa: E402


SIDECAR_ROOT = Path(os.environ.get("MT5_VIBE_ROOT", r"C:\mt5-vibe-research"))
STATE_FILE = DATA_CACHE / "vibe_shadow_forward_state.json"
REPORT_FILE = DATA_CACHE / "vibe_shadow_forward_report.json"
FDR_LEDGER_FILE = DATA_CACHE / "fdr_ledger.jsonl"
EVENT_FILE = PAPER_ROOT / "analytics" / "vibe-shadow.jsonl"
ENTRY_WINDOW_MINUTES = 10
MAX_TICK_LAG_SECONDS = 5 * 60
RATE_COUNT = 700
ATTEMPT_RETENTION_DAYS = 120


def _append_event(event: Mapping[str, Any]) -> None:
    payload = dict(event)
    EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
    print(json.dumps(payload, default=str, sort_keys=True))


def _reference_tick() -> tuple[str, object]:
    for symbol in ("BTCUSD", "GOLD", "USDJPY"):
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and int(getattr(tick, "time", 0) or 0) > 0:
            return symbol, tick
    raise RuntimeError("no MT5 reference tick is available")


def _feed_clock(reference_tick: object) -> FeedClockProvenance:
    clock = feed_clock_provenance(reference_tick)
    if clock is None or not clock.coherent:
        raise RuntimeError("MT5 feed clock is missing, stale, or incoherent")
    return clock


def _fresh_tick(symbol: str, reference_epoch: int) -> tuple[object | None, float | None]:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or int(getattr(tick, "time", 0) or 0) <= 0:
        return None, None
    lag = float(reference_epoch - int(tick.time))
    if abs(lag) > MAX_TICK_LAG_SECONDS:
        return None, lag
    return tick, lag


def _rates_frame(symbol: str) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, RATE_COUNT)
    if rates is None or len(rates) < 500:
        return None
    frame = pd.DataFrame(rates)
    required = {"time", "open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        return None
    numeric_time = pd.to_numeric(frame["time"], errors="coerce")
    frame = frame.loc[numeric_time.notna()].copy()
    frame.index = pd.to_datetime(numeric_time[numeric_time.notna()].astype("int64"), unit="s", utc=True)
    return prepare_frame(frame)


def _quote_price(tick: object, side: str, *, entry: bool) -> float:
    if entry:
        value = getattr(tick, "ask" if side == "long" else "bid", 0)
    else:
        value = getattr(tick, "bid" if side == "long" else "ask", 0)
    try:
        price = float(value)
    except (TypeError, ValueError):
        return 0.0
    return price if math.isfinite(price) and price > 0 else 0.0


def _instrument_values(info: object) -> tuple[float, float, float, float, float, float]:
    values = (
        float(getattr(info, "volume_min", 0) or 0),
        float(getattr(info, "volume_max", 0) or 0),
        float(getattr(info, "volume_step", 0) or 0),
        float(getattr(info, "trade_tick_size", 0) or 0),
        float(getattr(info, "trade_tick_value", 0) or 0),
        float(getattr(info, "point", 0) or 0),
    )
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("symbol contract metadata is incomplete")
    return values


def _trim_attempted(keys: list[Any], reference_epoch: int) -> list[str]:
    cutoff = reference_epoch - ATTEMPT_RETENTION_DAYS * 86400
    retained: set[str] = set()
    for raw in keys:
        if not isinstance(raw, str):
            continue
        try:
            signal_epoch = int(raw.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if signal_epoch >= cutoff:
            retained.add(raw)
    return sorted(retained)


def _close_positions(
    state: dict[str, Any],
    *,
    reference_epoch: int,
    clock: FeedClockProvenance,
    actions: list[dict[str, Any]],
) -> None:
    frames: dict[str, pd.DataFrame | None] = {}
    retained: list[dict[str, Any]] = []
    for position in list(state.get("open_positions") or []):
        symbol = str(position.get("symbol") or "")
        side = str(position.get("direction") or "")
        tick, lag = _fresh_tick(symbol, reference_epoch)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            action = {
                "event": "shadow_exit_deferred",
                "experiment_key": position.get("experiment_key"),
                "symbol": symbol,
                "reason": "fresh exit quote or symbol metadata unavailable",
                "tick_lag_seconds": lag,
            }
            retained.append(position)
            actions.append(action)
            _append_event({**action, **clock.as_dict()})
            continue
        exit_price = _quote_price(tick, side, entry=False)
        if exit_price <= 0:
            retained.append(position)
            continue

        stop_price = float(position["stop_price"])
        stop_hit = (side == "long" and exit_price <= stop_price) or (
            side == "short" and exit_price >= stop_price
        )
        exit_reason = "stop" if stop_hit else None
        completed_bars = 0
        latest_bar_epoch: int | None = None
        if exit_reason is None:
            if symbol not in frames:
                frames[symbol] = _rates_frame(symbol)
            frame = frames[symbol]
            if frame is not None and not frame.empty:
                entry_bar_epoch = int(position["entry_bar_epoch"])
                epochs = (frame.index.astype("int64") // 1_000_000_000).astype("int64")
                eligible = frame.loc[epochs >= entry_bar_epoch]
                completed_bars = len(eligible)
                if completed_bars:
                    latest = eligible.iloc[-1]
                    latest_bar_epoch = int(eligible.index[-1].timestamp())
                    direction = 1 if side == "long" else -1
                    if rule_exit(latest, str(position["family"]), direction):
                        exit_reason = "rule_exit"
                    elif completed_bars >= int(position["maximum_hold_bars"]):
                        exit_reason = "maximum_hold"
        if exit_reason is None:
            retained.append(position)
            continue

        try:
            _, _, _, tick_size, tick_value, point = _instrument_values(info)
            result = paper_trade_result(
                side=side,
                entry_price=float(position["entry_price"]),
                exit_price=exit_price,
                volume=float(position["volume"]),
                tick_size=tick_size,
                tick_value=tick_value,
                point=point,
                slippage_points_round_trip=float(position["slippage_points_round_trip"]),
            )
        except ValueError:
            retained.append(position)
            continue
        closed = {
            **position,
            "exit_price": exit_price,
            "exit_feed_epoch": int(getattr(tick, "time")),
            "exit_feed_time": datetime.fromtimestamp(int(getattr(tick, "time")), tz=timezone.utc).isoformat(),
            "exit_reason": exit_reason,
            "completed_hold_bars": completed_bars,
            "latest_completed_bar_epoch": latest_bar_epoch,
            "hold_minutes": round(
                (int(getattr(tick, "time")) - int(position["entry_feed_epoch"])) / 60.0, 3
            ),
            **{key: round(value, 6) for key, value in result.items()},
        }
        state.setdefault("closed_trades", []).append(closed)
        action = {
            "event": "shadow_trade_closed",
            "experiment_key": position["experiment_key"],
            "symbol": symbol,
            "direction": side,
            "exit_reason": exit_reason,
            "net_pnl_usd": closed["net_pnl_usd"],
        }
        actions.append(action)
        _append_event({**action, **clock.as_dict()})
    state["open_positions"] = retained


def _mark_open_positions(
    state: dict[str, Any],
    *,
    reference_epoch: int,
) -> list[dict[str, Any]]:
    """Estimate executable quote P/L if each paper position closed now."""
    marks: list[dict[str, Any]] = []
    for position in list(state.get("open_positions") or []):
        symbol = str(position.get("symbol") or "")
        side = str(position.get("direction") or "")
        tick, lag = _fresh_tick(symbol, reference_epoch)
        info = mt5.symbol_info(symbol)
        mark = {
            "experiment_key": position.get("experiment_key"),
            "symbol": symbol,
            "direction": side,
            "volume": float(position.get("volume") or 0.0),
            "entry_price": float(position.get("entry_price") or 0.0),
            "mark_available": False,
            "tick_lag_seconds": lag,
        }
        if tick is None or info is None:
            marks.append(mark)
            continue
        mark_price = _quote_price(tick, side, entry=False)
        if mark_price <= 0:
            marks.append(mark)
            continue
        try:
            _, _, _, tick_size, tick_value, point = _instrument_values(info)
            result = paper_trade_result(
                side=side,
                entry_price=mark["entry_price"],
                exit_price=mark_price,
                volume=mark["volume"],
                tick_size=tick_size,
                tick_value=tick_value,
                point=point,
                slippage_points_round_trip=float(
                    position.get("slippage_points_round_trip") or 0.0
                ),
            )
        except ValueError:
            marks.append(mark)
            continue
        marks.append(
            {
                **mark,
                "mark_available": True,
                "mark_price": mark_price,
                "mark_feed_time": datetime.fromtimestamp(
                    int(getattr(tick, "time")), tz=timezone.utc
                ).isoformat(),
                **{key: round(value, 6) for key, value in result.items()},
            }
        )
    return marks


def _open_positions(
    state: dict[str, Any],
    experiments: tuple[dict[str, Any], ...],
    *,
    reference_epoch: int,
    clock: FeedClockProvenance,
    actions: list[dict[str, Any]],
) -> None:
    if clock.feed_time.minute > ENTRY_WINDOW_MINUTES:
        return
    frames: dict[str, pd.DataFrame | None] = {}
    attempted = set(_trim_attempted(list(state.get("attempted_signals") or []), reference_epoch))
    open_keys = {position.get("experiment_key") for position in state.get("open_positions") or []}
    current_bar_epoch = (reference_epoch // 3600) * 3600
    expected_signal_epoch = current_bar_epoch - 3600
    for experiment in experiments:
        key = experiment["experiment_key"]
        if key in open_keys:
            continue
        symbol = str(experiment["symbol"])
        tick, lag = _fresh_tick(symbol, reference_epoch)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            continue
        if symbol not in frames:
            frames[symbol] = _rates_frame(symbol)
        frame = frames[symbol]
        if frame is None or frame.empty:
            continue
        signal_row = frame.iloc[-1]
        signal_epoch = int(frame.index[-1].timestamp())
        if signal_epoch != expected_signal_epoch:
            continue
        direction_value = 1 if experiment["direction"] == "long" else -1
        if not signal(signal_row, str(experiment["family"]), direction_value):
            continue
        signal_key = f"{experiment['spec_fingerprint']}:{signal_epoch}"
        if signal_key in attempted:
            continue
        try:
            volume_min, volume_max, volume_step, _, _, _ = _instrument_values(info)
            volume = normalize_volume(
                float(experiment["minimum_lot"]),
                minimum=volume_min,
                maximum=volume_max,
                step=volume_step,
            )
        except ValueError:
            continue
        if volume <= 0 or not math.isclose(volume, volume_min, rel_tol=1e-9, abs_tol=1e-12):
            continue
        entry_price = _quote_price(tick, str(experiment["direction"]), entry=True)
        atr = float(signal_row.get("atr14") or 0)
        if entry_price <= 0 or not math.isfinite(atr) or atr <= 0:
            continue
        stop_price = entry_price - direction_value * float(experiment["stop_atr"]) * atr
        position = {
            "experiment_key": key,
            "screen_id": experiment["screen_id"],
            "spec_fingerprint": experiment["spec_fingerprint"],
            "source_screen_sha256": experiment["source_screen_sha256"],
            "symbol": symbol,
            "family": experiment["family"],
            "direction": experiment["direction"],
            "volume": volume,
            "entry_price": entry_price,
            "entry_feed_epoch": int(getattr(tick, "time")),
            "entry_feed_time": datetime.fromtimestamp(int(getattr(tick, "time")), tz=timezone.utc).isoformat(),
            "entry_bar_epoch": current_bar_epoch,
            "signal_bar_epoch": signal_epoch,
            "signal_key": signal_key,
            "signal_atr": atr,
            "stop_price": stop_price,
            "maximum_hold_bars": int(experiment["maximum_hold_bars"]),
            "slippage_points_round_trip": float(experiment["slippage_points_round_trip"]),
            "paper_only": True,
            "order_authority": False,
        }
        state.setdefault("open_positions", []).append(position)
        attempted.add(signal_key)
        open_keys.add(key)
        action = {
            "event": "shadow_position_opened",
            "experiment_key": key,
            "symbol": symbol,
            "direction": experiment["direction"],
            "volume": volume,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "tick_lag_seconds": lag,
        }
        actions.append(action)
        _append_event({**action, **clock.as_dict()})
    state["attempted_signals"] = sorted(attempted)


def run_once(
    *,
    sidecar_root: Path = SIDECAR_ROOT,
    state_file: Path = STATE_FILE,
    report_file: Path = REPORT_FILE,
    ledger_file: Path = FDR_LEDGER_FILE,
) -> dict[str, Any]:
    state = load_state(state_file)
    actions: list[dict[str, Any]] = []
    artifacts = None
    artifact_status = "BLOCKED"
    artifact_reason: str | None = None
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        reference_symbol, reference_tick = _reference_tick()
        reference_epoch = int(getattr(reference_tick, "time"))
        clock = _feed_clock(reference_tick)
        _close_positions(
            state,
            reference_epoch=reference_epoch,
            clock=clock,
            actions=actions,
        )
        ledger = FDRLedger(ledger_file)
        try:
            artifacts = load_vibe_artifacts(sidecar_root)
            register_screen_trials(artifacts, ledger)
            state["experiment_catalog"] = merge_experiment_catalog(
                list(state.get("experiment_catalog") or []),
                artifacts.experiments,
            )
            state["active_screen_sha256"] = artifacts.screen_sha256
            artifact_status = "PASS"
            entry_experiments = entry_eligible_experiments(
                list(state["experiment_catalog"]),
                list(state.get("closed_trades") or []),
            )
            _open_positions(
                state,
                tuple(entry_experiments),
                reference_epoch=reference_epoch,
                clock=clock,
                actions=actions,
            )
        except (OSError, ValueError) as exc:
            artifact_reason = str(exc)
            catalog = list(state.get("experiment_catalog") or [])
            discoveries = {
                item.get("experiment_key"): ledger.is_discovery(FDR_FAMILY, item.get("experiment_key"))
                for item in catalog
                if isinstance(item, dict) and isinstance(item.get("experiment_key"), str)
            }
            action = {"event": "shadow_entries_blocked", "reason": artifact_reason}
            actions.append(action)
            _append_event({**action, **clock.as_dict()})

        catalog = list(state.get("experiment_catalog") or [])
        discoveries = {
            item["experiment_key"]: ledger.is_discovery(FDR_FAMILY, item["experiment_key"])
            for item in catalog
            if isinstance(item, dict) and isinstance(item.get("experiment_key"), str)
        }
        active_keys = {
            item["experiment_key"]
            for item in entry_eligible_experiments(
                catalog,
                list(state.get("closed_trades") or []),
            )
        } if artifact_status == "PASS" else set()
        experiment_reports = forward_metrics(
            catalog,
            list(state.get("closed_trades") or []),
            discoveries,
        )
        for experiment in experiment_reports:
            experiment["observation_active"] = experiment["experiment_key"] in active_keys
        state["attempted_signals"] = _trim_attempted(
            list(state.get("attempted_signals") or []), reference_epoch
        )
        state["closed_trades"] = list(state.get("closed_trades") or [])[-10000:]
        open_position_marks = _mark_open_positions(
            state,
            reference_epoch=reference_epoch,
        )
        state["updated_at_host_utc"] = datetime.now(tz=timezone.utc).isoformat()
        state["last_clock"] = clock.as_dict()
        write_json_atomic(state_file, state)
        report = build_report(
            state=state,
            artifacts=artifacts,
            experiments=experiment_reports,
            artifact_status=artifact_status,
            artifact_reason=artifact_reason,
            clock={**clock.as_dict(), "reference_symbol": reference_symbol},
            actions=actions,
            open_position_marks=open_position_marks,
        )
        write_json_atomic(report_file, report)
        heartbeat = {
            "event": "shadow_heartbeat",
            "artifact_status": artifact_status,
            "experiment_count": report["experiment_count"],
            "active_entry_experiment_count": report["active_entry_experiment_count"],
            "open_position_count": report["open_position_count"],
            "closed_trade_count": report["closed_trade_count"],
            "paper_net_pnl_usd": report["paper_net_pnl_usd"],
            "paper_unrealized_net_if_closed_usd": report[
                "paper_unrealized_net_if_closed_usd"
            ],
            "paper_evidence_gate_pass_count": report["paper_evidence_gate_pass_count"],
            "order_authority": False,
            "live_eligible_count": 0,
            **clock.as_dict(),
        }
        _append_event(heartbeat)
        return report
    finally:
        mt5.shutdown()


def status(report_file: Path = REPORT_FILE) -> None:
    if not report_file.is_file():
        raise RuntimeError("Vibe shadow report has not been created")
    print(report_file.read_text(encoding="utf-8-sig"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--sidecar-root", type=Path, default=SIDECAR_ROOT)
    parser.add_argument("--state-file", type=Path, default=STATE_FILE)
    parser.add_argument("--report-file", type=Path, default=REPORT_FILE)
    parser.add_argument("--ledger-file", type=Path, default=FDR_LEDGER_FILE)
    args = parser.parse_args()
    if args.status:
        status(args.report_file)
    else:
        run_once(
            sidecar_root=args.sidecar_root,
            state_file=args.state_file,
            report_file=args.report_file,
            ledger_file=args.ledger_file,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
