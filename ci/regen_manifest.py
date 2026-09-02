#!/usr/bin/env python3
"""Recompute every sha256 in hotfix-manifest.json from the files it points at.

CLAUDE.md §2 tells anyone who edits a manifest-delivered file to regenerate its hash,
because update.ps1 throws on the first mismatch and aborts the whole installer. Until
now that meant hand-editing a 64-character hex string; this does it for every entry.

    python3 ci/regen_manifest.py            # rewrite hashes in place
    python3 ci/regen_manifest.py --check    # exit 1 if any hash would change (CI-safe)

Pure stdlib. Preserves the file's UTF-8 BOM (PowerShell wrote it that way) and the
2-space JSON layout verify_deploy.py already accepts. Refuses to run when a source file
is missing — a missing source is a broken deploy, not something to hash around.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "hotfix-manifest.json"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regenerate(manifest_path: Path = MANIFEST, *, check: bool = False) -> int:
    raw = manifest_path.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    manifest = json.loads(raw.decode("utf-8-sig"))

    changed: list[str] = []
    missing: list[str] = []
    for entry in manifest["files"]:
        source = manifest_path.parent / entry["source"]
        if not source.is_file():
            missing.append(entry["source"])
            continue
        actual = sha256_of(source)
        if entry.get("sha256") != actual:
            changed.append(entry["source"])
            entry["sha256"] = actual

    if missing:
        print("manifest sources missing on disk (fix the manifest, do not hash around it):", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        return 2

    if not changed:
        print(f"ok: all {len(manifest['files'])} manifest hashes already match their sources")
        return 0

    for name in changed:
        print(f"{'would update' if check else 'updated'}: {name}")
    if check:
        return 1

    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    manifest_path.write_bytes((b"\xef\xbb\xbf" if had_bom else b"") + text.encode("utf-8"))
    print(f"rewrote {len(changed)} hash(es) in {manifest_path.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report stale hashes and exit 1 instead of rewriting")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args(argv)
    return regenerate(args.manifest, check=args.check)


if __name__ == "__main__":
    sys.exit(main())
