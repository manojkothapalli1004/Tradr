"""
Strategy engine — modular strategy framework with 8+ configurable strategies.
Each strategy takes a DataFrame with OHLCV data and returns it with a 'signal' column.
signal: 1 = buy, -1 = sell, 0 = hold
"""

import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional, Callable
from indicators import sma, ema


# ─────────────────────────────────────────────
# Base strategy registry
# ─────────────────────────────────────────────

STRATEGY_REGISTRY: Dict[str, dict] = {}


def register_strategy(name: str, description: str, default_params: dict):
    """Decorator to register a strategy function."""
    def decorator(fn):
        STRATEGY_REGISTRY[name] = {
            "fn": fn,
            "description": description,
            "default_params": default_params,
        }
        return fn
    return decorator


def get_strategy(name: str) -> dict:
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGY_REGISTRY.keys())}")
    return STRATEGY_REGISTRY[name]


def list_strategies() -> List[str]:
    return list(STRATEGY_REGISTRY.keys())


def apply_strategy(name: str, df: pd.DataFrame, params: Optional[dict] = None) -> pd.DataFrame:
    """Apply a named strategy with optional parameter overrides."""
    strat = get_strategy(name)
    p = {**strat["default_params"], **(params or {})}
    return strat["fn"](df, **p)


# ─────────────────────────────────────────────
# Strategy implementations
# ─────────────────────────────────────────────

@register_strategy(
    "sma_crossover",
    "SMA Crossover — buy when fast SMA crosses above slow SMA",
    {"fast_period": 20, "slow_period": 50}
)
def sma_crossover_strategy(df: pd.DataFrame, fast_period: int = 20, slow_period: int = 50) -> pd.DataFrame:
    result = df.copy()
    result["sma_fast"] = sma(result["close"], fast_period)
    result["sma_slow"] = sma(result["close"], slow_period)
    result["position"] = np.where(result["sma_fast"] > result["sma_slow"], 1, 0)
    result["signal"] = result["position"].diff()
    return result


@register_strategy(
    "ema_crossover",
    "EMA Crossover — faster response than SMA crossover",
    {"fast_period": 12, "slow_period": 26}
)
def ema_crossover_strategy(df: pd.DataFrame, fast_period: int = 12, slow_period: int = 26) -> pd.DataFrame:
    result = df.copy()
    result["ema_fast"] = ema(result["close"], fast_period)
    result["ema_slow"] = ema(result["close"], slow_period)
    result["position"] = np.where(result["ema_fast"] > result["ema_slow"], 1, 0)
    result["signal"] = result["position"].diff()
    return result


@register_strategy(
    "rsi",
    "RSI — buy at oversold, sell at overbought",
    {"period": 14, "overbought": 70, "oversold": 30}
)
def rsi_strategy(df: pd.DataFrame, period: int = 14, overbought: float = 70, oversold: float = 30) -> pd.DataFrame:
    result = df.copy()
    delta = result["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    result["rsi"] = 100 - (100 / (1 + rs))
    result["signal"] = 0
    result.loc[(result["rsi"] > oversold) & (result["rsi"].shift(1) <= oversold), "signal"] = 1
    result.loc[(result["rsi"] < overbought) & (result["rsi"].shift(1) >= overbought), "signal"] = -1
    return result


@register_strategy(
    "bollinger_bands",
    "Bollinger Bands — mean reversion at band touches",
    {"period": 20, "num_std": 2.0}
)
def bollinger_strategy(df: pd.DataFrame, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    result = df.copy()
    result["bb_middle"] = sma(result["close"], period)
    rolling_std = result["close"].rolling(window=period).std()
    result["bb_upper"] = result["bb_middle"] + (rolling_std * num_std)
    result["bb_lower"] = result["bb_middle"] - (rolling_std * num_std)
    result["signal"] = 0
    result.loc[(result["close"] > result["bb_lower"]) & (result["close"].shift(1) <= result["bb_lower"].shift(1)), "signal"] = 1
    result.loc[(result["close"] < result["bb_upper"]) & (result["close"].shift(1) >= result["bb_upper"].shift(1)), "signal"] = -1
    return result


@register_strategy(
    "macd",
    "MACD — buy/sell on MACD line crossing signal line",
    {"fast_period": 12, "slow_period": 26, "signal_period": 9}
)
def macd_strategy(df: pd.DataFrame, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> pd.DataFrame:
    result = df.copy()
    ema_fast = ema(result["close"], fast_period)
    ema_slow = ema(result["close"], slow_period)
    result["macd_line"] = ema_fast - ema_slow
    result["macd_signal"] = ema(result["macd_line"], signal_period)
    result["macd_hist"] = result["macd_line"] - result["macd_signal"]
    # Signal on crossovers
    result["position"] = np.where(result["macd_line"] > result["macd_signal"], 1, 0)
    result["signal"] = result["position"].diff()
    return result


@register_strategy(
    "mean_reversion",
    "Mean Reversion — buy when price is N std below mean, sell when above",
    {"lookback": 30, "entry_std": 1.5, "exit_std": 0.5}
)
def mean_reversion_strategy(df: pd.DataFrame, lookback: int = 30, entry_std: float = 1.5, exit_std: float = 0.5) -> pd.DataFrame:
    result = df.copy()
    result["rolling_mean"] = result["close"].rolling(window=lookback).mean()
    result["rolling_std"] = result["close"].rolling(window=lookback).std()
    result["z_score"] = (result["close"] - result["rolling_mean"]) / result["rolling_std"]
    result["signal"] = 0
    # Buy when z-score crosses up through -entry_std
    result.loc[(result["z_score"] > -entry_std) & (result["z_score"].shift(1) <= -entry_std), "signal"] = 1
    # Sell when z-score crosses down through +exit_std
    result.loc[(result["z_score"] < exit_std) & (result["z_score"].shift(1) >= exit_std), "signal"] = -1
    return result


@register_strategy(
    "momentum",
    "Momentum — buy on strong upward momentum, sell on reversal",
    {"roc_period": 14, "threshold": 5.0}
)
def momentum_strategy(df: pd.DataFrame, roc_period: int = 14, threshold: float = 5.0) -> pd.DataFrame:
    result = df.copy()
    result["roc"] = ((result["close"] - result["close"].shift(roc_period)) / result["close"].shift(roc_period)) * 100
    result["signal"] = 0
    # Buy when ROC crosses above threshold
    result.loc[(result["roc"] > threshold) & (result["roc"].shift(1) <= threshold), "signal"] = 1
    # Sell when ROC crosses below -threshold
    result.loc[(result["roc"] < -threshold) & (result["roc"].shift(1) >= -threshold), "signal"] = -1
    return result


@register_strategy(
    "volume_weighted",
    "Volume-Weighted — confirms trend with volume analysis",
    {"sma_period": 20, "vol_multiplier": 1.5}
)
def volume_weighted_strategy(df: pd.DataFrame, sma_period: int = 20, vol_multiplier: float = 1.5) -> pd.DataFrame:
    result = df.copy()
    result["price_sma"] = sma(result["close"], sma_period)
    result["vol_sma"] = sma(result["volume"], sma_period)
    result["high_volume"] = result["volume"] > (result["vol_sma"] * vol_multiplier)
    result["signal"] = 0
    # Buy: price crosses above SMA with high volume
    price_cross_up = (result["close"] > result["price_sma"]) & (result["close"].shift(1) <= result["price_sma"].shift(1))
    result.loc[price_cross_up & result["high_volume"], "signal"] = 1
    # Sell: price crosses below SMA with high volume
    price_cross_down = (result["close"] < result["price_sma"]) & (result["close"].shift(1) >= result["price_sma"].shift(1))
    result.loc[price_cross_down & result["high_volume"], "signal"] = -1
    return result


@register_strategy(
    "triple_ema",
    "Triple EMA — trend confirmation using 3 EMAs (short/mid/long)",
    {"short_period": 8, "mid_period": 21, "long_period": 55}
)
def triple_ema_strategy(df: pd.DataFrame, short_period: int = 8, mid_period: int = 21, long_period: int = 55) -> pd.DataFrame:
    result = df.copy()
    result["ema_short"] = ema(result["close"], short_period)
    result["ema_mid"] = ema(result["close"], mid_period)
    result["ema_long"] = ema(result["close"], long_period)
    # All three aligned bullish
    bullish = (result["ema_short"] > result["ema_mid"]) & (result["ema_mid"] > result["ema_long"])
    result["position"] = np.where(bullish, 1, 0)
    result["signal"] = result["position"].diff()
    return result


@register_strategy(
    "rsi_macd_combo",
    "RSI+MACD Combo — dual confirmation for higher quality signals",
    {"rsi_period": 14, "rsi_oversold": 35, "rsi_overbought": 65,
     "macd_fast": 12, "macd_slow": 26, "macd_signal": 9}
)
def rsi_macd_combo_strategy(df: pd.DataFrame,
                             rsi_period: int = 14, rsi_oversold: float = 35, rsi_overbought: float = 65,
                             macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9) -> pd.DataFrame:
    result = df.copy()
    # RSI
    delta = result["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/rsi_period, min_periods=rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/rsi_period, min_periods=rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss
    result["rsi"] = 100 - (100 / (1 + rs))
    # MACD
    ema_fast = ema(result["close"], macd_fast)
    ema_slow = ema(result["close"], macd_slow)
    result["macd_line"] = ema_fast - ema_slow
    result["macd_signal_line"] = ema(result["macd_line"], macd_signal)
    # Combined signals
    result["signal"] = 0
    # Buy: RSI recovering from oversold AND MACD bullish cross
    macd_bull = (result["macd_line"] > result["macd_signal_line"]) & (result["macd_line"].shift(1) <= result["macd_signal_line"].shift(1))
    rsi_ok = result["rsi"] < 50  # RSI not overbought
    result.loc[macd_bull & rsi_ok, "signal"] = 1
    # Sell: RSI dropping from overbought AND MACD bearish cross
    macd_bear = (result["macd_line"] < result["macd_signal_line"]) & (result["macd_line"].shift(1) >= result["macd_signal_line"].shift(1))
    rsi_high = result["rsi"] > 50
    result.loc[macd_bear & rsi_high, "signal"] = -1
    return result


@register_strategy(
    "pairs_spread",
    "Pairs/Spread Trading — trade z-score of price ratio between two assets (needs 'close_b' column)",
    {"lookback": 30, "entry_z": 2.0, "exit_z": 0.5}
)
def pairs_spread_strategy(df: pd.DataFrame, lookback: int = 30, entry_z: float = 2.0, exit_z: float = 0.5) -> pd.DataFrame:
    """
    Stat arb / pairs trading on spread. Requires 'close_b' column for the second asset.
    If 'close_b' is not present, uses close price ratio to its own rolling mean (self-mean-reversion).
    """
    result = df.copy()
    if "close_b" in result.columns:
        result["spread"] = result["close"] / result["close_b"]
    else:
        result["spread"] = result["close"]

    result["spread_mean"] = result["spread"].rolling(window=lookback).mean()
    result["spread_std"] = result["spread"].rolling(window=lookback).std()
    result["z_score"] = (result["spread"] - result["spread_mean"]) / result["spread_std"]
    result["signal"] = 0
    # Buy when z-score crosses up through -entry_z (spread is cheap)
    result.loc[(result["z_score"] > -entry_z) & (result["z_score"].shift(1) <= -entry_z), "signal"] = 1
    # Sell when z-score crosses down through +exit_z (spread normalizes)
    result.loc[(result["z_score"] < exit_z) & (result["z_score"].shift(1) >= exit_z), "signal"] = -1
    return result


@register_strategy(
    "orb",
    "Opening Range Breakout — buy/sell when price breaks prior N-bar range high/low (research candidate)",
    {"orb_bars": 4, "confirm_bars": 1}
)
def orb_strategy(df: pd.DataFrame, orb_bars: int = 4, confirm_bars: int = 1) -> pd.DataFrame:
    """
    Pseudo-ORB on rolling bars. orb_bars defines the 'range' (e.g. 4 × 15m = 1h).
    Buy when close breaks above the prior range high; sell when it breaks below the range low.
    Labeled as research hypothesis — not a proven edge.
    """
    result = df.copy()
    result["orb_high"] = result["high"].rolling(window=orb_bars).max().shift(confirm_bars)
    result["orb_low"] = result["low"].rolling(window=orb_bars).min().shift(confirm_bars)
    result["signal"] = 0
    result.loc[
        (result["close"] > result["orb_high"]) & (result["close"].shift(1) <= result["orb_high"].shift(1)),
        "signal"
    ] = 1
    result.loc[
        (result["close"] < result["orb_low"]) & (result["close"].shift(1) >= result["orb_low"].shift(1)),
        "signal"
    ] = -1
    return result


@register_strategy(
    "vwap_trend",
    "VWAP Trend — buy on VWAP reclaim, sell on VWAP rejection, RSI-filtered (research candidate)",
    {"vwap_period": 20, "rsi_period": 14, "rsi_filter": 50}
)
def vwap_trend_strategy(df: pd.DataFrame, vwap_period: int = 20, rsi_period: int = 14, rsi_filter: float = 50) -> pd.DataFrame:
    """
    Rolling VWAP computed over vwap_period bars. Buy when price reclaims VWAP from below
    with RSI > rsi_filter; sell when it rejects and crosses below with RSI < rsi_filter.
    Labeled as research hypothesis — not a proven edge.
    """
    result = df.copy()
    typical_price = (result["high"] + result["low"] + result["close"]) / 3
    result["vwap"] = (
        (typical_price * result["volume"]).rolling(window=vwap_period).sum()
        / result["volume"].rolling(window=vwap_period).sum()
    )
    delta = result["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / rsi_period, min_periods=rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_period, min_periods=rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss
    result["rsi"] = 100 - (100 / (1 + rs))
    result["signal"] = 0
    reclaim = (result["close"] > result["vwap"]) & (result["close"].shift(1) <= result["vwap"].shift(1))
    result.loc[reclaim & (result["rsi"] > rsi_filter), "signal"] = 1
    reject = (result["close"] < result["vwap"]) & (result["close"].shift(1) >= result["vwap"].shift(1))
    result.loc[reject & (result["rsi"] < rsi_filter), "signal"] = -1
    return result


@register_strategy(
    "break_retest",
    "Break-and-Retest — two-phase: breakout detection then retest of broken level (research candidate)",
    {"lookback": 20, "retest_window": 8, "pullback_pct": 1.5, "ema_trend_period": 50}
)
def break_retest_strategy(df: pd.DataFrame,
                          lookback: int = 20,
                          retest_window: int = 8,
                          pullback_pct: float = 1.5,
                          ema_trend_period: int = 50) -> pd.DataFrame:
    """
    Two-phase break-and-retest state machine (vectorized across the full dataframe).

    Phase 1 — Breakout:
        Close crosses above the prior N-bar resistance high (or below support low)
        for the first time. The broken level is recorded on that edge bar only
        (not on every bar where price stays above the level).

    Phase 2 — Retest:
        Within the next retest_window bars (e.g. 8 × 15m = 2h), price pulls back
        within pullback_pct% BELOW the broken resistance (which now acts as support).
        For shorts, price pulls back within pullback_pct% ABOVE broken support.

    Phase 3 — Confirmation (entry signal):
        On the bar immediately AFTER a retest bar, price closes BACK ABOVE the
        broken level AND above the trend EMA. Signal fires here.
        Mirror for shorts: close back below broken support AND below EMA.

    Minimum sequence: breakout bar → retest bar → confirmation bar (3 distinct bars).
    The breakout bar cannot also be the retest bar (close > broken level on breakout,
    close < broken level required for retest — mutually exclusive).

    Parameters:
        lookback        Rolling window for resistance/support level (bars).
        retest_window   Max bars after breakout to expect a valid retest.
        pullback_pct    How far (%) price can dip below the broken level and still
                        count as a retest rather than a breakdown. 1.5% is
                        reasonable for 15m BTC/ETH where single bars move 0.3–1%.
        ema_trend_period EMA period for trend direction filter.

    Labeled as research hypothesis — not a proven edge.
    """
    result = df.copy()

    # Trend filter
    result["ema_trend"] = ema(result["close"], ema_trend_period)

    # Rolling resistance and support — shifted by 1 to avoid look-ahead
    resist  = result["high"].rolling(window=lookback).max().shift(1)
    support = result["low"].rolling(window=lookback).min().shift(1)

    # ── Phase 1: Edge detection — first bar that crosses the level ────────────
    # We only record the broken level on the very first crossing bar (edge),
    # not on every bar where price stays above the level. This prevents the
    # "breakout level" from updating as the rolling window advances.
    above_resist  = result["close"] > resist
    below_support = result["close"] < support

    breakout_up_edge   = above_resist  & ~above_resist.shift(1, fill_value=False)
    breakout_down_edge = below_support & ~below_support.shift(1, fill_value=False)

    # Record the broken level at the edge bar; forward-fill for retest_window bars
    # so subsequent bars know which level to watch for a retest.
    broken_resist  = resist.where(breakout_up_edge).ffill(limit=retest_window)
    broken_support = support.where(breakout_down_edge).ffill(limit=retest_window)

    # ── Phase 2: Retest zone ──────────────────────────────────────────────────
    # Long retest: price pulls back BELOW the broken resistance (now potential support)
    # but not more than pullback_pct% below it. close < broken_resist is required
    # (genuine pullback, not still above it), so this cannot fire on the breakout bar.
    in_retest_long = (
        broken_resist.notna()
        & (result["close"] < broken_resist)
        & (result["close"] >= broken_resist * (1 - pullback_pct / 100))
    )

    # Short retest: price pulls back ABOVE broken support but not more than pullback_pct% above.
    in_retest_short = (
        broken_support.notna()
        & (result["close"] > broken_support)
        & (result["close"] <= broken_support * (1 + pullback_pct / 100))
    )

    # ── Phase 3: Confirmation — fires on the bar after a retest ──────────────
    # Long: previous bar was in retest zone; current bar closes back above the
    # broken level (reclaimed) and above the trend EMA.
    # We use .shift(1) on broken_resist to reference the level at the retest bar —
    # still valid because it was within the ffill window at that point.
    result["signal"] = 0

    result.loc[
        in_retest_long.shift(1, fill_value=False)
        & (result["close"] > broken_resist.shift(1))
        & (result["close"] > result["ema_trend"]),
        "signal"
    ] = 1

    result.loc[
        in_retest_short.shift(1, fill_value=False)
        & (result["close"] < broken_support.shift(1))
        & (result["close"] < result["ema_trend"]),
        "signal"
    ] = -1

    return result


if __name__ == "__main__":
    import sys
    import json
    if "--list-json" in sys.argv:
        print(json.dumps([{"id": name, "description": STRATEGY_REGISTRY[name]["description"]} for name in list_strategies()]))
    else:
        print(f"Registered strategies: {list_strategies()}")
        for name in list_strategies():
            s = STRATEGY_REGISTRY[name]
            print(f"  {name}: {s['description']}")
            print(f"    Defaults: {s['default_params']}")
