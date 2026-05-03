"""
Ops Aggregator V1 — combines ops-layer sub-agent outputs into one typed summary.

Read-only. No runtime mutation. No execution or repair actions.
Consumes runner health, incident, and repair-plan style inputs and produces
an ops summary suitable for later manager/reporting integration.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── Typed output models ──────────────────────────────────────────────────


@dataclass
class OpsInputStatus:
    available: bool = False
    summary: str = ""
    confidence: str = "LOW"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OpsAction:
    action: str = "monitor"
    reason: str = "No strong action required."
    approval_required: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OpsSummary:
    overall_health: str = "mixed"          # healthy | mixed | unhealthy
    blocker_present: bool = False
    recommended_next_action: str = "monitor"
    confidence: str = "LOW"                # LOW | MEDIUM | HIGH
    approval_required: bool = False
    runner_health: OpsInputStatus = field(default_factory=OpsInputStatus)
    incident_status: OpsInputStatus = field(default_factory=OpsInputStatus)
    repair_plan_status: OpsInputStatus = field(default_factory=OpsInputStatus)
    reasons: List[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "overall_health": self.overall_health,
            "blocker_present": self.blocker_present,
            "recommended_next_action": self.recommended_next_action,
            "confidence": self.confidence,
            "approval_required": self.approval_required,
            "runner_health": self.runner_health.to_dict(),
            "incident_status": self.incident_status.to_dict(),
            "repair_plan_status": self.repair_plan_status.to_dict(),
            "reasons": list(self.reasons),
            "generated_at": self.generated_at,
        }


# ── Aggregation entrypoint ───────────────────────────────────────────────


def aggregate_ops(
    runner_health: Optional[Any] = None,
    incident_report: Optional[Any] = None,
    repair_plan: Optional[Any] = None,
) -> OpsSummary:
    """
    Combine runner health, incident, and repair-plan signals into one ops view.

    Inputs may be dataclasses, plain objects, or dict-like payloads.
    Missing inputs are handled gracefully.
    """
    runner_status = _summarize_runner_health(runner_health)
    incident_status = _summarize_incident_report(incident_report)
    repair_status = _summarize_repair_plan(repair_plan)

    reasons = _collect_reasons(runner_health, incident_report, repair_plan)
    overall_health = _derive_overall_health(runner_health, incident_report)
    blocker_present = _derive_blocker_present(runner_health, incident_report, repair_plan)
    action = _derive_next_action(runner_health, incident_report, repair_plan, blocker_present)
    confidence = _derive_confidence(runner_status, incident_status, repair_status, reasons)

    return OpsSummary(
        overall_health=overall_health,
        blocker_present=blocker_present,
        recommended_next_action=action.action,
        confidence=confidence,
        approval_required=action.approval_required,
        runner_health=runner_status,
        incident_status=incident_status,
        repair_plan_status=repair_status,
        reasons=reasons,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Input summarizers ────────────────────────────────────────────────────


def _summarize_runner_health(value: Optional[Any]) -> OpsInputStatus:
    if value is None:
        return OpsInputStatus(
            available=False,
            summary="Runner health unavailable.",
            confidence="LOW",
        )

    health = _lower(_pick(value, "health_classification", "health", "overall_health", default="mixed"))
    latest_activity = _pick(value, "latest_observed_activity_timestamp", "latest_activity_timestamp", "latest_activity")
    state_fresh = _pick(value, "state_freshness", "state_age_minutes", "state_age")
    log_fresh = _pick(value, "log_freshness", "log_age_minutes", "log_age")
    duplicate = _as_bool(_pick(value, "duplicate_runner_risk", "duplicate_risk", default=False))
    process_present = _pick(value, "process_present", "runner_present", default=None)

    parts: List[str] = [f"Runner health {health or 'mixed'}."]
    if process_present is True:
        parts.append("Process present.")
    elif process_present is False:
        parts.append("Process missing.")
    if duplicate:
        parts.append("Duplicate-runner risk detected.")
    if latest_activity:
        parts.append(f"Latest activity: {latest_activity}.")
    if state_fresh is not None:
        parts.append(f"State freshness: {state_fresh}.")
    if log_fresh is not None:
        parts.append(f"Log freshness: {log_fresh}.")

    confidence = _upper(_pick(value, "confidence", default="MEDIUM"))
    if confidence not in {"LOW", "MEDIUM", "HIGH"}:
        confidence = "MEDIUM"

    return OpsInputStatus(
        available=True,
        summary=" ".join(parts),
        confidence=confidence,
    )



def _summarize_incident_report(value: Optional[Any]) -> OpsInputStatus:
    if value is None:
        return OpsInputStatus(
            available=False,
            summary="No incident report available.",
            confidence="LOW",
        )

    healthy = _pick(value, "healthy", default=None)
    primary = _pick(value, "primary_incident", default=None)
    incident_type = _pick(primary, "incident_type", default="none") if primary is not None else "none"
    severity = _pick(primary, "severity", default="info") if primary is not None else "info"
    evidence_summary = _pick(primary, "evidence_summary", "summary", default="") if primary is not None else ""
    confidence = _upper(_pick(primary, "confidence", default=_pick(value, "confidence", default="MEDIUM")))
    if confidence not in {"LOW", "MEDIUM", "HIGH"}:
        confidence = "MEDIUM"

    if healthy is True or incident_type == "none":
        summary = "No active incident detected."
    else:
        label = incident_type or "unknown_incident"
        sev = severity or "warning"
        detail = evidence_summary or "Incident detected."
        summary = f"{label} ({sev}): {detail}"

    return OpsInputStatus(
        available=True,
        summary=summary,
        confidence=confidence,
    )



def _summarize_repair_plan(value: Optional[Any]) -> OpsInputStatus:
    if value is None:
        return OpsInputStatus(
            available=False,
            summary="No repair plan available.",
            confidence="LOW",
        )

    next_step = _pick(
        value,
        "primary_action",
        "recommended_next_step",
        "recommended_action",
        "next_step",
        "next_action",
    )
    rationale = _pick(value, "rationale", "reason", "summary", default="")
    approval_required = _as_bool(_pick(value, "approval_required", default=False))
    blocker = _as_bool(_pick(value, "blocker_present", "has_blocker", default=False))
    confidence = _upper(_pick(value, "confidence", default="MEDIUM"))
    if confidence not in {"LOW", "MEDIUM", "HIGH"}:
        confidence = "MEDIUM"

    if next_step:
        summary = f"Repair plan available: {next_step}."
        if rationale:
            summary += f" {rationale}"
    else:
        summary = "Repair plan present but next step unspecified."
        if rationale:
            summary += f" {rationale}"

    if blocker:
        summary += " Blocker noted."
    if approval_required:
        summary += " Approval required."

    summary = summary.strip()
    return OpsInputStatus(
        available=True,
        summary=summary,
        confidence=confidence,
    )


# ── Decision logic ──────────────────────────────────────────────────────


def _derive_overall_health(
    runner_health: Optional[Any],
    incident_report: Optional[Any],
) -> str:
    runner_class = _lower(_pick(runner_health, "health_classification", "health", "overall_health", default="mixed"))
    if runner_class in {"unhealthy", "healthy", "mixed"}:
        base = runner_class
    else:
        base = "mixed"

    healthy = _pick(incident_report, "healthy", default=None)
    primary = _pick(incident_report, "primary_incident", default=None)
    incident_severity = _lower(_pick(primary, "severity", default="info")) if primary is not None else "info"

    if healthy is False and incident_severity == "critical":
        return "unhealthy"
    if base == "unhealthy":
        return "unhealthy"
    if healthy is False or base == "mixed":
        return "mixed"
    if healthy is True and base == "healthy":
        return "healthy"
    return base



def _derive_blocker_present(
    runner_health: Optional[Any],
    incident_report: Optional[Any],
    repair_plan: Optional[Any],
) -> bool:
    runner_blocker = _as_bool(_pick(runner_health, "blocker_present", "has_blocker", default=False))
    duplicate = _as_bool(_pick(runner_health, "duplicate_runner_risk", "duplicate_risk", default=False))
    process_present = _pick(runner_health, "process_present", "runner_present", default=None)
    runner_class = _lower(_pick(runner_health, "health_classification", "health", default="mixed"))

    incident_healthy = _pick(incident_report, "healthy", default=None)
    primary = _pick(incident_report, "primary_incident", default=None)
    severity = _lower(_pick(primary, "severity", default="info")) if primary is not None else "info"

    repair_blocker = _as_bool(_pick(repair_plan, "blocker_present", "has_blocker", default=False))

    if runner_blocker or repair_blocker or duplicate:
        return True
    if process_present is False and runner_class == "unhealthy":
        return True
    if incident_healthy is False and severity == "critical":
        return True
    return False



def _derive_next_action(
    runner_health: Optional[Any],
    incident_report: Optional[Any],
    repair_plan: Optional[Any],
    blocker_present: bool,
) -> OpsAction:
    repair_action = _pick(
        repair_plan,
        "primary_action",
        "recommended_next_step",
        "recommended_action",
        "next_step",
        "next_action",
    )
    repair_reason = _pick(repair_plan, "rationale", "reason", "summary", default="Repair-plan recommendation available.")
    repair_approval = _as_bool(_pick(repair_plan, "approval_required", default=False))
    if repair_action:
        return OpsAction(
            action=str(repair_action),
            reason=str(repair_reason) if repair_reason else "Repair-plan recommendation available.",
            approval_required=repair_approval,
        )

    primary = _pick(incident_report, "primary_incident", default=None)
    incident_type = _pick(primary, "incident_type", default="none") if primary is not None else "none"
    severity = _lower(_pick(primary, "severity", default="info")) if primary is not None else "info"
    runner_class = _lower(_pick(runner_health, "health_classification", "health", default="mixed"))
    duplicate = _as_bool(_pick(runner_health, "duplicate_runner_risk", "duplicate_risk", default=False))
    process_present = _pick(runner_health, "process_present", "runner_present", default=None)

    if duplicate:
        return OpsAction(
            action="request manual review of duplicate runner risk",
            reason="Duplicate-runner risk is operationally blocking.",
            approval_required=True,
        )
    if blocker_present and (incident_type == "stopped_bot" or process_present is False):
        return OpsAction(
            action="request manual review of missing or stopped runner",
            reason="Runner appears unavailable.",
            approval_required=True,
        )
    if blocker_present and severity == "critical":
        return OpsAction(
            action="request manual review of critical incident",
            reason="Critical incident present.",
            approval_required=True,
        )
    if runner_class == "mixed" or severity == "warning":
        return OpsAction(
            action="monitor and collect more runtime evidence",
            reason="Signal quality is mixed but not clearly blocking.",
            approval_required=False,
        )
    return OpsAction(
        action="continue monitoring",
        reason="No blocker or actionable incident detected.",
        approval_required=False,
    )



def _derive_confidence(
    runner_status: OpsInputStatus,
    incident_status: OpsInputStatus,
    repair_status: OpsInputStatus,
    reasons: List[str],
) -> str:
    confidences = [
        _confidence_rank(runner_status.confidence),
        _confidence_rank(incident_status.confidence),
        _confidence_rank(repair_status.confidence),
    ]
    available_count = sum(1 for s in (runner_status, incident_status, repair_status) if s.available)

    if available_count == 0:
        return "LOW"
    if available_count == 1:
        return _confidence_name(max(confidences))

    floor = min(rank for rank, status in zip(confidences, (runner_status, incident_status, repair_status)) if status.available)
    if len(reasons) <= 1 and available_count >= 2 and floor >= 1:
        return "MEDIUM" if floor == 1 else "HIGH"
    return _confidence_name(floor)



def _collect_reasons(
    runner_health: Optional[Any],
    incident_report: Optional[Any],
    repair_plan: Optional[Any],
) -> List[str]:
    reasons: List[str] = []

    runner_class = _pick(runner_health, "health_classification", "health", default=None)
    if runner_class:
        reasons.append(f"runner_health={runner_class}")

    process_present = _pick(runner_health, "process_present", "runner_present", default=None)
    if process_present is True:
        reasons.append("runner_process_present")
    elif process_present is False:
        reasons.append("runner_process_missing")

    if _as_bool(_pick(runner_health, "duplicate_runner_risk", "duplicate_risk", default=False)):
        reasons.append("duplicate_runner_risk")

    healthy = _pick(incident_report, "healthy", default=None)
    primary = _pick(incident_report, "primary_incident", default=None)
    incident_type = _pick(primary, "incident_type", default=None) if primary is not None else None
    severity = _pick(primary, "severity", default=None) if primary is not None else None

    if healthy is True:
        reasons.append("no_active_incident")
    elif healthy is False:
        reasons.append("incident_detected")
    if incident_type and incident_type != "none":
        reasons.append(f"incident_type={incident_type}")
    if severity:
        reasons.append(f"incident_severity={severity}")

    repair_action = _pick(
        repair_plan,
        "primary_action",
        "recommended_next_step",
        "recommended_action",
        "next_step",
        "next_action",
        default=None,
    )
    if repair_action:
        reasons.append("repair_plan_available")
        reasons.append(f"repair_action={repair_action}")
    repair_rationale = _pick(repair_plan, "rationale", "reason", "summary", default=None)
    if repair_rationale:
        reasons.append("repair_rationale_present")
    if _as_bool(_pick(repair_plan, "approval_required", default=False)):
        reasons.append("repair_plan_requires_approval")
    if _as_bool(_pick(repair_plan, "blocker_present", "has_blocker", default=False)):
        reasons.append("repair_plan_blocker")

    return reasons


# ── Generic access helpers ──────────────────────────────────────────────


def _pick(source: Optional[Any], *keys: str, default: Any = None) -> Any:
    if source is None:
        return default

    if is_dataclass(source):
        values = asdict(source)
        for key in keys:
            if key in values:
                return values[key]
        return default

    if isinstance(source, dict):
        for key in keys:
            if key in source:
                return source[key]
        return default

    for key in keys:
        if hasattr(source, key):
            return getattr(source, key)
    return default



def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)



def _lower(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()



def _upper(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()



def _confidence_rank(value: str) -> int:
    return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(_upper(value), 0)



def _confidence_name(rank: int) -> str:
    if rank >= 2:
        return "HIGH"
    if rank == 1:
        return "MEDIUM"
    return "LOW"
