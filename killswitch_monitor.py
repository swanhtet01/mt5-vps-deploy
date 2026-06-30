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

# The kill-switch disarms MT5_GOLD_DRIFT_LIVE, which arms ALL live edges -- so the cumulative
# loss/streak guards MUST sum across ALL live magics, not just gold (88001). Otherwise a
# drawdown on USDJPY/UK100/etc. never trips it and only the per-day caps + equity floor cover
# them. 88009 (GOLD_TUE) is culled, so excluded.
LIVE_MAGICS = {88001, 88002, 88003, 88004, 88005, 88006, 88007, 88008}
LIVE_ENV_FLAG = "MT5_GOLD_DRIFT_LIVE"
LOG = Path(r"C:\mt5-paper\gold-drift\killswitch.jsonl")

# Hard thresholds (all in USD; account is ~$608 equity)
THRESH_30D_LOSS = -60.0
THRESH_7D_LOSS = -30.0
THRESH_LOSING_STREAK = 5
THRESH_EQUITY_FLOOR = 500.0


def append(event: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, indent=2, default=str))


def _live_flag_is_set() -> bool:
    """Read MT5_GOLD_DRIFT_LIVE fresh from the HKCU registry (a freshly-launched scheduled
    task can carry a stale process env). True only if the flag is present and non-empty."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as _k:
            val, _ = winreg.QueryValueEx(_k, LIVE_ENV_FLAG)
            return bool(str(val).strip())
    except FileNotFoundError:
        return False
    except Exception:
        return bool(os.environ.get(LIVE_ENV_FLAG))


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


def main():
    if not mt5.initialize():
        print(f"killswitch: mt5.initialize failed: {mt5.last_error()} (skipping this cycle)", file=sys.stderr)
        return
    try:
        now = datetime.now(tz=timezone.utc)
        ai = mt5.account_info()
        # Pull deals since bot inception (be safe with a wider window)
        start = now - timedelta(days=45)
        deals = mt5.history_deals_get(start, now + timedelta(minutes=1))
        our_exits = sorted([d for d in (deals or []) if d.magic in LIVE_MAGICS and d.entry == 1],
                           key=lambda d: d.time)
        pnls = [(datetime.fromtimestamp(d.time, tz=timezone.utc), d.profit + d.commission + d.swap)
                for d in our_exits]

        last_30d = sum(p for t, p in pnls if (now - t).days <= 30)
        last_7d = sum(p for t, p in pnls if (now - t).days <= 7)
        # losing streak (most recent N consecutive losses)
        streak = 0
        for _, p in reversed(pnls):
            if p < 0:
                streak += 1
            else:
                break

        state = {
            "ts": now.isoformat(),
            "equity": ai.equity, "balance": ai.balance,
            "n_closed_trades": len(pnls),
            "realized_30d_usd": round(last_30d, 2),
            "realized_7d_usd": round(last_7d, 2),
            "current_losing_streak": streak,
            "env_flag_now": os.environ.get(LIVE_ENV_FLAG, "(unset)"),
        }
        append({"event": "monitor_heartbeat", **state})

        reasons = []
        if last_30d <= THRESH_30D_LOSS:
            reasons.append(f"30-day loss ${last_30d:.2f} <= ${THRESH_30D_LOSS}")
        if last_7d <= THRESH_7D_LOSS:
            reasons.append(f"7-day loss ${last_7d:.2f} <= ${THRESH_7D_LOSS}")
        if streak >= THRESH_LOSING_STREAK:
            reasons.append(f"losing streak {streak} >= {THRESH_LOSING_STREAK}")
        if ai.equity <= THRESH_EQUITY_FLOOR:
            reasons.append(f"equity ${ai.equity:.2f} <= floor ${THRESH_EQUITY_FLOOR}")

        if reasons:
            disarm(" | ".join(reasons), state)
        else:
            print(f"OK — all killswitch thresholds clear. equity=${ai.equity:.2f} "
                  f"30d=${last_30d:+.2f} 7d=${last_7d:+.2f} streak={streak}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
