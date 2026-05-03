"""
options_bot/risk.py — Pre-trade risk checks and portfolio safety rules.

Pure logic — no I/O, no imports from execution or journal.

Pre-trade gate (check_can_open):
  1. Portfolio kill switch (global halt).
  2. Max total premium deployed cap.
  3. Per-slot circuit breaker.

Post-trade accounting (record_trade_result, deduct_for_entry):
  Updates AlgoSlot; triggers circuit breaker on consecutive losses.

Portfolio state update (update_portfolio_state):
  Recomputes total premium deployed and drawdown; triggers kill switch.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple

from options_bot.config import RISK_CFG, RiskConfig
from options_bot.models import AlgoSlot, OptionTrade, PortfolioState

logger = logging.getLogger("options_bot.risk")


# ── Portfolio state ──────────────────────────────────────────────────────────────

def update_portfolio_state(
    ps: PortfolioState,
    open_trades: List[OptionTrade],
    closed_pnl_delta: float = 0.0,
    cfg: RiskConfig = RISK_CFG,
) -> PortfolioState:
    """
    Update portfolio state after a trade closes or on each cycle.

    closed_pnl_delta: realized_pnl_usd of the trade that just closed.
    Pass 0.0 when called without a new close (e.g. after opening a trade).

    Equity model:
      cumulative_pnl_usd tracks the running sum of all realized net P&L.
      peak_equity_usd is the watermark, seeded at 0.0 on first call.

    Drawdown is measured as:
      (peak_equity_usd - cumulative_pnl_usd) / abs(peak_equity_usd)

    Seeding at 0.0 means:
      - A bot that starts with losses immediately shows a drawdown relative
        to the zero baseline, which is correct (no trades = no loss).
      - The kill switch can fire during an early losing streak because
        peak_equity_usd stays at 0.0 while cumulative_pnl_usd goes negative,
        producing a computable drawdown ratio.

    Edge case: if cumulative_pnl_usd is exactly 0.0 and peak_equity_usd is
    also 0.0 (no trades closed yet), drawdown is 0% and kill switch is idle.
    This is correct — the bot has not lost anything yet.
    """
    deployed = sum(t.entry_premium_total for t in open_trades if t.is_open)
    ps.total_premium_deployed_usd = round(deployed, 2)

    # Accumulate realized P&L
    ps.cumulative_pnl_usd = round(ps.cumulative_pnl_usd + closed_pnl_delta, 4)

    # Update equity peak watermark (remains at 0.0 if bot has only lost so far)
    if ps.cumulative_pnl_usd > ps.peak_equity_usd:
        ps.peak_equity_usd = ps.cumulative_pnl_usd

    # Compute drawdown relative to peak.
    # Use abs(peak) to handle the case where peak_equity_usd is 0.0 safely.
    # When both are 0.0 (no trades closed), drawdown is 0%.
    # When peak is 0.0 and cumulative is negative (early losses), we measure
    # loss as a fraction of total max_premium cap instead, since a 0-baseline
    # peak makes percentage division undefined.
    if ps.peak_equity_usd != 0.0:
        dd = max(0.0, (ps.peak_equity_usd - ps.cumulative_pnl_usd) / abs(ps.peak_equity_usd) * 100.0)
    elif ps.cumulative_pnl_usd < 0.0:
        # Peak is still 0 (no winning trades yet) but cumulative P&L is negative.
        # Use the max premium cap as a reference so the kill switch can still fire.
        reference = cfg.max_total_premium_deployed_usd
        dd = min(100.0, abs(ps.cumulative_pnl_usd) / reference * 100.0) if reference > 0 else 0.0
    else:
        dd = 0.0

    ps.current_drawdown_pct = round(dd, 2)

    if not ps.kill_switch_active and dd >= cfg.portfolio_kill_switch_pct:
        ps.kill_switch_active = True
        ps.kill_switch_at     = datetime.now(timezone.utc)
        logger.warning(
            "KILL SWITCH TRIGGERED: drawdown %.1f%% ≥ %.1f%% "
            "(peak_equity=$%.2f cumulative_pnl=$%.2f)",
            dd, cfg.portfolio_kill_switch_pct,
            ps.peak_equity_usd, ps.cumulative_pnl_usd,
        )

    return ps


# ── Pre-trade gate ───────────────────────────────────────────────────────────────

def check_can_open(
    symbol: str,
    strategy: str,
    slots: Dict[str, AlgoSlot],
    portfolio: PortfolioState,
    new_premium_usd: float,
    cfg: RiskConfig = RISK_CFG,
) -> Tuple[bool, str]:
    """
    Final risk gate before opening a position.
    Called after the router has already enforced position caps.

    Returns (allowed, reason_string).
    """
    # 1. Kill switch
    if portfolio.kill_switch_active:
        return False, f"kill switch active since {portfolio.kill_switch_at}"

    # 2. Premium cap
    projected = portfolio.total_premium_deployed_usd + new_premium_usd
    if projected > cfg.max_total_premium_deployed_usd:
        return False, (
            f"premium cap: adding ${new_premium_usd:.2f} would reach "
            f"${projected:.2f} > ${cfg.max_total_premium_deployed_usd:.2f}"
        )

    # 3. Circuit breaker
    key  = f"{strategy}-{symbol}"
    slot = slots.get(key)
    if slot is None:
        return False, f"slot {key} not initialised"
    if slot.circuit_breaker_active:
        return False, f"circuit breaker until {slot.circuit_breaker_until}"

    return True, "ok"


# ── Post-trade accounting ────────────────────────────────────────────────────────

def deduct_for_entry(slot: AlgoSlot, trade: OptionTrade) -> AlgoSlot:
    """Register that a trade has been opened on this slot."""
    slot.active_trade_ids.append(trade.id)
    return slot


def record_trade_result(
    slot: AlgoSlot,
    trade: OptionTrade,
    cfg: RiskConfig = RISK_CFG,
) -> AlgoSlot:
    """
    Update AlgoSlot after a trade closes.
    Triggers circuit breaker when consecutive loss threshold is reached.
    """
    slot.total_trades    += 1
    slot.total_pnl_usd    = round(slot.total_pnl_usd + trade.realized_pnl_usd, 4)
    slot.active_trade_ids = [i for i in slot.active_trade_ids if i != trade.id]

    if trade.realized_pnl_usd >= 0:
        slot.winning_trades    += 1
        slot.consecutive_losses = 0
    else:
        slot.losing_trades      += 1
        slot.consecutive_losses += 1
        if slot.consecutive_losses >= cfg.circuit_breaker_losses:
            slot.circuit_breaker_until = (
                datetime.now(timezone.utc)
                + timedelta(hours=cfg.circuit_breaker_pause_hours)
            )
            logger.warning(
                "CIRCUIT BREAKER: %s — %d consecutive losses, paused until %s",
                slot.key, slot.consecutive_losses, slot.circuit_breaker_until,
            )

    return slot
