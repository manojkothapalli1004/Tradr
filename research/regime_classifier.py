"""
research/regime_classifier.py — Research-only market regime classifier.

Classifies regimes into: trending_up, trending_down, ranging, high_vol, panic.
Uses only OHLCV data via existing data layer (no new dependencies).

NOT imported by the main runtime. Designed for offline analysis, research
notebooks, and backtesting evaluation.

Indicators used:
    - ADX (14): trend strength (>25 = trending)
    - +DI / -DI: trend direction
    - ATR ratio: ATR / 30-bar ATR average (>2.0 = elevated volatility)
    - Returns drawdown: rolling 12-bar return (< -5% = panic candidate)
    - Realized vol: std(log returns) over 20 bars, annualized

Regime rules (evaluated top-to-bottom, first match wins):
    1. PANIC:       drawdown_12 < -5% AND atr_ratio > 2.0
    2. HIGH_VOL:    atr_ratio > 2.0 OR realized_vol > 80%
    3. TRENDING_UP: adx > 25 AND +DI > -DI
    4. TRENDING_DN: adx > 25 AND -DI > +DI
    5. RANGING:     fallback

All thresholds are explicit, configurable, and documented.
No HMM, no ML, no external libs beyond numpy/pandas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("research.regime_classifier")


# ── Regime enum (research-only, does NOT touch signal_trading.models.Regime) ──

class ResearchRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DN = "trending_dn"
    RANGING = "ranging"
    HIGH_VOL = "high_vol"
    PANIC = "panic"
    UNKNOWN = "unknown"


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RegimeClassifierConfig:
    """All thresholds in one place. Frozen to prevent accidental mutation."""

    # ADX
    adx_period: int = 14
    adx_trend_threshold: float = 25.0

    # ATR-based volatility
    atr_period: int = 14
    atr_lookback: int = 30          # bars for ATR average baseline
    atr_high_vol_multiplier: float = 2.0

    # Returns-based panic detection
    drawdown_bars: int = 12         # bars to measure recent drawdown
    panic_drawdown_pct: float = -5.0  # return threshold (negative %)

    # Realized volatility
    realized_vol_window: int = 20   # bars for rolling std(log returns)
    realized_vol_high_threshold: float = 0.80  # annualized (80%)

    # Annualization factor depends on timeframe; caller sets this
    # e.g. 4h bars: ~6 per day × 365 = 2190 bars/year
    bars_per_year: float = 2190.0   # default: 4h bars


DEFAULT_CFG = RegimeClassifierConfig()


# ── Snapshot ─────────────────────────────────────────────────────────────────

@dataclass
class RegimeClassification:
    """Full result of one classification call — inspectable and serializable."""
    asset: str
    regime: ResearchRegime
    timestamp: datetime

    # Raw indicator values (for audit / logging)
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    atr: float = 0.0
    atr_avg: float = 0.0
    atr_ratio: float = 0.0
    drawdown_12: float = 0.0        # 12-bar return (%)
    realized_vol: float = 0.0       # annualized realized vol

    def to_dict(self) -> Dict:
        return {
            "asset": self.asset,
            "regime": self.regime.value,
            "timestamp": self.timestamp.isoformat(),
            "adx": round(self.adx, 2),
            "plus_di": round(self.plus_di, 2),
            "minus_di": round(self.minus_di, 2),
            "atr": round(self.atr, 6),
            "atr_avg": round(self.atr_avg, 6),
            "atr_ratio": round(self.atr_ratio, 3),
            "drawdown_12": round(self.drawdown_12, 3),
            "realized_vol": round(self.realized_vol, 3),
        }

    def summary(self) -> str:
        return (
            f"{self.asset} → {self.regime.value:12s} | "
            f"ADX={self.adx:5.1f}  +DI={self.plus_di:5.1f}  -DI={self.minus_di:5.1f}  "
            f"ATR_ratio={self.atr_ratio:.2f}  "
            f"DD12={self.drawdown_12:+.2f}%  "
            f"RVol={self.realized_vol:.1%}"
        )


# ── Indicator math (self-contained, no import from signal_trading) ───────────

def _compute_adx(df: pd.DataFrame, period: int = 14):
    """ADX, +DI, -DI. Returns three pd.Series."""
    high, low, close = df["high"], df["low"], df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
    )

    alpha = 1 / period
    atr_smooth = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_smooth
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_smooth

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()

    return adx, plus_di, minus_di


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing)."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _rolling_return_pct(close: pd.Series, bars: int) -> pd.Series:
    """Simple percentage return over `bars` periods."""
    return (close / close.shift(bars) - 1) * 100


def _realized_vol(close: pd.Series, window: int, bars_per_year: float) -> pd.Series:
    """Annualized realized volatility from rolling std of log returns."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window).std() * np.sqrt(bars_per_year)


# ── Classifier ───────────────────────────────────────────────────────────────

def classify_regime(
    asset: str,
    df: pd.DataFrame,
    cfg: RegimeClassifierConfig = DEFAULT_CFG,
) -> RegimeClassification:
    """
    Classify the current market regime for `asset` given OHLCV data.

    Requires at least max(adx_period*3, atr_lookback+atr_period, drawdown_bars,
    realized_vol_window) bars of data. Returns UNKNOWN on insufficient data.

    This function is pure: it does not fetch data or produce side effects.
    """
    now = datetime.now(timezone.utc)
    min_rows = max(cfg.adx_period * 3, cfg.atr_lookback + cfg.atr_period,
                   cfg.drawdown_bars, cfg.realized_vol_window)

    if df is None or len(df) < min_rows:
        logger.warning("Insufficient data for %s (need %d, got %d)",
                       asset, min_rows, 0 if df is None else len(df))
        return RegimeClassification(asset=asset, regime=ResearchRegime.UNKNOWN,
                                    timestamp=now)

    try:
        # Compute indicators
        adx, plus_di, minus_di = _compute_adx(df, cfg.adx_period)
        atr = _compute_atr(df, cfg.atr_period)
        dd = _rolling_return_pct(df["close"], cfg.drawdown_bars)
        rvol = _realized_vol(df["close"], cfg.realized_vol_window,
                             cfg.bars_per_year)

        # Extract latest values
        adx_val = float(adx.iloc[-1])
        plus_di_val = float(plus_di.iloc[-1])
        minus_di_val = float(minus_di.iloc[-1])
        atr_val = float(atr.iloc[-1])
        atr_avg = float(atr.tail(cfg.atr_lookback).mean())
        atr_ratio = atr_val / atr_avg if atr_avg > 0 else 0.0
        dd_val = float(dd.iloc[-1]) if not np.isnan(dd.iloc[-1]) else 0.0
        rvol_val = float(rvol.iloc[-1]) if not np.isnan(rvol.iloc[-1]) else 0.0

        # ── Decision rules (order matters: first match wins) ──

        # 1. PANIC: sharp drawdown + elevated volatility
        if dd_val < cfg.panic_drawdown_pct and atr_ratio > cfg.atr_high_vol_multiplier:
            regime = ResearchRegime.PANIC

        # 2. HIGH_VOL: elevated ATR or extreme realized vol
        elif atr_ratio > cfg.atr_high_vol_multiplier or rvol_val > cfg.realized_vol_high_threshold:
            regime = ResearchRegime.HIGH_VOL

        # 3. TRENDING_UP: strong trend, bulls lead
        elif adx_val >= cfg.adx_trend_threshold and plus_di_val > minus_di_val:
            regime = ResearchRegime.TRENDING_UP

        # 4. TRENDING_DN: strong trend, bears lead
        elif adx_val >= cfg.adx_trend_threshold and minus_di_val > plus_di_val:
            regime = ResearchRegime.TRENDING_DN

        # 5. RANGING: default
        else:
            regime = ResearchRegime.RANGING

        return RegimeClassification(
            asset=asset,
            regime=regime,
            timestamp=now,
            adx=adx_val,
            plus_di=plus_di_val,
            minus_di=minus_di_val,
            atr=atr_val,
            atr_avg=atr_avg,
            atr_ratio=atr_ratio,
            drawdown_12=dd_val,
            realized_vol=rvol_val,
        )

    except Exception as exc:
        logger.error("Regime classification failed for %s: %s", asset, exc)
        return RegimeClassification(asset=asset, regime=ResearchRegime.UNKNOWN,
                                    timestamp=now)


# ── Batch + history helpers ──────────────────────────────────────────────────

def classify_history(
    asset: str,
    df: pd.DataFrame,
    cfg: RegimeClassifierConfig = DEFAULT_CFG,
) -> pd.DataFrame:
    """
    Classify regime for every bar in `df`. Returns a DataFrame aligned to df's
    index with columns: regime, adx, atr_ratio, drawdown_12, realized_vol.

    Useful for backtesting and visual inspection.
    """
    min_rows = max(cfg.adx_period * 3, cfg.atr_lookback + cfg.atr_period,
                   cfg.drawdown_bars, cfg.realized_vol_window)
    if df is None or len(df) < min_rows:
        return pd.DataFrame()

    adx, plus_di, minus_di = _compute_adx(df, cfg.adx_period)
    atr = _compute_atr(df, cfg.atr_period)
    dd = _rolling_return_pct(df["close"], cfg.drawdown_bars)
    rvol = _realized_vol(df["close"], cfg.realized_vol_window, cfg.bars_per_year)
    atr_avg = atr.rolling(cfg.atr_lookback).mean()
    atr_ratio = atr / atr_avg.replace(0, np.nan)

    result = pd.DataFrame(index=df.index)
    result["adx"] = adx
    result["plus_di"] = plus_di
    result["minus_di"] = minus_di
    result["atr_ratio"] = atr_ratio
    result["drawdown_12"] = dd
    result["realized_vol"] = rvol

    # Vectorized regime assignment (same priority order)
    regime = pd.Series(ResearchRegime.RANGING.value, index=df.index)
    regime[adx >= cfg.adx_trend_threshold] = np.where(
        plus_di[adx >= cfg.adx_trend_threshold] > minus_di[adx >= cfg.adx_trend_threshold],
        ResearchRegime.TRENDING_UP.value,
        ResearchRegime.TRENDING_DN.value,
    )
    high_vol_mask = (atr_ratio > cfg.atr_high_vol_multiplier) | (rvol > cfg.realized_vol_high_threshold)
    regime[high_vol_mask] = ResearchRegime.HIGH_VOL.value
    panic_mask = (dd < cfg.panic_drawdown_pct) & (atr_ratio > cfg.atr_high_vol_multiplier)
    regime[panic_mask] = ResearchRegime.PANIC.value

    result["regime"] = regime
    return result


# ── CLI entry point for quick local verification ─────────────────────────────

def _cli():
    """
    Run from trader/ directory:
        .venv/bin/python3 -m research.regime_classifier [--asset BTC] [--timeframe 4h]

    Prints current regime + last 5 bars of history for visual check.
    """
    import argparse
    import sys
    import os

    # Set up path so signal_trading imports work
    trader_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if trader_dir not in sys.path:
        sys.path.insert(0, trader_dir)

    from signal_trading.data import fetch_asset

    parser = argparse.ArgumentParser(description="Research regime classifier")
    parser.add_argument("--asset", default="BTC", help="Asset name (BTC, ETH)")
    parser.add_argument("--timeframe", default="4h", help="OHLCV timeframe")
    parser.add_argument("--bars", type=int, default=200, help="Number of candles")
    parser.add_argument("--history", type=int, default=10,
                        help="Number of recent bars to show history for")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    print(f"Fetching {args.bars} × {args.timeframe} candles for {args.asset}...")
    df = fetch_asset(args.asset, timeframe=args.timeframe, limit=args.bars)
    if df is None or df.empty:
        print(f"ERROR: Could not fetch data for {args.asset}")
        sys.exit(1)

    print(f"Got {len(df)} candles, last close: {df['close'].iloc[-1]:.2f}")
    print()

    # Current regime
    result = classify_regime(args.asset, df)
    if args.json:
        import json
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print("─── Current Regime ───")
        print(result.summary())
        print()

        # Recent history
        hist = classify_history(args.asset, df)
        if not hist.empty:
            tail = hist.tail(args.history).copy()
            tail["close"] = df["close"].reindex(tail.index)
            print(f"─── Last {args.history} bars ───")
            cols = ["close", "regime", "adx", "atr_ratio", "drawdown_12", "realized_vol"]
            display = tail[cols].copy()
            display["adx"] = display["adx"].map(lambda x: f"{x:.1f}")
            display["atr_ratio"] = display["atr_ratio"].map(lambda x: f"{x:.2f}")
            display["drawdown_12"] = display["drawdown_12"].map(lambda x: f"{x:+.2f}%")
            display["realized_vol"] = display["realized_vol"].map(lambda x: f"{x:.1%}")
            display["close"] = display["close"].map(lambda x: f"{x:.2f}")
            print(display.to_string())


if __name__ == "__main__":
    _cli()
