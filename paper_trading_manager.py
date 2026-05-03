#!/usr/bin/env python3
"""
Paper Trading Manager V3 — Production-quality paper trading with:
- Trailing stops on ALL trades (2% default)
- 2-hour max hold time
- Fee accounting (0.1% Binance taker)
- Portfolio kill switch + per-strategy circuit breakers
- Atomic state persistence
- Configurable parameters
- Unrealized P&L tracking
"""

import json
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add paths for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'shared_strategies', 'spot'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'shared_strategies', 'options'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'shared_strategies', 'futures'))

# ── Configuration ──────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "trade_size": 50,                  # $50 per trade (5% risk on $1000)
    "algo_limit": 1000,                # $1000 per algo
    "max_concurrent_trades": 3,        # max open positions per algo
    "signal_timeframe": "15m",        # signal generation timeframe used by the paper runner
    "stop_loss_pct": 0.8,              # SL WIDENED 2026-04-26: 0.5→0.8 (data: 8/38 orb@ETH trades hit -0.5% SL costing -$3.95 vs +$6.39 from 17 TP wins; testing if rescued trades outweigh deeper losses; experiment id=sl-widen-2026-04-26)
    "take_profit_pct": 0.3,            # TP TIGHTENED 2026-04-21: 0.6→0.3 (data: MFE never hit 0.6% in 129 research trades; 34% of TL exits reach 0.3%)
    "trailing_stop_pct": 0.5,          # trailing stop distance (only used when trailing_stop_active is True)
    "trailing_stop_active": False,     # DISABLED 2026-04-27: trailing stop fires on near-zero favorable moves and converts marginal entries into -0.5% locked-in losses (broken design — no activation threshold). Disabled while sl-widen-2026-04-26 is running so we can test hard-SL=0.8% alone. Re-design with proper activation threshold (e.g. only trail when peak >= 0.2% profit) is queued as a future experiment.
    "max_hold_time_hours": 1.0,        # 60m max — exit tuning experiment (was 0.5)
    "trading_fee_pct": 0.0,            # FEES ZEROED 2026-04-21 (temporary; restore to 0.001 to re-enable Binance spot taker)
    "kill_switch_drawdown_pct": 15.0,  # portfolio kill switch threshold
    "circuit_breaker_losses": 5,       # consecutive losses before pause
    "circuit_breaker_pause_hours": 1,  # pause duration after breaker trips
}

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "paper_trading_config.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_trading_state.json")


def load_config() -> dict:
    """Load config from file, falling back to defaults."""
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
    return cfg


# ── Manager ────────────────────────────────────────────────────────────────

class PaperTradingManager:
    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.config = load_config()
        self.state = self.load_state()

    # ── State persistence (atomic) ─────────────────────────────────────

    def load_state(self) -> dict:
        if os.path.exists(self.state_file) and os.path.getsize(self.state_file) > 0:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            # Ensure portfolio_risk exists (migration)
            state.setdefault("portfolio_risk", {
                "peak_value": 0,
                "current_drawdown_pct": 0,
                "kill_switch_active": False,
                "kill_switch_at": None,
                "events": [],
            })
            return state
        return {
            "algos": {},
            "active_trades": [],
            "completed_trades": [],
            "start_time": datetime.now(timezone.utc).isoformat(),
            "portfolio_risk": {
                "peak_value": 0,
                "current_drawdown_pct": 0,
                "kill_switch_active": False,
                "kill_switch_at": None,
                "events": [],
            },
        }

    def save_state(self):
        """Atomic write: tmp file + rename (crash-safe)."""
        tmp_path = self.state_file + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp_path, self.state_file)

    # ── Algo management ────────────────────────────────────────────────

    def initialize_algo(self, algo_name: str, asset: str):
        key = f"{algo_name}-{asset}"
        if key not in self.state["algos"]:
            self.state["algos"][key] = {
                "name": algo_name,
                "asset": asset,
                "initial_capital": self.config["algo_limit"],
                "available_capital": self.config["algo_limit"],
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0,
                "total_fees": 0,
                "active_positions": [],
                # Risk tracking
                "peak_value": self.config["algo_limit"],
                "consecutive_losses": 0,
                "circuit_breaker": False,
                "circuit_breaker_until": None,
            }
            self.save_state()

    # ── Risk checks ────────────────────────────────────────────────────

    def check_risk(self, algo_name: str, asset: str) -> tuple:
        """Check portfolio kill switch + per-strategy circuit breaker."""
        cfg = self.config
        pr = self.state["portfolio_risk"]

        # 1. Portfolio kill switch
        if pr.get("kill_switch_active"):
            return False, f"Portfolio kill switch active since {pr['kill_switch_at']}"

        # Compute total portfolio value
        total_value = 0
        total_initial = 0
        for a in self.state["algos"].values():
            total_value += a["available_capital"] + len(a["active_positions"]) * cfg["trade_size"]
            total_initial += a["initial_capital"]

        if total_initial > 0:
            peak = max(pr.get("peak_value", total_initial), total_value)
            pr["peak_value"] = peak
            if peak > 0:
                drawdown = (peak - total_value) / peak * 100
                pr["current_drawdown_pct"] = round(drawdown, 2)
                if drawdown > cfg["kill_switch_drawdown_pct"]:
                    pr["kill_switch_active"] = True
                    pr["kill_switch_at"] = datetime.now(timezone.utc).isoformat()
                    pr.setdefault("events", []).append({
                        "type": "triggered",
                        "timestamp": pr["kill_switch_at"],
                        "drawdown_pct": round(drawdown, 2),
                        "portfolio_value": round(total_value, 2),
                        "peak_value": round(peak, 2),
                    })
                    self.save_state()
                    return False, f"Portfolio drawdown {drawdown:.1f}% exceeds {cfg['kill_switch_drawdown_pct']}% — kill switch triggered"

        # 2. Per-strategy circuit breaker
        key = f"{algo_name}-{asset}"
        algo = self.state["algos"].get(key)
        if algo:
            if algo.get("circuit_breaker"):
                until = algo.get("circuit_breaker_until", "")
                if until:
                    try:
                        until_dt = datetime.fromisoformat(until)
                        if until_dt.tzinfo is None:
                            until_dt = until_dt.replace(tzinfo=timezone.utc)
                        if until_dt > datetime.now(timezone.utc):
                            return False, f"Circuit breaker active until {until}"
                    except (ValueError, TypeError):
                        pass
                # Breaker expired
                algo["circuit_breaker"] = False
                algo["consecutive_losses"] = 0

        return True, "OK"

    # ── Trade lifecycle ────────────────────────────────────────────────

    def can_trade(self, algo_name: str, asset: str) -> tuple:
        key = f"{algo_name}-{asset}"
        if key not in self.state["algos"]:
            return False, "Algo not initialized"

        cfg = self.config
        algo = self.state["algos"][key]
        trade_cost = cfg["trade_size"] * (1 + cfg["trading_fee_pct"])  # size + entry fee

        if algo["available_capital"] < trade_cost:
            return False, f"Insufficient capital: ${algo['available_capital']:.2f} < ${trade_cost:.2f}"
        if len(algo["active_positions"]) >= cfg["max_concurrent_trades"]:
            return False, f"Max concurrent trades: {len(algo['active_positions'])}/{cfg['max_concurrent_trades']}"
        return True, "OK"

    def open_trade(self, algo_name: str, asset: str, signal: str, price: float, reason: str = "") -> dict:
        key = f"{algo_name}-{asset}"
        cfg = self.config

        # Risk check first
        risk_ok, risk_msg = self.check_risk(algo_name, asset)
        if not risk_ok:
            return {"success": False, "reason": f"Risk blocked: {risk_msg}"}

        can, msg = self.can_trade(algo_name, asset)
        if not can:
            return {"success": False, "reason": msg}

        entry_fee = cfg["trade_size"] * cfg["trading_fee_pct"]

        trade = {
            "id": f"{key}-{len(self.state['completed_trades']) + len(self.state['active_trades'])}",
            "algo": algo_name,
            "asset": asset,
            "signal": signal,
            "entry_price": price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "size": cfg["trade_size"],
            "reason": reason,
            "status": "open",
            "entry_fee": round(entry_fee, 4),
            "highest_price": price if signal == "buy" else None,
            "lowest_price": price if signal == "sell" else None,
        }

        algo = self.state["algos"][key]
        algo["available_capital"] -= (cfg["trade_size"] + entry_fee)
        algo["active_positions"].append(trade["id"])

        self.state["active_trades"].append(trade)
        self.save_state()
        return {"success": True, "trade": trade}

    def close_trade(self, trade_id: str, exit_price: float, reason: str = "") -> dict:
        trade = None
        for t in self.state["active_trades"]:
            if t["id"] == trade_id:
                trade = t
                break
        if not trade:
            return {"success": False, "reason": "Trade not found"}

        cfg = self.config
        trade_size = trade["size"]

        # Gross P&L
        if trade["signal"] == "buy":
            gross_pnl = (exit_price - trade["entry_price"]) / trade["entry_price"] * trade_size
        else:
            gross_pnl = (trade["entry_price"] - exit_price) / trade["entry_price"] * trade_size

        # Fees
        entry_fee = trade.get("entry_fee", trade_size * cfg["trading_fee_pct"])
        exit_fee = trade_size * cfg["trading_fee_pct"]
        total_fees = entry_fee + exit_fee

        # Net P&L
        net_pnl = gross_pnl - exit_fee  # entry fee already deducted from capital
        pnl_pct = (net_pnl / trade_size) * 100

        # Hold duration
        entry_time = datetime.fromisoformat(trade["entry_time"])
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        exit_time = datetime.now(timezone.utc)
        hold_minutes = (exit_time - entry_time).total_seconds() / 60

        # Update trade record
        trade["exit_price"] = exit_price
        trade["exit_time"] = exit_time.isoformat()
        trade["gross_pnl"] = round(gross_pnl, 4)
        trade["entry_fee"] = round(entry_fee, 4)
        trade["exit_fee"] = round(exit_fee, 4)
        trade["total_fees"] = round(total_fees, 4)
        trade["net_pnl"] = round(net_pnl, 4)
        trade["pnl_pct"] = round(pnl_pct, 4)
        trade["hold_minutes"] = round(hold_minutes, 1)
        trade["exit_reason"] = reason
        trade["status"] = "closed"

        # Update algo state
        key = f"{trade['algo']}-{trade['asset']}"
        algo = self.state["algos"][key]
        algo["available_capital"] += trade_size + net_pnl  # return capital + net profit/loss
        algo["total_trades"] += 1
        algo["total_pnl"] = round(algo["total_pnl"] + net_pnl, 4)
        algo["total_fees"] = round(algo.get("total_fees", 0) + total_fees, 4)

        if net_pnl >= 0:
            algo["winning_trades"] += 1
            algo["consecutive_losses"] = 0
        else:
            algo["losing_trades"] += 1
            algo["consecutive_losses"] = algo.get("consecutive_losses", 0) + 1
            # Check circuit breaker
            if algo["consecutive_losses"] >= cfg["circuit_breaker_losses"]:
                algo["circuit_breaker"] = True
                algo["circuit_breaker_until"] = (
                    datetime.now(timezone.utc) + timedelta(hours=cfg["circuit_breaker_pause_hours"])
                ).isoformat()

        # Update peak value
        total_capital = algo["available_capital"] + len(algo["active_positions"]) * cfg["trade_size"]
        algo["peak_value"] = max(algo.get("peak_value", algo["initial_capital"]), total_capital)

        algo["active_positions"].remove(trade_id)

        # Move to completed (keep last 500)
        self.state["active_trades"].remove(trade)
        self.state["completed_trades"].append(trade)
        if len(self.state["completed_trades"]) > 500:
            self.state["completed_trades"] = self.state["completed_trades"][-500:]

        self.save_state()
        return {"success": True, "trade": trade, "net_pnl": net_pnl, "pnl_pct": pnl_pct, "hold_minutes": hold_minutes}

    # ── Trailing stop + time limit check ───────────────────────────────

    def check_trailing_stop_and_time_limit(self, current_prices: dict) -> list:
        """Check ALL active trades for exits. Returns list of actions taken."""
        cfg = self.config
        actions = []
        current_time = datetime.now(timezone.utc)

        for trade in list(self.state["active_trades"]):  # copy list since we may modify
            trade_id = trade["id"]
            asset = trade["asset"]
            signal = trade["signal"]
            entry_price = trade["entry_price"]

            entry_time = datetime.fromisoformat(trade["entry_time"])
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)

            current_price = current_prices.get(asset, 0)
            if current_price == 0:
                continue

            time_held_hours = (current_time - entry_time).total_seconds() / 3600

            # ── Update peak/trough prices ──
            if signal == "buy":
                prev_high = trade.get("highest_price") or entry_price
                trade["highest_price"] = max(prev_high, current_price)
            else:
                prev_low = trade.get("lowest_price") or entry_price
                trade["lowest_price"] = min(prev_low, current_price)

            # ── Check exit conditions (priority order) ──
            exit_reason = None

            # 1. Hard stop loss (5%) — safety net, check first
            if signal == "buy":
                pnl_pct = ((current_price - entry_price) / entry_price) * 100
            else:
                pnl_pct = ((entry_price - current_price) / entry_price) * 100

            if pnl_pct <= -cfg["stop_loss_pct"]:
                exit_reason = f"STOP LOSS: {pnl_pct:.2f}% (limit: -{cfg['stop_loss_pct']}%)"

            # 2. Take profit
            elif pnl_pct >= cfg["take_profit_pct"]:
                exit_reason = f"TAKE PROFIT: +{pnl_pct:.2f}% (target: {cfg['take_profit_pct']}%)"

            # 3. Trailing stop — gated on trailing_stop_active flag
            elif cfg.get("trailing_stop_active", False) and signal == "buy":
                trailing_stop = trade["highest_price"] * (1 - cfg["trailing_stop_pct"] / 100)
                if current_price <= trailing_stop and trade["highest_price"] > entry_price:
                    exit_reason = f"TRAILING STOP: {pnl_pct:.2f}% (peak: ${trade['highest_price']:.2f}, stop: ${trailing_stop:.2f})"
            elif cfg.get("trailing_stop_active", False) and signal == "sell":
                trailing_stop = trade["lowest_price"] * (1 + cfg["trailing_stop_pct"] / 100)
                if current_price >= trailing_stop and trade["lowest_price"] < entry_price:
                    exit_reason = f"TRAILING STOP: {pnl_pct:.2f}% (low: ${trade['lowest_price']:.2f}, stop: ${trailing_stop:.2f})"

            # 4. Time limit (2 hours)
            if not exit_reason and time_held_hours >= cfg["max_hold_time_hours"]:
                exit_reason = f"TIME LIMIT: {time_held_hours:.1f}h >= {cfg['max_hold_time_hours']}h ({pnl_pct:+.2f}%)"

            if exit_reason:
                # Execute the close
                close_result = self.close_trade(trade_id, current_price, exit_reason)
                actions.append({
                    "trade_id": trade_id,
                    "algo": trade["algo"],
                    "asset": asset,
                    "signal": signal,
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "reason": exit_reason,
                    "net_pnl": close_result.get("net_pnl", 0),
                    "pnl_pct": close_result.get("pnl_pct", 0),
                    "hold_minutes": close_result.get("hold_minutes", 0),
                })

        # Persist updated highest/lowest prices even if nothing closed
        self.save_state()
        return actions

    # ── Status & reporting ─────────────────────────────────────────────

    def get_status(self, current_prices: dict = None) -> dict:
        cfg = self.config
        status = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config": {
                "trade_size": cfg["trade_size"],
                "trailing_stop_pct": cfg["trailing_stop_pct"],
                "max_hold_time_hours": cfg["max_hold_time_hours"],
                "stop_loss_pct": cfg["stop_loss_pct"],
                "fee_pct": cfg["trading_fee_pct"],
            },
            "portfolio_risk": self.state.get("portfolio_risk", {}),
            "algos": [],
            "active_trades": [],
            "recent_completed": [],
            "summary": {},
        }

        # Algo summaries
        total_portfolio_value = 0
        total_pnl = 0
        total_fees = 0
        total_trades_count = 0
        total_wins = 0

        for key, algo in self.state["algos"].items():
            total_capital = algo["available_capital"] + len(algo["active_positions"]) * cfg["trade_size"]
            profit = total_capital - algo["initial_capital"]
            profit_pct = (profit / algo["initial_capital"]) * 100 if algo["initial_capital"] > 0 else 0
            win_rate = (algo["winning_trades"] / algo["total_trades"] * 100) if algo["total_trades"] > 0 else 0

            total_portfolio_value += total_capital
            total_pnl += algo["total_pnl"]
            total_fees += algo.get("total_fees", 0)
            total_trades_count += algo["total_trades"]
            total_wins += algo["winning_trades"]

            algo_status = {
                "name": f"{algo['name']} ({algo['asset']})",
                "capital": f"${total_capital:.2f}",
                "available": f"${algo['available_capital']:.2f}",
                "profit": f"${algo['total_pnl']:.2f}",
                "profit_pct": f"{profit_pct:.2f}%",
                "fees_paid": f"${algo.get('total_fees', 0):.2f}",
                "trades": algo["total_trades"],
                "win_rate": f"{win_rate:.1f}%",
                "active_positions": len(algo["active_positions"]),
                "consecutive_losses": algo.get("consecutive_losses", 0),
                "circuit_breaker": algo.get("circuit_breaker", False),
            }
            status["algos"].append(algo_status)

        # Active trades with unrealized P&L
        for trade in self.state["active_trades"]:
            entry_time = datetime.fromisoformat(trade["entry_time"])
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            hold_min = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60

            trade_info = {
                "id": trade["id"],
                "algo": f"{trade['algo']} ({trade['asset']})",
                "signal": trade["signal"],
                "entry_price": f"${trade['entry_price']:.2f}",
                "entry_time": trade["entry_time"],
                "size": f"${trade['size']}",
                "hold_minutes": f"{hold_min:.0f}m",
                "highest_price": f"${trade.get('highest_price', 0):.2f}" if trade.get("highest_price") else "N/A",
                "lowest_price": f"${trade.get('lowest_price', 0):.2f}" if trade.get("lowest_price") else "N/A",
            }

            # Unrealized P&L
            if current_prices:
                cp = current_prices.get(trade["asset"], 0)
                if cp > 0:
                    if trade["signal"] == "buy":
                        unrealized = (cp - trade["entry_price"]) / trade["entry_price"] * trade["size"]
                        unrealized_pct = (cp - trade["entry_price"]) / trade["entry_price"] * 100
                    else:
                        unrealized = (trade["entry_price"] - cp) / trade["entry_price"] * trade["size"]
                        unrealized_pct = (trade["entry_price"] - cp) / trade["entry_price"] * 100
                    trade_info["current_price"] = f"${cp:.2f}"
                    trade_info["unrealized_pnl"] = f"${unrealized:.2f}"
                    trade_info["unrealized_pnl_pct"] = f"{unrealized_pct:+.2f}%"

            status["active_trades"].append(trade_info)

        # Recent completed (last 20)
        for trade in self.state["completed_trades"][-20:]:
            status["recent_completed"].append({
                "id": trade["id"],
                "algo": f"{trade['algo']} ({trade['asset']})",
                "signal": trade["signal"],
                "entry_price": f"${trade['entry_price']:.2f}",
                "exit_price": f"${trade.get('exit_price', 0):.2f}",
                "net_pnl": f"${trade.get('net_pnl', 0):.2f}",
                "pnl_pct": f"{trade.get('pnl_pct', 0):.2f}%",
                "fees": f"${trade.get('total_fees', 0):.4f}",
                "hold_minutes": f"{trade.get('hold_minutes', 0):.0f}m",
                "exit_reason": trade.get("exit_reason", ""),
                "exit_time": trade.get("exit_time", ""),
            })

        # Portfolio summary
        overall_win_rate = (total_wins / total_trades_count * 100) if total_trades_count > 0 else 0
        avg_hold = 0
        if self.state["completed_trades"]:
            avg_hold = sum(t.get("hold_minutes", 0) for t in self.state["completed_trades"]) / len(self.state["completed_trades"])

        status["summary"] = {
            "total_portfolio_value": f"${total_portfolio_value:.2f}",
            "total_net_pnl": f"${total_pnl:.2f}",
            "total_fees_paid": f"${total_fees:.2f}",
            "total_completed_trades": total_trades_count,
            "overall_win_rate": f"{overall_win_rate:.1f}%",
            "active_trades_count": len(self.state["active_trades"]),
            "avg_hold_minutes": f"{avg_hold:.0f}m",
        }

        return status

    def generate_report(self) -> dict:
        cfg = self.config
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "start_time": self.state.get("start_time", "N/A"),
            "config": {
                "trade_size": cfg["trade_size"],
                "trailing_stop_pct": cfg["trailing_stop_pct"],
                "max_hold_time_hours": cfg["max_hold_time_hours"],
                "fee_pct": cfg["trading_fee_pct"],
            },
            "total_algos": len(self.state["algos"]),
            "total_trades": len(self.state["completed_trades"]),
            "active_trades": len(self.state["active_trades"]),
            "algo_performance": [],
            "exit_reason_breakdown": {},
        }

        # Per-algo performance
        for key, algo in sorted(self.state["algos"].items()):
            total_capital = algo["available_capital"] + len(algo["active_positions"]) * cfg["trade_size"]
            profit = total_capital - algo["initial_capital"]
            profit_pct = (profit / algo["initial_capital"]) * 100 if algo["initial_capital"] > 0 else 0
            win_rate = (algo["winning_trades"] / algo["total_trades"] * 100) if algo["total_trades"] > 0 else 0

            report["algo_performance"].append({
                "algo": algo["name"],
                "asset": algo["asset"],
                "initial_capital": f"${algo['initial_capital']}",
                "current_capital": f"${total_capital:.2f}",
                "net_profit": f"${algo['total_pnl']:.2f}",
                "profit_pct": f"{profit_pct:.2f}%",
                "fees_paid": f"${algo.get('total_fees', 0):.2f}",
                "total_trades": algo["total_trades"],
                "winning": algo["winning_trades"],
                "losing": algo["losing_trades"],
                "win_rate": f"{win_rate:.1f}%",
                "active": len(algo["active_positions"]),
            })

        # Exit reason breakdown
        reasons = {}
        for t in self.state["completed_trades"]:
            reason = t.get("exit_reason", "unknown")
            # Categorize
            if "STOP LOSS" in reason:
                cat = "stop_loss"
            elif "TAKE PROFIT" in reason:
                cat = "take_profit"
            elif "TRAILING STOP" in reason:
                cat = "trailing_stop"
            elif "TIME LIMIT" in reason:
                cat = "time_limit"
            elif "Exit signal" in reason:
                cat = "opposite_signal"
            else:
                cat = "other"
            reasons[cat] = reasons.get(cat, 0) + 1
        report["exit_reason_breakdown"] = reasons

        # Average P&L by exit type
        pnl_by_type = {}
        for t in self.state["completed_trades"]:
            reason = t.get("exit_reason", "unknown")
            if "STOP LOSS" in reason:
                cat = "stop_loss"
            elif "TAKE PROFIT" in reason:
                cat = "take_profit"
            elif "TRAILING STOP" in reason:
                cat = "trailing_stop"
            elif "TIME LIMIT" in reason:
                cat = "time_limit"
            else:
                cat = "other"
            if cat not in pnl_by_type:
                pnl_by_type[cat] = []
            pnl_by_type[cat].append(t.get("net_pnl", 0))
        report["avg_pnl_by_exit_type"] = {
            k: f"${sum(v)/len(v):.2f}" for k, v in pnl_by_type.items()
        }

        return report

    # ── Best algos ranking ─────────────────────────────────────────────

    def get_best_algos(self) -> list:
        """Rank algos by net P&L — shows which are worth trading with real money."""
        cfg = self.config
        rankings = []
        for key, algo in self.state["algos"].items():
            if algo["total_trades"] < 3:
                continue  # not enough data
            total_capital = algo["available_capital"] + len(algo["active_positions"]) * cfg["trade_size"]
            profit_pct = ((total_capital - algo["initial_capital"]) / algo["initial_capital"]) * 100
            win_rate = (algo["winning_trades"] / algo["total_trades"] * 100) if algo["total_trades"] > 0 else 0
            avg_pnl = algo["total_pnl"] / algo["total_trades"] if algo["total_trades"] > 0 else 0

            rankings.append({
                "algo": f"{algo['name']} ({algo['asset']})",
                "net_pnl": round(algo["total_pnl"], 2),
                "profit_pct": round(profit_pct, 2),
                "trades": algo["total_trades"],
                "win_rate": round(win_rate, 1),
                "avg_pnl_per_trade": round(avg_pnl, 2),
                "fees_paid": round(algo.get("total_fees", 0), 2),
                "verdict": "PROFITABLE" if algo["total_pnl"] > 0 and win_rate > 50 else "NEEDS WORK",
            })

        rankings.sort(key=lambda x: x["net_pnl"], reverse=True)
        return rankings


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    # Strip --state-file <path> from argv before dispatching commands.
    args = sys.argv[1:]
    state_file_override = None
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--state-file" and i + 1 < len(args):
            state_file_override = args[i + 1]
            i += 2
        else:
            filtered.append(args[i])
            i += 1
    if not filtered:
        print(json.dumps({"error": "Commands: init, status, report, best, open, close, check_stops, check_risk, reset_kill_switch"}))
        sys.exit(1)

    manager = PaperTradingManager(state_file=state_file_override) if state_file_override else PaperTradingManager()
    command = filtered[0]
    # Rebind sys.argv so existing positional code still works.
    sys.argv = [sys.argv[0]] + filtered

    if command == "init":
        from strategies import list_strategies as list_spot_strategies
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), 'shared_strategies', 'spot', 'strategies.py'), '--list-json'],
            capture_output=True, text=True
        )
        strategies_list = json.loads(result.stdout)
        spot_strategies = [s['id'] for s in strategies_list]
        # PAXG = tokenized gold; skip volume_weighted (illiquid)
        assets = ["BTC", "ETH", "PAXG"]
        volume_sensitive = {"volume_weighted"}
        initialized = 0
        for strategy in spot_strategies:
            for asset in assets:
                if strategy in volume_sensitive and asset == "PAXG":
                    continue
                manager.initialize_algo(strategy, asset)
                initialized += 1
        print(json.dumps({
            "success": True,
            "initialized": initialized,
            "strategies": spot_strategies,
            "assets": assets,
            "trade_size": manager.config["trade_size"],
            "trailing_stop_pct": manager.config["trailing_stop_pct"],
            "max_hold_time_hours": manager.config["max_hold_time_hours"],
        }))

    elif command == "status":
        prices = None
        if len(sys.argv) > 2:
            try:
                prices = json.loads(sys.argv[2])
            except json.JSONDecodeError:
                pass
        status = manager.get_status(current_prices=prices)
        print(json.dumps(status, indent=2))

    elif command == "report":
        report = manager.generate_report()
        print(json.dumps(report, indent=2))

    elif command == "best":
        rankings = manager.get_best_algos()
        print(json.dumps(rankings, indent=2))

    elif command == "open":
        if len(sys.argv) < 6:
            print(json.dumps({"error": "Usage: open <algo> <asset> <signal> <price> [reason]"}))
            sys.exit(1)
        result = manager.open_trade(
            sys.argv[2], sys.argv[3], sys.argv[4],
            float(sys.argv[5]),
            sys.argv[6] if len(sys.argv) > 6 else ""
        )
        print(json.dumps(result, indent=2))

    elif command == "close":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "Usage: close <trade_id> <exit_price> [reason]"}))
            sys.exit(1)
        result = manager.close_trade(
            sys.argv[2], float(sys.argv[3]),
            sys.argv[4] if len(sys.argv) > 4 else ""
        )
        print(json.dumps(result, indent=2))

    elif command == "check_stops":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "Usage: check_stops '<prices_json>'"}))
            sys.exit(1)
        prices = json.loads(sys.argv[2])
        actions = manager.check_trailing_stop_and_time_limit(prices)
        print(json.dumps({"exits": len(actions), "actions": actions}))

    elif command == "check_risk":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "Usage: check_risk <algo> <asset>"}))
            sys.exit(1)
        allowed, reason = manager.check_risk(sys.argv[2], sys.argv[3])
        print(json.dumps({"allowed": allowed, "reason": reason}))

    elif command == "reset_kill_switch":
        pr = manager.state.setdefault("portfolio_risk", {})
        pr["kill_switch_active"] = False
        pr.setdefault("events", []).append({
            "type": "reset",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        manager.save_state()
        print(json.dumps({"success": True, "message": "Kill switch reset"}))

    elif command == "init_single":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "Usage: init_single <algo> <asset>"}))
            sys.exit(1)
        manager.initialize_algo(sys.argv[2], sys.argv[3])
        print(json.dumps({"success": True, "algo": sys.argv[2], "asset": sys.argv[3]}))

    else:
        print(json.dumps({"error": f"Unknown command: {command}. Use: init, init_single, status, report, best, open, close, check_stops, check_risk, reset_kill_switch"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
