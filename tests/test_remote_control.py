"""Tests for the DEPLOYED remote-control config-pull channel.

Ported from the monorepo's tests/test_remote_control.py so they exercise the copy this repo
installs (remote_control.py at the repo root, manifest destination scripts/remote_control.py),
plus pins for the other pure helpers. The channel must stay AUTHORITATIVE (removing an edge
drops its remote block), SAFE (it only ever writes blacklist entries the traders already
honour) and NON-DESTRUCTIVE (self_improver entries are kept). main() is deliberately not
exercised: it is being wrapped by a concurrent change.
"""

from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pytest

import notify
import remote_control as rc

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_module_under_test_is_the_deployed_copy():
    assert Path(rc.__file__).resolve() == REPO_ROOT / "remote_control.py"


def _magics(bl):
    return {(e["symbol"], int(e["magic"])) for e in bl["entries"]}


# --- ported from the monorepo ---------------------------------------------------------------


def test_disable_edge_adds_blacklist_entry():
    control = {"disabled_edges": [{"symbol": "GOLD", "magic": 88009, "reason": "fails Bonferroni"}]}
    out = rc.reconcile(control, {"entries": []})
    assert ("GOLD", 88009) in _magics(out)
    e = next(x for x in out["entries"] if x["magic"] == 88009)
    assert e["source"] == "remote" and "Bonferroni" in e["reason"]


def test_removing_from_control_re_enables():
    """Authoritative: an edge previously disabled-by-remote is dropped when control omits it."""
    current = {"entries": [{"symbol": "GOLD", "magic": 88009, "reason": "remote: x", "source": "remote"}]}
    out = rc.reconcile({"disabled_edges": []}, current)
    assert ("GOLD", 88009) not in _magics(out)


def test_self_improver_entries_preserved():
    """Non-remote (self_improver) entries must survive a reconcile untouched."""
    current = {"entries": [{"symbol": "USDJPY", "magic": 88002, "reason": "leak auto-pause"}]}
    out = rc.reconcile({"disabled_edges": [{"symbol": "GOLD", "magic": 88009, "reason": "y"}]}, current)
    m = _magics(out)
    assert ("USDJPY", 88002) in m and ("GOLD", 88009) in m
    # the self_improver entry keeps no remote source tag
    kept = next(x for x in out["entries"] if x["magic"] == 88002)
    assert kept.get("source") != "remote"


def test_pause_all_blacklists_every_known_edge():
    out = rc.reconcile({"pause_all": True}, {"entries": []})
    m = _magics(out)
    for magic, symbol in rc.KNOWN_EDGES.items():
        assert (symbol, magic) in m
    assert out["remote_control"]["pause_all"] is True


def test_no_duplicate_when_already_blacklisted_by_self_improver():
    current = {"entries": [{"symbol": "GOLD", "magic": 88009, "reason": "leak"}]}
    out = rc.reconcile({"disabled_edges": [{"symbol": "GOLD", "magic": 88009, "reason": "z"}]}, current)
    golds = [e for e in out["entries"] if e["magic"] == 88009]
    assert len(golds) == 1  # not duplicated


def test_stale_remote_entries_cleared_each_run():
    current = {"entries": [
        {"symbol": "AUDJPY", "magic": 88007, "reason": "remote: old", "source": "remote"},
        {"symbol": "GOLD", "magic": 88009, "reason": "remote: keep", "source": "remote"},
    ]}
    out = rc.reconcile({"disabled_edges": [{"symbol": "GOLD", "magic": 88009, "reason": "keep"}]}, current)
    m = _magics(out)
    assert ("GOLD", 88009) in m and ("AUDJPY", 88007) not in m


def test_ignores_malformed_entries():
    control = {"disabled_edges": [{"symbol": "", "magic": 0}, {"reason": "no symbol/magic"}]}
    out = rc.reconcile(control, {"entries": []})
    assert out["entries"] == []


# --- additional pins on the deployed copy -----------------------------------------------------


def test_known_edges_cover_every_structural_magic():
    assert rc.KNOWN_EDGES == {
        88001: "GOLD", 88002: "USDJPY", 88003: "UK100Cash", 88004: "GOLD", 88005: "USDJPY",
        88006: "GOLD", 88007: "AUDJPY", 88008: "GBPJPY", 88009: "GOLD",
    }


def test_reconcile_metadata_counts_remote_entries_only():
    current = {"entries": [{"symbol": "USDJPY", "magic": 88002, "reason": "leak"}], "note": "kept"}
    control = {"disabled_edges": [
        {"symbol": "GOLD", "magic": "88009", "reason": "string magic"},
        {"symbol": "AUDJPY", "magic": 88007},
    ]}
    out = rc.reconcile(control, current)
    assert out["note"] == "kept"                       # unrelated top-level keys survive
    assert out["remote_control"] == {"applied": True, "remote_count": 2, "pause_all": False}
    remote = [e for e in out["entries"] if e.get("source") == "remote"]
    assert remote == [
        {"symbol": "GOLD", "magic": 88009, "reason": "remote: string magic", "source": "remote"},
        {"symbol": "AUDJPY", "magic": 88007, "reason": "remote: disabled", "source": "remote"},
    ]
    assert out["entries"][0]["magic"] == 88002        # kept entries come first, verbatim


def test_reconcile_with_empty_or_non_dict_current():
    for current in ({}, None, [], "junk"):
        out = rc.reconcile({"pause_all": True}, current)
        assert len(out["entries"]) == len(rc.KNOWN_EDGES)
        assert out["remote_control"]["pause_all"] is True


def test_pause_all_does_not_duplicate_explicit_disables():
    control = {"pause_all": True, "disabled_edges": [{"symbol": "GOLD", "magic": 88009, "reason": "x"}]}
    out = rc.reconcile(control, {"entries": []})
    assert len([e for e in out["entries"] if e["magic"] == 88009]) == 1
    assert out["remote_control"]["remote_count"] == len(rc.KNOWN_EDGES)


def test_remote_set_only_counts_remote_tagged_entries():
    bl = {"entries": [
        {"symbol": "GOLD", "magic": 88009, "source": "remote"},
        {"symbol": "USDJPY", "magic": "88002", "source": "remote"},
        {"symbol": "AUDJPY", "magic": 88007},
        {"symbol": "GBPJPY", "source": "remote"},        # missing magic -> 0
    ]}
    assert rc._remote_set(bl) == {("GOLD", 88009), ("USDJPY", 88002), ("GBPJPY", 0)}
    assert rc._remote_set({}) == set()


def test_notify_change_reports_pause_added_and_removed(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_ntfy", lambda msg, title=None, tags=None: sent.append((msg, title, tags)) or True)
    rc._notify_change({("GOLD", 88009), ("AUDJPY", 88007)}, {("USDJPY", 88002)}, pause_all=True)
    assert sent == [(
        "Remote action applied: ALL trading PAUSED; disabled AUDJPY (88007); disabled GOLD (88009); "
        "re-enabled USDJPY (88002)",
        "Bot config changed",
        "gear",
    )]


def test_notify_change_is_silent_without_a_change(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_ntfy", lambda *a, **k: sent.append((a, k)) or True)
    rc._notify_change(set(), set(), pause_all=False)
    assert sent == []


def test_notify_change_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ntfy down")
    monkeypatch.setattr(notify, "send_ntfy", boom)
    rc._notify_change({("GOLD", 88009)}, set(), pause_all=False)   # must not propagate


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_fetch_control_requests_uncached_and_parses_json(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["cache"] = req.get_header("Cache-control")
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps({"pause_all": False, "disabled_edges": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert rc.fetch_control() == {"pause_all": False, "disabled_edges": []}
    assert seen == {"url": rc.CONTROL_URL, "cache": "no-cache", "timeout": 8}
    assert rc.CONTROL_URL.endswith("/swanhtet01/mt5-vps-deploy/main/control.json")


def test_fetch_control_propagates_malformed_json(monkeypatch):
    # main() catches this and leaves the blacklist untouched; fetch_control itself must not
    # swallow it into an empty control (which would re-enable every remote-disabled edge).
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(b"{not json"))
    with pytest.raises(json.JSONDecodeError):
        rc.fetch_control()


def test_committed_control_json_parses_and_every_listed_edge_lands_in_the_blacklist():
    # The file the VPS polls from main. Operator choices (pause_all, which edges) are not
    # pinned here -- only that the file is well-formed and reconcile honours all of it.
    control = json.loads((REPO_ROOT / "control.json").read_text(encoding="utf-8-sig"))
    assert isinstance(control.get("disabled_edges", []), list)
    out = rc.reconcile(control, {"entries": []})
    remote = {(e["symbol"], e["magic"]) for e in out["entries"] if e.get("source") == "remote"}
    for edge in control.get("disabled_edges", []):
        assert (edge["symbol"], int(edge["magic"])) in remote
    assert out["remote_control"]["pause_all"] is bool(control.get("pause_all"))
