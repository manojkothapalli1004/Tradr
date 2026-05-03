"""
Experiment Journal V1 — typed sidecar journal for tracking runtime experiments.

This module is intentionally separate from trading runtime code. It provides
simple typed models plus file-based JSON persistence helpers for later use by
manager / proposal / approval layers.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional


class ExperimentStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    REVERTED = "reverted"
    ABANDONED = "abandoned"


@dataclass
class ExperimentEntry:
    experiment_id: str
    target_bot: str                 # spot | options | both
    target_scope: str               # runtime | exit_logic | combo | strategy | ops | baseline
    parameter_changed: str
    old_value: str
    new_value: str
    rationale: str
    start_timestamp: Optional[str] = None
    end_timestamp: Optional[str] = None
    status: ExperimentStatus = ExperimentStatus.PLANNED
    sample_size: int = 0
    verdict: str = ""              # success | no_change | worse | inconclusive | etc.
    notes: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, raw: dict) -> "ExperimentEntry":
        status_raw = raw.get("status", ExperimentStatus.PLANNED.value)
        try:
            status = ExperimentStatus(status_raw)
        except ValueError:
            status = ExperimentStatus.PLANNED
        return cls(
            experiment_id=str(raw.get("experiment_id", "")),
            target_bot=str(raw.get("target_bot", "")),
            target_scope=str(raw.get("target_scope", "")),
            parameter_changed=str(raw.get("parameter_changed", "")),
            old_value=str(raw.get("old_value", "")),
            new_value=str(raw.get("new_value", "")),
            rationale=str(raw.get("rationale", "")),
            start_timestamp=raw.get("start_timestamp"),
            end_timestamp=raw.get("end_timestamp"),
            status=status,
            sample_size=int(raw.get("sample_size", 0) or 0),
            verdict=str(raw.get("verdict", "")),
            notes=list(raw.get("notes", []) or []),
            metadata=dict(raw.get("metadata", {}) or {}),
        )


@dataclass
class ExperimentJournal:
    entries: Dict[str, ExperimentEntry] = field(default_factory=dict)

    def add(self, entry: ExperimentEntry) -> ExperimentEntry:
        self.entries[entry.experiment_id] = entry
        return entry

    def get(self, experiment_id: str) -> Optional[ExperimentEntry]:
        return self.entries.get(experiment_id)

    def list_all(self) -> List[ExperimentEntry]:
        return list(self.entries.values())

    def list_by_status(self, status: ExperimentStatus) -> List[ExperimentEntry]:
        return [entry for entry in self.entries.values() if entry.status == status]

    def update_status(
        self,
        experiment_id: str,
        status: ExperimentStatus,
        *,
        end_timestamp: Optional[str] = None,
        verdict: Optional[str] = None,
        sample_size: Optional[int] = None,
        note: Optional[str] = None,
    ) -> Optional[ExperimentEntry]:
        entry = self.entries.get(experiment_id)
        if entry is None:
            return None
        entry.status = status
        if end_timestamp is not None:
            entry.end_timestamp = end_timestamp
        if verdict is not None:
            entry.verdict = verdict
        if sample_size is not None:
            entry.sample_size = sample_size
        if note:
            entry.notes.append(note)
        return entry

    def to_dict(self) -> dict:
        return {
            "entries": {
                experiment_id: entry.to_dict()
                for experiment_id, entry in self.entries.items()
            }
        }

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "ExperimentJournal":
        journal = cls()
        if not isinstance(raw, dict):
            return journal
        raw_entries = raw.get("entries", {})
        if not isinstance(raw_entries, dict):
            return journal
        for experiment_id, entry_raw in raw_entries.items():
            if isinstance(entry_raw, dict):
                journal.entries[experiment_id] = ExperimentEntry.from_dict(entry_raw)
        return journal


# ── File persistence helpers ────────────────────────────────────────


def default_journal_path(base_dir: Optional[str] = None) -> str:
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "experiment_journal.json")


def load_journal(path: Optional[str] = None) -> ExperimentJournal:
    path = path or default_journal_path()
    if not os.path.exists(path):
        return ExperimentJournal()
    with open(path, "r") as f:
        raw = json.load(f)
    return ExperimentJournal.from_dict(raw)


def save_journal(journal: ExperimentJournal, path: Optional[str] = None) -> str:
    path = path or default_journal_path()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(journal.to_dict(), f, indent=2)
    os.replace(tmp_path, path)
    return path


# ── Convenience constructors ────────────────────────────────────────


def new_experiment(
    experiment_id: str,
    target_bot: str,
    target_scope: str,
    parameter_changed: str,
    old_value: str,
    new_value: str,
    rationale: str,
    *,
    status: ExperimentStatus = ExperimentStatus.PLANNED,
    start_timestamp: Optional[str] = None,
    notes: Optional[List[str]] = None,
    metadata: Optional[Dict[str, str]] = None,
) -> ExperimentEntry:
    return ExperimentEntry(
        experiment_id=experiment_id,
        target_bot=target_bot,
        target_scope=target_scope,
        parameter_changed=parameter_changed,
        old_value=old_value,
        new_value=new_value,
        rationale=rationale,
        start_timestamp=start_timestamp or datetime.now(timezone.utc).isoformat(),
        status=status,
        notes=list(notes or []),
        metadata=dict(metadata or {}),
    )
