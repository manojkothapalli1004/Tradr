# Trading Dashboard

Local read-only Streamlit dashboard for monitoring the spot paper bot and options paper bot.

## What it shows
- Overview KPIs across both bots
- Safer live-status indicators that distinguish recently updated, stale, uncertain, and unavailable states
- Operator alerts for kill switch state, portfolio-cap pressure, source loss, repeated recent router rejections, time-limit-only spot exit history, and missing completed options trades
- Spot bot performance, open trades, inactivity, exit patterns, and portfolio snapshots
- Options bot open positions, risk/cap state, simulator limitations, and completed-trade analytics when available
- Unified normalized trade journal across both bots, including lightweight entry-date filtering
- Cross-bot charts for P&L, trade count, exit reasons, and hold times
- Source health with row-level visibility into availability and freshness evidence

## Data sources
- `paper_trading_state.json`
- `paper_trading_v3.log`
- `options_bot/state.json`
- `options_bot/options_bot.log`

## Notes
- Read-only: no controls that can place trades or change bot state
- The dashboard tolerates missing or partial sources and surfaces gaps in the UI
- Auto-refresh defaults to `Off`; when enabled, it refreshes the dashboard body only on Streamlit runtimes that support fragments
- Live-status labels reflect available evidence from readable sources and latest observed timestamps; they do not claim actual process liveness
- Options fills and current P&L should be treated as simulator-limited because the bot uses delayed/free-tier market data and simplified theoretical pricing

## Run
```bash
cd /path/to/tradr && .venv/bin/python3 -m streamlit run dashboard/app.py
```

If Streamlit is not installed yet:
```bash
cd /path/to/tradr && .venv/bin/python3 -m pip install streamlit
```
