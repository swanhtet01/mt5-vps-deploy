"""Generate fail-closed, profit-funded position-size recommendations.

Only closed trades attributable to one strategy can establish that strategy's
edge. Account-wide history, including manual losses, can reduce the multiplier
cap but manual profits can never promote an automated strategy.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import coherent_feed_clock_from_mt5, history_window_from_feed_clock
from mt5_agent.profit_funded_scaling import (
    SCHEMA,
    bootstrap_scaling_stress,
    evaluate_account_guard,
    evaluate_profit_funded_scaling,
    simulate_profit_funded_path,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA_CACHE, write_json_atomic  # noqa: E402


OUT_FILE = DATA_CACHE / "position_sizing.json"
REFERENCE_SYMBOLS = ("BTCUSD", "GOLD", "USDJPY")
HISTORY_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
CODEX_MAGIC_MIN = 26_060_000
CODEX_MAGIC_MAX = 26_069_999
STRUCTURAL_MAGICS = {
    88001: "GOLD_DRIFT",
    88002: "USDJPY_MON",
    88003: "UK100_THU",
    88004: "GOLD_FRI",
    88005: "USDJPY_WED",
    88006: "GOLD_THU",
    88007: "AUDJPY_MON",
    88008: "GBPJPY_THU",
    88009: "GOLD_TUE",
}


def _is_agent_magic(magic: int) -> bool:
    return magic in STRUCTURAL_MAGICS or CODEX_MAGIC_MIN <= magic <= CODEX_MAGIC_MAX


def _closed_trade_records(deals: list) -> list[dict]:
    by_position: dict[int, list] = defaultdict(list)
    for deal in deals:
        position_id = int(getattr(deal, "position_id", 0) or 0)
        if position_id > 0:
            by_position[position_id].append(deal)

    out_codes = {
        int(getattr(mt5, "DEAL_ENTRY_OUT", 1)),
        int(getattr(mt5, "DEAL_ENTRY_INOUT", 2)),
        int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)),
    }
    records: list[dict] = []
    for position_id, position_deals in by_position.items():
        ordered = sorted(position_deals, key=lambda deal: (int(deal.time), int(deal.ticket)))
        if not any(int(getattr(deal, "entry", -1)) in out_codes for deal in ordered):
            continue
        entry = next(
            (deal for deal in ordered if int(getattr(deal, "entry", -1)) == 0),
            ordered[0],
        )
        magic = int(getattr(entry, "magic", 0) or 0)
        net = sum(
            float(getattr(deal, "profit", 0.0) or 0.0)
            + float(getattr(deal, "commission", 0.0) or 0.0)
            + float(getattr(deal, "swap", 0.0) or 0.0)
            + float(getattr(deal, "fee", 0.0) or 0.0)
            for deal in ordered
        )
        records.append(
            {
                "position_id": position_id,
                "close_time": max(int(deal.time) for deal in ordered),
                "magic": magic,
                "symbol": str(getattr(entry, "symbol", "") or ""),
                "net": net,
            }
        )
    return sorted(records, key=lambda record: (record["close_time"], record["position_id"]))


def stats(nets: list[float], *, multiplier_cap: float = 3.0) -> dict:
    """Compatibility wrapper used by tests and operational diagnostics."""
    decision = evaluate_profit_funded_scaling(nets, multiplier_cap=multiplier_cap)
    statistics = decision["statistics"]
    return {
        "n": statistics["closed_trades"],
        "win_rate": statistics["win_rate_pct"],
        "mean_per_trade": statistics["mean_per_trade_usd"],
        "t_stat": statistics["t_stat"],
        "ci95_mean_lower": statistics["ci95_mean_lower_usd"],
        "ci95_mean_upper": statistics["ci95_mean_upper_usd"],
        "profit_factor": statistics["profit_factor"],
        "loss_streak": statistics["current_losing_streak"],
        "rolling20_pf": statistics["recent_profit_factor"],
        "net_profit": statistics["net_profit_usd"],
        "profit_cushion_units": statistics["profit_cushion_units"],
        "tier": decision["effective_tier"],
        "lot_multiplier": decision["lot_multiplier"],
        "promotion_authorized": decision["promotion_authorized"],
        "promotion_blockers": decision["promotion_blockers"],
        "rollback_reasons": decision["rollback_reasons"],
    }


def build_payload(records: list[dict], *, generated_at: datetime, history_window: dict) -> dict:
    all_nets = [float(record["net"]) for record in records]
    agent_nets = [
        float(record["net"])
        for record in records
        if _is_agent_magic(int(record["magic"]))
    ]
    account_guard = evaluate_account_guard(all_nets)
    multiplier_cap = float(account_guard["multiplier_cap"])
    by_magic: dict[int, list[float]] = defaultdict(list)
    for record in records:
        by_magic[int(record["magic"])].append(float(record["net"]))

    recommendations: dict[str, dict] = {}
    for magic, name in STRUCTURAL_MAGICS.items():
        decision = evaluate_profit_funded_scaling(
            by_magic.get(magic, []),
            multiplier_cap=multiplier_cap,
        )
        recommendations[str(magic)] = {
            "name": name,
            **stats(by_magic.get(magic, []), multiplier_cap=multiplier_cap),
            "policy": decision,
        }

    return {
        "schema": SCHEMA,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "as_of": generated_at.astimezone(timezone.utc).isoformat(),
        "max_age_hours": 26,
        "source": {
            "history": history_window,
            "closed_account_trades": len(records),
            "closed_agent_trades": len(agent_nets),
            "agent_magic_policy": "structural 88001-88009 plus Codex 26060000-26069999",
        },
        "account_guard": account_guard,
        "agent_portfolio": {
            "decision": evaluate_profit_funded_scaling(
                agent_nets,
                multiplier_cap=multiplier_cap,
            ),
            "historical_no_lookahead_simulation": simulate_profit_funded_path(agent_nets),
            "moving_block_bootstrap": bootstrap_scaling_stress(agent_nets),
        },
        "recommendations": recommendations,
        "live_contract": {
            "default_on_missing_or_stale": "1x",
            "manual_losses_can_reduce_cap": True,
            "manual_profits_can_promote_strategy": False,
            "floating_pnl_can_promote_strategy": False,
            "broker_credit_can_promote_strategy": False,
            "maximum_multiplier": 3.0,
            "martingale": False,
        },
    }


def main() -> None:
    if not mt5.initialize():
        print(f"mt5.initialize failed: {mt5.last_error()}", file=sys.stderr)
        raise SystemExit(1)
    try:
        host_utc = datetime.now(tz=timezone.utc)
        _, clock = coherent_feed_clock_from_mt5(mt5, REFERENCE_SYMBOLS, host_utc=host_utc)
        window = history_window_from_feed_clock(clock, lookback=clock.feed_time - HISTORY_START)
        deals = list(mt5.history_deals_get(window.start, window.end) or [])
        records = _closed_trade_records(deals)
        payload = build_payload(
            records,
            generated_at=host_utc,
            history_window=window.as_dict(),
        )
        write_json_atomic(OUT_FILE, payload)

        guard = payload["account_guard"]
        portfolio = payload["agent_portfolio"]["decision"]
        print(f"PROFIT-FUNDED SIZING @ {payload['generated_at']}")
        print(
            f"account guard={guard['status']} cap={guard['multiplier_cap']:.2f}x; "
            f"agent trades={portfolio['statistics']['closed_trades']} "
            f"net=${portfolio['statistics']['net_profit_usd']:+.2f} "
            f"t={portfolio['statistics']['t_stat']:+.2f} "
            f"effective={portfolio['lot_multiplier']:.2f}x"
        )
        for magic, recommendation in payload["recommendations"].items():
            print(
                f"  {recommendation['name']:<13} magic={magic} "
                f"n={recommendation['n']:>3} net=${recommendation['net_profit']:+7.2f} "
                f"t={recommendation['t_stat']:+5.2f} "
                f"tier={recommendation['tier']} x={recommendation['lot_multiplier']:.2f}"
            )
        print(f"Wrote {OUT_FILE}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
