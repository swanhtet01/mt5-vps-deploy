"""Receipt-bound conversion of canonical structural discoveries into paper-only specs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CANONICAL_FAMILY = "structural_hourweekday"
PAPER_MAGIC_BASE = 89000
CANONICAL_SYMBOLS = frozenset({
    "GOLD", "SILVER", "OILCash", "BTCUSD", "ETHUSD", "US500Cash", "USDJPY",
    "UK100Cash", "AUDJPY", "GBPJPY", "EURUSD", "GBPUSD", "GER40Cash", "JP225Cash",
})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} is outside its allowed range")
    return parsed


def _validated_receipt(
    state_path: Path, *, now: datetime | None = None, max_age_days: int = 8
) -> tuple[dict, dict, Path, datetime]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    report_path = Path(str(state["output"])).resolve()
    if state.get("schema") != "mt5.structural_walk_forward_state.v1" or state.get("status") != "completed":
        raise ValueError("structural receipt is not completed")
    if state.get("order_authority") is not False or _sha256(report_path) != state.get("report_sha256"):
        raise ValueError("structural receipt integrity failed")
    finished = datetime.fromisoformat(str(state["finished_at_utc"]).replace("Z", "+00:00"))
    if finished.tzinfo is None:
        raise ValueError("structural receipt completion timestamp is invalid")
    reference = now or datetime.now(timezone.utc)
    if finished > reference + timedelta(minutes=5):
        raise ValueError("structural receipt completion timestamp is in the future")
    if reference - finished > timedelta(days=max_age_days):
        raise ValueError("structural receipt is stale")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    scope = report.get("research_scope", {})
    symbols = scope.get("symbols") if isinstance(scope, dict) else None
    if (report.get("mode") != "read_only_research" or report.get("orders_sent") != 0
            or scope.get("canonical_full_universe") is not True
            or scope.get("paper_eligibility_allowed") is not True
            or scope.get("fdr_family") != CANONICAL_FAMILY
            or not isinstance(symbols, list) or set(symbols) != CANONICAL_SYMBOLS):
        raise ValueError("structural report is not canonical paper evidence")
    return state, report, report_path, finished.astimezone(timezone.utc)


def paper_specs(state_path: Path, *, now: datetime | None = None, max_age_days: int = 8) -> dict[str, dict]:
    """Return deterministic paper-only specs from one validated structural receipt."""
    state, report, report_path, _finished = _validated_receipt(
        state_path, now=now, max_age_days=max_age_days
    )
    scope_symbols = set(report["research_scope"]["symbols"])
    candidates = report.get("paper_candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("structural paper candidates are invalid")
    specs: dict[str, dict] = {}
    magics: set[int] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("structural paper candidate is invalid")
        spec_id = candidate.get("spec_id")
        symbol = candidate.get("symbol")
        direction = candidate.get("oos", {}).get("direction") if isinstance(candidate.get("oos"), dict) else None
        if not isinstance(spec_id, str) or not spec_id or not isinstance(symbol, str) or symbol not in scope_symbols:
            raise ValueError("structural paper candidate identity is invalid")
        if direction not in {"long", "short"}:
            raise ValueError("structural paper candidate direction is invalid")
        weekday = _integer(candidate.get("entry_weekday"), "entry weekday", 0, 6)
        entry_hour = _integer(candidate.get("entry_hour"), "entry hour", 0, 23)
        exit_hour = _integer(candidate.get("exit_hour"), "exit hour", 0, 23)
        if entry_hour == exit_hour:
            raise ValueError("structural paper candidate has no holding interval")
        digest = int(hashlib.sha256(spec_id.encode()).hexdigest()[:6], 16)
        name = f"STRUCTURAL_{spec_id.replace('|', '_')}"
        magic = PAPER_MAGIC_BASE + digest % 900
        if name in specs or magic in magics:
            raise ValueError("structural paper candidate identifier collision")
        magics.add(magic)
        specs[name] = {"symbol": symbol, "weekdays": {weekday},
                       "entry_hour": entry_hour, "exit_hour": exit_hour,
                       "magic": magic, "side": direction,
                       "max_lot": 0.01, "paper_only": True, "use_regime_gate": False,
                       "source": str(report_path), "receipt_sha256": state["report_sha256"]}
    return specs


def paper_challenger_status(
    state_path: Path, *, now: datetime | None = None, max_age_days: int = 8
) -> dict[str, object]:
    """Return an observable, non-authorizing receipt status for VPS health."""
    state, _report, report_path, finished = _validated_receipt(
        state_path, now=now, max_age_days=max_age_days
    )
    specs = paper_specs(state_path, now=now, max_age_days=max_age_days)
    reference = now or datetime.now(timezone.utc)
    age_hours = max((reference - finished).total_seconds(), 0.0) / 3600.0
    return {
        "status": "OK",
        "mode": "paper_only",
        "paper_challenger_count": len(specs),
        "receipt": str(state_path),
        "report": str(report_path),
        "receipt_sha256": state["report_sha256"],
        "age_hours": round(age_hours, 1),
    }
