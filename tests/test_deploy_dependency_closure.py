from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_verifier_checks_manifest_python_dependency_closure():
    source = (ROOT / "ci" / "verify_deploy.py").read_text(encoding="utf-8")

    assert "def check_manifest_python_dependency_closure" in source
    assert 'fail(\n                    "unclosed-manifest-import"' in source
    assert "check_manifest_python_dependency_closure(files)" in source
