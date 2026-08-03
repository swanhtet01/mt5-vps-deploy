from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mt5_agent.agent import TradingAgent
from mt5_agent.audit import ENTRY_MODES, SignalAuditEngine
from mt5_agent.backtest import BacktestEngine, configured_spread_points, cost_model_for
from mt5_agent.broker import MT5Broker
from mt5_agent.calendar_edges import build_calendar_edge_events, load_calendar_edges
from mt5_agent.config import load_config
from mt5_agent.deals import (
    closed_position_net_profits,
    closed_position_net_profits_by_symbol,
    summarize_closed_deals,
)
from mt5_agent.evidence import build_evidence_report
from mt5_agent.health import build_health_report
from mt5_agent.hypothetical import evaluate_blocked_hypotheticals, load_jsonl_records
from mt5_agent.milestone import build_milestone_report, load_or_initialize_state
from mt5_agent.models import AccountState, Signal
from mt5_agent.optimizer import (
    default_grid,
    entry_quality_grid,
    expanded_grid,
    management_grid,
    mean_reversion_grid,
    optimize_strategy,
    pullback_reclaim_grid,
    quick_grid,
    recovery_grid,
    regime_grid,
    scalp_grid,
    squeeze_breakout_grid,
    spread_expansion_grid,
    turnover_grid,
    volatility_continuation_grid,
    walk_forward_validate,
)
from mt5_agent.chamber import time_chamber_eval
from mt5_agent.preflight import build_live_preflight
from mt5_agent.risk import RiskManager
from mt5_agent.research_pipeline import build_research_pipeline_report
from mt5_agent.scaling import build_profit_scale_report
from mt5_agent.state import DailyRiskState, PositionRiskState
from mt5_agent.storage import (
    append_jsonl,
    build_run_summary,
    latest_by_symbol,
    summarize_jsonl,
    summarize_signal_metrics,
)
from mt5_agent.strategy import TrendBreakoutStrategy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MT5 trading agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("check", "doctor", "once", "loop"):
        command = subparsers.add_parser(name)
        command.add_argument("--config", type=Path, default=Path("config.toml"))
        command.add_argument("--dry-run", action="store_true", help="never send real orders")
        if name in {"once", "loop"}:
            command.add_argument("--log-file", type=Path, default=Path("logs/paper-events.jsonl"))
            command.add_argument("--confirm-live", action="store_true", help="required for live real-money orders")
            command.add_argument(
                "--research",
                action="store_true",
                help="dry-run only: keep scanning even when daily live equity stop is tripped",
            )

    symbols = subparsers.add_parser("symbols")
    symbols.add_argument("--query", default="", help="filter by name or description")
    symbols.add_argument("--limit", type=int, default=100)

    rank_symbols = subparsers.add_parser("rank-symbols")
    rank_symbols.add_argument("--config", type=Path, default=Path("config.live-user-risk.toml"))
    rank_symbols.add_argument(
        "--query",
        action="append",
        help="symbol/path/description filter; repeat for multiple groups",
    )
    rank_symbols.add_argument("--limit", type=int, default=120, help="max discovered symbols before profiling")
    rank_symbols.add_argument("--top", type=int, default=12, help="max profiled symbols to return")
    rank_symbols.add_argument("--backtest-top", type=int, default=6, help="top profiled symbols to backtest")
    rank_symbols.add_argument("--profile-bars", type=int, default=300)
    rank_symbols.add_argument("--backtest-bars", type=int, default=1200)
    rank_symbols.add_argument("--output", type=Path)

    report = subparsers.add_parser("report")
    report.add_argument("--log-file", type=Path, default=Path("logs/paper-events.jsonl"))

    status = subparsers.add_parser("status")
    status.add_argument("--log-file", type=Path, default=Path("logs/live-watch-events.jsonl"))

    metrics_report = subparsers.add_parser("metrics-report")
    metrics_report.add_argument("--log-file", type=Path, default=Path("logs/autonomous-live-btc-us500-scalp-events.jsonl"))

    blocked_outcomes = subparsers.add_parser("blocked-outcomes")
    blocked_outcomes.add_argument("--config", type=Path, default=Path("config.live-btc-us500-scalp.toml"))
    blocked_outcomes.add_argument(
        "--log-file",
        type=Path,
        default=Path("logs/autonomous-live-btc-us500-scalp-events.jsonl"),
    )
    blocked_outcomes.add_argument("--bars", type=int, default=3000)
    blocked_outcomes.add_argument("--lookahead-bars", type=int, default=60)
    blocked_outcomes.add_argument("--min-promotion-resolved", type=int, default=20)
    blocked_outcomes.add_argument("--min-promotion-net-r", type=float, default=5.0)
    blocked_outcomes.add_argument("--min-promotion-win-rate-pct", type=float, default=60.0)
    blocked_outcomes.add_argument("--output", type=Path)

    deals = subparsers.add_parser("deals")
    deals.add_argument("--config", type=Path, default=Path("config.live-btc-us500-scalp.toml"))
    deals.add_argument("--days", type=int, default=90)

    health = subparsers.add_parser("health")
    health.add_argument("--config", type=Path, default=Path("config.live-btc-us500-scalp.toml"))
    health.add_argument("--log-file", type=Path, default=Path("logs/autonomous-live-btc-us500-scalp-events.jsonl"))
    health.add_argument("--state-file", type=Path, default=Path("state/daily-risk.json"))
    health.add_argument("--deals-days", type=int, default=1)
    health.add_argument("--target-balance", type=float, default=100000.0)

    calendar_edges = subparsers.add_parser("calendar-edges")
    calendar_edges.add_argument("--edge-file", type=Path, default=Path("logs/research/multi-instrument-edges.json"))
    calendar_edges.add_argument("--log-file", type=Path, default=Path("logs/calendar-edge-paper-events.jsonl"))
    calendar_edges.add_argument("--symbol", action="append", help="optional symbol filter; repeatable")
    calendar_edges.add_argument("--signals-only", action="store_true", help="skip no-signal heartbeat records")

    reset_daily_risk = subparsers.add_parser("reset-daily-risk")
    reset_daily_risk.add_argument("--config", type=Path, default=Path("config.live-btc-us500-scalp.toml"))
    reset_daily_risk.add_argument("--state-file", type=Path, default=Path("state/daily-risk.json"))
    reset_daily_risk.add_argument("--confirm-live", action="store_true")
    reset_daily_risk.add_argument("--force", action="store_true", help="allow reset while positions are open")

    scale_report = subparsers.add_parser("scale-report")
    scale_report.add_argument("--config", type=Path, default=Path("config.live-btc-us500-scalp.toml"))
    scale_report.add_argument("--log-file", type=Path, default=Path("logs/autonomous-live-btc-us500-scalp-events.jsonl"))
    scale_report.add_argument("--target-balance", type=float, default=100000.0)
    scale_report.add_argument("--min-live-orders-for-scale", type=int)
    scale_report.add_argument("--output", type=Path)

    evidence = subparsers.add_parser("evidence-report")
    evidence.add_argument("--root", type=Path, default=Path("."))
    evidence.add_argument("--output", type=Path)

    research_report = subparsers.add_parser("research-report")
    research_report.add_argument("--root", type=Path, default=Path("."))
    research_report.add_argument("--output", type=Path, default=Path("logs/research-pipeline-report.json"))
    research_report.add_argument("--target-balance", type=float, default=100000.0)

    positions = subparsers.add_parser("positions")
    positions.add_argument("--config", type=Path, default=Path("config.toml"))

    milestone = subparsers.add_parser("milestone")
    milestone.add_argument("--config", type=Path, default=Path("config.live-btc-scalp.toml"))
    milestone.add_argument("--state-file", type=Path, default=Path("state/local-live-milestone.json"))
    milestone.add_argument("--target-profit", type=float, default=100.0)
    milestone.add_argument("--reset", action="store_true", help="reset baseline to current account balance")

    preflight = subparsers.add_parser("preflight-live")
    preflight.add_argument("--config", type=Path, default=Path("config.live-micro.toml"))

    scan = subparsers.add_parser("scan")
    scan.add_argument("--config", type=Path, default=Path("config.toml"))

    backtest = subparsers.add_parser("backtest")
    backtest.add_argument("--config", type=Path, default=Path("config.live-micro.toml"))
    backtest.add_argument("--symbol", action="append", help="symbol to backtest; repeat for multiple symbols")
    backtest.add_argument("--bars", type=int, default=2000)
    backtest.add_argument("--initial-equity", type=float)
    backtest.add_argument("--spread-points", type=float, help="fixed spread assumption for every tested symbol")
    backtest.add_argument("--output", type=Path)

    optimize = subparsers.add_parser("optimize")
    optimize.add_argument("--config", type=Path, default=Path("config.live-micro.toml"))
    optimize.add_argument("--symbol", action="append", help="symbol to optimize; repeat for multiple symbols")
    optimize.add_argument("--bars", type=int, default=2000)
    optimize.add_argument("--initial-equity", type=float)
    optimize.add_argument("--spread-points", type=float, help="fixed spread assumption for every tested symbol")
    optimize.add_argument("--min-trades", type=int, default=3)
    optimize.add_argument("--limit", type=int, default=5)
    optimize.add_argument("--expanded-grid", action="store_true")
    optimize.add_argument("--turnover-grid", action="store_true")
    optimize.add_argument("--entry-quality-grid", action="store_true")
    optimize.add_argument("--management-grid", action="store_true")
    optimize.add_argument("--regime-grid", action="store_true")
    optimize.add_argument("--recovery-grid", action="store_true")
    optimize.add_argument("--mean-reversion-grid", action="store_true")
    optimize.add_argument("--squeeze-breakout-grid", action="store_true")
    optimize.add_argument("--pullback-reclaim-grid", action="store_true")
    optimize.add_argument("--volatility-continuation-grid", action="store_true")
    optimize.add_argument("--scalp-grid", action="store_true")
    optimize.add_argument("--spread-expansion-grid", action="store_true")
    optimize.add_argument("--output", type=Path)

    walk_forward = subparsers.add_parser("walk-forward")
    walk_forward.add_argument("--config", type=Path, default=Path("config.live-micro.toml"))
    walk_forward.add_argument("--symbol", action="append", help="symbol to validate; repeat for multiple symbols")
    walk_forward.add_argument("--bars", type=int, default=2000)
    walk_forward.add_argument("--initial-equity", type=float)
    walk_forward.add_argument("--spread-points", type=float, help="fixed spread assumption for every tested symbol")
    walk_forward.add_argument("--train-ratio", type=float, default=0.65)
    walk_forward.add_argument("--min-train-trades", type=int, default=1)
    walk_forward.add_argument("--limit", type=int, default=3)
    walk_forward.add_argument("--folds", type=int, default=1)
    walk_forward.add_argument("--full-grid", action="store_true")
    walk_forward.add_argument("--expanded-grid", action="store_true")
    walk_forward.add_argument("--turnover-grid", action="store_true")
    walk_forward.add_argument("--entry-quality-grid", action="store_true")
    walk_forward.add_argument("--management-grid", action="store_true")
    walk_forward.add_argument("--regime-grid", action="store_true")
    walk_forward.add_argument("--recovery-grid", action="store_true")
    walk_forward.add_argument("--mean-reversion-grid", action="store_true")
    walk_forward.add_argument("--squeeze-breakout-grid", action="store_true")
    walk_forward.add_argument("--pullback-reclaim-grid", action="store_true")
    walk_forward.add_argument("--volatility-continuation-grid", action="store_true")
    walk_forward.add_argument("--scalp-grid", action="store_true")
    walk_forward.add_argument("--spread-expansion-grid", action="store_true")
    walk_forward.add_argument("--allowed-utc-hours", help="comma-separated UTC hours to test as an allowed session window")
    walk_forward.add_argument("--blocked-utc-hours", help="comma-separated UTC hours to test as a blocked session window")
    walk_forward.add_argument("--output", type=Path)

    chamber = subparsers.add_parser(
        "time-chamber",
        help="honest expanding-window, cost-aware, no-leakage out-of-sample evaluation with a PASS/FAIL verdict",
    )
    chamber.add_argument("--config", type=Path, default=Path("config.live-micro.toml"))
    chamber.add_argument("--symbol", action="append", help="symbol to evaluate; repeat for multiple symbols")
    chamber.add_argument("--bars", type=int, default=5000)
    chamber.add_argument("--initial-equity", type=float)
    chamber.add_argument("--spread-points", type=float, help="fixed spread assumption for every tested symbol")
    chamber.add_argument("--folds", type=int, default=4)
    chamber.add_argument("--oos-fraction", type=float, default=0.4, help="trailing fraction of bars reserved for out-of-sample folds")
    chamber.add_argument("--optimizer-min-trades", type=int, default=20)
    chamber.add_argument("--min-oos-trades", type=int, default=30, help="pooled out-of-sample trades required for a verdict")
    chamber.add_argument("--min-oos-profit-factor", type=float, default=1.2)
    chamber.add_argument("--min-folds-profitable-pct", type=float, default=0.5)
    chamber.add_argument("--full-grid", action="store_true")
    chamber.add_argument("--expanded-grid", action="store_true")
    chamber.add_argument("--turnover-grid", action="store_true")
    chamber.add_argument("--entry-quality-grid", action="store_true")
    chamber.add_argument("--management-grid", action="store_true")
    chamber.add_argument("--regime-grid", action="store_true")
    chamber.add_argument("--recovery-grid", action="store_true")
    chamber.add_argument("--mean-reversion-grid", action="store_true")
    chamber.add_argument("--squeeze-breakout-grid", action="store_true")
    chamber.add_argument("--pullback-reclaim-grid", action="store_true")
    chamber.add_argument("--volatility-continuation-grid", action="store_true")
    chamber.add_argument("--scalp-grid", action="store_true")
    chamber.add_argument("--spread-expansion-grid", action="store_true")
    chamber.add_argument("--output", type=Path)

    audit = subparsers.add_parser("audit-signals")
    audit.add_argument("--config", type=Path, default=Path("config.live-micro.toml"))
    audit.add_argument("--symbol", action="append", help="symbol to audit; repeat for multiple symbols")
    audit.add_argument("--bars", type=int, default=2000)
    audit.add_argument("--initial-equity", type=float)
    audit.add_argument("--spread-points", type=float, help="fixed spread assumption for every tested symbol")
    audit.add_argument(
        "--shadow-entry-mode",
        action="append",
        choices=ENTRY_MODES,
        help="read-only comparison entry mode; repeat to compare multiple strategy families",
    )
    audit.add_argument("--output", type=Path)

    manual_order = subparsers.add_parser("manual-order")
    manual_order.add_argument("--config", type=Path, default=Path("config.live-micro.toml"))
    manual_order.add_argument("--symbol", required=True)
    manual_order.add_argument("--side", choices=["buy", "sell"], required=True)
    manual_order.add_argument("--volume", type=float, default=0.01)
    manual_order.add_argument("--stop-points", type=float, required=True)
    manual_order.add_argument("--take-profit-points", type=float, required=True)
    manual_order.add_argument("--confirm-live", action="store_true", help="required for live real-money orders")
    manual_order.add_argument("--dry-run", action="store_true", help="build and check order without sending")
    manual_order.add_argument("--log-file", type=Path, default=Path("logs/manual-live-events.jsonl"))

    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "symbols":
        broker = MT5Broker()
        try:
            print(json.dumps(broker.discover_symbols(args.query, args.limit), indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "report":
        print(json.dumps(summarize_jsonl(args.log_file), indent=2, default=str))
        return

    if args.command == "status":
        print(json.dumps(latest_by_symbol(args.log_file), indent=2, default=str))
        return

    if args.command == "metrics-report":
        print(json.dumps(summarize_signal_metrics(args.log_file), indent=2, default=str))
        return

    if args.command == "evidence-report":
        payload = build_evidence_report(args.root)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(json.dumps(payload, indent=2, default=str))
        return

    if args.command == "research-report":
        evidence_payload = build_evidence_report(args.root)
        payload = build_research_pipeline_report(
            evidence_payload,
            target_balance=float(args.target_balance),
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(json.dumps(payload, indent=2, default=str))
        return

    if args.command == "calendar-edges":
        started = datetime.now(tz=timezone.utc)
        events = build_calendar_edge_events(
            load_calendar_edges(args.edge_file),
            started,
            set(args.symbol) if args.symbol else None,
            emit_no_signal=not args.signals_only,
        )
        finished = datetime.now(tz=timezone.utc)
        if not events:
            print(json.dumps([], indent=2, default=str))
            return
        records = events + [build_run_summary(events, started, finished)]
        append_jsonl(args.log_file, records, "calendar-edges")
        print(json.dumps(records, indent=2, default=str))
        return

    config = load_config(args.config)

    if args.command == "health":
        broker = MT5Broker()
        try:
            diagnostics = broker.diagnostics()
            deals = broker.history_deals(magic_number=config.magic_number, days=args.deals_days)
            account_scale_deals = broker.history_deals()
            scale_deals = [
                deal for deal in account_scale_deals if deal.magic == config.magic_number
            ]
            recent_scale_days = max(2, int(config.profit_scaling.symbol_quality_window_days))
            recent_scale_deals = broker.history_deals(magic_number=config.magic_number, days=recent_scale_days)
            news_path = Path(config.news.state_path)
            news_state = {}
            if news_path.exists():
                try:
                    news_state = json.loads(news_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, ValueError, OSError):
                    news_state = {}
            account_payload = diagnostics["account"] or {}
            account_state = AccountState(
                balance=float(account_payload.get("balance") or 0.0),
                equity=float(account_payload.get("equity") or 0.0),
                free_margin=float(account_payload.get("margin_free") or 0.0),
                open_risk=float(diagnostics.get("open_risk") or 0.0),
            )
            scale_report = build_profit_scale_report(
                config=config,
                account=account_state,
                log_summary=summarize_jsonl(args.log_file),
                deal_summary=summarize_closed_deals(scale_deals, magic_number=config.magic_number),
                recent_deal_summary=summarize_closed_deals(
                    recent_scale_deals,
                    magic_number=config.magic_number,
                ),
                recent_deal_window_days=recent_scale_days,
                target_balance=float(args.target_balance),
                deal_nets=closed_position_net_profits(scale_deals, config.magic_number),
                deal_nets_by_symbol=closed_position_net_profits_by_symbol(
                    scale_deals,
                    config.magic_number,
                ),
                account_deal_nets=closed_position_net_profits(account_scale_deals, None),
                account_deal_window_days=90,
            )
            payload = build_health_report(
                account=account_payload,
                open_positions=diagnostics["open_positions"],
                daily_state=DailyRiskState(args.state_file)._load(),
                max_daily_loss_pct=config.risk.max_daily_loss_pct,
                deal_summary=summarize_closed_deals(deals, magic_number=config.magic_number),
                log_summary=summarize_jsonl(args.log_file),
                news_state=news_state,
                scale_report=scale_report,
                daily_profit_lock_trigger_usd=config.risk.daily_profit_lock_trigger_usd,
                daily_profit_lock_giveback_pct=config.risk.daily_profit_lock_giveback_pct,
                expected_poll_seconds=config.poll_seconds,
            )
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "reset-daily-risk":
        if config.mode == "live" and not args.confirm_live:
            raise SystemExit("reset-daily-risk requires --confirm-live for live configs")
        broker = MT5Broker()
        try:
            diagnostics = broker.diagnostics()
            open_positions = diagnostics.get("open_positions") or []
            if open_positions and not args.force:
                raise SystemExit("refusing to reset daily risk baseline while positions are open; pass --force to override")
            account = diagnostics.get("account") or {}
            equity = float(account.get("equity") or 0.0)
            status = DailyRiskState(args.state_file).reset(equity, config.risk.max_daily_loss_pct)
            print(
                json.dumps(
                    {
                        "state_file": str(args.state_file),
                        "status": status.__dict__,
                        "open_positions": len(open_positions),
                        "account": {
                            "balance": account.get("balance"),
                            "equity": account.get("equity"),
                            "profit": account.get("profit"),
                            "server": account.get("server"),
                            "currency": account.get("currency"),
                            "login_last4": account.get("login_last4"),
                        },
                    },
                    indent=2,
                    default=str,
                )
            )
        finally:
            broker.shutdown()
        return

    if args.command == "deals":
        broker = MT5Broker()
        try:
            deals = broker.history_deals(magic_number=config.magic_number, days=args.days)
            payload = summarize_closed_deals(deals, magic_number=config.magic_number)
            payload["magic_number"] = config.magic_number
            payload["days"] = args.days
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "scale-report":
        broker = MT5Broker()
        try:
            account = broker.account()
            account_scale_deals = broker.history_deals()
            scale_deals = [
                deal for deal in account_scale_deals if deal.magic == config.magic_number
            ]
            scale_deal_summary = summarize_closed_deals(scale_deals, magic_number=config.magic_number)
            recent_deal_window_days = 2
            recent_deal_summary = summarize_closed_deals(
                broker.history_deals(magic_number=config.magic_number, days=recent_deal_window_days),
                magic_number=config.magic_number,
            )
            payload = build_profit_scale_report(
                config=config,
                account=account,
                log_summary=summarize_jsonl(args.log_file),
                deal_summary=scale_deal_summary,
                recent_deal_summary=recent_deal_summary,
                recent_deal_window_days=recent_deal_window_days,
                target_balance=float(args.target_balance),
                min_live_orders_for_scale=(
                    None if args.min_live_orders_for_scale is None else int(args.min_live_orders_for_scale)
                ),
                deal_nets=closed_position_net_profits(scale_deals, config.magic_number),
                deal_nets_by_symbol=closed_position_net_profits_by_symbol(
                    scale_deals,
                    config.magic_number,
                ),
                account_deal_nets=closed_position_net_profits(account_scale_deals, None),
                account_deal_window_days=90,
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "blocked-outcomes":
        broker = MT5Broker()
        try:
            payload = run_blocked_outcomes(config, broker, args)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "check":
        print(json.dumps({"ok": True, "mode": config.mode, "symbols": config.symbols}, indent=2))
        return

    if args.command == "positions":
        broker = MT5Broker()
        try:
            diagnostics = broker.diagnostics()
            print(
                json.dumps(
                    {
                        "account": diagnostics["account"],
                        "open_risk": diagnostics["open_risk"],
                        "open_positions": diagnostics["open_positions"],
                    },
                    indent=2,
                    default=str,
                )
            )
        finally:
            broker.shutdown()
        return

    if args.command == "milestone":
        broker = MT5Broker()
        try:
            diagnostics = broker.diagnostics()
            account = diagnostics["account"] or {}
            current_balance = float(account.get("balance") or 0.0)
            target_profit = float(args.target_profit)
            state = load_or_initialize_state(args.state_file, current_balance, target_profit, args.reset)
            report = build_milestone_report(
                baseline_balance=float(state["baseline_balance"]),
                current_balance=current_balance,
                current_equity=float(account.get("equity") or current_balance),
                floating_profit=float(account.get("profit") or 0.0),
                target_profit=float(state.get("target_profit") or target_profit),
            )
            report["state_file"] = str(args.state_file)
            report["baseline_created_at"] = state.get("created_at")
            print(json.dumps(report, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "preflight-live":
        broker = MT5Broker()
        try:
            print(json.dumps(build_live_preflight(config, broker.diagnostics()), indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "scan":
        broker = MT5Broker()
        try:
            print(json.dumps(scan_signals(config, broker), indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "rank-symbols":
        broker = MT5Broker()
        try:
            payload = rank_symbol_candidates(config, broker, args)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "backtest":
        broker = MT5Broker()
        try:
            payload = run_backtest(config, broker, args)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "optimize":
        broker = MT5Broker()
        try:
            payload = run_optimize(config, broker, args)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "walk-forward":
        broker = MT5Broker()
        try:
            payload = run_walk_forward(config, broker, args)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "time-chamber":
        broker = MT5Broker()
        try:
            payload = run_time_chamber(config, broker, args)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "audit-signals":
        broker = MT5Broker()
        try:
            payload = run_signal_audit(config, broker, args)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(json.dumps(payload, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    if args.command == "manual-order":
        dry_run = bool(args.dry_run or not config.live)
        ensure_live_permission(config, args, dry_run)
        broker = MT5Broker()
        try:
            diagnostics = broker.diagnostics()
            preflight = build_live_preflight(config, diagnostics)
            if not preflight["ok"]:
                raise SystemExit(f"preflight failed: {preflight['issues']}")
            if not dry_run and not (diagnostics.get("terminal") or {}).get("trade_allowed"):
                raise SystemExit("MT5 Algo Trading toolbar is off; enable Algo Trading before real order_send")
            record = build_manual_order(config, broker, args, dry_run)
            append_jsonl(args.log_file, [record], "manual-order")
            print(json.dumps(record, indent=2, default=str))
        finally:
            broker.shutdown()
        return

    dry_run = bool(args.dry_run or not config.live)
    if args.command in {"once", "loop"}:
        if getattr(args, "research", False) and not dry_run:
            raise SystemExit("--research is allowed only with --dry-run or paper mode")
        ensure_live_permission(config, args, dry_run)
    broker = MT5Broker()
    scale_evidence = _scale_evidence_from_log_and_deals(config, broker, args) if args.command in {"once", "loop"} else {}
    agent = TradingAgent(config, broker, position_risk_state=PositionRiskState(), **scale_evidence)
    try:
        if args.command == "doctor":
            checks = {
                "ok": True,
                "mode": config.mode,
                "diagnostics": broker.diagnostics(),
                "symbols": [],
            }
            for symbol in config.symbols:
                strategy_config = config.strategy_for(symbol)
                symbol_check = {"symbol": symbol, "ok": True}
                try:
                    symbol_check["rules"] = broker.rules(symbol).__dict__
                    symbol_check["tick"] = broker.tick(symbol).__dict__
                    symbol_check["bars"] = len(
                        broker.bars(symbol, strategy_config.timeframe, min(strategy_config.bars, 20))
                    )
                except Exception as exc:
                    checks["ok"] = False
                    symbol_check["ok"] = False
                    symbol_check["error"] = str(exc)
                checks["symbols"].append(symbol_check)
            print(json.dumps(checks, indent=2, default=str))
            return

        if args.command == "once":
            started_at = datetime.now(tz=timezone.utc)
            perf_start = time.perf_counter()
            records = agent.run_once(dry_run=dry_run, enforce_daily_stop=not getattr(args, "research", False))
            finished_at = datetime.now(tz=timezone.utc)
            summary = build_run_summary(records, started_at, finished_at)
            monotonic_duration = round(time.perf_counter() - perf_start, 3)
            summary["duration_seconds"] = monotonic_duration
            summary["monotonic_duration_seconds"] = monotonic_duration
            payload = [*records, summary]
            append_jsonl(args.log_file, payload, "once")
            print(json.dumps(payload, indent=2, default=str))
            return
        while True:
            started_at = datetime.now(tz=timezone.utc)
            perf_start = time.perf_counter()
            records = agent.run_once(dry_run=dry_run, enforce_daily_stop=not getattr(args, "research", False))
            finished_at = datetime.now(tz=timezone.utc)
            summary = build_run_summary(records, started_at, finished_at)
            monotonic_duration = round(time.perf_counter() - perf_start, 3)
            summary["duration_seconds"] = monotonic_duration
            summary["monotonic_duration_seconds"] = monotonic_duration
            payload = [*records, summary]
            append_jsonl(args.log_file, payload, "loop")
            print(json.dumps(payload, indent=2, default=str), flush=True)
            time.sleep(config.poll_seconds)
    finally:
        broker.shutdown()


def ensure_live_permission(config, args: argparse.Namespace, dry_run: bool) -> None:
    if not config.live or dry_run:
        return
    if config.live_safety.require_confirm_flag and not getattr(args, "confirm_live", False):
        raise SystemExit("live mode requires --confirm-live")
    required_env = config.live_safety.require_env
    if required_env and os.getenv(required_env) != "yes":
        raise SystemExit(f"live mode requires environment variable {required_env}=yes")


def _scale_live_orders_from_log(args: argparse.Namespace) -> int | None:
    log_file = getattr(args, "log_file", None)
    if log_file is None:
        return None
    return int(summarize_jsonl(log_file).get("live_orders") or 0)


def _scale_evidence_from_log_and_deals(config, broker, args: argparse.Namespace) -> dict[str, Any]:
    log_live_orders = _scale_live_orders_from_log(args) or 0
    account_deals = broker.history_deals()
    all_deals = [deal for deal in account_deals if deal.magic == config.magic_number]
    deal_summary = summarize_closed_deals(
        all_deals,
        magic_number=config.magic_number,
    )
    recent_deal_summary = summarize_closed_deals(
        broker.history_deals(magic_number=config.magic_number, days=2),
        magic_number=config.magic_number,
    )
    closed_deals = int(deal_summary.get("closed_deals") or 0)
    by_symbol = deal_summary.get("by_symbol") or {}
    recent_by_symbol = recent_deal_summary.get("by_symbol") or {}
    scale_deal_nets = closed_position_net_profits(all_deals, config.magic_number)
    return {
        "scale_live_orders": max(log_live_orders, closed_deals),
        "scale_closed_deals": closed_deals,
        "scale_deal_net_profit": deal_summary.get("net_profit"),
        "scale_recent_losing_streak": deal_summary.get("recent_losing_streak"),
        "scale_deal_nets": scale_deal_nets,
        "scale_symbol_deal_nets": closed_position_net_profits_by_symbol(
            all_deals,
            config.magic_number,
        ),
        "scale_account_deal_nets": closed_position_net_profits(account_deals, None),
        "scale_symbol_closed_deals": {
            symbol: int(values.get("closed_deals") or 0)
            for symbol, values in by_symbol.items()
        },
        "scale_symbol_deal_net_profit": {
            symbol: float(values.get("net_profit") or 0.0)
            for symbol, values in by_symbol.items()
        },
        "scale_symbol_recent_losing_streak": {
            symbol: int(values.get("recent_losing_streak") or 0)
            for symbol, values in by_symbol.items()
        },
        "recent_symbol_closed_deals": {
            symbol: int(values.get("closed_deals") or 0)
            for symbol, values in recent_by_symbol.items()
        },
        "recent_symbol_deal_net_profit": {
            symbol: float(values.get("net_profit") or 0.0)
            for symbol, values in recent_by_symbol.items()
        },
        "recent_symbol_recent_losing_streak": {
            symbol: int(values.get("recent_losing_streak") or 0)
            for symbol, values in recent_by_symbol.items()
        },
    }


def build_manual_order(config, broker: MT5Broker, args: argparse.Namespace, dry_run: bool) -> dict:
    symbol = args.symbol
    if config.live_safety.allowed_symbols and symbol not in config.live_safety.allowed_symbols:
        raise SystemExit(f"{symbol} is not in live_safety.allowed_symbols")

    rules = broker.rules(symbol)
    tick = broker.tick(symbol)
    account = broker.account()
    manager = RiskManager(config, **_scale_evidence_from_log_and_deals(config, broker, args))
    if args.volume > manager.live_volume_cap(symbol):
        raise SystemExit(f"volume {args.volume} exceeds live cap {manager.live_volume_cap(symbol)}")
    if args.volume < rules.min_lot:
        raise SystemExit(f"volume {args.volume} below broker min lot {rules.min_lot}")
    if args.stop_points <= 0 or args.take_profit_points <= 0:
        raise SystemExit("stop-points and take-profit-points must be positive")

    stop_distance = args.stop_points * rules.point
    take_profit_distance = args.take_profit_points * rules.point
    planned_risk = manager.planned_order_risk(rules, args.volume, stop_distance)
    max_risk = manager.max_new_trade_risk(account, symbol)
    if planned_risk > max_risk:
        raise SystemExit(f"planned risk {planned_risk:.2f} exceeds allowed new-trade risk {max_risk:.2f}")
    signal = Signal(
        symbol=symbol,
        side=args.side,
        confidence=1.0,
        reason="manual one-shot micro test",
        stop_distance=stop_distance,
        take_profit_distance=take_profit_distance,
    )
    order = manager.build_order(tick, signal, args.volume)
    result = broker.send_order(order, config.magic_number, dry_run)
    return {
        "event": "manual_order_sent" if config.live and not dry_run else "manual_order_dry_run",
        "symbol": symbol,
        "side": args.side,
        "volume": args.volume,
        "stop_points": args.stop_points,
        "take_profit_points": args.take_profit_points,
        "price": order.price,
        "stop_loss": order.stop_loss,
        "take_profit": order.take_profit,
        "planned_risk": planned_risk,
        "max_allowed_risk": max_risk,
        "broker_result": result,
    }


def scan_signals(config, broker: MT5Broker) -> list[dict]:
    risk = RiskManager(config)
    account = broker.account()
    rows = []
    for symbol in config.symbols:
        try:
            strategy_config = config.strategy_for(symbol)
            strategy = TrendBreakoutStrategy(strategy_config)
            bars = broker.bars(symbol, strategy_config.timeframe, strategy_config.bars)
            regime_bars = broker.bars(symbol, strategy_config.regime_timeframe, strategy_config.regime_bars)
            signal = strategy.evaluate(symbol, bars, regime_bars)
            tick = broker.tick(symbol)
            rules = broker.rules(symbol)
            decision = risk.assess(account, rules, tick, signal, broker.open_positions_count(symbol))
            allowlist_diagnostic = _allowlist_risk_diagnostic(
                config,
                account,
                rules,
                tick,
                signal,
                broker.open_positions_count(symbol),
            )
            rows.append(
                {
                    "symbol": symbol,
                    "signal": signal.side,
                    "confidence": signal.confidence,
                    "strategy_reason": signal.reason,
                    "risk_allowed": decision.allowed,
                    "risk_reason": decision.reason,
                    "suggested_volume": decision.volume,
                    "spread_points": tick.spread_points,
                    "stop_distance": signal.stop_distance,
                    "take_profit_distance": signal.take_profit_distance,
                    **allowlist_diagnostic,
                }
            )
        except Exception as exc:
            rows.append({"symbol": symbol, "error": str(exc)})
    return rows


def _allowlist_risk_diagnostic(
    config,
    account,
    rules,
    tick,
    signal,
    open_positions_count: int,
) -> dict[str, Any]:
    allowed_symbols = config.live_safety.allowed_symbols
    if signal.side == "flat" or not allowed_symbols or signal.symbol in allowed_symbols:
        return {}
    diagnostic_config = replace(
        config,
        live_safety=replace(config.live_safety, allowed_symbols=[signal.symbol]),
    )
    diagnostic = RiskManager(diagnostic_config).assess(
        account,
        rules,
        tick,
        signal,
        open_positions_count,
    )
    return {
        "allowlist_blocked": True,
        "risk_allowed_if_symbol_allowed": diagnostic.allowed,
        "risk_reason_if_symbol_allowed": diagnostic.reason,
        "suggested_volume_if_symbol_allowed": diagnostic.volume,
    }


def rank_symbol_candidates(config, broker: MT5Broker, args: argparse.Namespace) -> dict[str, Any]:
    queries = args.query or [
        "Cryptocurrencies\\Standard",
        "Thematic Indices",
        "Forex\\Standard\\Majors",
        "Derivatives\\Cash",
        "Derivatives\\Spot Metals",
    ]
    discovered: dict[str, dict[str, Any]] = {}
    per_query: dict[str, int] = {}
    for query in queries:
        matches = broker.discover_symbols(query, args.limit)
        per_query[query] = len(matches)
        for match in matches:
            discovered.setdefault(match["name"], match)

    profiles: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    profile_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in sorted(discovered):
        try:
            profile = broker.symbol_profile(symbol, config.strategy.timeframe, args.profile_bars)
            profile.update(_candidate_metrics(profile))
            profile["score"] = _candidate_score(profile)
            issue = _candidate_issue(profile, args.profile_bars)
            profile_by_symbol[symbol] = profile
            if issue:
                profile["issue"] = issue
                rejected.append(profile)
            else:
                profiles.append(profile)
        except Exception as exc:
            rejected.append({"name": symbol, "issue": str(exc)})

    profiles.sort(key=lambda item: item["score"], reverse=True)
    selected = profiles[: args.top]
    backtest_profiles = list(selected[: args.backtest_top])
    backtest_symbols = {profile["name"] for profile in backtest_profiles}
    benchmark_symbols: list[str] = []
    for symbol in config.symbols:
        if symbol in backtest_symbols:
            continue
        profile = profile_by_symbol.get(symbol)
        if profile is None:
            try:
                profile = broker.symbol_profile(symbol, config.strategy_for(symbol).timeframe, args.profile_bars)
                profile.update(_candidate_metrics(profile))
                profile["score"] = _candidate_score(profile)
            except Exception as exc:
                rejected.append({"name": symbol, "issue": f"benchmark profile failed: {exc}"})
                continue
        backtest_profiles.append(profile)
        backtest_symbols.add(symbol)
        benchmark_symbols.append(symbol)

    backtests = []
    for profile in backtest_profiles:
        try:
            backtests.append(
                _annotate_backtest_candidate(
                    _backtest_candidate(config, broker, profile["name"], args.backtest_bars, profile["spread_points"])
                )
            )
        except Exception as exc:
            backtests.append(_annotate_backtest_candidate({"symbol": profile["name"], "error": str(exc)}))
    backtests.sort(key=lambda item: item["backtest_score"], reverse=True)

    return {
        "mode": config.mode,
        "queries": queries,
        "discovered": len(discovered),
        "per_query": per_query,
        "profile_bars": args.profile_bars,
        "backtest_bars": args.backtest_bars,
        "ranking_note": "Read-only candidate ranking; this does not change config or send orders.",
        "top": selected,
        "backtests": backtests,
        "benchmark_symbols": benchmark_symbols,
        "backtest_recommendations": [item for item in backtests if "issue" not in item],
        "rejected_count": len(rejected),
        "rejected_issue_counts": _issue_counts(rejected),
        "rejected_sample": rejected[:20],
    }


def _candidate_metrics(profile: dict[str, Any]) -> dict[str, Any]:
    point = float(profile.get("point") or 0.0)
    spread_points = float(profile.get("spread_points") or 0.0)
    tick_size = float(profile.get("tick_size") or 0.0)
    tick_value = float(profile.get("tick_value") or 0.0)
    min_lot = float(profile.get("min_lot") or 0.0)
    atr_points = float(profile.get("atr_points") or 0.0)
    if point <= 0 or tick_size <= 0 or tick_value <= 0 or min_lot <= 0:
        spread_cost = float("inf")
    else:
        spread_cost = (spread_points * point / tick_size) * tick_value * min_lot
    spread_to_atr = None if atr_points <= 0 else spread_points / atr_points
    return {
        "spread_cost_min_lot": round(spread_cost, 4) if spread_cost != float("inf") else spread_cost,
        "spread_to_atr_ratio": round(spread_to_atr, 4) if spread_to_atr is not None else None,
    }


def _issue_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        issue = row.get("issue")
        if not issue:
            continue
        key = str(issue)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _candidate_score(profile: dict[str, Any]) -> float:
    bars = min(float(profile.get("bars") or 0), 1000.0)
    spread_cost = float(profile.get("spread_cost_min_lot", 9999.0))
    spread_to_atr = profile.get("spread_to_atr_ratio")
    tick_value = float(profile.get("tick_value") or 0.0)
    trade_mode = int(profile.get("trade_mode", -1))
    score = bars / 20.0
    score -= min(spread_cost, 50.0) * 2.0
    if spread_to_atr is not None:
        score -= min(float(spread_to_atr), 10.0) * 8.0
    if tick_value <= 0:
        score -= 100.0
    if trade_mode == 0:
        score -= 100.0
    path = str(profile.get("path") or "")
    if "Cryptocurrencies" in path:
        score += 8.0
    if "Forex\\Standard\\Majors" in path:
        score += 6.0
    if "Cash Indices" in path:
        score += 4.0
    if "Stocks" in path:
        score -= 10.0
    return round(score, 4)


def _candidate_issue(profile: dict[str, Any], min_bars: int) -> str | None:
    if int(profile.get("trade_mode", -1)) == 0:
        return "broker trade mode disabled"
    if float(profile.get("bid") or 0.0) <= 0 or float(profile.get("ask") or 0.0) <= 0:
        return "no current quote"
    if int(profile.get("bars") or 0) < min_bars:
        return "not enough recent bars"
    if _last_bar_age_hours(profile) > 72:
        return "stale recent history"
    if float(profile.get("tick_value") or 0.0) <= 0:
        return "missing tick value"
    if float(profile.get("tick_size") or 0.0) <= 0:
        return "missing tick size"
    if float(profile.get("spread_cost_min_lot", 9999.0)) > 10:
        return "spread cost too high for small account"
    if "atr_points" in profile and float(profile.get("atr_points") or 0.0) <= 0:
        return "no recent ATR movement"
    spread_to_atr = profile.get("spread_to_atr_ratio")
    if spread_to_atr is not None and float(spread_to_atr) > 2.0:
        return "spread too high relative to recent ATR"
    return None


def _annotate_backtest_candidate(result: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(result)
    annotated["backtest_score"] = _backtest_candidate_score(annotated)
    issue = _backtest_candidate_issue(annotated)
    if issue:
        annotated["issue"] = issue
    return annotated


def _backtest_candidate_score(result: dict[str, Any]) -> float:
    if result.get("error"):
        return -1000.0
    trades = int(result.get("trades") or 0)
    net_profit = float(result.get("net_profit") or 0.0)
    max_drawdown_pct = float(result.get("max_drawdown_pct") or 0.0)
    profit_factor = result.get("profit_factor")
    if trades <= 0:
        return round(-100.0 + net_profit - max_drawdown_pct, 4)
    score = net_profit - (max_drawdown_pct * 2.0) + min(trades, 20) * 0.05
    if profit_factor is None:
        score -= 5.0
    else:
        score += min(float(profit_factor), 5.0)
    if net_profit <= 0:
        score -= 5.0
    return round(score, 4)


def _backtest_candidate_issue(result: dict[str, Any]) -> str | None:
    if result.get("error"):
        return str(result["error"])
    trades = int(result.get("trades") or 0)
    if trades <= 0:
        return "no tested trades"
    if trades < 3:
        return "too few tested trades"
    net_profit = float(result.get("net_profit") or 0.0)
    if net_profit <= 0:
        return "non-positive backtest net profit"
    profit_factor = result.get("profit_factor")
    if profit_factor is not None and float(profit_factor) <= 1.0:
        return "profit factor <= 1"
    max_drawdown_pct = float(result.get("max_drawdown_pct") or 0.0)
    if max_drawdown_pct > 5.0:
        return "backtest drawdown too high"
    return None


def _last_bar_age_hours(profile: dict[str, Any]) -> float:
    raw = profile.get("last_bar")
    if not raw:
        return float("inf")
    last_bar = datetime.fromisoformat(str(raw))
    if last_bar.tzinfo is None:
        last_bar = last_bar.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_bar).total_seconds() / 3600


def _backtest_candidate(
    config,
    broker: MT5Broker,
    symbol: str,
    bars_requested: int,
    spread_points: float,
) -> dict[str, Any]:
    strategy_config = config.strategy_for(symbol)
    candidate_config = replace(
        config,
        symbols=[symbol],
        symbol_risk={},
        live_safety=replace(config.live_safety, allowed_symbols=[symbol]),
    )
    min_bars = strategy_config.bars + 2
    bars_count = max(bars_requested, min_bars)
    rules = broker.rules(symbol)
    bars = broker.bars(symbol, strategy_config.timeframe, bars_count)
    regime_count = max(strategy_config.regime_bars, bars_count)
    regime_bars = broker.bars(symbol, strategy_config.regime_timeframe, regime_count)
    initial_equity = broker.account().equity
    summary = BacktestEngine(
        candidate_config, rules, initial_equity, spread_points, costs=cost_model_for(config, symbol)
    ).run(symbol, bars, regime_bars)
    return {
        "symbol": symbol,
        "net_profit": summary["net_profit"],
        "return_pct": summary["return_pct"],
        "max_drawdown_pct": summary["max_drawdown_pct"],
        "trades": summary["trades"],
        "win_rate_pct": summary["win_rate_pct"],
        "profit_factor": summary["profit_factor"],
        "spread_points": summary["spread_points"],
    }


def run_backtest(config, broker: MT5Broker, args: argparse.Namespace) -> dict:
    symbols = args.symbol or config.symbols
    min_bars = max(config.strategy_for(symbol).bars + 2 for symbol in symbols)
    if args.bars < min_bars:
        raise SystemExit(f"bars must be at least {min_bars}")

    account = broker.account()
    initial_equity = float(args.initial_equity if args.initial_equity is not None else account.equity)
    results = []
    for symbol in symbols:
        research_config = _config_for_research_symbol(config, symbol)
        strategy_config = research_config.strategy_for(symbol)
        rules = broker.rules(symbol)
        bars = broker.bars(symbol, strategy_config.timeframe, args.bars)
        regime_count = max(strategy_config.regime_bars, args.bars)
        regime_bars = broker.bars(symbol, strategy_config.regime_timeframe, regime_count)
        spread_points = (
            _research_spread_points(config, broker, args, symbol)
        )
        engine = BacktestEngine(
            research_config, rules, initial_equity, spread_points, costs=cost_model_for(config, symbol)
        )
        results.append(engine.run(symbol, bars, regime_bars))

    return {
        "mode": config.mode,
        "timeframe": config.strategy.timeframe,
        "regime_timeframe": config.strategy.regime_timeframe,
        "bars_requested": args.bars,
        "initial_equity_source": "argument" if args.initial_equity is not None else "mt5_account_equity",
        "note": "Each symbol is backtested independently from the same starting equity.",
        "results": results,
    }


def run_blocked_outcomes(config, broker: MT5Broker, args: argparse.Namespace) -> dict:
    records = load_jsonl_records(args.log_file)
    symbols = sorted(
        {
            str(record.get("symbol"))
            for record in records
            if record.get("event") == "blocked"
            and record.get("signal") in {"buy", "sell"}
            and record.get("hypothetical_entry_price") is not None
            and record.get("hypothetical_stop_loss") is not None
            and record.get("hypothetical_take_profit") is not None
        }
    )
    bars_by_symbol = {}
    for symbol in symbols:
        strategy = config.strategy_for(symbol)
        bars_by_symbol[symbol] = broker.bars(symbol, strategy.timeframe, int(args.bars))

    payload = evaluate_blocked_hypotheticals(
        records,
        bars_by_symbol,
        lookahead_bars=int(args.lookahead_bars),
        min_promotion_resolved=int(args.min_promotion_resolved),
        min_promotion_net_r=float(args.min_promotion_net_r),
        min_promotion_win_rate_pct=float(args.min_promotion_win_rate_pct),
        candidate_rank_floor=float(config.live_safety.min_candidate_rank_score),
    )
    payload["log_file"] = str(args.log_file)
    payload["bars_requested"] = int(args.bars)
    payload["lookahead_bars"] = int(args.lookahead_bars)
    payload["symbols"] = symbols
    return payload


def run_optimize(config, broker: MT5Broker, args: argparse.Namespace) -> dict:
    symbols = args.symbol or config.symbols
    min_bars = max(config.strategy_for(symbol).bars + 2 for symbol in symbols)
    if args.bars < min_bars:
        raise SystemExit(f"bars must be at least {min_bars}")
    if args.limit < 1:
        raise SystemExit("limit must be at least 1")
    if args.min_trades < 0:
        raise SystemExit("min-trades must be non-negative")

    account = broker.account()
    initial_equity = float(args.initial_equity if args.initial_equity is not None else account.equity)
    if args.expanded_grid:
        grid = expanded_grid()
    elif args.turnover_grid:
        grid = turnover_grid()
    elif args.entry_quality_grid:
        grid = entry_quality_grid()
    elif args.management_grid:
        grid = management_grid()
    elif args.regime_grid:
        grid = regime_grid()
    elif args.recovery_grid:
        grid = recovery_grid()
    elif args.mean_reversion_grid:
        grid = mean_reversion_grid()
    elif args.squeeze_breakout_grid:
        grid = squeeze_breakout_grid()
    elif args.pullback_reclaim_grid:
        grid = pullback_reclaim_grid()
    elif args.volatility_continuation_grid:
        grid = volatility_continuation_grid()
    elif args.scalp_grid:
        grid = scalp_grid()
    elif args.spread_expansion_grid:
        grid = spread_expansion_grid()
    else:
        grid = default_grid()
    results = []
    for symbol in symbols:
        research_config = _config_for_research_symbol(config, symbol)
        strategy_config = research_config.strategy_for(symbol)
        rules = broker.rules(symbol)
        bars = broker.bars(symbol, strategy_config.timeframe, args.bars)
        regime_count = max(strategy_config.regime_bars, args.bars)
        regime_bars = broker.bars(symbol, strategy_config.regime_timeframe, regime_count)
        spread_points = (
            _research_spread_points(config, broker, args, symbol)
        )
        results.append(
            optimize_strategy(
                config=research_config,
                symbol=symbol,
                bars=bars,
                regime_bars=regime_bars,
                rules=rules,
                initial_equity=initial_equity,
                spread_points=spread_points,
                grid=grid,
                min_trades=args.min_trades,
                limit=args.limit,
            )
        )

    return {
        "mode": config.mode,
        "timeframe": config.strategy.timeframe,
        "regime_timeframe": config.strategy.regime_timeframe,
        "bars_requested": args.bars,
        "grid": grid,
        "score": "net_profit - 2*max_drawdown - undertrade_penalty + small_trade_count_bonus",
        "note": "Candidates are ranked only for review; this command does not change config files.",
        "results": results,
    }


def run_walk_forward(config, broker: MT5Broker, args: argparse.Namespace) -> dict:
    symbols = args.symbol or config.symbols
    min_bars = max((config.strategy_for(symbol).bars + 2) * 2 for symbol in symbols)
    if args.bars < min_bars:
        raise SystemExit(f"bars must be at least {min_bars}")
    if args.limit < 1:
        raise SystemExit("limit must be at least 1")
    if args.min_train_trades < 0:
        raise SystemExit("min-train-trades must be non-negative")
    if args.folds < 1:
        raise SystemExit("folds must be at least 1")

    account = broker.account()
    initial_equity = float(args.initial_equity if args.initial_equity is not None else account.equity)
    if args.expanded_grid:
        grid = expanded_grid()
    elif args.turnover_grid:
        grid = turnover_grid()
    elif args.entry_quality_grid:
        grid = entry_quality_grid()
    elif args.management_grid:
        grid = management_grid()
    elif args.regime_grid:
        grid = regime_grid()
    elif args.recovery_grid:
        grid = recovery_grid()
    elif args.mean_reversion_grid:
        grid = mean_reversion_grid()
    elif args.squeeze_breakout_grid:
        grid = squeeze_breakout_grid()
    elif args.pullback_reclaim_grid:
        grid = pullback_reclaim_grid()
    elif args.volatility_continuation_grid:
        grid = volatility_continuation_grid()
    elif args.scalp_grid:
        grid = scalp_grid()
    elif args.spread_expansion_grid:
        grid = spread_expansion_grid()
    elif args.full_grid:
        grid = default_grid()
    else:
        grid = quick_grid()
    results = []
    for symbol in symbols:
        symbol_grid = _grid_for_walk_forward_symbol(grid, symbol, args)
        research_config = _config_for_research_symbol(config, symbol)
        strategy_config = research_config.strategy_for(symbol)
        rules = broker.rules(symbol)
        bars = broker.bars(symbol, strategy_config.timeframe, args.bars)
        regime_count = max(strategy_config.regime_bars, args.bars)
        regime_bars = broker.bars(symbol, strategy_config.regime_timeframe, regime_count)
        spread_points = (
            _research_spread_points(config, broker, args, symbol)
        )
        try:
            results.append(
                walk_forward_validate(
                    config=research_config,
                    symbol=symbol,
                    bars=bars,
                    regime_bars=regime_bars,
                    rules=rules,
                    initial_equity=initial_equity,
                    spread_points=spread_points,
                    grid=symbol_grid,
                    train_ratio=args.train_ratio,
                    min_train_trades=args.min_train_trades,
                    limit=args.limit,
                    folds=args.folds,
                )
            )
        except ValueError as exc:
            results.append({"symbol": symbol, "error": str(exc)})

    return {
        "mode": config.mode,
        "timeframe": config.strategy.timeframe,
        "regime_timeframe": config.strategy.regime_timeframe,
        "bars_requested": args.bars,
        "grid": _walk_forward_payload_grid(grid, symbols, args),
        "folds": args.folds,
        "note": "Anchored train splits optimize candidates; later unseen test folds validate them. No config files are changed.",
        "results": results,
    }


def _grid_from_args(args: argparse.Namespace) -> dict:
    """Resolve the parameter grid from the standard ``--*-grid`` flags shared by
    ``walk-forward`` and ``time-chamber``."""
    if getattr(args, "expanded_grid", False):
        return expanded_grid()
    if getattr(args, "turnover_grid", False):
        return turnover_grid()
    if getattr(args, "entry_quality_grid", False):
        return entry_quality_grid()
    if getattr(args, "management_grid", False):
        return management_grid()
    if getattr(args, "regime_grid", False):
        return regime_grid()
    if getattr(args, "recovery_grid", False):
        return recovery_grid()
    if getattr(args, "mean_reversion_grid", False):
        return mean_reversion_grid()
    if getattr(args, "squeeze_breakout_grid", False):
        return squeeze_breakout_grid()
    if getattr(args, "pullback_reclaim_grid", False):
        return pullback_reclaim_grid()
    if getattr(args, "volatility_continuation_grid", False):
        return volatility_continuation_grid()
    if getattr(args, "scalp_grid", False):
        return scalp_grid()
    if getattr(args, "spread_expansion_grid", False):
        return spread_expansion_grid()
    if getattr(args, "full_grid", False):
        return default_grid()
    return quick_grid()


def run_time_chamber(config, broker: MT5Broker, args: argparse.Namespace) -> dict:
    symbols = args.symbol or config.symbols
    if args.folds < 1:
        raise SystemExit("folds must be at least 1")
    if not 0.1 <= args.oos_fraction <= 0.8:
        raise SystemExit("oos-fraction must be between 0.1 and 0.8")

    account = broker.account()
    initial_equity = float(args.initial_equity if args.initial_equity is not None else account.equity)
    grid = _grid_from_args(args)

    results = []
    verdicts: dict[str, str] = {}
    for symbol in symbols:
        symbol_grid = _grid_for_walk_forward_symbol(grid, symbol, args)
        research_config = _config_for_research_symbol(config, symbol)
        strategy_config = research_config.strategy_for(symbol)
        rules = broker.rules(symbol)
        bars = broker.bars(symbol, strategy_config.timeframe, args.bars)
        regime_count = max(strategy_config.regime_bars, args.bars)
        regime_bars = broker.bars(symbol, strategy_config.regime_timeframe, regime_count)
        spread_points = _research_spread_points(config, broker, args, symbol)
        try:
            report = time_chamber_eval(
                research_config,
                symbol,
                bars,
                regime_bars,
                rules,
                initial_equity,
                spread_points,
                symbol_grid,
                costs=cost_model_for(research_config, symbol),
                folds=args.folds,
                oos_fraction=args.oos_fraction,
                optimizer_min_trades=args.optimizer_min_trades,
                min_oos_trades=args.min_oos_trades,
                min_oos_profit_factor=args.min_oos_profit_factor,
                min_folds_profitable_pct=args.min_folds_profitable_pct,
            )
            verdicts[symbol] = report["verdict"]
            results.append(report)
        except ValueError as exc:
            verdicts[symbol] = "ERROR"
            results.append({"symbol": symbol, "error": str(exc)})

    return {
        "mode": config.mode,
        "timeframe": config.strategy.timeframe,
        "regime_timeframe": config.strategy.regime_timeframe,
        "bars_requested": args.bars,
        "folds": args.folds,
        "oos_fraction": args.oos_fraction,
        "verdicts": verdicts,
        "note": (
            "Hyperbolic Time Chamber: each fold re-optimizes on only-prior data, picks the single "
            "in-sample best, and grades it once on the next unseen window, after broker costs. "
            "PASS means the edge survived data it was never fitted to. No config files are changed."
        ),
        "results": results,
    }


def _walk_forward_payload_grid(grid: dict, symbols: list[str], args: argparse.Namespace) -> dict:
    if len(symbols) == 1:
        return _grid_for_walk_forward_symbol(grid, symbols[0], args)
    return _grid_with_session_overrides(grid, args)


def _grid_for_walk_forward_symbol(grid: dict, symbol: str, args: argparse.Namespace) -> dict:
    updated = _grid_with_session_overrides(grid, args)
    if getattr(args, "allowed_utc_hours", None) or getattr(args, "blocked_utc_hours", None):
        return updated
    if str(symbol).upper() == "BTCUSD":
        return updated
    cleaned = {key: list(value) for key, value in updated.items()}
    cleaned["blocked_utc_hour"] = [-1]
    cleaned["blocked_utc_hours"] = [()]
    cleaned["allowed_utc_hours"] = [()]
    return cleaned


def _grid_with_session_overrides(grid: dict, args: argparse.Namespace) -> dict:
    allowed_hours = getattr(args, "allowed_utc_hours", None)
    blocked_hours = getattr(args, "blocked_utc_hours", None)
    if not allowed_hours and not blocked_hours:
        return grid
    if allowed_hours and blocked_hours:
        raise SystemExit("--allowed-utc-hours and --blocked-utc-hours cannot be used together")

    updated = {key: list(value) for key, value in grid.items()}
    updated["blocked_utc_hour"] = [-1]
    if allowed_hours:
        updated["allowed_utc_hours"] = [(), _parse_utc_hours(allowed_hours)]
        updated["blocked_utc_hours"] = [()]
    else:
        updated["blocked_utc_hours"] = [(), _parse_utc_hours(blocked_hours)]
        updated["allowed_utc_hours"] = [()]
    return updated


def _parse_utc_hours(value: str | None) -> tuple[int, ...]:
    if value is None:
        return ()
    hours: list[int] = []
    for raw in str(value).split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            hour = int(raw)
        except ValueError as exc:
            raise SystemExit(f"UTC hour must be an integer: {raw}") from exc
        if hour < 0 or hour > 23:
            raise SystemExit("UTC hours must be between 0 and 23")
        if hour not in hours:
            hours.append(hour)
    if not hours:
        raise SystemExit("at least one UTC hour is required")
    return tuple(sorted(hours))


def run_signal_audit(config, broker: MT5Broker, args: argparse.Namespace) -> dict:
    symbols = args.symbol or config.symbols
    min_bars = max(config.strategy_for(symbol).bars + 2 for symbol in symbols)
    if args.bars < min_bars:
        raise SystemExit(f"bars must be at least {min_bars}")

    account = broker.account()
    equity = float(args.initial_equity if args.initial_equity is not None else account.equity)
    results = []
    for symbol in symbols:
        research_config = _config_for_research_symbol(config, symbol)
        strategy_config = research_config.strategy_for(symbol)
        rules = broker.rules(symbol)
        bars = broker.bars(symbol, strategy_config.timeframe, args.bars)
        regime_count = max(strategy_config.regime_bars, args.bars)
        regime_bars = broker.bars(symbol, strategy_config.regime_timeframe, regime_count)
        spread_points = (
            _research_spread_points(config, broker, args, symbol)
        )
        results.append(
            SignalAuditEngine(
                config=research_config,
                rules=rules,
                equity=equity,
                spread_points=spread_points,
            ).run(symbol, bars, regime_bars, shadow_entry_modes=args.shadow_entry_mode)
        )

    return {
        "mode": config.mode,
        "timeframe": config.strategy.timeframe,
        "regime_timeframe": config.strategy.regime_timeframe,
        "bars_requested": args.bars,
        "note": "Signal audit is read-only. It explains strategy and risk bottlenecks; it does not change config files.",
        "results": results,
    }


def _config_for_research_symbol(config, symbol: str):
    allowed_symbols = config.live_safety.allowed_symbols
    if symbol in config.symbols and (not allowed_symbols or symbol in allowed_symbols):
        return config
    return replace(
        config,
        symbols=[symbol],
        live_safety=replace(config.live_safety, allowed_symbols=[symbol]),
    )


def _research_spread_points(config, broker: MT5Broker, args: argparse.Namespace, symbol: str) -> float:
    if getattr(args, "spread_points", None) is not None:
        return float(args.spread_points)
    if symbol in config.symbols or symbol in config.symbol_risk:
        return configured_spread_points(config, symbol)
    return float(broker.tick(symbol).spread_points)


if __name__ == "__main__":
    main()
