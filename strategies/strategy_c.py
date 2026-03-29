from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from models.signal import Side, TradeSignal
from strategies.indicators import atr


@dataclass
class StrategyCConfig:
    atr_period: int = 14
    atr_stop_k: float = 1.5       # NEW: ATR-based stop multiplier (replaces first_candle_open stop)
    min_atr: float | None = None
    max_atr: float | None = None
    require_increasing_volume: bool = True  # NEW: configurable volume filter


class StrategyC:
    """
    Strategy C: 3 consecutive candles in same direction on 3m.
    Long on 3 bullish closes, short on 3 bearish closes.

    FIXED:
    - volumes NameError: now properly extracts volumes from candles
    - continue outside loop: replaced with return None (correct flow)
    - Stop placement: uses ATR-based stops instead of first_candle_open
    """

    strategy_id = "candle3"

    def __init__(self, config: StrategyCConfig):
        self.config = config

    def generate_signal(
        self,
        candles: List[Dict[str, float]],
        size: float,
        symbol: str,
        timestamp: datetime,
    ) -> Optional[TradeSignal]:
        if len(candles) < max(3, self.config.atr_period) + 1:
            return None

        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        closes = [c["close"] for c in candles]

        atr_val = atr(
            highs=highs,
            lows=lows,
            closes=closes,
            period=self.config.atr_period,
        )
        if atr_val is None:
            return None
        if self.config.min_atr is not None and atr_val < self.config.min_atr:
            return None
        if self.config.max_atr is not None and atr_val > self.config.max_atr:
            return None

        last_three = candles[-3:]
        last_close = candles[-1]["close"]

        # FIXED: Extract volumes from candles (was referencing undefined 'volumes')
        if self.config.require_increasing_volume:
            vol_3 = last_three[2].get("volume", 0)
            vol_2 = last_three[1].get("volume", 0)
            vol_1 = last_three[0].get("volume", 0)
            if not (vol_3 > vol_2 > vol_1):
                # FIXED: was 'continue' (outside loop) - now returns None
                return None

        bull = all(c["close"] > c["open"] for c in last_three)
        bear = all(c["close"] < c["open"] for c in last_three)

        if bull:
            # FIXED: ATR-based stop instead of first_candle_open
            stop = last_close - self.config.atr_stop_k * atr_val
            risk = abs(last_close - stop)
            take_profit = last_close + 2 * risk
            return TradeSignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                side=Side.BUY,
                timestamp=timestamp,
                price=last_close,
                stop_loss=stop,
                take_profit=take_profit,
                size=size,
                reason="three_bullish_3m",
            )

        if bear:
            # FIXED: ATR-based stop instead of first_candle_open
            stop = last_close + self.config.atr_stop_k * atr_val
            risk = abs(stop - last_close)
            take_profit = last_close - 2 * risk
            return TradeSignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                side=Side.SELL,
                timestamp=timestamp,
                price=last_close,
                stop_loss=stop,
                take_profit=take_profit,
                size=size,
                reason="three_bearish_3m",
            )

        return None
