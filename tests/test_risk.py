from decimal import Decimal

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.risk import ProposedOrder, RiskContext, RiskEngine
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _context():
    return RiskContext(
        daily_live_notional=Decimal("0"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=10,
        forecast_age_seconds=10,
        rule_tradable=True,
        reconciliation_fresh=True,
    )


def test_risk_rejects_order_above_hard_cap():
    settings = Settings(MAX_ORDER_USDC=Decimal("999"))
    engine = RiskEngine(settings)
    order = ProposedOrder("m1", "buy_yes", "token", Decimal("0.50"), Decimal("60"))

    decision = engine.evaluate(order, _context())

    assert decision.accepted is False
    assert decision.max_order_usdc == Decimal("25")
    assert any("order cash at risk exceeds" in reason for reason in decision.reasons)


def test_risk_rejects_daily_cap():
    engine = RiskEngine(Settings())
    order = ProposedOrder("m1", "buy_yes", "token", Decimal("0.50"), Decimal("20"))
    context = RiskContext(
        daily_live_notional=Decimal("95"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=10,
        forecast_age_seconds=10,
        rule_tradable=True,
        reconciliation_fresh=True,
    )

    decision = engine.evaluate(order, context)

    assert decision.accepted is False
    assert any("daily cash at risk exceeds" in reason for reason in decision.reasons)


def test_risk_caps_include_expected_entry_fee():
    engine = RiskEngine(Settings(MAX_ORDER_USDC=Decimal("1.5")))
    order = ProposedOrder(
        "m1",
        "buy_yes",
        "token",
        Decimal("0.50"),
        Decimal("2.98"),
        estimated_entry_fee=Decimal("0.02"),
    )

    decision = engine.evaluate(order, _context())

    assert order.notional == Decimal("1.490")
    assert order.cash_at_risk == Decimal("1.510")
    assert decision.accepted is False
    assert decision.proposed_notional == Decimal("1.510")
    assert any("order cash at risk exceeds" in reason for reason in decision.reasons)


def test_risk_rejects_market_orders_and_stale_data():
    engine = RiskEngine(Settings())
    order = ProposedOrder(
        "m1", "buy_yes", "token", Decimal("0.50"), Decimal("10"), order_type="market"
    )
    context = RiskContext(
        daily_live_notional=Decimal("0"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=9999,
        forecast_age_seconds=999999,
        rule_tradable=True,
        reconciliation_fresh=True,
    )

    decision = engine.evaluate(order, context)

    assert decision.accepted is False
    assert "market orders are forbidden" in decision.reasons
    assert "order book is stale" in decision.reasons
    assert "forecast is stale" in decision.reasons


def test_daily_order_notional_counts_all_live_buy_states_only(tmp_path):
    database = Database(tmp_path / "risk.db")
    database.init_schema()
    connection = database.connect()
    try:
        repository = Repository(connection)
        repository.upsert_market(
            Market(id="risk-market", title="Risk fixture", is_weather=True),
            {"id": "risk-market"},
        )
        for status, notional in (
            ("submitted", 1),
            ("open", 2),
            ("matched", 3),
            ("partially_filled", 4),
            ("partially_filled_closed", 8),
            ("filled", 5),
            ("submitted_unverified", 6),
            ("reconcile_failed", 7),
        ):
            connection.execute(
                """
                INSERT INTO order_intents (
                    market_id, side, limit_price, size, notional, rationale,
                    dry_run, status, created_at
                ) VALUES (?, 'buy_yes', 0.1, 10, ?, 'test', 0, ?, ?)
                """,
                ("risk-market", notional, status, "2026-07-13T01:00:00+00:00"),
            )
        for side, status in (("sell_yes", "matched"), ("buy_yes", "cancelled")):
            connection.execute(
                """
                INSERT INTO order_intents (
                    market_id, side, limit_price, size, notional, rationale,
                    dry_run, status, created_at
                ) VALUES (?, ?, 0.1, 1000, 100, 'excluded', 0, ?, ?)
                """,
                ("risk-market", side, status, "2026-07-13T01:00:00+00:00"),
            )

        assert repository.daily_order_notional("2026-07-13") == Decimal("36")
    finally:
        connection.close()


def test_daily_and_market_buy_cap_totals_include_expected_weather_fee(tmp_path):
    database = Database(tmp_path / "fee-risk.db")
    database.init_schema()
    connection = database.connect()
    try:
        repository = Repository(connection)
        repository.upsert_market(
            Market(id="fee-market", title="Weather fee fixture", is_weather=True),
            {"id": "fee-market", "feesEnabled": True, "feeType": "weather"},
        )
        connection.execute(
            """
            INSERT INTO order_intents (
                market_id, side, limit_price, size, notional, rationale,
                dry_run, status, created_at
            ) VALUES ('fee-market', 'buy_yes', 0.14, 10, 1.4, 'test', 0,
                      'filled', '2026-07-13T01:00:00+00:00')
            """
        )

        # 10 * 0.05 * 0.14 * 0.86 = 0.06020 entry fee.
        assert repository.daily_order_notional("2026-07-13") == Decimal("1.46020")
        assert repository.live_buy_notional_for_market("fee-market") == Decimal("1.46020")
    finally:
        connection.close()
