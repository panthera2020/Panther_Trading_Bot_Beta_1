from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from models.signal import Side, TradeSignal
from strategies.indicators import atr


@dataclass
class StrategyCConfig:
    atr_period: int = 14
    atr_sl_mult: float = 0.5
    require_increasing_volume: bool = True


class StrategyC:
    """Volume filler: 3 consecutive same-direction 3m candles backed by volume.
    Holds until the next 3m candle closes, then exits.
    Not a profit engine — designed to hit daily volume targets."""

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

        last_three = candles[-3:]
        last_close = candles[-1]["close"]

        bull = all(c["close"] > c["open"] for c in last_three)
        bear = all(c["close"] < c["open"] for c in last_three)

        if not bull and not bear:
            return None

        if self.config.require_increasing_volume:
            volumes = [c["volume"] for c in candles]
            if not (volumes[-1] > volumes[-2] > volumes[-3]):
                return None

        # Tight ATR-based stop loss instead of first candle open
        stop_distance = self.config.atr_sl_mult * atr_val

        if bull:
            stop_loss = last_close - stop_distance
            return TradeSignal(
                symbol=symbol, strategy_id=self.strategy_id, side=Side.BUY,
                timestamp=timestamp, price=last_close, stop_loss=stop_loss,
                take_profit=None, size=size, reason="three_bullish_3m",
            )

        if bear:
            stop_loss = last_close + stop_distance
            return TradeSignal(
                symbol=symbol, strategy_id=self.strategy_id, side=Side.SELL,
                timestamp=timestamp, price=last_close, stop_loss=stop_loss,
                take_profit=None, size=size, reason="three_bearish_3m",
            )

        return None
