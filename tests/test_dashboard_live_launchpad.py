from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import handle_dashboard_post, render_dashboard_path
from polymarket_weather_arb.domain.execution import OrderIntent
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_live_launchpad_renders_locked_by_default(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db", TRADING_DISABLED=True)
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(settings, "/live?lang=zh")

    assert response.status.value == 200
    assert "Live Launchpad" in response.body
    assert "真实执行已锁定" in response.body
    assert "Refresh exchange state" not in response.body


def test_live_launchpad_nav_link_is_visible(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db", TRADING_DISABLED=True)
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(settings, "/overview-legacy?lang=en")

    assert response.status.value == 200
    assert "/live?lang=en" in response.body
    assert "Live" in response.body


class FakeReconciliationService:
    def __init__(self, repository):
        self.repository = repository

    def reconcile(self):
        self.repository.replace_open_orders([])
        self.repository.replace_positions([])
        self.repository.save_reconciled_fills([])
        self.repository.save_reconciliation("ok", {"source": "fake"})
        return {"status": "ok"}


class FakeOrderLifecycleService:
    def __init__(self, repository):
        self.repository = repository
        self.cancelled_stale = False
        self.cancelled_all = False

    def cancel_stale_orders(self):
        from polymarket_weather_arb.services.order_lifecycle_service import CancelStaleResult

        self.cancelled_stale = True
        return CancelStaleResult(cancelled=[{"id": "order-stale"}], failures=[])

    def cancel_all_open_orders(self):
        self.cancelled_all = True
        return [{"id": "order-1"}]


def test_live_refresh_redirects_to_live(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(
        settings,
        "/live/refresh?lang=en",
        b"lang=en",
        None,
        reconciliation_service_factory=lambda repo: FakeReconciliationService(repo),
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "flash=flash.live_refreshed" in response.headers["Location"]


def test_live_launchpad_renders_order_and_position_safety_controls(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db", TRADING_DISABLED=True)
    _seed_live_candidate(settings)
    _seed_live_exchange_risk(settings)

    response = render_dashboard_path(settings, "/live?lang=en")

    assert response.status.value == 200
    assert "Stale open orders" in response.body
    assert "Open order notional" in response.body
    assert "Position concentration" in response.body
    assert "Cancel stale orders" in response.body
    assert "/live/cancel-stale-orders?lang=en" in response.body
    assert "Cancel all open orders" in response.body
    assert "/live/cancel-all-orders?lang=en" in response.body


def test_live_launchpad_renders_calibration_trust(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db", TRADING_DISABLED=True)
    _seed_live_candidate(settings)
    connection = Database(settings.database_path).connect()
    try:
        Repository(connection).settle_model_signals_for_market(
            "m1",
            resolved_outcome="yes",
            settlement_value=Decimal("83"),
            settlement_source="nws-observation",
        )
        connection.commit()
    finally:
        connection.close()

    response = render_dashboard_path(settings, "/live?lang=en")

    assert response.status.value == 200
    assert "Model trust" in response.body
    assert "collecting" in response.body
    assert "1 resolved / 1 signals" in response.body


def test_live_cancel_stale_orders_redirects_to_launchpad(tmp_path):
    settings = Settings(
        DATABASE_PATH=tmp_path / "dashboard.db",
        POLYMARKET_PRIVATE_KEY="key",
        POLYMARKET_FUNDER="funder",
        COMPLIANCE_CHECK_ENABLED=False,
    )
    Database(settings.database_path).init_schema()
    services = []

    def factory(repo):
        service = FakeOrderLifecycleService(repo)
        services.append(service)
        return service

    response = handle_dashboard_post(
        settings,
        "/live/cancel-stale-orders?lang=en",
        b"lang=en",
        None,
        order_lifecycle_service_factory=factory,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "flash=flash.live_stale_orders_cancelled" in response.headers["Location"]
    assert services[0].cancelled_stale is True


def test_live_cancel_all_orders_requires_confirmation_phrase(tmp_path):
    settings = Settings(
        DATABASE_PATH=tmp_path / "dashboard.db",
        POLYMARKET_PRIVATE_KEY="key",
        POLYMARKET_FUNDER="funder",
        COMPLIANCE_CHECK_ENABLED=False,
    )
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(
        settings,
        "/live/cancel-all-orders?lang=en",
        b"lang=en&confirmation=wrong",
        None,
        order_lifecycle_service_factory=lambda repo: FakeOrderLifecycleService(repo),
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "error.live_cancel_confirmation_required" in response.headers["Location"]


def test_live_preview_redirects_with_market_id(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(
        settings,
        "/live/preview?lang=en",
        b"lang=en&market_id=m1",
        None,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?preview_market_id=m1&lang=en")
    assert "flash=flash.live_preview_ready" in response.headers["Location"]


def test_live_override_saves_micro_live_gate(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_market(settings)

    response = handle_dashboard_post(
        settings,
        "/live/override?lang=en",
        b"lang=en&market_id=m1&max_order_usdc=2",
        None,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "flash=flash.live_override_saved" in response.headers["Location"]
    override = _strategy_override(settings, "m1", "micro-live")
    assert override["live_auto_enabled"] == 1
    assert override["max_order_usdc"] == 2.0


def test_live_propose_requires_confirmation_phrase(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(
        settings,
        "/live/propose?lang=en",
        b"lang=en&market_id=m1&confirmation=wrong",
        None,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "error.live_confirmation_required" in response.headers["Location"]


def test_live_propose_creates_pending_action_only(tmp_path):
    settings = Settings(
        DATABASE_PATH=tmp_path / "dashboard.db",
        POLYMARKET_PRIVATE_KEY="key",
        POLYMARKET_FUNDER="funder",
        COMPLIANCE_CHECK_ENABLED=False,
        LIVE_MARKET_IDS="m1",
    )
    _seed_live_candidate(settings)

    response = handle_dashboard_post(
        settings,
        "/live/propose?lang=en",
        b"lang=en&market_id=m1&ack=true&confirmation=LIVE+2+USDC",
        None,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "flash=flash.live_action_proposed" in response.headers["Location"]
    action = _latest_action(settings)
    assert action["kind"] == "trade_live"
    assert action["status"] == "pending"
    assert action["approved_at"] is None
    assert action["executed_at"] is None


def test_live_propose_is_blocked_without_accepted_preview(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db", LIVE_MARKET_IDS="")
    _seed_market(settings)

    response = handle_dashboard_post(
        settings,
        "/live/propose?lang=en",
        b"lang=en&market_id=m1&ack=true&confirmation=LIVE+2+USDC",
        None,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "error.live_preview_blocked" in response.headers["Location"]


def test_live_execute_stays_locked(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(settings, "/live/execute?lang=en", b"lang=en", None)

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/live?lang=en")
    assert "error.live_execution_locked" in response.headers["Location"]


def _seed_market(settings: Settings) -> None:
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        Repository(connection).upsert_market(
            Market(
                id="m1",
                slug="m1",
                title="Live proposal candidate",
                description="NOAA station KNYC",
                yes_token_id="yes-token",
                no_token_id="no-token",
                status="active",
                is_weather=True,
            ),
            {"id": "m1"},
        )
        connection.commit()
    finally:
        connection.close()


def _seed_live_candidate(settings: Settings) -> None:
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        market = Market(
            id="m1",
            slug="m1",
            title="Live proposal candidate",
            description="Will NYC high temperature be above 80F?",
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
            is_weather=True,
        )
        now = datetime.now(timezone.utc)
        repo.upsert_market(market, {"id": "m1"})
        repo.save_resolution_rule(
            "m1",
            ResolutionRule(
                raw_text=market.description,
                location="New York",
                source="NOAA",
                station="KNYC",
                variable="temperature_high",
                threshold=Decimal("80"),
                operator=">",
                window_start="2026-06-09",
                window_end=None,
                unit="F",
                confidence=0.9,
                tradable=True,
                rejection_reason=None,
            ),
        )
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id="m1",
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.45"),
                midpoint=Decimal("0.425"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=now,
            ),
            {"id": "m1"},
        )
        repo.save_forecast(
            ForecastSnapshot(
                provider="test",
                variable="temperature_high",
                value=Decimal("83"),
                unit="F",
                issue_time=now,
                valid_time=now,
                market_id="m1",
                location="New York",
                station="KNYC",
                fetched_at=now,
            ),
            {"id": "m1"},
        )
        repo.save_analysis(
            Analysis(
                market_id="m1",
                model_version="test",
                fair_lower=Decimal("0.60"),
                fair_upper=Decimal("0.70"),
                reference_price=Decimal("0.45"),
                edge=Decimal("0.15"),
                side="buy_yes",
                decision="trade",
                reasons=["edge exists"],
            )
        )
        repo.save_order_intent(
            OrderIntent(
                market_id="m1",
                side="buy_yes",
                token_id="yes-token",
                limit_price=Decimal("0.45"),
                size=Decimal("4"),
                notional=Decimal("1.80"),
                rationale="dry run",
                dry_run=True,
                status="dry_run",
            )
        )
        repo.upsert_strategy_override(
            market_id="m1",
            profile="micro-live",
            live_auto_enabled=True,
            max_order_usdc="2",
        )
        repo.save_reconciliation("ok", {"source": "test"})
        connection.commit()
    finally:
        connection.close()


def _latest_action(settings: Settings):
    connection = Database(settings.database_path).connect()
    try:
        return Repository(connection).list_automation_actions(limit=1)[0]
    finally:
        connection.close()


def _strategy_override(settings: Settings, market_id: str, profile: str):
    connection = Database(settings.database_path).connect()
    try:
        return Repository(connection).get_strategy_override(market_id, profile)
    finally:
        connection.close()


def _seed_live_exchange_risk(settings: Settings) -> None:
    connection = Database(settings.database_path).connect()
    try:
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(seconds=600)
        connection.execute(
            """
            INSERT INTO open_orders (
                exchange_order_id, market_id, side, price, size, notional,
                status, updated_at, raw_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("order-stale", "m1", "buy", 0.5, 10, 5.0, "open", stale_time.isoformat(), "{}"),
        )
        connection.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("m1", "YES", 4, 3.2, now.isoformat()),
        )
        connection.commit()
    finally:
        connection.close()
