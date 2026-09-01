#!/usr/bin/env python3
"""Static checks on the deploy channel. Run locally or in CI:  python3 ci/verify_deploy.py

This repo installs and updates a live trading VPS, and until now nothing verified it. The
failures it guards against are all SILENT on the box:

  * A stale sha256 makes update.ps1 `throw` mid-loop, so the whole installer aborts and NO
    update lands -- including the task registration further down the file. The VPS keeps
    running old code and nothing says so.
  * A task pointed at a script the manifest does not deliver fails every single run,
    forever, in a log nobody reads.
  * tasks.ps1 calling a task CRITICAL that no installer creates makes the health check
    permanently red, which trains the operator to ignore it. That is how the kill switch
    and the VPS health watch went missing without anyone noticing.

Every check here is static: no PowerShell, no network, no MT5, no VPS access needed.
"""

from __future__ import annotations

import ast
import collections
import hashlib
import json
import ntpath
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "hotfix-manifest.json"

failures: list[str] = []
notes: list[str] = []


def fail(check: str, detail: str) -> None:
    failures.append(f"[{check}] {detail}")


def load_manifest() -> list[dict]:
    # PowerShell's Set-Content writes a UTF-8 BOM; json.load chokes on it, utf-8-sig does not.
    data = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != 1:
        fail("manifest", f"schema_version must be 1, got {data.get('schema_version')!r} "
                         "(update.ps1 throws on anything else)")
    files = data.get("files") or []
    if not files:
        fail("manifest", "no files listed; update.ps1 throws on an empty manifest")
    return files


def check_entries(files: list[dict]) -> None:
    for entry in files:
        source = str(entry.get("source", ""))
        dest = str(entry.get("destination", ""))

        # Mirror update.ps1's own guards so a rejected manifest is caught here, not on the VPS.
        if not source or ".." in source or ntpath.isabs(source) or source.startswith("/"):
            fail("unsafe-source", f"{source!r} -- update.ps1 throws 'Unsafe manifest source'")
        if ".." in dest or ntpath.isabs(dest) or dest.startswith("/"):
            fail("unsafe-destination", f"{dest!r} -- escapes the repo root on the VPS")

        path = ROOT / source
        if not path.is_file():
            fail("missing-source", f"{source} is in the manifest but not in this repo; "
                                   "the download 404s and the installer aborts")
            continue

        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = str(entry.get("sha256", "")).lower()
        if actual != expected:
            fail("stale-hash",
                 f"{source}\n      manifest: {expected}\n      actual:   {actual}\n"
                 f"      -> update.ps1 throws 'SHA256 mismatch' and NO update lands. "
                 f"Regenerate the manifest after editing this file.")

    dupes = [d for d, n in collections.Counter(
        str(e.get("destination", "")) for e in files).items() if n > 1]
    for dest in dupes:
        fail("duplicate-destination",
             f"{dest} is written by more than one source; which one wins is arbitrary")


def check_python_sources(files: list[dict]) -> None:
    """A syntax error ships to the VPS and every task running that file dies on import."""
    for entry in files:
        source = str(entry.get("source", ""))
        if not source.endswith(".py"):
            continue
        path = ROOT / source
        if not path.is_file():
            continue  # already reported
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            fail("syntax-error", f"{source}:{exc.lineno}: {exc.msg}")


def powershell_files() -> list[pathlib.Path]:
    return sorted(p for p in ROOT.glob("*.ps1"))


def check_task_scripts(files: list[dict]) -> None:
    """Report which task-run scripts come from the manifest and which from the release bundle.

    Two things put code on the VPS. The release bundle (mt5-bundle.zip) is robocopied over
    C:\\trading-agent\\scripts, then the manifest re-applies its hash-verified files on top.
    Only the manifest is pinned to $deployRef -- the bundle is whatever the LATEST RELEASE
    holds, so a script that is not in the manifest is frozen at the last release and a
    commit-pinned deploy will not refresh it. That is worth seeing, but it is not a failure:
    these scripts do exist on the box.
    """
    delivered = {str(e.get("destination", "")).replace("\\", "/") for e in files}
    referenced: dict[str, set[str]] = collections.defaultdict(set)
    pattern = re.compile(r"\$repo\\(scripts\\[A-Za-z0-9_]+\.py)")
    for ps1 in powershell_files():
        for match in pattern.findall(ps1.read_text(encoding="utf-8", errors="replace")):
            referenced[match.replace("\\", "/")].add(ps1.name)

    from_bundle = sorted(rel for rel in referenced if rel not in delivered)
    notes.append(f"{len(referenced) - len(from_bundle)}/{len(referenced)} task-referenced "
                 f"scripts are manifest-pinned")
    if from_bundle:
        notes.append("release-bundle only (not commit-pinned, only as fresh as the last "
                     "release): " + ", ".join(p.split("/")[-1] for p in from_bundle))


def parse_critical_tasks() -> set[str]:
    """Pull the $critical list out of tasks.ps1, including conditional += additions."""
    text = (ROOT / "tasks.ps1").read_text(encoding="utf-8")
    block = re.search(r"\$critical\s*=\s*@\((.*?)\)", text, re.S)
    if not block:
        fail("tasks-parse", "could not find the $critical = @(...) list in tasks.ps1")
        return set()
    names = set(re.findall(r"'([^']+)'", block.group(1)))
    names |= set(re.findall(r"\$critical\s*\+=\s*'([^']+)'", text))
    return names


def check_critical_tasks_are_installed() -> None:
    """The bug this repo actually shipped: tasks.ps1 called tasks critical that nothing created.

    `schtasks /change /enable` cannot create a task, so the health check's own remedy could
    never fix it -- the kill switch and VPS health watch were simply never installed.
    """
    created: dict[str, str] = {}
    creators = (
        re.compile(r"schtasks\s+/create\s+/tn\s+'([^']+)'"),
        re.compile(r"New-MT5TaskIfMissing\s+-TaskName\s+'([^']+)'"),
    )
    for ps1 in powershell_files():
        text = ps1.read_text(encoding="utf-8", errors="replace")
        for creator in creators:
            for name in creator.findall(text):
                created.setdefault(name, ps1.name)

    for name in sorted(parse_critical_tasks()):
        if name not in created:
            fail("uninstallable-critical-task",
                 f"tasks.ps1 lists {name} as CRITICAL but no installer in this repo creates "
                 f"it, so the health check is permanently red and its remedy cannot help")
    if created:
        notes.append(f"{len(created)} tasks created across "
                     f"{len(set(created.values()))} installer scripts")


def main() -> int:
    if not MANIFEST.is_file():
        print(f"FAIL: {MANIFEST} not found", file=sys.stderr)
        return 1

    files = load_manifest()
    check_entries(files)
    check_python_sources(files)
    check_task_scripts(files)
    check_critical_tasks_are_installed()

    print(f"verify_deploy: {len(files)} manifest entries, {len(powershell_files())} PowerShell scripts")
    for note in notes:
        print(f"  ok: {note}")

    if failures:
        print(f"\n{len(failures)} problem(s):\n", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print("  ok: all manifest hashes match their sources")
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
