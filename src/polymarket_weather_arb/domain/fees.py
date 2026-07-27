"""Polymarket fee helpers for weather (and related) markets.

Official docs (https://docs.polymarket.com/trading/fees, 2026-07):

    fee = C × feeRate × p × (1 - p)

where C = shares traded, p = share price. Makers are never charged. Weather
taker feeRate = 0.05. Fees round to 5 decimal places (USDC); amounts smaller
than 0.00001 USDC become zero.

SDK ``getClobMarketInfo`` exposes ``fd = {r, e, to}`` (rate, exponent,
taker-only). Weather markets use the share-based curve above with exponent
behavior that matches the documented product form (effective fee scales with
p*(1-p)); we do not invent an alternate exponent curve.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

# Category rates from Polymarket fee documentation.
CATEGORY_TAKER_FEE_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "weather_fees": Decimal("0.05"),
    "other": Decimal("0.05"),
    "general": Decimal("0.05"),
    "other_general": Decimal("0.05"),
    "mentions": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "geopolitics": Decimal("0"),
}

FEE_QUANTUM = Decimal("0.00001")
WEATHER_TAKER_FEE_RATE = Decimal("0.05")


@dataclass(frozen=True)
class MarketFeeSchedule:
    fees_enabled: bool
    fee_type: str | None
    fee_rate: Decimal | None
    source: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class FillFeeResolution:
    fee: Decimal
    role: str  # taker | maker | unknown
    method: str  # exchange_reported | formula | maker_zero | none
    fee_rate: Decimal | None
    evidence: dict[str, Any]


def extract_market_fee_schedule(payload: dict[str, Any] | None) -> MarketFeeSchedule:
    payload = payload if isinstance(payload, dict) else {}
    fees_enabled = _truthy(payload.get("feesEnabled") or payload.get("fees_enabled"))
    fee_type = _string(payload.get("feeType") or payload.get("fee_type"))
    schedule = payload.get("feeSchedule") or payload.get("fee_schedule") or {}
    if not isinstance(schedule, dict):
        schedule = {}

    rate: Decimal | None = None
    source = "none"
    # Prefer explicit schedule rate, then feeType category, then weather default when enabled.
    for key in ("r", "rate", "feeRate", "takerFeeRate", "taker_fee_rate"):
        raw_rate = schedule.get(key)
        if raw_rate is None and key in payload:
            raw_rate = payload.get(key)
        if raw_rate is not None and raw_rate != "":
            try:
                rate = Decimal(str(raw_rate))
                source = f"schedule.{key}"
                break
            except Exception:
                pass
    if rate is None and fee_type:
        normalized = fee_type.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in CATEGORY_TAKER_FEE_RATES:
            rate = CATEGORY_TAKER_FEE_RATES[normalized]
            source = f"feeType:{normalized}"
        elif "weather" in normalized:
            rate = WEATHER_TAKER_FEE_RATE
            source = f"feeType_weather:{normalized}"
    if fees_enabled and rate is None:
        # Conservative default for fee-enabled weather markets when schedule incomplete.
        rate = WEATHER_TAKER_FEE_RATE
        source = "default_weather_enabled"
    if not fees_enabled:
        rate = Decimal("0") if rate is None else rate
        source = source if source != "none" else "fees_disabled"

    return MarketFeeSchedule(
        fees_enabled=bool(fees_enabled),
        fee_type=fee_type,
        fee_rate=rate,
        source=source,
        raw={
            "feesEnabled": fees_enabled,
            "feeType": fee_type,
            "feeSchedule": schedule,
        },
    )


def compute_taker_fee(
    *,
    shares: Decimal,
    price: Decimal,
    fee_rate: Decimal,
) -> Decimal:
    """Official taker fee in USDC: C * feeRate * p * (1-p), rounded to 5 dp."""
    if shares <= 0 or fee_rate <= 0:
        return Decimal("0")
    p = price
    if p < 0:
        p = Decimal("0")
    if p > 1:
        p = Decimal("1")
    raw = shares * fee_rate * p * (Decimal("1") - p)
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def expected_taker_fee_per_share(*, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Per-share fee in probability units (USDC per share)."""
    if fee_rate <= 0:
        return Decimal("0")
    p = price
    if p < 0:
        p = Decimal("0")
    if p > 1:
        p = Decimal("1")
    return (fee_rate * p * (Decimal("1") - p)).quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP)


def expected_buy_fee(
    *,
    shares: Decimal,
    price: Decimal,
    market_payload: dict[str, Any] | None,
) -> Decimal:
    """Conservative entry fee reserved before a BUY reaches the exchange."""
    schedule = extract_market_fee_schedule(market_payload)
    if not schedule.fees_enabled:
        return Decimal("0")
    return compute_taker_fee(
        shares=shares,
        price=price,
        fee_rate=schedule.fee_rate or WEATHER_TAKER_FEE_RATE,
    )


def fee_adjusted_buy_notional_cap(
    *,
    cash_cap: Decimal,
    price: Decimal,
    market_payload: dict[str, Any] | None,
) -> Decimal:
    """Maximum BUY principal whose principal plus entry fee stays under cash_cap."""
    if cash_cap <= 0 or price <= 0 or price >= 1:
        return Decimal("0")
    schedule = extract_market_fee_schedule(market_payload)
    if not schedule.fees_enabled:
        return cash_cap
    rate = schedule.fee_rate or WEATHER_TAKER_FEE_RATE
    # shares * price + shares * rate * price * (1-price)
    # = notional * (1 + rate * (1-price)).
    return cash_cap / (Decimal("1") + rate * (Decimal("1") - price))


def resolve_fill_fee(
    *,
    fill: dict[str, Any],
    account_fill: dict[str, Any],
    market_payload: dict[str, Any] | None,
    known_order_ids: set[str],
) -> FillFeeResolution:
    """Resolve account fee for a reconciled trade leg.

    Prefer exchange-reported fee when present and positive. Otherwise compute
    from market fee schedule when the account is the taker. Makers stay zero.
    Unknown role with fees enabled uses a conservative taker estimate.
    """
    schedule = extract_market_fee_schedule(market_payload)
    role = _infer_trade_role(fill, account_fill, known_order_ids)
    reported = _reported_fee(account_fill) or _reported_fee(fill)
    evidence: dict[str, Any] = {
        "role": role,
        "fees_enabled": schedule.fees_enabled,
        "fee_type": schedule.fee_type,
        "fee_rate_source": schedule.source,
        "fee_rate": str(schedule.fee_rate) if schedule.fee_rate is not None else None,
        "reported_fee": str(reported) if reported is not None else None,
        "schedule_raw": schedule.raw,
    }

    if not schedule.fees_enabled and (schedule.fee_rate is None or schedule.fee_rate == 0):
        fee = reported if reported is not None else Decimal("0")
        return FillFeeResolution(
            fee=fee,
            role=role,
            method="exchange_reported" if reported is not None else "none",
            fee_rate=schedule.fee_rate,
            evidence=evidence,
        )

    if reported is not None and reported > 0:
        return FillFeeResolution(
            fee=reported,
            role=role,
            method="exchange_reported",
            fee_rate=schedule.fee_rate,
            evidence=evidence,
        )

    if role == "maker":
        return FillFeeResolution(
            fee=Decimal("0"),
            role="maker",
            method="maker_zero",
            fee_rate=schedule.fee_rate,
            evidence=evidence,
        )

    price = _decimal_field(account_fill, "price") or _decimal_field(fill, "price")
    size = (
        _decimal_field(account_fill, "size", "quantity", "matched_amount")
        or _decimal_field(fill, "size", "quantity", "matched_amount")
    )
    rate = schedule.fee_rate or Decimal("0")
    if price is None or size is None or rate <= 0:
        # Incomplete metadata: do not silently treat as free when fees are on.
        if schedule.fees_enabled:
            evidence["incomplete"] = True
            # Zero size/price cannot compute; keep zero but mark unknown method.
            return FillFeeResolution(
                fee=Decimal("0"),
                role=role if role != "unknown" else "unknown",
                method="unknown_incomplete",
                fee_rate=schedule.fee_rate,
                evidence=evidence,
            )
        return FillFeeResolution(
            fee=Decimal("0"),
            role=role,
            method="none",
            fee_rate=schedule.fee_rate,
            evidence=evidence,
        )

    computed = compute_taker_fee(shares=size, price=price, fee_rate=rate)
    method = "formula" if role == "taker" else "formula_conservative_unknown_role"
    evidence["computed_fee"] = str(computed)
    return FillFeeResolution(
        fee=computed,
        role=role,
        method=method,
        fee_rate=rate,
        evidence=evidence,
    )


def _infer_trade_role(
    fill: dict[str, Any],
    account_fill: dict[str, Any],
    known_order_ids: set[str],
) -> str:
    if not known_order_ids:
        # Without our order ids we cannot prove role.
        explicit = _string(account_fill.get("trader_side") or fill.get("trader_side") or fill.get("role"))
        if explicit:
            lowered = explicit.lower()
            if "maker" in lowered:
                return "maker"
            if "taker" in lowered:
                return "taker"
        return "unknown"

    taker_order_id = _string(
        fill.get("taker_order_id")
        or fill.get("order_id")
        or fill.get("orderId")
        or fill.get("orderID")
        or account_fill.get("order_id")
    )
    if taker_order_id and taker_order_id in known_order_ids:
        return "taker"

    maker_orders = fill.get("maker_orders")
    if isinstance(maker_orders, list):
        for maker_order in maker_orders:
            if not isinstance(maker_order, dict):
                continue
            oid = _string(
                maker_order.get("order_id")
                or maker_order.get("orderId")
                or maker_order.get("orderID")
                or maker_order.get("id")
            )
            if oid and oid in known_order_ids:
                return "maker"

    account_oid = _string(account_fill.get("order_id"))
    if account_oid and account_oid in known_order_ids:
        # Account view resolved to a known order but not as taker above.
        return "maker" if isinstance(maker_orders, list) else "unknown"
    return "unknown"


def _reported_fee(payload: dict[str, Any]) -> Decimal | None:
    for key in (
        "fee",
        "fee_amount",
        "feeAmount",
        "fee_usdc",
        "feeUsdc",
        "taker_fee",
        "takerFee",
    ):
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return Decimal(str(value))
        except Exception:
            continue
    return None


def _decimal_field(payload: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return Decimal(str(value))
        except Exception:
            continue
    return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}
