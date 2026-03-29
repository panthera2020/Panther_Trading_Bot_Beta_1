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
    atr_k: float = 2.0           # CHANGED: 1.5 -> 2.0 (wider stops, reduce noise stop-outs on 15m)
    max_holding_bars: int = 12
    use_rsi: bool = True
    use_trend_filter: bool = True  # NEW: require 1H EMA50 > EMA200 for longs


class MeanReversionStrategy:
    strategy_id = "scalp"

    def __init__(self, config: MeanReversionConfig):
        self.config = config
        # Cache for 1H trend state (set externally before generate_signal)
        self._trend_bullish: Optional[bool] = None

    def update_trend_filter(self, candles_1h: List[Dict[str, float]]) -> None:
        """
        Compute 1H EMA50 vs EMA200 and cache the result.
        Call this once per poll cycle with 1H candles before calling generate_signal.

        Sets self._trend_bullish:
            True  -> EMA50 > EMA200 (uptrend, longs OK)
            False -> EMA50 <= EMA200 (downtrend, longs blocked)
            None  -> Not enough data (longs allowed as fallback)
        """
        if not candles_1h or len(candles_1h) < 201:
            self._trend_bullish = None
            return
        closes_1h = [c["close"] for c in candles_1h]
        ema50 = ema(closes_1h, 50)
        ema200 = ema(closes_1h, 200)
        if ema50 is None or ema200 is None:
            self._trend_bullish = None
            return
        self._trend_bullish = ema50 > ema200

    def generate_signal(
        self,
        candles: List[Dict[str, float]],
        size: float,
        symbol: str,
        timestamp: datetime,
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

        # CHANGED: RSI thresholds loosened from 25/75 to 30/70 for better win rate
        rsi_long_ok = (rsi_val is None) or (rsi_val < 30)
        rsi_short_ok = (rsi_val is None) or (rsi_val > 70)

        # LONG signal
        if last_close < lower and last_close < vwap_val and rsi_long_ok:
            # NEW: Trend filter - only take longs when 1H EMA50 > EMA200
            if self.config.use_trend_filter and self._trend_bullish is False:
                # Downtrend on 1H - skip long entry
                return None

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

        # SHORT signal (no trend filter needed - shorts are fine in downtrend)
        if last_close > upper and last_close > vwap_val and rsi_short_ok:
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
