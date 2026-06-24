# WEEK 1 QUICK START
# Run this on your laptop to kick off the 4-week sprint

Write-Host ""
Write-Host "╔═══════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║        INSTITUTIONAL TRADING BOT — WEEK 1 DEPLOYMENT             ║" -ForegroundColor Cyan
Write-Host "╚═══════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# Step 1: VPS Update
Write-Host "[1/4] DEPLOY PHASES 1-3 ON VPS" -ForegroundColor Yellow
Write-Host "      This adds: slippage logging, news/macro context, dashboard, LLM thesis" -ForegroundColor Gray
Write-Host ""
Write-Host "      Open PowerShell on the VPS and run:" -ForegroundColor Yellow
Write-Host "      irm is.gd/mt5update | iex" -ForegroundColor Cyan
Write-Host ""
Write-Host "      (Keep this window open until it says 'UPDATE COMPLETE')" -ForegroundColor Gray
Write-Host ""
Read-Host "      Press ENTER when VPS update is done"

# Step 2: Verify deployment
Write-Host ""
Write-Host "[2/4] VERIFY DEPLOYMENT" -ForegroundColor Yellow
Write-Host "      Run this on the VPS to check everything:" -ForegroundColor Gray
Write-Host ""
Write-Host "      irm is.gd/mt5status | iex" -ForegroundColor Cyan
Write-Host ""
Write-Host "      Look for:"
Write-Host "      ✓ MT5 terminal running: True" -ForegroundColor Green
Write-Host "      ✓ Last MT5-GoldDrift-Live-Enter run today" -ForegroundColor Green
Write-Host "      ✓ MT5-Dashboard task present (new)" -ForegroundColor Green
Write-Host "      ✓ MT5-LLMThesis task present (new)" -ForegroundColor Green
Write-Host "      ✓ MT5-PositionMonitor task present (new)" -ForegroundColor Green
Write-Host ""
Read-Host "      Press ENTER when you've verified all green"

# Step 3: Symbol scanner prep
Write-Host ""
Write-Host "[3/4] PREPARE SYMBOL SCANNER (on your laptop)" -ForegroundColor Yellow
Write-Host "      Your scanner will discover new edges on SPY, TLT, CL, GC" -ForegroundColor Gray
Write-Host ""
Write-Host "      Run this in a PowerShell window (your laptop, not VPS):" -ForegroundColor Gray
Write-Host ""
Write-Host "      cd 'C:\Users\swann\OneDrive - BDA\trading-agent'" -ForegroundColor Cyan
Write-Host "      python scripts/multi_symbol_scanner.py --symbols SPY,TLT,CL,GC --parallel 5" -ForegroundColor Cyan
Write-Host ""
Write-Host "      (This takes 5-10 minutes. It will create files like data_cache/discovered_edges_SPY.json)" -ForegroundColor Gray
Write-Host ""

# Step 4: What to expect
Write-Host ""
Write-Host "[4/4] WHAT TO EXPECT THIS WEEK" -ForegroundColor Yellow
Write-Host ""
Write-Host "      Tomorrow morning (06:15 UTC):" -ForegroundColor Gray
Write-Host "      → You'll get a phone alert: 'Today's thesis ready'" -ForegroundColor Cyan
Write-Host "      → Read it (1 paragraph) and reply with ✓ to approve" -ForegroundColor Cyan
Write-Host ""
Write-Host "      Tomorrow evening (19:00 UTC):" -ForegroundColor Gray
Write-Host "      → Dashboard builds automatically" -ForegroundColor Cyan
Write-Host "      → You can view equity curve + per-edge metrics" -ForegroundColor Cyan
Write-Host ""
Write-Host "      Throughout the week:" -ForegroundColor Gray
Write-Host "      → You'll get trade alerts: 'GOLD opened +$8.50' when trades open/close" -ForegroundColor Cyan
Write-Host "      → LLM thesis every morning (read & approve)" -ForegroundColor Cyan
Write-Host "      → Dashboard every evening (optional viewing)" -ForegroundColor Cyan
Write-Host ""
Write-Host "      Days 4-7:" -ForegroundColor Gray
Write-Host "      → Run the symbol scanner" -ForegroundColor Cyan
Write-Host "      → It finds 3-8 new edges (SPY, TLT, crude, gold futures)" -ForegroundColor Cyan
Write-Host "      → New edges start as DISCOVERED (you'll promote them to PAPER next week)" -ForegroundColor Cyan
Write-Host ""

# Summary
Write-Host ""
Write-Host "╔═══════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║                          YOU'RE SET!                              ║" -ForegroundColor Green
Write-Host "╚═══════════════════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. On VPS: irm is.gd/mt5update | iex" -ForegroundColor Gray
Write-Host "  2. On VPS: irm is.gd/mt5status | iex (verify)" -ForegroundColor Gray
Write-Host "  3. On laptop: python scripts/multi_symbol_scanner.py --symbols SPY,TLT,CL,GC --parallel 5" -ForegroundColor Gray
Write-Host ""
Write-Host "Questions? See EXECUTION_PLAN.md for the full 4-week roadmap" -ForegroundColor Yellow
Write-Host ""
