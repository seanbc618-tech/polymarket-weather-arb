"""Offline micro-live BUY→SELL roundtrip rehearsal through production service paths.

Does not hit real networks. Verifies that trading_service / close_live actually
create and update roundtrip_runs, and that order_id fields from the adapter
participate in fill matching for stage=completed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import OrderAttempt
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import RiskContext
from polymarket_weather_arb.services.compliance_service import ComplianceDecision, ComplianceService
from polymarket_weather_arb.services.position_exit_service import PositionExitService
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.services.roundtrip_status_service import RoundtripStatusService
from polymarket_weather_arb.services.trading_service import TradingService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _seed_market(repo: Repository, market_id: str = "test-market-1") -> None:
    market = Market(
        id=market_id,
        title="Test Market",
        is_weather=True,
        tags=("weather",),
        slug="test-market",
        category="Weather",
        yes_token_id="token-yes",
        no_token_id="token-no",
        status="active",
    )
    repo.upsert_market(
        market,
        {
            "id": market_id,
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["token-yes", "token-no"],
        },
    )


def _live_settings(tmp_path) -> Settings:
    return Settings(
        DATABASE_PATH=tmp_path / "rehearsal.db",
        POLYMARKET_PRIVATE_KEY="test-key",
        POLYMARKET_FUNDER="0xfunder",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
        STALE_ORDER_BOOK_SECONDS=300,
        MAX_ORDER_USDC=Decimal("25"),
        MAX_DAILY_USDC=Decimal("100"),
        MAX_MARKET_USDC=Decimal("50"),
    )


def _allowed_compliance() -> ComplianceService:
    service = Mock(spec=ComplianceService)
    service.check_live_allowed.return_value = ComplianceDecision(
        ok=True, status="check_disabled", reason="test"
    )
    return service


def test_get_order_ids_for_intent_reads_order_id_field(tmp_path):
    """Adapter responses use order_id; exact-fill matching must accept it."""
    db = Database(tmp_path / "ids.db")
    db.init_schema()
    connection = db.connect()
    repo = Repository(connection)
    _seed_market(repo)
    intent_id = repo.save_order_intent(
        SimpleNamespace(
            market_id="test-market-1",
            side="buy_yes",
            token_id="token-yes",
            limit_price=Decimal("0.5"),
            size=Decimal("2"),
            notional=Decimal("1"),
            rationale="t",
            dry_run=False,
            status="submitted",
            idempotency_key="k-order-id",
            created_at=datetime.now(timezone.utc),
        )
    )
    repo.save_order_attempt(
        OrderAttempt(
            intent_id=intent_id,
            request_payload={"step": "submit"},
            response_payload={"ok": True, "order_id": "adapter-order-1"},
            status="submitted",
        )
    )
    assert repo.get_order_ids_for_intent(intent_id) == ["adapter-order-1"]
    connection.close()


def test_get_order_ids_for_intent_reads_orderID_and_id(tmp_path):
    db = Database(tmp_path / "ids2.db")
    db.init_schema()
    connection = db.connect()
    repo = Repository(connection)
    _seed_market(repo)
    for key, value, ikey in (
        ("orderID", "camel-order", "k-orderID"),
        ("id", "plain-id", "k-id"),
    ):
        intent_id = repo.save_order_intent(
            SimpleNamespace(
                market_id="test-market-1",
                side="buy_yes",
                token_id="token-yes",
                limit_price=Decimal("0.5"),
                size=Decimal("1"),
                notional=Decimal("0.5"),
                rationale="t",
                dry_run=False,
                status="submitted",
                idempotency_key=ikey,
                created_at=datetime.now(timezone.utc),
            )
        )
        repo.save_order_attempt(
            OrderAttempt(
                intent_id=intent_id,
                request_payload={},
                response_payload={key: value},
                status="submitted",
            )
        )
        assert value in repo.get_order_ids_for_intent(intent_id)
    connection.close()


def test_micro_live_rehearsal_via_production_buy_and_sell_paths(tmp_path):
    """BUY via TradingService and SELL via close_live must populate roundtrip_runs."""
    market_id = "test-market-1"
    settings = _live_settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    connection = db.connect()
    repo = Repository(connection)
    _seed_market(repo, market_id)
    repo.save_reconciliation("ok", {"status": "ok"})

    client = Mock()
    client.place_limit_order.return_value = {
        "ok": True,
        "order_id": "buy-order-1",
        "status": "live",
    }
    client.place_sell_limit_order.return_value = {
        "ok": True,
        "order_id": "sell-order-1",
        "status": "live",
    }
    client.get_order.side_effect = lambda order_id: {"id": order_id, "status": "LIVE"}
    client.get_balances.return_value = {"balance": 1000}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []
    client.get_token_order_book.return_value = (
        MarketSnapshot(
            market_id="token_book",
            best_bid=Decimal("0.40"),
            best_ask=Decimal("0.50"),
            midpoint=Decimal("0.45"),
            spread=Decimal("0.10"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {},
    )
    client.validate_order_signing.return_value = {"ok": True, "status": "ok"}

    roundtrip = RoundtripStatusService(repo)
    assert roundtrip.get_status(market_id).stage == "ready_to_buy"
    assert repo.get_active_roundtrip_run(market_id) is None

    # --- BUY leg through TradingService (production path) ---
    trading = TradingService(settings, client, repo)
    analysis = Analysis(
        market_id=market_id,
        model_version="test",
        fair_lower=Decimal("0.80"),
        fair_upper=Decimal("0.90"),
        reference_price=Decimal("0.50"),
        edge=Decimal("0.20"),
        side="buy_yes",
        decision="trade",
        reasons=["rehearsal"],
    )
    context = RiskContext(
        daily_live_notional=Decimal("0"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=1,
        forecast_age_seconds=1,
        rule_tradable=True,
        reconciliation_fresh=True,
    )
    buy_intent_id, reasons = trading.trade(
        analysis=analysis,
        yes_token_id="token-yes",
        no_token_id="token-no",
        context=context,
        dry_run=False,
        source_grade="official_forecast",
    )
    assert buy_intent_id is not None
    assert "live order submitted" in reasons
    client.place_limit_order.assert_called_once()

    run = repo.get_active_roundtrip_run(market_id)
    assert run is not None
    assert int(run["buy_intent_id"]) == int(buy_intent_id)
    assert run["sell_intent_id"] is None
    assert repo.get_order_ids_for_intent(buy_intent_id) == ["buy-order-1"]

    # Reconcile BUY fill + position (adapter-style order_id on trade)
    client.get_positions.return_value = [
        {
            "market": market_id,
            "outcome": "Yes",
            "size": "2.0",
            "avgPrice": "0.5",
        }
    ]
    client.get_trades.return_value = [
        {
            "market": market_id,
            "side": "buy_yes",
            "size": "2.0",
            "price": "0.5",
            "transactionHash": "hash-buy",
            "order_id": "buy-order-1",
        }
    ]
    ReconciliationService(client, repo).reconcile()
    repo.save_reconciliation("ok", {"status": "ok"})

    status = roundtrip.get_status(market_id)
    assert status.stage == "position_confirmed"
    assert any(float(p["size"]) == 2.0 for p in status.positions)

    # --- SELL leg through close_live (production path) ---
    # The exchange already exposes the SELL fill during close_live's internal
    # reconciliation. This used to leave the run stuck at sell_open because the
    # next Autopilot reconciliation no longer considered the fill "new".
    client.get_positions.return_value = []
    client.get_trades.return_value = [
        {
            "market": market_id,
            "side": "buy_yes",
            "size": "2.0",
            "price": "0.5",
            "transactionHash": "hash-buy",
            "order_id": "buy-order-1",
        },
        {
            "market": market_id,
            "side": "sell_yes",
            "size": "2.0",
            "price": "0.4",
            "transactionHash": "hash-sell",
            "order_id": "sell-order-1",
        },
    ]
    exit_service = PositionExitService(repo, client)
    sell_result = exit_service.close_live(
        settings=settings,
        market_id=market_id,
        outcome="YES",
        price=Decimal("0.40"),
        size=Decimal("2"),
        size_text="2",
        max_slippage=Decimal("0.05"),
        confirm="SELL test-market-1 YES 2",
        compliance_service=_allowed_compliance(),
    )
    assert sell_result["ok"] is True
    assert sell_result["order_id"] == "sell-order-1"
    client.place_sell_limit_order.assert_called_once()
    client.place_limit_order.assert_called_once()  # still only BUY

    run = repo.get_active_roundtrip_run(market_id)
    assert run is not None
    assert int(run["buy_intent_id"]) == int(buy_intent_id)
    assert int(run["sell_intent_id"]) == int(sell_result["intent_id"])
    assert repo.get_order_ids_for_intent(sell_result["intent_id"]) == ["sell-order-1"]

    status = roundtrip.get_status(market_id)
    assert status.stage == "completed"
    assert not any(float(p["size"]) > 0 for p in status.positions)
    run = repo.get_active_roundtrip_run(market_id)
    assert run is not None
    assert run["status"] == "completed"

    connection.close()


def test_smoke_live_records_roundtrip_buy(tmp_path, monkeypatch):
    """operator smoke-live must call record_roundtrip_buy_intent on real CLI path."""
    from typer.testing import CliRunner

    from polymarket_weather_arb.cli import app
    from polymarket_weather_arb.cli_commands import operator

    db_path = tmp_path / "smoke-roundtrip.db"
    runner = CliRunner()

    class FakeSmoke:
        def __init__(self):
            self.market = Market(
                id="2853383",
                slug="smoke",
                title="Smoke",
                description="t",
                yes_token_id="yes-token",
                no_token_id="no-token",
                status="active",
                is_weather=True,
            )
            self.submitted = []

        def get_market(self, market_id):
            return (self.market, {"id": market_id})

        def validate_order_signing(self):
            return {"ok": True, "status": "wallet-path-configured"}

        def place_limit_order(self, *, token_id, side, price, size):
            self.submitted.append(
                {"token_id": token_id, "side": side, "price": price, "size": size}
            )
            # Adapter-style field name used by GammaPolymarketClient tests.
            return {"ok": True, "order_id": "smoke-order-1", "status": "live"}

        def get_order(self, order_id):
            return {"id": order_id, "status": "LIVE"}

        def cancel_order(self, order_id):
            return {"canceled": [order_id], "not_canceled": {}}

        def get_balances(self):
            return {"balance": 1}

        def get_orders(self):
            return []

        def get_trades(self):
            return []

        def get_positions(self):
            return []

    fake = FakeSmoke()
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    monkeypatch.setenv("TRADING_DISABLED", "false")
    monkeypatch.setattr(operator, "GammaPolymarketClient", lambda settings: fake)

    result = runner.invoke(
        app,
        [
            "operator",
            "smoke-live",
            "--market",
            "2853383",
            "--side",
            "buy_yes",
            "--price",
            "0.001",
            "--size",
            "1000",
            "--cancel-immediately",
        ],
    )
    assert result.exit_code == 0, result.output

    database = Database(db_path)
    connection = database.connect()
    try:
        repo = Repository(connection)
        run = repo.get_active_roundtrip_run("2853383")
        assert run is not None
        assert run["buy_intent_id"] is not None
        assert repo.get_order_ids_for_intent(int(run["buy_intent_id"])) == ["smoke-order-1"]
    finally:
        connection.close()
