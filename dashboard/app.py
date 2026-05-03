from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

try:
    from .components import (
        render_bar_chart,
        render_dataframe,
        render_key_value_panel,
        render_line_chart,
        render_recent_activity,
        render_source_table,
    )
    from .data_loader import (
        active_experiment_rows,
        approval_status_frame,
        approval_summary_rows,
        combine_recent_activity,
        combine_trades,
        experiment_history_frame,
        filter_trades,
        load_dashboard_data,
        load_operator_panels_data,
        operator_panels_summary,
        overall_summary,
        proposal_summary_rows,
        proposals_frame,
    )
    from .styles import (
        hero_header,
        inject_global_styles,
        render_alert_card,
        render_alert_grid,
        render_caption,
        render_empty_state,
        render_filter_shell_end,
        render_filter_shell_start,
        render_kpi_card,
        render_status_card,
        render_status_grid,
        render_warning_state,
        section_header,
    )
except ImportError:
    from components import (
        render_bar_chart,
        render_dataframe,
        render_key_value_panel,
        render_line_chart,
        render_recent_activity,
        render_source_table,
    )
    from data_loader import (
        active_experiment_rows,
        approval_status_frame,
        approval_summary_rows,
        combine_recent_activity,
        combine_trades,
        experiment_history_frame,
        filter_trades,
        load_dashboard_data,
        load_operator_panels_data,
        operator_panels_summary,
        overall_summary,
        proposal_summary_rows,
        proposals_frame,
    )
    from styles import (
        hero_header,
        inject_global_styles,
        render_alert_card,
        render_alert_grid,
        render_caption,
        render_empty_state,
        render_filter_shell_end,
        render_filter_shell_start,
        render_kpi_card,
        render_status_card,
        render_status_grid,
        render_warning_state,
        section_header,
    )


ROOT = Path(__file__).resolve().parents[1]
REFRESH_OPTIONS = {"Off": None, "15s": "15s", "30s": "30s", "60s": "60s"}


def fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def fmt_hold(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    if value >= 1440:
        return f"{value / 1440:.1f}d"
    return f"{value:.0f}m"


def fmt_timestamp(value) -> str:
    if value is None or pd.isna(value):
        return "Unknown"
    ts = pd.Timestamp(value)
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def value_tone(value: float | None) -> str | None:
    if value is None or pd.isna(value):
        return None
    if value > 0:
        return "profit"
    if value < 0:
        return "loss"
    return None


def source_health_label(parsed) -> tuple[str, str, str]:
    source_status = parsed.source_status if not parsed.source_status.empty else pd.DataFrame()
    any_available = bool(source_status["available"].astype(bool).any()) if not source_status.empty else False
    last_updated = parsed.summary.last_updated
    if not any_available:
        return "Unavailable", "danger", "No readable source files"
    if last_updated is None:
        return "Uncertain", "warning", "Readable sources exist, but freshness evidence is incomplete"
    age_seconds = (pd.Timestamp.utcnow() - pd.Timestamp(last_updated)).total_seconds()
    if age_seconds <= 900:
        return "Recently updated", "healthy", f"Evidence seen {int(max(age_seconds, 0) // 60)}m ago"
    if age_seconds <= 3600:
        return "Stale", "warning", f"Last evidence {int(age_seconds // 60)}m ago"
    return "Available, stale", "warning", f"Readable sources, but last evidence {int(age_seconds // 3600)}h ago"


def recent_router_rejections(parsed) -> pd.DataFrame:
    if parsed.recent_activity.empty:
        return parsed.recent_activity.copy()
    frame = parsed.recent_activity.copy()
    frame = frame[
        (frame["event"].astype(str) == "router")
        & (frame["detail"].astype(str).str.contains("REJECT|SKIP", regex=True))
    ].copy()
    if frame.empty:
        return frame
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    latest = frame["timestamp"].max()
    if pd.notna(latest):
        cutoff = latest - pd.Timedelta(minutes=60)
        recent = frame[frame["timestamp"] >= cutoff].copy()
        if not recent.empty:
            return recent.sort_values("timestamp", ascending=False)
    return frame.sort_values("timestamp", ascending=False).head(8)


def build_alerts(data: dict) -> list[str]:
    alerts: list[str] = []
    for bot_name, parsed in data.items():
        source_status = parsed.source_status if not parsed.source_status.empty else pd.DataFrame()
        any_available = bool(source_status["available"].astype(bool).any()) if not source_status.empty else False
        if not any_available:
            alerts.append(render_alert_card(f"{bot_name.title()} sources unavailable", "No readable source file is available for this bot in the current dashboard session.", "danger"))
        if parsed.summary.kill_switch_active:
            alerts.append(render_alert_card(f"{bot_name.title()} kill switch active", "The persisted portfolio state reports an active kill switch.", "danger"))
        if bot_name == "options" and parsed.summary.open_positions >= 2:
            alerts.append(render_alert_card("Options portfolio cap reached", "Open positions are at the inferred 2/2 cap, so new entries may be rejected.", "warning"))
        if bot_name == "options" and parsed.completed_trades.empty:
            alerts.append(render_alert_card("No completed options trades yet", "Closed-trade analytics remain limited until the first options positions exit.", "active"))
        if bot_name == "spot" and not parsed.exit_reason_stats.empty:
            exit_reasons = parsed.exit_reason_stats["exit_reason"].astype(str).str.lower().tolist()
            if exit_reasons and all("time limit" in reason for reason in exit_reasons):
                alerts.append(render_alert_card("Spot exits observed so far are time-limit exits", "Current closed-trade history shows only time-limit exit reasons.", "warning"))
        if bot_name == "options":
            recent_blocks = recent_router_rejections(parsed)
            if len(recent_blocks) >= 3:
                alerts.append(render_alert_card("Repeated recent router rejections", f"Observed {len(recent_blocks)} router skip/reject events in the latest activity window.", "warning"))
    return alerts


def build_critical_events(data: dict) -> pd.DataFrame:
    frames = []
    for bot_name, parsed in data.items():
        if parsed.recent_activity.empty:
            continue
        frame = parsed.recent_activity.copy()
        frame["bot"] = bot_name
        frame["severity_rank"] = frame["severity"].map({"danger": 0, "warning": 1, "active": 2, "healthy": 3, "inactive": 4}).fillna(5)
        frame["event_group"] = frame["event"].map(
            {
                "warning": "Warnings",
                "trade_exit": "Exits",
                "opened": "Openings",
                "trade_open": "Openings",
                "router": "Router / blocks",
            }
        ).fillna("Other")
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["timestamp", "bot", "event_group", "event", "detail", "severity"])
    combined = pd.concat(frames, ignore_index=True)
    return combined.sort_values(["severity_rank", "timestamp"], ascending=[True, False])


def render_live_status(data: dict) -> None:
    section_header("Live status", "Availability and freshness based on readable sources and latest observed timestamps.")
    cards = []
    for bot_name in ["spot", "options"]:
        label, tone, help_text = source_health_label(data[bot_name])
        cards.append(render_status_card(f"{bot_name} bot", label, help_text, tone))
    render_status_grid(cards)


def render_refresh_control() -> str | None:
    choice = st.sidebar.selectbox("Auto-refresh", list(REFRESH_OPTIONS.keys()), index=0, help="Refreshes the dashboard body only when supported by this Streamlit runtime.")
    st.sidebar.caption("Default is Off for safe manual monitoring.")
    return REFRESH_OPTIONS[choice]


def render_overview(data: dict) -> None:
    summary = overall_summary(data)
    hero_header(
        "Trading analytics dashboard",
        f"Operator-first read-only monitoring for spot and options bots. Last observed update: {fmt_timestamp(summary['last_updated'])}.",
        badges=[("read-only", "active"), ("spot + options", "healthy"), ("local monitoring", "options")],
    )

    cols = st.columns(6)
    cards = [
        ("Bot health", "Healthy" if summary["healthy"] else "Check warnings", "Both bots summarized from current local state.", None if summary["healthy"] else "warning"),
        ("Open positions", str(summary["total_open_positions"]), "Combined open exposure across bots.", "active" if summary["total_open_positions"] else "inactive"),
        ("Realized P&L", fmt_money(summary["realized_pnl"]), "Closed-trade realized result.", value_tone(summary["realized_pnl"])),
        ("Unrealized P&L", fmt_money(summary["unrealized_pnl"]), "Only shown where the bot persists it.", value_tone(summary["unrealized_pnl"])),
        ("Closed trades", str(summary["total_closed_trades"]), "Completed trades in normalized journal.", None),
        ("Win rate", fmt_pct(summary["win_rate"]), "Based on normalized closed trades.", None),
    ]
    for col, (label, value, help_text, tone) in zip(cols, cards):
        with col:
            render_kpi_card(label, value, help_text=help_text, tone=tone)

    render_live_status(data)

    alerts = build_alerts(data)
    if alerts:
        section_header("Operator alerts", "Attention cues derived from current persisted state and recent activity.")
        render_alert_grid(alerts)

    bot_cols = st.columns(2)
    for col, bot_name in zip(bot_cols, ["spot", "options"]):
        parsed = data[bot_name]
        with col:
            render_key_value_panel(
                f"{bot_name.title()} summary",
                "Compact health and performance snapshot.",
                [
                    ("Status", parsed.summary.status, "warning" if parsed.summary.kill_switch_active else None),
                    ("Open positions", str(parsed.summary.open_positions), "active" if parsed.summary.open_positions else "inactive"),
                    ("Realized P&L", fmt_money(parsed.summary.realized_pnl), value_tone(parsed.summary.realized_pnl)),
                    ("Unrealized P&L", fmt_money(parsed.summary.unrealized_pnl), value_tone(parsed.summary.unrealized_pnl)),
                    ("Fees", fmt_money(parsed.summary.fees), None),
                    ("Avg hold", fmt_hold(parsed.summary.avg_hold_minutes), None),
                ],
            )
            if parsed.summary.notes:
                render_warning_state(f"{bot_name.title()} notes", " | ".join(parsed.summary.notes))

    critical_events = build_critical_events(data)
    section_header("Recent critical events", "Grouped for faster triage across warnings, exits, openings, and router blocks.")
    if critical_events.empty:
        render_empty_state("Recent critical events", "No critical or notable events could be derived from current sources.")
    else:
        tabs = st.tabs(["Warnings", "Exits", "Openings", "Router / blocks"])
        for tab, group in zip(tabs, ["Warnings", "Exits", "Openings", "Router / blocks"]):
            with tab:
                group_frame = critical_events[critical_events["event_group"] == group].copy().head(8)
                if group_frame.empty:
                    render_empty_state(group, f"No recent {group.lower()} events.")
                else:
                    st.dataframe(group_frame[["timestamp", "bot", "event", "detail", "severity"]], use_container_width=True, hide_index=True, height=260)

    activity = combine_recent_activity(data)
    render_recent_activity(activity)


def render_spot_page(parsed) -> None:
    section_header("Spot bot analytics", "Performance, activity, inactivity, and health from spot state and logs.", badge="spot", badge_tone="spot")
    top = st.columns(4)
    metrics = [
        ("Open positions", str(parsed.summary.open_positions), "State-derived current open trades.", "active" if parsed.summary.open_positions else "inactive"),
        ("Realized P&L", fmt_money(parsed.summary.realized_pnl), "Closed-trade net P&L.", value_tone(parsed.summary.realized_pnl)),
        ("Fees", fmt_money(parsed.summary.fees), "Total observed fees.", None),
        ("Avg hold", fmt_hold(parsed.summary.avg_hold_minutes), "Average completed-trade hold time.", None),
    ]
    for col, (label, value, help_text, tone) in zip(top, metrics):
        with col:
            render_kpi_card(label, value, help_text=help_text, tone=tone)

    c1, c2 = st.columns(2)
    with c1:
        render_bar_chart(parsed.strategy_stats.head(12), "strategy_key", "realized_pnl", "P&L by algo", "Top strategy + symbol combinations by realized P&L.")
    with c2:
        render_bar_chart(parsed.symbol_stats, "symbol", "realized_pnl", "P&L by symbol", "Realized spot performance by asset.")

    c3, c4 = st.columns(2)
    with c3:
        render_bar_chart(parsed.exit_reason_stats, "exit_reason", "count", "Exit reason counts", "Observed close reasons from completed spot trades.")
    with c4:
        render_line_chart(parsed.equity_curve, "timestamp", "equity", "Equity / portfolio curve", "Portfolio snapshots derived from the spot log.")

    render_dataframe(parsed.open_positions, "Current open positions", "State-derived spot positions currently open.", columns=["trade_id", "symbol", "strategy", "direction", "entry_time", "hold_minutes", "fees", "notes"])
    render_dataframe(parsed.completed_trades, "Recent trades", "Completed spot trades from state.", columns=["trade_id", "symbol", "strategy", "direction", "entry_time", "exit_time", "hold_minutes", "realized_pnl", "fees", "exit_reason"])
    render_dataframe(parsed.inactivity, "Inactive strategies / symbols", "Strategies with zero completed trades and no active positions.", columns=["strategy", "symbol", "total_trades", "active_trade_ids", "status"])
    render_dataframe(parsed.risk_items, "Health / risk status", "Portfolio-level risk fields and derived operating status.", columns=["label", "value", "status", "detail"])


def render_options_page(parsed) -> None:
    section_header("Options bot analytics", "Open positions, simulator caveats, risk posture, and completed options data.", badge="options", badge_tone="options")
    top = st.columns(5)
    metrics = [
        ("Open positions", str(parsed.summary.open_positions), "Current open option positions.", "active" if parsed.summary.open_positions else "inactive"),
        ("Realized P&L", fmt_money(parsed.summary.realized_pnl), "Closed-trade realized result.", value_tone(parsed.summary.realized_pnl)),
        ("Unrealized P&L", fmt_money(parsed.summary.unrealized_pnl), "Open-position simulator mark.", value_tone(parsed.summary.unrealized_pnl)),
        ("Fees", fmt_money(parsed.summary.fees), "Observed option entry/exit fees.", None),
        ("Avg hold", fmt_hold(parsed.summary.avg_hold_minutes), "Average observed hold time.", None),
    ]
    for col, (label, value, help_text, tone) in zip(top, metrics):
        with col:
            render_kpi_card(label, value, help_text=help_text, tone=tone)

    if parsed.summary.notes:
        render_warning_state("Simulator / source limitations", " | ".join(parsed.summary.notes))

    c1, c2 = st.columns(2)
    with c1:
        render_bar_chart(parsed.strategy_stats.head(12), "strategy_key", "active_trade_ids", "Open exposure by strategy", "Current open position count by strategy + symbol pair.")
    with c2:
        render_bar_chart(parsed.symbol_stats, "symbol", "unrealized_pnl", "Unrealized P&L by symbol", "Current options mark-to-model exposure by underlying.")

    c3, c4 = st.columns(2)
    with c3:
        render_bar_chart(parsed.exit_reason_stats, "exit_reason", "count", "Exit reason counts", "Will populate as completed options trades accumulate.")
    with c4:
        render_line_chart(parsed.equity_curve, "timestamp", "equity", "Realized P&L curve", "Derived from normalized options journal rows when trades close.")

    render_dataframe(parsed.open_positions, "Open positions", "State-derived open options positions.", columns=["trade_id", "symbol", "strategy", "entry_time", "hold_minutes", "unrealized_pnl", "fees", "notes"])
    render_dataframe(parsed.completed_trades, "Completed trades", "Closed options trades from state.", columns=["trade_id", "symbol", "strategy", "entry_time", "exit_time", "hold_minutes", "realized_pnl", "fees", "exit_reason"])
    render_dataframe(parsed.risk_items, "Risk / cap state", "Kill switch, deployed premium, drawdown, and cap utilization.", columns=["label", "value", "status", "detail"])
    render_dataframe(parsed.inactivity, "Inactive strategies / symbols", "Options strategies with no trade history and no active positions.", columns=["strategy", "symbol", "total_trades", "active_trade_ids", "status"])


def render_operator_panels() -> None:
    operator_data = load_operator_panels_data()
    section_header("Operator panels", "Read-only experiment, proposal, and approval state for recent operator workflow.")
    render_caption(operator_panels_summary(operator_data))

    top_left, top_mid, top_right = st.columns(3)
    with top_left:
        render_key_value_panel(
            "Active experiment",
            "Current experiment status from the journal.",
            active_experiment_rows(operator_data.experiment_report),
        )
    with top_mid:
        render_key_value_panel(
            "Proposals",
            "Current proposal file summary.",
            proposal_summary_rows(
                operator_data.current_proposals,
                operator_data.proposal_source_available,
            ),
        )
    with top_right:
        render_key_value_panel(
            "Approval status",
            "Approval-store counts by state.",
            approval_summary_rows(operator_data.approval_summary),
        )

    lower_left, lower_right = st.columns(2)
    with lower_left:
        history = experiment_history_frame(operator_data.experiment_report)
        if history.empty:
            render_empty_state("Recent experiment history", "No experiment journal entries are available.")
        else:
            render_dataframe(
                history,
                "Recent experiment history",
                "Latest running, completed, reverted, and verdict-bearing experiments.",
                columns=["experiment_id", "status", "target_bot", "target_scope", "parameter_changed", "verdict", "sample_size", "start_timestamp", "end_timestamp"],
                height=260,
            )
    with lower_right:
        proposals = proposals_frame(operator_data.current_proposals)
        if proposals.empty:
            render_empty_state("Current proposals", "No proposal data is currently available.")
        else:
            render_dataframe(
                proposals,
                "Current proposals",
                "Read-only view of the latest proposal entries.",
                columns=["proposal_id", "title", "status", "action_type", "target", "created_at"],
                height=260,
            )

    approval_df = approval_status_frame(operator_data.approval_summary)
    if not approval_df.empty and approval_df["count"].sum() > 0:
        render_bar_chart(
            approval_df,
            "status",
            "count",
            "Approval status counts",
            "Current approval-store distribution by status.",
        )


def render_unified_journal(trades: pd.DataFrame) -> None:
    section_header("Unified trade journal", "Combined normalized view across spot and options bots.")
    if trades.empty:
        render_empty_state("Unified trade journal", "No trade data is currently available from either bot.")
        return

    render_filter_shell_start("Journal filters", "Refine the normalized trade view by bot, symbol, strategy, status, and entry date.")
    f1, f2, f3, f4, f5 = st.columns(5)
    with f1:
        bot_filter = st.multiselect("Bot", options=sorted(trades["bot"].dropna().unique()), default=[])
    with f2:
        symbol_filter = st.multiselect("Symbol", options=sorted(trades["symbol"].dropna().unique()), default=[])
    with f3:
        strategy_filter = st.multiselect("Strategy / algo", options=sorted(trades["strategy"].dropna().unique()), default=[])
    with f4:
        status_filter = st.multiselect("Status", options=sorted(trades["status"].dropna().unique()), default=[])
    with f5:
        if trades["entry_time"].notna().any():
            min_date = trades["entry_time"].min().date()
            max_date = trades["entry_time"].max().date()
            date_range = st.date_input("Entry date range", value=(min_date, max_date))
        else:
            date_range = ()
    render_filter_shell_end()

    start_time = None
    end_time = None
    if isinstance(date_range, tuple) and len(date_range) == 2 and all(date_range):
        start_time = pd.Timestamp(date_range[0], tz="UTC")
        end_time = pd.Timestamp(date_range[1], tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

    filtered = filter_trades(trades, bot_filter, symbol_filter, strategy_filter, status_filter, start_time, end_time)
    render_caption(f"Showing {len(filtered)} of {len(trades)} normalized trade row(s)")
    render_dataframe(filtered, "Unified journal table", "Shared normalized trade fields where available.", height=440, columns=["bot", "trade_id", "symbol", "strategy", "direction", "status", "entry_time", "exit_time", "hold_minutes", "realized_pnl", "unrealized_pnl", "fees", "exit_reason", "notes"])


def render_charts_page(trades: pd.DataFrame) -> None:
    section_header("Charts", "Cross-bot views for activity, concentration, and hold-time behavior.")
    if trades.empty:
        render_empty_state("Charts", "Charts are unavailable because no normalized trades were produced.")
        return
    closed = trades[trades["status"] == "closed"].copy()
    c1, c2 = st.columns(2)
    with c1:
        if not closed.empty:
            strategy_counts = closed.groupby("strategy").size().reset_index(name="count").sort_values("count", ascending=False)
            render_bar_chart(strategy_counts, "strategy", "count", "Trade count by strategy", "Closed trades grouped by strategy.")
        else:
            render_empty_state("Trade count by strategy", "No closed trades yet.")
    with c2:
        if not closed.empty:
            strategy_pnl = closed.groupby("strategy", dropna=False)["realized_pnl"].sum().reset_index().sort_values("realized_pnl", ascending=False)
            render_bar_chart(strategy_pnl, "strategy", "realized_pnl", "P&L by strategy", "Aggregate realized P&L by strategy.")
        else:
            render_empty_state("P&L by strategy", "No closed trades yet.")

    c3, c4 = st.columns(2)
    with c3:
        if not closed.empty:
            symbol_pnl = closed.groupby("symbol", dropna=False)["realized_pnl"].sum().reset_index().sort_values("realized_pnl", ascending=False)
            render_bar_chart(symbol_pnl, "symbol", "realized_pnl", "P&L by symbol", "Aggregate realized P&L by symbol.")
        else:
            render_empty_state("P&L by symbol", "No closed trades yet.")
    with c4:
        if not closed.empty and "exit_reason" in closed.columns:
            exit_counts = closed.fillna({"exit_reason": "unknown"}).groupby("exit_reason").size().reset_index(name="count").sort_values("count", ascending=False)
            render_bar_chart(exit_counts, "exit_reason", "count", "Exit reason distribution", "Exit reasons observed across closed trades.")
        else:
            render_empty_state("Exit reason distribution", "Exit reasons unavailable.")

    if "hold_minutes" in trades.columns and not trades["hold_minutes"].dropna().empty:
        hold_df = trades[["trade_id", "hold_minutes"]].dropna().sort_values("hold_minutes", ascending=False)
        render_bar_chart(hold_df.head(25), "trade_id", "hold_minutes", "Hold-time distribution", "Longest holds across normalized trade records.")
    else:
        render_empty_state("Hold-time distribution", "Hold durations are unavailable.")


def render_sources(data: dict) -> None:
    section_header("Source health", "Visual view of readable vs stale bot sources.")
    status_cards = []
    for bot_name in ["spot", "options"]:
        label, tone, help_text = source_health_label(data[bot_name])
        status_cards.append(render_status_card(f"{bot_name} sources", label, help_text, tone))
    render_status_grid(status_cards)

    rows = []
    for bot_name in ["spot", "options"]:
        parsed = data[bot_name]
        health_label, _, health_detail = source_health_label(parsed)
        status_frame = parsed.source_status.copy()
        if status_frame.empty:
            rows.append({"bot": bot_name, "source": "—", "available": "no", "freshness": health_label, "last_evidence": fmt_timestamp(parsed.summary.last_updated), "detail": health_detail})
            continue
        for _, row in status_frame.iterrows():
            rows.append(
                {
                    "bot": bot_name,
                    "source": row.get("source", "—"),
                    "available": "yes" if bool(row.get("available")) else "no",
                    "freshness": health_label,
                    "last_evidence": fmt_timestamp(parsed.summary.last_updated),
                    "detail": row.get("detail", health_detail),
                }
            )
    render_source_table(pd.DataFrame(rows))


def render_dashboard_body(page: str) -> None:
    data = load_dashboard_data()
    trades = combine_trades(data)
    if page == "Overview":
        render_overview(data)
    elif page == "Operator panels":
        render_operator_panels()
    elif page == "Spot bot analytics":
        render_spot_page(data["spot"])
    elif page == "Options bot analytics":
        render_options_page(data["options"])
    elif page == "Unified trade journal":
        render_unified_journal(trades)
    elif page == "Charts":
        render_charts_page(trades)
    else:
        render_sources(data)


def main() -> None:
    inject_global_styles()
    sidebar = st.sidebar
    sidebar.title("Trading dashboard")
    sidebar.caption("Dark, read-only local monitor")
    refresh_every = render_refresh_control()
    page = sidebar.radio("Section", ["Overview", "Operator panels", "Spot bot analytics", "Options bot analytics", "Unified trade journal", "Charts", "Source health"])
    sidebar.caption(f"Repo root: {ROOT}")
    sidebar.caption("Safe while bots are running.")

    if refresh_every and hasattr(st, "fragment"):
        @st.fragment(run_every=refresh_every)
        def live_view() -> None:
            render_dashboard_body(page)

        live_view()
    else:
        render_dashboard_body(page)


if __name__ == "__main__":
    main()
