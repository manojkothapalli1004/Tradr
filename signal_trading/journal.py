"""
signal_trading/journal.py — Trade journal, state persistence, and reporting.

Handles:
- Atomic state load/save (crash-safe temp+rename)
- AlgoState initialisation for all asset×strategy pairs
- Performance stats: win rate, avg P&L, exit reason breakdown
- Summary report string for logging
"""

import json
import os
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from signal_trading.config import (
    ASSETS, ACTIVE_STRATEGIES, ASSET_STRATEGY_EXCLUSIONS,
    RISK_CFG, STATE_FILE,
)
from signal_trading.models import (
    AlgoState, Trade, PortfolioRisk, RegimeSnapshot,
)

logger = logging.getLogger("signal_trading.journal")

MAX_COMPLETED_TRADES = 1000   # cap completed list to avoid unbounded growth


# ── State schema ──────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "version": 1,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "algos": {},
        "open_trades": [],
        "completed_trades": [],
        "portfolio_risk": PortfolioRisk().to_dict(),
        "last_regimes": {},
        "safety_state": {},
    }


# ── Persistence ───────────────────────────────────────────────────────────────

def load_state(path: str = STATE_FILE) -> dict:
    """Load state from JSON file. Returns empty state if file missing or corrupt."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return _empty_state()
    try:
        with open(path) as f:
            state = json.load(f)
        # Migrate: add portfolio_risk if missing (v0 → v1)
        state.setdefault("portfolio_risk", PortfolioRisk().to_dict())
        state.setdefault("last_regimes", {})
        state.setdefault("open_trades", [])
        state.setdefault("completed_trades", [])
        state.setdefault("algos", {})
        state.setdefault("safety_state", {})
        return state
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("State file corrupt (%s), starting fresh: %s", path, exc)
        return _empty_state()


def save_state(state: dict, path: str = STATE_FILE):
    """Atomic write: write to .tmp then rename."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)


# ── AlgoState helpers ─────────────────────────────────────────────────────────

def ensure_algos_initialised(state: dict) -> dict:
    """
    Make sure every asset×strategy pair has an AlgoState entry.
    Safe to call on every startup — skips existing entries.
    """
    for asset in ASSETS:
        excluded = ASSET_STRATEGY_EXCLUSIONS.get(asset, set())
        for strategy in ACTIVE_STRATEGIES:
            if strategy in excluded:
                continue
            key = f"{strategy}-{asset}"
            if key not in state["algos"]:
                algo = AlgoState(
                    strategy=strategy,
                    asset=asset,
                    initial_capital=RISK_CFG.initial_capital_per_asset,
                    available_capital=RISK_CFG.initial_capital_per_asset,
                    peak_capital=RISK_CFG.initial_capital_per_asset,
                )
                state["algos"][key] = algo.to_dict()
                logger.debug("Initialised algo: %s", key)
    return state


def get_algo_states(state: dict) -> Dict[str, AlgoState]:
    """Deserialise all AlgoState objects from raw state dict."""
    return {k: AlgoState.from_dict(v) for k, v in state["algos"].items()}


def put_algo_states(state: dict, algos: Dict[str, AlgoState]) -> dict:
    """Serialise AlgoState objects back into state dict."""
    state["algos"] = {k: v.to_dict() for k, v in algos.items()}
    return state


def get_open_trades(state: dict) -> List[Trade]:
    return [Trade.from_dict(t) for t in state.get("open_trades", [])]


def get_completed_trades(state: dict) -> List[Trade]:
    return [Trade.from_dict(t) for t in state.get("completed_trades", [])]


def put_open_trades(state: dict, trades: List[Trade]) -> dict:
    state["open_trades"] = [t.to_dict() for t in trades]
    return state


def add_completed_trade(state: dict, trade: Trade) -> dict:
    state.setdefault("completed_trades", []).append(trade.to_dict())
    # Trim to cap
    if len(state["completed_trades"]) > MAX_COMPLETED_TRADES:
        state["completed_trades"] = state["completed_trades"][-MAX_COMPLETED_TRADES:]
    return state


# ── Reporting ─────────────────────────────────────────────────────────────────

def performance_summary(state: dict) -> dict:
    """Compute aggregate performance stats across all algos."""
    algos = get_algo_states(state)
    completed = get_completed_trades(state)
    open_trades = get_open_trades(state)
    pr = PortfolioRisk.from_dict(state.get("portfolio_risk", {}))

    total_pnl = sum(a.total_pnl for a in algos.values())
    total_fees = sum(a.total_fees for a in algos.values())
    total_trades = sum(a.total_trades for a in algos.values())
    total_wins = sum(a.winning_trades for a in algos.values())
    total_capital = sum(a.initial_capital for a in algos.values())
    total_value = sum(a.available_capital + len(a.active_trade_ids) * RISK_CFG.trade_size_usd
                      for a in algos.values())

    win_rate = total_wins / total_trades * 100 if total_trades > 0 else 0.0
    avg_hold = (sum(t.hold_minutes for t in completed) / len(completed)
                if completed else 0.0)

    # Exit reason breakdown
    exit_counts: Dict[str, int] = {}
    for t in completed:
        k = t.exit_reason.value if t.exit_reason else "unknown"
        exit_counts[k] = exit_counts.get(k, 0) + 1

    # Per-asset P&L
    asset_pnl: Dict[str, float] = {}
    for a in algos.values():
        asset_pnl[a.asset] = round(asset_pnl.get(a.asset, 0.0) + a.total_pnl, 2)

    return {
        "total_capital": round(total_capital, 2),
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 4),
        "total_trades": total_trades,
        "open_trades": len(open_trades),
        "win_rate_pct": round(win_rate, 1),
        "avg_hold_minutes": round(avg_hold, 0),
        "exit_reasons": exit_counts,
        "asset_pnl": asset_pnl,
        "portfolio_drawdown_pct": pr.current_drawdown_pct,
        "kill_switch_active": pr.kill_switch_active,
        "circuit_breakers_active": sum(
            1 for a in algos.values() if a.circuit_breaker_active
        ),
        "safety_stage": state.get("safety_state", {}).get("current_stage", "normal"),
        "daily_drawdown_pct": state.get("safety_state", {}).get("daily_drawdown_pct", 0.0),
    }


def format_status_report(state: dict, current_prices: Optional[Dict[str, float]] = None) -> str:
    """Return a multi-line status string suitable for logging."""
    summary = performance_summary(state)
    lines = [
        "=" * 70,
        "SIGNAL TRADING STATUS",
        f"  Portfolio: ${summary['total_capital']:,.0f} → ${summary['total_value']:,.2f}",
        f"  Net P&L: ${summary['total_pnl']:+.2f} | Fees: ${summary['total_fees']:.2f}",
        f"  Trades: {summary['total_trades']} closed, {summary['open_trades']} open",
        f"  Win rate: {summary['win_rate_pct']:.1f}% | Avg hold: {summary['avg_hold_minutes']:.0f}m",
        f"  Drawdown: {summary['portfolio_drawdown_pct']:.1f}%"
        + (" ⚠ KILL SWITCH" if summary["kill_switch_active"] else ""),
    ]

    safety_stage = summary.get("safety_stage", "normal")
    if safety_stage != "normal":
        lines.append(
            f"  Safety: {safety_stage.upper()} (daily dd: {summary.get('daily_drawdown_pct', 0):.1f}%)"
        )

    if summary["exit_reasons"]:
        lines.append("  Exit reasons: " + " | ".join(
            f"{k}={v}" for k, v in sorted(summary["exit_reasons"].items())
        ))

    if summary["asset_pnl"]:
        lines.append("  By asset: " + " | ".join(
            f"{a}=${p:+.2f}" for a, p in sorted(summary["asset_pnl"].items())
        ))

    if summary["circuit_breakers_active"]:
        lines.append(f"  ⚠ Circuit breakers: {summary['circuit_breakers_active']} active")

    # Active trades
    open_trades = get_open_trades(state)
    if open_trades:
        lines.append("-" * 70)
        lines.append(f"OPEN TRADES ({len(open_trades)}):")
        for t in open_trades:
            price = current_prices.get(t.asset, 0) if current_prices else 0
            upnl = t.unrealized_pnl(price) if price else 0
            upnl_pct = t.current_pnl_pct(price) if price else 0
            entry_time = t.entry_time
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            hold_m = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
            lines.append(
                f"  {t.id:30} | {t.direction.value.upper():5} @{t.entry_price:>10.2f}"
                f" | {hold_m:>5.0f}m | unrealized: ${upnl:+.2f} ({upnl_pct:+.2f}%)"
            )

    lines.append("=" * 70)
    return "\n".join(lines)
