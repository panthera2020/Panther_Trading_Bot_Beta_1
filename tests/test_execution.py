"""Unit tests for execution layer: session, risk, volume, position, sizing."""
import unittest
from datetime import datetime, timezone, timedelta

from execution.session_manager import SessionManager
from execution.risk_manager import RiskManager, RiskConfig
from execution.volume_manager import VolumeManager, VolumeConfig
from execution.position_manager import PositionManager, Position
from execution.qty_utils import normalize_qty, reduce_by_step
from backtest.engine import compute_size, check_sl_tp, close_trade, Trade


class TestSessionManager(unittest.TestCase):
    def setUp(self):
        self.sm = SessionManager()

    def test_asia_session(self):
        ts = datetime(2026, 3, 1, 3, 0, tzinfo=timezone.utc)  # 03:00 UTC
        session = self.sm.current_session(ts)
        self.assertEqual(session.name, "ASIA")
        self.assertEqual(session.strategy_id, "candle3")

    def test_london_session(self):
        ts = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)  # 10:00 UTC
        session = self.sm.current_session(ts)
        self.assertEqual(session.name, "LONDON")
        self.assertEqual(session.strategy_id, "scalp")

    def test_ny_session(self):
        ts = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)  # 18:00 UTC
        session = self.sm.current_session(ts)
        self.assertEqual(session.name, "NY")
        self.assertEqual(session.strategy_id, "trend")

    def test_no_overlap(self):
        """Each hour maps to exactly one session."""
        for hour in range(24):
            ts = datetime(2026, 3, 1, hour, 0, tzinfo=timezone.utc)
            session = self.sm.current_session(ts)
            self.assertIn(session.name, {"ASIA", "LONDON", "NY"})

    def test_strategy_allowed(self):
        ts_london = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(self.sm.is_strategy_allowed("scalp", ts_london))
        self.assertFalse(self.sm.is_strategy_allowed("trend", ts_london))
        self.assertFalse(self.sm.is_strategy_allowed("candle3", ts_london))


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager(RiskConfig(
            max_daily_loss_pct=0.03,
            max_consecutive_losses=3,
            max_orders_per_hour=20,
        ))
        self.ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        self.rm.start_day(500.0, self.ts)

    def test_can_trade_initially(self):
        self.assertTrue(self.rm.can_trade(500.0, self.ts))

    def test_blocks_after_max_loss(self):
        self.rm.register_pnl(-5.0)
        self.rm.register_pnl(-5.0)
        self.rm.register_pnl(-6.0)  # total = -16 > -15 (3% of 500)
        self.assertFalse(self.rm.can_trade(500.0, self.ts))

    def test_blocks_after_consecutive_losses(self):
        self.rm.register_pnl(-1.0)
        self.rm.register_pnl(-1.0)
        self.rm.register_pnl(-1.0)
        self.assertFalse(self.rm.can_trade(500.0, self.ts))

    def test_win_resets_consecutive(self):
        self.rm.register_pnl(-1.0)
        self.rm.register_pnl(-1.0)
        self.rm.register_pnl(5.0)  # win resets
        self.rm.register_pnl(-1.0)
        self.assertTrue(self.rm.can_trade(500.0, self.ts))

    def test_new_day_resets(self):
        self.rm.register_pnl(-5.0)
        self.rm.register_pnl(-5.0)
        self.rm.register_pnl(-6.0)
        next_day = self.ts + timedelta(days=1)
        self.assertTrue(self.rm.can_trade(500.0, next_day))


class TestVolumeManager(unittest.TestCase):
    def setUp(self):
        self.vm = VolumeManager(VolumeConfig(monthly_target=1_000_000, trading_days=30))
        self.ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def test_daily_target(self):
        self.assertAlmostEqual(self.vm.daily_target, 1_000_000 / 30)

    def test_register_trade(self):
        self.vm.register_trade("scalp", 1000.0, self.ts)
        self.assertEqual(self.vm.daily_volume, 1000.0)
        self.assertEqual(self.vm.monthly_volume, 1000.0)
        self.assertEqual(self.vm.strategy_volume["scalp"], 1000.0)

    def test_remaining_volume(self):
        daily_target = self.vm.daily_target
        self.vm.register_trade("scalp", 1000.0, self.ts)
        remaining = self.vm.remaining_daily_volume(self.ts)
        self.assertAlmostEqual(remaining, daily_target - 1000.0)

    def test_day_rollover(self):
        self.vm.register_trade("scalp", 1000.0, self.ts)
        next_day = self.ts + timedelta(days=1)
        self.vm.register_trade("scalp", 500.0, next_day)
        self.assertEqual(self.vm.daily_volume, 500.0)  # rolled
        self.assertEqual(self.vm.monthly_volume, 1500.0)  # accumulated

    def test_compute_size(self):
        size = self.vm.compute_size(risk_pct=0.01, equity=500, atr=100, k=2.0, price=85000)
        expected = (0.01 * 500) / (100 * 2.0)
        self.assertAlmostEqual(size, expected)

    def test_compute_size_zero_atr(self):
        self.assertEqual(self.vm.compute_size(0.01, 500, 0, 2.0, 85000), 0.0)


class TestPositionManager(unittest.TestCase):
    def setUp(self):
        self.pm = PositionManager()
        self.ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def _make_position(self, strategy_id="scalp") -> Position:
        return Position(
            symbol="BTCUSDT", strategy_id=strategy_id, side="BUY",
            size=0.001, entry_price=85000.0, stop_loss=84500.0,
            take_profit=86500.0, opened_at=self.ts,
        )

    def test_open_close(self):
        pos = self._make_position()
        self.pm.open_position(pos)
        self.assertTrue(self.pm.has_open_position("BTCUSDT", "scalp"))
        self.assertEqual(self.pm.open_positions_count(), 1)
        self.pm.close_position("BTCUSDT", "scalp")
        self.assertFalse(self.pm.has_open_position("BTCUSDT", "scalp"))

    def test_close_with_price(self):
        pos = self._make_position()
        self.pm.open_position(pos)
        trade = self.pm.close_position_with_price("BTCUSDT", "scalp", 86000.0, self.ts)
        self.assertIsNotNone(trade)
        self.assertGreater(trade.pnl, 0)  # BUY at 85000, exit at 86000

    def test_breakeven_flag(self):
        pos = self._make_position()
        self.pm.open_position(pos)
        self.assertFalse(pos.breakeven_moved)
        self.pm.update_stop_loss("BTCUSDT", "scalp", 85050.0)
        updated = self.pm.get_position("BTCUSDT", "scalp")
        self.assertTrue(updated.breakeven_moved)
        self.assertEqual(updated.stop_loss, 85050.0)

    def test_no_double_position(self):
        """Opening a second position with same key replaces the first."""
        pos1 = self._make_position()
        pos2 = self._make_position()
        pos2.entry_price = 86000.0
        self.pm.open_position(pos1)
        self.pm.open_position(pos2)
        self.assertEqual(self.pm.open_positions_count(), 1)
        self.assertEqual(self.pm.get_position("BTCUSDT", "scalp").entry_price, 86000.0)

    def test_trade_stats(self):
        pos1 = self._make_position("scalp")
        self.pm.open_position(pos1)
        self.pm.close_position_with_price("BTCUSDT", "scalp", 86000.0, self.ts)
        pos2 = self._make_position("trend")
        self.pm.open_position(pos2)
        self.pm.close_position_with_price("BTCUSDT", "trend", 84000.0, self.ts)
        stats = self.pm.trade_stats()
        self.assertEqual(stats["trades"], 2)
        self.assertEqual(stats["wins"], 1)


class TestQtyUtils(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_qty(0.00567, "0.001", "0.001"), "0.005")

    def test_below_min(self):
        self.assertEqual(normalize_qty(0.0001, "0.001", "0.001"), "0")

    def test_reduce_by_step(self):
        self.assertEqual(reduce_by_step("0.005", "0.001"), "0.004")

    def test_reduce_to_zero(self):
        self.assertEqual(reduce_by_step("0.001", "0.001"), "0")


class TestSizing(unittest.TestCase):
    def test_compute_size(self):
        size = compute_size(0.01, 500, 100, 2.0, 85000)
        self.assertAlmostEqual(size, 0.025)

    def test_min_notional_filter(self):
        # Very small position that would be below notional minimum
        size = compute_size(0.01, 10, 5000, 2.0, 85000, min_notional=100)
        self.assertEqual(size, 0.0)  # 0.001 * 85000 = 85 < 100


class TestTradeMechanics(unittest.TestCase):
    def test_check_sl_buy(self):
        trade = Trade("scalp", "BUY", 85000, 84500, 86000, 0.001,
                       datetime(2026, 3, 1, tzinfo=timezone.utc))
        candle = {"high": 85500, "low": 84400, "close": 84600, "open": 85000}
        self.assertEqual(check_sl_tp(trade, candle), 84500)  # SL hit

    def test_check_tp_buy(self):
        trade = Trade("scalp", "BUY", 85000, 84500, 86000, 0.001,
                       datetime(2026, 3, 1, tzinfo=timezone.utc))
        candle = {"high": 86100, "low": 85200, "close": 86050, "open": 85500}
        self.assertEqual(check_sl_tp(trade, candle), 86000)  # TP hit

    def test_close_trade_pnl(self):
        trade = Trade("scalp", "BUY", 85000, 84500, 86500, 0.001,
                       datetime(2026, 3, 1, tzinfo=timezone.utc))
        close_trade(trade, 86000, datetime(2026, 3, 1, 1, 0, tzinfo=timezone.utc), 0.00055)
        self.assertTrue(trade.closed)
        expected_pnl = (86000 - 85000) * 0.001 - (0.001 * 85000 * 0.00055 * 2)
        self.assertAlmostEqual(trade.pnl, expected_pnl, places=6)

    def test_close_trade_sell(self):
        trade = Trade("scalp", "SELL", 86000, 86500, 85000, 0.001,
                       datetime(2026, 3, 1, tzinfo=timezone.utc))
        close_trade(trade, 85500, datetime(2026, 3, 1, 1, 0, tzinfo=timezone.utc))
        self.assertGreater(trade.pnl, 0)  # SELL at 86000, exit at 85500


if __name__ == "__main__":
    unittest.main()
