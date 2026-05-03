"""
signal_trading/safety.py — Staged portfolio safety layer.

Tracks daily drawdown and maps it to four risk stages:
  NORMAL        — full trading
  REDUCED_RISK  — trades at reduced size
  NO_NEW_RISK   — no new entries, exits still process
  HARD_LOCKED   — no entries, persistent lock file, manual reset required

Stages only escalate within a day (no intraday de-escalation).
NORMAL / REDUCED_RISK / NO_NEW_RISK reset automatically on a new UTC day.
HARD_LOCKED persists until the operator deletes the HARD_LOCK file.

This module is additive — the existing all-time drawdown kill switch is untouched.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from signal_trading.config import (
    RISK_CFG, RiskConfig,
    SAFETY_CFG, PortfolioSafetyConfig,
    HARD_LOCK_FILE,
)
from signal_trading.models import AlgoState, RiskStage, Trade

logger = logging.getLogger("signal_trading.safety")


# ── Stage severity (for escalation-only enforcement) ─────────────────────────

_STAGE_SEVERITY = {
    RiskStage.NORMAL: 0,
    RiskStage.REDUCED_RISK: 1,
    RiskStage.NO_NEW_RISK: 2,
    RiskStage.HARD_LOCKED: 3,
}


# ── Portfolio value computation ──────────────────────────────────────────────

def _compute_portfolio_value(
    algo_states: Dict[str, AlgoState],
    open_trades: List[Trade],
    current_prices: Dict[str, float],
) -> float:
    """Compute current total portfolio value (available capital + mark-to-market)."""
    total = 0.0
    for state in algo_states.values():
        total += state.available_capital
    for trade in open_trades:
        price = current_prices.get(trade.asset, 0)
        if price > 0:
            total += trade.size_usd + trade.unrealized_pnl(price)
        else:
            total += trade.size_usd
    return total


# ── HARD_LOCK file management ────────────────────────────────────────────────

def _hard_lock_file_exists() -> bool:
    return os.path.exists(HARD_LOCK_FILE)


def _write_hard_lock_file(daily_drawdown_pct: float) -> None:
    """Write HARD_LOCK marker file (atomic tmp+rename)."""
    content = {
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "daily_drawdown_pct": round(daily_drawdown_pct, 2),
        "reset_instructions": "Delete this file to unlock trading on next cycle.",
    }
    tmp = HARD_LOCK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(content, f, indent=2)
    os.replace(tmp, HARD_LOCK_FILE)


# ── Safety state helpers ─────────────────────────────────────────────────────

def _init_safety_state(current_value: float) -> dict:
    """Create a fresh safety state dict for a new day or first run."""
    return {
        "day_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "day_start_value": current_value,
        "daily_drawdown_pct": 0.0,
        "current_stage": RiskStage.NORMAL.value,
        "last_transition_time": None,
        "last_transition_from": None,
    }


def _stage_from_thresholds(
    daily_dd_pct: float,
    cfg: PortfolioSafetyConfig,
) -> RiskStage:
    """Determine stage from daily drawdown (highest matching wins)."""
    if daily_dd_pct >= cfg.hard_lock_pct:
        return RiskStage.HARD_LOCKED
    if daily_dd_pct >= cfg.no_new_risk_pct:
        return RiskStage.NO_NEW_RISK
    if daily_dd_pct >= cfg.reduced_risk_pct:
        return RiskStage.REDUCED_RISK
    return RiskStage.NORMAL


# ── Main evaluation ──────────────────────────────────────────────────────────

def evaluate_safety(
    state: dict,
    algo_states: Dict[str, AlgoState],
    open_trades: List[Trade],
    current_prices: Dict[str, float],
    cfg: RiskConfig = RISK_CFG,
    safety_cfg: PortfolioSafetyConfig = SAFETY_CFG,
) -> RiskStage:
    """
    Evaluate the daily drawdown safety stage. Updates state["safety_state"]
    in-place. Returns the current RiskStage.

    Called once per signal cycle and once per stop-check cycle.
    """
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    current_value = _compute_portfolio_value(algo_states, open_trades, current_prices)

    ss = state.get("safety_state", {})

    # ── First run: initialise ─────────────────────────────────────────────
    if not ss or "day_date" not in ss:
        ss = _init_safety_state(current_value)
        state["safety_state"] = ss
        return RiskStage.NORMAL

    current_stage = RiskStage(ss.get("current_stage", "normal"))

    # ── Day rollover ──────────────────────────────────────────────────────
    if ss["day_date"] != today_str:
        if current_stage == RiskStage.HARD_LOCKED:
            if _hard_lock_file_exists():
                # Stay locked — don't reset day_start_value
                logger.info(
                    "SAFETY: new day %s but HARD_LOCK file still present — staying locked",
                    today_str,
                )
                ss["day_date"] = today_str
                state["safety_state"] = ss
                return RiskStage.HARD_LOCKED
            else:
                # Operator deleted the lock file — unlock
                logger.warning(
                    "SAFETY STAGE: HARD_LOCKED reset by operator (HARD_LOCK file deleted)"
                )

        # Reset to NORMAL for the new day
        old_dd = ss.get("daily_drawdown_pct", 0.0)
        logger.info(
            "SAFETY: daily reset to NORMAL (new day %s, yesterday closed at -%.1f%%)",
            today_str, old_dd,
        )
        ss = _init_safety_state(current_value)
        state["safety_state"] = ss
        return RiskStage.NORMAL

    # ── Still same day: compute daily drawdown ────────────────────────────
    day_start = ss.get("day_start_value", current_value)
    if day_start <= 0:
        day_start = current_value
        ss["day_start_value"] = day_start

    if day_start > 0:
        daily_dd = (day_start - current_value) / day_start * 100
        daily_dd = max(daily_dd, 0.0)  # no negative drawdown (portfolio up)
    else:
        daily_dd = 0.0

    ss["daily_drawdown_pct"] = round(daily_dd, 4)

    # ── Determine new stage ───────────────────────────────────────────────
    computed_stage = _stage_from_thresholds(daily_dd, safety_cfg)

    # Enforce escalation-only: never de-escalate within a day
    if _STAGE_SEVERITY[computed_stage] > _STAGE_SEVERITY[current_stage]:
        # Transition
        logger.warning(
            "SAFETY STAGE TRANSITION: %s -> %s (daily dd: %.2f%%)",
            current_stage.value, computed_stage.value, daily_dd,
        )
        ss["last_transition_from"] = current_stage.value
        ss["last_transition_time"] = now.isoformat()
        current_stage = computed_stage

        # Write lock file on HARD_LOCKED entry
        if current_stage == RiskStage.HARD_LOCKED:
            _write_hard_lock_file(daily_dd)
            logger.warning("SAFETY: HARD_LOCK file written at %s", HARD_LOCK_FILE)

    ss["current_stage"] = current_stage.value
    state["safety_state"] = ss
    return current_stage


# ── Gate check (used by risk.py) ─────────────────────────────────────────────

def check_safety_stage(stage: RiskStage) -> Tuple[bool, str]:
    """
    Gate check for check_can_open().
    Returns (allowed, reason).
    """
    if stage == RiskStage.NO_NEW_RISK:
        return False, "safety stage NO_NEW_RISK: no new entries until next day"
    if stage == RiskStage.HARD_LOCKED:
        return False, f"safety stage HARD_LOCKED: manual reset required (delete {HARD_LOCK_FILE})"
    return True, "ok"


# ── Size multiplier ──────────────────────────────────────────────────────────

def get_size_multiplier(
    stage: RiskStage,
    safety_cfg: PortfolioSafetyConfig = SAFETY_CFG,
) -> float:
    """Return the trade size multiplier for the current safety stage."""
    if stage == RiskStage.REDUCED_RISK:
        return safety_cfg.reduced_size_multiplier
    if stage in (RiskStage.NO_NEW_RISK, RiskStage.HARD_LOCKED):
        return 0.0
    return 1.0
