"""Validated Vibe artifacts and persistent quote-only paper-forward evidence.

This module has no broker dependency and no execution capability. It validates
the one-way Vibe research handoff, registers every repeated screen in the
cumulative FDR ledger, and grades independently observed shadow trades.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from mt5_agent.fdr_ledger import FDRLedger
from mt5_agent.structural_validation import block_bootstrap_mean_lcb
from mt5_agent.vibe_handoff import AUDITED_VIBE_COMMIT, validate_candidate_handoff
from mt5_agent.vibe_rules import RULES


ARTIFACT_POINTER_SCHEMA = "mt5.vibe_baseline_run.v1"
BUNDLE_SCHEMA = "mt5.vibe_research_bundle.v1"
BASELINE_SCHEMA = "mt5.vibe_deterministic_research.v1"
SCREEN_SCHEMA = "mt5.vibe_candidate_screen.v1"
STATE_SCHEMA = "mt5.vibe_shadow_forward_state.v1"
REPORT_SCHEMA = "mt5.vibe_shadow_forward_report.v1"
FDR_FAMILY = "vibe_deterministic_fixed_rules"
MAX_ARTIFACT_AGE_HOURS = 36.0
MIN_FORWARD_TRADES = 30
MIN_FORWARD_PROFIT_FACTOR = 1.20
OBSERVATION_DAYS = 90
MAX_FORWARD_TRADES_PER_EXPERIMENT = 60
BOOTSTRAP_SAMPLES = 3000
BOOTSTRAP_BLOCK_TRADES = 4
MINIMUM_HISTORICAL_DSR_PROBABILITY = 0.95
MAXIMUM_HISTORICAL_PBO = 0.20
MAXIMUM_MINIMUM_LOT_STOP_RISK_FRACTION = 0.02
MAXIMUM_MINIMUM_LOT_MARGIN_FREE_FRACTION = 0.25
PAPER_FORWARD_ENROLLMENT_GATE = "historical_screen_pass_and_cumulative_bh_fdr"


@dataclass(frozen=True)
class VibeArtifacts:
    generated_at: datetime
    bundle_manifest_sha256: str
    baseline_sha256: str
    handoff_sha256: str
    screen_sha256: str
    experiments: tuple[dict[str, Any], ...]
    report_path: Path
    handoff_path: Path
    screen_path: Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _utc_timestamp(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _bounded_file(raw: Any, root: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} path is missing")
    path = Path(raw).resolve()
    if not path.is_file() or path.is_symlink() or not _within(path, root):
        raise ValueError(f"{label} is missing, linked, or outside {root}")
    return path


def _bounded_directory(raw: Any, root: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} path is missing")
    path = Path(raw).resolve()
    if not path.is_dir() or path.is_symlink() or not _within(path, root):
        raise ValueError(f"{label} is missing, linked, or outside {root}")
    return path


def _bundle_file(bundle: Path, relative: Any) -> Path:
    if not isinstance(relative, str):
        raise ValueError("bundle file path must be text")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ValueError(f"unsafe bundle path: {relative!r}")
    path = bundle.joinpath(*pure.parts)
    if not _within(path, bundle):
        raise ValueError(f"bundle path escaped root: {relative!r}")
    return path


def _verify_bundle(bundle: Path) -> tuple[dict[str, Any], str, dict[str, str]]:
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("bundle manifest is missing or linked")
    manifest_sha256 = file_sha256(manifest_path)
    manifest = _read_json(manifest_path, "bundle manifest")
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise ValueError("bundle schema mismatch")
    if (
        manifest.get("mode") != "research_only"
        or manifest.get("contains_credentials") is not False
        or manifest.get("order_authority") is not False
        or manifest.get("automatic_live_promotion") is not False
        or manifest.get("export_errors")
    ):
        raise ValueError("bundle research boundary is invalid")
    if manifest.get("vibe_trading", {}).get("audited_commit") != AUDITED_VIBE_COMMIT:
        raise ValueError("bundle Vibe commit mismatch")
    if manifest.get("source", {}).get("feed_clock_sample", {}).get("coherent") is not True:
        raise ValueError("bundle feed clock is not coherent")

    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise ValueError("bundle file list is empty")
    seen: set[str] = set()
    broker_by_source: dict[str, str] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"bundle file record {index} is invalid")
        relative = record.get("path")
        if not isinstance(relative, str) or relative in seen:
            raise ValueError(f"bundle file path {relative!r} is invalid or duplicated")
        seen.add(relative)
        path = _bundle_file(bundle, relative)
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"bundle file is missing or linked: {relative}")
        if path.stat().st_size != int(record.get("bytes", -1)):
            raise ValueError(f"bundle file size mismatch: {relative}")
        if file_sha256(path) != record.get("sha256"):
            raise ValueError(f"bundle file hash mismatch: {relative}")
        source = record.get("source_symbol")
        broker = record.get("broker_symbol")
        if source is not None:
            if not isinstance(source, str) or not isinstance(broker, str) or source in broker_by_source:
                raise ValueError("bundle source-to-broker mapping is invalid")
            broker_by_source[source] = broker
    declared = manifest.get("research_scope", {}).get("symbols")
    if declared != list(broker_by_source):
        raise ValueError("bundle research scope does not match bar files")
    return manifest, manifest_sha256, broker_by_source


def _number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{label} is outside its allowed range")
    return number


def _spec_fingerprint(
    candidate: Mapping[str, Any], result: Mapping[str, Any], direction: str
) -> str:
    payload = {
        "broker_symbol": result["broker_symbol"],
        "source_symbols": candidate["source_symbols"],
        "timeframe": candidate["timeframe"],
        "family": result["family"],
        "direction": direction,
        "fixed_rule": result["fixed_rule"],
        "slippage_points_round_trip": candidate["cost_stress"]["slippage_points_round_trip"],
        "minimum_lot": candidate["cost_stress"]["minimum_lot_reference"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_vibe_artifacts(
    sidecar_root: str | Path,
    *,
    now: datetime | None = None,
    maximum_age_hours: float = MAX_ARTIFACT_AGE_HOURS,
) -> VibeArtifacts:
    """Validate the latest deterministic Vibe bundle and fixed screen end to end."""
    root = Path(sidecar_root).resolve()
    reports_root = root / "reports"
    exports_root = root / "exports"
    pointer_path = root / "last-baseline.json"
    pointer = _read_json(pointer_path, "Vibe baseline pointer")
    if (
        pointer.get("schema") != ARTIFACT_POINTER_SCHEMA
        or pointer.get("status") != "completed"
        or pointer.get("order_authority") is not False
        or int(pointer.get("paper_candidate_count", -1)) != 0
        or int(pointer.get("live_eligible_count", -1)) != 0
    ):
        raise ValueError("Vibe baseline pointer boundary is invalid")
    finished_at = _utc_timestamp(pointer.get("finished_at"), "baseline.finished_at")
    reference = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    age = reference - finished_at
    if age < timedelta(minutes=-5) or age > timedelta(hours=maximum_age_hours):
        raise ValueError("Vibe baseline pointer is stale or future-dated")

    report_path = _bounded_file(pointer.get("report"), reports_root, "baseline report")
    handoff_path = _bounded_file(pointer.get("handoff"), reports_root, "candidate handoff")
    screen_path = _bounded_file(pointer.get("candidate_screen"), reports_root, "candidate screen")
    bundle = _bounded_directory(pointer.get("bundle"), exports_root, "research bundle")
    expected_hashes = (
        (report_path, pointer.get("report_sha256"), "baseline report"),
        (handoff_path, pointer.get("handoff_sha256"), "candidate handoff"),
        (screen_path, pointer.get("candidate_screen_sha256"), "candidate screen"),
    )
    for path, expected, label in expected_hashes:
        if file_sha256(path) != expected:
            raise ValueError(f"{label} hash mismatch")

    manifest, manifest_sha256, broker_by_source = _verify_bundle(bundle)
    baseline = _read_json(report_path, "baseline report")
    if (
        baseline.get("schema") != BASELINE_SCHEMA
        or baseline.get("mode") != "research_only"
        or baseline.get("order_authority") is not False
        or baseline.get("automatic_live_promotion") is not False
        or baseline.get("data_quality_status") != "PASS"
        or baseline.get("bundle", {}).get("manifest_sha256") != manifest_sha256
    ):
        raise ValueError("baseline report contract is invalid")

    handoff = _read_json(handoff_path, "candidate handoff")
    validate_candidate_handoff(
        handoff,
        allowed_symbols=set(broker_by_source),
        broker_by_source=broker_by_source,
        expected_manifest_sha256=manifest_sha256,
        maximum_candidates=10,
    )
    if handoff.get("source", {}).get("kind") != "vibe_deterministic_baseline":
        raise ValueError("only deterministic Vibe candidates may enter shadow-forward")

    screen = _read_json(screen_path, "candidate screen")
    if (
        screen.get("schema") != SCREEN_SCHEMA
        or screen.get("mode") != "historical_research_only"
        or screen.get("order_authority") is not False
        or screen.get("automatic_live_promotion") is not False
        or screen.get("forecast_generated") is not False
        or int(screen.get("paper_candidate_count", -1)) != 0
        or int(screen.get("live_eligible_count", -1)) != 0
    ):
        raise ValueError("candidate screen boundary is invalid")
    source = screen.get("source")
    if not isinstance(source, dict) or (
        source.get("kind") != "vibe_deterministic_baseline"
        or source.get("vibe_commit") != AUDITED_VIBE_COMMIT
        or source.get("bundle_manifest_sha256") != manifest_sha256
        or source.get("candidate_handoff_sha256") != file_sha256(handoff_path)
    ):
        raise ValueError("candidate screen source binding is invalid")

    candidates = {candidate["candidate_id"]: candidate for candidate in handoff["candidates"]}
    expected_ids: set[str] = set()
    for candidate in handoff["candidates"]:
        directions = ("long", "short") if candidate["direction"] == "both" else (candidate["direction"],)
        expected_ids.update(f"{candidate['candidate_id']}-{direction.upper()}" for direction in directions)
    results = screen.get("results")
    if not isinstance(results, list) or int(screen.get("family_trials", -1)) != len(results):
        raise ValueError("candidate screen result count is invalid")
    if {result.get("screen_id") for result in results if isinstance(result, dict)} != expected_ids:
        raise ValueError("candidate screen identities do not exactly match the handoff")
    method = screen.get("method")
    if (
        not isinstance(method, dict)
        or method.get("minimum_lot_stop_risk_required") is not True
        or _number(
            method.get("maximum_minimum_lot_stop_risk_fraction"),
            "method.maximum_minimum_lot_stop_risk_fraction",
            minimum=0,
        )
        != MAXIMUM_MINIMUM_LOT_STOP_RISK_FRACTION
        or method.get("minimum_lot_margin_required") is not True
        or _number(
            method.get("maximum_minimum_lot_margin_free_fraction"),
            "method.maximum_minimum_lot_margin_free_fraction",
            minimum=0,
        )
        != MAXIMUM_MINIMUM_LOT_MARGIN_FREE_FRACTION
    ):
        raise ValueError("candidate screen minimum-lot risk contract is invalid")
    selection_overfitting = screen.get("selection_overfitting")
    if not isinstance(selection_overfitting, dict):
        raise ValueError("candidate screen PBO report is missing")
    pbo_status = selection_overfitting.get("status")
    maximum_pbo = _number(
        selection_overfitting.get("maximum_probability_backtest_overfitting"),
        "selection_overfitting.maximum_probability_backtest_overfitting",
        minimum=0,
    )
    pbo_value = selection_overfitting.get("probability_backtest_overfitting")
    pbo_probability = (
        None
        if pbo_value is None
        else _number(
            pbo_value,
            "selection_overfitting.probability_backtest_overfitting",
            minimum=0,
        )
    )
    if maximum_pbo != MAXIMUM_HISTORICAL_PBO:
        raise ValueError("candidate screen PBO threshold is invalid")
    if pbo_probability is not None and pbo_probability > 1:
        raise ValueError("selection_overfitting probability must be <= 1")
    pbo_pass = bool(
        pbo_status == "AVAILABLE"
        and pbo_probability is not None
        and pbo_probability <= maximum_pbo
    )

    generated_at = _utc_timestamp(screen.get("generated_at"), "screen.generated_at")
    screen_age = reference - generated_at
    if screen_age < timedelta(minutes=-5) or screen_age > timedelta(hours=maximum_age_hours):
        raise ValueError("Vibe candidate screen is stale or future-dated")
    if abs((finished_at - generated_at).total_seconds()) > 3600:
        raise ValueError("Vibe baseline pointer and candidate screen timestamps diverge")
    experiments: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError(f"candidate screen result {index} is invalid")
        candidate = candidates.get(result.get("parent_candidate_id"))
        if candidate is None:
            raise ValueError("candidate screen references an unknown handoff candidate")
        direction = result.get("direction")
        family = result.get("family")
        rules = RULES.get(str(family))
        fixed_rule = result.get("fixed_rule")
        if (
            direction not in {"long", "short"}
            or rules is None
            or result.get("candidate_stage") != "DISCOVERED"
            or not isinstance(result.get("historical_screen_pass"), bool)
            or result.get("paper_candidate") is not False
            or result.get("live_eligible") is not False
            or result.get("broker_symbol") != candidate["broker_symbols"][0]
            or family != candidate["family"]
            or not isinstance(fixed_rule, dict)
            or int(fixed_rule.get("maximum_hold_bars", -1)) != int(rules["maximum_hold_bars"])
            or _number(fixed_rule.get("stop_atr"), "fixed_rule.stop_atr", minimum=0) != float(rules["stop_atr"])
            or any(
                fixed_rule.get(key) is not True
                for key in (
                    "next_bar_open_entry",
                    "completed_bar_signals_only",
                    "conservative_intrabar_stop_priority",
                    "non_overlapping_positions",
                )
            )
        ):
            raise ValueError(f"candidate screen result {result.get('screen_id')} is inconsistent")
        multiple = result.get("multiple_testing")
        if (
            not isinstance(multiple, dict)
            or multiple.get("family") != FDR_FAMILY
            or int(multiple.get("family_trials", -1)) != len(results)
        ):
            raise ValueError("candidate screen multiple-testing family is invalid")
        p_raw = _number(multiple.get("p_raw"), "multiple_testing.p_raw", minimum=0)
        if p_raw > 1:
            raise ValueError("multiple_testing.p_raw must be <= 1")
        p_bonferroni = _number(
            multiple.get("p_bonferroni"),
            "multiple_testing.p_bonferroni",
            minimum=0,
        )
        minimum_dsr = _number(
            multiple.get("minimum_deflated_sharpe_probability"),
            "multiple_testing.minimum_deflated_sharpe_probability",
            minimum=0,
        )
        dsr_value = multiple.get("deflated_sharpe_probability")
        dsr_probability = (
            None
            if dsr_value is None
            else _number(
                dsr_value,
                "multiple_testing.deflated_sharpe_probability",
                minimum=0,
            )
        )
        if p_bonferroni > 1 or minimum_dsr != MINIMUM_HISTORICAL_DSR_PROBABILITY:
            raise ValueError("candidate screen correction thresholds are invalid")
        if dsr_probability is not None and dsr_probability > 1:
            raise ValueError("multiple_testing.deflated_sharpe_probability must be <= 1")
        if result["historical_screen_pass"] and (
            not multiple.get("bh_fdr_q_0_05")
            or p_bonferroni >= 0.05
            or dsr_probability is None
            or dsr_probability < minimum_dsr
            or multiple.get("family_pbo_pass") is not True
            or not pbo_pass
        ):
            raise ValueError("candidate screen pass bypasses a multiple-testing gate")
        if bool(multiple.get("family_pbo_pass")) != pbo_pass:
            raise ValueError("candidate screen PBO decision is inconsistent")
        stop_risk = result.get("minimum_lot_stop_risk")
        if not isinstance(stop_risk, dict):
            raise ValueError("candidate screen minimum-lot stop risk is missing")
        captured_equity = _number(
            stop_risk.get("captured_equity_usd"),
            "minimum_lot_stop_risk.captured_equity_usd",
            minimum=0,
        )
        maximum_risk_fraction = _number(
            stop_risk.get("maximum_risk_fraction"),
            "minimum_lot_stop_risk.maximum_risk_fraction",
            minimum=0,
        )
        measured_risk_fraction = stop_risk.get("maximum_initial_stop_risk_fraction")
        measured_risk_fraction = (
            None
            if measured_risk_fraction is None
            else _number(
                measured_risk_fraction,
                "minimum_lot_stop_risk.maximum_initial_stop_risk_fraction",
                minimum=0,
            )
        )
        risk_pass = bool(
            stop_risk.get("account_currency") == "USD"
            and captured_equity > 0
            and maximum_risk_fraction == MAXIMUM_MINIMUM_LOT_STOP_RISK_FRACTION
            and measured_risk_fraction is not None
            and measured_risk_fraction <= maximum_risk_fraction
        )
        if stop_risk.get("pass") is not risk_pass:
            raise ValueError("candidate screen minimum-lot stop risk is inconsistent")
        if result["historical_screen_pass"] and not risk_pass:
            raise ValueError("candidate screen pass bypasses the minimum-lot risk gate")
        margin = result.get("minimum_lot_margin")
        if not isinstance(margin, dict):
            raise ValueError("candidate screen minimum-lot margin is missing")
        margin_limit = _number(
            margin.get("maximum_free_margin_fraction"),
            "minimum_lot_margin.maximum_free_margin_fraction",
            minimum=0,
        )
        measured_margin = _number(
            margin.get("measured_free_margin_fraction"),
            "minimum_lot_margin.measured_free_margin_fraction",
            minimum=0,
        )
        margin_pass = bool(
            margin.get("account_currency") == "USD"
            and _number(
                margin.get("captured_free_margin_usd"),
                "minimum_lot_margin.captured_free_margin_usd",
                minimum=0,
            )
            > 0
            and margin_limit == MAXIMUM_MINIMUM_LOT_MARGIN_FREE_FRACTION
            and measured_margin <= margin_limit
        )
        if margin.get("pass") is not margin_pass:
            raise ValueError("candidate screen minimum-lot margin is inconsistent")
        if result["historical_screen_pass"] and not margin_pass:
            raise ValueError("candidate screen pass bypasses the minimum-lot margin gate")
        lot = _number(candidate["cost_stress"].get("minimum_lot_reference"), "minimum lot", minimum=0)
        slippage = _number(
            candidate["cost_stress"].get("slippage_points_round_trip"),
            "slippage points",
            minimum=0,
        )
        fingerprint = _spec_fingerprint(candidate, result, str(direction))
        experiment_key = f"{result['screen_id']}:{fingerprint[:12]}"
        experiments.append(
            {
                "experiment_key": experiment_key,
                "screen_id": result["screen_id"],
                "parent_candidate_id": result["parent_candidate_id"],
                "spec_fingerprint": fingerprint,
                "symbol": result["broker_symbol"],
                "source_symbol": candidate["source_symbols"][0],
                "timeframe": "H1",
                "family": family,
                "direction": direction,
                "maximum_hold_bars": int(rules["maximum_hold_bars"]),
                "stop_atr": float(rules["stop_atr"]),
                "minimum_lot": lot,
                "slippage_points_round_trip": slippage,
                "historical_screen_verdict": result.get("historical_screen_verdict"),
                "historical_screen_pass": result.get("historical_screen_pass") is True,
                "historical_oos_trades": int(result.get("oos", {}).get("trades", 0)),
                "p_raw": p_raw,
                "source_screen_sha256": file_sha256(screen_path),
                "source_handoff_sha256": file_sha256(handoff_path),
                "screen_generated_at": generated_at.isoformat(),
                "paper_only": True,
                "order_authority": False,
                "live_eligible": False,
            }
        )
    return VibeArtifacts(
        generated_at=generated_at,
        bundle_manifest_sha256=manifest_sha256,
        baseline_sha256=file_sha256(report_path),
        handoff_sha256=file_sha256(handoff_path),
        screen_sha256=file_sha256(screen_path),
        experiments=tuple(experiments),
        report_path=report_path,
        handoff_path=handoff_path,
        screen_path=screen_path,
    )


def register_screen_trials(
    artifacts: VibeArtifacts,
    ledger: FDRLedger,
) -> dict[str, bool]:
    """Record each result once per immutable screen and return cumulative decisions."""
    existing = {
        (trial.spec_id, str(trial.meta.get("screen_sha256") or ""))
        for trial in ledger.family_trials(FDR_FAMILY)
    }
    for experiment in artifacts.experiments:
        key = (experiment["experiment_key"], artifacts.screen_sha256)
        if key in existing:
            continue
        ledger.record(
            FDR_FAMILY,
            experiment["experiment_key"],
            experiment["p_raw"],
            n=experiment["historical_oos_trades"],
            screen_id=experiment["screen_id"],
            screen_sha256=artifacts.screen_sha256,
            handoff_sha256=artifacts.handoff_sha256,
        )
        existing.add(key)
    return {
        experiment["experiment_key"]: ledger.is_discovery(
            FDR_FAMILY, experiment["experiment_key"]
        )
        for experiment in artifacts.experiments
    }


def paper_forward_enrollments(
    experiments: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    discoveries: Mapping[str, bool],
) -> tuple[dict[str, Any], ...]:
    """Authorize only statistically screened specs for quote-only observation."""
    enrolled: list[dict[str, Any]] = []
    for experiment in experiments:
        key = experiment.get("experiment_key")
        if not isinstance(key, str):
            continue
        if experiment.get("historical_screen_pass") is not True:
            continue
        if discoveries.get(key) is not True:
            continue
        enrolled.append(
            {
                **experiment,
                "paper_forward_enrolled": True,
                "paper_forward_enrollment_gate": PAPER_FORWARD_ENROLLMENT_GATE,
                "paper_only": True,
                "order_authority": False,
                "live_eligible": False,
            }
        )
    return tuple(enrolled)


def empty_state() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "mode": "quote_only_shadow",
        "order_authority": False,
        "automatic_live_promotion": False,
        "updated_at_host_utc": None,
        "active_screen_sha256": None,
        "experiment_catalog": [],
        "attempted_signals": [],
        "open_positions": [],
        "closed_trades": [],
    }


def validate_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Vibe shadow state must be an object")
    if (
        payload.get("schema") != STATE_SCHEMA
        or payload.get("mode") != "quote_only_shadow"
        or payload.get("order_authority") is not False
        or payload.get("automatic_live_promotion") is not False
    ):
        raise ValueError("Vibe shadow state boundary is invalid")
    for key in ("experiment_catalog", "attempted_signals", "open_positions", "closed_trades"):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"Vibe shadow state {key} must be a list")
    for experiment in payload["experiment_catalog"]:
        if (
            not isinstance(experiment, dict)
            or not isinstance(experiment.get("experiment_key"), str)
            or experiment.get("paper_only") is not True
            or experiment.get("order_authority") is not False
            or experiment.get("live_eligible") is not False
        ):
            raise ValueError("Vibe shadow experiment catalog boundary is invalid")
    for position in payload["open_positions"]:
        if (
            not isinstance(position, dict)
            or position.get("paper_only") is not True
            or position.get("order_authority") is not False
        ):
            raise ValueError("Vibe shadow open-position boundary is invalid")
    return payload


def load_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    if not state_path.exists():
        return empty_state()
    return validate_state(_read_json(state_path, "Vibe shadow state"))


def paper_trade_result(
    *,
    side: str,
    entry_price: float,
    exit_price: float,
    volume: float,
    tick_size: float,
    tick_value: float,
    point: float,
    slippage_points_round_trip: float,
) -> dict[str, float]:
    """Value quote-to-quote P/L plus declared adverse round-trip slippage."""
    if volume <= 0 or tick_size <= 0 or tick_value <= 0 or point <= 0:
        raise ValueError("invalid paper-trade instrument metadata")
    if side not in {"long", "short"}:
        raise ValueError("paper-trade side must be long or short")
    direction = 1.0 if side == "long" else -1.0
    gross = direction * (exit_price - entry_price) / tick_size * tick_value * volume
    slippage = max(float(slippage_points_round_trip), 0.0) * point / tick_size * tick_value * volume
    return {
        "gross_pnl_usd": gross,
        "slippage_budget_usd": slippage,
        "net_pnl_usd": gross - slippage,
    }


def merge_experiment_catalog(
    existing: list[dict[str, Any]],
    incoming: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist fixed specs so later regime changes cannot censor forward evidence."""
    catalog = {
        item["experiment_key"]: dict(item)
        for item in existing
        if isinstance(item, dict) and isinstance(item.get("experiment_key"), str)
    }
    for experiment in incoming:
        key = experiment["experiment_key"]
        prior = catalog.get(key, {})
        enrolled_at = prior.get("enrolled_at_host_utc") or experiment["screen_generated_at"]
        enrollment = _utc_timestamp(enrolled_at, "experiment.enrolled_at_host_utc")
        observation_end = prior.get("observation_end_host_utc") or (
            enrollment + timedelta(days=OBSERVATION_DAYS)
        ).isoformat()
        catalog[key] = {
            **prior,
            **experiment,
            "enrolled_at_host_utc": enrollment.isoformat(),
            "observation_end_host_utc": observation_end,
        }
    return sorted(
        catalog.values(),
        key=lambda item: (str(item.get("enrolled_at_host_utc") or ""), item["experiment_key"]),
    )


def entry_eligible_experiments(
    catalog: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    discoveries: Mapping[str, bool],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return gated specs still significant and inside their observation budget."""
    reference = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    counts: dict[str, int] = {}
    for trade in closed_trades:
        key = trade.get("experiment_key") if isinstance(trade, dict) else None
        if isinstance(key, str):
            counts[key] = counts.get(key, 0) + 1
    eligible: list[dict[str, Any]] = []
    for experiment in catalog:
        key = experiment.get("experiment_key")
        if not isinstance(key, str):
            continue
        if experiment.get("paper_forward_enrolled") is not True:
            continue
        if experiment.get("historical_screen_pass") is not True:
            continue
        if discoveries.get(key) is not True:
            continue
        try:
            observation_end = _utc_timestamp(
                experiment.get("observation_end_host_utc"),
                "experiment.observation_end_host_utc",
            )
        except ValueError:
            continue
        if (
            reference <= observation_end
            and counts.get(key, 0) < MAX_FORWARD_TRADES_PER_EXPERIMENT
        ):
            eligible.append(experiment)
    return eligible


def _profit_factor(values: list[float]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return math.inf if losses == 0 and gains > 0 else (gains / losses if losses > 0 else 0.0)


def _max_drawdown(values: list[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def forward_metrics(
    experiments: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    discoveries: Mapping[str, bool],
) -> list[dict[str, Any]]:
    """Grade independent quote-only results; never grant live eligibility."""
    reports: list[dict[str, Any]] = []
    for experiment in experiments:
        key = experiment["experiment_key"]
        trades = [trade for trade in closed_trades if trade.get("experiment_key") == key]
        values = [float(trade.get("net_pnl_usd") or 0.0) for trade in trades]
        count = len(values)
        net = sum(values)
        profit_factor = _profit_factor(values)
        lcb = block_bootstrap_mean_lcb(
            values,
            samples=BOOTSTRAP_SAMPLES,
            block_size=BOOTSTRAP_BLOCK_TRADES,
            seed=int(experiment["spec_fingerprint"][:8], 16),
        ) if values else None
        reasons: list[str] = []
        if not experiment["historical_screen_pass"]:
            reasons.append("historical fixed-rule screen did not pass")
        if not discoveries.get(key, False):
            reasons.append("latest result is not a cumulative BH-FDR discovery")
        if count < MIN_FORWARD_TRADES:
            reasons.append(f"independent forward trades {count} < {MIN_FORWARD_TRADES}")
        if net <= 0:
            reasons.append("independent forward net P/L is not positive")
        if profit_factor < MIN_FORWARD_PROFIT_FACTOR:
            reasons.append(
                f"independent forward profit factor {profit_factor:.2f} < {MIN_FORWARD_PROFIT_FACTOR:.2f}"
            )
        if lcb is None or lcb <= 0:
            reasons.append("95% block-bootstrap lower bound for forward mean P/L is not positive")
        reports.append(
            {
                **experiment,
                "cumulative_fdr_discovery": bool(discoveries.get(key, False)),
                "forward": {
                    "trades": count,
                    "wins": sum(value > 0 for value in values),
                    "net_pnl_usd": round(net, 6),
                    "mean_net_pnl_usd": round(net / count, 6) if count else None,
                    "profit_factor": None if math.isinf(profit_factor) else round(profit_factor, 6),
                    "max_drawdown_usd": round(_max_drawdown(values), 6),
                    "bootstrap_mean_lcb_95_usd": round(lcb, 6) if lcb is not None else None,
                },
                "paper_evidence_gate_pass": not reasons,
                "readiness_reasons": reasons,
                "manual_live_authorization_present": False,
                "automatic_live_promotion": False,
                "live_eligible": False,
            }
        )
    return reports


def build_report(
    *,
    state: Mapping[str, Any],
    artifacts: VibeArtifacts | None,
    experiments: list[dict[str, Any]],
    artifact_status: str,
    artifact_reason: str | None,
    clock: Mapping[str, Any] | None,
    actions: list[dict[str, Any]],
    open_position_marks: list[dict[str, Any]],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    now = (generated_at or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    closed = list(state.get("closed_trades") or [])
    realized_net = sum(float(trade.get("net_pnl_usd") or 0.0) for trade in closed)
    unrealized_gross = sum(
        float(mark.get("gross_pnl_usd") or 0.0)
        for mark in open_position_marks
        if mark.get("mark_available") is True
    )
    unrealized_net = sum(
        float(mark.get("net_pnl_usd") or 0.0)
        for mark in open_position_marks
        if mark.get("mark_available") is True
    )
    return {
        "schema": REPORT_SCHEMA,
        "generated_at_host_utc": now.isoformat(),
        "mode": "quote_only_shadow",
        "order_authority": False,
        "automatic_live_promotion": False,
        "manual_live_authorization_present": False,
        "live_eligible_count": 0,
        "artifact_status": artifact_status,
        "artifact_reason": artifact_reason,
        "source_screen_sha256": artifacts.screen_sha256 if artifacts else None,
        "clock": dict(clock or {}),
        "experiment_count": len(experiments),
        "active_entry_experiment_count": sum(
            experiment.get("observation_active") is True for experiment in experiments
        ),
        "open_position_count": len(state.get("open_positions") or []),
        "marked_open_position_count": sum(
            mark.get("mark_available") is True for mark in open_position_marks
        ),
        "closed_trade_count": len(closed),
        "paper_net_pnl_usd": round(realized_net, 6),
        "paper_unrealized_gross_pnl_usd": round(unrealized_gross, 6),
        "paper_unrealized_net_if_closed_usd": round(unrealized_net, 6),
        "paper_total_net_if_closed_usd": round(realized_net + unrealized_net, 6),
        "open_position_marks": open_position_marks,
        "paper_evidence_gate_pass_count": sum(
            experiment.get("paper_evidence_gate_pass") is True for experiment in experiments
        ),
        "actions": actions,
        "experiments": experiments,
        "limitations": [
            "Shadow fills use sampled executable quotes, not broker fills or historical tick replay.",
            "Open-position marks are estimates if closed now and never count as forward evidence.",
            "A paper evidence pass is not live authorization and does not predict profit.",
            "This component contains no broker order path and cannot promote itself to live trading.",
        ],
    }
