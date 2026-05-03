"""
options_bot/runner.py — Main event loop.

PAPER TRADING ONLY. No real orders are placed.

Two-frequency loop:
  Every STOP_INTERVAL  (60s):  fetch current underlying prices →
                                check exit conditions on open positions.
  Every SIGNAL_INTERVAL (300s): fetch 5m + 1m bars → detect regime →
                                 run strategies → route signal →
                                 select contract → risk check → open position.

Market session gate:  signal cycles are skipped outside 9:30–16:00 ET.
EOD flat:             check_exit() returns TIME_LIMIT at 15:45 ET for any
                      open position; all positions are closed before the close.
PID lockfile:         prevents duplicate instances.

Module coordination (no logic duplicated here):
  data           → fetch bars and prices
  regime         → detect_regime()
  strategies     → ALL_STRATEGIES instances
  router         → route_signals()
  contract_selector → select_contract()
  risk           → check_can_open(), deduct_for_entry(), record_trade_result(),
                   update_portfolio_state()
  execution      → open_position(), close_position(), update_mark(), check_exit()
  journal        → load/save/report
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from options_bot.config import (
    ASSETS, CONTRACT_CFG, PAPER_MODE,
    STATE_FILE, LOG_FILE, PID_FILE, OPTIONS_DATA_LIMITATION,
)
from options_bot.data import (
    fetch_5m_bars, fetch_1m_bars, fetch_current_price,
    fetch_option_mark, is_market_open,
)
from options_bot.regime import detect_regime
from options_bot.strategies import ALL_STRATEGIES
from options_bot.router import route_signals
from options_bot.contract_selector import select_contract
from options_bot.risk import (
    check_can_open, deduct_for_entry, record_trade_result, update_portfolio_state,
)
from options_bot.execution import open_position, close_position, update_mark, check_exit
from options_bot.journal import (
    load_state, save_state, ensure_slots,
    get_slots, put_slots,
    get_open_trades, put_open_trades,
    add_completed, get_portfolio, put_portfolio,
    format_status, performance_summary,
)
from options_bot.models import Regime

assert PAPER_MODE, "options_bot: PAPER_MODE is False — refusing to start."

STOP_INTERVAL   = 60     # seconds between stop/exit checks
SIGNAL_INTERVAL = 300    # seconds between full signal cycles (5 min)
STATUS_EVERY_N  = 6      # print status every N signal cycles (~30 min)


# ── Logging ──────────────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    log = logging.getLogger("options_bot")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)-35s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    for h in (logging.FileHandler(LOG_FILE), logging.StreamHandler()):
        h.setFormatter(fmt)
        log.addHandler(h)
    return log


# ── PID guard ─────────────────────────────────────────────────────────────────────

def _acquire_pid(log: logging.Logger) -> bool:
    pid = os.getpid()
    if os.path.exists(PID_FILE):
        try:
            old = int(open(PID_FILE).read().strip())
            os.kill(old, 0)
            log.error("Another instance running (PID %d). Delete %s to force.", old, PID_FILE)
            return False
        except (ProcessLookupError, ValueError):
            pass   # dead process — take over
    open(PID_FILE, "w").write(str(pid))
    return True


def _release_pid() -> None:
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


# ── Stop check cycle (every 60s) ─────────────────────────────────────────────────

def _stop_cycle(state: dict, log: logging.Logger) -> dict:
    """
    Fetch current underlying prices, update marks, and close any positions
    that hit an exit condition (stop-loss, profit-target, time-limit, EOD).
    Uses last stored current_premium as the mark when live fetch fails.
    """
    open_trades = get_open_trades(state)
    if not open_trades:
        return state

    slots            = get_slots(state)
    ps               = get_portfolio(state)
    changed          = False
    total_pnl_closed = 0.0

    for trade in list(open_trades):
        # Attempt to get a fresh option mark from the chain (still delayed, labeled LIMITED).
        # Falls back to last stored current_premium if chain re-fetch fails.
        live_mark = fetch_option_mark(
            trade.symbol, trade.expiry, trade.strike, trade.option_type.value
        )
        mark = live_mark if (live_mark and live_mark > 0) else trade.current_premium

        update_mark(trade, mark)
        reason = check_exit(trade, mark, CONTRACT_CFG)

        if reason is None:
            continue

        closed  = close_position(trade, mark, reason)
        key     = closed.algo_key
        if key in slots:
            slots[key] = record_trade_result(slots[key], closed)
        open_trades      = [t for t in open_trades if t.id != closed.id]
        state            = add_completed(state, closed)
        changed          = True
        total_pnl_closed += closed.realized_pnl_usd
        log.info("EXIT %s reason=%s pnl=$%.2f", closed.id, reason.value, closed.realized_pnl_usd)

    if changed:
        state = put_open_trades(state, open_trades)
        state = put_slots(state, slots)
        ps    = update_portfolio_state(ps, open_trades, closed_pnl_delta=total_pnl_closed)
        state = put_portfolio(state, ps)
        save_state(state)

    return state


# ── Signal cycle (every 300s) ─────────────────────────────────────────────────────

def _signal_cycle(state: dict, log: logging.Logger) -> dict:
    """
    Full decision cycle: fetch data → regime → strategies → route →
    contract → risk → open.
    Skipped entirely outside market hours.
    """
    if not is_market_open():
        log.info("SIGNAL_CYCLE: market closed — skipping")
        return state

    slots       = get_slots(state)
    open_trades = get_open_trades(state)
    ps          = get_portfolio(state)

    all_signals = []

    for symbol in ASSETS:
        # Fetch bars
        df_5m = fetch_5m_bars(symbol)
        if df_5m is None or df_5m.empty:
            log.warning("SIGNAL_CYCLE: no 5m data for %s — skipping", symbol)
            continue

        df_1m = fetch_1m_bars(symbol)   # None is handled inside ORB strategy

        # Regime
        snap = detect_regime(symbol, df_5m)
        state["last_regimes"][symbol] = snap.to_dict()

        if snap.regime == Regime.UNKNOWN:
            log.info("REGIME %s UNKNOWN — no new positions", symbol)
            continue

        log.info("REGIME %s: %s ADX=%.1f ATR=%.4f", symbol, snap.regime.value, snap.adx, snap.atr)

        # Strategies
        for strategy_cls in ALL_STRATEGIES:
            instance = strategy_cls()
            try:
                sig = instance.evaluate(
                    symbol=symbol, df_5m=df_5m, regime=snap, df_1m=df_1m,
                )
            except Exception as exc:
                log.error("STRATEGY %s %s failed: %s", strategy_cls.strategy_id, symbol, exc, exc_info=True)
                sig = None

            if sig is not None:
                log.debug("SIGNAL: %s", sig)
                all_signals.append(sig)

    # Router: select at most one signal
    chosen = route_signals(all_signals, slots, open_trades)
    if chosen is None:
        log.info("SIGNAL_CYCLE: no signal selected")
        state = put_slots(state, slots)
        save_state(state)
        return state

    # Contract selection
    contract = select_contract(chosen)
    if contract is None:
        log.info("SIGNAL_CYCLE: no suitable contract for %s — no trade", chosen)
        state = put_slots(state, slots)
        save_state(state)
        return state

    # Risk gate
    cost = contract.estimated_premium * 100
    allowed, reason = check_can_open(
        chosen.symbol, chosen.strategy_name.value,
        slots, ps, cost,
    )
    if not allowed:
        log.info("RISK BLOCK %s@%s — %s", chosen.strategy_name.value, chosen.symbol, reason)
        state = put_slots(state, slots)
        save_state(state)
        return state

    # Open paper position
    trade = open_position(chosen, contract)
    key   = trade.algo_key
    if key in slots:
        slots[key] = deduct_for_entry(slots[key], trade)
    open_trades.append(trade)

    state = put_open_trades(state, open_trades)
    state = put_slots(state, slots)
    ps    = update_portfolio_state(ps, open_trades, closed_pnl_delta=0.0)
    state = put_portfolio(state, ps)
    save_state(state)

    log.info(
        "OPENED %s | %s %s K=%.2f exp=%s cost=$%.2f | %s",
        trade.id, chosen.symbol, contract.option_type.value,
        contract.strike, contract.expiry, cost, OPTIONS_DATA_LIMITATION,
    )
    return state


# ── Main loop ─────────────────────────────────────────────────────────────────────

def run(cycles: Optional[int] = None) -> None:
    log = _setup_logger()

    if not _acquire_pid(log):
        sys.exit(1)

    log.info(
        "START options_bot [PAPER ONLY] symbols=%s pid=%d | %s",
        list(ASSETS.keys()), os.getpid(), OPTIONS_DATA_LIMITATION,
    )

    try:
        state = load_state()
        state = ensure_slots(state)
        save_state(state)
        log.info("INIT slots=%d", len(state["algos"]))

        ticks_per_signal = SIGNAL_INTERVAL // STOP_INTERVAL   # 5
        stop_tick   = 0
        sig_cycles  = 0

        while True:
            stop_tick += 1
            state = _stop_cycle(state, log)

            if stop_tick % ticks_per_signal == 1:
                sig_cycles += 1
                log.info("SIGNAL_CYCLE #%d (tick=%d)", sig_cycles, stop_tick)
                state = _signal_cycle(state, log)

                if sig_cycles % STATUS_EVERY_N == 0:
                    log.info("\n%s", format_status(state))

                if cycles is not None and sig_cycles >= cycles:
                    log.info("Completed %d cycles. Final report:", cycles)
                    log.info("\n%s", format_status(state))
                    break

            log.debug(
                "next stop in %ds | next signal in %ds",
                STOP_INTERVAL,
                SIGNAL_INTERVAL - (stop_tick % ticks_per_signal) * STOP_INTERVAL,
            )
            time.sleep(STOP_INTERVAL)

    except KeyboardInterrupt:
        log.info("Interrupted.\n%s", format_status(load_state()))
    finally:
        _release_pid()


def status() -> None:
    """Print current status and exit."""
    _setup_logger()
    state = load_state()
    print(format_status(state))
    s = performance_summary(state)
    print(f"Slots: {len(state['algos'])} | Open: {s['open_trades']} | Closed: {s['total_trades']}")


if __name__ == "__main__":
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "status":
        status()
    elif cmd == "run":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else None
        run(n)
    else:
        print("Usage: python3 -m options_bot.runner [run [N] | status]")
