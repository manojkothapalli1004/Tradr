"""
options_bot/strategies/volatility_breakout.py

Volatility Breakout / Regime Expansion — paper-trading hypothesis.

Uses 5-minute bars. Requires EXPANDING regime (ATR has expanded above its
rolling average), evidence of a prior Bollinger Band squeeze in the lookback
window, price closing outside the Bollinger Bands on the latest bar, and a
strong bar body (doji filter). No prior squeeze → no signal.

All strategies are hypotheses under validation. No edge is assumed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
import pandas as pd

from options_bot.config import VBKR_CFG, VBKRConfig, REGIME_CFG, OPTIONS_DATA_LIMITATION
from options_bot.models import Direction, OptionsSignal, Regime, RegimeSnapshot, SignalType
from options_bot.regime import compute_atr
from options_bot.strategies.base import BaseOptionsStrategy

logger = logging.getLogger("options_bot.strategy.vbkr")


class VolatilityBreakout(BaseOptionsStrategy):

    strategy_id   = SignalType.VBKR.value
    valid_regimes = (Regime.EXPANDING,)

    def __init__(self, cfg: VBKRConfig = VBKR_CFG) -> None:
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

        close = df_5m["close"]

        # ── Bollinger Bands ───────────────────────────────────────────────────
        bb_mid   = close.rolling(cfg.bb_period).mean()
        bb_sigma = close.rolling(cfg.bb_period).std()
        bb_upper = bb_mid + cfg.bb_std * bb_sigma
        bb_lower = bb_mid - cfg.bb_std * bb_sigma
        bb_width = (bb_upper - bb_lower) / bb_mid.replace(0, np.nan)

        upper_now = float(bb_upper.iloc[-1])
        lower_now = float(bb_lower.iloc[-1])
        mid_now   = float(bb_mid.iloc[-1])
        bw_now    = float(bb_width.iloc[-1])

        if any(np.isnan(v) for v in [upper_now, lower_now, mid_now]):
            self._no_signal(symbol, "Bollinger Band NaN — insufficient warmup")
            return None

        # ── Prior squeeze required ────────────────────────────────────────────
        squeeze_window = bb_width.iloc[-cfg.squeeze_lookback - 1 : -1]
        squeeze_thresh = REGIME_CFG.bb_squeeze_threshold
        prior_squeeze  = bool((squeeze_window < squeeze_thresh).any())

        if not prior_squeeze:
            self._no_signal(
                symbol,
                f"no prior BB squeeze in last {cfg.squeeze_lookback} bars "
                f"(threshold={squeeze_thresh:.4f}) — not a squeeze-to-expand setup",
            )
            return None

        # ── ATR expansion confirmation ────────────────────────────────────────
        atr_s   = compute_atr(df_5m, cfg.atr_period)
        atr_now = float(atr_s.iloc[-1])
        atr_avg = float(atr_s.iloc[-cfg.atr_avg_lookback - 1 : -1].mean())

        if atr_avg <= 0 or atr_now <= atr_avg:
            self._no_signal(symbol, f"ATR not expanding: now={atr_now:.4f} avg={atr_avg:.4f}")
            return None

        # ── Latest bar ────────────────────────────────────────────────────────
        latest   = df_5m.iloc[-1]
        close_px = float(latest["close"])
        open_px  = float(latest["open"])
        bar_high = float(latest["high"])
        bar_low  = float(latest["low"])

        # ── Bar body quality (doji filter) ────────────────────────────────────
        bar_range = bar_high - bar_low
        bar_body  = abs(close_px - open_px)
        body_pct  = bar_body / bar_range if bar_range > 0 else 0.0

        if body_pct < cfg.min_body_pct:
            self._no_signal(symbol, f"bar body {body_pct:.1%} < {cfg.min_body_pct:.1%} — doji filter")
            return None

        # ── Breakout direction ────────────────────────────────────────────────
        if close_px > upper_now and close_px > open_px:
            direction  = Direction.BULLISH
            entry_zone = f"breakout above BB upper {upper_now:.2f}"
            stop_logic = f"close back inside BB upper {upper_now:.2f}"
        elif close_px < lower_now and close_px < open_px:
            direction  = Direction.BEARISH
            entry_zone = f"breakdown below BB lower {lower_now:.2f}"
            stop_logic = f"close back inside BB lower {lower_now:.2f}"
        else:
            self._no_signal(
                symbol,
                f"close {close_px:.2f} inside BB [{lower_now:.2f},{upper_now:.2f}]",
            )
            return None

        # ── Confidence ────────────────────────────────────────────────────────
        band_half = (upper_now - lower_now) / 2.0
        excess    = abs(close_px - (upper_now if direction == Direction.BULLISH else lower_now))
        conf_bb   = min(1.0, 0.5 + (excess / band_half) * 0.3) if band_half > 0 else 0.5
        conf_atr  = min(1.0, (atr_now / atr_avg) / 3.0)
        confidence = round(max(0.1, (conf_bb + conf_atr) / 2.0), 3)
        uq         = self._data_quality_score(df_5m, cfg.min_bars)

        codes: List[str] = ["bb_squeeze_resolved", "bb_band_breakout", "atr_expanding"]

        logger.info(
            "VBKR %s %s: close=%.2f bb=[%.2f,%.2f] bw=%.5f "
            "atr_ratio=%.2f body=%.1f%% conf=%.3f",
            symbol, direction.value, close_px,
            lower_now, upper_now, bw_now,
            atr_now / atr_avg, body_pct * 100, confidence,
        )

        return OptionsSignal(
            strategy_name=SignalType.VBKR,
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
                "price closes back inside the Bollinger Bands",
                "regime shifts to RANGING or UNKNOWN",
                "ATR contracts back below rolling average",
            ],
            reason_codes=codes,
            liquidity_ok=True,
            spread_ok=True,
            underlying_quality_score=uq,
            indicators={
                "bb_upper":       round(upper_now, 2),
                "bb_lower":       round(lower_now, 2),
                "bb_mid":         round(mid_now, 2),
                "bb_width":       round(bw_now, 5),
                "squeeze_thresh": squeeze_thresh,
                "atr_now":        round(atr_now, 4),
                "atr_avg":        round(atr_avg, 4),
                "atr_ratio":      round(atr_now / atr_avg, 3),
                "body_pct":       round(body_pct, 3),
                "adx":            regime.adx,
            },
        )
