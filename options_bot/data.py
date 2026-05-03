"""
options_bot/data.py

Market data fetching for equity ETF options (SPY, QQQ — v1).

DATA LIMITATION:
  All data sourced from yfinance free tier.
  - Underlying OHLCV: ~15 minutes delayed.
  - Options chain: ~15 minutes delayed, no live bid/ask.
  - No real-time Greeks available.

This module is suitable for paper trading hypothesis validation ONLY.
Do not use for real execution decisions.

Does NOT import from shared_tools/data_fetcher.py (which uses ccxt for crypto).
These are separate data paths.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from options_bot.config import DATA_CFG, DataConfig, OPTIONS_DATA_LIMITATION

logger = logging.getLogger("options_bot.data")


# ── yfinance import guard ──────────────────────────────────────────────────────

def _yf():
    """Lazy import so a missing package gives a clear error at call-time."""
    try:
        import yfinance as yf
        return yf
    except ImportError:
        raise ImportError(
            "yfinance is required for options_bot. "
            "Install with: uv add yfinance"
        )


# ── OHLCV ──────────────────────────────────────────────────────────────────────

def fetch_ohlcv(
    ticker: str,
    interval: str,
    period: str,
    cfg: DataConfig = DATA_CFG,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV bars from yfinance.

    Returns a DataFrame with lowercase columns [open, high, low, close, volume]
    indexed by UTC datetime, or None on any failure.

    Data is approximately 15 minutes delayed.
    """
    yf = _yf()
    try:
        raw = yf.download(
            tickers=ticker,
            interval=interval,
            period=period,
            auto_adjust=True,
            progress=False,
            prepost=False,
        )
        if raw is None or raw.empty:
            logger.warning(
                "fetch_ohlcv: no data for %s interval=%s period=%s | %s",
                ticker, interval, period, OPTIONS_DATA_LIMITATION,
            )
            return None

        # yfinance ≥ 0.2 may return a MultiIndex; flatten it
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.dropna()

        if df.empty:
            logger.warning(
                "fetch_ohlcv: all rows dropped after dropna for %s %s", ticker, interval
            )
            return None

        logger.debug(
            "fetch_ohlcv: %s interval=%s rows=%d latest=%s",
            ticker, interval, len(df), df.index[-1].isoformat(),
        )
        return df

    except Exception as exc:
        logger.error(
            "fetch_ohlcv: error for %s interval=%s: %s", ticker, interval, exc, exc_info=True
        )
        return None


def fetch_5m_bars(ticker: str, cfg: DataConfig = DATA_CFG) -> Optional[pd.DataFrame]:
    """5-minute bars — primary timeframe for all strategies."""
    df = fetch_ohlcv(ticker, cfg.bar_timeframe, cfg.download_period_5m)
    if df is not None and len(df) < cfg.bars_needed:
        logger.warning(
            "fetch_5m_bars: %s has only %d bars (need %d) — data insufficient",
            ticker, len(df), cfg.bars_needed,
        )
        # Return what we have; strategies will check bar counts themselves
    return df


def fetch_1m_bars(ticker: str, cfg: DataConfig = DATA_CFG) -> Optional[pd.DataFrame]:
    """1-minute bars — used only by the ORB strategy for opening range calculation."""
    df = fetch_ohlcv(ticker, cfg.orb_timeframe, cfg.download_period_1m)
    if df is not None and len(df) < cfg.orb_bars_needed:
        logger.warning(
            "fetch_1m_bars: %s has only %d bars (need %d) — ORB may fall back to 5m",
            ticker, len(df), cfg.orb_bars_needed,
        )
    return df


def fetch_current_price(ticker: str) -> Optional[float]:
    """
    Fetch the latest available price for a ticker.
    Used by the stop-check cycle to evaluate open positions.
    Data is still delayed — see OPTIONS_DATA_LIMITATION.
    """
    yf = _yf()
    try:
        info = yf.Ticker(ticker).fast_info
        price = getattr(info, "last_price", None)
        if price and float(price) > 0:
            return float(price)
        # Fallback to last 1m bar close
        df = fetch_ohlcv(ticker, "1m", "1d")
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
        return None
    except Exception as exc:
        logger.error("fetch_current_price: %s — %s", ticker, exc)
        return None


# ── Option mark (stop-cycle re-pricing) ───────────────────────────────────────

def fetch_option_mark(
    ticker: str,
    expiry: str,
    strike: float,
    option_type: str,   # "call" or "put"
) -> Optional[float]:
    """
    Fetch the latest available per-share mid price for a specific open option
    position. Used by the stop-check cycle to update current_premium.

    DATA LIMITATION: yfinance chain data is ~15-min delayed with no live
    bid/ask. The returned value is an indicative mid estimate only.
    Falls back to None if the contract cannot be found in the chain.

    Returns per-share price (not total contract cost).
    """
    yf = _yf()
    try:
        t     = yf.Ticker(ticker)
        chain = t.option_chain(expiry)
        df    = chain.calls if option_type == "call" else chain.puts
        if df is None or df.empty or "strike" not in df.columns:
            return None

        mask = abs(df["strike"].astype(float) - strike) < 0.01
        if not mask.any():
            return None

        row = df.loc[mask].iloc[0]

        # Prefer (bid + ask) / 2 if both available
        bid = row.get("bid")
        ask = row.get("ask")
        if bid is not None and ask is not None and pd.notna(bid) and pd.notna(ask):
            mid = (float(bid) + float(ask)) / 2.0
            if mid > 0:
                return mid

        # Fallback: lastPrice
        for col in ("lastPrice", "last"):
            if col in row and pd.notna(row[col]) and float(row[col]) > 0:
                return float(row[col])

        return None

    except Exception as exc:
        logger.debug("fetch_option_mark: %s %s K=%.2f %s — %s", ticker, expiry, strike, option_type, exc)
        return None


# ── Options chain ──────────────────────────────────────────────────────────────

def fetch_option_chain(
    ticker: str,
    min_dte: int,
    max_dte: int,
) -> Optional[Tuple[pd.DataFrame, pd.DataFrame, str, int]]:
    """
    Fetch option chain data for the nearest expiry within [min_dte, max_dte].

    Returns (calls_df, puts_df, expiry_str, dte) or None if no suitable expiry.

    DATA LIMITATION: chain data is ~15-min delayed, no live bid/ask.
    This function always logs the data limitation at WARNING level.
    """
    logger.info(
        "fetch_option_chain: %s — %s", ticker, OPTIONS_DATA_LIMITATION
    )
    yf = _yf()
    try:
        t = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            logger.warning("fetch_option_chain: no expirations available for %s", ticker)
            return None

        today = date.today()
        chosen_expiry: Optional[str] = None
        chosen_dte: Optional[int] = None

        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if min_dte <= dte <= max_dte:
                if chosen_dte is None or dte < chosen_dte:
                    chosen_expiry = exp_str
                    chosen_dte = dte

        if chosen_expiry is None or chosen_dte is None:
            logger.warning(
                "fetch_option_chain: no expiry in DTE [%d, %d] for %s. Available: %s",
                min_dte, max_dte, ticker, list(expirations[:5]),
            )
            return None

        chain = t.option_chain(chosen_expiry)
        calls = chain.calls.copy() if chain.calls is not None else pd.DataFrame()
        puts  = chain.puts.copy()  if chain.puts  is not None else pd.DataFrame()

        logger.info(
            "fetch_option_chain: %s expiry=%s DTE=%d calls=%d puts=%d",
            ticker, chosen_expiry, chosen_dte, len(calls), len(puts),
        )
        return calls, puts, chosen_expiry, chosen_dte

    except Exception as exc:
        logger.error("fetch_option_chain: error for %s: %s", ticker, exc, exc_info=True)
        return None


# ── Market session check ───────────────────────────────────────────────────────

_ET = ZoneInfo("America/New_York")


def is_market_open(cfg: DataConfig = DATA_CFG) -> bool:
    """
    Rough US market session check (Eastern time, no holiday awareness).
    Returns False on weekends or outside 9:30–16:00 ET.
    """
    now_et = datetime.now(_ET)
    et_minutes_now = now_et.hour * 60 + now_et.minute

    open_minutes  = cfg.market_open_hour  * 60 + cfg.market_open_minute   # 570
    close_minutes = cfg.market_close_hour * 60 + cfg.market_close_minute  # 960

    is_weekday = now_et.weekday() < 5
    return is_weekday and open_minutes <= et_minutes_now < close_minutes


def data_quality_score(df: pd.DataFrame, min_bars: int) -> float:
    """
    Compute a simple 0.0–1.0 data quality score for a DataFrame.

    Considers:
      - Bar count adequacy (< min_bars → low score)
      - NaN density (many NaNs → low score)
      - Zero-volume bars (stale data proxy)

    Used by strategies to populate OptionsSignal.underlying_quality_score.
    Score < 0.5 should be treated as marginal data.
    """
    if df is None or df.empty:
        return 0.0

    bar_score = min(1.0, len(df) / max(min_bars, 1))

    nan_density = df.isnull().mean().mean()
    nan_score = max(0.0, 1.0 - nan_density * 5)  # penalise heavily

    zero_vol = (df.get("volume", pd.Series(dtype=float)) == 0).mean()
    vol_score = max(0.0, 1.0 - zero_vol * 3)

    return round((bar_score * 0.5 + nan_score * 0.3 + vol_score * 0.2), 3)
