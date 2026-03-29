from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from threading import Event, Thread
from time import sleep, time as _time
from typing import Dict, List, Optional
import logging

from exchange.base import ExchangeClient
from execution.order_manager import OrderManager, OrderManagerConfig
from execution.position_manager import Position, PositionManager
from execution.risk_manager import RiskConfig, RiskManager
from execution.session_manager import SessionManager
from execution.volume_manager import VolumeConfig, VolumeManager
from models.signal import Side, TradeSignal
from models.status import BotMode, BotState, BotStatus
from strategies.hybrid_a import EntryExecutor, LiquiditySweepDetector, TrendBiasEvaluator, atr_1m
from strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from strategies.strategy_c import StrategyC, StrategyCConfig
from strategies.trend_breakout import TrendBreakoutConfig, TrendBreakoutStrategy

logger = logging.getLogger(__name__)


@dataclass
class BotConfig:
    symbols: List[str] = None
    monthly_volume_target: float = 1_000_000.0  # CHANGED: 3M -> 1M target
    trading_days: int = 30
    expected_trades_left: Dict[str, int] = None
    equity: float = 500.0
    test_trade_qty: float = 0.001
    test_trade_symbol: str = "BTCUSDT"
    poll_interval_seconds: int = 60
    use_hybrid_trend: bool = True
    max_spread: float = 2.0
    vol_spike_mult: float = 3.0
    entry_wait_minutes: int = 5
    cooldown_seconds: int = 120
    margin_safety_pct: float = 0.20
    balance_cache_ttl: float = 15.0
