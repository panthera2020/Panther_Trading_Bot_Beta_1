"""Unit tests for strategies/indicators.py"""
import unittest
from strategies.indicators import (
    sma, ema, atr, atr_from_candles, vwap, bollinger_bands, rsi,
    fractal_high, fractal_low, latest_fractal_levels, liquidity_sweep,
)


class TestSMA(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(sma([1, 2, 3, 4, 5], 3), 4.0)

    def test_insufficient_data(self):
        self.assertIsNone(sma([1, 2], 3))

    def test_period_one(self):
        self.assertAlmostEqual(sma([10, 20, 30], 1), 30.0)


class TestEMA(unittest.TestCase):
    def test_basic(self):
        val = ema([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5)
        self.assertIsNotNone(val)
        self.assertGreater(val, 7)  # EMA weights recent values more

    def test_insufficient(self):
        self.assertIsNone(ema([1, 2], 5))


class TestATR(unittest.TestCase):
    def test_basic(self):
        highs = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
        lows = [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
        closes = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
        val = atr(highs, lows, closes, 5)
        self.assertIsNotNone(val)
        self.assertGreater(val, 0)

    def test_insufficient(self):
        self.assertIsNone(atr([1], [1], [1], 5))

    def test_from_candles(self):
        candles = [
            {"high": 100 + i, "low": 98 + i, "close": 99 + i}
            for i in range(20)
        ]
        val = atr_from_candles(candles, 14)
        self.assertIsNotNone(val)
        self.assertGreater(val, 0)


class TestVWAP(unittest.TestCase):
    def test_basic(self):
        prices = [100, 101, 102]
        volumes = [10, 20, 30]
        val = vwap(prices, volumes)
        expected = (100*10 + 101*20 + 102*30) / 60
        self.assertAlmostEqual(val, expected)

    def test_zero_volume(self):
        self.assertIsNone(vwap([100], [0]))


class TestBollingerBands(unittest.TestCase):
    def test_basic(self):
        values = list(range(1, 21))
        result = bollinger_bands(values, 20, 2.0)
        self.assertIsNotNone(result)
        lower, mid, upper = result
        self.assertAlmostEqual(mid, 10.5)
        self.assertLess(lower, mid)
        self.assertGreater(upper, mid)

    def test_insufficient(self):
        self.assertIsNone(bollinger_bands([1, 2], 5, 2.0))


class TestRSI(unittest.TestCase):
    def test_overbought(self):
        # Steadily rising prices → RSI should be high
        values = [100 + i for i in range(20)]
        val = rsi(values, 14)
        self.assertIsNotNone(val)
        self.assertGreater(val, 90)

    def test_oversold(self):
        values = [100 - i for i in range(20)]
        val = rsi(values, 14)
        self.assertIsNotNone(val)
        self.assertLess(val, 10)

    def test_neutral(self):
        values = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101,
                  100, 101, 100, 101, 100, 101]
        val = rsi(values, 14)
        self.assertIsNotNone(val)
        self.assertGreater(val, 30)
        self.assertLess(val, 70)


class TestFractals(unittest.TestCase):
    def test_fractal_high(self):
        highs = [1, 2, 5, 2, 1]
        self.assertTrue(fractal_high(highs, 2))

    def test_fractal_low(self):
        lows = [5, 4, 1, 4, 5]
        self.assertTrue(fractal_low(lows, 2))

    def test_no_fractal(self):
        highs = [1, 2, 3, 4, 5]
        self.assertFalse(fractal_high(highs, 2))

    def test_latest_fractal_levels(self):
        candles = [
            {"high": 10, "low": 5},
            {"high": 11, "low": 6},
            {"high": 15, "low": 4},  # fractal high & low
            {"high": 12, "low": 6},
            {"high": 10, "low": 5},
            {"high": 9, "low": 4},
            {"high": 8, "low": 3},
        ]
        fh, fl = latest_fractal_levels(candles, lookback=50)
        self.assertEqual(fh, 15)
        self.assertEqual(fl, 4)


class TestLiquiditySweep(unittest.TestCase):
    def test_long_sweep(self):
        # Build candles with a low around 100, then a sweep below + close above
        candles = [{"high": 110, "low": 100 + i % 3, "close": 105} for i in range(25)]
        candles[-1] = {"high": 108, "low": 98, "close": 103}  # sweep below prior low, close above
        result = liquidity_sweep(candles, "LONG", 20)
        self.assertIsNotNone(result)

    def test_no_sweep(self):
        candles = [{"high": 110, "low": 100, "close": 105} for _ in range(25)]
        result = liquidity_sweep(candles, "LONG", 20)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
