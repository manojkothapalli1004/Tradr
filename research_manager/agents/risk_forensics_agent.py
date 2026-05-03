"""
Risk / Kill-Switch Forensics Agent V1 — read-only sub-agent for analyzing
risk blocks, kill-switch state, drawdown evidence, and whether the runtime is
being prevented from expressing the strategy.

Pure analysis first, with optional state/log readers at the edge only.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class RiskBlockEvidence:
    source: str
    count: int = 0
    summary: str = ""


@dataclass
class RiskForensicsResult:
    bot: str
    kill_switch_active: bool = False
    drawdown_pct: float = 0.0
    repeated_risk_blocks: int = 0
    repeated_router_selects: int = 0
    blocked_expression: bool = False
    expected_blocks: bool = False
    risk_state: str = "monitor"  # healthy | monitor | investigate kill switch | investigate drawdown/accounting
    assessment: str = ""
    evidence: List[str] = field(default_factory=list)
    block_sources: Dict[str, RiskBlockEvidence] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def analyze_risk_forensics(
    bot: str,
    state: dict,
    log_text: str = "",
) -> RiskForensicsResult:
    result = RiskForensicsResult(bot=bot)

    if bot == "options":
        portfolio = state.get("portfolio", {})
        result.kill_switch_active = bool(portfolio.get("kill_switch_active", False))
        result.drawdown_pct = float(portfolio.get("current_drawdown_pct", 0.0) or 0.0)
    else:
        portfolio = state.get("portfolio_risk", {})
        result.kill_switch_active = bool(portfolio.get("kill_switch_active", False))
        result.drawdown_pct = float(portfolio.get("current_drawdown_pct", 0.0) or 0.0)

    lines = [line.lower() for line in log_text.splitlines()]
    risk_block_count = sum("risk block" in line for line in lines)
    router_select_count = sum("router select" in line for line in lines)
    kill_switch_mentions = sum("kill switch" in line for line in lines)
    drawdown_mentions = sum("drawdown" in line for line in lines)

    result.repeated_risk_blocks = risk_block_count
    result.repeated_router_selects = router_select_count

    if risk_block_count:
        result.block_sources["risk_block"] = RiskBlockEvidence(
            source="risk_block",
            count=risk_block_count,
            summary=f"Detected {risk_block_count} risk-block log lines.",
        )
    if kill_switch_mentions:
        result.block_sources["kill_switch"] = RiskBlockEvidence(
            source="kill_switch",
            count=kill_switch_mentions,
            summary=f"Detected {kill_switch_mentions} kill-switch log lines.",
        )
    if drawdown_mentions:
        result.block_sources["drawdown"] = RiskBlockEvidence(
            source="drawdown",
            count=drawdown_mentions,
            summary=f"Detected {drawdown_mentions} drawdown log lines.",
        )

    result.blocked_expression = router_select_count > 0 and risk_block_count > 0
    result.expected_blocks = (not result.kill_switch_active) and risk_block_count == 0

    result.risk_state, result.assessment, result.evidence = _recommend(
        bot=bot,
        kill_switch_active=result.kill_switch_active,
        drawdown_pct=result.drawdown_pct,
        repeated_risk_blocks=risk_block_count,
        repeated_router_selects=router_select_count,
        blocked_expression=result.blocked_expression,
    )

    return result


def _recommend(
    bot: str,
    kill_switch_active: bool,
    drawdown_pct: float,
    repeated_risk_blocks: int,
    repeated_router_selects: int,
    blocked_expression: bool,
) -> tuple[str, str, List[str]]:
    evidence: List[str] = []

    if kill_switch_active:
        evidence.append("kill switch active")
    if drawdown_pct > 0:
        evidence.append(f"drawdown={drawdown_pct:.1f}%")
    if repeated_risk_blocks > 0:
        evidence.append(f"risk_blocks={repeated_risk_blocks}")
    if repeated_router_selects > 0:
        evidence.append(f"router_selects={repeated_router_selects}")

    if kill_switch_active and blocked_expression:
        return (
            "investigate kill switch",
            "Kill switch is active and the runtime is blocking new expressions of the strategy.",
            evidence,
        )
    if kill_switch_active:
        return (
            "investigate kill switch",
            "Kill switch is active; review why risk protection engaged before continuing.",
            evidence,
        )
    if drawdown_pct >= 20:
        return (
            "investigate drawdown/accounting",
            "Drawdown is elevated enough to warrant reviewing accounting and risk thresholds.",
            evidence,
        )
    if blocked_expression and repeated_risk_blocks >= 3:
        return (
            "monitor",
            "Repeated strategy selections are being blocked; verify whether this is expected risk gating or over-restriction.",
            evidence,
        )
    return (
        "healthy",
        "Risk state appears normal; no persistent blocking or kill-switch pressure detected.",
        evidence,
    )


def run_from_state_and_log(bot: str, state_path: str, log_path: Optional[str] = None) -> RiskForensicsResult:
    if not os.path.exists(state_path):
        return RiskForensicsResult(
            bot=bot,
            risk_state="monitor",
            assessment="State file missing.",
            evidence=["state file missing"],
        )

    with open(state_path) as f:
        state = json.load(f)

    log_text = ""
    if log_path and os.path.exists(log_path):
        with open(log_path, errors="replace") as f:
            log_text = f.read()

    return analyze_risk_forensics(bot, state, log_text)
