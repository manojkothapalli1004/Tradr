#!/usr/bin/env python3
"""
Research Manager V3 — cross-bot CLI with sub-agent integration.

Reads state files (read-only), runs spot and options analysis,
invokes sub-agents for per-combo/strategy verdicts and exit-health,
aggregates into a single manager recommendation.

CLI modes (all V1/V2-compatible):
  (default)   spot summary  (V1-compatible)
  --spot      spot summary only
  --options   options summary only
  --all       cross-bot manager summary (now with sub-agents)
  --json      JSON output (combines with --spot/--options/--all)
  --full      detailed spot report (with combo verdicts + exit health)
  --verdict   spot verdict JSON (V1-compatible)
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from .approval_gate import ApprovalStatus, ApprovalStore
from .experiment_journal_report import build_experiment_journal_report

# ── Analysis layer ────────────────────────────────────────────────────
from .spot_analysis import AnalysisResult, analyze_spot_state
from .options_analysis import OptionsAnalysisResult, analyze_options_state

# ── Reporting ─────────────────────────────────────────────────────────
from .reporting import (
    format_report,
    format_spot_report,
    format_options_report,
    format_cross_bot_report,
)

# ── Sub-agents ────────────────────────────────────────────────────────
from .agents.spot_strategy_agent import (
    evaluate as spot_agent_evaluate,
    SpotAgentResult,
)
from .agents.options_strategy_agent import (
    evaluate as options_agent_evaluate,
    OptionsAgentResult,
)
from .agents.exit_logic_agent import (
    evaluate_exit_reasons,
    ExitLogicVerdict,
)
from .agents.manager_aggregator import (
    aggregate as aggregator_aggregate,
    ManagerRecommendation,
)

# ── Ops layer ────────────────────────────────────────────────────────
from .ops.runner_health_agent import analyze as analyze_runner_health, RunnerHealthReport
from .ops.incident_agent import analyze as analyze_incidents, IncidentReport as OpsIncidentReport
from .ops.repair_planner_agent import (
    build_repair_plan,
    IncidentSignal,
    HealthSignal,
    RepairPlan,
)
from .ops.ops_aggregator import aggregate_ops, OpsSummary


# ── BotSummary: normalized per-bot output (V2-compat) ────────────────

@dataclass
class BotSummary:
    """Normalized summary any bot analysis module can produce."""
    bot_name: str
    total_trades: int = 0
    active_trades: int = 0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    confidence: str = "LOW"
    best_combo: Optional[str] = None
    weakest_combo: Optional[str] = None
    worth_continuing: str = "not enough data"
    narrow_further: str = "not yet"
    next_action: str = ""
    available: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# ── CrossBotVerdict (V2-compat) ───────────────────────────────────────

@dataclass
class CrossBotVerdict:
    """Aggregated manager view across all bots."""
    spot: BotSummary
    options: BotSummary
    stronger_bot: str
    either_worth_continuing: str
    next_focus: str
    generated: str = ""

    def to_dict(self) -> dict:
        return {
            "spot": self.spot.to_dict(),
            "options": self.options.to_dict(),
            "stronger_bot": self.stronger_bot,
            "either_worth_continuing": self.either_worth_continuing,
            "next_focus": self.next_focus,
            "generated": self.generated,
        }


# ── Per-bot verdict (spot — V1-compat) ────────────────────────────────

@dataclass
class ManagerVerdict:
    worth_continuing: str
    narrow_further: str
    best_combo: Optional[str]
    weakest_combo: Optional[str]
    next_action: str

    def to_dict(self) -> dict:
        return asdict(self)


def derive_verdict(r: AnalysisResult) -> ManagerVerdict:
    if r.total_trades < 5:
        worth = "not enough data"
    elif r.net_pnl > 0 and r.win_rate >= 45:
        worth = "yes"
    elif r.net_pnl > 0:
        worth = "yes"
    elif r.total_trades < 15:
        worth = "not enough data"
    else:
        worth = "no"

    if r.total_trades < 10:
        narrow = "not yet"
    elif r.combo_metrics:
        losers = [c for c in r.combo_metrics.values()
                  if c.net_pnl < 0 and c.trades >= 3]
        narrow = "yes" if losers else "no"
    else:
        narrow = "not yet"

    next_action = _next_action(r, worth, narrow)

    return ManagerVerdict(
        worth_continuing=worth,
        narrow_further=narrow,
        best_combo=r.strongest_combo,
        weakest_combo=r.weakest_combo,
        next_action=next_action,
    )


def _next_action(r: AnalysisResult, worth: str, narrow: str) -> str:
    if r.total_trades == 0:
        return "wait for first trades to complete"
    if worth == "not enough data":
        remaining = max(5 - r.total_trades, 0)
        if remaining > 0:
            return f"collect {remaining} more trades before evaluating"
        return "collect more trades — sample too small for conclusions"
    if worth == "no":
        return "pause the bot and review strategy parameters"
    if narrow == "yes" and r.weakest_combo:
        return f"consider dropping {r.weakest_combo} and reallocating capital"
    if r.sample_confidence == "LOW":
        return "keep running — build toward 30+ trades for reliable stats"
    if r.sample_confidence == "MEDIUM":
        need = max(30 - r.total_trades, 0)
        return f"on track — {need} more trades to HIGH confidence"
    return "system performing — maintain current configuration"


# ── BotSummary bridges (V2-compat) ────────────────────────────────────

def _spot_to_summary(r: AnalysisResult, v: ManagerVerdict) -> BotSummary:
    return BotSummary(
        bot_name="spot",
        total_trades=r.total_trades,
        active_trades=r.active_trades_count,
        net_pnl=r.net_pnl,
        win_rate=r.win_rate,
        confidence=r.sample_confidence,
        best_combo=v.best_combo,
        weakest_combo=v.weakest_combo,
        worth_continuing=v.worth_continuing,
        narrow_further=v.narrow_further,
        next_action=v.next_action,
    )


def _options_to_summary(r: OptionsAnalysisResult) -> BotSummary:
    if r.kill_switch_active:
        worth = "no"
    elif r.total_trades < 5:
        worth = "not enough data"
    elif r.realized_pnl > 0:
        worth = "yes"
    elif r.total_trades < 15:
        worth = "not enough data"
    else:
        worth = "no"

    if r.total_trades < 10:
        narrow = "not yet"
    elif r.strategy_metrics:
        losers = [s for s in r.strategy_metrics.values()
                  if s.realized_pnl < 0 and s.trades >= 3]
        narrow = "yes" if losers else "no"
    else:
        narrow = "not yet"

    if r.total_trades == 0:
        next_act = "wait for first trades to complete"
    elif r.kill_switch_active:
        next_act = "kill switch active — review drawdown"
    elif worth == "not enough data":
        remaining = max(5 - r.total_trades, 0)
        next_act = f"collect {remaining} more trades" if remaining > 0 else "collect more trades"
    elif worth == "no":
        next_act = "pause and review strategy parameters"
    elif r.sample_confidence == "LOW":
        next_act = "keep running — build toward 30+ trades"
    elif r.sample_confidence == "MEDIUM":
        need = max(30 - r.total_trades, 0)
        next_act = f"on track — {need} more trades to HIGH confidence"
    else:
        next_act = "system performing — maintain current configuration"

    return BotSummary(
        bot_name="options",
        total_trades=r.total_trades,
        active_trades=r.active_trades_count,
        net_pnl=r.realized_pnl,
        win_rate=r.win_rate,
        confidence=r.sample_confidence,
        best_combo=r.strongest_strategy,
        weakest_combo=r.weakest_strategy,
        worth_continuing=worth,
        narrow_further=narrow,
        next_action=next_act,
    )


# ── Cross-bot aggregation (V2-compat) ────────────────────────────────

def derive_cross_verdict(spot: BotSummary, options: BotSummary) -> CrossBotVerdict:
    stronger = _compare_bots(spot, options)

    verdicts = [spot.worth_continuing, options.worth_continuing]
    if "yes" in verdicts:
        either = "yes"
    elif all(v == "no" for v in verdicts):
        either = "no"
    else:
        either = "not enough data"

    next_focus = _decide_focus(spot, options, stronger)

    return CrossBotVerdict(
        spot=spot, options=options,
        stronger_bot=stronger,
        either_worth_continuing=either,
        next_focus=next_focus,
        generated=datetime.now(timezone.utc).isoformat(),
    )


def _compare_bots(spot: BotSummary, options: BotSummary) -> str:
    if not spot.available and not options.available:
        return "neither"
    if not spot.available:
        return "options"
    if not options.available:
        return "spot"
    if spot.total_trades < 5 and options.total_trades < 5:
        return "tied"
    if spot.total_trades >= 5 and options.total_trades < 5:
        return "spot" if spot.net_pnl > 0 else "tied"
    if options.total_trades >= 5 and spot.total_trades < 5:
        return "options" if options.net_pnl > 0 else "tied"
    if spot.net_pnl > 0 and options.net_pnl <= 0:
        return "spot"
    if options.net_pnl > 0 and spot.net_pnl <= 0:
        return "options"
    if spot.net_pnl > 0 and options.net_pnl > 0:
        if spot.net_pnl > options.net_pnl * 1.2:
            return "spot"
        if options.net_pnl > spot.net_pnl * 1.2:
            return "options"
        return "tied"
    return "tied"


def _decide_focus(spot: BotSummary, options: BotSummary, stronger: str) -> str:
    if spot.total_trades == 0 and options.total_trades > 0:
        return "spot — no trades yet, needs runtime"
    if options.total_trades == 0 and spot.total_trades > 0:
        return "options — no trades yet, needs runtime"
    if spot.total_trades == 0 and options.total_trades == 0:
        return "both — neither has trades yet"
    if stronger == "spot" and spot.worth_continuing == "yes":
        return "spot — strongest performer, grow the edge"
    if stronger == "options" and options.worth_continuing == "yes":
        return "options — strongest performer, grow the edge"
    if spot.confidence == "LOW" and options.confidence == "LOW":
        return "both — too early, keep both running"
    if spot.confidence == "LOW" and options.confidence != "LOW":
        return "spot — needs more data to evaluate"
    if options.confidence == "LOW" and spot.confidence != "LOW":
        return "options — needs more data to evaluate"
    if spot.worth_continuing == "yes" and options.worth_continuing == "yes":
        if stronger == "spot":
            return "options — spot is stable, improve options"
        return "spot — options is stable, improve spot"
    return "review both — no clear winner yet"


# ── ResearchManager ──────────────────────────────────────────────────

class ResearchManager:
    """
    Orchestrates analysis modules, sub-agents, and aggregator.

    V3: spot + options analysis + sub-agents + aggregator.
    All V1/V2 methods preserved.
    """

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.base_dir = base_dir
        self.spot_state_file = os.path.join(base_dir, "paper_trading_state.json")
        # Prefer the live options runner state. Fall back to the legacy
        # `options_bot/state.json` only if the live file is missing — that
        # file is fully simulator-tagged and gets filtered downstream.
        live_options = os.path.join(base_dir, "options_trading_state.json")
        legacy_options = os.path.join(base_dir, "options_bot", "state.json")
        self.options_state_file = live_options if os.path.exists(live_options) else legacy_options
        self.approval_store_file = os.path.join(base_dir, "research_manager", "approval_store.json")
        if not os.path.exists(self.approval_store_file):
            self.approval_store_file = os.path.join(base_dir, "approval_store.json")
        if not os.path.exists(self.approval_store_file):
            self.approval_store_file = ""

    def load_approval_store(self) -> ApprovalStore:
        if not self.approval_store_file or not os.path.exists(self.approval_store_file):
            return ApprovalStore()
        return ApprovalStore.from_dict(self._load_json(self.approval_store_file))

    def get_approval_summary(self) -> dict:
        store = self.load_approval_store()
        pending = [proposal.to_dict() for proposal in store.list_by_status(ApprovalStatus.PENDING)]
        approved = [proposal.to_dict() for proposal in store.list_by_status(ApprovalStatus.APPROVED)]
        rejected = [proposal.to_dict() for proposal in store.list_by_status(ApprovalStatus.REJECTED)]
        expired = [proposal.to_dict() for proposal in store.list_by_status(ApprovalStatus.EXPIRED)]
        return {
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
            "expired": expired,
            "counts": {
                "pending": len(pending),
                "approved": len(approved),
                "rejected": len(rejected),
                "expired": len(expired),
            },
        }

    def get_experiment_summary(self) -> dict:
        report = build_experiment_journal_report(os.path.join(self.base_dir, "research_manager", "experiment_journal.json"))
        return report.to_dict()

    def get_proposal_summary(self) -> dict:
        proposals_path = os.path.join(self.base_dir, "research_manager", "change_proposals.json")
        if not os.path.exists(proposals_path):
            return {"proposals": [], "summary": "No proposals available."}
        return self._load_json(proposals_path)

    def get_operator_summary(self) -> dict:
        experiments = self.get_experiment_summary()
        proposals = self.get_proposal_summary()
        return {
            "experiment_journal": experiments,
            "active_experiment": experiments.get("active_experiment"),
            "recent_experiments": experiments.get("verdict_history", [])[:5],
            "proposals": proposals.get("proposals", []),
            "proposal_summary": proposals.get("summary", ""),
        }
    # ── State loading (read-only) ────────────────────────────────

    # ── State loading (read-only) ────────────────────────────────

    def _load_json(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f)

    def load_spot_state(self) -> dict:
        return self._load_json(self.spot_state_file)

    def load_options_state(self) -> dict:
        return self._load_json(self.options_state_file)

    # ── Analysis ──────────────────────────────────────────────────

    def run_analysis(self) -> AnalysisResult:
        return analyze_spot_state(self.load_spot_state())

    def run_options_analysis(self) -> Optional[OptionsAnalysisResult]:
        state = self.load_options_state()
        if not state:
            return None
        return analyze_options_state(state)

    # ── Sub-agents ────────────────────────────────────────────────

    def run_spot_agent(self, analysis: Optional[AnalysisResult] = None) -> SpotAgentResult:
        if analysis is None:
            analysis = self.run_analysis()
        return spot_agent_evaluate(analysis)

    def run_options_agent(self, analysis: Optional[OptionsAnalysisResult] = None) -> Optional[OptionsAgentResult]:
        if analysis is None:
            analysis = self.run_options_analysis()
        if analysis is None:
            return None
        return options_agent_evaluate(analysis)

    def run_exit_verdict(self, analysis: AnalysisResult, bot: str = "spot") -> ExitLogicVerdict:
        return evaluate_exit_reasons(analysis.exit_reason_breakdown, bot)

    def run_options_exit_verdict(self, analysis: Optional[OptionsAnalysisResult] = None) -> Optional[ExitLogicVerdict]:
        if analysis is None:
            analysis = self.run_options_analysis()
        if analysis is None:
            return None
        return evaluate_exit_reasons(analysis.exit_reason_breakdown, "options")

    # ── V1-compat spot verdicts ───────────────────────────────────

    def run_verdict(self) -> ManagerVerdict:
        return derive_verdict(self.run_analysis())

    # ── V2-compat spot/options summaries ──────────────────────────

    def run_spot_bot_summary(self) -> BotSummary:
        state = self.load_spot_state()
        if not state:
            return BotSummary(bot_name="spot", available=False,
                              next_action="state file not found")
        result = analyze_spot_state(state)
        verdict = derive_verdict(result)
        return _spot_to_summary(result, verdict)

    def run_options_summary(self) -> BotSummary:
        result = self.run_options_analysis()
        if result is None:
            return BotSummary(bot_name="options", available=False,
                              next_action="state file not found")
        return _options_to_summary(result)

    def run_cross_summary(self) -> CrossBotVerdict:
        spot = self.run_spot_bot_summary()
        options = self.run_options_summary()
        return derive_cross_verdict(spot, options)

    # ── Ops layer ────────────────────────────────────────────────

    def run_runner_health(self) -> RunnerHealthReport:
        return analyze_runner_health(self.base_dir)

    def run_ops_incidents(self) -> OpsIncidentReport:
        return analyze_incidents(self.base_dir)

    def run_repair_plan(
        self,
        runner_health: Optional[RunnerHealthReport] = None,
        incident_report: Optional[OpsIncidentReport] = None,
    ) -> RepairPlan:
        if runner_health is None:
            runner_health = self.run_runner_health()
        if incident_report is None:
            incident_report = self.run_ops_incidents()

        incidents: list[IncidentSignal] = []
        primary = incident_report.primary_incident
        if primary and primary.incident_type != "none":
            incidents.append(IncidentSignal(
                code=primary.incident_type,
                severity=primary.severity,
                summary=primary.evidence_summary,
                blocker=primary.severity == "critical",
            ))

        health = HealthSignal(
            overall_health=runner_health.overall_health,
            runner_count=sum(1 for r in runner_health.runners.values() if r.process_present),
            market_open_expected=None,
            has_recent_data=all(
                (r.state_age_minutes is not None and r.state_age_minutes <= 20)
                for r in runner_health.runners.values()
                if r.state_file_present
            ) if runner_health.runners else None,
        )
        return build_repair_plan(incidents, health)

    def run_ops_summary(self) -> OpsSummary:
        runner_health = self.run_runner_health()
        incidents = self.run_ops_incidents()
        repair_plan = self.run_repair_plan(runner_health, incidents)
        return aggregate_ops(runner_health, incidents, repair_plan)

    # ── V3 enriched reports ───────────────────────────────────────

    def run_report(self) -> str:
        """Full spot report with sub-agent enrichment."""
        analysis = self.run_analysis()
        agent = self.run_spot_agent(analysis)
        exit_v = self.run_exit_verdict(analysis, "spot")
        return format_spot_report(analysis, agent=agent, exit_verdict=exit_v)

    def run_options_report(self) -> str:
        """Full options report with sub-agent enrichment."""
        analysis = self.run_options_analysis()
        if analysis is None:
            return "[OPTIONS] state file not found — nothing to report."
        agent = self.run_options_agent(analysis)
        exit_v = evaluate_exit_reasons(analysis.exit_reason_breakdown, "options")
        return format_options_report(analysis, agent=agent, exit_verdict=exit_v)

    def run_cross_report(self) -> str:
        """Full cross-bot report with aggregator + exit health."""
        spot_analysis = self.run_analysis() if self.load_spot_state() else None
        opts_analysis = self.run_options_analysis()

        spot_exit = evaluate_exit_reasons(
            spot_analysis.exit_reason_breakdown, "spot"
        ) if spot_analysis else None
        opts_exit = evaluate_exit_reasons(
            opts_analysis.exit_reason_breakdown, "options"
        ) if opts_analysis else None

        agg = aggregator_aggregate(
            spot=spot_analysis,
            options=opts_analysis,
            spot_exit=spot_exit,
            options_exit=opts_exit,
        )
        ops = self.run_ops_summary()
        approvals = self.get_approval_summary()
        operator_summary = self.get_operator_summary()

        return format_cross_bot_report(
            spot_analysis, opts_analysis,
            agg=agg,
            spot_exit=spot_exit,
            options_exit=opts_exit,
            ops=ops,
            approvals=approvals,
            operator_summary=operator_summary,
        )
    # ── Spot compact summary formatter (V1-compatible default) ─────

    def run_summary(self) -> str:
        """Concise spot summary (V1-compatible default)."""
        result = self.run_analysis()
        verdict = derive_verdict(result)
        return _format_spot_summary(result, verdict)


# ── Spot compact summary formatter (V1-compat) ───────────────────────

def _format_spot_summary(r: AnalysisResult, v: ManagerVerdict) -> str:
    w = 56
    lines = [
        "=" * w,
        "  RESEARCH MANAGER — SPOT SUMMARY",
        "=" * w,
        "",
        f"  Trades:          {r.total_trades} completed, {r.active_trades_count} active",
        f"  Net P&L:         ${r.net_pnl:.2f}",
        f"  Win Rate:        {r.win_rate:.1f}%",
        f"  Confidence:      {r.sample_confidence}",
        "",
        "-" * w,
        "  VERDICT",
        "-" * w,
        f"  Worth continuing:  {v.worth_continuing}",
        f"  Narrow further:    {v.narrow_further}",
        f"  Best combo:        {v.best_combo or 'N/A'}",
        f"  Weakest combo:     {v.weakest_combo or 'N/A'}",
        f"  Next action:       {v.next_action}",
        "",
        f"  Generated: {r.analysis_time}",
        "=" * w,
    ]
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mgr = ResearchManager(base_dir)
    args = sys.argv[1:]
    as_json = "--json" in args
    mode = "spot"

    if "--all" in args:
        mode = "all"
    elif "--options" in args:
        mode = "options"
    elif "--spot" in args:
        mode = "spot"
    elif "--full" in args:
        print(mgr.run_report())
        return
    elif "--verdict" in args:
        print(json.dumps(mgr.run_verdict().to_dict(), indent=2))
        return

    ops = mgr.run_ops_summary()
    operator_summary = mgr.get_operator_summary()

    if mode == "all":
        if as_json:
            spot_a = mgr.run_analysis() if mgr.load_spot_state() else None
            opts_a = mgr.run_options_analysis()
            spot_exit = evaluate_exit_reasons(
                spot_a.exit_reason_breakdown, "spot"
            ) if spot_a else None
            opts_exit = evaluate_exit_reasons(
                opts_a.exit_reason_breakdown, "options"
            ) if opts_a else None
            agg = aggregator_aggregate(spot_a, opts_a, spot_exit, opts_exit)
            cv = mgr.run_cross_summary()
            output = cv.to_dict()
            output["aggregator"] = agg.to_dict()
            output["ops"] = ops.to_dict()
            output["approvals"] = mgr.get_approval_summary()
            output["experiment_journal"] = operator_summary.get("experiment_journal")
            output["active_experiment"] = operator_summary.get("active_experiment")
            output["recent_experiments"] = operator_summary.get("recent_experiments")
            output["proposals"] = operator_summary.get("proposals")
            if spot_exit:
                output["spot_exit_health"] = spot_exit.to_dict()
            if opts_exit:
                output["options_exit_health"] = opts_exit.to_dict()
            print(json.dumps(output, indent=2))
        else:
            print(mgr.run_cross_report())

    elif mode == "options":
        if as_json:
            result = mgr.run_options_analysis()
            if result is None:
                print(json.dumps({
                    "error": "options state file not found",
                    "ops": ops.to_dict(),
                    "approvals": mgr.get_approval_summary(),
                    "experiment_journal": operator_summary.get("experiment_journal"),
                    "active_experiment": operator_summary.get("active_experiment"),
                    "recent_experiments": operator_summary.get("recent_experiments"),
                    "proposals": operator_summary.get("proposals"),
                }))
            else:
                agent = mgr.run_options_agent(result)
                exit_v = evaluate_exit_reasons(result.exit_reason_breakdown, "options")
                output = result.to_dict()
                if agent:
                    output["agent"] = agent.to_dict()
                output["exit_health"] = exit_v.to_dict()
                output["ops"] = ops.to_dict()
                output["approvals"] = mgr.get_approval_summary()
                output["experiment_journal"] = operator_summary.get("experiment_journal")
                output["active_experiment"] = operator_summary.get("active_experiment")
                output["recent_experiments"] = operator_summary.get("recent_experiments")
                output["proposals"] = operator_summary.get("proposals")
                print(json.dumps(output, indent=2))
        else:
            print(mgr.run_options_report())

    else:  # spot
        if as_json:
            result = mgr.run_analysis()
            verdict = derive_verdict(result)
            agent = mgr.run_spot_agent(result)
            exit_v = mgr.run_exit_verdict(result, "spot")
            print(json.dumps({
                "analysis": result.to_dict(),
                "verdict": verdict.to_dict(),
                "agent": agent.to_dict(),
                "exit_health": exit_v.to_dict(),
                "ops": ops.to_dict(),
                "approvals": mgr.get_approval_summary(),
                "experiment_journal": operator_summary.get("experiment_journal"),
                "active_experiment": operator_summary.get("active_experiment"),
                "recent_experiments": operator_summary.get("recent_experiments"),
                "proposals": operator_summary.get("proposals"),
            }, indent=2))
        else:
            print(mgr.run_summary())

if __name__ == "__main__":
    main()
