"""Shared path resolver + atomic-write helper — single source of truth for filesystem
locations and crash-safe JSON persistence.

Resolves paths from environment variables with sensible defaults that work in
BOTH layouts:
  - Dev machine (project in OneDrive):
      MT5_REPO=C:\\Users\\swann\\OneDrive - BDA\\trading-agent
      MT5_PAPER=C:\\mt5-paper
      MT5_VENV=C:\\Users\\swann\\mt5-venv
  - VPS (project under C:\\trading-agent):
      MT5_REPO=C:\\trading-agent
      MT5_PAPER=C:\\mt5-paper
      MT5_VENV=C:\\mt5-venv

All scripts import from here instead of hardcoding paths, so the same code
runs on either machine without changes.

Default behavior: walks up from the calling script until it finds a directory
containing pyproject.toml (the repo root). Falls back to the OneDrive path
only as a last resort to preserve the dev-machine workflow.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _detect_repo_root() -> Path:
    """Find the trading-agent repo root from the location of this paths.py file."""
    env_repo = os.environ.get("MT5_REPO", "").strip()
    if env_repo and Path(env_repo).exists():
        return Path(env_repo)
    # paths.py lives in <repo>/scripts/, so parent.parent is the repo root.
    here = Path(__file__).resolve().parent.parent
    if (here / "pyproject.toml").exists():
        return here
    # Last-resort fallback: the original dev path
    fallback = Path(r"C:\Users\swann\OneDrive - BDA\trading-agent")
    if fallback.exists():
        return fallback
    return here


REPO_ROOT: Path = _detect_repo_root()
PAPER_ROOT: Path = Path(os.environ.get("MT5_PAPER", r"C:\mt5-paper"))
DATA_CACHE: Path = REPO_ROOT / "data_cache"
LOGS_ROOT: Path = REPO_ROOT / "logs"
STATE_DIR: Path = Path(os.environ.get("MT5_AGENT_STATE_DIR") or (REPO_ROOT / "state"))


# Common file paths (resolved once at import)
NEWS_STATE_FILE: Path = DATA_CACHE / "news_state.json"
CALENDAR_FILE: Path = DATA_CACHE / "economic_calendar.json"
BLACKLIST_FILE: Path = DATA_CACHE / "blacklist.json"
SIZING_FILE: Path = DATA_CACHE / "position_sizing.json"
PATTERNS_FILE: Path = DATA_CACHE / "learned_patterns.json"
MOMENTUM_FILE: Path = DATA_CACHE / "stock_momentum.json"
WIDE_EDGE_FILE: Path = DATA_CACHE / "wide_edge_hunt.json"
VPS_HEALTH_FILE: Path = DATA_CACHE / "vps_health.json"


def read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON file. Returns ``default`` (or {}) on any error including
    file-missing, parse-fail, or empty. Used by ALL consumers so a half-written
    or corrupt shared file never crashes a live trader."""
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        # Windows PowerShell 5 writes a UTF-8 BOM for ``-Encoding utf8``.
        # ``utf-8-sig`` accepts both BOM-prefixed and ordinary UTF-8 JSON.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, ValueError, OSError):
        return default


def write_json_atomic(path: Path, payload: Any) -> None:
    """Atomic JSON write: temp file in same directory, then os.replace.
    Readers never see a half-written file — they get either the previous
    complete version or the new complete version. PID in the temp name
    prevents multiple writers from clobbering each other's temps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def ensure_dirs() -> None:
    """Create the standard runtime directories if missing."""
    for d in (DATA_CACHE, LOGS_ROOT, PAPER_ROOT,
              PAPER_ROOT / "gold-drift", PAPER_ROOT / "multi-drift",
              PAPER_ROOT / "news", PAPER_ROOT / "analytics",
              PAPER_ROOT / "swing"):
        d.mkdir(parents=True, exist_ok=True)
