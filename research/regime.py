#!/usr/bin/env python3
"""
research/regime.py — Simple market regime classifier for research use.

Classifies each bar into one of:
    trending_up   — strong uptrend (ADX high, +DI > -DI)
    trending_down — strong downtrend (ADX high, -DI > +DI)
    ranging       — no clear trend (ADX low, volatility normal)
    high_vol      — elevated volatility without strong trend
    panic         — sharp drawdown from recent high

Features used:
    ADX / +DI / -DI  — trend strength and direction
    ATR ratio         — current ATR vs rolling median ATR (volatility expansion)
    Drawdown          — % drop from rolling high (panic detection)

Usage:
    .venv/bin/python3 research/regime.py BTC/USDT 15m
    .venv/bin/python3 research/regime.py ETH/USDT 1h --bars 100
    .venv/bin/python3 research/regime.py BTC/USDT 15m --history
"""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class RegimeConfig:
    """All thresholds in one place. Adjust to taste."""

    # ADX
    adx_period: int = 14
    adx_trend_threshold: float = 25.0   # ADX above this = trending

    # ATR ratio
    atr_period: int = 14
    atr_median_window: int = 50         # rolling median of ATR for baseline
    atr_high_vol_ratio: float = 1.5     # ATR / median_ATR above this = high vol

    # Drawdown
    drawdown_window: int = 50           # rolling high lookback
    panic_drawdown_pct: float = 5.0     # drawdown > this = panic


DEFAULT_CONFIG = RegimeConfig()


# ── Indicators ────────────────────────────────────────────────────────────────

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Add +DI, -DI, ADX columns to a copy of df."""
    out = df.copy()
    high, low, close = out["high"], out["low"], out["close"]

    # True Range
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Directional movement
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Smoothed via Wilder's EMA (alpha = 1/period)
    atr = pd.Series(tr, index=df.index).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    plus_di_smooth = pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    minus_di_smooth = pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    out["plus_di"] = (plus_di_smooth / atr) * 100
    out["minus_di"] = (minus_di_smooth / atr) * 100

    dx = ((out["plus_di"] - out["minus_di"]).abs() / (out["plus_di"] + out["minus_di"])) * 100
    out["adx"] = dx.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    out["atr"] = atr

    return out


def compute_regime_features(df: pd.DataFrame, cfg: RegimeConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """Compute all regime features on a DataFrame with OHLCV columns."""
    out = compute_adx(df, cfg.adx_period)

    # ATR ratio: current ATR vs rolling median
    out["atr_median"] = out["atr"].rolling(window=cfg.atr_median_window, min_periods=1).median()
    out["atr_ratio"] = out["atr"] / out["atr_median"]

    # Drawdown from rolling high
    out["rolling_high"] = out["close"].rolling(window=cfg.drawdown_window, min_periods=1).max()
    out["drawdown_pct"] = ((out["close"] - out["rolling_high"]) / out["rolling_high"]) * 100

    return out


# ── Classifier ────────────────────────────────────────────────────────────────

def classify_bar(row: pd.Series, cfg: RegimeConfig = DEFAULT_CONFIG) -> str:
    """Classify a single bar into a regime label."""
    adx = row.get("adx", 0)
    plus_di = row.get("plus_di", 0)
    minus_di = row.get("minus_di", 0)
    atr_ratio = row.get("atr_ratio", 1.0)
    dd = row.get("drawdown_pct", 0)

    # Priority order: panic > trending > high_vol > ranging
    if dd <= -cfg.panic_drawdown_pct:
        return "panic"

    if adx >= cfg.adx_trend_threshold:
        return "trending_up" if plus_di > minus_di else "trending_down"

    if atr_ratio >= cfg.atr_high_vol_ratio:
        return "high_vol"

    return "ranging"


def classify_history(df: pd.DataFrame, cfg: RegimeConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """Add a 'regime' column to the DataFrame."""
    out = compute_regime_features(df, cfg)
    out["regime"] = out.apply(lambda row: classify_bar(row, cfg), axis=1)
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Market regime classifier")
    parser.add_argument("symbol", nargs="?", default="BTC/USDT", help="Trading pair")
    parser.add_argument("timeframe", nargs="?", default="15m", help="Candle interval")
    parser.add_argument("--bars", type=int, default=200, help="Number of bars to fetch")
    parser.add_argument("--history", action="store_true", help="Show last 20 regime classifications")
    parser.add_argument("--config", action="store_true", help="Print current config thresholds")
    args = parser.parse_args()

    if args.config:
        cfg = DEFAULT_CONFIG
        print(f"adx_period={cfg.adx_period}  adx_trend={cfg.adx_trend_threshold}")
        print(f"atr_period={cfg.atr_period}  atr_median_window={cfg.atr_median_window}  atr_high_vol_ratio={cfg.atr_high_vol_ratio}")
        print(f"drawdown_window={cfg.drawdown_window}  panic_drawdown_pct={cfg.panic_drawdown_pct}")
        return

    # Import data fetcher
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared_tools'))
    from data_fetcher import fetch_ohlcv

    print(f"Fetching {args.bars} bars of {args.symbol} @ {args.timeframe}...", file=sys.stderr)
    df = fetch_ohlcv(symbol=args.symbol, timeframe=args.timeframe, limit=args.bars, store=False)

    if df.empty or len(df) < 30:
        print(f"Insufficient data: {len(df)} bars", file=sys.stderr)
        sys.exit(1)

    result = classify_history(df)

    if args.history:
        cols = ["close", "adx", "plus_di", "minus_di", "atr_ratio", "drawdown_pct", "regime"]
        tail = result[cols].tail(20)
        for _, row in tail.iterrows():
            print(
                f"  close={row['close']:>10.2f}  ADX={row['adx']:5.1f}  "
                f"+DI={row['plus_di']:5.1f}  -DI={row['minus_di']:5.1f}  "
                f"ATR_r={row['atr_ratio']:4.2f}  DD={row['drawdown_pct']:+6.2f}%  "
                f"→ {row['regime']}"
            )
    else:
        last = result.iloc[-1]
        print(f"{args.symbol} @ {args.timeframe}")
        print(f"  Regime:   {last['regime']}")
        print(f"  ADX:      {last['adx']:.1f}  (+DI={last['plus_di']:.1f}  -DI={last['minus_di']:.1f})")
        print(f"  ATR ratio:{last['atr_ratio']:.2f}")
        print(f"  Drawdown: {last['drawdown_pct']:+.2f}%")

    # Regime distribution
    dist = result["regime"].value_counts()
    total = len(result)
    print(f"\n  Distribution ({total} bars):")
    for regime, count in dist.items():
        print(f"    {regime:<15} {count:>4}  ({count/total*100:5.1f}%)")


if __name__ == "__main__":
    main()
