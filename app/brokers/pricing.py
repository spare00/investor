"""US equity price tick helpers for broker submissions."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def round_equity_price(price: float | None) -> float | None:
    """Round to Alpaca-valid equity increments.

    - price >= $1 → $0.01
    - price < $1 → $0.0001
    """
    if price is None:
        return None
    value = float(price)
    if value <= 0:
        return value
    quant = Decimal("0.01") if value >= 1.0 else Decimal("0.0001")
    rounded = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    return float(rounded)
