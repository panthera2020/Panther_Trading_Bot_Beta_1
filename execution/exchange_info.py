from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class InstrumentRules:
    min_qty: str
    qty_step: str
    taker_fee: float


class ExchangeInfo:
    """
    Light wrapper around exchange client to cache instrument rules and fees.
    Uses duck-typing to avoid modifying exchange layer.
    """

    def __init__(self, client: Any):
        self.client = client
        self._rules_cache: Dict[str, InstrumentRules] = {}

    def get_rules(self, symbol: str) -> InstrumentRules:
        if symbol in self._rules_cache:
            return self._rules_cache[symbol]

        min_qty = "0.001"
        qty_step = "0.001"
        taker_fee = 0.0006

        # Try to fetch instrument info from Bybit SDK if available.
        session = getattr(self.client, "_session", None)
        category = getattr(getattr(self.client, "config", None), "category", "linear")
        if session is not None:
            try:
                info = session.get_instruments_info(category=category, symbol=symbol)
                items = info.get("result", {}).get("list", [])
                if items:
                    lot = items[0].get("lotSizeFilter", {})
                    min_qty = str(lot.get("minOrderQty", min_qty))
                    qty_step = str(lot.get("qtyStep", qty_step))
            except Exception:
                pass
            try:
                fees = session.get_fee_rates(category=category, symbol=symbol)
                items = fees.get("result", {}).get("list", [])
                if items:
                    taker_fee = float(items[0].get("takerFeeRate", taker_fee))
            except Exception:
                pass

        rules = InstrumentRules(min_qty=min_qty, qty_step=qty_step, taker_fee=taker_fee)
        self._rules_cache[symbol] = rules
        return rules
