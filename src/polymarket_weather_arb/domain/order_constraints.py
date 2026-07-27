"""Local order size/price preflight against exchange minima (no SDK call).

Polymarket marketable BUY minimum notional is $1 USDC. Markets may also expose
``orderMinSize`` / tick size in Gamma/CLOB payloads. Quantization must not turn
an accepted configured amount into a sub-minimum order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

# The official ``polymarket-client`` signs limit-order share sizes at two
# decimal places for every supported price tick. A finer local step can pass
# preflight and then be rounded down by the SDK, turning $1.0000 into $0.999x.
SDK_LIMIT_ORDER_SIZE_STEP = Decimal("0.01")
DEFAULT_SIZE_STEP = SDK_LIMIT_ORDER_SIZE_STEP
DEFAULT_PRICE_TICK = Decimal("0.001")
# Incident: SDK rejected $0.9996 as below $1 marketable BUY minimum.
MARKETABLE_MIN_NOTIONAL = Decimal("1")


@dataclass(frozen=True)
class OrderConstraints:
    min_notional: Decimal
    min_size: Decimal | None
    size_step: Decimal
    price_tick: Decimal


@dataclass(frozen=True)
class NormalizedOrderSize:
    size: Decimal
    notional: Decimal
    limit_price: Decimal
    adjusted: bool
    reason: str | None = None


def extract_order_constraints(payload: dict[str, Any] | None) -> OrderConstraints:
    payload = payload if isinstance(payload, dict) else {}
    min_size = _decimal(
        payload.get("orderMinSize")
        or payload.get("order_min_size")
        or payload.get("minimum_order_size")
        or payload.get("min_order_size")
    )
    size_step = _decimal(
        payload.get("orderSizeStep") or payload.get("size_step")
    )
    # Prefer explicit size precision; fall back to default share step.
    # Do not reuse minimum_tick_size as size_step (that field is a price tick).
    if size_step is None or size_step <= 0:
        size_step = DEFAULT_SIZE_STEP
    else:
        size_step = max(size_step, SDK_LIMIT_ORDER_SIZE_STEP)
    price_tick = _decimal(
        payload.get("minimum_tick_size")
        or payload.get("minimumTickSize")
        or payload.get("tickSize")
        or payload.get("tick_size")
    )
    if price_tick is None or price_tick <= 0:
        price_tick = DEFAULT_PRICE_TICK
    explicit_min_notional = _decimal(
        payload.get("orderMinNotional") or payload.get("min_notional")
    )
    min_notional = (
        explicit_min_notional
        if explicit_min_notional is not None and explicit_min_notional > 0
        else MARKETABLE_MIN_NOTIONAL
    )
    return OrderConstraints(
        min_notional=min_notional,
        min_size=min_size if min_size and min_size > 0 else None,
        size_step=size_step,
        price_tick=price_tick,
    )


def normalize_buy_order(
    *,
    limit_price: Decimal,
    max_notional: Decimal,
    constraints: OrderConstraints | None = None,
) -> NormalizedOrderSize | None:
    """Build a size that respects min notional / min size without exceeding max_notional.

    Returns None when the exchange minimum cannot be satisfied under the cap.
    """
    c = constraints or OrderConstraints(
        min_notional=MARKETABLE_MIN_NOTIONAL,
        min_size=None,
        size_step=DEFAULT_SIZE_STEP,
        price_tick=DEFAULT_PRICE_TICK,
    )
    if limit_price <= 0 or limit_price >= 1:
        return None
    if max_notional <= 0:
        return None

    # Snap price down onto tick (buyer-favorable for BUY limit). Clamp the size
    # step even for manually constructed constraints used by older callers.
    size_step = max(c.size_step, SDK_LIMIT_ORDER_SIZE_STEP)
    price = limit_price.quantize(c.price_tick, rounding=ROUND_DOWN)
    if price <= 0 or price >= 1:
        return None

    size = (max_notional / price).quantize(size_step, rounding=ROUND_DOWN)
    adjusted = False
    if c.min_size is not None and size < c.min_size:
        size = c.min_size.quantize(size_step, rounding=ROUND_UP)
        adjusted = True
    notional = size * price

    if notional < c.min_notional:
        needed = (c.min_notional / price).quantize(size_step, rounding=ROUND_UP)
        if c.min_size is not None and needed < c.min_size:
            needed = c.min_size.quantize(size_step, rounding=ROUND_UP)
        needed_notional = needed * price
        if needed_notional > max_notional:
            return None
        size = needed
        notional = needed_notional
        adjusted = True

    if size <= 0 or notional <= 0:
        return None
    if notional > max_notional:
        return None
    return NormalizedOrderSize(
        size=size,
        notional=notional,
        limit_price=price,
        adjusted=adjusted,
        reason="normalized to exchange minimum" if adjusted else None,
    )


def residual_is_dust(
    *,
    residual_size: Decimal,
    price: Decimal | None = None,
    constraints: OrderConstraints | None = None,
    min_notional: Decimal | None = None,
) -> tuple[bool, str]:
    """True when residual cannot be sold under exchange minima.

    SELL dust is driven primarily by ``orderMinSize``. The $1 marketable BUY
    floor is not applied to residual SELLs unless ``min_notional`` is passed
    explicitly (market-specific payload value).
    """
    c = constraints or extract_order_constraints(None)
    if residual_size <= 0:
        return True, "residual size is zero"
    if c.min_size is not None and residual_size < c.min_size:
        return True, f"residual {residual_size} below orderMinSize {c.min_size}"
    if min_notional is not None and price is not None and price > 0:
        notional = residual_size * price
        if notional < min_notional:
            return True, (
                f"residual notional {notional} below marketable minimum {min_notional}"
            )
    return False, ""


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
