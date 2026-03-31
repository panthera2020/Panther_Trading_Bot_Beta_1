"""Contract verification: ensure all classes satisfy their Protocol contracts.

This test catches breaking changes early — if someone modifies a strategy
or manager class in a way that breaks the interface, this test fails.
"""
import unittest

from models.contracts import Strategy, RiskGate, PositionTracker, VolumeTracker
from strategies.mean_reversion import MeanReversionStrategy, MeanReversionConfig
from strategies.trend_breakout import TrendBreakoutStrategy, TrendBreakoutConfig
from strategies.strategy_c import StrategyC, StrategyCConfig
from execution.risk_manager import RiskManager, RiskConfig
from execution.position_manager import PositionManager
from execution.volume_manager import VolumeManager, VolumeConfig


class TestStrategyContracts(unittest.TestCase):
    """Verify all strategies satisfy the Strategy protocol."""

    def test_mean_reversion_is_strategy(self):
        s = MeanReversionStrategy(MeanReversionConfig())
        self.assertIsInstance(s, Strategy)
        self.assertEqual(s.strategy_id, "scalp")

    def test_trend_breakout_is_strategy(self):
        s = TrendBreakoutStrategy(TrendBreakoutConfig())
        self.assertIsInstance(s, Strategy)
        self.assertEqual(s.strategy_id, "trend")

    def test_strategy_c_is_strategy(self):
        s = StrategyC(StrategyCConfig())
        self.assertIsInstance(s, Strategy)
        self.assertEqual(s.strategy_id, "candle3")


class TestRiskGateContract(unittest.TestCase):
    def test_risk_manager_is_risk_gate(self):
        rm = RiskManager(RiskConfig())
        self.assertIsInstance(rm, RiskGate)


class TestPositionTrackerContract(unittest.TestCase):
    def test_position_manager_is_tracker(self):
        pm = PositionManager()
        self.assertIsInstance(pm, PositionTracker)


class TestVolumeTrackerContract(unittest.TestCase):
    def test_volume_manager_is_tracker(self):
        vm = VolumeManager(VolumeConfig(monthly_target=1_000_000))
        self.assertIsInstance(vm, VolumeTracker)


if __name__ == "__main__":
    unittest.main()
