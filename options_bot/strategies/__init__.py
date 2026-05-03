"""
options_bot/strategies — All five option signal strategy modules.

ALL_STRATEGIES is the authoritative list used by runner.py and journal.py.
Order does not affect routing — the router ranks independently.
"""
from __future__ import annotations

from options_bot.strategies.opening_range_breakout import OpeningRangeBreakout
from options_bot.strategies.vwap_trend_continuation import VWAPTrendContinuation
from options_bot.strategies.ema_trend_pullback import EMATrendPullback
from options_bot.strategies.relative_volume_momentum import RelativeVolumeMomentum
from options_bot.strategies.volatility_breakout import VolatilityBreakout

ALL_STRATEGIES: list = [
    OpeningRangeBreakout,
    VWAPTrendContinuation,
    EMATrendPullback,
    RelativeVolumeMomentum,
    VolatilityBreakout,
]

__all__ = [
    "OpeningRangeBreakout",
    "VWAPTrendContinuation",
    "EMATrendPullback",
    "RelativeVolumeMomentum",
    "VolatilityBreakout",
    "ALL_STRATEGIES",
]
