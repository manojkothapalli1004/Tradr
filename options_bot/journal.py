"""
options_bot/journal.py — Atomic state persistence and reporting.

Writes ONLY to options_bot/state.json. Never touches any other file.
Crash-safe: writes to .tmp then os.replace() (atomic on POSIX).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from options_bot.config import ASSETS, STATE_FILE
from options_bot.models import AlgoSlot, OptionTrade, PortfolioState
from options_bot.strategies import ALL_STRATEGIES

logger = logging.getLogger("options_bot.journal")

MAX_COMPLETED = 500   # rolling cap to prevent unbounded state file growth


# ── State schema ─────────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {
        "version":          1,
        "start_time":       datetime.now(timezone.utc).isoformat(),
        "algos":            {},
        "open_trades":      [],
        "completed_trades": [],
        "portfolio":        PortfolioState().to_dict(),
        "last_regimes":     {},
    }


# ── Load / save ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE) or os.path.getsize(STATE_FILE) == 0:
        return _empty()
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        s.setdefault("portfolio",        PortfolioState().to_dict())
        s.setdefault("last_regimes",     {})
        s.setdefault("open_trades",      [])
        s.setdefault("completed_trades", [])
        s.setdefault("algos",            {})
        return s
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("state file corrupt — starting fresh: %s", exc)
        return _empty()


def save_state(state: dict) -> None:
    """Atomic write via tmp + os.replace()."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


# ── Slot helpers ─────────────────────────────────────────────────────────────────

def ensure_slots(state: dict) -> dict:
    """
    Create missing AlgoSlot entries for every strategy × symbol pair.
    Idempotent — safe to call on every startup.
    """
    for cls in ALL_STRATEGIES:
        for symbol in ASSETS:
            key = f"{cls.strategy_id}-{symbol}"
            if key not in state["algos"]:
                state["algos"][key] = AlgoSlot(
                    strategy=cls.strategy_id, symbol=symbol
                ).to_dict()
    return state


def get_slots(state: dict) -> Dict[str, AlgoSlot]:
    return {k: AlgoSlot.from_dict(v) for k, v in state["algos"].items()}


def put_slots(state: dict, slots: Dict[str, AlgoSlot]) -> dict:
    state["algos"] = {k: v.to_dict() for k, v in slots.items()}
    return state


# ── Trade helpers ────────────────────────────────────────────────────────────────

def get_open_trades(state: dict) -> List[OptionTrade]:
    return [OptionTrade.from_dict(t) for t in state.get("open_trades", [])]


def put_open_trades(state: dict, trades: List[OptionTrade]) -> dict:
    state["open_trades"] = [t.to_dict() for t in trades]
    return state


def add_completed(state: dict, trade: OptionTrade) -> dict:
    state.setdefault("completed_trades", []).append(trade.to_dict())
    if len(state["completed_trades"]) > MAX_COMPLETED:
        state["completed_trades"] = state["completed_trades"][-MAX_COMPLETED:]
    return state


def get_portfolio(state: dict) -> PortfolioState:
    return PortfolioState.from_dict(state.get("portfolio", {}))


def put_portfolio(state: dict, ps: PortfolioState) -> dict:
    state["portfolio"] = ps.to_dict()
    return state


# ── Reporting ─────────────────────────────────────────────────────────────────────

def performance_summary(state: dict) -> dict:
    slots     = get_slots(state)
    completed = [OptionTrade.from_dict(t) for t in state.get("completed_trades", [])]
    open_tr   = get_open_trades(state)
    ps        = get_portfolio(state)

    total_tr  = sum(s.total_trades    for s in slots.values())
    total_win = sum(s.winning_trades  for s in slots.values())
    total_pnl = sum(s.total_pnl_usd   for s in slots.values())
    win_rate  = total_win / total_tr * 100 if total_tr else 0.0
    avg_hold  = sum(t.hold_days for t in completed) / len(completed) if completed else 0.0

    exits: Dict[str, int] = {}
    for t in completed:
        k = t.exit_reason.value if t.exit_reason else "unknown"
        exits[k] = exits.get(k, 0) + 1

    sym_pnl: Dict[str, float] = {}
    for s in slots.values():
        sym_pnl[s.symbol] = round(sym_pnl.get(s.symbol, 0.0) + s.total_pnl_usd, 2)

    return {
        "total_trades":           total_tr,
        "open_trades":            len(open_tr),
        "total_pnl_usd":          round(total_pnl, 2),
        "win_rate_pct":           round(win_rate, 1),
        "avg_hold_days":          round(avg_hold, 3),
        "exit_reasons":           exits,
        "symbol_pnl":             sym_pnl,
        "premium_deployed_usd":   ps.total_premium_deployed_usd,
        "portfolio_drawdown_pct": ps.current_drawdown_pct,
        "kill_switch_active":     ps.kill_switch_active,
        "circuit_breakers":       sum(1 for s in slots.values() if s.circuit_breaker_active),
    }


def format_status(state: dict) -> str:
    s   = performance_summary(state)
    ot  = get_open_trades(state)
    now = datetime.now(timezone.utc)

    lines = [
        "=" * 70,
        "OPTIONS BOT  [PAPER TRADING — ALL FILLS ARE THEORETICAL/LIMITED]",
        f"  Closed: {s['total_trades']}  Open: {s['open_trades']}  "
        f"P&L: ${s['total_pnl_usd']:+.2f}  Win: {s['win_rate_pct']:.1f}%",
        f"  Avg hold: {s['avg_hold_days']:.2f}d  "
        f"Deployed: ${s['premium_deployed_usd']:.2f}  "
        f"Drawdown: {s['portfolio_drawdown_pct']:.1f}%"
        + ("  *** KILL SWITCH ***" if s["kill_switch_active"] else ""),
    ]
    if s["circuit_breakers"]:
        lines.append(f"  Circuit breakers active: {s['circuit_breakers']}")
    if s["exit_reasons"]:
        lines.append("  Exits: " + "  ".join(
            f"{k}={v}" for k, v in sorted(s["exit_reasons"].items())
        ))
    if s["symbol_pnl"]:
        lines.append("  By symbol: " + "  ".join(
            f"{sym}=${p:+.2f}" for sym, p in sorted(s["symbol_pnl"].items())
        ))
    if ot:
        lines.append(f"  Open positions ({len(ot)}):")
        for t in ot:
            et = t.entry_time.replace(tzinfo=timezone.utc) if t.entry_time.tzinfo is None else t.entry_time
            h  = (now - et).total_seconds() / 3600
            lines.append(
                f"    {t.id} | {t.symbol} {t.option_type.value.upper()} "
                f"K={t.strike:.0f} exp={t.expiry} | "
                f"entry=${t.entry_fill_per_share:.4f} | "
                f"pnl=${t.current_pnl_usd:+.2f} ({t.current_pnl_pct:+.1f}%) | "
                f"{h:.1f}h"
            )
    lines.append("=" * 70)
    return "\n".join(lines)
