from __future__ import annotations

from typing import List, Optional


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    k = 2 / (period + 1)
    ema_val = values[-period]
    for value in values[-period + 1:]:
        ema_val = value * k + ema_val * (1 - k)
    return ema_val


def atr(highs: List[float], lows: List[float], closes: List[float], period: int) -> Optional[float]:
    if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / period


def atr_from_candles(candles: List[dict], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    return atr(
        [c["high"] for c in candles],
        [c["low"] for c in candles],
        [c["close"] for c in candles],
        period,
    )


def vwap(prices: List[float], volumes: List[float]) -> Optional[float]:
    if not prices or not volumes or len(prices) != len(volumes):
        return None
    total_volume = sum(volumes)
    if total_volume == 0:
        return None
    return sum(p * v for p, v in zip(prices, volumes)) / total_volume


def bollinger_bands(values: List[float], period: int, std_mult: float) -> Optional[tuple[float, float, float]]:
    if len(values) < period or period <= 0:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    std = variance ** 0.5
    upper = mean + std_mult * std
    lower = mean - std_mult * std
    return lower, mean, upper


def rsi(values: List[float], period: int) -> Optional[float]:
    if len(values) < period + 1 or period <= 0:
        return None
    gains = []
    losses = []
    for i in range(-period, 0):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fractal_high(highs: List[float], index: int = -3) -> bool:
    """Williams fractal high: candle has 2 lower highs on each side."""
    n = len(highs)
    i = index if index >= 0 else n + index
    if i < 2 or i > n - 3:
        return False
    return (
        highs[i] > highs[i - 1]
        and highs[i] > highs[i - 2]
        and highs[i] > highs[i + 1]
        and highs[i] > highs[i + 2]
    )


def fractal_low(lows: List[float], index: int = -3) -> bool:
    """Williams fractal low: candle has 2 higher lows on each side."""
    n = len(lows)
    i = index if index >= 0 else n + index
    if i < 2 or i > n - 3:
        return False
    return (
        lows[i] < lows[i - 1]
        and lows[i] < lows[i - 2]
        and lows[i] < lows[i + 1]
        and lows[i] < lows[i + 2]
    )


def latest_fractal_levels(candles: List[dict], lookback: int = 50) -> tuple[Optional[float], Optional[float]]:
    """Return (fractal_high_level, fractal_low_level) from the most recent fractals."""
    if len(candles) < 5:
        return None, None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    fh = None
    fl = None
    start = max(2, len(candles) - lookback)
    for i in range(len(candles) - 3, start - 1, -1):
        if fh is None and fractal_high(highs, i):
            fh = highs[i]
        if fl is None and fractal_low(lows, i):
            fl = lows[i]
        if fh is not None and fl is not None:
            break
    return fh, fl


def liquidity_sweep(candles: List[dict], bias: str, lookback: int = 20) -> Optional[float]:
    """Detect a liquidity sweep: price pierces a prior swing level then reverses.
    Returns the swept level if detected, else None."""
    if len(candles) < lookback + 2:
        return None
    window = candles[-(lookback + 1):-1]
    last = candles[-1]
    if bias == "LONG":
        prior_low = min(c["low"] for c in window)
        if last["low"] < prior_low and last["close"] > prior_low:
            return prior_low
    elif bias == "SHORT":
        prior_high = max(c["high"] for c in window)
        if last["high"] > prior_high and last["close"] < prior_high:
            return prior_high
    return None
