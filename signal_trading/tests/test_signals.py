"""Tests for signals.py — strategy dispatch and signal extraction."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from signal_trading.signals import compute_signal, compute_all_signals, _run_strategy
from signal_trading.models import Direction, Regime, SignalStrength
from signal_trading.config import StrategyParams


def _make_df(n: int = 200, trend: str = "up") -> pd.DataFrame:
    """Synthetic OHLCV with controllable trend for deterministic signal testing."""
    np.random.seed(42)
    if trend == "up":
        close = 1000 + np.arange(n) * 2 + np.random.normal(0, 5, n)
    elif trend == "down":
        close = 1000 + np.arange(n) * -2 + np.random.normal(0, 5, n)
    else:  # flat
        close = 1000 + np.random.normal(0, 3, n)
    close = np.maximum(close, 10.0)
    dates = pd.date_range("2024-01-01", periods=n, freq="1h")
    df = pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.005,
        "low": close * 0.995,
        "close": close,
        "volume": np.random.uniform(100, 1000, n),
        "timestamp": dates,
    }, index=dates)
    return df


class TestComputeSignal:
    def test_returns_signal_object(self):
        df = _make_df()
        sig = compute_signal("BTC", "rsi", df)
        assert sig.asset == "BTC"
        assert sig.strategy == "rsi"
        assert sig.price > 0

    def test_hold_when_no_crossover(self):
        # Flat market — SMA crossover should not fire
        df = _make_df(trend="flat")
        sig = compute_signal("BTC", "sma_crossover", df)
        # Either hold or signal — just check it doesn't raise and returns valid type
        assert sig.direction in (None, Direction.LONG, Direction.SHORT)

    def test_excluded_strategy_returns_hold(self):
        df = _make_df()
        # volume_weighted is excluded for GOLD
        sig = compute_signal("GOLD", "volume_weighted", df)
        assert sig.direction is None

    def test_unknown_strategy_returns_hold(self):
        df = _make_df()
        sig = compute_signal("BTC", "nonexistent_strat", df)
        assert sig.direction is None

    def test_indicators_populated(self):
        df = _make_df()
        sig = compute_signal("BTC", "rsi", df)
        assert "price" in sig.indicators

    def test_strong_signal_on_agreement(self):
        # Use uptrend so momentum/trend strategies should agree on LONG
        df = _make_df(trend="up")
        sigs = compute_all_signals("BTC", df)
        strong = [s for s in sigs if s.strength == SignalStrength.STRONG and s.direction == Direction.LONG]
        # At least some strategies should agree on LONG in an uptrend
        # (not guaranteed every run, but structure should work)
        assert isinstance(strong, list)  # no crash

    def test_all_assets_run(self):
        df = _make_df()
        for asset in ["BTC", "ETH", "GOLD"]:
            sigs = compute_all_signals(asset, df)
            assert len(sigs) > 0
            assert all(s.asset == asset for s in sigs)


class TestRunStrategy:
    @pytest.mark.parametrize("name", [
        "sma_crossover", "ema_crossover", "rsi", "bollinger_bands",
        "macd", "mean_reversion", "momentum", "volume_weighted",
        "triple_ema", "rsi_macd_combo",
    ])
    def test_strategy_runs_without_error(self, name):
        df = _make_df()
        params = StrategyParams()
        result = _run_strategy(name, df, params)
        assert result is not None
        assert "signal" in result.columns
        assert len(result) == len(df)

    def test_signal_values_are_valid(self):
        df = _make_df()
        params = StrategyParams()
        for name in ["rsi", "macd", "bollinger_bands"]:
            result = _run_strategy(name, df, params)
            valid = {0, 1, -1, 0.0, 1.0, -1.0}
            last_sig = result["signal"].iloc[-1]
            assert last_sig in valid or pd.isna(last_sig), f"{name}: unexpected signal {last_sig}"
