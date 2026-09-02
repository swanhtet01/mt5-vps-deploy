# mt5-vps-deploy — rules for Claude Code / Codex sessions

This repo **installs and updates a live trading VPS**. Its contents place real orders on a real
XM account. Nothing here is a sandbox. Read this before your first edit.

The monorepo (`swanhtet01/supermega-workspace`) has its own `CLAUDE.md` and `AGENTS.md`; the
branch-discipline and never-`git add .` rules there apply here too.

---

## 1. Two delivery paths, and only one of them is pinned

Code reaches `C:\trading-agent` two ways, in this order:

1. **The release bundle** (`mt5-bundle.zip`) is robocopied over `scripts\` and `src\`. It is
   whatever the LATEST GITHUB RELEASE holds — **not** pinned to the deploy commit. Anything not
   in the manifest is only as fresh as that release.
2. **`hotfix-manifest.json`** is then re-applied on top, every file sha256-verified and pinned to
   `$deployRef`. The manifest wins.

`boot.ps1` and `finish.ps1` install from the bundle and **never reach the manifest sync** — they
hand off to `vps_one_shot.ps1` / `vps_bootstrap.ps1`, neither of which runs `update.ps1`. So a
freshly provisioned box runs bundle-only code until someone runs the updater.

## 2. Edit a manifest-delivered file → regenerate its sha256

This is the easiest way to break the VPS silently. `update.ps1` throws on the first mismatch,
which aborts the **whole** installer — including the task registration further down the file. No
update lands, on a box nobody logs into, and the auto-deploy log may still say it succeeded.

Before pushing, always:

```
python3 ci/verify_deploy.py
```

Pure stdlib, no network, runs on Linux. It also checks that manifest destinations cannot escape
the repo root, that every delivered `.py` parses, and that **every task `tasks.ps1` calls
CRITICAL is actually created by some installer here** — a check that exists because the kill
switch and VPS health watch were listed as critical for months while nothing created them.

## 3. Three different scripts create scheduled tasks

Do not assume `update.ps1` owns them all:

| Installer | Creates |
|---|---|
| `update.ps1` | most `MT5-*` tasks |
| `harden.ps1` | `MT5-Watchdog` — a generated `.cmd` that restarts `terminal64.exe` |
| `layer2.ps1` | `MT5-RemoteControl` |

A task missing from `update.ps1` is not necessarily unowned. Check all three before concluding a
task is dead — `MT5-Watchdog` looks absent from the tree because it is a generated `.cmd`, and it
is arguably the most important self-healing component on the box.

## 4. PowerShell gotchas that have already cost real outages

- **`return` is not a terminating error.** `auto_deploy.ps1` runs `update.ps1` through
  `Invoke-Expression` inside a try/catch. A `return` unwinds with no exception, so the catch never
  fires and the deploy is banked as successful. Use `throw` for any abort that must be noticed.
- **`schtasks /change /enable` cannot create a task.** It silently no-ops on one that was never
  registered, so it is never a valid remedy for a missing task.
- **`$LASTEXITCODE` after a native command throws on PowerShell 7.4+** under
  `$ErrorActionPreference='Stop'`. Prefer `Get-ScheduledTask -ErrorAction SilentlyContinue` when
  "missing" is a normal outcome.
- **Localised output.** `schtasks /query /v` column headers are translated; matching on
  `"Last Result"` finds nothing on a non-English box. Use `Get-ScheduledTaskInfo`.

## 5. Money-path rules

- **Never arm live trading.** `MT5_GOLD_DRIFT_LIVE` (HKCU `Environment`) is the master switch. The
  only automated writer is `killswitch_monitor.py`, and it only ever **clears** it.
- Never widen `allowed_symbols`, raise `max_order_volume` or `risk_per_trade_pct`, or reconcile a
  sizing clamp upward, without Swan's explicit say-so. `mt5_execution.py`'s `context_sizing` caps
  the LLM thesis multiplier at 1.0; two other files and several docstrings claim 2× is intended.
  Do not "fix" that inconsistency toward the docs.
- Never skip, disable or quarantine a test to get CI green.
- Do not execute trades or move money.

## 6. A permanently red check is worse than no check

`tasks.ps1` spent months listing tasks nothing installed, so the health panel was always red and
the operator learned to ignore it. That is how the drawdown brake went missing unnoticed. If a
check cannot pass, fix it or remove it — do not let it sit red.
