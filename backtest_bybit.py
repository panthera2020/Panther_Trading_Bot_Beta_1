"""
Panther Trading Bot - Bybit Backtest Engine (ccxt + pandas + tabulate)

Usage:
    pip install ccxt pandas tabulate
    python backtest_bybit.py --period 3
    python backtest_bybit.py --period 6
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

try:
    import ccxt
    import pandas as pd
    from tabulate import tabulate
except ImportError:
    print("Installing required packages...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "ccxt", "pandas", "tabulate"])
    import ccxt
    import pandas as pd
    from tabulate import tabulate

from backtest.data import fetch_klines_ccxt
from backtest.engine import (
    BacktestConfig, Trade, check_sl_tp, close_trade, compute_size,
    mean_reversion_signal, trend_breakout_signal,
)

CONFIG = BacktestConfig(taker_fee=0.00055, slippage=0.0002)
CCXT_SYMBOL = "BTC/USDT:USDT"


class BybitBacktest:
    def __init__(self, period: int):
        self.period = period
        self.trades: List[Trade] = []
        self.monthly: List[Dict] = []

    def run(self, candles_1h: List[Dict], candles_5m: List[Dict]):
        equity = CONFIG.starting_equity
        month_start_eq = equity
        current_month: Optional[str] = None
        open_trade: Optional[Trade] = None
        month_trades: List[Trade] = []
        month_pnl = 0.0
        month_vol = 0.0

        for i, bar in enumerate(candles_1h):
            ts_ms = bar["timestamp"]
            ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            month_key = ts.strftime("%Y-%m")

            # Monthly reset
            if current_month is None:
                current_month = month_key
            if month_key != current_month:
                if open_trade:
                    close_trade(open_trade, candles_1h[i - 1]["close"], ts, CONFIG.taker_fee)
                    month_pnl += open_trade.pnl
                    month_vol += open_trade.notional
                    month_trades.append(open_trade)
                    self.trades.append(open_trade)
                    open_trade = None
                self._record_month(current_month, month_start_eq, month_trades, month_pnl, month_vol)
                equity = CONFIG.starting_equity
                month_start_eq = equity
                month_trades = []
                month_pnl = 0.0
                month_vol = 0.0
                current_month = month_key

            # Check existing position
            if open_trade:
                exit_px = check_sl_tp(open_trade, bar)
                if exit_px is not None:
                    close_trade(open_trade, exit_px, ts, CONFIG.taker_fee)
                    equity += open_trade.pnl
                    month_pnl += open_trade.pnl
                    month_vol += open_trade.notional
                    month_trades.append(open_trade)
                    self.trades.append(open_trade)
                    open_trade = None
                continue

            # Signals
            c1h_win = candles_1h[max(0, i - 300):i + 1]
            c5m_win = [c for c in candles_5m if c["timestamp"] <= ts_ms][-300:]

            signal = trend_breakout_signal(c1h_win) or mean_reversion_signal(c5m_win, c1h_win)
            if signal:
                k = 2.0
                size = compute_size(CONFIG.risk_per_trade, equity, signal["atr"], k, signal["price"], CONFIG.min_notional)
                if size > 0:
                    open_trade = Trade(
                        strategy=signal["strategy"], side=signal["side"],
                        entry_price=signal["price"], stop_loss=signal["sl"],
                        take_profit=signal["tp"], size=size, entry_time=ts,
                    )

        # Flush last month
        if current_month:
            if open_trade:
                close_trade(open_trade, candles_1h[-1]["close"],
                            datetime.fromtimestamp(candles_1h[-1]["timestamp"] / 1000.0, tz=timezone.utc),
                            CONFIG.taker_fee)
                month_pnl += open_trade.pnl
                month_vol += open_trade.notional
                month_trades.append(open_trade)
                self.trades.append(open_trade)
            self._record_month(current_month, month_start_eq, month_trades, month_pnl, month_vol)

    def _record_month(self, month: str, start_eq: float, trades: List[Trade], pnl: float, volume: float):
        wins = [t for t in trades if t.pnl > 0]
        total = len(trades)
        wr = (len(wins) / total * 100) if total else 0.0
        self.monthly.append({
            "Month": month, "Start $": f"${start_eq:,.2f}",
            "End $": f"${start_eq + pnl:,.2f}", "Net P&L": f"${pnl:+,.2f}",
            "Trades": total, "Win Rate": f"{wr:.1f}%",
            "Volume": f"${volume:,.0f}",
        })

    def print_report(self):
        print(f"\n{'=' * 80}")
        print(f"  PANTHER BOT BACKTEST - {self.period}-MONTH | Bybit BTCUSDT Perp | $500/month reset")
        print(f"{'=' * 80}")

        if self.monthly:
            print("\nMONTHLY BREAKDOWN\n")
            print(tabulate(self.monthly, headers="keys", tablefmt="rounded_outline"))

        closed = [t for t in self.trades if t.closed]
        wins = [t for t in closed if t.pnl > 0]
        total = len(closed)
        wr = (len(wins) / total * 100) if total else 0.0
        total_pnl = sum(t.pnl for t in closed)
        total_vol = sum(t.notional for t in closed)

        summary = [
            ["Total trades", total],
            ["Win rate", f"{wr:.1f}%"],
            ["Total P&L", f"${total_pnl:+,.2f}"],
            ["Total volume", f"${total_vol:,.0f}"],
        ]
        print("\nOVERALL SUMMARY\n")
        print(tabulate(summary, tablefmt="rounded_outline"))

        if closed:
            print(f"\nLAST {min(20, len(closed))} TRADES\n")
            rows = []
            for t in closed[-20:]:
                rows.append([
                    t.strategy, t.side,
                    f"${t.entry_price:,.2f}", f"${t.exit_price:,.2f}",
                    f"${t.pnl:+,.2f}", t.outcome,
                ])
            print(tabulate(rows, headers=["Strategy", "Side", "Entry", "Exit", "P&L", "Outcome"],
                           tablefmt="rounded_outline"))
        print()


def main():
    parser = argparse.ArgumentParser(description="Panther Bot Bybit Backtest")
    parser.add_argument("--period", type=int, choices=[3, 6], default=3)
    args = parser.parse_args()

    print(f"\nConnecting to Bybit and fetching {args.period} months of data...")
    exchange = ccxt.bybit({"options": {"defaultType": "linear"}, "enableRateLimit": True})

    until_dt = datetime.now(timezone.utc)
    since_dt = until_dt - timedelta(days=args.period * 31)
    since_ms = int(since_dt.timestamp() * 1000)
    until_ms = int(until_dt.timestamp() * 1000)

    print(f"  From: {since_dt:%Y-%m-%d}  To: {until_dt:%Y-%m-%d}")

    print("  Fetching 1h candles...", end=" ", flush=True)
    candles_1h = fetch_klines_ccxt(exchange, CCXT_SYMBOL, "1h", since_ms, until_ms)
    print(f"{len(candles_1h)}")

    print("  Fetching 5m candles...", end=" ", flush=True)
    candles_5m = fetch_klines_ccxt(exchange, CCXT_SYMBOL, "5m", since_ms, until_ms)
    print(f"{len(candles_5m)}")

    bt = BybitBacktest(args.period)
    bt.run(candles_1h, candles_5m)
    bt.print_report()


if __name__ == "__main__":
    main()
