#!/usr/bin/env python3
"""
Auto-apply thesis to position sizing (AUTONOMOUS MODE).

Runs daily after thesis_ingest.py. Reads the LLM-generated thesis and applies
its suggested_multiplier to context_score.json (the sizing signal picked up by
all live scripts at entry time).

Multiplier range: 0.50x (defensive) to 2.0x (confident/upsized).
  - 1.0x = trade at the scaled base lot (default)
  - 2.0x = double position on high-conviction days
  - 0.5x = half position on risk-off or uncertain days

If thesis generation failed, falls back safely to 1.0x (full base size).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA_CACHE, read_json, write_json_atomic  # noqa: E402


def main() -> int:
    try:
        thesis = read_json(DATA_CACHE / "claude_thesis.json") or {}
        if not thesis:
            print("No thesis found; defaulting to 1.0x sizing", file=sys.stderr)
            _write_default(1.0)
            return 0

        suggested_mult = thesis.get("suggested_multiplier", 1.0)
        confidence = float(thesis.get("confidence", 0.5))

        # Clamp: 0.5x floor prevents extreme shrinkage; 2.0x cap caps upside risk.
        # High-confidence thesis (>0.70) is allowed the full upside range.
        # Low-confidence thesis (<0.45) is capped at 1.0x (don't upsize on uncertainty).
        if confidence < 0.45:
            upper = 1.0
        elif confidence < 0.65:
            upper = 1.5
        else:
            upper = 2.0
        mult = max(0.5, min(upper, float(suggested_mult)))

        context = read_json(DATA_CACHE / "context_score.json") or {}
        context["sizing_multiplier"] = mult
        context["sizing_source"] = "lm_thesis_autonomous"
        context["thesis_applied_at"] = datetime.now(tz=timezone.utc).isoformat()
        context["thesis_reasoning"] = thesis.get("reasoning_factors", [])
        context["thesis_confidence"] = confidence
        context["thesis_upper_cap"] = upper
        write_json_atomic(DATA_CACHE / "context_score.json", context)

        print(f"Thesis applied: {mult:.2f}x sizing (confidence={confidence:.0%}, cap={upper:.1f}x)")
        print(f"  Reasoning: {', '.join(thesis.get('reasoning_factors', [])[:3])}")
        return 0

    except Exception as exc:
        print(f"Error applying thesis: {exc}", file=sys.stderr)
        _write_default(1.0)
        return 0


def _write_default(mult: float) -> None:
    try:
        context = read_json(DATA_CACHE / "context_score.json") or {}
        context["sizing_multiplier"] = mult
        context["sizing_source"] = "fallback_default"
        context["thesis_applied_at"] = datetime.now(tz=timezone.utc).isoformat()
        write_json_atomic(DATA_CACHE / "context_score.json", context)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
