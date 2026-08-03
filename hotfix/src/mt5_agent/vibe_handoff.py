"""Strict research-only handoff contract for Vibe Trading hypotheses."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping


AUDITED_VIBE_COMMIT = "652917e74e2b2e1f767ef596623bae7f098a53c4"
HANDOFF_SCHEMA = "mt5.vibe_candidate_handoff.v1"
PROPOSAL_SCHEMA = "mt5.vibe_candidate_proposals.v1"
REQUIRED_VALIDATION_GATES = (
    "cost_aware_backtest",
    "purged_walk_forward",
    "multiple_testing_control",
    "paper_forward_minimum",
    "manual_live_authorization",
)
ALLOWED_FAMILIES = frozenset(
    {
        "trend_following",
        "breakout",
        "range_reversion",
        "volatility_regime",
        "cross_market_confirmation",
    }
)
ALLOWED_DIRECTIONS = frozenset({"long", "short", "both"})
ALLOWED_SOURCE_KINDS = frozenset(
    {"vibe_deterministic_baseline", "vibe_agent"}
)
_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{7,63}$")
_HANDOFF_KEYS = {
    "schema",
    "generated_at",
    "bundle_manifest_sha256",
    "research_only",
    "order_authority",
    "automatic_live_promotion",
    "source",
    "summary",
    "candidates",
}
_CANDIDATE_KEYS = {
    "candidate_id",
    "stage",
    "source_symbols",
    "broker_symbols",
    "timeframe",
    "family",
    "direction",
    "session",
    "entry_rule",
    "exit_rule",
    "stop_rule",
    "cost_stress",
    "rationale",
    "expected_frequency",
    "failure_regime",
    "lookahead_safeguards",
    "validation_required",
    "priority_score",
    "live_eligible",
}
_COST_KEYS = {
    "basis",
    "spread_multiplier",
    "slippage_points_round_trip",
    "minimum_lot_reference",
    "estimated_cost_usd_min_lot",
}


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{label} keys invalid; missing={missing}, extra={extra}")


def _require_text(value: Any, label: str, *, maximum: int = 1200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    text = value.strip()
    if len(text) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    return text


def _require_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    allow_none: bool = False,
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be <= {maximum}")
    return number


def _require_utc_timestamp(value: Any, label: str) -> str:
    text = _require_text(value, label, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} must identify UTC")
    return text


def _validate_candidate(
    candidate: Any,
    *,
    index: int,
    allowed_symbols: set[str],
    broker_by_source: Mapping[str, str],
) -> tuple[str, tuple[Any, ...]]:
    label = f"candidates[{index}]"
    if not isinstance(candidate, dict):
        raise ValueError(f"{label} must be an object")
    _require_exact_keys(candidate, _CANDIDATE_KEYS, label)

    candidate_id = _require_text(candidate["candidate_id"], f"{label}.candidate_id", maximum=64)
    if not _ID_PATTERN.fullmatch(candidate_id):
        raise ValueError(f"{label}.candidate_id has an invalid format")
    if candidate["stage"] != "DISCOVERED":
        raise ValueError(f"{label}.stage must be DISCOVERED")
    if candidate["timeframe"] != "H1":
        raise ValueError(f"{label}.timeframe must be H1")
    if candidate["family"] not in ALLOWED_FAMILIES:
        raise ValueError(f"{label}.family is not allowed")
    if candidate["direction"] not in ALLOWED_DIRECTIONS:
        raise ValueError(f"{label}.direction is not allowed")
    if candidate["live_eligible"] is not False:
        raise ValueError(f"{label}.live_eligible must be false")

    source_symbols = candidate["source_symbols"]
    if (
        not isinstance(source_symbols, list)
        or not 1 <= len(source_symbols) <= 2
        or any(not isinstance(item, str) for item in source_symbols)
        or len(source_symbols) != len(set(source_symbols))
    ):
        raise ValueError(f"{label}.source_symbols must contain one or two unique strings")
    unknown = sorted(set(source_symbols) - allowed_symbols)
    if unknown:
        raise ValueError(f"{label}.source_symbols are outside the bundle: {unknown}")

    broker_symbols = candidate["broker_symbols"]
    expected_brokers = [broker_by_source[item] for item in source_symbols]
    if broker_symbols != expected_brokers:
        raise ValueError(f"{label}.broker_symbols do not match the bundle mapping")

    for field in (
        "session",
        "entry_rule",
        "exit_rule",
        "stop_rule",
        "rationale",
        "expected_frequency",
        "failure_regime",
    ):
        _require_text(candidate[field], f"{label}.{field}")

    safeguards = candidate["lookahead_safeguards"]
    if not isinstance(safeguards, list) or not 1 <= len(safeguards) <= 8:
        raise ValueError(f"{label}.lookahead_safeguards must contain 1..8 items")
    for safeguard_index, safeguard in enumerate(safeguards):
        _require_text(
            safeguard,
            f"{label}.lookahead_safeguards[{safeguard_index}]",
            maximum=400,
        )

    if candidate["validation_required"] != list(REQUIRED_VALIDATION_GATES):
        raise ValueError(f"{label}.validation_required must contain every gate in order")
    _require_number(candidate["priority_score"], f"{label}.priority_score", minimum=0, maximum=100)

    cost = candidate["cost_stress"]
    if not isinstance(cost, dict):
        raise ValueError(f"{label}.cost_stress must be an object")
    _require_exact_keys(cost, _COST_KEYS, f"{label}.cost_stress")
    _require_text(cost["basis"], f"{label}.cost_stress.basis", maximum=500)
    _require_number(
        cost["spread_multiplier"],
        f"{label}.cost_stress.spread_multiplier",
        minimum=1,
        maximum=10,
    )
    _require_number(
        cost["slippage_points_round_trip"],
        f"{label}.cost_stress.slippage_points_round_trip",
        minimum=0,
        maximum=100000,
    )
    _require_number(
        cost["minimum_lot_reference"],
        f"{label}.cost_stress.minimum_lot_reference",
        minimum=0.000001,
        allow_none=True,
    )
    _require_number(
        cost["estimated_cost_usd_min_lot"],
        f"{label}.cost_stress.estimated_cost_usd_min_lot",
        minimum=0,
        allow_none=True,
    )

    fingerprint = (
        tuple(source_symbols),
        candidate["family"],
        candidate["direction"],
        candidate["entry_rule"].strip().casefold(),
    )
    return candidate_id, fingerprint


def validate_candidate_handoff(
    payload: Any,
    *,
    allowed_symbols: set[str],
    broker_by_source: Mapping[str, str],
    expected_manifest_sha256: str,
    maximum_candidates: int = 10,
) -> dict[str, Any]:
    """Reject any Vibe handoff that can bypass research or bundle boundaries."""
    if not isinstance(payload, dict):
        raise ValueError("handoff must be an object")
    _require_exact_keys(payload, _HANDOFF_KEYS, "handoff")
    if payload["schema"] != HANDOFF_SCHEMA:
        raise ValueError("handoff schema is invalid")
    _require_utc_timestamp(payload["generated_at"], "handoff.generated_at")
    if payload["bundle_manifest_sha256"] != expected_manifest_sha256:
        raise ValueError("handoff bundle manifest hash mismatch")
    if payload["research_only"] is not True:
        raise ValueError("handoff.research_only must be true")
    if payload["order_authority"] is not False:
        raise ValueError("handoff.order_authority must be false")
    if payload["automatic_live_promotion"] is not False:
        raise ValueError("handoff.automatic_live_promotion must be false")
    _require_text(payload["summary"], "handoff.summary")

    source = payload["source"]
    if not isinstance(source, dict):
        raise ValueError("handoff.source must be an object")
    _require_exact_keys(source, {"kind", "vibe_commit"}, "handoff.source")
    if source["kind"] not in ALLOWED_SOURCE_KINDS:
        raise ValueError("handoff.source.kind is invalid")
    if source["vibe_commit"] != AUDITED_VIBE_COMMIT:
        raise ValueError("handoff source Vibe commit mismatch")

    candidates = payload["candidates"]
    if not isinstance(candidates, list) or len(candidates) > maximum_candidates:
        raise ValueError(f"handoff.candidates must contain at most {maximum_candidates} items")
    ids: set[str] = set()
    fingerprints: set[tuple[Any, ...]] = set()
    for index, candidate in enumerate(candidates):
        candidate_id, fingerprint = _validate_candidate(
            candidate,
            index=index,
            allowed_symbols=allowed_symbols,
            broker_by_source=broker_by_source,
        )
        if candidate_id in ids:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        if fingerprint in fingerprints:
            raise ValueError(f"duplicate candidate rule: {candidate_id}")
        ids.add(candidate_id)
        fingerprints.add(fingerprint)
    return payload


def proposal_to_handoff(
    proposal: Any,
    *,
    generated_at: datetime,
    manifest_sha256: str,
    allowed_symbols: set[str],
    broker_by_source: Mapping[str, str],
) -> dict[str, Any]:
    """Wrap an untrusted Vibe final response in trusted research metadata."""
    if not isinstance(proposal, dict):
        raise ValueError("Vibe proposal must be an object")
    _require_exact_keys(proposal, {"schema", "summary", "candidates"}, "proposal")
    if proposal["schema"] != PROPOSAL_SCHEMA:
        raise ValueError("Vibe proposal schema is invalid")
    payload = {
        "schema": HANDOFF_SCHEMA,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bundle_manifest_sha256": manifest_sha256,
        "research_only": True,
        "order_authority": False,
        "automatic_live_promotion": False,
        "source": {
            "kind": "vibe_agent",
            "vibe_commit": AUDITED_VIBE_COMMIT,
        },
        "summary": proposal["summary"],
        "candidates": proposal["candidates"],
    }
    return validate_candidate_handoff(
        payload,
        allowed_symbols=allowed_symbols,
        broker_by_source=broker_by_source,
        expected_manifest_sha256=manifest_sha256,
    )
