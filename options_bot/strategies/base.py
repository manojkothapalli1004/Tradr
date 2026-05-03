"""
options_bot/strategies/base.py — Abstract base for all five option strategies.

Contract rules encoded here:
  - evaluate() is the only public method.
  - Stateless: all inputs as arguments, output as typed return value.
  - Return None for no-trade. Never raise out of evaluate().
  - UNKNOWN regime always blocks before _evaluate_impl is called.
  - Every signal must populate all OptionsSignal fields defined in models.py.
  - Strategies do not import router, risk, execution, or journal.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional, Tuple

import pandas as pd

from options_bot.config import OPTIONS_DATA_LIMITATION
from options_bot.models import OptionsSignal, Regime, RegimeSnapshot


class BaseOptionsStrategy(ABC):
    """
    All five strategies inherit from this class.

    Subclasses must:
      - Set `strategy_id` (matches SignalType.value).
      - Set `valid_regimes` tuple.
      - Implement `_evaluate_impl()`.
    """

    strategy_id:   str            = ""
    valid_regimes: Tuple[Regime, ...] = ()

    # ── Public entry point ────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol: str,
        df_5m: Optional[pd.DataFrame],
        regime: RegimeSnapshot,
        df_1m: Optional[pd.DataFrame] = None,
    ) -> Optional[OptionsSignal]:
        """
        Evaluate one symbol for one cycle.

        Args:
            symbol:  "SPY" or "QQQ".
            df_5m:   5-minute OHLCV. May be None if fetch failed.
            regime:  RegimeSnapshot from regime.py.
            df_1m:   1-minute bars. Required by ORB; all others receive None.

        Returns:
            OptionsSignal if all conditions are met, else None.
        """
        log = self._log()

        # UNKNOWN always blocks — prefer no-trade over bad-data trade
        if regime.is_unknown:
            log.debug("%s: regime UNKNOWN — no signal", symbol)
            return None

        # Regime gate
        if self.valid_regimes and regime.regime not in self.valid_regimes:
            log.debug(
                "%s: regime %s not in %s — no signal",
                symbol, regime.regime.value,
                [r.value for r in self.valid_regimes],
            )
            return None

        # Data guard
        if df_5m is None or df_5m.empty:
            log.warning(
                "%s: df_5m is None or empty — data unavailable or delayed, no signal",
                symbol,
            )
            return None

        try:
            return self._evaluate_impl(symbol, df_5m, regime, df_1m)
        except Exception as exc:
            log.error(
                "%s: unhandled exception in _evaluate_impl: %s — returning no-signal",
                symbol, exc, exc_info=True,
            )
            return None

    # ── Abstract implementation ───────────────────────────────────────────────

    @abstractmethod
    def _evaluate_impl(
        self,
        symbol: str,
        df_5m: pd.DataFrame,
        regime: RegimeSnapshot,
        df_1m: Optional[pd.DataFrame],
    ) -> Optional[OptionsSignal]:
        """
        Concrete strategy logic. Called only after regime and data guards pass.
        Must return OptionsSignal or None. Must not raise.
        """
        ...

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _log(self) -> logging.Logger:
        return logging.getLogger(f"options_bot.strategy.{self.strategy_id}")

    def _insufficient_data(self, symbol: str, have: int, need: int, note: str = "") -> None:
        msg = f"{symbol}: insufficient data — have {have} bars, need {need}. No signal."
        if note:
            msg += f" ({note})"
        self._log().warning(msg)

    def _no_signal(self, symbol: str, reason: str) -> None:
        self._log().debug("%s: no signal — %s", symbol, reason)

    def _data_quality_score(
        self,
        df_5m: pd.DataFrame,
        min_bars: int,
        df_1m: Optional[pd.DataFrame] = None,
    ) -> float:
        """
        0.0–1.0 composite score based on bar count, NaN density, zero-volume bars.
        Score < 0.5 means the strategy ran on marginal data.
        """
        def _score(df: pd.DataFrame, mb: int) -> float:
            if df is None or df.empty:
                return 0.0
            bar_s = min(1.0, len(df) / max(mb, 1))
            nan_s = max(0.0, 1.0 - df.isnull().mean().mean() * 5)
            zv_s  = max(0.0, 1.0 - (df.get("volume", pd.Series(dtype=float)) == 0).mean() * 3)
            return round(bar_s * 0.5 + nan_s * 0.3 + zv_s * 0.2, 3)

        q5 = _score(df_5m, min_bars)
        if df_1m is not None:
            return round(q5 * 0.7 + _score(df_1m, 30) * 0.3, 3)
        return q5

    def _dq_note(self, extra: str = "") -> str:
        """Data quality note for signals with degraded input data."""
        base = OPTIONS_DATA_LIMITATION
        return f"{extra} | {base}" if extra else base
