"""Exchange statistics: volume tracking, trade stats, open positions — from Bybit API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


class BybitStats:
    """Fetches reporting data from Bybit. Injected with the pybit session."""

    def __init__(self, session, category: str = "linear"):
        self._session = session
        self._category = category
        self._cache: Dict[str, Any] = {}
        self._cache_ts: float = 0.0

    def get_stats(self, symbols: List[str], cache_ttl: float = 15.0) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        if self._cache and (now.timestamp() - self._cache_ts) < cache_ttl:
            return self._cache

        symbol = symbols[0] if symbols else "BTCUSDT"
        start_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        start_week = now - timedelta(days=7)

        try:
            executions = self._fetch_executions(symbol, start_week, now)
            volume_daily = self._sum_volume(executions, start_day)
            volume_weekly = self._sum_volume(executions, start_week)

            closed_pnl = self._fetch_closed_pnl(symbol, start_week, now)
            trade_stats = self._compute_trade_stats(closed_pnl, start_week)
            closed_trades = self._map_closed_trades(closed_pnl, limit=10)
            open_trades = self._fetch_open_positions(symbol)
        except Exception:
            if self._cache:
                return self._cache
            return {
                "volume": {"daily": 0.0, "weekly": 0.0},
                "trade_stats": {"trades": 0, "wins": 0, "win_rate": 0.0, "pnl": 0.0},
                "open_trades": [],
                "closed_trades": [],
            }

        stats = {
            "volume": {"daily": round(volume_daily, 2), "weekly": round(volume_weekly, 2)},
            "trade_stats": trade_stats,
            "open_trades": open_trades,
            "closed_trades": closed_trades,
        }
        self._cache = stats
        self._cache_ts = now.timestamp()
        return stats

    # ── internals ────────────────────────────────────────────────

    def _safe_request(self, func, **kwargs):
        try:
            return func(**kwargs)
        except KeyError as exc:
            raise RuntimeError("Rate limit header missing; throttling request.") from exc

    def _fetch_executions(self, symbol: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        window_start = start
        while window_start < end:
            window_end = min(window_start + timedelta(days=7), end)
            results.extend(self._fetch_paged(
                self._session.get_executions, symbol,
                int(window_start.timestamp() * 1000),
                int(window_end.timestamp() * 1000),
            ))
            window_start = window_end
        return results

    def _fetch_closed_pnl(self, symbol: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        window_start = start
        while window_start < end:
            window_end = min(window_start + timedelta(days=7), end)
            results.extend(self._fetch_paged(
                self._session.get_closed_pnl, symbol,
                int(window_start.timestamp() * 1000),
                int(window_end.timestamp() * 1000),
            ))
            window_start = window_end
        return results

    def _fetch_paged(self, api_func, symbol: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(5):
            payload: Dict[str, Any] = {
                "category": self._category, "symbol": symbol,
                "startTime": start_ms, "endTime": end_ms, "limit": 200,
            }
            if cursor:
                payload["cursor"] = cursor
            response = self._safe_request(api_func, **payload)
            items = response.get("result", {}).get("list", [])
            results.extend(items)
            cursor = response.get("result", {}).get("nextPageCursor")
            if not cursor:
                break
        return results

    def _fetch_open_positions(self, symbol: str) -> List[Dict[str, Any]]:
        response = self._safe_request(
            self._session.get_positions, category=self._category, symbol=symbol,
        )
        mapped = []
        for pos in response.get("result", {}).get("list", []):
            size = float(pos.get("size", 0.0) or 0.0)
            if size == 0:
                continue
            mapped.append({
                "symbol": pos.get("symbol"),
                "side": pos.get("side"),
                "size": size,
                "entry_price": float(pos.get("avgPrice", 0.0) or 0.0),
                "unrealized_pnl": float(pos.get("unrealisedPnl", 0.0) or 0.0),
                "updated_at": pos.get("updatedTime"),
            })
        return mapped

    def _sum_volume(self, executions: List[Dict[str, Any]], start: datetime) -> float:
        start_ms = int(start.timestamp() * 1000)
        return sum(
            float(e.get("execPrice", 0.0) or 0.0) * float(e.get("execQty", 0.0) or 0.0)
            for e in executions
            if int(e.get("execTime", 0) or 0) >= start_ms
        )

    def _compute_trade_stats(self, closed_pnl: List[Dict[str, Any]], start: datetime) -> Dict[str, Any]:
        start_ms = int(start.timestamp() * 1000)
        trades = [t for t in closed_pnl if int(t.get("createdTime", 0) or 0) >= start_ms]
        total = len(trades)
        pnl = sum(float(t.get("closedPnl", 0.0) or 0.0) for t in trades)
        wins = sum(1 for t in trades if float(t.get("closedPnl", 0.0) or 0.0) > 0)
        wr = (wins / total) * 100 if total else 0.0
        return {"trades": total, "wins": wins, "win_rate": round(wr, 2), "pnl": round(pnl, 2)}

    def _map_closed_trades(self, closed_pnl: List[Dict[str, Any]], limit: int = 50) -> List[Dict[str, Any]]:
        return [
            {
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "qty": float(item.get("qty", 0.0) or 0.0),
                "entry_price": float(item.get("avgEntryPrice", 0.0) or 0.0),
                "exit_price": float(item.get("avgExitPrice", 0.0) or 0.0),
                "pnl": float(item.get("closedPnl", 0.0) or 0.0),
                "closed_at": item.get("createdTime"),
            }
            for item in closed_pnl[:limit]
        ]
