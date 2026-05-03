"""
options_bot/execution.py — Paper fill simulator.

PAPER TRADING ONLY. No real orders are placed.

Fill model: "simplified_theoretical"
  Entry fill = estimated_premium × (1 + slippage_pct)   [buyer pays more]
  Exit fill  = current_mark     × (1 - slippage_pct)   [seller receives less]

Both fills are clearly labeled LIMITED in every log line and in every
OptionTrade.data_quality_note because:
  - estimated_premium comes from 15-min delayed chain data or Black-Scholes
  - current_mark during stop checks is the last stored delayed value
  - No live bid/ask is available via yfinance free tier

Responsibilities:
  - open_position()   — create a new OptionTrade from a signal + contract
  - close_position()  — stamp exit fields and compute realized P&L
  - update_mark()     — refresh unrealized P&L on an open trade
  - check_exit()      — evaluate stop-loss / profit-target / time / EOD conditions
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from options_bot.config import (
    CONTRACT_CFG, DATA_CFG, EXEC_CFG,
    ContractConfig, DataConfig, ExecConfig, OPTIONS_DATA_LIMITATION,
)
from options_bot.models import (
    ExitReason, OptionAction, OptionContract, OptionTrade,
    OptionsSignal, new_trade_id,
)

logger = logging.getLogger("options_bot.execution")


# ── Fill price helpers ───────────────────────────────────────────────────────────

def _entry_fill(mid: float, cfg: ExecConfig) -> float:
    """Buyer pays mid + slippage. Adverse to buyer — conservative assumption."""
    return round(mid * (1.0 + cfg.slippage_conservative_pct), 4)


def _exit_fill(mid: float, cfg: ExecConfig) -> float:
    """Seller receives mid - slippage. Adverse to seller — conservative assumption."""
    return round(max(0.0, mid * (1.0 - cfg.slippage_conservative_pct)), 4)


# ── Open ─────────────────────────────────────────────────────────────────────────

def open_position(
    signal: OptionsSignal,
    contract: OptionContract,
    exec_cfg: ExecConfig = EXEC_CFG,
) -> OptionTrade:
    """
    Simulate opening a paper option position.
    Caller must have already passed router and risk gates.

    [LIMITED-SIMULATOR] Fill price is theoretical. Based on delayed data.
    """
    fill    = _entry_fill(contract.estimated_premium, exec_cfg)
    total   = fill * 100          # 1 contract = 100 shares
    fee     = total * 0.0         # FEES ZEROED 2026-04-21 (temporary; restore to 0.001)

    trade_id = new_trade_id(signal.strategy_name.value, signal.symbol)
    now      = datetime.now(timezone.utc)

    data_note = (
        f"[LIMITED-SIMULATOR] entry fill is theoretical. "
        f"mid_estimate={contract.estimated_premium:.4f}/sh "
        f"slippage={exec_cfg.slippage_conservative_pct:.1%} "
        f"fill={fill:.4f}/sh total=${total:.2f} | {OPTIONS_DATA_LIMITATION}"
    )

    trade = OptionTrade(
        id=trade_id,
        symbol=signal.symbol,
        strategy=signal.strategy_name.value,
        option_type=contract.option_type,
        action=OptionAction.BUY,
        expiry=contract.expiry,
        strike=contract.strike,
        dte_at_entry=contract.dte,
        entry_time=now,
        entry_fill_per_share=fill,
        contracts=1,
        entry_fee_usd=fee,
        entry_delta=contract.estimated_delta,
        entry_iv=contract.estimated_iv,
        entry_premium_total=total,
        current_premium=fill,
        current_pnl_usd=0.0,
        current_pnl_pct=0.0,
        data_quality_note=data_note,
    )

    logger.info(
        "[PAPER OPEN] %s | %s %s K=%.2f exp=%s DTE=%d "
        "fill=%.4f/sh total=$%.2f delta=%.3f iv=%.3f",
        trade_id, signal.symbol, contract.option_type.value,
        contract.strike, contract.expiry, contract.dte,
        fill, total, contract.estimated_delta, contract.estimated_iv,
    )
    logger.info("[LIMITED-SIMULATOR] %s", OPTIONS_DATA_LIMITATION)
    return trade


# ── Exit condition check ─────────────────────────────────────────────────────────

_ET = ZoneInfo("America/New_York")


def _is_eod(data_cfg: DataConfig = DATA_CFG) -> bool:
    """
    True when:
      - the current day is a weekday, AND
      - the current ET time is within the trading session
        (>= market open AND < market close), AND
      - the current ET time is at or past the configured EOD force-flat time.

    The session bounds prevent this from returning True outside US hours
    (e.g. 19:45 ET after market close, or overnight).
    """
    now_et     = datetime.now(_ET)
    is_wday    = now_et.weekday() < 5
    et_minutes = now_et.hour * 60 + now_et.minute

    open_minutes  = data_cfg.market_open_hour  * 60 + data_cfg.market_open_minute
    close_minutes = data_cfg.market_close_hour * 60 + data_cfg.market_close_minute
    eod_minutes   = data_cfg.eod_close_hour    * 60 + data_cfg.eod_close_minute

    in_session = open_minutes <= et_minutes < close_minutes
    past_eod   = et_minutes >= eod_minutes

    return is_wday and in_session and past_eod


def check_exit(
    trade: OptionTrade,
    current_mark: float,
    contract_cfg: ContractConfig = CONTRACT_CFG,
) -> Optional[ExitReason]:
    """
    Evaluate all exit conditions in priority order.
    current_mark is per-share mid estimate (may be stale — labeled LIMITED).

    Priority:
      1. EOD flat (force-close before market close)
      2. Stop loss  (premium dropped ≥ stop_loss_pct of entry)
      3. Profit target (premium gained ≥ profit_target_pct)
      4. Time limit (held beyond max_hold_days)

    Returns ExitReason or None (stay open).
    """
    entry = trade.entry_fill_per_share
    if entry <= 0:
        return None

    # 1. EOD flat (force-close during session at or after configured eod time)
    if _is_eod():
        logger.info("[EOD_FLAT] %s — force-close at EOD", trade.id)
        return ExitReason.EOD_FLAT

    pnl_pct = (current_mark - entry) / entry * 100.0

    # 2. Stop loss
    if pnl_pct <= -contract_cfg.stop_loss_pct:
        return ExitReason.STOP_LOSS

    # 3. Profit target
    if pnl_pct >= contract_cfg.profit_target_pct:
        return ExitReason.PROFIT_TARGET

    # 4. Time limit (calendar days held)
    now        = datetime.now(timezone.utc)
    entry_time = trade.entry_time
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)
    hold_days  = (now - entry_time).total_seconds() / 86400.0
    if hold_days >= contract_cfg.max_hold_days:
        return ExitReason.TIME_LIMIT

    return None


# ── Close ─────────────────────────────────────────────────────────────────────────

def close_position(
    trade: OptionTrade,
    current_mark: float,
    reason: ExitReason,
    exec_cfg: ExecConfig = EXEC_CFG,
) -> OptionTrade:
    """
    Simulate closing a paper position. Mutates and returns the trade.

    [LIMITED-SIMULATOR] Exit fill is theoretical. Based on delayed/stale data.
    """
    fill      = _exit_fill(current_mark, exec_cfg)
    exit_fee  = fill * 100 * 0.0   # FEES ZEROED 2026-04-21 (temporary; restore to 0.001)
    gross_pnl = (fill - trade.entry_fill_per_share) * 100
    net_pnl   = gross_pnl - trade.entry_fee_usd - exit_fee
    pnl_pct   = net_pnl / trade.entry_premium_total * 100 if trade.entry_premium_total > 0 else 0.0

    now        = datetime.now(timezone.utc)
    entry_time = trade.entry_time
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)
    hold_days  = (now - entry_time).total_seconds() / 86400.0

    trade.exit_time            = now
    trade.exit_fill_per_share  = fill
    trade.exit_fee_usd         = exit_fee
    trade.exit_reason          = reason
    trade.realized_pnl_usd     = round(net_pnl, 4)
    trade.realized_pnl_pct     = round(pnl_pct, 4)
    trade.hold_days             = round(hold_days, 3)
    trade.current_premium       = fill
    trade.current_pnl_usd       = round(net_pnl, 4)
    trade.current_pnl_pct       = round(pnl_pct, 4)

    sign = "+" if net_pnl >= 0 else ""
    logger.info(
        "[PAPER CLOSE] %s | %s reason=%s exit_fill=%.4f "
        "net_pnl=%s$%.2f (%.2f%%) hold=%.2fd",
        trade.id, trade.symbol, reason.value,
        fill, sign, net_pnl, pnl_pct, hold_days,
    )
    logger.info("[LIMITED-SIMULATOR] %s", OPTIONS_DATA_LIMITATION)
    return trade


# ── Mark-to-market ────────────────────────────────────────────────────────────────

def update_mark(trade: OptionTrade, current_mark: float) -> OptionTrade:
    """
    Refresh unrealized P&L on an open trade.
    Does not touch any exit fields.
    current_mark is the latest available per-share estimate (may be stale).
    """
    if not trade.is_open:
        return trade
    gross   = (current_mark - trade.entry_fill_per_share) * 100
    net     = gross - trade.entry_fee_usd
    pnl_pct = net / trade.entry_premium_total * 100 if trade.entry_premium_total > 0 else 0.0

    trade.current_premium = current_mark
    trade.current_pnl_usd = round(net, 4)
    trade.current_pnl_pct = round(pnl_pct, 4)
    return trade
