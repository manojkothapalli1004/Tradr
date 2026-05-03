from __future__ import annotations

from html import escape

import streamlit as st


TOKENS = {
    "bg": "#07101d",
    "panel": "#0d1728",
    "panel_alt": "#122138",
    "panel_soft": "rgba(15, 23, 42, 0.62)",
    "border": "rgba(148, 163, 184, 0.16)",
    "border_strong": "rgba(148, 163, 184, 0.26)",
    "text": "#e5edf7",
    "muted": "#9fb2c8",
    "subtle": "#d9e2ec",
    "profit": "#22c55e",
    "loss": "#ef4444",
    "healthy": "#10b981",
    "warning": "#f59e0b",
    "danger": "#ef4444",
    "active": "#38bdf8",
    "inactive": "#64748b",
    "open": "#3b82f6",
    "closed": "#a78bfa",
    "spot": "#22c55e",
    "options": "#8b5cf6",
}


SEMANTIC_MAP = {
    "profit": TOKENS["profit"],
    "loss": TOKENS["loss"],
    "healthy": TOKENS["healthy"],
    "warning": TOKENS["warning"],
    "danger": TOKENS["danger"],
    "active": TOKENS["active"],
    "inactive": TOKENS["inactive"],
    "open": TOKENS["open"],
    "closed": TOKENS["closed"],
    "spot": TOKENS["spot"],
    "options": TOKENS["options"],
}


BADGE_MAP = {
    "healthy": ("Healthy", TOKENS["healthy"]),
    "warning": ("Warning", TOKENS["warning"]),
    "danger": ("Danger", TOKENS["danger"]),
    "active": ("Active", TOKENS["active"]),
    "inactive": ("Inactive", TOKENS["inactive"]),
    "open": ("Open", TOKENS["open"]),
    "closed": ("Closed", TOKENS["closed"]),
    "profit": ("Profit", TOKENS["profit"]),
    "loss": ("Loss", TOKENS["loss"]),
    "spot": ("Spot", TOKENS["spot"]),
    "options": ("Options", TOKENS["options"]),
}


GLOBAL_CSS = f"""
<style>
    html, body, [data-testid="stAppViewContainer"], .stApp {{
        background:
            radial-gradient(circle at top left, rgba(34, 197, 94, 0.07), transparent 18%),
            radial-gradient(circle at top right, rgba(139, 92, 246, 0.10), transparent 24%),
            linear-gradient(180deg, rgba(17,24,39,0.15), rgba(15,23,42,0.05)),
            {TOKENS['bg']};
        color: {TOKENS['text']};
    }}
    header[data-testid="stHeader"] {{
        background: transparent;
        height: 0;
    }}
    header[data-testid="stHeader"] * {{
        display: none;
    }}
    .block-container {{
        padding-top: 0.4rem;
        padding-bottom: 1.4rem;
        max-width: 1380px;
    }}
    section[data-testid="stSidebar"] > div {{
        background: linear-gradient(180deg, rgba(9, 15, 26, 0.98), rgba(11, 18, 31, 0.98));
        border-right: 1px solid {TOKENS['border']};
    }}
    section[data-testid="stSidebar"] * {{
        color: {TOKENS['text']};
    }}
    section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] li,
    section[data-testid="stSidebar"] label p,
    section[data-testid="stSidebar"] .stCaption,
    section[data-testid="stSidebar"] small {{
        color: {TOKENS['subtle']} !important;
    }}
    section[data-testid="stSidebar"] .stRadio label,
    section[data-testid="stSidebar"] .stSelectbox label,
    section[data-testid="stSidebar"] .stMarkdown,
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3 {{
        color: {TOKENS['text']} !important;
    }}
    div[data-testid="stMetric"] {{
        background: linear-gradient(180deg, rgba(18,33,56,0.98), rgba(11,22,38,0.98));
        border: 1px solid {TOKENS['border']};
        border-radius: 18px;
        padding: 0.8rem 0.95rem;
        box-shadow: 0 10px 24px rgba(2, 6, 23, 0.22);
    }}
    .dash-card {{
        background: linear-gradient(180deg, rgba(18,33,56,0.98), rgba(11,22,38,0.98));
        border: 1px solid {TOKENS['border']};
        border-radius: 18px;
        padding: 0.95rem 1rem;
        min-height: 100%;
        box-shadow: 0 14px 34px rgba(2, 6, 23, 0.22);
    }}
    .dash-card-compact {{
        background: {TOKENS['panel_soft']};
        border: 1px solid {TOKENS['border']};
        border-radius: 16px;
        padding: 0.8rem 0.9rem;
    }}
    .dash-hero {{
        background: linear-gradient(135deg, rgba(34,197,94,0.10), rgba(59,130,246,0.08) 40%, rgba(139,92,246,0.10));
        border: 1px solid {TOKENS['border_strong']};
        border-radius: 22px;
        padding: 1rem 1.1rem;
        margin-bottom: 0.8rem;
        box-shadow: 0 18px 44px rgba(2, 6, 23, 0.26);
    }}
    .dash-hero-title {{
        font-size: 1.45rem;
        font-weight: 700;
        line-height: 1.15;
        margin-bottom: 0.22rem;
        color: {TOKENS['text']};
    }}
    .dash-hero-subtitle {{
        color: {TOKENS['subtle']};
        font-size: 0.95rem;
    }}
    .dash-status-grid {{
        display:grid;
        grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
        gap:0.8rem;
        margin:0.3rem 0 1.05rem 0;
    }}
    .dash-status-card {{
        background: linear-gradient(180deg, rgba(18,33,56,0.98), rgba(11,22,38,0.98));
        border:1px solid {TOKENS['border_strong']};
        border-radius:18px;
        padding:0.9rem 1rem;
        box-shadow: 0 10px 24px rgba(2, 6, 23, 0.18);
    }}
    .dash-status-label {{
        color:{TOKENS['subtle']};
        font-size:0.76rem;
        text-transform:uppercase;
        letter-spacing:0.09em;
        font-weight:700;
        margin-bottom:0.34rem;
    }}
    .dash-status-value {{
        font-size:1.12rem;
        font-weight:750;
        margin-bottom:0.22rem;
    }}
    .dash-status-help {{
        color:{TOKENS['muted']};
        font-size:0.84rem;
        line-height:1.4;
    }}
    .dash-kpi-label {{
        font-size: 0.75rem;
        color: {TOKENS['muted']};
        margin-bottom: 0.32rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 600;
    }}
    .dash-kpi-value {{
        font-size: 1.55rem;
        line-height: 1.05;
        font-weight: 750;
        color: {TOKENS['text']};
        margin-bottom: 0.18rem;
    }}
    .dash-kpi-help {{
        color: {TOKENS['muted']};
        font-size: 0.82rem;
        line-height: 1.35;
    }}
    .dash-section {{
        margin: 0.35rem 0 0.72rem 0;
    }}
    .dash-section h2 {{
        margin: 0;
        font-size: 1.08rem;
        color: {TOKENS['text']};
        line-height: 1.2;
    }}
    .dash-section p {{
        margin: 0.18rem 0 0 0;
        color: {TOKENS['muted']};
        font-size: 0.88rem;
        line-height: 1.35;
    }}
    .dash-badge {{
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        border-radius: 999px;
        padding: 0.24rem 0.62rem;
        font-size: 0.76rem;
        font-weight: 700;
        border: 1px solid currentColor;
        background: rgba(255,255,255,0.05);
        white-space: nowrap;
    }}
    .dash-alert-grid {{
        display:grid;
        grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
        gap:0.75rem;
        margin:0.1rem 0 0.9rem 0;
    }}
    .dash-alert {{
        border-radius:18px;
        padding:0.9rem 0.95rem;
        border:1px solid {TOKENS['border']};
        background:rgba(15,23,42,0.4);
    }}
    .dash-alert-title {{
        font-weight:700;
        margin-bottom:0.18rem;
        font-size:0.95rem;
    }}
    .dash-alert-detail {{
        color:{TOKENS['subtle']};
        font-size:0.84rem;
        line-height:1.35;
    }}
    .dash-filter-wrap {{
        background: linear-gradient(180deg, rgba(14,24,40,0.98), rgba(10,18,30,0.98));
        border: 1px solid {TOKENS['border']};
        border-radius: 18px;
        padding: 0.9rem 0.95rem 0.3rem 0.95rem;
        margin-bottom: 0.75rem;
    }}
    .dash-filter-title {{
        color: {TOKENS['text']};
        font-weight: 700;
        font-size: 0.95rem;
        margin-bottom: 0.15rem;
    }}
    .dash-filter-subtitle {{
        color: {TOKENS['muted']};
        font-size: 0.82rem;
        margin-bottom: 0.5rem;
    }}
    .dash-row {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.75rem;
        padding: 0.36rem 0;
        border-bottom: 1px solid rgba(148, 163, 184, 0.08);
        font-size: 0.89rem;
    }}
    .dash-row:last-child {{ border-bottom: none; }}
    .dash-row-label {{ color: {TOKENS['muted']}; }}
    .dash-row-value {{ color: {TOKENS['text']}; font-weight: 650; }}
    .dash-empty {{
        border: 1px dashed rgba(148, 163, 184, 0.25);
        background: rgba(15, 23, 42, 0.35);
        border-radius: 16px;
        padding: 0.95rem;
        color: {TOKENS['muted']};
        font-size: 0.9rem;
    }}
    .dash-warning {{
        border: 1px solid rgba(245, 158, 11, 0.25);
        background: rgba(245, 158, 11, 0.08);
        border-radius: 16px;
        padding: 0.95rem;
        color: #fde68a;
        font-size: 0.9rem;
    }}
    .dash-caption {{
        color: {TOKENS['muted']};
        font-size: 0.8rem;
        margin: -0.1rem 0 0.45rem 0;
    }}
    .stDataFrame, .stTable {{
        border-radius: 16px;
        overflow: hidden;
        border: 1px solid {TOKENS['border']};
    }}
    div[data-baseweb="select"] > div,
    section[data-testid="stSidebar"] div[data-baseweb="select"] > div {{
        background: rgba(15, 23, 42, 0.72);
        border-color: {TOKENS['border_strong']};
        min-height: 42px;
    }}
    div[data-baseweb="select"] span,
    section[data-testid="stSidebar"] div[data-baseweb="select"] span {{
        color: {TOKENS['text']} !important;
    }}
    section[data-testid="stSidebar"] input,
    section[data-testid="stSidebar"] textarea,
    section[data-testid="stSidebar"] [data-baseweb="input"] input {{
        color: {TOKENS['text']} !important;
    }}
    label[data-testid="stWidgetLabel"] p,
    section[data-testid="stSidebar"] label[data-testid="stWidgetLabel"] p {{
        color: {TOKENS['subtle']} !important;
        font-size: 0.84rem;
        font-weight: 600;
    }}
</style>
"""


def inject_global_styles() -> None:
    st.set_page_config(page_title="Trading Analytics Dashboard", layout="wide")
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)


def semantic_color(name: str) -> str:
    return SEMANTIC_MAP.get(name, TOKENS["muted"])


def section_header(title: str, subtitle: str | None = None, badge: str | None = None, badge_tone: str | None = None) -> None:
    badge_html = ""
    if badge:
        tone = semantic_color(badge_tone or "active")
        badge_html = f'<span class="dash-badge" style="color:{tone};">{escape(badge)}</span>'
    subtitle_html = f"<p>{escape(subtitle)}</p>" if subtitle else ""
    st.markdown(
        f'<div class="dash-section"><div style="display:flex;justify-content:space-between;gap:0.75rem;align-items:flex-start;">'
        f'<div><h2>{escape(title)}</h2>{subtitle_html}</div>{badge_html}</div></div>',
        unsafe_allow_html=True,
    )


def hero_header(title: str, subtitle: str, badges: list[tuple[str, str]] | None = None) -> None:
    badge_html = ""
    if badges:
        parts = []
        for label, tone in badges:
            color = semantic_color(tone)
            parts.append(f'<span class="dash-badge" style="color:{color};">{escape(label)}</span>')
        badge_html = f'<div style="display:flex;gap:0.45rem;flex-wrap:wrap;margin-top:0.7rem;">{"".join(parts)}</div>'
    st.markdown(
        f'<div class="dash-hero">'
        f'<div class="dash-hero-title">{escape(title)}</div>'
        f'<div class="dash-hero-subtitle">{escape(subtitle)}</div>'
        f'{badge_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def render_kpi_card(label: str, value: str, help_text: str | None = None, tone: str | None = None) -> None:
    tone_style = f' style="color:{semantic_color(tone)}"' if tone else ""
    help_html = f'<div class="dash-kpi-help">{escape(help_text)}</div>' if help_text else ""
    st.markdown(
        f'<div class="dash-card">'
        f'<div class="dash-kpi-label">{escape(label)}</div>'
        f'<div class="dash-kpi-value"{tone_style}>{escape(value)}</div>'
        f'{help_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def render_status_card(label: str, value: str, help_text: str, tone: str) -> str:
    color = semantic_color(tone)
    return (
        f'<div class="dash-status-card">'
        f'<div class="dash-status-label">{escape(label)}</div>'
        f'<div class="dash-status-value" style="color:{color};">{escape(value)}</div>'
        f'<div class="dash-status-help">{escape(help_text)}</div>'
        f'</div>'
    )


def render_status_grid(cards: list[str]) -> None:
    st.markdown(f'<div class="dash-status-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def status_badge(status: str) -> str:
    label, color = BADGE_MAP.get(status, (status.title(), TOKENS["muted"]))
    return f'<span class="dash-badge" style="color:{color};">{escape(label)}</span>'


def metric_row(label: str, value: str, tone: str | None = None) -> None:
    value_style = f' style="color:{semantic_color(tone)};"' if tone else ""
    st.markdown(
        f'<div class="dash-row"><span class="dash-row-label">{escape(label)}</span>'
        f'<span class="dash-row-value"{value_style}>{escape(value)}</span></div>',
        unsafe_allow_html=True,
    )


def render_empty_state(title: str, detail: str) -> None:
    st.markdown(
        f'<div class="dash-empty"><strong>{escape(title)}</strong><br>{escape(detail)}</div>',
        unsafe_allow_html=True,
    )


def render_warning_state(title: str, detail: str) -> None:
    st.markdown(
        f'<div class="dash-warning"><strong>{escape(title)}</strong><br>{escape(detail)}</div>',
        unsafe_allow_html=True,
    )


def render_caption(text: str) -> None:
    st.markdown(f'<div class="dash-caption">{escape(text)}</div>', unsafe_allow_html=True)


def render_alert_card(title: str, detail: str, tone: str = "warning") -> str:
    color = semantic_color(tone)
    bg = {
        "danger": "rgba(239,68,68,0.10)",
        "warning": "rgba(245,158,11,0.10)",
        "active": "rgba(56,189,248,0.10)",
        "healthy": "rgba(16,185,129,0.10)",
    }.get(tone, "rgba(255,255,255,0.04)")
    return (
        f'<div class="dash-alert" style="border-color:{color}; background:{bg};">'
        f'<div class="dash-alert-title" style="color:{color};">{escape(title)}</div>'
        f'<div class="dash-alert-detail">{escape(detail)}</div>'
        f'</div>'
    )


def render_alert_grid(cards: list[str]) -> None:
    if not cards:
        return
    st.markdown(f'<div class="dash-alert-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def render_panel_start() -> None:
    st.markdown('<div class="dash-card">', unsafe_allow_html=True)


def render_panel_end() -> None:
    st.markdown('</div>', unsafe_allow_html=True)


def render_filter_shell_start(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="dash-filter-wrap"><div class="dash-filter-title">{escape(title)}</div><div class="dash-filter-subtitle">{escape(subtitle)}</div>',
        unsafe_allow_html=True,
    )


def render_filter_shell_end() -> None:
    st.markdown('</div>', unsafe_allow_html=True)
