from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.config import Settings

HARDCODED_MAX_ORDER_USDC = Decimal("25")
HARDCODED_MAX_DAILY_USDC = Decimal("100")
HARDCODED_MAX_MARKET_USDC = Decimal("50")


@dataclass(frozen=True)
class ProposedOrder:
    market_id: str
    side: str
    token_id: str | None
    limit_price: Decimal
    size: Decimal
    order_type: str = "limit"
    estimated_entry_fee: Decimal = Decimal("0")

    @property
    def notional(self) -> Decimal:
        return self.limit_price * self.size

    @property
    def cash_at_risk(self) -> Decimal:
        return self.notional + self.estimated_entry_fee


@dataclass(frozen=True)
class RiskDecision:
    market_id: str
    accepted: bool
    proposed_side: str
    proposed_price: Decimal
    proposed_size: Decimal
    proposed_notional: Decimal
    reasons: list[str]
    max_order_usdc: Decimal
    max_daily_usdc: Decimal
    max_market_usdc: Decimal
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class RiskContext:
    daily_live_notional: Decimal
    market_live_exposure: Decimal
    order_book_age_seconds: int | None
    forecast_age_seconds: int | None
    rule_tradable: bool
    unsupported_variable: bool = False
    reconciliation_fresh: bool = True


class RiskEngine:
    def __init__(self, settings: Settings) -> None:
        self.max_order_usdc = min(settings.max_order_usdc, HARDCODED_MAX_ORDER_USDC)
        self.max_daily_usdc = min(settings.max_daily_usdc, HARDCODED_MAX_DAILY_USDC)
        self.max_market_usdc = min(settings.max_market_usdc, HARDCODED_MAX_MARKET_USDC)
        self.stale_order_book_seconds = settings.stale_order_book_seconds
        self.stale_forecast_seconds = settings.stale_forecast_seconds

    def evaluate(self, order: ProposedOrder, context: RiskContext) -> RiskDecision:
        reasons: list[str] = []
        if order.order_type != "limit":
            reasons.append("market orders are forbidden")
        if order.limit_price <= 0 or order.limit_price >= 1:
            reasons.append("limit price must be between 0 and 1")
        if order.size <= 0:
            reasons.append("order size must be positive")
        if order.cash_at_risk > self.max_order_usdc:
            reasons.append(f"order cash at risk exceeds {self.max_order_usdc} USDC cap")
        if context.daily_live_notional + order.cash_at_risk > self.max_daily_usdc:
            reasons.append(f"daily cash at risk exceeds {self.max_daily_usdc} USDC cap")
        if context.market_live_exposure + order.cash_at_risk > self.max_market_usdc:
            reasons.append(f"market exposure exceeds {self.max_market_usdc} USDC cap")
        if context.order_book_age_seconds is None:
            reasons.append("missing order book freshness")
        elif context.order_book_age_seconds > self.stale_order_book_seconds:
            reasons.append("order book is stale")
        if context.forecast_age_seconds is None:
            reasons.append("missing forecast freshness")
        elif context.forecast_age_seconds > self.stale_forecast_seconds:
            reasons.append("forecast is stale")
        if not context.rule_tradable:
            reasons.append("resolution rule is not tradable")
        if context.unsupported_variable:
            reasons.append("weather variable is unsupported")
        if not context.reconciliation_fresh:
            reasons.append("reconciliation state is stale")

        return RiskDecision(
            market_id=order.market_id,
            accepted=not reasons,
            proposed_side=order.side,
            proposed_price=order.limit_price,
            proposed_size=order.size,
            proposed_notional=order.cash_at_risk,
            reasons=reasons or ["accepted"],
            max_order_usdc=self.max_order_usdc,
            max_daily_usdc=self.max_daily_usdc,
            max_market_usdc=self.max_market_usdc,
        )
