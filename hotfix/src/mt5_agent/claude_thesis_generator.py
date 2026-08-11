"""
Claude Thesis Generator: Daily trading thesis from dashboard metrics + context + news + macro.

This module uses the Anthropic Python SDK to generate a 1-paragraph trading thesis
that informs position sizing for the trading day. The thesis is human-approved before
being applied to actual trading.

Functions:
  - generate_thesis: Call Claude API to generate thesis from dashboard + context + news + macro
  - build_thesis_snapshot: Wrap the thesis in a JSON-serializable snapshot
  - load_thesis_json: Load cached thesis from disk
  - write_thesis_json: Write thesis snapshot to disk with atomic writes

Returns a snapshot with:
  - thesis: 1-paragraph trading setup explanation
  - reasoning_factors: list of key drivers
  - suggested_multiplier: 0.25-1.0 position sizing recommendation
  - confidence: 0.0-1.0 confidence in the thesis
  - approval_needed: boolean (always True until user approves)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThesisSnapshot:
    """Point-in-time trading thesis snapshot."""
    as_of: str  # ISO 8601 UTC
    thesis: str  # 1-paragraph trading setup
    reasoning_factors: list[str]  # Key drivers
    suggested_multiplier: float  # 0.25-1.0
    confidence: float  # 0.0-1.0
    approval_needed: bool = True


def generate_thesis(
    dashboard_metrics: dict[str, Any],
    context_score: dict[str, Any],
    news_sentiment: dict[str, Any],
    macro_calendar: dict[str, Any],
) -> ThesisSnapshot:
    """
    Generate a trading thesis using Claude API.

    Reads the trading dashboard (equity, P/L, per-edge Sharpe, correlation),
    context score (risk-off %), macro events (upcoming 24h), and news sentiment,
    then calls Claude to generate a 1-paragraph thesis explaining:
    - Current market regime and positioning logic
    - Risk factors and hedges
    - Suggested position sizing multiplier
    - Actionable decision (size up / down / hold)

    Args:
        dashboard_metrics: Dict with "as_of", "account", "global", "per_edge", etc.
        context_score: Dict with "trading_context_score", "sizing_multiplier", "factors"
        news_sentiment: Dict with "risk_off_score", "keywords_found", "headlines"
        macro_calendar: Dict with "upcoming_24h", "upcoming_48h", "impact"

    Returns:
        ThesisSnapshot with thesis, reasoning_factors, suggested_multiplier, confidence

    Raises:
        ImportError: If anthropic package not installed
        Exception: If Claude API call fails (e.g., rate limit, network error)
    """
    try:
        import anthropic
    except ImportError:
        logger.error("anthropic package not installed; install with: pip install anthropic")
        raise ImportError("anthropic package required for thesis generation")

    # Build a concise context prompt for Claude
    dashboard_summary = _summarize_dashboard(dashboard_metrics)
    context_summary = _summarize_context(context_score)
    news_summary = _summarize_news(news_sentiment)
    macro_summary = _summarize_macro(macro_calendar)

    prompt = f"""You are a quantitative trading strategist. Based on the following trading context,
generate a concise 1-paragraph trading thesis (100-150 words) that explains today's market setup.

DASHBOARD METRICS (your account & edge performance):
{dashboard_summary}

TRADING CONTEXT (risk-off score + upcoming events):
{context_summary}

NEWS SENTIMENT (market headlines & risk keywords):
{news_summary}

MACRO CALENDAR (economic events in next 24-48h):
{macro_summary}

Generate a thesis that:
1. Explains the current market regime and your positioning logic
2. Names 2-3 key risk factors or catalysts
3. Suggests a position sizing multiplier (0.5-2.0) based on risk assessment:
   - 0.5x = high uncertainty or risk-off day: trade at half base size
   - 1.0x = normal conditions: trade at full base size
   - 1.5x = favorable setup: size up moderately
   - 2.0x = high-confidence trend alignment: maximum upsize
4. Is actionable: "size up", "size down", or "hold"
5. Cites specific data from the context above

Format your response EXACTLY as follows:
THESIS: [1 paragraph explaining the setup]
FACTORS: [comma-separated list of 3-5 key drivers]
MULTIPLIER: [0.5 to 2.0]
CONFIDENCE: [0.0 to 1.0]"""

    client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY env var

    try:
        message = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=600,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        response_text = message.content[0].text
        logger.info(f"Claude thesis generation succeeded: {len(response_text)} chars")

        # Parse the response
        thesis_text, factors, multiplier, confidence = _parse_thesis_response(response_text)

        now = datetime.now(tz=timezone.utc)
        return ThesisSnapshot(
            as_of=now.isoformat(),
            thesis=thesis_text,
            reasoning_factors=factors,
            suggested_multiplier=round(multiplier, 3),
            confidence=round(confidence, 3),
            approval_needed=True,
        )

    except Exception as exc:
        logger.error(f"Claude API call failed: {exc}")
        raise


def _summarize_dashboard(metrics: dict[str, Any]) -> str:
    """Summarize dashboard metrics for Claude prompt."""
    account = metrics.get("account", {})
    global_stats = metrics.get("global", {})
    per_edge = metrics.get("per_edge", {})

    equity = account.get("equity", 0)
    balance = account.get("balance", 0)
    daily_pnl = account.get("daily_pnl", 0)
    drawdown = account.get("drawdown_pct", 0)

    total_trades = global_stats.get("total_trades", 0)
    total_pnl = global_stats.get("total_pnl", 0)
    win_rate = global_stats.get("win_rate", 0)
    sharpe = global_stats.get("sharpe", 0)
    max_dd = global_stats.get("max_dd_pct", 0)

    edge_names = []
    edge_pnls = []
    for magic_str, edge_data in per_edge.items():
        name = edge_data.get("name", f"Edge {magic_str}")
        pnl = edge_data.get("pnl", 0)
        edge_names.append(name)
        edge_pnls.append(f"{name}: {pnl:.2f} USD")

    return f"""
Equity: ${equity:.2f} (Balance: ${balance:.2f})
Daily P/L: ${daily_pnl:.2f}, Drawdown: {drawdown:.2f}%
Total Trades: {total_trades}, Total P/L: ${total_pnl:.2f}
Win Rate: {win_rate:.1%}, Sharpe: {sharpe:.2f}, Max DD: {max_dd:.2f}%
Active Edges: {', '.join(edge_names) if edge_names else 'None'}
Edge P/L: {', '.join(edge_pnls) if edge_pnls else 'N/A'}
"""


def _summarize_context(context_score: dict[str, Any]) -> str:
    """Summarize context score for Claude prompt."""
    trading_score = context_score.get("trading_context_score", 50)
    multiplier = context_score.get("sizing_multiplier", 1.0)
    factors = context_score.get("factors", {})

    news_risk = factors.get("news_risk_off", 50)
    macro_24h = factors.get("macro_event_nearby_24h", False)
    macro_48h = factors.get("macro_event_nearby_48h", False)
    impact = factors.get("highest_impact", "low")
    upcoming_24h = factors.get("upcoming_24h_count", 0)
    upcoming_48h = factors.get("upcoming_48h_count", 0)

    return f"""
Trading Context Score: {trading_score}/100
Suggested Multiplier: {multiplier:.2f}x
News Risk-Off: {news_risk}/100
Macro Events (24h): {upcoming_24h} [{impact} impact]
Macro Events (48h): {upcoming_48h}
Risk Factors: {'macro event imminent' if macro_24h else 'no near-term events'}
"""


def _summarize_news(news_sentiment: dict[str, Any]) -> str:
    """Summarize news sentiment for Claude prompt."""
    risk_score = news_sentiment.get("risk_off_score", 50)
    keywords = news_sentiment.get("keywords_found", [])[:5]
    headlines = news_sentiment.get("headlines", [])[:3]

    keyword_str = ", ".join(keywords) if keywords else "None detected"
    headline_str = "\n".join([f"  - {h}" for h in headlines]) if headlines else "  (No headlines fetched)"

    return f"""
Risk-Off Score: {risk_score}/100
Top Keywords: {keyword_str}
Recent Headlines:
{headline_str}
"""


def _summarize_macro(macro_calendar: dict[str, Any]) -> str:
    """Summarize macro calendar for Claude prompt."""
    upcoming_24h = macro_calendar.get("upcoming_24h", [])
    upcoming_48h = macro_calendar.get("upcoming_48h", [])
    impact = macro_calendar.get("impact", "low")

    events_24h_str = "\n".join([f"  - {e.get('event_name', '?')} ({e.get('impact', '?')})" for e in upcoming_24h[:3]])
    if not events_24h_str:
        events_24h_str = "  (No high-impact events in 24h)"

    return f"""
Highest Impact Level: {impact}
Events in 24h ({len(upcoming_24h)}):
{events_24h_str}
Events in 48h ({len(upcoming_48h)}): {len(upcoming_48h)} total
"""


def _parse_thesis_response(response_text: str) -> tuple[str, list[str], float, float]:
    """
    Parse Claude's structured response.

    Expected format:
        THESIS: [paragraph]
        FACTORS: [factor1, factor2, ...]
        MULTIPLIER: [0.0-1.0]
        CONFIDENCE: [0.0-1.0]

    Returns:
        (thesis_text, factors_list, multiplier, confidence)
    """
    lines = response_text.split("\n")
    thesis = ""
    factors = []
    multiplier = 0.75
    confidence = 0.7

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("THESIS:"):
            # Thesis may span multiple lines until we hit FACTORS:
            thesis = line.replace("THESIS:", "").strip()
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("FACTORS:"):
                thesis += " " + lines[i].strip()
                i += 1
            thesis = thesis.strip()
            continue

        elif line.startswith("FACTORS:"):
            factors_str = line.replace("FACTORS:", "").strip()
            factors = [f.strip() for f in factors_str.split(",") if f.strip()]
            i += 1
            continue

        elif line.startswith("MULTIPLIER:"):
            multiplier_str = line.replace("MULTIPLIER:", "").strip()
            try:
                multiplier = float(multiplier_str)
                multiplier = max(0.25, min(1.0, multiplier))  # Clamp to valid range
            except ValueError:
                logger.warning(f"Could not parse multiplier '{multiplier_str}', using 0.75")
                multiplier = 0.75
            i += 1
            continue

        elif line.startswith("CONFIDENCE:"):
            confidence_str = line.replace("CONFIDENCE:", "").strip()
            try:
                confidence = float(confidence_str)
                confidence = max(0.0, min(1.0, confidence))  # Clamp to valid range
            except ValueError:
                logger.warning(f"Could not parse confidence '{confidence_str}', using 0.7")
                confidence = 0.7
            i += 1
            continue

        else:
            i += 1

    # Fallback: if we failed to parse, return sensible defaults
    if not thesis:
        thesis = response_text[:200]  # First 200 chars as fallback
    if not factors:
        factors = ["Unable to parse factors"]

    return thesis, factors, multiplier, confidence


def write_thesis_json(path: Path, snapshot: ThesisSnapshot) -> None:
    """Write thesis snapshot to JSON file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")

    data = asdict(snapshot)
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)

    logger.info(f"Wrote thesis to {path}: multiplier={snapshot.suggested_multiplier}, confidence={snapshot.confidence}")


def load_thesis_json(path: Path) -> Optional[ThesisSnapshot]:
    """Load thesis snapshot from JSON. Returns None if missing/corrupt."""
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ThesisSnapshot(
            as_of=str(data.get("as_of", "")),
            thesis=str(data.get("thesis", "")),
            reasoning_factors=list(data.get("reasoning_factors", [])),
            suggested_multiplier=float(data.get("suggested_multiplier", 0.75)),
            confidence=float(data.get("confidence", 0.7)),
            approval_needed=bool(data.get("approval_needed", True)),
        )
    except (json.JSONDecodeError, ValueError, OSError, KeyError) as exc:
        logger.warning(f"Could not load thesis from {path}: {exc}")
        return None


if __name__ == "__main__":
    # Quick test: load sample data and generate a thesis
    import sys

    data_cache = Path("data_cache")

    # Load all inputs
    dashboard_metrics = {}
    context_score = {}
    news_sentiment = {}
    macro_calendar = {}

    try:
        if (data_cache / "dashboard_metrics.json").exists():
            dashboard_metrics = json.loads((data_cache / "dashboard_metrics.json").read_text())
        if (data_cache / "context_score.json").exists():
            context_score = json.loads((data_cache / "context_score.json").read_text())
        if (data_cache / "news_sentiment.json").exists():
            news_sentiment = json.loads((data_cache / "news_sentiment.json").read_text())
        if (data_cache / "macro_calendar.json").exists():
            macro_calendar = json.loads((data_cache / "macro_calendar.json").read_text())

        thesis = generate_thesis(dashboard_metrics, context_score, news_sentiment, macro_calendar)
        output_path = data_cache / "claude_thesis.json"

        write_thesis_json(output_path, thesis)
        print(json.dumps(asdict(thesis), indent=2, default=str))

    except Exception as exc:
        logger.error(f"Thesis generation failed: {exc}", exc_info=True)
        sys.exit(1)
