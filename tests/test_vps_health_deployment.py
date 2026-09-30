from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hotfix" / "scripts"))

import vps_health  # noqa: E402


def _configure_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    receipt = tmp_path / "deployment_receipt.json"
    manifest = tmp_path / "hotfix-manifest.json"
    deployed = tmp_path / "last_deploy_sha.txt"
    completed = tmp_path / "last_update_complete.txt"
    monkeypatch.setattr(vps_health, "DEPLOYMENT_RECEIPT_FILE", receipt)
    monkeypatch.setattr(vps_health, "DEPLOYMENT_MANIFEST_FILE", manifest)
    monkeypatch.setattr(vps_health, "DEPLOY_SUCCESS_FILE", deployed)
    monkeypatch.setattr(vps_health, "UPDATE_COMPLETION_FILE", completed)
    monkeypatch.setattr(vps_health, "DEPLOYED_REPO_ROOT", tmp_path / "installed")
    return receipt, manifest, deployed, completed


def _write_integrity_manifest(monkeypatch, tmp_path: Path, manifest: Path) -> str:
    installed = vps_health.DEPLOYED_REPO_ROOT / "scripts"
    installed.mkdir(parents=True)
    target = installed / "vps_health.py"
    target.write_text("verified deployment", encoding="utf-8")
    payload = {
        "schema_version": 1,
        "files": [{
            "destination": "scripts/vps_health.py",
            "sha256": vps_health._file_sha256(target),
        }],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return vps_health._file_sha256(manifest)


def test_deployment_receipt_proves_exact_commit(monkeypatch, tmp_path: Path):
    receipt, manifest, deployed, completed = _configure_paths(monkeypatch, tmp_path)
    commit = "a" * 40
    receipt.write_text(
        json.dumps(
            {
                "schema": "mt5.deployment_receipt.v1",
                "commit": commit,
                "completed_at": "2026-09-30T12:00:00Z",
                "manifest_sha256": _write_integrity_manifest(monkeypatch, tmp_path, manifest),
                "hotfix_file_count": 1,
                "live_authorization_changed": False,
            }
        ),
        encoding="utf-8",
    )
    deployed.write_text(commit, encoding="utf-8")
    completed.write_text(commit, encoding="utf-8")

    result = vps_health.check_deployment_receipt(
        datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc)
    )

    assert result["status"] == "OK"
    assert result["commit"] == commit
    assert result["age_hours"] == 1.0
    assert result["verified_hotfix_files"] == 1


def test_deployment_receipt_warns_on_marker_mismatch(monkeypatch, tmp_path: Path):
    receipt, _manifest, deployed, completed = _configure_paths(monkeypatch, tmp_path)
    commit = "a" * 40
    receipt.write_text(
        json.dumps(
            {
                "schema": "mt5.deployment_receipt.v1",
                "commit": commit,
                "completed_at": "2026-09-30T12:00:00Z",
                "manifest_sha256": "b" * 64,
                "hotfix_file_count": 60,
                "live_authorization_changed": False,
            }
        ),
        encoding="utf-8",
    )
    deployed.write_text("c" * 40, encoding="utf-8")
    completed.write_text(commit, encoding="utf-8")

    result = vps_health.check_deployment_receipt(
        datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc)
    )

    assert result["status"] == "WARN"
    assert "last_deploy_sha does not match" in result["reason"]


def test_deployment_receipt_warns_when_installed_hotfix_changes(monkeypatch, tmp_path: Path):
    receipt, manifest, deployed, completed = _configure_paths(monkeypatch, tmp_path)
    commit = "a" * 40
    manifest_sha = _write_integrity_manifest(monkeypatch, tmp_path, manifest)
    target = vps_health.DEPLOYED_REPO_ROOT / "scripts" / "vps_health.py"
    target.write_text("tampered after deployment", encoding="utf-8")
    receipt.write_text(
        json.dumps(
            {
                "schema": "mt5.deployment_receipt.v1",
                "commit": commit,
                "completed_at": "2026-09-30T12:00:00Z",
                "manifest_sha256": manifest_sha,
                "hotfix_file_count": 1,
                "live_authorization_changed": False,
            }
        ),
        encoding="utf-8",
    )
    deployed.write_text(commit, encoding="utf-8")
    completed.write_text(commit, encoding="utf-8")

    result = vps_health.check_deployment_receipt(
        datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc)
    )

    assert result["status"] == "WARN"
    assert "deployed hotfix hash mismatch" in result["reason"]


def test_edge_registry_health_warns_for_unvalidated_live_record(monkeypatch, tmp_path: Path):
    registry = tmp_path / "edge_registry.json"
    monkeypatch.setattr(vps_health, "EDGE_REGISTRY_FILE", registry)
    registry.write_text(
        json.dumps(
            {"edges": [{
                "key": "UNSAFE", "magic": 88010, "symbol": "GOLD", "weekday": 1,
                "entry_hour": 1, "exit_hour": 2, "side": "long", "stage": "LIVE",
                "validation": {"bonferroni": False},
            }]}
        ),
        encoding="utf-8",
    )

    result = vps_health.check_edge_registry()

    assert result["status"] == "WARN"
    assert result["unvalidated_live_keys"] == ["UNSAFE"]


def test_edge_registry_health_rejects_malformed_record(monkeypatch, tmp_path: Path):
    registry = tmp_path / "edge_registry.json"
    monkeypatch.setattr(vps_health, "EDGE_REGISTRY_FILE", registry)
    registry.write_text(json.dumps({"edges": [{"key": "missing fields"}]}), encoding="utf-8")

    result = vps_health.check_edge_registry()

    assert result["status"] == "WARN"
    assert "malformed" in result["reason"]


def test_structural_health_does_not_report_live_without_registry_eligible_edge(monkeypatch, tmp_path: Path):
    allowlist = tmp_path / "allowlist.json"
    events = tmp_path / "events.jsonl"
    registry = tmp_path / "edge_registry.json"
    allowlist.write_text(json.dumps({"enabled_magics": [88001]}), encoding="utf-8")
    events.write_text("{}\n", encoding="utf-8")
    registry.write_text(json.dumps({"edges": []}), encoding="utf-8")
    monkeypatch.setattr(vps_health, "SCHEDULER_ALLOWLIST", allowlist)
    monkeypatch.setattr(vps_health, "SCHEDULER_EVENTS", events)
    monkeypatch.setattr(vps_health, "EDGE_REGISTRY_FILE", registry)
    monkeypatch.setattr(vps_health, "persistent_user_flag_enabled", lambda _name: True)

    result = vps_health.check_structural_scheduler()

    assert result["mode"] == "ARMED_NO_ELIGIBLE_EDGE"
    assert result["effective_allowlisted_magics"] == []
    assert result["status"] == "WARN"


def test_structural_research_health_requires_fresh_completed_bounded_artifact(monkeypatch, tmp_path: Path):
    state = tmp_path / "structural_walk_forward_state.json"
    monkeypatch.setattr(vps_health, "__file__", str(tmp_path / "vps_health.py"))
    report_root = tmp_path / "reports"
    report_root.mkdir(parents=True, exist_ok=True)
    report = report_root / "health-test-structural.json"
    report.write_text(json.dumps({
        "mode": "read_only_research", "orders_sent": 0,
        "research_scope": {
            "symbols": sorted(vps_health.STRUCTURAL_CANONICAL_SYMBOLS),
            "canonical_full_universe": True, "paper_eligibility_allowed": True,
            "fdr_family": "structural_hourweekday",
        },
    }), encoding="utf-8")
    state.write_text(json.dumps({
        "schema": "mt5.structural_walk_forward_state.v1", "status": "completed",
        "finished_at_utc": "2026-09-30T12:00:00Z", "output": str(report),
        "report_sha256": vps_health._file_sha256(report),
        "order_authority": False,
    }), encoding="utf-8")
    monkeypatch.setattr(vps_health, "STRUCTURAL_RESEARCH_STATE", state)

    result = vps_health.check_structural_research(datetime(2026, 9, 30, 13, tzinfo=timezone.utc))

    assert result["status"] == "OK"
    assert result["age_hours"] == 1.0


def test_structural_research_health_rejects_report_hash_mismatch(monkeypatch, tmp_path: Path):
    state = tmp_path / "structural_walk_forward_state.json"
    monkeypatch.setattr(vps_health, "__file__", str(tmp_path / "vps_health.py"))
    report_root = tmp_path / "reports"
    report_root.mkdir(parents=True, exist_ok=True)
    report = report_root / "health-test-structural.json"
    report.write_text(json.dumps({
        "mode": "read_only_research", "orders_sent": 0,
        "research_scope": {
            "symbols": sorted(vps_health.STRUCTURAL_CANONICAL_SYMBOLS),
            "canonical_full_universe": True, "paper_eligibility_allowed": True,
            "fdr_family": "structural_hourweekday",
        },
    }), encoding="utf-8")
    state.write_text(json.dumps({
        "schema": "mt5.structural_walk_forward_state.v1", "status": "completed",
        "finished_at_utc": "2026-09-30T12:00:00Z", "output": str(report),
        "report_sha256": "0" * 64, "order_authority": False,
    }), encoding="utf-8")
    monkeypatch.setattr(vps_health, "STRUCTURAL_RESEARCH_STATE", state)

    result = vps_health.check_structural_research(datetime(2026, 9, 30, 13, tzinfo=timezone.utc))

    assert result["status"] == "WARN"
    assert "report hash mismatch" in result["reason"]


def test_structural_research_health_rejects_hash_valid_diagnostic_scope(monkeypatch, tmp_path: Path):
    state = tmp_path / "structural_walk_forward_state.json"
    monkeypatch.setattr(vps_health, "__file__", str(tmp_path / "vps_health.py"))
    report_root = tmp_path / "reports"
    report_root.mkdir(parents=True, exist_ok=True)
    report = report_root / "diagnostic.json"
    report.write_text(json.dumps({
        "mode": "read_only_research", "orders_sent": 0,
        "research_scope": {"symbols": ["GOLD"], "canonical_full_universe": False,
                           "paper_eligibility_allowed": False, "fdr_family": "manual"},
    }), encoding="utf-8")
    state.write_text(json.dumps({
        "schema": "mt5.structural_walk_forward_state.v1", "status": "completed",
        "finished_at_utc": "2026-09-30T12:00:00Z", "output": str(report),
        "report_sha256": vps_health._file_sha256(report), "order_authority": False,
    }), encoding="utf-8")
    monkeypatch.setattr(vps_health, "STRUCTURAL_RESEARCH_STATE", state)

    result = vps_health.check_structural_research(datetime(2026, 9, 30, 13, tzinfo=timezone.utc))

    assert result["status"] == "WARN"
    assert "not a canonical" in result["reason"]


def test_structural_paper_challenger_health_reports_zero_without_warning(monkeypatch, tmp_path: Path):
    state = tmp_path / "structural_walk_forward_state.json"
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "mode": "read_only_research", "orders_sent": 0,
        "research_scope": {"symbols": sorted(vps_health.STRUCTURAL_CANONICAL_SYMBOLS), "canonical_full_universe": True,
                           "paper_eligibility_allowed": True, "fdr_family": "structural_hourweekday"},
        "paper_candidates": [],
    }), encoding="utf-8")
    state.write_text(json.dumps({
        "schema": "mt5.structural_walk_forward_state.v1", "status": "completed",
        "finished_at_utc": "2026-09-30T12:00:00Z", "output": str(report),
        "report_sha256": vps_health._file_sha256(report), "order_authority": False,
    }), encoding="utf-8")
    monkeypatch.setattr(vps_health, "STRUCTURAL_RESEARCH_STATE", state)

    result = vps_health.check_structural_paper_challengers(
        datetime(2026, 9, 30, 13, tzinfo=timezone.utc)
    )

    assert result["status"] == "OK"
    assert result["paper_challenger_count"] == 0


def test_structural_paper_forward_health_rejects_duplicate_symbol(monkeypatch, tmp_path: Path):
    state = tmp_path / "structural_paper_forward_state.json"
    state.write_text(json.dumps({"open_positions": [
        {"signal": "A", "symbol": "GOLD"},
        {"signal": "B", "symbol": "GOLD"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(vps_health, "STRUCTURAL_PAPER_FORWARD_STATE", state)

    result = vps_health.check_structural_paper_forward_state()

    assert result["status"] == "WARN"
    assert "symbol concurrency" in result["reason"]
