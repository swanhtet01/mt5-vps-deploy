from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hotfix" / "src"))

from mt5_agent.paper_protections import admission, control_block_reason  # noqa: E402


def test_paper_protection_allows_first_distinct_candidate():
    assert admission([], {"signal": "A", "symbol": "GOLD"}) == (True, "admitted")


def test_paper_protection_rejects_same_symbol_before_portfolio_cap():
    allowed, reason = admission(
        [{"signal": "A", "symbol": "GOLD"}], {"signal": "B", "symbol": "GOLD"}
    )

    assert allowed is False
    assert reason == "paper symbol concurrency protection"


def test_paper_protection_rejects_fifth_distinct_position():
    open_positions = [
        {"signal": signal, "symbol": symbol}
        for signal, symbol in zip("ABCD", ("GOLD", "BTCUSD", "EURUSD", "GBPJPY"))
    ]

    allowed, reason = admission(open_positions, {"signal": "E", "symbol": "US500Cash"})

    assert allowed is False
    assert reason == "paper portfolio concurrency protection"


def test_remote_pause_all_blocks_any_scheduler_candidate():
    assert control_block_reason(
        {"remote_control": {"pause_all": True}}, "BTCUSD", 89234
    ) == "remote control pause_all"


def test_explicit_blacklist_blocks_receipt_candidate_magic():
    reason = control_block_reason(
        {"entries": [{"symbol": "BTCUSD", "magic": 89234, "reason": "remote: operator review"}]},
        "BTCUSD", 89234,
    )

    assert reason == "remote: operator review"
