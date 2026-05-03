"""
options_bot/strategies/vwap_trend_continuation.py

VWAP Trend Continuation — paper-trading hypothesis.

Uses 5-minute bars only. Session VWAP is computed from today's intraday bars.
Signal requires: TRENDING regime + EMA alignment + VWAP pullback + bounce.

All strategies are hypotheses under validation. No edge is assumed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
import pandas as pd

from options_bot.config import VWAP_CFG, VWAPConfig, OPTIONS_DATA_LIMITATION
from options_bot.models import Direction, OptionsSignal, Regime, RegimeSnapshot, SignalType
from options_bot.strategies.base import BaseOptionsStrategy

logger = logging.getLogger("options_bot.strategy.vwap")


class VWAPTrendContinuation(BaseOptionsStrategy):

    strategy_id   = SignalType.VWAP.value
    valid_regimes = (Regime.TRENDING,)

    def __init__(self, cfg: VWAPConfig = VWAP_CFG) -> None:
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

        # ── Session VWAP ──────────────────────────────────────────────────────
        today   = df_5m.index[-1].strftime("%Y-%m-%d")
        session = df_5m[df_5m.index.strftime("%Y-%m-%d") == today].copy()

        if len(session) < cfg.min_session_bars:
            self._no_signal(
                symbol,
                f"only {len(session)} session bars, need {cfg.min_session_bars}",
            )
            return None

        typical      = (session["high"] + session["low"] + session["close"]) / 3.0
        cum_vol      = session["volume"].cumsum().replace(0, np.nan)
        vwap_s       = (typical * session["volume"]).cumsum() / cum_vol

        if vwap_s.isna().all():
            self._no_signal(symbol, "VWAP all-NaN — session volume may be zero")
            return None

        vwap_now = float(vwap_s.iloc[-1])
        if np.isnan(vwap_now) or vwap_now <= 0:
            self._no_signal(symbol, f"VWAP invalid: {vwap_now}")
            return None

        # ── EMA alignment ─────────────────────────────────────────────────────
        ema_short = float(df_5m["close"].ewm(span=cfg.ema_short, adjust=False).mean().iloc[-1])
        ema_long  = float(df_5m["close"].ewm(span=cfg.ema_long,  adjust=False).mean().iloc[-1])

        up   = ema_short > ema_long
        down = ema_short < ema_long

        if not up and not down:
            self._no_signal(
                symbol,
                f"EMAs flat (ema{cfg.ema_short}={ema_short:.2f} ema{cfg.ema_long}={ema_long:.2f})",
            )
            return None

        # ── VWAP pullback ─────────────────────────────────────────────────────
        lb_session = session.iloc[-cfg.pullback_lookback - 1 : -1]
        lb_vwap    = vwap_s.loc[lb_session.index]
        tol        = vwap_now * cfg.touch_tolerance_pct
        touched    = False

        for idx, row in lb_session.iterrows():
            v = float(lb_vwap.loc[idx])
            if np.isnan(v):
                continue
            if abs(float(row["close"]) - v) <= tol or float(row["low"]) <= v <= float(row["high"]):
                touched = True
                break

        if not touched:
            self._no_signal(symbol, f"no VWAP touch in last {cfg.pullback_lookback} bars")
            return None

        # ── Bounce confirmation ───────────────────────────────────────────────
        close_px = float(session["close"].iloc[-1])

        if up:
            if close_px <= vwap_now:
                self._no_signal(symbol, f"uptrend: close {close_px:.2f} ≤ VWAP {vwap_now:.2f}")
                return None
            direction  = Direction.BULLISH
            entry_zone = f"above VWAP {vwap_now:.2f} after pullback"
            stop_logic = f"close back below VWAP {vwap_now:.2f}"
        else:
            if close_px >= vwap_now:
                self._no_signal(symbol, f"downtrend: close {close_px:.2f} ≥ VWAP {vwap_now:.2f}")
                return None
            direction  = Direction.BEARISH
            entry_zone = f"below VWAP {vwap_now:.2f} after pullback"
            stop_logic = f"close back above VWAP {vwap_now:.2f}"

        # ── Confidence ────────────────────────────────────────────────────────
        atr_ref    = regime.atr if regime.atr > 0 else vwap_now * 0.002
        dist       = abs(close_px - vwap_now)
        confidence = round(min(1.0, max(0.1, 0.4 + (dist / atr_ref) * 0.15)), 3)
        uq         = self._data_quality_score(df_5m, cfg.min_bars)

        codes: List[str] = [
            "vwap_pullback_bounce",
            "ema_aligned_bullish" if up else "ema_aligned_bearish",
        ]

        logger.info(
            "VWAP %s %s: close=%.2f vwap=%.2f ema%d=%.2f ema%d=%.2f conf=%.3f",
            symbol, direction.value, close_px, vwap_now,
            cfg.ema_short, ema_short, cfg.ema_long, ema_long, confidence,
        )

        return OptionsSignal(
            strategy_name=SignalType.VWAP,
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
                f"price closes through VWAP {vwap_now:.2f} before entry",
                "regime shifts away from TRENDING",
                f"EMA{cfg.ema_short} crosses EMA{cfg.ema_long} opposite direction",
            ],
            reason_codes=codes,
            liquidity_ok=True,
            spread_ok=True,
            underlying_quality_score=uq,
            indicators={
                "vwap":              round(vwap_now, 2),
                f"ema{cfg.ema_short}": round(ema_short, 2),
                f"ema{cfg.ema_long}":  round(ema_long, 2),
                "dist_from_vwap":    round(dist, 3),
                "session_bars":      len(session),
                "adx":               regime.adx,
                "atr":               regime.atr,
            },
        )
