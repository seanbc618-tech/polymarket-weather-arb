"""Hotfix tests: exchange submit audit must survive roundtrip binding failures."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from typer.testing import CliRunner

from polymarket_weather_arb.cli import app
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import RiskContext
from polymarket_weather_arb.services.compliance_service import ComplianceDecision, ComplianceService
from polymarket_weather_arb.services.position_exit_service import PositionExitService
from polymarket_weather_arb.services.trading_service import TradingService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository

runner = CliRunner()


def _seed_market(repo: Repository, market_id: str = "m1") -> None:
    repo.upsert_market(
        Market(
            id=market_id,
            title="Durability Market",
            is_weather=True,
            tags=("weather",),
            slug=market_id,
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
        ),
        {
            "id": market_id,
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token", "no-token"],
        },
    )


def _live_settings(tmp_path) -> Settings:
    return Settings(
        DATABASE_PATH=tmp_path / "durability.db",
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
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


def test_sell_roundtrip_bind_failure_after_submit_keeps_audit(tmp_path):
    """P0: record_roundtrip_sell_intent raising must not erase submitted SELL audit."""
    settings = _live_settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    connection = db.connect()
    repo = Repository(connection)
    _seed_market(repo)
    repo.replace_positions(
        [{"market": "m1", "outcome": "Yes", "size": "10", "avgPrice": "0.4"}]
    )
    repo.save_reconciliation("ok", {"status": "ok"})

    client = Mock()
    client.place_limit_order.side_effect = RuntimeError("BUY path forbidden")
    client.place_sell_limit_order.return_value = {
        "order_id": "sell-live-1",
        "status": "live",
    }
    client.get_order.return_value = {"id": "sell-live-1", "status": "LIVE"}
    client.get_balances.return_value = {"balance": 1}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []
    client.get_token_order_book.return_value = (
        MarketSnapshot(
            market_id="token_book",
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.55"),
            midpoint=Decimal("0.525"),
            spread=Decimal("0.05"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {},
    )

    original_bind = repo.record_roundtrip_sell_intent
    bind_calls = {"n": 0}

    def _fail_bind(*args, **kwargs):
        bind_calls["n"] += 1
        raise RuntimeError("sqlite locked on roundtrip_runs")

    repo.record_roundtrip_sell_intent = _fail_bind  # type: ignore[method-assign]

    persisted: list[int] = []
    service = PositionExitService(repo, client)
    result = service.close_live(
        settings=settings,
        market_id="m1",
        outcome="YES",
        price=Decimal("0.49"),
        size=Decimal("5"),
        size_text="5",
        max_slippage=Decimal("0.05"),
        confirm="SELL m1 YES 5",
        compliance_service=_allowed_compliance(),
        on_submitted=lambda intent_id: (persisted.append(intent_id), connection.commit()),
    )
    connection.commit()

    assert result["ok"] is True
    assert result["order_id"] == "sell-live-1"
    assert bind_calls["n"] == 1
    assert persisted  # durable commit callback ran before bind failure
    assert "roundtrip sell binding failed" in (result.get("warning") or "")
    assert "order audit retained" in (result.get("warning") or "")

    # Simulate outer exception path would not roll back already-committed rows:
    # reopen a new connection and verify audit still exists.
    connection.close()
    connection2 = db.connect()
    try:
        repo2 = Repository(connection2)
        intents = repo2.list_recent_order_intents(limit=5, market_id="m1")
        assert len(intents) == 1
        assert intents[0]["side"] == "sell_yes"
        assert intents[0]["dry_run"] == 0
        attempts = connection2.execute(
            "SELECT status FROM order_attempts WHERE intent_id = ? ORDER BY id",
            (intents[0]["id"],),
        ).fetchall()
        assert attempts[0]["status"] == "submitted"
        # Roundtrip bind failed — may have no sell_intent_id, but order audit remains.
        assert original_bind is not None
    finally:
        connection2.close()


def test_buy_roundtrip_bind_failure_after_submit_keeps_audit(tmp_path):
    """P0: trading_service BUY bind failure must keep submitted intent/attempt."""
    settings = _live_settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    connection = db.connect()
    repo = Repository(connection)
    _seed_market(repo)

    client = Mock()
    client.place_limit_order.return_value = {
        "order_id": "buy-live-1",
        "status": "live",
    }
    client.validate_order_signing.return_value = {"ok": True}

    def _fail_bind(*_a, **_k):
        raise RuntimeError("disk full on roundtrip_runs")

    repo.record_roundtrip_buy_intent = _fail_bind  # type: ignore[method-assign]

    trading = TradingService(settings, client, repo)
    analysis = Analysis(
        market_id="m1",
        model_version="t",
        fair_lower=Decimal("0.8"),
        fair_upper=Decimal("0.9"),
        reference_price=Decimal("0.5"),
        edge=Decimal("0.2"),
        side="buy_yes",
        decision="trade",
        reasons=["test"],
    )
    context = RiskContext(
        daily_live_notional=Decimal("0"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=1,
        forecast_age_seconds=1,
        rule_tradable=True,
        reconciliation_fresh=True,
    )
    intent_id, reasons = trading.trade(
        analysis=analysis,
        yes_token_id="yes-token",
        no_token_id="no-token",
        context=context,
        dry_run=False,
        source_grade="official_forecast",
    )
    connection.commit()

    assert intent_id is not None
    assert "live order submitted" in reasons
    assert any("roundtrip buy binding failed" in r for r in reasons)
    assert any("order audit retained" in r for r in reasons)
    client.place_limit_order.assert_called_once()

    connection.close()
    connection2 = db.connect()
    try:
        repo2 = Repository(connection2)
        intent = repo2.list_recent_order_intents(limit=1, market_id="m1")[0]
        assert intent["side"] == "buy_yes"
        assert intent["status"] == "submitted"
        attempts = connection2.execute(
            "SELECT status FROM order_attempts WHERE intent_id = ?",
            (intent["id"],),
        ).fetchall()
        assert [a["status"] for a in attempts] == ["submitted"]
    finally:
        connection2.close()


def test_roundtrip_status_cli_commits_completed_across_reconnect(tmp_path, monkeypatch):
    """P1: completed/failed write must survive connection.close() after CLI."""
    from polymarket_weather_arb.cli_commands import operator
    from polymarket_weather_arb.domain.execution import OrderAttempt

    db_path = tmp_path / "status-commit.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COMPLIANCE_CHECK_ENABLED", "false")

    database = Database(db_path)
    database.init_schema()
    connection = database.connect()
    repo = Repository(connection)
    _seed_market(repo, "m-status")
    repo.save_reconciliation("ok", {"status": "ok"})

    buy_id = repo.save_order_intent(
        SimpleNamespace(
            market_id="m-status",
            side="buy_yes",
            token_id="yes-token",
            limit_price=Decimal("0.5"),
            size=Decimal("2"),
            notional=Decimal("1"),
            rationale="buy",
            dry_run=False,
            status="filled",
            idempotency_key="buy-k",
            created_at=datetime.now(timezone.utc),
        )
    )
    sell_id = repo.save_order_intent(
        SimpleNamespace(
            market_id="m-status",
            side="sell_yes",
            token_id="yes-token",
            limit_price=Decimal("0.4"),
            size=Decimal("2"),
            notional=Decimal("0.8"),
            rationale="sell",
            dry_run=False,
            status="filled",
            idempotency_key="sell-k",
            created_at=datetime.now(timezone.utc),
        )
    )
    repo.save_order_attempt(
        OrderAttempt(
            intent_id=buy_id,
            request_payload={},
            response_payload={"order_id": "buy-o"},
            status="submitted",
        )
    )
    repo.save_order_attempt(
        OrderAttempt(
            intent_id=sell_id,
            request_payload={},
            response_payload={"order_id": "sell-o"},
            status="submitted",
        )
    )
    run_id = repo.create_roundtrip_run("m-status")
    repo.update_roundtrip_run_buy(run_id, buy_id, "buy_open")
    repo.update_roundtrip_run_sell(run_id, sell_id, "sell_open")
    # Flat book + matching fills => completed
    connection.execute("DELETE FROM positions")
    repo.save_reconciled_fills(
        [
            {
                "id": "f1",
                "market": "m-status",
                "side": "buy_yes",
                "size": "2",
                "price": "0.5",
                "order_id": "buy-o",
            },
            {
                "id": "f2",
                "market": "m-status",
                "side": "sell_yes",
                "size": "2",
                "price": "0.4",
                "order_id": "sell-o",
            },
        ]
    )
    connection.commit()
    connection.close()

    # Spy: ensure operator path commits (no Gamma client needed for status).
    monkeypatch.setattr(operator, "GammaPolymarketClient", lambda settings: Mock())

    result = runner.invoke(
        app,
        ["operator", "roundtrip-status", "--market", "m-status"],
    )
    assert result.exit_code == 0, result.output
    assert "completed" in result.stdout.lower() or "Stage" in result.stdout

    # Reopen: status column must remain completed after CLI closed its connection.
    connection2 = database.connect()
    try:
        row = connection2.execute(
            "SELECT status FROM roundtrip_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "completed"
    finally:
        connection2.close()
