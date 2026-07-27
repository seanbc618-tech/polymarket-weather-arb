from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from polymarket_weather_arb.adapters.weather.open_meteo_ensemble import (
    OpenMeteoEnsembleProvider,
)
from polymarket_weather_arb.adapters.weather.open_meteo import OpenMeteoProvider
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.ensemble_weather import EnsembleForecastSnapshot
from polymarket_weather_arb.domain.global_bucket_pricing import (
    GlobalBucketPricingConfig,
    apply_temperature_biases,
    analyze_global_bucket_price,
    global_bucket_top_candidate_votes,
)
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import RiskContext
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.services.autopilot_service import _staged_entry_cap
from polymarket_weather_arb.services.market_workflow_service import MarketWorkflowService
from polymarket_weather_arb.services.trading_service import TradingService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _rule(bucket: str = "80-81F"):
    return parse_global_temperature_bucket_rule(
        f"Will the high temperature in New York be {bucket} on July 16, 2026?",
        "Settlement source: NOAA station KNYC.",
    )


def _forecast() -> ForecastSnapshot:
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    return ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal("80.5"),
        unit="F",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="m1",
        location="New York",
    )


def test_multimodel_provider_reads_local_daily_members_equally(monkeypatch):
    provider = OpenMeteoEnsembleProvider()
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.open_meteo_ensemble.fetch_awc_station_location",
        lambda station: (40.7, -74.0, {"icaoId": station}),
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "daily_units": {"temperature_2m_max": "°C"},
        "daily": {
            "time": ["2026-07-16"],
            "temperature_2m_max_ncep_gefs_seamless": [27.0],
            "temperature_2m_max_member01_ncep_gefs_seamless": [27.5],
            "temperature_2m_max_ecmwf_ifs025_ensemble": [26.0],
            "temperature_2m_max_member01_ecmwf_ifs025_ensemble": [26.5],
            "temperature_2m_max_icon_seamless_eps": [28.0],
        },
    }
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.open_meteo_ensemble.build_httpx_client",
        lambda **_: client,
    )

    snapshot, raw = provider.fetch_forecast("m1", _rule())

    assert raw["model_count"] == 3
    assert raw["member_count"] == 5
    assert set(raw["model_members"]) == {
        "ncep_gefs_seamless",
        "ecmwf_ifs025_ensemble",
        "icon_seamless_eps",
    }
    assert snapshot.unit == "F"
    assert raw["timezone"] == "America/New_York"
    assert raw["coordinate_source"] == "awc_stationinfo"
    assert raw["forecast_station"] == "KNYC"
    params = client.get.call_args.kwargs["params"]
    assert params["daily"] == "temperature_2m_max"
    assert params["timezone"] == "America/New_York"
    assert params["forecast_days"] == 8
    assert params["past_days"] == 1


def test_multimodel_provider_shares_one_response_across_target_dates(monkeypatch):
    provider = OpenMeteoEnsembleProvider()
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.open_meteo_ensemble.fetch_awc_station_location",
        lambda station: (40.7, -74.0, {"icaoId": station}),
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "daily_units": {"temperature_2m_max": "°C"},
        "daily": {
            "time": ["2026-07-16", "2026-07-17"],
            "temperature_2m_max_ncep_gefs_seamless": [27.0, 30.0],
            "temperature_2m_max_ecmwf_ifs025_ensemble": [26.0, 29.0],
            "temperature_2m_max_icon_seamless_eps": [28.0, 31.0],
        },
    }
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.open_meteo_ensemble.build_httpx_client",
        lambda **_: client,
    )
    next_day_rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 86-87F on July 17, 2026?",
        "Settlement source: NOAA station KNYC.",
    )

    first, first_raw = provider.fetch_forecast("m1", _rule())
    second, second_raw = provider.fetch_forecast("m2", next_day_rule)

    assert client.get.call_count == 1
    assert first.value != second.value
    assert first_raw["target_date"] == "2026-07-16"
    assert second_raw["target_date"] == "2026-07-17"
    assert second_raw["provider_cache_status"] == "fresh_cache"


def test_open_meteo_daily_provider_shares_eight_day_response_across_dates(monkeypatch):
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.open_meteo.fetch_awc_station_location",
        lambda station: (40.7, -74.0, {"icaoId": station}),
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "daily": {
            "time": ["2026-07-16", "2026-07-17"],
            "temperature_2m_max": [80.0, 86.0],
        }
    }
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.open_meteo.build_httpx_client",
        lambda **_: client,
    )
    next_day_rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 86-87F on July 17, 2026?",
        "Settlement source: NOAA station KNYC.",
    )
    provider = OpenMeteoProvider()

    first, first_raw = provider.fetch_forecast("m1", _rule())
    second, second_raw = provider.fetch_forecast("m2", next_day_rule)

    assert client.get.call_count == 1
    assert first.value == Decimal("80.0")
    assert second.value == Decimal("86.0")
    assert first_raw["provider_cache_status"] == "network_fresh"
    assert second_raw["provider_cache_status"] == "fresh_cache"


def test_d0_hourly_context_reuses_response_and_recomputes_local_now(monkeypatch):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "hourly": {
            "time": ["2026-07-16T10:00", "2026-07-16T11:00"],
            "temperature_2m": [80.0, 81.0],
            "cloud_cover": [10, 20],
            "shortwave_radiation": [600, 700],
            "wind_speed_10m": [5, 6],
        }
    }
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.open_meteo.build_httpx_client",
        lambda **_: client,
    )
    provider = OpenMeteoProvider()

    first = provider.fetch_hourly_context(
        latitude=40.7,
        longitude=-74.0,
        timezone_name="America/New_York",
        target_date="2026-07-16",
        unit="F",
        now=datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc),
    )
    second = provider.fetch_hourly_context(
        latitude=40.7,
        longitude=-74.0,
        timezone_name="America/New_York",
        target_date="2026-07-16",
        unit="F",
        now=datetime(2026, 7, 16, 14, 15, tzinfo=timezone.utc),
    )

    assert client.get.call_count == 1
    assert first["provider_cache_status"] == "network_fresh"
    assert second["provider_cache_status"] == "fresh_cache"
    assert first["local_now"] != second["local_now"]


def test_google_weather_contributes_one_deterministic_pricing_vote(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "google-shadow.db",
        google_weather_api_key="secret-key",
    )
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()

    class PrimaryProvider:
        name = "open-meteo"

        def fetch_forecast(self, market_id, rule):
            return _forecast(), {"source_grade": "research_forecast"}

    class EnsembleProvider:
        def fetch_forecast(self, market_id, rule):
            now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
            snapshot = EnsembleForecastSnapshot.from_members(
                market_id=market_id,
                location="New York",
                variable="temperature_high",
                members=[Decimal("80.5"), Decimal("80.4")],
                fetched_at=now,
                raw_payload={},
                unit="F",
            )
            return snapshot, {
                "model_members": {"gfs": [80.5], "ecmwf": [80.4]},
                "model_count": 2,
                "latitude": 40.7,
                "longitude": -74.0,
            }

    seen = {}

    class GoogleProvider:
        def __init__(self, api_key):
            seen["api_key"] = api_key

        def fetch_forecast(self, market_id, rule, *, latitude, longitude):
            seen["coordinates"] = (latitude, longitude)
            forecast = _forecast()
            forecast = type(forecast)(
                **{**forecast.__dict__, "provider": "google-weather", "value": Decimal("81")}
            )
            return forecast, {
                "provider": "google-weather",
                "source_grade": "research_forecast",
                "decision_role": "pricing_reference",
            }

    class UnavailableTafProvider:
        def fetch_forecast(self, market_id, rule):
            raise ValueError("TAF has no TX group")

    monkeypatch.setattr(
        "polymarket_weather_arb.services.market_workflow_service.OpenMeteoEnsembleProvider",
        EnsembleProvider,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.market_workflow_service.GoogleWeatherProvider",
        GoogleProvider,
    )
    try:
        repository = Repository(connection)
        workflow = MarketWorkflowService(
            settings,
            repository,
            weather_provider_factory=PrimaryProvider,
            polymarket_client_factory=MagicMock,
            awc_forecast_provider_factory=UnavailableTafProvider,
        )

        _, raw = workflow._fetch_global_weather("m1", _rule())

        assert seen == {"api_key": "secret-key", "coordinates": (40.7, -74.0)}
        assert raw["pricing_references"]["google_weather"]["value"] == 81.0
        assert set(raw["model_members"]) == {
            "gfs",
            "ecmwf",
            "reference_open-meteo-ensemble",
            "reference_google-weather",
        }
        assert raw["model_members"]["reference_google-weather"] == [81.0]
        assert raw["model_count"] == 4
        assert raw["pricing_references"]["awc_taf"]["status"] == "unavailable"
    finally:
        connection.close()


def test_awc_taf_contributes_one_station_aligned_pricing_vote(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_path=tmp_path / "awc-taf-vote.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()

    class PrimaryProvider:
        name = "open-meteo"

        def fetch_forecast(self, market_id, rule):
            return _forecast(), {"source_grade": "research_forecast"}

    class EnsembleProvider:
        def fetch_forecast(self, market_id, rule):
            now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
            snapshot = EnsembleForecastSnapshot.from_members(
                market_id=market_id,
                location="New York",
                variable="temperature_high",
                members=[Decimal("80.5"), Decimal("80.4")],
                fetched_at=now,
                raw_payload={},
                unit="F",
            )
            return snapshot, {
                "model_members": {"gfs": [80.5], "ecmwf": [80.4]},
                "model_count": 2,
                "latitude": 40.7,
                "longitude": -74.0,
            }

    class TafProvider:
        def fetch_forecast(self, market_id, rule):
            now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
            return (
                ForecastSnapshot(
                    provider="awc-taf",
                    variable="temperature_high",
                    value=Decimal("81"),
                    unit="F",
                    issue_time=now,
                    valid_time=now,
                    market_id=market_id,
                    station="KNYC",
                ),
                {
                    "provider": "awc-taf",
                    "source_grade": "official_forecast",
                    "decision_role": "pricing_reference",
                    "station": "KNYC",
                    "issue_time": now.isoformat(),
                    "valid_time": now.isoformat(),
                },
            )

    monkeypatch.setattr(
        "polymarket_weather_arb.services.market_workflow_service.OpenMeteoEnsembleProvider",
        EnsembleProvider,
    )
    try:
        repository = Repository(connection)
        workflow = MarketWorkflowService(
            settings,
            repository,
            weather_provider_factory=PrimaryProvider,
            polymarket_client_factory=MagicMock,
            awc_forecast_provider_factory=TafProvider,
        )

        _, raw = workflow._fetch_global_weather("m1", _rule())

        assert raw["model_members"]["reference_awc-taf"] == [81.0]
        assert raw["model_count"] == 4
        assert raw["pricing_references"]["awc_taf"] == {
            "provider": "awc-taf",
            "source_grade": "official_forecast",
            "decision_role": "pricing_reference",
            "station": "KNYC",
            "issue_time": "2026-07-14T12:00:00+00:00",
            "valid_time": "2026-07-14T12:00:00+00:00",
            "status": "available",
            "value": 81.0,
            "unit": "F",
        }
    finally:
        connection.close()


def test_multimodel_probability_equal_weights_models_not_member_counts():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.01"),
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        model_members={
            "large_model": [Decimal("80.5")] * 30,
            "small_model_a": [Decimal("78")],
            "small_model_b": [Decimal("83")],
        },
    )

    assert analysis.model_version == "global-temp-bucket-multimodel-v8"
    assert "model_probability_mean=0.3333" in analysis.reasons
    assert any("families=3 members=32" in reason for reason in analysis.reasons)


def test_calibrated_weights_cannot_replace_two_thirds_model_quorum():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.05"),
        GlobalBucketPricingConfig(min_edge=Decimal("0.01"), slippage_buffer=Decimal("0")),
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        model_members={
            "good-a": [Decimal("80.5"), Decimal("80.5")],
            "good-b": [Decimal("80.5"), Decimal("80.5")],
            "bad-c": [Decimal("90"), Decimal("90")],
        },
        source_weights={
            "good-a": Decimal("0.5"),
            "good-b": Decimal("0.5"),
            "bad-c": Decimal("1.5"),
        },
    )

    assert analysis.decision == "watch"
    assert any("weighted_support_ratio=" in reason for reason in analysis.reasons)


def test_google_deterministic_reference_receives_one_model_level_vote():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.10"),
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        model_members={
            "gfs": [Decimal("78")] * 30,
            "reference_google-weather": [Decimal("80.5")],
        },
    )

    assert "model_probability_mean=0.2977" in analysis.reasons
    assert any("families=2 members=31" in reason for reason in analysis.reasons)
    assert any("reference_google-weather:0.5953" in reason for reason in analysis.reasons)


def test_correlated_sources_share_one_independent_family_vote():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.10"),
        model_members={
            "gfs_seamless": [Decimal("80.5")],
            "gefs": [Decimal("80.5")],
            "reference_noaa-nws": [Decimal("80.5")],
            "ecmwf_ifs025": [Decimal("78")],
            "icon_seamless_eps": [Decimal("78")],
        },
    )

    assert analysis.decision == "watch"
    assert "supporting_models=3/5 required=4" in analysis.reasons
    assert "supporting_families=1/3 required=2" in analysis.reasons
    assert any(
        "ncep:[gefs|gfs_seamless|reference_noaa-nws]" in reason for reason in analysis.reasons
    )


def test_awc_taf_is_advisory_until_same_phase_history_promotes_it():
    members = {
        "ecmwf_ifs025": [Decimal("80.5")],
        "icon_seamless_eps": [Decimal("80.5")],
        "reference_awc-taf": [Decimal("80.5")],
    }
    advisory = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.10"),
        model_members=members,
    )
    promoted = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.10"),
        model_members=members,
        source_weights={"reference_awc-taf": Decimal("1")},
    )

    assert advisory.decision == "watch"
    assert any(
        "advisory_families_excluded_from_pricing_quorum=aviation-taf" in reason
        for reason in advisory.reasons
    )
    assert promoted.decision == "trade", promoted.reasons
    assert "supporting_families=3/3 required=2" in promoted.reasons


def test_advisory_awc_taf_only_vetoes_strong_probability_contradiction():
    supporting_members = {
        "ecmwf_ifs025": [Decimal("80.5")],
        "icon_seamless_eps": [Decimal("80.5")],
        "gefs": [Decimal("80.5")],
    }
    moderate_disagreement = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.10"),
        model_members={
            **supporting_members,
            "reference_awc-taf": [Decimal("82")],
        },
    )
    strong_contradiction = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.10"),
        model_members={
            **supporting_members,
            "reference_awc-taf": [Decimal("90")],
        },
    )

    assert moderate_disagreement.decision == "trade"
    assert "awc_taf_entry_role=advisory_veto_only weight=0.35" in moderate_disagreement.reasons
    assert any(
        reason.startswith("awc_taf_strong_contradiction_check=True;")
        for reason in moderate_disagreement.reasons
    )
    assert strong_contradiction.decision == "watch"
    assert any(
        reason.startswith("awc_taf_strong_contradiction_check=False;")
        for reason in strong_contradiction.reasons
    )
    assert any(
        "station-aligned TAF bucket probability is below 0.05" in reason
        for reason in strong_contradiction.reasons
    )


def test_soft_reference_and_robust_dispersion_allow_consensus_entry():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.10"),
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        model_members={
            "gfs": [Decimal("80.5")] * 4 + [Decimal("78")] * 6,
            "ecmwf": [Decimal("80.5")] * 4 + [Decimal("83")] * 6,
            "icon": [Decimal("80.5")] * 3 + [Decimal("78")] * 7,
            "gem": [Decimal("80.5")] * 4 + [Decimal("83")] * 6,
            "reference_google-weather": [Decimal("80.5")],
        },
    )

    assert analysis.model_version == "global-temp-bucket-multimodel-v8"
    assert analysis.decision == "trade"
    assert analysis.fair_lower > Decimal("0.16")
    assert any("model_robust_dispersion=" in reason for reason in analysis.reasons)
    assert any("deterministic_reference_sigma=1.20F" in reason for reason in analysis.reasons)


def test_broad_ensemble_split_remains_blocked():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.01"),
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        model_members={
            "gfs": [Decimal("80.5")] * 10,
            "ecmwf": [Decimal("80.5")] * 10,
            "icon": [Decimal("78")] * 10,
            "gem": [Decimal("83")] * 10,
        },
    )

    assert analysis.decision == "watch"
    assert analysis.fair_lower == Decimal("0")
    assert "model_robust_dispersion=0.7413" in analysis.reasons


def test_complete_integer_settlement_bucket_groups_conserve_probability():
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)

    def group_probability_sum(
        titles: list[str],
        unit: str,
        model_name: str,
        members: list[Decimal],
        observed_max: Decimal | None = None,
    ) -> Decimal:
        forecast = ForecastSnapshot(
            provider="open-meteo-ensemble",
            variable="temperature_high",
            value=members[len(members) // 2],
            unit=unit,
            issue_time=now,
            valid_time=now,
            fetched_at=now,
            market_id="group",
            location="Test City",
        )
        probabilities: list[Decimal] = []
        for index, title in enumerate(titles):
            rule = parse_global_temperature_bucket_rule(
                title,
                "Settlement source: Wunderground station KNYC.",
            )
            analysis = analyze_global_bucket_price(
                f"m{index}",
                rule,
                forecast,
                Decimal("0.01"),
                now=now,
                model_members={model_name: members},
                observed_max=observed_max,
                observed_max_unit=unit if observed_max is not None else None,
            )
            assert analysis.fair_probability is not None
            probabilities.append(analysis.fair_probability)
        return sum(probabilities)

    celsius_titles = [
        "Will the highest temperature in Test City be 24°C or below on July 16, 2026?",
        *[
            f"Will the highest temperature in Test City be {value}°C on July 16, 2026?"
            for value in range(25, 34)
        ],
        "Will the highest temperature in Test City be 34°C or higher on July 16, 2026?",
    ]
    fahrenheit_titles = [
        "Will the highest temperature in Test City be 89°F or below on July 16, 2026?",
        *[
            f"Will the highest temperature in Test City be between {value}-{value + 1}°F on July 16, 2026?"
            for value in range(90, 108, 2)
        ],
        "Will the highest temperature in Test City be 108°F or higher on July 16, 2026?",
    ]

    celsius_sum = group_probability_sum(
        celsius_titles,
        "C",
        "ensemble",
        [Decimal("20"), *[Decimal(f"{value}.5") for value in range(23, 34)], Decimal("40")],
    )
    fahrenheit_sum = group_probability_sum(
        fahrenheit_titles,
        "F",
        "ensemble",
        [Decimal("80"), *[Decimal(f"{value}.5") for value in range(89, 108, 2)], Decimal("120")],
    )
    celsius_reference_sum = group_probability_sum(
        celsius_titles, "C", "reference_open-meteo", [Decimal("28")]
    )
    fahrenheit_reference_sum = group_probability_sum(
        fahrenheit_titles, "F", "reference_google-weather", [Decimal("96")]
    )
    conditioned_celsius_sum = group_probability_sum(
        celsius_titles,
        "C",
        "ensemble",
        [Decimal("24"), Decimal("28"), Decimal("31"), Decimal("35")],
        observed_max=Decimal("30.2"),
    )

    assert abs(celsius_sum - Decimal("1")) <= Decimal("1e-12")
    assert abs(fahrenheit_sum - Decimal("1")) <= Decimal("1e-12")
    assert abs(celsius_reference_sum - Decimal("1")) <= Decimal("1e-12")
    assert abs(fahrenheit_reference_sum - Decimal("1")) <= Decimal("1e-12")
    assert abs(conditioned_celsius_sum - Decimal("1")) <= Decimal("1e-12")


def test_llm_fractional_vote_cannot_replace_minimum_weather_model_count():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.20"),
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        model_members={
            "gfs": [Decimal("78")],
            "ecmwf": [Decimal("83")],
        },
        external_probability=Decimal("1"),
        external_weight=Decimal("0.5"),
    )

    assert any("blend_ratio=0.2000 against 2 base models" in reason for reason in analysis.reasons)
    assert any(
        "requires at least 3 independent source families" in reason for reason in analysis.reasons
    )
    assert analysis.decision == "watch"


def test_consensus_can_buy_probable_bucket_without_legacy_price_cap():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.50"),
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        model_members={
            "gfs": [Decimal("80.5")] * 10,
            "ecmwf": [Decimal("80.4")] * 10,
            "icon": [Decimal("80.6")] * 10,
        },
    )

    assert analysis.decision == "trade"
    assert "supporting_models=3/3 required=2" in analysis.reasons
    assert analysis.reference_price == Decimal("0.50")


def test_four_of_five_models_can_trade_despite_one_zero_probability_outlier():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.27"),
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        model_members={
            "gefs": [Decimal("80.5")] * 5 + [Decimal("78")] * 5,
            "ecmwf": [Decimal("80.5")] * 5 + [Decimal("83")] * 5,
            "icon": [Decimal("80.5")] * 4 + [Decimal("78")] * 6,
            "gem": [Decimal("78")] * 10,
            "reference_open-meteo": [Decimal("80.5")],
        },
    )

    assert analysis.decision == "trade"
    assert "supporting_models=4/5 required=4" in analysis.reasons
    assert "consensus_probability_median=0.5000" in analysis.reasons
    assert analysis.edge == Decimal("0.120")
    assert "decision_probability_conservative=0.4000" in analysis.reasons


def test_exact_four_of_six_models_meets_two_thirds_quorum():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.30"),
        best_bid=Decimal("0.29"),
        model_members={
            "gefs": [Decimal("80.5")] * 5 + [Decimal("78")] * 5,
            "ecmwf": [Decimal("80.5")] * 5 + [Decimal("78")] * 5,
            "icon": [Decimal("80.5")] * 5 + [Decimal("78")] * 5,
            "gem": [Decimal("80.5")] * 5 + [Decimal("78")] * 5,
            "aifs": [Decimal("80.5")] * 3 + [Decimal("78")] * 7,
            "jma": [Decimal("80.5")] * 3 + [Decimal("78")] * 7,
        },
    )

    assert analysis.decision == "trade"
    assert "supporting_models=4/6 required=4" in analysis.reasons
    assert "supporting_families=4/5 required=4" in analysis.reasons
    assert "support_ratio=0.8000" in analysis.reasons
    assert not any("fewer than two-thirds" in reason for reason in analysis.reasons)


def test_global_bucket_fee_aware_edge_deducts_entry_and_exit_taker_fees():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.27"),
        best_bid=Decimal("0.25"),
        fees_enabled=True,
        fee_rate=Decimal("0.05"),
        model_members={
            "gefs": [Decimal("80.5")] * 5 + [Decimal("78")] * 5,
            "ecmwf": [Decimal("80.5")] * 5 + [Decimal("83")] * 5,
            "icon": [Decimal("80.5")] * 4 + [Decimal("78")] * 6,
            "gem": [Decimal("78")] * 10,
            "reference_open-meteo": [Decimal("80.5")],
        },
    )

    assert analysis.gross_edge == Decimal("0.120")
    assert analysis.entry_fee_per_share == Decimal("0.0098550")
    assert analysis.exit_fee_per_share == Decimal("0.0093750")
    assert analysis.edge == Decimal("0.1007700")
    assert "consensus_gross_edge=0.1200" in analysis.reasons
    assert "consensus_net_edge=0.1008" in analysis.reasons
    assert any("net edge after taker fees rate=0.05" in reason for reason in analysis.reasons)


def test_global_bucket_fees_can_move_gross_trade_below_minimum_net_edge():
    members = {
        "gfs": [Decimal("80.5")] * 5 + [Decimal("78")] * 5,
        "ecmwf": [Decimal("80.5")] * 5 + [Decimal("83")] * 5,
        "icon": [Decimal("80.5")] * 5 + [Decimal("78")] * 5,
    }
    config = GlobalBucketPricingConfig(
        min_edge=Decimal("0.05"),
        slippage_buffer=Decimal("0.01"),
    )
    gross = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.44"),
        config,
        model_members=members,
    )
    net = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.44"),
        config,
        model_members=members,
        best_bid=Decimal("0.42"),
        fees_enabled=True,
        fee_rate=Decimal("0.05"),
    )

    assert gross.gross_edge == gross.edge == Decimal("0.05")
    assert gross.decision == "trade"
    assert net.gross_edge == Decimal("0.05")
    assert net.edge < Decimal("0.05")
    assert net.decision == "watch"
    assert "conservative fee-aware net edge does not clear minimum edge" in net.reasons


def test_global_bucket_workflow_reads_market_fee_schedule_and_persists_net_edge(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "fee-aware-bucket.db",
        min_edge=Decimal("0.05"),
        slippage_buffer=Decimal("0.02"),
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repository = Repository(connection)
        repository.upsert_market(
            Market(id="m1", title="fee-aware bucket", yes_token_id="yes", no_token_id="no"),
            {"id": "m1", "feesEnabled": True, "feeType": "weather_fees"},
        )
        repository.save_market_snapshot(
            MarketSnapshot(
                market_id="m1",
                best_bid=Decimal("0.25"),
                best_ask=Decimal("0.27"),
                midpoint=Decimal("0.26"),
                spread=Decimal("0.02"),
                liquidity=Decimal("100"),
                fetched_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
            ),
            {},
        )
        workflow = MarketWorkflowService(
            settings,
            repository,
            weather_provider_factory=MagicMock,
            polymarket_client_factory=MagicMock,
        )

        analysis = workflow._price_global_bucket_market(
            "m1",
            _rule(),
            _forecast(),
            {
                "model_members": {
                    "gfs": [80.5] * 10,
                    "ecmwf": [80.5] * 10,
                    "icon": [80.5] * 10,
                }
            },
        )

        stored = repository.latest_analysis("m1")
        stored_reasons = json.loads(stored["reasons"])
        assert analysis.gross_edge is not None and analysis.edge < analysis.gross_edge
        assert any("net edge after taker fees rate=0.05" in reason for reason in stored_reasons)
        assert any("consensus_gross_edge=" in reason for reason in stored_reasons)
        assert any("consensus_net_edge=" in reason for reason in stored_reasons)
    finally:
        connection.close()


def test_three_of_five_models_do_not_meet_two_thirds_quorum():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.10"),
        model_members={
            "gefs": [Decimal("80.5")],
            "ecmwf": [Decimal("80.5")],
            "icon": [Decimal("80.5")],
            "gem": [Decimal("78")],
            "reference_open-meteo": [Decimal("78")],
        },
    )

    assert analysis.decision == "watch"
    assert "supporting_models=3/5 required=4" in analysis.reasons
    assert (
        "fewer than two-thirds of independent source families support the entry price"
        in analysis.reasons
    )


def test_probability_below_twenty_five_percent_can_trade_on_conservative_net_edge():
    members = [Decimal("80.5")] * 2 + [Decimal("78")] * 8
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.01"),
        model_members={"gefs": members, "ecmwf": members, "icon": members},
        top_candidate_supporters=3,
        top_candidate_model_count=3,
    )

    assert analysis.decision == "trade"
    assert "consensus_probability_median=0.2000" in analysis.reasons
    assert "supporting_models=3/3 required=2" in analysis.reasons
    assert analysis.edge == Decimal("0.18")
    assert (
        "entry_absolute_probability_floor=disabled; conservative fee-aware edge is decisive"
        in analysis.reasons
    )


def test_exact_twenty_five_percent_probability_clears_probability_floor():
    members = [Decimal("80.5")] + [Decimal("78")] * 3
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.01"),
        model_members={"gefs": members, "ecmwf": members, "icon": members},
        top_candidate_supporters=3,
        top_candidate_model_count=3,
    )

    assert analysis.decision == "trade"
    assert "consensus_probability_median=0.2500" in analysis.reasons
    assert "supporting_models=3/3 required=2" in analysis.reasons
    assert (
        "entry_absolute_probability_floor=disabled; conservative fee-aware edge is decisive"
        in analysis.reasons
    )


def test_low_price_bucket_requires_majority_first_choice_evidence():
    members = [Decimal("80.5")] + [Decimal("78")] * 3
    common = {
        "model_members": {"gefs": members, "ecmwf": members, "icon": members},
        "top_candidate_model_count": 3,
    }

    minority = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.08"),
        top_candidate_supporters=1,
        **common,
    )
    majority = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.08"),
        top_candidate_supporters=2,
        **common,
    )

    assert minority.decision == "watch"
    assert any("strict majority" in reason for reason in minority.reasons)
    assert majority.decision == "trade"


def test_temperature_biases_shift_each_source_without_mutating_raw_members():
    raw = {"ecmwf": [Decimal("29"), Decimal("30")], "icon": [Decimal("31")]}

    corrected = apply_temperature_biases(
        raw,
        {"ecmwf": Decimal("0.6"), "icon": Decimal("-0.2")},
    )

    assert corrected == {
        "ecmwf": [Decimal("29.6"), Decimal("30.6")],
        "icon": [Decimal("30.8")],
    }
    assert raw["ecmwf"] == [Decimal("29"), Decimal("30")]


def test_top_candidate_votes_are_computed_across_sibling_buckets():
    rules = {
        "m80": _rule("80-81F"),
        "m82": _rule("82-83F"),
    }
    votes = global_bucket_top_candidate_votes(
        rules,
        _forecast(),
        {
            "ecmwf": [Decimal("80.5")],
            "icon": [Decimal("80.5")],
            "gefs": [Decimal("82.5")],
        },
        now=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
    )

    assert votes == {"m80": (2, 3), "m82": (1, 3)}


def test_top_candidate_votes_do_not_count_tied_buckets_as_first_choice():
    rules = {
        "m80": _rule("80-81F"),
        "m82": _rule("82-83F"),
    }
    votes = global_bucket_top_candidate_votes(
        rules,
        _forecast(),
        {"flat-model": [Decimal("79"), Decimal("80.5"), Decimal("82.5"), Decimal("84")]},
        now=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
    )

    assert votes == {"m80": (0, 1), "m82": (0, 1)}


def test_multimodel_d0_observation_floor_eliminates_passed_bucket():
    analysis = analyze_global_bucket_price(
        "m1",
        _rule(),
        _forecast(),
        Decimal("0.01"),
        model_members={"gfs": [Decimal("80.5")], "ecmwf": [Decimal("80.4")]},
        observed_max=Decimal("83"),
        observed_max_unit="F",
    )

    assert analysis.fair_lower == Decimal("0")
    assert analysis.fair_upper == Decimal("0")
    assert analysis.fair_probability == Decimal("0")
    assert analysis.decision == "reject"
    assert any("cannot reopen an impossible bucket" in reason for reason in analysis.reasons)


def test_d0_atlanta_observation_above_range_upper_is_hard_zero_even_with_overlays():
    analysis = analyze_global_bucket_price(
        "atlanta-90-91",
        _rule("90-91F"),
        _forecast(),
        Decimal("0.001"),
        model_members={
            "gefs": [Decimal("91.0")] * 10,
            "ecmwf": [Decimal("91.0")] * 10,
            "icon": [Decimal("91.0")] * 10,
        },
        observed_max=Decimal("91.94"),
        observed_max_unit="F",
        conditioning_probability=Decimal("0.90"),
        conditioning_weight=Decimal("0.75"),
        external_probability=Decimal("0.95"),
        external_weight=Decimal("0.5"),
    )

    assert analysis.decision == "reject"
    assert analysis.fair_lower == analysis.fair_upper == Decimal("0")
    assert analysis.fair_probability == Decimal("0")
    assert any("upper bound 91.5F" in reason for reason in analysis.reasons)


def test_d0_qingdao_observation_above_exact_bucket_rounding_bound_is_hard_zero():
    analysis = analyze_global_bucket_price(
        "qingdao-25",
        _rule("25C"),
        _forecast(),
        Decimal("0.001"),
        model_members={
            "gefs": [Decimal("25.0")],
            "ecmwf": [Decimal("25.0")],
            "icon": [Decimal("25.0")],
        },
        observed_max=Decimal("26"),
        observed_max_unit="C",
    )

    assert analysis.decision == "reject"
    assert analysis.fair_probability == Decimal("0")
    assert any("upper bound 25.5C" in reason for reason in analysis.reasons)


def test_d0_observation_below_exclusive_rounding_bound_is_not_hard_excluded():
    analysis = analyze_global_bucket_price(
        "dallas-90-91",
        _rule("90-91F"),
        _forecast(),
        Decimal("0.01"),
        model_members={
            "gefs": [Decimal("91.0")],
            "ecmwf": [Decimal("91.0")],
            "icon": [Decimal("91.0")],
        },
        observed_max=Decimal("91.4"),
        observed_max_unit="F",
    )

    assert analysis.fair_probability > 0
    assert not any("cannot reopen an impossible bucket" in reason for reason in analysis.reasons)


def test_d0_observation_does_not_turn_lower_forecasts_into_false_bucket_consensus():
    analysis = analyze_global_bucket_price(
        "atlanta-90-91",
        _rule("90-91F"),
        _forecast(),
        Decimal("0.01"),
        model_members={
            "gefs": [Decimal("90.2"), Decimal("90.8"), Decimal("87.0")],
            "ecmwf": [Decimal("84.0"), Decimal("86.0"), Decimal("87.0")],
            "icon": [Decimal("82.0"), Decimal("85.0"), Decimal("86.0")],
            "gem": [Decimal("83.0"), Decimal("86.0"), Decimal("88.0")],
            "reference_open-meteo": [Decimal("85.0")],
            "reference_google-weather": [Decimal("86.0")],
        },
        observed_max=Decimal("89.6"),
        observed_max_unit="F",
    )

    assert analysis.decision == "watch"
    assert analysis.side is None
    assert "supporting_models=3/6 required=4" in analysis.reasons
    assert any("source-tolerant" in reason for reason in analysis.reasons)
    assert (
        "fewer than two-thirds of independent source families support the entry price"
        in analysis.reasons
    )


def test_d0_trajectory_ceiling_blocks_consensus_for_unreachable_upper_bucket():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Wuhan be 32C on July 18, 2026?",
        "Settlement source: Wunderground station ZHHH.",
    )
    now = datetime(2026, 7, 18, 5, 22, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal("32"),
        unit="C",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="wuhan-32",
        location="Wuhan",
    )
    kwargs = {
        "now": now,
        "observed_max": Decimal("28"),
        "observed_max_unit": "C",
        "model_members": {
            "ecmwf": [Decimal("32")],
            "gfs": [Decimal("32")],
            "icon": [Decimal("32")],
            "gem": [Decimal("32")],
            "reference": [Decimal("32")],
        },
        "conditioning_probability": Decimal("0.05"),
        "conditioning_weight": Decimal("0.5"),
        "top_candidate_supporters": 5,
        "top_candidate_model_count": 5,
    }

    unguarded = analyze_global_bucket_price(
        "wuhan-32",
        rule,
        forecast,
        Decimal("0.049"),
        **kwargs,
    )
    guarded = analyze_global_bucket_price(
        "wuhan-32",
        rule,
        forecast,
        Decimal("0.049"),
        d0_trajectory_upper_bound=Decimal("31.05"),
        **kwargs,
    )

    assert unguarded.decision == "trade"
    assert guarded.decision == "watch"
    assert guarded.side is None
    assert any(
        "D0 trajectory upper bound 31.05C is below bucket lower bound 31.5C" in reason
        for reason in guarded.reasons
    )


def test_d0_hourly_strong_contradiction_blocks_chicago_style_entry():
    rule = _rule("86-87F")
    members = {
        "gefs": [Decimal("86.5")],
        "ecmwf": [Decimal("86.5")],
        "icon": [Decimal("86.5")],
        "gem": [Decimal("86.5")],
        "google": [Decimal("86.5")],
        "noaa": [Decimal("86.5")],
    }
    common = {
        "model_members": members,
        "top_candidate_supporters": 4,
        "top_candidate_model_count": 5,
        "observed_max": Decimal("80.96"),
        "observed_max_unit": "F",
        "best_bid": Decimal("0.20"),
        "conditioning_weight": Decimal("0.75"),
        "d0_trajectory_upper_bound": Decimal("95.775"),
    }

    contradictory = analyze_global_bucket_price(
        "chicago-86-87",
        rule,
        _forecast(),
        Decimal("0.21"),
        conditioning_probability=Decimal("0.0002"),
        **common,
    )
    boundary = analyze_global_bucket_price(
        "chicago-86-87",
        rule,
        _forecast(),
        Decimal("0.21"),
        conditioning_probability=Decimal("0.05"),
        **common,
    )

    assert contradictory.decision == "watch"
    assert contradictory.side is None
    assert any(
        "D0 hourly bucket probability is below 0.05" in reason for reason in contradictory.reasons
    )
    assert boundary.decision == "trade"


def test_d0_hourly_conditioning_is_authoritative_for_taipei_style_entry():
    now = datetime(2026, 7, 24, 0, 23, tzinfo=timezone.utc)
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Taipei be 34°C on July 24, 2026?",
        "Settlement source: Wunderground station RCSS.",
    )
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal("34"),
        unit="C",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="taipei-34",
        location="Taipei",
    )
    # Each independent family gives the bucket about 60%, while the D0
    # observed trajectory gives it only 13.59%. The latter must carry the
    # configured 75% probability weight instead of being diluted by five.
    family_members = [Decimal("34")] * 6 + [Decimal("31")] * 4

    analysis = analyze_global_bucket_price(
        "taipei-34",
        rule,
        forecast,
        Decimal("0.18"),
        best_bid=Decimal("0.17"),
        now=now,
        observed_max=Decimal("33"),
        observed_max_unit="C",
        model_members={
            family: list(family_members)
            for family in ("gefs", "ecmwf", "icon", "gem", "reference_open-meteo")
        },
        conditioning_probability=Decimal("0.1359"),
        conditioning_weight=Decimal("0.75"),
        top_candidate_supporters=5,
        top_candidate_model_count=5,
        fees_enabled=True,
        fee_rate=Decimal("0.05"),
    )

    assert analysis.decision == "watch"
    assert analysis.edge < Decimal("0.05")
    assert any("blend_ratio=0.7500" in reason for reason in analysis.reasons)
    assert "conservative fee-aware net edge does not clear minimum edge" in analysis.reasons


def test_d0_station_bias_prevents_hourly_vote_from_lifting_weak_consensus():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Wuhan be 32C on July 18, 2026?",
        "Settlement source: Wunderground station ZHHH.",
    )
    now = datetime(2026, 7, 18, 5, 22, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal("30.8"),
        unit="C",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="wuhan-32",
        location="Wuhan",
    )
    members = {
        "ecmwf": [Decimal("32"), Decimal("30"), Decimal("30"), Decimal("30")],
        "gem": [Decimal("32")] + [Decimal("30")] * 9,
        "icon": [Decimal("30")] * 10,
        "ncep": [Decimal("32"), Decimal("33"), Decimal("33"), Decimal("33")],
        "reference_open-meteo": [Decimal("32.1")],
    }
    common = {
        "now": now,
        "observed_max": Decimal("28"),
        "observed_max_unit": "C",
        "model_members": members,
        "top_candidate_supporters": 4,
        "top_candidate_model_count": 5,
    }

    overconfident = analyze_global_bucket_price(
        "wuhan-32",
        rule,
        forecast,
        Decimal("0.049"),
        conditioning_probability=Decimal("0.7218"),
        conditioning_weight=Decimal("0.5"),
        **common,
    )
    anchored = analyze_global_bucket_price(
        "wuhan-32",
        rule,
        forecast,
        Decimal("0.049"),
        conditioning_probability=Decimal("0.08"),
        conditioning_weight=Decimal("0.75"),
        **common,
    )

    assert overconfident.decision == "trade"
    assert anchored.decision == "watch"
    assert anchored.side is None
    assert "conservative fee-aware net edge does not clear minimum edge" in anchored.reasons


def test_qingdao_wide_entry_spread_blocks_crossing_illiquid_ask():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Qingdao be 29C on July 18, 2026?",
        "Settlement source: Wunderground station ZSQD.",
    )
    now = datetime(2026, 7, 18, 5, 7, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal("29.1"),
        unit="C",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="qingdao-29",
        location="Qingdao",
        station="ZSQD",
    )
    members = {
        "gefs": [Decimal("29.1")],
        "ecmwf": [Decimal("29.1")],
        "icon": [Decimal("29.1")],
        "gem": [Decimal("29.1")],
        "reference": [Decimal("29.1")],
    }

    analysis = analyze_global_bucket_price(
        "qingdao-29",
        rule,
        forecast,
        Decimal("0.19"),
        GlobalBucketPricingConfig(min_edge=Decimal("0.05"), slippage_buffer=Decimal("0.01")),
        now=now,
        observed_max=Decimal("29"),
        observed_max_unit="C",
        model_members=members,
        best_bid=Decimal("0.06"),
    )

    assert analysis.decision == "watch"
    assert analysis.side is None
    assert any("entry spread 0.13 exceeds allowed 0.076" in reason for reason in analysis.reasons)


def test_loss_replay_dallas_post_peak_cannot_buy_neighboring_90_91_bucket():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Dallas be between 90-91F on July 16, 2026?",
        "Settlement source: Wunderground station KDAL.",
    )
    now = datetime(2026, 7, 17, 4, 5, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal("91"),
        unit="F",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="2929453",
        location="Dallas",
        station="KDAL",
    )
    members = {
        "gefs": [Decimal("91")],
        "ecmwf": [Decimal("91")],
        "icon": [Decimal("91")],
        "gem": [Decimal("91")],
        "reference_open-meteo": [Decimal("91")],
    }

    before_peak_guard = analyze_global_bucket_price(
        "2929453",
        rule,
        forecast,
        Decimal("0.02"),
        now=now,
        observed_max=Decimal("89.06"),
        observed_max_unit="F",
        model_members=members,
        top_candidate_supporters=5,
        top_candidate_model_count=5,
    )
    guarded = analyze_global_bucket_price(
        "2929453",
        rule,
        forecast,
        Decimal("0.02"),
        now=now,
        observed_max=Decimal("89.06"),
        observed_max_unit="F",
        model_members=members,
        d0_post_peak=True,
        top_candidate_supporters=5,
        top_candidate_model_count=5,
    )

    assert before_peak_guard.decision == "trade"
    assert guarded.decision == "watch"
    assert guarded.side is None
    assert any("observed maximum is outside this bucket" in reason for reason in guarded.reasons)


def test_loss_replay_singapore_post_peak_requires_confirmed_peak_lock():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Singapore be 31C on July 20, 2026?",
        "Settlement source: Wunderground station WSSS.",
    )
    now = datetime(2026, 7, 20, 4, 25, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal("31"),
        unit="C",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="singapore-31",
        location="Singapore",
        station="WSSS",
    )
    members = {
        source: [Decimal("31")]
        for source in ("gefs", "ecmwf", "icon", "gem", "reference", "google")
    }

    unlocked = analyze_global_bucket_price(
        "singapore-31",
        rule,
        forecast,
        Decimal("0.20"),
        now=now,
        observed_max=Decimal("31"),
        observed_max_unit="C",
        model_members=members,
        d0_post_peak=True,
        d0_peak_lock_confirmed=False,
    )
    locked = analyze_global_bucket_price(
        "singapore-31",
        rule,
        forecast,
        Decimal("0.20"),
        now=now,
        observed_max=Decimal("31"),
        observed_max_unit="C",
        model_members=members,
        d0_post_peak=True,
        d0_peak_lock_confirmed=True,
    )

    assert unlocked.decision == "watch"
    assert unlocked.side is None
    assert any("peak lock=False" in reason for reason in unlocked.reasons)
    assert locked.decision == "trade"


def test_loss_replay_extreme_price_requires_all_models_to_converge():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Dallas be between 90-91F on July 16, 2026?",
        "Settlement source: Wunderground station KDAL.",
    )
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal("91"),
        unit="F",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="2929453",
        location="Dallas",
        station="KDAL",
    )
    members = {
        "gefs": [Decimal("91")],
        "ecmwf": [Decimal("91")],
        "icon": [Decimal("91")],
        "gem": [Decimal("91")],
        "reference_open-meteo": [Decimal("86")],
    }

    normal_price = analyze_global_bucket_price(
        "2929453",
        rule,
        forecast,
        Decimal("0.02"),
        now=now,
        model_members=members,
        top_candidate_supporters=4,
        top_candidate_model_count=5,
    )
    lottery_price = analyze_global_bucket_price(
        "2929453",
        rule,
        forecast,
        Decimal("0.001"),
        now=now,
        model_members=members,
        top_candidate_supporters=4,
        top_candidate_model_count=5,
    )
    unanimous_lottery = analyze_global_bucket_price(
        "2929453",
        rule,
        forecast,
        Decimal("0.001"),
        now=now,
        model_members={model: [Decimal("91")] for model in members},
        top_candidate_supporters=5,
        top_candidate_model_count=5,
    )

    assert normal_price.decision == "trade"
    assert lottery_price.decision == "watch"
    assert lottery_price.side is None
    assert any("extreme-price entry" in reason for reason in lottery_price.reasons)
    assert unanimous_lottery.decision == "trade"


@pytest.mark.parametrize(
    ("market_id", "title", "description", "observed_max", "unit"),
    [
        (
            "2929463",
            "Will the highest temperature in Atlanta be between 90-91F on July 16, 2026?",
            "Settlement source: Wunderground station KATL.",
            "91.94",
            "F",
        ),
        (
            "2930884",
            "Will the highest temperature in Qingdao be 25C on July 17, 2026?",
            "Settlement source: Wunderground station ZSQD.",
            "26",
            "C",
        ),
    ],
)
def test_loss_replay_observed_max_irreversibly_rejects_passed_bucket(
    market_id, title, description, observed_max, unit
):
    rule = parse_global_temperature_bucket_rule(title, description)
    now = datetime(2026, 7, 17, 6, 12, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal(observed_max),
        unit=unit,
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id=market_id,
    )
    analysis = analyze_global_bucket_price(
        market_id,
        rule,
        forecast,
        Decimal("0.001"),
        now=now,
        observed_max=Decimal(observed_max),
        observed_max_unit=unit,
        model_members={
            "gefs": [forecast.value],
            "ecmwf": [forecast.value],
            "icon": [forecast.value],
            "gem": [forecast.value],
        },
    )

    assert analysis.decision == "reject"
    assert analysis.side is None
    assert any("already exceeds bucket upper bound" in reason for reason in analysis.reasons)


def test_staged_entry_caps_are_d2_scout_then_d1_build():
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    assert _staged_entry_cap(
        "Will the highest temperature in New York be 80-81F on July 16, 2026?", now=now
    ) == Decimal("4")
    assert _staged_entry_cap(
        "Will the highest temperature in New York be 80-81F on July 15, 2026?", now=now
    ) == Decimal("10")
    assert _staged_entry_cap(
        "Will the highest temperature in New York be 80-81F on July 20, 2026?", now=now
    ) == Decimal("0")
    assert _staged_entry_cap(
        "Will the highest temperature in New York be 80-81F on July 14, 2026?",
        now=datetime(2026, 7, 14, 17, tzinfo=timezone.utc),
    ) == Decimal("10.00")


def test_staged_entry_cap_accepts_persisted_dynamic_city_timezone():
    now = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    title = "Will the highest temperature in Cape Town be 21C on July 21, 2026?"

    assert _staged_entry_cap(
        title,
        now=now,
        timezone_name="Africa/Johannesburg",
    ) == Decimal("10.00")


def test_trading_service_honors_staged_notional_override(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "stage.db", max_order_usdc=2)
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repository = Repository(connection)
        repository.upsert_market(
            Market(id="m1", title="staged entry", yes_token_id="yes", no_token_id="no"),
            {"id": "m1"},
        )
        analysis = Analysis(
            market_id="m1",
            model_version="v2",
            fair_lower=Decimal("0.7"),
            fair_upper=Decimal("0.8"),
            reference_price=Decimal("0.20"),
            edge=Decimal("0.4"),
            side="buy_yes",
            decision="trade",
            reasons=["stage test"],
        )
        intent_id, reasons = TradingService(settings, MagicMock(), repository).trade(
            analysis=analysis,
            yes_token_id="yes",
            no_token_id="no",
            context=RiskContext(
                daily_live_notional=Decimal("0"),
                market_live_exposure=Decimal("0"),
                order_book_age_seconds=0,
                forecast_age_seconds=0,
                rule_tradable=True,
            ),
            dry_run=True,
            max_notional_override=Decimal("1"),
        )
        intent = repository.get_order_intent(intent_id)
        assert reasons == ["dry-run order recorded"]
        assert Decimal(str(intent["notional"])) <= Decimal("1")
    finally:
        connection.close()


def test_trading_service_reports_exchange_minimum_for_dust_override(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "dust.db", max_order_usdc=2)
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repository = Repository(connection)
        repository.upsert_market(
            Market(id="m1", title="dust entry", yes_token_id="yes", no_token_id="no"),
            {"id": "m1", "orderMinSize": "5"},
        )
        analysis = Analysis(
            market_id="m1",
            model_version="v2",
            fair_lower=Decimal("0.7"),
            fair_upper=Decimal("0.8"),
            reference_price=Decimal("0.02"),
            edge=Decimal("0.4"),
            side="buy_yes",
            decision="trade",
            reasons=["dust test"],
        )

        intent_id, reasons = TradingService(settings, MagicMock(), repository).trade(
            analysis=analysis,
            yes_token_id="yes",
            no_token_id="no",
            context=RiskContext(
                daily_live_notional=Decimal("0"),
                market_live_exposure=Decimal("0"),
                order_book_age_seconds=0,
                forecast_age_seconds=0,
                rule_tradable=True,
            ),
            dry_run=True,
            max_notional_override=Decimal("0.0000005"),
            market_payload={"orderMinSize": "5"},
        )

        assert intent_id is None
        assert "below exchange minimum" in reasons[0]
        assert "effective entry headroom 5E-7" in reasons[0]
    finally:
        connection.close()


def test_bucket_switch_hysteresis_marks_probability_dominant_candidate(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "switch.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repository = Repository(connection)
        for market_id, bucket in (("held", "80-81F"), ("better", "82-83F")):
            market = Market(
                id=market_id,
                title=f"Will the high temperature in New York be {bucket} on July 16, 2026?",
                description="Settlement source: NOAA station KNYC.",
                event_slug="nyc-july-16",
                yes_token_id=f"yes-{market_id}",
                no_token_id=f"no-{market_id}",
                is_weather=True,
            )
            repository.upsert_market(
                type("ModuleMarket", (), {**market.__dict__, "module_id": "global_temp_bucket"})(),
                {"id": market_id},
            )
            repository.save_temperature_bucket_rule(
                market_id,
                parse_global_temperature_bucket_rule(market.title, market.description),
                module_id="global_temp_bucket",
            )
        repository.replace_positions(
            [{"market": "held", "token_id": "yes-held", "outcome": "Yes", "size": "5"}]
        )
        repository.save_analysis(
            Analysis(
                "held",
                "v2",
                Decimal("0.25"),
                Decimal("0.35"),
                Decimal("0.20"),
                Decimal("0.05"),
                "buy_yes",
                "trade",
                ["two-thirds model quorum"],
            )
        )
        repository.save_analysis(
            Analysis(
                "better",
                "v2",
                Decimal("0.75"),
                Decimal("0.85"),
                Decimal("0.20"),
                Decimal("0.50"),
                "buy_yes",
                "trade",
                ["two-thirds model quorum"],
            )
        )
        workflow = MarketWorkflowService(
            settings,
            repository,
            weather_provider_factory=MagicMock,
            polymarket_client_factory=MagicMock,
        )

        workflow._apply_bucket_switch_hysteresis(["held", "better"])

        latest = repository.latest_analysis("held")
        assert latest["decision"] == "watch"
        assert latest["side"] is None
        assert "rebalance_target=better" in json.loads(latest["reasons"])
    finally:
        connection.close()


def test_bucket_switch_hysteresis_does_not_abandon_more_likely_seoul_bucket(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "seoul-switch.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repository = Repository(connection)
        for market_id, bucket in (("seoul-26", "26C"), ("seoul-27", "27C")):
            market = Market(
                id=market_id,
                title=f"Will the highest temperature in Seoul be {bucket} on July 18, 2026?",
                description="Settlement source: Wunderground station RKSI.",
                event_slug="seoul-july-18",
                yes_token_id=f"yes-{market_id}",
                no_token_id=f"no-{market_id}",
                is_weather=True,
            )
            repository.upsert_market(
                type("ModuleMarket", (), {**market.__dict__, "module_id": "global_temp_bucket"})(),
                {"id": market_id},
            )
            repository.save_temperature_bucket_rule(
                market_id,
                parse_global_temperature_bucket_rule(market.title, market.description),
                module_id="global_temp_bucket",
            )
        repository.replace_positions(
            [{"market": "seoul-26", "token_id": "yes-seoul-26", "outcome": "Yes", "size": "6.25"}]
        )
        repository.save_analysis(
            Analysis(
                "seoul-26",
                "global-temp-bucket-multimodel-v6",
                Decimal("0.1851"),
                Decimal("0.4689"),
                Decimal("0.33"),
                Decimal("-0.0244"),
                None,
                "watch",
                ["consensus_probability_median=0.3467"],
            )
        )
        repository.save_analysis(
            Analysis(
                "seoul-27",
                "global-temp-bucket-multimodel-v6",
                Decimal("0.1187"),
                Decimal("0.5869"),
                Decimal("0.15"),
                Decimal("0.1397"),
                "buy_yes",
                "trade",
                ["consensus_probability_median=0.3218"],
            )
        )
        workflow = MarketWorkflowService(
            settings,
            repository,
            weather_provider_factory=MagicMock,
            polymarket_client_factory=MagicMock,
        )

        workflow._apply_bucket_switch_hysteresis(["seoul-26", "seoul-27"])

        latest = repository.latest_analysis("seoul-26")
        assert latest["model_version"] == "global-temp-bucket-multimodel-v6"
        assert not any(
            str(reason).startswith("rebalance_target=") for reason in json.loads(latest["reasons"])
        )
    finally:
        connection.close()


def test_d0_entry_gate_does_not_turn_existing_position_into_model_reversal(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "entry-gate.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repository = Repository(connection)
        repository.upsert_market(
            Market(id="held", title="held bucket", yes_token_id="yes", no_token_id="no"),
            {"id": "held"},
        )
        repository.replace_positions(
            [{"market": "held", "token_id": "yes", "outcome": "Yes", "size": "5"}]
        )
        repository.save_analysis(
            Analysis(
                "held",
                "v2",
                Decimal("0.6"),
                Decimal("0.7"),
                Decimal("0.2"),
                Decimal("0.4"),
                "buy_yes",
                "trade",
                ["model still supports held bucket"],
            )
        )
        workflow = MarketWorkflowService(
            settings,
            repository,
            weather_provider_factory=MagicMock,
            polymarket_client_factory=MagicMock,
        )

        result = workflow._save_global_bucket_guard_rejection(
            "held", "D0 entry requires a fresh verified observation"
        )

        assert result.decision == "trade"
        assert result.side == "buy_yes"
        assert any("entry_gate_only=" in reason for reason in result.reasons)
    finally:
        connection.close()
