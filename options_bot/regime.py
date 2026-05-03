"""
options_bot/regime.py — Market regime engine (5-minute bars).

Classifies one symbol per cycle into TRENDING / RANGING / EXPANDING / UNKNOWN.
UNKNOWN always means no new positions (safe default when data is insufficient).

Indicator functions are module-level so volatility_breakout.py can import
compute_atr directly without circular imports.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from options_bot.config import REGIME_CFG, RegimeConfig
from options_bot.models import Regime, RegimeSnapshot

logger = logging.getLogger("options_bot.regime")


# ── Indicators ──────────────────────────────────────────────────────────────────

def compute_adx(
    df: pd.DataFrame, period: int
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Wilder-smoothed ADX, +DI, -DI.
    Returns three Series aligned to df.index.
    First ~period*2 rows will be NaN.
    """
    high, low, close = df["high"], df["low"], df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    up   = high.diff()
    down = -low.diff()
    plus_dm  = pd.Series(np.where((up > down) & (up > 0),   up,   0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)

    a = 1.0 / period
    atr_s    = tr.ewm(alpha=a, adjust=False).mean()
    plus_di  = 100.0 * plus_dm.ewm( alpha=a, adjust=False).mean() / atr_s
    minus_di = 100.0 * minus_dm.ewm(alpha=a, adjust=False).mean() / atr_s
    dx       = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx      = dx.ewm(alpha=a, adjust=False).mean()

    return adx, plus_di, minus_di


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder-smoothed ATR."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def compute_bb_width(df: pd.DataFrame, period: int, n_std: float) -> pd.Series:
    """
    Bollinger Band width as a fraction of the middle band.
    (upper - lower) / mid.  NaN for first `period` rows.
    """
    mid   = df["close"].rolling(period).mean()
    sigma = df["close"].rolling(period).std()
    return ((mid + n_std * sigma) - (mid - n_std * sigma)) / mid.replace(0, np.nan)


# ── Regime detection ────────────────────────────────────────────────────────────

def detect_regime(
    symbol: str,
    df_5m: Optional[pd.DataFrame],
    cfg: RegimeConfig = REGIME_CFG,
) -> RegimeSnapshot:
    """
    Classify the current regime from 5-minute bars.

    Falls back to UNKNOWN on any of:
      - df_5m is None or empty
      - fewer than adx_period * 4 bars (insufficient warmup)
      - any indicator produces NaN on the latest bar
      - unexpected exception

    UNKNOWN → callers must not open new positions.
    """
    now = datetime.now(timezone.utc)

    def _unknown(reason: str) -> RegimeSnapshot:
        logger.warning("REGIME %s → UNKNOWN: %s", symbol, reason)
        return RegimeSnapshot(
            symbol=symbol, regime=Regime.UNKNOWN,
            adx=0.0, atr=0.0, atr_avg=0.0, bb_width=0.0, timestamp=now,
        )

    if df_5m is None or df_5m.empty:
        return _unknown("df_5m is None or empty")

    min_bars = cfg.adx_period * 4
    if len(df_5m) < min_bars:
        return _unknown(f"only {len(df_5m)} bars, need {min_bars}")

    try:
        adx_s, _, _ = compute_adx(df_5m, cfg.adx_period)
        atr_s       = compute_atr(df_5m, cfg.atr_period)
        bw_s        = compute_bb_width(df_5m, cfg.bb_period, cfg.bb_std)

        adx_val   = float(adx_s.iloc[-1])
        atr_val   = float(atr_s.iloc[-1])
        atr_avg   = float(atr_s.iloc[-cfg.atr_avg_lookback:].mean())
        bw_val_raw = bw_s.iloc[-1]
        bw_val    = float(bw_val_raw) if not (isinstance(bw_val_raw, float) and np.isnan(bw_val_raw)) else float("nan")

        if any(np.isnan(v) for v in [adx_val, atr_val, atr_avg]):
            return _unknown("indicator NaN on latest bar — insufficient warmup")

        atr_ratio = atr_val / atr_avg if atr_avg > 0 else 1.0

        # Priority: EXPANDING > RANGING > TRENDING > UNKNOWN
        if atr_ratio >= cfg.atr_expansion_multiplier:
            regime = Regime.EXPANDING
        elif (
            adx_val < cfg.adx_trend_threshold
            and atr_ratio <= cfg.atr_squeeze_multiplier
        ):
            regime = Regime.RANGING
        elif (
            adx_val < cfg.adx_trend_threshold
            and not np.isnan(bw_val)
            and bw_val < cfg.bb_squeeze_threshold
        ):
            regime = Regime.RANGING
        elif adx_val >= cfg.adx_trend_threshold:
            regime = Regime.TRENDING
        else:
            regime = Regime.UNKNOWN   # borderline — prefer no-trade

        snap = RegimeSnapshot(
            symbol=symbol,
            regime=regime,
            adx=round(adx_val, 2),
            atr=round(atr_val, 4),
            atr_avg=round(atr_avg, 4),
            bb_width=round(bw_val, 5) if not np.isnan(bw_val) else 0.0,
            timestamp=now,
        )
        logger.info(
            "REGIME %s → %s  ADX=%.1f ATR=%.4f avg=%.4f ratio=%.2f BB_w=%s",
            symbol, regime.value, adx_val, atr_val, atr_avg, atr_ratio,
            f"{bw_val:.5f}" if not np.isnan(bw_val) else "n/a",
        )
        return snap

    except Exception as exc:
        return _unknown(f"exception: {exc}")
