"""Tests for execution.py — adverse slippage, vol-sensitivity, gap fills, P&L."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
from datetime import datetime, timezone, timedelta

from signal_trading.execution import (
    open_trade, close_trade, check_exit_conditions,
    update_trade_prices, check_all_stops, _fill_price, _gap_fill_price,
)
from signal_trading.models import Signal, Direction, ExitReason
from signal_trading.config import RiskConfig, ExecutionConfig


def _sig(direction: Direction = Direction.LONG, price: float = 1000.0,
         asset: str = "BTC", strategy: str = "rsi",
         indicators: dict = None) -> Signal:
    return Signal(
        asset=asset, strategy=strategy, direction=direction,
        price=price, timestamp=datetime.now(timezone.utc),
        indicators=indicators or {},
    )


def _open(direction: Direction = Direction.LONG, price: float = 1000.0,
          cfg: RiskConfig = None, exec_cfg: ExecutionConfig = None) -> "Trade":
    from signal_trading.config import RISK_CFG, EXEC_CFG
    return open_trade(_sig(direction, price), cfg or RISK_CFG, exec_cfg or EXEC_CFG)


# ── Slippage model tests ────────────────────────────────────────────────────

class TestFillPrice:
    def test_always_adverse_long(self):
        """Buys must always fill ABOVE mid."""
        cfg = ExecutionConfig(base_slippage_bps=5.0, jitter_bps=0)
        for _ in range(100):
            fill = _fill_price(1000.0, Direction.LONG, cfg)
            assert fill > 1000.0, f"buy fill {fill} should be > 1000.0"

    def test_always_adverse_short(self):
        """Sells must always fill BELOW mid."""
        cfg = ExecutionConfig(base_slippage_bps=5.0, jitter_bps=0)
        for _ in range(100):
            fill = _fill_price(1000.0, Direction.SHORT, cfg)
            assert fill < 1000.0, f"sell fill {fill} should be < 1000.0"

    def test_volatility_increases_slippage(self):
        """High ATR/price should produce worse fills than calm conditions."""
        cfg = ExecutionConfig(
            base_slippage_bps=3.0, vol_bps_per_unit=10.0,
            vol_calm_atr_ratio=0.005, jitter_bps=0,
        )
        # Calm: ATR = 3.0 on price 1000 → ATR/price = 0.3% (below 0.5%)
        fill_calm = _fill_price(1000.0, Direction.LONG, cfg, atr=3.0)
        # Volatile: ATR = 15.0 on price 1000 → ATR/price = 1.5%
        fill_vol = _fill_price(1000.0, Direction.LONG, cfg, atr=15.0)

        assert fill_vol > fill_calm, (
            f"volatile fill {fill_vol} should be worse (higher) than calm {fill_calm}"
        )

    def test_size_scaling(self):
        """Larger orders should get worse fills when size scaling is enabled."""
        cfg = ExecutionConfig(
            base_slippage_bps=3.0, size_bps_per_sqrt=2.0,
            size_ref_usd=10000.0, jitter_bps=0,
        )
        fill_small = _fill_price(1000.0, Direction.LONG, cfg, order_usd=100.0)
        fill_large = _fill_price(1000.0, Direction.LONG, cfg, order_usd=100000.0)
        assert fill_large > fill_small

    def test_zero_price(self):
        cfg = ExecutionConfig()
        fill = _fill_price(0, Direction.LONG, cfg)
        assert fill == 0

    def test_base_bps_magnitude(self):
        """Base slippage of 10 bps on 10000 should move price by ~1.0."""
        cfg = ExecutionConfig(base_slippage_bps=10.0, jitter_bps=0)
        fill = _fill_price(10000.0, Direction.LONG, cfg)
        slip = fill - 10000.0
        assert abs(slip - 10.0) < 0.01, f"expected ~10.0 slip, got {slip}"


class TestGapFillPrice:
    def test_long_stop_gap_through(self):
        """When price gaps below stop, fill should be at gap price, not stop."""
        cfg = ExecutionConfig(base_slippage_bps=3.0, jitter_bps=0)
        stop_price = 950.0
        gap_price = 930.0  # gapped 2% below stop

        fill = _gap_fill_price(stop_price, gap_price, Direction.SHORT, cfg)

        # Fill should be at or below gap price (adverse for sell)
        assert fill <= gap_price, f"fill {fill} should be <= gap {gap_price}"
        assert fill < stop_price, f"fill {fill} should be < stop {stop_price}"

    def test_short_stop_gap_through(self):
        """When price gaps above stop (short), fill should be at gap price."""
        cfg = ExecutionConfig(base_slippage_bps=3.0, jitter_bps=0)
        stop_price = 1050.0
        gap_price = 1080.0  # gapped 3% above stop

        fill = _gap_fill_price(stop_price, gap_price, Direction.LONG, cfg)

        # Fill should be at or above gap price (adverse for buy-to-cover)
        assert fill >= gap_price, f"fill {fill} should be >= gap {gap_price}"

    def test_no_gap(self):
        """When price is exactly at stop level, just apply normal slippage."""
        cfg = ExecutionConfig(base_slippage_bps=3.0, jitter_bps=0)
        fill = _gap_fill_price(950.0, 950.0, Direction.SHORT, cfg)
        assert fill < 950.0  # adverse sell slippage


# ── Open trade tests ─────────────────────────────────────────────────────────

class TestOpenTrade:
    def test_creates_trade(self):
        t = _open()
        assert t.id
        assert t.direction == Direction.LONG
        assert t.is_open
        assert t.entry_fee > 0

    def test_slippage_applied_long(self):
        cfg = ExecutionConfig(base_slippage_bps=10.0, jitter_bps=0)
        t = open_trade(_sig(Direction.LONG, 1000.0), RiskConfig(), cfg)
        assert t.entry_price > 1000.0  # filled higher (adverse)

    def test_slippage_applied_short(self):
        cfg = ExecutionConfig(base_slippage_bps=10.0, jitter_bps=0)
        t = open_trade(_sig(Direction.SHORT, 1000.0), RiskConfig(), cfg)
        assert t.entry_price < 1000.0  # filled lower (adverse)

    def test_size_from_config(self):
        cfg = RiskConfig(trade_size_usd=75.0)
        t = open_trade(_sig(), cfg)
        assert t.size_usd == 75.0

    def test_atr_from_indicators(self):
        """ATR in indicators should produce worse fills than no ATR."""
        cfg_no_jitter = ExecutionConfig(
            base_slippage_bps=3.0, vol_bps_per_unit=10.0,
            vol_calm_atr_ratio=0.005, jitter_bps=0,
        )
        # Without ATR
        sig_no_atr = _sig(Direction.LONG, 1000.0, indicators={})
        t_no_atr = open_trade(sig_no_atr, RiskConfig(), cfg_no_jitter)

        # With high ATR
        sig_atr = _sig(Direction.LONG, 1000.0, indicators={"atr": 15.0})
        t_atr = open_trade(sig_atr, RiskConfig(), cfg_no_jitter)

        assert t_atr.entry_price > t_no_atr.entry_price


# ── Exit condition tests ─────────────────────────────────────────────────────

class TestCheckExitConditions:
    def test_stop_loss_long(self):
        cfg = RiskConfig(stop_loss_pct=3.0)
        t = _open(Direction.LONG, 1000.0, cfg)
        reason = check_exit_conditions(t, 965.0, cfg)
        assert reason == ExitReason.STOP_LOSS

    def test_stop_loss_short(self):
        cfg = RiskConfig(stop_loss_pct=3.0)
        t = _open(Direction.SHORT, 1000.0, cfg)
        reason = check_exit_conditions(t, 1035.0, cfg)
        assert reason == ExitReason.STOP_LOSS

    def test_take_profit_long(self):
        cfg = RiskConfig(take_profit_pct=8.0)
        t = _open(Direction.LONG, 1000.0, cfg)
        reason = check_exit_conditions(t, 1085.0, cfg)
        assert reason == ExitReason.TAKE_PROFIT

    def test_trailing_stop_triggers(self):
        cfg = RiskConfig(trailing_stop_pct=2.0, take_profit_pct=50.0)
        t = _open(Direction.LONG, 1000.0, cfg)
        update_trade_prices(t, 1100.0)
        reason = check_exit_conditions(t, 1070.0, cfg)
        assert reason == ExitReason.TRAILING_STOP

    def test_trailing_stop_not_before_profit(self):
        cfg = RiskConfig(trailing_stop_pct=2.0, stop_loss_pct=5.0)
        t = _open(Direction.LONG, 1000.0, cfg)
        reason = check_exit_conditions(t, 990.0, cfg)
        assert reason is None

    def test_time_limit(self):
        cfg = RiskConfig(max_hold_hours=1.0)
        t = _open()
        t.entry_time = datetime.now(timezone.utc) - timedelta(hours=2)
        reason = check_exit_conditions(t, 1000.0, cfg)
        assert reason == ExitReason.TIME_LIMIT

    def test_no_exit_in_normal_conditions(self):
        cfg = RiskConfig(stop_loss_pct=3.0, take_profit_pct=8.0,
                         trailing_stop_pct=2.0, max_hold_hours=4.0)
        t = _open(Direction.LONG, 1000.0, cfg)
        reason = check_exit_conditions(t, 1010.0, cfg)
        assert reason is None


# ── Close trade tests ────────────────────────────────────────────────────────

class TestCloseTrade:
    def test_pnl_positive_on_winning_long(self):
        t = _open(Direction.LONG, 1000.0)
        closed = close_trade(t, 1050.0, ExitReason.TAKE_PROFIT)
        assert closed.net_pnl > 0
        assert closed.pnl_pct > 0
        assert not closed.is_open

    def test_pnl_negative_on_losing_long(self):
        t = _open(Direction.LONG, 1000.0)
        closed = close_trade(t, 970.0, ExitReason.STOP_LOSS)
        assert closed.net_pnl < 0
        assert closed.hold_minutes >= 0

    def test_pnl_positive_on_winning_short(self):
        t = _open(Direction.SHORT, 1000.0)
        closed = close_trade(t, 950.0, ExitReason.TAKE_PROFIT)
        assert closed.net_pnl > 0

    def test_fees_deducted(self):
        cfg = RiskConfig(trade_size_usd=100.0, fee_pct=0.001)
        t = open_trade(_sig(price=1000.0), cfg)
        closed = close_trade(t, 1000.0, ExitReason.TIME_LIMIT, cfg)
        assert closed.net_pnl < 0

    def test_gap_stop_loss_fills_worse_than_stop(self):
        """Stop-loss close should use gap-aware fill when price gaps through."""
        cfg = RiskConfig(stop_loss_pct=3.0, trade_size_usd=100.0, fee_pct=0.001)
        exec_cfg = ExecutionConfig(base_slippage_bps=3.0, jitter_bps=0)
        t = open_trade(_sig(Direction.LONG, 1000.0), cfg, exec_cfg)

        # Price gapped to 960 (through the ~970 stop level)
        closed = close_trade(t, 960.0, ExitReason.STOP_LOSS, cfg, exec_cfg)

        # Exit price should be at or below the gap price (not at stop)
        assert closed.exit_price <= 960.0, (
            f"gap stop fill {closed.exit_price} should be <= gap price 960.0"
        )


# ── Batch stop check tests ──────────────────────────────────────────────────

class TestCheckAllStops:
    def test_returns_empty_when_no_open_trades(self):
        result = check_all_stops([], {"BTC": 1000.0})
        assert result == []

    def test_detects_stop_loss(self):
        cfg = RiskConfig(stop_loss_pct=3.0)
        t = _open(Direction.LONG, 1000.0, cfg)
        to_close = check_all_stops([t], {"BTC": 960.0}, cfg)
        assert len(to_close) == 1
        _, reason, _ = to_close[0]
        assert reason == ExitReason.STOP_LOSS

    def test_skips_trade_with_no_price(self):
        t = _open()
        to_close = check_all_stops([t], {})
        assert to_close == []
