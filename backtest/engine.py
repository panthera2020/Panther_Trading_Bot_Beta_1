"""Shared backtest engine: trade model, SL/TP checking, sizing, simulation loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from strategies.indicators import (
    atr, bollinger_bands, ema, rsi, sma, vwap,
)


# ── Configuration ────────────────────────────────────────────────
@dataclass
class BacktestConfig:
    symbol: str = "BTCUSDT"
    starting_equity: float = 500.0
    leverage: int = 50
    risk_per_trade: float = 0.01
    monthly_volume_target: float = 1_000_000.0
    min_notional: float = 5.0
    taker_fee: float = 0.00055
    slippage: float = 0.0002


# ── Trade ─────────────────────────────────────────────────────────
@dataclass
class Trade:
    strategy: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit: Optional[float]
    size: float
    entry_time: datetime
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    pnl: float = 0.0
    fees: float = 0.0
    notional: float = 0.0
    closed: bool = False
    outcome: str = "open"


# ── Month result ──────────────────────────────────────────────────
@dataclass
class MonthResult:
    month_label: str
    starting_equity: float
    ending_equity: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    volume_generated: float
    weekly_results: List[Dict] = field(default_factory=list)


# ── Sizing ────────────────────────────────────────────────────────
def compute_size(
    risk_pct: float,
    equity: float,
    atr_val: float,
    k: float,
    price: float,
    min_notional: float = 5.0,
) -> float:
    if atr_val <= 0 or k <= 0 or price <= 0:
        return 0.0
    size = (risk_pct * equity) / (atr_val * k)
    if size * price < min_notional:
        return 0.0
    return size


# ── SL / TP check ────────────────────────────────────────────────
def check_sl_tp(trade: Trade, candle: Dict) -> Optional[float]:
    """Return exit price if SL or TP was hit during this candle, else None."""
    if trade.side == "BUY":
        if candle["low"] <= trade.stop_loss:
            return trade.stop_loss
        if trade.take_profit and candle["high"] >= trade.take_profit:
            return trade.take_profit
    else:
        if candle["high"] >= trade.stop_loss:
            return trade.stop_loss
        if trade.take_profit and candle["low"] <= trade.take_profit:
            return trade.take_profit
    return None


def close_trade(trade: Trade, exit_price: float, ts: datetime, fee_rate: float = 0.0) -> None:
    """Mutate a trade to closed state, computing PnL and fees."""
    if trade.side == "BUY":
        trade.pnl = (exit_price - trade.entry_price) * trade.size
    else:
        trade.pnl = (trade.entry_price - exit_price) * trade.size
    trade.notional = trade.size * trade.entry_price
    if fee_rate > 0:
        trade.fees = trade.notional * fee_rate * 2
        trade.pnl -= trade.fees
    trade.exit_price = exit_price
    trade.exit_time = ts
    trade.closed = True
    trade.outcome = "win" if trade.pnl > 0 else "loss"


# ── Strategy signal helpers (reuse existing indicators) ──────────

# Mean Reversion params
MR_BB_PERIOD = 20
MR_BB_STD = 2.0
MR_ATR_PERIOD = 14
MR_RSI_PERIOD = 14
MR_ATR_K = 2.0
MR_RSI_LONG = 30.0
MR_RSI_SHORT = 70.0
MR_TREND_EMA_FAST = 50
MR_TREND_EMA_SLOW = 200
MR_RR = 3.0

# Trend Breakout params
TB_LOOKBACK = 20
TB_EMA_FAST = 50
TB_EMA_SLOW = 200
TB_ATR_K = 2.0
TB_VOL_SMA = 20
TB_MIN_GAP = 0.005
TB_RR = 3.0

# Strategy C params
C3_ATR_PERIOD = 14
C3_ATR_SL_MULT = 0.5


def mean_reversion_signal(candles_5m: List[Dict], candles_1h: List[Dict]) -> Optional[Dict]:
    if len(candles_5m) < max(MR_BB_PERIOD, MR_ATR_PERIOD, MR_RSI_PERIOD) + 2:
        return None

    closes = [c["close"] for c in candles_5m]
    highs = [c["high"] for c in candles_5m]
    lows = [c["low"] for c in candles_5m]
    volumes = [c["volume"] for c in candles_5m]

    bands = bollinger_bands(closes, MR_BB_PERIOD, MR_BB_STD)
    atr_val = atr(highs, lows, closes, MR_ATR_PERIOD)
    vwap_val = vwap(closes[-MR_BB_PERIOD:], volumes[-MR_BB_PERIOD:])
    rsi_val = rsi(closes, MR_RSI_PERIOD)

    if bands is None or atr_val is None or vwap_val is None:
        return None

    lower, mid, upper = bands
    last_close = closes[-1]

    trend_long_ok, trend_short_ok = True, True
    if len(candles_1h) >= MR_TREND_EMA_SLOW + 2:
        closes_1h = [c["close"] for c in candles_1h]
        ema_f = ema(closes_1h, MR_TREND_EMA_FAST)
        ema_s = ema(closes_1h, MR_TREND_EMA_SLOW)
        if ema_f is not None and ema_s is not None:
            trend_long_ok = ema_f > ema_s
            trend_short_ok = ema_f < ema_s

    rsi_long_ok = rsi_val is None or rsi_val < MR_RSI_LONG
    rsi_short_ok = rsi_val is None or rsi_val > MR_RSI_SHORT

    # Reaction candle confirmation
    bullish_reaction = candles_5m[-1]["close"] > candles_5m[-1]["open"]
    bearish_reaction = candles_5m[-1]["close"] < candles_5m[-1]["open"]

    if last_close < lower and last_close < vwap_val and rsi_long_ok and trend_long_ok and bullish_reaction:
        stop = last_close - MR_ATR_K * atr_val
        risk = abs(last_close - stop)
        return {"side": "BUY", "price": last_close, "sl": stop, "tp": last_close + MR_RR * risk, "atr": atr_val, "strategy": "scalp"}

    if last_close > upper and last_close > vwap_val and rsi_short_ok and trend_short_ok and bearish_reaction:
        stop = last_close + MR_ATR_K * atr_val
        risk = abs(stop - last_close)
        return {"side": "SELL", "price": last_close, "sl": stop, "tp": last_close - MR_RR * risk, "atr": atr_val, "strategy": "scalp"}

    return None


def trend_breakout_signal(candles_1h: List[Dict]) -> Optional[Dict]:
    if len(candles_1h) < max(TB_EMA_SLOW, TB_LOOKBACK) + 2:
        return None

    closes = [c["close"] for c in candles_1h]
    highs = [c["high"] for c in candles_1h]
    lows = [c["low"] for c in candles_1h]
    volumes = [c["volume"] for c in candles_1h]

    ema_f = ema(closes, TB_EMA_FAST)
    ema_s = ema(closes, TB_EMA_SLOW)
    atr_val = atr(highs, lows, closes, 14)
    vol_avg = sma(volumes, TB_VOL_SMA)

    if ema_f is None or ema_s is None or atr_val is None:
        return None

    gap = abs(ema_f - ema_s) / ema_s
    if gap < TB_MIN_GAP:
        return None

    recent_high = max(highs[-TB_LOOKBACK:])
    recent_low = min(lows[-TB_LOOKBACK:])
    last_close = closes[-1]
    volume_ok = vol_avg is None or volumes[-1] > vol_avg

    # Strong candle confirmation
    last = candles_1h[-1]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    strong = rng > 0 and body / rng > 0.5

    if ema_f > ema_s and last_close > recent_high and volume_ok and strong:
        stop = last_close - TB_ATR_K * atr_val
        risk = abs(last_close - stop)
        return {"side": "BUY", "price": last_close, "sl": stop, "tp": last_close + TB_RR * risk, "atr": atr_val, "strategy": "trend"}

    if ema_f < ema_s and last_close < recent_low and volume_ok and strong:
        stop = last_close + TB_ATR_K * atr_val
        risk = abs(stop - last_close)
        return {"side": "SELL", "price": last_close, "sl": stop, "tp": last_close - TB_RR * risk, "atr": atr_val, "strategy": "trend"}

    return None


def candle3_signal(candles_3m: List[Dict]) -> Optional[Dict]:
    if len(candles_3m) < max(3, C3_ATR_PERIOD) + 1:
        return None

    atr_val = atr(
        [c["high"] for c in candles_3m],
        [c["low"] for c in candles_3m],
        [c["close"] for c in candles_3m],
        C3_ATR_PERIOD,
    )
    if atr_val is None:
        return None

    last_three = candles_3m[-3:]
    bull = all(c["close"] > c["open"] for c in last_three)
    bear = all(c["close"] < c["open"] for c in last_three)

    if not bull and not bear:
        return None

    volumes = [c["volume"] for c in candles_3m]
    if not (volumes[-1] > volumes[-2] > volumes[-3]):
        return None

    last_close = candles_3m[-1]["close"]
    stop_dist = C3_ATR_SL_MULT * atr_val

    if bull:
        return {"side": "BUY", "price": last_close, "sl": last_close - stop_dist, "tp": None, "atr": atr_val, "strategy": "candle3"}
    if bear:
        return {"side": "SELL", "price": last_close, "sl": last_close + stop_dist, "tp": None, "atr": atr_val, "strategy": "candle3"}
    return None
