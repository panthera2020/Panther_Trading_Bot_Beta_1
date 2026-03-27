from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict
import logging
from collections import deque

from exchange.base import ExchangeClient
from execution.exchange_info import ExchangeInfo
from execution.position_manager import Position, PositionManager
from execution.risk_manager import RiskManager
from execution.volume_manager import VolumeManager
from execution.qty_utils import normalize_qty, reduce_by_step
from models.signal import TradeSignal

logger = logging.getLogger(__name__)

@dataclass
class OrderManagerConfig:
    symbol: str
    order_type: str = "market"
    fee_safety_multiplier: float = 1.5


class OrderManager:
    def __init__(
        self,
        client: ExchangeClient,
        position_manager: PositionManager,
        risk_manager: RiskManager,
        volume_manager: VolumeManager,
        config: OrderManagerConfig,
    ):
        self.client = client
        self.position_manager = position_manager
        self.risk_manager = risk_manager
        self.volume_manager = volume_manager
        self.config = config
        self.exchange_info = ExchangeInfo(client)
        self._event_log: deque[Dict[str, str]] = deque(maxlen=50)

    def _log_event(self, level: str, message: str) -> None:
        self._event_log.append(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "level": level,
                "message": message,
            }
        )

    def get_events(self) -> List[Dict[str, str]]:
        return list(self._event_log)

    def log_event(self, level: str, message: str) -> None:
        self._log_event(level, message)

    def execute_signal(self, signal: TradeSignal, timestamp: datetime) -> Optional[str]:
        if self._has_exchange_position(signal.symbol):
            msg = f"Skipping trade: exchange position already open. symbol={signal.symbol}"
            logger.info(msg)
            self._log_event("INFO", msg)
            return None
        if self.position_manager.has_open_position(signal.symbol, signal.strategy_id):
            return None
        self.risk_manager.register_order(timestamp)

        rules = self.exchange_info.get_rules(signal.symbol)
        qty_str = normalize_qty(signal.size, rules.min_qty, rules.qty_step)
        if qty_str == "0":
            msg = f"Skipping trade: qty below min. symbol={signal.symbol}"
            logger.info(msg)
            self._log_event("INFO", msg)
            return None

        if not self._fee_safe(signal, float(qty_str), rules.taker_fee):
            qty_str = reduce_by_step(qty_str, rules.qty_step)
            if qty_str == "0" or not self._fee_safe(signal, float(qty_str), rules.taker_fee):
                msg = f"Skipping trade: fee safety. symbol={signal.symbol}"
                logger.info(msg)
                self._log_event("INFO", msg)
                return None

        params = {}
        if signal.stop_loss and signal.stop_loss > 0:
            params["stopLoss"] = str(signal.stop_loss)
        if signal.take_profit and signal.take_profit > 0:
            params["takeProfit"] = str(signal.take_profit)

        try:
            result = self.client.create_order(
                symbol=signal.symbol,
                side=signal.side.value.lower(),
                order_type=self.config.order_type,
                amount=qty_str,
                price=None,
                params=params,
            )
        except Exception as exc:
            result = self._retry_on_error(exc, signal, rules, qty_str, params)
            if result is None:
                raise
        if result.status in {"open", "closed"}:
            position = Position(
                symbol=signal.symbol,
                strategy_id=signal.strategy_id,
                side=signal.side.value,
                size=float(qty_str),
                entry_price=signal.price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                opened_at=signal.timestamp,
            )
            self.position_manager.open_position(position)
            notional = signal.price * float(qty_str)
            self.volume_manager.register_trade(signal.strategy_id, notional, timestamp)
            # Ensure TP/SL are set on exchange (fallback if order params ignored).
            if hasattr(self.client, "set_trading_stop"):
                try:
                    self.client.set_trading_stop(
                        symbol=signal.symbol,
                        side=signal.side.value.lower(),
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                    )
                except Exception as exc:
                    msg = f"TP/SL set failed. symbol={signal.symbol} err={exc}"
                    logger.info(msg)
                    self._log_event("WARN", msg)
            return result.order_id
        return None

    def _retry_on_error(self, exc: Exception, signal: TradeSignal, rules, qty_str: str, params: dict):
        message = str(exc)
        if "ErrCode: 10001" in message:
            self.exchange_info._rules_cache.pop(signal.symbol, None)
            rules = self.exchange_info.get_rules(signal.symbol)
            qty_str = normalize_qty(signal.size, rules.min_qty, rules.qty_step)
            self._log_event("WARN", f"Retry: qty invalid, renormalized. symbol={signal.symbol}")
        elif "110017" in message:
            qty_str = reduce_by_step(qty_str, rules.qty_step)
            self._log_event("WARN", f"Retry: precision adjust. symbol={signal.symbol}")
        elif "Read timed out" in message or "Timeout" in message:
            self._log_event("WARN", f"Retry: timeout. symbol={signal.symbol}")
            pass
        elif "ErrCode: 110007" in message:
            # Insufficient balance: reduce qty by steps and retry.
            for _ in range(3):
                qty_str = reduce_by_step(qty_str, rules.qty_step)
                if qty_str == "0":
                    break
                self._log_event("WARN", f"Retry: insufficient balance, reduced qty. symbol={signal.symbol}")
                try:
                    return self.client.create_order(
                        symbol=signal.symbol,
                        side=signal.side.value.lower(),
                        order_type=self.config.order_type,
                        amount=qty_str,
                        price=None,
                        params=params,
                    )
                except Exception:
                    continue
            return None
        else:
            return None

        if qty_str == "0":
            msg = f"Retry aborted: qty below min. symbol={signal.symbol}"
            logger.info(msg)
            self._log_event("INFO", msg)
            return None

        msg = f"Retrying order. symbol={signal.symbol}"
        logger.info(msg)
        self._log_event("INFO", msg)
        return self.client.create_order(
            symbol=signal.symbol,
            side=signal.side.value.lower(),
            order_type=self.config.order_type,
            amount=qty_str,
            price=None,
            params=params,
        )

    def _fee_safe(self, signal: TradeSignal, qty: float, taker_fee: float) -> bool:
        fee_cost = 2 * signal.price * qty * taker_fee
        expected_move_value = abs(signal.price - signal.stop_loss) * qty
        return expected_move_value > fee_cost * self.config.fee_safety_multiplier

    def _has_exchange_position(self, symbol: str) -> bool:
        session = getattr(self.client, "_session", None)
        category = getattr(getattr(self.client, "config", None), "category", "linear")
        if session is None:
            return False
        try:
            resp = session.get_positions(category=category, symbol=symbol)
            positions = resp.get("result", {}).get("list", [])
            for pos in positions:
                size = float(pos.get("size", 0.0) or 0.0)
                if size != 0:
                    return True
        except Exception:
            return False
        return False

    def breakeven_price(self, symbol: str, side: str, entry_price: float, qty: float) -> float:
        rules = self.exchange_info.get_rules(symbol)
        fee_cost = 2 * entry_price * qty * rules.taker_fee
        fee_per_unit = fee_cost / max(qty, 1e-9)
        if side.upper() == "BUY":
            return entry_price + fee_per_unit
        return entry_price - fee_per_unit

    def close_position(self, symbol: str, strategy_id: str, exit_price: float, timestamp: datetime) -> None:
        position = self.position_manager.get_position(symbol, strategy_id)
        if not position:
            return
        close_side = "sell" if position.side.upper() == "BUY" else "buy"
        self.client.close_position(
            symbol=symbol,
            side=close_side,
            amount=position.size,
        )
        trade = self.position_manager.close_position_with_price(symbol, strategy_id, exit_price, timestamp)
        if trade:
            self.risk_manager.register_pnl(trade.pnl)
