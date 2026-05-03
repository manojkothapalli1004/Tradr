from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd


@dataclass(slots=True)
class NormalizedTrade:
    bot: str
    symbol: str
    strategy: str
    trade_id: str
    direction: str
    entry_time: datetime | None
    exit_time: datetime | None
    hold_minutes: float | None
    status: str
    realized_pnl: float | None
    unrealized_pnl: float | None
    fees: float | None
    exit_reason: str | None
    entry_price: float | None
    exit_price: float | None
    size: float | None
    notes: str | None
    source: str


@dataclass(slots=True)
class BotSummary:
    bot: str
    healthy: bool
    status: str
    open_positions: int
    closed_trades: int
    realized_pnl: float
    unrealized_pnl: float
    fees: float
    win_rate: float | None
    avg_hold_minutes: float | None
    kill_switch_active: bool
    last_updated: datetime | None
    notes: list[str]


@dataclass(slots=True)
class ParsedBotData:
    bot: str
    summary: BotSummary
    trades: pd.DataFrame
    open_positions: pd.DataFrame
    completed_trades: pd.DataFrame
    strategy_stats: pd.DataFrame
    symbol_stats: pd.DataFrame
    exit_reason_stats: pd.DataFrame
    inactivity: pd.DataFrame
    equity_curve: pd.DataFrame
    recent_activity: pd.DataFrame
    risk_items: pd.DataFrame
    source_status: pd.DataFrame
    raw_notes: list[str]


TRADE_COLUMNS = [
    "bot",
    "symbol",
    "strategy",
    "trade_id",
    "direction",
    "entry_time",
    "exit_time",
    "hold_minutes",
    "status",
    "realized_pnl",
    "unrealized_pnl",
    "fees",
    "exit_reason",
    "entry_price",
    "exit_price",
    "size",
    "notes",
    "source",
]


EMPTY_PARSED_DATA = ParsedBotData(
    bot="unknown",
    summary=BotSummary(
        bot="unknown",
        healthy=False,
        status="data unavailable",
        open_positions=0,
        closed_trades=0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        fees=0.0,
        win_rate=None,
        avg_hold_minutes=None,
        kill_switch_active=False,
        last_updated=None,
        notes=["Source unavailable"],
    ),
    trades=pd.DataFrame(columns=TRADE_COLUMNS),
    open_positions=pd.DataFrame(columns=TRADE_COLUMNS),
    completed_trades=pd.DataFrame(columns=TRADE_COLUMNS),
    strategy_stats=pd.DataFrame(),
    symbol_stats=pd.DataFrame(),
    exit_reason_stats=pd.DataFrame(),
    inactivity=pd.DataFrame(),
    equity_curve=pd.DataFrame(columns=["timestamp", "equity", "realized_pnl", "fees", "trade_count"]),
    recent_activity=pd.DataFrame(columns=["timestamp", "event", "detail", "severity"]),
    risk_items=pd.DataFrame(columns=["label", "value", "status", "detail"]),
    source_status=pd.DataFrame(columns=["source", "available", "detail"]),
    raw_notes=[],
)


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_trade_row(trade: NormalizedTrade) -> dict[str, Any]:
    return asdict(trade)


def trades_to_frame(trades: list[NormalizedTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    frame = pd.DataFrame(normalize_trade_row(trade) for trade in trades)
    for column in ["entry_time", "exit_time"]:
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    return frame.sort_values(by=["entry_time", "trade_id"], ascending=[False, False], na_position="last")


def safe_divide(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return numerator / denominator


def pct(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100.0


def with_hold_days(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if "hold_minutes" not in result.columns:
        result["hold_days"] = pd.Series(dtype=float)
        return result
    result["hold_days"] = pd.to_numeric(result["hold_minutes"], errors="coerce") / (60 * 24)
    return result


def summarize_status(healthy: bool, kill_switch_active: bool, open_positions: int, notes: list[str]) -> str:
    if kill_switch_active:
        return "kill switch active"
    if not healthy:
        return "warning"
    if open_positions > 0:
        return "active"
    if notes:
        return "idle with notes"
    return "healthy"
