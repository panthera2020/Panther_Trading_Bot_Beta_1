from __future__ import annotations

from decimal import Decimal, ROUND_DOWN


def normalize_qty(raw_qty: float | str, min_qty: str, step: str) -> str:
    """
    Normalize quantity using Decimal math.
    - Floors to nearest step
    - Enforces min quantity
    - Strips trailing zeros
    """
    qty = Decimal(str(raw_qty))
    min_dec = Decimal(str(min_qty))
    step_dec = Decimal(str(step))

    if qty <= 0:
        return "0"
    if qty < min_dec:
        return "0"
    if step_dec > 0:
        steps = (qty / step_dec).to_integral_value(rounding=ROUND_DOWN)
        qty = steps * step_dec
    if qty < min_dec:
        return "0"
    return _strip_zeros(qty)


def reduce_by_step(qty_str: str, step: str) -> str:
    qty = Decimal(qty_str)
    step_dec = Decimal(str(step))
    if step_dec <= 0:
        return qty_str
    qty = qty - step_dec
    if qty <= 0:
        return "0"
    return _strip_zeros(qty)


def _strip_zeros(value: Decimal) -> str:
    normalized = value.normalize()
    # Avoid scientific notation for whole numbers
    if normalized == normalized.to_integral():
        return format(normalized, "f").split(".")[0]
    return format(normalized, "f").rstrip("0").rstrip(".")
