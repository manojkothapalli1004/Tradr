"""
options_bot/contract_selector.py — Option contract selection.

DATA LIMITATION (applied to every returned contract):
  yfinance option chain data is ~15-min delayed with no live bid/ask.
  Strike prices, open interest, IV, and last prices are indicative only.
  All estimated_premium values are theoretical. Do not infer real
  fill quality from paper results.

Selection is fully configurable via ContractConfig:
  - DTE range (min_dte, max_dte, preferred_dte)
  - Strike method (ATM, DELTA, OTM_FIXED)
  - Liquidity filters (min_open_interest, max_spread_pct)
  - Size cap (max_premium_per_trade_usd)

Returns OptionContract or None. Prefers no contract over a bad-fit contract.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd

from options_bot.config import (
    CONTRACT_CFG, ContractConfig, StrikeMethod, OPTIONS_DATA_LIMITATION,
)
from options_bot.data import _yf, fetch_option_chain
from options_bot.models import OptionContract, OptionType, OptionsSignal

logger = logging.getLogger("options_bot.contract_selector")


# ── Minimal Black-Scholes (no hard scipy dependency) ────────────────────────────

def _ncdf(x: float) -> float:
    """Normal CDF via math.erf — no external dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, call: bool) -> float:
    """
    Theoretical Black-Scholes price.
    Explicitly labeled as an estimate when used in paper fills.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def _bs_delta(S: float, K: float, T: float, r: float, sigma: float, call: bool) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _ncdf(d1) if call else _ncdf(d1) - 1.0


# ── Strike selection ─────────────────────────────────────────────────────────────

def _pick_strike(
    strikes: np.ndarray,
    S: float,
    call: bool,
    T: float,
    sigma: float,
    cfg: ContractConfig,
    r: float = 0.05,
) -> Optional[float]:
    """
    Select a strike from available chain strikes per the configured method.
    Returns None if selection is impossible.
    """
    if len(strikes) == 0:
        return None

    if cfg.strike_method == StrikeMethod.ATM:
        return float(strikes[int(np.argmin(np.abs(strikes - S)))])

    if cfg.strike_method == StrikeMethod.DELTA:
        target  = cfg.delta_target if call else -cfg.delta_target
        deltas  = np.array([_bs_delta(S, float(k), T, r, sigma, call) for k in strikes])
        return float(strikes[int(np.argmin(np.abs(deltas - target)))])

    if cfg.strike_method == StrikeMethod.OTM_FIXED:
        sorted_k = np.sort(strikes)
        atm_idx  = int(np.argmin(np.abs(sorted_k - S)))
        if call:
            idx = min(atm_idx + cfg.otm_strikes, len(sorted_k) - 1)
        else:
            idx = max(atm_idx - cfg.otm_strikes, 0)
        return float(sorted_k[idx])

    return None  # unknown method


# ── Expiry selection ─────────────────────────────────────────────────────────────

def _pick_expiry(
    ticker: str,
    cfg: ContractConfig,
) -> Optional[tuple]:
    """
    Select the expiry closest to cfg.preferred_dte within [min_dte, max_dte].
    Returns (calls_df, puts_df, expiry_str, dte) or None.
    """
    yf = _yf()
    try:
        t           = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return None

        today      = date.today()
        candidates: list[tuple[int, str]] = []

        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if cfg.min_dte <= dte <= cfg.max_dte:
                candidates.append((dte, exp_str))

        if not candidates:
            return None

        # Pick the expiry whose DTE is closest to preferred_dte
        chosen_dte, chosen_expiry = min(
            candidates, key=lambda x: abs(x[0] - cfg.preferred_dte)
        )

        chain = t.option_chain(chosen_expiry)
        calls = chain.calls.copy() if chain.calls is not None else pd.DataFrame()
        puts  = chain.puts.copy()  if chain.puts  is not None else pd.DataFrame()

        logger.info(
            "EXPIRY %s: selected %s DTE=%d (preferred=%d) from %d candidates | %s",
            ticker, chosen_expiry, chosen_dte, cfg.preferred_dte, len(candidates),
            OPTIONS_DATA_LIMITATION,
        )
        return calls, puts, chosen_expiry, chosen_dte

    except Exception as exc:
        logger.error("_pick_expiry %s: %s", ticker, exc, exc_info=True)
        return None


# ── Liquidity check (indicative — data is delayed) ───────────────────────────────

def _liquidity_flags(row: pd.Series, cfg: ContractConfig) -> tuple[bool, bool]:
    """
    Returns (liquidity_ok, spread_ok).
    INFORMATIONAL ONLY — data is ~15-min delayed, no live bid/ask.
    These flags are logged and carried in data_quality_note for review.
    They do not gate contract selection in v1.
    """
    oi  = float(row.get("openInterest", 0) or 0)
    liq = oi >= cfg.min_open_interest

    bid = row.get("bid")
    ask = row.get("ask")
    spd = True
    if bid is not None and ask is not None and pd.notna(bid) and pd.notna(ask):
        mid = (float(bid) + float(ask)) / 2.0
        if mid > 0:
            spd = (float(ask) - float(bid)) / mid <= cfg.max_spread_pct

    return liq, spd


# ── Main selector ────────────────────────────────────────────────────────────────

def select_contract(
    signal: OptionsSignal,
    cfg: ContractConfig = CONTRACT_CFG,
) -> Optional[OptionContract]:
    """
    Find the best-fit option contract for a given signal.

    Returns None if:
      - No expiry found within configured DTE range.
      - Chain is empty or missing required columns.
      - Strike selection returns no result.
      - Estimated premium cost exceeds max_premium_per_trade_usd.

    The returned OptionContract always carries OPTIONS_DATA_LIMITATION
    in its data_quality_note field.
    """
    call = signal.option_type == OptionType.CALL

    # Fetch all expirations in range, then select the one closest to preferred_dte.
    # fetch_option_chain returns the shortest-DTE expiry in range; we override
    # that selection here by fetching a wider result set via _pick_expiry.
    chain_data = _pick_expiry(signal.symbol, cfg)
    if chain_data is None:
        logger.warning(
            "CONTRACT %s: no chain in DTE [%d,%d] — no trade",
            signal.symbol, cfg.min_dte, cfg.max_dte,
        )
        return None

    calls_df, puts_df, expiry_str, dte = chain_data
    chain = calls_df if call else puts_df

    if chain is None or chain.empty or "strike" not in chain.columns:
        logger.warning("CONTRACT %s: empty/malformed chain for %s", signal.symbol, expiry_str)
        return None

    chain = chain.dropna(subset=["strike"])
    if chain.empty:
        return None

    S     = signal.underlying_price
    T     = max(dte / 365.0, 1.0 / 365.0)
    r     = 0.05   # risk-free rate proxy

    # IV: prefer chain median if available; fall back to 0.20
    sigma      = 0.20
    iv_source  = "fallback_0.20"
    if "impliedVolatility" in chain.columns:
        iv_vals = chain["impliedVolatility"].dropna()
        iv_vals = iv_vals[(iv_vals > 0.01) & (iv_vals < 5.0)]
        if not iv_vals.empty:
            sigma     = float(iv_vals.median())
            iv_source = "chain_median"

    strikes       = chain["strike"].values.astype(float)
    chosen_strike = _pick_strike(strikes, S, call, T, sigma, cfg)

    if chosen_strike is None:
        logger.warning("CONTRACT %s: strike selection returned None (%s)", signal.symbol, cfg.strike_method.value)
        return None

    # Get the chain row closest to chosen_strike
    mask = np.abs(chain["strike"].values.astype(float) - chosen_strike) < 0.01
    if not mask.any():
        return None
    row = chain.loc[mask].iloc[0]

    # Premium: prefer chain lastPrice if plausible, else use BS estimate
    chain_last = 0.0
    for col in ("lastPrice", "last"):
        if col in row and pd.notna(row[col]):
            chain_last = float(row[col])
            break

    bs_est = _bs_price(S, chosen_strike, T, r, sigma, call)

    # Accept chain_last only if non-zero and within 3× BS (sanity bound)
    if chain_last > 0 and bs_est > 0 and chain_last < bs_est * 3.0:
        premium    = chain_last
        prem_src   = "chain_last"
    else:
        premium    = bs_est
        prem_src   = "bs_estimate"

    if premium <= 0:
        logger.warning(
            "CONTRACT %s: zero premium K=%.2f expiry=%s — no trade",
            signal.symbol, chosen_strike, expiry_str,
        )
        return None

    cost = premium * 100   # 1 contract = 100 shares
    if cost > cfg.max_premium_per_trade_usd:
        logger.warning(
            "CONTRACT %s: cost $%.2f > cap $%.2f (K=%.2f DTE=%d) — no trade",
            signal.symbol, cost, cfg.max_premium_per_trade_usd, chosen_strike, dte,
        )
        return None

    delta        = _bs_delta(S, chosen_strike, T, r, sigma, call)
    liq_ok, spd_ok = _liquidity_flags(row, cfg)

    if not liq_ok:
        logger.info(
            "CONTRACT %s: low OI on K=%.2f (indicative — data delayed)",
            signal.symbol, chosen_strike,
        )
    if not spd_ok:
        logger.info(
            "CONTRACT %s: wide spread on K=%.2f (indicative — data delayed)",
            signal.symbol, chosen_strike,
        )

    data_note = (
        f"{OPTIONS_DATA_LIMITATION} | "
        f"strike_method={cfg.strike_method.value} "
        f"iv_source={iv_source}({sigma:.3f}) "
        f"prem_source={prem_src} bs={bs_est:.4f} chain_last={chain_last:.4f} | "
        f"[INFORMATIONAL] liquidity_ok={liq_ok} spread_ok={spd_ok} "
        f"(delayed data — not used as a gate)"
    )

    logger.info(
        "CONTRACT %s: %s K=%.2f exp=%s DTE=%d "
        "prem=%.4f/sh cost=$%.2f delta=%.3f iv=%.3f [%s|%s]",
        signal.symbol, signal.option_type.value,
        chosen_strike, expiry_str, dte,
        premium, cost, delta, sigma, iv_source, prem_src,
    )

    return OptionContract(
        symbol=signal.symbol,
        option_type=signal.option_type,
        expiry=expiry_str,
        strike=chosen_strike,
        dte=dte,
        estimated_premium=premium,
        estimated_delta=delta,
        estimated_iv=sigma,
        data_quality_note=data_note,
    )
