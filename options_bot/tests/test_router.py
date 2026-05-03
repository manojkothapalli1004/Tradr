"""
Tests for options_bot/router.py — portfolio gating and signal ranking.
No network calls. All objects constructed directly.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from options_bot.config import RouterConfig
from options_bot.models import (
    AlgoSlot, Direction, ExitReason, OptionAction, OptionTrade, OptionType,
    OptionsSignal, Regime, SignalType,
)
from options_bot.router import _check_eligible, _rank, route_signals


# ── Helpers ───────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _signal(
    strategy: SignalType = SignalType.ORB,
    symbol: str = "SPY",
    direction: Direction = Direction.BULLISH,
    regime: Regime = Regime.TRENDING,
    confidence: float = 0.5,
) -> OptionsSignal:
    return OptionsSignal(
        strategy_name=strategy,
        symbol=symbol,
        direction=direction,
        timestamp=_now(),
        regime_required=[regime.value],
        regime_at_signal=regime,
        underlying_price=450.0,
        confidence_score=confidence,
    )


def _slot(
    strategy: str = SignalType.ORB.value,
    symbol: str = "SPY",
    total_trades: int = 0,
    pnl: float = 0.0,
    wins: int = 0,
    cb_active: bool = False,
) -> AlgoSlot:
    s = AlgoSlot(strategy=strategy, symbol=symbol)
    s.total_trades   = total_trades
    s.winning_trades = wins
    s.losing_trades  = total_trades - wins
    s.total_pnl_usd  = pnl
    if cb_active:
        from datetime import timedelta
        s.circuit_breaker_until = datetime.now(timezone.utc) + timedelta(hours=1)
    return s


def _trade(symbol: str = "SPY", strategy: str = SignalType.ORB.value) -> OptionTrade:
    return OptionTrade(
        id=f"test-{symbol}",
        symbol=symbol,
        strategy=strategy,
        option_type=OptionType.CALL,
        action=OptionAction.BUY,
        expiry="2025-06-20",
        strike=450.0,
        dte_at_entry=21,
        entry_time=_now(),
        entry_fill_per_share=3.50,
        entry_premium_total=350.0,
    )


def _slots_for(signals: list) -> dict:
    return {
        f"{s.strategy_name.value}-{s.symbol}": _slot(s.strategy_name.value, s.symbol)
        for s in signals
    }


# ── _check_eligible ───────────────────────────────────────────────────────────────

class TestCheckEligible:
    cfg = RouterConfig(max_total_positions=2, max_per_symbol=1, min_trades_for_ranking=30)

    def test_passes_with_no_open_trades(self):
        sig   = _signal()
        slots = {f"{SignalType.ORB.value}-SPY": _slot()}
        ok, _ = _check_eligible(sig, slots, [], self.cfg)
        assert ok

    def test_rejected_portfolio_cap(self):
        sig         = _signal()
        slots       = {f"{SignalType.ORB.value}-SPY": _slot()}
        open_trades = [_trade("SPY"), _trade("QQQ")]
        ok, reason  = _check_eligible(sig, slots, open_trades, self.cfg)
        assert not ok
        assert "portfolio cap" in reason

    def test_rejected_symbol_cap(self):
        sig         = _signal(symbol="SPY")
        slots       = {f"{SignalType.ORB.value}-SPY": _slot()}
        open_trades = [_trade("SPY")]
        ok, reason  = _check_eligible(sig, slots, open_trades, self.cfg)
        assert not ok
        assert "symbol cap" in reason

    def test_rejected_missing_slot(self):
        sig        = _signal()
        ok, reason = _check_eligible(sig, {}, [], self.cfg)
        assert not ok
        assert "not initialised" in reason

    def test_rejected_circuit_breaker(self):
        sig   = _signal()
        slots = {f"{SignalType.ORB.value}-SPY": _slot(cb_active=True)}
        ok, reason = _check_eligible(sig, slots, [], self.cfg)
        assert not ok
        assert "circuit breaker" in reason

    def test_rejected_non_actionable_neutral(self):
        sig   = _signal(direction=Direction.NEUTRAL)
        slots = {f"{SignalType.ORB.value}-SPY": _slot()}
        ok, reason = _check_eligible(sig, slots, [], self.cfg)
        assert not ok

    def test_rejected_regime_mismatch(self):
        # Signal says it needs TRENDING but regime_at_signal is RANGING
        sig = OptionsSignal(
            strategy_name=SignalType.ORB,
            symbol="SPY",
            direction=Direction.BULLISH,
            timestamp=_now(),
            regime_required=[Regime.TRENDING.value],
            regime_at_signal=Regime.RANGING,
            underlying_price=450.0,
        )
        slots = {f"{SignalType.ORB.value}-SPY": _slot()}
        ok, _ = _check_eligible(sig, slots, [], self.cfg)
        assert not ok


# ── _rank ─────────────────────────────────────────────────────────────────────────

class TestRank:
    cfg = RouterConfig(max_total_positions=2, max_per_symbol=1, min_trades_for_ranking=30)

    def test_unranked_sorted_by_confidence_desc(self):
        sigs  = [_signal(SignalType.ORB, confidence=0.3),
                 _signal(SignalType.VWAP, symbol="QQQ", confidence=0.8)]
        slots = _slots_for(sigs)
        out   = _rank(sigs, slots, self.cfg)
        assert out[0].confidence_score >= out[1].confidence_score

    def test_ranked_slot_precedes_unranked(self):
        low_conf  = _signal(SignalType.VWAP, symbol="QQQ", confidence=0.2)
        high_conf = _signal(SignalType.ORB,  symbol="SPY",  confidence=0.9)
        ranked_slot = _slot(SignalType.VWAP.value, "QQQ", total_trades=35,
                            wins=25, pnl=200.0)
        slots = {
            f"{SignalType.VWAP.value}-QQQ": ranked_slot,
            f"{SignalType.ORB.value}-SPY":  _slot(total_trades=0),
        }
        out = _rank([low_conf, high_conf], slots, self.cfg)
        # Ranked slot should come first even with lower confidence
        assert out[0].strategy_name == SignalType.VWAP

    def test_empty_returns_empty(self):
        assert _rank([], {}, self.cfg) == []


# ── route_signals ─────────────────────────────────────────────────────────────────

class TestRouteSignals:
    cfg = RouterConfig(max_total_positions=2, max_per_symbol=1, min_trades_for_ranking=30)

    def test_no_signals_returns_none(self):
        assert route_signals([], {}, [], self.cfg) is None

    def test_all_rejected_returns_none(self):
        sig    = _signal()
        result = route_signals([sig], {}, [], self.cfg)  # missing slot
        assert result is None

    def test_single_eligible_selected(self):
        sig    = _signal()
        slots  = _slots_for([sig])
        result = route_signals([sig], slots, [], self.cfg)
        assert result is sig

    def test_selects_highest_confidence_when_unranked(self):
        sig_lo = _signal(SignalType.ORB,  "SPY",  confidence=0.3)
        sig_hi = _signal(SignalType.VWAP, "QQQ",  confidence=0.9)
        slots  = _slots_for([sig_lo, sig_hi])
        result = route_signals([sig_lo, sig_hi], slots, [], self.cfg)
        assert result is sig_hi

    def test_portfolio_cap_blocks_all(self):
        sig         = _signal()
        slots       = _slots_for([sig])
        open_trades = [_trade("SPY"), _trade("QQQ")]
        result      = route_signals([sig], slots, open_trades, self.cfg)
        assert result is None

    def test_symbol_cap_blocks_same_symbol(self):
        sig         = _signal(symbol="SPY")
        slots       = _slots_for([sig])
        open_trades = [_trade("SPY")]
        result      = route_signals([sig], slots, open_trades, self.cfg)
        assert result is None
