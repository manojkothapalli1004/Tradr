"""Tests for regime.py — regime detection and signal filtering."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from signal_trading.regime import detect_regime, filter_signals, _compute_adx, _compute_atr
from signal_trading.models import Regime, RegimeSnapshot, Signal, Direction, SignalStrength
from signal_trading.config import RegimeConfig


def _trending_df(n: int = 200, slope: float = 5.0) -> pd.DataFrame:
    np.random.seed(0)
    close = 1000 + np.arange(n) * slope + np.random.normal(0, 2, n)
    dates = pd.date_range("2024-01-01", periods=n, freq="4h")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.003,
        "low": close * 0.997, "close": close,
        "volume": 1000.0, "timestamp": dates,
    }, index=dates)


def _ranging_df(n: int = 200) -> pd.DataFrame:
    np.random.seed(1)
    close = 1000 + np.random.normal(0, 2, n)
    dates = pd.date_range("2024-01-01", periods=n, freq="4h")
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.001,
        "low": close * 0.999, "close": close,
        "volume": 500.0, "timestamp": dates,
    }, index=dates)


def _make_signal(asset: str, strategy: str, direction) -> Signal:
    return Signal(
        asset=asset, strategy=strategy, direction=direction,
        price=1000.0, timestamp=datetime.now(timezone.utc),
    )


class TestIndicators:
    def test_adx_returns_series(self):
        df = _trending_df()
        adx, plus_di, minus_di = _compute_adx(df, 14)
        assert len(adx) == len(df)
        assert float(adx.iloc[-1]) > 0

    def test_atr_returns_series(self):
        df = _trending_df()
        atr = _compute_atr(df, 14)
        assert len(atr) == len(df)
        assert float(atr.iloc[-1]) > 0

    def test_trending_adx_higher(self):
        df_trend = _trending_df(slope=10.0)
        df_range = _ranging_df()
        adx_t, _, _ = _compute_adx(df_trend)
        adx_r, _, _ = _compute_adx(df_range)
        assert float(adx_t.iloc[-1]) > float(adx_r.iloc[-1])


class TestDetectRegime:
    def test_returns_snapshot(self):
        df = _trending_df()
        snap = detect_regime("BTC", df_4h=df)
        assert isinstance(snap, RegimeSnapshot)
        assert snap.asset == "BTC"

    def test_trending_df_detected_as_trend(self):
        df = _trending_df(slope=15.0)
        snap = detect_regime("BTC", df_4h=df)
        assert snap.regime in (Regime.TREND_UP, Regime.TREND_DOWN)

    def test_ranging_df_detected_as_range(self):
        df = _ranging_df()
        snap = detect_regime("BTC", df_4h=df)
        assert snap.regime == Regime.RANGE

    def test_insufficient_data_returns_unknown(self):
        df = _trending_df(n=10)
        snap = detect_regime("BTC", df_4h=df)
        assert snap.regime == Regime.UNKNOWN


class TestFilterSignals:
    def _snap(self, regime: Regime) -> RegimeSnapshot:
        return RegimeSnapshot(
            asset="BTC", regime=regime, adx=30.0,
            atr=100.0, atr_avg=50.0, timestamp=datetime.now(timezone.utc),
        )

    def test_volatile_blocks_all(self):
        signals = [
            _make_signal("BTC", "rsi", Direction.LONG),
            _make_signal("BTC", "macd", Direction.SHORT),
        ]
        allowed = filter_signals(signals, self._snap(Regime.VOLATILE))
        assert allowed == []

    def test_trend_up_allows_trend_longs(self):
        signals = [
            _make_signal("BTC", "sma_crossover", Direction.LONG),
            _make_signal("BTC", "rsi", Direction.LONG),        # range strategy
            _make_signal("BTC", "macd", Direction.SHORT),      # wrong direction
        ]
        allowed = filter_signals(signals, self._snap(Regime.TREND_UP))
        strats = {s.strategy for s in allowed}
        assert "sma_crossover" in strats       # trend strategy, correct dir
        assert "rsi" not in strats             # range strategy filtered
        assert not any(s.direction == Direction.SHORT for s in allowed)

    def test_range_allows_range_strategies(self):
        signals = [
            _make_signal("BTC", "rsi", Direction.LONG),
            _make_signal("BTC", "bollinger_bands", Direction.SHORT),
            _make_signal("BTC", "macd", Direction.LONG),       # trend strategy
        ]
        allowed = filter_signals(signals, self._snap(Regime.RANGE))
        strats = {s.strategy for s in allowed}
        assert "rsi" in strats
        assert "bollinger_bands" in strats
        assert "macd" not in strats

    def test_hold_signals_always_excluded(self):
        signals = [_make_signal("BTC", "rsi", None)]  # None = hold
        allowed = filter_signals(signals, self._snap(Regime.RANGE))
        assert allowed == []

    def test_regime_tagged_on_all_signals(self):
        signals = [
            _make_signal("BTC", "rsi", Direction.LONG),
            _make_signal("BTC", "macd", None),
        ]
        filter_signals(signals, self._snap(Regime.TREND_DOWN))
        for s in signals:
            assert s.regime == Regime.TREND_DOWN
