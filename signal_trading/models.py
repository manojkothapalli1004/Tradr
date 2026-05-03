"""
signal_trading/models.py — Shared data models.

All modules use these dataclasses. No dicts with string keys flying around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List
import uuid


# ── Enums ────────────────────────────────────────────────────────────────────

class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


class RiskStage(str, Enum):
    NORMAL = "normal"
    REDUCED_RISK = "reduced_risk"
    NO_NEW_RISK = "no_new_risk"
    HARD_LOCKED = "hard_locked"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_LIMIT = "time_limit"
    OPPOSITE_SIGNAL = "opposite_signal"
    REGIME_CHANGE = "regime_change"
    CIRCUIT_BREAKER = "circuit_breaker"
    KILL_SWITCH = "kill_switch"
    MANUAL = "manual"


class SignalStrength(str, Enum):
    STRONG = "strong"    # multiple confirming strategies
    WEAK = "weak"        # single strategy, regime not aligned


# ── Signal ───────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """Output of the signal engine for one strategy×asset pair."""
    asset: str                          # "BTC", "ETH", "GOLD"
    strategy: str                       # strategy id
    direction: Optional[Direction]      # None = hold
    price: float                        # price at signal time
    timestamp: datetime
    regime: Regime = Regime.UNKNOWN
    strength: SignalStrength = SignalStrength.WEAK
    indicators: dict = field(default_factory=dict)  # raw indicator values for journal

    @property
    def is_actionable(self) -> bool:
        return self.direction is not None and self.price > 0

    def __str__(self) -> str:
        return (
            f"Signal({self.asset} {self.strategy} "
            f"{self.direction.value if self.direction else 'HOLD'} "
            f"@{self.price:.2f} [{self.regime.value}])"
        )


# ── Trade ────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """A single open or closed paper trade."""
    id: str
    asset: str
    strategy: str
    direction: Direction
    entry_price: float
    entry_time: datetime
    size_usd: float                 # notional trade size
    entry_fee: float

    # Updated while open
    highest_price: float = 0.0      # peak price (for trailing stop on longs)
    lowest_price: float = float("inf")  # trough price (for trailing stop on shorts)

    # Set on close
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_fee: float = 0.0
    exit_reason: Optional[ExitReason] = None
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    pnl_pct: float = 0.0
    hold_minutes: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def algo_key(self) -> str:
        return f"{self.strategy}-{self.asset}"

    def unrealized_pnl(self, current_price: float) -> float:
        if self.direction == Direction.LONG:
            return (current_price - self.entry_price) / self.entry_price * self.size_usd
        else:
            return (self.entry_price - current_price) / self.entry_price * self.size_usd

    def current_pnl_pct(self, current_price: float) -> float:
        if self.direction == Direction.LONG:
            return (current_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - current_price) / self.entry_price * 100

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "asset": self.asset,
            "strategy": self.strategy,
            "direction": self.direction.value,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "size_usd": self.size_usd,
            "entry_fee": self.entry_fee,
            "highest_price": self.highest_price,
            "lowest_price": self.lowest_price if self.lowest_price != float("inf") else self.entry_price,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_fee": self.exit_fee,
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "gross_pnl": self.gross_pnl,
            "net_pnl": self.net_pnl,
            "pnl_pct": self.pnl_pct,
            "hold_minutes": self.hold_minutes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Trade":
        return cls(
            id=d["id"],
            asset=d["asset"],
            strategy=d["strategy"],
            direction=Direction(d["direction"]),
            entry_price=d["entry_price"],
            entry_time=datetime.fromisoformat(d["entry_time"]).replace(tzinfo=timezone.utc)
                       if d["entry_time"].endswith("+00:00") or "Z" not in d["entry_time"]
                       else datetime.fromisoformat(d["entry_time"]),
            size_usd=d["size_usd"],
            entry_fee=d["entry_fee"],
            highest_price=d.get("highest_price", d["entry_price"]),
            lowest_price=d.get("lowest_price", d["entry_price"]),
            exit_price=d.get("exit_price"),
            exit_time=datetime.fromisoformat(d["exit_time"]) if d.get("exit_time") else None,
            exit_fee=d.get("exit_fee", 0.0),
            exit_reason=ExitReason(d["exit_reason"]) if d.get("exit_reason") else None,
            gross_pnl=d.get("gross_pnl", 0.0),
            net_pnl=d.get("net_pnl", 0.0),
            pnl_pct=d.get("pnl_pct", 0.0),
            hold_minutes=d.get("hold_minutes", 0.0),
        )


# ── AlgoState ────────────────────────────────────────────────────────────────

@dataclass
class AlgoState:
    """Per strategy×asset accounting."""
    strategy: str
    asset: str
    initial_capital: float
    available_capital: float
    peak_capital: float

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    consecutive_losses: int = 0
    circuit_breaker_until: Optional[datetime] = None
    active_trade_ids: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.strategy}-{self.asset}"

    @property
    def circuit_breaker_active(self) -> bool:
        if self.circuit_breaker_until is None:
            return False
        return datetime.now(timezone.utc) < self.circuit_breaker_until

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades * 100

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "asset": self.asset,
            "initial_capital": self.initial_capital,
            "available_capital": self.available_capital,
            "peak_capital": self.peak_capital,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "total_pnl": self.total_pnl,
            "total_fees": self.total_fees,
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker_until": self.circuit_breaker_until.isoformat()
                                     if self.circuit_breaker_until else None,
            "active_trade_ids": self.active_trade_ids,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AlgoState":
        cb_until = None
        if d.get("circuit_breaker_until"):
            cb_until = datetime.fromisoformat(d["circuit_breaker_until"])
            if cb_until.tzinfo is None:
                cb_until = cb_until.replace(tzinfo=timezone.utc)
        return cls(
            strategy=d["strategy"],
            asset=d["asset"],
            initial_capital=d["initial_capital"],
            available_capital=d["available_capital"],
            peak_capital=d.get("peak_capital", d["initial_capital"]),
            total_trades=d.get("total_trades", 0),
            winning_trades=d.get("winning_trades", 0),
            losing_trades=d.get("losing_trades", 0),
            total_pnl=d.get("total_pnl", 0.0),
            total_fees=d.get("total_fees", 0.0),
            consecutive_losses=d.get("consecutive_losses", 0),
            circuit_breaker_until=cb_until,
            active_trade_ids=d.get("active_trade_ids", []),
        )


# ── PortfolioRisk ────────────────────────────────────────────────────────────

@dataclass
class PortfolioRisk:
    """Portfolio-level risk state."""
    peak_value: float = 0.0
    current_drawdown_pct: float = 0.0
    kill_switch_active: bool = False
    kill_switch_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "peak_value": self.peak_value,
            "current_drawdown_pct": self.current_drawdown_pct,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_at": self.kill_switch_at.isoformat() if self.kill_switch_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioRisk":
        ks_at = None
        if d.get("kill_switch_at"):
            ks_at = datetime.fromisoformat(d["kill_switch_at"])
            if ks_at.tzinfo is None:
                ks_at = ks_at.replace(tzinfo=timezone.utc)
        return cls(
            peak_value=d.get("peak_value", 0.0),
            current_drawdown_pct=d.get("current_drawdown_pct", 0.0),
            kill_switch_active=d.get("kill_switch_active", False),
            kill_switch_at=ks_at,
        )


# ── RegimeSnapshot ───────────────────────────────────────────────────────────

@dataclass
class RegimeSnapshot:
    """Regime detection result for one asset."""
    asset: str
    regime: Regime
    adx: float
    atr: float
    atr_avg: float
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "regime": self.regime.value,
            "adx": round(self.adx, 2),
            "atr": round(self.atr, 4),
            "atr_avg": round(self.atr_avg, 4),
            "timestamp": self.timestamp.isoformat(),
        }


# ── Helpers ──────────────────────────────────────────────────────────────────

def new_trade_id(strategy: str, asset: str) -> str:
    """Generate a short unique trade ID."""
    short = uuid.uuid4().hex[:6]
    return f"{strategy}-{asset}-{short}"
