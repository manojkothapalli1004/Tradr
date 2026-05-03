"""
signal_trading/execution.py — Paper execution engine.

Handles:
- Trade open (fill simulation with adverse, volatility-sensitive slippage)
- Stop checks: hard stop, take profit, trailing stop, time limit
- Trade close with P&L calculation
- Gap-aware stop fills: when price gaps through a stop level, fill is
  at the gap price (worse than the stop), not at the stop level.

Slippage model assumptions:
1. ALWAYS ADVERSE: buys fill above mid, sells fill below mid.
2. VOLATILITY-SENSITIVE: when ATR/price is high, slippage increases.
3. SIZE-SENSITIVE (optional): larger trades get worse fills (sqrt model).
4. RANDOM JITTER: small adverse-only noise models execution timing variance.

No subprocess calls, no shared state files — all in-memory.
Caller (runner.py) is responsible for persistence.
"""

import logging
import math
import random
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from signal_trading.config import RISK_CFG, EXEC_CFG, RiskConfig, ExecutionConfig
from signal_trading.models import (
    Trade, Direction, ExitReason, Signal, new_trade_id,
)

logger = logging.getLogger("signal_trading.execution")


# ── Fill price ────────────────────────────────────────────────────────────────

def _fill_price(
    market_price: float,
    direction: Direction,
    exec_cfg: ExecutionConfig,
    atr: float = 0.0,
    order_usd: float = 0.0,
) -> float:
    """
    Compute an adverse fill price for paper trading.

    Always worse than mid for the trader:
    - LONG entries / SHORT exits fill ABOVE mid
    - SHORT entries / LONG exits fill BELOW mid

    Args:
        market_price: current mid price
        direction: Direction.LONG = buying, Direction.SHORT = selling
        exec_cfg: execution parameters (base_bps, vol scaling, etc.)
        atr: recent ATR for the symbol (0 = unknown, base slippage only)
        order_usd: notional order value in USD (0 = skip size component)
    """
    if market_price <= 0:
        return market_price

    # 1. Base adverse slippage
    total_bps = exec_cfg.base_slippage_bps

    # 2. Volatility component
    if atr > 0 and market_price > 0:
        atr_ratio = atr / market_price
        excess = atr_ratio - exec_cfg.vol_calm_atr_ratio
        if excess > 0 and exec_cfg.vol_calm_atr_ratio > 0:
            total_bps += exec_cfg.vol_bps_per_unit * (excess / exec_cfg.vol_calm_atr_ratio)

    # 3. Size component (sqrt model, optional)
    if exec_cfg.size_bps_per_sqrt > 0 and order_usd > 0 and exec_cfg.size_ref_usd > 0:
        total_bps += exec_cfg.size_bps_per_sqrt * math.sqrt(order_usd / exec_cfg.size_ref_usd)

    # 4. Random jitter (always adverse)
    if exec_cfg.jitter_bps > 0:
        total_bps += random.random() * exec_cfg.jitter_bps

    slip = total_bps / 10_000

    if direction == Direction.LONG:
        return market_price * (1 + slip)  # buys fill above mid
    return market_price * (1 - slip)      # sells fill below mid


def _gap_fill_price(
    stop_price: float,
    gap_price: float,
    exit_direction: Direction,
    exec_cfg: ExecutionConfig,
    atr: float = 0.0,
    order_usd: float = 0.0,
) -> float:
    """
    Compute fill price when a stop is triggered by a gap-through.

    In real markets, stop-loss orders become market orders when triggered.
    If price gaps past the stop, you fill at the gap price, not at your stop.
    This returns the worse of (stop_level, gap_price) plus slippage.

    exit_direction: the direction of the exit order
        - closing a LONG = selling = Direction.SHORT
        - closing a SHORT = buying = Direction.LONG
    """
    if exit_direction == Direction.LONG:
        # Closing a short — higher is worse
        base = max(stop_price, gap_price)
    else:
        # Closing a long — lower is worse
        base = min(stop_price, gap_price)

    return _fill_price(base, exit_direction, exec_cfg, atr, order_usd)


# ── Open ─────────────────────────────────────────────────────────────────────

def open_trade(
    signal: Signal,
    risk_cfg: RiskConfig = RISK_CFG,
    exec_cfg: ExecutionConfig = EXEC_CFG,
) -> Trade:
    """
    Create a new Trade from a Signal.
    Does NOT check risk gates — caller must call risk.check_can_open first.
    """
    # Extract ATR from signal indicators if available
    atr = _extract_atr(signal.indicators)

    fill = _fill_price(
        signal.price, signal.direction, exec_cfg,
        atr=atr, order_usd=risk_cfg.trade_size_usd,
    )
    entry_fee = risk_cfg.trade_size_usd * risk_cfg.fee_pct

    trade = Trade(
        id=new_trade_id(signal.strategy, signal.asset),
        asset=signal.asset,
        strategy=signal.strategy,
        direction=signal.direction,
        entry_price=fill,
        entry_time=signal.timestamp,
        size_usd=risk_cfg.trade_size_usd,
        entry_fee=entry_fee,
        highest_price=fill,
        lowest_price=fill,
    )

    logger.info(
        "OPEN %s %s %s @%.4f (mid=%.4f) size=$%.0f fee=$%.4f",
        trade.id, trade.asset, trade.direction.value,
        fill, signal.price, risk_cfg.trade_size_usd, entry_fee,
    )
    return trade


# ── Stop checks ───────────────────────────────────────────────────────────────

def update_trade_prices(trade: Trade, current_price: float) -> Trade:
    """Track peak/trough prices for trailing stop calculation."""
    if trade.direction == Direction.LONG:
        trade.highest_price = max(trade.highest_price, current_price)
    else:
        trade.lowest_price = min(trade.lowest_price, current_price)
    return trade


def check_exit_conditions(
    trade: Trade,
    current_price: float,
    risk_cfg: RiskConfig = RISK_CFG,
) -> Optional[ExitReason]:
    """
    Check all exit conditions in priority order.
    Returns ExitReason if trade should close, None to stay open.
    """
    if trade.direction == Direction.LONG:
        pnl_pct = (current_price - trade.entry_price) / trade.entry_price * 100
    else:
        pnl_pct = (trade.entry_price - current_price) / trade.entry_price * 100

    # 1. Hard stop loss
    if pnl_pct <= -risk_cfg.stop_loss_pct:
        return ExitReason.STOP_LOSS

    # 2. Take profit
    if pnl_pct >= risk_cfg.take_profit_pct:
        return ExitReason.TAKE_PROFIT

    # 3. Trailing stop (only fires once trade is profitable)
    if trade.direction == Direction.LONG:
        if trade.highest_price > trade.entry_price:
            trailing_stop = trade.highest_price * (1 - risk_cfg.trailing_stop_pct / 100)
            if current_price <= trailing_stop:
                return ExitReason.TRAILING_STOP
    else:
        if trade.lowest_price < trade.entry_price:
            trailing_stop = trade.lowest_price * (1 + risk_cfg.trailing_stop_pct / 100)
            if current_price >= trailing_stop:
                return ExitReason.TRAILING_STOP

    # 4. Time limit
    now = datetime.now(timezone.utc)
    entry_time = trade.entry_time
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)
    hold_hours = (now - entry_time).total_seconds() / 3600
    if hold_hours >= risk_cfg.max_hold_hours:
        return ExitReason.TIME_LIMIT

    return None


# ── Close ─────────────────────────────────────────────────────────────────────

def close_trade(
    trade: Trade,
    exit_price: float,
    reason: ExitReason,
    risk_cfg: RiskConfig = RISK_CFG,
    exec_cfg: ExecutionConfig = EXEC_CFG,
    atr: float = 0.0,
) -> Trade:
    """
    Finalise a trade. Calculates P&L and stamps exit fields.

    For stop-loss exits, uses gap-aware fill: if the observed price has
    gapped through the stop level, the fill is at the gap price (worse),
    not at the stop level.

    Returns the same Trade object (mutated).
    """
    # Determine exit direction (opposite of trade direction)
    exit_direction = Direction.SHORT if trade.direction == Direction.LONG else Direction.LONG

    # Gap-aware fill for stop exits
    if reason == ExitReason.STOP_LOSS:
        # Compute where the stop level was
        if trade.direction == Direction.LONG:
            stop_level = trade.entry_price * (1 - risk_cfg.stop_loss_pct / 100)
        else:
            stop_level = trade.entry_price * (1 + risk_cfg.stop_loss_pct / 100)
        fill = _gap_fill_price(
            stop_level, exit_price, exit_direction, exec_cfg,
            atr=atr, order_usd=trade.size_usd,
        )
    else:
        fill = _fill_price(
            exit_price, exit_direction, exec_cfg,
            atr=atr, order_usd=trade.size_usd,
        )

    exit_fee = trade.size_usd * risk_cfg.fee_pct

    if trade.direction == Direction.LONG:
        gross_pnl = (fill - trade.entry_price) / trade.entry_price * trade.size_usd
    else:
        gross_pnl = (trade.entry_price - fill) / trade.entry_price * trade.size_usd

    net_pnl = gross_pnl - exit_fee  # entry fee already deducted on open
    pnl_pct = net_pnl / trade.size_usd * 100

    now = datetime.now(timezone.utc)
    entry_time = trade.entry_time
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)

    trade.exit_price = fill
    trade.exit_time = now
    trade.exit_fee = exit_fee
    trade.exit_reason = reason
    trade.gross_pnl = round(gross_pnl, 4)
    trade.net_pnl = round(net_pnl, 4)
    trade.pnl_pct = round(pnl_pct, 4)
    trade.hold_minutes = round((now - entry_time).total_seconds() / 60, 1)

    sign = "+" if net_pnl >= 0 else ""
    logger.info(
        "CLOSE %s %s %s @%.4f (mid=%.4f) net_pnl=%s$%.2f (%.2f%%) hold=%.0fm reason=%s",
        trade.id, trade.asset, trade.direction.value,
        fill, exit_price, sign, net_pnl, pnl_pct, trade.hold_minutes, reason.value,
    )
    return trade


# ── Batch stop check ─────────────────────────────────────────────────────────

def check_all_stops(
    open_trades: List[Trade],
    current_prices: Dict[str, float],
    risk_cfg: RiskConfig = RISK_CFG,
) -> List[Tuple[Trade, ExitReason, float]]:
    """
    Check every open trade for exit conditions.
    Returns list of (trade, reason, exit_price) for trades to close.
    """
    to_close = []
    for trade in open_trades:
        price = current_prices.get(trade.asset, 0)
        if price <= 0:
            continue
        update_trade_prices(trade, price)
        reason = check_exit_conditions(trade, price, risk_cfg)
        if reason:
            to_close.append((trade, reason, price))
    return to_close


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_atr(indicators: dict) -> float:
    """Pull ATR from indicators dict. Returns 0 if not present."""
    if not indicators:
        return 0.0
    for key in ("atr", "ATR", "atr_14", "atr14"):
        val = indicators.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    return 0.0
