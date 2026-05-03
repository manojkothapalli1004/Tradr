from __future__ import annotations

from typing import Iterable

import pandas as pd
import streamlit as st

try:
    from .styles import metric_row, render_caption, render_empty_state, render_panel_end, render_panel_start, section_header
except ImportError:
    from styles import metric_row, render_caption, render_empty_state, render_panel_end, render_panel_start, section_header


def render_source_table(frame: pd.DataFrame) -> None:
    if frame.empty:
        render_empty_state("No source status", "No source metadata was produced.")
        return
    display = frame.copy()
    if "available" in display.columns:
        display["available"] = display["available"].map({True: "yes", False: "no"}).fillna("unknown")
    st.dataframe(display, use_container_width=True, hide_index=True)


def render_dataframe(frame: pd.DataFrame, title: str, subtitle: str, height: int = 280, columns: Iterable[str] | None = None) -> None:
    section_header(title, subtitle)
    if frame.empty:
        render_empty_state(title, "No rows available for the current data source or filters.")
        return
    display = frame.copy()
    if columns is not None:
        available_columns = [column for column in columns if column in display.columns]
        display = display[available_columns]
    render_caption(f"{len(display)} row(s)")
    st.dataframe(display, use_container_width=True, hide_index=True, height=height)


def render_bar_chart(frame: pd.DataFrame, index_col: str, value_col: str, title: str, subtitle: str, color_hint: str | None = None) -> None:
    section_header(title, subtitle, badge=color_hint, badge_tone=color_hint)
    if frame.empty or index_col not in frame.columns or value_col not in frame.columns:
        render_empty_state(title, "Chart unavailable because the required fields were not found.")
        return
    chart_df = frame[[index_col, value_col]].dropna().copy()
    if chart_df.empty:
        render_empty_state(title, "Chart unavailable because the filtered result set is empty.")
        return
    chart_df[index_col] = chart_df[index_col].astype(str).str.slice(0, 36)
    chart_df = chart_df.set_index(index_col)
    render_caption(f"{len(chart_df)} category(s)")
    st.bar_chart(chart_df, height=320)


def render_line_chart(frame: pd.DataFrame, x_col: str, y_col: str, title: str, subtitle: str) -> None:
    section_header(title, subtitle)
    if frame.empty or x_col not in frame.columns or y_col not in frame.columns:
        render_empty_state(title, "Line chart unavailable for the current source data.")
        return
    chart_df = frame[[x_col, y_col]].dropna().copy()
    if chart_df.empty:
        render_empty_state(title, "Line chart unavailable because there are no valid points.")
        return
    chart_df = chart_df.set_index(x_col)
    render_caption(f"{len(chart_df)} point(s)")
    st.line_chart(chart_df, height=320)


def render_key_value_panel(title: str, subtitle: str, items: list[tuple[str, str, str | None]]) -> None:
    section_header(title, subtitle)
    render_panel_start()
    if not items:
        render_empty_state(title, "No items available.")
    else:
        for label, value, tone in items:
            metric_row(label, value, tone)
    render_panel_end()


def render_recent_activity(frame: pd.DataFrame, title: str = "Recent activity") -> None:
    section_header(title, "Latest observed events from parsed state and logs.")
    if frame.empty:
        render_empty_state(title, "No recent activity could be derived from the available sources.")
        return
    display = frame.copy()
    if "timestamp" in display.columns:
        display = display.sort_values("timestamp", ascending=False)
    render_caption(f"Showing {len(display)} event(s)")
    st.dataframe(display, use_container_width=True, hide_index=True, height=320)
