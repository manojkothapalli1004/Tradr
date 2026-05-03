"""
Exit Forensics Agent V1 — read-only sub-agent for analyzing exit behavior
across spot and options bots.

Pure analysis first: consumes completed-trade payloads and emits typed,
structured forensics outputs. Optional helper functions can read state files,
but no mutation or execution occurs.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


# ── Models ───────────────────────────────────────────────────────────


@dataclass
class ExitReasonStats:
    reason: str
    trades: int = 0
    net_pnl: float = 0.0
    avg_hold: float = 0.0


@dataclass
class PathExitStats:
    path: str
    trades: int = 0
    exit_reason_breakdown: Dict[str, int] = field(default_factory=dict)
    net_pnl_by_reason: Dict[str, float] = field(default_factory=dict)
    avg_hold_by_reason: Dict[str, float] = field(default_factory=dict)


@dataclass
class ExitForensicsResult:
    bot: str
    total_exits: int = 0
    exit_reason_breakdown: Dict[str, int] = field(default_factory=dict)
    exit_reason_stats: Dict[str, ExitReasonStats] = field(default_factory=dict)
    path_exit_stats: Dict[str, PathExitStats] = field(default_factory=dict)

    informative_exits: int = 0
    forced_exits: int = 0
    time_limit_exits: int = 0
    forced_cleanup_ratio: float = 0.0
    time_limit_ratio: float = 0.0

    recommendation: str = "exits healthy"
    assessment: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


INFORMATIVE_EXITS = frozenset({
    "trailing_stop", "stop_loss", "take_profit", "profit_target",
    "opposite_signal", "regime_change",
})
FORCED_EXITS = frozenset({
    "time_limit", "eod_flat", "circuit_breaker", "kill_switch",
})


# ── Parsing helpers ──────────────────────────────────────────────────


def _normalize_reason(reason: str) -> str:
    if not reason:
        return "unknown"
    r = reason.strip().lower()
    if "time limit" in r or r == "time_limit":
        return "time_limit"
    if "take profit" in r or r == "take_profit" or r == "profit_target":
        return "profit_target"
    if "stop loss" in r or r == "stop_loss":
        return "stop_loss"
    if "trailing" in r:
        return "trailing_stop"
    if "opposite" in r:
        return "opposite_signal"
    if r in {"eod_flat", "circuit_breaker", "kill_switch", "regime_change", "manual"}:
        return r
    return r


def _spot_path(trade: dict) -> str:
    algo = trade.get("algo", "")
    asset = trade.get("asset", "")
    m = re.match(r"^(.+?)\s*\((\w+)\)$", algo)
    if m:
        return f"{m.group(1).strip()}@{m.group(2)}"
    return f"{algo}@{asset}"


def _options_path(trade: dict) -> str:
    return trade.get("strategy", "unknown")


# ── Core analysis ────────────────────────────────────────────────────


def analyze_exit_forensics(bot: str, completed_trades: List[dict]) -> ExitForensicsResult:
    result = ExitForensicsResult(bot=bot)
    result.total_exits = len(completed_trades)

    hold_totals: Dict[str, float] = {}
    hold_counts: Dict[str, int] = {}

    for trade in completed_trades:
        reason = _normalize_reason(trade.get("exit_reason", ""))
        pnl = float(trade.get("net_pnl", trade.get("realized_pnl_usd", 0.0)) or 0.0)
        hold = float(trade.get("hold_minutes", trade.get("hold_days", 0.0)) or 0.0)

        result.exit_reason_breakdown[reason] = result.exit_reason_breakdown.get(reason, 0) + 1
        hold_totals[reason] = hold_totals.get(reason, 0.0) + hold
        hold_counts[reason] = hold_counts.get(reason, 0) + 1

        if reason not in result.exit_reason_stats:
            result.exit_reason_stats[reason] = ExitReasonStats(reason=reason)
        ers = result.exit_reason_stats[reason]
        ers.trades += 1
        ers.net_pnl += pnl

        if reason in INFORMATIVE_EXITS:
            result.informative_exits += 1
        elif reason in FORCED_EXITS:
            result.forced_exits += 1
            if reason == "time_limit":
                result.time_limit_exits += 1

        path = _spot_path(trade) if bot == "spot" else _options_path(trade)
        if path not in result.path_exit_stats:
            result.path_exit_stats[path] = PathExitStats(path=path)
        pes = result.path_exit_stats[path]
        pes.trades += 1
        pes.exit_reason_breakdown[reason] = pes.exit_reason_breakdown.get(reason, 0) + 1
        pes.net_pnl_by_reason[reason] = pes.net_pnl_by_reason.get(reason, 0.0) + pnl
        pes.avg_hold_by_reason.setdefault(reason, 0.0)
        pes.avg_hold_by_reason[reason] += hold

    for reason, ers in result.exit_reason_stats.items():
        if hold_counts.get(reason, 0):
            ers.avg_hold = hold_totals[reason] / hold_counts[reason]

    for pes in result.path_exit_stats.values():
        for reason, total_hold in list(pes.avg_hold_by_reason.items()):
            count = pes.exit_reason_breakdown.get(reason, 0)
            if count > 0:
                pes.avg_hold_by_reason[reason] = total_hold / count

    if result.total_exits > 0:
        result.forced_cleanup_ratio = result.forced_exits / result.total_exits
        result.time_limit_ratio = result.time_limit_exits / result.total_exits

    result.recommendation, result.assessment = _recommend(result)
    return result


def _recommend(result: ExitForensicsResult) -> tuple[str, str]:
    if result.total_exits < 5:
        return (
            "exits need work",
            f"Only {result.total_exits} exits available — too early for a reliable exit-quality verdict.",
        )
    if result.time_limit_ratio >= 0.5:
        return (
            "time-limit too dominant",
            f"Time-limit exits are {result.time_limit_ratio:.0%} of exits; investigate hold-time settings and target/stop distances.",
        )
    if result.forced_cleanup_ratio >= 0.6:
        return (
            "exits need work",
            f"Forced-cleanup exits are {result.forced_cleanup_ratio:.0%} of exits; exits look structural rather than signal-driven.",
        )
    if result.informative_exits >= result.forced_exits:
        return (
            "exits healthy",
            "Exit mix is mostly informative and appears to capture signal rather than just forcing cleanup.",
        )
    return (
        "investigate hold-time settings",
        "Forced exits are elevated; monitor whether more trades improve the exit mix.",
    )


# ── Convenience readers ──────────────────────────────────────────────


def analyze_spot_state(state: dict) -> ExitForensicsResult:
    return analyze_exit_forensics("spot", state.get("completed_trades", []))


def analyze_options_state(state: dict) -> ExitForensicsResult:
    return analyze_exit_forensics("options", state.get("completed_trades", []))


def run_from_state_file(bot: str, path: str) -> ExitForensicsResult:
    if not os.path.exists(path):
        return ExitForensicsResult(
            bot=bot,
            recommendation="exits need work",
            assessment="State file missing.",
        )
    with open(path) as f:
        state = json.load(f)
    return analyze_spot_state(state) if bot == "spot" else analyze_options_state(state)
