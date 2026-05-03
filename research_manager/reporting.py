"""
Report formatters for Research Manager V3.

Four report modes:
  - spot:     format_spot_report(AnalysisResult, spot_agent?, exit_verdict?)
  - options:  format_options_report(OptionsAnalysisResult, opts_agent?, exit_verdict?)
  - summary:  format_cross_bot_report(spot?, options?, aggregator?)
  - compat:   all sub-agent params are Optional — omit them and output is V2-identical

All formatters are pure functions: typed input -> string output.
"""
from __future__ import annotations

from typing import Optional

from .spot_analysis import AnalysisResult, ComboMetrics
from .options_analysis import OptionsAnalysisResult, StrategyMetrics, SymbolMetrics
from .agents.spot_strategy_agent import SpotAgentResult
from .agents.options_strategy_agent import OptionsAgentResult
from .agents.exit_logic_agent import ExitLogicVerdict
from .agents.manager_aggregator import ManagerRecommendation
from .ops.ops_aggregator import OpsSummary


def _format_operator_lines(operator_summary: Optional[dict], approvals: Optional[dict]) -> list[str]:
    lines: list[str] = []

    if operator_summary and isinstance(operator_summary, dict):
        active = operator_summary.get("active_experiment")
        recent = operator_summary.get("recent_experiments", []) or []
        proposals = operator_summary.get("proposals", []) or []
        proposal_summary = operator_summary.get("proposal_summary", "")
        journal = operator_summary.get("experiment_journal", {}) or {}

        lines.extend(["EXPERIMENT JOURNAL", _hr()])
        if active:
            lines.append(f"  Active:    {active.get('experiment_id', '')} [{active.get('status', '')}] {active.get('parameter_changed', '')}")
        else:
            lines.append("  Active:    none")

        latest_completed = journal.get("most_recent_completed_experiment")
        if latest_completed:
            lines.append(f"  Completed: {latest_completed.get('experiment_id', '')} [{latest_completed.get('verdict', '') or latest_completed.get('status', '')}]")
        else:
            lines.append("  Completed: none")

        reverted = journal.get("reverted_experiments", []) or []
        abandoned = [r for r in recent if isinstance(r, dict) and r.get("status") == "abandoned"]
        latest_rev = reverted[0] if reverted else (abandoned[0] if abandoned else None)
        if latest_rev:
            lines.append(f"  Reverted:  {latest_rev.get('experiment_id', '')} [{latest_rev.get('status', '')}]")
        else:
            lines.append("  Reverted:  none")

        summary = journal.get("summary", "")
        if summary:
            lines.append(f"  Summary:   {summary}")
        lines.append("")

        lines.extend(["PROPOSED NEXT CHANGES", _hr()])
        if proposals:
            for item in proposals[:5]:
                proposal_id = item.get("proposal_id", "")
                title = item.get("title", item.get("proposed_change", ""))
                status = item.get("status", "pending")
                lines.append(f"  - {proposal_id}: {title} [{status}]")
        else:
            lines.append(f"  {proposal_summary or 'No proposals available.'}")
        lines.append("")

    if approvals and isinstance(approvals, dict):
        counts = approvals.get("counts", {})
        pending = approvals.get("pending", [])
        approved = approvals.get("approved", [])
        rejected = approvals.get("rejected", [])
        expired = approvals.get("expired", [])

        lines.extend(["PROPOSAL STATUS", _hr()])
        lines.append(f"  Pending:   {counts.get('pending', len(pending))}")
        lines.append(f"  Approved:  {counts.get('approved', len(approved))}")
        lines.append(f"  Rejected:  {counts.get('rejected', len(rejected))}")
        if expired:
            lines.append(f"  Expired:   {counts.get('expired', len(expired))}")
        lines.append("")

    return lines

# ── Helpers ──────────────────────────────────────────────────────────────────

W = 70


def _hr(char: str = "-") -> str:
    return char * W


def _derive_next_action_spot(r: AnalysisResult) -> str:
    if r.total_trades < 5:
        return "Continue running — need 5+ trades before any assessment."
    if r.total_trades < 10:
        return "Collect more data — at least 10 trades needed to evaluate combos."
    if r.sample_confidence == "LOW":
        return "Keep running all combos until 30+ trades for reliable narrowing."

    losers = [c for c in r.combo_metrics.values() if c.net_pnl < 0 and c.trades >= 3]
    winners = [c for c in r.combo_metrics.values() if c.net_pnl > 0 and c.trades >= 3]

    if losers and r.total_trades >= 30:
        worst = min(losers, key=lambda c: c.net_pnl)
        return f"Drop {worst.combo} (worst P&L), reallocate capital to top performers."
    if winners and not losers:
        return "All tested combos profitable — continue running, increase observation window."
    if r.net_pnl < 0:
        return "Review exit logic and fee impact — negative P&L despite trades completing."
    return "Continue collecting data — no clear action until sample confidence is HIGH."


def _derive_next_action_options(r: OptionsAnalysisResult) -> str:
    if r.total_trades < 5:
        return "Continue running — need 5+ trades before any assessment."
    if r.kill_switch_active:
        return "Kill switch active — investigate drawdown before resuming."
    if r.total_trades < 10:
        return "Collect more data — at least 10 trades needed to evaluate strategies."
    if r.sample_confidence == "LOW":
        return "Keep running all strategies until 30+ trades for reliable narrowing."

    losers = [s for s in r.strategy_metrics.values() if s.realized_pnl < 0 and s.trades >= 3]
    if losers and r.total_trades >= 30:
        worst = min(losers, key=lambda s: s.realized_pnl)
        return f"Drop {worst.strategy} (worst P&L), reallocate to top strategies."
    if r.drawdown_pct > 10:
        return f"Drawdown at {r.drawdown_pct:.1f}% — review position sizing and stop logic."
    if r.realized_pnl < 0:
        return "Review exit timing and premium decay — negative realized P&L."
    return "Continue collecting data — no clear action until sample confidence is HIGH."


# ── Spot report ──────────────────────────────────────────────────────────────

def format_spot_report(
    r: AnalysisResult,
    agent: Optional[SpotAgentResult] = None,
    exit_verdict: Optional[ExitLogicVerdict] = None,
) -> str:
    lines: list[str] = []

    lines.append("=" * W)
    lines.append("  RESEARCH MANAGER — SPOT ANALYSIS")
    lines.append("=" * W)
    lines.append("")

    lines.append("SUMMARY")
    lines.append(_hr())
    lines.append(f"  Total Trades:       {r.total_trades:>10d}")
    lines.append(f"  Active Trades:      {r.active_trades_count:>10d}")
    lines.append(f"  Net P&L:           ${r.net_pnl:>10.2f}")
    lines.append(f"  Fees Paid:         ${r.fees_paid:>10.2f}")
    lines.append(f"  Portfolio Value:    ${r.portfolio_value:>10.2f}")
    lines.append(f"  Win Rate:           {r.win_rate:>9.1f}%")
    lines.append(f"  Avg Hold:           {r.avg_hold_minutes:>9.1f}m")
    lines.append(f"  Runtime:            {r.runtime_hours:>9.1f}h")
    lines.append(f"  Confidence:         {r.sample_confidence:>10s}")
    lines.append("")

    lines.append("EXIT REASON BREAKDOWN")
    lines.append(_hr())
    if r.exit_reason_breakdown:
        for reason, count in sorted(r.exit_reason_breakdown.items(), key=lambda x: -x[1]):
            pct = (count / r.total_trades * 100) if r.total_trades > 0 else 0
            lines.append(f"  {reason:<20s} {count:>4d}  ({pct:5.1f}%)")
    else:
        lines.append("  No completed trades yet.")
    lines.append("")

    # exit health from exit logic agent
    if exit_verdict:
        lines.append(f"  Exit Health:  {exit_verdict.rating.upper()}")
        lines.append(f"  Assessment:   {exit_verdict.reason}")
        lines.append("")

    lines.append("P&L BY COMBO")
    lines.append(_hr())
    lines.append(f"  {'Combo':<28s} {'Trades':>6s} {'W/L':>7s} {'Win%':>6s} {'Net P&L':>10s} {'Fees':>8s} {'Hold':>6s}")
    lines.append(f"  {'─' * 28} {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 10} {'─' * 8} {'─' * 6}")
    if r.combo_metrics:
        for combo in sorted(r.combo_metrics.values(), key=lambda c: c.net_pnl, reverse=True):
            wl = f"{combo.wins}/{combo.losses}"
            lines.append(
                f"  {combo.combo:<28s} {combo.trades:>6d} {wl:>7s} {combo.win_rate:>5.1f}%"
                f" ${combo.net_pnl:>9.2f} ${combo.fees:>7.2f} {combo.avg_hold_minutes:>5.1f}m"
            )
    else:
        lines.append("  No completed trades yet.")
    lines.append("")

    lines.append("RANKINGS")
    lines.append(_hr())
    lines.append(f"  Strongest: {r.strongest_combo or 'N/A'}")
    lines.append(f"  Weakest:   {r.weakest_combo or 'N/A'}")
    lines.append("")

    # combo verdicts from spot strategy agent
    if agent and agent.combo_verdicts:
        lines.append("COMBO VERDICTS")
        lines.append(_hr())
        for cv in sorted(agent.combo_verdicts.values(), key=lambda v: v.net_pnl, reverse=True):
            tag = cv.recommendation.upper().rjust(5)
            lines.append(f"  [{tag}]  {cv.combo}")
            lines.append(f"          {cv.reason}")
        lines.append("")

    lines.append("RECOMMENDATIONS")
    lines.append(_hr())
    lines.append(f"  Worth continuing?  {r.continue_recommendation}")
    lines.append(f"  Narrow further?    {r.narrow_recommendation}")
    lines.append(f"  Next action:       {_derive_next_action_spot(r)}")
    lines.append("")

    if r.sample_confidence == "LOW":
        lines.append("* WARNING: Sample size too small for reliable conclusions.")
        lines.append("  All recommendations are preliminary. Wait for 30+ trades.")
        lines.append("")

    lines.append(f"Generated: {r.analysis_time}")
    lines.append("=" * W)

    return "\n".join(lines)


# V1 compat alias
format_report = format_spot_report


# ── Options report ───────────────────────────────────────────────────────────

def format_options_report(
    r: OptionsAnalysisResult,
    agent: Optional[OptionsAgentResult] = None,
    exit_verdict: Optional[ExitLogicVerdict] = None,
) -> str:
    lines: list[str] = []

    lines.append("=" * W)
    lines.append("  RESEARCH MANAGER — OPTIONS ANALYSIS")
    lines.append("=" * W)
    lines.append("")

    lines.append("SUMMARY")
    lines.append(_hr())
    lines.append(f"  Total Trades:       {r.total_trades:>10d}")
    lines.append(f"  Open Trades:        {r.active_trades_count:>10d}")
    lines.append(f"  Realized P&L:      ${r.realized_pnl:>10.2f}")
    lines.append(f"  Unrealized P&L:    ${r.unrealized_pnl:>10.2f}")
    lines.append(f"  Premium Deployed:  ${r.premium_deployed:>10.2f}")
    lines.append(f"  Fees Paid:         ${r.fees_paid:>10.2f}")
    lines.append(f"  Win Rate:           {r.win_rate:>9.1f}%")
    lines.append(f"  Avg Hold:           {r.avg_hold_days:>9.3f}d")
    lines.append(f"  Drawdown:           {r.drawdown_pct:>9.1f}%")
    lines.append(f"  Kill Switch:        {'ACTIVE' if r.kill_switch_active else 'off':>10s}")
    lines.append(f"  Runtime:            {r.runtime_hours:>9.1f}h")
    lines.append(f"  Confidence:         {r.sample_confidence:>10s}")
    lines.append("")

    lines.append("EXIT REASON BREAKDOWN")
    lines.append(_hr())
    if r.exit_reason_breakdown:
        for reason, count in sorted(r.exit_reason_breakdown.items(), key=lambda x: -x[1]):
            pct = (count / r.total_trades * 100) if r.total_trades > 0 else 0
            lines.append(f"  {reason:<20s} {count:>4d}  ({pct:5.1f}%)")
    else:
        lines.append("  No completed trades yet.")
    lines.append("")

    if exit_verdict:
        lines.append(f"  Exit Health:  {exit_verdict.rating.upper()}")
        lines.append(f"  Assessment:   {exit_verdict.reason}")
        lines.append("")

    lines.append("P&L BY STRATEGY")
    lines.append(_hr())
    lines.append(f"  {'Strategy':<30s} {'Trades':>6s} {'W/L':>7s} {'Win%':>6s} {'P&L':>10s} {'Fees':>8s} {'Hold':>6s}")
    lines.append(f"  {'─' * 30} {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 10} {'─' * 8} {'─' * 6}")
    if r.strategy_metrics:
        for sm in sorted(r.strategy_metrics.values(), key=lambda s: s.realized_pnl, reverse=True):
            wl = f"{sm.wins}/{sm.losses}"
            lines.append(
                f"  {sm.strategy:<30s} {sm.trades:>6d} {wl:>7s} {sm.win_rate:>5.1f}%"
                f" ${sm.realized_pnl:>9.2f} ${sm.fees:>7.2f} {sm.avg_hold_days:>5.3f}d"
            )
    else:
        lines.append("  No completed trades yet.")
    lines.append("")

    if r.symbol_metrics:
        lines.append("P&L BY SYMBOL")
        lines.append(_hr())
        lines.append(f"  {'Symbol':<10s} {'Trades':>6s} {'W/L':>7s} {'Win%':>6s} {'P&L':>10s} {'Fees':>8s} {'Hold':>6s}")
        lines.append(f"  {'─' * 10} {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 10} {'─' * 8} {'─' * 6}")
        for ym in sorted(r.symbol_metrics.values(), key=lambda s: s.realized_pnl, reverse=True):
            wl = f"{ym.wins}/{ym.losses}"
            lines.append(
                f"  {ym.symbol:<10s} {ym.trades:>6d} {wl:>7s} {ym.win_rate:>5.1f}%"
                f" ${ym.realized_pnl:>9.2f} ${ym.fees:>7.2f} {ym.avg_hold_days:>5.3f}d"
            )
        lines.append("")

    lines.append("RANKINGS")
    lines.append(_hr())
    lines.append(f"  Strongest: {r.strongest_strategy or 'N/A'}")
    lines.append(f"  Weakest:   {r.weakest_strategy or 'N/A'}")
    lines.append("")

    # strategy verdicts from options strategy agent
    if agent and agent.strategy_recommendations:
        lines.append("STRATEGY VERDICTS")
        lines.append(_hr())
        for rec in agent.strategy_recommendations:
            tag = rec.action.upper().rjust(7)
            lines.append(f"  [{tag}]  {rec.strategy}")
            lines.append(f"           {rec.reason}")
        lines.append("")

    lines.append("RECOMMENDATIONS")
    lines.append(_hr())
    lines.append(f"  Worth continuing?  {r.continue_recommendation}")
    lines.append(f"  Narrow further?    {r.narrow_recommendation}")
    lines.append(f"  Next action:       {_derive_next_action_options(r)}")
    lines.append("")

    if r.sample_confidence == "LOW":
        lines.append("* WARNING: Sample size too small for reliable conclusions.")
        lines.append("  All recommendations are preliminary. Wait for 30+ trades.")
        lines.append("")
    if r.kill_switch_active:
        lines.append("* KILL SWITCH ACTIVE — no new trades will be opened.")
        lines.append("")

    lines.append(f"Generated: {r.analysis_time}")
    lines.append("=" * W)

    return "\n".join(lines)


# ── Cross-bot summary ───────────────────────────────────────────────────────

def format_cross_bot_report(
    spot: Optional[AnalysisResult],
    options: Optional[OptionsAnalysisResult],
    agg: Optional[ManagerRecommendation] = None,
    spot_exit: Optional[ExitLogicVerdict] = None,
    options_exit: Optional[ExitLogicVerdict] = None,
    ops: Optional[OpsSummary] = None,
    approvals: Optional[dict] = None,
    operator_summary: Optional[dict] = None,
) -> str:
    lines: list[str] = []

    lines.append("=" * W)
    lines.append("  RESEARCH MANAGER — CROSS-BOT SUMMARY")
    lines.append("=" * W)
    lines.append("")

    # ── Per-bot rows ─────────────────────────────────────────────────
    lines.append("BOT OVERVIEW")
    lines.append(_hr())
    lines.append(f"  {'Bot':<12s} {'Trades':>7s} {'Active':>7s} {'Net P&L':>10s} {'Win%':>6s} {'Confidence':>11s}")
    lines.append(f"  {'─' * 12} {'─' * 7} {'─' * 7} {'─' * 10} {'─' * 6} {'─' * 11}")

    combined_pnl = 0.0
    combined_trades = 0
    combined_active = 0

    if spot:
        lines.append(
            f"  {'Spot':<12s} {spot.total_trades:>7d} {spot.active_trades_count:>7d}"
            f" ${spot.net_pnl:>9.2f} {spot.win_rate:>5.1f}% {spot.sample_confidence:>11s}"
        )
        combined_pnl += spot.net_pnl
        combined_trades += spot.total_trades
        combined_active += spot.active_trades_count
    else:
        lines.append(f"  {'Spot':<12s} {'—':>7s} {'—':>7s} {'—':>10s} {'—':>6s} {'not running':>11s}")

    if options:
        lines.append(
            f"  {'Options':<12s} {options.total_trades:>7d} {options.active_trades_count:>7d}"
            f" ${options.realized_pnl:>9.2f} {options.win_rate:>5.1f}% {options.sample_confidence:>11s}"
        )
        combined_pnl += options.realized_pnl
        combined_trades += options.total_trades
        combined_active += options.active_trades_count
    else:
        lines.append(f"  {'Options':<12s} {'—':>7s} {'—':>7s} {'—':>10s} {'—':>6s} {'not running':>11s}")

    lines.append(f"  {'─' * 12} {'─' * 7} {'─' * 7} {'─' * 10} {'─' * 6} {'─' * 11}")
    lines.append(f"  {'TOTAL':<12s} {combined_trades:>7d} {combined_active:>7d} ${combined_pnl:>9.2f}")
    lines.append("")

    # ── Exit health ──────────────────────────────────────────────────
    if spot_exit or options_exit:
        lines.append("EXIT HEALTH")
        lines.append(_hr())
        if spot_exit:
            lines.append(f"  Spot:     {spot_exit.rating.upper()} — {spot_exit.reason}")
        if options_exit:
            lines.append(f"  Options:  {options_exit.rating.upper()} — {options_exit.reason}")
        lines.append("")

    # ── Strongest / weakest across bots ──────────────────────────────
    if agg:
        lines.append("STRONGEST / WEAKEST PATH")
        lines.append(_hr())
        lines.append(f"  Best:  {agg.strongest_path or 'N/A'}")
        lines.append(f"  Worst: {agg.weakest_path or 'N/A'}")
        lines.append("")

        if agg.risk_alerts:
            lines.append("RISK ALERTS")
            lines.append(_hr())
            for alert in agg.risk_alerts:
                lines.append(f"  ! {alert}")
            lines.append("")

        # path scores table
        if agg.path_scores:
            lines.append("PATH SCORES")
            lines.append(_hr())
            lines.append(f"  {'Path':<36s} {'Trades':>6s} {'P&L':>10s} {'Win%':>6s} {'Exits':>10s}")
            lines.append(f"  {'─' * 36} {'─' * 6} {'─' * 10} {'─' * 6} {'─' * 10}")
            for p in agg.path_scores:
                lines.append(
                    f"  {p.label:<36s} {p.trades:>6d} ${p.net_pnl:>9.2f}"
                    f" {p.win_rate:>5.1f}% {p.exit_health or '—':>10s}"
                )
            lines.append("")

        lines.append("MANAGER RECOMMENDATION")
        lines.append(_hr())
        lines.append(f"  Worth continuing:  {agg.worth_continuing}")
        lines.append(f"  Narrow further:    {agg.narrow_further}")
        lines.append(f"  Confidence:        {agg.confidence}")
        lines.append(f"  Next action:       {agg.next_action}")
        lines.append("")

        if ops:
            lines.append("OPS STATUS")
            lines.append(_hr())
            lines.append(f"  Runtime health:    {ops.overall_health}")
            lines.append(f"  Blocker present:   {'yes' if ops.blocker_present else 'no'}")
            lines.append(f"  Approval required: {'yes' if ops.approval_required else 'no'}")
            lines.append(f"  Incident summary:  {ops.incident_status.summary}")
            lines.append(f"  Next action:       {ops.recommended_next_action}")
            if ops.reasons:
                lines.append(f"  Evidence tags:     {', '.join(ops.reasons)}")
    else:
        # V2-compat fallback
        lines.append("BEST & WORST ACROSS BOTS")
        lines.append(_hr())
        best_label = "N/A"
        worst_label = "N/A"
        candidates: list[tuple[str, float]] = []
        if spot and spot.combo_metrics:
            for cm in spot.combo_metrics.values():
                candidates.append((f"spot:{cm.combo}", cm.net_pnl))
        if options and options.strategy_metrics:
            for sm in options.strategy_metrics.values():
                candidates.append((f"options:{sm.strategy}", sm.realized_pnl))
        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            best_label = f"{candidates[0][0]} (${candidates[0][1]:+.2f})"
            worst_label = f"{candidates[-1][0]} (${candidates[-1][1]:+.2f})"
        lines.append(f"  Best:  {best_label}")
        lines.append(f"  Worst: {worst_label}")
        lines.append("")

        if options and options.kill_switch_active:
            lines.append("RISK ALERTS")
            lines.append(_hr())
            lines.append("  Options kill switch ACTIVE — no new options trades.")
            lines.append("")

        lines.append("OVERALL RECOMMENDATION")
        lines.append(_hr())
        if combined_trades < 10:
            lines.append("  Both bots need more data. Continue running, check back after 30+ combined trades.")
        elif combined_pnl > 0:
            lines.append(f"  Combined P&L positive (${combined_pnl:+.2f}). Continue current allocation.")
            if spot and options:
                if spot.net_pnl > 0 and options.realized_pnl <= 0:
                    lines.append("  Spot outperforming options — consider shifting capital toward spot combos.")
                elif options.realized_pnl > 0 and spot.net_pnl <= 0:
                    lines.append("  Options outperforming spot — consider shifting capital toward options strategies.")
        else:
            lines.append(f"  Combined P&L negative (${combined_pnl:+.2f}). Review losing combos/strategies before adding capital.")
    lines.append("")

    operator_lines = _format_operator_lines(operator_summary, approvals)
    if operator_lines:
        lines.extend(operator_lines)

    ts = ""
    if spot:
        ts = spot.analysis_time
    elif options:
        ts = options.analysis_time
    elif agg:
        ts = ""
    lines.append(f"Generated: {ts}")
    lines.append("=" * W)

    return "\n".join(lines)
