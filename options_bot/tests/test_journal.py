"""
Tests for options_bot/journal.py — state persistence and reporting.
Uses a temporary file to avoid touching the live state.json.
No network calls.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from options_bot.models import (
    AlgoSlot, ExitReason, OptionAction, OptionTrade, OptionType,
    PortfolioState, SignalType,
)
from options_bot.journal import (
    _empty, ensure_slots, get_slots, put_slots,
    get_open_trades, put_open_trades, add_completed,
    get_portfolio, put_portfolio,
    load_state, save_state,
    performance_summary, format_status,
    MAX_COMPLETED,
)


# ── Helpers ───────────────────────────────────────────────────────────────────────

def _trade(trade_id: str = "opt-spy-orb-abc123", symbol: str = "SPY") -> OptionTrade:
    return OptionTrade(
        id=trade_id, symbol=symbol, strategy=SignalType.ORB.value,
        option_type=OptionType.CALL, action=OptionAction.BUY,
        expiry="2025-06-20", strike=450.0, dte_at_entry=21,
        entry_time=datetime.now(timezone.utc),
        entry_fill_per_share=3.50, entry_premium_total=350.0,
        data_quality_note="test",
    )


def _closed_trade(trade_id: str, pnl: float) -> OptionTrade:
    t = _trade(trade_id)
    t.exit_time           = datetime.now(timezone.utc)
    t.exit_reason         = ExitReason.PROFIT_TARGET if pnl >= 0 else ExitReason.STOP_LOSS
    t.realized_pnl_usd    = pnl
    t.realized_pnl_pct    = pnl / t.entry_premium_total * 100
    t.hold_days           = 1.0
    t.exit_fill_per_share = 3.50 + pnl / 100
    return t


# ── _empty ────────────────────────────────────────────────────────────────────────

class TestEmpty:
    def test_has_required_keys(self):
        s = _empty()
        for k in ("version", "algos", "open_trades", "completed_trades", "portfolio", "last_regimes"):
            assert k in s

    def test_algos_starts_empty(self):
        assert _empty()["algos"] == {}


# ── ensure_slots ─────────────────────────────────────────────────────────────────

class TestEnsureSlots:
    def test_creates_10_slots(self):
        # 5 strategies × 2 symbols
        state = ensure_slots(_empty())
        assert len(state["algos"]) == 10

    def test_idempotent(self):
        state = ensure_slots(_empty())
        n     = len(state["algos"])
        state = ensure_slots(state)
        assert len(state["algos"]) == n

    def test_existing_slots_not_overwritten(self):
        state = ensure_slots(_empty())
        key   = next(iter(state["algos"]))
        state["algos"][key]["total_trades"] = 99
        state = ensure_slots(state)
        assert state["algos"][key]["total_trades"] == 99


# ── Slot round-trip ───────────────────────────────────────────────────────────────

class TestSlotRoundTrip:
    def test_put_then_get(self):
        state = ensure_slots(_empty())
        slots = get_slots(state)
        for s in slots.values():
            s.total_trades = 7
        state = put_slots(state, slots)
        slots2 = get_slots(state)
        assert all(s.total_trades == 7 for s in slots2.values())


# ── Trade helpers ─────────────────────────────────────────────────────────────────

class TestTradeHelpers:
    def test_open_trade_round_trip(self):
        state = _empty()
        t     = _trade()
        state = put_open_trades(state, [t])
        out   = get_open_trades(state)
        assert len(out) == 1
        assert out[0].id == t.id

    def test_add_completed_persists(self):
        state = _empty()
        t     = _closed_trade("t1", 50.0)
        state = add_completed(state, t)
        assert len(state["completed_trades"]) == 1

    def test_completed_cap_enforced(self):
        state = _empty()
        for i in range(MAX_COMPLETED + 5):
            state = add_completed(state, _closed_trade(f"t{i}", 10.0))
        assert len(state["completed_trades"]) == MAX_COMPLETED


# ── Portfolio helpers ─────────────────────────────────────────────────────────────

class TestPortfolioHelpers:
    def test_default_portfolio(self):
        ps = get_portfolio(_empty())
        assert ps.kill_switch_active is False
        assert ps.total_premium_deployed_usd == 0.0

    def test_put_get_round_trip(self):
        state = _empty()
        ps    = PortfolioState(total_premium_deployed_usd=200.0, kill_switch_active=True)
        state = put_portfolio(state, ps)
        ps2   = get_portfolio(state)
        assert ps2.total_premium_deployed_usd == 200.0
        assert ps2.kill_switch_active is True


# ── Atomic persistence ────────────────────────────────────────────────────────────

class TestAtomicPersistence:
    def test_save_then_load_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            state = ensure_slots(_empty())
            with patch("options_bot.journal.STATE_FILE", path):
                save_state(state)
                loaded = load_state()
            assert loaded["version"] == state["version"]
            assert len(loaded["algos"]) == len(state["algos"])
        finally:
            os.unlink(path)
            tmp = path + ".tmp"
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_load_missing_file_returns_empty(self):
        with patch("options_bot.journal.STATE_FILE", "/tmp/nonexistent_options_bot_xyz.json"):
            s = load_state()
        assert s["algos"] == {}

    def test_load_corrupt_file_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{bad json{{")
            path = f.name
        try:
            with patch("options_bot.journal.STATE_FILE", path):
                s = load_state()
            assert s["algos"] == {}
        finally:
            os.unlink(path)


# ── Reporting ─────────────────────────────────────────────────────────────────────

class TestReporting:
    def test_performance_summary_empty(self):
        state = ensure_slots(_empty())
        s     = performance_summary(state)
        assert s["total_trades"] == 0
        assert s["open_trades"]  == 0
        assert s["total_pnl_usd"] == 0.0

    def test_performance_summary_with_closed_trade(self):
        state = ensure_slots(_empty())
        state = add_completed(state, _closed_trade("t1", 75.0))
        # Also record in the slot
        slots  = get_slots(state)
        key    = f"{SignalType.ORB.value}-SPY"
        if key in slots:
            slots[key].total_trades   = 1
            slots[key].winning_trades = 1
            slots[key].total_pnl_usd  = 75.0
        state = put_slots(state, slots)
        s     = performance_summary(state)
        assert s["total_pnl_usd"] == pytest.approx(75.0)
        assert s["win_rate_pct"]  == pytest.approx(100.0)

    def test_format_status_contains_header(self):
        state = ensure_slots(_empty())
        out   = format_status(state)
        assert "OPTIONS BOT" in out
        assert "PAPER TRADING" in out
        assert "LIMITED" in out
