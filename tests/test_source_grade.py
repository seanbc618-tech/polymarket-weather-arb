"""Regression tests for forecast vs settlement provenance grades."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from polymarket_weather_arb.adapters.weather.noaa import NoaaProvider
from polymarket_weather_arb.adapters.weather.open_meteo import OpenMeteoProvider
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import RiskContext
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.source_grade import (
    LEGACY,
    OFFICIAL_FORECAST,
    RESEARCH_FORECAST,
    SETTLEMENT_OBSERVATION,
    UNKNOWN,
    extract_forecast_source_grade,
    is_live_eligible_forecast_grade,
    live_forecast_rejection_reason,
    normalize_source_grade,
)
from polymarket_weather_arb.services.trading_service import TradingService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_normalize_maps_legacy_tokens():
    assert normalize_source_grade("research_grade") == RESEARCH_FORECAST
    assert normalize_source_grade("settlement_grade") == LEGACY
    assert normalize_source_grade("signal_only") == UNKNOWN
    assert normalize_source_grade(None) == UNKNOWN
    assert normalize_source_grade("official_forecast") == OFFICIAL_FORECAST
    assert normalize_source_grade("settlement_observation") == SETTLEMENT_OBSERVATION


def test_live_eligible_only_official_forecast():
    assert is_live_eligible_forecast_grade(OFFICIAL_FORECAST)
    assert not is_live_eligible_forecast_grade(RESEARCH_FORECAST)
    assert not is_live_eligible_forecast_grade("research_grade")
    assert not is_live_eligible_forecast_grade("settlement_grade")
    assert not is_live_eligible_forecast_grade(SETTLEMENT_OBSERVATION)
    assert not is_live_eligible_forecast_grade(None)
    assert not is_live_eligible_forecast_grade("")
    assert live_forecast_rejection_reason(SETTLEMENT_OBSERVATION)
    assert "official_forecast" in (live_forecast_rejection_reason("settlement_grade") or "")


def test_extract_does_not_promote_official_signal_alone():
    assert extract_forecast_source_grade({"official_signal": True}) == UNKNOWN
    assert (
        extract_forecast_source_grade(
            {"source_grade": "official_forecast", "official_signal": True}
        )
        == OFFICIAL_FORECAST
    )
    assert extract_forecast_source_grade(None) == UNKNOWN
    assert extract_forecast_source_grade({}) == UNKNOWN
    # Legacy DB rows keep settlement_grade but are not live-eligible.
    assert extract_forecast_source_grade({"source_grade": "settlement_grade"}) == LEGACY
    assert not is_live_eligible_forecast_grade(
        extract_forecast_source_grade({"source_grade": "settlement_grade"})
    )


def _noaa_rule(**overrides) -> ResolutionRule:
    defaults = {
        "raw_text": "NYC temperature high",
        "location": "New York",
        "source": "OKX/82,75",
        "station": "OKX",
        "variable": "temperature_high",
        "threshold": None,
        "operator": None,
        "window_start": "2026-06-03",
        "window_end": None,
        "unit": "F",
        "confidence": 0.9,
        "tradable": True,
        "rejection_reason": None,
    }
    defaults.update(overrides)
    return ResolutionRule(**defaults)


@patch("httpx.Client")
def test_noaa_forecast_is_official_forecast_not_settlement(mock_client_class):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "properties": {
            "periods": [
                {
                    "startTime": "2026-06-03T06:00:00-04:00",
                    "isDaytime": True,
                    "temperature": 75,
                    "temperatureUnit": "F",
                    "name": "Tuesday",
                }
            ]
        }
    }
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    snapshot, raw_payload = NoaaProvider().fetch_forecast("m1", _noaa_rule())
    assert raw_payload["source_grade"] == OFFICIAL_FORECAST
    assert raw_payload["source_grade"] != SETTLEMENT_OBSERVATION
    assert raw_payload["source_grade"] != "settlement_grade"
    assert extract_forecast_source_grade(raw_payload) == OFFICIAL_FORECAST
    assert is_live_eligible_forecast_grade(raw_payload["source_grade"])
    assert snapshot.value == Decimal("75")


@patch("httpx.Client")
def test_open_meteo_forecast_is_research_forecast(mock_client_class):
    geocode = MagicMock()
    geocode.json.return_value = {
        "results": [
            {
                "latitude": 40.7,
                "longitude": -74.0,
                "name": "New York",
                "timezone": "America/New_York",
            }
        ]
    }
    geocode.raise_for_status = MagicMock()
    forecast = MagicMock()
    forecast.json.return_value = {"daily": {"time": ["2026-06-03"], "temperature_2m_max": [75.0]}}
    forecast.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get.side_effect = [geocode, forecast]
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    rule = ResolutionRule(
        raw_text="NYC high",
        location="New York",
        source="Open-Meteo",
        station=None,
        variable="temperature_high",
        operator=">=",
        threshold=Decimal("80"),
        unit="F",
        window_start="2026-06-03",
        window_end="2026-06-03",
        confidence=0.5,
        tradable=True,
        rejection_reason=None,
    )
    snapshot, raw_payload = OpenMeteoProvider().fetch_forecast("m1", rule)
    assert raw_payload["source_grade"] == RESEARCH_FORECAST
    assert raw_payload["target_date"] == "2026-06-03"
    assert not is_live_eligible_forecast_grade(raw_payload["source_grade"])
    assert snapshot.value == Decimal("75.0")
    forecast_call = mock_client.get.call_args_list[1]
    assert forecast_call.kwargs["params"]["past_days"] == 1


@patch("httpx.Client")
def test_open_meteo_station_market_uses_exact_airport_coordinates(mock_client_class, monkeypatch):
    forecast = MagicMock()
    forecast.json.return_value = {"daily": {"time": ["2026-07-18"], "temperature_2m_max": [30.0]}}
    forecast.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get.return_value = forecast
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.open_meteo.fetch_awc_station_location",
        lambda station: (36.362, 120.087, {"icaoId": station}),
    )
    rule = _noaa_rule(
        raw_text="Highest temperature in Qingdao; Wunderground station ZSQD",
        location="Qingdao",
        station="ZSQD",
        source="Wunderground",
        unit="C",
        window_start="2026-07-18",
        window_end="2026-07-18",
    )

    snapshot, raw_payload = OpenMeteoProvider().fetch_forecast("qingdao", rule)

    params = mock_client.get.call_args.kwargs["params"]
    assert (params["latitude"], params["longitude"]) == (36.362, 120.087)
    assert params["timezone"] == "Asia/Shanghai"
    assert snapshot.station == "ZSQD"
    assert raw_payload["coordinate_source"] == "awc_stationinfo"
    assert raw_payload["forecast_station"] == "ZSQD"


@patch("httpx.Client")
def test_open_meteo_timezone_validation(mock_client_class):
    geocode = MagicMock()
    # Missing timezone
    geocode.json.return_value = {
        "results": [{"latitude": 39.9, "longitude": 116.4, "name": "Beijing"}]
    }
    geocode.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get.return_value = geocode
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    rule = ResolutionRule(
        raw_text="Beijing high",
        location="Beijing",
        source="Open-Meteo",
        station=None,
        variable="temperature_high",
        operator=">=",
        threshold=Decimal("30"),
        unit="C",
        window_start="2026-07-13",
        window_end="2026-07-13",
        confidence=0.5,
        tradable=True,
        rejection_reason=None,
    )

    with pytest.raises(ValueError, match="missing timezone for location: Beijing"):
        OpenMeteoProvider().fetch_forecast("m1", rule)

    # Invalid timezone
    geocode.json.return_value = {
        "results": [
            {"latitude": 39.9, "longitude": 116.4, "name": "Beijing", "timezone": "Asia/FakeZone"}
        ]
    }
    with pytest.raises(ValueError, match="invalid timezone 'Asia/FakeZone' for location: Beijing"):
        OpenMeteoProvider().fetch_forecast("m1", rule)


@patch("httpx.Client")
def test_open_meteo_can_select_prior_utc_day_for_american_local_day(mock_client_class):
    geocode = MagicMock()
    geocode.json.return_value = {
        "results": [
            {
                "latitude": 32.78,
                "longitude": -96.81,
                "name": "Dallas",
                "timezone": "America/Chicago",
            }
        ]
    }
    geocode.raise_for_status = MagicMock()
    forecast = MagicMock()
    forecast.json.return_value = {
        "daily": {
            "time": ["2026-07-12", "2026-07-13"],
            "temperature_2m_max": [101.0, 99.0],
        }
    }
    forecast.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.get.side_effect = [geocode, forecast]
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    rule = ResolutionRule(
        raw_text="Dallas high",
        location="Dallas",
        source="Open-Meteo",
        station=None,
        variable="temperature_high",
        operator=">=",
        threshold=Decimal("100"),
        unit="F",
        window_start="2026-07-12",
        window_end="2026-07-12",
        confidence=0.5,
        tradable=True,
        rejection_reason=None,
    )

    snapshot, _ = OpenMeteoProvider().fetch_forecast("dallas", rule)

    assert snapshot.value == Decimal("101.0")


def test_legacy_raw_payload_without_grade_is_unknown_and_blocks_live(tmp_path):
    database = Database(tmp_path / "legacy.db")
    database.init_schema()
    connection = database.connect()
    client = _FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        grade = extract_forecast_source_grade({})
        assert grade == UNKNOWN
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "legacy.db"), client, repo)
        intent_id, reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade=grade,
        )
        assert intent_id is not None
        assert client.orders == []
        assert any("official_forecast" in r for r in reasons)
        assert any("refresh" in r for r in reasons)
    finally:
        connection.close()


def test_trading_service_rejects_research_and_accepts_official(tmp_path):
    database = Database(tmp_path / "gate.db")
    database.init_schema()
    connection = database.connect()
    client = _FakeClient()
    try:
        repo = Repository(connection)
        _seed_market(repo)
        service = TradingService(Settings(DATABASE_PATH=tmp_path / "gate.db"), client, repo)

        _, reject_reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade=RESEARCH_FORECAST,
        )
        assert client.orders == []
        assert any("official_forecast" in r for r in reject_reasons)

        _, accept_reasons = service.trade(
            analysis=_trade_analysis(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=_fresh_context(),
            dry_run=False,
            source_grade=OFFICIAL_FORECAST,
        )
        assert accept_reasons == ["live order submitted"]
        assert client.orders
    finally:
        connection.close()


class _FakeClient:
    def __init__(self):
        self.orders = []

    def place_limit_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"status": "live", "id": f"order-{len(self.orders)}"}


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


def _fresh_context() -> RiskContext:
    return RiskContext(
        daily_live_notional=Decimal("0"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=1,
        forecast_age_seconds=1,
        rule_tradable=True,
        reconciliation_fresh=True,
    )
