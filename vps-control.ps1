# vps-control.ps1 — Master VPS control panel. Run from your laptop.
# Usage: .\vps-control.ps1 -action <status|update|pause|resume|scanner|dashboard|thesis|logs>
#
# Examples:
#   .\vps-control.ps1 -action status                    # Check bot health
#   .\vps-control.ps1 -action scanner -symbols SPY,TLT  # Run symbol scanner
#   .\vps-control.ps1 -action dashboard                 # Generate dashboard now
#   .\vps-control.ps1 -action thesis                    # Generate LLM thesis now
#   .\vps-control.ps1 -action logs -follow              # Watch live logs

param(
    [string]$action = "status",
    [string]$symbols = "",
    [int]$parallel = 5,
    [switch]$follow = $false
)

$ErrorActionPreference = 'SilentlyContinue'
$vps_ip = "213.136.89.119"
$vps_port = 63082

function Invoke-VPS {
    param([string]$cmd)
    # For now, output the command to run on the VPS
    # In week 2, we'll wire actual SSH/RDP
    Write-Host "=== RUN THIS ON VPS POWERSHELL ===" -ForegroundColor Cyan
    Write-Host $cmd -ForegroundColor Yellow
    Write-Host "==================================" -ForegroundColor Cyan
}

switch ($action) {
    "status" {
        Invoke-VPS "irm is.gd/mt5status | iex"
    }
    "update" {
        Write-Host "⚠️  This redeploys the entire bot. Proceed? (y/n)" -ForegroundColor Yellow
        if ((Read-Host).ToLower() -eq 'y') {
            Invoke-VPS "irm is.gd/mt5update | iex"
        }
    }
    "pause" {
        Write-Host "⏸️  Pausing all trading..." -ForegroundColor Yellow
        Invoke-VPS "irm is.gd/mt5pause | iex"
    }
    "resume" {
        Write-Host "▶️  Resuming trading..." -ForegroundColor Green
        Invoke-VPS "irm is.gd/mt5resume | iex"
    }
    "scanner" {
        if (-not $symbols) { $symbols = "SPY,TLT,CL,GC"; Write-Host "No symbols specified; using default: $symbols" }
        Write-Host "🔍 Scanning symbols: $symbols" -ForegroundColor Cyan
        Invoke-VPS "cd C:\trading-agent && python scripts\multi_symbol_scanner.py --symbols $symbols --parallel $parallel"
        Write-Host "✅ New edges should appear in data_cache/discovered_edges_*.json"
    }
    "dashboard" {
        Write-Host "📊 Generating dashboard (may take 30 sec)..." -ForegroundColor Cyan
        Invoke-VPS "cd C:\trading-agent && python scripts\build_dashboard.py"
        Write-Host "✅ Dashboard saved to data_cache/dashboard.html"
    }
    "thesis" {
        Write-Host "🧠 Generating LLM thesis (reads dashboard + news + macro)..." -ForegroundColor Cyan
        Invoke-VPS "cd C:\trading-agent && python scripts\thesis_ingest.py"
        Write-Host "✅ Thesis saved to data_cache/claude_thesis.json (check phone for alert)"
    }
    "approve" {
        Write-Host "✅ Approving today's LLM thesis (1.0x multiplier)..." -ForegroundColor Green
        Invoke-VPS "cd C:\trading-agent && python scripts\apply_approved_thesis.py"
    }
    "logs" {
        Write-Host "📝 Live logs:" -ForegroundColor Cyan
        Invoke-VPS "Get-Content 'C:\mt5-paper\gold-drift\live_events.jsonl' -Tail 50 -Wait"
    }
    "promote" {
        Write-Host "⚡ Promote a PAPER edge to LIVE (requires testing first!)" -ForegroundColor Yellow
        $magic = Read-Host "Magic number to promote"
        if ($magic) {
            Invoke-VPS "cd C:\trading-agent && python -c `"from mt5_agent.edge_registry import *; r=EdgeRegistry(); e=r.by_magic($magic); r.promote(e.key, Stage.LIVE); r.save(); print(f'{e.key} -> LIVE')`""
        }
    }
    "blacklist" {
        Write-Host "🚫 Disable an edge remotely (via GitHub control.json)" -ForegroundColor Yellow
        Write-Host "This will be live in 5 minutes on the VPS. Edit: https://github.com/swanhtet01/mt5-vps-deploy/blob/main/control.json"
    }
    "config" {
        Write-Host "⚙️  Show current VPS configuration" -ForegroundColor Cyan
        Invoke-VPS "cd C:\trading-agent && python -c `"import json; from paths import *; r=read_json(EDGE_REGISTRY_PATH); print(f'Live edges: {len([e for e in r.get(\"edges\",[]) if e.get(\"stage\")==\"LIVE\"])}'); print(json.dumps([e for e in r.get('edges',[]) if e.get('stage')=='LIVE'][:3], indent=2))`""
    }
    default {
        Write-Host @"
VPS Control Panel - Master command reference

USAGE:  .\vps-control.ps1 -action <command> [options]

COMMANDS:
  status              Check bot health (equity, tasks, P/L)
  update              Deploy latest code (careful!)
  pause               Stop all trading (emergency)
  resume              Resume trading after pause
  scanner             Discover new edges (e.g., -symbols SPY,TLT,CL -parallel 5)
  dashboard           Generate dashboard now (normally runs at 19:00 UTC)
  thesis              Generate LLM thesis now (normally at 06:00 UTC)
  approve             Approve today's thesis (1.0x multiplier)
  logs                Watch live trade log (follow with -follow)
  promote             Promote a PAPER edge to LIVE (requires magic number)
  blacklist           Edit GitHub control.json to disable edges remotely
  config              Show current VPS config

EXAMPLES:
  .\vps-control.ps1 -action status
  .\vps-control.ps1 -action scanner -symbols SPY,TLT,CL -parallel 5
  .\vps-control.ps1 -action dashboard
  .\vps-control.ps1 -action pause

"@ -ForegroundColor Cyan
    }
}
