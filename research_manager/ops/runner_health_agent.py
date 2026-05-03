"""
Runner Health Agent V1 — read-only runtime health checker for spot and options bots.

Inspects PID files, state files, and log files without mutating runtime or
process state. No subprocess/process-table inspection; health is inferred from
repo-local evidence only. Returns typed health structures suitable for a later
ops aggregator.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class RunnerHealth:
    runner: str
    process_present: bool = False
    pid_file_present: bool = False
    pid_value: Optional[int] = None
    pid_process_alive: bool = False
    duplicate_runner_risk: bool = False
    duplicate_evidence: List[str] = field(default_factory=list)

    state_file_present: bool = False
    state_file_corrupt: bool = False
    state_error: str = ""
    state_age_minutes: Optional[float] = None

    log_file_present: bool = False
    log_age_minutes: Optional[float] = None

    latest_activity_timestamp: str = ""
    latest_activity_source: str = ""

    health: str = "mixed"          # healthy | mixed | unhealthy
    summary: str = ""
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunnerHealthReport:
    overall_health: str = "mixed"  # healthy | mixed | unhealthy
    generated_at: str = ""
    runners: Dict[str, RunnerHealth] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "overall_health": self.overall_health,
            "generated_at": self.generated_at,
            "runners": {name: runner.to_dict() for name, runner in self.runners.items()},
        }


@dataclass
class RunnerTarget:
    name: str
    pid_path: str
    state_path: str
    log_path: str
    stale_minutes: float = 20.0


class RunnerHealthAgent:
    """Read-only spot/options runtime health evaluator using file evidence only."""

    STALE_MINUTES = 20.0

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.base_dir = base_dir
        self.targets = self._build_targets(base_dir)

    def analyze(self) -> RunnerHealthReport:
        report = RunnerHealthReport(generated_at=datetime.now(timezone.utc).isoformat())
        for target in self.targets:
            report.runners[target.name] = self._analyze_target(target)
        report.overall_health = self._derive_overall_health(report.runners)
        return report

    def _build_targets(self, base_dir: str) -> List[RunnerTarget]:
        return [
            RunnerTarget(
                name="spot",
                pid_path=os.path.join(base_dir, "paper_trading_runner.pid"),
                state_path=os.path.join(base_dir, "paper_trading_state.json"),
                log_path=os.path.join(base_dir, "paper_trading_v3.log"),
                stale_minutes=self.STALE_MINUTES,
            ),
            RunnerTarget(
                name="options",
                pid_path=os.path.join(base_dir, "options_bot", "options_bot.pid"),
                state_path=os.path.join(base_dir, "options_bot", "state.json"),
                log_path=os.path.join(base_dir, "options_bot", "options_bot.log"),
                stale_minutes=self.STALE_MINUTES,
            ),
        ]

    def _analyze_target(self, target: RunnerTarget) -> RunnerHealth:
        health = RunnerHealth(runner=target.name)

        # PID evidence only
        health.pid_file_present = os.path.exists(target.pid_path)
        if health.pid_file_present:
            try:
                with open(target.pid_path) as f:
                    raw = f.read().strip()
                if raw:
                    health.pid_value = int(raw)
                    health.pid_process_alive = self._pid_is_running(health.pid_value)
                    health.process_present = health.pid_process_alive
                    if not health.pid_process_alive:
                        health.evidence.append(f"stale PID file: {health.pid_value}")
                else:
                    health.evidence.append("PID file empty")
            except (OSError, ValueError):
                health.evidence.append("PID file unreadable or invalid")
        else:
            health.evidence.append("PID file missing")

        # State file evidence
        state_payload: Optional[dict] = None
        health.state_file_present = os.path.exists(target.state_path)
        if health.state_file_present:
            health.state_age_minutes = self._age_minutes(os.path.getmtime(target.state_path))
            try:
                with open(target.state_path) as f:
                    state_payload = json.load(f)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                health.state_file_corrupt = True
                health.state_error = str(exc)
                health.evidence.append(f"state unreadable: {exc}")
        else:
            health.evidence.append("state file missing")

        # Log file evidence
        health.log_file_present = os.path.exists(target.log_path)
        if health.log_file_present:
            health.log_age_minutes = self._age_minutes(os.path.getmtime(target.log_path))
        else:
            health.evidence.append("log file missing")

        # Duplicate-runner risk: detectable only from stale/contradictory PID evidence in read-only mode
        health.duplicate_runner_risk = False
        if health.pid_file_present and not health.pid_process_alive and health.log_file_present:
            if health.log_age_minutes is not None and health.log_age_minutes <= target.stale_minutes:
                health.duplicate_runner_risk = True
                health.duplicate_evidence.append(
                    "recent log activity with stale/missing PID process suggests possible unmanaged duplicate runner"
                )

        latest_ts, latest_source = self._latest_activity(target, state_payload)
        health.latest_activity_timestamp = latest_ts
        health.latest_activity_source = latest_source

        self._finalize_health(target, health)
        return health

    def _finalize_health(self, target: RunnerTarget, health: RunnerHealth) -> None:
        problems = 0

        if not health.process_present:
            problems += 2
            health.evidence.append("process not confirmed from PID file")
        else:
            health.evidence.append("process confirmed by PID file")

        if health.duplicate_runner_risk:
            problems += 1
            health.evidence.extend(health.duplicate_evidence)

        if not health.state_file_present or health.state_file_corrupt:
            problems += 2
        elif health.state_age_minutes is not None:
            if health.state_age_minutes > target.stale_minutes:
                problems += 1
                health.evidence.append(f"state stale: {health.state_age_minutes:.1f}m")
            else:
                health.evidence.append(f"state fresh: {health.state_age_minutes:.1f}m")

        if not health.log_file_present:
            problems += 1
        elif health.log_age_minutes is not None:
            if health.log_age_minutes > target.stale_minutes:
                problems += 1
                health.evidence.append(f"log stale: {health.log_age_minutes:.1f}m")
            else:
                health.evidence.append(f"log fresh: {health.log_age_minutes:.1f}m")

        if health.latest_activity_timestamp:
            health.evidence.append(
                f"latest activity from {health.latest_activity_source}: {health.latest_activity_timestamp}"
            )
        else:
            health.evidence.append("latest activity timestamp unavailable")

        if problems == 0:
            health.health = "healthy"
            health.summary = f"{target.name} runner appears healthy."
        elif problems <= 2:
            health.health = "mixed"
            health.summary = f"{target.name} runner has partial health signals or stale evidence."
        else:
            health.health = "unhealthy"
            health.summary = f"{target.name} runner shows strong signs of runtime issues."

    def _latest_activity(self, target: RunnerTarget, state_payload: Optional[dict]) -> tuple[str, str]:
        candidates: List[tuple[datetime, str, str]] = []

        if state_payload:
            for value in self._extract_state_timestamps(state_payload):
                parsed = self._parse_timestamp(value)
                if parsed is not None:
                    candidates.append((parsed, value, "state"))

        if os.path.exists(target.log_path):
            mtime = os.path.getmtime(target.log_path)
            iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            candidates.append((datetime.fromtimestamp(mtime, tz=timezone.utc), iso, "log_mtime"))

        if os.path.exists(target.state_path):
            mtime = os.path.getmtime(target.state_path)
            iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            candidates.append((datetime.fromtimestamp(mtime, tz=timezone.utc), iso, "state_mtime"))

        if not candidates:
            return "", ""

        latest = max(candidates, key=lambda item: item[0])
        return latest[1], latest[2]

    def _extract_state_timestamps(self, obj: object) -> List[str]:
        values: List[str] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, str) and ("time" in key.lower() or key.lower().endswith("_at")):
                        values.append(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(obj)
        return values

    def _derive_overall_health(self, runners: Dict[str, RunnerHealth]) -> str:
        if not runners:
            return "unhealthy"
        values = [runner.health for runner in runners.values()]
        if any(value == "unhealthy" for value in values):
            return "unhealthy"
        if all(value == "healthy" for value in values):
            return "healthy"
        return "mixed"

    def _pid_is_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def _age_minutes(self, mtime: float) -> float:
        return max((datetime.now(timezone.utc).timestamp() - mtime) / 60.0, 0.0)

    def _parse_timestamp(self, value: str) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


def analyze(base_dir: Optional[str] = None) -> RunnerHealthReport:
    """Convenience function for one-shot evaluation."""
    return RunnerHealthAgent(base_dir=base_dir).analyze()
