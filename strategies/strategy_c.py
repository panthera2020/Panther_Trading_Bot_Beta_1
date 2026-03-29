from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from models.signal import Side, TradeSignal
from strategies.indicators import atr


@dataclass
class StrategyCConfig:
    atr_period: int = 14
    min_atr: float | None = None
    max_atr: float | None = None
    rr_ratio: float = 2.0  # Risk:Reward — TP = entry +/- rr_ratio * risk


class StrategyC:
    """
    Strategy C: 3 consecutive candles in same direction on 3m.
    Long on 3 bullish closes, short on 3 bearish closes.
    Requires increasing volume across the 3 candles for momentum confirmation.

    BUG FIXES:
    - 'continue' inside a function (not a loop) replaced with early return
    - 'volumes' variable was undefined — now derived from candles
    - Added TP at 2R (configurable via rr_ratio)
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

        atr_val = atr(
            highs=[c["high"] for c in candles],
            lows=[c["low"] for c in candles],
            closes=[c["close"] for c in candles],
            period=self.config.atr_period,
        )
        if atr_val is None:
            return None
        if self.config.min_atr is not None and atr_val < self.config.min_atr:
            return None
        if self.config.max_atr is not None and atr_val > self.config.max_atr:
            return None

        last_three = candles[-3:]
        volumes = [c["volume"] for c in candles]  # FIX: was undefined

        # Volume momentum filter — skip if volumes aren't increasing across the 3 candles
        if not (volumes[-1] > volumes[-2] > volumes[-3]):
            return None  # FIX: was 'continue' which is invalid outside a loop

        first_candle_open = last_three[0]["open"]
        bull = all(c["close"] > c["open"] for c in last_three)
        bear = all(c["close"] < c["open"] for c in last_three)
        last_close = candles[-1]["close"]

        if bull:
            stop_loss = first_candle_open
            risk = abs(last_close - stop_loss)
            take_profit = last_close + self.config.rr_ratio * risk if risk > 0 else None
            return TradeSignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                side=Side.BUY,
                timestamp=timestamp,
                price=last_close,
                stop_loss=stop_loss,
                take_profit=take_profit,
                size=size,
                reason="three_bullish_3m",
            )

        if bear:
            stop_loss = first_candle_open
            risk = abs(stop_loss - last_close)
            take_profit = last_close - self.config.rr_ratio * risk if risk > 0 else None
            return TradeSignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                side=Side.SELL,
                timestamp=timestamp,
                price=last_close,
                stop_loss=stop_loss,
                take_profit=take_profit,
                size=size,
                reason="three_bearish_3m",
            )

        return None
