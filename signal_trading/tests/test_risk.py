"""Tests for risk.py — position gates, circuit breaker, kill switch."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
from datetime import datetime, timezone, timedelta

from signal_trading.risk import (
    check_can_open, record_trade_result, deduct_for_entry, update_portfolio_risk,
)
from signal_trading.models import AlgoState, PortfolioRisk, Trade, Direction, ExitReason
from signal_trading.config import RiskConfig, RISK_CFG


def _make_algo(key: str = "rsi-BTC", capital: float = 1000.0) -> AlgoState:
    strategy, asset = key.rsplit("-", 1)
    return AlgoState(
        strategy=strategy, asset=asset,
        initial_capital=capital, available_capital=capital, peak_capital=capital,
    )


def _make_algos(*keys) -> dict:
    return {k: _make_algo(k) for k in keys}


def _make_trade(asset: str = "BTC", strategy: str = "rsi",
                net_pnl: float = 0.0, size: float = 50.0) -> Trade:
    t = Trade(
        id=f"{strategy}-{asset}-test",
        asset=asset, strategy=strategy,
        direction=Direction.LONG,
        entry_price=100.0, entry_time=datetime.now(timezone.utc),
        size_usd=size, entry_fee=0.05,
    )
    t.net_pnl = net_pnl
    t.exit_fee = 0.05
    t.exit_reason = ExitReason.TAKE_PROFIT if net_pnl >= 0 else ExitReason.STOP_LOSS
    return t


class TestCheckCanOpen:
    def test_happy_path(self):
        algos = _make_algos("rsi-BTC")
        pr = PortfolioRisk()
        ok, msg = check_can_open("BTC", "rsi", algos, pr, [])
        assert ok

    def test_kill_switch_blocks(self):
        algos = _make_algos("rsi-BTC")
        pr = PortfolioRisk(kill_switch_active=True, kill_switch_at=datetime.now(timezone.utc))
        ok, msg = check_can_open("BTC", "rsi", algos, pr, [])
        assert not ok
        assert "kill switch" in msg.lower()

    def test_circuit_breaker_blocks(self):
        algos = _make_algos("rsi-BTC")
        algos["rsi-BTC"].circuit_breaker_until = datetime.now(timezone.utc) + timedelta(hours=1)
        pr = PortfolioRisk()
        ok, msg = check_can_open("BTC", "rsi", algos, pr, [])
        assert not ok
        assert "circuit breaker" in msg.lower()

    def test_expired_circuit_breaker_allows(self):
        algos = _make_algos("rsi-BTC")
        algos["rsi-BTC"].circuit_breaker_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        pr = PortfolioRisk()
        ok, _ = check_can_open("BTC", "rsi", algos, pr, [])
        assert ok

    def test_concurrent_limit_blocks(self):
        algos = _make_algos("rsi-BTC", "macd-BTC", "sma_crossover-BTC")
        cfg = RiskConfig(max_concurrent_per_asset=2)
        pr = PortfolioRisk()
        open_trades = [_make_trade("BTC"), _make_trade("BTC")]  # 2 open BTC trades
        ok, msg = check_can_open("BTC", "rsi", algos, pr, open_trades, cfg)
        assert not ok
        assert "max concurrent" in msg.lower()

    def test_insufficient_capital_blocks(self):
        algos = _make_algos("rsi-BTC")
        algos["rsi-BTC"].available_capital = 1.0  # way too low
        pr = PortfolioRisk()
        ok, msg = check_can_open("BTC", "rsi", algos, pr, [])
        assert not ok
        assert "capital" in msg.lower()

    def test_uninitialised_algo_blocks(self):
        algos = {}
        pr = PortfolioRisk()
        ok, msg = check_can_open("BTC", "rsi", algos, pr, [])
        assert not ok


class TestRecordTradeResult:
    def test_winning_trade_clears_losses(self):
        algo = _make_algo("rsi-BTC")
        algo.consecutive_losses = 3
        trade = _make_trade(net_pnl=5.0)
        algo = record_trade_result(algo, trade, RISK_CFG)
        assert algo.winning_trades == 1
        assert algo.consecutive_losses == 0
        assert algo.total_pnl == pytest.approx(5.0, 0.01)

    def test_losing_trade_increments_losses(self):
        algo = _make_algo("rsi-BTC")
        trade = _make_trade(net_pnl=-3.0)
        algo = record_trade_result(algo, trade, RISK_CFG)
        assert algo.losing_trades == 1
        assert algo.consecutive_losses == 1

    def test_circuit_breaker_triggers_at_threshold(self):
        cfg = RiskConfig(circuit_breaker_losses=3, circuit_breaker_pause_hours=1)
        algo = _make_algo("rsi-BTC")
        algo.consecutive_losses = 2  # one more will trigger
        trade = _make_trade(net_pnl=-1.0)
        algo = record_trade_result(algo, trade, cfg)
        assert algo.circuit_breaker_active
        assert algo.circuit_breaker_until is not None

    def test_capital_returned_on_close(self):
        algo = _make_algo("rsi-BTC", capital=1000.0)
        algo.available_capital = 950.0  # already deducted for open
        trade = _make_trade(net_pnl=5.0, size=50.0)
        algo = record_trade_result(algo, trade, RISK_CFG)
        assert algo.available_capital == pytest.approx(950.0 + 50.0 + 5.0, 0.01)


class TestUpdatePortfolioRisk:
    def test_kill_switch_triggers_on_drawdown(self):
        cfg = RiskConfig(portfolio_kill_switch_pct=10.0)
        algos = {"rsi-BTC": _make_algo("rsi-BTC", 1000.0)}
        algos["rsi-BTC"].available_capital = 850.0  # 15% down from peak
        pr = PortfolioRisk(peak_value=1000.0)
        pr = update_portfolio_risk(pr, algos, [], {}, cfg)
        assert pr.kill_switch_active

    def test_no_kill_switch_below_threshold(self):
        cfg = RiskConfig(portfolio_kill_switch_pct=20.0)
        algos = {"rsi-BTC": _make_algo("rsi-BTC", 1000.0)}
        algos["rsi-BTC"].available_capital = 950.0  # 5% down
        pr = PortfolioRisk(peak_value=1000.0)
        pr = update_portfolio_risk(pr, algos, [], {}, cfg)
        assert not pr.kill_switch_active
