"""Cumulative false-discovery ledger (Benjamini-Hochberg, per registered family).

WHY THIS EXISTS
---------------
The honest research engine's whole value is killing mirages before money is risked.
But the moment we run *many* hypotheses (nightly scans over symbols x hours x weekdays,
strategy grids, rule batteries) a fixed |t| >= 3 / p < 0.05 threshold becomes a
false-positive machine: test 2,000 pure-noise buckets and ~5 will clear p<0.0027 by luck.
Per-run Bonferroni helps within one run, but it does NOT account for the fact that we run
the scan AGAIN every night. The Nth night eventually flukes a "survivor" that then gets
hand-pasted into the live SIGNALS dict (this already happened: 88007-88009 were added from
a Bonferroni-less wide_edge_hunt run).

This ledger is the fix: an append-only record of EVERY hypothesis ever graded, with
Benjamini-Hochberg FDR control applied WITHIN a registered family over the family's
*cumulative* trial count. A candidate is only a "discovery" if its p-value survives BH at
the true, growing denominator. Off-ledger grading is meant to be impossible: promotion
code asks `is_discovery()`, which forces the trial to be recorded first.

DESIGN NOTES / HONESTY GUARANTEES
---------------------------------
* BH is applied WITHIN a registered family, never naively pooled across every test ever run
  (naive global pooling is both statistically wrong and trivially gameable by splitting one
  family into many). Families are a fixed registry; recording an unknown family raises.
* Every re-test remains in the denominator. The latest result for a spec must itself survive
  BH; an old lucky p-value cannot permanently bless a decayed edge.
* BH controls the *false discovery rate* (expected proportion of false positives among
  declared discoveries) <= q under independence/PRDS. Under the complete null it rejects
  nothing with probability >= 1-q. `simulate_null_fdr()` is the calibration check the tests
  assert against.
* Stdlib only (the repo's sole runtime dep is MetaTrader5). Append-only JSONL so a crash
  mid-write can lose at most the last line, never corrupt history.

This module finds NO new alpha. It makes scaling the search *safe* -- the more hypotheses
we throw at it, the harder (correctly) it is to fluke a survivor.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Registered test families. BH runs within a family over its cumulative trial count.
# Add a family here deliberately; do NOT split a family to shrink its denominator.
FAMILIES = {
    "calendar_hourweekday",  # (symbol x hour x weekday) seasonality scans
    "strategy_chamber",      # entry-mode / parameter strategy specs graded by the chamber
    "rule_engine",           # non-TA rule hypotheses (rules_battery)
    "vibe_deterministic_fixed_rules",  # cumulative Vibe fixed-rule screens
    "manual",                # ad-hoc one-offs -- still corrected, never exempt
}

# Default ledger lives in the (gitignored) data_cache so history is runtime state, not code.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER_PATH = _REPO_ROOT / "data_cache" / "fdr_ledger.jsonl"


def benjamini_hochberg(pvalues: list[float], q: float = 0.05) -> list[bool]:
    """Pure Benjamini-Hochberg step-up. Returns a reject-mask aligned to `pvalues`.

    Rejects every hypothesis with p <= p_(k*), where k* is the largest rank k such that
    p_(k) <= (k/m) * q. Controls FDR <= q under independence. Ties resolved by `<=`.
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    max_k = 0
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= (rank / m) * q:
            max_k = rank
    if max_k == 0:
        return [False] * m
    threshold = pvalues[order[max_k - 1]]
    return [p <= threshold for p in pvalues]


def bh_threshold(pvalues: list[float], q: float = 0.05) -> float:
    """The largest p-value BH would reject given this set (0.0 if it rejects nothing)."""
    mask = benjamini_hochberg(pvalues, q)
    rejected = [p for p, r in zip(pvalues, mask) if r]
    return max(rejected) if rejected else 0.0


@dataclass
class Trial:
    family: str
    spec_id: str
    p_value: float
    t_stat: float | None = None
    n: int | None = None
    ts: str = ""
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "family": self.family, "spec_id": self.spec_id, "p_value": self.p_value,
            "t_stat": self.t_stat, "n": self.n, "ts": self.ts, "meta": self.meta,
        })

    @classmethod
    def from_dict(cls, d: dict) -> "Trial":
        return cls(family=d["family"], spec_id=d["spec_id"], p_value=float(d["p_value"]),
                   t_stat=d.get("t_stat"), n=d.get("n"), ts=d.get("ts", ""),
                   meta=d.get("meta") or {})


class FDRLedger:
    """Append-only cumulative trial ledger with per-family BH discovery decisions."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_LEDGER_PATH
        self._trials: list[Trial] = []
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        self._trials = []
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._trials.append(Trial.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue  # tolerate a torn last line, never crash on read

    def record(self, family: str, spec_id: str, p_value: float,
               t_stat: float | None = None, n: int | None = None, **meta) -> Trial:
        """Append one graded hypothesis. Unknown families are rejected by construction."""
        if family not in FAMILIES:
            raise ValueError(f"unregistered family {family!r}; add it to FAMILIES deliberately. "
                             f"Known: {sorted(FAMILIES)}")
        if not (0.0 <= p_value <= 1.0):
            raise ValueError(f"p_value must be in [0,1], got {p_value}")
        trial = Trial(family=family, spec_id=spec_id, p_value=float(p_value),
                      t_stat=t_stat, n=n,
                      ts=datetime.now(tz=timezone.utc).isoformat(), meta=dict(meta))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(trial.to_json() + "\n")
        self._trials.append(trial)
        return trial

    # ---- queries ----
    def family_trials(self, family: str) -> list[Trial]:
        return [t for t in self._trials if t.family == family]

    def bh_rejected(self, family: str, q: float = 0.05) -> set[str]:
        """Return specs whose latest trial survives BH over every family trial.

        Repeated scans are repeated statistical trials, so they must grow the denominator.
        Requiring the latest trial to survive also prevents a stale best-ever p-value from
        keeping a decayed strategy eligible forever.
        """
        trials = [trial for trial in self._trials if trial.family == family]
        if not trials:
            return set()
        pvals = [trial.p_value for trial in trials]
        mask = benjamini_hochberg(pvals, q)
        latest_index: dict[str, int] = {}
        for index, trial in enumerate(trials):
            latest_index[trial.spec_id] = index
        return {
            spec_id
            for spec_id, index in latest_index.items()
            if mask[index]
        }

    def is_discovery(self, family: str, spec_id: str, q: float = 0.05) -> bool:
        """Hard promotion gate: True only if spec_id survives BH at the family's
        cumulative denominator. Promotion code MUST route through here."""
        return spec_id in self.bh_rejected(family, q)

    def family_denominator(self, family: str) -> int:
        """How many trials have been graded in this family, including re-tests."""
        return sum(1 for trial in self._trials if trial.family == family)

    def summary(self) -> dict:
        out = {}
        for fam in sorted(FAMILIES):
            m = self.family_denominator(fam)
            if m == 0:
                continue
            rej = self.bh_rejected(fam)
            distinct = len({t.spec_id for t in self._trials if t.family == fam})
            out[fam] = {"trials": m, "distinct_specs": distinct, "discoveries": len(rej),
                        "discovery_ids": sorted(rej)}
        return out


def simulate_null_fdr(m: int = 500, q: float = 0.05, families: int = 400,
                      seed: int = 0) -> dict:
    """Calibration self-check: feed pure-noise (uniform) p-values and confirm BH controls
    the error. Returns the empirical family-wise false-positive rate and per-family rejects.
    Under the complete null, P(any rejection) <= q, so `any_rejection_rate` should sit at/below q."""
    rng = random.Random(seed)
    any_rej = 0
    total_rej = 0
    for _ in range(families):
        pvals = [rng.random() for _ in range(m)]
        mask = benjamini_hochberg(pvals, q)
        k = sum(mask)
        total_rej += k
        if k:
            any_rej += 1
    return {
        "families": families, "m_per_family": m, "q": q,
        "any_rejection_rate": any_rej / families,
        "mean_false_rejects_per_family": total_rej / families,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "calibrate":
        print(json.dumps(simulate_null_fdr(), indent=2))
    else:
        led = FDRLedger()
        print(json.dumps(led.summary(), indent=2))
