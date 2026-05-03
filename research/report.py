"""
research/report.py — Score, rank, and verdict research candidates from state file.

Reads paper_trading_research_state.json (isolated research state — never touches
the main runtime state) and produces:
  - per-strategy stats: trades, WR, net P&L, avg hold, exit mix
  - automated verdict: PROMOTE / KEEP TESTING / REVISE / CUT
  - ranked table

Verdict rules (conservative — this is paper research):
  PROMOTE      WR >= 45%  AND net_pnl > 0  AND trades >= 30  AND no red flags
  KEEP TESTING trades > 0  AND (WR >= 35%  OR net_pnl >= 0)  AND trades < 30
  REVISE       trades > 0  AND (WR < 30%   OR avg_pnl < -0.10)
  CUT          WR < 20%   AND net_pnl < -3.0   AND trades >= 10
               OR zero signals after >= 20 research cycles (detected via lint)
"""

from __future__ import annotations
import json
import os
import sys
from dataclasses import dataclass, field
from typing import List, Dict, Optional

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH_STATE_FILE = os.path.join(_DIR, "paper_trading_research_state.json")


@dataclass
class StrategyStats:
    name: str
    asset: str
    trades: int
    wins: int
    losses: int
    net_pnl: float
    total_fees: float
    avg_hold_min: float
    exit_mix: Dict[str, int]
    win_rate: float
    avg_pnl: float
    verdict: str = ""
    verdict_reason: str = ""


def _load_research_state(state_file: str = RESEARCH_STATE_FILE) -> dict:
    if not os.path.exists(state_file):
        return {}
    with open(state_file) as f:
        return json.load(f)


def _categorize_exit(reason: str) -> str:
    if "TAKE PROFIT" in reason:
        return "take_profit"
    if "STOP LOSS"   in reason:
        return "stop_loss"
    if "TRAILING"    in reason:
        return "trailing_stop"
    if "TIME LIMIT"  in reason:
        return "time_limit"
    if "signal"      in reason.lower():
        return "opposite_signal"
    return "other"


def _compute_verdict(s: StrategyStats, lint_verdict: str = "") -> tuple[str, str]:
    # If lint already flagged it as FAIL, CUT immediately
    if lint_verdict == "FAIL":
        return "CUT", "Lint FAIL — conceptual or structural flaw"

    t = s.trades
    wr = s.win_rate
    pnl = s.net_pnl
    avg = s.avg_pnl

    if t == 0:
        return "REVISE", "No trades yet — strategy may not be firing or is too restrictive"

    if t >= 10 and wr < 20 and pnl < -3.0:
        return "CUT", f"WR {wr:.0f}% < 20% and net P&L ${pnl:.2f} < -$3 over {t} trades"

    if t >= 30 and wr >= 45 and pnl > 0:
        return "PROMOTE", f"WR {wr:.0f}% ≥ 45%, net P&L ${pnl:.2f} > 0 over {t} trades"

    if wr < 30 or avg < -0.10:
        return "REVISE", (
            f"WR {wr:.0f}% < 30%" if wr < 30
            else f"Avg P&L ${avg:.3f} < -$0.10/trade"
        )

    if t < 30:
        return "KEEP TESTING", f"Only {t} trades — need ≥ 30 for reliable signal"

    return "KEEP TESTING", f"WR {wr:.0f}% / net ${pnl:.2f} — not yet promotable"


def score_from_state(
    state_file: str = RESEARCH_STATE_FILE,
    lint_verdicts: Dict[str, str] = None,
) -> List[StrategyStats]:
    state = _load_research_state(state_file)
    if not state:
        return []

    algos = state.get("algos", {})
    completed = state.get("completed_trades", [])
    lint_verdicts = lint_verdicts or {}
    results = []

    for key, algo in algos.items():
        name  = algo["name"]
        asset = algo["asset"]

        # Trades for this algo
        algo_trades = [t for t in completed if t.get("algo") == name and t.get("asset") == asset]
        trade_count = len(algo_trades)
        wins   = sum(1 for t in algo_trades if t.get("net_pnl", 0) > 0)
        losses = trade_count - wins
        net_pnl = round(sum(t.get("net_pnl", 0) for t in algo_trades), 4)
        fees    = round(sum(t.get("total_fees", 0) for t in algo_trades), 4)
        avg_hold = (
            sum(t.get("hold_minutes", 0) for t in algo_trades) / trade_count
            if trade_count else 0.0
        )
        wr  = wins / trade_count * 100 if trade_count else 0.0
        avg = net_pnl / trade_count if trade_count else 0.0

        exit_mix: Dict[str, int] = {}
        for t in algo_trades:
            cat = _categorize_exit(t.get("exit_reason", ""))
            exit_mix[cat] = exit_mix.get(cat, 0) + 1

        stats = StrategyStats(
            name=name, asset=asset,
            trades=trade_count, wins=wins, losses=losses,
            net_pnl=net_pnl, total_fees=fees,
            avg_hold_min=round(avg_hold, 1),
            exit_mix=exit_mix, win_rate=round(wr, 1), avg_pnl=round(avg, 4),
        )
        verdict, reason = _compute_verdict(stats, lint_verdicts.get(name, ""))
        stats.verdict = verdict
        stats.verdict_reason = reason
        results.append(stats)

    # Rank: PROMOTE first, then by net_pnl desc
    order = {"PROMOTE": 0, "KEEP TESTING": 1, "REVISE": 2, "CUT": 3}
    results.sort(key=lambda s: (order.get(s.verdict, 9), -s.net_pnl))
    return results


def print_report(
    stats_list: List[StrategyStats],
    show_exit_mix: bool = True,
):
    WIDTH = 76

    def fmt_exit(mix: Dict[str, int]) -> str:
        parts = []
        for k in ("take_profit", "stop_loss", "trailing_stop", "time_limit", "opposite_signal", "other"):
            if k in mix:
                short = {"take_profit": "TP", "stop_loss": "SL", "trailing_stop": "TS",
                         "time_limit": "TL", "opposite_signal": "OS", "other": "OT"}.get(k, k)
                parts.append(f"{short}:{mix[k]}")
        return " ".join(parts) if parts else "—"

    print("=" * WIDTH)
    print("RESEARCH PERFORMANCE REPORT")
    print("=" * WIDTH)

    if not stats_list:
        print("  No research state found. Run the research batch first.")
        print("=" * WIDTH)
        return

    for s in stats_list:
        icon = {"PROMOTE": "▲", "KEEP TESTING": "→", "REVISE": "↺", "CUT": "✗"}.get(s.verdict, "?")
        pnl_str = f"+${s.net_pnl:.4f}" if s.net_pnl >= 0 else f"-${abs(s.net_pnl):.4f}"
        print(f"\n[{icon}] {s.name}@{s.asset}  —  {s.verdict}")
        print(f"    Trades: {s.trades}  WR: {s.win_rate:.1f}%  "
              f"Net P&L: {pnl_str}  Avg/trade: ${s.avg_pnl:.4f}")
        print(f"    W/L: {s.wins}/{s.losses}  Avg hold: {s.avg_hold_min:.0f}m  "
              f"Fees: ${s.total_fees:.4f}")
        if show_exit_mix and s.exit_mix:
            print(f"    Exit mix: {fmt_exit(s.exit_mix)}")
        print(f"    Reason: {s.verdict_reason}")

    print("\n" + "=" * WIDTH)
    print("RANKED SUMMARY")
    print(f"  {'Strategy':<22} {'T':>4} {'WR':>6} {'Net P&L':>10} {'Verdict'}")
    print("  " + "-" * 60)
    for s in stats_list:
        pnl_str = f"+${s.net_pnl:.2f}" if s.net_pnl >= 0 else f"-${abs(s.net_pnl):.2f}"
        print(f"  {s.name+'@'+s.asset:<22} {s.trades:>4} {s.win_rate:>5.1f}% {pnl_str:>10}  {s.verdict}")
    print("=" * WIDTH)
