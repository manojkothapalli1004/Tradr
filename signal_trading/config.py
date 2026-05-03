"""
signal_trading/config.py — Central configuration for the signal trading system.

All tunable parameters live here. No magic numbers elsewhere.
"""

from dataclasses import dataclass, field
from typing import Dict, List


# ── Assets ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AssetConfig:
    name: str           # internal name: "BTC", "ETH", "GOLD"
    symbol: str         # ccxt symbol: "BTC/USDT", "ETH/USDT", "PAXG/USDT"
    exchange: str       # ccxt exchange id: "binanceus"
    pip: float          # minimum price unit for rounding
    label: str          # display label


ASSETS: Dict[str, AssetConfig] = {
    "BTC": AssetConfig(
        name="BTC", symbol="BTC/USDT", exchange="binanceus",
        pip=1.0, label="Bitcoin",
    ),
    "ETH": AssetConfig(
        name="ETH", symbol="ETH/USDT", exchange="binanceus",
        pip=0.01, label="Ethereum",
    ),
}

# Strategies to skip per asset (set of strategy IDs)
# Keep only the strongest currently supported live combinations:
# - BTC: macd, rsi_macd_combo
# - ETH: rsi, pairs_spread
ASSET_STRATEGY_EXCLUSIONS: Dict[str, set] = {
    "BTC": {"sma_crossover", "ema_crossover", "rsi", "bollinger_bands", "mean_reversion", "momentum", "volume_weighted", "triple_ema", "pairs_spread"},
    "ETH": {"sma_crossover", "ema_crossover", "bollinger_bands", "macd", "mean_reversion", "momentum", "volume_weighted", "triple_ema", "rsi_macd_combo"},
}

# PAXG is intentionally disabled by removing it from ASSETS above.


# ── Timeframes ──────────────────────────────────────────────────────────────

PRIMARY_TIMEFRAME = "15m"    # used for all signal generation
REGIME_TIMEFRAME = "4h"      # higher timeframe for regime filter
CANDLES_NEEDED = 200         # minimum candles to compute all indicators


# ── Signal Engine ───────────────────────────────────────────────────────────

@dataclass
class StrategyParams:
    """Per-strategy tunable parameters."""
    enabled: bool = True

    # SMA crossover
    sma_fast: int = 20
    sma_slow: int = 50

    # EMA crossover
    ema_fast: int = 12
    ema_slow: int = 26

    # RSI
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # Bollinger Bands
    bb_period: int = 20
    bb_std: float = 2.0

    # MACD
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # Mean reversion
    mr_lookback: int = 30
    mr_entry_std: float = 1.5
    mr_exit_std: float = 0.5

    # Momentum (ROC)
    mom_period: int = 14
    mom_threshold: float = 3.0          # % — lowered from 5% (fires more often)

    # Volume weighted
    vw_sma_period: int = 20
    vw_vol_multiplier: float = 1.5

    # Triple EMA
    tema_short: int = 8
    tema_mid: int = 21
    tema_long: int = 55

    # RSI+MACD combo
    combo_rsi_period: int = 14
    combo_rsi_oversold: float = 40.0
    combo_rsi_overbought: float = 60.0
    combo_macd_fast: int = 12
    combo_macd_slow: int = 26
    combo_macd_signal: int = 9


DEFAULT_STRATEGY_PARAMS = StrategyParams()

# Strategies to run (must match IDs in shared_strategies/spot/strategies.py)
ACTIVE_STRATEGIES: List[str] = [
    "sma_crossover",
    "ema_crossover",
    "rsi",
    "bollinger_bands",
    "macd",
    "mean_reversion",
    "momentum",
    "volume_weighted",
    "triple_ema",
    "rsi_macd_combo",
]


# ── Regime Filter ───────────────────────────────────────────────────────────

@dataclass
class RegimeConfig:
    """Market regime detection parameters."""
    adx_period: int = 14
    adx_trend_threshold: float = 25.0   # ADX > 25 → trending market
    adx_strong_threshold: float = 40.0  # ADX > 40 → strong trend

    atr_period: int = 14
    atr_volatile_multiplier: float = 2.0   # ATR > 2× 30-day avg → volatile

    # Which strategies are suitable for each regime
    # "trend" strategies: follow direction; "range" strategies: mean reversion
    trend_strategies: tuple = (
        "sma_crossover", "ema_crossover", "momentum", "triple_ema",
        "macd", "volume_weighted", "rsi_macd_combo",
    )
    range_strategies: tuple = (
        "rsi", "bollinger_bands", "mean_reversion",
    )
    # "volatile" regime: block all new entries (risk-off)
    block_in_volatile: bool = True


REGIME_CFG = RegimeConfig()


# ── Risk Engine ─────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    """Position sizing and risk management parameters."""
    # Per-trade sizing
    initial_capital_per_asset: float = 1000.0   # USD per asset
    trade_size_usd: float = 50.0                 # fixed $50 per trade
    max_concurrent_per_asset: int = 3            # max open trades per asset

    # Stop / target
    stop_loss_pct: float = 0.5          # hard stop (% against entry)
    take_profit_pct: float = 0.8        # hard take profit
    trailing_stop_pct: float = 0.5      # trailing stop from peak
    max_hold_hours: float = 0.5         # max hold time before forced exit

    # Circuit breaker (per asset×strategy pair)
    circuit_breaker_losses: int = 4     # consecutive losses to trigger
    circuit_breaker_pause_hours: float = 2.0

    # Portfolio kill switch
    portfolio_kill_switch_pct: float = 15.0   # portfolio drawdown % to halt all trading

    # Fee model (Binance US spot taker)
    fee_pct: float = 0.0      # FEES ZEROED 2026-04-21 (temporary; restore to 0.001 for 0.1% Binance US spot taker)


RISK_CFG = RiskConfig()


# ── Portfolio Safety (daily drawdown staged protection) ────────────────────

@dataclass(frozen=True)
class PortfolioSafetyConfig:
    """Daily drawdown staged safety thresholds."""
    reduced_risk_pct: float = 1.5       # daily dd% → REDUCED_RISK
    no_new_risk_pct: float = 3.0        # daily dd% → NO_NEW_RISK
    hard_lock_pct: float = 5.0          # daily dd% → HARD_LOCKED
    reduced_size_multiplier: float = 0.5  # trade size multiplier in REDUCED_RISK


SAFETY_CFG = PortfolioSafetyConfig()


# ── Execution ───────────────────────────────────────────────────────────────

@dataclass
class ExecutionConfig:
    """Paper execution parameters.

    Slippage model:
    - base_slippage_bps: fixed adverse cost in basis points (always applied)
    - vol_bps_per_unit: extra bps per unit of ATR/price above calm baseline
    - vol_calm_atr_ratio: ATR/price considered calm (no vol penalty below this)
    - size_bps_per_sqrt: extra bps per sqrt(orderUSD/size_ref_usd), 0 = disabled
    - size_ref_usd: reference order size for sqrt scaling
    - jitter_bps: random adverse-only jitter in bps

    All slippage is ADVERSE: buys fill above mid, sells fill below mid.
    Total slippage (calm market): ~3-5 bps.
    Total slippage (volatile market, ATR/price=1.5%): ~13-15 bps.
    """
    # Slippage components
    base_slippage_bps: float = 3.0       # fixed adverse cost
    vol_bps_per_unit: float = 10.0       # bps per unit of excess ATR/price
    vol_calm_atr_ratio: float = 0.005    # ATR/price baseline (0.5%)
    size_bps_per_sqrt: float = 0.0       # disabled by default; set ~1.0 for large capital
    size_ref_usd: float = 10000.0        # reference size for sqrt scaling
    jitter_bps: float = 2.0             # adverse-only random noise

    # Legacy field (deprecated, kept for backward compat with old configs)
    slippage_pct: float = 0.0005

    fill_at: str = "close"               # "close" or "next_open"


EXEC_CFG = ExecutionConfig()


# ── Paths ────────────────────────────────────────────────────────────────────

import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TRADER_DIR = os.path.dirname(_THIS_DIR)

STATE_FILE = os.path.join(_THIS_DIR, "state.json")
LOG_FILE = os.path.join(_THIS_DIR, "signal_trading.log")
PID_FILE = os.path.join(_THIS_DIR, "signal_trading.pid")
HARD_LOCK_FILE = os.path.join(_THIS_DIR, "HARD_LOCK")

# Path to shared_tools so we can import data_fetcher
SHARED_TOOLS_PATH = os.path.join(_TRADER_DIR, "shared_tools")
SHARED_STRATEGIES_SPOT_PATH = os.path.join(_TRADER_DIR, "shared_strategies", "spot")
