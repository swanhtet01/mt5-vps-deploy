"""Text-level contract tests for update.ps1, the script that installs onto the live VPS.

update.ps1 cannot run off Windows (it needs schtasks, an admin token and the broker tree),
so these tests pin the parts of it that other components DEPEND on, by reading the file:

* it writes ``installed-manifest.json`` (the drift-detection record vps_health.py reads),
  and does so only after the LAST hotfix sync and before the completion marker -- a failed
  sync throws and never reaches the write, so the record can never claim a deploy that did
  not land;
* the admin guard still uses ``throw`` -- a ``return`` there once let auto_deploy.ps1 bank
  a deploy that installed nothing (CLAUDE.md section 4);
* the MT5-AutoDeploy task is still registered with ``/rl HIGHEST`` -- without it the
  self-updater runs with a filtered token under UAC and every update is a silent no-op.

When a PowerShell 7 binary is available the file is also parsed with the real parser and
the new ``Write-InstalledManifest`` function is executed against a fake manifest, so the
JSON schema is checked by running it rather than by eyeballing it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_PS1 = REPO_ROOT / "update.ps1"
TEXT = UPDATE_PS1.read_text(encoding="utf-8")
LINES = TEXT.splitlines()

INVOCATION = re.compile(r"^\s*Sync-Hotfixes\s*$")
MARKER_WRITE = "last_update_complete.txt"
RECORD_FILE = "installed-manifest.json"


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _first_line_index(needle: str, *, code_only: bool = False) -> int:
    for index, line in enumerate(LINES):
        if needle in line and not (code_only and _is_comment(line)):
            return index
    raise AssertionError(f"{needle!r} not found in update.ps1")


def _pwsh() -> str | None:
    """PowerShell 7 binary, if any: $PWSH, then PATH, then the scratch copy used locally."""
    candidates = [
        os.environ.get("PWSH"),
        shutil.which("pwsh"),
        "/tmp/claude-0/-home-user-supermega-workspace/44d44bdb-0030-54c3-8142-62d59e618975/scratchpad/pwsh/pwsh",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _function_source(name: str) -> str:
    """Extract a top-level ``function <name> { ... }`` block (closing brace at column 0)."""
    start = next(
        (i for i, line in enumerate(LINES) if line.startswith(f"function {name} {{")),
        None,
    )
    assert start is not None, f"function {name} is not defined in update.ps1"
    end = next(i for i in range(start, len(LINES)) if LINES[i] == "}")
    return "\n".join(LINES[start : end + 1])


needs_pwsh = pytest.mark.skipif(
    _pwsh() is None,
    reason="no PowerShell 7 binary: set $PWSH or put `pwsh` on PATH to run this check",
)


# --- the installed-manifest record ---------------------------------------------------------


def test_installed_manifest_record_is_written():
    assert RECORD_FILE in TEXT
    # The write goes to the deploy root next to the other markers, not into the code tree.
    write_line = LINES[_first_line_index(RECORD_FILE, code_only=True)]
    assert f'"$deploy\\{RECORD_FILE}"' in write_line


def test_record_is_written_after_last_sync_and_before_completion_marker():
    invocations = [i for i, line in enumerate(LINES) if INVOCATION.match(line)]
    assert len(invocations) == 2, "expected the initial sync and the post-bundle re-sync"
    write = _first_line_index(RECORD_FILE, code_only=True)
    marker = _first_line_index(MARKER_WRITE, code_only=True)
    assert "Set-Content" in LINES[marker]
    assert max(invocations) < write < marker


def test_record_write_uses_the_manifest_captured_on_the_sync_success_path():
    """The parsed manifest is exported only after the whole batch was committed.

    Sync-Hotfixes rolls back and throws on any failure; the export must sit after the
    "applied ... as one batch" line and before the function's finally, so no failure path
    can publish a manifest that was not actually installed.
    """
    export = _first_line_index("$script:appliedManifest = $manifest", code_only=True)
    applied = _first_line_index("verified hotfixes as one batch", code_only=True)
    finally_index = next(i for i in range(applied, len(LINES)) if "} finally {" in LINES[i])
    assert applied < export < finally_index
    write_line = LINES[_first_line_index(RECORD_FILE, code_only=True)]
    assert "-Manifest $script:appliedManifest" in write_line
    assert "-DeploySha $deployRef" in write_line


def test_record_schema_keys_are_the_ones_the_health_check_reads():
    source = _function_source("Write-InstalledManifest")
    for key in ("deploy_sha", "applied_utc", "files", "destination", "sha256"):
        assert key in source
    # Reuses the already-verified entries: no second download and no re-hashing.
    assert "Invoke-WebRequest" not in source
    assert "Get-FileHash" not in source
    # UTF-8 without a BOM, like the other generated files, and staged-then-renamed.
    assert "UTF8Encoding($false)" in source
    assert "Move-Item $staged $Path -Force" in source


# --- regression pins from past outages -----------------------------------------------------


def test_admin_guard_throws_instead_of_returning():
    start = next(
        i for i, line in enumerate(LINES)
        if "IsInRole" in line and "Administrator" in line and line.lstrip().startswith("if (-not")
    )
    end = next(i for i in range(start, len(LINES)) if LINES[i] == "}")
    body = [line.strip() for line in LINES[start + 1 : end] if not _is_comment(line)]
    assert any(line.startswith("throw ") for line in body), "admin guard must throw"
    assert not any(line.startswith("return") for line in body), (
        "a `return` in the admin guard is not a terminating error under Invoke-Expression; "
        "auto_deploy.ps1 would bank the deploy as successful"
    )


def test_autodeploy_task_is_registered_elevated():
    line = LINES[_first_line_index("/tn 'MT5-AutoDeploy'", code_only=True)]
    assert "schtasks /create" in line
    assert "/rl HIGHEST" in line


def test_completion_marker_is_the_last_write_and_names_the_deploy_commit():
    marker = _first_line_index(MARKER_WRITE, code_only=True)
    assert "$deployRef" in LINES[marker]
    tail = [line for line in LINES[marker + 1 :] if not _is_comment(line) and line.strip()]
    assert all(line.startswith("Write-Host") for line in tail), (
        "nothing but console output may follow the completion marker"
    )


# --- checks that need a real PowerShell parser ---------------------------------------------


@needs_pwsh
def test_update_ps1_parses_with_powershell():
    command = (
        "$errs = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{UPDATE_PS1}', [ref]$null, [ref]$errs) | Out-Null; "
        "if ($errs) { $errs | ForEach-Object { $_.ToString() }; exit 1 } else { 'parse ok' }"
    )
    proc = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "parse ok" in proc.stdout


@needs_pwsh
@pytest.mark.parametrize("entry_count", [1, 3])
def test_write_installed_manifest_emits_exact_schema(tmp_path, entry_count):
    """Run the real function against a fake parsed manifest and check the file it writes.

    One entry is included on purpose: PowerShell's JSON serializer unrolls single-element
    collections in some positions, and ``files`` must stay a list either way.
    """
    entries = [
        {
            "source": f"hotfix/scripts/file{i}.py",
            "destination": f"scripts/file{i}.py",
            "sha256": f"ABCDEF{i:058d}",  # upper-case on purpose: the record must be lower-case hex
        }
        for i in range(entry_count)
    ]
    manifest = json.dumps({"schema_version": 1, "files": entries})
    sha = "b07d0a8" + "0" * 33
    out = tmp_path / "installed-manifest.json"
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        _function_source("Write-InstalledManifest"),
        f"$manifest = ConvertFrom-Json -InputObject '{manifest}'",
        f"Write-InstalledManifest -Manifest $manifest -DeploySha '{sha}' -Path '{out}'",
    ])
    script_path = tmp_path / "run.ps1"
    script_path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-File", str(script_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "record must be UTF-8 without a BOM"
    assert not (tmp_path / "installed-manifest.json.tmp").exists(), "staging file must be renamed away"

    record = json.loads(raw.decode("utf-8"))
    assert list(record) == ["deploy_sha", "applied_utc", "files"]
    assert record["deploy_sha"] == sha
    applied = datetime.fromisoformat(record["applied_utc"])
    assert applied.tzinfo is not None and applied.utcoffset().total_seconds() == 0
    assert abs((datetime.now(tz=timezone.utc) - applied).total_seconds()) < 300
    assert isinstance(record["files"], list) and len(record["files"]) == entry_count
    assert record["files"] == [
        {"destination": e["destination"], "sha256": e["sha256"].lower()} for e in entries
    ]
