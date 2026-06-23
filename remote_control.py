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

AUTHORITATIVE: remote entries are tagged source="remote" and fully reconciled each run, so
removing an edge from control.json RE-ENABLES it on the next poll. self_improver's own
blacklist entries (no source tag) are preserved untouched.

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


def main() -> None:
    try:
        control = fetch_control()
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"remote_control: fetch failed ({exc}); blacklist left untouched", file=sys.stderr)
        return
    current = read_json(BLACKLIST_FILE) or {}
    new = reconcile(control, current)
    write_json_atomic(BLACKLIST_FILE, new)
    rc = new["remote_control"]
    print(f"remote_control: applied (remote_entries={rc['remote_count']}, pause_all={rc['pause_all']})")


if __name__ == "__main__":
    main()
