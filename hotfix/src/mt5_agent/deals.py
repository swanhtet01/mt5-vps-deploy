from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from mt5_agent.models import DealSnapshot


def closed_position_net_profits(
    deals: list[DealSnapshot],
    magic_number: int | None,
) -> list[float]:
    """Chronological full-position net P/L for statistical sizing evidence.

    Entry-side commission and fees count. Legacy snapshots without a
    ``position_id`` fall back to one closed deal per observation.
    """
    return [net for _, _, net in _closed_position_results(deals, magic_number)]


def closed_position_net_profits_by_symbol(
    deals: list[DealSnapshot],
    magic_number: int | None,
) -> dict[str, list[float]]:
    by_symbol: dict[str, list[float]] = {}
    for _, symbol, net in _closed_position_results(deals, magic_number):
        by_symbol.setdefault(symbol, []).append(net)
    return by_symbol


def _closed_position_results(
    deals: list[DealSnapshot],
    magic_number: int | None,
) -> list[tuple[datetime, str, float]]:
    relevant = [
        deal
        for deal in deals
        if magic_number is None or deal.magic == magic_number
    ]
    by_position: dict[int, list[DealSnapshot]] = {}
    legacy: list[DealSnapshot] = []
    for deal in relevant:
        if deal.position_id > 0:
            by_position.setdefault(deal.position_id, []).append(deal)
        elif deal.entry.lower() in {"out", "out_by", "inout"}:
            legacy.append(deal)

    closed: list[tuple[datetime, str, float]] = []
    for position_deals in by_position.values():
        ordered = sorted(position_deals, key=lambda deal: deal.time)
        exits = [
            deal
            for deal in ordered
            if deal.entry.lower() in {"out", "out_by", "inout"}
        ]
        if not exits:
            continue
        closed.append(
            (
                exits[-1].time,
                ordered[0].symbol,
                sum(deal.net_profit for deal in ordered),
            )
        )
    closed.extend((deal.time, deal.symbol, deal.net_profit) for deal in legacy)
    closed.sort(key=lambda item: item[0])
    return closed


def summarize_closed_deals(deals: list[DealSnapshot], magic_number: int) -> dict[str, Any]:
    closed = [
        deal
        for deal in deals
        if deal.magic == magic_number and deal.entry.lower() in {"out", "out_by", "inout"}
    ]
    closed.sort(key=lambda deal: deal.time)
    wins = [deal for deal in closed if deal.net_profit > 0]
    losses = [deal for deal in closed if deal.net_profit < 0]
    gross_profit = sum(deal.net_profit for deal in wins)
    gross_loss = abs(sum(deal.net_profit for deal in losses))
    net_profit = sum(deal.net_profit for deal in closed)
    symbols: dict[str, int] = {}
    for deal in closed:
        symbols[deal.symbol] = symbols.get(deal.symbol, 0) + 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    return {
        "closed_deals": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / len(closed) * 100) if closed else 0.0, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_profit": round(net_profit, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "symbols": symbols,
        "by_symbol": _by_symbol_summary(closed),
        "recent_losing_streak": _recent_losing_streak(closed),
        "recent_deals": [_deal_payload(deal) for deal in closed[-10:]],
    }


def summarize_closed_deals_for_day(
    deals: list[DealSnapshot],
    magic_number: int,
    now: datetime | None = None,
    lookahead_hours: float = 0.0,
) -> dict[str, Any]:
    local_now = now if now is not None else datetime.now().astimezone()
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = local_now + timedelta(hours=max(lookahead_hours, 0.0))
    daily_deals = [
        deal
        for deal in deals
        if start <= deal.time.astimezone(start.tzinfo) <= end
    ]
    return summarize_closed_deals(daily_deals, magic_number)


def _recent_losing_streak(closed: list[DealSnapshot]) -> int:
    streak = 0
    for deal in reversed(closed):
        if deal.net_profit < 0:
            streak += 1
            continue
        if deal.net_profit > 0:
            break
    return streak


def _by_symbol_summary(closed: list[DealSnapshot]) -> dict[str, Any]:
    by_symbol: dict[str, Any] = {}
    for symbol in sorted({deal.symbol for deal in closed}):
        symbol_deals = [deal for deal in closed if deal.symbol == symbol]
        wins = [deal for deal in symbol_deals if deal.net_profit > 0]
        losses = [deal for deal in symbol_deals if deal.net_profit < 0]
        gross_profit = sum(deal.net_profit for deal in wins)
        gross_loss = abs(sum(deal.net_profit for deal in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
        by_symbol[symbol] = {
            "closed_deals": len(symbol_deals),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round((len(wins) / len(symbol_deals) * 100) if symbol_deals else 0.0, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "net_profit": round(sum(deal.net_profit for deal in symbol_deals), 2),
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "recent_losing_streak": _recent_losing_streak(symbol_deals),
        }
    return by_symbol


def _deal_payload(deal: DealSnapshot) -> dict[str, Any]:
    return {
        "symbol": deal.symbol,
        "time": deal.time.isoformat(),
        "profit": round(deal.profit, 2),
        "commission": round(deal.commission, 2),
        "swap": round(deal.swap, 2),
        "net_profit": round(deal.net_profit, 2),
        "volume": deal.volume,
        "ticket_last4": deal.ticket_last4,
    }
