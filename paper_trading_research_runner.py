#!/usr/bin/env python3
"""
Paper Trading Research Runner — isolated parallel batch for day-trading research candidates.

Runs independently from the main runner (paper_trading_runner.py).
Uses its own state file, log file, and PID file so it never touches
the active production-like spot runtime.

Research candidates (hypotheses only — no guaranteed edge):
  orb@BTC          Opening Range Breakout
  orb@ETH          Opening Range Breakout
  vwap_trend@BTC   VWAP Reclaim / Reject Trend
  vwap_trend@ETH   VWAP Reclaim / Reject Trend
  break_retest@BTC Break-and-Retest Trend Pullback
  break_retest@ETH Break-and-Retest Trend Pullback

Each candidate has:
  - separate strategy name (orb / vwap_trend / break_retest)
  - separate algo key in its own state file (never merged with production state)
  - separate log (paper_trading_research.log)
  - independent enable/disable via RESEARCH_COMBOS below
"""

import json
import sys
import os
import time
import subprocess
import logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'shared_strategies', 'spot'))

STOP_CHECK_INTERVAL = 60
SIGNAL_CHECK_INTERVAL = 900
SIGNAL_TIMEFRAME = "15m"

ASSETS = ["BTC", "ETH"]
ASSET_SYMBOL = {
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
}

# ── Research batch — edit here to enable/disable individual candidates ──────
RESEARCH_COMBOS = {
    # ("orb",          "BTC"),   # CUT 2026-04-16: 13% WR, 23 trades, circuit-breaker tripped
    ("orb",          "ETH"),
    ("vwap_trend",   "BTC"),
    # ("vwap_trend",   "ETH"),   # CUT 2026-04-16: 22% WR, 0% in 2nd half, 9 trades
    ("break_retest", "BTC"),
    ("break_retest", "ETH"),
}

# ── Isolated paths — never overlap with the main runner ─────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_DIR, "paper_trading_research_state.json")
LOG_FILE   = os.path.join(_DIR, "paper_trading_research.log")
PID_FILE   = os.path.join(_DIR, "paper_trading_research_runner.pid")


def setup_logger():
    logger = logging.getLogger("research_trader")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


class ResearchRunner:
    def __init__(self):
        self.manager_script = os.path.join(_DIR, 'paper_trading_manager.py')
        self.check_script   = os.path.join(_DIR, 'shared_scripts', 'check_strategy.py')
        self.python         = os.path.join(_DIR, '.venv', 'bin', 'python3')
        self.logger         = setup_logger()

    def _mgr(self, *args) -> list:
        """Build a manager command targeting the research state file."""
        return [self.python, self.manager_script, '--state-file', STATE_FILE] + list(args)

    def run_command(self, cmd: list) -> dict:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.stdout.strip():
                return json.loads(result.stdout)
            return {"error": "No output", "stderr": result.stderr}
        except subprocess.TimeoutExpired:
            return {"error": "Command timeout (30s)"}
        except json.JSONDecodeError as e:
            return {"error": f"JSON decode error: {e}", "raw_output": result.stdout[:200]}
        except Exception as e:
            return {"error": str(e)}

    def is_allowed(self, strategy: str, asset: str) -> bool:
        return (strategy, asset) in RESEARCH_COMBOS

    def initialize_all(self):
        self.logger.info(
            "RESEARCH_INIT combos=%s",
            ", ".join(f"{s}@{a}" for s, a in sorted(RESEARCH_COMBOS))
        )
        count = 0
        for strategy, asset in sorted(RESEARCH_COMBOS):
            result = self.run_command(self._mgr('init_single', strategy, asset))
            if result.get('success'):
                count += 1
        status = self.run_command(self._mgr('status'))
        cfg = status.get('config', {})
        self.logger.info(
            "RESEARCH_INIT success count=%d timeframe=%s trailing=%s%% max_hold=%sh",
            count, SIGNAL_TIMEFRAME,
            cfg.get('trailing_stop_pct'), cfg.get('max_hold_time_hours')
        )
        return count

    def fetch_prices(self, assets: list) -> dict:
        prices = {}
        for asset in assets:
            symbol = ASSET_SYMBOL.get(asset, f"{asset}/USDT")
            result = self.run_command(
                [self.python, self.check_script, "rsi", symbol, SIGNAL_TIMEFRAME]
            )
            price = result.get("price", 0)
            if price > 0:
                prices[asset] = price
        return prices

    def check_trailing_stops(self, current_prices: dict) -> list:
        if not current_prices:
            return []
        result = self.run_command(self._mgr('check_stops', json.dumps(current_prices)))
        actions = result.get("actions", [])
        for action in actions:
            self.logger.info(
                "STOP_EXIT trade_id=%s algo=%s asset=%s reason='%s' net_pnl=$%.2f pnl_pct=%.2f%% hold=%sm",
                action["trade_id"], action["algo"], action["asset"], action["reason"],
                action.get("net_pnl", 0), action.get("pnl_pct", 0), action.get("hold_minutes", 0),
            )
        if actions:
            self.logger.info("STOP_SUMMARY exits=%d", len(actions))
        return actions

    def get_active_trades(self) -> list:
        status = self.run_command(self._mgr('status'))
        return status.get("active_trades", [])

    def process_signal(self, strategy: str, asset: str, signal_data: dict, active_trades: list):
        signal = signal_data.get("signal", "hold")
        price  = signal_data.get("price", 0)

        if isinstance(signal, int):
            signal = {1: "buy", -1: "sell"}.get(signal, "hold")
        if isinstance(signal, str):
            signal = signal.lower()

        if signal == "hold" or price == 0:
            return

        algo_key = f"{strategy} ({asset})"
        existing = [t for t in active_trades if t.get("algo") == algo_key]

        if signal in ("buy", "sell") and not existing:
            risk_result = self.run_command(self._mgr('check_risk', strategy, asset))
            if not risk_result.get("allowed", True):
                self.logger.info(
                    "RISK_BLOCK strategy=%s asset=%s reason='%s'",
                    strategy, asset, risk_result.get("reason")
                )
                return
            reason = signal_data.get("reason", "Signal triggered")
            result = self.run_command(
                self._mgr('open', strategy, asset, signal, str(price), reason)
            )
            if result.get("success"):
                trade = result.get("trade", {})
                self.logger.info(
                    "TRADE_OPEN strategy=%s asset=%s signal=%s price=%.2f size=$%s trade_id=%s",
                    strategy, asset, signal, price, trade.get("size"), trade.get("id")
                )
            else:
                self.logger.warning(
                    "TRADE_FAIL strategy=%s asset=%s reason='%s'",
                    strategy, asset, result.get("reason")
                )
        elif existing:
            for trade in existing:
                trade_id     = trade.get("id")
                entry_signal = trade.get("signal", "")
                should_close = (
                    (entry_signal == "buy"  and signal == "sell") or
                    (entry_signal == "sell" and signal == "buy")
                )
                if should_close:
                    result = self.run_command(
                        self._mgr('close', trade_id, str(price), f"Opposite signal: {signal}")
                    )
                    if result.get("success"):
                        self.logger.info(
                            "TRADE_CLOSE trade_id=%s exit_price=%.2f net_pnl=$%.2f pnl_pct=%.2f%% hold=%sm reason='opposite signal'",
                            trade_id, price,
                            result.get("net_pnl", 0), result.get("pnl_pct", 0), result.get("hold_minutes", 0),
                        )

    def run_cycle(self):
        self.logger.info("=" * 70)
        self.logger.info("RESEARCH_CYCLE_START")

        current_prices = self.fetch_prices(ASSETS)
        for asset, price in current_prices.items():
            self.logger.info("PRICE %s=$%.2f", asset, price)

        if not current_prices:
            self.logger.error("RESEARCH_CYCLE_ABORT no prices fetched")
            return

        self.check_trailing_stops(current_prices)
        active_trades = self.get_active_trades()

        strategies = sorted({s for s, _ in RESEARCH_COMBOS})
        for strategy in strategies:
            for asset in ASSETS:
                if not self.is_allowed(strategy, asset):
                    continue
                symbol = ASSET_SYMBOL.get(asset, f"{asset}/USDT")
                signal_data = self.run_command(
                    [self.python, self.check_script, strategy, symbol, SIGNAL_TIMEFRAME]
                )
                if "error" in signal_data:
                    self.logger.warning(
                        "SIGNAL_ERROR strategy=%s asset=%s error='%s'",
                        strategy, asset, signal_data["error"]
                    )
                    continue
                signal     = signal_data.get("signal", "hold")
                price      = signal_data.get("price", 0)
                signal_str = {1: "BUY", -1: "SELL"}.get(signal, str(signal).upper()) if isinstance(signal, int) else str(signal).upper()
                if signal_str != "HOLD":
                    self.logger.info(
                        "SIGNAL strategy=%s asset=%s signal=%s price=%.2f",
                        strategy, asset, signal_str, price
                    )
                self.process_signal(strategy, asset, signal_data, active_trades)
                time.sleep(0.5)

        self.logger.info("RESEARCH_CYCLE_END")

    def print_status(self, current_prices: dict = None):
        prices_arg = json.dumps(current_prices) if current_prices else "{}"
        status = self.run_command(self._mgr('status', prices_arg))
        if "error" in status:
            return
        summary = status.get("summary", {})
        self.logger.info(
            "RESEARCH_STATUS Portfolio: %s | Net P&L: %s | Trades: %s | Win Rate: %s",
            summary.get("total_portfolio_value", "N/A"),
            summary.get("total_net_pnl", "N/A"),
            summary.get("total_completed_trades", 0),
            summary.get("overall_win_rate", "N/A"),
        )
        for algo in status.get("algos", []):
            self.logger.info(
                "  %-35s | Cap: %10s | P&L: %10s | W/L: %s",
                algo['name'], algo['capital'], algo['profit'], algo['win_rate'],
            )

    def run(self, cycles: int = None):
        pid = os.getpid()
        if os.path.exists(PID_FILE):
            try:
                old_pid = int(open(PID_FILE).read().strip())
                os.kill(old_pid, 0)
                self.logger.error(
                    "ABORT: research runner already running (PID %d). Kill it first or delete %s",
                    old_pid, PID_FILE
                )
                return
            except (ProcessLookupError, ValueError):
                pass
        with open(PID_FILE, 'w') as f:
            f.write(str(pid))

        self.logger.info("RESEARCH_START state=%s pid=%d", STATE_FILE, pid)
        self.logger.info(
            "RESEARCH_COMBOS %s",
            ", ".join(f"{s}@{a}" for s, a in sorted(RESEARCH_COMBOS))
        )

        self.initialize_all()
        time.sleep(2)

        stop_tick    = 0
        signal_tick  = 0
        report_every = SIGNAL_CHECK_INTERVAL // STOP_CHECK_INTERVAL

        try:
            while True:
                current_prices = self.fetch_prices(ASSETS)
                if not current_prices:
                    self.logger.warning("RESEARCH price fetch failed, skipping tick")
                    time.sleep(STOP_CHECK_INTERVAL)
                    continue

                stop_tick += 1
                self.check_trailing_stops(current_prices)

                if stop_tick % report_every == 1:
                    signal_tick += 1
                    self.logger.info(
                        "RESEARCH_SIGNAL_CYCLE tick=%d signal_cycle=%d",
                        stop_tick, signal_tick
                    )
                    self.run_cycle()
                    self.print_status(current_prices)
                    if cycles and signal_tick >= cycles:
                        self.logger.info("Completed %d research signal cycles.", cycles)
                        break

                self.logger.info(
                    "RESEARCH next stop-check in %ds | next signal in %ds",
                    STOP_CHECK_INTERVAL,
                    SIGNAL_CHECK_INTERVAL - ((stop_tick % report_every) * STOP_CHECK_INTERVAL),
                )
                time.sleep(STOP_CHECK_INTERVAL)
        except KeyboardInterrupt:
            self.logger.info("RESEARCH interrupted.")
        finally:
            try:
                os.remove(PID_FILE)
            except OSError:
                pass


def main():
    runner = ResearchRunner()
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            prices = runner.fetch_prices(ASSETS)
            runner.print_status(prices)
        elif cmd == "run":
            cycles = int(sys.argv[2]) if len(sys.argv) > 2 else None
            runner.run(cycles)
        else:
            print("Usage: paper_trading_research_runner.py [status|run [cycles]]")
    else:
        runner.run()


if __name__ == "__main__":
    main()
