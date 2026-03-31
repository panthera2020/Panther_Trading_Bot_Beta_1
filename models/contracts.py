"""Contracts (Protocols) for strategy and execution layer components.

These define the interfaces that classes must satisfy. Python's Protocol
means you don't need to inherit — just implement the methods. This makes
it easy to add new strategies or swap execution components without
breaking existing code.

Usage:
    from models.contracts import Strategy

    class MyNewStrategy:
        strategy_id = "my_strat"
        def generate_signal(self, candles, size, symbol, timestamp, **kwargs):
            ...  # returns Optional[TradeSignal]

    # MyNewStrategy automatically satisfies the Strategy protocol
    # because it has strategy_id and generate_signal with the right signature.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from models.signal import TradeSignal


@runtime_checkable
class Strategy(Protocol):
    """Contract: every trading strategy must satisfy this interface.

    Properties:
        strategy_id: unique string identifier (e.g. "scalp", "trend", "candle3")

    Methods:
        generate_signal: given candles + sizing info, return a TradeSignal or None
    """
    strategy_id: str

    def generate_signal(
        self,
        candles: List[Dict[str, float]],
        size: float,
        symbol: str,
        timestamp: datetime,
        **kwargs: Any,
    ) -> Optional[TradeSignal]: ...


@runtime_checkable
class RiskGate(Protocol):
    """Contract: risk management must implement these checks."""

    def can_trade(self, equity: float, timestamp: datetime) -> bool: ...
    def register_pnl(self, pnl: float) -> None: ...
    def register_order(self, timestamp: datetime) -> None: ...


@runtime_checkable
class PositionTracker(Protocol):
    """Contract: position tracking must support these operations."""

    def has_open_position(self, symbol: str, strategy_id: str) -> bool: ...
    def open_positions_count(self) -> int: ...
    def open_position(self, position: Any) -> None: ...
    def close_position(self, symbol: str, strategy_id: str) -> None: ...
    def get_position(self, symbol: str, strategy_id: str) -> Any: ...


@runtime_checkable
class VolumeTracker(Protocol):
    """Contract: volume tracking for daily/monthly targets."""

    daily_volume: float
    monthly_volume: float

    def register_trade(self, strategy_id: str, notional: float, timestamp: datetime) -> None: ...
    def remaining_daily_volume(self, timestamp: datetime) -> float: ...
    def compute_size(self, risk_pct: float, equity: float, atr: float, k: float, price: float) -> float: ...
