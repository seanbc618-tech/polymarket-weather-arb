"""Integration: AutoExit preserves settlement core and routes confirmed exits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION
from polymarket_weather_arb.services.auto_exit_service import AutoExitService
from polymarket_weather_arb.services.compliance_service import ComplianceDecision, ComplianceService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _settings(tmp_path, **kw):
    base = dict(
        DATABASE_PATH=tmp_path / "pp-exit.db",
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
        AUTO_EXIT_ENABLED=True,
        MAX_AUTO_EXITS_PER_TICK=2,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("50"),
        AUTO_EXIT_MAX_SLIPPAGE=Decimal("0.05"),
        STALE_ORDER_BOOK_SECONDS=300,
        MIN_EDGE=Decimal("0.05"),
    )
    base.update(kw)
    return Settings(**base)


def _seed(repo, market_id="m-pp", *, size="100", decision="trade", edge="0.25", side="buy_yes"):
    repo.upsert_market(
        Market(
            id=market_id,
            title="Will the highest temperature in Chicago be 80F or higher on December 31, 2099?",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
            close_time="2099-12-31T23:59:59+00:00",
            status="active",
        ),
        {
            "id": market_id,
            "closed": False,
            "acceptingOrders": True,
            "feesEnabled": True,
            "feeType": "weather_fees",
            "orderMinSize": "1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token", "no-token"],
            "endDate": "2099-12-31T23:59:59+00:00",
        },
    )
    repo.save_reconciliation("ok", {"status": "ok"})
    repo.replace_positions(
        [{"market": market_id, "outcome": "Yes", "size": size, "avgPrice": "0.01"}]
    )
    repo.connection.execute(
        """
        INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at, raw_payload)
        VALUES ('buy1', ?, 'ob1', 'BUY', 0.01, ?, 0, ?, ?)
        """,
        (
            market_id,
            float(size),
            datetime.now(timezone.utc).isoformat(),
            '{"outcome":"YES"}',
        ),
    )
    repo.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="t",
            fair_lower=Decimal("0.7"),
            fair_upper=Decimal("0.9"),
            reference_price=Decimal("0.5"),
            edge=Decimal(edge),
            side=side if decision == "trade" else None,
            decision=decision,
            reasons=["fixture"],
        )
    )


def _client(bid="0.02"):
    client = Mock()
    client.place_limit_order.side_effect = RuntimeError("BUY forbidden")
    client.place_sell_limit_order.return_value = {"order_id": "sell-pp-1", "status": "live"}
    client.get_token_order_book.return_value = (
        MarketSnapshot(
            market_id="token_book",
            best_bid=Decimal(bid),
            best_ask=Decimal("0.03"),
            midpoint=Decimal("0.025"),
            spread=Decimal("0.01"),
            liquidity=Decimal("1000"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {},
    )
    client.get_order.return_value = {"id": "sell-pp-1", "status": "LIVE"}
    client.get_balances.return_value = {"balance": 1}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []
    return client


def _compliance():
    svc = Mock(spec=ComplianceService)
    svc.check_live_allowed.return_value = ComplianceDecision(
        ok=True, status="check_disabled", reason="test"
    )
    return svc


def _seed_value_exit_confirmations(repo, market_id="m-pp"):
    now = datetime.now(timezone.utc)
    for index, revision in enumerate(("value-r1", "value-r2")):
        analysis_id = repo.save_analysis(
            Analysis(
                market_id=market_id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.00"),
                fair_upper=Decimal("0.05"),
                reference_price=Decimal("0.12"),
                edge=Decimal("-0.07"),
                side=None,
                decision="watch",
                reasons=["settlement-core value test"],
                created_at=now + timedelta(microseconds=index),
            )
        )
        repo.connection.execute(
            """
            UPDATE model_signals
            SET raw_payload = json_set(raw_payload, '$.forecast_revision', ?)
            WHERE analysis_id = ?
            """,
            (revision, analysis_id),
        )


def test_auto_exit_does_not_recover_principal_or_partial_sell(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo, size="100", decision="trade", edge="0.25", side="buy_yes")
    client = _client(bid="0.02")
    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance(),
    )
    assert result.executed == 0
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_hold_runner_no_sell(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo, size="40", decision="trade", edge="0.25", side="buy_yes")
    # Prior SELL recovered principal
    repo.connection.execute(
        """
        INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at, raw_payload)
        VALUES ('s1', 'm-pp', 'os1', 'SELL', 0.03, 60, 0, ?, '{"outcome":"YES"}')
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    # Adjust buy size to 100 total with residual 40
    repo.connection.execute("UPDATE fills SET size=100 WHERE exchange_fill_id='buy1'")
    client = _client(bid="0.04")
    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance(),
    )
    assert result.executed == 0
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_model_reversal_alone_does_not_sell(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo, size="10", decision="trade", edge="0.20", side="buy_no")
    client = _client(bid="0.12")
    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance(),
    )
    assert result.executed == 0
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_repeated_value_dominance_never_sells(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo, size="10", decision="trade", edge="0.20", side="buy_yes")
    _seed_value_exit_confirmations(repo)
    client = _client(bid="0.12")

    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance(),
    )

    assert result.executed == 0
    assert result.attempted == 0
    assert result.notes == ["no executable exit recommendations"]
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_insufficient_model_evidence_never_sells(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo, size="10", decision="trade", edge="0.20", side="buy_yes")
    repo.save_analysis(
        Analysis(
            market_id="m-pp",
            model_version="global-temp-multimodel-v1",
            fair_lower=Decimal("0"),
            fair_upper=Decimal("1"),
            reference_price=Decimal("0.12"),
            edge=Decimal("-0.12"),
            side=None,
            decision="watch",
            reasons=[
                "supporting_models=0/0 required=0",
                "evidence_status=insufficient_models",
                "requires at least 3 models",
            ],
        )
    )
    client = _client(bid="0.06")

    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance(),
    )

    assert result.executed == 0
    assert "no executable exit recommendations" in result.notes
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_settlement_no_sell(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo, size="10")
    repo.save_analysis(
        Analysis(
            market_id="m-pp",
            model_version="settlement-route-v1",
            fair_lower=Decimal("0"),
            fair_upper=Decimal("0"),
            reference_price=None,
            edge=Decimal("0"),
            side=None,
            decision="skip",
            reasons=["Position expired/closed; settlement state: awaiting observation"],
        )
    )
    client = _client()
    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance(),
    )
    assert result.executed == 0
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_skips_order_book_for_expired_position(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo, size="10")
    repo.connection.execute(
        "UPDATE markets SET title = ?, close_time = ? WHERE id = 'm-pp'",
        (
            "Will the highest temperature in Chicago be 80F or higher on January 1, 2020?",
            "2020-01-01T23:59:59+00:00",
        ),
    )
    client = _client()

    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance(),
    )

    assert result.executed == 0
    assert "no executable exit recommendations" in result.notes
    client.get_token_order_book.assert_not_called()
    client.place_sell_limit_order.assert_not_called()
    conn.close()
