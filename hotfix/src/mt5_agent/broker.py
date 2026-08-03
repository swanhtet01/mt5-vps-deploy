from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any

from mt5_agent.exposure import position_open_risk, total_open_risk
from mt5_agent.mt5_execution import (
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
)
from mt5_agent.models import AccountState, Bar, DealSnapshot, InstrumentRules, OrderProposal, PositionSnapshot, Tick


class Broker(ABC):
    @abstractmethod
    def account(self) -> AccountState:
        raise NotImplementedError

    @abstractmethod
    def bars(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        raise NotImplementedError

    @abstractmethod
    def tick(self, symbol: str) -> Tick:
        raise NotImplementedError

    @abstractmethod
    def rules(self, symbol: str) -> InstrumentRules:
        raise NotImplementedError

    @abstractmethod
    def open_positions_count(self, symbol: str | None = None) -> int:
        raise NotImplementedError

    @abstractmethod
    def open_positions(self, symbol: str | None = None) -> list[PositionSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def history_deals(self, magic_number: int | None = None, days: int = 90) -> list[DealSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def send_order(self, order: OrderProposal, magic_number: int, dry_run: bool) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def modify_position_stops(
        self,
        position: PositionSnapshot,
        stop_loss: float,
        take_profit: float,
        magic_number: int,
        dry_run: bool,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def shutdown(self) -> None:
        raise NotImplementedError


class MT5Broker(Broker):
    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise RuntimeError("MetaTrader5 package is not installed or this is not Windows") from exc
        self.mt5 = mt5
        self._server_time_offsets: dict[str, int] = {}
        self._initialize()

    def _initialize(self) -> None:
        terminal_path = self._terminal_path()
        login = os.getenv("MT5_LOGIN")
        password = os.getenv("MT5_PASSWORD")
        server = os.getenv("MT5_SERVER")

        auth_kwargs: dict[str, Any] = {}
        if login:
            auth_kwargs["login"] = int(login)
            if server:
                auth_kwargs["server"] = server
            if password:
                auth_kwargs["password"] = password

        initialized = (
            self.mt5.initialize(terminal_path, **auth_kwargs)
            if terminal_path
            else self.mt5.initialize(**auth_kwargs)
        )
        if not initialized:
            raise RuntimeError(f"MT5 initialize failed: {self.mt5.last_error()}")

    def _terminal_path(self) -> str | None:
        configured = os.getenv("MT5_TERMINAL_PATH")
        if configured:
            return configured
        candidates = [
            r"C:\Program Files\XM Global MT5\terminal64.exe",
            r"C:\Program Files\MetaTrader 5\terminal64.exe",
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def account(self) -> AccountState:
        info = self.mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {self.mt5.last_error()}")
        positions = self.open_positions()
        rules_by_symbol = {position.symbol: self.rules(position.symbol) for position in positions}
        return AccountState(
            balance=info.balance,
            equity=info.equity,
            free_margin=info.margin_free,
            open_risk=total_open_risk(positions, rules_by_symbol),
        )

    def bars(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        self._select_symbol(symbol)
        mt5_timeframe = self._timeframe(timeframe)
        raw = self.mt5.copy_rates_from_pos(symbol, mt5_timeframe, 1, count)
        if raw is None:
            raise RuntimeError(f"copy_rates_from_pos failed for {symbol}: {self.mt5.last_error()}")
        return [
            Bar(
                time=datetime.fromtimestamp(int(item["time"]), tz=timezone.utc),
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                tick_volume=int(item["tick_volume"]),
            )
            for item in raw
        ]

    def tick(self, symbol: str) -> Tick:
        self._select_symbol(symbol)
        raw_tick = self.mt5.symbol_info_tick(symbol)
        info = self.mt5.symbol_info(symbol)
        if raw_tick is None or info is None:
            raise RuntimeError(f"symbol tick/info failed for {symbol}: {self.mt5.last_error()}")
        return Tick(symbol=symbol, bid=float(raw_tick.bid), ask=float(raw_tick.ask), point=float(info.point))

    def rules(self, symbol: str) -> InstrumentRules:
        self._select_symbol(symbol)
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol_info failed for {symbol}: {self.mt5.last_error()}")
        return InstrumentRules(
            symbol=symbol,
            min_lot=float(info.volume_min),
            max_lot=float(info.volume_max),
            lot_step=float(info.volume_step),
            tick_size=float(info.trade_tick_size),
            tick_value=float(info.trade_tick_value),
            point=float(info.point),
            digits=int(info.digits),
        )

    def open_positions_count(self, symbol: str | None = None) -> int:
        return len(self.open_positions(symbol))

    def open_positions(self, symbol: str | None = None) -> list[PositionSnapshot]:
        raw_positions = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        if raw_positions is None:
            return []
        return [self._position_snapshot(position) for position in raw_positions]

    def history_deals(self, magic_number: int | None = None, days: int = 90) -> list[DealSnapshot]:
        host_utc = datetime.now(tz=timezone.utc)
        _, clock = coherent_feed_clock_from_mt5(
            self.mt5,
            ("BTCUSD", "GOLD", "USDJPY"),
            host_utc=host_utc,
        )
        window = history_window_from_feed_clock(
            clock,
            lookback=timedelta(days=max(days, 1)),
        )
        raw_deals = self.mt5.history_deals_get(window.start, window.end)
        if raw_deals is None:
            return []
        deals = [self._deal_snapshot(deal) for deal in raw_deals]
        if magic_number is None:
            return deals
        return [deal for deal in deals if deal.magic == magic_number]

    def send_order(self, order: OrderProposal, magic_number: int, dry_run: bool) -> dict[str, Any]:
        request = self._order_request(order, magic_number)
        order_check = self.mt5.order_check(request)
        check_payload = order_check._asdict() if order_check is not None else None
        if dry_run:
            return {"dry_run": True, "order": order, "order_check": check_payload}
        if not self._order_check_ok(order_check):
            raise RuntimeError(f"order_check failed: {check_payload}")
        result = self.mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"order_send failed: {self.mt5.last_error()}")
        return result._asdict()

    def modify_position_stops(
        self,
        position: PositionSnapshot,
        stop_loss: float,
        take_profit: float,
        magic_number: int,
        dry_run: bool,
    ) -> dict[str, Any]:
        if position.ticket <= 0:
            raise RuntimeError("cannot modify position stops without a position ticket")
        request = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "position": position.ticket,
            "symbol": position.symbol,
            "sl": stop_loss,
            "tp": take_profit,
            "magic": magic_number,
            "comment": "mt5-agent manage-stop",
        }
        if dry_run:
            safe_request = dict(request)
            safe_request["position_last4"] = str(position.ticket)[-4:]
            safe_request.pop("position", None)
            return {"dry_run": True, "request": safe_request}

        result = self.mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"stop modify order_send failed: {self.mt5.last_error()}")
        payload = result._asdict()
        if int(payload.get("retcode", -1)) not in {
            getattr(self.mt5, "TRADE_RETCODE_DONE", 10009),
            getattr(self.mt5, "TRADE_RETCODE_PLACED", 10008),
        }:
            raise RuntimeError(f"stop modify failed: {payload}")
        return payload

    def _order_request(self, order: OrderProposal, magic_number: int) -> dict[str, Any]:
        order_type = self.mt5.ORDER_TYPE_BUY if order.side == "buy" else self.mt5.ORDER_TYPE_SELL
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": order.volume,
            "type": order_type,
            "price": order.price,
            "sl": order.stop_loss,
            "tp": order.take_profit,
            "deviation": 20,
            "magic": magic_number,
            "comment": order.comment,
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_type(order.symbol),
        }
        return request

    def _filling_type(self, symbol: str) -> int:
        mapping = {
            "RETURN": self.mt5.ORDER_FILLING_RETURN,
            "IOC": self.mt5.ORDER_FILLING_IOC,
            "FOK": self.mt5.ORDER_FILLING_FOK,
        }
        requested = os.getenv("MT5_ORDER_FILLING")
        if requested:
            return mapping.get(requested.upper(), self.mt5.ORDER_FILLING_IOC)

        info = self.mt5.symbol_info(symbol)
        filling_mode = int(getattr(info, "filling_mode", 0)) if info is not None else 0
        if filling_mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if filling_mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_IOC

    def _order_check_ok(self, order_check: Any) -> bool:
        if order_check is None:
            return False
        retcode = int(getattr(order_check, "retcode", -1))
        accepted = {
            0,
            getattr(self.mt5, "TRADE_RETCODE_DONE", 10009),
            getattr(self.mt5, "TRADE_RETCODE_PLACED", 10008),
        }
        return retcode in accepted

    def diagnostics(self) -> dict[str, Any]:
        terminal = self.mt5.terminal_info()
        account = self.mt5.account_info()
        version = self.mt5.version()
        positions = self.open_positions()
        rules_by_symbol = {position.symbol: self.rules(position.symbol) for position in positions}
        return {
            "terminal": terminal._asdict() if terminal is not None else None,
            "account": self._safe_account(account._asdict()) if account is not None else None,
            "open_positions": [
                {
                    "symbol": position.symbol,
                    "side": position.side,
                    "volume": position.volume,
                    "price_open": position.price_open,
                    "stop_loss": position.stop_loss,
                    "take_profit": position.take_profit,
                    "profit": position.profit,
                    "ticket_last4": position.ticket_last4,
                    "magic": position.magic,
                    "open_risk": position_open_risk(position, rules_by_symbol[position.symbol]),
                }
                for position in positions
            ],
            "open_risk": total_open_risk(positions, rules_by_symbol),
            "version": version,
            "last_error": self.mt5.last_error(),
        }

    def discover_symbols(self, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        symbols = self.mt5.symbols_get()
        if symbols is None:
            raise RuntimeError(f"symbols_get failed: {self.mt5.last_error()}")
        query_lower = query.lower()
        matches = []
        for symbol in symbols:
            name = str(symbol.name)
            description = str(getattr(symbol, "description", ""))
            path = str(getattr(symbol, "path", ""))
            haystack = f"{name} {description} {path}".lower()
            if query_lower and query_lower not in haystack:
                continue
            matches.append(
                {
                    "name": name,
                    "description": description,
                    "visible": bool(symbol.visible),
                    "path": path,
                }
            )
            if len(matches) >= limit:
                break
        return matches

    def symbol_profile(self, symbol: str, timeframe: str = "M15", bars: int = 240) -> dict[str, Any]:
        selected = self._select_symbol(symbol)
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol_info failed for {symbol}: {self.mt5.last_error()}")
        tick = self.tick(symbol)
        rules = self.rules(symbol)
        history = self.bars(symbol, timeframe, bars)
        atr_points = _average_true_range_points(history, tick.point)
        return {
            "name": symbol,
            "description": str(getattr(info, "description", "")),
            "path": str(getattr(info, "path", "")),
            "selected": selected,
            "visible": bool(getattr(info, "visible", False)),
            "trade_mode": int(getattr(info, "trade_mode", -1)),
            "spread_points": tick.spread_points,
            "bid": tick.bid,
            "ask": tick.ask,
            "point": tick.point,
            "digits": int(getattr(info, "digits", 0)),
            "min_lot": rules.min_lot,
            "lot_step": rules.lot_step,
            "max_lot": rules.max_lot,
            "tick_size": rules.tick_size,
            "tick_value": rules.tick_value,
            "bars": len(history),
            "last_bar": history[-1].time.isoformat() if history else None,
            "atr_points": atr_points,
        }

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def _select_symbol(self, symbol: str) -> bool:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol_info failed for {symbol}: {self.mt5.last_error()}")
        if bool(getattr(info, "visible", False)):
            return True
        selected = self.mt5.symbol_select(symbol, True)
        if not selected:
            raise RuntimeError(f"symbol_select failed for {symbol}: {self.mt5.last_error()}")
        return True

    def _timeframe(self, value: str) -> int:
        mapping = {
            "M1": self.mt5.TIMEFRAME_M1,
            "M5": self.mt5.TIMEFRAME_M5,
            "M15": self.mt5.TIMEFRAME_M15,
            "M30": self.mt5.TIMEFRAME_M30,
            "H1": self.mt5.TIMEFRAME_H1,
            "H4": self.mt5.TIMEFRAME_H4,
            "D1": self.mt5.TIMEFRAME_D1,
        }
        try:
            return mapping[value.upper()]
        except KeyError as exc:
            raise ValueError(f"unsupported timeframe: {value}") from exc

    def _safe_account(self, account: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "trade_mode",
            "leverage",
            "limit_orders",
            "margin_so_mode",
            "trade_allowed",
            "trade_expert",
            "margin_mode",
            "currency_digits",
            "fifo_close",
            "balance",
            "credit",
            "profit",
            "equity",
            "margin",
            "margin_free",
            "margin_level",
            "currency",
            "server",
            "company",
        }
        safe = {key: value for key, value in account.items() if key in allowed}
        login = str(account.get("login", ""))
        if login:
            safe["login_last4"] = login[-4:]
        if account.get("name"):
            safe["name"] = "[redacted]"
        return safe

    def _position_snapshot(self, position: Any) -> PositionSnapshot:
        side = "buy" if position.type == self.mt5.POSITION_TYPE_BUY else "sell"
        ticket = int(getattr(position, "ticket", 0) or 0)
        return PositionSnapshot(
            symbol=str(position.symbol),
            side=side,
            volume=float(position.volume),
            price_open=float(position.price_open),
            stop_loss=float(position.sl),
            take_profit=float(position.tp),
            profit=float(position.profit),
            ticket_last4=str(ticket)[-4:] if ticket else "",
            ticket=ticket,
            magic=int(getattr(position, "magic", 0) or 0),
        )

    def _deal_snapshot(self, deal: Any) -> DealSnapshot:
        ticket = int(getattr(deal, "ticket", 0) or 0)
        symbol = str(getattr(deal, "symbol", ""))
        raw_time = int(getattr(deal, "time", 0) or 0)
        return DealSnapshot(
            symbol=symbol,
            time=datetime.fromtimestamp(self._normalized_deal_timestamp(symbol, raw_time), tz=timezone.utc),
            magic=int(getattr(deal, "magic", 0) or 0),
            entry=self._deal_entry_name(int(getattr(deal, "entry", -1))),
            profit=float(getattr(deal, "profit", 0.0) or 0.0),
            commission=float(getattr(deal, "commission", 0.0) or 0.0),
            swap=float(getattr(deal, "swap", 0.0) or 0.0),
            volume=float(getattr(deal, "volume", 0.0) or 0.0),
            ticket_last4=str(ticket)[-4:] if ticket else "",
            position_id=int(getattr(deal, "position_id", 0) or 0),
            fee=float(getattr(deal, "fee", 0.0) or 0.0),
        )

    def _normalized_deal_timestamp(self, symbol: str, raw_timestamp: int) -> int:
        if raw_timestamp <= 0:
            return 0
        return raw_timestamp - self._server_time_offset_seconds(symbol)

    def _server_time_offset_seconds(self, symbol: str) -> int:
        offsets = getattr(self, "_server_time_offsets", None)
        if offsets is None:
            offsets = {}
            self._server_time_offsets = offsets
        if symbol in offsets:
            return offsets[symbol]

        offset = 0
        symbol_info_tick = getattr(self.mt5, "symbol_info_tick", None)
        if callable(symbol_info_tick):
            tick = symbol_info_tick(symbol)
            raw_tick_time = int(getattr(tick, "time", 0) or 0) if tick is not None else 0
            if raw_tick_time > 0:
                detected = raw_tick_time - int(datetime.now(tz=timezone.utc).timestamp())
                rounded = int(round(detected / 3600) * 3600)
                if (
                    abs(detected) >= 15 * 60
                    and abs(rounded) <= 14 * 3600
                    and abs(detected - rounded) <= 15 * 60
                ):
                    offset = rounded

        offsets[symbol] = offset
        return offset

    def _deal_entry_name(self, entry: int) -> str:
        mapping = {
            getattr(self.mt5, "DEAL_ENTRY_IN", 0): "in",
            getattr(self.mt5, "DEAL_ENTRY_OUT", 1): "out",
            getattr(self.mt5, "DEAL_ENTRY_INOUT", 2): "inout",
            getattr(self.mt5, "DEAL_ENTRY_OUT_BY", 3): "out_by",
        }
        return mapping.get(entry, str(entry))


def _average_true_range_points(history: list[Bar], point: float, period: int = 60) -> float | None:
    if point <= 0 or len(history) < 2:
        return None
    ranges: list[float] = []
    recent = history[-(period + 1) :]
    for previous, current in zip(recent, recent[1:]):
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        if true_range > 0:
            ranges.append(true_range / point)
    if not ranges:
        return None
    return round(sum(ranges) / len(ranges), 4)
