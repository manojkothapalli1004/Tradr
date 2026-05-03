#!/usr/bin/env python3
"""
Test suite for Paper Trading V3 — validates trailing stops, fees,
risk management, atomic saves, and P&L calculations.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

# Add paths
sys.path.insert(0, os.path.dirname(__file__))
from paper_trading_manager import PaperTradingManager, load_config, DEFAULT_CONFIG


class TestTrailingStop(unittest.TestCase):
    """Test trailing stop detection and execution."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self.tmp.close()
        self.mgr = PaperTradingManager(state_file=self.tmp.name)
        self.mgr.config = DEFAULT_CONFIG.copy()
        self.mgr.initialize_algo("momentum", "BTC")

    def tearDown(self):
        os.unlink(self.tmp.name)
        tmp_path = self.tmp.name + ".tmp"
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    def test_trailing_stop_buy_triggers(self):
        """BUY trade: price goes up to $105, then drops 2% → trailing stop fires."""
        self.mgr.open_trade("momentum", "BTC", "buy", 100.0, "test")
        trade = self.mgr.state["active_trades"][0]
        self.assertEqual(trade["highest_price"], 100.0)

        # Price goes to 105 — no exit, but highest updates
        actions = self.mgr.check_trailing_stop_and_time_limit({"BTC": 105.0})
        self.assertEqual(len(actions), 0)
        # Reload state (was saved)
        self.mgr.state = self.mgr.load_state()
        # Note: trade was not closed, so it should still be in active_trades
        if self.mgr.state["active_trades"]:
            self.assertEqual(self.mgr.state["active_trades"][0]["highest_price"], 105.0)

        # Price drops to 102.89 (2% below 105 = 102.90) — should trigger
        actions = self.mgr.check_trailing_stop_and_time_limit({"BTC": 102.89})
        self.assertEqual(len(actions), 1)
        self.assertIn("TRAILING STOP", actions[0]["reason"])
        self.assertGreater(actions[0]["net_pnl"], 0)  # Still profitable

    def test_trailing_stop_only_after_profit(self):
        """Trailing stop should NOT fire if price never went above entry."""
        self.mgr.open_trade("momentum", "BTC", "buy", 100.0, "test")

        # Price drops to 98 — this is a 2% drop but from entry, not from peak
        # Trailing stop requires price to have moved favorably first
        actions = self.mgr.check_trailing_stop_and_time_limit({"BTC": 98.0})
        # Should NOT trigger trailing stop (highest=100, 2% below 100 = 98, but
        # highest hasn't exceeded entry, so trailing should not fire)
        # The hard stop is at 5% so 98 won't hit that either
        self.assertEqual(len(actions), 0)

    def test_trailing_stop_sell_triggers(self):
        """SELL trade: price goes down, then bounces up 2% → trailing stop fires."""
        self.mgr.open_trade("momentum", "BTC", "sell", 100.0, "test")

        # Price drops to 95
        actions = self.mgr.check_trailing_stop_and_time_limit({"BTC": 95.0})
        self.assertEqual(len(actions), 0)

        # Price bounces to 96.91 (2% above 95 = 96.90) — triggers
        actions = self.mgr.check_trailing_stop_and_time_limit({"BTC": 96.91})
        self.assertEqual(len(actions), 1)
        self.assertIn("TRAILING STOP", actions[0]["reason"])

    def test_hard_stop_loss(self):
        """5% drop triggers hard stop loss."""
        self.mgr.open_trade("momentum", "BTC", "buy", 100.0, "test")

        actions = self.mgr.check_trailing_stop_and_time_limit({"BTC": 94.99})
        self.assertEqual(len(actions), 1)
        self.assertIn("STOP LOSS", actions[0]["reason"])

    def test_take_profit(self):
        """8% gain triggers take profit."""
        self.mgr.open_trade("momentum", "BTC", "buy", 100.0, "test")

        actions = self.mgr.check_trailing_stop_and_time_limit({"BTC": 108.01})
        self.assertEqual(len(actions), 1)
        self.assertIn("TAKE PROFIT", actions[0]["reason"])


class TestTimeLimit(unittest.TestCase):
    """Test 2-hour time limit enforcement."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self.tmp.close()
        self.mgr = PaperTradingManager(state_file=self.tmp.name)
        self.mgr.config = DEFAULT_CONFIG.copy()
        self.mgr.initialize_algo("rsi", "BTC")

    def tearDown(self):
        os.unlink(self.tmp.name)
        tmp_path = self.tmp.name + ".tmp"
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    def test_time_limit_triggers_at_2h(self):
        """Trade older than 2 hours should be force-closed."""
        self.mgr.open_trade("rsi", "BTC", "buy", 100.0, "test")

        # Manually backdate the entry time
        trade = self.mgr.state["active_trades"][0]
        old_time = datetime.now(timezone.utc) - timedelta(hours=2, minutes=5)
        trade["entry_time"] = old_time.isoformat()
        self.mgr.save_state()

        actions = self.mgr.check_trailing_stop_and_time_limit({"BTC": 100.50})
        self.assertEqual(len(actions), 1)
        self.assertIn("TIME LIMIT", actions[0]["reason"])

    def test_no_exit_before_2h(self):
        """Trade younger than 2 hours should NOT be force-closed."""
        self.mgr.open_trade("rsi", "BTC", "buy", 100.0, "test")

        # Set time to 1.5 hours ago
        trade = self.mgr.state["active_trades"][0]
        trade["entry_time"] = (datetime.now(timezone.utc) - timedelta(hours=1, minutes=30)).isoformat()
        self.mgr.save_state()

        actions = self.mgr.check_trailing_stop_and_time_limit({"BTC": 100.50})
        self.assertEqual(len(actions), 0)


class TestFees(unittest.TestCase):
    """Test fee accounting."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self.tmp.close()
        self.mgr = PaperTradingManager(state_file=self.tmp.name)
        self.mgr.config = DEFAULT_CONFIG.copy()
        self.mgr.config["trade_size"] = 50
        self.mgr.initialize_algo("macd", "ETH")

    def tearDown(self):
        os.unlink(self.tmp.name)
        tmp_path = self.tmp.name + ".tmp"
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    def test_entry_fee_deducted(self):
        """Entry fee should be deducted from available capital."""
        initial = self.mgr.state["algos"]["macd-ETH"]["available_capital"]
        self.mgr.open_trade("macd", "ETH", "buy", 2000.0, "test")
        after = self.mgr.state["algos"]["macd-ETH"]["available_capital"]

        # Should deduct trade_size + fee (50 + 0.05 = 50.05)
        expected_deduction = 50 + 50 * 0.001
        self.assertAlmostEqual(initial - after, expected_deduction, places=2)

    def test_round_trip_fees(self):
        """Round-trip fees should be correctly recorded."""
        self.mgr.open_trade("macd", "ETH", "buy", 2000.0, "test")
        trade_id = self.mgr.state["active_trades"][0]["id"]

        # Close at 5% profit
        result = self.mgr.close_trade(trade_id, 2100.0, "test close")
        self.assertTrue(result["success"])

        trade = result["trade"]
        self.assertAlmostEqual(trade["entry_fee"], 0.05, places=2)
        self.assertAlmostEqual(trade["exit_fee"], 0.05, places=2)
        self.assertAlmostEqual(trade["total_fees"], 0.10, places=2)

        # Net P&L should be gross - exit_fee (entry already deducted from capital)
        gross = (2100 - 2000) / 2000 * 50  # $2.50
        expected_net = gross - 0.05  # $2.45
        self.assertAlmostEqual(trade["net_pnl"], expected_net, places=2)

    def test_fees_on_losing_trade(self):
        """Fees make losing trades worse."""
        self.mgr.open_trade("macd", "ETH", "buy", 2000.0, "test")
        trade_id = self.mgr.state["active_trades"][0]["id"]

        # Close at 1% loss
        result = self.mgr.close_trade(trade_id, 1980.0, "test loss")
        trade = result["trade"]

        # Gross loss: -1% of $50 = -$0.50
        # Net loss: -$0.50 - $0.05 exit fee = -$0.55
        self.assertLess(trade["net_pnl"], -0.50)


class TestRiskManagement(unittest.TestCase):
    """Test kill switch and circuit breaker."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self.tmp.close()
        self.mgr = PaperTradingManager(state_file=self.tmp.name)
        self.mgr.config = DEFAULT_CONFIG.copy()
        self.mgr.config["kill_switch_drawdown_pct"] = 15.0
        self.mgr.config["circuit_breaker_losses"] = 3  # Lower for testing
        self.mgr.initialize_algo("rsi", "BTC")

    def tearDown(self):
        os.unlink(self.tmp.name)
        tmp_path = self.tmp.name + ".tmp"
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    def test_circuit_breaker_after_consecutive_losses(self):
        """3 consecutive losses should trigger circuit breaker."""
        algo = self.mgr.state["algos"]["rsi-BTC"]

        # Simulate 3 consecutive losses
        for i in range(3):
            self.mgr.open_trade("rsi", "BTC", "buy", 100.0, f"loss {i}")
            trade_id = self.mgr.state["active_trades"][0]["id"]
            self.mgr.close_trade(trade_id, 95.0, "stop loss")

        # Now check risk — should be blocked (either "consecutive losses" or "circuit breaker active")
        allowed, reason = self.mgr.check_risk("rsi", "BTC")
        self.assertFalse(allowed)
        self.assertTrue("consecutive losses" in reason.lower() or "circuit breaker" in reason.lower())

    def test_circuit_breaker_resets_on_win(self):
        """A winning trade should reset the consecutive loss counter."""
        algo = self.mgr.state["algos"]["rsi-BTC"]

        # 2 losses
        for i in range(2):
            self.mgr.open_trade("rsi", "BTC", "buy", 100.0, f"loss {i}")
            trade_id = self.mgr.state["active_trades"][0]["id"]
            self.mgr.close_trade(trade_id, 95.0, "stop loss")

        # 1 win — resets counter
        self.mgr.open_trade("rsi", "BTC", "buy", 100.0, "win")
        trade_id = self.mgr.state["active_trades"][0]["id"]
        self.mgr.close_trade(trade_id, 110.0, "take profit")

        self.assertEqual(self.mgr.state["algos"]["rsi-BTC"]["consecutive_losses"], 0)

    def test_kill_switch_blocks_all_trading(self):
        """Kill switch should block trading when active."""
        self.mgr.state["portfolio_risk"]["kill_switch_active"] = True
        self.mgr.state["portfolio_risk"]["kill_switch_at"] = datetime.now(timezone.utc).isoformat()

        allowed, reason = self.mgr.check_risk("rsi", "BTC")
        self.assertFalse(allowed)
        self.assertIn("kill switch", reason.lower())


class TestAtomicSave(unittest.TestCase):
    """Test atomic state persistence."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self.tmp.close()
        self.mgr = PaperTradingManager(state_file=self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)
        tmp_path = self.tmp.name + ".tmp"
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    def test_save_creates_valid_json(self):
        """State file should always be valid JSON after save."""
        self.mgr.initialize_algo("test", "BTC")
        self.mgr.save_state()

        with open(self.tmp.name, 'r') as f:
            data = json.load(f)
        self.assertIn("algos", data)
        self.assertIn("test-BTC", data["algos"])

    def test_no_tmp_file_after_save(self):
        """Temp file should not exist after successful save."""
        self.mgr.save_state()
        self.assertFalse(os.path.exists(self.tmp.name + ".tmp"))

    def test_state_survives_reload(self):
        """State should be identical after save + reload."""
        self.mgr.initialize_algo("test", "BTC")
        self.mgr.open_trade("test", "BTC", "buy", 50000.0, "test")
        self.mgr.save_state()

        mgr2 = PaperTradingManager(state_file=self.tmp.name)
        self.assertEqual(len(mgr2.state["active_trades"]), 1)
        self.assertEqual(mgr2.state["active_trades"][0]["entry_price"], 50000.0)


class TestPnLCalculation(unittest.TestCase):
    """Test P&L calculations including hold time tracking."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self.tmp.close()
        self.mgr = PaperTradingManager(state_file=self.tmp.name)
        self.mgr.config = DEFAULT_CONFIG.copy()
        self.mgr.config["trade_size"] = 50
        self.mgr.initialize_algo("ema", "BTC")

    def tearDown(self):
        os.unlink(self.tmp.name)
        tmp_path = self.tmp.name + ".tmp"
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    def test_buy_profit_calculation(self):
        """BUY at $100, sell at $105 = 5% * $50 = $2.50 gross."""
        self.mgr.open_trade("ema", "BTC", "buy", 100.0, "test")
        trade_id = self.mgr.state["active_trades"][0]["id"]
        result = self.mgr.close_trade(trade_id, 105.0, "take profit")

        self.assertTrue(result["success"])
        self.assertGreater(result["net_pnl"], 0)
        # Gross = 5% * 50 = 2.50, minus exit fee 0.05 = 2.45
        self.assertAlmostEqual(result["net_pnl"], 2.45, places=2)

    def test_sell_profit_calculation(self):
        """SELL at $100, cover at $95 = 5% * $50 = $2.50 gross."""
        self.mgr.open_trade("ema", "BTC", "sell", 100.0, "test")
        trade_id = self.mgr.state["active_trades"][0]["id"]
        result = self.mgr.close_trade(trade_id, 95.0, "take profit")

        self.assertTrue(result["success"])
        self.assertGreater(result["net_pnl"], 0)

    def test_hold_time_tracked(self):
        """Hold time should be recorded in minutes."""
        self.mgr.open_trade("ema", "BTC", "buy", 100.0, "test")
        trade_id = self.mgr.state["active_trades"][0]["id"]

        # Backdate entry
        trade = self.mgr.state["active_trades"][0]
        trade["entry_time"] = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        self.mgr.save_state()

        result = self.mgr.close_trade(trade_id, 105.0, "test")
        self.assertGreater(result["hold_minutes"], 44)
        self.assertLess(result["hold_minutes"], 47)


class TestBestAlgos(unittest.TestCase):
    """Test algo ranking."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        self.tmp.close()
        self.mgr = PaperTradingManager(state_file=self.tmp.name)
        self.mgr.config = DEFAULT_CONFIG.copy()
        self.mgr.config["trade_size"] = 50
        self.mgr.initialize_algo("winner", "BTC")
        self.mgr.initialize_algo("loser", "BTC")

    def tearDown(self):
        os.unlink(self.tmp.name)
        tmp_path = self.tmp.name + ".tmp"
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    def test_ranking_order(self):
        """Winner algo should rank above loser."""
        # Winner: 3 profitable trades
        for _ in range(3):
            self.mgr.open_trade("winner", "BTC", "buy", 100.0, "win")
            tid = self.mgr.state["active_trades"][0]["id"]
            self.mgr.close_trade(tid, 105.0, "take profit")

        # Loser: 3 losing trades
        for _ in range(3):
            self.mgr.open_trade("loser", "BTC", "buy", 100.0, "loss")
            tid = self.mgr.state["active_trades"][0]["id"]
            self.mgr.close_trade(tid, 96.0, "stop loss")

        rankings = self.mgr.get_best_algos()
        self.assertEqual(len(rankings), 2)
        self.assertEqual(rankings[0]["algo"], "winner (BTC)")
        self.assertEqual(rankings[0]["verdict"], "PROFITABLE")
        self.assertEqual(rankings[1]["verdict"], "NEEDS WORK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
