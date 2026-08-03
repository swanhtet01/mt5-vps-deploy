"""Broker-clock coordinator for the structural one-hour strategies.

MT5 bars and ticks use the broker server clock on this account. The original
Windows tasks used the host UTC clock, so they executed different H1 buckets
from the buckets researched in MT5 history. This coordinator schedules from a
fresh MT5 tick instead and deduplicates each broker-clock entry window.

Real entries fail closed. They require all four controls:
  - MT5_GOLD_DRIFT_LIVE=1
  - MT5_STRUCTURAL_SCHEDULER_LIVE=1
  - the strategy magic in data_cache/structural_live_allowlist.json
  - a LIVE registry record that passes every research and paper-forward gate

With no effective allowlist, the scheduler records quote-based paper entries/exits only.
MT5_STRUCTURAL_FORCE_PAPER_ONLY=1 overrides every live entry control and is used
by unattended research launchers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

import MetaTrader5 as mt5

from mt5_agent.edge_registry import EdgeRegistry
from mt5_agent.mt5_execution import (
    FeedClockProvenance,
    feed_clock_provenance,
    normalize_volume,
    persistent_user_flag_enabled,
)


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from paths import DATA_CACHE, PAPER_ROOT, read_json, write_json_atomic  # noqa: E402


LIVE_ENV_FLAG = "MT5_GOLD_DRIFT_LIVE"
SCHEDULER_LIVE_ENV_FLAG = "MT5_STRUCTURAL_SCHEDULER_LIVE"
FORCE_PAPER_ONLY_ENV_FLAG = "MT5_STRUCTURAL_FORCE_PAPER_ONLY"
STATE_FILE = DATA_CACHE / "structural_scheduler_state.json"
ALLOWLIST_FILE = DATA_CACHE / "structural_live_allowlist.json"
REGISTRY_FILE = DATA_CACHE / "edge_registry.json"
PAPER_FORWARD_STATE_FILE = DATA_CACHE / "structural_paper_forward_state.json"
EVENT_FILE = PAPER_ROOT / "analytics" / "structural-scheduler.jsonl"
STRUCTURAL_MAGICS = set(range(88001, 88010))
ENTRY_WINDOW_MINUTES = 10
MAX_HOLD_MINUTES = 75
MAX_TICK_LAG_SECONDS = 5 * 60


def structural_specs() -> dict[str, dict]:
    """Return the researched broker-clock entry specifications."""
    from multi_drift_live import SIGNALS

    specs = {
        "GOLD_ASIAN": {
            "symbol": "GOLD",
            "weekdays": {0, 1, 2, 3, 4},
            "entry_hour": 0,
            "magic": 88001,
            "side": "long",
            "description": "GOLD weekday 00:00 broker-server entry",
        }
    }
    specs.update({name: dict(spec) for name, spec in SIGNALS.items()})
    specs["GBPJPY_MON_WF"] = {
        "symbol": "GBPJPY",
        "weekdays": {0},
        "weekday": 0,
        "entry_hour": 0,
        "exit_hour": 1,
        "research_weekday": 0,
        "research_return_hour": 1,
        "magic": 88901,
        "side": "long",
        "max_lot": 0.01,
        "paper_only": True,
        "use_regime_gate": False,
        "description": "GBPJPY Monday 00:00-01:00 feed-time walk-forward candidate",
        "source": "reports/structural-walk-forward-2026-08-03-refresh.json",
    }
    specs["AUDJPY_MON_WF"] = {
        "symbol": "AUDJPY",
        "weekdays": {0},
        "weekday": 0,
        "entry_hour": 0,
        "exit_hour": 1,
        "research_weekday": 0,
        "research_return_hour": 1,
        "magic": 88902,
        "side": "long",
        "max_lot": 0.01,
        "paper_only": True,
        "use_regime_gate": False,
        "description": "AUDJPY Monday 00:00-01:00 feed-time walk-forward candidate",
        "source": "reports/structural-walk-forward-2026-08-03-refresh.json",
    }
    specs["GBPJPY_THU_WF"] = {
        "symbol": "GBPJPY",
        "weekdays": {2},
        "weekday": 2,
        "entry_hour": 23,
        "exit_hour": 0,
        "research_weekday": 3,
        "research_return_hour": 0,
        "magic": 88903,
        "side": "short",
        "max_lot": 0.01,
        "paper_only": True,
        "use_regime_gate": False,
        "description": "GBPJPY Wednesday 23:00-Thursday 00:00 feed-time walk-forward candidate",
        "source": "reports/structural-walk-forward-2026-08-03-refresh.json",
    }
    return specs


def entry_window_key(name: str, server_now: datetime) -> str:
    return f"{name}:{server_now.date().isoformat()}:{server_now.hour:02d}"


def _spec_weekdays(spec: Mapping) -> set[int]:
    if "weekdays" in spec:
        raw = spec.get("weekdays") or []
        return {int(value) for value in raw}
    if "weekday" in spec:
        return {int(spec["weekday"])}
    return set()


def due_entries(
    server_now: datetime,
    specs: Mapping[str, Mapping],
    attempted: set[str],
    *,
    window_minutes: int = ENTRY_WINDOW_MINUTES,
) -> list[str]:
    """Find entries due in the broker-clock window and not already attempted."""
    if server_now.minute > window_minutes:
        return []
    due: list[str] = []
    for name, spec in specs.items():
        if server_now.weekday() not in _spec_weekdays(spec):
            continue
        if server_now.hour != int(spec["entry_hour"]):
            continue
        if entry_window_key(name, server_now) in attempted:
            continue
        due.append(name)
    return due


def overdue_positions(
    positions: Iterable,
    reference_server_epoch: int,
    *,
    max_hold_minutes: int = MAX_HOLD_MINUTES,
) -> list[tuple[object, float]]:
    """Return overdue structural positions using raw broker timestamps."""
    due: list[tuple[object, float]] = []
    for position in positions:
        try:
            magic = int(position.magic)
            opened = int(position.time)
        except (AttributeError, TypeError, ValueError):
            continue
        if magic not in STRUCTURAL_MAGICS:
            continue
        age_minutes = (int(reference_server_epoch) - opened) / 60.0
        if age_minutes >= max_hold_minutes:
            due.append((position, round(age_minutes, 3)))
    return due


def load_requested_live_allowlist(path: Path = ALLOWLIST_FILE) -> set[int]:
    """Read operator-requested strategy magics; missing or invalid means none."""
    payload = read_json(path, default={})
    raw = payload.get("enabled_magics", []) if isinstance(payload, dict) else []
    enabled: set[int] = set()
    for value in raw:
        try:
            magic = int(value)
        except (TypeError, ValueError):
            continue
        if magic in STRUCTURAL_MAGICS:
            enabled.add(magic)
    return enabled


def load_live_eligible_magics(path: Path = REGISTRY_FILE) -> set[int]:
    """Return LIVE registry edges that pass every validation gate."""
    registry = EdgeRegistry(path)
    return {
        int(edge.magic)
        for edge in registry.live()
        if edge.promotable and int(edge.magic) in STRUCTURAL_MAGICS
    }


def load_live_allowlist(
    path: Path = ALLOWLIST_FILE,
    registry_path: Path = REGISTRY_FILE,
) -> set[int]:
    """Effective allowlist = operator request intersected with validated LIVE edges."""
    return load_requested_live_allowlist(path) & load_live_eligible_magics(registry_path)


def _append_event(event: dict) -> None:
    EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, default=str, sort_keys=True) + "\n")
    print(json.dumps(event, indent=2, default=str, sort_keys=True))


def paper_trade_result(
    *,
    side: str,
    entry_price: float,
    exit_price: float,
    volume: float,
    tick_size: float,
    tick_value: float,
    point: float,
    slippage_points: float = 6.0,
) -> dict[str, float]:
    """Calculate quote-to-quote paper P/L with an extra slippage budget."""
    if volume <= 0 or tick_size <= 0 or tick_value <= 0 or point <= 0:
        raise ValueError("invalid paper-trade instrument metadata")
    normalized_side = side.lower()
    if normalized_side not in {"long", "short"}:
        raise ValueError(f"unsupported paper-trade side: {side}")
    direction = 1.0 if normalized_side == "long" else -1.0
    gross = direction * (exit_price - entry_price) / tick_size * tick_value * volume
    slippage = max(float(slippage_points), 0.0) * point / tick_size * tick_value * volume
    return {
        "gross_pnl_usd": gross,
        "slippage_budget_usd": slippage,
        "net_pnl_usd": gross - slippage,
    }


def _load_paper_forward_state() -> dict:
    payload = read_json(PAPER_FORWARD_STATE_FILE, default={})
    if not isinstance(payload, dict):
        payload = {}
    return {
        "open_positions": list(payload.get("open_positions") or []),
        "closed_trades": list(payload.get("closed_trades") or []),
    }


def _store_paper_forward_state(state: dict, clock: FeedClockProvenance) -> None:
    write_json_atomic(
        PAPER_FORWARD_STATE_FILE,
        {
            "updated_at_host_utc": datetime.now(tz=timezone.utc).isoformat(),
            "clock": clock.as_dict(),
            "open_positions": list(state.get("open_positions") or []),
            "closed_trades": list(state.get("closed_trades") or [])[-2000:],
        },
    )


def _open_paper_position(
    name: str,
    spec: Mapping,
    clock: FeedClockProvenance,
    *,
    regime_on: bool,
) -> None:
    if bool(spec.get("use_regime_gate", True)) and not regime_on:
        return
    state = _load_paper_forward_state()
    if any(position.get("signal") == name for position in state["open_positions"]):
        _append_event({
            "event": "paper_entry_skipped",
            "signal": name,
            "reason": "paper position already open",
            **clock.as_dict(),
        })
        return
    symbol = str(spec["symbol"])
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        _append_event({
            "event": "paper_entry_skipped", "signal": name, "symbol": symbol,
            "reason": "missing symbol info or tick", **clock.as_dict(),
        })
        return
    volume = normalize_volume(
        float(spec.get("max_lot") or info.volume_min),
        minimum=float(info.volume_min),
        maximum=float(info.volume_max),
        step=float(info.volume_step or info.volume_min),
    )
    if volume <= 0:
        _append_event({
            "event": "paper_entry_skipped", "signal": name, "symbol": symbol,
            "reason": "candidate volume is below broker minimum", **clock.as_dict(),
        })
        return
    side = str(spec["side"]).lower()
    entry_price = float(tick.ask if side == "long" else tick.bid)
    entry_epoch = int(getattr(tick, "time", 0) or 0)
    if entry_price <= 0 or entry_epoch <= 0:
        _append_event({
            "event": "paper_entry_skipped", "signal": name, "symbol": symbol,
            "reason": "invalid quote", **clock.as_dict(),
        })
        return
    position = {
        "id": entry_window_key(name, clock.feed_time),
        "signal": name,
        "magic": int(spec["magic"]),
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "entry_price": entry_price,
        "entry_feed_epoch": entry_epoch,
        "entry_feed_time": datetime.fromtimestamp(entry_epoch, tz=timezone.utc).isoformat(),
        "due_exit_feed_epoch": entry_epoch + 3600,
        "source": str(spec.get("source") or "structural_specs"),
    }
    state["open_positions"].append(position)
    _store_paper_forward_state(state, clock)
    _append_event({"event": "paper_position_opened", **position, **clock.as_dict()})


def _close_due_paper_positions(
    reference_feed_epoch: int,
    clock: FeedClockProvenance,
) -> None:
    state = _load_paper_forward_state()
    retained: list[dict] = []
    changed = False
    for position in state["open_positions"]:
        if reference_feed_epoch < int(position.get("due_exit_feed_epoch") or 0):
            retained.append(position)
            continue
        symbol = str(position.get("symbol") or "")
        fresh, tick, lag_seconds = _symbol_tick_is_fresh(symbol, reference_feed_epoch)
        info = mt5.symbol_info(symbol)
        if not fresh or tick is None or info is None:
            retained.append(position)
            _append_event({
                "event": "paper_exit_deferred",
                "signal": position.get("signal"),
                "symbol": symbol,
                "tick_lag_seconds": lag_seconds,
                "reason": "exit quote is missing or stale",
                **clock.as_dict(),
            })
            continue
        side = str(position["side"])
        exit_price = float(tick.bid if side == "long" else tick.ask)
        result = paper_trade_result(
            side=side,
            entry_price=float(position["entry_price"]),
            exit_price=exit_price,
            volume=float(position["volume"]),
            tick_size=float(info.trade_tick_size),
            tick_value=float(info.trade_tick_value),
            point=float(info.point),
        )
        closed = {
            **position,
            "exit_price": exit_price,
            "exit_feed_epoch": int(tick.time),
            "exit_feed_time": datetime.fromtimestamp(int(tick.time), tz=timezone.utc).isoformat(),
            "hold_minutes": round((int(tick.time) - int(position["entry_feed_epoch"])) / 60.0, 3),
            **{key: round(value, 6) for key, value in result.items()},
        }
        state["closed_trades"].append(closed)
        changed = True
        _append_event({"event": "paper_trade_closed", **closed, **clock.as_dict()})
    state["open_positions"] = retained
    if changed:
        _store_paper_forward_state(state, clock)


def _live_armed() -> bool:
    return (
        os.environ.get(FORCE_PAPER_ONLY_ENV_FLAG, "").strip() != "1"
        and persistent_user_flag_enabled(LIVE_ENV_FLAG)
        and persistent_user_flag_enabled(SCHEDULER_LIVE_ENV_FLAG)
    )


def _reference_tick() -> tuple[str, object]:
    for symbol in ("BTCUSD", "GOLD", "USDJPY"):
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and int(getattr(tick, "time", 0) or 0) > 0:
            return symbol, tick
    raise RuntimeError("no MT5 reference tick available")


def _feed_clock(
    reference_tick: object,
    host_utc: datetime | None = None,
) -> FeedClockProvenance:
    clock = feed_clock_provenance(reference_tick, host_utc=host_utc)
    if clock is None:
        raise RuntimeError("reference tick has no feed timestamp")
    if not clock.coherent:
        raise RuntimeError(
            "reference tick is stale or not coherent with a plausible whole-hour feed offset"
        )
    return clock


def _symbol_tick_is_fresh(symbol: str, reference_server_epoch: int) -> tuple[bool, object | None, float | None]:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or int(getattr(tick, "time", 0) or 0) <= 0:
        return False, tick, None
    lag = float(reference_server_epoch - int(tick.time))
    return abs(lag) <= MAX_TICK_LAG_SECONDS, tick, lag


def _load_attempted() -> set[str]:
    state = read_json(STATE_FILE, default={})
    raw = state.get("attempted_windows", []) if isinstance(state, dict) else []
    return {str(value) for value in raw if isinstance(value, str)}


def _store_attempted(attempted: set[str], clock: FeedClockProvenance) -> None:
    feed_now = clock.feed_time
    cutoff = (feed_now.date() - timedelta(days=45)).isoformat()
    retained = sorted(
        key for key in attempted
        if len(key.split(":")) >= 3 and key.split(":")[-2] >= cutoff
    )
    write_json_atomic(
        STATE_FILE,
        {
            "updated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
            "last_broker_server_time": feed_now.isoformat(),
            "clock": clock.as_dict(),
            "attempted_windows": retained,
        },
    )


def _paper_evaluate(
    name: str,
    spec: Mapping,
    clock: FeedClockProvenance,
    reason: str,
) -> None:
    from gold_drift_live import compute_regime_state as gold_regime
    from multi_drift_live import compute_regime_state as multi_regime

    if int(spec["magic"]) == 88001:
        symbol_info = mt5.symbol_info(str(spec["symbol"]))
        if symbol_info is None:
            regime_on, win_rate, count = False, 0.0, 0
        else:
            regime_on, win_rate, count = gold_regime(symbol_info)
    else:
        regime_on, win_rate, count = multi_regime(dict(spec))
    _append_event(
        {
            "event": "paper_entry_eval",
            "signal": name,
            "magic": int(spec["magic"]),
            "symbol": spec["symbol"],
            "broker_server_time": clock.feed_time.isoformat(),
            **clock.as_dict(),
            "regime_on": regime_on,
            "regime_required": bool(spec.get("use_regime_gate", True)),
            "trailing_win_rate": round(win_rate, 4),
            "trailing_n": count,
            "reason": reason,
        }
    )
    _open_paper_position(name, spec, clock, regime_on=regime_on)


def _run_entry(
    name: str,
    spec: Mapping,
    clock: FeedClockProvenance,
    allowlist: set[int],
) -> None:
    magic = int(spec["magic"])
    if bool(spec.get("paper_only")):
        _paper_evaluate(name, spec, clock, "research candidate is permanently paper-only")
        return
    live_authorized = _live_armed() and magic in allowlist
    if not live_authorized:
        missing: list[str] = []
        if not _live_armed():
            missing.append("scheduler live flags not both armed")
        if magic not in allowlist:
            missing.append(f"magic {magic} not allowlisted")
        _paper_evaluate(name, spec, clock, "; ".join(missing))
        return

    if magic == 88001:
        from gold_drift_live import live_enter

        live_enter(force=True)
    else:
        from multi_drift_live import live_enter

        live_enter(name, dict(spec), force=True)


def _run_overdue_exits(
    reference_server_epoch: int,
    specs: Mapping[str, Mapping],
    clock: FeedClockProvenance,
) -> None:
    positions = mt5.positions_get() or []
    by_magic = {int(spec["magic"]): (name, spec) for name, spec in specs.items()}
    for position, age_minutes in overdue_positions(positions, reference_server_epoch):
        magic = int(position.magic)
        name_spec = by_magic.get(magic)
        if name_spec is None:
            continue
        name, spec = name_spec
        # Entry authorization is deliberately fail-closed, but it must never
        # strand an existing position past the strategy's maximum hold time.
        _append_event(
            {
                "event": "overdue_exit_attempted",
                "signal": name,
                "magic": magic,
                "symbol": position.symbol,
                "ticket": position.ticket,
                "age_minutes": age_minutes,
                "entry_armed": _live_armed(),
                **clock.as_dict(),
            }
        )
        if magic == 88001:
            from gold_drift_live import live_exit

            live_exit()
        else:
            from multi_drift_live import live_exit

            live_exit(name, dict(spec))


def run_once() -> None:
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        reference_symbol, reference_tick = _reference_tick()
        reference_epoch = int(reference_tick.time)
        clock = _feed_clock(reference_tick)
        feed_now = clock.feed_time
        specs = structural_specs()
        _close_due_paper_positions(reference_epoch, clock)
        attempted = _load_attempted()
        allowlist = load_live_allowlist()

        for name in due_entries(feed_now, specs, attempted):
            key = entry_window_key(name, feed_now)
            attempted.add(key)
            _store_attempted(attempted, clock)
            spec = specs[name]
            fresh, _, lag_seconds = _symbol_tick_is_fresh(str(spec["symbol"]), reference_epoch)
            if not fresh:
                _append_event(
                    {
                        "event": "entry_skipped",
                        "signal": name,
                        "magic": int(spec["magic"]),
                        "symbol": spec["symbol"],
                        "broker_server_time": feed_now.isoformat(),
                        **clock.as_dict(),
                        "reference_symbol": reference_symbol,
                        "tick_lag_seconds": lag_seconds,
                        "reason": "symbol tick is missing or stale",
                    }
                )
                continue
            _run_entry(name, spec, clock, allowlist)

        _run_overdue_exits(reference_epoch, specs, clock)
        _store_attempted(attempted, clock)
        _append_event(
            {
                "event": "scheduler_heartbeat",
                "broker_server_time": feed_now.isoformat(),
                **clock.as_dict(),
                "reference_symbol": reference_symbol,
                "live_armed": _live_armed(),
                "force_paper_only": os.environ.get(FORCE_PAPER_ONLY_ENV_FLAG, "").strip() == "1",
                "requested_allowlist_magics": sorted(load_requested_live_allowlist()),
                "registry_eligible_magics": sorted(load_live_eligible_magics()),
                "allowlisted_magics": sorted(allowlist),
                "due_entries": due_entries(feed_now, specs, attempted),
            }
        )
    finally:
        mt5.shutdown()


def status() -> None:
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        reference_symbol, reference_tick = _reference_tick()
        reference_epoch = int(reference_tick.time)
        clock = _feed_clock(reference_tick)
        positions = mt5.positions_get() or []
        overdue = overdue_positions(positions, reference_epoch)
        paper_state = _load_paper_forward_state()
        paper_closed = list(paper_state.get("closed_trades") or [])
        print(
            json.dumps(
                {
                    "broker_server_time": clock.feed_time.isoformat(),
                    **clock.as_dict(),
                    "reference_symbol": reference_symbol,
                    "live_armed": _live_armed(),
                    "force_paper_only": os.environ.get(FORCE_PAPER_ONLY_ENV_FLAG, "").strip() == "1",
                    "requested_allowlist_magics": sorted(load_requested_live_allowlist()),
                    "registry_eligible_magics": sorted(load_live_eligible_magics()),
                    "allowlisted_magics": sorted(load_live_allowlist()),
                    "paper_forward": {
                        "open_positions": len(paper_state.get("open_positions") or []),
                        "closed_trades": len(paper_closed),
                        "net_pnl_usd": round(sum(
                            float(trade.get("net_pnl_usd") or 0.0)
                            for trade in paper_closed
                        ), 2),
                    },
                    "structural_positions": [
                        {
                            "ticket_last4": str(position.ticket)[-4:],
                            "magic": int(position.magic),
                            "symbol": position.symbol,
                            "age_minutes": round((reference_epoch - int(position.time)) / 60.0, 2),
                            "overdue": any(item.ticket == position.ticket for item, _ in overdue),
                        }
                        for position in positions
                        if int(getattr(position, "magic", 0)) in STRUCTURAL_MAGICS
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        mt5.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="read-only broker-clock and position status")
    args = parser.parse_args()
    if args.status:
        status()
    else:
        run_once()


if __name__ == "__main__":
    main()
