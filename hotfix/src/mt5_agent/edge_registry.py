"""Canonical edge registry: one auditable source of truth per trading edge.

WHY
---
Today an edge's truth is scattered and hand-edited: the live (symbol, weekday, hour, magic)
lives in multi_drift_live.SIGNALS (a literal dict), its sizing tier in position_sizing.json,
its blacklist status in blacklist.json, and stale COPIES of the magic list in dynamic_sizing
and html_dashboard (which already drifted -- they miss 88007-88009). Worse, an edge can be
promoted to LIVE by pasting into SIGNALS with NO validation gate -- which is exactly how
GOLD_TUE (88009, t=3.31) got armed despite failing honest multiple-testing.

This module is the single record per edge spanning discovery -> validation -> lifecycle ->
live magic -> expected stats. Promotion to LIVE is GATED on validation, by construction, so
an unvalidated edge can never reach live by hand again. Other modules should READ this instead
of keeping their own magic maps.

Lifecycle:  DISCOVERED -> PAPER -> LIVE   (forward only past validation)
            any -> WATCH (decaying, size down) -> RETIRED / BLACKLIST

Stdlib only. JSON-backed, atomic writes via the shared paths helper when available.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = _REPO_ROOT / "data_cache" / "edge_registry.json"


class Stage(str, Enum):
    DISCOVERED = "DISCOVERED"   # found by a scan, not yet validated
    PAPER = "PAPER"             # validated enough to paper-trade
    LIVE = "LIVE"              # armed on the real account
    WATCH = "WATCH"            # live but decaying / under review; size down
    RETIRED = "RETIRED"        # taken off, kept for history
    BLACKLIST = "BLACKLIST"    # actively refused


# Stages from which forward-promotion to LIVE is allowed
_PROMOTABLE_FROM = {Stage.PAPER.value, Stage.LIVE.value, Stage.WATCH.value}


@dataclass
class Validation:
    """How honestly an edge was vetted. `promotable` is the LIVE gate."""
    bonferroni: bool = False        # survived multiple-testing in its scan
    fdr_discovery: bool = False     # survived the cumulative FDR ledger
    chamber_verdict: str = "NONE"   # PASS / FAIL / INSUFFICIENT_DATA / NONE
    clock_verified: bool = False    # research bucket was mapped to the executable feed clock
    forward_trades: int = 0         # untouched paper-forward sample only
    forward_profit_factor: float = 0.0
    forward_net: float = 0.0

    def blocking_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self.bonferroni:
            reasons.append("Bonferroni test not passed")
        if not self.fdr_discovery:
            reasons.append("latest cumulative-FDR trial not passed")
        if self.chamber_verdict != "PASS":
            reasons.append(f"walk-forward verdict is {self.chamber_verdict}")
        if not self.clock_verified:
            reasons.append("feed-clock mapping not verified")
        if self.forward_trades < 30:
            reasons.append(f"paper-forward sample is {self.forward_trades}/30 trades")
        if self.forward_profit_factor < 1.2:
            reasons.append(f"paper-forward profit factor is {self.forward_profit_factor:.2f}/1.20")
        if self.forward_net <= 0:
            reasons.append("paper-forward net is not positive")
        return reasons

    @property
    def promotable(self) -> bool:
        return not self.blocking_reasons()


@dataclass
class EdgeRecord:
    key: str                 # e.g. "GOLD_TUE"
    magic: int
    symbol: str
    weekday: int             # 0=Mon
    entry_hour: int
    exit_hour: int
    side: str                # long / short
    stage: str = Stage.DISCOVERED.value
    sl_usd: float = 0.0
    tp_usd: float = 0.0
    max_lot: float = 0.01
    discovery: dict = field(default_factory=dict)      # {t, n, mean_bps, source}
    validation: dict = field(default_factory=lambda: asdict(Validation()))
    expected_win_rate: float | None = None
    expected_mean_r: float | None = None
    notes: str = ""

    def validation_obj(self) -> Validation:
        v = self.validation or {}
        return Validation(bonferroni=bool(v.get("bonferroni")),
                          fdr_discovery=bool(v.get("fdr_discovery")),
                          chamber_verdict=str(v.get("chamber_verdict", "NONE")),
                          clock_verified=bool(v.get("clock_verified")),
                          forward_trades=int(v.get("forward_trades") or 0),
                          forward_profit_factor=float(v.get("forward_profit_factor") or 0.0),
                          forward_net=float(v.get("forward_net") or 0.0))

    @property
    def promotable(self) -> bool:
        return self.validation_obj().promotable

    @property
    def is_live(self) -> bool:
        return self.stage == Stage.LIVE.value


class PromotionError(RuntimeError):
    """Raised when promotion to LIVE is attempted without validation."""


class EdgeRegistry:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
        self._edges: dict[str, EdgeRecord] = {}
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        self._edges = {}
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return
        for rec in payload.get("edges", []):
            try:
                self._edges[rec["key"]] = EdgeRecord(**rec)
            except (TypeError, KeyError):
                continue

    def save(self) -> None:
        import os
        payload = {"edges": [asdict(e) for e in self._edges.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- access ----
    def upsert(self, rec: EdgeRecord) -> None:
        self._edges[rec.key] = rec

    def get(self, key: str) -> EdgeRecord | None:
        return self._edges.get(key)

    def all(self) -> list[EdgeRecord]:
        return list(self._edges.values())

    def live(self) -> list[EdgeRecord]:
        return [e for e in self._edges.values() if e.is_live]

    def by_magic(self, magic: int) -> EdgeRecord | None:
        for e in self._edges.values():
            if e.magic == magic:
                return e
        return None

    # ---- lifecycle (the gate) ----
    def promote(self, key: str, to_stage: str) -> EdgeRecord:
        rec = self._edges.get(key)
        if rec is None:
            raise KeyError(key)
        target = Stage(to_stage)  # raises if invalid
        if target is Stage.LIVE and not rec.promotable:
            raise PromotionError(
                f"{key} cannot go LIVE: "
                + "; ".join(rec.validation_obj().blocking_reasons()))
        rec.stage = target.value
        return rec

    def demote(self, key: str, to_stage: str = Stage.WATCH.value, note: str = "") -> EdgeRecord:
        rec = self._edges.get(key)
        if rec is None:
            raise KeyError(key)
        target = Stage(to_stage)
        if target in {Stage.LIVE, Stage.PAPER, Stage.DISCOVERED}:
            raise ValueError(f"demote() only moves to WATCH/RETIRED/BLACKLIST, not {to_stage}")
        rec.stage = target.value
        if note:
            rec.notes = (rec.notes + " | " if rec.notes else "") + note
        return rec

    # ---- audit ----
    def audit_live_unvalidated(self) -> list[EdgeRecord]:
        """LIVE edges whose validation is not promotable -> these should be demoted to WATCH.
        An empty list means every live edge is honestly validated (the goal)."""
        return [e for e in self._edges.values() if e.is_live and not e.promotable]

    def summary(self) -> dict[str, Any]:
        by_stage: dict[str, int] = {}
        for e in self._edges.values():
            by_stage[e.stage] = by_stage.get(e.stage, 0) + 1
        return {
            "total": len(self._edges),
            "by_stage": by_stage,
            "live_unvalidated": [e.key for e in self.audit_live_unvalidated()],
        }
