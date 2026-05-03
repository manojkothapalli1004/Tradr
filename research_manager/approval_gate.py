"""
Approval Gate V1 — lightweight typed approval-state layer for proposed actions.

Defines proposal/decision state only. No execution. No side effects.
Suitable for later wiring into apply agents or persistence adapters.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class ProposalAction:
    action_type: str
    target: str
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProposalDecision:
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str = ""
    decided_at: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class ApprovalProposal:
    proposal_id: str
    title: str
    summary: str
    action: ProposalAction
    created_at: str
    expires_at: Optional[str] = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    decision: ProposalDecision = field(default_factory=ProposalDecision)

    def is_terminal(self) -> bool:
        return self.status in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
        }

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if not self.expires_at:
            return False
        if now is None:
            now = utc_now()
        expires = parse_timestamp(self.expires_at)
        if expires is None:
            return False
        return now >= expires

    def with_expiry_check(self, now: Optional[datetime] = None) -> "ApprovalProposal":
        if self.status == ApprovalStatus.PENDING and self.is_expired(now=now):
            self.status = ApprovalStatus.EXPIRED
            self.decision.status = ApprovalStatus.EXPIRED
            self.decision.decided_at = utc_now().isoformat()
            if not self.decision.reason:
                self.decision.reason = "proposal expired"
        return self

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "title": self.title,
            "summary": self.summary,
            "action": self.action.to_dict(),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "requested_by": self.requested_by,
            "metadata": self.metadata,
            "decision": self.decision.to_dict(),
        }


@dataclass
class ApprovalStore:
    """In-memory proposal store with persistence-friendly dict serialization."""

    proposals: Dict[str, ApprovalProposal] = field(default_factory=dict)

    def add(self, proposal: ApprovalProposal) -> ApprovalProposal:
        self.proposals[proposal.proposal_id] = proposal
        return proposal

    def get(self, proposal_id: str) -> Optional[ApprovalProposal]:
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            return None
        return proposal.with_expiry_check()

    def list_all(self) -> List[ApprovalProposal]:
        return [proposal.with_expiry_check() for proposal in self.proposals.values()]

    def list_by_status(self, status: ApprovalStatus) -> List[ApprovalProposal]:
        return [
            proposal for proposal in self.list_all()
            if proposal.status == status
        ]

    def decide(
        self,
        proposal_id: str,
        status: ApprovalStatus,
        decided_by: str = "",
        reason: str = "",
        decided_at: Optional[str] = None,
    ) -> Optional[ApprovalProposal]:
        proposal = self.get(proposal_id)
        if proposal is None or proposal.is_terminal():
            return proposal

        proposal.status = status
        proposal.decision = ProposalDecision(
            status=status,
            decided_by=decided_by,
            decided_at=decided_at or utc_now().isoformat(),
            reason=reason,
        )
        return proposal

    def to_dict(self) -> dict:
        return {
            "proposals": {
                proposal_id: proposal.to_dict()
                for proposal_id, proposal in self.proposals.items()
            }
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ApprovalStore":
        store = cls()
        if not data:
            return store

        raw_proposals = data.get("proposals", {})
        if not isinstance(raw_proposals, dict):
            return store

        for proposal_id, raw in raw_proposals.items():
            proposal = proposal_from_dict(proposal_id, raw)
            if proposal is not None:
                store.proposals[proposal_id] = proposal
        return store


def proposal_from_dict(proposal_id: str, raw: Optional[dict]) -> Optional[ApprovalProposal]:
    if not isinstance(raw, dict):
        return None

    raw_action = raw.get("action", {})
    if not isinstance(raw_action, dict):
        raw_action = {}

    raw_decision = raw.get("decision", {})
    if not isinstance(raw_decision, dict):
        raw_decision = {}

    try:
        status = ApprovalStatus(raw.get("status", ApprovalStatus.PENDING.value))
    except ValueError:
        status = ApprovalStatus.PENDING

    try:
        decision_status = ApprovalStatus(raw_decision.get("status", status.value))
    except ValueError:
        decision_status = status

    action = ProposalAction(
        action_type=str(raw_action.get("action_type", "")),
        target=str(raw_action.get("target", "")),
        summary=str(raw_action.get("summary", "")),
        details=raw_action.get("details", {}) if isinstance(raw_action.get("details", {}), dict) else {},
    )

    decision = ProposalDecision(
        status=decision_status,
        decided_by=str(raw_decision.get("decided_by", "")),
        decided_at=raw_decision.get("decided_at"),
        reason=str(raw_decision.get("reason", "")),
    )

    proposal = ApprovalProposal(
        proposal_id=str(raw.get("proposal_id", proposal_id)),
        title=str(raw.get("title", "")),
        summary=str(raw.get("summary", "")),
        action=action,
        created_at=str(raw.get("created_at", "")),
        expires_at=raw.get("expires_at"),
        status=status,
        requested_by=str(raw.get("requested_by", "")),
        metadata=raw.get("metadata", {}) if isinstance(raw.get("metadata", {}), dict) else {},
        decision=decision,
    )
    return proposal.with_expiry_check()


def new_proposal(
    proposal_id: str,
    title: str,
    summary: str,
    action_type: str,
    target: str,
    action_summary: str,
    *,
    details: Optional[Dict[str, Any]] = None,
    requested_by: str = "",
    expires_at: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ApprovalProposal:
    return ApprovalProposal(
        proposal_id=proposal_id,
        title=title,
        summary=summary,
        action=ProposalAction(
            action_type=action_type,
            target=target,
            summary=action_summary,
            details=details or {},
        ),
        created_at=utc_now().isoformat(),
        expires_at=expires_at,
        status=ApprovalStatus.PENDING,
        requested_by=requested_by,
        metadata=metadata or {},
        decision=ProposalDecision(status=ApprovalStatus.PENDING),
    )


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
