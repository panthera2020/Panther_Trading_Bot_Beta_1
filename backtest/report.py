"""Shared reporting utilities for backtest results."""
from __future__ import annotations

import json
from typing import Dict, List

from backtest.engine import MonthResult


def print_text_report(title: str, results: List[MonthResult]) -> None:
    print(f"\n{'#' * 70}")
    print(f"  {title}")
    print(f"{'#' * 70}")

    total_pnl = sum(r.total_pnl for r in results)
    total_trades = sum(r.total_trades for r in results)
    total_wins = sum(r.wins for r in results)
    total_volume = sum(r.volume_generated for r in results)
    overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0.0

    print(f"\n  OVERALL SUMMARY")
    print(f"  {'=' * 50}")
    print(f"  Total Months: {len(results)}")
    print(f"  Total Trades: {total_trades}")
    print(f"  Overall Win Rate: {overall_wr:.1f}%")
    print(f"  Total P&L: ${total_pnl:,.2f}")
    print(f"  Total Volume: ${total_volume:,.2f}")
    print(f"  Avg Monthly Volume: ${total_volume / max(len(results), 1):,.2f}")

    print(f"\n  MONTHLY BREAKDOWN")
    print(f"  {'=' * 50}")
    print(f"  {'Month':<15} {'Trades':>7} {'WR%':>6} {'P&L':>10} {'End Eq':>10} {'Volume':>12}")
    print(f"  {'-' * 60}")
    for r in results:
        print(f"  {r.month_label:<15} {r.total_trades:>7} {r.win_rate:>5.1f}% ${r.total_pnl:>9,.2f} ${r.ending_equity:>9,.2f} ${r.volume_generated:>11,.2f}")

    print(f"\n  WEEKLY BREAKDOWN")
    print(f"  {'=' * 50}")
    for r in results:
        print(f"\n  --- {r.month_label} ---")
        print(f"  {'Week':<25} {'Trades':>7} {'WR%':>6} {'P&L':>10} {'Equity':>10} {'Volume':>12}")
        print(f"  {'-' * 70}")
        for w in r.weekly_results:
            print(
                f"  {w['week_start']} - {w['week_end'][-5:]}  {w['trades']:>7} "
                f"{w['win_rate']:>5.1f}% ${w['pnl']:>9,.2f} ${w['equity']:>9,.2f} "
                f"${w['volume']:>11,.2f}"
            )


def save_json_report(filename: str, sections: Dict[str, List[MonthResult]]) -> None:
    report = {}
    for key, results in sections.items():
        report[key] = {
            "months": [
                {
                    "month": r.month_label,
                    "starting_equity": r.starting_equity,
                    "ending_equity": r.ending_equity,
                    "trades": r.total_trades,
                    "wins": r.wins,
                    "losses": r.losses,
                    "win_rate": r.win_rate,
                    "pnl": r.total_pnl,
                    "volume": r.volume_generated,
                    "weekly": r.weekly_results,
                }
                for r in results
            ]
        }
    with open(filename, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nResults saved to {filename}")
