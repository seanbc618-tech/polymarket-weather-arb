"""Offline tests for verified campaign inventory and principal recovery math."""

from __future__ import annotations

from decimal import Decimal

from polymarket_weather_arb.domain.fees import WEATHER_TAKER_FEE_RATE, compute_taker_fee
from polymarket_weather_arb.domain.order_constraints import OrderConstraints
from polymarket_weather_arb.domain.position_inventory import (
    build_campaign_inventory,
    plan_principal_recovery,
)


def test_campaign_0_01_x_100_basic_cost():
    fills = [
        {
            "id": 1,
            "filled_at": "2026-07-11T01:00:00+00:00",
            "side": "BUY",
            "outcome": "YES",
            "price": "0.01",
            "size": "100",
            "fee": "0",
        }
    ]
    inv = build_campaign_inventory(
        fills,
        market_id="m1",
        outcome="YES",
        position_size=Decimal("100"),
        yes_token_id="y",
        no_token_id="n",
    )
    assert inv.accounting_verified
    assert inv.verified_buy_cost == Decimal("1.00")
    assert inv.unrecovered_cash == Decimal("1.00")
    assert inv.net_fill_size == Decimal("100")


def test_prior_partial_sell_and_second_campaign_isolated():
    fills = [
        # Old completed campaign
        {
            "id": 1,
            "filled_at": "2026-07-10T01:00:00+00:00",
            "side": "BUY",
            "outcome": "YES",
            "price": "0.20",
            "size": "10",
            "fee": "0.01",
        },
        {
            "id": 2,
            "filled_at": "2026-07-10T02:00:00+00:00",
            "side": "SELL",
            "outcome": "YES",
            "price": "0.30",
            "size": "10",
            "fee": "0.01",
        },
        # New campaign
        {
            "id": 3,
            "filled_at": "2026-07-11T01:00:00+00:00",
            "side": "BUY",
            "outcome": "YES",
            "price": "0.01",
            "size": "100",
            "fee": "0",
        },
    ]
    inv = build_campaign_inventory(
        fills,
        market_id="m1",
        outcome="YES",
        position_size=Decimal("100"),
    )
    assert inv.accounting_verified
    assert inv.verified_buy_cost == Decimal("1.00")
    assert inv.campaign_fill_count == 1
    assert inv.unrecovered_cash == Decimal("1.00")


def test_opposite_outcome_fills_excluded():
    fills = [
        {
            "id": 1,
            "filled_at": "2026-07-11T01:00:00+00:00",
            "side": "BUY",
            "outcome": "NO",
            "price": "0.50",
            "size": "50",
            "fee": "0",
        },
        {
            "id": 2,
            "filled_at": "2026-07-11T02:00:00+00:00",
            "side": "BUY",
            "outcome": "YES",
            "price": "0.01",
            "size": "100",
            "fee": "0",
        },
    ]
    inv = build_campaign_inventory(
        fills,
        market_id="m1",
        outcome="YES",
        position_size=Decimal("100"),
    )
    assert inv.verified_buy_cost == Decimal("1.00")
    assert inv.buy_size == Decimal("100")


def test_mismatched_position_marks_unverified():
    fills = [
        {
            "id": 1,
            "filled_at": "2026-07-11T01:00:00+00:00",
            "side": "BUY",
            "outcome": "YES",
            "price": "0.01",
            "size": "50",
            "fee": "0",
        }
    ]
    inv = build_campaign_inventory(
        fills,
        market_id="m1",
        outcome="YES",
        position_size=Decimal("100"),
    )
    assert inv.accounting_verified is False
    assert "disagrees" in inv.evidence_reason


def test_principal_recovery_min_size_at_bid_0_02():
    # Cost 1.00 on 100 shares @ 0.01; bid 0.02 weather fee.
    fee_rate = WEATHER_TAKER_FEE_RATE
    plan = plan_principal_recovery(
        unrecovered_cash=Decimal("1.00"),
        position_size=Decimal("100"),
        best_bid=Decimal("0.02"),
        fee_rate=fee_rate,
        constraints=OrderConstraints(
            min_notional=Decimal("0"),
            min_size=None,
            size_step=Decimal("0.0001"),
            price_tick=Decimal("0.001"),
        ),
    )
    assert plan is not None
    assert plan.recovers_principal
    fee = compute_taker_fee(shares=plan.size, price=Decimal("0.02"), fee_rate=fee_rate)
    net = plan.size * Decimal("0.02") - fee
    assert net + Decimal("0.00001") >= Decimal("1.00")
    assert plan.runner_size_after == Decimal("100") - plan.size
    assert plan.size < Decimal("100")  # partial, not full exit


def test_principal_recovery_dust_residual_blocked():
    plan = plan_principal_recovery(
        unrecovered_cash=Decimal("0.50"),
        position_size=Decimal("5.1"),
        best_bid=Decimal("0.50"),
        fee_rate=Decimal("0"),
        constraints=OrderConstraints(
            min_notional=Decimal("0"),
            min_size=Decimal("5"),
            size_step=Decimal("0.1"),
            price_tick=Decimal("0.001"),
        ),
    )
    # size ~1 to recover 0.50 leaves residual 4.1 < min_size 5 → dust
    assert plan is not None
    assert plan.recovers_principal is False
    assert "dust" in plan.reason
