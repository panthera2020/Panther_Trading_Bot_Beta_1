"""Data fetching utilities for backtesting — Bybit OHLCV via pybit or ccxt."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

try:
    from pybit.unified_trading import HTTP as BybitHTTP
    HAS_PYBIT = True
except ImportError:
    HAS_PYBIT = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


INTERVAL_MAP = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15",
    "30m": "30", "1h": "60", "4h": "240", "1d": "D",
}


def fetch_klines_pybit(
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
) -> List[Dict]:
    """Fetch OHLCV from Bybit using pybit or raw REST fallback."""
    interval = INTERVAL_MAP.get(timeframe)
    if interval is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    all_candles: List[Dict] = []
    cursor_end = end_ms

    while cursor_end > start_ms:
        if HAS_PYBIT:
            session = BybitHTTP(testnet=False)
            resp = session.get_kline(
                category="linear", symbol=symbol, interval=interval,
                start=start_ms, end=cursor_end, limit=limit,
            )
            rows = resp.get("result", {}).get("list", [])
        elif HAS_REQUESTS:
            url = "https://api.bybit.com/v5/market/kline"
            params = {
                "category": "linear", "symbol": symbol, "interval": interval,
                "start": start_ms, "end": cursor_end, "limit": limit,
            }
            resp = requests.get(url, params=params, timeout=15)
            rows = resp.json().get("result", {}).get("list", [])
        else:
            raise RuntimeError("Neither pybit nor requests is installed")

        if not rows:
            break

        for row in rows:
            ts_val = int(row[0])
            if ts_val < start_ms:
                continue
            all_candles.append({
                "timestamp": ts_val,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })

        oldest_ts = min(int(r[0]) for r in rows)
        if oldest_ts >= cursor_end:
            break
        cursor_end = oldest_ts - 1

    all_candles.sort(key=lambda c: c["timestamp"])
    seen: set = set()
    unique: List[Dict] = []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)
    return unique


def fetch_klines_ccxt(
    exchange,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
    limit: int = 1000,
) -> List[Dict]:
    """Fetch OHLCV using a ccxt exchange instance."""
    all_candles: List[Dict] = []
    current = since_ms
    while current < until_ms:
        candles = exchange.fetch_ohlcv(symbol, timeframe, since=current, limit=limit)
        if not candles:
            break
        for row in candles:
            ts_val = int(row[0])
            if ts_val >= until_ms:
                continue
            all_candles.append({
                "timestamp": ts_val,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        current = candles[-1][0] + 1
        if len(candles) < limit:
            break

    all_candles.sort(key=lambda c: c["timestamp"])
    seen: set = set()
    unique: List[Dict] = []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)
    return unique
