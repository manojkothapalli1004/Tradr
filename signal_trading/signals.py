"""
signal_trading/signals.py — Signal engine.

Runs strategy functions directly (in-process) against OHLCV DataFrames.
Returns typed Signal objects — no subprocess, no JSON round-trip.

Key design decisions:
- Imports strategy functions from shared_strategies/spot — safe read-only use
- Each strategy function returns a DataFrame with a 'signal' column
  (1=buy, -1=sell, 0=hold). We read the LAST row only.
- Indicators dict is attached to Signal for journaling.
"""

import sys
import os
import logging
from datetime import datetime, timezone
from typing import Optional, List

import pandas as pd
import numpy as np

# Path injection — must happen before importing shared code
_TRADER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPOT_PATH = os.path.join(_TRADER_DIR, "shared_strategies", "spot")
if _SPOT_PATH not in sys.path:
    sys.path.insert(0, _SPOT_PATH)

from strategies import (          # noqa: E402
    sma_crossover_strategy,
    ema_crossover_strategy,
    rsi_strategy,
    bollinger_strategy,
    macd_strategy,
    mean_reversion_strategy,
    momentum_strategy,
    volume_weighted_strategy,
    triple_ema_strategy,
    rsi_macd_combo_strategy,
)

from signal_trading.config import (
    ASSETS, ACTIVE_STRATEGIES, ASSET_STRATEGY_EXCLUSIONS,
    StrategyParams, DEFAULT_STRATEGY_PARAMS,
)
from signal_trading.models import Signal, Direction, Regime, SignalStrength, new_trade_id

logger = logging.getLogger("signal_trading.signals")


# ── Strategy dispatcher ───────────────────────────────────────────────────────

def _run_strategy(
    name: str,
    df: pd.DataFrame,
    params: StrategyParams,
) -> Optional[pd.DataFrame]:
    """Run named strategy against df. Returns enriched df or None on error."""
    try:
        if name == "sma_crossover":
            return sma_crossover_strategy(df, params.sma_fast, params.sma_slow)
        if name == "ema_crossover":
            return ema_crossover_strategy(df, params.ema_fast, params.ema_slow)
        if name == "rsi":
            return rsi_strategy(df, params.rsi_period, params.rsi_overbought, params.rsi_oversold)
        if name == "bollinger_bands":
            return bollinger_strategy(df, params.bb_period, params.bb_std)
        if name == "macd":
            return macd_strategy(df, params.macd_fast, params.macd_slow, params.macd_signal)
        if name == "mean_reversion":
            return mean_reversion_strategy(df, params.mr_lookback, params.mr_entry_std, params.mr_exit_std)
        if name == "momentum":
            return momentum_strategy(df, params.mom_period, params.mom_threshold)
        if name == "volume_weighted":
            return volume_weighted_strategy(df, params.vw_sma_period, params.vw_vol_multiplier)
        if name == "triple_ema":
            return triple_ema_strategy(df, params.tema_short, params.tema_mid, params.tema_long)
        if name == "rsi_macd_combo":
            return rsi_macd_combo_strategy(
                df,
                params.combo_rsi_period,
                params.combo_rsi_oversold,
                params.combo_rsi_overbought,
                params.combo_macd_fast,
                params.combo_macd_slow,
                params.combo_macd_signal,
            )
    except Exception as exc:
        logger.warning("Strategy %s raised: %s", name, exc)
    return None


def _extract_indicators(name: str, result_df: pd.DataFrame) -> dict:
    """Pull the last row's indicator values for journaling."""
    row = result_df.iloc[-1]
    indicators = {"price": float(row.get("close", 0))}
    # Attach any indicator columns that exist
    for col in ["sma_fast", "sma_slow", "ema_fast", "ema_slow",
                "rsi", "bb_upper", "bb_lower", "bb_middle",
                "macd", "macd_signal", "macd_hist",
                "z_score", "roc", "volume_ratio",
                "ema_short", "ema_mid", "ema_long"]:
        if col in result_df.columns:
            val = row.get(col)
            if val is not None and not (isinstance(val, float) and np.isnan(val)):
                indicators[col] = round(float(val), 6)
    return indicators


# ── Public API ────────────────────────────────────────────────────────────────

def compute_signal(
    asset: str,
    strategy: str,
    df: pd.DataFrame,
    regime: Regime = Regime.UNKNOWN,
    params: Optional[StrategyParams] = None,
) -> Signal:
    """
    Compute a signal for one asset×strategy pair.

    Always returns a Signal — direction=None means HOLD.
    """
    now = datetime.now(timezone.utc)
    price = float(df["close"].iloc[-1]) if not df.empty else 0.0

    if params is None:
        params = DEFAULT_STRATEGY_PARAMS

    # Check asset-level exclusions
    excluded = ASSET_STRATEGY_EXCLUSIONS.get(asset, set())
    if strategy in excluded:
        return Signal(asset=asset, strategy=strategy, direction=None,
                      price=price, timestamp=now, regime=regime)

    result_df = _run_strategy(strategy, df, params)
    if result_df is None or result_df.empty:
        return Signal(asset=asset, strategy=strategy, direction=None,
                      price=price, timestamp=now, regime=regime)

    raw_signal = result_df["signal"].iloc[-1]

    direction: Optional[Direction] = None
    if raw_signal == 1 or raw_signal == 1.0:
        direction = Direction.LONG
    elif raw_signal == -1 or raw_signal == -1.0:
        direction = Direction.SHORT

    indicators = _extract_indicators(strategy, result_df)

    return Signal(
        asset=asset,
        strategy=strategy,
        direction=direction,
        price=price,
        timestamp=now,
        regime=regime,
        strength=SignalStrength.WEAK,   # regime filter upgrades to STRONG
        indicators=indicators,
    )


def compute_all_signals(
    asset: str,
    df: pd.DataFrame,
    regime: Regime = Regime.UNKNOWN,
    strategies: Optional[List[str]] = None,
    params: Optional[StrategyParams] = None,
) -> List[Signal]:
    """
    Run all active strategies for one asset. Returns list of all Signals
    (including holds — caller filters).
    """
    if strategies is None:
        strategies = ACTIVE_STRATEGIES
    if params is None:
        params = DEFAULT_STRATEGY_PARAMS

    signals = []
    for strat in strategies:
        sig = compute_signal(asset, strat, df, regime=regime, params=params)
        signals.append(sig)

    # Upgrade to STRONG if ≥2 strategies agree on direction
    directional = [s for s in signals if s.direction is not None]
    longs = sum(1 for s in directional if s.direction == Direction.LONG)
    shorts = sum(1 for s in directional if s.direction == Direction.SHORT)

    if longs >= 2:
        for s in signals:
            if s.direction == Direction.LONG:
                s.strength = SignalStrength.STRONG
    if shorts >= 2:
        for s in signals:
            if s.direction == Direction.SHORT:
                s.strength = SignalStrength.STRONG

    return signals
