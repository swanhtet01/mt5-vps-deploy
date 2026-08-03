from __future__ import annotations

import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class FeedClockProvenance:
    """Empirical relationship between an MT5 tick epoch and the host UTC clock."""

    host_utc: datetime
    feed_time: datetime
    observed_offset_seconds: float
    whole_hour_offset_minutes: int
    residual_seconds: float
    coherent: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "host_utc": self.host_utc.isoformat(),
            "feed_time": self.feed_time.isoformat(),
            "feed_offset_minutes": round(self.observed_offset_seconds / 60.0, 3),
            "feed_whole_hour_offset_minutes": self.whole_hour_offset_minutes,
            "feed_clock_residual_seconds": round(self.residual_seconds, 3),
            "feed_clock_coherent": self.coherent,
        }


@dataclass(frozen=True)
class FeedHistoryWindow:
    """MT5 history bounds expressed on the broker feed clock."""

    start: datetime
    end: datetime
    clock: FeedClockProvenance

    def as_dict(self) -> dict[str, object]:
        return {
            "history_start": self.start.isoformat(),
            "history_end": self.end.isoformat(),
            **self.clock.as_dict(),
        }


def broker_server_datetime(tick: object) -> datetime | None:
    """Interpret an MT5 tick timestamp on the broker's H1 schedule clock.

    The timezone marker only makes the value aware. Callers must describe and
    schedule this value as broker-server time, not host UTC.
    """
    try:
        epoch = int(getattr(tick, "time"))
    except (AttributeError, TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def feed_clock_provenance(
    tick: object,
    *,
    host_utc: datetime | None = None,
    max_residual_seconds: float = 90.0,
    max_abs_offset_hours: int = 14,
) -> FeedClockProvenance | None:
    """Measure the feed timestamp against host UTC without assuming they match.

    MetaQuotes documents MT5 epochs as UTC, while this XM terminal has empirically
    returned a coherent whole-hour offset. A usable schedule clock must therefore be
    current relative to *some* plausible whole-hour zone offset. Stale ticks and
    malformed clocks fail the coherence check.
    """
    feed_time = broker_server_datetime(tick)
    if feed_time is None:
        return None
    host = host_utc or datetime.now(tz=timezone.utc)
    if host.tzinfo is None:
        raise ValueError("host_utc must be timezone-aware")
    host = host.astimezone(timezone.utc)
    observed = (feed_time - host).total_seconds()
    nearest_hour_seconds = round(observed / 3600.0) * 3600
    residual = observed - nearest_hour_seconds
    coherent = (
        abs(nearest_hour_seconds) <= max(int(max_abs_offset_hours), 0) * 3600
        and abs(residual) <= max(float(max_residual_seconds), 0.0)
    )
    return FeedClockProvenance(
        host_utc=host,
        feed_time=feed_time,
        observed_offset_seconds=observed,
        whole_hour_offset_minutes=int(nearest_hour_seconds / 60),
        residual_seconds=residual,
        coherent=coherent,
    )


def history_window_from_feed_clock(
    clock: FeedClockProvenance | None,
    *,
    lookback: timedelta | None = None,
    start_of_feed_day: bool = False,
    safety_minutes: float = 5.0,
) -> FeedHistoryWindow:
    """Build fail-closed MT5 history bounds from a measured broker clock.

    XM deal epochs in this environment follow the same broker-server wall clock
    exposed by ticks. Querying only through host ``now`` can therefore omit the
    newest deals when the feed is ahead by a whole-hour offset.
    """
    if clock is None or not clock.coherent:
        raise RuntimeError("fresh coherent MT5 feed clock is required for history")
    if start_of_feed_day == (lookback is not None):
        raise ValueError("choose exactly one of lookback or start_of_feed_day")
    if lookback is not None and lookback <= timedelta(0):
        raise ValueError("lookback must be positive")
    if safety_minutes < 0:
        raise ValueError("safety_minutes must be non-negative")

    feed_now = clock.feed_time
    start = (
        feed_now.replace(hour=0, minute=0, second=0, microsecond=0)
        if start_of_feed_day
        else feed_now - lookback
    )
    return FeedHistoryWindow(
        start=start,
        end=feed_now + timedelta(minutes=float(safety_minutes)),
        clock=clock,
    )


def feed_history_window(
    tick: object,
    *,
    host_utc: datetime | None = None,
    lookback: timedelta | None = None,
    start_of_feed_day: bool = False,
    safety_minutes: float = 5.0,
) -> FeedHistoryWindow:
    """Measure a tick clock and return safe history bounds on that clock."""
    return history_window_from_feed_clock(
        feed_clock_provenance(tick, host_utc=host_utc),
        lookback=lookback,
        start_of_feed_day=start_of_feed_day,
        safety_minutes=safety_minutes,
    )


def coherent_feed_clock_from_mt5(
    mt5: object,
    symbols: Iterable[str],
    *,
    host_utc: datetime | None = None,
) -> tuple[str, FeedClockProvenance]:
    """Return the first fresh broker clock from an ordered symbol fallback list."""
    failures: list[str] = []
    for symbol in dict.fromkeys(str(value) for value in symbols if str(value)):
        try:
            tick = mt5.symbol_info_tick(symbol)
        except Exception as exc:
            failures.append(f"{symbol}: {exc}")
            continue
        clock = feed_clock_provenance(tick, host_utc=host_utc) if tick is not None else None
        if clock is not None and clock.coherent:
            return symbol, clock
        failures.append(f"{symbol}: stale, missing, or incoherent tick")
    raise RuntimeError(
        "No fresh coherent MT5 reference clock is available"
        + (f" ({'; '.join(failures)})" if failures else "")
    )


def persistent_user_flag_enabled(name: str) -> bool:
    """Read a live-authorization flag from persistent user state, fail closed on Windows."""
    if not str(name).strip():
        return False
    if os.name != "nt":
        return os.environ.get(name, "").strip() == "1"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
        return str(value).strip() == "1"
    except (FileNotFoundError, OSError, ValueError):
        return False


def record_is_fresh(
    record: dict,
    *,
    max_age_minutes: int = 180,
    now: datetime | None = None,
) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        as_of = datetime.fromisoformat(str(record.get("as_of") or ""))
    except ValueError:
        return False
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    try:
        allowed_age = max(int(record.get("max_age_minutes") or max_age_minutes), 1)
    except (TypeError, ValueError):
        allowed_age = max(max_age_minutes, 1)
    reference = now or datetime.now(tz=timezone.utc)
    return reference - as_of <= timedelta(minutes=allowed_age)


def context_sizing(
    payload: dict,
    *,
    max_age_minutes: int = 180,
    now: datetime | None = None,
) -> tuple[float, bool]:
    if not record_is_fresh(payload, max_age_minutes=max_age_minutes, now=now):
        return 1.0, False
    try:
        multiplier = float(payload.get("sizing_multiplier", 1.0))
    except (TypeError, ValueError):
        multiplier = 1.0
    if not math.isfinite(multiplier):
        multiplier = 1.0
    return min(max(multiplier, 0.0), 1.0), True


def normalize_volume(
    requested: float,
    *,
    minimum: float,
    maximum: float,
    step: float,
) -> float:
    """Floor volume to the broker step without increasing requested risk.

    Zero means the requested size is below the broker minimum and must be skipped.
    """
    if minimum <= 0 or maximum < minimum or step <= 0:
        raise ValueError("invalid broker volume limits")
    requested = float(requested)
    if not math.isfinite(requested) or requested < minimum:
        return 0.0
    bounded = min(requested, maximum)
    stepped = math.floor((bounded + step * 1e-9) / step) * step
    decimals = max(0, len(f"{step:.12f}".rstrip("0").split(".")[-1]))
    return round(min(stepped, maximum), decimals)


def adverse_slippage_points(
    side: str,
    requested_price: float,
    fill_price: float,
    point: float,
) -> float:
    """Return positive points for an adverse fill and negative for price improvement."""
    if point <= 0:
        return 0.0
    normalized_side = side.lower()
    if normalized_side in {"buy", "long"}:
        return (fill_price - requested_price) / point
    if normalized_side in {"sell", "short"}:
        return (requested_price - fill_price) / point
    raise ValueError(f"unsupported order side: {side}")


def successful_deal_retcode(
    retcode: int | None,
    *,
    done: int,
    done_partial: int,
) -> bool:
    return retcode in {done, done_partial}
