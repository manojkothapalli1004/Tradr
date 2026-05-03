"""
Repair Planner Agent V1 — maps incidents and health states to safe next-step plans.

Read-only. No I/O. No side effects. No execution.

This module converts detected ops conditions into structured recommendations only.
It never restarts processes, mutates runtime, or performs automatic repair work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# ── Allowed recommendation actions ───────────────────────────────────

ACTION_WAIT_FOR_MORE_DATA = "wait for more data"
ACTION_RERUN_MANAGER = "rerun manager"
ACTION_CHECK_MARKET_HOURS = "check market-hours expectation"
ACTION_CLEAN_DUPLICATE_RUNNERS = "clean duplicate runners"
ACTION_RESTART_BOT = "restart bot"
ACTION_RESET_CLEAN_BASELINE = "reset clean baseline"

ALLOWED_ACTIONS = frozenset({
    ACTION_WAIT_FOR_MORE_DATA,
    ACTION_RERUN_MANAGER,
    ACTION_CHECK_MARKET_HOURS,
    ACTION_CLEAN_DUPLICATE_RUNNERS,
    ACTION_RESTART_BOT,
    ACTION_RESET_CLEAN_BASELINE,
})


# ── Typed inputs ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class IncidentSignal:
    """Normalized incident or health signal detected elsewhere in the ops layer."""
    code: str
    severity: str = "info"          # info / warning / critical
    summary: str = ""
    blocker: bool = False


@dataclass(frozen=True)
class HealthSignal:
    """Simple normalized health view from upstream health checks."""
    overall_health: str = "unknown"  # healthy / watch / blocked / unknown
    runner_count: Optional[int] = None
    market_open_expected: Optional[bool] = None
    has_recent_data: Optional[bool] = None


# ── Typed outputs ────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlannedAction:
    """One safe recommendation produced by the repair planner."""
    action: str
    reason: str
    confidence: str                  # LOW / MEDIUM / HIGH
    approval_required: bool

    def __post_init__(self) -> None:
        if self.action not in ALLOWED_ACTIONS:
            raise ValueError(f"Unsupported action: {self.action}")


@dataclass
class RepairPlan:
    """Structured read-only repair plan for current ops conditions."""
    overall_health: str = "unknown"
    blocker_present: bool = False
    primary_action: str = ACTION_WAIT_FOR_MORE_DATA
    confidence: str = "LOW"
    approval_required: bool = False
    rationale: str = ""
    supporting_actions: List[PlannedAction] = field(default_factory=list)
    incident_codes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "overall_health": self.overall_health,
            "blocker_present": self.blocker_present,
            "primary_action": self.primary_action,
            "confidence": self.confidence,
            "approval_required": self.approval_required,
            "rationale": self.rationale,
            "incident_codes": list(self.incident_codes),
            "supporting_actions": [
                {
                    "action": a.action,
                    "reason": a.reason,
                    "confidence": a.confidence,
                    "approval_required": a.approval_required,
                }
                for a in self.supporting_actions
            ],
        }


# ── Planner ──────────────────────────────────────────────────────────

def build_repair_plan(
    incidents: Optional[List[IncidentSignal]] = None,
    health: Optional[HealthSignal] = None,
) -> RepairPlan:
    """
    Build a safe, read-only repair plan from normalized incident and health inputs.

    The planner only recommends approved human-reviewed next steps.
    It never executes any action.
    """
    incidents = incidents or []
    health = health or HealthSignal()

    plan = RepairPlan(
        overall_health=health.overall_health,
        blocker_present=any(i.blocker for i in incidents) or health.overall_health == "blocked",
        incident_codes=[i.code for i in incidents],
    )

    actions: List[PlannedAction] = []

    if not incidents:
        actions.append(_plan_for_health_only(health))
    else:
        for incident in incidents:
            action = _plan_for_incident(incident, health)
            if action is not None:
                actions.append(action)

    if not actions:
        actions.append(_plan_for_health_only(health))

    deduped = _dedupe_actions(actions)
    primary = deduped[0]

    plan.primary_action = primary.action
    plan.confidence = primary.confidence
    plan.approval_required = primary.approval_required
    plan.rationale = primary.reason
    plan.supporting_actions = deduped[1:]
    return plan


def _plan_for_incident(incident: IncidentSignal, health: HealthSignal) -> Optional[PlannedAction]:
    code = incident.code.lower().strip()

    if code in {"insufficient_data", "low_sample", "no_completed_trades"}:
        return PlannedAction(
            action=ACTION_WAIT_FOR_MORE_DATA,
            reason="Sample is too small for repair conclusions; gather more observations first.",
            confidence="HIGH",
            approval_required=False,
        )

    if code in {"stale_manager_view", "analysis_outdated"}:
        return PlannedAction(
            action=ACTION_RERUN_MANAGER,
            reason="Ops summary may be stale; refresh the read-only manager view before deciding.",
            confidence="HIGH",
            approval_required=False,
        )

    if code in {"market_closed_expected", "market_hours_mismatch", "session_idle_expected"}:
        return PlannedAction(
            action=ACTION_CHECK_MARKET_HOURS,
            reason="Observed inactivity may match expected closed-session behavior.",
            confidence="MEDIUM",
            approval_required=False,
        )

    if code in {"duplicate_runners", "multiple_runner_processes"}:
        return PlannedAction(
            action=ACTION_CLEAN_DUPLICATE_RUNNERS,
            reason="More than one runner appears active; duplicate processes can distort ops health.",
            confidence="HIGH",
            approval_required=True,
        )

    if code in {"runner_stalled", "bot_unresponsive", "manager_detected_hang"}:
        return PlannedAction(
            action=ACTION_RESTART_BOT,
            reason="Health checks suggest the runner may be stuck and may need a controlled restart.",
            confidence="MEDIUM",
            approval_required=True,
        )

    if code in {"state_corrupt", "dirty_runtime_state", "baseline_drift"}:
        return PlannedAction(
            action=ACTION_RESET_CLEAN_BASELINE,
            reason="Current state may be contaminated or inconsistent with a known-good baseline.",
            confidence="MEDIUM",
            approval_required=True,
        )

    return _plan_for_health_only(health)


def _plan_for_health_only(health: HealthSignal) -> PlannedAction:
    if health.overall_health == "healthy":
        return PlannedAction(
            action=ACTION_WAIT_FOR_MORE_DATA,
            reason="System appears healthy; no repair action indicated yet.",
            confidence="MEDIUM",
            approval_required=False,
        )

    if health.market_open_expected is False and health.has_recent_data is False:
        return PlannedAction(
            action=ACTION_CHECK_MARKET_HOURS,
            reason="No recent activity may be normal outside expected market hours.",
            confidence="MEDIUM",
            approval_required=False,
        )

    if health.runner_count is not None and health.runner_count > 1:
        return PlannedAction(
            action=ACTION_CLEAN_DUPLICATE_RUNNERS,
            reason="Runner count suggests duplicate processes that should be cleaned up manually.",
            confidence="HIGH",
            approval_required=True,
        )

    if health.overall_health == "blocked":
        return PlannedAction(
            action=ACTION_RERUN_MANAGER,
            reason="Health is blocked but no specific incident was mapped; refresh manager evidence first.",
            confidence="LOW",
            approval_required=False,
        )

    return PlannedAction(
        action=ACTION_WAIT_FOR_MORE_DATA,
        reason="No specific repair signal mapped yet; preserve read-only posture and observe.",
        confidence="LOW",
        approval_required=False,
    )


def _dedupe_actions(actions: List[PlannedAction]) -> List[PlannedAction]:
    seen: set[str] = set()
    deduped: List[PlannedAction] = []
    for action in sorted(actions, key=_priority_key):
        if action.action in seen:
            continue
        seen.add(action.action)
        deduped.append(action)
    return deduped


def _priority_key(action: PlannedAction) -> tuple[int, int]:
    action_priority = {
        ACTION_CLEAN_DUPLICATE_RUNNERS: 0,
        ACTION_RESTART_BOT: 1,
        ACTION_RESET_CLEAN_BASELINE: 2,
        ACTION_RERUN_MANAGER: 3,
        ACTION_CHECK_MARKET_HOURS: 4,
        ACTION_WAIT_FOR_MORE_DATA: 5,
    }
    confidence_priority = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return (
        action_priority.get(action.action, 99),
        confidence_priority.get(action.confidence, 99),
    )
