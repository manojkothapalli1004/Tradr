"""
Apply-and-Verify Agent V1 — constrained execution planning layer for approved actions.

This module is intentionally allowlist-based and does not expose arbitrary shell,
code-editing, or free-form command interfaces. It accepts an already-approved
proposal object, translates it into a typed execution plan, and defines explicit
verification expectations/results so concrete wiring can be added carefully later.

V1 is structural only:
- no real execution
- no process control
- no file mutation
- no shell command construction
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, is_dataclass
from typing import Any, Dict, List, Optional


# ── Allowlisted actions ──────────────────────────────────────────────

ACTION_CONFIG_VALUE_CHANGE = "config_value_change"
ACTION_CLEAN_BOT_RESTART = "clean_bot_restart"
ACTION_BASELINE_BACKUP_RESET = "baseline_backup_reset"

ALLOWED_ACTIONS = frozenset({
    ACTION_CONFIG_VALUE_CHANGE,
    ACTION_CLEAN_BOT_RESTART,
    ACTION_BASELINE_BACKUP_RESET,
})

VERIFY_SINGLE_RUNNING_PID = "single_running_pid"
VERIFY_EXPECTED_CONFIG_LINE = "expected_config_line_present"
VERIFY_EXPECTED_LOG_CONFIRMATION = "expected_log_confirmation_present"

ALLOWED_VERIFICATIONS = frozenset({
    VERIFY_SINGLE_RUNNING_PID,
    VERIFY_EXPECTED_CONFIG_LINE,
    VERIFY_EXPECTED_LOG_CONFIRMATION,
})

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_SKIPPED = "skipped"
STATUS_NOT_EXECUTED = "not_executed"

OUTCOME_PENDING = "pending"
OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_SKIPPED = "skipped"


# ── Input model ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ApprovedProposal:
    """Minimal approved proposal contract accepted by this agent."""
    proposal_id: str
    proposed_change: str
    target_bot: str = ""
    target_scope: str = ""
    approval_granted: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.proposal_id.strip():
            raise ValueError("proposal_id is required")
        if not self.proposed_change.strip():
            raise ValueError("proposed_change is required")

    def to_dict(self) -> dict:
        return asdict(self)


# ── Execution plan models ───────────────────────────────────────────

@dataclass(frozen=True)
class ExecutionStep:
    """One constrained step to be wired later by a controlled executor."""
    step_id: str
    action_type: str
    target_path: Optional[str] = None
    field_name: Optional[str] = None
    expected_value: Optional[str] = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.action_type not in ALLOWED_ACTIONS:
            raise ValueError(f"Unsupported action_type: {self.action_type}")


@dataclass(frozen=True)
class VerificationExpectation:
    """Explicit post-apply verification expectation."""
    check_type: str
    target: str
    expected_value: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.check_type not in ALLOWED_VERIFICATIONS:
            raise ValueError(f"Unsupported check_type: {self.check_type}")


@dataclass
class ApplyPlan:
    """Execution plan derived from an approved proposal."""
    proposal_id: str
    action_type: str
    ready: bool = False
    blocked_reason: str = ""
    execution_steps: List[ExecutionStep] = field(default_factory=list)
    verification_expectations: List[VerificationExpectation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "action_type": self.action_type,
            "ready": self.ready,
            "blocked_reason": self.blocked_reason,
            "execution_steps": [asdict(step) for step in self.execution_steps],
            "verification_expectations": [asdict(v) for v in self.verification_expectations],
        }


# ── Result models ───────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """Execution status placeholder for future controlled wiring."""
    proposal_id: str
    action_type: str
    status: str = STATUS_PENDING
    executed_steps: List[str] = field(default_factory=list)
    skipped_steps: List[str] = field(default_factory=list)
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationCheckResult:
    """Result of one explicit verification check."""
    check_type: str
    target: str
    expected_value: str
    outcome: str = OUTCOME_PENDING
    observed_value: str = ""
    details: str = ""

    def __post_init__(self) -> None:
        if self.check_type not in ALLOWED_VERIFICATIONS:
            raise ValueError(f"Unsupported check_type: {self.check_type}")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationResult:
    """Grouped verification result for one apply attempt."""
    proposal_id: str
    overall_outcome: str = OUTCOME_PENDING
    checks: List[VerificationCheckResult] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "overall_outcome": self.overall_outcome,
            "checks": [check.to_dict() for check in self.checks],
            "summary": self.summary,
        }


@dataclass
class ApplyAndVerifyResult:
    """Top-level typed result returned by the agent."""
    plan: ApplyPlan
    execution: ExecutionResult
    verification: VerificationResult

    def to_dict(self) -> dict:
        return {
            "plan": self.plan.to_dict(),
            "execution": self.execution.to_dict(),
            "verification": self.verification.to_dict(),
        }


# ── Main entrypoint ─────────────────────────────────────────────────

def build_apply_and_verify_result(proposal: Any) -> ApplyAndVerifyResult:
    """
    Build a constrained apply/verify structure for an already-approved proposal.

    This function does not execute anything. It only:
    1. normalizes the approved proposal
    2. chooses one allowlisted action type
    3. constructs typed execution and verification structures
    """
    approved = _normalize_proposal(proposal)
    action_type = _map_proposal_to_action(approved)
    plan = _build_plan(approved, action_type)
    execution = _build_execution_result(plan)
    verification = _build_verification_result(plan)
    return ApplyAndVerifyResult(plan=plan, execution=execution, verification=verification)


# ── Helpers ─────────────────────────────────────────────────────────

def _normalize_proposal(source: Any) -> ApprovedProposal:
    if isinstance(source, ApprovedProposal):
        proposal = source
    elif is_dataclass(source):
        proposal = ApprovedProposal(
            proposal_id=str(getattr(source, "proposal_id", "")).strip(),
            proposed_change=str(getattr(source, "proposed_change", "")).strip(),
            target_bot=str(getattr(source, "target_bot", "") or ""),
            target_scope=str(getattr(source, "target_scope", "") or ""),
            approval_granted=bool(getattr(source, "approval_required", True) is False or getattr(source, "approval_granted", False)),
            metadata=dict(getattr(source, "metadata", {}) or {}),
        )
    elif isinstance(source, dict):
        proposal = ApprovedProposal(
            proposal_id=str(source.get("proposal_id", "")).strip(),
            proposed_change=str(source.get("proposed_change", "")).strip(),
            target_bot=str(source.get("target_bot", "") or ""),
            target_scope=str(source.get("target_scope", "") or ""),
            approval_granted=bool(source.get("approval_granted", False)),
            metadata=dict(source.get("metadata", {}) or {}),
        )
    else:
        raise TypeError("proposal must be ApprovedProposal, dataclass, or dict")

    if not proposal.approval_granted:
        raise ValueError("proposal must be already approved")
    return proposal


def _map_proposal_to_action(proposal: ApprovedProposal) -> str:
    change = proposal.proposed_change.lower().strip()

    if any(token in change for token in ("config", "set ", "update value", "toggle ")):
        return ACTION_CONFIG_VALUE_CHANGE
    if "restart" in change:
        return ACTION_CLEAN_BOT_RESTART
    if "baseline" in change or "backup/reset" in change or "reset clean baseline" in change:
        return ACTION_BASELINE_BACKUP_RESET
    raise ValueError(f"Proposal is outside allowlist: {proposal.proposed_change}")


def _build_plan(proposal: ApprovedProposal, action_type: str) -> ApplyPlan:
    plan = ApplyPlan(
        proposal_id=proposal.proposal_id,
        action_type=action_type,
        ready=True,
    )

    metadata = proposal.metadata

    if action_type == ACTION_CONFIG_VALUE_CHANGE:
        plan.execution_steps = [
            ExecutionStep(
                step_id="prepare_config_change",
                action_type=action_type,
                target_path=_string_meta(metadata, "config_path"),
                field_name=_string_meta(metadata, "config_field"),
                expected_value=_string_meta(metadata, "expected_config_line"),
                note="Apply a pre-approved config value change through a future constrained editor.",
            ),
        ]
        plan.verification_expectations = [
            VerificationExpectation(
                check_type=VERIFY_EXPECTED_CONFIG_LINE,
                target=_string_meta(metadata, "config_path"),
                expected_value=_string_meta(metadata, "expected_config_line"),
                note="Confirm the expected config line is present after apply.",
            ),
        ]
        if not _string_meta(metadata, "config_path") or not _string_meta(metadata, "expected_config_line"):
            plan.ready = False
            plan.blocked_reason = "config_path and expected_config_line are required for config changes"

    elif action_type == ACTION_CLEAN_BOT_RESTART:
        plan.execution_steps = [
            ExecutionStep(
                step_id="prepare_clean_restart",
                action_type=action_type,
                target_path=_string_meta(metadata, "service_name"),
                note="Perform a controlled clean restart through a future fixed restart handler.",
            ),
        ]
        plan.verification_expectations = [
            VerificationExpectation(
                check_type=VERIFY_SINGLE_RUNNING_PID,
                target=_string_meta(metadata, "process_name") or _string_meta(metadata, "service_name"),
                expected_value="1",
                note="Exactly one runner PID should be active after restart.",
            ),
            VerificationExpectation(
                check_type=VERIFY_EXPECTED_LOG_CONFIRMATION,
                target=_string_meta(metadata, "log_path"),
                expected_value=_string_meta(metadata, "expected_log_confirmation"),
                note="Look for the expected startup confirmation in logs.",
            ),
        ]
        if not _string_meta(metadata, "service_name"):
            plan.ready = False
            plan.blocked_reason = "service_name is required for clean bot restart"

    elif action_type == ACTION_BASELINE_BACKUP_RESET:
        plan.execution_steps = [
            ExecutionStep(
                step_id="prepare_baseline_backup",
                action_type=action_type,
                target_path=_string_meta(metadata, "state_path"),
                note="Create backup before any reset through a future fixed baseline handler.",
            ),
            ExecutionStep(
                step_id="prepare_baseline_reset",
                action_type=action_type,
                target_path=_string_meta(metadata, "baseline_path"),
                note="Apply reset from a known clean baseline through a future fixed handler.",
            ),
        ]
        plan.verification_expectations = [
            VerificationExpectation(
                check_type=VERIFY_SINGLE_RUNNING_PID,
                target=_string_meta(metadata, "process_name") or _string_meta(metadata, "service_name"),
                expected_value="1",
                note="Exactly one runner PID should be active after reset flow completes.",
            ),
            VerificationExpectation(
                check_type=VERIFY_EXPECTED_LOG_CONFIRMATION,
                target=_string_meta(metadata, "log_path"),
                expected_value=_string_meta(metadata, "expected_log_confirmation"),
                note="Log should confirm clean baseline start/reset completion.",
            ),
        ]
        if not _string_meta(metadata, "state_path") or not _string_meta(metadata, "baseline_path"):
            plan.ready = False
            plan.blocked_reason = "state_path and baseline_path are required for baseline backup/reset"

    else:
        raise ValueError(f"Unsupported action_type: {action_type}")

    return plan


def _build_execution_result(plan: ApplyPlan) -> ExecutionResult:
    if not plan.ready:
        return ExecutionResult(
            proposal_id=plan.proposal_id,
            action_type=plan.action_type,
            status=STATUS_SKIPPED,
            skipped_steps=[step.step_id for step in plan.execution_steps],
            details=[plan.blocked_reason] if plan.blocked_reason else ["plan is not ready"],
        )

    return ExecutionResult(
        proposal_id=plan.proposal_id,
        action_type=plan.action_type,
        status=STATUS_NOT_EXECUTED,
        skipped_steps=[step.step_id for step in plan.execution_steps],
        details=["Execution wiring is intentionally not implemented in V1."],
    )


def _build_verification_result(plan: ApplyPlan) -> VerificationResult:
    checks = [
        VerificationCheckResult(
            check_type=expectation.check_type,
            target=expectation.target,
            expected_value=expectation.expected_value,
            outcome=OUTCOME_SKIPPED if not plan.ready else OUTCOME_PENDING,
            details="Verification wiring is intentionally not implemented in V1.",
        )
        for expectation in plan.verification_expectations
    ]

    if not plan.ready:
        return VerificationResult(
            proposal_id=plan.proposal_id,
            overall_outcome=OUTCOME_SKIPPED,
            checks=checks,
            summary=plan.blocked_reason or "Verification skipped because plan is not ready.",
        )

    return VerificationResult(
        proposal_id=plan.proposal_id,
        overall_outcome=OUTCOME_PENDING,
        checks=checks,
        summary="Verification expectations prepared; concrete checks are not wired in V1.",
    )


def _string_meta(metadata: Dict[str, Any], key: str) -> str:
    value = metadata.get(key, "")
    if value is None:
        return ""
    return str(value)
