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
from strategies.hybrid_a import EntryExecutor, LiquiditySweepDetector, TrendBiasEvaluator
from strategies.indicators import atr_from_candles
from strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from strategies.strategy_c import StrategyC, StrategyCConfig
from strategies.trend_breakout import TrendBreakoutConfig, TrendBreakoutStrategy

logger = logging.getLogger(__name__)

MIN_NOTIONAL_USD: float = 5.0


@dataclass
class BotConfig:
    symbols: List[str] = None
    monthly_volume_target: float = 1_000_000.0
    trading_days: int = 30
    equity: float = 500.0
    test_trade_qty: float = 0.001
    test_trade_symbol: str = "BTCUSDT"
    poll_interval_seconds: int = 60
    cooldown_seconds: int = 120
    margin_safety_pct: float = 0.20
    balance_cache_ttl: float = 15.0
    # Strategy C: seconds to hold before closing (one 3m candle = 180s)
    strategy_c_hold_seconds: int = 180


class TradingBot:

    # ── init ─────────────────────────────────────────────────────

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
            )
        )
        self.symbols = config.symbols or ["BTCUSDT"]
        self.order_manager = OrderManager(
            client=exchange_client,
            position_manager=self.position_manager,
            risk_manager=self.risk_manager,
            volume_manager=self.volume_manager,
            config=OrderManagerConfig(symbol=""),
        )

        # Strategies
        self.trend_strategy = TrendBreakoutStrategy(TrendBreakoutConfig())
        self.scalp_strategy = MeanReversionStrategy(MeanReversionConfig())
        self.strategy_c = StrategyC(StrategyCConfig())

        # Hybrid trend helpers (used within trend session)
        self.bias_evaluator = TrendBiasEvaluator()
        self.sweep_detector = LiquiditySweepDetector()
        self.entry_executor = EntryExecutor()
        self._last_sweep = None

        # State
        self._last_trade_time: Dict[str, datetime] = {}
        self.last_error: Optional[str] = None
        self.enabled_strategies = {"trend", "scalp", "candle3"}
        self._strategies_enabled = True
        self._test_trade_in_progress = False
        self._mode = BotMode.IDLE
        self._stop_event = Event()
        self._loop_thread: Optional[Thread] = None
        self._cached_balance: Dict[str, float] = {}
        self._balance_cache_ts: float = 0.0

    # ── equity & margin ──────────────────────────────────────────

    def _get_live_equity(self) -> float:
        now = _time()
        if self._cached_balance and (now - self._balance_cache_ts) < self.config.balance_cache_ttl:
            return float(self._cached_balance.get("total_equity", self.config.equity) or self.config.equity)
        try:
            balance = self.exchange_client.get_balance()
            equity = float(balance.get("total_equity", 0.0) or 0.0)
            if equity > 0:
                self._cached_balance = balance
                self._balance_cache_ts = now
                self.config.equity = equity
                return equity
            logger.warning("Exchange returned zero equity, using cached value")
            return self.config.equity
        except Exception as exc:
            logger.warning(f"Balance fetch failed: {exc}")
            self.last_error = str(exc)
            return self.config.equity

    def _margin_check(self, price: float, qty: float, symbol: str) -> bool:
        self._get_live_equity()
        available = float(self._cached_balance.get("available_balance", 0.0) or 0.0)
        if available <= 0:
            self.order_manager.log_event("WARN", f"No available margin. symbol={symbol}")
            return False
        leverage = getattr(getattr(self.exchange_client, "config", None), "leverage", 50)
        required = (price * qty) / leverage
        usable = available * (1.0 - self.config.margin_safety_pct)
        if required > usable:
            self.order_manager.log_event("WARN", f"Margin check failed. symbol={symbol}")
            return False
        return True

    # ── sizing (single method for all strategies) ────────────────

    def _compute_qty(self, strategy_id: str, atr_val: float, price: float, k: float) -> float:
        equity = self._get_live_equity()
        size = self.volume_manager.compute_size(
            risk_pct=self.risk_manager.config.risk_per_trade_pct,
            equity=equity, atr=atr_val, k=k, price=price,
        )
        if size * price < MIN_NOTIONAL_USD:
            return 0.0
        return size

    # ── lifecycle ────────────────────────────────────────────────

    def start(self, strategies: Optional[List[str]] = None, run_test_trade: bool = True) -> None:
        if self.state in {BotState.TERMINATED, BotState.ERROR}:
            return
        self.enabled_strategies = set(strategies) if strategies else {"trend", "scalp", "candle3"}
        equity = self._get_live_equity()
        logger.info(f"Bot starting | equity=${equity:,.2f} | risk={self.risk_manager.config.risk_per_trade_pct:.1%}")
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
            daily_volume = exchange_volume.get("daily", daily_volume)
        if exchange_stats.get("trade_stats"):
            trade_stats = exchange_stats["trade_stats"]
        if exchange_stats.get("open_trades"):
            open_trades = self._merge_open_trades(exchange_stats["open_trades"], open_trades)
            open_positions_count = len(open_trades)
        if exchange_stats.get("closed_trades"):
            closed_trades = exchange_stats["closed_trades"]

        return BotStatus(
            state=self.state, mode=self._mode,
            daily_volume=daily_volume, daily_target=self.volume_manager.daily_target,
            monthly_volume=monthly_volume, exchange_volume=exchange_volume,
            strategy_volume=self.volume_manager.strategy_volume,
            open_positions=open_positions_count, last_error=self.last_error,
            balance=balance, trade_stats=trade_stats,
            open_trades=open_trades, closed_trades=closed_trades,
            execution_events=self.order_manager.get_events(),
        )

    # ── market data entry point ──────────────────────────────────

    def on_market_data(self, candles_1h, candles_5m, candles_3m, candles_15m, candles_1m, timestamp=None):
        if self.state != BotState.RUNNING or self._test_trade_in_progress or not self._strategies_enabled:
            return
        ts = timestamp or datetime.now(timezone.utc)
        self._get_live_equity()
        if not self.risk_manager.can_trade(self.config.equity, ts):
            return
        if self.position_manager.open_positions_count() > 0:
            self._check_breakeven_all(ts)
            return

        session = self.session_manager.current_session(ts)
        active_strategy = session.strategy_id

        # Normalise input: accept dict-per-symbol or raw lists
        def _as_map(data):
            return data if isinstance(data, dict) else {self.symbols[0]: data}

        c1h = _as_map(candles_1h)
        c5m = _as_map(candles_5m)
        c3m = _as_map(candles_3m)
        c15m = _as_map(candles_15m)
        c1m = _as_map(candles_1m)

        for symbol in self.symbols:
            if active_strategy == "scalp" and "scalp" in self.enabled_strategies:
                self._try_scalp(symbol, c5m.get(symbol, []), c1h.get(symbol, []), ts)
            elif active_strategy == "trend" and "trend" in self.enabled_strategies:
                self._try_trend(symbol, c1h.get(symbol, []), c15m.get(symbol, []), c1m.get(symbol, []), ts)
            elif active_strategy == "candle3" and "candle3" in self.enabled_strategies:
                self._try_candle3(symbol, c3m.get(symbol, []), ts)

    # ── strategy runners ─────────────────────────────────────────

    def _try_scalp(self, symbol: str, candles_5m: list, candles_1h: list, ts: datetime) -> None:
        if self.position_manager.has_open_position(symbol, "scalp"):
            return
        atr_val = atr_from_candles(candles_5m)
        if not atr_val:
            return
        price = candles_5m[-1]["close"] if candles_5m else 0.0
        size = self._compute_qty("scalp", atr_val, price, self.scalp_strategy.config.atr_k)
        size = self.exchange_client.normalize_qty(symbol, size)
        if size <= 0 or not self._margin_check(price, size, symbol):
            return
        signal = self.scalp_strategy.generate_signal(candles_5m, size, symbol, ts, candles_1h=candles_1h)
        if signal:
            self._execute(signal, ts)

    def _try_trend(self, symbol: str, candles_1h: list, candles_15m: list, candles_1m: list, ts: datetime) -> None:
        if self.position_manager.has_open_position(symbol, "trend"):
            return
        self._run_hybrid_trend(symbol, candles_1h, candles_15m, candles_1m, ts)

    def _try_candle3(self, symbol: str, candles_3m: list, ts: datetime) -> None:
        if self.position_manager.has_open_position(symbol, "candle3"):
            return
        signal = self.strategy_c.generate_signal(candles_3m, 1.0, symbol, ts)
        if not signal:
            return
        risk = abs(signal.price - signal.stop_loss)
        if risk <= 0:
            return
        equity = self._get_live_equity()
        size = self.volume_manager.compute_size(
            risk_pct=self.risk_manager.config.risk_per_trade_pct,
            equity=equity, atr=risk, k=1.0, price=signal.price,
        )
        size = self.exchange_client.normalize_qty(symbol, size)
        if size <= 0 or not self._margin_check(signal.price, size, symbol):
            return
        signal = TradeSignal(
            symbol=signal.symbol, strategy_id=signal.strategy_id, side=signal.side,
            timestamp=signal.timestamp, price=signal.price, stop_loss=signal.stop_loss,
            take_profit=signal.take_profit, size=size, reason=signal.reason,
        )
        if self._execute(signal, ts):
            Thread(
                target=self._monitor_strategy_c,
                args=(symbol, "candle3", self.config.strategy_c_hold_seconds),
                daemon=True,
            ).start()

    def _execute(self, signal: TradeSignal, ts: datetime) -> bool:
        try:
            result = self.order_manager.execute_signal(signal, ts)
            return result is not None
        except Exception as exc:
            self.last_error = str(exc)
            self.state = BotState.ERROR
            return False

    # ── breakeven (applies to ALL strategies at 1R) ──────────────

    def _check_breakeven_all(self, ts: datetime) -> None:
        for symbol in self.symbols:
            for strategy_id in ("trend", "scalp"):
                self._apply_breakeven(symbol, strategy_id, ts)

    def _apply_breakeven(self, symbol: str, strategy_id: str, ts: datetime) -> None:
        position = self.position_manager.get_position(symbol, strategy_id)
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
        # Move SL to breakeven (covering fees) once price reaches 1R in profit
        triggered = False
        if position.side.upper() == "BUY" and price >= position.entry_price + r:
            triggered = True
        elif position.side.upper() == "SELL" and price <= position.entry_price - r:
            triggered = True
        if triggered:
            be_price = self.order_manager.breakeven_price(symbol, position.side, position.entry_price, position.size)
            self.position_manager.update_stop_loss(symbol, strategy_id, be_price)
            self.order_manager.log_event("INFO", f"Breakeven moved. {strategy_id} {symbol}")

    # ── hybrid trend logic ───────────────────────────────────────

    def _run_hybrid_trend(self, symbol: str, candles_1h: list, candles_15m: list, candles_1m: list, ts: datetime) -> None:
        bias = self.bias_evaluator.evaluate(candles_1h)
        if bias == "NONE":
            self._last_sweep = None
            return
        sweep = self.sweep_detector.detect(candles_15m, bias)
        if sweep:
            self._last_sweep = sweep
        if not self._last_sweep or self._last_sweep.direction != bias:
            self._last_sweep = None
            return

        last_trade = self._last_trade_time.get("trend")
        if last_trade and (ts - last_trade).total_seconds() < self.config.cooldown_seconds:
            return
        if not self.entry_executor.should_enter(self._last_sweep, candles_1m, bias):
            return
        if not candles_1m:
            return

        last = candles_1m[-1]
        entry_price = last["close"]
        stop_loss = self._last_sweep.level
        risk = abs(entry_price - stop_loss)
        if risk <= 0:
            return

        size = self._compute_qty("trend", risk, entry_price, 1.0)
        size = self.exchange_client.normalize_qty(symbol, size)
        if size <= 0 or not self._margin_check(entry_price, size, symbol):
            return

        # 1:3 RR
        if bias == "LONG":
            take_profit = entry_price + 3 * risk
            side = Side.BUY
        else:
            take_profit = entry_price - 3 * risk
            side = Side.SELL

        signal = TradeSignal(
            symbol=symbol, strategy_id="trend", side=side, timestamp=ts,
            price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
            size=size, reason="hybrid_sweep_entry",
        )
        if self._execute(signal, ts):
            self._last_trade_time["trend"] = ts

    # ── strategy C monitor (hold until next 3m candle) ───────────

    def _monitor_strategy_c(self, symbol: str, strategy_id: str, hold_seconds: int) -> None:
        position = self.position_manager.get_position(symbol, strategy_id)
        if not position:
            return
        deadline = position.opened_at.astimezone(timezone.utc) + timedelta(seconds=hold_seconds)

        while datetime.now(timezone.utc) < deadline:
            pos = self.position_manager.get_position(symbol, strategy_id)
            if not pos:
                return
            try:
                price = self.exchange_client.get_last_price(symbol)
                hit_sl = (
                    (pos.side.upper() == "BUY" and price <= pos.stop_loss)
                    or (pos.side.upper() == "SELL" and price >= pos.stop_loss)
                )
                if hit_sl:
                    self.order_manager.close_position(symbol, strategy_id, price, datetime.now(timezone.utc))
                    return
            except Exception as exc:
                self.last_error = str(exc)
            sleep(1)

        # Time expired — close at market
        for _ in range(3):
            try:
                price = self.exchange_client.get_last_price(symbol)
                self.order_manager.close_position(symbol, strategy_id, price, datetime.now(timezone.utc))
                return
            except Exception as exc:
                self.last_error = str(exc)
                sleep(1)

    # ── test trade ───────────────────────────────────────────────

    def _run_test_trade(self) -> None:
        try:
            sym = self.config.test_trade_symbol
            qty = self.config.test_trade_qty
            entry_price = self.exchange_client.get_last_price(sym)
            self.exchange_client.create_order(symbol=sym, side="buy", order_type="market", amount=qty)
            self.position_manager.open_position(Position(
                symbol=sym, strategy_id="test", side="BUY", size=qty,
                entry_price=entry_price, stop_loss=entry_price * 0.99 if entry_price else 0.0,
                take_profit=None, opened_at=datetime.now(timezone.utc),
            ))
            sleep(5)
            exit_price = self.exchange_client.get_last_price(sym) or entry_price
            self.exchange_client.close_position(symbol=sym, side="sell", amount=qty)
            trade = self.position_manager.close_position_with_price(sym, "test", exit_price, datetime.now(timezone.utc))
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

    # ── helpers ──────────────────────────────────────────────────

    def _merge_open_trades(self, exchange_trades: List[dict], local_trades: List[dict]) -> List[dict]:
        local_map = {
            (t.get("symbol"), (t.get("side") or "").upper()): t
            for t in local_trades
        }
        merged = []
        for trade in exchange_trades:
            key = (trade.get("symbol"), (trade.get("side") or "").upper())
            local = local_map.get(key, {})
            merged.append({
                **trade,
                "strategy_id": local.get("strategy_id"),
                "stop_loss": local.get("stop_loss"),
                "take_profit": local.get("take_profit"),
            })
        return merged

    # ── polling loop ─────────────────────────────────────────────

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
                    data = {tf: {s: self.exchange_client.fetch_ohlcv(s, tf, 300) for s in self.symbols}
                            for tf in ("1h", "15m", "5m", "3m", "1m")}
                    self._mode = BotMode.SCANNING
                    self.on_market_data(
                        data["1h"], data["5m"], data["3m"], data["15m"], data["1m"],
                        datetime.now(timezone.utc),
                    )
                except Exception as exc:
                    self.last_error = str(exc)
                    self._mode = BotMode.IDLE
                    sleep(120)
                    continue
            sleep(self.config.poll_interval_seconds)
