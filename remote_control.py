"""Remote control channel for the live VPS bot -- CONFIG-PULL, never code execution.

WHY
---
The VPS has no remote shell we can reach (RDP is broken by the user's VPN, the VNC console
can't be driven programmatically). So to disable a bad edge or pause trading we previously had
to hand-drive the VNC. This closes that gap SAFELY: it fetches a small control file from a URL
we can edit (GitHub raw) and applies it by writing the SAME blacklist.json the live traders
already honor (`_is_blacklisted(symbol, magic)` -> skip). It NEVER executes remote code; the
worst a compromised control file can do is refuse trades (fail-safe direction).

control.json schema:
  {
    "pause_all": false,
    "disabled_edges": [
      {"symbol": "GOLD", "magic": 88009, "reason": "fails Bonferroni (t=3.31)"}
    ]
  }

Remote entries are tagged source="remote" and fully reconciled each run. Removing an edge
only removes that blacklist block; it does not enable a disabled Windows task and it does
not add the strategy to structural_live_allowlist.json. self_improver's own blacklist
entries (no source tag) are preserved untouched.

Fail-safe: any fetch/parse error leaves the existing blacklist exactly as-is.
Stdlib only. Run every few minutes by a scheduled task (MT5-RemoteControl).
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import BLACKLIST_FILE, read_json, write_json_atomic  # noqa: E402
try:
    from task_receipt import run_receipt  # noqa: E402  (sibling in scripts\ on the VPS)
except ImportError:
    try:  # repo layout: this file sits at the root, task_receipt.py under hotfix/scripts
        sys.path.insert(0, str(Path(__file__).resolve().parent / "hotfix" / "scripts"))
        from task_receipt import run_receipt  # noqa: E402
    except ImportError:  # helper not delivered: bookkeeping must never block the pause lever
        print("remote_control: task_receipt.py missing; running without a run receipt",
              file=sys.stderr)
        from contextlib import nullcontext as _nullcontext  # noqa: E402

        def run_receipt(task, directory=None):
            return _nullcontext(None)

CONTROL_URL = "https://raw.githubusercontent.com/swanhtet01/mt5-vps-deploy/main/control.json"

# magic -> symbol, for pause_all. Stable; the edge registry will own this map later.
KNOWN_EDGES = {
    88001: "GOLD", 88002: "USDJPY", 88003: "UK100Cash", 88004: "GOLD", 88005: "USDJPY",
    88006: "GOLD", 88007: "AUDJPY", 88008: "GBPJPY", 88009: "GOLD",
}


def fetch_control(url: str = CONTROL_URL) -> dict:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def reconcile(control: dict, current: dict) -> dict:
    """Pure: return the new blacklist dict given the control file and current blacklist.
    Drops stale source=remote entries, keeps everything else, re-adds the current remote set."""
    entries = list(current.get("entries", []) if isinstance(current, dict) else [])
    # keep non-remote (e.g. self_improver) entries verbatim
    kept = [e for e in entries if e.get("source") != "remote"]
    have = {(e.get("symbol"), int(e.get("magic", 0))) for e in kept}
    remote: list[dict] = []

    def add(symbol: str, magic: int, reason: str):
        if not symbol or not magic:
            return
        if (symbol, magic) in have:
            return
        remote.append({"symbol": symbol, "magic": int(magic),
                       "reason": f"remote: {reason}", "source": "remote"})
        have.add((symbol, magic))

    if control.get("pause_all"):
        for magic, symbol in KNOWN_EDGES.items():
            add(symbol, magic, "pause_all")
    for e in control.get("disabled_edges", []):
        add(str(e.get("symbol") or ""), int(e.get("magic") or 0),
            str(e.get("reason", "disabled")))

    new = dict(current) if isinstance(current, dict) else {}
    new["entries"] = kept + remote
    new["remote_control"] = {"applied": True, "remote_count": len(remote),
                             "pause_all": bool(control.get("pause_all"))}
    return new


def _remote_set(bl: dict) -> set:
    return {(e.get("symbol"), int(e.get("magic", 0))) for e in bl.get("entries", [])
            if e.get("source") == "remote"}


def _notify_change(added: set, removed: set, pause_all: bool) -> None:
    """Best-effort phone alert when a remote action takes effect (only on real change)."""
    try:
        import notify as notifier  # sibling
        parts = []
        if pause_all:
            parts.append("ALL trading PAUSED")
        for sym, magic in sorted(added):
            parts.append(f"disabled {sym} ({magic})")
        for sym, magic in sorted(removed):
            parts.append(f"re-enabled {sym} ({magic})")
        if parts:
            notifier.send_ntfy("Remote action applied: " + "; ".join(parts),
                               title="Bot config changed", tags="gear")
    except Exception:
        pass


def main() -> None:
    try:
        control = fetch_control()
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
        # Fail-safe for the blacklist (untouched) but NOT silent: exit 1 so Task Scheduler
        # and the run receipt both show that remote control has not been applied.
        print(f"remote_control: fetch failed ({exc}); blacklist left untouched", file=sys.stderr)
        sys.exit(1)
    current = read_json(BLACKLIST_FILE) or {}
    old_remote = _remote_set(current)
    new = reconcile(control, current)
    new_remote = _remote_set(new)
    write_json_atomic(BLACKLIST_FILE, new)

    added, removed = new_remote - old_remote, old_remote - new_remote
    if added or removed:
        _notify_change(added, removed, bool(control.get("pause_all")))
    rc = new["remote_control"]
    print(f"remote_control: applied (remote_entries={rc['remote_count']}, pause_all={rc['pause_all']}, "
          f"changed={bool(added or removed)})")


if __name__ == "__main__":
    with run_receipt("MT5-RemoteControl"):
        main()
