"""
Exit Logic Agent V1 — evaluates exit reason distributions for spot and options.

Read-only. No I/O. No side effects.

Detects:
  - time-limit dominance (too many trades expiring instead of hitting targets/stops)
  - forced-cleanup ratio (exits that are structural, not signal-driven)
  - whether exits appear informative or mostly acting as safety valves

Returns a structured ExitLogicVerdict per bot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


# ── Exit classification ──────────────────────────────────────────────

# "Informative" exits: the strategy or risk logic chose to close.
INFORMATIVE_EXITS = frozenset({
    "trailing_stop", "stop_loss", "take_profit", "profit_target",
    "opposite_signal", "regime_change",
})

# "Forced" exits: structural time/safety limits, not signal-driven.
FORCED_EXITS = frozenset({
    "time_limit", "eod_flat", "circuit_breaker", "kill_switch",
})

# Threshold: if forced exits exceed this fraction, exit logic needs review.
FORCED_DOMINANCE_THRESHOLD = 0.60

# Threshold: if time_limit alone exceeds this fraction, it's dominant.
TIME_LIMIT_DOMINANCE_THRESHOLD = 0.50


# ── Verdict ──────────────────────────────────────────────────────────

@dataclass
class ExitLogicVerdict:
    """Structured output of the exit logic agent for one bot."""
    bot: str                         # "spot" or "options"
    total_exits: int = 0
    informative_count: int = 0
    forced_count: int = 0
    other_count: int = 0

    informative_pct: float = 0.0
    forced_pct: float = 0.0
    time_limit_pct: float = 0.0

    time_limit_dominant: bool = False
    forced_dominant: bool = False

    # healthy / watch / needs_work
    rating: str = "healthy"
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "bot": self.bot,
            "total_exits": self.total_exits,
            "informative_count": self.informative_count,
            "forced_count": self.forced_count,
            "other_count": self.other_count,
            "informative_pct": round(self.informative_pct, 1),
            "forced_pct": round(self.forced_pct, 1),
            "time_limit_pct": round(self.time_limit_pct, 1),
            "time_limit_dominant": self.time_limit_dominant,
            "forced_dominant": self.forced_dominant,
            "rating": self.rating,
            "reason": self.reason,
        }


# ── Core evaluation ──────────────────────────────────────────────────

def evaluate_exit_reasons(
    exit_breakdown: Dict[str, int],
    bot: str,
    min_exits: int = 5,
) -> ExitLogicVerdict:
    """
    Evaluate an exit reason breakdown dict.

    Parameters
    ----------
    exit_breakdown : dict mapping exit reason string -> count
    bot : "spot" or "options"
    min_exits : minimum exits required for meaningful analysis

    Returns
    -------
    ExitLogicVerdict with rating and diagnostics.
    """
    v = ExitLogicVerdict(bot=bot)

    v.total_exits = sum(exit_breakdown.values())
    if v.total_exits < min_exits:
        v.rating = "watch"
        v.reason = f"Only {v.total_exits} exits — need {min_exits}+ for meaningful analysis."
        return v

    # classify each reason
    time_limit_count = 0
    for reason, count in exit_breakdown.items():
        key = reason.lower().strip()
        if key in INFORMATIVE_EXITS:
            v.informative_count += count
        elif key in FORCED_EXITS:
            v.forced_count += count
            if key == "time_limit":
                time_limit_count += count
        else:
            v.other_count += count

    v.informative_pct = (v.informative_count / v.total_exits) * 100
    v.forced_pct = (v.forced_count / v.total_exits) * 100
    v.time_limit_pct = (time_limit_count / v.total_exits) * 100

    v.time_limit_dominant = (time_limit_count / v.total_exits) >= TIME_LIMIT_DOMINANCE_THRESHOLD
    v.forced_dominant = (v.forced_count / v.total_exits) >= FORCED_DOMINANCE_THRESHOLD

    # determine rating
    if v.time_limit_dominant:
        v.rating = "needs_work"
        v.reason = (
            f"Time-limit exits are {v.time_limit_pct:.0f}% of all exits. "
            "Strategies are not reaching targets or stops — exit logic is acting "
            "as forced cleanup, not informed decision-making. "
            "Review target/stop distances or hold-time limits."
        )
    elif v.forced_dominant:
        v.rating = "needs_work"
        v.reason = (
            f"Forced exits (time limit + EOD + circuit breaker) are {v.forced_pct:.0f}% "
            "of all exits. Most trades are being cleaned up by safety limits, "
            "not by strategy signals. Review whether targets are realistic."
        )
    elif v.forced_pct > 40:
        v.rating = "watch"
        v.reason = (
            f"Forced exits at {v.forced_pct:.0f}% — not dominant but elevated. "
            "Monitor whether this improves as more trades complete."
        )
    else:
        v.rating = "healthy"
        v.reason = (
            f"Informative exits are {v.informative_pct:.0f}% of all exits. "
            "Exit logic is functioning as intended — trades are reaching "
            "targets, stops, or signal-driven closes."
        )

    return v
