"""
options_bot/models.py — Shared typed data models.
All inter-module communication uses these dataclasses. No raw dicts between modules.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional


# ── Enums ───────────────────────────────────────────────────────────────────────

class OptionType(str, Enum):
    CALL = "call"
    PUT  = "put"


class OptionAction(str, Enum):
    BUY = "buy"
    # SELL excluded in v1 — short premium requires separate risk modelling


class Direction(str, Enum):
    BULLISH = "bullish"   # → CALL
    BEARISH = "bearish"   # → PUT
    NEUTRAL = "neutral"   # not actionable; must not reach the router


class Regime(str, Enum):
    TRENDING  = "trending"
    RANGING   = "ranging"
    EXPANDING = "expanding"
    UNKNOWN   = "unknown"   # insufficient data → no new entries


class SignalType(str, Enum):
    ORB  = "opening_range_breakout"
    VWAP = "vwap_trend_continuation"
    EMA  = "ema_trend_pullback"
    RVOL = "relative_volume_momentum"
    VBKR = "volatility_breakout"


class ExitReason(str, Enum):
    PROFIT_TARGET   = "profit_target"
    STOP_LOSS       = "stop_loss"
    TIME_LIMIT      = "time_limit"
    EOD_FLAT        = "eod_flat"        # force-close at end of day; distinct from max-hold TIME_LIMIT
    REGIME_CHANGE   = "regime_change"
    CIRCUIT_BREAKER = "circuit_breaker"
    KILL_SWITCH     = "kill_switch"
    MANUAL          = "manual"


# ── Regime snapshot ─────────────────────────────────────────────────────────────

@dataclass
class RegimeSnapshot:
    """Output of regime.py for one symbol. Passed to every strategy."""
    symbol:    str
    regime:    Regime
    adx:       float
    atr:       float      # latest bar ATR
    atr_avg:   float      # rolling-average ATR
    bb_width:  float      # Bollinger Band width / mid-price (0.0 if unavailable)
    timestamp: datetime

    @property
    def is_unknown(self) -> bool:
        return self.regime == Regime.UNKNOWN

    def to_dict(self) -> dict:
        return {
            "symbol":    self.symbol,
            "regime":    self.regime.value,
            "adx":       round(self.adx, 2),
            "atr":       round(self.atr, 4),
            "atr_avg":   round(self.atr_avg, 4),
            "bb_width":  round(self.bb_width, 5),
            "timestamp": self.timestamp.isoformat(),
        }


# ── Signal ───────────────────────────────────────────────────────────────────────

@dataclass
class OptionsSignal:
    """
    Standardized output of every strategy module.

    This is a paper-trading hypothesis, not a trade recommendation with
    proven edge. confidence_score is a tie-breaker for the router only —
    it must NOT be interpreted as a probability of profit.

    All five strategy fields below are required.  Any strategy that cannot
    populate them cleanly should return None instead of this object.
    """

    # ── Required: identity ──────────────────────────────────────────────────────
    strategy_name:    SignalType
    symbol:           str        # "SPY" or "QQQ"
    direction:        Direction
    timestamp:        datetime

    # ── Required: regime ────────────────────────────────────────────────────────
    regime_required:  List[str]  # Regime.value strings this strategy is valid in
    regime_at_signal: Regime

    # ── Required: price ─────────────────────────────────────────────────────────
    underlying_price: float

    # ── Signal quality ───────────────────────────────────────────────────────────
    confidence_score:         float = 0.5    # 0–1; tie-breaker only
    data_quality_ok:          bool  = True   # False when data was sparse or fell back
    data_quality_note:        str   = ""     # populated when data_quality_ok is False

    # ── Trade context (descriptive; not orders) ──────────────────────────────────
    entry_zone:   str = ""   # e.g. "breakout above 451.20"
    stop_logic:   str = ""   # e.g. "close back below range high"
    target_logic: str = ""   # e.g. "50% gain on entry premium"

    # ── Structured conditions ────────────────────────────────────────────────────
    invalidation_conditions: List[str] = field(default_factory=list)
    reason_codes:            List[str] = field(default_factory=list)

    # ── Chain quality (indicative; delayed data) ─────────────────────────────────
    liquidity_ok:            bool  = True
    spread_ok:               bool  = True

    # ── Underlying data quality ──────────────────────────────────────────────────
    underlying_quality_score: float = 1.0   # 0–1; <0.5 = marginal data

    # ── Raw indicator snapshot (journal / debugging) ─────────────────────────────
    indicators: Dict[str, float] = field(default_factory=dict)

    # ── Derived ─────────────────────────────────────────────────────────────────

    @property
    def option_type(self) -> OptionType:
        if self.direction == Direction.BULLISH:
            return OptionType.CALL
        if self.direction == Direction.BEARISH:
            return OptionType.PUT
        raise ValueError(f"option_type undefined for direction={self.direction}")

    @property
    def is_actionable(self) -> bool:
        return (
            self.direction != Direction.NEUTRAL
            and self.underlying_price > 0
            and self.regime_at_signal.value in self.regime_required
        )

    def __str__(self) -> str:
        return (
            f"OptionsSignal({self.symbol} {self.strategy_name.value} "
            f"{self.direction.value} @{self.underlying_price:.2f} "
            f"regime={self.regime_at_signal.value} conf={self.confidence_score:.2f} "
            f"dq_ok={self.data_quality_ok})"
        )


# ── Contract ─────────────────────────────────────────────────────────────────────

@dataclass
class OptionContract:
    """
    Candidate contract from contract_selector.py.
    All pricing fields are theoretical estimates from delayed/incomplete data.
    data_quality_note must always carry OPTIONS_DATA_LIMITATION.
    """
    symbol:            str
    option_type:       OptionType
    expiry:            str     # "YYYY-MM-DD"
    strike:            float
    dte:               int

    estimated_premium: float   # per-share; 1 contract = 100 × this
    estimated_delta:   float
    estimated_iv:      float

    data_quality_note: str = ""


# ── Trade ─────────────────────────────────────────────────────────────────────────

@dataclass
class OptionTrade:
    """
    Single open or closed paper trade.
    All fill_* fields are outputs of the fill simulator (simplified theoretical model).
    data_quality_note must be populated by the fill simulator on every open.
    """
    id:           str
    symbol:       str
    strategy:     str       # SignalType.value
    option_type:  OptionType
    action:       OptionAction
    expiry:       str
    strike:       float
    dte_at_entry: int

    entry_time:           datetime
    entry_fill_per_share: float    # theoretical
    contracts:            int   = 1   # always 1 in v1; no spreads
    entry_fee_usd:        float = 0.0
    entry_delta:          float = 0.0
    entry_iv:             float = 0.0
    entry_premium_total:  float = 0.0   # entry_fill × 100 × contracts

    current_premium:  float = 0.0
    current_pnl_usd:  float = 0.0
    current_pnl_pct:  float = 0.0

    exit_time:           Optional[datetime] = None
    exit_fill_per_share: Optional[float]    = None
    exit_fee_usd:        float              = 0.0
    exit_reason:         Optional[ExitReason] = None
    realized_pnl_usd:    float = 0.0
    realized_pnl_pct:    float = 0.0
    hold_days:           float = 0.0

    data_quality_note: str = ""

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def algo_key(self) -> str:
        return f"{self.strategy}-{self.symbol}"

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "symbol":               self.symbol,
            "strategy":             self.strategy,
            "option_type":          self.option_type.value,
            "action":               self.action.value,
            "expiry":               self.expiry,
            "strike":               self.strike,
            "dte_at_entry":         self.dte_at_entry,
            "entry_time":           self.entry_time.isoformat(),
            "entry_fill_per_share": self.entry_fill_per_share,
            "contracts":            self.contracts,
            "entry_fee_usd":        self.entry_fee_usd,
            "entry_delta":          self.entry_delta,
            "entry_iv":             self.entry_iv,
            "entry_premium_total":  self.entry_premium_total,
            "current_premium":      self.current_premium,
            "current_pnl_usd":      self.current_pnl_usd,
            "current_pnl_pct":      self.current_pnl_pct,
            "exit_time":            self.exit_time.isoformat() if self.exit_time else None,
            "exit_fill_per_share":  self.exit_fill_per_share,
            "exit_fee_usd":         self.exit_fee_usd,
            "exit_reason":          self.exit_reason.value if self.exit_reason else None,
            "realized_pnl_usd":     self.realized_pnl_usd,
            "realized_pnl_pct":     self.realized_pnl_pct,
            "hold_days":            self.hold_days,
            "data_quality_note":    self.data_quality_note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OptionTrade":
        return cls(
            id=d["id"],
            symbol=d["symbol"],
            strategy=d["strategy"],
            option_type=OptionType(d["option_type"]),
            action=OptionAction(d["action"]),
            expiry=d["expiry"],
            strike=d["strike"],
            dte_at_entry=d["dte_at_entry"],
            entry_time=datetime.fromisoformat(d["entry_time"]),
            entry_fill_per_share=d["entry_fill_per_share"],
            contracts=d.get("contracts", 1),
            entry_fee_usd=d.get("entry_fee_usd", 0.0),
            entry_delta=d.get("entry_delta", 0.0),
            entry_iv=d.get("entry_iv", 0.0),
            entry_premium_total=d.get("entry_premium_total", 0.0),
            current_premium=d.get("current_premium", 0.0),
            current_pnl_usd=d.get("current_pnl_usd", 0.0),
            current_pnl_pct=d.get("current_pnl_pct", 0.0),
            exit_time=datetime.fromisoformat(d["exit_time"]) if d.get("exit_time") else None,
            exit_fill_per_share=d.get("exit_fill_per_share"),
            exit_fee_usd=d.get("exit_fee_usd", 0.0),
            exit_reason=ExitReason(d["exit_reason"]) if d.get("exit_reason") else None,
            realized_pnl_usd=d.get("realized_pnl_usd", 0.0),
            realized_pnl_pct=d.get("realized_pnl_pct", 0.0),
            hold_days=d.get("hold_days", 0.0),
            data_quality_note=d.get("data_quality_note", ""),
        )


# ── AlgoSlot ──────────────────────────────────────────────────────────────────────

@dataclass
class AlgoSlot:
    """
    Per strategy × symbol accounting and ranking state.
    ranking_score is meaningless until total_trades ≥ RouterConfig.min_trades_for_ranking.
    """
    strategy: str
    symbol:   str

    total_trades:       int   = 0
    winning_trades:     int   = 0
    losing_trades:      int   = 0
    total_pnl_usd:      float = 0.0
    consecutive_losses: int   = 0
    circuit_breaker_until: Optional[datetime] = None
    active_trade_ids:   List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.strategy}-{self.symbol}"

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades else 0.0

    @property
    def avg_pnl_per_trade(self) -> float:
        return self.total_pnl_usd / self.total_trades if self.total_trades else 0.0

    @property
    def circuit_breaker_active(self) -> bool:
        if self.circuit_breaker_until is None:
            return False
        return datetime.now(timezone.utc) < self.circuit_breaker_until

    @property
    def ranking_score(self) -> float:
        """Used by router ONLY after min_trades_for_ranking. Not a profit predictor."""
        if self.avg_pnl_per_trade <= 0:
            return 0.0
        return self.win_rate * self.avg_pnl_per_trade

    def to_dict(self) -> dict:
        return {
            "strategy":             self.strategy,
            "symbol":               self.symbol,
            "total_trades":         self.total_trades,
            "winning_trades":       self.winning_trades,
            "losing_trades":        self.losing_trades,
            "total_pnl_usd":        self.total_pnl_usd,
            "consecutive_losses":   self.consecutive_losses,
            "circuit_breaker_until": (
                self.circuit_breaker_until.isoformat()
                if self.circuit_breaker_until else None
            ),
            "active_trade_ids": self.active_trade_ids,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AlgoSlot":
        cb = None
        if d.get("circuit_breaker_until"):
            cb = datetime.fromisoformat(d["circuit_breaker_until"])
            if cb.tzinfo is None:
                cb = cb.replace(tzinfo=timezone.utc)
        return cls(
            strategy=d["strategy"],
            symbol=d["symbol"],
            total_trades=d.get("total_trades", 0),
            winning_trades=d.get("winning_trades", 0),
            losing_trades=d.get("losing_trades", 0),
            total_pnl_usd=d.get("total_pnl_usd", 0.0),
            consecutive_losses=d.get("consecutive_losses", 0),
            circuit_breaker_until=cb,
            active_trade_ids=d.get("active_trade_ids", []),
        )


# ── PortfolioState ────────────────────────────────────────────────────────────────

@dataclass
class PortfolioState:
    total_premium_deployed_usd: float = 0.0

    # Equity-based kill-switch tracking.
    # cumulative_pnl_usd = sum of all realized net P&L across closed trades.
    # peak_equity_usd    = highest cumulative_pnl_usd ever reached (watermark).
    # current_drawdown_pct = drawdown from peak equity; 0 when no trades closed yet.
    # This avoids the incorrect prior behavior where closing profitable positions
    # reduced deployed premium and falsely appeared as a drawdown.
    cumulative_pnl_usd:   float = 0.0
    peak_equity_usd:      float = 0.0
    current_drawdown_pct: float = 0.0

    kill_switch_active: bool              = False
    kill_switch_at:     Optional[datetime] = None

    # Kept for legacy state file compatibility (not used in drawdown logic).
    peak_value_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total_premium_deployed_usd": self.total_premium_deployed_usd,
            "cumulative_pnl_usd":         self.cumulative_pnl_usd,
            "peak_equity_usd":            self.peak_equity_usd,
            "current_drawdown_pct":       self.current_drawdown_pct,
            "kill_switch_active":         self.kill_switch_active,
            "kill_switch_at":             self.kill_switch_at.isoformat() if self.kill_switch_at else None,
            "peak_value_usd":             self.peak_value_usd,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioState":
        ks = None
        if d.get("kill_switch_at"):
            ks = datetime.fromisoformat(d["kill_switch_at"])
            if ks.tzinfo is None:
                ks = ks.replace(tzinfo=timezone.utc)
        return cls(
            total_premium_deployed_usd=d.get("total_premium_deployed_usd", 0.0),
            cumulative_pnl_usd=d.get("cumulative_pnl_usd", 0.0),
            peak_equity_usd=d.get("peak_equity_usd", 0.0),
            current_drawdown_pct=d.get("current_drawdown_pct", 0.0),
            kill_switch_active=d.get("kill_switch_active", False),
            kill_switch_at=ks,
            peak_value_usd=d.get("peak_value_usd", 0.0),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────────

def new_trade_id(strategy: str, symbol: str) -> str:
    return f"opt-{symbol.lower()}-{strategy[:4]}-{uuid.uuid4().hex[:6]}"
