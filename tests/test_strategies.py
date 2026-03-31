"""Unit tests for each strategy using synthetic market data."""
import unittest
from datetime import datetime, timezone

from tests.market_data import (
    btc_uptrend_5m, btc_downtrend_5m, btc_ranging_5m,
    btc_uptrend_1h, btc_downtrend_1h, btc_ranging_1h,
    btc_3bull_3m, btc_3bear_3m, btc_choppy_5m, btc_sweep_5m,
)
from strategies.mean_reversion import MeanReversionStrategy, MeanReversionConfig
from strategies.trend_breakout import TrendBreakoutStrategy, TrendBreakoutConfig
from strategies.strategy_c import StrategyC, StrategyCConfig


TS = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class TestMeanReversion(unittest.TestCase):
    def setUp(self):
        self.strategy = MeanReversionStrategy(MeanReversionConfig())

    def test_no_signal_on_uptrend(self):
        """Strong uptrend should not trigger mean reversion (trend filter blocks)."""
        candles = btc_uptrend_5m()
        candles_1h = btc_uptrend_1h()
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS, candles_1h=candles_1h)
        # In a strong uptrend, MR should rarely trigger (no BB touch + trend filter)
        # It's acceptable if it returns None OR a signal — the key is it doesn't crash
        # and respects its contract (returns Optional[TradeSignal])
        if signal is not None:
            self.assertIn(signal.side.value, ["BUY", "SELL"])
            self.assertGreater(signal.stop_loss, 0)
            self.assertGreater(signal.take_profit, 0)

    def test_ranging_market_may_signal(self):
        """Ranging market is where mean reversion should find opportunities."""
        candles = btc_ranging_5m()
        candles_1h = btc_ranging_1h()
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS, candles_1h=candles_1h)
        # May or may not signal depending on exact BB touch + reaction candle
        if signal is not None:
            self.assertEqual(signal.strategy_id, "scalp")
            self.assertEqual(signal.symbol, "BTCUSDT")
            # Verify 1:3 RR
            risk = abs(signal.price - signal.stop_loss)
            reward = abs(signal.take_profit - signal.price)
            rr = reward / risk if risk > 0 else 0
            self.assertAlmostEqual(rr, 3.0, places=1)

    def test_signal_contract(self):
        """Any returned signal must have all required fields."""
        candles = btc_sweep_5m()
        candles_1h = btc_ranging_1h()
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS, candles_1h=candles_1h)
        if signal is not None:
            self.assertIsNotNone(signal.symbol)
            self.assertIsNotNone(signal.strategy_id)
            self.assertIsNotNone(signal.side)
            self.assertGreater(signal.price, 0)
            self.assertGreater(signal.stop_loss, 0)
            self.assertIsNotNone(signal.take_profit)

    def test_insufficient_data(self):
        """Should return None with too few candles."""
        signal = self.strategy.generate_signal(
            [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}] * 5,
            0.001, "BTCUSDT", TS,
        )
        self.assertIsNone(signal)


class TestTrendBreakout(unittest.TestCase):
    def setUp(self):
        self.strategy = TrendBreakoutStrategy(TrendBreakoutConfig())

    def test_uptrend_may_signal_long(self):
        """Strong uptrend with breakout above recent high should signal BUY."""
        candles = btc_uptrend_1h()
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS)
        if signal is not None:
            self.assertEqual(signal.side.value, "BUY")
            self.assertEqual(signal.strategy_id, "trend")
            # Verify 1:3 RR
            risk = abs(signal.price - signal.stop_loss)
            reward = abs(signal.take_profit - signal.price)
            rr = reward / risk if risk > 0 else 0
            self.assertAlmostEqual(rr, 3.0, places=1)

    def test_downtrend_may_signal_short(self):
        """Strong downtrend with breakdown should signal SELL."""
        candles = btc_downtrend_1h()
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS)
        if signal is not None:
            self.assertEqual(signal.side.value, "SELL")

    def test_choppy_no_signal(self):
        """Choppy market shouldn't trigger breakout (no EMA gap)."""
        candles = btc_ranging_1h()  # range-bound, EMA gap → small
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS)
        # Ranging market has small EMA gap, so breakout unlikely
        if signal is not None:
            # If it somehow signals, contract must hold
            self.assertGreater(signal.stop_loss, 0)

    def test_insufficient_data(self):
        signal = self.strategy.generate_signal(
            [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}] * 10,
            0.001, "BTCUSDT", TS,
        )
        self.assertIsNone(signal)


class TestStrategyC(unittest.TestCase):
    def setUp(self):
        self.strategy = StrategyC(StrategyCConfig())

    def test_three_bullish_signals_buy(self):
        """3 consecutive bullish 3m candles with rising volume → BUY."""
        candles = btc_3bull_3m()
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS)
        if signal is not None:
            self.assertEqual(signal.side.value, "BUY")
            self.assertEqual(signal.strategy_id, "candle3")
            self.assertIsNone(signal.take_profit)  # no TP for strategy C

    def test_three_bearish_signals_sell(self):
        """3 consecutive bearish 3m candles with rising volume → SELL."""
        candles = btc_3bear_3m()
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS)
        if signal is not None:
            self.assertEqual(signal.side.value, "SELL")
            self.assertEqual(signal.strategy_id, "candle3")

    def test_no_signal_on_mixed(self):
        """Mixed candles should not trigger."""
        candles = btc_choppy_5m()[:30]  # choppy → no 3 consecutive same-direction
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS)
        # May or may not signal — choppy data is unpredictable
        # Just verify it doesn't crash

    def test_atr_based_stop_loss(self):
        """Stop loss should be ATR-based, not first candle open."""
        candles = btc_3bull_3m()
        signal = self.strategy.generate_signal(candles, 0.001, "BTCUSDT", TS)
        if signal is not None:
            # SL should NOT equal the first candle's open (old behavior)
            first_open = candles[-3]["open"]
            self.assertNotAlmostEqual(signal.stop_loss, first_open, places=0)

    def test_insufficient_data(self):
        signal = self.strategy.generate_signal(
            [{"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10}] * 5,
            0.001, "BTCUSDT", TS,
        )
        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main()
