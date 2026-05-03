"""
signal_trading/runner.py — Main event loop.

Two-frequency loop:
  Every STOP_INTERVAL  (60s):  fetch prices → check trailing stops / time limits
  Every SIGNAL_INTERVAL (900s): fetch OHLCV → detect regime → compute signals →
                                 filter → check risk → open/close trades

PID lockfile prevents duplicate instances.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from signal_trading.config import ASSETS, STATE_FILE, LOG_FILE, PID_FILE, RISK_CFG
from signal_trading.data import fetch_asset, fetch_all_prices
from signal_trading.signals import compute_all_signals
from signal_trading.regime import detect_regime, filter_signals
from signal_trading.risk import (
    update_portfolio_risk, check_can_open, record_trade_result, deduct_for_entry,
)
from signal_trading.execution import open_trade, close_trade, check_all_stops
from signal_trading.journal import (
    load_state, save_state, ensure_algos_initialised,
    get_algo_states, put_algo_states,
    get_open_trades, put_open_trades, add_completed_trade,
    format_status_report, performance_summary,
)
from signal_trading.models import (
    Trade, PortfolioRisk, Direction, ExitReason, Signal, RiskStage,
)
from signal_trading.safety import evaluate_safety, get_size_multiplier

STOP_INTERVAL = 60       # seconds between stop checks
SIGNAL_INTERVAL = 900    # seconds between signal cycles (15 min)
STATUS_EVERY_N = 5       # print full status every N signal cycles


# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("signal_trading")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ── PID guard ─────────────────────────────────────────────────────────────────

def _acquire_pid_lock(logger: logging.Logger) -> bool:
    pid = os.getpid()
    if os.path.exists(PID_FILE):
        try:
            old_pid = int(open(PID_FILE).read().strip())
            os.kill(old_pid, 0)   # raises if dead
            logger.error("Another instance running (PID %d). Delete %s to force restart.", old_pid, PID_FILE)
            return False
        except (ProcessLookupError, ValueError):
            pass  # dead process — take over
    with open(PID_FILE, "w") as f:
        f.write(str(pid))
    return True


def _release_pid_lock():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


# ── Core cycle logic ──────────────────────────────────────────────────────────

def _stop_check_cycle(
    state: dict,
    current_prices: Dict[str, float],
    logger: logging.Logger,
) -> dict:
    """Check stops on all open trades. Close those that hit exit conditions."""
    open_trades = get_open_trades(state)
    if not open_trades:
        return state

    to_close = check_all_stops(open_trades, current_prices, RISK_CFG)
    if not to_close:
        return state

    algos = get_algo_states(state)
    pr = PortfolioRisk.from_dict(state.get("portfolio_risk", {}))

    for trade, reason, exit_price in to_close:
        closed = close_trade(trade, exit_price, reason, RISK_CFG)
        key = closed.algo_key
        if key in algos:
            algo = algos[key]
            algo.active_trade_ids = [i for i in algo.active_trade_ids if i != closed.id]
            algos[key] = record_trade_result(algo, closed, RISK_CFG)

        open_trades = [t for t in open_trades if t.id != closed.id]
        state = add_completed_trade(state, closed)

    state = put_open_trades(state, open_trades)
    state = put_algo_states(state, algos)

    pr = update_portfolio_risk(pr, algos, open_trades, current_prices, RISK_CFG)
    state["portfolio_risk"] = pr.to_dict()

    # Update daily safety stage (keeps state current during stop cascades)
    evaluate_safety(state, algos, open_trades, current_prices, RISK_CFG)

    save_state(state)
    return state


def _signal_cycle(
    state: dict,
    current_prices: Dict[str, float],
    logger: logging.Logger,
) -> dict:
    """Full signal cycle: fetch OHLCV, detect regime, compute+filter signals, open trades."""
    algos = get_algo_states(state)
    pr = PortfolioRisk.from_dict(state.get("portfolio_risk", {}))
    open_trades = get_open_trades(state)

    # Evaluate daily safety stage before processing signals
    safety_stage = evaluate_safety(state, algos, open_trades, current_prices, RISK_CFG)

    for asset in ASSETS:
        price = current_prices.get(asset, 0)
        if price <= 0:
            logger.warning("No price for %s, skipping", asset)
            continue

        # Fetch OHLCV
        df = fetch_asset(asset, "1h")
        if df is None:
            logger.warning("No OHLCV data for %s, skipping", asset)
            continue

        # Regime detection (separate 4h fetch)
        snap = detect_regime(asset)
        state["last_regimes"][asset] = snap.to_dict()
        logger.info("REGIME %s: %s (ADX=%.1f)", asset, snap.regime.value, snap.adx)

        # Compute signals for all strategies on this asset
        raw_signals = compute_all_signals(asset, df, regime=snap.regime)

        # Filter by regime
        allowed_signals = filter_signals(raw_signals, snap)

        if not allowed_signals:
            logger.debug("No allowed signals for %s in %s regime", asset, snap.regime.value)
            continue

        # Process each allowed signal
        for signal in allowed_signals:
            # Check if we already have an open trade for this algo
            existing = [t for t in open_trades
                        if t.asset == asset and t.strategy == signal.strategy]

            if existing:
                # Close on opposite direction signal
                for trade in existing:
                    if ((trade.direction == Direction.LONG and signal.direction == Direction.SHORT) or
                            (trade.direction == Direction.SHORT and signal.direction == Direction.LONG)):
                        closed = close_trade(trade, price, ExitReason.OPPOSITE_SIGNAL, RISK_CFG)
                        key = closed.algo_key
                        if key in algos:
                            algo = algos[key]
                            algo.active_trade_ids = [i for i in algo.active_trade_ids if i != closed.id]
                            algos[key] = record_trade_result(algo, closed, RISK_CFG)
                        open_trades = [t for t in open_trades if t.id != closed.id]
                        state = add_completed_trade(state, closed)
                        logger.info("CLOSE on opposite signal: %s", closed.id)
                continue  # don't open another trade this cycle for same algo

            # Compute effective config (reduced size in REDUCED_RISK stage)
            multiplier = get_size_multiplier(safety_stage)
            if multiplier < 1.0:
                from dataclasses import replace
                effective_cfg = replace(RISK_CFG, trade_size_usd=RISK_CFG.trade_size_usd * multiplier)
            else:
                effective_cfg = RISK_CFG

            # Risk gate
            allowed, reason = check_can_open(
                asset, signal.strategy, algos, pr, open_trades, effective_cfg,
                safety_stage=safety_stage,
            )
            if not allowed:
                logger.debug("RISK_BLOCK %s %s: %s", signal.strategy, asset, reason)
                continue

            # Open trade
            trade = open_trade(signal, effective_cfg)
            key = trade.algo_key
            if key in algos:
                algos[key] = deduct_for_entry(algos[key], trade, RISK_CFG)
            open_trades.append(trade)

    # Persist
    state = put_open_trades(state, open_trades)
    state = put_algo_states(state, algos)
    pr = update_portfolio_risk(pr, algos, open_trades, current_prices, RISK_CFG)
    state["portfolio_risk"] = pr.to_dict()
    save_state(state)
    return state


# ── Main run loop ─────────────────────────────────────────────────────────────

def run(cycles: Optional[int] = None):
    logger = _setup_logger()

    if not _acquire_pid_lock(logger):
        sys.exit(1)

    logger.info("START signal_trading assets=%s pid=%d", list(ASSETS.keys()), os.getpid())

    try:
        state = load_state()
        state = ensure_algos_initialised(state)
        save_state(state)
        logger.info("INIT algos=%d", len(state["algos"]))

        stop_tick = 0
        signal_tick = 0
        ticks_per_signal = SIGNAL_INTERVAL // STOP_INTERVAL  # 15

        while True:
            # Fetch prices every tick (cheap — 3 API calls)
            current_prices = fetch_all_prices(list(ASSETS.keys()))
            if not current_prices:
                logger.warning("Price fetch failed, skipping tick")
                time.sleep(STOP_INTERVAL)
                continue

            for asset, p in current_prices.items():
                logger.debug("PRICE %s=$%.2f", asset, p)

            stop_tick += 1

            # Every tick: check stops
            state = _stop_check_cycle(state, current_prices, logger)

            # Every N ticks: full signal cycle
            if stop_tick % ticks_per_signal == 1:
                signal_tick += 1
                logger.info("SIGNAL_CYCLE tick=%d cycle=%d", stop_tick, signal_tick)
                state = _signal_cycle(state, current_prices, logger)

                if signal_tick % STATUS_EVERY_N == 0:
                    logger.info("\n%s", format_status_report(state, current_prices))

                if cycles is not None and signal_tick >= cycles:
                    logger.info("Completed %d cycles. Final report:", cycles)
                    logger.info("\n%s", format_status_report(state, current_prices))
                    break

            logger.info(
                "next stop in %ds | next signal in %ds",
                STOP_INTERVAL,
                SIGNAL_INTERVAL - (stop_tick % ticks_per_signal) * STOP_INTERVAL,
            )
            time.sleep(STOP_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Interrupted. Final state:")
        logger.info("\n%s", format_status_report(load_state(), {}))
    finally:
        _release_pid_lock()


def status():
    """Print current status without running the loop."""
    _setup_logger()
    state = load_state()
    prices = fetch_all_prices(list(ASSETS.keys()))
    print(format_status_report(state, prices))
    summary = performance_summary(state)
    print(f"\nAlgos: {len(state['algos'])} | Open trades: {summary['open_trades']}")


if __name__ == "__main__":
    # Allow running as: python3 signal_trading/runner.py
    # by adding the trader root to sys.path
    import sys, os as _os
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "status":
        status()
    elif cmd == "run":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else None
        run(n)
    else:
        print("Usage: runner.py [run [N_cycles] | status]")
