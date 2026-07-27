from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from polymarket_weather_arb.domain.order_constraints import (
    OrderConstraints,
    extract_order_constraints,
    normalize_buy_order,
)
from polymarket_weather_arb.domain.fees import (
    FEE_QUANTUM,
    expected_buy_fee,
    fee_adjusted_buy_notional_cap,
)
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import ProposedOrder
from polymarket_weather_arb.domain.strategy_versions import WEATHER_ENTRY_POLICY_VERSION


@dataclass(frozen=True)
class OrderIntent:
    market_id: str
    side: str
    token_id: str | None
    limit_price: Decimal
    size: Decimal
    notional: Decimal
    rationale: str
    dry_run: bool
    status: str
    idempotency_key: str | None = None
    entry_policy_version: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class OrderAttempt:
    intent_id: int
    request_payload: dict[str, object]
    response_payload: dict[str, object] | None
    status: str
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def build_proposed_order(
    analysis: Analysis,
    yes_token_id: str | None,
    no_token_id: str | None,
    max_notional: Decimal,
    *,
    market_payload: dict[str, Any] | None = None,
    constraints: OrderConstraints | None = None,
    enforce_exchange_minimum: bool = True,
) -> ProposedOrder | None:
    if analysis.decision != "trade" or analysis.side is None or analysis.reference_price is None:
        return None
    token_id = yes_token_id if analysis.side == "buy_yes" else no_token_id
    if not token_id:
        return None
    resolved = constraints or extract_order_constraints(market_payload)
    principal_cap = fee_adjusted_buy_notional_cap(
        cash_cap=max_notional,
        price=analysis.reference_price,
        market_payload=market_payload,
    )
    if enforce_exchange_minimum:
        normalized = normalize_buy_order(
            limit_price=analysis.reference_price,
            max_notional=principal_cap,
            constraints=resolved,
        )
        if normalized is None:
            return None
        return ProposedOrder(
            market_id=analysis.market_id,
            side=analysis.side,
            token_id=token_id,
            limit_price=normalized.limit_price,
            size=normalized.size,
            estimated_entry_fee=expected_buy_fee(
                shares=normalized.size,
                price=normalized.limit_price,
                market_payload=market_payload,
            ),
        )
    # Gate-evaluation sizing (risk / source-grade): quantize under the cap without
    # failing early on exchange minima. Final preflight before SDK enforces mins.
    from decimal import ROUND_DOWN

    size = (principal_cap / analysis.reference_price).quantize(
        resolved.size_step, rounding=ROUND_DOWN
    )
    if size <= 0:
        return None
    return ProposedOrder(
        market_id=analysis.market_id,
        side=analysis.side,
        token_id=token_id,
        limit_price=analysis.reference_price,
        size=size,
        estimated_entry_fee=expected_buy_fee(
            shares=size,
            price=analysis.reference_price,
            market_payload=market_payload,
        ),
    )


def preflight_buy_rejection_reason(
    analysis: Analysis,
    max_notional: Decimal,
    *,
    market_payload: dict[str, Any] | None = None,
) -> str | None:
    """Return a concrete rejection when size cannot meet exchange minima under the cap."""
    if analysis.decision != "trade" or analysis.side is None or analysis.reference_price is None:
        return "latest analysis does not produce an executable order"
    constraints = extract_order_constraints(market_payload)
    principal_cap = fee_adjusted_buy_notional_cap(
        cash_cap=max_notional,
        price=analysis.reference_price,
        market_payload=market_payload,
    )
    normalized = normalize_buy_order(
        limit_price=analysis.reference_price,
        max_notional=principal_cap,
        constraints=constraints,
    )
    if normalized is None:
        return (
            f"order below exchange minimum notional/size "
            f"(min_notional={constraints.min_notional}, min_size={constraints.min_size}); "
            f"cannot meet minimum within effective entry headroom {max_notional}"
        )
    return None


def minimum_buy_cash_required(
    analysis: Analysis,
    *,
    market_payload: dict[str, Any] | None = None,
) -> Decimal | None:
    """Return the smallest fee-inclusive cash amount that forms a valid BUY."""
    if analysis.decision != "trade" or analysis.side is None or analysis.reference_price is None:
        return None
    constraints = extract_order_constraints(market_payload)
    price = analysis.reference_price.quantize(constraints.price_tick, rounding=ROUND_DOWN)
    if price <= 0 or price >= 1:
        return None
    size = (constraints.min_notional / price).quantize(
        constraints.size_step,
        rounding=ROUND_UP,
    )
    if constraints.min_size is not None:
        size = max(
            size,
            constraints.min_size.quantize(constraints.size_step, rounding=ROUND_UP),
        )
    if size <= 0:
        return None
    principal = size * price
    fee = expected_buy_fee(
        shares=size,
        price=price,
        market_payload=market_payload,
    )
    # The cap conversion is continuous while exchange fee accounting is rounded
    # to 5 decimal places. Reserve one fee quantum so the next normalization
    # step cannot turn a valid minimum into a sub-minimum principal.
    return principal + fee + (FEE_QUANTUM if fee > 0 else Decimal("0"))


def build_order_intent(
    order: ProposedOrder,
    rationale: str,
    dry_run: bool,
    status: str,
    idempotency_key: str | None = None,
    entry_policy_version: str | None = WEATHER_ENTRY_POLICY_VERSION,
) -> OrderIntent:
    return OrderIntent(
        market_id=order.market_id,
        side=order.side,
        token_id=order.token_id,
        limit_price=order.limit_price,
        size=order.size,
        notional=order.notional,
        rationale=rationale,
        dry_run=dry_run,
        status=status,
        idempotency_key=idempotency_key,
        entry_policy_version=entry_policy_version,
    )


def live_order_idempotency_key(order: ProposedOrder, *, opportunity_id: str) -> str:
    """Identify one BUY attempt for one analyzed opportunity.

    A market/token key alone permanently blocked later valid opportunities after
    the first order reached a terminal state. The analysis timestamp keeps
    retries of the same decision idempotent while allowing a freshly analyzed
    opportunity to submit after cancellation or completion.
    """
    return live_order_opportunity_key(
        market_id=order.market_id,
        side=order.side,
        token_id=order.token_id,
        opportunity_id=opportunity_id,
    )


def live_order_opportunity_key(
    *, market_id: str, side: str, token_id: str | None, opportunity_id: str
) -> str:
    """Build the stable key used by execution and pre-selection deduplication."""
    return f"live:{market_id}:{side}:{token_id or ''}:{opportunity_id}"


def exit_order_idempotency_key(
    *, market_id: str, outcome: str, token_id: str, reconciliation_id: str
) -> str:
    """Identify one SELL attempt against one reconciled inventory snapshot."""
    return f"exit:sell:{market_id}:{outcome.upper()}:{token_id}:{reconciliation_id}"


def build_close_confirm_phrase(*, market_id: str, outcome: str, size_text: str) -> str:
    """Exact confirm phrase required before any live SELL mutation."""
    return f"SELL {market_id} {outcome.upper()} {size_text}"
