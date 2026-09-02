"""ci/regen_manifest.py must rewrite exactly the stale hashes and nothing else."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ci"))

import regen_manifest  # noqa: E402


def _write_manifest(tmp_path: Path, entries: list[dict], *, bom: bool) -> Path:
    body = json.dumps({"schema_version": 1, "files": entries}, indent=2) + "\n"
    manifest = tmp_path / "hotfix-manifest.json"
    manifest.write_bytes((b"\xef\xbb\xbf" if bom else b"") + body.encode("utf-8"))
    return manifest


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_up_to_date_manifest_is_left_alone(tmp_path: Path, capsys) -> None:
    src = tmp_path / "a.py"
    src.write_text("print('a')\n", encoding="utf-8")
    manifest = _write_manifest(tmp_path, [{"source": "a.py", "destination": "scripts/a.py", "sha256": _sha(src)}], bom=True)
    before = manifest.read_bytes()

    assert regen_manifest.regenerate(manifest) == 0
    assert manifest.read_bytes() == before
    assert "already match" in capsys.readouterr().out


def test_stale_hash_is_rewritten_and_bom_preserved(tmp_path: Path) -> None:
    src = tmp_path / "a.py"
    src.write_text("print('a')\n", encoding="utf-8")
    other = tmp_path / "b.py"
    other.write_text("print('b')\n", encoding="utf-8")
    manifest = _write_manifest(
        tmp_path,
        [
            {"source": "a.py", "destination": "scripts/a.py", "sha256": "0" * 64},
            {"source": "b.py", "destination": "scripts/b.py", "sha256": _sha(other)},
        ],
        bom=True,
    )

    assert regen_manifest.regenerate(manifest) == 0

    raw = manifest.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "PowerShell wrote the BOM; keep it"
    data = json.loads(raw.decode("utf-8-sig"))
    assert data["files"][0]["sha256"] == _sha(src)
    assert data["files"][1]["sha256"] == _sha(other)
    assert data["files"][1]["destination"] == "scripts/b.py"


def test_check_mode_reports_without_writing(tmp_path: Path, capsys) -> None:
    src = tmp_path / "a.py"
    src.write_text("print('a')\n", encoding="utf-8")
    manifest = _write_manifest(tmp_path, [{"source": "a.py", "destination": "scripts/a.py", "sha256": "f" * 64}], bom=False)
    before = manifest.read_bytes()

    assert regen_manifest.regenerate(manifest, check=True) == 1
    assert manifest.read_bytes() == before
    assert "would update: a.py" in capsys.readouterr().out


def test_missing_source_refuses_to_rewrite(tmp_path: Path, capsys) -> None:
    manifest = _write_manifest(tmp_path, [{"source": "gone.py", "destination": "scripts/gone.py", "sha256": "a" * 64}], bom=True)
    before = manifest.read_bytes()

    assert regen_manifest.regenerate(manifest) == 2
    assert manifest.read_bytes() == before
    assert "gone.py" in capsys.readouterr().err


def test_real_manifest_is_currently_consistent() -> None:
    # The committed manifest must agree with the committed sources; this is the same
    # invariant verify_deploy.py enforces, checked from the regenerator's side.
    assert regen_manifest.regenerate(REPO_ROOT / "hotfix-manifest.json", check=True) == 0
