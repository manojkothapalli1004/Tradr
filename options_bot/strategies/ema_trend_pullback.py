"""
options_bot/strategies/ema_trend_pullback.py

EMA Trend Pullback — paper-trading hypothesis.

Uses 5-minute bars only. Signals only in the direction of a confirmed
three-EMA stack. Requires a pullback touch of the mid EMA followed by
a close back on the trend side. No counter-trend entries.

All strategies are hypotheses under validation. No edge is assumed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
import pandas as pd

from options_bot.config import EMA_CFG, EMAConfig, OPTIONS_DATA_LIMITATION
from options_bot.models import Direction, OptionsSignal, Regime, RegimeSnapshot, SignalType
from options_bot.strategies.base import BaseOptionsStrategy

logger = logging.getLogger("options_bot.strategy.ema")


class EMATrendPullback(BaseOptionsStrategy):

    strategy_id   = SignalType.EMA.value
    valid_regimes = (Regime.TRENDING,)

    def __init__(self, cfg: EMAConfig = EMA_CFG) -> None:
        self.cfg = cfg

    def _evaluate_impl(
        self,
        symbol: str,
        df_5m: pd.DataFrame,
        regime: RegimeSnapshot,
        df_1m: Optional[pd.DataFrame],
    ) -> Optional[OptionsSignal]:
        cfg = self.cfg

        # ── Bar count ─────────────────────────────────────────────────────────
        if len(df_5m) < cfg.min_bars:
            self._insufficient_data(symbol, len(df_5m), cfg.min_bars)
            return None

        # ── EMA values ────────────────────────────────────────────────────────
        close      = df_5m["close"]
        ema_fast_s = close.ewm(span=cfg.ema_fast, adjust=False).mean()
        ema_mid_s  = close.ewm(span=cfg.ema_mid,  adjust=False).mean()
        ema_slow_s = close.ewm(span=cfg.ema_slow, adjust=False).mean()

        ema_fast = float(ema_fast_s.iloc[-1])
        ema_mid  = float(ema_mid_s.iloc[-1])
        ema_slow = float(ema_slow_s.iloc[-1])
        close_px = float(close.iloc[-1])

        if any(np.isnan(v) for v in [ema_fast, ema_mid, ema_slow]):
            self._no_signal(symbol, "EMA NaN — insufficient warmup")
            return None

        # ── Full stack alignment ──────────────────────────────────────────────
        stack_up   = ema_fast > ema_mid > ema_slow
        stack_down = ema_fast < ema_mid < ema_slow

        if not stack_up and not stack_down:
            self._no_signal(
                symbol,
                f"EMAs not stacked: fast={ema_fast:.2f} mid={ema_mid:.2f} slow={ema_slow:.2f}",
            )
            return None

        # ── Trend integrity: price must not have closed beyond ema_slow ───────
        if stack_up and close_px < ema_slow:
            self._no_signal(symbol, f"uptrend: close {close_px:.2f} < EMA{cfg.ema_slow} {ema_slow:.2f}")
            return None
        if stack_down and close_px > ema_slow:
            self._no_signal(symbol, f"downtrend: close {close_px:.2f} > EMA{cfg.ema_slow} {ema_slow:.2f}")
            return None

        # ── Pullback: touch of ema_mid in lookback window ─────────────────────
        lb_close = close.iloc[-cfg.pullback_lookback - 1 : -1]
        lb_ema21 = ema_mid_s.iloc[-cfg.pullback_lookback - 1 : -1]
        lb_low   = df_5m["low"].iloc[-cfg.pullback_lookback - 1 : -1]
        lb_high  = df_5m["high"].iloc[-cfg.pullback_lookback - 1 : -1]
        tol      = ema_mid * cfg.touch_tolerance_pct

        touched = any(
            abs(float(c) - float(e)) <= tol
            or float(lo) <= float(e) <= float(hi)
            for c, e, lo, hi in zip(lb_close, lb_ema21, lb_low, lb_high)
        )

        if not touched:
            self._no_signal(symbol, f"no EMA{cfg.ema_mid} touch in last {cfg.pullback_lookback} bars")
            return None

        # ── Bounce confirmation: latest close on trend side of ema_mid ─────────
        if stack_up:
            if close_px <= ema_mid:
                self._no_signal(symbol, f"uptrend: close {close_px:.2f} not above EMA{cfg.ema_mid} {ema_mid:.2f}")
                return None
            direction  = Direction.BULLISH
            entry_zone = f"bounce above EMA{cfg.ema_mid} {ema_mid:.2f}"
            stop_logic = f"close below EMA{cfg.ema_mid} {ema_mid:.2f}"
        else:
            if close_px >= ema_mid:
                self._no_signal(symbol, f"downtrend: close {close_px:.2f} not below EMA{cfg.ema_mid} {ema_mid:.2f}")
                return None
            direction  = Direction.BEARISH
            entry_zone = f"bounce below EMA{cfg.ema_mid} {ema_mid:.2f}"
            stop_logic = f"close above EMA{cfg.ema_mid} {ema_mid:.2f}"

        # ── Confidence ────────────────────────────────────────────────────────
        atr_ref    = regime.atr if regime.atr > 0 else close_px * 0.002
        dist       = abs(close_px - ema_mid)
        confidence = round(min(1.0, max(0.1, 0.4 + (dist / atr_ref) * 0.2)), 3)
        uq         = self._data_quality_score(df_5m, cfg.min_bars)

        codes: List[str] = [
            "ema_triple_stack",
            "ema_mid_pullback_bounce",
            "trend_bullish" if stack_up else "trend_bearish",
        ]

        logger.info(
            "EMA %s %s: close=%.2f ema%d=%.2f ema%d=%.2f ema%d=%.2f conf=%.3f",
            symbol, direction.value, close_px,
            cfg.ema_fast, ema_fast, cfg.ema_mid, ema_mid, cfg.ema_slow, ema_slow,
            confidence,
        )

        return OptionsSignal(
            strategy_name=SignalType.EMA,
            symbol=symbol,
            direction=direction,
            timestamp=datetime.now(timezone.utc),
            regime_required=[r.value for r in self.valid_regimes],
            regime_at_signal=regime.regime,
            underlying_price=close_px,
            confidence_score=confidence,
            data_quality_ok=True,
            data_quality_note=OPTIONS_DATA_LIMITATION,
            entry_zone=entry_zone,
            stop_logic=stop_logic,
            target_logic="50% gain on entry premium",
            invalidation_conditions=[
                f"close back through EMA{cfg.ema_mid} before entry",
                f"close beyond EMA{cfg.ema_slow} against trend",
                "EMA stack alignment breaks",
                "regime shifts away from TRENDING",
            ],
            reason_codes=codes,
            liquidity_ok=True,
            spread_ok=True,
            underlying_quality_score=uq,
            indicators={
                f"ema{cfg.ema_fast}": round(ema_fast, 2),
                f"ema{cfg.ema_mid}":  round(ema_mid, 2),
                f"ema{cfg.ema_slow}": round(ema_slow, 2),
                "stack":              "up" if stack_up else "down",
                "dist_from_mid":      round(dist, 3),
                "adx":                regime.adx,
                "atr":                regime.atr,
            },
        )
