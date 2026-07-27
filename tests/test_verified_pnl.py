from polymarket_weather_arb.services.cockpit_service import _build_verified_pnl
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository
from polymarket_weather_arb.config import Settings
import math
from decimal import Decimal


def test_verified_pnl_complex_scenarios(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "cockpit.db")
    Database(settings.database_path).init_schema()
    db = Database(settings.database_path)
    with db.transaction() as conn:
        repository = Repository(conn)

        # 1. Market
        conn.execute("INSERT INTO markets (id, title, raw_payload) VALUES ('m1', 'T', '{}')")
        conn.execute(
            "INSERT INTO positions (market_id, outcome, size, notional) "
            "VALUES ('m1', 'YES', 100, 2.25), ('m1', 'NO', -50, -3.25)"
        )

        # 2. Reconciliations
        conn.execute(
            "INSERT INTO reconciliations (status, details, created_at) VALUES ('ok', '{}', datetime('now'))"
        )

        # Create multiple runs:
        # Run 1: Completed, partial fill. Buy 10, sell 5. Matched 5.
        conn.execute(
            "INSERT INTO order_intents (id, market_id, side, limit_price, size, notional, rationale, dry_run, status) VALUES (1, 'm1', 'BUY', 0.5, 10, 5, 'ok', 0, 'filled')"
        )
        conn.execute(
            "INSERT INTO order_intents (id, market_id, side, limit_price, size, notional, rationale, dry_run, status) VALUES (2, 'm1', 'SELL', 0.6, 5, 3, 'ok', 0, 'filled')"
        )
        conn.execute(
            "INSERT INTO order_attempts (intent_id, status, request_payload, response_payload) VALUES (1, 'submitted', '{}', '{\"orderID\": \"o1\"}')"
        )
        conn.execute(
            "INSERT INTO order_attempts (intent_id, status, request_payload, response_payload) VALUES (2, 'submitted', '{}', '{\"orderID\": \"o2\"}')"
        )
        conn.execute(
            "INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at) VALUES ('f1', 'm1', 'o1', 'BUY', 0.5, 10, 0, datetime('now'))"
        )
        conn.execute(
            "INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at) VALUES ('f2', 'm1', 'o2', 'SELL', 0.6, 5, 0.1, datetime('now'))"
        )
        conn.execute(
            "INSERT INTO roundtrip_runs (market_id, buy_intent_id, sell_intent_id, status) VALUES ('m1', 1, 2, 'completed')"
        )

        # Run 2: Completed, partial fill reversed. Buy 5, sell 10. Matched 5.
        conn.execute(
            "INSERT INTO order_intents (id, market_id, side, limit_price, size, notional, rationale, dry_run, status) VALUES (3, 'm1', 'BUY', 0.4, 5, 2, 'ok', 0, 'filled')"
        )
        conn.execute(
            "INSERT INTO order_intents (id, market_id, side, limit_price, size, notional, rationale, dry_run, status) VALUES (4, 'm1', 'SELL', 0.8, 10, 8, 'ok', 0, 'filled')"
        )
        conn.execute(
            "INSERT INTO order_attempts (intent_id, status, request_payload, response_payload) VALUES (3, 'submitted', '{}', '{\"orderID\": \"o3\"}')"
        )
        conn.execute(
            "INSERT INTO order_attempts (intent_id, status, request_payload, response_payload) VALUES (4, 'submitted', '{}', '{\"orderID\": \"o4\"}')"
        )
        conn.execute(
            "INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at) VALUES ('f3', 'm1', 'o3', 'BUY', 0.4, 5, 0, datetime('now'))"
        )
        conn.execute(
            "INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at) VALUES ('f4', 'm1', 'o4', 'SELL', 0.8, 10, 0.2, datetime('now'))"
        )
        conn.execute(
            "INSERT INTO roundtrip_runs (market_id, buy_intent_id, sell_intent_id, status) VALUES ('m1', 3, 4, 'completed')"
        )

        # Historical fill mixing: Some unrelated fill that belongs to the market but has NO intent/run mapped
        conn.execute(
            "INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at) VALUES ('f_history', 'm1', 'o_history', 'BUY', 0.1, 100, 0, datetime('now'))"
        )

        # Run 3: Latest run incomplete
        conn.execute(
            "INSERT INTO order_intents (id, market_id, side, limit_price, size, notional, rationale, dry_run, status) VALUES (5, 'm1', 'BUY', 0.5, 10, 5, 'ok', 0, 'filled')"
        )
        conn.execute(
            "INSERT INTO order_attempts (intent_id, status, request_payload, response_payload) VALUES (5, 'submitted', '{}', '{\"orderID\": \"o5\"}')"
        )
        conn.execute(
            "INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at) VALUES ('f5', 'm1', 'o5', 'BUY', 0.5, 10, 0, datetime('now'))"
        )
        conn.execute(
            "INSERT INTO roundtrip_runs (market_id, buy_intent_id, sell_intent_id, status) VALUES ('m1', 5, NULL, 'buy_open')"
        )

        # Calculate PnL
        pnl = _build_verified_pnl(repository)
        assert pnl.reconciliation_fresh is True
        assert len(pnl.markets) == 1

        m = pnl.markets[0]
        # Total roundtrips should only count completed ones = 2
        assert m.roundtrips == 2

        # Matched size = Run 1 (5) + Run 2 (5) = 10
        assert m.matched_size == 10

        # Cost = Run 1 (5 * 0.5) + Run 2 (5 * 0.4) = 2.5 + 2.0 = 4.5
        assert math.isclose(float(m.gross_buy_cost), 4.5)

        # Proceeds = Run 1 (5 * 0.6) + Run 2 (5 * 0.8) = 3.0 + 4.0 = 7.0
        assert math.isclose(float(m.gross_sell_proceeds), 7.0)

        # Fees = Run 1 (0.1 fee for 5 size, so all 0.1 since size is 5)
        #        Run 2 (0.2 fee for 10 size, but matched is 5, ratio is 5/10 = 0.5. Fee = 0.1)
        # Total Fees = 0.2
        assert math.isclose(float(m.fees), 0.2)

        # Net = Proceeds - Cost - Fees = 7.0 - 4.5 - 0.2 = 2.3
        assert math.isclose(float(m.realized_pnl), 2.3)
        assert m.reconciled_exposure == Decimal("5.5")

        assert pnl.total_roundtrips == 2
        assert pnl.total_matched_size == 10
        assert math.isclose(float(pnl.total_realized_pnl), 2.3)
        assert pnl.total_reconciled_exposure == Decimal("5.5")


def test_verified_pnl_includes_partial_sell_and_open_position_value(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "cockpit.db")
    Database(settings.database_path).init_schema()
    db = Database(settings.database_path)
    with db.transaction() as conn:
        repository = Repository(conn)
        conn.execute(
            """
            INSERT INTO markets (id, title, yes_token_id, no_token_id, raw_payload)
            VALUES ('m1', 'T', 'yes-token', 'no-token', '{}')
            """
        )
        conn.execute(
            "INSERT INTO positions (market_id, outcome, size, notional) "
            "VALUES ('m1', 'YES', 6, 0.6)"
        )
        conn.execute(
            "INSERT INTO reconciliations (status, details, created_at) "
            "VALUES ('ok', '{}', datetime('now'))"
        )
        conn.execute(
            """
            INSERT INTO order_intents
                (id, market_id, side, token_id, limit_price, size, notional,
                 rationale, dry_run, status)
            VALUES
                (1, 'm1', 'buy_yes', 'yes-token', 0.2, 10, 2, 'ok', 0, 'filled'),
                (2, 'm1', 'sell_yes', 'yes-token', 0.3, 4, 1.2, 'ok', 0, 'matched')
            """
        )
        conn.execute(
            "INSERT INTO order_attempts "
            "(intent_id, status, request_payload, response_payload) VALUES "
            "(1, 'submitted', '{}', '{\"orderID\": \"buy-order\"}'), "
            "(2, 'submitted', '{}', '{\"orderID\": \"sell-order\"}')"
        )
        conn.execute(
            """
            INSERT INTO fills
                (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at)
            VALUES
                ('buy-fill', 'm1', 'buy-order', 'BUY', 0.2, 10, 0.1, datetime('now')),
                ('sell-fill', 'm1', 'sell-order', 'SELL', 0.3, 4, 0.05, datetime('now'))
            """
        )
        conn.execute(
            """
            INSERT INTO roundtrip_runs
                (market_id, buy_intent_id, sell_intent_id, status)
            VALUES ('m1', 1, 2, 'sell_open')
            """
        )

        pnl = _build_verified_pnl(repository)

        assert pnl.total_roundtrips == 1
        assert pnl.total_realized_pnl == Decimal("0.31")
        assert len(pnl.open_campaigns) == 1
        campaign = pnl.open_campaigns[0]
        assert campaign.buy_cost == Decimal("2.1")
        assert campaign.sell_proceeds == Decimal("1.15")
        assert campaign.current_value == Decimal("0.6")
        assert campaign.estimated_pnl == Decimal("-0.35")
        assert pnl.total_open_estimated_pnl == Decimal("-0.35")
        assert pnl.unverified_open_positions == 0


def test_verified_pnl_includes_all_sell_legs_when_run_pointer_advances(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "multi-sell.db")
    Database(settings.database_path).init_schema()
    db = Database(settings.database_path)
    with db.transaction() as conn:
        repository = Repository(conn)
        conn.execute("INSERT INTO markets (id, title, raw_payload) VALUES ('m1', 'T', '{}')")
        conn.execute(
            "INSERT INTO reconciliations (status, details, created_at) "
            "VALUES ('ok', '{}', datetime('now'))"
        )
        conn.execute(
            """
            INSERT INTO order_intents
                (id, market_id, side, limit_price, size, notional, rationale, dry_run, status)
            VALUES
                (1, 'm1', 'buy_yes', 0.10, 10, 1.0, 'buy', 0, 'filled'),
                (2, 'm1', 'sell_yes', 0.30, 4, 1.2, 'recover', 0, 'matched'),
                (3, 'm1', 'sell_yes', 0.40, 6, 2.4, 'exit', 0, 'matched')
            """
        )
        conn.execute(
            """
            INSERT INTO order_attempts (intent_id, status, request_payload, response_payload)
            VALUES
                (1, 'submitted', '{}', '{"orderID": "buy-order"}'),
                (2, 'submitted', '{}', '{"orderID": "recover-order"}'),
                (3, 'submitted', '{}', '{"orderID": "exit-order"}')
            """
        )
        conn.execute(
            """
            INSERT INTO fills
                (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at)
            VALUES
                ('buy-fill', 'm1', 'buy-order', 'BUY', 0.10, 10, 0.01, '2026-07-19T01:00:00Z'),
                ('recover-fill', 'm1', 'recover-order', 'SELL', 0.30, 4, 0.02, '2026-07-19T02:00:00Z'),
                ('exit-fill', 'm1', 'exit-order', 'SELL', 0.40, 6, 0.03, '2026-07-19T03:00:00Z')
            """
        )
        # Lifecycle pointer contains only the latest SELL, as it does in production.
        conn.execute(
            """
            INSERT INTO roundtrip_runs
                (market_id, buy_intent_id, sell_intent_id, status)
            VALUES ('m1', 1, 3, 'completed')
            """
        )

        pnl = _build_verified_pnl(repository)

        assert pnl.total_roundtrips == 1
        assert pnl.total_matched_size == Decimal("10")
        assert pnl.total_gross_buy_cost == Decimal("1.0")
        assert pnl.total_gross_sell_proceeds == Decimal("3.6")
        assert pnl.total_fees == Decimal("0.06")
        assert pnl.total_realized_pnl == Decimal("2.54")


def test_verified_pnl_is_not_fresh_when_latest_reconciliation_failed(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "reconciliation-health.db")
    Database(settings.database_path).init_schema()
    db = Database(settings.database_path)
    with db.transaction() as conn:
        repository = Repository(conn)
        repository.save_reconciliation("ok", {"status": "ok"})
        repository.save_reconciliation(
            "adapter-error",
            {
                "status": "adapter-error",
                "failed_stage": "trades",
                "error_type": "UnexpectedResponseError",
            },
        )

        pnl = _build_verified_pnl(repository)

        assert pnl.reconciliation_fresh is False
