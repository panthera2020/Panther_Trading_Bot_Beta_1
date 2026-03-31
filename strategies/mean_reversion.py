from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from models.signal import Side, TradeSignal
from strategies.indicators import (
    atr, atr_from_candles, bollinger_bands, ema, rsi, vwap,
    liquidity_sweep, latest_fractal_levels,
)


@dataclass
class MeanReversionConfig:
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14
    rsi_period: int = 14
    atr_k: float = 2.0
    rr_ratio: float = 3.0
    use_rsi: bool = True
    rsi_long_threshold: float = 30.0
    rsi_short_threshold: float = 70.0
    use_trend_filter: bool = True
    trend_ema_fast: int = 50
    trend_ema_slow: int = 200
    sweep_lookback: int = 20


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
        min_bars = max(self.config.bb_period, self.config.atr_period, self.config.rsi_period) + 2
        if len(candles) < min_bars:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        volumes = [c["volume"] for c in candles]

        bands = bollinger_bands(closes, self.config.bb_period, self.config.bb_std)
        atr_val = atr(highs, lows, closes, self.config.atr_period)
        vwap_val = vwap(closes[-self.config.bb_period:], volumes[-self.config.bb_period:])
        rsi_val = rsi(closes, self.config.rsi_period) if self.config.use_rsi else None

        if bands is None or atr_val is None or vwap_val is None:
            return None

        lower, mid, upper = bands
        last_close = closes[-1]

        rsi_long_ok = rsi_val is None or rsi_val < self.config.rsi_long_threshold
        rsi_short_ok = rsi_val is None or rsi_val > self.config.rsi_short_threshold

        trend_long_ok, trend_short_ok = True, True
        if self.config.use_trend_filter and candles_1h:
            trend_long_ok, trend_short_ok = self._evaluate_trend(candles_1h)

        # Fractal confirmation: need a recent fractal swing near the zone
        fh, fl = latest_fractal_levels(candles)

        # Liquidity sweep detection
        long_sweep = liquidity_sweep(candles, "LONG", self.config.sweep_lookback)
        short_sweep = liquidity_sweep(candles, "SHORT", self.config.sweep_lookback)

        # --- LONG: price below lower BB + VWAP + RSI oversold + trend OK ---
        if last_close < lower and last_close < vwap_val and rsi_long_ok and trend_long_ok:
            # Require either a fractal low nearby or a liquidity sweep
            has_fractal = fl is not None and fl >= last_close - atr_val
            has_sweep = long_sweep is not None
            if not has_fractal and not has_sweep:
                return None

            # Reaction candle: last candle must be bullish (reversal confirmation)
            if candles[-1]["close"] <= candles[-1]["open"]:
                return None

            stop = last_close - self.config.atr_k * atr_val
            risk = abs(last_close - stop)
            take_profit = last_close + self.config.rr_ratio * risk
            return TradeSignal(
                symbol=symbol, strategy_id=self.strategy_id, side=Side.BUY,
                timestamp=timestamp, price=last_close, stop_loss=stop,
                take_profit=take_profit, size=size, reason="mean_reversion_long",
            )

        # --- SHORT: price above upper BB + VWAP + RSI overbought + trend OK ---
        if last_close > upper and last_close > vwap_val and rsi_short_ok and trend_short_ok:
            has_fractal = fh is not None and fh <= last_close + atr_val
            has_sweep = short_sweep is not None
            if not has_fractal and not has_sweep:
                return None

            # Reaction candle: last candle must be bearish
            if candles[-1]["close"] >= candles[-1]["open"]:
                return None

            stop = last_close + self.config.atr_k * atr_val
            risk = abs(stop - last_close)
            take_profit = last_close - self.config.rr_ratio * risk
            return TradeSignal(
                symbol=symbol, strategy_id=self.strategy_id, side=Side.SELL,
                timestamp=timestamp, price=last_close, stop_loss=stop,
                take_profit=take_profit, size=size, reason="mean_reversion_short",
            )

        return None

    def _evaluate_trend(self, candles_1h: List[Dict[str, float]]) -> tuple[bool, bool]:
        if len(candles_1h) < self.config.trend_ema_slow + 2:
            return True, True
        closes_1h = [c["close"] for c in candles_1h]
        ema_fast = ema(closes_1h, self.config.trend_ema_fast)
        ema_slow = ema(closes_1h, self.config.trend_ema_slow)
        if ema_fast is None or ema_slow is None:
            return True, True
        return ema_fast > ema_slow, ema_fast < ema_slow
