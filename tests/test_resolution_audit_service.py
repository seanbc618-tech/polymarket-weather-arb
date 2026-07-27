from unittest.mock import MagicMock
import json
import pytest
from pathlib import Path

from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.services.circuit_breaker_service import CircuitBreakerService
from polymarket_weather_arb.services.resolution_audit_service import ResolutionAuditService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


@pytest.fixture
def repository(tmp_path: Path):
    db = Database(path=tmp_path / "test.db")
    db.init_schema()
    with db.connect() as conn:
        yield Repository(conn)


@pytest.fixture
def polymarket_client():
    return MagicMock()


@pytest.fixture
def circuit_breaker_service(repository):
    return CircuitBreakerService(repository)


@pytest.fixture
def resolution_audit_service(repository, polymarket_client, circuit_breaker_service):
    return ResolutionAuditService(
        repository=repository,
        polymarket_client=polymarket_client,
        circuit_breaker_service=circuit_breaker_service,
    )


def test_audit_market_unavailable(resolution_audit_service, polymarket_client):
    polymarket_client.get_market.return_value = None

    audit = resolution_audit_service.audit_market("m1")
    assert audit.status == "unavailable"
    assert audit.match is True


def test_unresolved_audit_compacts_gamma_payload_with_full_hash(
    resolution_audit_service, repository, polymarket_client
):
    repository.connection.execute(
        "INSERT INTO markets (id, title, raw_payload) VALUES ('m1', 'Weather', '{}')"
    )
    payload = {
        "id": "m1",
        "question": "Weather question",
        "closed": False,
        "umaResolutionStatus": "unresolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.4", "0.6"]',
        "description": "large vendor payload" * 1000,
    }
    polymarket_client.get_market.return_value = (MagicMock(), payload)

    audit = resolution_audit_service.audit_market("m1")

    stored = repository.latest_resolution_audit("m1")
    compact = json.loads(stored["raw_polymarket_payload"])
    assert audit.status == "unavailable"
    assert compact["question"] == "Weather question"
    assert compact["outcomePrices"] == '["0.4", "0.6"]'
    assert len(compact["payload_sha256"]) == 64
    assert "description" not in compact
    assert len(stored["raw_polymarket_payload"]) < 1000


def test_audit_market_mismatch_trips_breaker(
    resolution_audit_service, repository, polymarket_client, circuit_breaker_service
):
    # Setup local outcome
    repository.connection.execute(
        "INSERT INTO markets (id, title, raw_payload) VALUES ('m1', 'title', '{}')"
    )
    repository.connection.execute(
        "INSERT INTO analyses (id, market_id, model_version, fair_lower, fair_upper, edge, decision, reasons) VALUES (1, 'm1', 'v1', 0, 0, 0, 'pass', '[]')"
    )
    repository.connection.execute(
        "INSERT INTO model_signals (analysis_id, market_id, model_version, yes_probability, fair_lower, fair_upper, edge, decision, outcome_status, resolved_outcome, raw_payload) VALUES (1, 'm1', 'v1', 0, 0, 0, 0, 'pass', 'resolved', 'no', '{}')"
    )

    # Setup PM outcome
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
    }
    polymarket_client.get_market.return_value = (MagicMock(), payload)

    audit = resolution_audit_service.audit_market("m1")
    assert audit.status == "mismatch"
    assert not audit.match

    status = circuit_breaker_service.status()
    assert status.tripped
    assert "Resolution mismatch" in status.reason


def test_audit_market_recomputes_from_observation(
    resolution_audit_service, repository, polymarket_client, circuit_breaker_service
):
    # Set up rule in DB
    from polymarket_weather_arb.domain.rules import ResolutionRule

    rule = ResolutionRule(
        raw_text="Test",
        location="NYC",
        station=None,
        source="NOAA",
        variable="temperature",
        threshold=50.0,
        operator=">",
        unit="F",
        window_start=None,
        window_end=None,
        confidence=1.0,
        tradable=True,
        rejection_reason=None,
    )
    repository.connection.execute(
        "INSERT INTO markets (id, title, raw_payload) VALUES ('m1', 'title', '{}')"
    )
    repository.save_resolution_rule("m1", rule)

    # Save a latest weather observation (temperature = 51.0 -> should resolve 'yes')
    repository.connection.execute(
        "INSERT INTO weather_observations (market_id, provider, variable, unit, value, raw_payload, observed_at, fetched_at) VALUES ('m1', 'noaa', 'temperature', 'F', 51.0, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )

    # Add a conflicting model_signal that is stale and says 'no'
    repository.connection.execute(
        "INSERT INTO analyses (id, market_id, model_version, fair_lower, fair_upper, edge, decision, reasons) VALUES (1, 'm1', 'v1', 0, 0, 0, 'pass', '[]')"
    )
    repository.connection.execute(
        "INSERT INTO model_signals (analysis_id, market_id, model_version, yes_probability, fair_lower, fair_upper, edge, decision, outcome_status, resolved_outcome, raw_payload) VALUES (1, 'm1', 'v1', 0, 0, 0, 0, 'pass', 'resolved', 'no', '{}')"
    )

    # Setup PM outcome as 'yes'
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
    }
    polymarket_client.get_market.return_value = (MagicMock(), payload)

    audit = resolution_audit_service.audit_market("m1")

    # It should compute from observation (51 > 50 = yes) and match PM (yes)
    assert audit.status == "match"
    assert audit.match is True
    assert audit.local_source == "recomputed_observation"
    assert audit.local_resolved_outcome == "yes"

    status = circuit_breaker_service.status()
    assert not status.tripped


def test_polymarket_winner_settles_pending_calibration_signals(
    resolution_audit_service, repository, polymarket_client
):
    repository.connection.execute(
        "INSERT INTO markets (id, title, module_id, raw_payload) "
        "VALUES ('m1', 'bucket', 'global_temp_bucket', '{}')"
    )
    repository.connection.execute(
        "INSERT INTO model_signals "
        "(market_id, model_version, forecast_provider, yes_probability, fair_lower, "
        "fair_upper, edge, decision, outcome_status, raw_payload) "
        "VALUES ('m1', 'v5', 'ensemble', 0.7, 0.5, 0.8, 0.2, 'trade', 'pending', '{}')"
    )
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
    }
    polymarket_client.get_market.return_value = (MagicMock(), payload)

    audit = resolution_audit_service.audit_market("m1")

    signal = repository.latest_model_signal("m1")
    assert audit.updated_signals == 1
    assert signal["outcome_status"] == "resolved"
    assert signal["resolved_outcome"] == "yes"
    assert signal["settlement_source"] == "polymarket_gamma_resolution"

    repeated = resolution_audit_service.audit_market("m1")
    assert repeated.local_source == "none"
    assert repeated.local_resolved_outcome is None
    assert repeated.updated_signals == 0


def test_audit_event_uses_one_event_read_and_preserves_module(
    resolution_audit_service, repository, polymarket_client
):
    market = Market(
        id="m1",
        title="Will the high be 30C?",
        event_slug="weather-event",
        is_weather=True,
    )
    repository.upsert_market(market, {"closed": False}, module_id="global_temp_bucket")
    repository.connection.execute(
        "INSERT INTO model_signals "
        "(market_id, model_version, forecast_provider, yes_probability, fair_lower, "
        "fair_upper, edge, decision, outcome_status, raw_payload) "
        "VALUES ('m1', 'v5', 'ensemble', 0.3, 0.2, 0.4, 0.1, 'trade', 'pending', '{}')"
    )
    payload = {
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0", "1"]',
    }
    polymarket_client.get_event_markets_by_slug.return_value = [(market, payload)]

    results = resolution_audit_service.audit_event("weather-event")

    assert len(results) == 1
    assert results[0].updated_signals == 1
    assert repository.latest_model_signal("m1")["resolved_outcome"] == "no"
    assert repository.get_market("m1")["module_id"] == "global_temp_bucket"
    polymarket_client.get_event_markets_by_slug.assert_called_once_with("weather-event")
