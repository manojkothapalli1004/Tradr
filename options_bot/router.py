"""
options_bot/router.py — Portfolio-level signal router.

Single gate all signals must pass each cycle. Enforces:
  - Max total open positions across all symbols.
  - Max open positions per symbol.
  - Per-slot circuit breaker.
  - Performance-based ranking (only after min_trades_for_ranking per slot).
  - Selects at most ONE signal per cycle.
  - Logs every rejection with a reason — no silent drops.

Strategies never call this directly. The runner calls route_signals()
after collecting all strategy outputs for a cycle.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from options_bot.config import ROUTER_CFG, RouterConfig
from options_bot.models import AlgoSlot, OptionsSignal, OptionTrade

logger = logging.getLogger("options_bot.router")


# ── Eligibility ─────────────────────────────────────────────────────────────────

def _check_eligible(
    sig: OptionsSignal,
    slots: Dict[str, AlgoSlot],
    open_trades: List[OptionTrade],
    cfg: RouterConfig,
) -> Tuple[bool, str]:
    """
    Returns (eligible, rejection_reason).
    All checks are independent; first failure wins.
    """
    # Portfolio cap
    if len(open_trades) >= cfg.max_total_positions:
        return False, f"portfolio cap {len(open_trades)}/{cfg.max_total_positions} open"

    # Per-symbol cap
    symbol_open = sum(1 for t in open_trades if t.symbol == sig.symbol)
    if symbol_open >= cfg.max_per_symbol:
        return False, f"symbol cap {symbol_open}/{cfg.max_per_symbol} for {sig.symbol}"

    # Signal must be actionable (regime check embedded in OptionsSignal)
    if not sig.is_actionable:
        return False, f"signal not actionable (direction={sig.direction.value} regime={sig.regime_at_signal.value})"

    # Slot must exist and not be in circuit breaker
    key  = f"{sig.strategy_name.value}-{sig.symbol}"
    slot = slots.get(key)
    if slot is None:
        return False, f"slot {key} not initialised"
    if slot.circuit_breaker_active:
        return False, f"circuit breaker until {slot.circuit_breaker_until}"

    return True, "ok"


# ── Ranking ──────────────────────────────────────────────────────────────────────

def _rank(
    eligible: List[OptionsSignal],
    slots: Dict[str, AlgoSlot],
    cfg: RouterConfig,
) -> List[OptionsSignal]:
    """
    Split eligible signals into two buckets:
      ranked   — slot has ≥ min_trades_for_ranking completed trades;
                 sorted by ranking_score desc, then confidence_score desc.
      unranked — slot below minimum sample size;
                 sorted by confidence_score desc only.

    Ranked bucket precedes unranked. Within each bucket ties broken by
    confidence_score. No ranking change is applied to unranked slots
    regardless of their recent results.
    """
    ranked:   List[OptionsSignal] = []
    unranked: List[OptionsSignal] = []

    for sig in eligible:
        key  = f"{sig.strategy_name.value}-{sig.symbol}"
        slot = slots.get(key)
        if slot and slot.total_trades >= cfg.min_trades_for_ranking:
            ranked.append(sig)
        else:
            unranked.append(sig)

    ranked.sort(
        key=lambda s: (
            slots[f"{s.strategy_name.value}-{s.symbol}"].ranking_score,
            s.confidence_score,
        ),
        reverse=True,
    )
    unranked.sort(key=lambda s: s.confidence_score, reverse=True)

    return ranked + unranked


# ── Main entry point ─────────────────────────────────────────────────────────────

def route_signals(
    signals: List[OptionsSignal],
    slots: Dict[str, AlgoSlot],
    open_trades: List[OptionTrade],
    cfg: RouterConfig = ROUTER_CFG,
) -> Optional[OptionsSignal]:
    """
    Select the single best eligible signal for this cycle, or None.

    Args:
        signals:     All OptionsSignal objects produced by strategies this cycle.
        slots:       Current AlgoSlot state keyed by "strategy-symbol".
        open_trades: Currently open OptionTrade list.
        cfg:         RouterConfig (position caps, ranking threshold).

    Returns:
        The chosen OptionsSignal, or None if nothing passes.
    """
    if not signals:
        logger.debug("ROUTER: no signals this cycle")
        return None

    eligible: List[OptionsSignal] = []
    for sig in signals:
        ok, reason = _check_eligible(sig, slots, open_trades, cfg)
        if ok:
            eligible.append(sig)
        else:
            logger.info(
                "ROUTER REJECT %s@%s dir=%s — %s",
                sig.strategy_name.value, sig.symbol, sig.direction.value, reason,
            )

    if not eligible:
        logger.info("ROUTER: all %d signal(s) rejected this cycle", len(signals))
        return None

    ordered = _rank(eligible, slots, cfg)
    chosen  = ordered[0]

    key   = f"{chosen.strategy_name.value}-{chosen.symbol}"
    slot  = slots.get(key)
    n     = slot.total_trades if slot else 0
    basis = (
        f"ranked (trades={n} ≥ {cfg.min_trades_for_ranking})"
        if n >= cfg.min_trades_for_ranking
        else f"unranked (trades={n} < {cfg.min_trades_for_ranking})"
    )

    logger.info(
        "ROUTER SELECT %s@%s %s conf=%.3f [%s]",
        chosen.strategy_name.value, chosen.symbol,
        chosen.direction.value, chosen.confidence_score, basis,
    )

    # Log skipped eligible signals
    for skipped in ordered[1:]:
        sk_key  = f"{skipped.strategy_name.value}-{skipped.symbol}"
        sk_slot = slots.get(sk_key)
        logger.info(
            "ROUTER SKIP (cap) %s@%s conf=%.3f trades=%d",
            skipped.strategy_name.value, skipped.symbol,
            skipped.confidence_score,
            sk_slot.total_trades if sk_slot else 0,
        )

    return chosen
