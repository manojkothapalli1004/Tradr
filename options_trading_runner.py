#!/usr/bin/env python3
"""
Options Trading Runner — Automated options paper trading with:
- IV rank gating (only sell premium when DVOL IVR > 50)
- Weekend strangle on ETH (Friday 16:00 UTC entry, 2 DTE)
- Short strangle on BTC (14 DTE, 15-delta strikes)
- Iron condor on BTC/ETH (14 DTE, defined risk)
- Exit management: 50% profit, 200% loss, <7 DTE, IV crush
- 5-minute position check cycle, 30-minute signal cycle
"""

import json
import sys
import os
import time
import subprocess
import logging
import atexit
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, 'platforms', 'deribit'))

POSITION_CHECK_INTERVAL = 300   # 5 min — check exits
SIGNAL_CHECK_INTERVAL = 1800    # 30 min — check new entries
WEEKEND_CHECK_HOUR = 16         # UTC hour for weekend strangle entry (Friday)
PID_FILE = os.path.join(_THIS_DIR, "options_trading_runner.pid")

# Strategies to run and their configs
STRATEGIES = {
    "weekend_strangle": {
        "type": "strangle",
        "underlying": "ETH",
        "dte": 2,
        "otm_pct": 0.08,       # 8% OTM (~0.35 delta)
        "side": "sell",
        "min_iv_rank": 0,       # always enter (weekend premium is the edge)
        "weekend_only": True,
        "enabled": False,       # disabled by experiment options-theta-thesis-2026-05-02
    },
    "short_strangle_btc": {
        "type": "strangle",
        "underlying": "BTC",
        "dte": 14,
        "otm_pct": 0.12,       # 12% OTM (~15 delta)
        "side": "sell",
        "min_iv_rank": 70,      # raised from 50 by experiment options-theta-thesis-2026-05-02
        "weekend_only": False,
        "enabled": True,
    },
    "short_strangle_eth": {
        "type": "strangle",
        "underlying": "ETH",
        "dte": 14,
        "otm_pct": 0.10,       # 10% OTM
        "side": "sell",
        "min_iv_rank": 50,
        "weekend_only": False,
        "enabled": False,       # disabled by experiment options-theta-thesis-2026-05-02
    },
    "iron_condor_btc": {
        "type": "iron_condor",
        "underlying": "BTC",
        "dte": 14,
        "short_otm_pct": 0.12,
        "wing_width_pct": 0.05,
        "min_iv_rank": 50,
        "weekend_only": False,
        "enabled": False,       # disabled by experiment options-theta-thesis-2026-05-02
    },
}


def setup_logger():
    logger = logging.getLogger("options_trader")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s',
                            datefmt='%Y-%m-%dT%H:%M:%S')
    fh = logging.FileHandler(os.path.join(_THIS_DIR, "options_trading.log"))
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


class OptionsRunner:
    def __init__(self):
        self.manager_script = os.path.join(_THIS_DIR, 'options_trading_manager.py')
        self.python = os.path.join(_THIS_DIR, '.venv', 'bin', 'python3')
        self.logger = setup_logger()
        self.adapter = None

    def _init_adapter(self):
        """Lazy-init the Deribit adapter for price/IV lookups."""
        if self.adapter is None:
            try:
                from adapter import DeribitOptionsAdapter
                self.adapter = DeribitOptionsAdapter(sandbox=True)
                self.logger.info("INIT Deribit adapter initialized (sandbox)")
            except Exception as e:
                self.logger.error("INIT adapter failed: %s", e)
                # Fallback: use check_options.py subprocess
                self.adapter = None

    def run_manager(self, cmd: list) -> dict:
        """Call options_trading_manager.py via subprocess."""
        full_cmd = [self.python, self.manager_script] + cmd
        try:
            result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=30)
            if result.stdout.strip():
                return json.loads(result.stdout)
            return {"error": "No output", "stderr": result.stderr}
        except subprocess.TimeoutExpired:
            return {"error": "Timeout"}
        except json.JSONDecodeError:
            return {"error": "Invalid JSON", "raw": result.stdout[:200]}

    def get_iv_rank(self, underlying: str) -> float:
        """Get IV rank from adapter or fallback."""
        self._init_adapter()
        if self.adapter:
            try:
                return self.adapter.get_iv_rank(underlying)
            except Exception as e:
                self.logger.warning("IV rank fetch failed for %s: %s", underlying, e)
        return 50.0  # neutral fallback

    def get_spot_price(self, underlying: str) -> float:
        """Get spot price."""
        self._init_adapter()
        if self.adapter:
            try:
                return self.adapter.get_spot_price(underlying)
            except Exception:
                pass
        return 0.0

    def get_option_premium(self, underlying: str, option_type: str,
                           strike: float, dte: float) -> Dict[str, float]:
        """Get estimated premium for an option."""
        self._init_adapter()
        if self.adapter:
            try:
                from adapter import bs_price, bs_greeks
                spot = self.adapter.get_spot_price(underlying)
                atm_iv = self.adapter.get_atm_iv(underlying, dte_target=dte)
                t = dte / 365.0
                r = 0.05
                price = bs_price(spot, strike, t, r, atm_iv, option_type)
                greeks = bs_greeks(spot, strike, t, r, atm_iv, option_type)
                return {
                    "premium_usd": round(price, 2),
                    "premium_btc": round(price / spot, 8) if spot > 0 else 0,
                    "delta": round(greeks.delta, 4),
                    "gamma": round(greeks.gamma, 6),
                    "theta": round(greeks.theta, 4),
                    "vega": round(greeks.vega, 4),
                    "iv": round(atm_iv, 4),
                }
            except Exception as e:
                self.logger.warning("Premium calc failed: %s", e)
        return {"premium_usd": 0, "premium_btc": 0, "delta": 0, "gamma": 0, "theta": 0, "vega": 0, "iv": 0}

    def is_weekend_entry_time(self) -> bool:
        """Check if it's Friday ~16:00 UTC (weekend strangle entry window)."""
        now = datetime.now(timezone.utc)
        return now.weekday() == 4 and abs(now.hour - WEEKEND_CHECK_HOUR) <= 1

    def has_position_for(self, strategy: str, underlying: str) -> bool:
        """Check if we already have an open position for this strategy+underlying."""
        status = self.run_manager(["status"])
        positions = status.get("positions", [])
        for pos in positions:
            if pos.get("strategy") == strategy and pos.get("underlying") == underlying:
                return True
        return False

    def try_open_strangle(self, strategy_name: str, config: dict) -> Optional[dict]:
        """Attempt to open a strangle position."""
        underlying = config["underlying"]
        dte = config["dte"]
        otm_pct = config["otm_pct"]

        # Check if already have position
        if self.has_position_for(strategy_name, underlying):
            return None

        # IV rank check
        iv_rank = self.get_iv_rank(underlying)
        min_iv = config.get("min_iv_rank", 50)
        if iv_rank < min_iv:
            self.logger.info("SKIP %s: IV rank %.0f < %.0f", strategy_name, iv_rank, min_iv)
            return None

        spot = self.get_spot_price(underlying)
        if spot <= 0:
            return None

        call_strike = round(spot * (1 + otm_pct), -1 if underlying == "BTC" else 0)
        put_strike = round(spot * (1 - otm_pct), -1 if underlying == "BTC" else 0)
        expiry_date = (datetime.now(timezone.utc) + timedelta(days=dte)).strftime("%Y-%m-%d")

        # Get premiums
        call_premium = self.get_option_premium(underlying, "call", call_strike, dte)
        put_premium = self.get_option_premium(underlying, "put", put_strike, dte)

        total_credit = call_premium["premium_usd"] + put_premium["premium_usd"]
        if total_credit <= 0:
            self.logger.warning("SKIP %s: zero premium", strategy_name)
            return None

        legs = [
            {
                "type": "sell_call",
                "strike": call_strike,
                "expiry": expiry_date,
                "premium_usd": call_premium["premium_usd"],
                "premium_btc": call_premium["premium_btc"],
                "delta": call_premium["delta"],
                "theta": call_premium["theta"],
                "vega": call_premium["vega"],
                "iv": call_premium["iv"],
                "spot_price": spot,
                "quantity": 0.1 if underlying == "BTC" else 1.0,
            },
            {
                "type": "sell_put",
                "strike": put_strike,
                "expiry": expiry_date,
                "premium_usd": put_premium["premium_usd"],
                "premium_btc": put_premium["premium_btc"],
                "delta": put_premium["delta"],
                "theta": put_premium["theta"],
                "vega": put_premium["vega"],
                "iv": put_premium["iv"],
                "spot_price": spot,
                "quantity": 0.1 if underlying == "BTC" else 1.0,
            },
        ]

        self.logger.info("OPEN %s %s: call@%.0f put@%.0f credit=$%.2f IVR=%.0f",
                         strategy_name, underlying, call_strike, put_strike, total_credit, iv_rank)

        result = self.run_manager([
            "open", strategy_name, underlying, "strangle",
            json.dumps(legs), str(iv_rank), str(total_credit)
        ])

        if result.get("success"):
            self.logger.info("OPENED %s: %s", strategy_name, result.get("position", {}).get("id", ""))
        else:
            self.logger.warning("OPEN_FAILED %s: %s", strategy_name, result.get("reason", "unknown"))

        return result

    def try_open_iron_condor(self, strategy_name: str, config: dict) -> Optional[dict]:
        """Attempt to open an iron condor position."""
        underlying = config["underlying"]
        dte = config["dte"]
        short_otm = config["short_otm_pct"]
        wing_width = config["wing_width_pct"]

        if self.has_position_for(strategy_name, underlying):
            return None

        iv_rank = self.get_iv_rank(underlying)
        min_iv = config.get("min_iv_rank", 50)
        if iv_rank < min_iv:
            self.logger.info("SKIP %s: IV rank %.0f < %.0f", strategy_name, iv_rank, min_iv)
            return None

        spot = self.get_spot_price(underlying)
        if spot <= 0:
            return None

        short_call = round(spot * (1 + short_otm), -1 if underlying == "BTC" else 0)
        short_put = round(spot * (1 - short_otm), -1 if underlying == "BTC" else 0)
        long_call = round(spot * (1 + short_otm + wing_width), -1 if underlying == "BTC" else 0)
        long_put = round(spot * (1 - short_otm - wing_width), -1 if underlying == "BTC" else 0)
        expiry_date = (datetime.now(timezone.utc) + timedelta(days=dte)).strftime("%Y-%m-%d")

        # Get premiums (short legs collect, long legs cost)
        sc = self.get_option_premium(underlying, "call", short_call, dte)
        sp = self.get_option_premium(underlying, "put", short_put, dte)
        lc = self.get_option_premium(underlying, "call", long_call, dte)
        lp = self.get_option_premium(underlying, "put", long_put, dte)

        total_credit = (sc["premium_usd"] + sp["premium_usd"]) - (lc["premium_usd"] + lp["premium_usd"])
        if total_credit <= 0:
            self.logger.warning("SKIP %s: negative credit $%.2f", strategy_name, total_credit)
            return None

        qty = 0.1 if underlying == "BTC" else 1.0
        legs = [
            {"type": "sell_call", "strike": short_call, "expiry": expiry_date,
             "premium_usd": sc["premium_usd"], "premium_btc": sc["premium_btc"],
             "delta": sc["delta"], "theta": sc["theta"], "vega": sc["vega"],
             "iv": sc["iv"], "spot_price": spot, "quantity": qty},
            {"type": "sell_put", "strike": short_put, "expiry": expiry_date,
             "premium_usd": sp["premium_usd"], "premium_btc": sp["premium_btc"],
             "delta": sp["delta"], "theta": sp["theta"], "vega": sp["vega"],
             "iv": sp["iv"], "spot_price": spot, "quantity": qty},
            {"type": "buy_call", "strike": long_call, "expiry": expiry_date,
             "premium_usd": lc["premium_usd"], "premium_btc": lc["premium_btc"],
             "delta": lc["delta"], "theta": lc["theta"], "vega": lc["vega"],
             "iv": lc["iv"], "spot_price": spot, "quantity": qty},
            {"type": "buy_put", "strike": long_put, "expiry": expiry_date,
             "premium_usd": lp["premium_usd"], "premium_btc": lp["premium_btc"],
             "delta": lp["delta"], "theta": lp["theta"], "vega": lp["vega"],
             "iv": lp["iv"], "spot_price": spot, "quantity": qty},
        ]

        self.logger.info("OPEN %s %s: SC@%.0f SP@%.0f LC@%.0f LP@%.0f credit=$%.2f IVR=%.0f",
                         strategy_name, underlying, short_call, short_put,
                         long_call, long_put, total_credit, iv_rank)

        result = self.run_manager([
            "open", strategy_name, underlying, "iron_condor",
            json.dumps(legs), str(iv_rank), str(total_credit)
        ])

        if result.get("success"):
            self.logger.info("OPENED %s: %s", strategy_name, result.get("position", {}).get("id", ""))
        else:
            self.logger.warning("OPEN_FAILED %s: %s", strategy_name, result.get("reason", "unknown"))

        return result

    def update_position_values(self):
        """Update current market values for all open positions."""
        status = self.run_manager(["status"])
        positions = status.get("positions", [])

        for pos in positions:
            underlying = pos.get("underlying", "")
            spot = self.get_spot_price(underlying)
            if spot <= 0:
                continue

            # Recalculate current value of each leg
            total_value = 0
            for leg in pos.get("legs", []):
                strike = leg.get("strike", 0)
                expiry = leg.get("expiry", "")
                leg_type = leg.get("type", "")

                try:
                    exp_dt = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    dte = max((exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400, 0.01)
                except (ValueError, TypeError):
                    dte = 1

                opt_type = "call" if "call" in leg_type else "put"
                premium = self.get_option_premium(underlying, opt_type, strike, dte)

                if "sell" in leg_type:
                    total_value += premium["premium_usd"]  # cost to close short
                else:
                    total_value -= premium["premium_usd"]  # value of long

                leg["current_premium_usd"] = premium["premium_usd"]
                leg["current_premium_btc"] = premium["premium_btc"]
                leg["current_delta"] = premium["delta"]

            # Update via state file directly (simpler than subprocess for updates)
            # The check_exits will use current_value from state

    def check_position_exits(self):
        """Check all positions for exit conditions."""
        # Get IV ranks for crush detection
        iv_ranks = {}
        for strat_config in STRATEGIES.values():
            underlying = strat_config["underlying"]
            if underlying not in iv_ranks:
                iv_ranks[underlying] = self.get_iv_rank(underlying)

        result = self.run_manager(["check_exits", json.dumps(iv_ranks)])
        exits = result.get("actions", [])
        for exit_info in exits:
            self.logger.info("EXIT %s: pnl=$%.2f (%.1f%%) reason=%s fees=$%.2f hold=%.0fm",
                             exit_info.get("position_id", ""),
                             exit_info.get("net_pnl", 0),
                             exit_info.get("pnl_pct", 0),
                             exit_info.get("exit_reason", ""),
                             exit_info.get("total_fees", 0),
                             exit_info.get("hold_minutes", 0))
        return exits

    def check_signals(self):
        """Check for new entry signals across all strategies."""
        # Risk check first
        risk = self.run_manager(["check_risk"])
        if not risk.get("allowed"):
            self.logger.info("RISK_BLOCKED: %s", risk.get("reason"))
            return

        for name, config in STRATEGIES.items():
            if not config.get("enabled"):
                continue

            # Weekend-only check
            if config.get("weekend_only") and not self.is_weekend_entry_time():
                continue

            try:
                if config["type"] == "strangle":
                    self.try_open_strangle(name, config)
                elif config["type"] == "iron_condor":
                    self.try_open_iron_condor(name, config)
                else:
                    self.logger.warning("Unknown strategy type: %s", config["type"])
            except Exception as e:
                self.logger.error("SIGNAL_ERROR %s: %s", name, e)

            time.sleep(1)  # rate limit between strategies

    def print_status(self):
        """Log current status."""
        status = self.run_manager(["status"])
        self.logger.info("=" * 70)
        self.logger.info("OPTIONS STATUS")
        self.logger.info("-" * 70)
        self.logger.info("Portfolio: $%.2f | P&L: $%.2f | Fees: $%.2f | Trades: %d | Win: %.0f%%",
                         status.get("portfolio_value", 0),
                         status.get("total_pnl", 0),
                         status.get("total_fees", 0),
                         status.get("total_trades", 0),
                         status.get("win_rate", 0))
        self.logger.info("Open: %d | Cash: $%.2f",
                         status.get("open_positions", 0),
                         status.get("cash", 0))

        for pos in status.get("positions", []):
            self.logger.info("  %s | %s %s | credit=$%.2f | IVR=%.0f",
                             pos.get("id", ""),
                             pos.get("structure", ""),
                             pos.get("underlying", ""),
                             pos.get("total_credit", 0),
                             pos.get("entry_iv_rank", 0))

        for key, s in status.get("strategies", {}).items():
            if s.get("total_trades", 0) > 0:
                self.logger.info("  STRAT %s-%s | trades=%d win=%.0f%% pnl=$%.2f fees=$%.2f",
                                 s.get("strategy", ""), s.get("underlying", ""),
                                 s["total_trades"],
                                 (s.get("winning_trades", 0) / s["total_trades"] * 100),
                                 s.get("total_pnl", 0), s.get("total_fees", 0))
        self.logger.info("=" * 70)

    def print_report(self):
        """Log full report."""
        report = self.run_manager(["report"])
        self.logger.info("=" * 70)
        self.logger.info("OPTIONS PERFORMANCE REPORT")
        self.logger.info("-" * 70)
        self.logger.info("Portfolio: $%.2f | Net P&L: $%.2f | Fees: $%.2f",
                         report.get("portfolio_value", 0),
                         report.get("total_pnl", 0),
                         report.get("total_fees", 0))
        self.logger.info("Trades: %d | Win Rate: %.1f%%",
                         report.get("total_trades", 0),
                         report.get("win_rate", 0))

        exit_reasons = report.get("exit_reasons", {})
        if exit_reasons:
            self.logger.info("Exit reasons: %s", exit_reasons)

        avg_pnl = report.get("avg_pnl_by_exit_type", {})
        if avg_pnl:
            for reason, avg in avg_pnl.items():
                self.logger.info("  Avg P&L for %s: $%.2f", reason, avg)

        for s in report.get("strategy_performance", []):
            self.logger.info("  %s-%s: %d trades, %.0f%% win, $%.2f P&L",
                             s["strategy"], s["underlying"],
                             s["trades"], s["win_rate"], s["net_pnl"])
        self.logger.info("=" * 70)

    def run(self, cycles: int = None):
        """Main loop."""
        self.logger.info("INIT Options Trading Runner starting")
        self._init_adapter()

        # Initialize state if needed
        self.run_manager(["init"])
        time.sleep(2)

        tick = 0
        signal_tick = 0
        report_tick = 0

        try:
            while True:
                tick += 1

                # ── Every 5 min: check exits + update values ──
                self.logger.info("TICK %d: checking positions", tick)
                self.update_position_values()
                exits = self.check_position_exits()
                if exits:
                    self.logger.info("TICK_EXITS %d: %d positions closed", tick, len(exits))

                # ── Every 30 min: check signals ──
                if tick % (SIGNAL_CHECK_INTERVAL // POSITION_CHECK_INTERVAL) == 1:
                    signal_tick += 1
                    self.logger.info("SIGNAL_CYCLE tick=%d signal=%d", tick, signal_tick)
                    self.check_signals()
                    self.print_status()

                    report_tick += 1
                    if report_tick % 6 == 0:  # Every ~3 hours
                        self.print_report()

                    if cycles and signal_tick >= cycles:
                        self.logger.info("Completed %d cycles. Final report:", cycles)
                        self.print_report()
                        break

                self.logger.info("Next check in %ds", POSITION_CHECK_INTERVAL)
                time.sleep(POSITION_CHECK_INTERVAL)

        except KeyboardInterrupt:
            self.logger.info("Interrupted. Final report:")
            self.print_report()


def acquire_pid_lock(logger: logging.Logger) -> bool:
    pid = os.getpid()
    while True:
        try:
            fd = os.open(PID_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(pid))
            atexit.register(release_pid_lock)
            return True
        except FileExistsError:
            try:
                with open(PID_FILE) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)
                logger.error("Another options_trading_runner instance is running (PID %d). Delete %s to force restart.", old_pid, PID_FILE)
                return False
            except (ProcessLookupError, ValueError, OSError):
                try:
                    os.remove(PID_FILE)
                except FileNotFoundError:
                    pass
                continue


def release_pid_lock() -> None:
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                pid = f.read().strip()
            if pid == str(os.getpid()):
                os.remove(PID_FILE)
    except OSError:
        pass


def main():
    runner = OptionsRunner()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "run":
            if not acquire_pid_lock(runner.logger):
                sys.exit(1)
            cycles = int(sys.argv[2]) if len(sys.argv) > 2 else None
            runner.run(cycles)
        elif cmd == "status":
            runner.print_status()
        elif cmd == "report":
            runner.print_report()
        elif cmd == "once":
            # Single cycle for testing
            runner._init_adapter()
            runner.run_manager(["init"])
            runner.check_signals()
            runner.check_position_exits()
            runner.print_status()
        else:
            print(f"Usage: {sys.argv[0]} run|status|report|once")
    else:
        print(f"Usage: {sys.argv[0]} run|status|report|once")


if __name__ == "__main__":
    main()
