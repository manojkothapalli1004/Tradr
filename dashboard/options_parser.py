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


REPORT_RE = re.compile(
    r"^(?P<timestamp>[^|]+) \| INFO\s+\| options_bot\s+\|\s+OPENED (?P<trade_id>[^ ]+) \| (?P<symbol>[^ ]+) (?P<option_type>PUT|CALL) K=(?P<strike>[^ ]+) exp=(?P<expiry>[^ ]+) cost=\$(?P<cost>-?\d+(?:\.\d+)?)"
)

ROUTER_RE = re.compile(
    r"^(?P<timestamp>[^|]+) \| INFO\s+\| options_bot\.router\s+\| (?P<detail>ROUTER .+)$"
)

WARNING_RE = re.compile(
    r"^(?P<timestamp>[^|]+) \| WARNING \| options_bot\.contract_selector\s+\| (?P<detail>.+)$"
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


def _open_trade(item: dict[str, Any]) -> NormalizedTrade:
    entry_time = parse_datetime(item.get("entry_time"))
    hold_minutes = (now_utc() - entry_time).total_seconds() / 60 if entry_time else None
    return NormalizedTrade(
        bot="options",
        symbol=str(item.get("symbol", "unknown")),
        strategy=str(item.get("strategy", "unknown")),
        trade_id=str(item.get("id", "unknown")),
        direction=str(item.get("action", "unknown")),
        entry_time=entry_time,
        exit_time=parse_datetime(item.get("exit_time")),
        hold_minutes=float(item.get("hold_days", 0.0)) * 24 * 60 if item.get("hold_days") is not None and item.get("hold_days") else hold_minutes,
        status="open",
        realized_pnl=float(item.get("realized_pnl_usd", 0.0)),
        unrealized_pnl=float(item.get("current_pnl_usd", 0.0)) if item.get("current_pnl_usd") is not None else None,
        fees=float(item.get("entry_fee_usd", 0.0)) + float(item.get("exit_fee_usd", 0.0)),
        exit_reason=item.get("exit_reason"),
        entry_price=float(item.get("entry_fill_per_share")) if item.get("entry_fill_per_share") is not None else None,
        exit_price=float(item.get("exit_fill_per_share")) if item.get("exit_fill_per_share") is not None else None,
        size=float(item.get("contracts")) if item.get("contracts") is not None else None,
        notes=item.get("data_quality_note"),
        source="options_bot/state.json",
    )


def _closed_trade(item: dict[str, Any]) -> NormalizedTrade:
    return NormalizedTrade(
        bot="options",
        symbol=str(item.get("symbol", "unknown")),
        strategy=str(item.get("strategy", "unknown")),
        trade_id=str(item.get("id", "unknown")),
        direction=str(item.get("action", "unknown")),
        entry_time=parse_datetime(item.get("entry_time")),
        exit_time=parse_datetime(item.get("exit_time")),
        hold_minutes=float(item.get("hold_days", 0.0)) * 24 * 60 if item.get("hold_days") is not None else None,
        status="closed",
        realized_pnl=float(item.get("realized_pnl_usd", 0.0)),
        unrealized_pnl=0.0,
        fees=float(item.get("entry_fee_usd", 0.0)) + float(item.get("exit_fee_usd", 0.0)),
        exit_reason=item.get("exit_reason"),
        entry_price=float(item.get("entry_fill_per_share")) if item.get("entry_fill_per_share") is not None else None,
        exit_price=float(item.get("exit_fill_per_share")) if item.get("exit_fill_per_share") is not None else None,
        size=float(item.get("contracts")) if item.get("contracts") is not None else None,
        notes=item.get("data_quality_note"),
        source="options_bot/state.json",
    )


def _recent_activity(log_lines: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for line in log_lines:
        line = line.strip()
        report_match = REPORT_RE.match(line)
        if report_match:
            rows.append(
                {
                    "timestamp": parse_datetime(report_match.group("timestamp")),
                    "event": "opened",
                    "detail": f"{report_match.group('trade_id')} {report_match.group('symbol')} {report_match.group('option_type')} cost ${report_match.group('cost')}",
                    "severity": "active",
                }
            )
            continue
        router_match = ROUTER_RE.match(line)
        if router_match:
            rows.append(
                {
                    "timestamp": parse_datetime(router_match.group("timestamp")),
                    "event": "router",
                    "detail": router_match.group("detail"),
                    "severity": "inactive" if "REJECT" in router_match.group("detail") or "SKIP" in router_match.group("detail") else "healthy",
                }
            )
            continue
        warning_match = WARNING_RE.match(line)
        if warning_match:
            rows.append(
                {
                    "timestamp": parse_datetime(warning_match.group("timestamp")),
                    "event": "warning",
                    "detail": warning_match.group("detail"),
                    "severity": "warning",
                }
            )
    if not rows:
        return pd.DataFrame(columns=["timestamp", "event", "detail", "severity"])
    return pd.DataFrame(rows).sort_values("timestamp", ascending=False)


def parse_options_data(base_dir: Path) -> ParsedBotData:
    state_path = base_dir / "options_bot" / "state.json"
    log_path = base_dir / "options_bot" / "options_bot.log"
    state = _read_json(state_path)
    log_lines = _read_lines(log_path)
    notes: list[str] = []
    source_rows = [
        {"source": "options_bot/state.json", "available": state is not None, "detail": "State with open positions, completed trades, and risk fields"},
        {"source": "options_bot/options_bot.log", "available": bool(log_lines), "detail": "Operational log with router decisions and warnings"},
    ]

    open_items = state.get("open_trades", []) if state else []
    completed_items = state.get("completed_trades", []) if state else []
    trades = [*[_open_trade(item) for item in open_items], *[_closed_trade(item) for item in completed_items]]
    trade_frame = trades_to_frame(trades)
    open_frame = trade_frame[trade_frame["status"] == "open"].copy() if not trade_frame.empty else trade_frame.copy()
    completed_frame = trade_frame[trade_frame["status"] == "closed"].copy() if not trade_frame.empty else trade_frame.copy()

    algos = state.get("algos", {}) if state else {}
    strategy_rows: list[dict[str, Any]] = []
    for algo_key, algo_data in algos.items():
        strategy_rows.append(
            {
                "strategy_key": algo_key,
                "strategy": algo_data.get("strategy", "unknown"),
                "symbol": algo_data.get("symbol", "unknown"),
                "total_trades": int(algo_data.get("total_trades", 0)),
                "wins": int(algo_data.get("winning_trades", 0)),
                "losses": int(algo_data.get("losing_trades", 0)),
                "realized_pnl": float(algo_data.get("total_pnl_usd", 0.0)),
                "active_trade_ids": len(algo_data.get("active_trade_ids", [])),
                "circuit_breaker_until": algo_data.get("circuit_breaker_until"),
                "win_rate": safe_divide(float(algo_data.get("winning_trades", 0)), float(algo_data.get("total_trades", 0))),
                "inactive": int(algo_data.get("total_trades", 0)) == 0 and not algo_data.get("active_trade_ids"),
            }
        )
    strategy_frame = pd.DataFrame(strategy_rows)

    symbol_frame = pd.DataFrame(columns=["symbol", "realized_pnl", "unrealized_pnl", "trades", "open_positions"])
    if not trade_frame.empty:
        grouped = trade_frame.groupby("symbol", dropna=False)
        symbol_frame = grouped.agg(
            realized_pnl=("realized_pnl", lambda s: float(pd.Series(s).fillna(0).sum())),
            unrealized_pnl=("unrealized_pnl", lambda s: float(pd.Series(s).fillna(0).sum())),
            trades=("trade_id", "count"),
        ).reset_index()
        open_counts = open_frame.groupby("symbol").size().reset_index(name="open_positions") if not open_frame.empty else pd.DataFrame(columns=["symbol", "open_positions"])
        symbol_frame = symbol_frame.merge(open_counts, on="symbol", how="left").fillna({"open_positions": 0})

    exit_reason_frame = pd.DataFrame(columns=["exit_reason", "count"])
    if not completed_frame.empty:
        exit_reason_frame = completed_frame.fillna({"exit_reason": "open_or_missing"}).groupby("exit_reason").size().reset_index(name="count").sort_values("count", ascending=False)

    inactivity_frame = pd.DataFrame(columns=["strategy", "symbol", "status"])
    if not strategy_frame.empty:
        inactivity_frame = strategy_frame[strategy_frame["inactive"]].copy()
        inactivity_frame["status"] = "inactive"
        inactivity_frame = inactivity_frame[["strategy", "symbol", "status", "total_trades", "active_trade_ids"]].sort_values(["strategy", "symbol"])

    portfolio = state.get("portfolio", {}) if state else {}
    recent_activity = _recent_activity(log_lines)

    if state is None:
        notes.append("Options state file missing or unreadable.")
    if not log_lines:
        notes.append("Options log missing or unreadable; router and warning context are limited.")
    if not completed_items:
        notes.append("No completed options trades yet; realized performance sections will be sparse.")
    if open_items:
        notes.append("Options fills are simulator-limited and based on delayed data; treat open P&L as indicative only.")

    risk_rows = [
        {"label": "Kill switch", "value": "Active" if portfolio.get("kill_switch_active") else "Off", "status": "danger" if portfolio.get("kill_switch_active") else "healthy", "detail": "Portfolio-level options kill switch"},
        {"label": "Premium deployed", "value": f"${float(portfolio.get('total_premium_deployed_usd', 0.0)):,.2f}", "status": "active" if float(portfolio.get("total_premium_deployed_usd", 0.0)) > 0 else "inactive", "detail": "Current premium deployed across open positions"},
        {"label": "Current drawdown", "value": f"{float(portfolio.get('current_drawdown_pct', 0.0)):.2f}%", "status": "warning" if float(portfolio.get("current_drawdown_pct", 0.0)) > 5 else "healthy", "detail": "Current options portfolio drawdown"},
        {"label": "Position cap usage", "value": f"{len(open_items)}/2", "status": "warning" if len(open_items) >= 2 else ("active" if len(open_items) else "inactive"), "detail": "Configured open-position cap inferred from logs and state"},
    ]
    risk_items = pd.DataFrame(risk_rows)

    equity_curve = pd.DataFrame(columns=["timestamp", "equity", "realized_pnl", "fees", "trade_count"])
    if not trade_frame.empty:
        running = trade_frame.copy()
        running["timestamp"] = running["exit_time"].fillna(running["entry_time"])
        running["realized_component"] = running["realized_pnl"].fillna(0.0)
        running["fees_component"] = running["fees"].fillna(0.0)
        running = running.sort_values("timestamp")
        running["realized_pnl"] = running["realized_component"].cumsum()
        running["fees"] = running["fees_component"].cumsum()
        running["trade_count"] = range(1, len(running.index) + 1)
        running["equity"] = running["realized_pnl"]
        equity_curve = running[["timestamp", "equity", "realized_pnl", "fees", "trade_count"]].copy()

    realized_pnl = float(portfolio.get("cumulative_pnl_usd", completed_frame["realized_pnl"].fillna(0).sum() if not completed_frame.empty else 0.0))
    unrealized_pnl = float(open_frame["unrealized_pnl"].fillna(0).sum()) if not open_frame.empty else 0.0
    total_fees = float(trade_frame["fees"].fillna(0).sum()) if not trade_frame.empty else 0.0
    win_rate = safe_divide(float((completed_frame["realized_pnl"] > 0).sum()) if not completed_frame.empty else 0.0, float(len(completed_frame.index)))
    avg_hold = float(completed_frame["hold_minutes"].mean()) if not completed_frame.empty else (float(open_frame["hold_minutes"].mean()) if not open_frame.empty else None)

    summary = BotSummary(
        bot="options",
        healthy=state is not None,
        status=summarize_status(True, bool(portfolio.get("kill_switch_active")), len(open_items), notes),
        open_positions=len(open_items),
        closed_trades=len(completed_items),
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        fees=total_fees,
        win_rate=win_rate,
        avg_hold_minutes=avg_hold,
        kill_switch_active=bool(portfolio.get("kill_switch_active")),
        last_updated=max(
            [dt for dt in [parse_datetime(state.get("start_time") if state else None), parse_datetime(state.get("last_regimes", {}).get("SPY", {}).get("timestamp") if state else None), parse_datetime(state.get("last_regimes", {}).get("QQQ", {}).get("timestamp") if state else None)] if dt is not None],
            default=None,
        ),
        notes=notes,
    )

    return ParsedBotData(
        bot="options",
        summary=summary,
        trades=trade_frame,
        open_positions=open_frame,
        completed_trades=completed_frame,
        strategy_stats=strategy_frame.sort_values(["active_trade_ids", "realized_pnl"], ascending=[False, False]) if not strategy_frame.empty else strategy_frame,
        symbol_stats=symbol_frame.sort_values(["unrealized_pnl", "realized_pnl"], ascending=[False, False]) if not symbol_frame.empty else symbol_frame,
        exit_reason_stats=exit_reason_frame,
        inactivity=inactivity_frame,
        equity_curve=equity_curve,
        recent_activity=recent_activity,
        risk_items=risk_items,
        source_status=pd.DataFrame(source_rows),
        raw_notes=notes,
    )
