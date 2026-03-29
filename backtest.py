"""
backtest.py — Panther Trading Bot Backtester (OKX data, optimised)
===================================================================
Fetches BTC-USDT-SWAP OHLCV from OKX public API, paginated.
Uses threaded parallel fetching for speed.

Usage:
  python backtest.py --period 3m
  python backtest.py --period 6m
  python backtest.py --period both
"""
from __future__ import annotations

import argparse, sys, math, json, time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests

# -- Config -------------------------------------------------------------------
OKX_BASE         = "https://www.okx.com"
OKX_INSTID       = "BTC-USDT-SWAP"
SYMBOL           = "BTCUSDT"
STARTING_EQUITY  = 500.0
RISK_PCT         = 0.01
MIN_NOTIONAL     = 5.0
TAKER_FEE        = 0.00055
BREAKEVEN_TRIGGER_R = 1.0
RR_RATIO         = 2.0

OKX_BAR_MAP = {"1h": "1H", "5m": "5m", "3m": "3m"}


# -- Data Fetch ---------------------------------------------------------------

def _fetch_page(instid: str, bar: str, after: str) -> List[dict]:
    url = f"{OKX_BASE}/api/v5/market/history-candles"
    for attempt in range(3):
        try:
            r = requests.get(url, params={"instId": instid, "bar": bar,
                                           "after": after, "limit": "100"},
                             timeout=20)
            r.raise_for_status()
            rows = r.json().get("data", [])
            return rows
        except Exception as e:
            if attempt == 2:
                print(f"  [WARN] fetch_page failed ({bar} after={after}): {e}")
            _time.sleep(0.5)
    return []


def fetch_ohlcv(interval: str, start_ms: int, end_ms: int) -> List[dict]:
    bar = OKX_BAR_MAP[interval]
    candles: List[dict] = []
    after = str(end_ms)
    while True:
        rows = _fetch_page(OKX_INSTID, bar, after)
        if not rows:
            break
        for row in rows:
            ts = int(row[0])
            if start_ms <= ts < end_ms:
                candles.append({"timestamp": ts,
                                 "open": float(row[1]), "high": float(row[2]),
                                 "low":  float(row[3]), "close": float(row[4]),
                                 "volume": float(row[5])})
        oldest = int(rows[-1][0])
        if oldest <= start_ms or len(rows) < 100:
            break
        after = str(oldest)
        _time.sleep(0.05)
    candles.sort(key=lambda c: c["timestamp"])
    return candles


def fetch_all_parallel(intervals: List[str], start_ms: int, end_ms: int) -> Dict[str, List[dict]]:
    results: Dict[str, List[dict]] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch_ohlcv, iv, start_ms, end_ms): iv
                   for iv in intervals}
        for fut in as_completed(futures):
            iv = futures[fut]
            data = fut.result()
            print(f"    {iv}: {len(data):,} bars")
            results[iv] = data
    return results


# -- Indicators ---------------------------------------------------------------

def _ema(v: List[float], n: int) -> Optional[float]:
    if len(v) < n: return None
    k, val = 2/(n+1), sum(v[:n])/n
    for x in v[n:]: val = x*k + val*(1-k)
    return val

def _sma(v: List[float], n: int) -> Optional[float]:
    return sum(v[-n:])/n if len(v) >= n else None

def _atr(h, lo, c, n=14) -> Optional[float]:
    if len(c) < n+1: return None
    trs = [max(h[i]-lo[i], abs(h[i]-c[i-1]), abs(lo[i]-c[i-1])) for i in range(1,len(c))]
    return sum(trs[-n:])/n if len(trs) >= n else None

def _bb(c: List[float], n=20, sd=2.0):
    if len(c) < n: return None
    w = c[-n:]; m = sum(w)/n
    std = (sum((x-m)**2 for x in w)/n)**0.5
    return m-sd*std, m, m+sd*std

def _rsi(c: List[float], n=14) -> Optional[float]:
    if len(c) < n+1: return None
    gs = [max(c[i]-c[i-1],0) for i in range(-n,0)]
    ls = [max(c[i-1]-c[i],0) for i in range(-n,0)]
    ag, al = sum(gs)/n, sum(ls)/n
    return 100 if al==0 else 100-100/(1+ag/al)

def _vwap(c: List[float], v: List[float]) -> Optional[float]:
    tv = sum(v); return sum(x*y for x,y in zip(c,v))/tv if tv else None


# -- Data classes -------------------------------------------------------------

@dataclass
class Pos:
    strat: str; side: str; size: float; entry: float
    sl: float; tp: Optional[float]; opened: datetime
    be_moved: bool = False; notional: float = 0.0

@dataclass
class Trade:
    strat: str; side: str; size: float; entry: float; exit: float
    opened: datetime; closed: datetime; pnl: float
    reason: str; notional: float; month: str


# -- Backtester ---------------------------------------------------------------

class Backtester:
    def __init__(self, start: datetime, end: datetime, eq0: float = STARTING_EQUITY):
        self.start, self.end, self.eq0 = start, end, eq0
        self.eq = eq0
        self.pos: Optional[Pos] = None
        self.trades: List[Trade] = []
        self._month: Optional[str] = None
        self.m_eq0: Dict[str,float] = {}
        self.m_vol: Dict[str,float] = {}
        self.m_trades: Dict[str,List[Trade]] = {}

    def _mk(self, dt): return dt.strftime("%Y-%m")

    def _month_reset(self, dt):
        mk = self._mk(dt)
        if mk == self._month: return
        if self._month:
            print(f"  Rollover {self._month} to {mk}: eq=${self.eq:,.2f} -> reset ${self.eq0:,.2f}")
        self._month = mk
        self.eq = self.eq0
        self.m_eq0.setdefault(mk, self.eq0)
        self.m_vol.setdefault(mk, 0.0)
        self.m_trades.setdefault(mk, [])

    def _size(self, entry, sl):
        risk_pts = abs(entry - sl)
        if risk_pts <= 0: return 0.0
        s = math.floor((RISK_PCT * self.eq / risk_pts) / 0.001) * 0.001
        return s if s * entry >= MIN_NOTIONAL else 0.0

    def _fee(self, size, price): return 2 * size * price * TAKER_FEE

    def _check_exits(self, hi, lo, cl, dt):
        p = self.pos
        if p is None: return
        if not p.be_moved:
            r = abs(p.entry - p.sl)
            if r > 0:
                fee_buf = self._fee(p.size, p.entry) / max(p.size, 1e-9) / p.size
                if p.side == "BUY" and cl >= p.entry + BREAKEVEN_TRIGGER_R * r:
                    p.sl = p.entry + fee_buf; p.be_moved = True
                elif p.side == "SELL" and cl <= p.entry - BREAKEVEN_TRIGGER_R * r:
                    p.sl = p.entry - fee_buf; p.be_moved = True
        if p.side == "BUY"  and lo <= p.sl: self._close(p.sl, dt, "stop_loss"); return
        if p.side == "SELL" and hi >= p.sl: self._close(p.sl, dt, "stop_loss"); return
        if p.tp:
            if p.side == "BUY"  and hi >= p.tp: self._close(p.tp, dt, "take_profit")
            if p.side == "SELL" and lo <= p.tp: self._close(p.tp, dt, "take_profit")

    def _close(self, exit_px, dt, reason):
        p = self.pos
        pnl_pts = (exit_px - p.entry) if p.side == "BUY" else (p.entry - exit_px)
        pnl = pnl_pts * p.size - self._fee(p.size, p.entry)
        self.eq = max(self.eq + pnl, 0.01)
        mk = self._mk(dt)
        self.m_vol[mk] = self.m_vol.get(mk, 0.0) + p.notional
        t = Trade(strat=p.strat, side=p.side, size=p.size, entry=p.entry,
                  exit=exit_px, opened=p.opened, closed=dt, pnl=pnl,
                  reason=reason, notional=p.notional, month=mk)
        self.trades.append(t)
        self.m_trades.setdefault(mk, []).append(t)
        self.pos = None

    def _open(self, strat, side, entry, sl, tp, dt):
        if self.pos: return
        size = self._size(entry, sl)
        if size <= 0: return
        self.pos = Pos(strat=strat, side=side, size=size, entry=entry,
                       sl=sl, tp=tp, opened=dt, notional=size*entry)

    # -- Signals --------------------------------------------------------------

    def _sig_trend(self, cs) -> Optional[Tuple]:
        if len(cs) < 210: return None
        c=[x["close"] for x in cs]; h=[x["high"] for x in cs]; lo=[x["low"] for x in cs]; v=[x["volume"] for x in cs]
        ef=_ema(c,50); es=_ema(c,200); at=_atr(h,lo,c,14); vs=_sma(v,20)
        if None in (ef,es,at): return None
        if abs(ef-es)/es < 0.005: return None
        rh=max(h[-20:]); rl=min(lo[-20:]); lc=c[-1]
        vok = vs is None or v[-1]>vs
        if ef>es and lc>rh and vok:
            sl=lc-2*at; r=abs(lc-sl); return ("BUY",lc,sl,lc+RR_RATIO*r)
        if ef<es and lc<rl and vok:
            sl=lc+2*at; r=abs(sl-lc); return ("SELL",lc,sl,lc-RR_RATIO*r)

    def _sig_scalp(self, cs) -> Optional[Tuple]:
        if len(cs) < 22: return None
        c=[x["close"] for x in cs]; h=[x["high"] for x in cs]; lo=[x["low"] for x in cs]; v=[x["volume"] for x in cs]
        bb=_bb(c); at=_atr(h,lo,c,14); vw=_vwap(c[-20:],v[-20:]); rs=_rsi(c,14)
        if None in (bb,at,vw): return None
        lower,_,upper=bb; lc=c[-1]
        if lc<lower and lc<vw and (rs is None or rs<25):
            sl=lc-1.5*at; r=abs(lc-sl); return ("BUY",lc,sl,lc+RR_RATIO*r)
        if lc>upper and lc>vw and (rs is None or rs>75):
            sl=lc+1.5*at; r=abs(sl-lc); return ("SELL",lc,sl,lc-RR_RATIO*r)

    def _sig_c3(self, cs) -> Optional[Tuple]:
        if len(cs) < 16: return None
        c=[x["close"] for x in cs]; h=[x["high"] for x in cs]; lo=[x["low"] for x in cs]; v=[x["volume"] for x in cs]
        at=_atr(h,lo,c,14)
        if at is None: return None
        l3=cs[-3:]; vols=v[-3:]
        if not (vols[-1]>vols[-2]>vols[-3]): return None
        bull=all(x["close"]>x["open"] for x in l3)
        bear=all(x["close"]<x["open"] for x in l3)
        fo=l3[0]["open"]; lc=c[-1]
        if bull:
            r=abs(lc-fo); return ("BUY",lc,fo,lc+RR_RATIO*r) if r>0 else None
        if bear:
            r=abs(fo-lc); return ("SELL",lc,fo,lc-RR_RATIO*r) if r>0 else None

    # -- Run ------------------------------------------------------------------

    def run(self, c1h: List[dict], c5m: List[dict], c3m: List[dict]):
        print(f"\nBacktest {self.start.date()} to {self.end.date()} | "
              f"equity ${self.eq0:,.2f}/mo | risk {RISK_PCT*100}% | TP {RR_RATIO}R | BE {BREAKEVEN_TRIGGER_R}R")
        print(f"  Bars  1h:{len(c1h):,}  5m:{len(c5m):,}  3m:{len(c3m):,}")

        for i, bar in enumerate(c1h):
            ts = bar["timestamp"]
            dt = datetime.fromtimestamp(ts/1000, tz=timezone.utc)
            if dt < self.start or dt > self.end: continue
            self._month_reset(dt)
            self._check_exits(bar["high"], bar["low"], bar["close"], dt)
            if self.pos: continue

            win1h = c1h[max(0,i-250):i+1]
            sig = self._sig_trend(win1h)
            if sig: self._open("trend", *sig, dt); continue

            win5m = [c for c in c5m if c["timestamp"] <= ts][-80:]
            if len(win5m) >= 22:
                sig = self._sig_scalp(win5m)
                if sig: self._open("scalp", *sig, dt); continue

            if 1 <= dt.hour < 9:
                win3m = [c for c in c3m if c["timestamp"] <= ts][-60:]
                if len(win3m) >= 16:
                    sig = self._sig_c3(win3m)
                    if sig: self._open("candle3", *sig, dt); continue

        if c1h and self.pos:
            last = c1h[-1]
            self._close(last["close"],
                        datetime.fromtimestamp(last["timestamp"]/1000, tz=timezone.utc),
                        "end_of_period")


# -- Report -------------------------------------------------------------------

def report(bt: Backtester, label: str):
    T = bt.trades; SEP = "="*65
    print(f"\n{SEP}")
    print(f"  BACKTEST RESULTS -- {label}")
    print(SEP)
    print(f"  Period        : {bt.start.date()} to {bt.end.date()}")
    print(f"  Symbol        : {SYMBOL} Perpetual (BTC/USDT)")
    print(f"  Starting eq   : ${bt.eq0:,.2f} per month (reset monthly)")
    print(f"  Risk          : {RISK_PCT*100:.1f}%/trade | TP {RR_RATIO}:1 | BE at {BREAKEVEN_TRIGGER_R}R")
    if not T:
        print("  No trades executed."); print(SEP); return
    wins  = [t for t in T if t.pnl>0]; loss=[t for t in T if t.pnl<=0]
    pnl   = sum(t.pnl for t in T)
    wr    = len(wins)/len(T)*100
    aw    = sum(t.pnl for t in wins)/len(wins) if wins else 0
    al    = sum(t.pnl for t in loss)/len(loss) if loss else 0
    gw    = sum(t.pnl for t in wins); gl=abs(sum(t.pnl for t in loss))
    pf    = gw/gl if gl else float("inf")
    vol   = sum(t.notional for t in T)
    print(f"\n  -- Overall --------------------------------------------------")
    print(f"  Total trades  : {len(T)}")
    print(f"  Win/Loss      : {len(wins)}W / {len(loss)}L  ({wr:.1f}% WR)")
    print(f"  Total PnL     : ${pnl:+,.2f}")
    print(f"  Avg win       : ${aw:+,.2f}  |  Avg loss: ${al:+,.2f}")
    print(f"  Profit factor : {pf:.2f}x")
    print(f"  Total volume  : ${vol:,.2f}")

    print(f"\n  -- Strategy Breakdown ---------------------------------------")
    print(f"  {'Strategy':<12} {'Trades':>6} {'WR':>6} {'PnL':>11} {'Volume':>14}")
    print(f"  {'-'*12} {'-'*6} {'-'*6} {'-'*11} {'-'*14}")
    by_s: Dict[str,list] = {}
    for t in T: by_s.setdefault(t.strat,[]).append(t)
    for s, st in sorted(by_s.items()):
        sw=[t for t in st if t.pnl>0]; swr=len(sw)/len(st)*100
        print(f"  {s:<12} {len(st):>6} {swr:>5.1f}% ${sum(t.pnl for t in st):>+10,.2f} ${sum(t.notional for t in st):>13,.2f}")

    print(f"\n  -- Monthly Breakdown ----------------------------------------")
    print(f"  {'Month':<8} {'Trades':>6} {'WR':>6} {'PnL':>11} {'Volume':>14} {'Start Eq':>10}")
    print(f"  {'-'*8} {'-'*6} {'-'*6} {'-'*11} {'-'*14} {'-'*10}")
    for mk in sorted(bt.m_trades.keys()):
        mt=bt.m_trades[mk]; mw=sum(1 for t in mt if t.pnl>0)
        mwr=mw/len(mt)*100 if mt else 0; mpnl=sum(t.pnl for t in mt)
        mvol=bt.m_vol.get(mk,0); meq=bt.m_eq0.get(mk,bt.eq0)
        print(f"  {mk:<8} {len(mt):>6} {mwr:>5.1f}% ${mpnl:>+10,.2f} ${mvol:>13,.2f} ${meq:>9,.2f}")

    print(f"\n  -- Exit Reasons ---------------------------------------------")
    er: Dict[str,int]={}
    for t in T: er[t.reason]=er.get(t.reason,0)+1
    for r,n in sorted(er.items(),key=lambda x:-x[1]):
        print(f"  {r:<22} {n:>4}  ({n/len(T)*100:.1f}%)")
    print(f"\n{SEP}\n")


# -- Main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["3m","6m","both"], default="both")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    periods = []
    if args.period in ("3m","both"): periods.append(("3-Month", now-timedelta(days=91), now))
    if args.period in ("6m","both"): periods.append(("6-Month", now-timedelta(days=182), now))

    earliest = min(p[1] for p in periods)
    start_ms = int(earliest.timestamp()*1000)
    end_ms   = int(now.timestamp()*1000)

    print(f"Fetching BTC-USDT-SWAP OHLCV from OKX public API")
    print(f"Range: {earliest.date()} to {now.date()}\n")

    ivs = ["1h","5m","3m"]
    data = fetch_all_parallel(ivs, start_ms, end_ms)
    c1h, c5m, c3m = data["1h"], data["5m"], data["3m"]

    if not c1h:
        print("ERROR: No 1h data. Aborting."); sys.exit(1)

    for label, s, e in periods:
        bt = Backtester(start=s, end=e, eq0=STARTING_EQUITY)
        bt.run(c1h, c5m, c3m)
        report(bt, label)
        fname = f"backtest_{label.lower().replace('-','').replace(' ','_')}.json"
        with open(fname,"w") as f:
            json.dump([asdict(t) for t in bt.trades], f, indent=2, default=str)
        print(f"  Trade log -> {fname}\n")


if __name__ == "__main__":
    main()
