"""Integration test: full bot loop with mock exchange + synthetic data.

Tests the complete signal → sizing → execution → position → breakeven pipeline
without any network calls.
"""
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from exchange.types import OrderResult
from execution.position_manager import Position
from main import TradingBot, BotConfig
from models.status import BotState
from tests.market_data import (
    btc_uptrend_5m, btc_uptrend_1h, btc_ranging_5m, btc_ranging_1h,
    btc_3bull_3m, btc_choppy_5m, make_candles, T0, uptrend, ranging,
)


class MockExchange:
    """Minimal mock implementing the ExchangeClient protocol."""

    def __init__(self, price: float = 85000.0):
        self._price = price
        self.orders: List[Dict] = []
        self.closes: List[Dict] = []

    def create_order(self, symbol, side, order_type, amount, price=None, params=None) -> OrderResult:
        self.orders.append({"symbol": symbol, "side": side, "amount": amount, "params": params})
        return OrderResult(order_id="mock-001", status="closed", filled=float(amount), average_price=self._price)

    def close_position(self, symbol, side, amount, params=None) -> OrderResult:
        self.closes.append({"symbol": symbol, "side": side, "amount": amount})
        return OrderResult(order_id="mock-002", status="closed", filled=float(amount), average_price=self._price)

    def fetch_ohlcv(self, symbol, timeframe, limit=200) -> List[Dict[str, Any]]:
        return []

    def get_balance(self) -> Dict[str, float]:
        return {"total_equity": 500.0, "available_balance": 400.0}

    def get_last_price(self, symbol) -> float:
        return self._price

    def normalize_qty(self, symbol, qty) -> float:
        # Mimic BTCUSDT step of 0.001
        return round(int(qty * 1000) / 1000, 3)

    def get_exchange_stats(self, symbols) -> Dict[str, Any]:
        return {}


class TestBotIntegration(unittest.TestCase):
    def setUp(self):
        self.exchange = MockExchange()
        self.config = BotConfig(
            symbols=["BTCUSDT"],
            equity=500.0,
            monthly_volume_target=1_000_000,
            poll_interval_seconds=9999,  # don't auto-poll
        )
        self.bot = TradingBot(self.config, self.exchange)
        self.bot.state = BotState.RUNNING
        self.bot._strategies_enabled = True

    def test_session_routing(self):
        """Each session routes to the correct strategy."""
        ts_asia = datetime(2026, 3, 1, 3, 0, tzinfo=timezone.utc)
        ts_london = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
        ts_ny = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)

        session_asia = self.bot.session_manager.current_session(ts_asia)
        session_london = self.bot.session_manager.current_session(ts_london)
        session_ny = self.bot.session_manager.current_session(ts_ny)

        self.assertEqual(session_asia.strategy_id, "candle3")
        self.assertEqual(session_london.strategy_id, "scalp")
        self.assertEqual(session_ny.strategy_id, "trend")

    def test_no_trade_while_position_open(self):
        """Bot should not open a new position when one is already open."""
        pos = Position(
            symbol="BTCUSDT", strategy_id="scalp", side="BUY",
            size=0.001, entry_price=85000.0, stop_loss=84500.0,
            take_profit=86500.0, opened_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.bot.position_manager.open_position(pos)

        ts = datetime(2026, 3, 1, 10, 5, tzinfo=timezone.utc)
        c5m = btc_ranging_5m()
        c1h = btc_ranging_1h()
        c3m = btc_3bull_3m()

        initial_orders = len(self.exchange.orders)
        self.bot.on_market_data(c1h, c5m, c3m, c1h, c5m, ts)
        self.assertEqual(len(self.exchange.orders), initial_orders)

    def test_risk_manager_blocks(self):
        """After 3 consecutive losses, bot should not trade."""
        self.bot.risk_manager.start_day(500.0, datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc))
        self.bot.risk_manager.register_pnl(-1.0)
        self.bot.risk_manager.register_pnl(-1.0)
        self.bot.risk_manager.register_pnl(-1.0)

        ts = datetime(2026, 3, 1, 10, 5, tzinfo=timezone.utc)
        c5m = btc_ranging_5m()
        c1h = btc_ranging_1h()

        initial_orders = len(self.exchange.orders)
        self.bot.on_market_data(c1h, c5m, c5m, c1h, c5m, ts)
        self.assertEqual(len(self.exchange.orders), initial_orders)

    def test_bot_lifecycle(self):
        """Start → pause → stop → state transitions."""
        self.bot.state = BotState.STOPPED
        self.bot.start(run_test_trade=False)
        self.assertEqual(self.bot.state, BotState.RUNNING)

        self.bot.pause()
        self.assertEqual(self.bot.state, BotState.PAUSED)

        self.bot.state = BotState.RUNNING  # resume
        self.bot.stop()
        self.assertEqual(self.bot.state, BotState.STOPPED)

    def test_status_returns_complete(self):
        """Status should return all fields without error."""
        status = self.bot.status()
        self.assertIsNotNone(status.state)
        self.assertIsNotNone(status.mode)
        self.assertIsInstance(status.daily_volume, float)
        self.assertIsInstance(status.daily_target, float)

    def test_volume_tracking(self):
        """After a trade, volume should be registered."""
        ts = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        self.bot.volume_manager.register_trade("scalp", 85000 * 0.001, ts)
        self.assertGreater(self.bot.volume_manager.daily_volume, 0)

    def test_on_market_data_accepts_dicts_and_lists(self):
        """on_market_data should accept both dict-per-symbol and raw lists."""
        ts = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
        c5m = btc_ranging_5m()
        c1h = btc_ranging_1h()
        # Raw lists
        self.bot.on_market_data(c1h, c5m, c5m, c1h, c5m, ts)
        # Dict-per-symbol
        self.bot.on_market_data(
            {"BTCUSDT": c1h}, {"BTCUSDT": c5m}, {"BTCUSDT": c5m},
            {"BTCUSDT": c1h}, {"BTCUSDT": c5m}, ts,
        )


class TestBacktestEngine(unittest.TestCase):
    """Test the backtest engine components with synthetic data."""

    def test_full_sim_uptrend(self):
        """Run a mini simulation on uptrend data and verify basic sanity."""
        from backtest.engine import (
            BacktestConfig, Trade, check_sl_tp, close_trade, compute_size,
            mean_reversion_signal, trend_breakout_signal,
        )

        # 1h candles need 202+ bars for EMA200, 5m is the walk-through timeframe
        candles_1h = make_candles(T0, 60, uptrend(500))
        candles_5m = make_candles(T0, 5, uptrend(500))

        equity = 500.0
        trades = []
        open_trade = None

        for i in range(22, len(candles_5m)):
            candle = candles_5m[i]
            ts = datetime.fromtimestamp(candle["timestamp"] / 1000.0, tz=timezone.utc)

            if open_trade:
                exit_px = check_sl_tp(open_trade, candle)
                if exit_px is not None:
                    close_trade(open_trade, exit_px, ts, 0.00055)
                    equity += open_trade.pnl
                    trades.append(open_trade)
                    open_trade = None
                    if equity <= 0:
                        break
                continue

            c5m_win = candles_5m[max(0, i - 200):i + 1]

            # Only test mean reversion here (doesn't need 200+ 1h bars)
            signal = mean_reversion_signal(c5m_win, [])
            if signal:
                size = compute_size(0.01, equity, signal["atr"], 2.0, signal["price"])
                if size > 0:
                    open_trade = Trade(
                        strategy=signal["strategy"], side=signal["side"],
                        entry_price=signal["price"], stop_loss=signal["sl"],
                        take_profit=signal["tp"], size=size, entry_time=ts,
                    )

        # This is a structural test — it should run without crashing.
        # On a pure uptrend with no 1h filter, MR may still not fire (no BB touch),
        # which is correct behavior.
        self.assertGreaterEqual(equity, 0, "Simulation should not produce negative equity")

    def test_full_sim_ranging(self):
        """Run a mini simulation on ranging data."""
        from backtest.engine import (
            Trade, check_sl_tp, close_trade, compute_size,
            mean_reversion_signal,
        )

        candles_5m = make_candles(T0, 5, ranging(300))
        candles_1h = make_candles(T0, 60, ranging(300))

        equity = 500.0
        trades = []
        open_trade = None

        for i in range(22, len(candles_5m)):
            candle = candles_5m[i]
            ts = datetime.fromtimestamp(candle["timestamp"] / 1000.0, tz=timezone.utc)

            if open_trade:
                exit_px = check_sl_tp(open_trade, candle)
                if exit_px is not None:
                    close_trade(open_trade, exit_px, ts, 0.00055)
                    equity += open_trade.pnl
                    trades.append(open_trade)
                    open_trade = None
                continue

            c1h_win = [c for c in candles_1h if c["timestamp"] <= candle["timestamp"]][-250:]
            c5m_win = candles_5m[max(0, i - 200):i + 1]

            signal = mean_reversion_signal(c5m_win, c1h_win)
            if signal:
                size = compute_size(0.01, equity, signal["atr"], 2.0, signal["price"])
                if size > 0:
                    open_trade = Trade(
                        strategy=signal["strategy"], side=signal["side"],
                        entry_price=signal["price"], stop_loss=signal["sl"],
                        take_profit=signal["tp"], size=size, entry_time=ts,
                    )

        # Should not blow up account on ranging data
        self.assertGreater(equity, 0, "Equity should remain positive in ranging market")


if __name__ == "__main__":
    unittest.main()
