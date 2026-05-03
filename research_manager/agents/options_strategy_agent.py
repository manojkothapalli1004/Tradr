"""
Options Strategy Agent V1 — read-only sub-agent for the Research Manager.

Consumes OptionsAnalysisResult from the analysis layer and produces
per-strategy recommendations (keep / watch / cut) with supporting metrics.

Pure functions + typed dataclasses. No I/O. No side effects.
The only I/O is in run_from_state_file(), kept at the edge for CLI use.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..options_analysis import (
    OptionsAnalysisResult,
    StrategyMetrics,
    SymbolMetrics,
    analyze_options_state,
)


# ── Per-strategy recommendation ──────────────────────────────────────

@dataclass
class StrategyRecommendation:
    """Agent's per-strategy verdict."""
    strategy: str
    action: str             # "keep" | "watch" | "cut" | "no data"
    reason: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    win_rate: float = 0.0
    avg_hold_days: float = 0.0
    fees: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Per-symbol breakdown (for aggregator) ────────────────────────────

@dataclass
class SymbolBreakdown:
    """Per-symbol P&L summary for the aggregator."""
    symbol: str
    trades: int = 0
    realized_pnl: float = 0.0
    win_rate: float = 0.0
    avg_hold_days: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Agent result ─────────────────────────────────────────────────────

@dataclass
class OptionsAgentResult:
    """Full output of the options strategy agent."""
    # summary
    total_trades: int = 0
    active_trades: int = 0
    realized_pnl: float = 0.0
    win_rate: float = 0.0
    avg_hold_days: float = 0.0
    confidence: str = "LOW"

    # rankings
    strongest_strategy: Optional[str] = None
    weakest_strategy: Optional[str] = None

    # per-strategy recommendations
    strategy_recommendations: List[StrategyRecommendation] = field(default_factory=list)

    # per-symbol breakdown
    symbol_breakdowns: List[SymbolBreakdown] = field(default_factory=list)

    # metadata
    analysis_time: str = ""
    runtime_hours: float = 0.0
    kill_switch_active: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ── Recommendation logic ─────────────────────────────────────────────

MIN_TRADES_EVAL = 3     # minimum trades before judging a strategy
MIN_TRADES_CUT = 5      # minimum trades before recommending cut


def _recommend_strategy(sm: StrategyMetrics, total_trades: int) -> StrategyRecommendation:
    """Produce a keep/watch/cut recommendation for a single strategy."""
    rec = StrategyRecommendation(
        strategy=sm.strategy,
        trades=sm.trades,
        wins=sm.wins,
        losses=sm.losses,
        realized_pnl=sm.realized_pnl,
        win_rate=sm.win_rate,
        avg_hold_days=sm.avg_hold_days,
        fees=sm.fees,
        action="no data",
        reason="",
    )

    if sm.trades < MIN_TRADES_EVAL:
        rec.action = "no data"
        rec.reason = f"only {sm.trades} trade(s) — need {MIN_TRADES_EVAL}+ to evaluate"
        return rec

    # positive P&L + decent win rate → keep
    if sm.realized_pnl > 0 and sm.win_rate >= 40:
        rec.action = "keep"
        rec.reason = f"${sm.realized_pnl:+.2f} P&L, {sm.win_rate:.0f}% win rate"
        return rec

    # positive P&L but low win rate → watch (fragile edge)
    if sm.realized_pnl > 0 and sm.win_rate < 40:
        rec.action = "watch"
        rec.reason = f"positive P&L (${sm.realized_pnl:+.2f}) but low win rate ({sm.win_rate:.0f}%) — fragile"
        return rec

    # negative P&L but high win rate → watch (sizing/exit issue)
    if sm.realized_pnl <= 0 and sm.win_rate >= 50:
        rec.action = "watch"
        rec.reason = f"negative P&L (${sm.realized_pnl:+.2f}) despite {sm.win_rate:.0f}% wins — check exit logic"
        return rec

    # negative P&L, low win rate, enough trades → cut
    if sm.trades >= MIN_TRADES_CUT:
        rec.action = "cut"
        rec.reason = f"${sm.realized_pnl:+.2f} P&L, {sm.win_rate:.0f}% win rate over {sm.trades} trades"
        return rec

    # negative but not enough trades to confidently cut
    rec.action = "watch"
    rec.reason = f"negative early (${sm.realized_pnl:+.2f}) — {MIN_TRADES_CUT - sm.trades} more trade(s) before cutting"
    return rec


# ── Core agent function ──────────────────────────────────────────────

def evaluate(analysis: OptionsAnalysisResult) -> OptionsAgentResult:
    """
    Pure function: OptionsAnalysisResult → OptionsAgentResult.

    This is the agent's main entry point. No I/O.
    """
    result = OptionsAgentResult(
        total_trades=analysis.total_trades,
        active_trades=analysis.active_trades_count,
        realized_pnl=analysis.realized_pnl,
        win_rate=analysis.win_rate,
        avg_hold_days=analysis.avg_hold_days,
        confidence=analysis.sample_confidence,
        strongest_strategy=analysis.strongest_strategy,
        weakest_strategy=analysis.weakest_strategy,
        analysis_time=analysis.analysis_time,
        runtime_hours=analysis.runtime_hours,
        kill_switch_active=analysis.kill_switch_active,
    )

    # per-strategy recommendations
    for sm in sorted(analysis.strategy_metrics.values(),
                     key=lambda s: s.realized_pnl, reverse=True):
        rec = _recommend_strategy(sm, analysis.total_trades)
        result.strategy_recommendations.append(rec)

    # include strategies from slots that have zero completed trades
    # (they exist in algos but not in strategy_metrics)
    seen = {r.strategy for r in result.strategy_recommendations}
    slot_strategies: Dict[str, int] = {}
    for slot in analysis.slot_metrics.values():
        if slot.strategy not in seen:
            slot_strategies[slot.strategy] = slot_strategies.get(slot.strategy, 0) + slot.trades
    for strat, trades in sorted(slot_strategies.items()):
        result.strategy_recommendations.append(StrategyRecommendation(
            strategy=strat,
            action="no data",
            reason="no completed trades yet",
            trades=trades,
        ))

    # per-symbol breakdown
    for ym in sorted(analysis.symbol_metrics.values(),
                     key=lambda s: s.realized_pnl, reverse=True):
        result.symbol_breakdowns.append(SymbolBreakdown(
            symbol=ym.symbol,
            trades=ym.trades,
            realized_pnl=ym.realized_pnl,
            win_rate=ym.win_rate,
            avg_hold_days=ym.avg_hold_days,
        ))

    return result


# ── Convenience: state dict → agent result ───────────────────────────

def evaluate_from_state(state: dict) -> OptionsAgentResult:
    """Convenience: raw state dict → agent result."""
    analysis = analyze_options_state(state)
    return evaluate(analysis)


# ── I/O edge: file → agent result (for CLI) ─────────────────────────

def run_from_state_file(path: str) -> OptionsAgentResult:
    """Read state file and produce agent result. Only I/O entry point."""
    if not os.path.exists(path):
        return OptionsAgentResult(
            analysis_time=datetime.now(timezone.utc).isoformat(),
        )
    with open(path) as f:
        state = json.load(f)
    return evaluate_from_state(state)


# ── Text formatter ───────────────────────────────────────────────────

def format_agent_report(r: OptionsAgentResult) -> str:
    """Plain-text agent report."""
    w = 64
    lines = [
        "=" * w,
        "  OPTIONS STRATEGY AGENT — V1 REPORT",
        "=" * w,
        "",
        f"  Completed Trades:  {r.total_trades}",
        f"  Active Trades:     {r.active_trades}",
        f"  Realized P&L:      ${r.realized_pnl:.2f}",
        f"  Win Rate:          {r.win_rate:.1f}%",
        f"  Avg Hold:          {r.avg_hold_days:.3f}d",
        f"  Confidence:        {r.confidence}",
        f"  Kill Switch:       {'ACTIVE' if r.kill_switch_active else 'off'}",
        "",
    ]

    if r.strongest_strategy or r.weakest_strategy:
        lines.append("-" * w)
        lines.append("  RANKINGS")
        lines.append("-" * w)
        lines.append(f"  Strongest: {r.strongest_strategy or 'N/A'}")
        lines.append(f"  Weakest:   {r.weakest_strategy or 'N/A'}")
        lines.append("")

    lines.append("-" * w)
    lines.append("  PER-STRATEGY RECOMMENDATIONS")
    lines.append("-" * w)
    if r.strategy_recommendations:
        for rec in r.strategy_recommendations:
            tag = rec.action.upper().rjust(7)
            lines.append(f"  [{tag}]  {rec.strategy}")
            lines.append(f"           {rec.trades} trades | ${rec.realized_pnl:+.2f} | {rec.win_rate:.0f}% win")
            lines.append(f"           {rec.reason}")
            lines.append("")
    else:
        lines.append("  No strategies to evaluate yet.")
        lines.append("")

    if r.symbol_breakdowns:
        lines.append("-" * w)
        lines.append("  P&L BY SYMBOL")
        lines.append("-" * w)
        for sb in r.symbol_breakdowns:
            lines.append(f"  {sb.symbol:<6s}  {sb.trades} trades  ${sb.realized_pnl:+.2f}  {sb.win_rate:.0f}% win  {sb.avg_hold_days:.3f}d hold")
        lines.append("")

    if r.confidence == "LOW":
        lines.append("* WARNING: Sample size too small for reliable recommendations.")
        lines.append("")

    lines.append(f"  Generated: {r.analysis_time}")
    lines.append("=" * w)
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    import sys

    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    state_path = os.path.join(base_dir, "options_bot", "state.json")

    result = run_from_state_file(state_path)

    if "--json" in sys.argv:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_agent_report(result))


if __name__ == "__main__":
    main()
