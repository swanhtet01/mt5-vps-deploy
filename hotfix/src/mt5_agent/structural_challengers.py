"""Receipt-bound conversion of canonical structural discoveries into paper-only specs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


CANONICAL_FAMILY = "structural_hourweekday"
PAPER_MAGIC_BASE = 89000


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def paper_specs(state_path: Path, *, now: datetime | None = None, max_age_days: int = 8) -> dict[str, dict]:
    """Return deterministic paper-only specs from one validated structural receipt."""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    report_path = Path(str(state["output"])).resolve()
    if state.get("schema") != "mt5.structural_walk_forward_state.v1" or state.get("status") != "completed":
        raise ValueError("structural receipt is not completed")
    if state.get("order_authority") is not False or _sha256(report_path) != state.get("report_sha256"):
        raise ValueError("structural receipt integrity failed")
    finished = datetime.fromisoformat(str(state["finished_at_utc"]).replace("Z", "+00:00"))
    if (now or datetime.now(timezone.utc)) - finished > timedelta(days=max_age_days):
        raise ValueError("structural receipt is stale")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    scope = report.get("research_scope", {})
    if (report.get("mode") != "read_only_research" or report.get("orders_sent") != 0
            or scope.get("canonical_full_universe") is not True
            or scope.get("paper_eligibility_allowed") is not True
            or scope.get("fdr_family") != CANONICAL_FAMILY):
        raise ValueError("structural report is not canonical paper evidence")
    specs: dict[str, dict] = {}
    for candidate in report.get("paper_candidates", []):
        spec_id = str(candidate["spec_id"])
        digest = int(hashlib.sha256(spec_id.encode()).hexdigest()[:6], 16)
        name = f"STRUCTURAL_{spec_id.replace('|', '_')}"
        specs[name] = {"symbol": candidate["symbol"], "weekdays": {int(candidate["entry_weekday"])},
                       "entry_hour": int(candidate["entry_hour"]), "exit_hour": int(candidate["exit_hour"]),
                       "magic": PAPER_MAGIC_BASE + digest % 900, "side": candidate["oos"]["direction"],
                       "max_lot": 0.01, "paper_only": True, "use_regime_gate": False,
                       "source": str(report_path), "receipt_sha256": state["report_sha256"]}
    return specs
