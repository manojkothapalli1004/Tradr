#!/usr/bin/env python3
"""
Options Trading Manager — State management, trade execution, P&L tracking.
Manages options positions with multi-leg support, greeks tracking, and risk management.

CLI Interface:
    init                          Initialize adapter + risk manager
    open_strangle <underlying> <dte> <otm_pct> <side>   Open strangle
    open_iron_condor <underlying> <dte> <short_otm> <wing_width>  Open iron condor
    close <leg_group> <reason>    Close a position group
    check_exits <prices_json>     Check all positions for exit conditions
    status                        Portfolio status + open positions
    report                        Performance report
    best                          Rank strategies by P&L
"""

import json
import sys
import os
import time
import copy
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

# ── paths ──
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, 'platforms', 'deribit'))
sys.path.insert(0, os.path.join(_THIS_DIR, 'shared_strategies', 'options'))

STATE_FILE = os.path.join(_THIS_DIR, 'options_trading_state.json')

# ── config ──
DEFAULT_CONFIG = {
    "initial_capital": 10000.0,
    "max_concurrent_positions": 3,
    "max_per_strategy_per_underlying": 1,
    # Exit thresholds
    "profit_target_pct": 50.0,       # close at 50% of max credit
    "stop_loss_pct": 200.0,          # stop at 200% of credit (2x loss)
    "min_dte_exit": 7,               # close if <7 DTE
    "iv_crush_exit_pts": 20,         # close if IV rank drops >20 pts
    # Entry filters
    "min_iv_rank_entry": 50,         # only sell premium when IVR > 50
    "min_iv_rank_straddle_buy": 0,   # buy straddles any IV (event plays)
    "max_iv_rank_straddle_buy": 25,  # buy straddles when IV is low
    # Fee model (Deribit)
    "fee_pct": 0.0,                 # FEES ZEROED 2026-04-21 (temporary; restore to 0.0003 for 0.03% Deribit)
    "fee_cap_pct": 0.125,           # capped at 12.5% of option price
    # Risk
    "max_portfolio_drawdown_pct": 20.0,
    "max_daily_loss_pct": 5.0,
    "max_consecutive_losses": 4,
    "circuit_breaker_minutes": 60,
    "kill_switch_drawdown_pct": 25.0,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return _default_state()


def _save_state(state: dict):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def _default_state() -> dict:
    return {
        "config": DEFAULT_CONFIG,
        "portfolio": {
            "initial_capital": DEFAULT_CONFIG["initial_capital"],
            "cash": DEFAULT_CONFIG["initial_capital"],
            "peak_value": DEFAULT_CONFIG["initial_capital"],
        },
        "strategies": {},
        "positions": {},
        "completed": [],
        "risk": {
            "consecutive_losses": 0,
            "circuit_breaker_until": None,
            "kill_switch_active": False,
            "kill_switch_at": None,
            "daily_pnl": 0.0,
            "daily_reset_date": _now()[:10],
        },
        "start_time": _now(),
        "next_position_id": 1,
    }


# ── fee calculation ──

def calculate_fee(underlying_price: float, option_price_btc: float,
                  quantity: float, config: dict) -> float:
    """Calculate Deribit-style fee in USD."""
    notional = underlying_price * quantity
    fee = notional * config["fee_pct"]
    # Cap at 12.5% of option value
    option_value_usd = option_price_btc * underlying_price * quantity
    cap = option_value_usd * config["fee_cap_pct"]
    if cap > 0:
        fee = min(fee, cap)
    return round(fee, 4)


# ── position management ──

def open_position(state: dict, strategy: str, underlying: str,
                  legs: List[dict], total_credit_or_debit: float,
                  iv_rank: float, structure: str) -> dict:
    """Open a new options position."""
    config = state["config"]
    pos_id = f"{strategy}_{underlying}_{state['next_position_id']}"
    state["next_position_id"] += 1

    # Calculate entry fees
    spot = legs[0].get("spot_price", 0) if legs else 0
    total_fees = 0.0
    for leg in legs:
        premium_btc = leg.get("premium_btc", 0)
        qty = leg.get("quantity", 1.0)
        fee = calculate_fee(spot, premium_btc, qty, config)
        leg["entry_fee"] = fee
        total_fees += fee

    position = {
        "id": pos_id,
        "strategy": strategy,
        "underlying": underlying,
        "structure": structure,  # strangle, iron_condor, straddle, etc.
        "legs": legs,
        "entry_time": _now(),
        "entry_iv_rank": iv_rank,
        "total_credit": total_credit_or_debit,  # positive = credit, negative = debit
        "total_fees": total_fees,
        "current_value": total_credit_or_debit,
        "status": "open",
    }

    state["positions"][pos_id] = position

    # Track per-strategy stats
    strat_key = f"{strategy}-{underlying}"
    if strat_key not in state["strategies"]:
        state["strategies"][strat_key] = {
            "strategy": strategy,
            "underlying": underlying,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_pnl": 0.0,
            "total_fees": 0.0,
        }

    # Deduct from cash (for debit trades) or hold margin (for credit trades)
    if total_credit_or_debit > 0:
        # Credit trade: collect premium, but hold margin
        state["portfolio"]["cash"] += total_credit_or_debit - total_fees
    else:
        # Debit trade: pay premium + fees
        state["portfolio"]["cash"] += total_credit_or_debit - total_fees

    _save_state(state)
    return {"success": True, "position": position}


def close_position(state: dict, pos_id: str, current_value: float,
                   reason: str) -> dict:
    """Close a position and realize P&L."""
    if pos_id not in state["positions"]:
        return {"success": False, "reason": f"Position {pos_id} not found"}

    pos = state["positions"].pop(pos_id)
    config = state["config"]

    # Calculate exit fees
    spot = pos["legs"][0].get("spot_price", 0) if pos["legs"] else 0
    exit_fees = 0.0
    for leg in pos["legs"]:
        premium_btc = leg.get("current_premium_btc", leg.get("premium_btc", 0))
        qty = leg.get("quantity", 1.0)
        fee = calculate_fee(spot, premium_btc, qty, config)
        exit_fees += fee

    total_fees = pos["total_fees"] + exit_fees

    # P&L calculation
    # For credit trades: profit = credit_received - cost_to_close
    # For debit trades: profit = value_at_close - debit_paid
    if pos["total_credit"] > 0:
        # Credit trade (sold premium)
        net_pnl = pos["total_credit"] - current_value - total_fees
    else:
        # Debit trade (bought premium)
        net_pnl = current_value + pos["total_credit"] - total_fees

    hold_minutes = 0
    try:
        entry_dt = datetime.fromisoformat(pos["entry_time"])
        hold_minutes = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
    except (ValueError, TypeError):
        pass

    pnl_pct = (net_pnl / abs(pos["total_credit"])) * 100 if pos["total_credit"] != 0 else 0

    completed = {
        **pos,
        "exit_time": _now(),
        "exit_value": current_value,
        "exit_fees": exit_fees,
        "total_fees": total_fees,
        "net_pnl": round(net_pnl, 4),
        "pnl_pct": round(pnl_pct, 2),
        "hold_minutes": round(hold_minutes, 1),
        "exit_reason": reason,
        "status": "closed",
    }

    state["completed"].append(completed)

    # Return cash
    if pos["total_credit"] > 0:
        state["portfolio"]["cash"] += (pos["total_credit"] - current_value - exit_fees)
    else:
        state["portfolio"]["cash"] += (current_value - exit_fees)

    # Update strategy stats
    strat_key = f"{pos['strategy']}-{pos['underlying']}"
    if strat_key in state["strategies"]:
        s = state["strategies"][strat_key]
        s["total_trades"] += 1
        s["total_pnl"] += net_pnl
        s["total_fees"] += total_fees
        if net_pnl > 0:
            s["winning_trades"] += 1
        else:
            s["losing_trades"] += 1

    # Update risk tracking
    if net_pnl > 0:
        state["risk"]["consecutive_losses"] = 0
    else:
        state["risk"]["consecutive_losses"] += 1

    state["risk"]["daily_pnl"] += net_pnl

    # Update peak
    portfolio_val = get_portfolio_value(state)
    if portfolio_val > state["portfolio"]["peak_value"]:
        state["portfolio"]["peak_value"] = portfolio_val

    _save_state(state)
    return {
        "success": True,
        "position_id": pos_id,
        "net_pnl": round(net_pnl, 4),
        "pnl_pct": round(pnl_pct, 2),
        "hold_minutes": round(hold_minutes, 1),
        "exit_reason": reason,
        "total_fees": round(total_fees, 4),
    }


# ── exit checks ──

def check_exits(state: dict, iv_ranks: Dict[str, float] = None) -> List[dict]:
    """Check all open positions for exit conditions."""
    config = state["config"]
    exits = []
    iv_ranks = iv_ranks or {}

    for pos_id, pos in list(state["positions"].items()):
        reason = None
        current_value = pos.get("current_value", 0)
        credit = pos["total_credit"]

        if credit > 0:
            # Credit trade: profit when current_value < credit
            pnl_pct = ((credit - current_value) / credit) * 100
        else:
            # Debit trade: profit when current_value > |debit|
            pnl_pct = ((current_value + credit) / abs(credit)) * 100 if credit != 0 else 0

        # 1. Profit target (50% of max credit)
        if credit > 0 and pnl_pct >= config["profit_target_pct"]:
            reason = f"PROFIT_TARGET: {pnl_pct:.1f}% >= {config['profit_target_pct']}%"

        # 2. Stop loss (200% of credit = 2x loss)
        if credit > 0 and pnl_pct <= -config["stop_loss_pct"]:
            reason = f"STOP_LOSS: {pnl_pct:.1f}% <= -{config['stop_loss_pct']}%"

        # 3. DTE check
        if not reason:
            try:
                for leg in pos.get("legs", []):
                    expiry = leg.get("expiry")
                    if expiry:
                        exp_dt = datetime.fromisoformat(expiry) if "T" in str(expiry) else datetime.strptime(str(expiry), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        dte = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400
                        if dte < config["min_dte_exit"]:
                            reason = f"DTE_EXIT: {dte:.1f} DTE < {config['min_dte_exit']}"
                            break
            except (ValueError, TypeError):
                pass

        # 4. IV crush check
        if not reason and iv_ranks:
            underlying = pos["underlying"]
            if underlying in iv_ranks:
                current_iv = iv_ranks[underlying]
                entry_iv = pos.get("entry_iv_rank", 50)
                iv_drop = entry_iv - current_iv
                if iv_drop >= config["iv_crush_exit_pts"]:
                    reason = f"IV_CRUSH: rank dropped {iv_drop:.0f}pts ({entry_iv:.0f} → {current_iv:.0f})"

        if reason:
            result = close_position(state, pos_id, current_value, reason)
            if result["success"]:
                exits.append(result)

    return exits


# ── risk checks ──

def check_can_trade(state: dict) -> dict:
    """Pre-trade risk check."""
    config = state["config"]
    risk = state["risk"]

    # Reset daily if new day
    today = _now()[:10]
    if risk.get("daily_reset_date") != today:
        risk["daily_pnl"] = 0.0
        risk["daily_reset_date"] = today

    # Kill switch
    if risk.get("kill_switch_active"):
        return {"allowed": False, "reason": "Kill switch active"}

    # Circuit breaker
    if risk.get("circuit_breaker_until"):
        try:
            cb_until = datetime.fromisoformat(risk["circuit_breaker_until"])
            if datetime.now(timezone.utc) < cb_until:
                remaining = (cb_until - datetime.now(timezone.utc)).total_seconds() / 60
                return {"allowed": False, "reason": f"Circuit breaker ({remaining:.0f}m remaining)"}
            else:
                risk["circuit_breaker_until"] = None
        except (ValueError, TypeError):
            risk["circuit_breaker_until"] = None

    # Consecutive losses
    if risk["consecutive_losses"] >= config["max_consecutive_losses"]:
        cb_until = datetime.now(timezone.utc) + timedelta(minutes=config["circuit_breaker_minutes"])
        risk["circuit_breaker_until"] = cb_until.isoformat()
        risk["consecutive_losses"] = 0
        _save_state(state)
        return {"allowed": False, "reason": f"Circuit breaker triggered ({config['max_consecutive_losses']} consecutive losses)"}

    # Daily loss limit
    portfolio_val = get_portfolio_value(state)
    daily_loss_limit = portfolio_val * (config["max_daily_loss_pct"] / 100)
    if risk["daily_pnl"] < -daily_loss_limit:
        return {"allowed": False, "reason": f"Daily loss limit (${risk['daily_pnl']:.2f})"}

    # Drawdown kill switch
    peak = state["portfolio"]["peak_value"]
    if peak > 0:
        drawdown_pct = ((peak - portfolio_val) / peak) * 100
        if drawdown_pct >= config["kill_switch_drawdown_pct"]:
            risk["kill_switch_active"] = True
            risk["kill_switch_at"] = _now()
            _save_state(state)
            return {"allowed": False, "reason": f"Kill switch ({drawdown_pct:.1f}% drawdown)"}

    # Max concurrent positions
    open_count = len(state["positions"])
    if open_count >= config["max_concurrent_positions"]:
        return {"allowed": False, "reason": f"Max positions ({open_count}/{config['max_concurrent_positions']})"}

    # Max per strategy+underlying
    for pos in state["positions"].values():
        strat_key = f"{pos['strategy']}-{pos['underlying']}"
        count = sum(1 for p in state["positions"].values()
                    if f"{p['strategy']}-{p['underlying']}" == strat_key)
        if count >= config["max_per_strategy_per_underlying"]:
            pass  # checked at entry time, not here

    return {"allowed": True, "reason": "OK"}


# ── portfolio helpers ──

def get_portfolio_value(state: dict) -> float:
    """Cash + net position value."""
    total = state["portfolio"]["cash"]
    for pos in state["positions"].values():
        total += pos.get("current_value", 0)
    return total


def format_status(state: dict) -> dict:
    """Full status for reporting."""
    portfolio_val = get_portfolio_value(state)
    initial = state["portfolio"]["initial_capital"]
    total_pnl = portfolio_val - initial
    total_fees = sum(c.get("total_fees", 0) for c in state["completed"])
    total_trades = len(state["completed"])
    wins = sum(1 for c in state["completed"] if c.get("net_pnl", 0) > 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    # Exit reasons
    exit_reasons = {}
    for c in state["completed"]:
        reason_type = c.get("exit_reason", "unknown").split(":")[0]
        exit_reasons[reason_type] = exit_reasons.get(reason_type, 0) + 1

    return {
        "portfolio_value": round(portfolio_val, 2),
        "cash": round(state["portfolio"]["cash"], 2),
        "initial_capital": initial,
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "open_positions": len(state["positions"]),
        "exit_reasons": exit_reasons,
        "positions": list(state["positions"].values()),
        "strategies": state["strategies"],
        "risk": state["risk"],
    }


def format_report(state: dict) -> dict:
    """Detailed performance report."""
    status = format_status(state)

    # Per-strategy breakdown
    strat_perf = []
    for key, s in state["strategies"].items():
        total = s["total_trades"]
        wr = (s["winning_trades"] / total * 100) if total > 0 else 0
        strat_perf.append({
            "strategy": s["strategy"],
            "underlying": s["underlying"],
            "trades": total,
            "wins": s["winning_trades"],
            "losses": s["losing_trades"],
            "win_rate": round(wr, 1),
            "net_pnl": round(s["total_pnl"], 2),
            "fees": round(s["total_fees"], 2),
        })

    # Best trades
    best_trades = sorted(state["completed"], key=lambda c: c.get("net_pnl", 0), reverse=True)[:5]

    # Avg P&L by exit type
    exit_pnl = {}
    for c in state["completed"]:
        reason_type = c.get("exit_reason", "unknown").split(":")[0]
        if reason_type not in exit_pnl:
            exit_pnl[reason_type] = []
        exit_pnl[reason_type].append(c.get("net_pnl", 0))
    avg_exit_pnl = {k: round(sum(v) / len(v), 2) for k, v in exit_pnl.items()}

    return {
        **status,
        "strategy_performance": strat_perf,
        "best_trades": best_trades,
        "avg_pnl_by_exit_type": avg_exit_pnl,
    }


# ── CLI ──

def main():
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error": "Usage: options_trading_manager.py <command> [args]"}))
        sys.exit(1)

    cmd = args[0]
    state = _load_state()

    if cmd == "init":
        state = _default_state()
        _save_state(state)
        print(json.dumps({"success": True, "message": "Options trading state initialized",
                          "config": state["config"]}))

    elif cmd == "status":
        print(json.dumps(format_status(state), default=str))

    elif cmd == "report":
        print(json.dumps(format_report(state), default=str))

    elif cmd == "check_exits":
        iv_ranks = {}
        if len(args) > 1:
            try:
                iv_ranks = json.loads(args[1])
            except (json.JSONDecodeError, ValueError):
                pass
        exits = check_exits(state, iv_ranks)
        print(json.dumps({"exits": len(exits), "actions": exits}))

    elif cmd == "check_risk":
        result = check_can_trade(state)
        print(json.dumps(result))

    elif cmd == "open":
        if len(args) < 5:
            print(json.dumps({"error": "Usage: open <strategy> <underlying> <structure> <legs_json> [iv_rank] [total_credit]"}))
            sys.exit(1)
        strategy = args[1]
        underlying = args[2]
        structure = args[3]
        try:
            legs = json.loads(args[4])
        except (json.JSONDecodeError, ValueError):
            print(json.dumps({"error": "Invalid legs JSON"}))
            sys.exit(1)
        iv_rank = float(args[5]) if len(args) > 5 else 50.0
        total_credit = float(args[6]) if len(args) > 6 else 0.0

        # Risk check
        risk_result = check_can_trade(state)
        if not risk_result["allowed"]:
            print(json.dumps({"success": False, "reason": risk_result["reason"]}))
            return

        result = open_position(state, strategy, underlying, legs, total_credit, iv_rank, structure)
        print(json.dumps(result, default=str))

    elif cmd == "close":
        if len(args) < 3:
            print(json.dumps({"error": "Usage: close <position_id> <current_value> [reason]"}))
            sys.exit(1)
        pos_id = args[1]
        current_value = float(args[2])
        reason = args[3] if len(args) > 3 else "manual"
        result = close_position(state, pos_id, current_value, reason)
        print(json.dumps(result, default=str))

    elif cmd == "reset_kill_switch":
        state["risk"]["kill_switch_active"] = False
        state["risk"]["kill_switch_at"] = None
        state["risk"]["consecutive_losses"] = 0
        _save_state(state)
        print(json.dumps({"success": True, "message": "Kill switch reset"}))

    elif cmd == "best":
        strategies = []
        for key, s in state["strategies"].items():
            if s["total_trades"] >= 3:
                wr = (s["winning_trades"] / s["total_trades"] * 100) if s["total_trades"] > 0 else 0
                strategies.append({
                    "strategy": s["strategy"],
                    "underlying": s["underlying"],
                    "trades": s["total_trades"],
                    "win_rate": round(wr, 1),
                    "net_pnl": round(s["total_pnl"], 2),
                    "verdict": "PROFITABLE" if s["total_pnl"] > 0 else "NEEDS WORK",
                })
        strategies.sort(key=lambda x: x["net_pnl"], reverse=True)
        print(json.dumps({"best_strategies": strategies}))

    else:
        print(json.dumps({"error": f"Unknown command: {cmd}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
