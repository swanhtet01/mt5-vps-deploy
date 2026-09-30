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


def control_block_reason(control_state: Mapping[str, object], symbol: str, magic: int) -> str:
    """Return a fail-closed operator block reason from the shared blacklist state."""
    remote = control_state.get("remote_control")
    if isinstance(remote, Mapping) and remote.get("pause_all") is True:
        return "remote control pause_all"
    entries = control_state.get("entries")
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        try:
            matched = str(entry.get("symbol") or "") == symbol and int(entry.get("magic")) == magic
        except (TypeError, ValueError):
            continue
        if matched:
            return str(entry.get("reason") or "blacklisted")
    return ""
