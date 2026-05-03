"""
options_bot/strategies/relative_volume_momentum.py

Relative Volume Momentum Breakout — paper-trading hypothesis.

Uses 5-minute bars. Requires sustained elevated volume (multiple consecutive
bars, not a single spike) plus a directional price breakout with a strong
bar body. Extra volume threshold applied in RANGING regime.

All strategies are hypotheses under validation. No edge is assumed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
import pandas as pd

from options_bot.config import RVOL_CFG, RVolConfig, OPTIONS_DATA_LIMITATION
from options_bot.models import Direction, OptionsSignal, Regime, RegimeSnapshot, SignalType
from options_bot.strategies.base import BaseOptionsStrategy

logger = logging.getLogger("options_bot.strategy.rvol")


class RelativeVolumeMomentum(BaseOptionsStrategy):

    strategy_id   = SignalType.RVOL.value
    # Valid in TRENDING and EXPANDING; RANGING allowed but stricter threshold applied.
    # UNKNOWN always blocked by base class.
    valid_regimes = (Regime.TRENDING, Regime.EXPANDING, Regime.RANGING)

    def __init__(self, cfg: RVolConfig = RVOL_CFG) -> None:
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

        # ── Volume threshold (stricter in RANGING) ────────────────────────────
        threshold = (
            cfg.relvol_threshold * cfg.relvol_ranging_mult
            if regime.regime == Regime.RANGING
            else cfg.relvol_threshold
        )

        # ── Baseline and confirmation windows ─────────────────────────────────
        # Baseline: vol_avg_lookback bars before the confirmation window
        # Confirmation: confirm_bars consecutive bars immediately before the latest bar
        n             = len(df_5m)
        confirm_end   = n - 1                          # up to (not including) latest bar
        confirm_start = confirm_end - cfg.confirm_bars
        base_end      = confirm_start
        base_start    = max(0, base_end - cfg.vol_avg_lookback)

        confirm_vols = df_5m["volume"].iloc[confirm_start:confirm_end]
        baseline_vol = df_5m["volume"].iloc[base_start:base_end]

        avg_vol = float(baseline_vol.mean())
        if avg_vol <= 0:
            self._no_signal(symbol, "zero avg volume in baseline")
            return None

        relvols   = confirm_vols / avg_vol
        confirmed = bool((relvols >= threshold).all())

        if not confirmed:
            self._no_signal(
                symbol,
                f"volume not sustained: min_relvol={float(relvols.min()):.2f} "
                f"threshold={threshold:.1f} over {cfg.confirm_bars} bars",
            )
            return None

        # ── Latest bar ────────────────────────────────────────────────────────
        latest   = df_5m.iloc[-1]
        close_px = float(latest["close"])
        open_px  = float(latest["open"])
        bar_high = float(latest["high"])
        bar_low  = float(latest["low"])
        bar_vol  = float(latest["volume"])

        # ── Recent high/low for breakout reference ────────────────────────────
        recent_closes = df_5m["close"].iloc[-cfg.breakout_lookback - 1 : -1]
        recent_high   = float(recent_closes.max())
        recent_low    = float(recent_closes.min())

        # ── Bar body quality (doji filter) ────────────────────────────────────
        bar_range = bar_high - bar_low
        bar_body  = abs(close_px - open_px)
        body_pct  = bar_body / bar_range if bar_range > 0 else 0.0

        if body_pct < cfg.min_body_pct:
            self._no_signal(
                symbol,
                f"bar body {body_pct:.1%} < {cfg.min_body_pct:.1%} — doji filter",
            )
            return None

        # ── Breakout direction ────────────────────────────────────────────────
        latest_relvol = bar_vol / avg_vol

        if close_px > recent_high and close_px > open_px:
            direction  = Direction.BULLISH
            entry_zone = f"breakout above {recent_high:.2f}"
            stop_logic = f"close back below {recent_high:.2f}"
        elif close_px < recent_low and close_px < open_px:
            direction  = Direction.BEARISH
            entry_zone = f"breakdown below {recent_low:.2f}"
            stop_logic = f"close back above {recent_low:.2f}"
        else:
            self._no_signal(
                symbol,
                f"no breakout: close={close_px:.2f} "
                f"recent_high={recent_high:.2f} recent_low={recent_low:.2f}",
            )
            return None

        # ── Confidence ────────────────────────────────────────────────────────
        avg_relvol = float(relvols.mean())
        confidence = round(
            min(1.0, max(0.1, min(avg_relvol, latest_relvol) / (threshold * 2.5))),
            3,
        )
        uq = self._data_quality_score(df_5m, cfg.min_bars)

        codes: List[str] = [
            "relvol_breakout",
            f"relvol_{cfg.confirm_bars}bar_sustained",
        ]
        if regime.regime == Regime.RANGING:
            codes.append("ranging_strict_filter")

        logger.info(
            "RVOL %s %s: close=%.2f hi=%.2f lo=%.2f "
            "avg_rvol=%.2f latest_rvol=%.2f body=%.1f%% regime=%s conf=%.3f",
            symbol, direction.value, close_px, recent_high, recent_low,
            avg_relvol, latest_relvol, body_pct * 100, regime.regime.value, confidence,
        )

        return OptionsSignal(
            strategy_name=SignalType.RVOL,
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
                "price closes back through breakout level",
                "volume drops below threshold on next bar",
                "regime shifts to UNKNOWN",
            ],
            reason_codes=codes,
            liquidity_ok=True,
            spread_ok=True,
            underlying_quality_score=uq,
            indicators={
                "avg_relvol":    round(avg_relvol, 3),
                "latest_relvol": round(latest_relvol, 3),
                "threshold":     threshold,
                "confirm_bars":  cfg.confirm_bars,
                "recent_high":   round(recent_high, 2),
                "recent_low":    round(recent_low, 2),
                "body_pct":      round(body_pct, 3),
                "adx":           regime.adx,
                "atr":           regime.atr,
            },
        )
