# options_bot — Equity ETF Options Paper-Trading Bot

> **PAPER TRADING ONLY — v1**
> This module places no real orders of any kind. All fills are simulated
> using a simplified theoretical pricing model on delayed market data.
> Results are not representative of live execution quality.
> Do not infer profitability from paper results.

---

## Overview

`options_bot/` is a self-contained Python module that runs an intraday
paper-trading loop for SPY and QQQ options. It is independent of the
existing spot bot and does not share state files, logs, or configuration
with `signal_trading/`, `paper_trading_*.py`, or the Go scheduler.

Five intraday signal strategies are evaluated each cycle. The best eligible
signal passes through a portfolio-level gate, a contract is selected from
the yfinance options chain, and a simulated fill is recorded in a local
state file.

---

## Data limitations

All data is sourced from yfinance free tier.

| Data | Delay | Limitation |
|---|---|---|
| Underlying OHLCV (5m, 1m) | ~15 min | — |
| Options chain | ~15 min | No live bid/ask |
| Greeks | — | Not available; estimated via Black-Scholes |
| Fill prices | — | Theoretical mid ± 2% slippage; not real fills |

Every fill log line and every `OptionTrade` record carries the label
`[LIMITED-SIMULATOR]`. This label must not be removed or ignored when
reviewing results.

**Strategies are hypotheses under paper validation. No strategy is assumed
to have a proven edge until results accumulate over sufficient sample size
(≥ 30 completed trades per strategy × symbol slot).**

---

## Requirements

```
Python 3.12+
uv (dependency manager)
```

Install / sync all dependencies (including yfinance):

```bash
cd trader
uv sync
```

No API keys or brokerage account required. yfinance free tier is used for
all data in paper mode.

---

## Configuration

All tunable parameters live in `options_bot/config.py`. No magic numbers
exist in any strategy or orchestration file.

Key defaults:

| Parameter | Default | Config field |
|---|---|---|
| Max total positions | 2 | `RouterConfig.max_total_positions` |
| Max positions per symbol | 1 | `RouterConfig.max_per_symbol` |
| Ranking activates after | 30 trades/slot | `RouterConfig.min_trades_for_ranking` |
| Strike selection method | OTM_FIXED (2 strikes OTM) | `ContractConfig.strike_method` / `otm_strikes` |
| Target DTE | 21 days | `ContractConfig.preferred_dte` |
| DTE range | 7–45 days | `ContractConfig.min_dte / max_dte` |
| Max premium per trade | $1,500 | `ContractConfig.max_premium_per_trade_usd` |
| Max total premium deployed | $3,000 | `RiskConfig.max_total_premium_deployed_usd` |
| Profit target | 50% of entry premium | `ContractConfig.profit_target_pct` |
| Stop loss | 100% of entry premium | `ContractConfig.stop_loss_pct` |
| Max hold time | 5 calendar days | `ContractConfig.max_hold_days` |
| Kill switch threshold | 50% drawdown | `RiskConfig.portfolio_kill_switch_pct` |
| Slippage penalty | 2% per fill | `ExecConfig.slippage_conservative_pct` |
| Circuit breaker | 4 consecutive losses → 24h pause | `RiskConfig.circuit_breaker_*` |

To switch strike selection to delta-targeting instead of fixed OTM, set in `config.py`:
```python
CONTRACT_CFG = ContractConfig(
    strike_method=StrikeMethod.DELTA,
    delta_target=0.40,
)
```

---

## Module structure

```
options_bot/
├── config.py               ← all tunable parameters
├── models.py               ← shared typed dataclasses and enums
├── interfaces.py           ← strategy/selector/fill Protocols
├── data.py                 ← yfinance wrappers (5m bars, 1m bars, chain)
├── regime.py               ← regime engine: TRENDING/RANGING/EXPANDING/UNKNOWN
├── strategies/
│   ├── __init__.py         ← ALL_STRATEGIES list
│   ├── base.py             ← abstract BaseOptionsStrategy
│   ├── opening_range_breakout.py    ← ORB (1m range + 5m breakout)
│   ├── vwap_trend_continuation.py  ← VWAP pullback bounce
│   ├── ema_trend_pullback.py       ← EMA-stack pullback bounce
│   ├── relative_volume_momentum.py ← sustained RelVol + breakout
│   └── volatility_breakout.py      ← BB squeeze resolution
├── contract_selector.py    ← chain selection: DTE, strike, liquidity
├── router.py               ← portfolio gate: caps, ranking, rejection log
├── execution.py            ← paper fill simulator (labeled LIMITED)
├── risk.py                 ← pre-trade checks, kill switch, circuit breaker
├── journal.py              ← atomic state persistence, reporting
├── runner.py               ← event loop (60s stops / 300s signals)
├── state.json              ← runtime state (auto-created; edit only while bot is stopped — e.g. kill switch reset)
├── options_bot.log         ← execution log (auto-created)
├── options_bot.pid         ← PID lockfile (auto-created)
└── tests/
    ├── test_regime.py
    ├── test_router.py
    ├── test_strategies.py
    └── test_journal.py
```

---

## Pre-run checklist

Run from the `trader/` directory before every first start or after any config change.

- [ ] `uv sync` — deps installed, `.venv/` exists
- [ ] `.venv/bin/python3 -c "import yfinance"` — yfinance importable
- [ ] `PYTHONPATH=. .venv/bin/python3 -m pytest options_bot/tests/ -q` — 92 tests pass
- [ ] No stale PID file: `ls options_bot/options_bot.pid` should return "No such file" (delete if process is dead)
- [ ] Current time is within US market hours (9:30–16:00 ET weekdays) — signal cycles are skipped outside this window
- [ ] If resuming after a kill switch trip: `"kill_switch_active"` is `false` in `options_bot/state.json` → `portfolio`

---

## Starting the bot

All commands run from the `trader/` directory.

**Run indefinitely:**
```bash
./start_options_bot.sh
```

**First paper run (recommended: smoke test during market hours):**
```bash
./start_options_bot.sh 3      # runs 3 signal cycles (~15 min) then exits
```
Check the log immediately after for errors:
```bash
grep -E "ERROR|RISK BLOCK|SIGNAL_CYCLE|OPENED|EXIT" options_bot/options_bot.log | tail -30
```

**Smoke test (same command, just a naming alias):**
```bash
./start_options_bot.sh 3
```

**Print current state and exit:**
```bash
./start_options_bot.sh status
```

**Direct invocation:**
```bash
PYTHONPATH=. .venv/bin/python3 -m options_bot.runner run
PYTHONPATH=. .venv/bin/python3 -m options_bot.runner status
PYTHONPATH=. .venv/bin/python3 -m options_bot.runner run 3
```

**Stop the bot:**
```bash
kill $(cat options_bot/options_bot.pid)
```

The PID lockfile prevents duplicate instances. If the process died without
cleaning up, delete `options_bot/options_bot.pid` before restarting.

---

## Post-run inspection checklist

After the first smoke test or any session, verify:

- [ ] Log file exists and has no Python tracebacks: `grep -c "Traceback" options_bot/options_bot.log` → should be `0`
- [ ] State file is valid JSON: `python3 -m json.tool options_bot/state.json > /dev/null` → no error
- [ ] Slots were initialized: `python3 -c "import json; s=json.load(open('options_bot/state.json')); print('slots:', len(s['algos']))"`
- [ ] If market was open: check for regime lines in log: `grep "REGIME" options_bot/options_bot.log | tail -5`
- [ ] If a trade opened: confirm `[LIMITED-SIMULATOR]` label is present: `grep "LIMITED-SIMULATOR" options_bot/options_bot.log | tail -3`
- [ ] PID file was cleaned up (bot exited cleanly): `ls options_bot/options_bot.pid` → should be gone after normal exit

---

## Running tests

```bash
cd trader
PYTHONPATH=. .venv/bin/python3 -m pytest options_bot/tests/ -v
```

Expected: **92 tests, all passing.** No network calls are made during tests.

Run a specific test file:
```bash
PYTHONPATH=. .venv/bin/python3 -m pytest options_bot/tests/test_router.py -v
```

---

## Inspecting logs, state, and journal

**Follow the live log:**
```bash
tail -f options_bot/options_bot.log
```

**Print current status (open positions, P&L summary):**
```bash
./start_options_bot.sh status
```

**Inspect raw state (all sections):**
```bash
python3 -m json.tool options_bot/state.json | head -100
```

**Last 10 completed trades:**
```bash
python3 -c "
import json
s = json.load(open('options_bot/state.json'))
for t in s['completed_trades'][-10:]:
    print(t['id'], t['symbol'], t['strategy'],
          f\"\${t['realized_pnl_usd']:+.2f}\", t['exit_reason'])
"
```

**Per-strategy performance (slots with trades only):**
```bash
python3 -c "
import json
s = json.load(open('options_bot/state.json'))
for k, v in sorted(s['algos'].items()):
    if v['total_trades'] > 0:
        wr = v['winning_trades'] / v['total_trades'] * 100
        print(f\"{k:48} trades={v['total_trades']:3d} win={wr:5.1f}% pnl=\${v['total_pnl_usd']:+.2f}\")
"
```

---

## Safety and risk behavior

| Behavior | Detail |
|---|---|
| **Portfolio kill switch** | Halts all new positions when drawdown ≥ 50% from peak equity. Does not auto-reset. To reset: stop the bot, open `options_bot/state.json`, set `"kill_switch_active": false` inside the `portfolio` object, save, and restart. |
| **Per-slot circuit breaker** | Each strategy × symbol slot pauses for 24 hours after 4 consecutive losing trades. Resets automatically after the pause period. |
| **EOD flat (force-close)** | All open positions are force-closed at 15:45 ET via `TIME_LIMIT` exit. No overnight positions are held. |
| **Position caps** | Max 2 total open positions across all symbols; max 1 per symbol. Enforced in the router before any contract selection or fill attempt. |
| **Strategy ranking gate** | Ranking by paper performance is disabled until a slot reaches ≥ 30 completed trades. Before that threshold, eligible signals are ordered by `confidence_score` only. This prevents premature promotion of lucky short runs. |
| **No-trade default** | Any ambiguous condition — UNKNOWN regime, insufficient data, no signal passing the router, no suitable contract found — results in no trade. All rejection reasons are logged. |
| **Paper mode assertion** | `PAPER_MODE = True` is hardcoded in `config.py`. The runner asserts this at startup and refuses to start if it is ever `False`. |

---

## Strategies (v1)

All five are hypotheses under paper validation. No edge is assumed or claimed.

| Strategy | Regime required | Bar source | Signal type |
|---|---|---|---|
| Opening Range Breakout (ORB) | TRENDING, EXPANDING | 1m (range) + 5m (breakout) | Directional breakout above/below first-30-min range |
| VWAP Trend Continuation | TRENDING | 5m | Pullback to session VWAP + bounce in trend direction |
| EMA Trend Pullback | TRENDING | 5m | Triple-EMA stack + pullback touch of EMA21 + bounce |
| Relative Volume Momentum | TRENDING, EXPANDING, RANGING | 5m | Sustained RelVol ≥ 2× for 5 bars + price breakout |
| Volatility Breakout | EXPANDING | 5m | Prior BB squeeze + ATR expansion + close outside bands |

**Long options only** (calls and puts). No short premium. No spreads. One
contract per trade.

---

## Known limitations (v1)

- **No live Greeks.** Delta at entry is estimated via Black-Scholes on
  delayed chain IV. Greeks are not updated after entry.
- **No IV rank filter.** IV percentile or rank is not tracked; no filter on
  whether IV is historically elevated or suppressed.
- **No holiday/half-day calendar.** Market session detection uses weekday
  and hour only. On US market holidays yfinance returns empty data; the
  strategies will produce no-signal and no position will be opened.
- **Stop checks use delayed chain mark.** The 60-second stop cycle
  re-fetches the option mid from yfinance chain data (~15-min delayed).
  If the re-fetch fails, the last stored `current_premium` is used as
  fallback. Exit timing may lag real premium moves.
- **No cross-symbol correlation tracking.** SPY and QQQ are highly
  correlated (~0.95). The position caps prevent same-symbol stacking but
  do not prevent directionally correlated exposure across both symbols.
- **No Discord or notification integration.** All output goes to
  `options_bot.log` only.
- **Kill switch requires manual reset.** Once triggered it stays active.
  To reset: stop the bot, set `"kill_switch_active": false` in
  `options_bot/state.json` → `portfolio` object, then restart.

---

## Troubleshooting

**Bot won't start — "Another instance running"**
The PID lockfile is stale. Check if the old process is actually dead, then remove:
```bash
kill -0 $(cat options_bot/options_bot.pid) 2>/dev/null && echo "still alive" || rm options_bot/options_bot.pid
```

**Log shows only "market closed — skipping" every cycle**
Signal cycles are gated to 9:30–16:00 ET weekdays. Stop checks still run (they evaluate open positions regardless). Wait for market hours or check that your system clock is correct.

**No trades after several market-hours cycles**
This is normal — the no-trade default is intentional. Check the log for:
- `REGIME ... UNKNOWN` — insufficient data for any signal
- `SIGNAL_CYCLE: no signal selected` — strategies ran but none fired
- `RISK BLOCK` — signal existed but was rejected by risk/router caps
- `no suitable contract` — chain had no expiry in the DTE window

All of these are logged with reasons. No trade is the safe default.

**state.json is corrupt or missing**
Stop the bot. Delete or rename the file. The runner creates a fresh `state.json` on next start with empty slots. All prior trade history will be lost.

**Kill switch tripped — all new positions blocked**
```bash
grep "kill_switch" options_bot/options_bot.log | tail -5
```
To reset: stop the bot, edit `options_bot/state.json`, set `"kill_switch_active": false` in the `portfolio` object, save, restart. Review the drawdown curve before resuming.

**yfinance errors / empty data**
yfinance free tier is rate-limited and occasionally returns empty responses. The bot handles this gracefully (logs a warning, skips the cycle). Persistent failures across many cycles may indicate a yfinance outage or IP throttle — wait and retry later.

---

## What this does NOT do

- Place real orders of any kind
- Connect to any brokerage or exchange
- Use live bid/ask or real-time market data
- Guarantee or imply any level of profitability
- Replace paper results with proven live performance
