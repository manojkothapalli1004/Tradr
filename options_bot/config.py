"""
options_bot/config.py — Central configuration. All tunable parameters live here.
No magic numbers in any other module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Dict


# ── Safety ─────────────────────────────────────────────────────────────────────

PAPER_MODE: bool = True  # hard-wired; no live execution path exists in v1


# ── Data quality notice ─────────────────────────────────────────────────────────
# Attached to every fill record, contract estimate, and signal log.

OPTIONS_DATA_LIMITATION: str = (
    "[LIMITED-SIMULATOR] Data sourced from yfinance free tier (~15-min delayed). "
    "No live bid/ask available. Paper fills use a simplified theoretical pricing "
    "model and are NOT representative of real execution quality."
)


# ── Assets ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AssetConfig:
    ticker: str   # yfinance ticker
    label: str
    pip: float    # minimum price increment


ASSETS: Dict[str, AssetConfig] = {
    "SPY": AssetConfig(ticker="SPY", label="SPDR S&P 500 ETF", pip=0.01),
    "QQQ": AssetConfig(ticker="QQQ", label="Invesco QQQ ETF",  pip=0.01),
}


# ── Data fetching ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DataConfig:
    bar_timeframe: str = "5m"       # primary timeframe for all strategies
    bars_needed:   int = 200        # minimum bars for indicator warmup

    orb_timeframe:  str = "1m"      # 1-min bars for ORB opening range only
    orb_bars_needed: int = 90       # covers ~1.5h of session

    download_period_5m: str = "5d"
    download_period_1m: str = "1d"

    market_open_hour:    int = 9
    market_open_minute:  int = 30
    market_close_hour:   int = 16
    market_close_minute: int = 0

    # EOD force-flat: positions are closed at or after this time (ET).
    # Must be before market_close to allow fill simulation before chain data freezes.
    eod_close_hour:   int = 15
    eod_close_minute: int = 45


DATA_CFG = DataConfig()


# ── Contract selection ──────────────────────────────────────────────────────────
# Selection logic is implemented in contract_selector.py.
# These parameters define the configurable space only.

class StrikeMethod(str, Enum):
    ATM       = "atm"        # closest strike to underlying price
    DELTA     = "delta"      # target a specific delta
    OTM_FIXED = "otm_fixed"  # fixed number of strikes OTM


@dataclass(frozen=True)
class ContractConfig:
    # DTE range.  The selector picks the expiry closest to preferred_dte
    # within [min_dte, max_dte].  If no expiry lands near preferred_dte the
    # shortest available expiry in the range is used as the fallback.
    min_dte:       int = 1
    max_dte:       int = 7
    preferred_dte: int = 2

    # Strike selection
    strike_method: StrikeMethod = StrikeMethod.ATM
    delta_target:  float = 0.45   # used when strike_method == DELTA
    otm_strikes:   int   = 0      # retained for config compatibility when OTM_FIXED is selected

    # Liquidity filters (indicative only — data is delayed)
    min_open_interest: int   = 100
    max_spread_pct:    float = 0.20   # (ask - bid) / mid

    # Paper exit thresholds
    profit_target_pct: float = 12.0   # close at 12% premium gain
    stop_loss_pct:     float = 12.0   # close at 12% premium loss
    max_hold_days:     int   = 0      # same-day force exit; fastest safe config-supported hold setting

    # Position sizing cap (1 contract = 100 shares × per-share premium)
    max_premium_per_trade_usd: float = 1500.0


CONTRACT_CFG = ContractConfig()


# ── Regime engine ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RegimeConfig:
    adx_period:            int   = 14
    adx_trend_threshold:   float = 20.0   # ADX ≥ 20 → TRENDING
    adx_strong_threshold:  float = 35.0

    atr_period:                  int   = 14
    atr_avg_lookback:            int   = 20
    atr_expansion_multiplier:    float = 1.5   # ATR > 1.5× avg → EXPANDING
    atr_squeeze_multiplier:      float = 0.75  # ATR < 0.75× avg → RANGING

    bb_period:            int   = 20
    bb_std:               float = 2.0
    bb_squeeze_threshold: float = 0.015   # BB width / mid < 1.5% → squeeze


REGIME_CFG = RegimeConfig()


# ── Per-strategy parameters ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ORBConfig:
    orb_minutes:        int   = 30     # first N minutes define the opening range
    window_hours:       float = 2.5    # only signal within first N hours of session
    max_range_width_pct: float = 0.012 # range > 1.2% of price → ambiguous, skip
    vol_avg_lookback:   int   = 20
    min_vol_ratio:      float = 1.0    # breakout bar volume ≥ N× average

    @property
    def min_bars_5m(self) -> int:
        return self.vol_avg_lookback + 5

    @property
    def min_bars_1m(self) -> int:
        return self.orb_minutes + 10


@dataclass(frozen=True)
class VWAPConfig:
    ema_short:          int   = 9
    ema_long:           int   = 21
    pullback_lookback:  int   = 6
    touch_tolerance_pct: float = 0.0015  # within 0.15% of VWAP counts as touch
    min_session_bars:   int   = 8

    @property
    def min_bars(self) -> int:
        return max(50, self.ema_long + self.pullback_lookback + 5)


@dataclass(frozen=True)
class EMAConfig:
    ema_fast:           int   = 8
    ema_mid:            int   = 21
    ema_slow:           int   = 55
    pullback_lookback:  int   = 8
    touch_tolerance_pct: float = 0.002

    @property
    def min_bars(self) -> int:
        return self.ema_slow + self.pullback_lookback + 10


@dataclass(frozen=True)
class RVolConfig:
    vol_avg_lookback:    int   = 20
    confirm_bars:        int   = 5      # consecutive bars above threshold
    relvol_threshold:    float = 2.0
    relvol_ranging_mult: float = 1.5    # stricter in RANGING regime
    breakout_lookback:   int   = 20
    min_body_pct:        float = 0.40   # doji filter

    @property
    def min_bars(self) -> int:
        return self.vol_avg_lookback + self.breakout_lookback + self.confirm_bars + 5


@dataclass(frozen=True)
class VBKRConfig:
    bb_period:        int   = 20
    bb_std:           float = 2.0
    squeeze_lookback: int   = 10
    atr_period:       int   = 14
    atr_avg_lookback: int   = 20
    min_body_pct:     float = 0.45

    @property
    def min_bars(self) -> int:
        return self.bb_period + self.squeeze_lookback + self.atr_period + 10


ORB_CFG  = ORBConfig()
VWAP_CFG = VWAPConfig()
EMA_CFG  = EMAConfig()
RVOL_CFG = RVolConfig()
VBKR_CFG = VBKRConfig()


# ── Portfolio orchestration ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class RouterConfig:
    max_total_positions:    int = 2   # across all symbols
    max_per_symbol:         int = 1
    min_trades_for_ranking: int = 30  # ranking disabled below this per slot


ROUTER_CFG = RouterConfig()


# ── Risk ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskConfig:
    max_total_premium_deployed_usd: float = 3000.0
    portfolio_kill_switch_pct:      float = 50.0
    circuit_breaker_losses:         int   = 4
    circuit_breaker_pause_hours:    float = 24.0


RISK_CFG = RiskConfig()


# ── Execution (fill model is abstract; concrete model in execution.py) ──────────

@dataclass(frozen=True)
class ExecConfig:
    # "simplified_theoretical" — fills estimated from delayed chain data.
    # Always labeled LIMITED. Concrete implementation in execution.py.
    fill_model:               str   = "simplified_theoretical"
    slippage_conservative_pct: float = 0.02  # 2% cost penalty applied to all fills


EXEC_CFG = ExecConfig()


# ── Paths ────────────────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))

STATE_FILE = os.path.join(_HERE, "state.json")
LOG_FILE   = os.path.join(_HERE, "options_bot.log")
PID_FILE   = os.path.join(_HERE, "options_bot.pid")
