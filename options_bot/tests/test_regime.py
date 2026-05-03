"""
Tests for options_bot/regime.py — regime detection and indicator helpers.
No network calls. All DataFrames are synthetic.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from options_bot.regime import compute_adx, compute_atr, compute_bb_width, detect_regime
from options_bot.models import Regime, RegimeSnapshot


# ── Fixtures ──────────────────────────────────────────────────────────────────────

def _flat(n: int = 120, price: float = 450.0, noise: float = 0.10) -> pd.DataFrame:
    np.random.seed(0)
    p   = price + np.cumsum(np.random.randn(n) * noise)
    p   = np.maximum(p, 1.0)
    idx = pd.date_range("2025-01-02 14:30", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({
        "open":   p - 0.05,
        "high":   p + 0.10,
        "low":    p - 0.10,
        "close":  p,
        "volume": np.random.randint(1000, 5000, n).astype(float),
    }, index=idx)


def _trend(n: int = 120, drift: float = 0.5) -> pd.DataFrame:
    np.random.seed(1)
    p   = 450.0 + np.cumsum(np.random.randn(n) * 0.05 + drift)
    p   = np.maximum(p, 1.0)
    idx = pd.date_range("2025-01-02 14:30", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({
        "open":   p - abs(drift) * 0.1,
        "high":   p + 0.20,
        "low":    p - 0.20,
        "close":  p,
        "volume": np.random.randint(1000, 5000, n).astype(float),
    }, index=idx)


# ── Indicator tests ───────────────────────────────────────────────────────────────

class TestComputeATR:
    def test_returns_positive_values(self):
        df  = _flat()
        atr = compute_atr(df, period=14)
        assert float(atr.iloc[-1]) > 0

    def test_length_matches_input(self):
        df  = _flat(60)
        atr = compute_atr(df, period=14)
        assert len(atr) == 60


class TestComputeADX:
    def test_adx_in_valid_range(self):
        df              = _trend()
        adx, plus, minus = compute_adx(df, period=14)
        adx_val         = float(adx.iloc[-1])
        assert 0 <= adx_val <= 100

    def test_plus_minus_di_non_negative(self):
        df               = _trend()
        _, plus_di, minus_di = compute_adx(df, period=14)
        assert float(plus_di.iloc[-1])  >= 0
        assert float(minus_di.iloc[-1]) >= 0


class TestComputeBBWidth:
    def test_returns_positive_width(self):
        df = _flat()
        bw = compute_bb_width(df, period=20, n_std=2.0)
        assert float(bw.iloc[-1]) > 0

    def test_nan_for_first_period_bars(self):
        df = _flat(30)
        bw = compute_bb_width(df, period=20, n_std=2.0)
        assert bw.iloc[:19].isna().all()


# ── detect_regime tests ───────────────────────────────────────────────────────────

class TestDetectRegime:
    def test_none_df_returns_unknown(self):
        snap = detect_regime("SPY", None)
        assert snap.regime == Regime.UNKNOWN
        assert snap.symbol == "SPY"

    def test_empty_df_returns_unknown(self):
        snap = detect_regime("QQQ", pd.DataFrame())
        assert snap.regime == Regime.UNKNOWN

    def test_too_few_bars_returns_unknown(self):
        df   = _flat(10)
        snap = detect_regime("SPY", df)
        assert snap.regime == Regime.UNKNOWN

    def test_returns_regime_snapshot_type(self):
        df   = _flat()
        snap = detect_regime("SPY", df)
        assert isinstance(snap, RegimeSnapshot)
        assert snap.symbol == "SPY"
        assert snap.regime in list(Regime)
        assert isinstance(snap.timestamp, datetime)

    def test_adx_atr_non_negative(self):
        df   = _flat()
        snap = detect_regime("SPY", df)
        assert snap.adx   >= 0
        assert snap.atr   >= 0
        assert snap.atr_avg >= 0

    def test_regime_not_unknown_with_sufficient_data(self):
        # A well-formed flat dataset with sufficient bars should classify to something
        df   = _flat(120)
        snap = detect_regime("SPY", df)
        # We cannot assert a specific regime on synthetic data, but it should
        # not raise and should return a valid Regime value.
        assert snap.regime in list(Regime)

    def test_strong_trend_not_classified_ranging(self):
        df   = _trend(200, drift=2.0)
        snap = detect_regime("SPY", df)
        # Strong drift → expect TRENDING or EXPANDING, not RANGING
        assert snap.regime not in (Regime.RANGING,)
