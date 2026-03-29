"""
Panther Trading Bot - Backtester
Pulls real Bybit OHLCV data and simulates the upgraded MeanReversion + TrendBreakout + Candle3 strategies.

Usage:
    pip install pybit requests
    python backtest.py

Two separate runs:
    1) 3-month backtest (Jan 2026 - Mar 2026) - restart $500 each month
    2) 6-month backtest (Oct 2025 - Mar 2026) - restart $500 each month

Outputs weekly performance breakdown with volume tracking per month.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from strategies.indicators import atr, bollinger_bands, ema, rsi, vwap, sma

# Try to use pybit for real data, fall back to requests
try:
    from pybit.unified_trading import HTTP as BybitHTTP
    HAS_PYBIT = True
except ImportError:
    HAS_PYBIT = False
    import requests


# ─── Configuration ───────────────────────────────────────────────
SYMBOL = "BTCUSDT"
STARTING_EQUITY = 500.0
LEVERAGE = 50
RISK_PER_TRADE = 0.01  # 1%
MONTHLY_VOLUME_TARGET = 1_000_000.0
TRADING_DAYS = 30
MIN_NOTIONAL = 5.0

# Strategy params (upgraded)
MR_BB_PERIOD = 20
MR_BB_STD = 2.0
MR_ATR_PERIOD = 14
MR_RSI_PERIOD = 14
MR_ATR_K = 2.0       # widened from 1.5
MR_RSI_LONG = 30.0   # loosened from 25
MR_RSI_SHORT = 70.0  # loosened from 75
MR_TREND_EMA_FAST = 50
MR_TREND_EMA_SLOW = 200

TB_LOOKBACK = 20
TB_EMA_FAST = 50
TB_EMA_SLOW = 200
TB_ATR_K = 2.0
TB_VOL_SMA = 20
TB_MIN_GAP = 0.005

C3_ATR_PERIOD = 14


# ─── Data Fetching ───────────────────────────────────────────────
def fetch_bybit_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[Dict]:
    """Fetch OHLCV candles from Bybit public API."""
    all_candles = []
    cursor_end = end_ms

    while cursor_end > start_ms:
        if HAS_PYBIT:
            session = BybitHTTP(testnet=False)
            resp = session.get_kline(
                category="linear", symbol=symbol, interval=interval,
                start=start_ms, end=cursor_end, limit=1000
            )
            rows = resp.get("result", {}).get("list", [])
        else:
            url = "https://api.bybit.com/v5/market/kline"
            params = {
                "category": "linear", "symbol": symbol, "interval": interval,
                "start": start_ms, "end": cursor_end, "limit": 1000
            }
            resp = requests.get(url, params=params, timeout=15)
            rows = resp.json().get("result", {}).get("list", [])

        if not rows:
            break

        for row in rows:
            ts_val = int(row[0])
            if ts_val < start_ms:
                continue
            all_candles.append({
                "timestamp": ts_val,
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]),
            })

        oldest_ts = min(int(r[0]) for r in rows)
        if oldest_ts >= cursor_end:
            break
        cursor_end = oldest_ts - 1

    all_candles.sort(key=lambda c: c["timestamp"])
    # deduplicate
    seen = set()
    unique = []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)
    return unique


def interval_to_bybit(tf: str) -> str:
    return {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}[tf]


# ─── Trade Simulation ────────────────────────────────────────────
@dataclass
class Trade:
    side: str
    entry_price: float
    stop_loss: float
    take_profit: Optional[float]
    size: float
    strategy: str
    entry_time: datetime
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    pnl: float = 0.0
    notional: float = 0.0
    closed: bool = False


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


def compute_size(equity: float, atr_val: float, k: float, price: float) -> float:
    if atr_val <= 0 or k <= 0 or price <= 0:
        return 0.0
    risk_size = (RISK_PER_TRADE * equity) / (atr_val * k)
    notional = risk_size * price
    if notional < MIN_NOTIONAL:
        return 0.0
    return risk_size


def check_sl_tp(trade: Trade, candle: Dict) -> Optional[float]:
    """Check if stop loss or take profit was hit during this candle."""
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


# ─── Strategy Logic ──────────────────────────────────────────────
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

    # Trend filter from 1H
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

    if last_close < lower and last_close < vwap_val and rsi_long_ok and trend_long_ok:
        stop = last_close - MR_ATR_K * atr_val
        risk = abs(last_close - stop)
        tp = last_close + 2 * risk
        return {"side": "BUY", "price": last_close, "sl": stop, "tp": tp, "atr": atr_val, "strategy": "scalp"}

    if last_close > upper and last_close > vwap_val and rsi_short_ok and trend_short_ok:
        stop = last_close + MR_ATR_K * atr_val
        risk = abs(stop - last_close)
        tp = last_close - 2 * risk
        return {"side": "SELL", "price": last_close, "sl": stop, "tp": tp, "atr": atr_val, "strategy": "scalp"}

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

    if ema_f > ema_s and last_close > recent_high and volume_ok:
        stop = last_close - TB_ATR_K * atr_val
        tp = last_close + TB_ATR_K * atr_val
        return {"side": "BUY", "price": last_close, "sl": stop, "tp": tp, "atr": atr_val, "strategy": "trend"}

    if ema_f < ema_s and last_close < recent_low and volume_ok:
        stop = last_close + TB_ATR_K * atr_val
        tp = last_close - TB_ATR_K * atr_val
        return {"side": "SELL", "price": last_close, "sl": stop, "tp": tp, "atr": atr_val, "strategy": "trend"}

    return None


def candle3_signal(candles_3m: List[Dict]) -> Optional[Dict]:
    if len(candles_3m) < max(3, C3_ATR_PERIOD) + 1:
        return None

    atr_val = atr([c["high"] for c in candles_3m], [c["low"] for c in candles_3m],
                  [c["close"] for c in candles_3m], C3_ATR_PERIOD)
    if atr_val is None:
        return None

    last_three = candles_3m[-3:]
    bull = all(c["close"] > c["open"] for c in last_three)
    bear = all(c["close"] < c["open"] for c in last_three)

    if not bull and not bear:
        return None

    # Volume check
    volumes = [c["volume"] for c in candles_3m]
    if not (volumes[-1] > volumes[-2] > volumes[-3]):
        return None

    first_open = last_three[0]["open"]
    last_close = candles_3m[-1]["close"]

    if bull:
        return {"side": "BUY", "price": last_close, "sl": first_open, "tp": None, "atr": atr_val, "strategy": "candle3"}
    if bear:
        return {"side": "SELL", "price": last_close, "sl": first_open, "tp": None, "atr": atr_val, "strategy": "candle3"}

    return None


# ─── Backtest Engine ─────────────────────────────────────────────
def run_backtest_month(start_dt: datetime, end_dt: datetime, month_label: str) -> MonthResult:
    print(f"\n{'='*60}")
    print(f"  Backtesting: {month_label}")
    print(f"  Period: {start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')}")
    print(f"  Starting equity: ${STARTING_EQUITY:.2f}")
    print(f"{'='*60}")

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    print("  Fetching 1H candles...")
    candles_1h = fetch_bybit_klines(SYMBOL, interval_to_bybit("1h"), start_ms, end_ms)
    print(f"    Got {len(candles_1h)} candles")

    print("  Fetching 5m candles...")
    candles_5m = fetch_bybit_klines(SYMBOL, interval_to_bybit("5m"), start_ms, end_ms)
    print(f"    Got {len(candles_5m)} candles")

    print("  Fetching 3m candles...")
    candles_3m = fetch_bybit_klines(SYMBOL, interval_to_bybit("3m"), start_ms, end_ms)
    print(f"    Got {len(candles_3m)} candles")

    equity = STARTING_EQUITY
    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    volume_generated = 0.0

    weekly_results = []
    current_week_start = start_dt
    week_trades = 0
    week_wins = 0
    week_pnl = 0.0
    week_volume = 0.0

    # Walk through 5m candles as the primary timeframe
    for i, candle in enumerate(candles_5m):
        candle_dt = datetime.fromtimestamp(candle["timestamp"] / 1000.0, tz=timezone.utc)

        # Weekly boundary check
        if (candle_dt - current_week_start).days >= 7:
            weekly_results.append({
                "week_start": current_week_start.strftime("%Y-%m-%d"),
                "week_end": (current_week_start + timedelta(days=6)).strftime("%Y-%m-%d"),
                "trades": week_trades,
                "wins": week_wins,
                "win_rate": round((week_wins / week_trades * 100) if week_trades > 0 else 0.0, 1),
                "pnl": round(week_pnl, 2),
                "equity": round(equity, 2),
                "volume": round(week_volume, 2),
            })
            current_week_start = candle_dt
            week_trades = 0
            week_wins = 0
            week_pnl = 0.0
            week_volume = 0.0

        # Check open trade for SL/TP hit
        if open_trade:
            exit_price = check_sl_tp(open_trade, candle)
            if exit_price is not None:
                if open_trade.side == "BUY":
                    pnl = (exit_price - open_trade.entry_price) * open_trade.size
                else:
                    pnl = (open_trade.entry_price - exit_price) * open_trade.size

                open_trade.exit_price = exit_price
                open_trade.exit_time = candle_dt
                open_trade.pnl = pnl
                open_trade.closed = True
                notional = open_trade.size * open_trade.entry_price
                open_trade.notional = notional

                equity += pnl
                volume_generated += notional * 2  # entry + exit
                week_volume += notional * 2

                week_trades += 1
                week_pnl += pnl
                if pnl > 0:
                    week_wins += 1

                trades.append(open_trade)
                open_trade = None

                if equity <= 0:
                    print(f"    LIQUIDATED at {candle_dt.strftime('%Y-%m-%d %H:%M')}")
                    break

            # For candle3, auto-close after ~30 seconds (10 x 3m candles)
            if open_trade and open_trade.strategy == "candle3":
                bars_held = 0
                for j in range(len(candles_3m)):
                    if candles_3m[j]["timestamp"] >= open_trade.entry_time.timestamp() * 1000:
                        bars_held = i - j if j <= i else 0
                        break
                if bars_held >= 10:  # roughly 30 seconds equivalent
                    exit_price = candle["close"]
                    if open_trade.side == "BUY":
                        pnl = (exit_price - open_trade.entry_price) * open_trade.size
                    else:
                        pnl = (open_trade.entry_price - exit_price) * open_trade.size

                    open_trade.exit_price = exit_price
                    open_trade.exit_time = candle_dt
                    open_trade.pnl = pnl
                    open_trade.closed = True
                    notional = open_trade.size * open_trade.entry_price
                    open_trade.notional = notional

                    equity += pnl
                    volume_generated += notional * 2
                    week_volume += notional * 2

                    week_trades += 1
                    week_pnl += pnl
                    if pnl > 0:
                        week_wins += 1

                    trades.append(open_trade)
                    open_trade = None

            continue  # Don't open new trade while one is open

        # Try to generate signals (priority: trend > scalp > candle3)
        # Build lookback windows
        candles_1h_window = [c for c in candles_1h if c["timestamp"] <= candle["timestamp"]][-250:]
        candles_5m_window = candles_5m[max(0, i - 200):i + 1]
        candles_3m_window = [c for c in candles_3m if c["timestamp"] <= candle["timestamp"]][-200:]

        signal = None

        # 1. Try trend breakout on 1H
        sig = trend_breakout_signal(candles_1h_window)
        if sig:
            signal = sig

        # 2. Try mean reversion on 5m with 1H trend filter
        if not signal:
            sig = mean_reversion_signal(candles_5m_window, candles_1h_window)
            if sig:
                signal = sig

        # 3. Try candle3 on 3m
        if not signal:
            sig = candle3_signal(candles_3m_window)
            if sig:
                signal = sig

        if signal:
            size = compute_size(equity, signal["atr"], MR_ATR_K if signal["strategy"] == "scalp" else TB_ATR_K if signal["strategy"] == "trend" else 1.0, signal["price"])
            if size > 0:
                open_trade = Trade(
                    side=signal["side"], entry_price=signal["price"],
                    stop_loss=signal["sl"], take_profit=signal["tp"],
                    size=size, strategy=signal["strategy"], entry_time=candle_dt,
                )

    # Final weekly entry
    if week_trades > 0 or week_pnl != 0:
        weekly_results.append({
            "week_start": current_week_start.strftime("%Y-%m-%d"),
            "week_end": end_dt.strftime("%Y-%m-%d"),
            "trades": week_trades,
            "wins": week_wins,
            "win_rate": round((week_wins / week_trades * 100) if week_trades > 0 else 0.0, 1),
            "pnl": round(week_pnl, 2),
            "equity": round(equity, 2),
            "volume": round(week_volume, 2),
        })

    total_trades = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = total_trades - wins
    total_pnl = sum(t.pnl for t in trades)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

    result = MonthResult(
        month_label=month_label, starting_equity=STARTING_EQUITY,
        ending_equity=round(equity, 2), total_trades=total_trades,
        wins=wins, losses=losses, win_rate=round(win_rate, 1),
        total_pnl=round(total_pnl, 2), volume_generated=round(volume_generated, 2),
        weekly_results=weekly_results,
    )

    print(f"\n  Results for {month_label}:")
    print(f"    Trades: {total_trades} | Wins: {wins} | Losses: {losses}")
    print(f"    Win Rate: {win_rate:.1f}%")
    print(f"    P&L: ${total_pnl:,.2f}")
    print(f"    Ending Equity: ${equity:,.2f}")
    print(f"    Volume Generated: ${volume_generated:,.2f}")

    return result


def print_detailed_report(title: str, results: List[MonthResult]):
    print(f"\n{'#'*70}")
    print(f"  {title}")
    print(f"{'#'*70}")

    total_pnl = sum(r.total_pnl for r in results)
    total_trades = sum(r.total_trades for r in results)
    total_wins = sum(r.wins for r in results)
    total_volume = sum(r.volume_generated for r in results)
    overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0.0

    print(f"\n  OVERALL SUMMARY")
    print(f"  {'='*50}")
    print(f"  Total Months: {len(results)}")
    print(f"  Total Trades: {total_trades}")
    print(f"  Overall Win Rate: {overall_wr:.1f}%")
    print(f"  Total P&L: ${total_pnl:,.2f}")
    print(f"  Total Volume: ${total_volume:,.2f}")
    print(f"  Avg Monthly Volume: ${total_volume / len(results):,.2f}")

    print(f"\n  MONTHLY BREAKDOWN")
    print(f"  {'='*50}")
    print(f"  {'Month':<15} {'Trades':>7} {'WR%':>6} {'P&L':>10} {'End Eq':>10} {'Volume':>12}")
    print(f"  {'-'*60}")
    for r in results:
        print(f"  {r.month_label:<15} {r.total_trades:>7} {r.win_rate:>5.1f}% ${r.total_pnl:>9,.2f} ${r.ending_equity:>9,.2f} ${r.volume_generated:>11,.2f}")

    print(f"\n  WEEKLY BREAKDOWN")
    print(f"  {'='*50}")
    for r in results:
        print(f"\n  --- {r.month_label} ---")
        print(f"  {'Week':<25} {'Trades':>7} {'WR%':>6} {'P&L':>10} {'Equity':>10} {'Volume':>12}")
        print(f"  {'-'*70}")
        for w in r.weekly_results:
            print(f"  {w['week_start']} - {w['week_end'][-5:]}  {w['trades']:>7} {w['win_rate']:>5.1f}% ${w['pnl']:>9,.2f} ${w['equity']:>9,.2f} ${w['volume']:>11,.2f}")


def main():
    # ─── 3-MONTH BACKTEST ────────────────────────────────────────
    months_3 = [
        (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 31, 23, 59, tzinfo=timezone.utc), "Jan 2026"),
        (datetime(2026, 2, 1, tzinfo=timezone.utc), datetime(2026, 2, 28, 23, 59, tzinfo=timezone.utc), "Feb 2026"),
        (datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 28, 23, 59, tzinfo=timezone.utc), "Mar 2026"),
    ]

    results_3m = []
    for start, end, label in months_3:
        result = run_backtest_month(start, end, label)
        results_3m.append(result)

    print_detailed_report("3-MONTH BACKTEST (Jan-Mar 2026) | $500 restart each month", results_3m)

    # ─── 6-MONTH BACKTEST ────────────────────────────────────────
    months_6 = [
        (datetime(2025, 10, 1, tzinfo=timezone.utc), datetime(2025, 10, 31, 23, 59, tzinfo=timezone.utc), "Oct 2025"),
        (datetime(2025, 11, 1, tzinfo=timezone.utc), datetime(2025, 11, 30, 23, 59, tzinfo=timezone.utc), "Nov 2025"),
        (datetime(2025, 12, 1, tzinfo=timezone.utc), datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc), "Dec 2025"),
        (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 31, 23, 59, tzinfo=timezone.utc), "Jan 2026"),
        (datetime(2026, 2, 1, tzinfo=timezone.utc), datetime(2026, 2, 28, 23, 59, tzinfo=timezone.utc), "Feb 2026"),
        (datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 28, 23, 59, tzinfo=timezone.utc), "Mar 2026"),
    ]

    results_6m = []
    for start, end, label in months_6:
        result = run_backtest_month(start, end, label)
        results_6m.append(result)

    print_detailed_report("6-MONTH BACKTEST (Oct 2025 - Mar 2026) | $500 restart each month", results_6m)

    # Save JSON report
    report = {
        "3_month_backtest": {
            "months": [{
                "month": r.month_label, "starting_equity": r.starting_equity,
                "ending_equity": r.ending_equity, "trades": r.total_trades,
                "wins": r.wins, "losses": r.losses, "win_rate": r.win_rate,
                "pnl": r.total_pnl, "volume": r.volume_generated,
                "weekly": r.weekly_results,
            } for r in results_3m]
        },
        "6_month_backtest": {
            "months": [{
                "month": r.month_label, "starting_equity": r.starting_equity,
                "ending_equity": r.ending_equity, "trades": r.total_trades,
                "wins": r.wins, "losses": r.losses, "win_rate": r.win_rate,
                "pnl": r.total_pnl, "volume": r.volume_generated,
                "weekly": r.weekly_results,
            } for r in results_6m]
        },
    }
    with open("backtest_results.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nResults saved to backtest_results.json")


if __name__ == "__main__":
    main()
