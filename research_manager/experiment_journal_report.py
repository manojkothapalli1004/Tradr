"""
Experiment Journal Report V1 — read-only summary layer for recent experiment history.

This module reads the experiment journal safely, derives concise typed summaries,
and returns structured data for future manager/dashboard integration.

No runtime changes. No journal mutation. Safe on missing or empty journal files.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from .experiment_journal import (
    ExperimentEntry,
    ExperimentJournal,
    ExperimentStatus,
    default_journal_path,
    load_journal,
)


@dataclass(frozen=True)
class ExperimentHeadline:
    experiment_id: str
    target_bot: str
    target_scope: str
    parameter_changed: str
    status: str
    verdict: str = ""
    start_timestamp: Optional[str] = None
    end_timestamp: Optional[str] = None
    sample_size: int = 0

    @classmethod
    def from_entry(cls, entry: ExperimentEntry) -> "ExperimentHeadline":
        return cls(
            experiment_id=entry.experiment_id,
            target_bot=entry.target_bot,
            target_scope=entry.target_scope,
            parameter_changed=entry.parameter_changed,
            status=entry.status.value,
            verdict=entry.verdict,
            start_timestamp=entry.start_timestamp,
            end_timestamp=entry.end_timestamp,
            sample_size=entry.sample_size,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExperimentJournalReport:
    journal_path: str
    journal_present: bool = False
    total_experiments: int = 0
    active_experiment: Optional[ExperimentHeadline] = None
    most_recent_completed_experiment: Optional[ExperimentHeadline] = None
    reverted_experiments: List[ExperimentHeadline] = field(default_factory=list)
    current_running_tests: List[ExperimentHeadline] = field(default_factory=list)
    verdict_history: List[ExperimentHeadline] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "journal_path": self.journal_path,
            "journal_present": self.journal_present,
            "total_experiments": self.total_experiments,
            "active_experiment": self.active_experiment.to_dict() if self.active_experiment else None,
            "most_recent_completed_experiment": (
                self.most_recent_completed_experiment.to_dict()
                if self.most_recent_completed_experiment else None
            ),
            "reverted_experiments": [entry.to_dict() for entry in self.reverted_experiments],
            "current_running_tests": [entry.to_dict() for entry in self.current_running_tests],
            "verdict_history": [entry.to_dict() for entry in self.verdict_history],
            "summary": self.summary,
        }


def build_experiment_journal_report(path: Optional[str] = None) -> ExperimentJournalReport:
    """Load the journal read-only and derive a compact typed report."""
    journal_path = path or default_journal_path()
    report = ExperimentJournalReport(journal_path=journal_path, journal_present=False)

    journal = load_journal(journal_path)
    entries = journal.list_all()

    report.journal_present = bool(entries)
    report.total_experiments = len(entries)

    if not entries:
        report.summary = "No experiment history available."
        return report

    running = _sort_entries(
        [entry for entry in entries if entry.status == ExperimentStatus.RUNNING],
        primary_time="start_timestamp",
    )
    completed = _sort_entries(
        [entry for entry in entries if entry.status == ExperimentStatus.COMPLETED],
        primary_time="end_timestamp",
    )
    reverted = _sort_entries(
        [entry for entry in entries if entry.status == ExperimentStatus.REVERTED],
        primary_time="end_timestamp",
    )
    with_verdicts = _sort_entries(
        [entry for entry in entries if entry.verdict.strip()],
        primary_time="end_timestamp",
    )

    report.current_running_tests = [ExperimentHeadline.from_entry(entry) for entry in running]
    report.active_experiment = report.current_running_tests[0] if report.current_running_tests else None
    report.most_recent_completed_experiment = (
        ExperimentHeadline.from_entry(completed[0]) if completed else None
    )
    report.reverted_experiments = [ExperimentHeadline.from_entry(entry) for entry in reverted]
    report.verdict_history = [ExperimentHeadline.from_entry(entry) for entry in with_verdicts]
    report.summary = _build_summary(report)
    return report


def build_experiment_journal_report_from_journal(
    journal: Optional[ExperimentJournal],
    *,
    journal_path: str = "",
) -> ExperimentJournalReport:
    """Build a report from an already-loaded journal object."""
    if journal is None:
        return ExperimentJournalReport(
            journal_path=journal_path or default_journal_path(),
            journal_present=False,
            summary="No experiment history available.",
        )

    return _build_report_from_entries(
        entries=journal.list_all(),
        journal_path=journal_path or default_journal_path(),
    )


def _build_report_from_entries(entries: List[ExperimentEntry], journal_path: str) -> ExperimentJournalReport:
    report = ExperimentJournalReport(
        journal_path=journal_path,
        journal_present=bool(entries),
        total_experiments=len(entries),
    )

    if not entries:
        report.summary = "No experiment history available."
        return report

    running = _sort_entries(
        [entry for entry in entries if entry.status == ExperimentStatus.RUNNING],
        primary_time="start_timestamp",
    )
    completed = _sort_entries(
        [entry for entry in entries if entry.status == ExperimentStatus.COMPLETED],
        primary_time="end_timestamp",
    )
    reverted = _sort_entries(
        [entry for entry in entries if entry.status == ExperimentStatus.REVERTED],
        primary_time="end_timestamp",
    )
    with_verdicts = _sort_entries(
        [entry for entry in entries if entry.verdict.strip()],
        primary_time="end_timestamp",
    )

    report.current_running_tests = [ExperimentHeadline.from_entry(entry) for entry in running]
    report.active_experiment = report.current_running_tests[0] if report.current_running_tests else None
    report.most_recent_completed_experiment = (
        ExperimentHeadline.from_entry(completed[0]) if completed else None
    )
    report.reverted_experiments = [ExperimentHeadline.from_entry(entry) for entry in reverted]
    report.verdict_history = [ExperimentHeadline.from_entry(entry) for entry in with_verdicts]
    report.summary = _build_summary(report)
    return report


def _sort_entries(entries: List[ExperimentEntry], primary_time: str) -> List[ExperimentEntry]:
    return sorted(
        entries,
        key=lambda entry: (
            _time_key(getattr(entry, primary_time, None)),
            _time_key(entry.start_timestamp),
            entry.experiment_id,
        ),
        reverse=True,
    )


def _time_key(value: Optional[str]) -> str:
    return value or ""


def _build_summary(report: ExperimentJournalReport) -> str:
    parts: List[str] = []

    if report.active_experiment:
        parts.append(f"Active: {report.active_experiment.experiment_id}")
    else:
        parts.append("Active: none")

    if report.most_recent_completed_experiment:
        parts.append(
            f"Latest completed: {report.most_recent_completed_experiment.experiment_id}"
        )
    else:
        parts.append("Latest completed: none")

    parts.append(f"Reverted: {len(report.reverted_experiments)}")
    parts.append(f"Running: {len(report.current_running_tests)}")
    parts.append(f"Verdicts: {len(report.verdict_history)}")
    return " | ".join(parts)
