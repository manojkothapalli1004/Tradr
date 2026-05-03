from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(HERE), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from dashboard.normalize import BotSummary, ParsedBotData
    from dashboard.options_parser import parse_options_data
    from dashboard.spot_parser import parse_spot_data
except ImportError:
    from normalize import BotSummary, ParsedBotData
    from options_parser import parse_options_data
    from spot_parser import parse_spot_data

try:
    from research_manager.approval_gate import ApprovalStatus, ApprovalStore
    from research_manager.experiment_journal_report import build_experiment_journal_report
except ImportError:
    from ..research_manager.approval_gate import ApprovalStatus, ApprovalStore
    from ..research_manager.experiment_journal_report import build_experiment_journal_report


PROPOSALS_PATH = ROOT / "research_manager" / "change_proposals.json"
APPROVALS_PATH = ROOT / "research_manager" / "approval_store.json"
JOURNAL_PATH = ROOT / "research_manager" / "experiment_journal.json"


@dataclass(frozen=True)
class OperatorProposalItem:
    proposal_id: str
    title: str
    summary: str
    status: str
    action_type: str
    target: str
    requested_by: str
    created_at: str


@dataclass
class ApprovalStatusSummary:
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    expired: int = 0
    total: int = 0


@dataclass
class OperatorPanelsData:
    experiment_report: Any
    current_proposals: list[OperatorProposalItem] = field(default_factory=list)
    approval_summary: ApprovalStatusSummary = field(default_factory=ApprovalStatusSummary)
    proposal_source_available: bool = False
    approval_source_available: bool = False
    proposal_source_path: str = ""
    approval_source_path: str = ""

    def has_any_operator_data(self) -> bool:
        return bool(
            getattr(self.experiment_report, "journal_present", False)
            or self.current_proposals
            or self.approval_source_available
            or self.proposal_source_available
        )


def load_dashboard_data() -> dict[str, ParsedBotData]:
    return {
        "spot": parse_spot_data(ROOT),
        "options": parse_options_data(ROOT),
    }


def load_operator_panels_data() -> OperatorPanelsData:
    experiment_report = build_experiment_journal_report(str(JOURNAL_PATH))
    current_proposals = _load_current_proposals(PROPOSALS_PATH)
    approval_store = _load_approval_store(APPROVALS_PATH)
    return OperatorPanelsData(
        experiment_report=experiment_report,
        current_proposals=current_proposals,
        approval_summary=_build_approval_summary(approval_store),
        proposal_source_available=PROPOSALS_PATH.exists(),
        approval_source_available=APPROVALS_PATH.exists(),
        proposal_source_path=str(PROPOSALS_PATH),
        approval_source_path=str(APPROVALS_PATH),
    )


def _safe_load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_current_proposals(path: Path) -> list[OperatorProposalItem]:
    raw = _safe_load_json(path)
    if not isinstance(raw, dict):
        return []

    raw_items = raw.get("proposals", raw.get("items", raw.get("entries", [])))
    if isinstance(raw_items, dict):
        values = list(raw_items.values())
    elif isinstance(raw_items, list):
        values = raw_items
    else:
        return []

    items: list[OperatorProposalItem] = []
    for raw_item in values:
        if not isinstance(raw_item, dict):
            continue
        items.append(
            OperatorProposalItem(
                proposal_id=str(raw_item.get("proposal_id", "") or raw_item.get("id", "")),
                title=str(raw_item.get("title", raw_item.get("proposed_change", ""))),
                summary=str(raw_item.get("summary", raw_item.get("rationale", ""))),
                status=str(raw_item.get("status", "pending") or "pending"),
                action_type=str(raw_item.get("action_type", raw_item.get("target_scope", ""))),
                target=str(raw_item.get("target", raw_item.get("target_bot", ""))),
                requested_by=str(raw_item.get("requested_by", "")),
                created_at=str(raw_item.get("created_at", "")),
            )
        )
    return sorted(items, key=lambda item: (item.created_at, item.proposal_id), reverse=True)


def _load_approval_store(path: Path) -> ApprovalStore:
    raw = _safe_load_json(path)
    if not isinstance(raw, dict):
        return ApprovalStore()
    return ApprovalStore.from_dict(raw)


def _build_approval_summary(store: ApprovalStore) -> ApprovalStatusSummary:
    proposals = store.list_all()
    summary = ApprovalStatusSummary(total=len(proposals))
    for proposal in proposals:
        if proposal.status == ApprovalStatus.PENDING:
            summary.pending += 1
        elif proposal.status == ApprovalStatus.APPROVED:
            summary.approved += 1
        elif proposal.status == ApprovalStatus.REJECTED:
            summary.rejected += 1
        elif proposal.status == ApprovalStatus.EXPIRED:
            summary.expired += 1
    return summary


def proposals_frame(items: list[OperatorProposalItem]) -> pd.DataFrame:
    if not items:
        return pd.DataFrame(columns=["proposal_id", "title", "status", "action_type", "target", "created_at"])
    return pd.DataFrame([
        {
            "proposal_id": item.proposal_id,
            "title": item.title,
            "status": item.status,
            "action_type": item.action_type,
            "target": item.target,
            "created_at": item.created_at,
        }
        for item in items
    ])


def experiment_history_frame(report: Any, limit: int = 8) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    entries = list(getattr(report, "current_running_tests", [])) + list(getattr(report, "reverted_experiments", []))
    most_recent = getattr(report, "most_recent_completed_experiment", None)
    if most_recent is not None:
        entries.append(most_recent)
    seen: set[str] = set()
    for entry in list(getattr(report, "verdict_history", [])) + entries:
        if entry.experiment_id in seen:
            continue
        seen.add(entry.experiment_id)
        rows.append(
            {
                "experiment_id": entry.experiment_id,
                "status": entry.status,
                "target_bot": entry.target_bot,
                "target_scope": entry.target_scope,
                "parameter_changed": entry.parameter_changed,
                "verdict": entry.verdict,
                "sample_size": entry.sample_size,
                "start_timestamp": entry.start_timestamp,
                "end_timestamp": entry.end_timestamp,
            }
        )
        if len(rows) >= limit:
            break
    if not rows:
        return pd.DataFrame(columns=["experiment_id", "status", "target_bot", "target_scope", "parameter_changed", "verdict", "sample_size", "start_timestamp", "end_timestamp"])
    return pd.DataFrame(rows)


def approval_status_frame(summary: ApprovalStatusSummary) -> pd.DataFrame:
    return pd.DataFrame([
        {"status": "pending", "count": summary.pending},
        {"status": "approved", "count": summary.approved},
        {"status": "rejected", "count": summary.rejected},
        {"status": "expired", "count": summary.expired},
    ])


def active_experiment_rows(report: Any) -> list[tuple[str, str, str | None]]:
    active = getattr(report, "active_experiment", None)
    if active is None:
        return [("Active experiment", "None", None)]
    return [
        ("Experiment", active.experiment_id, "active"),
        ("Bot", active.target_bot or "—", None),
        ("Scope", active.target_scope or "—", None),
        ("Parameter", active.parameter_changed or "—", None),
        ("Status", active.status, "active"),
        ("Started", active.start_timestamp or "—", None),
        ("Verdict", active.verdict or "—", None),
    ]


def approval_summary_rows(summary: ApprovalStatusSummary) -> list[tuple[str, str, str | None]]:
    return [
        ("Pending", str(summary.pending), "warning" if summary.pending else None),
        ("Approved", str(summary.approved), "healthy" if summary.approved else None),
        ("Rejected", str(summary.rejected), None),
        ("Expired", str(summary.expired), None),
        ("Total", str(summary.total), None),
    ]


def proposal_summary_rows(items: list[OperatorProposalItem], source_available: bool) -> list[tuple[str, str, str | None]]:
    latest = items[0] if items else None
    return [
        ("Source", "Present" if source_available else "Missing", "healthy" if source_available else "warning"),
        ("Current proposals", str(len(items)), "warning" if items else None),
        ("Latest proposal", latest.proposal_id if latest else "None", None),
        ("Latest status", latest.status if latest else "—", None),
    ]


def operator_panels_summary(operator_data: OperatorPanelsData) -> str:
    if not operator_data.has_any_operator_data():
        return "No operator metadata available."
    summary = getattr(operator_data.experiment_report, "summary", "No experiment history available.")
    return " | ".join([
        summary,
        f"Proposals: {len(operator_data.current_proposals)}",
        f"Pending approvals: {operator_data.approval_summary.pending}",
    ])


def combine_trades(data: dict[str, ParsedBotData]) -> pd.DataFrame:
    frames = [parsed.trades for parsed in data.values() if not parsed.trades.empty]
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    if "net_pnl" not in combined.columns:
        realized = pd.to_numeric(combined.get("realized_pnl"), errors="coerce") if "realized_pnl" in combined.columns else pd.Series(0.0, index=combined.index)
        unrealized = pd.to_numeric(combined.get("unrealized_pnl"), errors="coerce") if "unrealized_pnl" in combined.columns else pd.Series(0.0, index=combined.index)
        combined["net_pnl"] = realized.fillna(0.0) + unrealized.fillna(0.0)
    if "outcome" not in combined.columns:
        status = combined.get("status", pd.Series("unknown", index=combined.index)).astype(str).str.lower()
        net_pnl = pd.to_numeric(combined["net_pnl"], errors="coerce").fillna(0.0)
        combined["outcome"] = "loss"
        combined.loc[status == "open", "outcome"] = "open"
        combined.loc[(status != "open") & (net_pnl > 0), "outcome"] = "win"
    return combined


def combine_recent_activity(data: dict[str, ParsedBotData]) -> pd.DataFrame:
    frames = [parsed.recent_activity for parsed in data.values() if not parsed.recent_activity.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False).sort_values("timestamp", ascending=False)


def filter_trades(trades: pd.DataFrame, bot_filter: str, outcome_filter: str, symbol_filter: str) -> pd.DataFrame:
    filtered = trades.copy()
    if filtered.empty:
        return filtered
    if bot_filter != "All":
        filtered = filtered[filtered["bot"].astype(str) == bot_filter.lower()]
    if outcome_filter != "All":
        outcome_map = {"Wins": "win", "Losses": "loss", "Open": "open"}
        filtered = filtered[filtered["outcome"].astype(str) == outcome_map[outcome_filter]]
    if symbol_filter != "All":
        filtered = filtered[filtered["symbol"].astype(str) == symbol_filter]
    return filtered.sort_values("entry_time", ascending=False)


def overall_summary(data: dict[str, ParsedBotData]) -> dict[str, Any]:
    trades = combine_trades(data)
    open_positions = sum(parsed.summary.open_positions for parsed in data.values())
    realized_pnl = float(trades.loc[trades["outcome"] != "open", "net_pnl"].sum()) if not trades.empty and "net_pnl" in trades else 0.0
    unrealized_pnl = float(trades.loc[trades["outcome"] == "open", "net_pnl"].sum()) if not trades.empty and "net_pnl" in trades else 0.0
    closed = trades[trades["outcome"] != "open"] if not trades.empty else pd.DataFrame()
    wins = int((closed["net_pnl"] > 0).sum()) if not closed.empty and "net_pnl" in closed else 0
    total_closed = len(closed)
    win_rate = (wins / total_closed) if total_closed else None
    last_updated = max((parsed.summary.last_updated for parsed in data.values() if parsed.summary.last_updated is not None), default=None)
    healthy = all(not parsed.summary.kill_switch_active for parsed in data.values())
    return {
        "healthy": healthy,
        "total_open_positions": open_positions,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_closed_trades": total_closed,
        "win_rate": win_rate,
        "last_updated": last_updated,
    }
