# Research Manager V2

Read-only analysis layer for the spot and options paper trading bots.
**Strictly read-only** — never writes to state, log, or config files and never triggers trades.

## Files

```
research_manager/
  manager.py            — CLI entry point, orchestrates analysis for all bots
  spot_analysis.py      — Pure functions: AnalysisResult, analyze_spot_state()
  options_analysis.py   — Pure functions: OptionsAnalysisResult, analyze_options_state()
  reporting.py          — Formatters: spot, options, cross-bot summary
  __init__.py
```

## CLI

```bash
cd trader

# Spot summary (default)
.venv/bin/python3 -m research_manager.manager

# Spot detailed report
.venv/bin/python3 -m research_manager.manager --full

# Options summary
.venv/bin/python3 -m research_manager.manager --options

# Cross-bot summary
.venv/bin/python3 -m research_manager.manager --all

# JSON output (combinable with --spot, --options, --all)
.venv/bin/python3 -m research_manager.manager --json
.venv/bin/python3 -m research_manager.manager --options --json

# Spot verdict only (V1-compat)
.venv/bin/python3 -m research_manager.manager --verdict
```

Requires state files:
- Spot: `paper_trading_state.json`
- Options: `options_bot/state.json`

## What spot analysis covers

| Metric                  | Source                             |
|-------------------------|------------------------------------|
| Total trades            | `completed_trades[]` count         |
| Active trades           | `active_trades[]` count            |
| Net P&L                 | Sum of `net_pnl` across completed  |
| Win rate                | Wins / total completed trades      |
| Average hold time       | Mean `hold_minutes`                |
| Exit reason breakdown   | Classified from `exit_reason` text |
| P&L by combo            | Grouped by strategy@asset          |
| Strongest / weakest     | Combo ranked by net P&L            |
| Confidence level        | LOW <10, MEDIUM 10-29, HIGH 30+    |
| Worth continuing        | P&L + win rate assessment          |
| Narrow further          | Drop/keep/need-data per combo      |
| Next recommended action | Derived from confidence + combos   |

## What options analysis covers

| Metric                  | Source                                      |
|-------------------------|---------------------------------------------|
| Total trades            | `completed_trades[]` count                  |
| Open trades             | `open_trades[]` count                       |
| Realized P&L            | Sum of `realized_pnl_usd` on closed trades  |
| Unrealized P&L          | Sum of `current_pnl_usd` on open trades     |
| Premium deployed        | `portfolio.total_premium_deployed_usd`      |
| Fees paid               | Entry + exit fees across all trades         |
| Win rate                | Wins / closed trades                        |
| Average hold time       | Mean `hold_days` on closed trades           |
| Drawdown                | `portfolio.current_drawdown_pct`            |
| Kill switch status      | `portfolio.kill_switch_active`              |
| Exit reason breakdown   | From `exit_reason` enum values              |
| P&L by strategy         | Grouped by strategy name                    |
| P&L by symbol           | Grouped by underlying (SPY/QQQ)             |
| Strongest / weakest     | Strategy ranked by realized P&L             |
| Confidence level        | LOW <10, MEDIUM 10-29, HIGH 30+             |
| Worth continuing        | P&L + win rate + drawdown assessment        |
| Narrow further          | Drop/keep/need-data per strategy            |
| Next recommended action | Derived from confidence + strategies + risk |

## What cross-bot summary covers

| Section                | Content                                           |
|------------------------|---------------------------------------------------|
| Bot overview           | Trades, active, net P&L, win%, confidence per bot |
| Combined totals        | Aggregated trades, active, P&L across all bots    |
| Best & worst           | Top/bottom combo or strategy across both bots     |
| Risk alerts            | Kill switch, drawdown warnings                    |
| Overall recommendation | Capital allocation guidance based on relative perf |

## Read-only contract

- Never writes to state, log, or config files
- Never calls commands that mutate state (open, close, reset)
- No auto-fixes, no restarts, no side effects
- Subprocess calls (if any) limited to read-only commands
