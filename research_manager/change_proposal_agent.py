"""
Change Proposal Agent V1 — read-only decision-to-action translation layer.

Consumes structured findings from manager / ops / forensic layers and produces
recommendation-only change proposals. No execution. No side effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, is_dataclass
from typing import Any, Dict, List, Optional


# ── Typed models ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChangeProposal:
    proposal_id: str
    target_bot: str                 # spot | options | both
    target_scope: str               # runtime | exit_logic | combo | ops | baseline
    proposed_change: str
    rationale: str
    expected_benefit: str
    main_risk: str
    approval_required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChangeProposalResult:
    proposals: List[ChangeProposal] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "proposals": [p.to_dict() for p in self.proposals],
            "summary": self.summary,
        }


# ── Main entrypoint ──────────────────────────────────────────────────


def build_change_proposals(
    manager_view: Optional[Any] = None,
    ops_view: Optional[Any] = None,
    spot_exit_forensics: Optional[Any] = None,
    options_exit_forensics: Optional[Any] = None,
    spot_risk_forensics: Optional[Any] = None,
    options_risk_forensics: Optional[Any] = None,
) -> ChangeProposalResult:
    result = ChangeProposalResult()

    # 1. Spot exit pressure → exit tuning
    spot_time_limit_ratio = _pick(spot_exit_forensics, "time_limit_ratio", default=0.0) or 0.0
    if spot_time_limit_ratio >= 0.5:
        result.proposals.append(ChangeProposal(
            proposal_id="spot-exit-tune-take-profit",
            target_bot="spot",
            target_scope="exit_logic",
            proposed_change="adjust take_profit modestly downward",
            rationale=f"Spot time-limit exits are {spot_time_limit_ratio:.0%}; exits are not reaching target before forced cleanup.",
            expected_benefit="Reduce time-limit exits and convert more profitable drift into explicit target captures.",
            main_risk="Lower targets may cut winners too early and reduce upside per trade.",
            approval_required=True,
        ))

    # 2. Spot combo-specific weakness → pause combo
    weakest_path = _pick(manager_view, "strongest_path", default=None)  # not used, but keeps symmetry
    weakest_path = _pick(manager_view, "weakest_path", default=None)
    if isinstance(weakest_path, str) and weakest_path.startswith("spot:"):
        combo = weakest_path.split("spot:", 1)[1]
        result.proposals.append(ChangeProposal(
            proposal_id="spot-pause-weakest-combo",
            target_bot="spot",
            target_scope="combo",
            proposed_change=f"pause combo {combo}",
            rationale=f"Current weakest spot path is {combo}.",
            expected_benefit="Prevents weakest combo from dragging aggregate spot performance while stronger paths continue.",
            main_risk="May remove a combo before enough sample accumulates to judge it fairly.",
            approval_required=True,
        ))

    # 3. Options kill switch → investigate / keep blocked
    options_kill_switch = bool(_pick(options_risk_forensics, "kill_switch_active", default=False))
    if options_kill_switch:
        result.proposals.append(ChangeProposal(
            proposal_id="options-investigate-kill-switch",
            target_bot="options",
            target_scope="ops",
            proposed_change="investigate kill switch before further tuning",
            rationale="Options kill switch is active and is materially preventing strategy expression.",
            expected_benefit="Restores confidence that future options research is measuring strategy behavior rather than kill-switch blockage.",
            main_risk="Investigation may delay experimentation while preserving current blocked state.",
            approval_required=True,
        ))

    # 4. Options blocked expression + stale health → restart recommendation
    options_runtime_health = _pick(ops_view, "runner_health", default=None)
    options_summary = _pick(options_runtime_health, "summary", default="") if options_runtime_health is not None else ""
    if isinstance(options_summary, str) and "mixed" in options_summary.lower():
        result.proposals.append(ChangeProposal(
            proposal_id="options-restart-runner",
            target_bot="options",
            target_scope="ops",
            proposed_change="restart bot",
            rationale="Options runtime health is degraded and state/log freshness is uneven.",
            expected_benefit="Resets runtime to a known-good state for cleaner observation.",
            main_risk="Restart can interrupt current monitoring continuity and requires manual approval.",
            approval_required=True,
        ))

    # 5. Dirty runtime history → reset clean baseline
    incident_summary = _pick(_pick(ops_view, "incident_status", default=None), "summary", default="")
    if isinstance(incident_summary, str) and ("stale" in incident_summary.lower() or "corrupt" in incident_summary.lower()):
        result.proposals.append(ChangeProposal(
            proposal_id="ops-reset-clean-baseline",
            target_bot="both",
            target_scope="baseline",
            proposed_change="reset clean baseline",
            rationale="Ops evidence suggests stale or degraded runtime evidence.",
            expected_benefit="Improves confidence in subsequent before/after experiments.",
            main_risk="Resets can discard continuity unless state is backed up first.",
            approval_required=True,
        ))

    # 6. Fallback: no change
    if not result.proposals:
        result.proposals.append(ChangeProposal(
            proposal_id="no-change",
            target_bot="both",
            target_scope="runtime",
            proposed_change="no change",
            rationale="Current findings do not yet justify a specific controlled change.",
            expected_benefit="Preserves the current baseline and allows more evidence to accumulate.",
            main_risk="Delays action if a real issue is already emerging.",
            approval_required=False,
        ))

    result.summary = _build_summary(result.proposals)
    return result


# ── Helpers ──────────────────────────────────────────────────────────


def _build_summary(proposals: List[ChangeProposal]) -> str:
    if not proposals:
        return "No proposals."
    if len(proposals) == 1 and proposals[0].proposal_id == "no-change":
        return "No change proposed; continue observing."
    ids = ", ".join(p.proposal_id for p in proposals)
    return f"Generated {len(proposals)} proposal(s): {ids}."


def _pick(source: Optional[Any], key: str, default: Any = None) -> Any:
    if source is None:
        return default
    if is_dataclass(source):
        return getattr(source, key, default)
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)
