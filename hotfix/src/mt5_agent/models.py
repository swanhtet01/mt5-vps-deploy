from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Side = Literal["buy", "sell"]
SignalSide = Literal["buy", "sell", "flat"]


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: SignalSide
    confidence: float
    reason: str
    stop_distance: float = 0.0
    take_profit_distance: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Tick:
    symbol: str
    bid: float
    ask: float
    point: float

    @property
    def spread_points(self) -> float:
        if self.point <= 0:
            return float("inf")
        return abs(self.ask - self.bid) / self.point


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    min_lot: float
    max_lot: float
    lot_step: float
    tick_size: float
    tick_value: float
    point: float
    digits: int


@dataclass(frozen=True)
class AccountState:
    balance: float
    equity: float
    free_margin: float
    daily_realized_pnl: float = 0.0
    open_risk: float = 0.0


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    side: Side
    volume: float
    price_open: float
    stop_loss: float
    take_profit: float
    profit: float
    ticket_last4: str = ""
    ticket: int = 0
    magic: int = 0

    @property
    def has_stop_loss(self) -> bool:
        return self.stop_loss > 0


@dataclass(frozen=True)
class DealSnapshot:
    symbol: str
    time: datetime
    magic: int
    entry: str
    profit: float
    commission: float
    swap: float
    volume: float
    ticket_last4: str = ""
    position_id: int = 0
    fee: float = 0.0

    @property
    def net_profit(self) -> float:
        return self.profit + self.commission + self.swap + self.fee


@dataclass(frozen=True)
class OrderProposal:
    symbol: str
    side: Side
    volume: float
    price: float
    stop_loss: float
    take_profit: float
    comment: str


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    volume: float = 0.0
    stop_distance: float = 0.0
    take_profit_distance: float = 0.0
