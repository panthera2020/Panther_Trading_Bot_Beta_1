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
    # R:R multiplier for take-profit.
    # 1.5 means TP is 1.5x the risk (stop-loss distance).
    rr_multiplier: float = 1.5
    # Volume confirmation: require each successive candle to have higher volume.
    require_increasing_volume: bool = True


class StrategyC:
    """
    Strategy C: 3 consecutive candles in same direction on 3m.
    Long on 3 bullish closes, short on 3 bearish closes.

    Fixes applied:
    - BUG FIX: 'volumes' variable was never defined; now extracted from candle dicts.
    - BUG FIX: used bare 'continue' outside a loop; replaced with early return None.
    - R:R: take_profit now set at entry ± rr_multiplier * risk (default 1.5 = 1:1.5).
    - Volume target: monthly_volume_target in main.py reduced to $2,000,000.
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

        # FIX 1: Extract volumes from the candle dicts (was undefined before).
        # Candle dicts are expected to carry a "volume" key.
        # If volume data is absent, default to 0.0 so the check fails gracefully.
        volumes = [c.get("volume", 0.0) for c in last_three]

        # FIX 2: Volume momentum filter.
        # Replaced bare 'continue' (invalid outside a loop) with an early return.
        if self.config.require_increasing_volume:
            if not (volumes[2] > volumes[1] > volumes[0]):
                # No volume momentum — likely a fake / weak signal. Skip.
                return None

        first_candle_open = last_three[0]["open"]
        bull = all(c["close"] > c["open"] for c in last_three)
        bear = all(c["close"] < c["open"] for c in last_three)
        last_close = candles[-1]["close"]

        # FIX 3: R:R — take_profit now calculated at rr_multiplier * risk.
        # Default rr_multiplier = 1.5 → minimum 1:1.5 R:R.
        if bull:
            risk = abs(last_close - first_candle_open)
            take_profit = last_close + self.config.rr_multiplier * risk if risk > 0 else None
            return TradeSignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                side=Side.BUY,
                timestamp=timestamp,
                price=last_close,
                stop_loss=first_candle_open,
                take_profit=take_profit,
                size=size,
                reason="three_bullish_3m",
            )

        if bear:
            risk = abs(first_candle_open - last_close)
            take_profit = last_close - self.config.rr_multiplier * risk if risk > 0 else None
            return TradeSignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                side=Side.SELL,
                timestamp=timestamp,
                price=last_close,
                stop_loss=first_candle_open,
                take_profit=take_profit,
                size=size,
                reason="three_bearish_3m",
            )

        return None
