#!/usr/bin/env python3
"""
Paper Trading Runner V3 — Automated execution with:
- Trailing stop checks EVERY cycle (wired to manager)
- Max hold enforcement
- Risk checks before opening trades
- Structured logging
- Efficient single-fetch price cycle
"""

import json
import sys
import os
import time
import subprocess
import logging
from datetime import datetime, timezone

# Add paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'shared_strategies', 'spot'))
from strategies import list_strategies as list_spot_strategies

STOP_CHECK_INTERVAL = 60
SIGNAL_CHECK_INTERVAL = 900
SIGNAL_TIMEFRAME = "15m"

# Narrowed runtime universe.
ASSETS = ["BTC", "ETH"]

ASSET_SYMBOL = {
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
}

# Active runtime combinations for the next live spot test.
ALLOWED_RUNTIME_COMBOS = {
    ("pairs_spread", "ETH"),
}
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading_runner.pid")


def setup_logger():
    logger = logging.getLogger("paper_trader")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')

    fh = logging.FileHandler(os.path.join(os.path.dirname(__file__), "paper_trading_v3.log"))
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


class PaperTradingRunner:
    def __init__(self):
        self.manager_script = os.path.join(os.path.dirname(__file__), 'paper_trading_manager.py')
        self.check_script = os.path.join(os.path.dirname(__file__), 'shared_scripts', 'check_strategy.py')
        self.python = os.path.join(os.path.dirname(__file__), '.venv', 'bin', 'python3')
        self.logger = setup_logger()

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

    def runtime_strategies(self) -> list:
        return [s for s in list_spot_strategies() if any(combo[0] == s for combo in ALLOWED_RUNTIME_COMBOS)]

    def is_allowed_combo(self, strategy: str, asset: str) -> bool:
        return (strategy, asset) in ALLOWED_RUNTIME_COMBOS

    def initialize_all_algos(self):
        self.logger.info("INIT initializing runtime combinations: %s", ", ".join(f"{s}@{a}" for s, a in sorted(ALLOWED_RUNTIME_COMBOS)))
        initialized = 0
        for strategy, asset in sorted(ALLOWED_RUNTIME_COMBOS):
            result = self.run_command([self.python, self.manager_script, 'init_single', strategy, asset])
            if result.get('success'):
                initialized += 1
        status = self.run_command([self.python, self.manager_script, 'status'])
        cfg = status.get('config', {})
        self.logger.info("INIT success count=%d timeframe=%s trailing=%s%% max_hold=%sh", initialized, SIGNAL_TIMEFRAME, cfg.get('trailing_stop_pct'), cfg.get('max_hold_time_hours'))
        return {"success": True, "initialized": initialized}

    def fetch_prices(self, assets: list) -> dict:
        prices = {}
        for asset in assets:
            symbol = ASSET_SYMBOL.get(asset, f"{asset}/USDT")
            result = self.run_command([self.python, self.check_script, "rsi", symbol, SIGNAL_TIMEFRAME])
            price = result.get("price", 0)
            if price > 0:
                prices[asset] = price
        return prices

    def check_strategy_signal(self, strategy: str, asset: str, timeframe: str = SIGNAL_TIMEFRAME, symbol: str = None) -> dict:
        if symbol is None:
            symbol = ASSET_SYMBOL.get(asset, f"{asset}/USDT")
        return self.run_command([self.python, self.check_script, strategy, symbol, timeframe])

    def check_trailing_stops(self, current_prices: dict) -> list:
        if not current_prices:
            return []
        prices_json = json.dumps(current_prices)
        result = self.run_command([self.python, self.manager_script, 'check_stops', prices_json])
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
        status = self.run_command([self.python, self.manager_script, 'status'])
        return status.get("active_trades", [])

    def process_signal(self, strategy: str, asset: str, signal_data: dict, active_trades: list):
        signal = signal_data.get("signal", "hold")
        price = signal_data.get("price", 0)

        if isinstance(signal, int):
            signal = {1: "buy", -1: "sell"}.get(signal, "hold")
        if isinstance(signal, str):
            signal = signal.lower()

        if signal == "hold" or price == 0:
            return

        algo_key = f"{strategy} ({asset})"
        existing = [t for t in active_trades if t.get("algo") == algo_key]

        if signal in ("buy", "sell") and not existing:
            risk_result = self.run_command([self.python, self.manager_script, 'check_risk', strategy, asset])
            if not risk_result.get("allowed", True):
                self.logger.info("RISK_BLOCK strategy=%s asset=%s reason='%s'", strategy, asset, risk_result.get("reason"))
                return

            reason = signal_data.get("reason", "Signal triggered")
            result = self.run_command([
                self.python, self.manager_script, 'open', strategy, asset, signal, str(price), reason
            ])
            if result.get("success"):
                trade = result.get("trade", {})
                self.logger.info("TRADE_OPEN strategy=%s asset=%s signal=%s price=%.2f size=$%s trade_id=%s", strategy, asset, signal, price, trade.get("size"), trade.get("id"))
            else:
                self.logger.warning("TRADE_FAIL strategy=%s asset=%s reason='%s'", strategy, asset, result.get("reason"))

        elif existing:
            for trade in existing:
                trade_id = trade.get("id")
                entry_signal = trade.get("signal", "")
                should_close = (
                    (entry_signal == "buy" and signal == "sell") or
                    (entry_signal == "sell" and signal == "buy")
                )
                if should_close:
                    result = self.run_command([
                        self.python, self.manager_script, 'close', trade_id, str(price), f"Opposite signal: {signal}"
                    ])
                    if result.get("success"):
                        self.logger.info(
                            "TRADE_CLOSE trade_id=%s exit_price=%.2f net_pnl=$%.2f pnl_pct=%.2f%% hold=%sm reason='opposite signal'",
                            trade_id, price, result.get("net_pnl", 0), result.get("pnl_pct", 0), result.get("hold_minutes", 0),
                        )

    def run_cycle(self):
        self.logger.info("=" * 70)
        self.logger.info("CYCLE_START")

        strategies = self.runtime_strategies()
        current_prices = self.fetch_prices(ASSETS)
        for asset, price in current_prices.items():
            self.logger.info("PRICE %s=$%.2f", asset, price)

        if not current_prices:
            self.logger.error("CYCLE_ABORT no prices fetched")
            return

        self.check_trailing_stops(current_prices)
        active_trades = self.get_active_trades()

        for strategy in strategies:
            for asset in ASSETS:
                if not self.is_allowed_combo(strategy, asset):
                    continue
                symbol = ASSET_SYMBOL.get(asset, f"{asset}/USDT")
                signal_data = self.check_strategy_signal(strategy, asset, symbol=symbol)
                if "error" in signal_data:
                    self.logger.warning("SIGNAL_ERROR strategy=%s asset=%s error='%s'", strategy, asset, signal_data["error"])
                    continue

                signal = signal_data.get("signal", "hold")
                price = signal_data.get("price", 0)
                signal_str = {1: "BUY", -1: "SELL"}.get(signal, str(signal).upper()) if isinstance(signal, int) else str(signal).upper()
                if signal_str != "HOLD":
                    self.logger.info("SIGNAL strategy=%s asset=%s signal=%s price=%.2f", strategy, asset, signal_str, price)
                self.process_signal(strategy, asset, signal_data, active_trades)
                time.sleep(0.5)

        self.logger.info("CYCLE_END")

    def print_status(self, current_prices: dict = None):
        prices_arg = json.dumps(current_prices) if current_prices else "{}"
        status = self.run_command([self.python, self.manager_script, 'status', prices_arg])
        if "error" in status:
            self.logger.error("Status error: %s", status['error'])
            return

        self.logger.info("=" * 70)
        self.logger.info("STATUS UPDATE")
        self.logger.info("-" * 70)
        summary = status.get("summary", {})
        self.logger.info(
            "Portfolio: %s | Net P&L: %s | Fees: %s | Trades: %s | Win Rate: %s | Avg Hold: %s",
            summary.get("total_portfolio_value", "N/A"),
            summary.get("total_net_pnl", "N/A"),
            summary.get("total_fees_paid", "N/A"),
            summary.get("total_completed_trades", 0),
            summary.get("overall_win_rate", "N/A"),
            summary.get("avg_hold_minutes", "N/A"),
        )

        self.logger.info("-" * 70)
        self.logger.info("ALGO PERFORMANCE:")
        for algo in status.get("algos", []):
            self.logger.info(
                "  %-30s | Cap: %10s | P&L: %10s (%7s) | W/L: %d/%d (%s) | Fees: %s%s",
                algo['name'], algo['capital'], algo['profit'], algo['profit_pct'],
                algo['trades'] - (algo['trades'] - int(float(algo['win_rate'].rstrip('%')) / 100 * algo['trades'])) if algo['trades'] > 0 else 0,
                algo['trades'] - int(float(algo['win_rate'].rstrip('%')) / 100 * algo['trades']) if algo['trades'] > 0 else 0,
                algo['win_rate'], algo.get('fees_paid', '$0.00'),
                " [CIRCUIT BREAKER]" if algo.get("circuit_breaker") else "",
            )

        active = status.get("active_trades", [])
        self.logger.info("-" * 70)
        self.logger.info("ACTIVE TRADES (%d):", len(active))
        for t in active:
            self.logger.info(
                "  %-30s | %4s @ %10s | Hold: %5s | Peak: %10s | Unrealized: %s %s",
                t['id'], t['signal'].upper(), t['entry_price'], t.get('hold_minutes', '?'), t.get('highest_price', 'N/A'),
                t.get("unrealized_pnl", "N/A"), t.get("unrealized_pnl_pct", ""),
            )

        completed = status.get("recent_completed", [])
        if completed:
            self.logger.info("-" * 70)
            self.logger.info("RECENT COMPLETED (%d):", len(completed))
            for t in completed:
                self.logger.info(
                    "  %-30s | P&L: %8s (%7s) | Fees: %s | Hold: %4s | %s",
                    t['id'], t['net_pnl'], t['pnl_pct'], t['fees'], t['hold_minutes'], t['exit_reason']
                )

    def print_report(self):
        report = self.run_command([self.python, self.manager_script, 'report'])
        if "error" in report:
            self.logger.error("Report error: %s", report['error'])
            return
        self.logger.info("=" * 70)
        self.logger.info("PERFORMANCE REPORT")
        self.logger.info(
            "Config: trade=$%s timeframe=%s trailing=%s%% max_hold=%sh stop=%s%% target=%s%% fee=%s%%",
            report.get("config", {}).get("trade_size"),
            SIGNAL_TIMEFRAME,
            report.get("config", {}).get("trailing_stop_pct"),
            report.get("config", {}).get("max_hold_time_hours"),
            report.get("config", {}).get("stop_loss_pct"),
            report.get("config", {}).get("take_profit_pct"),
            float(report.get("config", {}).get("fee_pct", 0)) * 100,
        )

    def run(self, cycles: int = None):
        pid = os.getpid()
        if os.path.exists(PID_FILE):
            try:
                old_pid = int(open(PID_FILE).read().strip())
                os.kill(old_pid, 0)
                self.logger.error("ABORT: another instance is running (PID %d). Kill it first or delete %s", old_pid, PID_FILE)
                return
            except (ProcessLookupError, ValueError):
                pass
        with open(PID_FILE, 'w') as f:
            f.write(str(pid))
        self.logger.info("START assets=%s pid=%d", ASSETS, pid)
        self.logger.info("RUNTIME_COMBOS %s", ", ".join(f"{s}@{a}" for s, a in sorted(ALLOWED_RUNTIME_COMBOS)))

        self.initialize_all_algos()
        time.sleep(2)

        stop_tick = 0
        signal_tick = 0
        report_tick = 0

        try:
            while True:
                current_prices = self.fetch_prices(ASSETS)
                if not current_prices:
                    self.logger.warning("Price fetch failed, skipping tick")
                    time.sleep(STOP_CHECK_INTERVAL)
                    continue

                stop_tick += 1
                exits = self.check_trailing_stops(current_prices)
                if exits:
                    self.logger.info("TICK_STOPS tick=%d exits=%d", stop_tick, len(exits))

                if stop_tick % (SIGNAL_CHECK_INTERVAL // STOP_CHECK_INTERVAL) == 1:
                    signal_tick += 1
                    self.logger.info("SIGNAL_CYCLE tick=%d signal_cycle=%d", stop_tick, signal_tick)
                    self.run_cycle()
                    self.print_status(current_prices)
                    report_tick += 1
                    if report_tick % 5 == 0:
                        self.print_report()
                    if cycles and signal_tick >= cycles:
                        self.logger.info("Completed %d signal cycles. Final report:", cycles)
                        self.print_report()
                        break

                self.logger.info(
                    "Next stop-check in %ds | Next signal in %ds",
                    STOP_CHECK_INTERVAL,
                    SIGNAL_CHECK_INTERVAL - ((stop_tick % (SIGNAL_CHECK_INTERVAL // STOP_CHECK_INTERVAL)) * STOP_CHECK_INTERVAL),
                )
                time.sleep(STOP_CHECK_INTERVAL)
        except KeyboardInterrupt:
            self.logger.info("Interrupted. Final report:")
            self.print_report()
        finally:
            try:
                os.remove(PID_FILE)
            except OSError:
                pass


def main():
    runner = PaperTradingRunner()
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            prices = runner.fetch_prices(ASSETS)
            runner.print_status(prices)
        elif cmd == "report":
            runner.print_report()
        elif cmd == "run":
            cycles = int(sys.argv[2]) if len(sys.argv) > 2 else None
            runner.run(cycles)
        else:
            print("Usage: paper_trading_runner.py [status|report|run [cycles]]")
    else:
        runner.run()


if __name__ == "__main__":
    main()
