"""
Manager Aggregator V1 — combines sub-agent outputs into a single recommendation.

Read-only. No I/O. No side effects. No CLI (wired by manager.py).

Takes typed inputs from:
  - spot_analysis.AnalysisResult
  - options_analysis.OptionsAnalysisResult
  - exit_logic_agent.ExitLogicVerdict

Produces a ManagerRecommendation with:
  - strongest / weakest current path
  - worth_continuing
  - narrow_further (yes / no / not_yet)
  - next_action
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..spot_analysis import AnalysisResult
from ..options_analysis import OptionsAnalysisResult
from .exit_logic_agent import ExitLogicVerdict


# ── Output types ─────────────────────────────────────────────────────

@dataclass
class PathScore:
    """One scored combo or strategy across both bots."""
    label: str          # e.g. "spot:macd@BTC" or "options:ema_trend_pullback"
    bot: str            # "spot" or "options"
    trades: int = 0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    exit_health: str = ""   # from exit logic agent: healthy/watch/needs_work


@dataclass
class ManagerRecommendation:
    """Combined recommendation from all sub-agents."""
    strongest_path: Optional[str] = None
    weakest_path: Optional[str] = None
    worth_continuing: str = ""      # yes / yes_with_caveats / no / not_yet
    narrow_further: str = ""        # yes / no / not_yet
    next_action: str = ""
    confidence: str = "LOW"         # LOW / MEDIUM / HIGH
    risk_alerts: List[str] = field(default_factory=list)
    path_scores: List[PathScore] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "strongest_path": self.strongest_path,
            "weakest_path": self.weakest_path,
            "worth_continuing": self.worth_continuing,
            "narrow_further": self.narrow_further,
            "next_action": self.next_action,
            "confidence": self.confidence,
            "risk_alerts": self.risk_alerts,
            "path_scores": [
                {
                    "label": p.label,
                    "bot": p.bot,
                    "trades": p.trades,
                    "net_pnl": round(p.net_pnl, 2),
                    "win_rate": round(p.win_rate, 1),
                    "exit_health": p.exit_health,
                }
                for p in self.path_scores
            ],
        }


# ── Aggregation ──────────────────────────────────────────────────────

MIN_TRADES_CONFIDENCE = 30
MIN_TRADES_NARROW = 10
MIN_TRADES_EVALUATE = 5


def aggregate(
    spot: Optional[AnalysisResult] = None,
    options: Optional[OptionsAnalysisResult] = None,
    spot_exit: Optional[ExitLogicVerdict] = None,
    options_exit: Optional[ExitLogicVerdict] = None,
) -> ManagerRecommendation:
    """
    Combine analysis results and exit verdicts into a single recommendation.

    All parameters are optional — the aggregator works with whatever data
    is available (spot-only, options-only, or both).
    """
    rec = ManagerRecommendation()

    # ── Build path scores ────────────────────────────────────────────
    paths: list[PathScore] = []
    combined_trades = 0
    combined_pnl = 0.0

    exit_health_spot = spot_exit.rating if spot_exit else ""
    exit_health_options = options_exit.rating if options_exit else ""

    if spot and spot.combo_metrics:
        for cm in spot.combo_metrics.values():
            paths.append(PathScore(
                label=f"spot:{cm.combo}",
                bot="spot",
                trades=cm.trades,
                net_pnl=cm.net_pnl,
                win_rate=cm.win_rate,
                exit_health=exit_health_spot,
            ))
        combined_trades += spot.total_trades
        combined_pnl += spot.net_pnl

    if options and options.strategy_metrics:
        for sm in options.strategy_metrics.values():
            paths.append(PathScore(
                label=f"options:{sm.strategy}",
                bot="options",
                trades=sm.trades,
                net_pnl=sm.realized_pnl,
                win_rate=sm.win_rate,
                exit_health=exit_health_options,
            ))
        combined_trades += options.total_trades
        combined_pnl += options.realized_pnl

    rec.path_scores = sorted(paths, key=lambda p: p.net_pnl, reverse=True)

    # ── Strongest / weakest ──────────────────────────────────────────
    if rec.path_scores:
        rec.strongest_path = rec.path_scores[0].label
        rec.weakest_path = rec.path_scores[-1].label

    # ── Confidence ───────────────────────────────────────────────────
    if combined_trades >= MIN_TRADES_CONFIDENCE:
        rec.confidence = "HIGH"
    elif combined_trades >= MIN_TRADES_NARROW:
        rec.confidence = "MEDIUM"
    else:
        rec.confidence = "LOW"

    # ── Risk alerts ──────────────────────────────────────────────────
    if spot_exit and spot_exit.rating == "needs_work":
        rec.risk_alerts.append(f"Spot exit logic: {spot_exit.reason}")
    if options_exit and options_exit.rating == "needs_work":
        rec.risk_alerts.append(f"Options exit logic: {options_exit.reason}")
    if options and options.kill_switch_active:
        rec.risk_alerts.append("Options kill switch active.")
    if options and options.drawdown_pct > 10:
        rec.risk_alerts.append(f"Options drawdown at {options.drawdown_pct:.1f}%.")

    # ── Worth continuing ─────────────────────────────────────────────
    rec.worth_continuing = _assess_continuation(
        combined_trades, combined_pnl, rec.risk_alerts,
        spot, options,
    )

    # ── Narrow further ───────────────────────────────────────────────
    rec.narrow_further = _assess_narrowing(combined_trades, rec.path_scores)

    # ── Next action ──────────────────────────────────────────────────
    rec.next_action = _derive_next_action(rec, combined_trades, combined_pnl)

    return rec


# ── Decision helpers ─────────────────────────────────────────────────

def _assess_continuation(
    trades: int,
    pnl: float,
    alerts: list[str],
    spot: Optional[AnalysisResult],
    options: Optional[OptionsAnalysisResult],
) -> str:
    if trades < MIN_TRADES_EVALUATE:
        return "not_yet"

    if options and options.kill_switch_active:
        return "yes_with_caveats"

    has_exit_problems = any("exit logic" in a for a in alerts)
    if pnl > 0 and not has_exit_problems:
        return "yes"
    if pnl > 0 and has_exit_problems:
        return "yes_with_caveats"
    if pnl <= 0 and trades < MIN_TRADES_CONFIDENCE:
        return "yes_with_caveats"
    if pnl <= 0 and trades >= MIN_TRADES_CONFIDENCE:
        return "no"
    return "not_yet"


def _assess_narrowing(trades: int, paths: list[PathScore]) -> str:
    if trades < MIN_TRADES_NARROW:
        return "not_yet"

    tested = [p for p in paths if p.trades >= 3]
    if len(tested) < 2:
        return "not_yet"

    losers = [p for p in tested if p.net_pnl < 0]
    if losers and trades >= MIN_TRADES_CONFIDENCE:
        return "yes"
    if losers:
        return "not_yet"
    return "no"


def _derive_next_action(
    rec: ManagerRecommendation,
    trades: int,
    pnl: float,
) -> str:
    if trades < MIN_TRADES_EVALUATE:
        return "Continue running — need 5+ trades before any assessment."

    if rec.risk_alerts:
        top_alert = rec.risk_alerts[0]
        return f"Address risk: {top_alert}"

    if rec.narrow_further == "yes" and rec.weakest_path:
        return f"Drop {rec.weakest_path} and reallocate to top performers."

    if rec.worth_continuing == "no":
        return "Review all strategies — combined P&L negative at HIGH confidence."

    if rec.confidence == "LOW":
        return "Collect more data — no reliable conclusions until 30+ combined trades."

    if pnl > 0:
        return "Continue current allocation. Re-evaluate after next 20 trades."

    return "Monitor closely — P&L negative but more data needed before dropping paths."
