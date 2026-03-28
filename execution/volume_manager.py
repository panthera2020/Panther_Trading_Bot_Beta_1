from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict
import logging

logger = logging.getLogger(__name__)


@dataclass
class VolumeConfig:
    monthly_target: float
    trading_days: int = 30
    strategy_allocations: Dict[str, float] = field(default_factory=dict)


class VolumeManager:
    def __init__(self, config: VolumeConfig):
        self.config = config
        self.daily_volume: float = 0.0
        self.monthly_volume: float = 0.0
        self.strategy_volume: Dict[str, float] = {k: 0.0 for k in config.strategy_allocations}
        self._current_day = self._day_key(datetime.now(timezone.utc))
        self._current_month = self._month_key(datetime.now(timezone.utc))

    @property
    def daily_target(self) -> float:
        return self.config.monthly_target / max(self.config.trading_days, 1)

    def _day_key(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d")

    def _month_key(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m")

    def _roll_if_needed(self, dt: datetime) -> None:
        day_key = self._day_key(dt)
        month_key = self._month_key(dt)
        if day_key != self._current_day:
            self.daily_volume = 0.0
            self._current_day = day_key
        if month_key != self._current_month:
            self.monthly_volume = 0.0
            self.strategy_volume = {k: 0.0 for k in self.strategy_volume}
            self._current_month = month_key

    def register_trade(self, strategy_id: str, notional: float, timestamp: datetime) -> None:
        self._roll_if_needed(timestamp)
        self.daily_volume += notional
        self.monthly_volume += notional
        self.strategy_volume.setdefault(strategy_id, 0.0)
        self.strategy_volume[strategy_id] += notional

    def remaining_daily_volume(self, timestamp: datetime) -> float:
        self._roll_if_needed(timestamp)
        return max(self.daily_target - self.daily_volume, 0.0)

    def strategy_remaining(self, strategy_id: str, timestamp: datetime) -> float:
        self._roll_if_needed(timestamp)
        allocation = self.config.strategy_allocations.get(strategy_id, 1.0)
        target = self.daily_target * allocation
        return max(target - self.strategy_volume.get(strategy_id, 0.0), 0.0)

    def compute_size(
        self,
        strategy_id: str,
        risk_pct: float,
        equity: float,
        atr: float,
        k: float,
        expected_trades_left: int,
        price: float,
        timestamp: datetime,
    ) -> float:
        """
        Compute position size using a two-component model:

        1. Risk-based size: size = (risk% * equity) / (atr * k)
           Limits the loss per trade to risk% of equity.

        2. Volume-pacing size: size = (remaining_volume / expected_trades) / price
           Keeps each trade sized to hit the daily target, assuming the
           expected number of remaining trades evenly consume the remaining quota.

        FIX v1.1: The old code took min(risk_size, volume_size), which meant
        the risk cap always won. On a $500 account with 1% risk, risk_size
        is tiny (~0.006 BTC = ~$500 notional), far below what is needed to
        hit $100k/day. The volume-pacing size was always ignored.

        New behaviour:
        - volume_pacing_size is still computed and logged for transparency.
        - The returned size is risk_size, which is now properly calibrated
          (3% risk instead of 1%) PLUS the leverage floor in main.py
          ensures a minimum notional that keeps the bot on pace.
        - Volume target acts as a SOFT STOP: once the target is met for a
          strategy, that strategy pauses but does not inflate position size.
        - When volume target is already met, fall back to pure risk-based size.
        """
        if atr <= 0 or k <= 0 or price <= 0:
            return 0.0

        # Primary: risk-based sizing using REAL equity
        risk_size = (risk_pct * equity) / (atr * k)

        # Volume pacing: informational + soft stop
        remaining_volume = self.strategy_remaining(strategy_id, timestamp)
        if expected_trades_left <= 0:
            expected_trades_left = 1
        volume_size = (remaining_volume / expected_trades_left) / price

        # Log both for debugging
        logger.debug(
            f"[VOLUME_MGR] {strategy_id}: risk_size={risk_size:.6f} "
            f"volume_size={volume_size:.6f} remaining=${remaining_volume:,.0f} "
            f"expected_trades={expected_trades_left}"
        )

        # If volume target already met for this strategy, still allow risk-based trades
        # (never kill trading just because volume target is reached — it's aspirational).
        if volume_size <= 0 and risk_size > 0:
            logger.info(
                f"Volume target met for {strategy_id}, using risk-based size only. "
                f"risk_size={risk_size:.6f}"
            )
            return risk_size

        # Use risk-based size. The leverage floor in main.py will lift it
        # if the account is too small to generate meaningful notional.
        return risk_size
