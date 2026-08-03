from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_BLACKLIST_FILE = Path("data_cache/blacklist.json")


def _read_blacklist_entries() -> list[dict]:
    """Read the auto-blacklist written by scripts/self_improver.py. Returns [] on any error
    (fail-open: a missing or broken file does NOT block trades, only an active entry does)."""
    if not _BLACKLIST_FILE.exists():
        return []
    try:
        return json.loads(_BLACKLIST_FILE.read_text(encoding="utf-8")).get("entries", []) or []
    except (json.JSONDecodeError, OSError, ValueError):
        return []

from mt5_agent.broker import Broker
from mt5_agent.config import AgentConfig
from mt5_agent.deals import summarize_closed_deals, summarize_closed_deals_for_day
from mt5_agent.models import DealSnapshot
from mt5_agent.management import PositionManager
from mt5_agent.news import load_news_state, news_block_reason
from mt5_agent.risk import RiskManager
from mt5_agent.state import DailyRiskState, PositionRiskState
from mt5_agent.strategy import TrendBreakoutStrategy


class TradingAgent:
    def __init__(
        self,
        config: AgentConfig,
        broker: Broker,
        daily_risk: DailyRiskState | None = None,
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
        position_risk_state: PositionRiskState | None = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.risk = RiskManager(
            config,
            scale_live_orders=scale_live_orders,
            scale_closed_deals=scale_closed_deals,
            scale_deal_net_profit=scale_deal_net_profit,
            scale_recent_losing_streak=scale_recent_losing_streak,
            scale_deal_nets=scale_deal_nets,
            scale_symbol_deal_nets=scale_symbol_deal_nets,
            scale_account_deal_nets=scale_account_deal_nets,
            scale_symbol_closed_deals=scale_symbol_closed_deals,
            scale_symbol_deal_net_profit=scale_symbol_deal_net_profit,
            scale_symbol_recent_losing_streak=scale_symbol_recent_losing_streak,
            recent_symbol_closed_deals=recent_symbol_closed_deals,
            recent_symbol_deal_net_profit=recent_symbol_deal_net_profit,
            recent_symbol_recent_losing_streak=recent_symbol_recent_losing_streak,
        )
        self.position_manager = PositionManager(config, broker, position_risk_state=position_risk_state)
        self.daily_risk = daily_risk or DailyRiskState()

    def run_once(self, dry_run: bool = True, enforce_daily_stop: bool = True) -> list[dict[str, Any]]:
        now = datetime.now()
        utc_now = datetime.now(tz=timezone.utc)
        account = self.broker.account()
        effective_dry_run = dry_run or not self.config.live
        results: list[dict[str, Any]] = self.position_manager.manage_open_positions(effective_dry_run)
        deal_history: list[DealSnapshot] = []
        needs_deal_history = enforce_daily_stop or self._needs_deal_history_for_symbol_gates()
        daily_status = (
            self.daily_risk.check(
                account.equity,
                self.config.risk.max_daily_loss_pct,
                profit_lock_trigger_usd=self.config.risk.daily_profit_lock_trigger_usd,
                profit_lock_giveback_pct=self.config.risk.daily_profit_lock_giveback_pct,
            )
            if enforce_daily_stop
            else None
        )
        if daily_status is not None and not daily_status.allowed:
            return results + [
                {
                    "event": "blocked",
                    "signal": "flat",
                    "reason": daily_status.reason,
                    "baseline_equity": daily_status.baseline_equity,
                    "current_equity": daily_status.current_equity,
                    "max_loss_amount": daily_status.max_loss_amount,
                    "peak_equity": daily_status.peak_equity,
                    "profit_lock_floor": daily_status.profit_lock_floor,
                }
            ]
        if needs_deal_history:
            deal_history = self.broker.history_deals(
                magic_number=self.config.magic_number,
                days=self._deal_history_days(enforce_daily_stop),
            )
        if enforce_daily_stop:
            daily_deals = summarize_closed_deals_for_day(
                deal_history,
                magic_number=self.config.magic_number,
                lookahead_hours=12,
            )
            daily_deal_net_profit = float(daily_deals["net_profit"])
            max_loss_amount = account.equity * (self.config.risk.max_daily_loss_pct / 100)
            if int(daily_deals["closed_deals"]) > 0 and daily_deal_net_profit <= -max_loss_amount:
                return results + [
                    {
                        "event": "blocked",
                        "signal": "flat",
                        "reason": (
                            "daily realized deal stop hit: "
                            f"net {daily_deal_net_profit:.2f} <= -{max_loss_amount:.2f}"
                        ),
                        "daily_deal_net_profit": daily_deal_net_profit,
                        "daily_closed_deals": daily_deals["closed_deals"],
                        "max_loss_amount": max_loss_amount,
                    }
                ]

        news_state = (
            load_news_state(Path(self.config.news.state_path)) if self.config.news.enabled else {}
        )
        candidates: list[dict[str, Any]] = []
        blacklist_entries = _read_blacklist_entries()
        for symbol in self.config.symbols:
            if self._is_symbol_disallowed_for_live_loop(symbol):
                results.append(
                    {
                        "symbol": symbol,
                        "event": "blocked",
                        "signal": "flat",
                        "reason": "symbol not allowed for live trading",
                        "allowed_symbols": list(self.config.live_safety.allowed_symbols),
                    }
                )
                continue
            # Shared blacklist with Claude's calendar-edge traders (scripts/self_improver.py).
            # Blocks entries for (symbol, magic) pairs that meet persistent-leak criteria.
            blacklisted = next(
                (
                    e for e in blacklist_entries
                    if e.get("symbol") == symbol and int(e.get("magic", -1)) == self.config.magic_number
                ),
                None,
            )
            if blacklisted is not None:
                results.append(
                    {
                        "symbol": symbol,
                        "event": "blocked",
                        "signal": "flat",
                        "reason": f"auto-blacklisted: {blacklisted.get('reason', 'persistent leak')}",
                        "blacklist_entry": blacklisted,
                    }
                )
                continue
            if not self.config.session.is_symbol_enabled_weekday(symbol, now.weekday()):
                results.append(
                    {
                        "symbol": symbol,
                        "event": "blocked",
                        "signal": "flat",
                        "reason": "session disabled for symbol",
                        "weekday": now.weekday(),
                    }
                )
                continue
            if self.config.session.is_symbol_blocked_utc_hour(symbol, utc_now.hour):
                results.append(
                    {
                        "symbol": symbol,
                        "event": "blocked",
                        "signal": "flat",
                        "reason": f"utc hour {utc_now.hour:02d} disabled for symbol",
                        "utc_hour": f"{utc_now.hour:02d}",
                    }
                )
                continue
            symbol_positions = self.broker.open_positions_count(symbol)
            if symbol_positions >= self.config.live_safety.max_positions_per_symbol:
                results.append(
                    {
                        "symbol": symbol,
                        "event": "blocked",
                        "signal": "flat",
                        "reason": "symbol position cap reached",
                        "open_symbol_positions": symbol_positions,
                        "max_positions_per_symbol": self.config.live_safety.max_positions_per_symbol,
                    }
                )
                continue
            if symbol_positions > 0 and self.config.live_safety.require_symbol_profit_for_additional_position:
                symbol_open_profit = sum(position.profit for position in self.broker.open_positions(symbol))
                min_add_profit = self.config.live_safety.min_symbol_profit_for_additional_position_usd
                if symbol_open_profit < min_add_profit:
                    results.append(
                        {
                            "symbol": symbol,
                            "event": "blocked",
                            "signal": "flat",
                            "reason": (
                                f"existing symbol profit {symbol_open_profit:.2f} "
                                f"below add-on threshold {min_add_profit:.2f}"
                            ),
                            "open_symbol_positions": symbol_positions,
                            "open_symbol_profit": round(symbol_open_profit, 2),
                            "min_symbol_profit_for_additional_position_usd": min_add_profit,
                        }
                    )
                    continue
            cooldown_record = self._loss_cooldown_block(symbol, deal_history, utc_now)
            if cooldown_record is not None:
                results.append(cooldown_record)
                continue
            quality_record = self._symbol_quality_block(symbol, deal_history)
            if quality_record is not None:
                if self._should_block_symbol_quality(quality_record):
                    results.append(quality_record)
                    continue
                symbol_quality_context = self._symbol_quality_context(quality_record)
            else:
                symbol_quality_context = {}
            strategy_config = self.config.strategy_for(symbol)
            strategy = TrendBreakoutStrategy(strategy_config)
            bars = self.broker.bars(symbol, strategy_config.timeframe, strategy_config.bars)
            regime_bars = self.broker.bars(
                symbol,
                strategy_config.regime_timeframe,
                strategy_config.regime_bars,
            )
            signal = strategy.evaluate(symbol, bars, regime_bars)
            if self.config.news.enabled and signal.side in ("buy", "sell"):
                news_reason = news_block_reason(
                    news_state.get(symbol),
                    signal.side,
                    sentiment_threshold=self.config.news.sentiment_threshold,
                    fade_strategy=self.config.news.fade_strategy,
                )
                if news_reason:
                    results.append(
                        {
                            "symbol": symbol,
                            "event": "blocked",
                            "signal": signal.side,
                            "reason": news_reason,
                            "confidence": signal.confidence,
                            "signal_metrics": self._signal_metrics(signal),
                        }
                    )
                    continue
            tick = self.broker.tick(symbol)
            rules = self.broker.rules(symbol)
            open_count = self.broker.open_positions_count()
            decision = self.risk.assess(account, rules, tick, signal, open_count)
            if not decision.allowed:
                quality = self._signal_quality(signal, tick)
                hypothetical_order = self._hypothetical_order(signal, tick, decision.volume, rules)
                results.append(
                    {
                        "symbol": symbol,
                        "event": "blocked",
                        "signal": signal.side,
                        "reason": decision.reason,
                        "confidence": signal.confidence,
                        "signal_metrics": self._signal_metrics(signal),
                        **quality,
                        **symbol_quality_context,
                        **hypothetical_order,
                    }
                )
                continue

            signal = self.risk.signal_for_decision(signal, decision)
            order = self.risk.build_order(tick, signal, decision.volume)
            candidates.append(
                {
                    "symbol": symbol,
                    "signal": signal,
                    "tick": tick,
                    "rules": rules,
                    "order": order,
                    "confidence": signal.confidence,
                    "signal_metrics": self._signal_metrics(signal),
                    "symbol_quality_context": symbol_quality_context,
                }
            )

        orders_this_run = 0
        ranked_candidates = sorted(candidates, key=self._order_candidate_rank, reverse=True)
        for candidate in ranked_candidates:
            symbol = candidate["symbol"]
            signal = candidate["signal"]
            order = candidate["order"]
            quality = self._order_candidate_quality(candidate)
            min_rank_score = self.config.min_candidate_rank_score_for(symbol)
            if float(quality["candidate_rank_score"]) < min_rank_score:
                results.append(
                    {
                        "symbol": symbol,
                        "event": "blocked",
                        "signal": signal.side,
                        "reason": (
                            f"candidate rank score {float(quality['candidate_rank_score']):.4f} "
                            f"below minimum {min_rank_score:.4f}"
                        ),
                        "confidence": signal.confidence,
                        "signal_metrics": candidate["signal_metrics"],
                        **quality,
                    }
                )
                continue
            if orders_this_run >= self.config.live_safety.max_orders_per_run:
                results.append(
                    {
                        "symbol": symbol,
                        "event": "blocked",
                        "signal": signal.side,
                        "reason": "max orders per run reached",
                        "confidence": signal.confidence,
                        "signal_metrics": candidate["signal_metrics"],
                        **quality,
                    }
                )
                continue
            sent = self.broker.send_order(order, self.config.magic_number, effective_dry_run)
            orders_this_run += 1
            results.append(
                {
                    "symbol": symbol,
                    "event": "order_sent" if self.config.live and not dry_run else "paper_order",
                    "side": order.side,
                    "volume": order.volume,
                    "price": order.price,
                    "stop_loss": order.stop_loss,
                    "take_profit": order.take_profit,
                    "broker_result": sent,
                    "signal_metrics": candidate["signal_metrics"],
                    **quality,
                }
            )
        return results

    def _order_candidate_rank(self, candidate: dict[str, Any]) -> tuple[float, float, float]:
        quality = self._order_candidate_quality(candidate)
        return (
            float(quality["candidate_rank_score"]),
            float(quality["confidence"]),
            -float(quality["spread_points"]),
        )

    def _order_candidate_quality(self, candidate: dict[str, Any]) -> dict[str, Any]:
        signal = candidate["signal"]
        tick = candidate["tick"]
        quality = self._signal_quality(signal, tick)
        quality.update(candidate.get("symbol_quality_context", {}))
        rules = candidate.get("rules")
        order = candidate.get("order")
        if (
            rules is not None
            and order is not None
            and signal.stop_distance > 0
            and signal.take_profit_distance > 0
        ):
            quality["planned_risk_usd"] = round(
                float(self.risk.planned_order_risk(rules, order.volume, signal.stop_distance)),
                4,
            )
            quality["planned_reward_usd"] = round(
                float(self.risk.planned_order_reward(rules, order.volume, signal.take_profit_distance)),
                4,
            )
        return quality

    def _signal_quality(self, signal, tick) -> dict[str, float]:
        if signal.side not in {"buy", "sell"}:
            return {}
        reward_to_risk = (
            signal.take_profit_distance / signal.stop_distance
            if signal.stop_distance > 0
            else 0.0
        )
        spread_to_stop = (
            tick.spread_points * tick.point / signal.stop_distance
            if signal.stop_distance > 0
            else float("inf")
        )
        expectancy_score = (float(signal.confidence) * reward_to_risk) - spread_to_stop
        return {
            "candidate_rank_score": round(expectancy_score, 4),
            "reward_to_risk": round(reward_to_risk, 4),
            "spread_to_stop_ratio": round(spread_to_stop, 4),
            "spread_points": round(float(tick.spread_points), 2),
            "confidence": round(float(signal.confidence), 4),
        }

    def _hypothetical_order(self, signal, tick, volume: float = 0.0, rules=None) -> dict[str, float]:
        if signal.side not in {"buy", "sell"}:
            return {}
        if signal.stop_distance <= 0 or signal.take_profit_distance <= 0:
            return {}
        order = self.risk.build_order(tick, signal, max(float(volume), 0.0))
        payload = {
            "hypothetical_entry_price": round(float(order.price), 6),
            "hypothetical_stop_loss": round(float(order.stop_loss), 6),
            "hypothetical_take_profit": round(float(order.take_profit), 6),
            "hypothetical_risk_distance": round(float(signal.stop_distance), 6),
            "hypothetical_reward_distance": round(float(signal.take_profit_distance), 6),
        }
        if rules is not None and volume > 0:
            payload["hypothetical_risk_usd"] = round(
                float(self.risk.planned_order_risk(rules, volume, signal.stop_distance)),
                4,
            )
            payload["hypothetical_reward_usd"] = round(
                float(self.risk.planned_order_reward(rules, volume, signal.take_profit_distance)),
                4,
            )
        return payload

    def _signal_metrics(self, signal) -> dict[str, Any]:
        return dict(getattr(signal, "metadata", {}) or {})

    def _loss_cooldown_block(
        self,
        symbol: str,
        deals: list[DealSnapshot],
        now: datetime,
    ) -> dict[str, Any] | None:
        cooldown_minutes = self._loss_cooldown_minutes(symbol)
        if cooldown_minutes <= 0:
            return None

        closed = [
            deal
            for deal in deals
            if deal.symbol == symbol
            and deal.magic == self.config.magic_number
            and deal.entry in {"out", "inout", "out_by"}
        ]
        if not closed:
            return None

        latest = max(closed, key=lambda deal: deal.time)
        if latest.net_profit >= 0:
            return None

        age = now - latest.time
        if age > timedelta(minutes=cooldown_minutes):
            return None

        age_minutes = max(0, int(age.total_seconds() // 60))
        return {
            "symbol": symbol,
            "event": "blocked",
            "signal": "flat",
            "reason": f"recent {symbol} loss cooldown: net {latest.net_profit:.2f} closed {age_minutes}m ago",
            "cooldown_minutes": cooldown_minutes,
            "last_loss_net_profit": latest.net_profit,
            "last_loss_time": latest.time.isoformat(),
        }

    def _loss_cooldown_minutes(self, symbol: str) -> int:
        override = self.config.symbol_risk.get(symbol)
        if override is not None and override.loss_cooldown_minutes is not None:
            return int(override.loss_cooldown_minutes)
        return int(self.config.live_safety.loss_cooldown_minutes)

    def _is_symbol_disallowed_for_live_loop(self, symbol: str) -> bool:
        allowed_symbols = self.config.live_safety.allowed_symbols
        if not allowed_symbols:
            return False
        if not (self.config.live or self.config.live_safety.enforce_in_paper):
            return False
        return symbol not in allowed_symbols

    def _needs_deal_history_for_symbol_gates(self) -> bool:
        if any(self._loss_cooldown_minutes(symbol) > 0 for symbol in self.config.symbols):
            return True
        scaling = self.config.profit_scaling
        return bool(
            self.config.live
            and scaling.enabled
            and scaling.require_symbol_closed_deal_quality_for_scale
            and (
                scaling.min_deal_net_profit_for_scale is not None
                or scaling.max_losing_streak_for_scale > 0
            )
        )

    def _deal_history_days(self, enforce_daily_stop: bool) -> int:
        days = 2 if enforce_daily_stop else 0
        if self._needs_deal_history_for_symbol_gates():
            days = max(days, int(self.config.profit_scaling.symbol_quality_window_days))
        return max(days, 2)

    def _symbol_quality_block(
        self,
        symbol: str,
        deals: list[DealSnapshot],
    ) -> dict[str, Any] | None:
        scaling = self.config.profit_scaling
        if not (
            self.config.live
            and scaling.enabled
            and scaling.require_symbol_closed_deal_quality_for_scale
        ):
            return None
        if not deals:
            return None

        summary = summarize_closed_deals(deals, self.config.magic_number)
        symbol_summary = (summary.get("by_symbol") or {}).get(symbol)
        if not symbol_summary:
            return None

        closed_deals = int(symbol_summary.get("closed_deals") or 0)
        net_profit = float(symbol_summary.get("net_profit") or 0.0)
        losing_streak = int(symbol_summary.get("recent_losing_streak") or 0)
        required_closed_deals = max(scaling.min_live_orders_for_scale, 1)
        reasons: list[str] = []
        if (
            scaling.min_deal_net_profit_for_scale is not None
            and closed_deals >= required_closed_deals
            and net_profit < scaling.min_deal_net_profit_for_scale
        ):
            reasons.append(
                f"net profit {net_profit:.2f} below scale minimum {scaling.min_deal_net_profit_for_scale:.2f}"
            )
        profitable_enough_for_base_risk = (
            scaling.min_deal_net_profit_for_scale is not None
            and closed_deals >= required_closed_deals
            and net_profit >= scaling.min_deal_net_profit_for_scale
        ) or (
            scaling.min_deal_net_profit_for_scale is None
            and net_profit > 0
        )
        if (
            scaling.max_losing_streak_for_scale > 0
            and losing_streak >= scaling.max_losing_streak_for_scale
            and not profitable_enough_for_base_risk
        ):
            reasons.append(
                f"losing streak {losing_streak} reached scale limit {scaling.max_losing_streak_for_scale}"
            )
        if not reasons:
            return None
        return {
            "symbol": symbol,
            "event": "blocked",
            "signal": "flat",
            "reason": f"symbol quality gate: {reasons[0]}",
            "symbol_closed_deals": closed_deals,
            "symbol_deal_net_profit": net_profit,
            "symbol_recent_losing_streak": losing_streak,
            "symbol_quality_trade_action": scaling.symbol_quality_trade_action,
            "symbol_quality_losing_streak_trade_action": scaling.symbol_quality_losing_streak_trade_action,
            "symbol_quality_reasons": reasons,
        }

    def _should_block_symbol_quality(self, quality_record: dict[str, Any]) -> bool:
        scaling = self.config.profit_scaling
        if scaling.symbol_quality_trade_action == "block":
            return True
        streak_action = scaling.symbol_quality_losing_streak_trade_action
        if streak_action == "inherit":
            return False
        if streak_action != "block":
            return False
        return any("losing streak" in reason for reason in quality_record.get("symbol_quality_reasons") or [])

    def _symbol_quality_context(self, quality_record: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol_quality_trade_action": self.config.profit_scaling.symbol_quality_trade_action,
            "symbol_quality_losing_streak_trade_action": (
                self.config.profit_scaling.symbol_quality_losing_streak_trade_action
            ),
            "symbol_quality_warning": quality_record["reason"],
            "symbol_closed_deals": quality_record["symbol_closed_deals"],
            "symbol_deal_net_profit": quality_record["symbol_deal_net_profit"],
            "symbol_recent_losing_streak": quality_record["symbol_recent_losing_streak"],
            "symbol_quality_reasons": quality_record["symbol_quality_reasons"],
        }
