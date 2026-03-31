"""Synthetic OHLCV generators that mimic real market microstructure.

Produces deterministic candle sequences for:
- Trending markets (for trend breakout)
- Ranging/mean-reverting markets (for scalp)
- Momentum runs (for Strategy C)
- Choppy/noisy markets (strategy should stay out)

All prices are BTC-scale (~80000-90000) to test realistic sizing.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List


def _candle(ts_ms: int, o: float, h: float, l: float, c: float, v: float) -> Dict:
    return {"timestamp": ts_ms, "open": o, "high": h, "low": l, "close": c, "volume": v}


def make_candles(
    start: datetime,
    interval_minutes: int,
    prices: List[float],
    base_volume: float = 100.0,
    seed: int = 42,
) -> List[Dict]:
    """Turn a list of close prices into OHLCV candles with realistic wicks."""
    rng = random.Random(seed)
    candles = []
    for i, close in enumerate(prices):
        ts_ms = int((start + timedelta(minutes=i * interval_minutes)).timestamp() * 1000)
        if i == 0:
            open_ = close * (1 + rng.uniform(-0.001, 0.001))
        else:
            open_ = prices[i - 1]
        high = max(open_, close) * (1 + rng.uniform(0, 0.003))
        low = min(open_, close) * (1 - rng.uniform(0, 0.003))
        vol = base_volume * (1 + rng.uniform(-0.3, 0.5))
        candles.append(_candle(ts_ms, round(open_, 2), round(high, 2), round(low, 2), round(close, 2), round(vol, 2)))
    return candles


# ── Scenario Generators ───────────────────────────────────────────

def uptrend(n: int = 300, start_price: float = 82000.0) -> List[float]:
    """Steady uptrend with pullbacks — good for trend breakout."""
    prices = []
    p = start_price
    rng = random.Random(1)
    for i in range(n):
        drift = 15.0 + rng.gauss(0, 8)
        if i % 30 < 5:
            drift = -20.0  # periodic pullback
        p += drift
        prices.append(round(p, 2))
    return prices


def downtrend(n: int = 300, start_price: float = 88000.0) -> List[float]:
    """Steady downtrend with bounces."""
    prices = []
    p = start_price
    rng = random.Random(2)
    for i in range(n):
        drift = -15.0 + rng.gauss(0, 8)
        if i % 30 < 5:
            drift = 20.0
        p += drift
        prices.append(round(p, 2))
    return prices


def ranging(n: int = 300, center: float = 85000.0, amplitude: float = 600.0) -> List[float]:
    """Oscillating around a mean — ideal for mean reversion."""
    prices = []
    rng = random.Random(3)
    for i in range(n):
        p = center + amplitude * math.sin(2 * math.pi * i / 60) + rng.gauss(0, 30)
        prices.append(round(p, 2))
    return prices


def three_bullish_candles(n: int = 30, start_price: float = 84000.0) -> List[float]:
    """Generate a sequence where the last 3 candles are distinctly bullish
    with increasing volume — triggers Strategy C."""
    prices = []
    p = start_price
    rng = random.Random(4)
    for i in range(n - 3):
        p += rng.gauss(0, 15)
        prices.append(round(p, 2))
    # Force 3 consecutive bullish candles
    for _ in range(3):
        p += rng.uniform(20, 60)
        prices.append(round(p, 2))
    return prices


def three_bearish_candles(n: int = 30, start_price: float = 86000.0) -> List[float]:
    """Last 3 candles are bearish with increasing volume."""
    prices = []
    p = start_price
    rng = random.Random(5)
    for i in range(n - 3):
        p += rng.gauss(0, 15)
        prices.append(round(p, 2))
    for _ in range(3):
        p -= rng.uniform(20, 60)
        prices.append(round(p, 2))
    return prices


def choppy(n: int = 300, center: float = 85000.0) -> List[float]:
    """Random walk with no trend or pattern — strategies should mostly stay out."""
    prices = []
    p = center
    rng = random.Random(6)
    for _ in range(n):
        p += rng.gauss(0, 50)
        prices.append(round(p, 2))
    return prices


def liquidity_sweep_down_then_up(n: int = 60, low_point: float = 83500.0) -> List[float]:
    """Price drops below a swing low then reverses hard — mimics a liquidity grab."""
    prices = []
    p = 84500.0
    rng = random.Random(7)
    # Build swing low
    for i in range(n // 3):
        p = 84500 - 300 * math.sin(math.pi * i / (n // 3)) + rng.gauss(0, 10)
        prices.append(round(p, 2))
    # Sweep below
    prices.append(round(low_point - 100, 2))
    prices.append(round(low_point - 150, 2))
    # Sharp reversal (bullish reaction)
    for i in range(n - len(prices)):
        p = low_point + 50 * (i + 1) + rng.gauss(0, 10)
        prices.append(round(p, 2))
    return prices


# ── Convenience: pre-built datasets ──────────────────────────────

T0 = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)


def btc_uptrend_5m() -> List[Dict]:
    return make_candles(T0, 5, uptrend(300))


def btc_downtrend_5m() -> List[Dict]:
    return make_candles(T0, 5, downtrend(300))


def btc_ranging_5m() -> List[Dict]:
    return make_candles(T0, 5, ranging(300))


def btc_uptrend_1h() -> List[Dict]:
    return make_candles(T0, 60, uptrend(300))


def btc_downtrend_1h() -> List[Dict]:
    return make_candles(T0, 60, downtrend(300))


def btc_ranging_1h() -> List[Dict]:
    return make_candles(T0, 60, ranging(300))


def btc_3bull_3m() -> List[Dict]:
    """3m candles with last 3 bullish + increasing volume."""
    candles = make_candles(T0, 3, three_bullish_candles(30))
    # Force increasing volume on last 3
    candles[-3]["volume"] = 80.0
    candles[-2]["volume"] = 120.0
    candles[-1]["volume"] = 180.0
    return candles


def btc_3bear_3m() -> List[Dict]:
    """3m candles with last 3 bearish + increasing volume."""
    candles = make_candles(T0, 3, three_bearish_candles(30))
    candles[-3]["volume"] = 80.0
    candles[-2]["volume"] = 120.0
    candles[-1]["volume"] = 180.0
    return candles


def btc_choppy_5m() -> List[Dict]:
    return make_candles(T0, 5, choppy(300))


def btc_sweep_5m() -> List[Dict]:
    return make_candles(T0, 5, liquidity_sweep_down_then_up(60))
