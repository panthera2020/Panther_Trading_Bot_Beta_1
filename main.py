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
    monthly_volume_target: float = 3_000_000.0
    trading_days: int = 30
    expected_trades_left: Dict[str, int] = None
    # equity is now a FALLBACK default, not the primary source.
    # The bot fetches real equity from the exchange before every sizing cycle.
    equity: float = 500.0  # Fallback only â€” real equity is fetched from exchange on startup
    test_trade_qty: float = 0.001
    test_trade_symbol: str = "BTCUSDT"
    poll_interval_seconds: int = 60
    use_hybrid_trend: bool = True
    max_spread: float = 2.0
    vol_spike_mult: float = 3.0
    entry_wait_minutes: int = 5
    cooldown_seconds: int = 120
    # NEW: margin safety buffer â€” require at least 20% free margin after trade
    margin_safety_pct: float = 0.20
    # NEW: balance cache TTL in seconds (avoid hammering API)
    balance_cache_ttl: float = 15.0


class TradingBot:
    def __init__(self, config: BotConfig, exchange_client: ExchangeClient):
        self.config = config
        self.exchange_client = exchange_client
        self.state = BotState.STOPPED
        self.session_manager = SessionManager()
        self.position_manager = PositionManager()
        self.risk_manager = RiskManager(RiskConfig())
        self.volume_manager = VolumeManager(
            VolumeConfig(
                monthly_target=config.monthly_volume_target,
                trading_days=config.trading_days,
                strategy_allocations={"trend": 0.25, "scalp": 0.55, "candle3": 0.2},
            )
        )
        # BTC-only for now; add other symbols when ready.
        self.symbols = config.symbols or ["BTCUSDT"]
        self.order_manager = OrderManager(
            client=exchange_client,
            position_manager=self.position_manager,
            risk_manager=self.risk_manager,
            volume_manager=self.volume_manager,
            config=OrderManagerConfig(symbol=""),
        )
        self.trend_strategy = TrendBreakoutStrategy(TrendBreakoutConfig())
        self.scalp_strategy = MeanReversionStrategy(MeanReversionConfig())
        self.strategy_c = StrategyC(StrategyCConfig())
        self.bias_evaluator = TrendBiasEvaluator()
        self.sweep_detector = LiquiditySweepDetector()
        self.entry_executor = EntryExecutor(max_entry_wait_minutes=config.entry_wait_minutes)
        self._last_sweep = None
        self._last_trade_time: Dict[str, datetime] = {}
        self.last_error: Optional[str] = None
        self.expected_trades_left = config.expected_trades_left or {"trend": 2, "scalp": 20, "candle3": 30}
        self.enabled_strategies = {"trend", "scalp", "candle3"}
        self._strategies_enabled = True
        self._test_trade_in_progress = False
        self._mode = BotMode.IDLE
        self._stop_event = Event()
        self._loop_thread: Optional[Thread] = None
        # NEW: cached balance data
        self._cached_balance: Dict[str, float] = {}
        self._balance_cache_ts: float = 0.0

    # â”€â”€â”€ LIVE EQUITY â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_live_equity(self) -> float:
        """
        Return the real account equity from the exchange, with a short cache
        to avoid hammering the API on every sizing call within the same poll.
        Falls back to config.equity if the API call fails.
        """
        now = _time()
        if self._cached_balance and (now - self._balance_cache_ts) < self.config.balance_cache_ttl:
            return float(self._cached_balance.get("total_equity", self.config.equity) or self.config.equity)

        try:
            balance = self.exchange_client.get_balance()
            equity = float(balance.get("total_equity", 0.0) or 0.0)
            if equity > 0:
                self._cached_balance = balance
                self._balance_cache_ts = now
                self.config.equity = equity  # keep config in sync for status display
                return equity
            else:
                logger.warning("Exchange returned zero equity, using cached/config value")
                return self.config.equity
        except Exception as exc:
            logger.warning(f"Failed to fetch balance: {exc}. Using config equity={self.config.equity}")
            self.last_error = str(exc)
            return self.config.equity

    def _get_available_margin(self) -> float:
        """Return the available margin from the last balance fetch."""
        self._get_live_equity()  # ensure cache is fresh
        return float(self._cached_balance.get("available_balance", 0.0) or 0.0)

    def _margin_check(self, price: float, qty: float, symbol: str) -> bool:
        """
        Pre-trade margin validation.
        Ensure the notional value of the position doesn't exceed available margin
        minus a safety buffer. This prevents the bot from sending orders that
        Bybit will reject with insufficient balance.
        """
        available = self._get_available_margin()
        if available <= 0:
            logger.warning(f"No available margin. symbol={symbol}")
            self.order_manager.log_event("WARN", f"No available margin. symbol={symbol}")
            return False

        notional = price * qty
        # With leverage, the required margin is notional / leverage
        leverage = getattr(getattr(self.exchange_client, 'config', None), 'leverage', 50)
        required_margin = notional / leverage
        # Keep a safety buffer
        max_margin = available * (1.0 - self.config.margin_safety_pct)

        if required_margin > max_margin:
            logger.warning(
                f"Margin check FAILED. symbol={symbol} "
                f"required_margin={required_margin:.2f} available={available:.2f} "
                f"max_usable={max_margin:.2f} notional={notional:.2f}"
            )
            self.order_manager.log_event(
                "WARN",
                f"Margin check failed: need ${required_margin:.2f} but only ${max_margin:.2f} available. symbol={symbol}",
            )
            return False

        return True

    # â”€â”€â”€ STARTUP / CONTROL â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def start(self, strategies: Optional[List[str]] = None, run_test_trade: bool = True) -> None:
        if self.state in {BotState.TERMINATED, BotState.ERROR}:
            return
        if strategies:
            self.enabled_strategies = set(strategies)
        else:
            self.enabled_strategies = {"trend", "scalp", "candle3"}

        # Fetch real equity on startup so all sizing uses the actual balance
        startup_equity = self._get_live_equity()
        risk_pct = self.risk_manager.config.risk_per_trade_pct
        risk_per_trade = risk_pct * startup_equity
        logger.info(
            f"Bot starting | Live equity: ${startup_equity:,.2f} | "
            f"Risk per trade: {risk_pct:.1%} = ${risk_per_trade:,.2f} | "
            f"Min notional: ${self.MIN_NOTIONAL_USD:.2f}"
        )
        if startup_equity < 50:
            logger.warning(
                f"Very low equity (${startup_equity:,.2f}). "
                f"Some trades may be skipped if position size is below exchange minimums."
            )

        if run_test_trade:
            self._strategies_enabled = False
            self._test_trade_in_progress = True
            self._mode = BotMode.TEST_TRADE
            Thread(target=self._run_test_trade, daemon=True).start()
        else:
            self._strategies_enabled = True
            self._mode = BotMode.SCANNING
        self.state = BotState.RUNNING
        self._start_loop()

    def stop(self) -> None:
        self.state = BotState.STOPPED
        self._mode = BotMode.IDLE
        self._stop_loop()

    def pause(self) -> None:
        if self.state == BotState.RUNNING:
            self.state = BotState.PAUSED
            self._mode = BotMode.IDLE

    def terminate(self) -> None:
        self.state = BotState.TERMINATED
        self._mode = BotMode.IDLE
        self._stop_loop()

    def status(self) -> BotStatus:
        balance = {}
        exchange_stats: dict = {}
        try:
            balance = self.exchange_client.get_balance()
            exchange_stats = self.exchange_client.get_exchange_stats(self.symbols)
        except Exception as exc:
            if "Retryable error occurred" not in str(exc):
                self.last_error = str(exc)
        daily_volume = self.volume_manager.daily_volume
        monthly_volume = self.volume_manager.monthly_volume
        exchange_volume: dict = {}
        trade_stats = self.position_manager.trade_stats()
        open_trades = self.position_manager.open_positions()
        closed_trades = self.position_manager.closed_trades()
        open_positions_count = self.position_manager.open_positions_count()
        if exchange_stats.get("volume"):
            exchange_volume = exchange_stats["volume"]
            daily_volume = exchange_stats["volume"].get("daily", daily_volume)
        if exchange_stats.get("trade_stats"):
            trade_stats = exchange_stats["trade_stats"]
        if exchange_stats.get("open_trades"):
            open_trades = self._merge_open_trades(exchange_stats["open_trades"], open_trades)
            open_positions_count = len(open_trades)
        if exchange_stats.get("closed_trades"):
            closed_trades = exchange_stats["closed_trades"]
        return BotStatus(
            state=self.state,
            mode=self._mode,
            daily_volume=daily_volume,
            daily_target=self.volume_manager.daily_target,
            monthly_volume=monthly_volume,
            exchange_volume=exchange_volume,
            strategy_volume=self.volume_manager.strategy_volume,
            open_positions=open_positions_count,
            last_error=self.last_error,
            balance=balance,
            trade_stats=trade_stats,
            open_trades=open_trades,
            closed_trades=closed_trades,
            execution_events=self.order_manager.get_events(),
        )

    def _merge_open_trades(self, exchange_trades: List[dict], local_trades: List[dict]) -> List[dict]:
        local_map = {}
        for trade in local_trades:
            key = (trade.get("symbol"), (trade.get("side") or "").upper())
            local_map[key] = trade
        merged = []
        for trade in exchange_trades:
            key = (trade.get("symbol"), (trade.get("side") or "").upper())
            local = local_map.get(key, {})
            merged.append(
                {
                    **trade,
                    "strategy_id": local.get("strategy_id"),
                    "stop_loss": local.get("stop_loss"),
                    "take_profit": local.get("take_profit"),
                }
            )
        return merged

    # â”€â”€â”€ POSITION SIZING (FIXED) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    # Bybit minimum notional value for perpetual orders (USD)
    MIN_NOTIONAL_USD: float = 5.0

    def _size_for_strategy(
        self,
        strategy_id: str,
        atr_value: float,
        price: float,
        k: float,
        timestamp: datetime,
    ) -> float:
        # FIXED: Use live equity instead of hardcoded config value
        live_equity = self._get_live_equity()

        session = self.session_manager.current_session(timestamp)
        size_mult = session.strategy_size_mult.get(strategy_id, 1.0)
        base_size = self.volume_manager.compute_size(
            strategy_id=strategy_id,
            risk_pct=self.risk_manager.config.risk_per_trade_pct,
            equity=live_equity,  # FIXED: was self.config.equity (hardcoded)
            atr=atr_value,
            k=k,
            expected_trades_left=self.expected_trades_left.get(strategy_id, 1),
            price=price,
            timestamp=timestamp,
        )
        final_size = base_size * size_mult

        # Log sizing details for debugging small accounts
        notional = final_size * price if price > 0 else 0.0
        risk_dollar = self.risk_manager.config.risk_per_trade_pct * live_equity
        logger.debug(
            f"[SIZE] {strategy_id}: equity=${live_equity:,.2f} "
            f"risk=${risk_dollar:,.2f} atr={atr_value:.2f} "
            f"size={final_size:.6f} notional=${notional:,.2f}"
        )

        # Skip if notional is below exchange minimum
        if notional < self.MIN_NOTIONAL_USD:
            logger.warning(
                f"Notional ${notional:.2f} below minimum ${self.MIN_NOTIONAL_USD:.2f} "
                f"for {strategy_id}. equity=${live_equity:,.2f}. Skipping trade."
            )
            return 0.0

        return final_size

    # â”€â”€â”€ MARKET DATA HANDLER â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def on_market_data(
        self,
        candles_1h: Dict[str, List[Dict[str, float]]] | List[Dict[str, float]],
        candles_5m: Dict[str, List[Dict[str, float]]] | List[Dict[str, float]],
        candles_3m: Dict[str, List[Dict[str, float]]] | List[Dict[str, float]],
        candles_15m: Dict[str, List[Dict[str, float]]] | List[Dict[str, float]],
        candles_1m: Dict[str, List[Dict[str, float]]] | List[Dict[str, float]],
        timestamp: Optional[datetime] = None,
    ) -> None:
        if self.state != BotState.RUNNING:
            return
        if self._test_trade_in_progress or not self._strategies_enabled:
            return

        ts = timestamp or datetime.now(timezone.utc)

        # FIXED: _refresh_equity now uses _get_live_equity with caching
        self._refresh_equity()

        if not self.risk_manager.can_trade(self.config.equity, ts):
            return

        # Global single-position rule: do not open new trades
        # until all positions are closed (any strategy).
        if self.position_manager.open_positions_count() > 0:
            return

        session = self.session_manager.current_session(ts)
        allow_trend = (
            "trend" in self.enabled_strategies
            and session.name in {"LONDON", "NY"}
            and session.strategy_size_mult.get("trend", 0.0) > 0
        )
        allow_scalp = (
            "scalp" in self.enabled_strategies
            and session.name in {"LONDON", "NY"}
            and session.strategy_size_mult.get("scalp", 0.0) > 0
        )
        allow_c = (
            "candle3" in self.enabled_strategies
            and session.name == "ASIA"
            and session.strategy_size_mult.get("scalp", 0.0) > 0
            # End at London session start (09:00 UTC+1).
            and self.session_manager.is_within_window(ts, 1, 0, 9, 0, tz_offset_hours=1)
        )

        candles_1h_map = (
            candles_1h if isinstance(candles_1h, dict) else {self.config.test_trade_symbol: candles_1h}
        )
        candles_5m_map = (
            candles_5m if isinstance(candles_5m, dict) else {self.config.test_trade_symbol: candles_5m}
        )
        candles_3m_map = (
            candles_3m if isinstance(candles_3m, dict) else {self.config.test_trade_symbol: candles_3m}
        )
        candles_15m_map = (
            candles_15m if isinstance(candles_15m, dict) else {self.config.test_trade_symbol: candles_15m}
        )
        candles_1m_map = (
            candles_1m if isinstance(candles_1m, dict) else {self.config.test_trade_symbol: candles_1m}
        )

        for symbol in self.symbols:
            self._apply_breakeven(symbol, ts)
            if allow_trend and not self.position_manager.has_open_position(symbol, "trend"):
                if self.config.use_hybrid_trend:
                    self._run_hybrid_trend(
                        symbol,
                        candles_1h_map.get(symbol, []),
                        candles_15m_map.get(symbol, []),
                        candles_1m_map.get(symbol, []),
                        ts,
                    )
                else:
                    candles = candles_1h_map.get(symbol, [])
                    atr_val = self._estimate_atr(candles)
                    if atr_val:
                        price = candles[-1]["close"] if candles else 0.0
                        size = self._size_for_strategy("trend", atr_val, price, self.trend_strategy.config.atr_k, ts)
                        size = self.exchange_client.normalize_qty(symbol, size)
                        if size <= 0:
                            continue
                        # NEW: Pre-trade margin check
                        if not self._margin_check(price, size, symbol):
                            continue
                        signal = self.trend_strategy.generate_signal(candles, size, symbol, ts)
                        if signal:
                            try:
                                self.order_manager.execute_signal(signal, ts)
                            except Exception as exc:  # exchange errors should halt the bot
                                self.last_error = str(exc)
                                self.state = BotState.ERROR
                                return

            if allow_scalp and not self.position_manager.has_open_position(symbol, "scalp"):
                candles = candles_5m_map.get(symbol, [])
                atr_val = self._estimate_atr(candles)
                if atr_val:
                    price = candles[-1]["close"] if candles else 0.0
                    size = self._size_for_strategy("scalp", atr_val, price, self.scalp_strategy.config.atr_k, ts)
                    size = self.exchange_client.normalize_qty(symbol, size)
                    if size <= 0:
                        continue
                    # NEW: Pre-trade margin check
                    if not self._margin_check(price, size, symbol):
                        continue
                    signal = self.scalp_strategy.generate_signal(candles, size, symbol, ts)
                    if signal:
                        try:
                            self.order_manager.execute_signal(signal, ts)
                        except Exception as exc:  # exchange errors should halt the bot
                            self.last_error = str(exc)
                            self.state = BotState.ERROR
                            return

            if allow_c and not self.position_manager.has_open_position(symbol, "candle3"):
                candles = candles_3m_map.get(symbol, [])
                atr_val = self._estimate_atr(candles)
                if atr_val:
                    price = candles[-1]["close"] if candles else 0.0
                    signal = self.strategy_c.generate_signal(candles, 1.0, symbol, ts)
                    if not signal:
                        continue
                    risk = abs(signal.price - signal.stop_loss)
                    if risk <= 0:
                        continue
                    # FIXED: Use live equity
                    live_equity = self._get_live_equity()
                    size = self.volume_manager.compute_size(
                        strategy_id="candle3",
                        risk_pct=self.risk_manager.config.risk_per_trade_pct,
                        equity=live_equity,  # FIXED: was self.config.equity
                        atr=risk,
                        k=1.0,
                        expected_trades_left=self.expected_trades_left.get("candle3", 1),
                        price=signal.price,
                        timestamp=ts,
                    )
                    size = self.exchange_client.normalize_qty(symbol, size)
                    if size <= 0:
                        continue
                    # NEW: Pre-trade margin check
                    if not self._margin_check(signal.price, size, symbol):
                        continue
                    signal = TradeSignal(
                        symbol=signal.symbol,
                        strategy_id=signal.strategy_id,
                        side=signal.side,
                        timestamp=signal.timestamp,
                        price=signal.price,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                        size=size,
                        reason=signal.reason,
                    )
                    try:
                        self.order_manager.execute_signal(signal, ts)
                        Thread(
                            target=self._monitor_strategy_c,
                            args=(symbol, "candle3", 30),
                            daemon=True,
                        ).start()
                    except Exception as exc:  # exchange errors should halt the bot
                        self.last_error = str(exc)
                        self.state = BotState.ERROR
                        return

    def _estimate_atr(self, candles: List[Dict[str, float]]) -> Optional[float]:
        if len(candles) < 15:
            return None
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        closes = [c["close"] for c in candles]
        trs = []
        for i in range(-14, 0):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        return sum(trs) / 14

    def _refresh_equity(self) -> None:
        """Refresh equity using the cached live balance fetcher."""
        equity = self._get_live_equity()
        logger.debug(f"Equity refreshed: ${equity:,.2f}")

    def _apply_breakeven(self, symbol: str, ts: datetime) -> None:
        position = self.position_manager.get_position(symbol, "trend")
        if not position or position.breakeven_moved:
            return
        r = abs(position.entry_price - position.stop_loss)
        if r <= 0:
            return
        try:
            price = self.exchange_client.get_last_price(symbol)
        except Exception as exc:
            self.last_error = str(exc)
            return
        if position.side.upper() == "BUY" and price >= position.entry_price + 1.5 * r:
            be_price = self.order_manager.breakeven_price(symbol, position.side, position.entry_price, position.size)
            self.position_manager.update_stop_loss(symbol, "trend", be_price)
        if position.side.upper() == "SELL" and price <= position.entry_price - 1.5 * r:
            be_price = self.order_manager.breakeven_price(symbol, position.side, position.entry_price, position.size)
            self.position_manager.update_stop_loss(symbol, "trend", be_price)

    def _run_hybrid_trend(
        self,
        symbol: str,
        candles_1h: List[Dict[str, float]],
        candles_15m: List[Dict[str, float]],
        candles_1m: List[Dict[str, float]],
        ts: datetime,
    ) -> None:
        bias = self.bias_evaluator.evaluate(candles_1h)
        if bias == "NONE":
            self.order_manager.log_event("INFO", f"Hybrid: no bias. symbol={symbol}")
            self._last_sweep = None
            return

        sweep = self.sweep_detector.detect(candles_15m, bias)
        if sweep:
            self._last_sweep = sweep
            self.order_manager.log_event("INFO", f"Hybrid: sweep detected {sweep.direction}. symbol={symbol}")

        if not self._last_sweep:
            return

        if self._last_sweep.direction != bias:
            self.order_manager.log_event("INFO", f"Hybrid: bias flipped. symbol={symbol}")
            self._last_sweep = None
            return

        last_trade = self._last_trade_time.get("trend")
        if last_trade and (ts - last_trade).total_seconds() < self.config.cooldown_seconds:
            self.order_manager.log_event("INFO", f"Hybrid: cooldown active. symbol={symbol}")
            return

        if not self.entry_executor.should_enter(self._last_sweep, candles_1m, bias):
            self.order_manager.log_event("INFO", f"Hybrid: no 1m entry. symbol={symbol}")
            return

        if not candles_1m:
            self.order_manager.log_event("INFO", f"Hybrid: no 1m candles. symbol={symbol}")
            return

        last = candles_1m[-1]
        spread_proxy = abs(last["high"] - last["low"])
        if spread_proxy > self.config.max_spread:
            self.order_manager.log_event("INFO", f"Hybrid: spread too high. symbol={symbol}")
            return

        atr_val = atr_1m(candles_1m)
        if atr_val is not None and spread_proxy > (atr_val * self.config.vol_spike_mult):
            self.order_manager.log_event("INFO", f"Hybrid: volatility spike. symbol={symbol}")
            return

        entry_price = last["close"]
        stop_loss = self._last_sweep.level
        risk = abs(entry_price - stop_loss)
        if risk <= 0:
            self.order_manager.log_event("INFO", f"Hybrid: invalid risk. symbol={symbol}")
            return

        # FIXED: Use live equity
        live_equity = self._get_live_equity()
        size = self.volume_manager.compute_size(
            strategy_id="trend",
            risk_pct=self.risk_manager.config.risk_per_trade_pct,
            equity=live_equity,  # FIXED: was self.config.equity
            atr=risk,
            k=1.0,
            expected_trades_left=self.expected_trades_left.get("trend", 1),
            price=entry_price,
            timestamp=ts,
        )
        if size <= 0:
            self.order_manager.log_event("INFO", f"Hybrid: size below min. symbol={symbol}")
            return

        # Min notional check for hybrid
        notional = size * entry_price
        if notional < self.MIN_NOTIONAL_USD:
            logger.warning(
                f"Hybrid: notional ${notional:.2f} below min ${self.MIN_NOTIONAL_USD:.2f}. Skipping."
            )
            self.order_manager.log_event("INFO", f"Hybrid: notional too small. symbol={symbol}")
            return

        # NEW: Pre-trade margin check
        if not self._margin_check(entry_price, size, symbol):
            return

        if bias == "LONG":
            take_profit = entry_price + 2 * risk
            side = Side.BUY
        else:
            take_profit = entry_price - 2 * risk
            side = Side.SELL

        signal = TradeSignal(
            symbol=symbol,
            strategy_id="trend",
            side=side,
            timestamp=ts,
            price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            size=size,
            reason="hybrid_sweep_entry",
        )
        try:
            self.order_manager.execute_signal(signal, ts)
            self._last_trade_time["trend"] = ts
            self.order_manager.log_event("INFO", f"Hybrid: entry placed {bias}. symbol={symbol}")
        except Exception as exc:
            self.last_error = str(exc)
            self.state = BotState.ERROR

    def _run_test_trade(self) -> None:
        try:
            entry_price = self.exchange_client.get_last_price(self.config.test_trade_symbol)
            self.exchange_client.create_order(
                symbol=self.config.test_trade_symbol,
                side="buy",
                order_type="market",
                amount=self.config.test_trade_qty,
            )
            self.position_manager.open_position(
                Position(
                    symbol=self.config.test_trade_symbol,
                    strategy_id="test",
                    side="BUY",
                    size=self.config.test_trade_qty,
                    entry_price=entry_price,
                    stop_loss=entry_price * 0.99 if entry_price else 0.0,
                    take_profit=None,
                    opened_at=datetime.now(timezone.utc),
                )
            )
            sleep(5)
            exit_price = self.exchange_client.get_last_price(self.config.test_trade_symbol)
            self.exchange_client.close_position(
                symbol=self.config.test_trade_symbol,
                side="sell",
                amount=self.config.test_trade_qty,
            )
            trade = self.position_manager.close_position_with_price(
                self.config.test_trade_symbol, "test", exit_price or entry_price, datetime.now(timezone.utc)
            )
            if trade:
                self.risk_manager.register_pnl(trade.pnl)
        except Exception as exc:
            self.last_error = str(exc)
            self.state = BotState.ERROR
        finally:
            self._test_trade_in_progress = False
            self._strategies_enabled = True
            if self.state == BotState.RUNNING:
                self._mode = BotMode.SCANNING

    def _monitor_strategy_c(self, symbol: str, strategy_id: str, delay_seconds: int) -> None:
        """
        Close after fixed seconds or on stop-loss.
        """
        while True:
            position = self.position_manager.get_position(symbol, strategy_id)
            if not position:
                return
            opened_at = position.opened_at.astimezone(timezone.utc)
            target_close_time = opened_at + timedelta(seconds=delay_seconds)
            try:
                price = self.exchange_client.get_last_price(symbol)
                if position.side.upper() == "BUY" and price <= position.stop_loss:
                    self.order_manager.close_position(symbol, strategy_id, price, datetime.now(timezone.utc))
                    return
                if position.side.upper() == "SELL" and price >= position.stop_loss:
                    self.order_manager.close_position(symbol, strategy_id, price, datetime.now(timezone.utc))
                    return
            except Exception as exc:
                self.last_error = str(exc)
                # Continue to enforce time-based close even if price fetch fails.
            now = datetime.now(timezone.utc)
            sleep_seconds = max((target_close_time - now).total_seconds(), 0.0)
            if sleep_seconds <= 0:
                break
            sleep(min(sleep_seconds, 1.0))

        # Hard close after fixed timer, with a few retries.
        for _ in range(3):
            try:
                try:
                    exit_price = self.exchange_client.get_last_price(symbol)
                except Exception:
                    exit_price = position.entry_price
                self.order_manager.close_position(symbol, strategy_id, exit_price, datetime.now(timezone.utc))
                return
            except Exception as exc:
                self.last_error = str(exc)
                sleep(1)

    def _start_loop(self) -> None:
        if self._loop_thread and self._loop_thread.is_alive():
            return
        self._stop_event.clear()
        self._loop_thread = Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

    def _stop_loop(self) -> None:
        self._stop_event.set()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.state == BotState.RUNNING and self._strategies_enabled and not self._test_trade_in_progress:
                try:
                    candles_1h = {s: self.exchange_client.fetch_ohlcv(s, "1h", 300) for s in self.symbols}
                    candles_15m = {s: self.exchange_client.fetch_ohlcv(s, "15m", 300) for s in self.symbols}
                    candles_5m = {s: self.exchange_client.fetch_ohlcv(s, "5m", 300) for s in self.symbols}
                    candles_3m = {s: self.exchange_client.fetch_ohlcv(s, "3m", 300) for s in self.symbols}
                    candles_1m = {s: self.exchange_client.fetch_ohlcv(s, "1m", 300) for s in self.symbols}
                    self._mode = BotMode.SCANNING
                    self.on_market_data(
                        candles_1h,
                        candles_5m,
                        candles_3m,
                        candles_15m,
                        candles_1m,
                        datetime.now(timezone.utc),
                    )
                except Exception as exc:
                    # Network/exchange hiccups: keep bot running and retry after 2 minutes.
                    self.last_error = str(exc)
                    self._mode = BotMode.IDLE
                    sleep(120)
                    continue
            sleep(self.config.poll_interval_seconds)