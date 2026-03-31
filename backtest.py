"""
Panther Trading Bot - Basic Backtester (pybit / requests)
Usage: python backtest.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from backtest.data import fetch_klines_pybit
from backtest.engine import (
    BacktestConfig, MonthResult, Trade,
    check_sl_tp, close_trade, compute_size,
    mean_reversion_signal, trend_breakout_signal, candle3_signal,
)
from backtest.report import print_text_report, save_json_report

CONFIG = BacktestConfig()


def run_month(start_dt: datetime, end_dt: datetime, label: str) -> MonthResult:
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    print(f"\n{'=' * 60}")
    print(f"  Backtesting: {label} | {start_dt:%Y-%m-%d} to {end_dt:%Y-%m-%d}")
    print(f"{'=' * 60}")

    print("  Fetching 1H candles...", end=" ", flush=True)
    candles_1h = fetch_klines_pybit(CONFIG.symbol, "1h", start_ms, end_ms)
    print(f"{len(candles_1h)}")

    print("  Fetching 5m candles...", end=" ", flush=True)
    candles_5m = fetch_klines_pybit(CONFIG.symbol, "5m", start_ms, end_ms)
    print(f"{len(candles_5m)}")

    print("  Fetching 3m candles...", end=" ", flush=True)
    candles_3m = fetch_klines_pybit(CONFIG.symbol, "3m", start_ms, end_ms)
    print(f"{len(candles_3m)}")

    equity = CONFIG.starting_equity
    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    volume = 0.0

    weekly: List[Dict] = []
    week_start = start_dt
    wk_trades = wk_wins = 0
    wk_pnl = wk_vol = 0.0

    def _flush_week(end: datetime):
        nonlocal wk_trades, wk_wins, wk_pnl, wk_vol, week_start
        if wk_trades > 0:
            weekly.append({
                "week_start": week_start.strftime("%Y-%m-%d"),
                "week_end": end.strftime("%Y-%m-%d"),
                "trades": wk_trades, "wins": wk_wins,
                "win_rate": round((wk_wins / wk_trades * 100) if wk_trades else 0.0, 1),
                "pnl": round(wk_pnl, 2), "equity": round(equity, 2),
                "volume": round(wk_vol, 2),
            })
        wk_trades = wk_wins = 0
        wk_pnl = wk_vol = 0.0
        week_start = end

    for i, candle in enumerate(candles_5m):
        candle_dt = datetime.fromtimestamp(candle["timestamp"] / 1000.0, tz=timezone.utc)

        if (candle_dt - week_start).days >= 7:
            _flush_week(candle_dt)

        if open_trade:
            exit_px = check_sl_tp(open_trade, candle)
            if exit_px is not None:
                close_trade(open_trade, exit_px, candle_dt, CONFIG.taker_fee)
                equity += open_trade.pnl
                vol_add = open_trade.notional * 2
                volume += vol_add
                wk_vol += vol_add
                wk_trades += 1
                wk_pnl += open_trade.pnl
                if open_trade.pnl > 0:
                    wk_wins += 1
                trades.append(open_trade)
                open_trade = None
                if equity <= 0:
                    break

            # Strategy C: close after next 3m candle
            if open_trade and open_trade.strategy == "candle3":
                entry_ts = int(open_trade.entry_time.timestamp() * 1000)
                bars_since = sum(1 for c in candles_3m if c["timestamp"] >= entry_ts and c["timestamp"] <= candle["timestamp"])
                if bars_since >= 2:
                    close_trade(open_trade, candle["close"], candle_dt, CONFIG.taker_fee)
                    equity += open_trade.pnl
                    vol_add = open_trade.notional * 2
                    volume += vol_add
                    wk_vol += vol_add
                    wk_trades += 1
                    wk_pnl += open_trade.pnl
                    if open_trade.pnl > 0:
                        wk_wins += 1
                    trades.append(open_trade)
                    open_trade = None
            continue

        c1h_win = [c for c in candles_1h if c["timestamp"] <= candle["timestamp"]][-250:]
        c5m_win = candles_5m[max(0, i - 200):i + 1]
        c3m_win = [c for c in candles_3m if c["timestamp"] <= candle["timestamp"]][-200:]

        signal = (
            trend_breakout_signal(c1h_win)
            or mean_reversion_signal(c5m_win, c1h_win)
            or candle3_signal(c3m_win)
        )
        if signal:
            k = {"scalp": 2.0, "trend": 2.0, "candle3": 1.0}.get(signal["strategy"], 2.0)
            size = compute_size(CONFIG.risk_per_trade, equity, signal["atr"], k, signal["price"], CONFIG.min_notional)
            if size > 0:
                open_trade = Trade(
                    strategy=signal["strategy"], side=signal["side"],
                    entry_price=signal["price"], stop_loss=signal["sl"],
                    take_profit=signal["tp"], size=size, entry_time=candle_dt,
                )

    _flush_week(end_dt)

    total = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    total_pnl = sum(t.pnl for t in trades)
    wr = (wins / total * 100) if total else 0.0

    result = MonthResult(
        month_label=label, starting_equity=CONFIG.starting_equity,
        ending_equity=round(equity, 2), total_trades=total,
        wins=wins, losses=total - wins, win_rate=round(wr, 1),
        total_pnl=round(total_pnl, 2), volume_generated=round(volume, 2),
        weekly_results=weekly,
    )
    print(f"  Trades: {total} | WR: {wr:.1f}% | P&L: ${total_pnl:,.2f} | Equity: ${equity:,.2f}")
    return result


def main():
    months_3 = [
        (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 31, 23, 59, tzinfo=timezone.utc), "Jan 2026"),
        (datetime(2026, 2, 1, tzinfo=timezone.utc), datetime(2026, 2, 28, 23, 59, tzinfo=timezone.utc), "Feb 2026"),
        (datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 28, 23, 59, tzinfo=timezone.utc), "Mar 2026"),
    ]
    months_6 = [
        (datetime(2025, 10, 1, tzinfo=timezone.utc), datetime(2025, 10, 31, 23, 59, tzinfo=timezone.utc), "Oct 2025"),
        (datetime(2025, 11, 1, tzinfo=timezone.utc), datetime(2025, 11, 30, 23, 59, tzinfo=timezone.utc), "Nov 2025"),
        (datetime(2025, 12, 1, tzinfo=timezone.utc), datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc), "Dec 2025"),
    ] + months_3

    results_3m = [run_month(s, e, l) for s, e, l in months_3]
    print_text_report("3-MONTH BACKTEST (Jan-Mar 2026) | $500 restart each month", results_3m)

    results_6m = [run_month(s, e, l) for s, e, l in months_6]
    print_text_report("6-MONTH BACKTEST (Oct 2025 - Mar 2026) | $500 restart each month", results_6m)

    save_json_report("backtest_results.json", {
        "3_month_backtest": results_3m, "6_month_backtest": results_6m,
    })


if __name__ == "__main__":
    main()
