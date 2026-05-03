"""
research/registry.py — Candidate registry for paper-trading research.

Edit CANDIDATES to add, enable, or disable research strategies.
Each entry is the single source of truth for metadata: what a strategy
claims to do, which assets to run it on, and its current research status.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List


@dataclass
class Candidate:
    name: str                  # must match strategy name in strategies.py
    category: str              # e.g. "breakout", "trend", "mean_reversion"
    assets: List[str]          # which assets to paper-trade
    enabled: bool              # False = skip sanity-check and execution
    hypothesis: str            # plain-English claim the strategy makes
    notes: str = ""            # reviewer notes / known issues
    status: str = "pending"    # pending | linting | running | scored | promoted | cut


# ── Active research candidates ───────────────────────────────────────────────
CANDIDATES: List[Candidate] = [
    Candidate(
        name       = "orb",
        category   = "breakout",
        assets     = ["BTC", "ETH"],
        enabled    = True,
        hypothesis = (
            "Rolling 4-bar (1h) high/low breakout produces positive expectancy on 15m crypto. "
            "Entry on first close above prior range high; reverse for shorts. "
            "Note: rolling window, not session-anchored — tests breakout momentum, not true ORB."
        ),
        notes = "No volume filter — will fire on thin breakouts. Acceptable for baseline.",
    ),
    Candidate(
        name       = "vwap_trend",
        category   = "trend",
        assets     = ["BTC", "ETH"],
        enabled    = True,
        hypothesis = (
            "Rolling 20-bar VWMA reclaim signals continuation; RSI > 50 filter removes "
            "counter-trend entries. Symmetric short logic on rejection."
        ),
        notes = "Rolling VWMA, not session-anchored VWAP. No volume confirmation on cross.",
    ),
    Candidate(
        name       = "break_retest",
        category   = "breakout",
        assets     = ["BTC", "ETH"],
        enabled    = True,
        hypothesis = (
            "Two-phase state machine: detect close above 20-bar resistance (breakout), "
            "wait for pullback within 1.5% of that level (retest), then signal entry "
            "on reclaim above the level with EMA-50 trend filter."
        ),
        notes = "Fixed 2026-04-13: now uses proper two-phase logic with ffill state across bars.",
    ),
]


def get_enabled() -> List[Candidate]:
    return [c for c in CANDIDATES if c.enabled]


def get_by_name(name: str) -> Candidate | None:
    return next((c for c in CANDIDATES if c.name == name), None)
