from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hotfix" / "scripts"))

import vps_health  # noqa: E402


def _screen(
    *,
    pbo: float | None,
    pbo_status: str = "AVAILABLE",
    family_pbo_pass: bool = False,
    historical_pass: bool = False,
    dsr: float = 0.10,
) -> dict:
    return {
        "method": {
            "deflated_sharpe_required": True,
            "pbo_cscv_required": True,
            "minimum_deflated_sharpe_probability": 0.95,
            "maximum_probability_backtest_overfitting": 0.20,
            "minimum_lot_stop_risk_required": True,
            "maximum_minimum_lot_stop_risk_fraction": 0.02,
        },
        "selection_overfitting": {
            "method": "combinatorially_symmetric_cross_validation",
            "partitions": 8,
            "status": pbo_status,
            "maximum_probability_backtest_overfitting": 0.20,
            "probability_backtest_overfitting": pbo,
            "evaluated_splits": 70 if pbo_status == "AVAILABLE" else 0,
        },
        "results": [
            {
                "historical_screen_pass": historical_pass,
                "multiple_testing": {
                    "minimum_deflated_sharpe_probability": 0.95,
                    "deflated_sharpe_probability": dsr,
                    "family_pbo_pass": family_pbo_pass,
                },
                "minimum_lot_stop_risk": {
                    "account_currency": "USD",
                    "maximum_risk_fraction": 0.02,
                    "maximum_initial_stop_risk_fraction": 0.01,
                    "pass": True,
                },
            }
        ],
    }


def test_low_pbo_family_is_accepted_when_candidate_contract_is_valid():
    summary, problems = vps_health._vibe_screen_statistics(
        _screen(
            pbo=0.10,
            family_pbo_pass=True,
            historical_pass=True,
            dsr=0.99,
        )
    )

    assert problems == []
    assert summary["statistical_gate_status"] == "VALID"
    assert summary["research_family_decision"] == "PBO_ACCEPTED"


def test_high_pbo_family_is_valid_research_rejection_not_health_failure():
    summary, problems = vps_health._vibe_screen_statistics(_screen(pbo=0.257142857143))

    assert problems == []
    assert summary["statistical_gate_status"] == "VALID"
    assert summary["research_family_decision"] == "REJECTED_OVERFIT_RISK"


def test_insufficient_data_is_valid_research_rejection():
    summary, problems = vps_health._vibe_screen_statistics(
        _screen(pbo=None, pbo_status="INSUFFICIENT_DATA")
    )

    assert problems == []
    assert summary["statistical_gate_status"] == "VALID"
    assert summary["research_family_decision"] == "REJECTED_INSUFFICIENT_EVIDENCE"


def test_malformed_pbo_artifact_is_health_failure():
    screen = _screen(pbo=0.10, family_pbo_pass=True)
    del screen["selection_overfitting"]["method"]

    summary, problems = vps_health._vibe_screen_statistics(screen)

    assert problems
    assert summary["statistical_gate_status"] == "INVALID"
    assert summary["research_family_decision"] == "INVALID_ARTIFACT"


def test_candidate_pass_cannot_bypass_minimum_lot_risk():
    screen = _screen(
        pbo=0.10,
        family_pbo_pass=True,
        historical_pass=True,
        dsr=0.99,
    )
    risk = screen["results"][0]["minimum_lot_stop_risk"]
    risk["maximum_initial_stop_risk_fraction"] = 0.03
    risk["pass"] = False

    summary, problems = vps_health._vibe_screen_statistics(screen)

    assert "Vibe candidate pass bypasses minimum-lot stop risk" in problems
    assert summary["statistical_gate_status"] == "INVALID"
