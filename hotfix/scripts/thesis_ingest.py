"""Daily thesis ingest — runs Claude Opus to produce a sizing multiplier.

Schedule: daily at 02:00 UTC (after build_dashboard_metrics at 01:00).
Output: data_cache/claude_thesis.json (consumed by apply_approved_thesis.py at 02:30).

Pipeline:
  01:00  build_dashboard_metrics  →  dashboard_metrics.json
  02:00  thesis_ingest (this)     →  claude_thesis.json
  02:30  apply_approved_thesis    →  context_score.json["sizing_multiplier"]

The bot then reads context_score.json at every trade entry; high-conviction
days get up to 2.0x lots, risk-off days scale down to 0.5x.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))

from mt5_agent.claude_thesis_generator import (  # noqa: E402
    generate_thesis,
    write_thesis_json,
)
from paths import DATA_CACHE, read_json, write_json_atomic  # noqa: E402

THESIS_FILE = DATA_CACHE / "claude_thesis.json"
LOG_DIR = Path(r"C:\mt5-paper\analytics")


def _load_news_sentiment() -> dict:
    # news_state.json is keyed by symbol; extract a global risk-off score
    raw = read_json(DATA_CACHE / "news_state.json") or {}
    if not raw:
        return {}

    # Collect sentiments across symbols
    sentiments = []
    keywords: set[str] = set()
    headlines = []
    for sym, rec in raw.items():
        if isinstance(rec, dict):
            s = rec.get("sentiment")
            if s is not None:
                try:
                    sentiments.append(float(s))
                except (TypeError, ValueError):
                    pass
            h = rec.get("headline") or ""
            if h:
                headlines.append(f"[{sym}] {h}")
            kws = rec.get("keywords") or []
            if isinstance(kws, list):
                keywords.update(kws[:3])

    avg_sent = sum(sentiments) / len(sentiments) if sentiments else 0.0
    # Convert sentiment (-1..+1) to risk-off score (0..100); negative sentiment = high risk-off
    risk_off = max(0, min(100, int(50 - avg_sent * 50)))
    return {
        "risk_off_score": risk_off,
        "keywords_found": list(keywords)[:8],
        "headlines": headlines[:5],
        "avg_sentiment": round(avg_sent, 3),
    }


def _load_macro_calendar() -> dict:
    raw = read_json(DATA_CACHE / "economic_calendar.json") or {}
    upcoming_24h = raw.get("upcoming_24h", [])
    upcoming_48h = raw.get("upcoming_48h", [])
    impact = "low"
    for ev in upcoming_24h:
        if isinstance(ev, dict):
            ev_impact = str(ev.get("impact", "")).lower()
            if "high" in ev_impact:
                impact = "high"
                break
            elif "medium" in ev_impact and impact == "low":
                impact = "medium"
    return {
        "upcoming_24h": upcoming_24h[:5],
        "upcoming_48h": upcoming_48h[:5],
        "impact": impact,
    }


def append_log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry["ts"] = datetime.now(tz=timezone.utc).isoformat()
    with (LOG_DIR / "thesis_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def main() -> int:
    dashboard = read_json(DATA_CACHE / "dashboard_metrics.json") or {}
    context = read_json(DATA_CACHE / "context_score.json") or {}
    news = _load_news_sentiment()
    macro = _load_macro_calendar()

    if not dashboard or not dashboard.get("account", {}).get("equity"):
        print("WARNING: dashboard_metrics.json missing or empty — thesis will be low-confidence",
              file=sys.stderr)

    try:
        snapshot = generate_thesis(dashboard, context, news, macro)
        # Mark as pre-approved for autonomous apply (apply_approved_thesis.py reads this)
        import dataclasses
        snap_dict = dataclasses.asdict(snapshot)
        snap_dict["approval_needed"] = False
        snap_dict["generated_by"] = "thesis_ingest_autonomous"

        THESIS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = THESIS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap_dict, indent=2), encoding="utf-8")
        tmp.replace(THESIS_FILE)

        append_log({
            "event": "thesis_generated",
            "multiplier": snap_dict.get("suggested_multiplier"),
            "confidence": snap_dict.get("confidence"),
            "factors": snap_dict.get("reasoning_factors", [])[:3],
        })
        print(
            f"Thesis OK: {snap_dict['suggested_multiplier']:.2f}x "
            f"(confidence={snap_dict['confidence']:.0%}) "
            f"— {', '.join(snap_dict.get('reasoning_factors', [])[:2])}"
        )

        # Push phone notification
        try:
            from notify import send_ntfy
            mult = snap_dict["suggested_multiplier"]
            conf = snap_dict["confidence"]
            tag = "chart_increasing" if mult >= 1.5 else ("chart_with_downwards_trend" if mult <= 0.7 else "bar_chart")
            send_ntfy(
                f"Thesis: {mult:.2f}x sizing (conf={conf:.0%})\n"
                f"{', '.join(snap_dict.get('reasoning_factors', [])[:2])}",
                title="Daily Thesis", tags=tag,
            )
        except Exception:
            pass

        return 0

    except Exception as exc:
        append_log({"event": "thesis_failed", "error": str(exc)})
        print(f"thesis_ingest FAILED: {exc}", file=sys.stderr)
        # Write a safe fallback so apply_approved_thesis gets 1.0x rather than erroring
        fallback = {
            "as_of": datetime.now(tz=timezone.utc).isoformat(),
            "thesis": "Thesis generation failed; trading at normal size.",
            "reasoning_factors": ["api_failure"],
            "suggested_multiplier": 1.0,
            "confidence": 0.5,
            "approval_needed": False,
            "generated_by": "thesis_ingest_fallback",
        }
        write_json_atomic(THESIS_FILE, fallback)
        return 1


if __name__ == "__main__":
    sys.exit(main())
