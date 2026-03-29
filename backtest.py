"""
Panther Trading Bot — Backtest Engine
======================================
Fetches real OHLCV data from Binance public API (data-api.binance.vision, no key required).
BTCUSDT perpetual spot — same price reference as Bybit linear (correlation >0.999).

Usage:
    python backtest.py

Backtests:
  - 3-Month:  Jan 2026 → Mar 2026
  - 6-Month:  Oct 2025 → Mar 2026

Rules:
  - Strategies: trend (TrendBreakout) + scalp (MeanReversion)
  - Candle3 (StrategyC) is DISABLED
  - Monthly equity reset: $500 at the start of each month
  - Volume tracked per month (notional = size × entry_price)
  - Leverage: 50x | Risk per trade: 1% | Sizing: ATR-based

Bug fixes that were applied before running this backtest:
  [BUG-1] strategy_c.py: `volumes` NameError — variable used before definition.
           Fix: Extract volumes = [c.get("volume", 0.0) for c in last_three] first.
  [BUG-2] strategy_c.py: `continue` outside loop — SyntaxError at runtime.
           Fix: Replace with `return None`.
  [BUG-3] strategy_c.py: `consecutive_required` assigned but never used — dead code.
           Fix: Removed.
  [BUG-4] main.py start(): hardcoded enabled_strategies always includes "candle3".
           Backtest explicitly passes strategies=["trend","scalp"] to exclude it.
  [BUG-5] main.py _get_live_equity: balance_cache check order is safe (already guarded).
           No code change needed — documented here for completeness.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests

from strategies.indicators import atr, ema
from strategies.trend_breakout import TrendBreakoutStrategy, TrendBreakoutConfig
from strategies.mean_reversion import MeanReversionStrategy, MeanReversionConfig
from models.signal import Side, TradeSignal

# ═══════════════════════════════════════════════════════════════════════════════
# BINANCE PUBLIC DATA FETCHER  (data-api.binance.vision — no API key needed)
# ═══════════════════════════════════════════════════════════════════════════════

BINANCE_BASE = "https://data-api.binance.vision"

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000,
    "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
}


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[Dict]:
    """Paginated fetch from Binance Vision public OHLCV endpoint."""
    url = f"{BINANCE_BASE}/api/v3/klines"
    all_candles: Dict[int, Dict] = {}
    cursor = start_ms
    step = INTERVAL_MS.get(interval, 60_000) * 500

    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": min(cursor + step - 1, end_ms - 1),
            "limit": 500,
        }
        rows = []
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=20)
                rows = resp.json()
                break
            except Exception as exc:
                if attempt == 2:
                    print(f"  [WARN] fetch failed after 3 retries: {exc}")
                time.sleep(1)

        if not rows or isinstance(rows, dict):
            break

        for row in rows:
            ts = int(row[0])
            if start_ms <= ts < end_ms:
                all_candles[ts] = {
                    "timestamp": ts,
                    "open":   float(row[1]),
                    "high":   float(row[2]),
                    "low":    float(row[3]),
                    "close":  float(row[4]),
                    "volume": float(row[5]),
                }

        last_ts = int(rows[-1][0])
        next_start = last_ts + INTERVAL_MS.get(interval, 60_000)
        if next_start <= cursor:
            break
        cursor = next_start
        time.sleep(0.08)

    return sorted(all_candles.values(), key=lambda x: x["timestamp"])


def dt_to_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def get_month_range(year: int, month: int) -> Tuple[int, int]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end   = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12 \
            else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return dt_to_ms(start), dt_to_ms(end)


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE RECORD
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    strategy_id: str
    symbol: str
    side: str
    entry_time: datetime
    entry_price: float
    size: float
    stop_loss: float
    take_profit: Optional[float]
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: float = 0.0
    exit_reason: str = ""
    notional: float = 0.0
    _open_bar_idx: int = field(default=0, repr=False)


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION SIZER
# ═══════════════════════════════════════════════════════════════════════════════

RISK_PCT     = 0.01
MIN_NOTIONAL = 5.0
LEVERAGE     = 50


def compute_size(equity: float, atr_val: float, k: float, price: float) -> float:
    if atr_val <= 0 or k <= 0 or price <= 0 or equity <= 0:
        return 0.0
    size = (equity * RISK_PCT) / (atr_val * k)
    if size * price < MIN_NOTIONAL:
        return 0.0
    return math.floor(size * 1000) / 1000


# ═══════════════════════════════════════════════════════════════════════════════
# MONTHLY RESULTS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class MonthResult:
    month_label: str
    start_equity: float
    end_equity: float = 0.0
    trades: List[Trade] = field(default_factory=list)
    volume_usd: float = 0.0

    @property
    def net_pnl(self) -> float:        return self.end_equity - self.start_equity
    @property
    def pnl_pct(self) -> float:        return (self.net_pnl / self.start_equity * 100) if self.start_equity else 0.0
    @property
    def total_trades(self) -> int:     return len(self.trades)
    @property
    def wins(self) -> int:             return sum(1 for t in self.trades if t.pnl > 0)
    @property
    def losses(self) -> int:           return sum(1 for t in self.trades if t.pnl <= 0)
    @property
    def win_rate(self) -> float:       return (self.wins / len(self.trades) * 100) if self.trades else 0.0
    @property
    def avg_win(self) -> float:
        w = [t.pnl for t in self.trades if t.pnl > 0]; return sum(w)/len(w) if w else 0.0
    @property
    def avg_loss(self) -> float:
        l = [t.pnl for t in self.trades if t.pnl <= 0]; return sum(l)/len(l) if l else 0.0
    @property
    def max_drawdown_pct(self) -> float:
        eq = self.start_equity; peak = eq; mdd = 0.0
        for t in self.trades:
            eq += t.pnl; peak = max(peak, eq)
            dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
            mdd = max(mdd, dd)
        return round(mdd, 2)

    def by_strategy(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for t in self.trades:
            s = t.strategy_id
            out.setdefault(s, {"trades": 0, "wins": 0, "pnl": 0.0, "volume": 0.0})
            out[s]["trades"] += 1; out[s]["pnl"] += t.pnl; out[s]["volume"] += t.notional
            if t.pnl > 0: out[s]["wins"] += 1
        for s in out:
            n = out[s]["trades"]
            out[s]["win_rate"] = round(out[s]["wins"] / n * 100, 1) if n else 0.0
            out[s]["pnl"] = round(out[s]["pnl"], 4)
            out[s]["volume"] = round(out[s]["volume"], 2)
        return out


# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    def __init__(self, symbol: str = "BTCUSDT"):
        self.symbol = symbol
        self.trend_strat = TrendBreakoutStrategy(TrendBreakoutConfig())
        self.scalp_strat = MeanReversionStrategy(MeanReversionConfig())

    @staticmethod
    def _session(ts: datetime) -> str:
        h = ts.hour
        if 2 <= h < 8:  return "ASIA"
        if 8 <= h < 16: return "LONDON"
        if 13 <= h < 21: return "NY"
        return "OFF"

    def run_month(self, month_label: str, candles_1h: List[Dict],
                  candles_5m: List[Dict], candles_15m: List[Dict],
                  start_equity: float = 500.0) -> MonthResult:
        equity = start_equity
        result = MonthResult(month_label=month_label, start_equity=start_equity)
        open_trade: Optional[Trade] = None

        idx_1h = {c["timestamp"]: i for i, c in enumerate(candles_1h)}
        idx_5m = {c["timestamp"]: i for i, c in enumerate(candles_5m)}

        def window(candles, idx_map, ts_ms, lb=300):
            i = idx_map.get(ts_ms)
            return [] if i is None else candles[max(0, i - lb): i + 1]

        for bar_idx, bar in enumerate(candles_1h):
            ts_ms = bar["timestamp"]
            ts    = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            sess  = self._session(ts)
            px    = bar["close"]

            if open_trade is not None:
                sl, tp = open_trade.stop_loss, open_trade.take_profit
                hit_sl = hit_tp = False
                exit_p = px
                if open_trade.side == "BUY":
                    if bar["low"] <= sl:              hit_sl, exit_p = True, sl
                    elif tp and bar["high"] >= tp:    hit_tp, exit_p = True, tp
                else:
                    if bar["high"] >= sl:             hit_sl, exit_p = True, sl
                    elif tp and bar["low"] <= tp:     hit_tp, exit_p = True, tp

                bars_held  = bar_idx - open_trade._open_bar_idx
                force_exit = open_trade.strategy_id == "scalp" and bars_held >= 12

                if hit_sl or hit_tp or force_exit:
                    raw = (exit_p - open_trade.entry_price) * open_trade.size * LEVERAGE \
                          if open_trade.side == "BUY" else \
                          (open_trade.entry_price - exit_p) * open_trade.size * LEVERAGE
                    open_trade.exit_time   = ts
                    open_trade.exit_price  = exit_p
                    open_trade.pnl         = round(raw, 4)
                    open_trade.exit_reason = "TP" if hit_tp else ("SL" if hit_sl else "MAX_HOLD")
                    equity = max(equity + raw, 0.0)
                    result.trades.append(open_trade)
                    open_trade = None
                    continue

            if equity < start_equity * 0.05 or open_trade is not None:
                continue

            w1h = window(candles_1h, idx_1h, ts_ms, 300)
            w5m = window(candles_5m, idx_5m, ts_ms, 300)
            signal: Optional[TradeSignal] = None

            if sess in ("LONDON", "NY") and len(w1h) >= 202:
                av = atr([c["high"] for c in w1h], [c["low"] for c in w1h],
                         [c["close"] for c in w1h], 14)
                if av:
                    sz = compute_size(equity, av, 2.0, px)
                    if sz > 0:
                        signal = self.trend_strat.generate_signal(w1h, sz, self.symbol, ts)

            if signal is None and sess in ("LONDON", "NY") and len(w5m) >= 22:
                av = atr([c["high"] for c in w5m], [c["low"] for c in w5m],
                         [c["close"] for c in w5m], 14)
                if av:
                    sz = compute_size(equity, av, 1.5, px)
                    if sz > 0:
                        signal = self.scalp_strat.generate_signal(w5m, sz, self.symbol, ts)

            if signal is not None:
                notional = signal.size * signal.price
                t = Trade(strategy_id=signal.strategy_id, symbol=self.symbol,
                          side="BUY" if signal.side == Side.BUY else "SELL",
                          entry_time=ts, entry_price=signal.price, size=signal.size,
                          stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                          notional=round(notional, 2))
                t._open_bar_idx = bar_idx
                result.volume_usd += notional
                open_trade = t

        if open_trade is not None and candles_1h:
            last   = candles_1h[-1]
            exit_p = last["close"]
            raw    = (exit_p - open_trade.entry_price) * open_trade.size * LEVERAGE \
                     if open_trade.side == "BUY" else \
                     (open_trade.entry_price - exit_p) * open_trade.size * LEVERAGE
            open_trade.exit_time   = datetime.fromtimestamp(last["timestamp"] / 1000.0, tz=timezone.utc)
            open_trade.exit_price  = exit_p
            open_trade.pnl         = round(raw, 4)
            open_trade.exit_reason = "EOD_CLOSE"
            equity = max(equity + raw, 0.0)
            result.trades.append(open_trade)

        result.end_equity  = round(equity, 2)
        result.volume_usd  = round(result.volume_usd, 2)
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════════════════════

def print_report(results: List[MonthResult], label: str) -> None:
    print(f"\n\n{'#'*70}")
    print(f"  BACKTEST RESULTS — {label}")
    print(f"{'#'*70}")
    total_pnl  = sum(r.net_pnl for r in results)
    total_tr   = sum(r.total_trades for r in results)
    total_wins = sum(r.wins for r in results)
    total_vol  = sum(r.volume_usd for r in results)
    wr_all     = (total_wins / total_tr * 100) if total_tr else 0.0

    for r in results:
        print(f"\n  {'─'*58}")
        print(f"  📅  {r.month_label}")
        print(f"  {'─'*58}")
        print(f"  Starting Equity     : ${r.start_equity:>10,.2f}")
        print(f"  Ending Equity       : ${r.end_equity:>10,.2f}")
        print(f"  Net PnL             : ${r.net_pnl:>+10.2f}  ({r.pnl_pct:>+.1f}%)")
        print(f"  Total Trades        :   {r.total_trades}")
        print(f"  Wins / Losses       :   {r.wins} / {r.losses}")
        print(f"  Win Rate            :   {r.win_rate:.1f}%")
        print(f"  Avg Win             : ${r.avg_win:>+.4f}")
        print(f"  Avg Loss            : ${r.avg_loss:>+.4f}")
        print(f"  Max Drawdown        :   {r.max_drawdown_pct:.1f}%")
        print(f"  Volume Generated    : ${r.volume_usd:>12,.2f}")
        print(f"\n  Strategy Breakdown:")
        by_s = r.by_strategy()
        if not by_s:
            print("    (no trades this month)")
        for strat, st in by_s.items():
            print(f"    [{strat.upper():10s}]  trades={st['trades']:3d}  wins={st['wins']:3d}  "
                  f"WR={st['win_rate']:5.1f}%  PnL=${st['pnl']:>+9.4f}  "
                  f"Vol=${st['volume']:>12,.2f}")
        if r.trades:
            print(f"\n  Trade Log ({r.total_trades} trades):")
            for t in r.trades:
                d = "↑BUY " if t.side == "BUY" else "↓SELL"
                print(f"    {t.entry_time.strftime('%m/%d %H:%M')}  "
                      f"[{t.strategy_id:6s}] {d} @{t.entry_price:>9,.2f} → "
                      f"exit @{t.exit_price:>9,.2f}  PnL ${t.pnl:>+9.4f}  {t.exit_reason}")

    print(f"\n  {'='*58}")
    print(f"  OVERALL SUMMARY — {label}")
    print(f"  {'='*58}")
    print(f"  Months Tested       :  {len(results)}")
    print(f"  Total Trades        :  {total_tr}")
    print(f"  Overall Win Rate    :  {wr_all:.1f}%")
    print(f"  Total Net PnL       : ${total_pnl:>+.2f}")
    print(f"  Total Volume        : ${total_vol:>12,.2f}")
    if results:
        print(f"  Avg Monthly PnL     : ${total_pnl/len(results):>+.2f}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    SYMBOL = "BTCUSDT"
    THREE_MONTH = [(2026, 1), (2026, 2), (2026, 3)]
    SIX_MONTH   = [(2025, 10), (2025, 11), (2025, 12),
                   (2026, 1),  (2026, 2),  (2026, 3)]

    def run_backtest(periods, label) -> List[MonthResult]:
        engine, results = BacktestEngine(SYMBOL), []
        print(f"\n{'='*60}\n  Fetching data — {label}\n{'='*60}")
        for year, month in periods:
            ms = datetime(year, month, 1).strftime("%b %Y")
            print(f"\n  ⬇  {ms} ...")
            s, e = get_month_range(year, month)
            c1h  = fetch_klines(SYMBOL, "1h",  s, e)
            c5m  = fetch_klines(SYMBOL, "5m",  s, e)
            c15m = fetch_klines(SYMBOL, "15m", s, e)
            print(f"     1H={len(c1h)}, 15m={len(c15m)}, 5m={len(c5m)}")
            if len(c1h) < 10:
                print(f"  [WARN] Insufficient data for {ms}, skipping.")
                continue
            r = engine.run_month(ms, c1h, c5m, c15m, start_equity=500.0)
            results.append(r)
            print(f"  ✓  {ms}: {r.total_trades} trades | WR {r.win_rate:.1f}% | "
                  f"PnL ${r.net_pnl:+.2f} ({r.pnl_pct:+.1f}%) | "
                  f"Vol ${r.volume_usd:,.0f}")
        return results

    print("\n" + "="*60)
    print("  Panther Trading Bot — Backtest Runner")
    print("  Strategies : trend + scalp  (Candle3 DISABLED)")
    print("  Equity     : $500 reset each month | Leverage 50x | Risk 1%")
    print("  Data       : Binance public OHLCV (BTCUSDT)")
    print("="*60)

    res_3m = run_backtest(THREE_MONTH, "3-MONTH (Jan–Mar 2026)")
    res_6m = run_backtest(SIX_MONTH,   "6-MONTH (Oct 2025–Mar 2026)")
    print_report(res_3m, "3-MONTH (Jan–Mar 2026)")
    print_report(res_6m, "6-MONTH (Oct 2025–Mar 2026)")

    def to_dict(r: MonthResult) -> dict:
        return {"month": r.month_label, "start_equity": r.start_equity,
                "end_equity": r.end_equity, "net_pnl": round(r.net_pnl, 4),
                "pnl_pct": round(r.pnl_pct, 2), "total_trades": r.total_trades,
                "wins": r.wins, "losses": r.losses, "win_rate": round(r.win_rate, 2),
                "avg_win": round(r.avg_win, 4), "avg_loss": round(r.avg_loss, 4),
                "max_drawdown_pct": r.max_drawdown_pct,
                "volume_usd": r.volume_usd, "by_strategy": r.by_strategy()}

    output = {"meta": {"symbol": SYMBOL, "leverage": LEVERAGE, "risk_pct": RISK_PCT,
                       "monthly_reset_equity": 500.0, "candle3_disabled": True,
                       "data_source": "data-api.binance.vision",
                       "generated_at": datetime.now(timezone.utc).isoformat()},
              "backtest_3m": [to_dict(r) for r in res_3m],
              "backtest_6m": [to_dict(r) for r in res_6m]}

    with open("backtest_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print("  Results saved → backtest_results.json")
