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
    rr_ratio: float = 1.5  # Minimum reward-to-risk ratio (take_profit = rr_ratio * risk)


class StrategyC:
    """
    Strategy C: 3 consecutive candles in same direction on 3m.
    Long on 3 bullish closes, short on 3 bearish closes.
    Requires increasing volume across the three candles (momentum confirmation).
    Take-profit is set at rr_ratio * risk from entry (default 1:1.5).
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
        first_candle_open = last_three[0]["open"]
        last_close = candles[-1]["close"]

        bull = all(c["close"] > c["open"] for c in last_three)
        bear = all(c["close"] < c["open"] for c in last_three)

        if not bull and not bear:
            return None

        # --- Volume momentum confirmation ---
        # Extract volumes from the three candles.
        # Candle dicts are expected to have a 'volume' key.
        # If volume data is unavailable (all zeros), skip the check rather than blocking every signal.
        volumes = [c.get("volume", 0.0) for c in last_three]
        has_volume_data = any(v > 0 for v in volumes)
        if has_volume_data:
            if not (volumes[2] > volumes[1] > volumes[0]):
                # No volume momentum — likely a fake/low-conviction signal, skip it.
                return None

        # --- Risk calculation ---
        # Stop-loss anchor: first candle open (the origin of the three-candle move).
        risk = abs(last_close - first_candle_open)
        if risk <= 0:
            return None

        # Take-profit at rr_ratio * risk from entry (default 1:1.5)
        tp_distance = self.config.rr_ratio * risk

        if bull:
            stop_loss = first_candle_open
            take_profit = last_close + tp_distance
            side = Side.BUY
            reason = "three_bullish_3m"
        else:
            stop_loss = first_candle_open
            take_profit = last_close - tp_distance
            side = Side.SELL
            reason = "three_bearish_3m"

        return TradeSignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            side=side,
            timestamp=timestamp,
            price=last_close,
            stop_loss=stop_loss,
            take_profit=take_profit,
            size=size,
            reason=reason,
        )
