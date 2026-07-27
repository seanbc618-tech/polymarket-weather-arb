from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import handle_dashboard_post, render_dashboard_path
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.weather import ForecastSnapshot, WeatherObservation
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_calibration_dashboard_renders_report_and_recent_signals(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_signal(settings)
    _settle_signal(settings)
    _seed_observation(settings)

    response = render_dashboard_path(settings, "/calibration?lang=en")

    assert response.status.value == 200
    assert "Calibration" in response.body
    assert "Model Scoreboard" in response.body
    assert "Distinct events" in response.body
    assert "LLM Weight" in response.body
    assert "Weight reason" in response.body
    assert "weather-threshold-v1" in response.body
    assert "noaa-nws" in response.body
    assert "collecting" in response.body
    assert "Recent Signals" in response.body
    assert "Manual Settlement" in response.body
    assert "Official Observation Backfill" in response.body
    assert "Recent Observations" in response.body
    assert "KNYC" in response.body
    assert "83" in response.body
    assert "V" in response.body


def test_calibration_settle_post_updates_signals_and_redirects(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_signal(settings)

    response = handle_dashboard_post(
        settings,
        "/calibration/settle?lang=en",
        b"lang=en&market_id=m1&outcome=yes&settlement_value=83&settlement_source=nws-observation",
        None,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/calibration?lang=en")
    assert "flash=flash.calibration_settled" in response.headers["Location"]
    connection = Database(settings.database_path).connect()
    try:
        signal = Repository(connection).latest_model_signal("m1")
        assert signal["outcome_status"] == "resolved"
        assert signal["resolved_outcome"] == "yes"
        assert signal["settlement_source"] == "nws-observation"
    finally:
        connection.close()


def test_calibration_settle_post_requires_market_and_valid_outcome(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(
        settings,
        "/calibration/settle?lang=en",
        b"lang=en&market_id=&outcome=maybe",
        None,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/calibration?lang=en")
    assert "error.calibration_market_required" in response.headers["Location"]


def test_calibration_backfill_post_uses_settlement_service_and_redirects(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_signal(settings)
    calls = []

    def fake_settlement_service_factory(repository):
        class FakeSettlementService:
            def backfill_market(self, market_id):
                calls.append(market_id)
                repository.settle_model_signals_for_market(
                    market_id,
                    resolved_outcome="yes",
                    settlement_value=Decimal("83"),
                    settlement_source="nws-observation",
                )
                return SimpleNamespace(
                    market_id=market_id,
                    resolved_outcome="yes",
                    observation_value=Decimal("83"),
                    observation_unit="F",
                    settlement_source="nws-observation",
                    updated_signals=1,
                )

        return FakeSettlementService()

    response = handle_dashboard_post(
        settings,
        "/calibration/backfill?lang=en",
        b"lang=en&market_id=m1",
        None,
        settlement_service_factory=fake_settlement_service_factory,
    )

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/calibration?lang=en")
    assert "flash=flash.calibration_backfilled" in response.headers["Location"]
    assert calls == ["m1"]
    connection = Database(settings.database_path).connect()
    try:
        signal = Repository(connection).latest_model_signal("m1")
        assert signal["outcome_status"] == "resolved"
        assert signal["settlement_value"] == 83
        assert signal["settlement_source"] == "nws-observation"
    finally:
        connection.close()


def test_calibration_backfill_preview_post_uses_fake_service_and_redirects(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_signal(settings)
    preview_calls = []

    def fake_settlement_service_factory(repository):
        class FakeSettlementService:
            def preview_market(self, market_id):
                preview_calls.append(market_id)
                return SimpleNamespace(
                    market_id=market_id,
                    station="KNYC",
                    variable="temperature_high",
                    observed_value=Decimal("83"),
                    unit="F",
                    observed_at=None,
                    quality_status="V",
                    would_resolve_outcome="yes",
                    settlement_source="nws-observation",
                    rule_operator=">=",
                    rule_threshold=Decimal("80"),
                    warnings=[],
                )

        return FakeSettlementService()

    response = handle_dashboard_post(
        settings,
        "/calibration/backfill-preview?lang=en",
        b"lang=en&market_id=m1",
        None,
        settlement_service_factory=fake_settlement_service_factory,
    )

    assert response.status.value == 303
    location = response.headers["Location"]
    assert "/calibration?" in location
    assert "flash=flash.calibration_previewed" in location
    assert "preview_market_id=m1" in location
    assert "preview_station=KNYC" in location
    assert "preview_value=83" in location
    assert "preview_outcome=yes" in location
    assert preview_calls == ["m1"]
    # preview must not settle signals
    connection = Database(settings.database_path).connect()
    try:
        signal = Repository(connection).latest_model_signal("m1")
        assert signal["outcome_status"] != "resolved"
    finally:
        connection.close()


def test_calibration_preview_result_renders_on_page(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(
        settings,
        "/calibration?lang=en&preview_market_id=m1&preview_station=KNYC"
        "&preview_variable=temperature_high&preview_value=83&preview_unit=F"
        "&preview_quality=V&preview_outcome=yes&preview_source=nws-observation",
    )

    assert response.status.value == 200
    assert "Preview Result" in response.body
    assert "m1" in response.body
    assert "KNYC" in response.body
    assert "83" in response.body
    assert "YES" in response.body
    assert "nws-observation" in response.body


def test_calibration_preview_result_card_renders_warning_when_present(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(
        settings,
        "/calibration?lang=en&preview_market_id=m1&preview_station=KNYC"
        "&preview_variable=temperature_high&preview_value=83&preview_unit=F"
        "&preview_quality=X&preview_outcome=yes&preview_source=nws-observation"
        "&preview_warnings=low+observation+coverage:+1+usable+records|selected+observation+quality+is+X",
    )

    assert response.status.value == 200
    assert "Warnings" in response.body
    assert "low observation coverage" in response.body
    assert "selected observation quality is X" in response.body


def _seed_signal(settings: Settings) -> None:
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        repo.upsert_market(
            Market(
                id="m1",
                slug="m1",
                title="Will NYC high temperature exceed 80F?",
                description="NOAA station KNYC",
                yes_token_id="yes-token",
                no_token_id="no-token",
                status="active",
                is_weather=True,
            ),
            {"id": "m1"},
        )
        now = datetime.now(timezone.utc)
        repo.save_forecast(
            ForecastSnapshot(
                provider="noaa-nws",
                variable="temperature_high",
                value=Decimal("82"),
                unit="F",
                issue_time=now,
                valid_time=now,
                market_id="m1",
                location="New York",
                station="KNYC",
                fetched_at=now,
            ),
            {"source_grade": "official_forecast", "provider": "noaa-nws"},
        )
        repo.save_analysis(
            Analysis(
                market_id="m1",
                model_version="weather-threshold-v1",
                fair_lower=Decimal("0.60"),
                fair_upper=Decimal("0.70"),
                reference_price=Decimal("0.50"),
                edge=Decimal("0.10"),
                side="buy_yes",
                decision="trade",
                reasons=["edge"],
            )
        )
        connection.commit()
    finally:
        connection.close()


def _settle_signal(settings: Settings) -> None:
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


def _seed_observation(settings: Settings) -> None:
    connection = Database(settings.database_path).connect()
    try:
        now = datetime.now(timezone.utc)
        Repository(connection).save_observation(
            WeatherObservation(
                provider="noaa",
                market_id="m1",
                station="KNYC",
                observed_at=now,
                variable="temperature_high",
                value=Decimal("83"),
                unit="F",
                quality_status="V",
                fetched_at=now,
            ),
            {"source": "nws-observation", "source_grade": "settlement_observation"},
        )
        connection.commit()
    finally:
        connection.close()
