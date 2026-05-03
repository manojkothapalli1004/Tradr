"""
Tests for signal_trading/safety.py — staged portfolio safety layer.

Tests cover:
  - Default initialisation → NORMAL
  - Each threshold triggers correct stage
  - No intraday de-escalation (recovery doesn't lower stage)
  - Day rollover resets non-locked stages
  - HARD_LOCKED persists across days while lock file exists
  - HARD_LOCKED clears when lock file is manually deleted
  - Size multiplier values for each stage
  - check_safety_stage gate decisions
  - Integration: check_can_open blocks at NO_NEW_RISK / HARD_LOCKED
"""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from signal_trading.config import PortfolioSafetyConfig, RiskConfig, RISK_CFG
from signal_trading.models import (
    AlgoState, Direction, PortfolioRisk, RiskStage, Trade,
)
from signal_trading.safety import (
    evaluate_safety,
    check_safety_stage,
    get_size_multiplier,
    _compute_portfolio_value,
    _init_safety_state,
    _stage_from_thresholds,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def safety_cfg():
    return PortfolioSafetyConfig(
        reduced_risk_pct=1.5,
        no_new_risk_pct=3.0,
        hard_lock_pct=5.0,
        reduced_size_multiplier=0.5,
    )


@pytest.fixture
def algos():
    """Two algo states with $1000 each available."""
    return {
        "macd-BTC": AlgoState(
            strategy="macd", asset="BTC",
            initial_capital=1000, available_capital=1000, peak_capital=1000,
        ),
        "rsi-ETH": AlgoState(
            strategy="rsi", asset="ETH",
            initial_capital=1000, available_capital=1000, peak_capital=1000,
        ),
    }


@pytest.fixture
def prices():
    return {"BTC": 50000.0, "ETH": 3000.0}


@pytest.fixture
def empty_trades():
    return []


@pytest.fixture
def state():
    return {
        "algos": {},
        "open_trades": [],
        "completed_trades": [],
        "portfolio_risk": PortfolioRisk().to_dict(),
        "last_regimes": {},
        "safety_state": {},
    }


@pytest.fixture
def tmp_lock_file(tmp_path):
    """Patch HARD_LOCK_FILE to a temp path for isolation."""
    lock_path = str(tmp_path / "HARD_LOCK")
    with patch("signal_trading.safety.HARD_LOCK_FILE", lock_path):
        yield lock_path


# ── Portfolio value computation ──────────────────────────────────────────────

class TestComputePortfolioValue:
    def test_available_capital_only(self, algos, empty_trades, prices):
        val = _compute_portfolio_value(algos, empty_trades, prices)
        assert val == 2000.0  # 1000 + 1000

    def test_with_open_trades(self, algos, prices):
        trade = Trade(
            id="t1", asset="BTC", strategy="macd",
            direction=Direction.LONG, entry_price=50000.0,
            entry_time=datetime.now(timezone.utc),
            size_usd=50.0, entry_fee=0.05,
        )
        val = _compute_portfolio_value(algos, [trade], prices)
        # Capital + trade at no-change = 2000 + 50 (size_usd + 0 pnl)
        assert val == 2050.0

    def test_missing_price_uses_cost_basis(self, algos):
        trade = Trade(
            id="t1", asset="BTC", strategy="macd",
            direction=Direction.LONG, entry_price=50000.0,
            entry_time=datetime.now(timezone.utc),
            size_usd=50.0, entry_fee=0.05,
        )
        val = _compute_portfolio_value(algos, [trade], {})  # no prices
        assert val == 2050.0  # uses size_usd as fallback


# ── Stage from thresholds ────────────────────────────────────────────────────

class TestStageFromThresholds:
    def test_normal(self, safety_cfg):
        assert _stage_from_thresholds(0.0, safety_cfg) == RiskStage.NORMAL
        assert _stage_from_thresholds(1.0, safety_cfg) == RiskStage.NORMAL
        assert _stage_from_thresholds(1.49, safety_cfg) == RiskStage.NORMAL

    def test_reduced_risk(self, safety_cfg):
        assert _stage_from_thresholds(1.5, safety_cfg) == RiskStage.REDUCED_RISK
        assert _stage_from_thresholds(2.99, safety_cfg) == RiskStage.REDUCED_RISK

    def test_no_new_risk(self, safety_cfg):
        assert _stage_from_thresholds(3.0, safety_cfg) == RiskStage.NO_NEW_RISK
        assert _stage_from_thresholds(4.99, safety_cfg) == RiskStage.NO_NEW_RISK

    def test_hard_locked(self, safety_cfg):
        assert _stage_from_thresholds(5.0, safety_cfg) == RiskStage.HARD_LOCKED
        assert _stage_from_thresholds(10.0, safety_cfg) == RiskStage.HARD_LOCKED


# ── evaluate_safety ──────────────────────────────────────────────────────────

class TestEvaluateSafety:
    def test_first_run_returns_normal(self, state, algos, empty_trades, prices, safety_cfg):
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert stage == RiskStage.NORMAL
        assert state["safety_state"]["current_stage"] == "normal"
        assert state["safety_state"]["day_start_value"] == 2000.0

    def test_no_drawdown_stays_normal(self, state, algos, empty_trades, prices, safety_cfg):
        # First call: initialise
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        # Second call: same value, still normal
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert stage == RiskStage.NORMAL

    def test_reduced_risk_on_drawdown(self, state, algos, empty_trades, prices, safety_cfg):
        # Init at 2000
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        # Simulate loss: reduce capital to trigger 1.5% dd
        algos["macd-BTC"].available_capital = 970  # 2000 -> 1970 = 1.5% dd
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert stage == RiskStage.REDUCED_RISK

    def test_no_new_risk_on_deeper_drawdown(self, state, algos, empty_trades, prices, safety_cfg):
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        algos["macd-BTC"].available_capital = 940  # 2000 -> 1940 = 3% dd
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert stage == RiskStage.NO_NEW_RISK

    def test_hard_locked_on_severe_drawdown(self, state, algos, empty_trades, prices, safety_cfg, tmp_lock_file):
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        algos["macd-BTC"].available_capital = 900  # 2000 -> 1900 = 5% dd
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert stage == RiskStage.HARD_LOCKED
        # Lock file should be written
        assert os.path.exists(tmp_lock_file)
        with open(tmp_lock_file) as f:
            lock_data = json.load(f)
        assert "locked_at" in lock_data
        assert lock_data["daily_drawdown_pct"] == 5.0

    def test_no_intraday_deescalation(self, state, algos, empty_trades, prices, safety_cfg):
        """Portfolio recovery should NOT lower the stage within the same day."""
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)

        # Trigger REDUCED_RISK
        algos["macd-BTC"].available_capital = 970
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert state["safety_state"]["current_stage"] == "reduced_risk"

        # Recover — capital goes back up
        algos["macd-BTC"].available_capital = 1000
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        # Should still be REDUCED_RISK, NOT NORMAL
        assert stage == RiskStage.REDUCED_RISK

    def test_day_rollover_resets_non_locked(self, state, algos, empty_trades, prices, safety_cfg):
        """REDUCED_RISK and NO_NEW_RISK reset to NORMAL on new day."""
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)

        # Trigger REDUCED_RISK
        algos["macd-BTC"].available_capital = 970
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert state["safety_state"]["current_stage"] == "reduced_risk"

        # Simulate new day
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        state["safety_state"]["day_date"] = yesterday

        # Capital recovered
        algos["macd-BTC"].available_capital = 1000
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert stage == RiskStage.NORMAL

    def test_hard_lock_persists_across_days(self, state, algos, empty_trades, prices, safety_cfg, tmp_lock_file):
        """HARD_LOCKED does NOT reset on day rollover while lock file exists."""
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)

        # Trigger HARD_LOCKED
        algos["macd-BTC"].available_capital = 900
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert state["safety_state"]["current_stage"] == "hard_locked"
        assert os.path.exists(tmp_lock_file)

        # Simulate new day
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        state["safety_state"]["day_date"] = yesterday

        algos["macd-BTC"].available_capital = 1000  # recovered
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        # Still locked because file exists
        assert stage == RiskStage.HARD_LOCKED

    def test_hard_lock_clears_after_file_deleted(self, state, algos, empty_trades, prices, safety_cfg, tmp_lock_file):
        """HARD_LOCKED resets to NORMAL when operator deletes lock file + new day."""
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)

        # Trigger HARD_LOCKED
        algos["macd-BTC"].available_capital = 900
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert os.path.exists(tmp_lock_file)

        # Operator deletes lock file
        os.remove(tmp_lock_file)

        # Simulate new day
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        state["safety_state"]["day_date"] = yesterday

        algos["macd-BTC"].available_capital = 1000
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert stage == RiskStage.NORMAL

    def test_escalation_jumps_stages(self, state, algos, empty_trades, prices, safety_cfg, tmp_lock_file):
        """Can jump directly from NORMAL to HARD_LOCKED."""
        evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        algos["macd-BTC"].available_capital = 900
        stage = evaluate_safety(state, algos, empty_trades, prices, RISK_CFG, safety_cfg)
        assert stage == RiskStage.HARD_LOCKED


# ── check_safety_stage ───────────────────────────────────────────────────────

class TestCheckSafetyStage:
    def test_normal_allowed(self):
        allowed, reason = check_safety_stage(RiskStage.NORMAL)
        assert allowed is True

    def test_reduced_risk_allowed(self):
        allowed, reason = check_safety_stage(RiskStage.REDUCED_RISK)
        assert allowed is True

    def test_no_new_risk_blocked(self):
        allowed, reason = check_safety_stage(RiskStage.NO_NEW_RISK)
        assert allowed is False
        assert "NO_NEW_RISK" in reason

    def test_hard_locked_blocked(self):
        allowed, reason = check_safety_stage(RiskStage.HARD_LOCKED)
        assert allowed is False
        assert "HARD_LOCKED" in reason
        assert "delete" in reason.lower()


# ── get_size_multiplier ──────────────────────────────────────────────────────

class TestGetSizeMultiplier:
    def test_normal(self, safety_cfg):
        assert get_size_multiplier(RiskStage.NORMAL, safety_cfg) == 1.0

    def test_reduced_risk(self, safety_cfg):
        assert get_size_multiplier(RiskStage.REDUCED_RISK, safety_cfg) == 0.5

    def test_no_new_risk(self, safety_cfg):
        assert get_size_multiplier(RiskStage.NO_NEW_RISK, safety_cfg) == 0.0

    def test_hard_locked(self, safety_cfg):
        assert get_size_multiplier(RiskStage.HARD_LOCKED, safety_cfg) == 0.0

    def test_custom_multiplier(self):
        cfg = PortfolioSafetyConfig(reduced_size_multiplier=0.25)
        assert get_size_multiplier(RiskStage.REDUCED_RISK, cfg) == 0.25


# ── Integration with check_can_open ──────────────────────────────────────────

class TestCheckCanOpenIntegration:
    def test_none_safety_stage_skips_check(self, algos, empty_trades):
        """Backward compat: safety_stage=None skips the safety check entirely."""
        from signal_trading.risk import check_can_open
        pr = PortfolioRisk()
        allowed, _ = check_can_open("BTC", "macd", algos, pr, empty_trades, RISK_CFG,
                                     safety_stage=None)
        assert allowed is True

    def test_normal_stage_allows(self, algos, empty_trades):
        from signal_trading.risk import check_can_open
        pr = PortfolioRisk()
        allowed, _ = check_can_open("BTC", "macd", algos, pr, empty_trades, RISK_CFG,
                                     safety_stage=RiskStage.NORMAL)
        assert allowed is True

    def test_reduced_risk_allows(self, algos, empty_trades):
        from signal_trading.risk import check_can_open
        pr = PortfolioRisk()
        allowed, _ = check_can_open("BTC", "macd", algos, pr, empty_trades, RISK_CFG,
                                     safety_stage=RiskStage.REDUCED_RISK)
        assert allowed is True

    def test_no_new_risk_blocks(self, algos, empty_trades):
        from signal_trading.risk import check_can_open
        pr = PortfolioRisk()
        allowed, reason = check_can_open("BTC", "macd", algos, pr, empty_trades, RISK_CFG,
                                          safety_stage=RiskStage.NO_NEW_RISK)
        assert allowed is False
        assert "NO_NEW_RISK" in reason

    def test_hard_locked_blocks(self, algos, empty_trades):
        from signal_trading.risk import check_can_open
        pr = PortfolioRisk()
        allowed, reason = check_can_open("BTC", "macd", algos, pr, empty_trades, RISK_CFG,
                                          safety_stage=RiskStage.HARD_LOCKED)
        assert allowed is False
        assert "HARD_LOCKED" in reason
