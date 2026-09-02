"""Bounded VPS housekeeping for logs and Vibe research artifacts."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paths import DATA_CACHE, PAPER_ROOT, write_json_atomic
try:
    from task_receipt import run_receipt
except ImportError:  # helper not delivered: bookkeeping must never stop the task
    print("WARN: task_receipt.py missing; running without a run receipt", file=sys.stderr)
    from contextlib import nullcontext as _nullcontext

    def run_receipt(task, directory=None):
        return _nullcontext(None)


MAX_LOG_BYTES = 25 * 1024 * 1024
ROTATED_LOGS_TO_KEEP = 4
EXPORT_RETENTION_DAYS = 30
REPORT_RETENTION_DAYS = 45
PROMPT_RETENTION_DAYS = 14
TIMESTAMPED_BUNDLE = re.compile(r"^\d{8}T\d{6}Z$")


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def rotate_file(path: Path, *, max_bytes: int, keep: int) -> dict | None:
    """Rotate one oversized regular file without crossing its parent directory."""
    if max_bytes < 1 or keep < 1 or not path.is_file() or path.is_symlink():
        return None
    size = path.stat().st_size
    if size <= max_bytes:
        return None
    root = path.parent.resolve()
    if not _within(path, root):
        return None
    oldest = path.with_name(f"{path.name}.{keep}")
    if oldest.exists() and oldest.is_file() and not oldest.is_symlink():
        oldest.unlink()
    for index in range(keep - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        target = path.with_name(f"{path.name}.{index + 1}")
        if source.exists() and source.is_file() and not source.is_symlink():
            os.replace(source, target)
    os.replace(path, path.with_name(f"{path.name}.1"))
    path.touch()
    return {"path": str(path), "rotated_bytes": size}


def rotate_logs(roots: list[Path], *, max_bytes: int = MAX_LOG_BYTES) -> list[dict]:
    rotated: list[dict] = []
    for root in roots:
        if not root.exists() or root.is_symlink():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".log", ".jsonl"}:
                continue
            try:
                result = rotate_file(
                    path, max_bytes=max_bytes, keep=ROTATED_LOGS_TO_KEEP
                )
                if result:
                    rotated.append(result)
            except OSError as exc:
                rotated.append({"path": str(path), "error": str(exc)})
    return rotated


def _latest_bundle(exports_root: Path) -> Path | None:
    pointer = exports_root / "latest.json"
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        bundle = Path(str(payload.get("bundle") or ""))
        if bundle and _within(bundle, exports_root):
            return bundle.resolve()
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def _latest_report(reports_root: Path) -> Path | None:
    pointer = reports_root / "latest.json"
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8-sig"))
        report_dir = Path(str(payload.get("report_dir") or ""))
        if report_dir and _within(report_dir, reports_root):
            return report_dir.resolve()
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def prune_vibe_artifacts(
    sidecar_root: Path,
    *,
    now: datetime,
    export_days: int = EXPORT_RETENTION_DAYS,
    report_days: int = REPORT_RETENTION_DAYS,
    prompt_days: int = PROMPT_RETENTION_DAYS,
) -> list[dict]:
    removed: list[dict] = []
    if not sidecar_root.exists() or sidecar_root.is_symlink():
        return removed
    exports_root = sidecar_root / "exports"
    latest = _latest_bundle(exports_root)
    export_cutoff = now - timedelta(days=max(export_days, 1))
    if exports_root.exists() and not exports_root.is_symlink():
        for candidate in exports_root.iterdir():
            if (
                not candidate.is_dir()
                or candidate.is_symlink()
                or not TIMESTAMPED_BUNDLE.fullmatch(candidate.name)
                or candidate.resolve() == latest
            ):
                continue
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
            if modified < export_cutoff and _within(candidate, exports_root):
                size = sum(
                    item.stat().st_size
                    for item in candidate.rglob("*")
                    if item.is_file() and not item.is_symlink()
                )
                shutil.rmtree(candidate)
                removed.append({"path": str(candidate), "removed_bytes": size})

    reports_root = sidecar_root / "reports"
    latest_report = _latest_report(reports_root)
    report_cutoff = now - timedelta(days=max(report_days, 1))
    if reports_root.exists() and not reports_root.is_symlink():
        for candidate in reports_root.iterdir():
            if (
                not candidate.is_dir()
                or candidate.is_symlink()
                or not TIMESTAMPED_BUNDLE.fullmatch(candidate.name)
                or candidate.resolve() == latest_report
            ):
                continue
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
            if modified < report_cutoff and _within(candidate, reports_root):
                size = sum(
                    item.stat().st_size
                    for item in candidate.rglob("*")
                    if item.is_file() and not item.is_symlink()
                )
                shutil.rmtree(candidate)
                removed.append({"path": str(candidate), "removed_bytes": size})

    prompt_cutoff = now - timedelta(days=max(prompt_days, 1))
    for prompt in sidecar_root.glob("prompt-*.txt"):
        if not prompt.is_file() or prompt.is_symlink() or not _within(prompt, sidecar_root):
            continue
        modified = datetime.fromtimestamp(prompt.stat().st_mtime, tz=timezone.utc)
        if modified < prompt_cutoff:
            size = prompt.stat().st_size
            prompt.unlink()
            removed.append({"path": str(prompt), "removed_bytes": size})
    return removed


def main() -> None:
    now = datetime.now(tz=timezone.utc)
    sidecar_root = Path(os.environ.get("MT5_VIBE_ROOT", r"C:\mt5-vibe-research"))
    report = {
        "schema": "mt5.vps_maintenance.v1",
        "timestamp": now.isoformat(),
        "rotated": rotate_logs([PAPER_ROOT, sidecar_root / "logs"]),
        "removed": prune_vibe_artifacts(sidecar_root, now=now),
    }
    write_json_atomic(DATA_CACHE / "vps_maintenance.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    with run_receipt("MT5-Maintenance"):
        main()
