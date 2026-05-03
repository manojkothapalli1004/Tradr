"""
options_bot/strategies/opening_range_breakout.py

Opening Range Breakout — paper-trading hypothesis.

Opening range is computed from 1-minute bars (first orb_minutes of session).
If 1m data is unavailable or insufficient, falls back to 5m bars with an
explicit data quality warning and confidence penalty.

All strategies are hypotheses under validation. No edge is assumed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from options_bot.config import DATA_CFG, ORB_CFG, ORBConfig, OPTIONS_DATA_LIMITATION
from options_bot.models import Direction, OptionsSignal, Regime, RegimeSnapshot, SignalType
from options_bot.strategies.base import BaseOptionsStrategy

logger = logging.getLogger("options_bot.strategy.orb")


class OpeningRangeBreakout(BaseOptionsStrategy):

    strategy_id   = SignalType.ORB.value
    valid_regimes = (Regime.TRENDING, Regime.EXPANDING)

    def __init__(self, cfg: ORBConfig = ORB_CFG) -> None:
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
        if len(df_5m) < cfg.min_bars_5m:
            self._insufficient_data(symbol, len(df_5m), cfg.min_bars_5m, "5m volume baseline")
            return None

        # ── Session timing ────────────────────────────────────────────────────
        now_et        = datetime.now(ZoneInfo("America/New_York"))
        et_mins_now   = now_et.hour * 60 + now_et.minute
        market_open_m = DATA_CFG.market_open_hour * 60 + DATA_CFG.market_open_minute
        since_open    = et_mins_now - market_open_m

        if since_open < cfg.orb_minutes:
            self._no_signal(symbol, f"opening range not complete ({since_open}m < {cfg.orb_minutes}m)")
            return None
        if since_open > cfg.window_hours * 60:
            self._no_signal(symbol, f"past ORB window ({since_open:.0f}m)")
            return None

        # ── Opening range ─────────────────────────────────────────────────────
        r_high, r_low, src, degraded = self._opening_range(symbol, df_5m, df_1m, cfg)
        if r_high is None or r_low is None:
            self._no_signal(symbol, "could not compute opening range")
            return None

        mid = (r_high + r_low) / 2.0
        if mid <= 0:
            return None
        width_pct = (r_high - r_low) / mid
        if width_pct > cfg.max_range_width_pct:
            self._no_signal(
                symbol,
                f"range too wide {width_pct:.3%} > {cfg.max_range_width_pct:.3%} — ambiguous open",
            )
            return None

        # ── Volume confirmation ───────────────────────────────────────────────
        latest   = df_5m.iloc[-1]
        close_px = float(latest["close"])
        open_px  = float(latest["open"])
        bar_vol  = float(latest["volume"])
        avg_vol  = float(df_5m["volume"].iloc[-cfg.vol_avg_lookback - 1 : -1].mean())

        if avg_vol <= 0:
            self._no_signal(symbol, "zero avg volume in baseline")
            return None
        vol_ratio = bar_vol / avg_vol

        if vol_ratio < cfg.min_vol_ratio:
            self._no_signal(symbol, f"vol_ratio {vol_ratio:.2f} < {cfg.min_vol_ratio}")
            return None

        # ── Breakout direction ────────────────────────────────────────────────
        if close_px > r_high and close_px > open_px:
            direction  = Direction.BULLISH
            entry_zone = f"breakout above range high {r_high:.2f}"
            stop_logic = f"close back below range high {r_high:.2f}"
        elif close_px < r_low and close_px < open_px:
            direction  = Direction.BEARISH
            entry_zone = f"breakdown below range low {r_low:.2f}"
            stop_logic = f"close back above range low {r_low:.2f}"
        else:
            self._no_signal(
                symbol,
                f"no clean breakout: close={close_px:.2f} range=[{r_low:.2f},{r_high:.2f}]",
            )
            return None

        # ── Confidence & quality ──────────────────────────────────────────────
        base_conf  = min(1.0, vol_ratio / (cfg.min_vol_ratio * 3.0))
        confidence = round(max(0.05, base_conf - (0.15 if degraded else 0.0)), 3)
        uq         = self._data_quality_score(df_5m, cfg.min_bars_5m, df_1m)

        # ── Reason codes ──────────────────────────────────────────────────────
        codes: List[str] = ["orb_breakout"]
        codes.append("vol_confirmed" if vol_ratio >= 1.5 else "vol_marginal")
        if degraded:
            codes.append("orb_1m_fallback_5m")

        dq_note = (
            f"[ORB FALLBACK] 1m bars unavailable; range estimated from 5m. "
            f"Range precision reduced. {OPTIONS_DATA_LIMITATION}"
            if degraded else OPTIONS_DATA_LIMITATION
        )

        logger.info(
            "ORB %s %s: close=%.2f range=[%.2f,%.2f] width=%.3f%% "
            "vol_ratio=%.2f conf=%.3f src=%s",
            symbol, direction.value, close_px,
            r_low, r_high, width_pct * 100, vol_ratio, confidence, src,
        )

        return OptionsSignal(
            strategy_name=SignalType.ORB,
            symbol=symbol,
            direction=direction,
            timestamp=datetime.now(timezone.utc),
            regime_required=[r.value for r in self.valid_regimes],
            regime_at_signal=regime.regime,
            underlying_price=close_px,
            confidence_score=confidence,
            data_quality_ok=not degraded,
            data_quality_note=dq_note,
            entry_zone=entry_zone,
            stop_logic=stop_logic,
            target_logic="50% gain on entry premium",
            invalidation_conditions=[
                f"price closes back through breakout level",
                "regime shifts to UNKNOWN",
                "volume drops below threshold on next bar",
            ],
            reason_codes=codes,
            liquidity_ok=True,
            spread_ok=True,
            underlying_quality_score=uq,
            indicators={
                "range_high":        round(r_high, 2),
                "range_low":         round(r_low, 2),
                "range_width_pct":   round(width_pct * 100, 3),
                "vol_ratio":         round(vol_ratio, 3),
                "avg_vol":           round(avg_vol, 0),
                "since_open_min":    round(since_open, 1),
                "adx":               regime.adx,
                "atr":               regime.atr,
            },
        )

    # ── Opening range helper ───────────────────────────────────────────────────

    def _opening_range(
        self,
        symbol: str,
        df_5m: pd.DataFrame,
        df_1m: Optional[pd.DataFrame],
        cfg: ORBConfig,
    ) -> Tuple[Optional[float], Optional[float], str, bool]:
        """
        Returns (high, low, source_label, quality_degraded).
        Tries 1m bars first; falls back to 5m with a warning.
        """
        today = df_5m.index[-1].strftime("%Y-%m-%d")

        if df_1m is not None and not df_1m.empty:
            today_1m   = df_1m[df_1m.index.strftime("%Y-%m-%d") == today]
            opening_1m = today_1m.head(cfg.orb_minutes)
            if len(opening_1m) >= cfg.orb_minutes // 2:
                return (
                    float(opening_1m["high"].max()),
                    float(opening_1m["low"].min()),
                    "1m", False,
                )
            logger.warning(
                "ORB %s: 1m today bars=%d < %d required. Falling back to 5m.",
                symbol, len(today_1m), cfg.orb_minutes,
            )

        # 5m fallback
        logger.warning(
            "ORB %s: 1m bars unavailable — estimating range from 5m. %s",
            symbol, OPTIONS_DATA_LIMITATION,
        )
        today_5m   = df_5m[df_5m.index.strftime("%Y-%m-%d") == today]
        n_5m       = max(1, cfg.orb_minutes // 5)
        opening_5m = today_5m.head(n_5m)

        if opening_5m.empty:
            logger.warning("ORB %s: no today bars in 5m data", symbol)
            return None, None, "none", True

        return (
            float(opening_5m["high"].max()),
            float(opening_5m["low"].min()),
            "5m-fallback", True,
        )
