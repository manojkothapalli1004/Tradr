"""
Incident Agent V1 — read-only cross-bot ops sub-agent.

Consumes shared repo evidence only:
- spot: paper_trading_state.json, paper_trading_v3.log, paper_trading_runner.pid
- options: options_bot/state.json, options_bot/options_bot.log, options_bot/options_bot.pid

No subprocess/process-table inspection. No side effects.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class IncidentResult:
    bot: str
    incident_type: str = "none"
    severity: str = "info"          # info | warning | critical
    evidence_summary: str = ""
    confidence: str = "HIGH"        # LOW | MEDIUM | HIGH
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class IncidentReport:
    healthy: bool = True
    primary_incident: Optional[IncidentResult] = None
    incidents: List[IncidentResult] = field(default_factory=list)
    incidents_by_bot: Dict[str, List[IncidentResult]] = field(default_factory=dict)
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "primary_incident": self.primary_incident.to_dict() if self.primary_incident else None,
            "incidents": [incident.to_dict() for incident in self.incidents],
            "incidents_by_bot": {
                bot: [incident.to_dict() for incident in incident_list]
                for bot, incident_list in self.incidents_by_bot.items()
            },
            "generated_at": self.generated_at,
        }


@dataclass
class BotEvidenceSnapshot:
    bot: str
    state_path: str
    log_path: str
    pid_path: str

    state_exists: bool = False
    state_corrupt: bool = False
    state_error: str = ""
    state_age_minutes: Optional[float] = None
    state_total_trades: int = 0
    state_active_trades: int = 0
    state_runtime_hours: float = 0.0

    log_exists: bool = False
    log_age_minutes: Optional[float] = None
    log_tail: str = ""

    pid_exists: bool = False
    pid_value: Optional[int] = None
    pid_valid: bool = False


@dataclass
class BotTarget:
    bot: str
    state_path: str
    log_path: str
    pid_path: str


class IncidentAgent:
    """Read-only incident classifier for spot and options bots."""

    TRACEBACK_THRESHOLD = 3
    STALE_MINUTES = 20
    STAGNATION_RUNTIME_HOURS = 6.0
    LOG_TAIL_BYTES = 32_000

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.base_dir = base_dir
        self.targets = self._build_targets(base_dir)

    def analyze(self) -> IncidentReport:
        report = IncidentReport(generated_at=datetime.now(timezone.utc).isoformat())

        all_incidents: List[IncidentResult] = []
        incidents_by_bot: Dict[str, List[IncidentResult]] = {}

        for target in self.targets:
            evidence = self.collect_evidence(target)
            incidents = self.classify(evidence)
            incidents_by_bot[target.bot] = incidents
            all_incidents.extend(incidents)

        report.incidents_by_bot = incidents_by_bot

        if all_incidents:
            report.healthy = False
            report.incidents = sorted(all_incidents, key=self._incident_rank, reverse=True)
            report.primary_incident = report.incidents[0]
            return report

        report.primary_incident = IncidentResult(
            bot="all",
            incident_type="none",
            severity="info",
            evidence_summary="No obvious incidents detected from current shared state/log/PID evidence.",
            confidence="HIGH",
            evidence=self._healthy_evidence(),
        )
        return report

    def _build_targets(self, base_dir: str) -> List[BotTarget]:
        return [
            BotTarget(
                bot="spot",
                state_path=os.path.join(base_dir, "paper_trading_state.json"),
                log_path=os.path.join(base_dir, "paper_trading_v3.log"),
                pid_path=os.path.join(base_dir, "paper_trading_runner.pid"),
            ),
            BotTarget(
                bot="options",
                state_path=os.path.join(base_dir, "options_bot", "state.json"),
                log_path=os.path.join(base_dir, "options_bot", "options_bot.log"),
                pid_path=os.path.join(base_dir, "options_bot", "options_bot.pid"),
            ),
        ]

    def collect_evidence(self, target: BotTarget) -> BotEvidenceSnapshot:
        snapshot = BotEvidenceSnapshot(
            bot=target.bot,
            state_path=target.state_path,
            log_path=target.log_path,
            pid_path=target.pid_path,
        )

        snapshot.state_exists = os.path.exists(target.state_path)
        if snapshot.state_exists:
            snapshot.state_age_minutes = self._age_minutes(os.path.getmtime(target.state_path))
            try:
                with open(target.state_path) as f:
                    state = json.load(f)
                snapshot.state_total_trades = len(state.get("completed_trades", []))
                active_key = "active_trades" if target.bot == "spot" else "open_trades"
                snapshot.state_active_trades = len(state.get(active_key, []))
                snapshot.state_runtime_hours = self._runtime_hours(state.get("start_time"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                snapshot.state_corrupt = True
                snapshot.state_error = str(exc)
        else:
            snapshot.state_error = "state file missing"

        snapshot.log_exists = os.path.exists(target.log_path)
        if snapshot.log_exists:
            snapshot.log_age_minutes = self._age_minutes(os.path.getmtime(target.log_path))
            snapshot.log_tail = self._read_log_tail(target.log_path)

        snapshot.pid_exists = os.path.exists(target.pid_path)
        if snapshot.pid_exists:
            try:
                with open(target.pid_path) as f:
                    raw = f.read().strip()
                if raw:
                    snapshot.pid_value = int(raw)
                    snapshot.pid_valid = True
            except (OSError, ValueError):
                snapshot.pid_value = None
                snapshot.pid_valid = False

        return snapshot

    def classify(self, evidence: BotEvidenceSnapshot) -> List[IncidentResult]:
        incidents: List[IncidentResult] = []

        missing_or_corrupt = self._detect_missing_or_corrupt_inputs(evidence)
        if missing_or_corrupt:
            incidents.append(missing_or_corrupt)

        duplicate_risk = self._detect_duplicate_runner_risk(evidence)
        if duplicate_risk:
            incidents.append(duplicate_risk)

        stopped_bot = self._detect_stopped_bot(evidence)
        if stopped_bot:
            incidents.append(stopped_bot)

        stale_inputs = self._detect_stale_inputs(evidence)
        if stale_inputs:
            incidents.append(stale_inputs)

        zero_trade = self._detect_zero_trade_stagnation(evidence)
        if zero_trade:
            incidents.append(zero_trade)

        repeated_errors = self._detect_repeated_errors(evidence)
        if repeated_errors:
            incidents.append(repeated_errors)

        return incidents

    def _detect_missing_or_corrupt_inputs(self, e: BotEvidenceSnapshot) -> Optional[IncidentResult]:
        problems: List[str] = []
        if not e.state_exists:
            problems.append(f"{os.path.basename(e.state_path)} missing")
        elif e.state_corrupt:
            problems.append(f"{os.path.basename(e.state_path)} unreadable: {e.state_error}")

        if not e.log_exists:
            problems.append(f"{os.path.basename(e.log_path)} missing")

        if not problems:
            return None

        severity = "critical" if (not e.state_exists or e.state_corrupt) else "warning"
        return IncidentResult(
            bot=e.bot,
            incident_type="missing_or_corrupt_inputs",
            severity=severity,
            evidence_summary="Required runtime evidence is missing or unreadable.",
            confidence="HIGH",
            evidence=problems,
        )

    def _detect_duplicate_runner_risk(self, e: BotEvidenceSnapshot) -> Optional[IncidentResult]:
        if not e.pid_exists:
            return None
        if e.pid_valid:
            return None

        return IncidentResult(
            bot=e.bot,
            incident_type="duplicate_runner_risk",
            severity="warning",
            evidence_summary="PID evidence is inconsistent or unreadable; duplicate or drift risk cannot be ruled out from shared files alone.",
            confidence="LOW",
            evidence=[f"PID file unreadable or invalid: {e.pid_path}"],
        )

    def _detect_stopped_bot(self, e: BotEvidenceSnapshot) -> Optional[IncidentResult]:
        stale_state = e.state_age_minutes is not None and e.state_age_minutes > self.STALE_MINUTES
        stale_log = e.log_age_minutes is not None and e.log_age_minutes > self.STALE_MINUTES

        if not e.pid_exists and not e.log_exists and not e.state_exists:
            return IncidentResult(
                bot=e.bot,
                incident_type="stopped_bot",
                severity="critical",
                evidence_summary="No PID, state, or log evidence found for the bot.",
                confidence="MEDIUM",
                evidence=["PID file missing", "state file missing", "log file missing"],
            )

        if e.pid_exists and not e.pid_valid and (stale_state or stale_log):
            evidence_lines: List[str] = ["PID file present but invalid/unreadable"]
            if stale_state:
                evidence_lines.append(f"state file stale: {e.state_age_minutes:.1f}m old")
            if stale_log:
                evidence_lines.append(f"log file stale: {e.log_age_minutes:.1f}m old")
            return IncidentResult(
                bot=e.bot,
                incident_type="stopped_bot",
                severity="critical",
                evidence_summary="Bot likely stopped based on invalid PID evidence and stale runtime artifacts.",
                confidence="MEDIUM",
                evidence=evidence_lines,
            )

        if not e.pid_exists and stale_state and stale_log:
            return IncidentResult(
                bot=e.bot,
                incident_type="stopped_bot",
                severity="critical",
                evidence_summary="Bot likely stopped based on missing PID file and stale state/log evidence.",
                confidence="MEDIUM",
                evidence=[
                    f"state file stale: {e.state_age_minutes:.1f}m old",
                    f"log file stale: {e.log_age_minutes:.1f}m old",
                ],
            )

        return None

    def _detect_stale_inputs(self, e: BotEvidenceSnapshot) -> Optional[IncidentResult]:
        stale_lines: List[str] = []
        if e.state_age_minutes is not None and e.state_age_minutes > self.STALE_MINUTES:
            stale_lines.append(f"state file is {e.state_age_minutes:.1f}m old")
        if e.log_age_minutes is not None and e.log_age_minutes > self.STALE_MINUTES:
            stale_lines.append(f"log file is {e.log_age_minutes:.1f}m old")

        if not stale_lines:
            return None

        return IncidentResult(
            bot=e.bot,
            incident_type="stale_inputs",
            severity="warning",
            evidence_summary="Runtime evidence is stale; monitoring confidence is degraded.",
            confidence="HIGH",
            evidence=stale_lines,
        )

    def _detect_zero_trade_stagnation(self, e: BotEvidenceSnapshot) -> Optional[IncidentResult]:
        if e.state_corrupt or not e.state_exists:
            return None
        if e.state_runtime_hours < self.STAGNATION_RUNTIME_HOURS:
            return None
        if e.state_total_trades > 0 or e.state_active_trades > 0:
            return None

        return IncidentResult(
            bot=e.bot,
            incident_type="zero_trade_stagnation",
            severity="warning",
            evidence_summary="Bot has been up for a meaningful window but has produced no completed or active trades.",
            confidence="MEDIUM",
            evidence=[
                f"runtime: {e.state_runtime_hours:.1f}h",
                f"completed trades: {e.state_total_trades}",
                f"active trades: {e.state_active_trades}",
            ],
        )

    def _detect_repeated_errors(self, e: BotEvidenceSnapshot) -> Optional[IncidentResult]:
        if not e.log_tail:
            return None

        patterns = [
            "traceback",
            "exception",
            "error",
            "failed",
            "algo not initialized",
        ]
        tail_lower = e.log_tail.lower()
        matches = sum(tail_lower.count(pattern) for pattern in patterns)
        if matches < self.TRACEBACK_THRESHOLD:
            return None

        severity = "critical" if "traceback" in tail_lower or "exception" in tail_lower else "warning"
        return IncidentResult(
            bot=e.bot,
            incident_type="repeated_errors",
            severity=severity,
            evidence_summary="Recent logs contain repeated error-like signals.",
            confidence="MEDIUM",
            evidence=[
                f"error-like pattern count in log tail: {matches}",
                f"log age: {e.log_age_minutes:.1f}m" if e.log_age_minutes is not None else "log age unknown",
            ],
        )

    def _healthy_evidence(self) -> List[str]:
        return [
            "spot and options shared evidence inspected",
            "no incident thresholds crossed",
        ]

    def _incident_rank(self, incident: IncidentResult) -> tuple[int, int]:
        severity_rank = {"info": 0, "warning": 1, "critical": 2}
        confidence_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        return (
            severity_rank.get(incident.severity, 0),
            confidence_rank.get(incident.confidence, 0),
        )

    def _age_minutes(self, mtime: float) -> float:
        return max((time_now_utc().timestamp() - mtime) / 60.0, 0.0)

    def _runtime_hours(self, start_time: Optional[str]) -> float:
        if not start_time:
            return 0.0
        try:
            start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return 0.0
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return max((time_now_utc() - start).total_seconds() / 3600.0, 0.0)

    def _read_log_tail(self, path: str) -> str:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(size - self.LOG_TAIL_BYTES, 0))
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""


def time_now_utc() -> datetime:
    return datetime.now(timezone.utc)


def analyze(base_dir: Optional[str] = None) -> IncidentReport:
    """Convenience function for one-shot evaluation."""
    return IncidentAgent(base_dir=base_dir).analyze()
