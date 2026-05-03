"""
signal_trading/risk.py — Risk engine.

Checks before opening a trade:
  1. Portfolio kill switch (global halt)
  2. Per-algo circuit breaker (consecutive losses)
  3. Concurrent position limit
  4. Capital availability

Position sizing: fixed USD per trade (configurable in config.py).
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple

from signal_trading.config import RISK_CFG, RiskConfig
from signal_trading.models import AlgoState, PortfolioRisk, Trade, Direction

logger = logging.getLogger("signal_trading.risk")


# ── Portfolio kill switch ─────────────────────────────────────────────────────

def update_portfolio_risk(
    risk: PortfolioRisk,
    algo_states: Dict[str, AlgoState],
    open_trades: List[Trade],
    current_prices: Dict[str, float],
    cfg: RiskConfig = RISK_CFG,
) -> PortfolioRisk:
    """Recompute portfolio drawdown and check kill switch threshold."""
    total_value = 0.0
    total_initial = 0.0

    for state in algo_states.values():
        # Available capital
        total_value += state.available_capital
        total_initial += state.initial_capital

    # Add mark-to-market of open trades
    for trade in open_trades:
        price = current_prices.get(trade.asset, 0)
        if price > 0:
            total_value += trade.size_usd + trade.unrealized_pnl(price)
        else:
            total_value += trade.size_usd  # use cost basis if no price

    if total_initial <= 0:
        return risk

    # Update peak
    if total_value > risk.peak_value:
        risk.peak_value = total_value

    if risk.peak_value > 0:
        dd = (risk.peak_value - total_value) / risk.peak_value * 100
        risk.current_drawdown_pct = round(dd, 2)

        if dd >= cfg.portfolio_kill_switch_pct and not risk.kill_switch_active:
            risk.kill_switch_active = True
            risk.kill_switch_at = datetime.now(timezone.utc)
            logger.warning(
                "KILL SWITCH TRIGGERED: drawdown %.1f%% >= %.1f%%",
                dd, cfg.portfolio_kill_switch_pct,
            )

    return risk


# ── Trade approval ────────────────────────────────────────────────────────────

def check_can_open(
    asset: str,
    strategy: str,
    algo_states: Dict[str, AlgoState],
    portfolio_risk: PortfolioRisk,
    open_trades: List[Trade],
    cfg: RiskConfig = RISK_CFG,
    safety_stage=None,
) -> Tuple[bool, str]:
    """
    Gate check before opening a new trade.
    Returns (allowed: bool, reason: str).

    safety_stage: optional RiskStage from the daily safety layer.
    When None (default), the safety check is skipped for backward compatibility.
    """
    # 1. Portfolio kill switch
    if portfolio_risk.kill_switch_active:
        return False, f"portfolio kill switch active since {portfolio_risk.kill_switch_at}"

    # 1.5 Daily safety stage
    if safety_stage is not None:
        from signal_trading.safety import check_safety_stage
        allowed, reason = check_safety_stage(safety_stage)
        if not allowed:
            return False, reason

    # 2. Per-algo circuit breaker
    key = f"{strategy}-{asset}"
    state = algo_states.get(key)
    if state is None:
        return False, f"algo {key} not initialised"

    if state.circuit_breaker_active:
        return False, f"circuit breaker active until {state.circuit_breaker_until}"

    # 3. Concurrent position limit (per asset, across all strategies)
    asset_open = sum(1 for t in open_trades if t.asset == asset)
    if asset_open >= cfg.max_concurrent_per_asset:
        return False, f"max concurrent trades for {asset}: {asset_open}/{cfg.max_concurrent_per_asset}"

    # 4. Capital
    trade_cost = cfg.trade_size_usd * (1 + cfg.fee_pct)
    if state.available_capital < trade_cost:
        return False, f"insufficient capital: ${state.available_capital:.2f} < ${trade_cost:.2f}"

    return True, "ok"


# ── Post-trade updates ────────────────────────────────────────────────────────

def record_trade_result(
    state: AlgoState,
    trade: Trade,
    cfg: RiskConfig = RISK_CFG,
) -> AlgoState:
    """
    Update AlgoState after a trade closes.
    Triggers circuit breaker if consecutive_losses threshold hit.
    """
    state.total_trades += 1
    state.total_pnl = round(state.total_pnl + trade.net_pnl, 4)
    state.total_fees = round(state.total_fees + trade.entry_fee + trade.exit_fee, 4)
    state.available_capital += trade.size_usd + trade.net_pnl

    if trade.net_pnl >= 0:
        state.winning_trades += 1
        state.consecutive_losses = 0
    else:
        state.losing_trades += 1
        state.consecutive_losses += 1
        if state.consecutive_losses >= cfg.circuit_breaker_losses:
            state.circuit_breaker_until = (
                datetime.now(timezone.utc)
                + timedelta(hours=cfg.circuit_breaker_pause_hours)
            )
            logger.warning(
                "CIRCUIT BREAKER: %s — %d consecutive losses, paused until %s",
                state.key, state.consecutive_losses, state.circuit_breaker_until,
            )

    # Update peak capital
    total_cap = state.available_capital + len(state.active_trade_ids) * cfg.trade_size_usd
    state.peak_capital = max(state.peak_capital, total_cap)

    return state


def deduct_for_entry(state: AlgoState, trade: Trade, cfg: RiskConfig = RISK_CFG) -> AlgoState:
    """Deduct trade cost from available capital when opening a trade."""
    cost = trade.size_usd + trade.entry_fee
    state.available_capital -= cost
    state.active_trade_ids.append(trade.id)
    return state
