"""
signal_trading/data.py — Data access layer.

Wraps shared_tools/data_fetcher so no other module does sys.path surgery.
Returns clean DataFrames with guaranteed column set.
"""

import sys
import os
import logging
from typing import Optional

import pandas as pd

# One-time path injection — all other modules import from here
_TRADER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED_TOOLS = os.path.join(_TRADER_DIR, "shared_tools")

if _SHARED_TOOLS not in sys.path:
    sys.path.insert(0, _SHARED_TOOLS)

from data_fetcher import fetch_ohlcv  # noqa: E402 (after path setup)

from signal_trading.config import ASSETS, CANDLES_NEEDED

logger = logging.getLogger("signal_trading.data")

REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}


def _validate(df: pd.DataFrame, label: str) -> bool:
    if df is None or df.empty:
        logger.warning("Empty dataframe for %s", label)
        return False
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        logger.warning("Missing columns %s for %s", missing, label)
        return False
    if len(df) < 50:
        logger.warning("Too few rows (%d) for %s — need ≥50", len(df), label)
        return False
    return True


def fetch_asset(
    asset: str,
    timeframe: str = "1h",
    limit: int = CANDLES_NEEDED,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV for a named asset (BTC, ETH, GOLD).

    Returns a DataFrame indexed by datetime, or None on failure.
    """
    if asset not in ASSETS:
        logger.error("Unknown asset: %s", asset)
        return None

    cfg = ASSETS[asset]
    label = f"{asset}/{timeframe}"

    try:
        df = fetch_ohlcv(
            symbol=cfg.symbol,
            timeframe=timeframe,
            limit=limit,
            exchange_id=cfg.exchange,
            store=False,    # don't write to shared SQLite
        )
    except Exception as exc:
        logger.error("fetch_ohlcv failed for %s: %s", label, exc)
        return None

    if not _validate(df, label):
        return None

    # Ensure float columns (some exchanges return Decimal)
    for col in REQUIRED_COLUMNS:
        df[col] = df[col].astype(float)

    return df


def fetch_current_price(asset: str) -> float:
    """
    Quick price fetch — returns the most recent close, or 0.0 on failure.
    Uses the exchange directly to avoid the row-count guard in fetch_asset.
    """
    if asset not in ASSETS:
        return 0.0
    cfg = ASSETS[asset]
    try:
        df = fetch_ohlcv(
            symbol=cfg.symbol,
            timeframe="1h",
            limit=5,
            exchange_id=cfg.exchange,
            store=False,
        )
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
    except Exception as exc:
        logger.warning("Price fetch failed for %s: %s", asset, exc)
    return 0.0


def fetch_all_prices(assets: list) -> dict:
    """
    Fetch current prices for all listed assets.
    Returns {asset: price}. Missing assets are omitted.
    """
    prices = {}
    for asset in assets:
        price = fetch_current_price(asset)
        if price > 0:
            prices[asset] = price
    return prices
