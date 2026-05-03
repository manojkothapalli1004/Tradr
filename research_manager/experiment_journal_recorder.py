"""
Experiment Journal Recorder V1 — safe helper layer over experiment_journal.py.

This module only reads/writes the experiment journal sidecar file. It never
modifies runtime bot files, configs, or process state.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, Optional

from .experiment_journal import (
    ExperimentEntry,
    ExperimentJournal,
    ExperimentStatus,
    default_journal_path,
    load_journal,
    new_experiment,
    save_journal,
)


@dataclass
class RecorderResult:
    journal_path: str
    entry: ExperimentEntry
    action: str
    saved: bool = True
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["entry"] = self.entry.to_dict()
        return data


class ExperimentJournalRecorder:
    """Small typed recorder for lifecycle-safe experiment journal updates."""

    def __init__(self, journal_path: Optional[str] = None):
        self.journal_path = journal_path or default_journal_path()

    def create_experiment(
        self,
        experiment_id: str,
        target_bot: str,
        target_scope: str,
        parameter_changed: str,
        old_value: str,
        new_value: str,
        rationale: str,
        *,
        notes: Optional[list[str]] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> RecorderResult:
        journal = self._load()
        entry = new_experiment(
            experiment_id=experiment_id,
            target_bot=target_bot,
            target_scope=target_scope,
            parameter_changed=parameter_changed,
            old_value=old_value,
            new_value=new_value,
            rationale=rationale,
            status=ExperimentStatus.PLANNED,
            notes=notes,
            metadata=metadata,
        )
        journal.add(entry)
        self._save(journal)
        return RecorderResult(
            journal_path=self.journal_path,
            entry=entry,
            action="create_experiment",
        )

    def mark_running(
        self,
        experiment_id: str,
        *,
        note: str = "",
        sample_size: Optional[int] = None,
    ) -> Optional[RecorderResult]:
        return self._update_status(
            experiment_id,
            ExperimentStatus.RUNNING,
            note=note,
            sample_size=sample_size,
            set_start_if_missing=True,
            action="mark_running",
        )

    def mark_completed(
        self,
        experiment_id: str,
        *,
        verdict: str = "",
        note: str = "",
        sample_size: Optional[int] = None,
    ) -> Optional[RecorderResult]:
        return self._update_status(
            experiment_id,
            ExperimentStatus.COMPLETED,
            verdict=verdict,
            note=note,
            sample_size=sample_size,
            set_end=True,
            action="mark_completed",
        )

    def mark_reverted(
        self,
        experiment_id: str,
        *,
        verdict: str = "reverted",
        note: str = "",
        sample_size: Optional[int] = None,
    ) -> Optional[RecorderResult]:
        return self._update_status(
            experiment_id,
            ExperimentStatus.REVERTED,
            verdict=verdict,
            note=note,
            sample_size=sample_size,
            set_end=True,
            action="mark_reverted",
        )

    def update_verdict(
        self,
        experiment_id: str,
        verdict: str,
        *,
        note: str = "",
        sample_size: Optional[int] = None,
    ) -> Optional[RecorderResult]:
        journal = self._load()
        entry = journal.get(experiment_id)
        if entry is None:
            return None
        entry.verdict = verdict
        if sample_size is not None:
            entry.sample_size = sample_size
        if note:
            entry.notes.append(note)
        self._save(journal)
        return RecorderResult(
            journal_path=self.journal_path,
            entry=entry,
            action="update_verdict",
        )

    def append_notes(self, experiment_id: str, *notes: str) -> Optional[RecorderResult]:
        journal = self._load()
        entry = journal.get(experiment_id)
        if entry is None:
            return None
        for note in notes:
            if note:
                entry.notes.append(note)
        self._save(journal)
        return RecorderResult(
            journal_path=self.journal_path,
            entry=entry,
            action="append_notes",
        )

    def _update_status(
        self,
        experiment_id: str,
        status: ExperimentStatus,
        *,
        verdict: str = "",
        note: str = "",
        sample_size: Optional[int] = None,
        set_start_if_missing: bool = False,
        set_end: bool = False,
        action: str,
    ) -> Optional[RecorderResult]:
        journal = self._load()
        entry = journal.get(experiment_id)
        if entry is None:
            return None

        if set_start_if_missing and not entry.start_timestamp:
            entry.start_timestamp = utc_now().isoformat()

        end_timestamp = utc_now().isoformat() if set_end else None
        updated = journal.update_status(
            experiment_id,
            status,
            end_timestamp=end_timestamp,
            verdict=verdict or None,
            sample_size=sample_size,
            note=note or None,
        )
        if updated is None:
            return None

        self._save(journal)
        return RecorderResult(
            journal_path=self.journal_path,
            entry=updated,
            action=action,
        )

    def _load(self) -> ExperimentJournal:
        return load_journal(self.journal_path)

    def _save(self, journal: ExperimentJournal) -> str:
        return save_journal(journal, self.journal_path)


def create_experiment(
    experiment_id: str,
    target_bot: str,
    target_scope: str,
    parameter_changed: str,
    old_value: str,
    new_value: str,
    rationale: str,
    *,
    journal_path: Optional[str] = None,
    notes: Optional[list[str]] = None,
    metadata: Optional[Dict[str, str]] = None,
) -> RecorderResult:
    recorder = ExperimentJournalRecorder(journal_path=journal_path)
    return recorder.create_experiment(
        experiment_id=experiment_id,
        target_bot=target_bot,
        target_scope=target_scope,
        parameter_changed=parameter_changed,
        old_value=old_value,
        new_value=new_value,
        rationale=rationale,
        notes=notes,
        metadata=metadata,
    )


def mark_running(
    experiment_id: str,
    *,
    journal_path: Optional[str] = None,
    note: str = "",
    sample_size: Optional[int] = None,
) -> Optional[RecorderResult]:
    return ExperimentJournalRecorder(journal_path=journal_path).mark_running(
        experiment_id,
        note=note,
        sample_size=sample_size,
    )


def mark_completed(
    experiment_id: str,
    *,
    journal_path: Optional[str] = None,
    verdict: str = "",
    note: str = "",
    sample_size: Optional[int] = None,
) -> Optional[RecorderResult]:
    return ExperimentJournalRecorder(journal_path=journal_path).mark_completed(
        experiment_id,
        verdict=verdict,
        note=note,
        sample_size=sample_size,
    )


def mark_reverted(
    experiment_id: str,
    *,
    journal_path: Optional[str] = None,
    verdict: str = "reverted",
    note: str = "",
    sample_size: Optional[int] = None,
) -> Optional[RecorderResult]:
    return ExperimentJournalRecorder(journal_path=journal_path).mark_reverted(
        experiment_id,
        verdict=verdict,
        note=note,
        sample_size=sample_size,
    )


def update_verdict(
    experiment_id: str,
    verdict: str,
    *,
    journal_path: Optional[str] = None,
    note: str = "",
    sample_size: Optional[int] = None,
) -> Optional[RecorderResult]:
    return ExperimentJournalRecorder(journal_path=journal_path).update_verdict(
        experiment_id,
        verdict,
        note=note,
        sample_size=sample_size,
    )


def append_notes(
    experiment_id: str,
    *notes: str,
    journal_path: Optional[str] = None,
) -> Optional[RecorderResult]:
    return ExperimentJournalRecorder(journal_path=journal_path).append_notes(
        experiment_id,
        *notes,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
