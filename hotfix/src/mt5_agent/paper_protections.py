"""Shared protections for simulated forward validation.

Paper execution must resemble an operable portfolio, not an unlimited archive of
every backtest candidate. These protections are deliberately independent of all
live-authorization controls and can only reject simulated entries.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


MAX_OPEN_PAPER_POSITIONS = 4
MAX_OPEN_PAPER_POSITIONS_PER_SYMBOL = 1


def admission(
    open_positions: Iterable[Mapping[str, object]],
    candidate: Mapping[str, object],
) -> tuple[bool, str]:
    """Return whether a simulated candidate can open under portfolio protections."""
    positions = list(open_positions)
    symbol = str(candidate.get("symbol") or "")
    signal = str(candidate.get("signal") or "")
    if not symbol or not signal:
        return False, "paper candidate identity is missing"
    if any(str(position.get("signal") or "") == signal for position in positions):
        return False, "paper position already open"
    if sum(str(position.get("symbol") or "") == symbol for position in positions) >= MAX_OPEN_PAPER_POSITIONS_PER_SYMBOL:
        return False, "paper symbol concurrency protection"
    if len(positions) >= MAX_OPEN_PAPER_POSITIONS:
        return False, "paper portfolio concurrency protection"
    return True, "admitted"
