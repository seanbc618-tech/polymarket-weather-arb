from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.fees import expected_taker_fee_per_share
from polymarket_weather_arb.domain.probability import ProbabilityInterval


@dataclass(frozen=True)
class Analysis:
    market_id: str
    model_version: str
    fair_lower: Decimal
    fair_upper: Decimal
    reference_price: Decimal | None
    edge: Decimal
    side: str | None
    decision: str
    reasons: list[str]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Gross probability-vs-market edge before fee drag (when fees applied).
    gross_edge: Decimal | None = None
    entry_fee_per_share: Decimal | None = None
    exit_fee_per_share: Decimal | None = None
    # Central model probability for calibration. This differs from the
    # midpoint of a conservative interval when a bound is clamped at 0 or 1.
    fair_probability: Decimal | None = None


def analyze_price(
    market_id: str,
    interval: ProbabilityInterval,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    min_edge: Decimal,
    slippage_buffer: Decimal,
    *,
    fees_enabled: bool = False,
    fee_rate: Decimal | None = None,
) -> Analysis:
    reasons = list(interval.reasons)
    if best_bid is None or best_ask is None:
        return Analysis(
            market_id=market_id,
            model_version=interval.model_version,
            fair_lower=interval.lower,
            fair_upper=interval.upper,
            reference_price=None,
            edge=Decimal("0"),
            side=None,
            decision="reject",
            reasons=reasons + ["missing bid/ask"],
        )

    rate = fee_rate if fees_enabled and fee_rate is not None else Decimal("0")
    # Entry fee at trade price; conservative exit fee at the opposite quote
    # (assume taker on both legs when fees are enabled).
    buy_entry_fee = expected_taker_fee_per_share(price=best_ask, fee_rate=rate)
    buy_exit_fee = expected_taker_fee_per_share(price=best_bid, fee_rate=rate)
    sell_entry_fee = expected_taker_fee_per_share(price=best_bid, fee_rate=rate)
    sell_exit_fee = expected_taker_fee_per_share(price=best_ask, fee_rate=rate)

    gross_buy_edge = interval.lower - best_ask - slippage_buffer
    gross_sell_edge = best_bid - interval.upper - slippage_buffer
    buy_edge = gross_buy_edge - buy_entry_fee - buy_exit_fee
    sell_edge = gross_sell_edge - sell_entry_fee - sell_exit_fee

    fee_notes: list[str] = []
    if fees_enabled and rate > 0:
        fee_notes.append(
            f"net edge after taker fees rate={rate} "
            f"buy_fees={buy_entry_fee + buy_exit_fee} sell_fees={sell_entry_fee + sell_exit_fee}"
        )

    if buy_edge >= min_edge and buy_edge >= sell_edge:
        return Analysis(
            market_id=market_id,
            model_version=interval.model_version,
            fair_lower=interval.lower,
            fair_upper=interval.upper,
            reference_price=best_ask,
            edge=buy_edge,
            side="buy_yes",
            decision="trade",
            reasons=reasons + ["ask is below conservative fair lower bound"] + fee_notes,
            gross_edge=gross_buy_edge,
            entry_fee_per_share=buy_entry_fee,
            exit_fee_per_share=buy_exit_fee,
        )
    if sell_edge >= min_edge:
        return Analysis(
            market_id=market_id,
            model_version=interval.model_version,
            fair_lower=interval.lower,
            fair_upper=interval.upper,
            reference_price=best_bid,
            edge=sell_edge,
            side="buy_no",
            decision="trade",
            reasons=reasons + ["bid is above conservative fair upper bound"] + fee_notes,
            gross_edge=gross_sell_edge,
            entry_fee_per_share=sell_entry_fee,
            exit_fee_per_share=sell_exit_fee,
        )
    return Analysis(
        market_id=market_id,
        model_version=interval.model_version,
        fair_lower=interval.lower,
        fair_upper=interval.upper,
        reference_price=(best_bid + best_ask) / Decimal("2"),
        edge=max(buy_edge, sell_edge),
        side=None,
        decision="watch",
        reasons=reasons + ["edge does not clear cost and safety buffer"] + fee_notes,
        gross_edge=max(gross_buy_edge, gross_sell_edge),
        entry_fee_per_share=buy_entry_fee if buy_edge >= sell_edge else sell_entry_fee,
        exit_fee_per_share=buy_exit_fee if buy_edge >= sell_edge else sell_exit_fee,
    )
