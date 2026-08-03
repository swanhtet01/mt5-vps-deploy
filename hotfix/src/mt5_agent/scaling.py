from __future__ import annotations

from typing import Any

from mt5_agent.config import AgentConfig, ProfitScalingStep
from mt5_agent.models import AccountState
from mt5_agent.profit_funded_scaling import (
    evaluate_account_guard,
    evaluate_profit_funded_scaling,
)
from mt5_agent.risk import RiskManager


def build_profit_scale_report(
    config: AgentConfig,
    account: AccountState,
    log_summary: dict[str, Any] | None,
    deal_summary: dict[str, Any] | None = None,
    recent_deal_summary: dict[str, Any] | None = None,
    recent_deal_window_days: int | None = None,
    target_balance: float = 100000.0,
    min_live_orders_for_scale: int | None = None,
    deal_nets: list[float] | None = None,
    deal_nets_by_symbol: dict[str, list[float]] | None = None,
    account_deal_nets: list[float] | None = None,
    account_deal_window_days: int | None = None,
) -> dict[str, Any]:
    scaling = config.profit_scaling
    min_live_orders = (
        scaling.min_live_orders_for_scale
        if min_live_orders_for_scale is None
        else int(min_live_orders_for_scale)
    )
    realized_profit = account.balance - scaling.baseline_balance
    active_step = _active_step(scaling.steps, realized_profit) if scaling.enabled else None
    next_step = _next_step(scaling.steps, realized_profit) if scaling.enabled else None
    log_live_orders = int((log_summary or {}).get("live_orders") or 0)
    closed_deals = int((deal_summary or {}).get("closed_deals") or 0)
    deal_net_profit = (deal_summary or {}).get("net_profit")
    deal_net_profit = float(deal_net_profit) if deal_net_profit is not None else None
    recent_losing_streak = (deal_summary or {}).get("recent_losing_streak")
    recent_losing_streak = int(recent_losing_streak) if recent_losing_streak is not None else None
    by_symbol = (deal_summary or {}).get("by_symbol") or {}
    recent_by_symbol = (recent_deal_summary or {}).get("by_symbol") or {}
    scale_evidence_count = max(log_live_orders, closed_deals)
    risk = RiskManager(
        config,
        scale_live_orders=scale_evidence_count,
        scale_closed_deals=closed_deals,
        scale_deal_net_profit=deal_net_profit,
        scale_recent_losing_streak=recent_losing_streak,
        scale_deal_nets=deal_nets,
        scale_symbol_deal_nets=deal_nets_by_symbol,
        scale_account_deal_nets=account_deal_nets,
        scale_symbol_closed_deals={
            symbol: int(values.get("closed_deals") or 0)
            for symbol, values in by_symbol.items()
        },
        scale_symbol_deal_net_profit={
            symbol: float(values.get("net_profit") or 0.0)
            for symbol, values in by_symbol.items()
        },
        scale_symbol_recent_losing_streak={
            symbol: int(values.get("recent_losing_streak") or 0)
            for symbol, values in by_symbol.items()
        },
        recent_symbol_closed_deals={
            symbol: int(values.get("closed_deals") or 0)
            for symbol, values in recent_by_symbol.items()
        },
        recent_symbol_deal_net_profit={
            symbol: float(values.get("net_profit") or 0.0)
            for symbol, values in recent_by_symbol.items()
        },
        recent_symbol_recent_losing_streak={
            symbol: int(values.get("recent_losing_streak") or 0)
            for symbol, values in recent_by_symbol.items()
        },
    )
    missing_live_orders = max(min_live_orders - scale_evidence_count, 0)
    quality_gate_enabled = (
        scaling.min_deal_net_profit_for_scale is not None
        or scaling.max_losing_streak_for_scale > 0
    )
    required_closed_deals_for_quality = min_live_orders if quality_gate_enabled else 0
    if quality_gate_enabled and required_closed_deals_for_quality <= 0:
        required_closed_deals_for_quality = 1
    symbol_risk_posture = _symbol_risk_posture(
        config,
        by_symbol,
        required_closed_deals_for_quality,
    )
    missing_closed_deals_for_quality = max(required_closed_deals_for_quality - closed_deals, 0)
    deal_net_profit_below_scale_min = (
        scaling.min_deal_net_profit_for_scale is not None
        and deal_net_profit is not None
        and deal_net_profit < scaling.min_deal_net_profit_for_scale
    )
    losing_streak_scale_blocked = (
        scaling.max_losing_streak_for_scale > 0
        and recent_losing_streak is not None
        and recent_losing_streak >= scaling.max_losing_streak_for_scale
    )
    quality_blocked = deal_net_profit_below_scale_min or losing_streak_scale_blocked
    profit_funded_policy = (
        evaluate_profit_funded_scaling(deal_nets) if deal_nets is not None else None
    )
    profit_funded_policy_by_symbol = (
        {
            symbol: evaluate_profit_funded_scaling(deal_nets_by_symbol.get(symbol, []))
            for symbol in config.symbols
        }
        if deal_nets_by_symbol is not None
        else None
    )
    profit_funded_account_guard = (
        evaluate_account_guard(account_deal_nets)
        if account_deal_nets is not None
        else None
    )
    if profit_funded_account_guard is not None and account_deal_window_days is not None:
        profit_funded_account_guard["window_days"] = int(account_deal_window_days)
    profit_funded_blocked = bool(
        active_step is not None
        and active_step.profit_usd > 0
        and profit_funded_policy is not None
        and not profit_funded_policy["promotion_authorized"]
    )
    profit_funded_next_blocked = bool(
        next_step is not None
        and profit_funded_policy is not None
        and not profit_funded_policy["promotion_authorized"]
    )
    current_step_evidence_ok = (
        active_step is None
        or active_step.profit_usd <= 0
        or (
            missing_live_orders == 0
            and missing_closed_deals_for_quality == 0
            and not quality_blocked
            and not profit_funded_blocked
        )
    )
    floating_drawdown_pct = _floating_drawdown_pct(account)
    floating_drawdown_paused = (
        scaling.enabled
        and scaling.drawdown_pause_pct > 0
        and floating_drawdown_pct >= scaling.drawdown_pause_pct
    )

    blockers: list[str] = []
    if not scaling.enabled:
        blockers.append("profit scaling disabled")
    if active_step is not None and active_step.profit_usd > 0 and missing_live_orders > 0:
        blockers.append(f"need {missing_live_orders} more live orders before trusting scaled step")
    if active_step is not None and active_step.profit_usd > 0 and missing_closed_deals_for_quality > 0:
        blockers.append(
            f"need {missing_closed_deals_for_quality} closed deals before trusting deal-quality scale gate"
        )
    if deal_net_profit_below_scale_min:
        blockers.append(
            "closed deal net profit "
            f"{deal_net_profit:.2f} below scale minimum {scaling.min_deal_net_profit_for_scale:.2f}"
        )
    if losing_streak_scale_blocked:
        blockers.append(
            f"recent losing streak {recent_losing_streak} reached scale limit {scaling.max_losing_streak_for_scale}"
        )
    if floating_drawdown_paused:
        blockers.append("floating drawdown pause active")
    if profit_funded_blocked:
        blockers.append("closed-profit statistical promotion policy holds scaling at 1x")
    if profit_funded_next_blocked:
        blockers.append("closed-profit statistical policy blocks the next scale step")
    if (
        next_step is not None
        and profit_funded_account_guard is not None
        and float(profit_funded_account_guard["multiplier_cap"]) <= 1.0
    ):
        blockers.append("account-wide closed P/L guard blocks the next scale step")
    if next_step is not None:
        if missing_live_orders > 0:
            blockers.append(f"need {missing_live_orders} live orders before next scale step")
        if missing_closed_deals_for_quality > 0:
            blockers.append(
                f"need {missing_closed_deals_for_quality} closed deals before next scale step quality gate"
            )
        remaining_to_next = max(next_step.profit_usd - realized_profit, 0.0)
        if remaining_to_next > 0:
            blockers.append(f"need {remaining_to_next:.2f} more realized profit for next step")
    else:
        remaining_to_next = 0.0

    return {
        "target_balance": round(target_balance, 2),
        "current_balance": round(account.balance, 2),
        "current_equity": round(account.equity, 2),
        "remaining_balance_to_target": round(max(target_balance - account.balance, 0.0), 2),
        "profit_scaling_enabled": scaling.enabled,
        "baseline_balance": round(scaling.baseline_balance, 2),
        "realized_profit": round(realized_profit, 2),
        "active_step": _step_payload(active_step),
        "next_step": _step_payload(next_step),
        "remaining_profit_to_next_step": round(remaining_to_next, 2),
        "effective_risk": risk.effective_risk_snapshot(account),
        "effective_risk_by_symbol": _effective_risk_by_symbol(config, risk, account),
        "evidence": {
            "live_orders": log_live_orders,
            "closed_deals": closed_deals,
            "scale_evidence_count": scale_evidence_count,
            "min_live_orders_for_scale": min_live_orders,
            "missing_live_orders": missing_live_orders,
            "required_closed_deals_for_quality": required_closed_deals_for_quality,
            "missing_closed_deals_for_quality": missing_closed_deals_for_quality,
            "log_records": int((log_summary or {}).get("records") or 0),
            "deal_net_profit": 0.0 if deal_net_profit is None else deal_net_profit,
            "deal_profit_factor": (deal_summary or {}).get("profit_factor"),
            "recent_losing_streak": recent_losing_streak or 0,
            "min_deal_net_profit_for_scale": scaling.min_deal_net_profit_for_scale,
            "max_losing_streak_for_scale": scaling.max_losing_streak_for_scale,
            "require_symbol_closed_deal_quality_for_scale": (
                scaling.require_symbol_closed_deal_quality_for_scale
            ),
            "by_symbol": by_symbol,
        },
        "profit_funded_policy": profit_funded_policy,
        "profit_funded_policy_by_symbol": profit_funded_policy_by_symbol,
        "profit_funded_account_guard": profit_funded_account_guard,
        "deal_windows": _deal_windows_payload(
            deal_summary,
            recent_deal_summary,
            recent_deal_window_days,
        ),
        "current_step_evidence_ok": current_step_evidence_ok,
        "symbol_risk_posture": symbol_risk_posture,
        "floating_drawdown_pct": round(floating_drawdown_pct, 4),
        "floating_drawdown_paused": floating_drawdown_paused,
        "scale_blockers": blockers,
        "scale_status": "clear" if not blockers else "hold",
    }


def _effective_risk_by_symbol(
    config: AgentConfig,
    risk: RiskManager,
    account: AccountState,
) -> dict[str, Any]:
    allowed_symbols = set(config.live_safety.allowed_symbols)
    payload: dict[str, Any] = {}
    for symbol in config.symbols:
        snapshot = risk.effective_risk_snapshot(account, symbol)
        max_total_open_risk = risk.max_total_open_risk_limit(account, symbol)
        payload[symbol] = {
            "live_allowed": not allowed_symbols or symbol in allowed_symbols,
            "active_profit_step_usd": snapshot["active_profit_step_usd"],
            "risk_per_trade_pct": snapshot["risk_per_trade_pct"],
            "max_trade_risk_usd": snapshot["max_trade_risk_usd"],
            "max_total_open_risk_pct": snapshot["max_total_open_risk_pct"],
            "max_total_open_risk_usd": round(max_total_open_risk, 2),
            "max_new_trade_risk_usd": round(risk.max_new_trade_risk(account, symbol), 2),
            "live_max_order_volume": snapshot["live_max_order_volume"],
            "profit_funded_policy_multiplier": snapshot[
                "profit_funded_policy_multiplier"
            ],
            "symbol_quality_risk_factor": snapshot["symbol_quality_risk_factor"],
            "symbol_quality_risk_reduced": snapshot["symbol_quality_risk_reduced"],
            "recent_symbol_deal_net_profit": snapshot["recent_symbol_deal_net_profit"],
            "recent_symbol_recent_losing_streak": snapshot["recent_symbol_recent_losing_streak"],
            "loss_recovery_active": snapshot["loss_recovery_active"],
            "floating_drawdown_paused": snapshot["floating_drawdown_paused"],
        }
    return payload


def _deal_windows_payload(
    deal_summary: dict[str, Any] | None,
    recent_deal_summary: dict[str, Any] | None,
    recent_deal_window_days: int | None,
) -> dict[str, Any]:
    scale_window = _compact_deal_window(deal_summary)
    recent_window = _compact_deal_window(recent_deal_summary)
    if recent_window is not None and recent_deal_window_days is not None:
        recent_window["days"] = int(recent_deal_window_days)
    return {
        "scale_window": scale_window,
        "recent_window": recent_window,
        "interpretation": _deal_window_interpretation(scale_window, recent_window),
    }


def _compact_deal_window(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "closed_deals": int(summary.get("closed_deals") or 0),
        "net_profit": round(float(summary.get("net_profit") or 0.0), 2),
        "profit_factor": summary.get("profit_factor"),
        "recent_losing_streak": int(summary.get("recent_losing_streak") or 0),
        "by_symbol": summary.get("by_symbol") or {},
    }


def _deal_window_interpretation(
    scale_window: dict[str, Any] | None,
    recent_window: dict[str, Any] | None,
) -> str:
    if scale_window is None or recent_window is None:
        return "single_window"
    scale_net = float(scale_window.get("net_profit") or 0.0)
    recent_net = float(recent_window.get("net_profit") or 0.0)
    if recent_net > 0 and scale_net < 0:
        return "recent_recovery_but_scale_window_still_negative"
    if recent_net < 0 and scale_net > 0:
        return "recent_degradation_inside_positive_scale_window"
    if recent_net > 0 and scale_net > 0:
        return "recent_and_scale_windows_positive"
    if recent_net < 0 and scale_net < 0:
        return "recent_and_scale_windows_negative"
    return "mixed_or_flat"


def _active_step(steps: list[ProfitScalingStep], realized_profit: float) -> ProfitScalingStep | None:
    active: ProfitScalingStep | None = None
    for step in steps:
        if realized_profit >= step.profit_usd:
            active = step
        else:
            break
    return active


def _next_step(steps: list[ProfitScalingStep], realized_profit: float) -> ProfitScalingStep | None:
    for step in steps:
        if realized_profit < step.profit_usd:
            return step
    return None


def _step_payload(step: ProfitScalingStep | None) -> dict[str, Any] | None:
    if step is None:
        return None
    return {
        "profit_usd": round(step.profit_usd, 2),
        "risk_per_trade_pct": step.risk_per_trade_pct,
        "max_trade_risk_usd": step.max_trade_risk_usd,
        "max_total_open_risk_pct": step.max_total_open_risk_pct,
        "live_max_order_volume": step.live_max_order_volume,
        "symbol_risk_per_trade_pct": step.symbol_risk_per_trade_pct,
        "symbol_max_order_volume": step.symbol_max_order_volume,
    }


def _symbol_risk_posture(
    config: AgentConfig,
    by_symbol: dict[str, Any],
    required_closed_deals: int,
) -> dict[str, Any]:
    scaling = config.profit_scaling
    quality_gate_enabled = (
        scaling.require_symbol_closed_deal_quality_for_scale
        and (
            scaling.min_deal_net_profit_for_scale is not None
            or scaling.max_losing_streak_for_scale > 0
        )
    )
    posture: dict[str, Any] = {}
    for symbol in config.symbols:
        values = by_symbol.get(symbol) or {}
        closed_deals = int(values.get("closed_deals") or 0)
        net_profit = float(values.get("net_profit") or 0.0)
        losing_streak = int(values.get("recent_losing_streak") or 0)
        missing_closed_deals = max(required_closed_deals - closed_deals, 0)
        blockers: list[str] = []
        if not quality_gate_enabled:
            status = "not_evaluated"
            action = "use_global_scale_gate"
        else:
            if missing_closed_deals > 0:
                blockers.append(
                    f"need {missing_closed_deals} more closed deals for symbol quality gate"
                )
            if (
                scaling.min_deal_net_profit_for_scale is not None
                and closed_deals > 0
                and net_profit < scaling.min_deal_net_profit_for_scale
            ):
                blockers.append(
                    "symbol closed deal net profit "
                    f"{net_profit:.2f} below scale minimum {scaling.min_deal_net_profit_for_scale:.2f}"
                )
            if (
                scaling.max_losing_streak_for_scale > 0
                and losing_streak >= scaling.max_losing_streak_for_scale
            ):
                blockers.append(
                    f"symbol losing streak {losing_streak} reached scale limit "
                    f"{scaling.max_losing_streak_for_scale}"
                )
            if any("losing streak" in item for item in blockers):
                status = "hold"
                action = "throttle_or_pause"
            elif any("net profit" in item for item in blockers):
                status = "hold"
                action = "keep_base_risk"
            elif missing_closed_deals > 0:
                status = "collect_more_evidence"
                action = "keep_base_risk"
            else:
                status = "eligible_for_scale"
                action = "allow_scaled_risk"
        posture[symbol] = {
            "status": status,
            "action": action,
            "closed_deals": closed_deals,
            "required_closed_deals": required_closed_deals,
            "missing_closed_deals": missing_closed_deals,
            "net_profit": round(net_profit, 2),
            "recent_losing_streak": losing_streak,
            "blockers": blockers,
        }
    return posture


def _floating_drawdown_pct(account: AccountState) -> float:
    if account.balance <= 0 or account.equity >= account.balance:
        return 0.0
    return (account.balance - account.equity) / account.balance * 100
