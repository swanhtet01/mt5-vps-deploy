from __future__ import annotations

import math
from dataclasses import replace

from mt5_agent.config import AgentConfig, ProfitScalingStep
from mt5_agent.models import AccountState, InstrumentRules, OrderProposal, RiskDecision, Signal, Tick
from mt5_agent.profit_funded_scaling import (
    evaluate_account_guard,
    evaluate_profit_funded_scaling,
)


class RiskManager:
    def __init__(
        self,
        config: AgentConfig,
        scale_live_orders: int | None = None,
        scale_closed_deals: int | None = None,
        scale_deal_net_profit: float | None = None,
        scale_recent_losing_streak: int | None = None,
        scale_deal_nets: list[float] | None = None,
        scale_symbol_deal_nets: dict[str, list[float]] | None = None,
        scale_account_deal_nets: list[float] | None = None,
        scale_symbol_closed_deals: dict[str, int] | None = None,
        scale_symbol_deal_net_profit: dict[str, float] | None = None,
        scale_symbol_recent_losing_streak: dict[str, int] | None = None,
        recent_symbol_closed_deals: dict[str, int] | None = None,
        recent_symbol_deal_net_profit: dict[str, float] | None = None,
        recent_symbol_recent_losing_streak: dict[str, int] | None = None,
    ) -> None:
        self.config = config
        self.scale_live_orders = scale_live_orders
        self.scale_closed_deals = scale_closed_deals
        self.scale_deal_net_profit = scale_deal_net_profit
        self.scale_recent_losing_streak = scale_recent_losing_streak
        self.scale_deal_nets = scale_deal_nets
        self.scale_symbol_deal_nets = scale_symbol_deal_nets
        self.scale_account_deal_nets = scale_account_deal_nets
        self.scale_symbol_closed_deals = scale_symbol_closed_deals
        self.scale_symbol_deal_net_profit = scale_symbol_deal_net_profit
        self.scale_symbol_recent_losing_streak = scale_symbol_recent_losing_streak
        self.recent_symbol_closed_deals = recent_symbol_closed_deals
        self.recent_symbol_deal_net_profit = recent_symbol_deal_net_profit
        self.recent_symbol_recent_losing_streak = recent_symbol_recent_losing_streak
        self._profit_funded_account_cap_cache = (
            float(evaluate_account_guard(scale_account_deal_nets)["multiplier_cap"])
            if scale_account_deal_nets is not None
            else 3.0
        )
        self._profit_funded_global_multiplier_cache = (
            float(evaluate_profit_funded_scaling(scale_deal_nets)["lot_multiplier"])
            if scale_deal_nets is not None
            else None
        )
        self._profit_funded_symbol_multiplier_cache = (
            {
                symbol: float(
                    evaluate_profit_funded_scaling(nets)["lot_multiplier"]
                )
                for symbol, nets in scale_symbol_deal_nets.items()
            }
            if scale_symbol_deal_nets is not None
            else None
        )

    def assess(
        self,
        account: AccountState,
        rules: InstrumentRules,
        tick: Tick,
        signal: Signal,
        open_positions_count: int,
    ) -> RiskDecision:
        if signal.side == "flat":
            return RiskDecision(False, signal.reason)
        min_confidence = self.config.strategy_for(signal.symbol).min_confidence
        if signal.confidence < min_confidence:
            return RiskDecision(False, f"confidence {signal.confidence:.2f} below threshold")
        if open_positions_count >= self.config.max_positions:
            return RiskDecision(False, "max positions reached")
        if self._enforce_live_safety():
            allowed_symbols = self.config.live_safety.allowed_symbols
            if allowed_symbols and signal.symbol not in allowed_symbols:
                return RiskDecision(False, "symbol not allowed for live trading")
        max_spread_points = self._risk_value(signal.symbol, "max_spread_points", account)
        spread_limit_tolerance = max(abs(max_spread_points) * 1e-9, 1e-9)
        if tick.spread_points - max_spread_points > spread_limit_tolerance:
            return RiskDecision(False, f"spread {tick.spread_points:.1f} points too high")
        stop_distance = float(signal.stop_distance)
        take_profit_distance = float(signal.take_profit_distance)
        if stop_distance <= 0:
            return RiskDecision(False, "invalid stop distance")
        min_atr_points = self._risk_value(signal.symbol, "min_atr_points", account)
        if rules.point > 0 and stop_distance / rules.point < min_atr_points:
            return RiskDecision(False, "stop distance below minimum ATR points")
        spread_distance = tick.spread_points * tick.point
        max_spread_to_stop_ratio = self._risk_value(signal.symbol, "max_spread_to_stop_ratio", account)
        reason = "risk checks passed"
        if stop_distance > 0 and spread_distance / stop_distance > max_spread_to_stop_ratio:
            expanded = self._spread_expanded_distances(
                account,
                signal,
                spread_distance,
                max_spread_to_stop_ratio,
            )
            if expanded is None:
                return RiskDecision(
                    False,
                    f"spread-to-stop ratio {spread_distance / stop_distance:.2f} exceeds {max_spread_to_stop_ratio:.2f}",
                )
            stop_distance, take_profit_distance = expanded
            reason = "risk checks passed with spread-aware stop expansion"

        daily_loss_limit = -account.equity * (self.config.risk.max_daily_loss_pct / 100)
        if account.daily_realized_pnl <= daily_loss_limit:
            return RiskDecision(False, "daily loss kill switch triggered")

        if self._profit_scaling_drawdown_paused(account):
            return RiskDecision(False, "profit scaling floating drawdown pause")

        max_open_risk = self.max_total_open_risk_limit(account, signal.symbol)
        if account.open_risk >= max_open_risk:
            return RiskDecision(False, "total open risk limit reached")
        remaining_open_risk = max(max_open_risk - account.open_risk, 0.0)

        volume = self._position_size(account, rules, stop_distance, remaining_open_risk)
        if volume < rules.min_lot:
            if self._minimum_lot_risk(rules, stop_distance) > remaining_open_risk:
                return RiskDecision(False, "remaining open risk below broker minimum")
            return RiskDecision(False, "calculated volume below broker minimum")
        if self._enforce_live_safety():
            max_live_volume = self._max_live_volume(signal.symbol, account)
            if volume > max_live_volume:
                if self._allow_downsize_to_live_cap(signal.symbol) and max_live_volume >= rules.min_lot:
                    volume = max_live_volume
                    reason = "risk checks passed with live cap downsize"
                else:
                    return RiskDecision(False, f"volume {volume} exceeds live micro cap {max_live_volume}")
        min_take_profit_usd = self._risk_value(signal.symbol, "min_take_profit_usd", account)
        if min_take_profit_usd > 0:
            planned_reward = self.planned_order_reward(rules, volume, take_profit_distance)
            if planned_reward < min_take_profit_usd:
                upsized_volume = self._min_take_profit_upsize_volume(
                    account,
                    rules,
                    signal.symbol,
                    volume,
                    stop_distance,
                    take_profit_distance,
                    min_take_profit_usd,
                    remaining_open_risk,
                )
                if upsized_volume is not None:
                    volume = upsized_volume
                    planned_reward = self.planned_order_reward(rules, volume, take_profit_distance)
                    reason = self._append_reason(reason, "minimum reward upsize")
            if planned_reward < min_take_profit_usd:
                return RiskDecision(
                    False,
                    f"take-profit value {planned_reward:.2f} below target {min_take_profit_usd:.2f}",
                    volume,
                )
        return RiskDecision(True, reason, volume, stop_distance, take_profit_distance)

    def _spread_expanded_distances(
        self,
        account: AccountState,
        signal: Signal,
        spread_distance: float,
        max_spread_to_stop_ratio: float,
    ) -> tuple[float, float] | None:
        max_expansion = self._risk_value(signal.symbol, "max_stop_expansion_multiple", account)
        min_confidence = self._risk_value(signal.symbol, "min_confidence_for_stop_expansion", account)
        if max_expansion <= 1 or signal.confidence < min_confidence:
            return None
        if signal.stop_distance <= 0 or signal.take_profit_distance <= 0 or max_spread_to_stop_ratio <= 0:
            return None

        required_stop_distance = spread_distance / max_spread_to_stop_ratio
        if required_stop_distance > signal.stop_distance * max_expansion:
            return None
        reward_to_risk = signal.take_profit_distance / signal.stop_distance
        return required_stop_distance, required_stop_distance * reward_to_risk

    def _min_take_profit_upsize_volume(
        self,
        account: AccountState,
        rules: InstrumentRules,
        symbol: str,
        current_volume: float,
        stop_distance: float,
        take_profit_distance: float,
        min_take_profit_usd: float,
        remaining_open_risk: float,
    ) -> float | None:
        if not self._allow_upsize_to_min_take_profit(symbol):
            return None
        if rules.tick_size <= 0 or rules.tick_value <= 0 or rules.lot_step <= 0 or take_profit_distance <= 0:
            return None
        reward_per_lot = (take_profit_distance / rules.tick_size) * rules.tick_value
        if reward_per_lot <= 0:
            return None

        required_volume = min_take_profit_usd / reward_per_lot
        decimals = max(0, len(str(rules.lot_step).split(".")[-1]))
        stepped_volume = math.ceil((required_volume - 1e-12) / rules.lot_step) * rules.lot_step
        stepped_volume = round(max(stepped_volume, rules.min_lot), decimals)
        if stepped_volume <= current_volume:
            return None

        max_allowed_volume = rules.max_lot
        if self._enforce_live_safety():
            max_allowed_volume = min(max_allowed_volume, self._max_live_volume(symbol, account))
        if stepped_volume > max_allowed_volume:
            return None

        planned_risk = self.planned_order_risk(rules, stepped_volume, stop_distance)
        fixed_cap = self._effective_max_trade_risk_usd(account, symbol)
        if fixed_cap is None:
            return None
        effective_fixed_cap = float(fixed_cap) * self._symbol_quality_risk_factor(symbol)
        if planned_risk > effective_fixed_cap or planned_risk > remaining_open_risk:
            return None
        return stepped_volume

    def _allow_upsize_to_min_take_profit(self, symbol: str) -> bool:
        override = self.config.symbol_risk.get(symbol)
        if override is not None and override.upsize_to_min_take_profit is not None:
            return bool(override.upsize_to_min_take_profit)
        return bool(self.config.risk.upsize_to_min_take_profit)

    def _append_reason(self, reason: str, modifier: str) -> str:
        if reason == "risk checks passed":
            return f"{reason} with {modifier}"
        return f"{reason} and {modifier}"

    def signal_for_decision(self, signal: Signal, decision: RiskDecision) -> Signal:
        if not decision.allowed or decision.stop_distance <= 0 or decision.take_profit_distance <= 0:
            return signal
        if (
            math.isclose(decision.stop_distance, signal.stop_distance)
            and math.isclose(decision.take_profit_distance, signal.take_profit_distance)
        ):
            return signal
        metadata = dict(signal.metadata)
        metadata.update(
            {
                "spread_stop_expanded": True,
                "original_stop_distance": signal.stop_distance,
                "original_take_profit_distance": signal.take_profit_distance,
                "adjusted_stop_distance": decision.stop_distance,
                "adjusted_take_profit_distance": decision.take_profit_distance,
            }
        )
        return replace(
            signal,
            stop_distance=decision.stop_distance,
            take_profit_distance=decision.take_profit_distance,
            metadata=metadata,
        )

    def build_order(self, tick: Tick, signal: Signal, volume: float) -> OrderProposal:
        if signal.side not in {"buy", "sell"}:
            raise ValueError("signal must be buy or sell")
        price = tick.ask if signal.side == "buy" else tick.bid
        if signal.side == "buy":
            stop_loss = price - signal.stop_distance
            take_profit = price + signal.take_profit_distance
        else:
            stop_loss = price + signal.stop_distance
            take_profit = price - signal.take_profit_distance
        return OrderProposal(
            symbol=signal.symbol,
            side=signal.side,
            volume=volume,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            comment="mt5-agent trend-breakout",
        )

    def _position_size(
        self,
        account: AccountState,
        rules: InstrumentRules,
        stop_distance: float,
        remaining_open_risk: float | None = None,
    ) -> float:
        risk_amount = self._risk_amount(account, rules.symbol)
        if remaining_open_risk is not None:
            risk_amount = min(risk_amount, remaining_open_risk)
        if rules.tick_size <= 0 or rules.tick_value <= 0:
            return 0.0
        loss_per_lot = (stop_distance / rules.tick_size) * rules.tick_value
        if loss_per_lot <= 0:
            return 0.0
        raw_volume = risk_amount / loss_per_lot
        if raw_volume < rules.min_lot:
            return 0.0
        stepped = math.floor(raw_volume / rules.lot_step) * rules.lot_step
        bounded = min(stepped, rules.max_lot)
        decimals = max(0, len(str(rules.lot_step).split(".")[-1]))
        return round(bounded, decimals)

    def _minimum_lot_risk(self, rules: InstrumentRules, stop_distance: float) -> float:
        if rules.tick_size <= 0 or rules.tick_value <= 0:
            return float("inf")
        loss_per_lot = (stop_distance / rules.tick_size) * rules.tick_value
        if loss_per_lot <= 0:
            return float("inf")
        return loss_per_lot * rules.min_lot

    def _risk_value(self, symbol: str, field_name: str, account: AccountState | None = None) -> float:
        step = self._active_profit_scaling_step(account, symbol) if account is not None else None
        if field_name == "risk_per_trade_pct" and step is not None:
            symbol_value = step.symbol_risk_per_trade_pct.get(symbol)
            if symbol_value is not None:
                return self._apply_loss_recovery_factor(float(symbol_value), field_name, account)
        override = self.config.symbol_risk.get(symbol)
        if override is not None and hasattr(override, field_name):
            value = getattr(override, field_name)
            if value is not None:
                return self._apply_loss_recovery_factor(float(value), field_name, account)
        if step is not None and hasattr(step, field_name):
            value = getattr(step, field_name)
            if value is not None:
                return self._apply_loss_recovery_factor(float(value), field_name, account)
        return self._apply_loss_recovery_factor(float(getattr(self.config.risk, field_name)), field_name, account)

    def _risk_amount(self, account: AccountState, symbol: str) -> float:
        risk_pct = self._risk_value(symbol, "risk_per_trade_pct", account)
        quality_factor = self._symbol_quality_risk_factor(symbol)
        percent_amount = account.equity * (risk_pct / 100) * quality_factor
        fixed_cap = self._effective_max_trade_risk_usd(account, symbol)
        if fixed_cap is None:
            return percent_amount
        return min(percent_amount, float(fixed_cap) * quality_factor)

    def _max_live_volume(self, symbol: str, account: AccountState | None = None) -> float:
        global_cap = self._effective_live_safety_max_order_volume(account, symbol)
        step = self._active_profit_scaling_step(account, symbol) if account is not None else None
        if step is not None:
            symbol_cap = step.symbol_max_order_volume.get(symbol)
            if symbol_cap is not None:
                return min(float(symbol_cap), global_cap)
        override = self.config.symbol_risk.get(symbol)
        if override is not None and override.max_order_volume is not None:
            return min(float(override.max_order_volume), global_cap)
        return global_cap

    def _allow_downsize_to_live_cap(self, symbol: str) -> bool:
        override = self.config.symbol_risk.get(symbol)
        return bool(override is not None and override.downsize_to_max_order_volume)

    def live_volume_cap(self, symbol: str, account: AccountState | None = None) -> float:
        return self._max_live_volume(symbol, account)

    def _enforce_live_safety(self) -> bool:
        return self.config.live or self.config.live_safety.enforce_in_paper

    def planned_order_risk(self, rules: InstrumentRules, volume: float, stop_distance: float) -> float:
        if rules.tick_size <= 0 or rules.tick_value <= 0:
            return float("inf")
        return (stop_distance / rules.tick_size) * rules.tick_value * volume

    def planned_order_reward(self, rules: InstrumentRules, volume: float, take_profit_distance: float) -> float:
        if rules.tick_size <= 0 or rules.tick_value <= 0:
            return float("inf")
        return (take_profit_distance / rules.tick_size) * rules.tick_value * volume

    def max_new_trade_risk(self, account: AccountState, symbol: str) -> float:
        per_trade = self._risk_amount(account, symbol)
        total_open = self.max_total_open_risk_limit(account, symbol)
        remaining_open = max(total_open - account.open_risk, 0.0)
        return min(per_trade, remaining_open)

    def max_total_open_risk_limit(self, account: AccountState, symbol: str) -> float:
        max_total_open_risk_pct = self._risk_value(symbol, "max_total_open_risk_pct", account)
        return account.equity * (max_total_open_risk_pct / 100)

    def effective_risk_snapshot(self, account: AccountState, symbol: str | None = None) -> dict:
        symbol_for_risk = symbol or (self.config.symbols[0] if self.config.symbols else "")
        scaling = self.config.profit_scaling
        step = self._active_profit_scaling_step(account, symbol_for_risk)
        return {
            "profit_scaling_enabled": scaling.enabled,
            "baseline_balance": round(scaling.baseline_balance, 2),
            "realized_profit": round(self._profit_scaling_realized_profit(account), 2),
            "active_profit_step_usd": round(step.profit_usd, 2) if step is not None else None,
            "risk_per_trade_pct": round(self._risk_value(symbol_for_risk, "risk_per_trade_pct", account), 4),
            "max_trade_risk_usd": self._effective_max_trade_risk_usd(account, symbol_for_risk),
            "max_total_open_risk_pct": round(
                self._risk_value(symbol_for_risk, "max_total_open_risk_pct", account),
                4,
            ),
            "live_max_order_volume": self._max_live_volume(symbol_for_risk, account),
            "floating_drawdown_pct": round(self._floating_drawdown_pct(account), 4),
            "drawdown_pause_pct": scaling.drawdown_pause_pct,
            "loss_recovery_active": self._loss_recovery_active(account),
            "loss_recovery_risk_factor": self._loss_recovery_factor(account),
            "symbol_quality_window_days": scaling.symbol_quality_window_days,
            "symbol_quality_trade_action": scaling.symbol_quality_trade_action,
            "symbol_quality_risk_factor": self._symbol_quality_risk_factor(symbol_for_risk),
            "symbol_quality_risk_reduced": self._symbol_quality_risk_reduced(symbol_for_risk),
            "min_live_orders_for_scale": scaling.min_live_orders_for_scale,
            "min_deal_net_profit_for_scale": scaling.min_deal_net_profit_for_scale,
            "max_losing_streak_for_scale": scaling.max_losing_streak_for_scale,
            "require_symbol_closed_deal_quality_for_scale": (
                scaling.require_symbol_closed_deal_quality_for_scale
            ),
            "scale_live_orders": self.scale_live_orders,
            "scale_closed_deals": self.scale_closed_deals,
            "scale_deal_net_profit": self.scale_deal_net_profit,
            "scale_recent_losing_streak": self.scale_recent_losing_streak,
            "profit_funded_policy_multiplier": self._profit_funded_policy_multiplier(
                symbol_for_risk
            ),
            "profit_funded_account_multiplier_cap": self._profit_funded_account_cap(),
            "scale_symbol_closed_deals": (
                None
                if self.scale_symbol_closed_deals is None
                else self.scale_symbol_closed_deals.get(symbol_for_risk, 0)
            ),
            "scale_symbol_deal_net_profit": (
                None
                if self.scale_symbol_deal_net_profit is None
                else self.scale_symbol_deal_net_profit.get(symbol_for_risk, 0.0)
            ),
            "scale_symbol_recent_losing_streak": (
                None
                if self.scale_symbol_recent_losing_streak is None
                else self.scale_symbol_recent_losing_streak.get(symbol_for_risk, 0)
            ),
            "recent_symbol_closed_deals": (
                None
                if self.recent_symbol_closed_deals is None
                else self.recent_symbol_closed_deals.get(symbol_for_risk, 0)
            ),
            "recent_symbol_deal_net_profit": (
                None
                if self.recent_symbol_deal_net_profit is None
                else self.recent_symbol_deal_net_profit.get(symbol_for_risk, 0.0)
            ),
            "recent_symbol_recent_losing_streak": (
                None
                if self.recent_symbol_recent_losing_streak is None
                else self.recent_symbol_recent_losing_streak.get(symbol_for_risk, 0)
            ),
            "floating_drawdown_paused": self._profit_scaling_drawdown_paused(account),
        }

    def _effective_max_trade_risk_usd(self, account: AccountState | None, symbol: str | None = None) -> float | None:
        step = self._active_profit_scaling_step(account, symbol) if account is not None else None
        if step is not None and step.max_trade_risk_usd is not None:
            return self._apply_loss_recovery_factor(float(step.max_trade_risk_usd), "max_trade_risk_usd", account)
        if self.config.risk.max_trade_risk_usd is None:
            return None
        return self._apply_loss_recovery_factor(float(self.config.risk.max_trade_risk_usd), "max_trade_risk_usd", account)

    def _effective_live_safety_max_order_volume(
        self,
        account: AccountState | None,
        symbol: str | None = None,
    ) -> float:
        step = self._active_profit_scaling_step(account, symbol) if account is not None else None
        if step is not None and step.live_max_order_volume is not None:
            return self._apply_loss_recovery_factor(float(step.live_max_order_volume), "live_max_order_volume", account)
        return self._apply_loss_recovery_factor(
            float(self.config.live_safety.max_order_volume),
            "live_max_order_volume",
            account,
        )

    def _active_profit_scaling_step(
        self,
        account: AccountState | None,
        symbol: str | None = None,
    ) -> ProfitScalingStep | None:
        scaling = self.config.profit_scaling
        if account is None or not scaling.enabled:
            return None
        realized_profit = self._profit_scaling_realized_profit(account)
        active_step: ProfitScalingStep | None = None
        for step in scaling.steps:
            if realized_profit >= step.profit_usd:
                active_step = step
            else:
                break
        if self._scale_evidence_missing(active_step, symbol):
            return self._base_profit_scaling_step()
        policy_multiplier = self._profit_funded_policy_multiplier(symbol)
        if policy_multiplier is not None:
            return self._policy_capped_step(active_step, policy_multiplier)
        return active_step

    def _profit_funded_policy_multiplier(self, symbol: str | None = None) -> float | None:
        if self._profit_funded_global_multiplier_cache is None:
            return None
        multiplier = self._profit_funded_global_multiplier_cache
        if symbol is not None and self._profit_funded_symbol_multiplier_cache is not None:
            multiplier = min(
                multiplier,
                self._profit_funded_symbol_multiplier_cache.get(symbol, 1.0),
            )
        multiplier = min(multiplier, self._profit_funded_account_cap())
        return multiplier

    def _profit_funded_account_cap(self) -> float:
        return self._profit_funded_account_cap_cache

    def _policy_capped_step(
        self,
        step: ProfitScalingStep,
        multiplier: float,
    ) -> ProfitScalingStep:
        base = self._base_profit_scaling_step()
        if base is None:
            return step

        def capped(value: float | None, reference: float | None) -> float | None:
            if value is None:
                return None
            if reference is None:
                return float(value)
            return min(float(value), float(reference) * multiplier)

        risk_reference = (
            base.risk_per_trade_pct
            if base.risk_per_trade_pct is not None
            else self.config.risk.risk_per_trade_pct
        )
        total_reference = (
            base.max_total_open_risk_pct
            if base.max_total_open_risk_pct is not None
            else self.config.risk.max_total_open_risk_pct
        )
        trade_reference = (
            base.max_trade_risk_usd
            if base.max_trade_risk_usd is not None
            else self.config.risk.max_trade_risk_usd
        )
        volume_reference = (
            base.live_max_order_volume
            if base.live_max_order_volume is not None
            else self.config.live_safety.max_order_volume
        )
        symbol_risk = {
            symbol: min(
                float(value),
                float(
                    base.symbol_risk_per_trade_pct.get(
                        symbol,
                        self.config.symbol_risk.get(symbol).risk_per_trade_pct
                        if self.config.symbol_risk.get(symbol) is not None
                        and self.config.symbol_risk.get(symbol).risk_per_trade_pct is not None
                        else self.config.risk.risk_per_trade_pct,
                    )
                )
                * multiplier,
            )
            for symbol, value in step.symbol_risk_per_trade_pct.items()
        }
        symbol_volume = {
            symbol: min(
                float(value),
                float(
                    base.symbol_max_order_volume.get(
                        symbol,
                        self.config.symbol_risk.get(symbol).max_order_volume
                        if self.config.symbol_risk.get(symbol) is not None
                        and self.config.symbol_risk.get(symbol).max_order_volume is not None
                        else self.config.live_safety.max_order_volume,
                    )
                )
                * multiplier,
            )
            for symbol, value in step.symbol_max_order_volume.items()
        }
        return replace(
            step,
            risk_per_trade_pct=capped(step.risk_per_trade_pct, risk_reference),
            max_trade_risk_usd=capped(step.max_trade_risk_usd, trade_reference),
            max_total_open_risk_pct=capped(step.max_total_open_risk_pct, total_reference),
            live_max_order_volume=capped(step.live_max_order_volume, volume_reference),
            symbol_risk_per_trade_pct=symbol_risk,
            symbol_max_order_volume=symbol_volume,
        )

    def _apply_loss_recovery_factor(
        self,
        value: float,
        field_name: str,
        account: AccountState | None,
    ) -> float:
        if field_name not in {
            "risk_per_trade_pct",
            "max_trade_risk_usd",
            "max_total_open_risk_pct",
            "live_max_order_volume",
        }:
            return value
        return value * self._loss_recovery_factor(account)

    def _loss_recovery_factor(self, account: AccountState | None) -> float:
        if account is None or not self._loss_recovery_active(account):
            return 1.0
        return float(self.config.profit_scaling.loss_recovery_risk_factor)

    def _loss_recovery_active(self, account: AccountState | None) -> bool:
        scaling = self.config.profit_scaling
        return bool(
            account is not None
            and scaling.enabled
            and scaling.baseline_balance > 0
            and self._profit_scaling_realized_profit(account) < 0
            and scaling.loss_recovery_risk_factor < 1
        )

    def _symbol_quality_risk_factor(self, symbol: str | None) -> float:
        scaling = self.config.profit_scaling
        if not self._symbol_quality_risk_reduced(symbol):
            return 1.0
        return float(scaling.symbol_quality_risk_factor)

    def _symbol_quality_risk_reduced(self, symbol: str | None) -> bool:
        scaling = self.config.profit_scaling
        if (
            symbol is None
            or not scaling.enabled
            or not scaling.require_symbol_closed_deal_quality_for_scale
            or scaling.symbol_quality_trade_action != "scale_only"
            or scaling.symbol_quality_risk_factor >= 1
        ):
            return False
        if self._recent_symbol_quality_recovered(symbol):
            return False
        quality_gate_enabled = (
            scaling.min_deal_net_profit_for_scale is not None
            or scaling.max_losing_streak_for_scale > 0
        )
        if not quality_gate_enabled:
            return False
        required_closed_deals = max(scaling.min_live_orders_for_scale, 1)
        if self.scale_symbol_closed_deals is None:
            return False
        symbol_closed_deals = int(self.scale_symbol_closed_deals.get(symbol, 0))
        if symbol_closed_deals < required_closed_deals:
            return False
        if scaling.min_deal_net_profit_for_scale is not None:
            if self.scale_symbol_deal_net_profit is None:
                return False
            symbol_net_profit = float(self.scale_symbol_deal_net_profit.get(symbol, 0.0))
            if symbol_net_profit < scaling.min_deal_net_profit_for_scale:
                return True
        if scaling.max_losing_streak_for_scale > 0:
            if self.scale_symbol_recent_losing_streak is None:
                return False
            symbol_losing_streak = int(self.scale_symbol_recent_losing_streak.get(symbol, 0))
            if symbol_losing_streak >= scaling.max_losing_streak_for_scale:
                return True
        return False

    def _recent_symbol_quality_recovered(self, symbol: str) -> bool:
        scaling = self.config.profit_scaling
        required_closed_deals = max(min(scaling.min_live_orders_for_scale, 2), 1)
        if self.recent_symbol_closed_deals is None:
            return False
        recent_closed_deals = int(self.recent_symbol_closed_deals.get(symbol, 0))
        if recent_closed_deals < required_closed_deals:
            return False
        if self.recent_symbol_deal_net_profit is None:
            return False
        recent_net_profit = float(self.recent_symbol_deal_net_profit.get(symbol, 0.0))
        min_net_profit = (
            float(scaling.min_deal_net_profit_for_scale)
            if scaling.min_deal_net_profit_for_scale is not None
            else 0.0
        )
        if recent_net_profit < min_net_profit:
            return False
        if scaling.max_losing_streak_for_scale > 0 and self.recent_symbol_recent_losing_streak is not None:
            recent_losing_streak = int(self.recent_symbol_recent_losing_streak.get(symbol, 0))
            if recent_losing_streak >= scaling.max_losing_streak_for_scale:
                return False
        return True

    def _scale_evidence_missing(self, step: ProfitScalingStep | None, symbol: str | None = None) -> bool:
        scaling = self.config.profit_scaling
        if step is None or step.profit_usd <= 0:
            return False
        if self.scale_deal_nets is not None:
            if self._base_profit_scaling_step() is None:
                return True
            if self._profit_funded_policy_multiplier(symbol) <= 1.0:
                return True
        if (
            scaling.min_live_orders_for_scale > 0
            and self.scale_live_orders is not None
            and self.scale_live_orders < scaling.min_live_orders_for_scale
        ):
            return True
        quality_gate_enabled = (
            scaling.min_deal_net_profit_for_scale is not None
            or scaling.max_losing_streak_for_scale > 0
        )
        if (
            quality_gate_enabled
            and self.scale_closed_deals is not None
            and self.scale_closed_deals < max(scaling.min_live_orders_for_scale, 1)
        ):
            return True
        if (
            scaling.min_deal_net_profit_for_scale is not None
            and self.scale_deal_net_profit is not None
            and self.scale_deal_net_profit < scaling.min_deal_net_profit_for_scale
        ):
            return True
        if (
            scaling.max_losing_streak_for_scale > 0
            and self.scale_recent_losing_streak is not None
            and self.scale_recent_losing_streak >= scaling.max_losing_streak_for_scale
        ):
            return True
        return self._symbol_scale_evidence_missing(symbol)

    def _symbol_scale_evidence_missing(self, symbol: str | None) -> bool:
        scaling = self.config.profit_scaling
        if not scaling.require_symbol_closed_deal_quality_for_scale or symbol is None:
            return False
        quality_gate_enabled = (
            scaling.min_deal_net_profit_for_scale is not None
            or scaling.max_losing_streak_for_scale > 0
        )
        if not quality_gate_enabled:
            return False
        required_closed_deals = max(scaling.min_live_orders_for_scale, 1)
        if self.scale_symbol_closed_deals is None:
            return True
        symbol_closed_deals = int(self.scale_symbol_closed_deals.get(symbol, 0))
        if symbol_closed_deals < required_closed_deals:
            return True
        if scaling.min_deal_net_profit_for_scale is not None:
            if self.scale_symbol_deal_net_profit is None:
                return True
            symbol_net_profit = float(self.scale_symbol_deal_net_profit.get(symbol, 0.0))
            if symbol_net_profit < scaling.min_deal_net_profit_for_scale:
                return True
        if scaling.max_losing_streak_for_scale > 0:
            if self.scale_symbol_recent_losing_streak is None:
                return True
            symbol_losing_streak = int(self.scale_symbol_recent_losing_streak.get(symbol, 0))
            if symbol_losing_streak >= scaling.max_losing_streak_for_scale:
                return True
        return False

    def _base_profit_scaling_step(self) -> ProfitScalingStep | None:
        for step in self.config.profit_scaling.steps:
            if step.profit_usd <= 0:
                return step
        return None

    def _profit_scaling_realized_profit(self, account: AccountState) -> float:
        return account.balance - self.config.profit_scaling.baseline_balance

    def _profit_scaling_drawdown_paused(self, account: AccountState) -> bool:
        scaling = self.config.profit_scaling
        return (
            scaling.enabled
            and scaling.drawdown_pause_pct > 0
            and self._floating_drawdown_pct(account) >= scaling.drawdown_pause_pct
        )

    def _floating_drawdown_pct(self, account: AccountState) -> float:
        if account.balance <= 0 or account.equity >= account.balance:
            return 0.0
        return (account.balance - account.equity) / account.balance * 100
