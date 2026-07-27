from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import WEATHER_ENTRY_POLICY_VERSION
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import RiskContext
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
)
from polymarket_weather_arb.services.trading_service import TradingService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeClient:
    def __init__(self):
        self.orders = []

    def list_markets(self, limit=100):
        return []

    def get_order_book(self, market):
        raise NotImplementedError

    def place_limit_order(self, *, token_id, side, price, size):
        self.orders.append({"token_id": token_id, "side": side, "price": price, "size": size})
        return {
            "ok": True,
            "order_id": f"0xfake{len(self.orders):064d}"[:66],
            "status": "ok",
        }

    def get_balances(self):
        return {}

    def get_positions(self):
        return []

    def get_orders(self):
        return []


class SigningBlockedClient(FakeClient):
    def validate_order_signing(self):
        return {
            "ok": False,
            "status": "missing-sdk",
            "detail": "polymarket-client is not importable",
        }


def test_dry_run_records_intent_without_submitting(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        repo.upsert_market(
            SimpleNamespace(
                id="m1",
                slug="m1",
                title="Test market",
                description="NOAA station KNYC",
                event_slug=None,
                event_title=None,
                category=None,
                tags=(),
                yes_token_id="yes-token",
                no_token_id="no-token",
                close_time=None,
                status="active",
                is_weather=True,
            ),
            {"id": "m1"},
        )
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "test.db"), client, repo)
        analysis = Analysis(
            market_id="m1",
            model_version="test",
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

        intent_id, reasons = service.trade(
            analysis=analysis,
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=context,
            dry_run=True,
        )
        connection.commit()

        assert intent_id is not None
        assert reasons == ["dry-run order recorded"]
        assert client.orders == []
        rows = repo.list_recent_order_intents()
        assert rows[0]["dry_run"] == 1
        assert rows[0]["status"] == "dry_run"
        assert rows[0]["entry_policy_version"] == WEATHER_ENTRY_POLICY_VERSION
    finally:
        connection.close()


def test_risk_rejection_is_audited_without_creating_order_intent(tmp_path):
    database = Database(tmp_path / "risk-reject.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(
            Settings(
                _env_file=None,
                database_path=tmp_path / "risk-reject.db",
                MAX_MARKET_USDC=Decimal("1"),
            ),
            client,
            repo,
        )

        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=replace(_fresh_context(), market_live_exposure=Decimal("1")),
            dry_run=False,
            source_grade="official_forecast",
        )

        assert intent_id is None
        assert any("market exposure exceeds" in reason for reason in reasons)
        assert repo.list_recent_order_intents() == []
        assert len(repo.list_recent_risk_decisions()) == 1
        assert client.orders == []
    finally:
        connection.close()


def test_live_trade_rejects_research_forecast_source(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        repo.upsert_market(
            SimpleNamespace(
                id="m1",
                slug="m1",
                title="Test market",
                description="NOAA station KNYC",
                event_slug=None,
                event_title=None,
                category=None,
                tags=(),
                yes_token_id="yes-token",
                no_token_id="no-token",
                close_time=None,
                status="active",
                is_weather=True,
            ),
            {"id": "m1"},
        )
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "test.db"), client, repo)

        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="research_forecast",
        )
        connection.commit()

        assert intent_id is not None
        assert client.orders == []
        assert any("official_forecast" in r for r in reasons)
        assert repo.list_recent_order_intents()[0]["status"] == "rejected"
    finally:
        connection.close()


def test_live_trade_accepts_explicit_bucket_research_forecast_policy(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "test.db"), client, repo)

        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="research_forecast",
            allow_research_forecast_live=True,
        )

        assert intent_id is not None
        assert reasons == ["live order submitted"]
        assert len(client.orders) == 1
        submitted = repo.list_recent_order_intents()[0]
        assert submitted["status"] == "submitted"
        assert submitted["entry_policy_version"] == WEATHER_ENTRY_POLICY_VERSION
    finally:
        connection.close()


def test_live_trade_rejects_legacy_settlement_grade_and_unknown(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "test.db"), client, repo)

        for grade in ("settlement_grade", "settlement_observation", "unknown", None, ""):
            client.orders.clear()
            intent_id, reasons = service.trade(
                analysis=_trade_analysis(),
                yes_token_id="yes-token",
                no_token_id="no-token",
                context=_fresh_context(),
                dry_run=False,
                source_grade=grade or "unknown",
            )
            assert intent_id is not None
            assert client.orders == []
            assert any("official_forecast" in r or "settlement observation" in r for r in reasons)
    finally:
        connection.close()


def test_live_trade_rejects_when_source_grade_default_omitted(tmp_path):
    """Missing source_grade must default to unknown and never place a live order."""
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "test.db"), client, repo)

        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
        )
        connection.commit()

        assert intent_id is not None
        assert client.orders == []
        assert any("official_forecast" in r for r in reasons)
        assert any("refresh" in r or "unknown" in r for r in reasons)
        assert repo.list_recent_order_intents()[0]["status"] == "rejected"
    finally:
        connection.close()


def test_live_trade_accepts_official_forecast_source(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        repo.upsert_market(
            SimpleNamespace(
                id="m1",
                slug="m1",
                title="Test market",
                description="NOAA station KNYC",
                event_slug=None,
                event_title=None,
                category=None,
                tags=(),
                yes_token_id="yes-token",
                no_token_id="no-token",
                close_time=None,
                status="active",
                is_weather=True,
            ),
            {"id": "m1"},
        )
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "test.db"), client, repo)

        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )
        connection.commit()

        assert intent_id is not None
        assert reasons == ["live order submitted"]
        assert client.orders
    finally:
        connection.close()


def test_live_trade_rejects_when_order_signing_path_is_blocked(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    client = SigningBlockedClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "test.db"), client, repo)

        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )
        connection.commit()

        assert intent_id is not None
        assert reasons == ["polymarket-client is not importable"]
        assert client.orders == []
        assert repo.list_recent_order_intents(limit=1)[0]["status"] == "rejected"
    finally:
        connection.close()


def test_live_trade_rejects_duplicate_active_intent(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "test.db"), client, repo)

        first_intent_id, first_reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )
        second_intent_id, second_reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )
        connection.commit()

        assert first_intent_id is not None
        assert first_reasons == ["live order submitted"]
        assert second_intent_id is not None
        assert second_reasons == ["duplicate active live order intent for market m1 side buy_yes"]
        assert len(client.orders) == 1
        rows = repo.list_recent_order_intents(limit=2)
        assert rows[0]["status"] == "rejected"
        assert rows[1]["status"] == "submitted"
    finally:
        connection.close()


def test_live_trade_rejects_second_bucket_in_same_city_date_event(tmp_path):
    database = Database(tmp_path / "event-exposure.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        for market_id, bucket in (("m1", "80-81F"), ("m2", "82-83F")):
            repo.upsert_market(
                SimpleNamespace(
                    id=market_id,
                    slug=market_id,
                    title=(
                        f"Will the highest temperature in New York be {bucket} "
                        "on December 31, 2099?"
                    ),
                    description="NOAA station KNYC",
                    event_slug=None,
                    event_title=None,
                    category=None,
                    tags=(),
                    yes_token_id=f"yes-{market_id}",
                    no_token_id=f"no-{market_id}",
                    close_time=None,
                    status="active",
                    is_weather=True,
                ),
                {"id": market_id},
            )
            rule = parse_global_temperature_bucket_rule(
                (f"Will the highest temperature in New York be {bucket} on December 31, 2099?"),
                "Settlement source: NOAA station KNYC.",
            )
            repo.save_temperature_bucket_rule(
                market_id,
                rule,
                module_id="global_temp_bucket",
            )
        repo.replace_positions(
            [
                {
                    "market": "m1",
                    "token_id": "yes-m1",
                    "outcome": "Yes",
                    "size": "5",
                    "current_value": "1",
                }
            ]
        )
        service = TradingService(
            Settings(DATABASE_PATH=tmp_path / "event-exposure.db"), client, repo
        )

        intent_id, reasons = service.trade(
            analysis=replace(_trade_analysis(), market_id="m2"),
            yes_token_id="yes-m2",
            no_token_id="no-m2",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )

        assert intent_id is not None
        assert client.orders == []
        assert "active exposure in market m1" in reasons[0]
        assert "only the best bucket per city/date" in reasons[0]
        assert repo.list_recent_order_intents(limit=1)[0]["status"] == "rejected"
    finally:
        connection.close()


def test_live_trade_rejects_duplicate_exchange_open_order(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        repo.connection.execute(
            """
            INSERT INTO open_orders (
                exchange_order_id, market_id, token_id, side, price, size, notional,
                status, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("exchange-order-1", "m1", "yes-token", "BUY", 0.5, 10, 5, "open", "{}"),
        )
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "test.db"), client, repo)

        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )
        connection.commit()

        assert intent_id is not None
        assert reasons == [
            "duplicate exchange open order exchange-order-1 for market m1 token yes-token"
        ]
        assert client.orders == []
        assert repo.list_recent_order_intents(limit=1)[0]["status"] == "rejected"
    finally:
        connection.close()


def test_live_trade_same_analyzed_opportunity_is_idempotent_after_cancel(tmp_path):
    database = Database(tmp_path / "same-opportunity.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(
            Settings(DATABASE_PATH=tmp_path / "same-opportunity.db"), client, repo
        )
        analysis = _trade_analysis()

        first_id, _ = service.trade(
            analysis=analysis,
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )
        repo.update_order_intent_status(first_id, "cancelled")
        second_id, reasons = service.trade(
            analysis=analysis,
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )

        assert second_id == first_id
        assert reasons == [f"duplicate analyzed opportunity already recorded as intent {first_id}"]
        assert len(client.orders) == 1
    finally:
        connection.close()


def test_live_trade_new_analysis_can_submit_after_cancel(tmp_path):
    database = Database(tmp_path / "new-opportunity.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(
            Settings(DATABASE_PATH=tmp_path / "new-opportunity.db"), client, repo
        )
        analysis = _trade_analysis()

        first_id, _ = service.trade(
            analysis=analysis,
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )
        repo.update_order_intent_status(first_id, "cancelled")
        second_id, reasons = service.trade(
            analysis=replace(analysis, created_at=analysis.created_at + timedelta(minutes=1)),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )

        assert second_id != first_id
        assert reasons == ["live order submitted"]
        assert len(client.orders) == 2
        keys = [row["idempotency_key"] for row in repo.list_recent_order_intents(limit=2)]
        assert len(set(keys)) == 2
    finally:
        connection.close()


def test_live_trade_same_forecast_revision_is_idempotent_across_analyses(tmp_path):
    database = Database(tmp_path / "same-forecast-revision.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(
            Settings(DATABASE_PATH=tmp_path / "same-forecast-revision.db"), client, repo
        )
        analysis = _trade_analysis()

        first_id, _ = service.trade(
            analysis=analysis,
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
            opportunity_id="forecast:revision-1",
        )
        repo.update_order_intent_status(first_id, "filled")
        second_id, reasons = service.trade(
            analysis=replace(analysis, created_at=analysis.created_at + timedelta(minutes=10)),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
            opportunity_id="forecast:revision-1",
        )

        assert second_id == first_id
        assert reasons == [f"duplicate analyzed opportunity already recorded as intent {first_id}"]
        assert len(client.orders) == 1
    finally:
        connection.close()


def test_live_trade_rejects_explicit_ok_false_without_ghost_submitted(tmp_path):
    database = Database(tmp_path / "reject-ok-false.db")
    database.init_schema()
    connection = database.connect()

    class RejectClient(FakeClient):
        def place_limit_order(self, *, token_id, side, price, size):
            self.orders.append(
                {"token_id": token_id, "side": side, "price": price, "size": size}
            )
            return {
                "ok": False,
                "error": "invalid post-only order: would cross the book",
                "status": "rejected",
            }

    client = RejectClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(
            Settings(DATABASE_PATH=tmp_path / "reject-ok-false.db"), client, repo
        )
        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )
        assert intent_id is not None
        assert any("not accepted" in r for r in reasons)
        assert any("post_only_would_cross" in r for r in reasons)
        row = repo.list_recent_order_intents(limit=1)[0]
        assert row["status"] == "failed"
        attempt = connection.execute(
            "SELECT status, error FROM order_attempts WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        assert attempt["status"] == "rejected"
        assert attempt["error"] == "post_only_would_cross"
    finally:
        connection.close()


def test_live_trade_rejects_missing_order_id_without_ghost_submitted(tmp_path):
    database = Database(tmp_path / "reject-no-id.db")
    database.init_schema()
    connection = database.connect()

    class NoIdClient(FakeClient):
        def place_limit_order(self, *, token_id, side, price, size):
            self.orders.append(
                {"token_id": token_id, "side": side, "price": price, "size": size}
            )
            return {"ok": True, "status": "ok"}

    client = NoIdClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(
            Settings(DATABASE_PATH=tmp_path / "reject-no-id.db"), client, repo
        )
        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade="official_forecast",
        )
        assert intent_id is not None
        assert any("missing_order_id" in r for r in reasons)
        assert repo.list_recent_order_intents(limit=1)[0]["status"] == "failed"
        attempt = connection.execute(
            "SELECT status, error FROM order_attempts WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        assert attempt["status"] == "failed"
        assert attempt["error"] == "missing_order_id"
    finally:
        connection.close()


def test_live_trade_commits_submitted_audit_before_roundtrip_binding(tmp_path):
    database = Database(tmp_path / "durable-submit.db")
    database.init_schema()
    connection = database.connect()
    client = FakeClient()
    repo = Repository(connection)
    _seed_market(repo)

    def fail_roundtrip(*_args, **_kwargs):
        raise RuntimeError("roundtrip write failed")

    repo.record_roundtrip_buy_intent = fail_roundtrip  # type: ignore[method-assign]
    service = TradingService(Settings(DATABASE_PATH=tmp_path / "durable-submit.db"), client, repo)
    intent_id, reasons = service.trade(
        analysis=_trade_analysis(),
        yes_token_id="yes-token",
        no_token_id="no-token",
        context=_fresh_context(),
        dry_run=False,
        source_grade="official_forecast",
        on_submitted=lambda _intent_id: connection.commit(),
    )
    assert intent_id is not None
    assert "live order submitted" in reasons
    connection.close()

    check_connection = database.connect()
    try:
        row = Repository(check_connection).list_recent_order_intents(limit=1)[0]
        assert row["id"] == intent_id
        assert row["status"] == "submitted"
        assert row["entry_policy_version"] == WEATHER_ENTRY_POLICY_VERSION
        attempt = check_connection.execute(
            "SELECT status FROM order_attempts WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        assert attempt["status"] == "submitted"
    finally:
        check_connection.close()


def _trade_analysis() -> Analysis:
    return Analysis(
        market_id="m1",
        model_version="test",
        fair_lower=Decimal("0.8"),
        fair_upper=Decimal("0.9"),
        reference_price=Decimal("0.5"),
        edge=Decimal("0.2"),
        side="buy_yes",
        decision="trade",
        reasons=["test"],
    )


def _seed_market(repo: Repository) -> None:
    repo.upsert_market(
        SimpleNamespace(
            id="m1",
            slug="m1",
            title="Test market",
            description="NOAA station KNYC",
            event_slug=None,
            event_title=None,
            category=None,
            tags=(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            close_time=None,
            status="active",
            is_weather=True,
        ),
        {"id": "m1"},
    )


def _fresh_context() -> RiskContext:
    return RiskContext(
        daily_live_notional=Decimal("0"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=1,
        forecast_age_seconds=1,
        rule_tradable=True,
        reconciliation_fresh=True,
    )


def test_forecast_source_grade_noaa():
    """NOAA forecast payloads are official_forecast (not settlement observation)."""
    import json
    from polymarket_weather_arb.cli import _forecast_source_grade

    forecast_row = {
        "raw_payload": json.dumps(
            {
                "source": "noaa_nws",
                "station_id": "OKX/82,75",
                "source_grade": "official_forecast",
                "official_signal": True,
            }
        )
    }

    assert _forecast_source_grade(forecast_row) == "official_forecast"


def test_forecast_source_grade_open_meteo():
    """Open-Meteo without grade is unknown; with research_forecast stays research."""
    import json
    from polymarket_weather_arb.cli import _forecast_source_grade

    forecast_row = {
        "raw_payload": json.dumps(
            {
                "geocoding": {"results": [{"latitude": 40.7, "longitude": -74.0}]},
                "forecast": {"daily": {"time": ["2026-06-03"], "temperature_2m_max": [75]}},
            }
        )
    }
    assert _forecast_source_grade(forecast_row) == "unknown"

    graded = {
        "raw_payload": json.dumps(
            {
                "source_grade": "research_forecast",
                "official_signal": False,
            }
        )
    }
    assert _forecast_source_grade(graded) == "research_forecast"


def test_forecast_source_grade_missing_payload():
    """Missing payload is unknown and never live-eligible by default."""
    from polymarket_weather_arb.cli import _forecast_source_grade

    forecast_row = {"raw_payload": None}
    assert _forecast_source_grade(forecast_row) == "unknown"

    forecast_row = {}
    assert _forecast_source_grade(forecast_row) == "unknown"


def test_forecast_source_grade_invalid_json():
    """Invalid JSON is unknown (safe reject for live)."""
    from polymarket_weather_arb.cli import _forecast_source_grade

    forecast_row = {"raw_payload": "invalid json"}
    assert _forecast_source_grade(forecast_row) == "unknown"


def test_forecast_source_grade_legacy_settlement_grade_not_promoted():
    """Old settlement_grade forecast rows require refresh; not live-eligible."""
    import json
    from polymarket_weather_arb.cli import _forecast_source_grade

    forecast_row = {
        "raw_payload": json.dumps(
            {
                "source_grade": "settlement_grade",
                "official_signal": True,
            }
        )
    }
    assert _forecast_source_grade(forecast_row) == "legacy"
