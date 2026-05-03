"""
Tests for all five strategy modules.
Validates: None returned on bad inputs, correct signal fields when signal fires,
regime gating, and no unexpected exceptions.
No network calls.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from options_bot.models import (
    Direction, OptionsSignal, Regime, RegimeSnapshot, SignalType,
)
from options_bot.strategies.opening_range_breakout import OpeningRangeBreakout
from options_bot.strategies.vwap_trend_continuation import VWAPTrendContinuation
from options_bot.strategies.ema_trend_pullback import EMATrendPullback
from options_bot.strategies.relative_volume_momentum import RelativeVolumeMomentum
from options_bot.strategies.volatility_breakout import VolatilityBreakout


# ── Fixtures ──────────────────────────────────────────────────────────────────────

def _snap(regime: Regime = Regime.TRENDING, adx: float = 25.0, atr: float = 0.5) -> RegimeSnapshot:
    return RegimeSnapshot(
        symbol="SPY", regime=regime, adx=adx, atr=atr, atr_avg=atr * 0.8,
        bb_width=0.010, timestamp=datetime.now(timezone.utc),
    )


def _bars(n: int = 120, price: float = 450.0, noise: float = 0.1) -> pd.DataFrame:
    np.random.seed(42)
    p   = price + np.cumsum(np.random.randn(n) * noise)
    p   = np.maximum(p, 1.0)
    idx = pd.date_range("2025-01-02 14:30", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({
        "open":   p - 0.05,
        "high":   p + 0.15,
        "low":    p - 0.15,
        "close":  p,
        "volume": np.random.randint(1000, 5000, n).astype(float),
    }, index=idx)


# ── Base contract: all strategies ────────────────────────────────────────────────

ALL_STRATEGY_CLASSES = [
    OpeningRangeBreakout,
    VWAPTrendContinuation,
    EMATrendPullback,
    RelativeVolumeMomentum,
    VolatilityBreakout,
]


class TestBaseContract:
    """Every strategy must satisfy these contracts regardless of signal output."""

    @pytest.mark.parametrize("cls", ALL_STRATEGY_CLASSES)
    def test_returns_none_on_none_df(self, cls):
        s   = cls()
        out = s.evaluate("SPY", None, _snap())
        assert out is None

    @pytest.mark.parametrize("cls", ALL_STRATEGY_CLASSES)
    def test_returns_none_on_empty_df(self, cls):
        s   = cls()
        out = s.evaluate("SPY", pd.DataFrame(), _snap())
        assert out is None

    @pytest.mark.parametrize("cls", ALL_STRATEGY_CLASSES)
    def test_returns_none_on_unknown_regime(self, cls):
        s   = cls()
        out = s.evaluate("SPY", _bars(), _snap(Regime.UNKNOWN))
        assert out is None

    @pytest.mark.parametrize("cls", ALL_STRATEGY_CLASSES)
    def test_never_raises(self, cls):
        s = cls()
        try:
            s.evaluate("SPY", _bars(), _snap())
        except Exception as exc:
            pytest.fail(f"{cls.__name__} raised unexpectedly: {exc}")

    @pytest.mark.parametrize("cls", ALL_STRATEGY_CLASSES)
    def test_strategy_id_set(self, cls):
        assert cls.strategy_id != ""

    @pytest.mark.parametrize("cls", ALL_STRATEGY_CLASSES)
    def test_signal_fields_complete_when_returned(self, cls):
        """If a signal is returned it must have all required fields populated."""
        s   = cls()
        out = s.evaluate("SPY", _bars(200), _snap())
        if out is None:
            return   # no signal this cycle — that is fine
        assert isinstance(out, OptionsSignal)
        assert out.symbol          in ("SPY", "QQQ")
        assert out.direction       in list(Direction)
        assert out.regime_required != []
        assert out.underlying_price > 0
        assert 0.0 <= out.confidence_score <= 1.0
        assert out.entry_zone       != ""
        assert out.stop_logic       != ""
        assert out.target_logic     != ""
        assert isinstance(out.invalidation_conditions, list)
        assert isinstance(out.reason_codes, list)
        assert isinstance(out.indicators, dict)
        assert 0.0 <= out.underlying_quality_score <= 1.0
        assert out.data_quality_note != ""


# ── Regime gating ─────────────────────────────────────────────────────────────────

class TestRegimeGating:
    def test_orb_rejects_ranging(self):
        out = OpeningRangeBreakout().evaluate("SPY", _bars(), _snap(Regime.RANGING))
        assert out is None

    def test_vwap_rejects_expanding(self):
        out = VWAPTrendContinuation().evaluate("SPY", _bars(), _snap(Regime.EXPANDING))
        assert out is None

    def test_ema_rejects_ranging(self):
        out = EMATrendPullback().evaluate("SPY", _bars(), _snap(Regime.RANGING))
        assert out is None

    def test_rvol_rejects_unknown(self):
        out = RelativeVolumeMomentum().evaluate("SPY", _bars(), _snap(Regime.UNKNOWN))
        assert out is None

    def test_vbkr_rejects_trending(self):
        out = VolatilityBreakout().evaluate("SPY", _bars(), _snap(Regime.TRENDING))
        assert out is None

    def test_vbkr_rejects_ranging(self):
        out = VolatilityBreakout().evaluate("SPY", _bars(), _snap(Regime.RANGING))
        assert out is None


# ── Insufficient data ─────────────────────────────────────────────────────────────

class TestInsufficientData:
    @pytest.mark.parametrize("cls", ALL_STRATEGY_CLASSES)
    def test_returns_none_with_too_few_bars(self, cls):
        out = cls().evaluate("SPY", _bars(5), _snap())
        assert out is None


# ── Signal direction ──────────────────────────────────────────────────────────────

class TestSignalDirection:
    """When a signal is returned its direction must derive a valid option_type."""

    @pytest.mark.parametrize("cls", ALL_STRATEGY_CLASSES)
    def test_option_type_consistent_with_direction(self, cls):
        from options_bot.models import OptionType
        out = cls().evaluate("SPY", _bars(200), _snap())
        if out is None:
            return
        if out.direction == Direction.BULLISH:
            assert out.option_type == OptionType.CALL
        elif out.direction == Direction.BEARISH:
            assert out.option_type == OptionType.PUT
        else:
            pytest.fail(f"NEUTRAL direction reached signal output: {out}")
