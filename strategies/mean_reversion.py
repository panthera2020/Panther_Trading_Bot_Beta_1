from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from models.signal import Side, TradeSignal
from strategies.indicators import atr, bollinger_bands, ema, rsi, vwap


@dataclass
class MeanReversionConfig:
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14
    rsi_period: int = 14
    atr_k: float = 2.0  # CHANGED: widened from 1.5x to 2.0x ATR stops
    max_holding_bars: int = 12
    use_rsi: bool = True
    rsi_long_threshold: float = 30.0  # CHANGED: loosened from 25 to 30
    rsi_short_threshold: float = 70.0  # CHANGED: loosened from 75 to 70
    # NEW: 1H EMA trend filter - only take longs when EMA50 > EMA200
    use_trend_filter: bool = True
    trend_ema_fast: int = 50
    trend_ema_slow: int = 200


class MeanReversionStrategy:
    strategy_id = "scalp"

    def __init__(self, config: MeanReversionConfig):
        self.config = config

    def generate_signal(
        self,
        candles: List[Dict[str, float]],
        size: float,
        symbol: str,
        timestamp: datetime,
        candles_1h: Optional[List[Dict[str, float]]] = None,
    ) -> Optional[TradeSignal]:
        if len(candles) < max(self.config.bb_period, self.config.atr_period, self.config.rsi_period) + 2:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        volumes = [c["volume"] for c in candles]

        bands = bollinger_bands(closes, self.config.bb_period, self.config.bb_std)
        atr_val = atr(highs, lows, closes, self.config.atr_period)
        vwap_val = vwap(closes[-self.config.bb_period :], volumes[-self.config.bb_period :])
        rsi_val = rsi(closes, self.config.rsi_period) if self.config.use_rsi else None

        if bands is None or atr_val is None or vwap_val is None:
            return None

        lower, mid, upper = bands
        last_close = closes[-1]

        rsi_long_ok = (rsi_val is None) or (rsi_val < self.config.rsi_long_threshold)
        rsi_short_ok = (rsi_val is None) or (rsi_val > self.config.rsi_short_threshold)

        # NEW: 1H EMA trend filter
        trend_long_ok = True
        trend_short_ok = True
        if self.config.use_trend_filter and candles_1h is not None:
            trend_long_ok, trend_short_ok = self._evaluate_trend(candles_1h)

        if last_close < lower and last_close < vwap_val and rsi_long_ok and trend_long_ok:
            stop = last_close - self.config.atr_k * atr_val
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
                reason="mean_reversion_long",
                metadata={"max_holding_bars": self.config.max_holding_bars},
            )

        if last_close > upper and last_close > vwap_val and rsi_short_ok and trend_short_ok:
            stop = last_close + self.config.atr_k * atr_val
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
                reason="mean_reversion_short",
                metadata={"max_holding_bars": self.config.max_holding_bars},
            )

        return None

    def _evaluate_trend(self, candles_1h: List[Dict[str, float]]) -> tuple[bool, bool]:
        """
        Evaluate 1H trend using EMA50 vs EMA200.
        Returns (long_ok, short_ok):
          - long_ok = True when EMA50 > EMA200 (uptrend)
          - short_ok = True when EMA50 < EMA200 (downtrend)
        If insufficient data, both return True (no filter applied).
        """
        if len(candles_1h) < self.config.trend_ema_slow + 2:
            return True, True

        closes_1h = [c["close"] for c in candles_1h]
        ema_fast = ema(closes_1h, self.config.trend_ema_fast)
        ema_slow = ema(closes_1h, self.config.trend_ema_slow)

        if ema_fast is None or ema_slow is None:
            return True, True

        long_ok = ema_fast > ema_slow   # Only take longs in uptrend
        short_ok = ema_fast < ema_slow  # Only take shorts in downtrend
        return long_ok, short_ok
