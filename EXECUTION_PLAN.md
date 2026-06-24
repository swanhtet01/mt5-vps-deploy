# 4-Week Execution Plan: Option B (Aggressive)

**Goal:** Deploy Phase 1-3 + expand from 8 edges to 15-20 edges, Sharpe 2.0+, live trading with LLM guidance.

---

## **WEEK 1: Deploy Phases 1-3 + Tier 1 Scanner**

### Day 1 (Today)
**On VPS (one command):**
```powershell
irm is.gd/mt5update | iex
```
This deploys:
- ✅ Slippage logging (every fill instrumented)
- ✅ News/macro context scoring (sizing multiplier 0.25-1.0x)
- ✅ Quant dashboard (daily 19:00 UTC)
- ✅ LLM thesis generation (daily 06:00 UTC)
- ✅ Position monitoring alerts (every trade open/close on phone)

**What to expect:**
- Dashboard task added (MT5-Dashboard)
- Thesis task added (MT5-LLMThesis)
- Position monitor added (MT5-PositionMonitor)
- Next morning (06:00 UTC): first LLM thesis on phone ("Today's Setup: ...")

### Days 2-3
**Monitor baseline:**
- Check your phone for daily thesis (read it, approve with ✓)
- Check dashboard at 19:00 UTC (see equity curve + per-edge metrics)
- Watch position alerts (trade opens/closes throughout the day)
- Verify slippage logging is working: run `irm is.gd/mt5status | iex` and check if `slippage_points` is in the trade events

**Deliverable:** Confirm Phase 1-3 running smoothly (dashboard + thesis visible on phone)

### Days 4-7
**Scan Tier 1 symbols:**
```powershell
# On laptop, run this (it talks to the VPS):
python C:\Users\swann\OneDrive - BDA\trading-agent\scripts\multi_symbol_scanner.py `
  --symbols SPY,TLT,QQQ,CL,GC `
  --timeframes 1h,4h `
  --parallel 5
```

**What happens:**
- Scanner discovers calendar edges on SPY (e.g., "Monday 14:30-15:30 UTC, long"), TLT (e.g., "Thu 16:00, short"), CL (crude oil, specific hours), etc.
- All new edges start as DISCOVERED (not PAPER, not LIVE)
- Edges are written to: `data_cache/discovered_edges_SPY.json`, `discovered_edges_TLT.json`, etc.
- Summary in: `data_cache/edge_discovery_summary.json` (tells you pass/fail rate, how many Bonferroni-survived)

**Deliverable:** 3-8 new candidate edges (DISCOVERED stage)

---

## **WEEK 2: Validate New Edges (Paper Trading)**

### Days 1-3
**Promote discoveries to PAPER:**
```powershell
# For each good edge (e.g., SPY-MON-14h-LONG with t=4.5, p<0.001):
python -c "
from mt5_agent.edge_registry import EdgeRegistry, Stage
r = EdgeRegistry()
e = r.by_magic(88010)  # example: first new edge
r.stage(e.key, Stage.PAPER)
r.save()
print(f'{e.key} -> PAPER')
"
```

**What to expect:**
- New edges trade on PAPER (0.01 lot, no real money)
- They appear in the daily thesis: "SPY-MON edge live on paper, +$3 today"
- Position alerts show: "[PAPER] Bought SPY 100 shares @ 425.30"
- Dashboard shows them separately (PAPER vs LIVE columns)

**Deliverable:** 2-4 new edges on PAPER

### Days 4-7
**Monitor paper trading:**
- Win rate on PAPER edges? (should match backtest ±10%)
- Any surprises? (e.g., SPY edge only works in bull markets, not today's bear)
- Slippage reasonable? (should match backtest assumptions)
- Sizing sensible? (context score adjusting it properly?)

**Deliverable:** Validated paper edges ready to go LIVE

---

## **WEEK 3: Go Live + Add More Symbols**

### Days 1-2
**Promote best PAPER edges to LIVE:**
```powershell
# For the 2-3 best performers:
python -c "
from mt5_agent.edge_registry import EdgeRegistry, Stage
r = EdgeRegistry()
r.promote('SPY_MON_14h_LONG', Stage.LIVE)  # if it passed validation
r.save()
"
```

**What to expect:**
- Portfolio goes 8 edges (original GOLD/USDJPY/etc.) → 10-12 edges
- Position sizes now real money (0.01 lot per edge, context-adjusted)
- Daily thesis mentions new live edges
- Kill-switch now guards 12 edges (more eyes on the portfolio)

**Deliverable:** 2-4 new live edges, portfolio expanded

### Days 3-7
**Scan Tier 2 symbols:**
```powershell
# Soybeans, coffee, more indices
python C:\Users\swann\OneDrive - BDA\trading-agent\scripts\multi_symbol_scanner.py `
  --symbols ZS,KC,IWM,EEM `
  --timeframes 1h,4h `
  --parallel 5
```

**What happens:**
- Find edges on seasonal commodities (soybeans: planting/harvest windows, coffee: weather risk)
- Find edges on small-cap indices (IWM: different momentum patterns than SPY)
- Find edges on emerging markets (EEM: correlations + regime patterns)

**Deliverable:** 4-8 more DISCOVERED edges

---

## **WEEK 4: Consolidate + Optimize**

### Days 1-3
**Promote Tier 2 winners to PAPER:**
- Same as Week 2 (validate on paper, check slippage + win rate)

### Days 4-5
**Optimize and consolidate:**
```powershell
# Run this to see what you have:
python -c "
from mt5_agent.edge_registry import EdgeRegistry
r = EdgeRegistry()
live = [e for e in r.all() if e.stage == 'LIVE']
print(f'LIVE edges: {len(live)}')
for e in live:
    print(f'  {e.key}: {e.symbol}, {e.weekday}, {e.entry_hour}-{e.exit_hour}, {e.side}')
"
```

**What to optimize:**
- Any edges with negative slippage? (that's GOOD—favorable fills)
- Any edges getting sized down a lot by context score? (maybe they're fragile to news)
- Any edges from the same symbol clustered at the same time? (okay if correlation is low, but check)
- Sharpe improving? (target: > 1.8)

### Days 6-7
**Deploy best Tier 2 to LIVE:**
- 2-3 most promising
- Keep the rest as PAPER for next week's data

**Deliverable:** 15-20 total edges (8 original + 4-6 Tier 1 + 2-4 Tier 2), portfolio Sharpe targeting 2.0+

---

## **VPS Commands Reference**

Use these PowerShell one-liners to control the VPS from your laptop:

```powershell
# 1. CHECK STATUS
irm is.gd/mt5status | iex
# Output: which tasks ran, MT5 connected, equity, P/L, regime

# 2. PAUSE TRADING (emergency)
irm is.gd/mt5pause | iex
# This kills all trades and stops new ones

# 3. RESUME
irm is.gd/mt5resume | iex

# 4. RUN SYMBOL SCANNER
python C:\Users\swann\OneDrive - BDA\trading-agent\scripts\multi_symbol_scanner.py --symbols SPY,TLT,CL,GC --parallel 5

# 5. GENERATE DASHBOARD NOW (normally 19:00 UTC)
python C:\Users\swann\OneDrive - BDA\trading-agent\scripts\build_dashboard.py

# 6. GENERATE THESIS NOW (normally 06:00 UTC)
python C:\Users\swann\OneDrive - BDA\trading-agent\scripts\thesis_ingest.py

# 7. APPROVE THESIS
python C:\Users\swann\OneDrive - BDA\trading-agent\scripts\apply_approved_thesis.py

# 8. WATCH LIVE LOGS
Get-Content 'C:\mt5-paper\gold-drift\live_events.jsonl' -Tail 50 -Wait

# 9. DISABLE AN EDGE (via GitHub, applies in 5 min)
# Edit: https://github.com/swanhtet01/mt5-vps-deploy/blob/main/control.json
# Add to "disabled_edges": {"symbol": "SPY", "magic": 88010, "reason": "..."}
# VPS polls every 5 min and blacklists it
```

---

## **Daily Checklist (takes 5 min)**

✅ **06:15 UTC** — Read LLM thesis on phone, reply ✓ (or modify sizing)  
✅ **19:00 UTC** — (Optional) check dashboard, scan for red flags  
✅ **Anytime** — Run `irm is.gd/mt5status | iex` if concerned  

---

## **Success Metrics (Week 4 end target)**

| Metric | Week 1 | Week 4 target | Notes |
|--------|--------|---------------|-------|
| Live edges | 8 | 15-20 | Portfolio diversified |
| Sharpe | 1.8 | 2.0+ | Risk-adjusted return |
| Max DD | 4.2% | <3.5% | More stable |
| Daily P/L volatility | $45 | <$30 | Less noise, more signal |
| Trades/day | ~1 | ~3 | More alpha captured |

---

## **Risks (mitigated)**

1. **New edges flop on paper** → Keep them as PAPER, don't promote
2. **Portfolio correlation explodes** → portfolio_budget.json caps concurrent clusters
3. **LLM thesis is bad** → You read & approve (human-in-the-loop)
4. **Slippage worse than expected** → Logged, we re-anchor cost model in Phase 4
5. **VPS crashes** → Kill-switch + heartbeat + remote control (can pause from GitHub)

---

## **After Week 4: Phase 4 (Optional)**

Once you have 4 weeks of data + 15-20 edges + stable Sharpe 2.0:
- Read `PHASE_4_QUICK_START.md`
- Pick ONE subsystem (vol-targeting is easiest, ML is hardest)
- Build + validate on paper for 2 weeks
- Go live with 1-2 Phase 4 features

**Example:** vol-targeting could reduce daily P/L volatility from $30 → $20 (more consistent returns).
