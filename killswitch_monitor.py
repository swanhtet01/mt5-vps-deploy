"""Cumulative-drawdown auto-killswitch monitor.

Runs as a scheduled task (every hour) and watches realized P/L on the strategy magic.
If the cumulative loss over the past 30 days exceeds a HARD dollar threshold, it deletes
the live env var and pings a kill record. The bot then reverts to paper-only on its next
scheduled fire — no further real orders are sent.

Defense beyond the bot's own per-trade and per-day caps. Belt + suspenders.

Triggers:
  - 30-day realized loss <= -$60 (10% of $608 equity) → disarm
  - 7-day realized loss <= -$30 → disarm
  - Any 5-trade losing streak → disarm
  - Account equity drops below $500 (≈18% drawdown) → disarm

When disarmed, the user must MANUALLY re-arm by re-setting MT5_GOLD_DRIFT_LIVE=1 and
investigating what happened. The bot self-stops; it does not self-restart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

from mt5_agent.mt5_execution import (
    FeedHistoryWindow,
    coherent_feed_clock_from_mt5,
    history_window_from_feed_clock,
    persistent_user_flag_enabled,
)
from mt5_agent.trade_history import closed_trades_from_deals

# The kill-switch disarms MT5_GOLD_DRIFT_LIVE, which arms ALL live edges -- so the cumulative
# loss/streak guards MUST sum across ALL live magics, not just gold (88001). Otherwise a
# drawdown on USDJPY/UK100/etc. never trips it and only the per-day caps + equity floor cover
# them. 88009 (GOLD_TUE) is culled, so excluded.
LIVE_MAGICS = {88001, 88002, 88003, 88004, 88005, 88006, 88007, 88008,
               88011, 88012, 88013, 88014}  # includes intraday mean-rev
LIVE_ENV_FLAG = "MT5_GOLD_DRIFT_LIVE"
LOG = Path(r"C:\mt5-paper\gold-drift\killswitch.jsonl")

# Hard thresholds (all in USD). RECALIBRATED 2026-08-11: lot sizes scaled 5x (0.01->0.05)
# so per-trade P&L and variance are 5x larger. Weekly noise floor is now ~$50-150 at 0.05 lot.
# Old thresholds (-$65/7d, -$110/30d) would false-trip on a single bad structural trade.
# New thresholds match the user's stated $100 risk tolerance and the 5x position scale-up.
# Equity floor lowered slightly to give more room while still catching catastrophic drawdowns.
# WIDENED 2026-09-01 at the account owner's request: the cumulative limits were halting
# live trading on ordinary variance rather than on genuine loss.
#
# The arithmetic that forced this, at ~$632 equity and a $480 floor:
#   * only $152 of room exists before the equity floor trips at all;
#   * the -$100 7-day limit therefore sat INSIDE the documented $50-150 weekly noise band
#     for 0.05 lots -- a normal losing week disarmed live trading and required a manual
#     re-arm, which is exactly the false-trip being complained about;
#   * the -$200 30-day limit was already unreachable: $632 - $200 = $432 is below the
#     floor, so the floor always fired first. It has never once been the binding brake.
#
# Both cumulative limits are now set outside the noise band. The honest consequence, stated
# rather than buried: at this lot size the weekly noise (~$150) is the same magnitude as the
# whole drawdown budget (~$152), so THE EQUITY FLOOR IS NOW THE ONLY BRAKE THAT CAN FIRE on
# this balance. The cumulative limits become live again only once equity grows enough to put
# real distance between it and the floor. If the goal is a working early brake rather than a
# single catastrophic backstop, the fix is a smaller lot size, not looser thresholds --
# thresholds cannot create room that the position size has already spent.
#
# The floor is deliberately UNCHANGED. It is the one control that still bounds a losing run.
THRESH_30D_LOSS = -300.0      # was -200, which the floor dominated; outside a month of noise
THRESH_7D_LOSS = -150.0       # was -100, inside the $50-150 weekly noise band at 0.05 lot
THRESH_LOSING_STREAK = 10     # was 7; ~1.5 weeks of every edge losing, not a normal streak
THRESH_EQUITY_FLOOR = 480.0   # UNCHANGED -- ~24% drawdown from $632; the real backstop
REFERENCE_SYMBOLS = ("BTCUSD", "GOLD", "USDJPY")


def append(event: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, indent=2, default=str))


def _live_flag_is_set() -> bool:
    return persistent_user_flag_enabled(LIVE_ENV_FLAG)


def disarm(reason: str, payload: dict) -> None:
    # Idempotent: if live trading is ALREADY disarmed, the brake has already done its job --
    # log it quietly but do NOT re-send the phone alert. (Re-firing every 2h was alert spam.)
    if not _live_flag_is_set():
        append({"event": "killswitch_breach_already_disarmed",
                "ts": datetime.now(tz=timezone.utc).isoformat(), "reason": reason, **payload})
        return
    # Remove the user-scope env var so subsequent scheduled fires run paper-only
    subprocess.run([
        "powershell.exe", "-NoProfile", "-Command",
        f"[Environment]::SetEnvironmentVariable('{LIVE_ENV_FLAG}', $null, 'User')"
    ], check=False)
    append({"event": "KILL_SWITCH_FIRED", "ts": datetime.now(tz=timezone.utc).isoformat(),
            "reason": reason, **payload})
    try:  # loud phone alert -- a fired kill-switch must not be a silent JSONL line
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import notify as _notify
        _notify.send_ntfy(f"LIVE TRADING DISARMED. Reason: {reason}. The bot is now paper-only "
                          f"until you re-arm MT5_GOLD_DRIFT_LIVE=1 and investigate.",
                          title="KILL-SWITCH FIRED", tags="rotating_light")
    except Exception:
        pass


def recent_history(host_utc: datetime) -> tuple[str, FeedHistoryWindow, list]:
    symbol, clock = coherent_feed_clock_from_mt5(
        mt5,
        REFERENCE_SYMBOLS,
        host_utc=host_utc,
    )
    window = history_window_from_feed_clock(clock, lookback=timedelta(days=45))
    deals = mt5.history_deals_get(window.start, window.end)
    if deals is None:
        raise RuntimeError(f"history_deals_get failed: {mt5.last_error()}")
    return symbol, window, list(deals)


def summarize_losses(deals: list, history_window: FeedHistoryWindow) -> dict[str, float | int]:
    feed_now = history_window.clock.feed_time
    position_deals = [
        deal
        for deal in deals
        if int(getattr(deal, "position_id", 0) or 0) > 0
        and bool(str(getattr(deal, "symbol", "") or "").strip())
    ]
    closed_trades = closed_trades_from_deals(position_deals)
    account_pnls = [(trade.close_time, trade.net) for trade in closed_trades]
    agent_pnls = [
        (trade.close_time, trade.net)
        for trade in closed_trades
        if trade.magic in LIVE_MAGICS
    ]

    def realized(period_days: int, values: list[tuple[datetime, float]]) -> float:
        cutoff = feed_now - timedelta(days=period_days)
        return sum(
            pnl
            for closed_at, pnl in values
            if cutoff <= closed_at <= history_window.end
        )

    streak = 0
    for _, pnl in reversed(agent_pnls):
        if pnl < 0:
            streak += 1
        else:
            break
    return {
        "n_closed_trades": len(agent_pnls),
        "n_account_closed_trades": len(account_pnls),
        "realized_30d_usd": realized(30, agent_pnls),
        "realized_7d_usd": realized(7, agent_pnls),
        "account_realized_30d_usd": realized(30, account_pnls),
        "account_realized_7d_usd": realized(7, account_pnls),
        "current_losing_streak": streak,
    }


def main():
    if not mt5.initialize():
        # Loud, not silent. This is the drawdown brake: if MT5 is down it is not "skipping a
        # cycle", it is INERT -- no threshold can be evaluated and nothing can be disarmed.
        # Returning here wrote nothing to killswitch.jsonl and exited 0, so the task read
        # Ready forever and the log had no gap to notice. An event plus a non-zero exit make
        # a blind brake visible to both the JSONL and Task Scheduler's LastTaskResult.
        error = str(mt5.last_error())
        print(f"killswitch: mt5.initialize failed: {error} (brake is INERT this cycle)",
              file=sys.stderr)
        append({
            "event": "killswitch_inert",
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "reason": "mt5.initialize failed",
            "error": error,
        })
        sys.exit(1)
    try:
        now = datetime.now(tz=timezone.utc)
        ai = mt5.account_info()
        if ai is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        try:
            reference_symbol, history_window, deals = recent_history(now)
        except RuntimeError as exc:
            payload = {"error": str(exc), "host_utc": now.isoformat()}
            if _live_flag_is_set():
                disarm("history feed clock unavailable", payload)
            else:
                append({"event": "killswitch_history_unavailable", "ts": now.isoformat(), **payload})
            return
        losses = summarize_losses(deals, history_window)
        last_30d = float(losses["realized_30d_usd"])
        last_7d = float(losses["realized_7d_usd"])
        account_last_30d = float(losses["account_realized_30d_usd"])
        account_last_7d = float(losses["account_realized_7d_usd"])
        streak = int(losses["current_losing_streak"])

        state = {
            "ts": now.isoformat(),
            "history_reference_symbol": reference_symbol,
            **history_window.as_dict(),
            "equity": ai.equity, "balance": ai.balance,
            "n_closed_trades": int(losses["n_closed_trades"]),
            "n_account_closed_trades": int(losses["n_account_closed_trades"]),
            "realized_30d_usd": round(last_30d, 2),
            "realized_7d_usd": round(last_7d, 2),
            "account_realized_30d_usd": round(account_last_30d, 2),
            "account_realized_7d_usd": round(account_last_7d, 2),
            "current_losing_streak": streak,
            "persistent_live_authorized": _live_flag_is_set(),
            "process_env_value": os.environ.get(LIVE_ENV_FLAG, "(unset)"),
        }
        append({"event": "monitor_heartbeat", **state})

        reasons = []
        if last_30d <= THRESH_30D_LOSS:
            reasons.append(f"30-day loss ${last_30d:.2f} <= ${THRESH_30D_LOSS}")
        if last_7d <= THRESH_7D_LOSS:
            reasons.append(f"7-day loss ${last_7d:.2f} <= ${THRESH_7D_LOSS}")
        if account_last_30d <= THRESH_30D_LOSS:
            reasons.append(
                f"account 30-day loss ${account_last_30d:.2f} <= ${THRESH_30D_LOSS}"
            )
        if account_last_7d <= THRESH_7D_LOSS:
            reasons.append(
                f"account 7-day loss ${account_last_7d:.2f} <= ${THRESH_7D_LOSS}"
            )
        if streak >= THRESH_LOSING_STREAK:
            reasons.append(f"losing streak {streak} >= {THRESH_LOSING_STREAK}")
        if ai.equity <= THRESH_EQUITY_FLOOR:
            reasons.append(f"equity ${ai.equity:.2f} <= floor ${THRESH_EQUITY_FLOOR}")

        if reasons:
            disarm(" | ".join(reasons), state)
        else:
            print(f"OK — all killswitch thresholds clear. equity=${ai.equity:.2f} "
                  f"agent30d=${last_30d:+.2f} agent7d=${last_7d:+.2f} "
                  f"account30d=${account_last_30d:+.2f} "
                  f"account7d=${account_last_7d:+.2f} streak={streak}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
