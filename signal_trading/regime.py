"""
signal_trading/regime.py — Market regime detection.

Uses ADX (trend strength) and ATR (volatility) on the 4h timeframe.
Filters signals: trend strategies only in trending markets,
range strategies only in ranging markets, nothing in volatile markets.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
import pandas as pd

from signal_trading.config import REGIME_CFG, REGIME_TIMEFRAME, RegimeConfig
from signal_trading.data import fetch_asset
from signal_trading.models import Regime, RegimeSnapshot, Signal, Direction

logger = logging.getLogger("signal_trading.regime")


# ── Indicator calculations ────────────────────────────────────────────────────

def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength (not direction)."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    # True Range
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Directional movement
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    # Smoothed (Wilder's EMA)
    alpha = 1 / period
    atr_smooth = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_smooth
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_smooth

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=alpha, adjust=False).mean()

    return adx, plus_di, minus_di


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ── Regime detection ──────────────────────────────────────────────────────────

def detect_regime(
    asset: str,
    df_4h: Optional[pd.DataFrame] = None,
    cfg: RegimeConfig = REGIME_CFG,
) -> RegimeSnapshot:
    """
    Detect the current market regime for an asset.

    Falls back to UNKNOWN if data is unavailable.
    """
    now = datetime.now(timezone.utc)

    if df_4h is None:
        df_4h = fetch_asset(asset, timeframe=REGIME_TIMEFRAME)

    if df_4h is None or len(df_4h) < cfg.adx_period * 3:
        logger.warning("Insufficient 4h data for regime detection on %s", asset)
        return RegimeSnapshot(
            asset=asset, regime=Regime.UNKNOWN,
            adx=0.0, atr=0.0, atr_avg=0.0, timestamp=now,
        )

    try:
        adx, plus_di, minus_di = _compute_adx(df_4h, cfg.adx_period)
        atr = _compute_atr(df_4h, cfg.atr_period)

        adx_val = float(adx.iloc[-1])
        plus_di_val = float(plus_di.iloc[-1])
        minus_di_val = float(minus_di.iloc[-1])
        atr_val = float(atr.iloc[-1])
        atr_avg = float(atr.tail(30).mean())  # 30-bar ATR average for volatility comparison

        # Volatility check: ATR > N× its own 30-bar average → volatile
        if atr_avg > 0 and atr_val > cfg.atr_volatile_multiplier * atr_avg:
            regime = Regime.VOLATILE
        elif adx_val >= cfg.adx_trend_threshold:
            regime = Regime.TREND_UP if plus_di_val > minus_di_val else Regime.TREND_DOWN
        else:
            regime = Regime.RANGE

        return RegimeSnapshot(
            asset=asset, regime=regime,
            adx=adx_val, atr=atr_val, atr_avg=atr_avg,
            timestamp=now,
        )

    except Exception as exc:
        logger.error("Regime detection failed for %s: %s", asset, exc)
        return RegimeSnapshot(
            asset=asset, regime=Regime.UNKNOWN,
            adx=0.0, atr=0.0, atr_avg=0.0, timestamp=now,
        )


# ── Signal filtering ─────────────────────────────────────────────────────────

def filter_signals(
    signals: List[Signal],
    regime: RegimeSnapshot,
    cfg: RegimeConfig = REGIME_CFG,
) -> List[Signal]:
    """
    Filter signals based on regime. Returns only the signals allowed
    to proceed. Modifies signal.regime in-place for all signals.

    Rules:
    - VOLATILE: block all new entries (return empty list)
    - TREND_UP / TREND_DOWN: only trend-following strategies pass
    - RANGE: only mean-reversion strategies pass
    - UNKNOWN: pass all (no information to filter on)
    """
    # Tag every signal with the current regime
    for sig in signals:
        sig.regime = regime.regime

    if not signals:
        return []

    r = regime.regime

    if r == Regime.VOLATILE and cfg.block_in_volatile:
        logger.info("REGIME %s VOLATILE — blocking all new entries", regime.asset)
        return []

    if r == Regime.UNKNOWN:
        return [s for s in signals if s.direction is not None]

    allowed = []
    for sig in signals:
        if sig.direction is None:
            continue  # HOLD — skip regardless

        if r in (Regime.TREND_UP, Regime.TREND_DOWN):
            # Only allow long signals in uptrend, short in downtrend
            if sig.strategy not in cfg.trend_strategies:
                continue
            if r == Regime.TREND_UP and sig.direction.value == "short":
                continue
            if r == Regime.TREND_DOWN and sig.direction.value == "long":
                continue
        elif r == Regime.RANGE:
            if sig.strategy not in cfg.range_strategies:
                continue

        allowed.append(sig)

    return allowed
