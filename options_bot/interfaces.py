"""
options_bot/interfaces.py

Abstract interfaces (Protocols) for the options bot.

Defines the contracts that strategy modules, the contract selector,
and the fill simulator must conform to. Concrete implementations are
in separate modules. Nothing here imports from those modules.

These exist so that:
  - Strategies can be tested in isolation without a data layer.
  - The fill simulator can be swapped without touching strategy code.
  - Contract selection logic can be upgraded without changing strategy output.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

import pandas as pd

from options_bot.models import (
    OptionsSignal,
    OptionContract,
    OptionTrade,
    RegimeSnapshot,
)


# ── Strategy interface ─────────────────────────────────────────────────────────

@runtime_checkable
class StrategyProtocol(Protocol):
    """
    Every strategy module must expose one callable instance with this interface.

    evaluate() is the only public method. It is stateless: all inputs are passed
    as arguments, all outputs are returned as a typed object or None.

    Returning None is always valid and means "no trade this cycle".
    Strategies must never raise — they must log and return None on any failure.
    """

    strategy_id: str   # must match SignalType.value

    def evaluate(
        self,
        symbol: str,
        df_5m: pd.DataFrame,
        regime: RegimeSnapshot,
        df_1m: Optional[pd.DataFrame] = None,
    ) -> Optional[OptionsSignal]:
        """
        Evaluate current market conditions for one symbol.

        Args:
            symbol:  Underlying ticker ("SPY" or "QQQ").
            df_5m:   5-minute OHLCV bars (min bars depend on strategy config).
            regime:  Current regime snapshot (computed by regime.py).
            df_1m:   1-minute bars. Required by ORB; all other strategies ignore it.
                     Callers must pass None if unavailable — strategies handle that.

        Returns:
            OptionsSignal if all entry conditions are met.
            None if any condition fails, data is insufficient, or regime is wrong.
        """
        ...


# ── Contract selector interface ────────────────────────────────────────────────

@runtime_checkable
class ContractSelectorProtocol(Protocol):
    """
    Contract selection logic is intentionally abstract in Prompt 2.
    A concrete implementation will be provided in Prompt 3.

    The selector receives a signal and returns an OptionContract (or None if no
    suitable contract exists given the configured DTE range, strike method, and
    liquidity filters).
    """

    def select(
        self,
        signal: OptionsSignal,
    ) -> Optional[OptionContract]:
        """
        Select the best-fit option contract for the given signal.

        Returns None (no trade) if:
          - No expiry found within configured DTE range.
          - No strike satisfies the configured selection method.
          - Liquidity filters cannot be evaluated (data insufficient).
          - Estimated premium would exceed max_premium_per_trade_usd.
        """
        ...


# ── Fill simulator interface ───────────────────────────────────────────────────

@runtime_checkable
class FillSimulatorProtocol(Protocol):
    """
    Fill simulation is intentionally abstract in Prompt 2.
    A concrete implementation will be provided in Prompt 3.

    All fills are paper-only. The simulator must attach OPTIONS_DATA_LIMITATION
    to every OptionTrade it produces. It must never produce a fill that implies
    real execution quality.
    """

    def open_position(
        self,
        signal: OptionsSignal,
        contract: OptionContract,
    ) -> OptionTrade:
        """Simulate opening a paper option position. Returns a new OptionTrade."""
        ...

    def close_position(
        self,
        trade: OptionTrade,
        current_mark: float,
        reason: str,
    ) -> OptionTrade:
        """Simulate closing a paper position. Returns the mutated trade with exit fields set."""
        ...
