"""
Spot-bot analysis — pure functions over raw paper_trading_state.json data.
No I/O. No side effects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class ComboMetrics:
    combo: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    fees: float = 0.0
    avg_hold_minutes: float = 0.0
    win_rate: float = 0.0


@dataclass
class AnalysisResult:
    # summary
    total_trades: int = 0
    active_trades_count: int = 0
    net_pnl: float = 0.0
    fees_paid: float = 0.0
    portfolio_value: float = 0.0
    win_rate: float = 0.0
    avg_hold_minutes: float = 0.0

    # breakdowns
    exit_reason_breakdown: Dict[str, int] = field(default_factory=dict)
    combo_metrics: Dict[str, ComboMetrics] = field(default_factory=dict)

    # rankings
    strongest_combo: Optional[str] = None
    weakest_combo: Optional[str] = None

    # recommendations
    sample_confidence: str = "LOW"
    continue_recommendation: str = ""
    narrow_recommendation: str = ""

    # metadata
    start_time: Optional[str] = None
    analysis_time: str = ""
    runtime_hours: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _parse_exit_reason(exit_reason: str) -> str:
    """Classify exit_reason string into a bucket."""
    if not exit_reason:
        return "unknown"
    r = exit_reason.upper()
    if "TIME LIMIT" in r or "TIME_LIMIT" in r:
        return "time_limit"
    if "TRAILING" in r:
        return "trailing_stop"
    if "STOP LOSS" in r or "STOP_LOSS" in r:
        return "stop_loss"
    if "OPPOSITE" in r:
        return "opposite_signal"
    if "TAKE PROFIT" in r or "TAKE_PROFIT" in r:
        return "take_profit"
    return "other"


def _combo_key(algo_name: str, asset: str) -> str:
    """Normalize to strategy@ASSET."""
    # algo field in trades is "strategy (ASSET)" format
    m = re.match(r"^(.+?)\s*\((\w+)\)$", algo_name)
    if m:
        return f"{m.group(1).strip()}@{m.group(2)}"
    return f"{algo_name}@{asset}"


def analyze_spot_state(state: dict) -> AnalysisResult:
    """Analyze raw paper_trading_state.json dict. Pure function."""
    result = AnalysisResult()
    result.analysis_time = datetime.now(timezone.utc).isoformat()
    result.start_time = state.get("start_time")

    if result.start_time:
        try:
            start = datetime.fromisoformat(result.start_time)
            now = datetime.now(timezone.utc)
            result.runtime_hours = (now - start).total_seconds() / 3600
        except (ValueError, TypeError):
            pass

    # ── Completed trades ────────────────────────────────────────────
    completed = state.get("completed_trades", [])
    result.total_trades = len(completed)

    wins = 0
    total_hold = 0.0
    exit_reasons: Dict[str, int] = {}
    combo_data: Dict[str, ComboMetrics] = {}

    for t in completed:
        algo = t.get("algo", "")
        asset = t.get("asset", "")
        combo = _combo_key(algo, asset)

        if combo not in combo_data:
            combo_data[combo] = ComboMetrics(combo=combo)
        cm = combo_data[combo]

        pnl = t.get("net_pnl", 0.0)
        fees = t.get("total_fees", 0.0)
        hold = t.get("hold_minutes", 0.0)

        cm.trades += 1
        cm.net_pnl += pnl
        cm.fees += fees

        result.net_pnl += pnl
        result.fees_paid += fees
        total_hold += hold

        if pnl > 0:
            wins += 1
            cm.wins += 1
        else:
            cm.losses += 1

        reason = _parse_exit_reason(t.get("exit_reason", ""))
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    if result.total_trades > 0:
        result.win_rate = (wins / result.total_trades) * 100
        result.avg_hold_minutes = total_hold / result.total_trades

    for cm in combo_data.values():
        if cm.trades > 0:
            cm.win_rate = (cm.wins / cm.trades) * 100
            cm.avg_hold_minutes = (
                sum(t.get("hold_minutes", 0) for t in completed
                    if _combo_key(t.get("algo", ""), t.get("asset", "")) == cm.combo)
                / cm.trades
            )

    result.exit_reason_breakdown = exit_reasons
    result.combo_metrics = combo_data

    # ── Active trades ───────────────────────────────────────────────
    result.active_trades_count = len(state.get("active_trades", []))

    # ── Portfolio value ─────────────────────────────────────────────
    algos = state.get("algos", {})
    if isinstance(algos, dict):
        result.portfolio_value = sum(
            a.get("available_capital", 0) + a.get("total_pnl", 0)
            for a in algos.values()
        )
    elif isinstance(algos, list):
        result.portfolio_value = sum(
            a.get("available_capital", 0) + a.get("total_pnl", 0)
            for a in algos
        )

    # ── Rankings ────────────────────────────────────────────────────
    if combo_data:
        by_pnl = sorted(combo_data.values(), key=lambda c: c.net_pnl, reverse=True)
        result.strongest_combo = by_pnl[0].combo
        result.weakest_combo = by_pnl[-1].combo

    # ── Confidence ──────────────────────────────────────────────────
    if result.total_trades >= 30:
        result.sample_confidence = "HIGH"
    elif result.total_trades >= 10:
        result.sample_confidence = "MEDIUM"
    else:
        result.sample_confidence = "LOW"

    # ── Recommendations ─────────────────────────────────────────────
    result.continue_recommendation = _continue_recommendation(result)
    result.narrow_recommendation = _narrow_recommendation(result, combo_data)

    return result


def _continue_recommendation(r: AnalysisResult) -> str:
    if r.total_trades < 5:
        return "INSUFFICIENT DATA — need at least 5 completed trades to evaluate. Continue running."

    if r.total_trades < 10:
        prefix = "EARLY SIGNAL"
    elif r.total_trades < 30:
        prefix = "TENTATIVE"
    else:
        prefix = "ASSESSED"

    if r.net_pnl > 0 and r.win_rate >= 50:
        return f"{prefix} — positive P&L (${r.net_pnl:.2f}) with {r.win_rate:.0f}% win rate. Worth continuing."
    if r.net_pnl > 0 and r.win_rate < 50:
        return f"{prefix} — positive P&L (${r.net_pnl:.2f}) but low win rate ({r.win_rate:.0f}%). Wins are larger than losses. Continue with monitoring."
    if r.net_pnl <= 0 and r.win_rate >= 50:
        return f"{prefix} — negative P&L (${r.net_pnl:.2f}) despite {r.win_rate:.0f}% win rate. Fees or large losses eating gains. Review exit logic."
    return f"{prefix} — negative P&L (${r.net_pnl:.2f}) with {r.win_rate:.0f}% win rate. Consider pausing if trend persists past 30 trades."


def _narrow_recommendation(r: AnalysisResult, combos: Dict[str, ComboMetrics]) -> str:
    if r.total_trades < 10:
        return "TOO EARLY — need at least 10 trades across combos before narrowing."

    profitable = [c for c in combos.values() if c.net_pnl > 0 and c.trades >= 3]
    unprofitable = [c for c in combos.values() if c.net_pnl < 0 and c.trades >= 3]
    untested = [c for c in combos.values() if c.trades < 3]

    parts = []
    if unprofitable:
        names = ", ".join(c.combo for c in sorted(unprofitable, key=lambda x: x.net_pnl))
        parts.append(f"Consider dropping: {names}")
    if profitable:
        names = ", ".join(c.combo for c in sorted(profitable, key=lambda x: x.net_pnl, reverse=True))
        parts.append(f"Keep: {names}")
    if untested:
        names = ", ".join(c.combo for c in untested)
        parts.append(f"Need more data: {names}")

    if not parts:
        return "No clear narrowing signal yet."

    return " | ".join(parts)
