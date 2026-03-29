"""
Panther Trading Bot — Bybit Backtest Engine
============================================
Fetches real OHLCV data from Bybit (via ccxt), then replays it bar-by-bar
through the bot's on_market_data() pipeline.

Usage
-----
  pip install ccxt pandas tabulate
  python backtest_bybit.py --period 3   # 3-month backtest
  python backtest_bybit.py --period 6   # 6-month backtest

Rules
-----
- Starting capital : $500 (reset at the 1st of every month)
- Leverage         : 50x (Bybit perpetual default in config)
- Strategies       : trend (hybrid) + scalp (mean-reversion) — candle3 OFF
- Fees             : 0.055% taker per side (Bybit standard)
- Slippage         : 0.02% per trade (conservative estimate)
- All-sessions     : Asia / London / NY — no session gate
"""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import logging

# ── third-party ──────────────────────────────────────────────────────────────
try:
    import ccxt
    import pandas as pd
    from tabulate import tabulate
except ImportError:
    print("Installing required packages …")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "ccxt", "pandas", "tabulate"])
    import ccxt
    import pandas as pd
    from tabulate import tabulate

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("backtest")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL          = "BTC/USDT:USDT"   # Bybit linear perp
SYMBOL_DISPLAY  = "BTCUSDT"
LEVERAGE        = 50
TAKER_FEE       = 0.00055           # 0.055% per side
SLIPPAGE        = 0.0002            # 0.02% per side
STARTING_EQUITY = 500.0
RISK_PCT        = 0.01              # 1% risk per trade
ATR_PERIOD      = 14
RR_RATIO        = 2.0               # take-profit at 2R
MIN_NOTIONAL    = 5.0               # Bybit minimum

# Session multipliers (from session_manager.py)
SESSION_MULTS = {
    "ASIA":   {"trend": 0.3, "scalp": 0.6},
    "LONDON": {"trend": 0.6, "scalp": 0.8},
    "NY":     {"trend": 1.0, "scalp": 1.0},
}

# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_ohlcv(exchange: ccxt.Exchange, symbol: str, timeframe: str,
                since_ms: int, until_ms: int) -> pd.DataFrame:
    """Paginated OHLCV fetch from Bybit."""
    all_candles = []
    current = since_ms
    limit = 1000
    while current < until_ms:
        candles = exchange.fetch_ohlcv(symbol, timeframe, since=current, limit=limit)
        if not candles:
            break
        all_candles.extend(candles)
        current = candles[-1][0] + 1
        if len(candles) < limit:
            break
    if not all_candles:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(all_candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df[df["ts"] < pd.Timestamp(until_ms, unit="ms", tz="UTC")]
    return df.reset_index(drop=True)


def to_candle_list(df: pd.DataFrame) -> List[Dict]:
    return [
        {"ts": row.ts, "open": row.open, "high": row.high,
         "low": row.low, "close": row.close, "volume": row.volume}
        for row in df.itertuples()
    ]

# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def atr(candles: List[Dict], period: int = ATR_PERIOD) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

# ─────────────────────────────────────────────────────────────────────────────
# SESSION
# ─────────────────────────────────────────────────────────────────────────────

def current_session(ts: datetime) -> str:
    h = ts.hour
    if 0 <= h < 8:   return "ASIA"
    if 8 <= h < 16:  return "LONDON"
    return "NY"

# ─────────────────────────────────────────────────────────────────────────────
# TREND STRATEGY (simplified hybrid — bias + ATR breakout)
# ─────────────────────────────────────────────────────────────────────────────

def trend_signal(candles_1h: List[Dict], candles_1m: List[Dict],
                 ts: datetime) -> Optional[Tuple[str, float, float, float]]:
    """Returns (side, entry, stop_loss, take_profit) or None."""
    if len(candles_1h) < 50 or len(candles_1m) < 20:
        return None

    closes_1h = [c["close"] for c in candles_1h]
    ema20 = ema(closes_1h, 20)
    ema50 = ema(closes_1h, 50)
    if ema20 is None or ema50 is None:
        return None

    bias = "LONG" if ema20 > ema50 else "SHORT"

    atr_val = atr(candles_1m)
    if atr_val is None:
        return None

    last = candles_1m[-1]
    entry = last["close"]

    if bias == "LONG":
        # Require the last 1m close to be above the prior high (sweep confirmation)
        if entry <= candles_1m[-2]["high"]:
            return None
        stop_loss   = entry - 1.5 * atr_val
        take_profit = entry + RR_RATIO * (entry - stop_loss)
        return ("BUY", entry, stop_loss, take_profit)
    else:
        if entry >= candles_1m[-2]["low"]:
            return None
        stop_loss   = entry + 1.5 * atr_val
        take_profit = entry - RR_RATIO * (stop_loss - entry)
        return ("SELL", entry, stop_loss, take_profit)

# ─────────────────────────────────────────────────────────────────────────────
# SCALP STRATEGY (mean reversion — RSI + Bollinger)
# ─────────────────────────────────────────────────────────────────────────────

def bollinger(candles: List[Dict], period: int = 20, std_mult: float = 2.0):
    closes = [c["close"] for c in candles[-period:]]
    if len(closes) < period:
        return None, None, None
    mid = sum(closes) / period
    var = sum((c - mid) ** 2 for c in closes) / period
    std = var ** 0.5
    return mid - std_mult * std, mid, mid + std_mult * std


def rsi(candles: List[Dict], period: int = 14) -> Optional[float]:
    closes = [c["close"] for c in candles[-(period + 1):]]
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def scalp_signal(candles_5m: List[Dict],
                 ts: datetime) -> Optional[Tuple[str, float, float, float]]:
    if len(candles_5m) < 25:
        return None

    lower, mid, upper = bollinger(candles_5m)
    rsi_val = rsi(candles_5m)
    if lower is None or rsi_val is None:
        return None

    atr_val = atr(candles_5m)
    if atr_val is None:
        return None

    last = candles_5m[-1]
    price = last["close"]

    if price <= lower and rsi_val < 35:
        stop_loss   = price - atr_val
        take_profit = price + RR_RATIO * atr_val
        return ("BUY", price, stop_loss, take_profit)
    if price >= upper and rsi_val > 65:
        stop_loss   = price + atr_val
        take_profit = price - RR_RATIO * atr_val
        return ("SELL", price, stop_loss, take_profit)
    return None

# ─────────────────────────────────────────────────────────────────────────────
# POSITION SIZING
# ─────────────────────────────────────────────────────────────────────────────

def size_position(equity: float, entry: float, stop_loss: float,
                  session: str, strategy: str) -> float:
    risk_dollar = RISK_PCT * equity
    risk_points = abs(entry - stop_loss)
    if risk_points <= 0:
        return 0.0
    mult = SESSION_MULTS.get(session, {}).get(strategy, 1.0)
    size = (risk_dollar / risk_points) * mult
    notional = size * entry
    if notional < MIN_NOTIONAL:
        return 0.0
    return size

# ─────────────────────────────────────────────────────────────────────────────
# TRADE DATACLASS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    strategy: str
    symbol: str
    side: str
    entry_time: datetime
    exit_time: Optional[datetime]
    entry: float
    stop_loss: float
    take_profit: float
    size: float
    exit_price: Optional[float] = None
    pnl: float = 0.0
    fees: float = 0.0
    notional: float = 0.0
    outcome: str = "open"   # "win" | "loss" | "open"
    session: str = ""
    month_label: str = ""

# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class BacktestEngine:
    def __init__(self, period_months: int):
        self.period_months = period_months
        self.trades: List[Trade] = []
        self.monthly_snapshots: List[Dict] = []
        self._open_trend: Optional[Trade] = None
        self._open_scalp: Optional[Trade] = None
        self._last_trend_ts: Optional[datetime] = None
        self.COOLDOWN = timedelta(seconds=120)

    # ── helpers ──────────────────────────────────────────────────────

    def _apply_fees(self, trade: Trade):
        notional = trade.size * trade.entry
        trade.notional = notional
        trade.fees = notional * (TAKER_FEE + SLIPPAGE) * 2  # entry + exit

    def _close_trade(self, trade: Trade, exit_price: float, ts: datetime, outcome: str):
        trade.exit_price = exit_price
        trade.exit_time  = ts
        trade.outcome    = outcome
        if trade.side == "BUY":
            trade.pnl = (exit_price - trade.entry) * trade.size
        else:
            trade.pnl = (trade.entry - exit_price) * trade.size
        trade.pnl -= trade.fees

    def _check_position(self, trade: Optional[Trade], high: float, low: float,
                        ts: datetime) -> Optional[Trade]:
        """Check if SL or TP was hit in this bar. Returns None if closed."""
        if trade is None:
            return None
        if trade.side == "BUY":
            if low <= trade.stop_loss:
                self._close_trade(trade, trade.stop_loss * (1 - SLIPPAGE), ts, "loss")
                return None
            if high >= trade.take_profit:
                self._close_trade(trade, trade.take_profit * (1 - SLIPPAGE), ts, "win")
                return None
        else:
            if high >= trade.stop_loss:
                self._close_trade(trade, trade.stop_loss * (1 + SLIPPAGE), ts, "loss")
                return None
            if low <= trade.take_profit:
                self._close_trade(trade, trade.take_profit * (1 + SLIPPAGE), ts, "win")
                return None
        return trade

    # ── main loop ────────────────────────────────────────────────────

    def run(self, df_1h: pd.DataFrame, df_5m: pd.DataFrame, df_1m: pd.DataFrame):
        candles_1h = to_candle_list(df_1h)
        candles_5m = to_candle_list(df_5m)
        candles_1m = to_candle_list(df_1m)

        # Build index maps for efficient lookup
        idx_5m = {c["ts"]: i for i, c in enumerate(candles_5m)}
        idx_1m = {c["ts"]: i for i, c in enumerate(candles_1m)}

        equity = STARTING_EQUITY
        month_equity_start = STARTING_EQUITY
        current_month = None
        month_trades: List[Trade] = []
        closed_pnl_month = 0.0
        volume_month = 0.0

        # Align on 1h bars
        for i in range(50, len(candles_1h)):
            bar      = candles_1h[i]
            ts: datetime = bar["ts"].to_pydatetime()
            bar_high = bar["high"]
            bar_low  = bar["low"]

            month_label = ts.strftime("%Y-%m")

            # ── monthly reset ────────────────────────────────────────
            if current_month is None:
                current_month = month_label
            if month_label != current_month:
                # close any open positions at last close
                close_price = candles_1h[i - 1]["close"]
                if self._open_trend:
                    self._close_trade(self._open_trend, close_price, ts, "open_close")
                    closed_pnl_month += self._open_trend.pnl
                    volume_month     += self._open_trend.notional
                    self._open_trend  = None
                if self._open_scalp:
                    self._close_trade(self._open_scalp, close_price, ts, "open_close")
                    closed_pnl_month += self._open_scalp.pnl
                    volume_month     += self._open_scalp.notional
                    self._open_scalp  = None

                self._record_month(current_month, month_equity_start, equity,
                                   month_trades, closed_pnl_month, volume_month)

                # reset for new month
                equity             = STARTING_EQUITY
                month_equity_start = STARTING_EQUITY
                month_trades       = []
                closed_pnl_month   = 0.0
                volume_month       = 0.0
                current_month      = month_label

            session = current_session(ts)

            # ── check existing positions ─────────────────────────────
            prev_trend = self._open_trend
            prev_scalp = self._open_scalp

            self._open_trend = self._check_position(self._open_trend, bar_high, bar_low, ts)
            self._open_scalp = self._check_position(self._open_scalp, bar_high, bar_low, ts)

            for old, new in [(prev_trend, self._open_trend), (prev_scalp, self._open_scalp)]:
                if old and new is None:
                    closed_pnl_month += old.pnl
                    volume_month     += old.notional
                    equity           += old.pnl
                    month_trades.append(old)
                    self.trades.append(old)

            # ── new signal: trend ────────────────────────────────────
            if self._open_trend is None and self._open_scalp is None:
                # find the closest 1m bar
                ts_1m_key = ts.replace(second=0, microsecond=0)
                j = idx_1m.get(pd.Timestamp(ts_1m_key, tz="UTC"))
                if j is not None and j >= 20:
                    slice_1m = candles_1m[max(0, j - 300): j + 1]
                    sig = trend_signal(candles_1h[max(0, i - 300): i + 1], slice_1m, ts)
                    if sig:
                        side, entry, sl, tp = sig
                        if self._last_trend_ts is None or (ts - self._last_trend_ts) >= self.COOLDOWN:
                            sz = size_position(equity, entry, sl, session, "trend")
                            if sz > 0:
                                t = Trade(
                                    strategy="trend",
                                    symbol=SYMBOL_DISPLAY,
                                    side=side,
                                    entry_time=ts,
                                    exit_time=None,
                                    entry=entry,
                                    stop_loss=sl,
                                    take_profit=tp,
                                    size=sz,
                                    session=session,
                                    month_label=month_label,
                                )
                                self._apply_fees(t)
                                self._open_trend    = t
                                self._last_trend_ts = ts

            # ── new signal: scalp ────────────────────────────────────
            if self._open_trend is None and self._open_scalp is None:
                ts_5m_key = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
                j5 = idx_5m.get(pd.Timestamp(ts_5m_key, tz="UTC"))
                if j5 is not None and j5 >= 25:
                    slice_5m = candles_5m[max(0, j5 - 300): j5 + 1]
                    sig = scalp_signal(slice_5m, ts)
                    if sig:
                        side, entry, sl, tp = sig
                        sz = size_position(equity, entry, sl, session, "scalp")
                        if sz > 0:
                            t = Trade(
                                strategy="scalp",
                                symbol=SYMBOL_DISPLAY,
                                side=side,
                                entry_time=ts,
                                exit_time=None,
                                entry=entry,
                                stop_loss=sl,
                                take_profit=tp,
                                size=sz,
                                session=session,
                                month_label=month_label,
                            )
                            self._apply_fees(t)
                            self._open_scalp = t

        # ── flush last month ─────────────────────────────────────────
        if current_month:
            close_price = candles_1h[-1]["close"]
            if self._open_trend:
                self._close_trade(self._open_trend, close_price,
                                  candles_1h[-1]["ts"].to_pydatetime(), "open_close")
                closed_pnl_month += self._open_trend.pnl
                volume_month     += self._open_trend.notional
                self.trades.append(self._open_trend)
            if self._open_scalp:
                self._close_trade(self._open_scalp, close_price,
                                  candles_1h[-1]["ts"].to_pydatetime(), "open_close")
                closed_pnl_month += self._open_scalp.pnl
                volume_month     += self._open_scalp.notional
                self.trades.append(self._open_scalp)
            self._record_month(current_month, month_equity_start, equity,
                               month_trades, closed_pnl_month, volume_month)

    def _record_month(self, month: str, start_equity: float, end_equity: float,
                      trades: List[Trade], closed_pnl: float, volume: float):
        wins   = [t for t in trades if t.outcome == "win"]
        losses = [t for t in trades if t.outcome == "loss"]
        total  = len(trades)
        win_rate = (len(wins) / total * 100) if total else 0.0
        avg_win  = sum(t.pnl for t in wins)  / max(len(wins),  1)
        avg_loss = sum(t.pnl for t in losses)/ max(len(losses), 1)
        end_eq   = start_equity + closed_pnl
        ret_pct  = ((end_eq - start_equity) / start_equity * 100) if start_equity else 0.0

        trend_vol  = sum(t.notional for t in trades if t.strategy == "trend")
        scalp_vol  = sum(t.notional for t in trades if t.strategy == "scalp")
        trend_cnt  = len([t for t in trades if t.strategy == "trend"])
        scalp_cnt  = len([t for t in trades if t.strategy == "scalp"])

        self.monthly_snapshots.append({
            "Month":          month,
            "Start $":        f"${start_equity:,.2f}",
            "End $":          f"${end_eq:,.2f}",
            "Net P&L":        f"${closed_pnl:+,.2f}",
            "Return %":       f"{ret_pct:+.1f}%",
            "Total Trades":   total,
            "Trend Trades":   trend_cnt,
            "Scalp Trades":   scalp_cnt,
            "Win Rate":       f"{win_rate:.1f}%",
            "Avg Win $":      f"${avg_win:,.2f}",
            "Avg Loss $":     f"${avg_loss:,.2f}",
            "Volume (Notional)": f"${volume:,.0f}",
            "Trend Volume":   f"${trend_vol:,.0f}",
            "Scalp Volume":   f"${scalp_vol:,.0f}",
        })

    def print_report(self):
        print("\n" + "=" * 90)
        print(f"  PANTHER BOT BACKTEST — {self.period_months}-MONTH | Bybit BTCUSDT Perp | $500/month reset")
        print("=" * 90)

        if not self.monthly_snapshots:
            print("No data to show.")
            return

        print("\n📅 MONTHLY BREAKDOWN\n")
        print(tabulate(self.monthly_snapshots, headers="keys", tablefmt="rounded_outline",
                       numalign="right", stralign="right"))

        # ── aggregate stats ───────────────────────────────────────────
        closed = [t for t in self.trades if t.outcome in ("win", "loss")]
        wins   = [t for t in closed if t.outcome == "win"]
        losses = [t for t in closed if t.outcome == "loss"]
        total  = len(closed)
        win_rate   = len(wins) / total * 100 if total else 0
        total_pnl  = sum(t.pnl for t in closed)
        total_vol  = sum(t.notional for t in closed)
        avg_win    = sum(t.pnl for t in wins)  / max(len(wins),  1)
        avg_loss   = sum(t.pnl for t in losses)/ max(len(losses), 1)
        profit_factor = (sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses))
                         if losses else float("inf"))

        trend_closed = [t for t in closed if t.strategy == "trend"]
        scalp_closed = [t for t in closed if t.strategy == "scalp"]

        # Max drawdown (equity curve)
        running = STARTING_EQUITY
        peak = STARTING_EQUITY
        max_dd = 0.0
        for t in closed:
            running += t.pnl
            if running > peak:
                peak = running
            dd = (peak - running) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # Session breakdown
        session_stats: Dict[str, Dict] = {}
        for t in closed:
            s = t.session
            if s not in session_stats:
                session_stats[s] = {"trades": 0, "wins": 0, "pnl": 0.0, "volume": 0.0}
            session_stats[s]["trades"] += 1
            session_stats[s]["wins"]   += int(t.outcome == "win")
            session_stats[s]["pnl"]    += t.pnl
            session_stats[s]["volume"] += t.notional

        print("\n\n📊 OVERALL SUMMARY\n")
        summary = [
            ["Total closed trades",   total],
            ["  ↳ Trend",             len(trend_closed)],
            ["  ↳ Scalp",             len(scalp_closed)],
            ["Win rate",              f"{win_rate:.1f}%"],
            ["Total P&L",             f"${total_pnl:+,.2f}"],
            ["Avg win",               f"${avg_win:,.2f}"],
            ["Avg loss",              f"${avg_loss:,.2f}"],
            ["Profit factor",         f"{profit_factor:.2f}"],
            ["Max drawdown",          f"{max_dd:.1f}%"],
            ["Total volume generated",f"${total_vol:,.0f}"],
        ]
        print(tabulate(summary, tablefmt="rounded_outline"))

        print("\n\n🕐 SESSION BREAKDOWN\n")
        session_rows = []
        for s in ["ASIA", "LONDON", "NY"]:
            d = session_stats.get(s, {"trades": 0, "wins": 0, "pnl": 0.0, "volume": 0.0})
            wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            session_rows.append([
                s,
                d["trades"],
                f"{wr:.1f}%",
                f"${d['pnl']:+,.2f}",
                f"${d['volume']:,.0f}",
            ])
        print(tabulate(session_rows,
                       headers=["Session", "Trades", "Win Rate", "Net P&L", "Volume"],
                       tablefmt="rounded_outline"))

        print("\n\n📋 ALL TRADES (last 30)\n")
        trade_rows = []
        for t in self.trades[-30:]:
            trade_rows.append([
                t.month_label,
                t.strategy,
                t.side,
                t.session,
                f"${t.entry:,.2f}",
                f"${t.exit_price:,.2f}" if t.exit_price else "—",
                f"${t.pnl:+,.2f}",
                f"${t.notional:,.0f}",
                t.outcome,
            ])
        print(tabulate(
            trade_rows,
            headers=["Month", "Strategy", "Side", "Session", "Entry", "Exit",
                     "P&L", "Notional", "Outcome"],
            tablefmt="rounded_outline",
        ))
        print()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Panther Bot Bybit Backtest")
    parser.add_argument("--period", type=int, choices=[3, 6], default=3,
                        help="Backtest period in months (3 or 6)")
    parser.add_argument("--symbol", type=str, default=SYMBOL,
                        help="Bybit symbol (default: BTC/USDT:USDT)")
    args = parser.parse_args()

    print(f"\n🔄 Connecting to Bybit and fetching {args.period} months of OHLCV data …")
    exchange = ccxt.bybit({"options": {"defaultType": "linear"}, "enableRateLimit": True})

    until_dt  = datetime.now(timezone.utc)
    since_dt  = until_dt - timedelta(days=args.period * 31)
    since_ms  = int(since_dt.timestamp() * 1000)
    until_ms  = int(until_dt.timestamp() * 1000)

    print(f"   From : {since_dt.strftime('%Y-%m-%d')}")
    print(f"   To   : {until_dt.strftime('%Y-%m-%d')}")
    print(f"   Symbol: {args.symbol}")

    print("   Fetching 1h candles …", end=" ", flush=True)
    df_1h = fetch_ohlcv(exchange, args.symbol, "1h", since_ms, until_ms)
    print(f"{len(df_1h)} bars")

    print("   Fetching 5m candles …", end=" ", flush=True)
    df_5m = fetch_ohlcv(exchange, args.symbol, "5m", since_ms, until_ms)
    print(f"{len(df_5m)} bars")

    print("   Fetching 1m candles …", end=" ", flush=True)
    df_1m = fetch_ohlcv(exchange, args.symbol, "1m", since_ms, until_ms)
    print(f"{len(df_1m)} bars")

    print("\n⚙️  Running backtest …\n")
    engine = BacktestEngine(args.period)
    engine.run(df_1h, df_5m, df_1m)
    engine.print_report()


if __name__ == "__main__":
    main()
