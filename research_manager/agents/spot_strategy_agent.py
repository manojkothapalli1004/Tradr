"""
Spot Strategy Agent V1 — read-only sub-agent that evaluates each spot combo
and produces per-combo keep/watch/cut recommendations.

Consumes AnalysisResult from spot_analysis.py (no I/O of its own).
Returns a typed AgentResult for the manager aggregator.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..spot_analysis import AnalysisResult, ComboMetrics


# ── Per-combo verdict ───────────────────────────────────────────────


@dataclass
class ComboVerdict:
    """Agent's assessment of a single strategy@asset combo."""
    combo: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    avg_hold_minutes: float = 0.0
    fees: float = 0.0
    recommendation: str = "watch"  # "keep" | "watch" | "cut"
    reason: str = ""


# ── Agent result ────────────────────────────────────────────────────


@dataclass
class SpotAgentResult:
    """Structured output of the spot strategy agent."""
    combo_verdicts: Dict[str, ComboVerdict] = field(default_factory=dict)
    strongest_combo: Optional[str] = None
    weakest_combo: Optional[str] = None
    confidence: str = "LOW"
    total_trades: int = 0
    agent_summary: str = ""
    analysis_time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Agent logic ─────────────────────────────────────────────────────

# Minimum trades before a combo can be recommended for cut
_MIN_TRADES_FOR_CUT = 5
# Minimum trades before a combo can be recommended for keep
_MIN_TRADES_FOR_KEEP = 3


def evaluate(analysis: AnalysisResult) -> SpotAgentResult:
    """
    Evaluate each spot combo and produce per-combo recommendations.

    Pure function: AnalysisResult in, SpotAgentResult out.
    """
    result = SpotAgentResult()
    result.analysis_time = datetime.now(timezone.utc).isoformat()
    result.total_trades = analysis.total_trades
    result.confidence = analysis.sample_confidence
    result.strongest_combo = analysis.strongest_combo
    result.weakest_combo = analysis.weakest_combo

    for key, cm in analysis.combo_metrics.items():
        verdict = _evaluate_combo(cm, analysis)
        result.combo_verdicts[key] = verdict

    result.agent_summary = _build_summary(result)
    return result


def _evaluate_combo(cm: ComboMetrics, analysis: AnalysisResult) -> ComboVerdict:
    """Classify a single combo as keep / watch / cut."""
    v = ComboVerdict(
        combo=cm.combo,
        trades=cm.trades,
        wins=cm.wins,
        losses=cm.losses,
        net_pnl=cm.net_pnl,
        win_rate=cm.win_rate,
        avg_hold_minutes=cm.avg_hold_minutes,
        fees=cm.fees,
    )

    # not enough data — always watch
    if cm.trades < _MIN_TRADES_FOR_KEEP:
        v.recommendation = "watch"
        v.reason = f"only {cm.trades} trade(s) — need {_MIN_TRADES_FOR_KEEP}+ to assess"
        return v

    # enough to potentially keep or cut
    if cm.net_pnl > 0 and cm.win_rate >= 40:
        v.recommendation = "keep"
        v.reason = f"positive P&L (${cm.net_pnl:.2f}), {cm.win_rate:.0f}% win rate"
        return v

    if cm.net_pnl > 0 and cm.win_rate < 40:
        v.recommendation = "watch"
        v.reason = f"positive P&L (${cm.net_pnl:.2f}) but low win rate ({cm.win_rate:.0f}%) — fragile"
        return v

    # negative P&L cases
    if cm.trades < _MIN_TRADES_FOR_CUT:
        v.recommendation = "watch"
        v.reason = f"negative P&L (${cm.net_pnl:.2f}) but only {cm.trades} trades — too early to cut"
        return v

    # enough trades and negative
    if cm.win_rate < 35:
        v.recommendation = "cut"
        v.reason = f"negative P&L (${cm.net_pnl:.2f}), {cm.win_rate:.0f}% win rate over {cm.trades} trades"
        return v

    if cm.losses > cm.wins and cm.net_pnl < -cm.fees:
        v.recommendation = "cut"
        v.reason = f"losses exceed wins ({cm.losses}L vs {cm.wins}W), net loss (${cm.net_pnl:.2f}) exceeds fees"
        return v

    v.recommendation = "watch"
    v.reason = f"negative P&L (${cm.net_pnl:.2f}) but {cm.win_rate:.0f}% win rate — needs more data"
    return v


def _build_summary(result: SpotAgentResult) -> str:
    """One-line agent summary for the manager aggregator."""
    if result.total_trades == 0:
        return "No trades yet — all combos in watch state."

    verdicts = result.combo_verdicts
    keeps = [k for k, v in verdicts.items() if v.recommendation == "keep"]
    cuts = [k for k, v in verdicts.items() if v.recommendation == "cut"]
    watches = [k for k, v in verdicts.items() if v.recommendation == "watch"]

    parts = []
    if keeps:
        parts.append(f"keep {len(keeps)}")
    if watches:
        parts.append(f"watch {len(watches)}")
    if cuts:
        parts.append(f"cut {len(cuts)}")

    tag = " | ".join(parts)

    if result.confidence == "LOW":
        return f"{tag} — LOW confidence, all verdicts preliminary"
    if cuts:
        cut_names = ", ".join(cuts)
        return f"{tag} — recommend dropping: {cut_names}"
    if keeps and not watches:
        return f"{tag} — all assessed combos profitable"
    return f"{tag}"
