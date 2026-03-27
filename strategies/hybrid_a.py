from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from strategies.indicators import ema, atr


@dataclass
class SweepEvent:
    direction: str  # "LONG" or "SHORT"
    level: float
    timestamp: datetime


class TrendBiasEvaluator:
    def __init__(self, ema_fast: int = 50, ema_slow: int = 200, min_gap: float = 0.005):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.min_gap = min_gap

    def evaluate(self, candles: List[Dict[str, float]]) -> str:
        if len(candles) < self.ema_slow + 2:
            return "NONE"
        closes = [c["close"] for c in candles]
        fast = ema(closes, self.ema_fast)
        slow = ema(closes, self.ema_slow)
        if fast is None or slow is None:
            return "NONE"
        gap = abs(fast - slow) / slow
        if gap < self.min_gap:
            return "NONE"
        return "LONG" if fast > slow else "SHORT"


class LiquiditySweepDetector:
    def __init__(self, lookback: int = 20):
        self.lookback = lookback

    def detect(self, candles: List[Dict[str, float]], bias: str) -> Optional[SweepEvent]:
        if bias == "NONE" or len(candles) < self.lookback + 2:
            return None
        window = candles[-(self.lookback + 1) : -1]
        last = candles[-1]
        if bias == "LONG":
            prior_low = min(c["low"] for c in window)
            if last["low"] < prior_low and last["close"] > prior_low:
                return SweepEvent(direction="LONG", level=prior_low, timestamp=_ts(last))
        if bias == "SHORT":
            prior_high = max(c["high"] for c in window)
            if last["high"] > prior_high and last["close"] < prior_high:
                return SweepEvent(direction="SHORT", level=prior_high, timestamp=_ts(last))
        return None


class EntryExecutor:
    def __init__(self, max_entry_wait_minutes: int = 5):
        self.max_entry_wait_minutes = max_entry_wait_minutes

    def should_enter(
        self,
        sweep: SweepEvent,
        candles_1m: List[Dict[str, float]],
        bias: str,
    ) -> bool:
        if sweep is None or bias == "NONE":
            return False
        if sweep.direction != bias:
            return False
        if not candles_1m:
            return False
        last = candles_1m[-1]
        last_ts = _ts(last)
        if last_ts > sweep.timestamp + timedelta(minutes=self.max_entry_wait_minutes):
            return False
        bullish = last["close"] > last["open"]
        bearish = last["close"] < last["open"]
        return (bias == "LONG" and bullish) or (bias == "SHORT" and bearish)


def atr_1m(candles: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    return atr(highs, lows, closes, period)


def _ts(candle: Dict[str, float]) -> datetime:
    return datetime.fromtimestamp(candle["timestamp"] / 1000.0)
