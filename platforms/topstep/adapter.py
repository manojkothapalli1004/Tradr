"""
TopStep Prop Trading Exchange Adapter.

Supports paper (simulated fills, no credentials) and live (real orders via
TopStepX API, credentials required) modes for CME futures.

Environment variables:
    TOPSTEP_API_KEY        — API key for TopStepX REST API
    TOPSTEP_API_SECRET     — API secret
    TOPSTEP_ACCOUNT_ID     — trading account ID
"""

import os
import time
from datetime import datetime, timezone, timedelta

# TopStepX API base URL
API_BASE_URL = "https://api.topstepx.com"

# CME contract specifications
CONTRACT_SPECS = {
    "ES": {"tick_size": 0.25, "tick_value": 12.50, "multiplier": 50, "type": "index"},
    "NQ": {"tick_size": 0.25, "tick_value": 5.00, "multiplier": 20, "type": "index"},
    "MES": {"tick_size": 0.25, "tick_value": 1.25, "multiplier": 5, "type": "index"},
    "MNQ": {"tick_size": 0.25, "tick_value": 0.50, "multiplier": 2, "type": "index"},
    "CL": {"tick_size": 0.01, "tick_value": 10.00, "multiplier": 1000, "type": "energy"},
    "GC": {"tick_size": 0.10, "tick_value": 10.00, "multiplier": 100, "type": "metals"},
}


class TopStepExchangeAdapter:
    """
    Exchange adapter for TopStep prop trading (CME futures).

    Paper mode:  no credentials needed; uses simulated prices.
    Live mode:   requires TOPSTEP_API_KEY, TOPSTEP_API_SECRET, TOPSTEP_ACCOUNT_ID.
    """

    def __init__(self, mode="paper"):
        self._mode = mode
        self._api_key = os.environ.get("TOPSTEP_API_KEY", "")
        self._api_secret = os.environ.get("TOPSTEP_API_SECRET", "")
        self._account_id = os.environ.get("TOPSTEP_ACCOUNT_ID", "")
        self._session = None

        if mode == "live":
            if not self._api_key or not self._api_secret or not self._account_id:
                raise RuntimeError(
                    "Live mode requires TOPSTEP_API_KEY, TOPSTEP_API_SECRET, "
                    "and TOPSTEP_ACCOUNT_ID environment variables"
                )
            try:
                import requests
                self._session = requests.Session()
                self._session.headers.update({
                    "X-API-Key": self._api_key,
                    "X-API-Secret": self._api_secret,
                    "Content-Type": "application/json",
                })
            except ImportError:
                raise ImportError("requests package required for live mode. Run: uv sync")

    @property
    def is_live(self) -> bool:
        return self._mode == "live" and self._session is not None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def name(self) -> str:
        return "topstep"

    # ─────────────────────────────────────────────
    # Contract specs
    # ─────────────────────────────────────────────

    def get_contract_spec(self, symbol: str) -> dict:
        """Get contract specification for a symbol."""
        spec = CONTRACT_SPECS.get(symbol)
        if spec is None:
            raise ValueError(f"Unknown symbol: {symbol}. Supported: {list(CONTRACT_SPECS.keys())}")
        return dict(spec)

    # ─────────────────────────────────────────────
    # Market data
    # ─────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        """Get current price for a futures symbol."""
        if not self.is_live:
            return 0.0
        try:
            resp = self._session.get(
                f"{API_BASE_URL}/v1/market/quote",
                params={"symbol": symbol, "accountId": self._account_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("lastPrice", 0))
        except Exception:
            return 0.0

    def get_ohlcv(self, symbol: str, interval: str = "1h", limit: int = 200) -> list:
        """
        Fetch OHLCV candles from TopStepX API.

        Returns list of [timestamp_ms, open, high, low, close, volume].
        """
        if not self.is_live:
            return []
        try:
            resp = self._session.get(
                f"{API_BASE_URL}/v1/market/candles",
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "limit": limit,
                    "accountId": self._account_id,
                },
                timeout=15,
            )
            resp.raise_for_status()
            candles = resp.json().get("candles", [])
            result = []
            for c in candles:
                result.append([
                    int(c.get("timestamp", 0)),
                    float(c.get("open", 0)),
                    float(c.get("high", 0)),
                    float(c.get("low", 0)),
                    float(c.get("close", 0)),
                    float(c.get("volume", 0)),
                ])
            return result
        except Exception:
            return []

    # ─────────────────────────────────────────────
    # Account data
    # ─────────────────────────────────────────────

    def get_open_positions(self) -> list:
        """Get open positions for the configured account."""
        if not self.is_live:
            return []
        try:
            resp = self._session.get(
                f"{API_BASE_URL}/v1/account/positions",
                params={"accountId": self._account_id},
                timeout=10,
            )
            resp.raise_for_status()
            positions = []
            for pos in resp.json().get("positions", []):
                qty = int(pos.get("quantity", 0))
                if qty == 0:
                    continue
                positions.append({
                    "symbol": pos.get("symbol", ""),
                    "quantity": qty,
                    "avg_price": float(pos.get("avgPrice", 0)),
                    "side": "long" if qty > 0 else "short",
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                })
            return positions
        except Exception:
            return []

    # ─────────────────────────────────────────────
    # Order execution (live mode only)
    # ─────────────────────────────────────────────

    def market_open(self, symbol: str, is_buy: bool, contracts: int) -> dict:
        """Place a market order. Live mode only, integer contracts."""
        if not self.is_live:
            raise RuntimeError("market_open requires live mode")
        contracts = int(contracts)
        if contracts <= 0:
            raise ValueError("contracts must be > 0")
        resp = self._session.post(
            f"{API_BASE_URL}/v1/order/market",
            json={
                "accountId": self._account_id,
                "symbol": symbol,
                "side": "buy" if is_buy else "sell",
                "quantity": contracts,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def market_close(self, symbol: str) -> dict:
        """Close all positions for a symbol. Live mode only."""
        if not self.is_live:
            raise RuntimeError("market_close requires live mode")
        resp = self._session.post(
            f"{API_BASE_URL}/v1/order/close",
            json={
                "accountId": self._account_id,
                "symbol": symbol,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    # ─────────────────────────────────────────────
    # Market hours
    # ─────────────────────────────────────────────

    def is_market_open(self) -> bool:
        """
        Check if CME futures market is open.

        CME Globex hours: Sunday 6:00 PM ET – Friday 5:00 PM ET
        Daily maintenance break: 5:00 PM – 6:00 PM ET
        """
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
        weekday = now.weekday()  # Monday=0, Sunday=6
        hour = now.hour
        minute = now.minute
        current_minutes = hour * 60 + minute

        # Daily maintenance break: 5:00 PM – 6:00 PM ET (17:00 – 18:00)
        maintenance_start = 17 * 60  # 5:00 PM
        maintenance_end = 18 * 60    # 6:00 PM
        if maintenance_start <= current_minutes < maintenance_end:
            return False

        # Saturday: closed all day
        if weekday == 5:
            return False

        # Sunday: opens at 6:00 PM ET
        if weekday == 6:
            return current_minutes >= maintenance_end

        # Friday: closes at 5:00 PM ET
        if weekday == 4:
            return current_minutes < maintenance_start

        # Monday–Thursday: open except during maintenance
        return True
