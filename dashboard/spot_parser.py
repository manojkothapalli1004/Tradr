from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from .normalize import BotSummary, NormalizedTrade, ParsedBotData, now_utc, parse_datetime, safe_divide, summarize_status, trades_to_frame
except ImportError:
    from normalize import BotSummary, NormalizedTrade, ParsedBotData, now_utc, parse_datetime, safe_divide, summarize_status, trades_to_frame


PORTFOLIO_RE = re.compile(
    r"^(?P<timestamp>[^|]+) \| INFO \| Portfolio: \$(?P<equity>-?\d+(?:\.\d+)?) \| Net P&L: \$(?P<pnl>-?\d+(?:\.\d+)?) \| Fees: \$(?P<fees>-?\d+(?:\.\d+)?) \| Trades: (?P<trades>\d+) \| Win Rate: (?P<win_rate>-?\d+(?:\.\d+)?)% \| Avg Hold: (?P<avg_hold>-?\d+(?:\.\d+)?)m$"
)

OPEN_RE = re.compile(
    r"^(?P<timestamp>[^|]+) \| INFO \| TRADE_OPEN strategy=(?P<strategy>[^ ]+) asset=(?P<asset>[^ ]+) signal=(?P<signal>[^ ]+) price=(?P<price>-?\d+(?:\.\d+)?) size=\$(?P<size>-?\d+(?:\.\d+)?) trade_id=(?P<trade_id>[^ ]+)$"
)

EXIT_RE = re.compile(
    r"^(?P<timestamp>[^|]+) \| INFO \| STOP_EXIT trade_id=(?P<trade_id>[^ ]+) algo=(?P<strategy>[^ ]+) asset=(?P<asset>[^ ]+) reason='(?P<reason>.+)' net_pnl=\$(?P<net_pnl>-?\d+(?:\.\d+)?) pnl_pct=(?P<pnl_pct>-?\d+(?:\.\d+)?)% hold=(?P<hold>-?\d+(?:\.\d+)?)m$"
)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


def _build_trade_from_state(item: dict[str, Any]) -> NormalizedTrade:
    return NormalizedTrade(
        bot="spot",
        symbol=str(item.get("asset", "unknown")),
        strategy=str(item.get("algo", "unknown")),
        trade_id=str(item.get("id", "unknown")),
        direction=str(item.get("signal", "unknown")),
        entry_time=parse_datetime(item.get("entry_time")),
        exit_time=parse_datetime(item.get("exit_time")),
        hold_minutes=float(item["hold_minutes"]) if item.get("hold_minutes") is not None else None,
        status=str(item.get("status", "unknown")),
        realized_pnl=float(item["net_pnl"]) if item.get("net_pnl") is not None else None,
        unrealized_pnl=None,
        fees=float(item["total_fees"]) if item.get("total_fees") is not None else None,
        exit_reason=item.get("exit_reason"),
        entry_price=float(item["entry_price"]) if item.get("entry_price") is not None else None,
        exit_price=float(item["exit_price"]) if item.get("exit_price") is not None else None,
        size=float(item["size"]) if item.get("size") is not None else None,
        notes=item.get("reason"),
        source="paper_trading_state.json",
    )


def _build_open_trade(item: dict[str, Any]) -> NormalizedTrade:
    return NormalizedTrade(
        bot="spot",
        symbol=str(item.get("asset", "unknown")),
        strategy=str(item.get("algo", "unknown")),
        trade_id=str(item.get("id", "unknown")),
        direction=str(item.get("signal", "unknown")),
        entry_time=parse_datetime(item.get("entry_time")),
        exit_time=None,
        hold_minutes=(now_utc() - parse_datetime(item.get("entry_time"))).total_seconds() / 60 if parse_datetime(item.get("entry_time")) else None,
        status="open",
        realized_pnl=None,
        unrealized_pnl=None,
        fees=float(item.get("entry_fee", 0.0)),
        exit_reason=None,
        entry_price=float(item["entry_price"]) if item.get("entry_price") is not None else None,
        exit_price=None,
        size=float(item["size"]) if item.get("size") is not None else None,
        notes=item.get("reason"),
        source="paper_trading_state.json",
    )


def _portfolio_curve(log_lines: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for line in log_lines:
        match = PORTFOLIO_RE.match(line.strip())
        if not match:
            continue
        rows.append(
            {
                "timestamp": parse_datetime(match.group("timestamp")),
                "equity": float(match.group("equity")),
                "realized_pnl": float(match.group("pnl")),
                "fees": float(match.group("fees")),
                "trade_count": int(match.group("trades")),
                "win_rate": float(match.group("win_rate")),
                "avg_hold_minutes": float(match.group("avg_hold")),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["timestamp", "equity", "realized_pnl", "fees", "trade_count", "win_rate", "avg_hold_minutes"])
    frame = pd.DataFrame(rows).drop_duplicates(subset=["timestamp"], keep="last")
    return frame.sort_values("timestamp")


def _recent_activity(log_lines: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for line in log_lines:
        line = line.strip()
        open_match = OPEN_RE.match(line)
        if open_match:
            rows.append(
                {
                    "timestamp": parse_datetime(open_match.group("timestamp")),
                    "event": "trade_open",
                    "detail": f"{open_match.group('strategy')} {open_match.group('asset')} {open_match.group('signal')} @ {open_match.group('price')}",
                    "severity": "active",
                }
            )
            continue
        exit_match = EXIT_RE.match(line)
        if exit_match:
            rows.append(
                {
                    "timestamp": parse_datetime(exit_match.group("timestamp")),
                    "event": "trade_exit",
                    "detail": f"{exit_match.group('strategy')} {exit_match.group('asset')} {exit_match.group('reason')}",
                    "severity": "healthy" if float(exit_match.group("net_pnl")) >= 0 else "warning",
                }
            )
    if not rows:
        return pd.DataFrame(columns=["timestamp", "event", "detail", "severity"])
    return pd.DataFrame(rows).sort_values("timestamp", ascending=False)


def parse_spot_data(base_dir: Path) -> ParsedBotData:
    state_path = base_dir / "paper_trading_state.json"
    log_path = base_dir / "paper_trading_v3.log"
    state = _read_json(state_path)
    log_lines = _read_lines(log_path)
    notes: list[str] = []
    source_rows = [
        {"source": str(state_path.name), "available": state is not None, "detail": "State with active and completed spot trades"},
        {"source": str(log_path.name), "available": bool(log_lines), "detail": "Operational log with portfolio snapshots and activity"},
    ]

    completed_items = state.get("completed_trades", []) if state else []
    active_items = []
    if state:
        active_items = state.get("active_trades")
        if active_items is None:
            active_items = state.get("open_trades", [])
    trades = [*[_build_trade_from_state(item) for item in completed_items], *[_build_open_trade(item) for item in active_items]]
    trade_frame = trades_to_frame(trades)
    completed_frame = trade_frame[trade_frame["status"] == "closed"].copy() if not trade_frame.empty else trade_frame.copy()
    open_frame = trade_frame[trade_frame["status"] == "open"].copy() if not trade_frame.empty else trade_frame.copy()

    algos = state.get("algos", {}) if state else {}
    strategy_rows: list[dict[str, Any]] = []
    for algo_key, algo_data in algos.items():
        strategy_rows.append(
            {
                "strategy_key": algo_key,
                "strategy": algo_data.get("name", algo_data.get("strategy", "unknown")),
                "symbol": algo_data.get("asset", algo_data.get("symbol", "unknown")),
                "total_trades": int(algo_data.get("total_trades", 0)),
                "wins": int(algo_data.get("winning_trades", 0)),
                "losses": int(algo_data.get("losing_trades", 0)),
                "realized_pnl": float(algo_data.get("total_pnl", 0.0)),
                "fees": float(algo_data.get("total_fees", 0.0)),
                "available_capital": float(algo_data.get("available_capital", 0.0)),
                "circuit_breaker_until": algo_data.get("circuit_breaker_until"),
                "active_trade_ids": len(algo_data.get("active_positions", [])),
                "win_rate": safe_divide(float(algo_data.get("winning_trades", 0)), float(algo_data.get("total_trades", 0))),
                "inactive": int(algo_data.get("total_trades", 0)) == 0 and not algo_data.get("active_positions"),
            }
        )
    strategy_frame = pd.DataFrame(strategy_rows)
    symbol_frame = pd.DataFrame(columns=["symbol", "realized_pnl", "fees", "trades", "wins", "win_rate"])
    if not completed_frame.empty:
        symbol_frame = (
            completed_frame.groupby("symbol", dropna=False)
            .agg(realized_pnl=("realized_pnl", "sum"), fees=("fees", "sum"), trades=("trade_id", "count"), wins=("realized_pnl", lambda s: int((s > 0).sum())))
            .reset_index()
        )
        symbol_frame["win_rate"] = symbol_frame.apply(lambda row: safe_divide(float(row["wins"]), float(row["trades"])), axis=1)

    exit_reason_frame = pd.DataFrame(columns=["exit_reason", "count"])
    if not completed_frame.empty:
        exit_reason_frame = (
            completed_frame.assign(exit_reason=completed_frame["exit_reason"].fillna("unknown"))
            .groupby("exit_reason", dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
        )

    inactivity_frame = pd.DataFrame(columns=["strategy", "symbol", "status"])
    if not strategy_frame.empty:
        inactivity_frame = strategy_frame[strategy_frame["inactive"]].copy()
        inactivity_frame["status"] = "inactive"
        inactivity_frame = inactivity_frame[["strategy", "symbol", "status", "total_trades", "active_trade_ids"]].sort_values(["strategy", "symbol"])

    equity_curve = _portfolio_curve(log_lines)
    recent_activity = _recent_activity(log_lines)

    latest_curve = equity_curve.iloc[-1].to_dict() if not equity_curve.empty else {}
    portfolio_risk = state.get("portfolio_risk", {}) if state else {}
    realized_pnl = float(latest_curve.get("realized_pnl", completed_frame["realized_pnl"].sum() if not completed_frame.empty else 0.0))
    fees = float(latest_curve.get("fees", completed_frame["fees"].sum() if not completed_frame.empty else 0.0))
    closed_trades = int(latest_curve.get("trade_count", len(completed_frame.index)))
    open_positions = len(open_frame.index)
    win_rate = float(latest_curve["win_rate"]) / 100.0 if latest_curve.get("win_rate") is not None else safe_divide(float((completed_frame["realized_pnl"] > 0).sum()) if not completed_frame.empty else 0.0, float(len(completed_frame.index)))
    avg_hold = float(latest_curve.get("avg_hold_minutes", completed_frame["hold_minutes"].mean() if not completed_frame.empty else 0.0)) if (latest_curve or not completed_frame.empty) else None
    if state is None:
        notes.append("Spot state file missing or unreadable.")
    if not log_lines:
        notes.append("Spot log missing or unreadable; equity history and recent activity are limited.")
    if open_frame["unrealized_pnl"].dropna().empty if not open_frame.empty else True:
        notes.append("Spot unrealized P&L is not persisted in the current state format.")

    risk_items = pd.DataFrame(
        [
            {"label": "Kill switch", "value": "Active" if portfolio_risk.get("kill_switch_active") else "Off", "status": "danger" if portfolio_risk.get("kill_switch_active") else "healthy", "detail": "Portfolio-level kill switch"},
            {"label": "Drawdown", "value": f"{float(portfolio_risk.get('current_drawdown_pct', 0.0)):.2f}%", "status": "warning" if float(portfolio_risk.get("current_drawdown_pct", 0.0)) > 5 else "healthy", "detail": "Current portfolio drawdown"},
            {"label": "Open positions", "value": str(open_positions), "status": "active" if open_positions else "inactive", "detail": "Active spot trades from state"},
        ]
    )

    summary = BotSummary(
        bot="spot",
        healthy=state is not None,
        status=summarize_status(True, bool(portfolio_risk.get("kill_switch_active")), open_positions, notes),
        open_positions=open_positions,
        closed_trades=closed_trades,
        realized_pnl=realized_pnl,
        unrealized_pnl=0.0,
        fees=fees,
        win_rate=win_rate,
        avg_hold_minutes=avg_hold,
        kill_switch_active=bool(portfolio_risk.get("kill_switch_active")),
        last_updated=equity_curve["timestamp"].max() if not equity_curve.empty else parse_datetime(state.get("start_time") if state else None),
        notes=notes,
    )

    return ParsedBotData(
        bot="spot",
        summary=summary,
        trades=trade_frame,
        open_positions=open_frame,
        completed_trades=completed_frame,
        strategy_stats=strategy_frame.sort_values(["realized_pnl", "total_trades"], ascending=[False, False]) if not strategy_frame.empty else strategy_frame,
        symbol_stats=symbol_frame.sort_values(["realized_pnl", "trades"], ascending=[False, False]) if not symbol_frame.empty else symbol_frame,
        exit_reason_stats=exit_reason_frame,
        inactivity=inactivity_frame,
        equity_curve=equity_curve,
        recent_activity=recent_activity,
        risk_items=risk_items,
        source_status=pd.DataFrame(source_rows),
        raw_notes=notes,
    )
