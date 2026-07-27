from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.weather import ForecastSnapshot, WeatherObservation
from polymarket_weather_arb.services.market_workflow_service import (
    D0ObservationContext,
    MarketWorkflowService,
    _condition_d0_hourly_context,
    _forecast_calibration_phase,
    _forecast_lead_hours,
    _global_weather_cache_ttl,
    _merge_station_observations,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FailingWeatherProvider:
    def fetch_forecast(self, market_id, rule):
        raise AssertionError("generic weather provider should not handle China buckets")


class FakeChinaWeatherProvider:
    def fetch_forecast(self, market_id, rule):
        now = datetime.now(timezone.utc)
        return (
            ForecastSnapshot(
                provider="fake-china-official",
                variable=rule.variable,
                value=Decimal("18"),
                unit="C",
                issue_time=now,
                valid_time=now,
                market_id=market_id,
                location=rule.city,
                station=rule.station_id,
                lower_value=Decimal("17.8"),
                upper_value=Decimal("18.2"),
                fetched_at=now,
            ),
            {"fake": True},
        )


class FakeGlobalWeatherProvider:
    def fetch_forecast(self, market_id, rule):
        now = datetime.now(timezone.utc)
        return (
            ForecastSnapshot(
                provider="fake-global-weather",
                variable=rule.variable,
                value=Decimal("80.5"),
                unit=rule.unit,
                issue_time=now,
                valid_time=now,
                market_id=market_id,
                location=rule.location,
                station=rule.station,
                lower_value=Decimal("80.2"),
                upper_value=Decimal("80.8"),
                fetched_at=now,
            ),
            {
                "fake": True,
                "coordinate_source": "awc_stationinfo",
                "forecast_station": rule.station,
                "model_members": {
                    "gefs": [80.5],
                    "ecmwf": [80.5],
                    "icon": [80.5],
                },
            },
        )


class FakeGlobalWeatherWithCoordinates(FakeGlobalWeatherProvider):
    def fetch_forecast(self, market_id, rule):
        forecast, raw = super().fetch_forecast(market_id, rule)
        return forecast, {
            **raw,
            "latitude": 40.7,
            "longitude": -74.0,
            "timezone": "America/New_York",
            "target_date": rule.target_date,
        }


class SplitTopCandidateGlobalWeatherProvider(FakeGlobalWeatherProvider):
    def fetch_forecast(self, market_id, rule):
        forecast, raw = super().fetch_forecast(market_id, rule)
        return forecast, {
            **raw,
            "latitude": 40.7,
            "longitude": -74.0,
            "timezone": "America/New_York",
            "target_date": rule.target_date,
            "model_members": {
                "ecmwf": [80.5],
                "icon": [82.5],
                "gefs": [82.5],
            },
        }


class CountingGlobalWeatherProvider(FakeGlobalWeatherProvider):
    def __init__(self):
        self.calls = 0

    def fetch_forecast(self, market_id, rule):
        self.calls += 1
        return super().fetch_forecast(market_id, rule)


class FlakyGlobalWeatherProvider(CountingGlobalWeatherProvider):
    def __init__(self):
        super().__init__()
        self.fail = False

    def fetch_forecast(self, market_id, rule):
        if self.fail:
            self.calls += 1
            raise RuntimeError("HTTP 429 ensemble quota exhausted")
        return super().fetch_forecast(market_id, rule)


class UnavailableGlobalWeatherProvider:
    name = "unavailable"

    def fetch_forecast(self, market_id, rule):
        raise ValueError("forecast does not include target day")


class FakeD0ObservationProvider:
    def __init__(self, observed_at: datetime, *, value: Decimal = Decimal("80")):
        self.observed_at = observed_at
        self.value = value
        self.calls = 0

    def fetch_observation(self, market_id, rule):
        self.calls += 1
        observation = WeatherObservation(
            market_id=market_id,
            provider="NOAA",
            station="KNYC",
            variable="temperature_high",
            value=self.value,
            unit="F",
            observed_at=self.observed_at,
            quality_status="V",
            fetched_at=self.observed_at,
        )
        return observation, {
            "source_grade": "settlement_observation",
            "official_signal": True,
            "latest_observation_at": self.observed_at.isoformat(),
            "observations": [
                {
                    "timestamp": self.observed_at.isoformat(),
                    "value": str(self.value),
                    "unit": "F",
                    "quality_status": "V",
                }
            ],
        }


class UnavailableD0ObservationProvider:
    def __init__(self):
        self.calls = 0

    def fetch_observation(self, market_id, rule):
        self.calls += 1
        raise ValueError("no observation samples")


class FakePrecipWeatherProvider:
    def fetch_forecast(self, market_id, rule):
        now = datetime.now(timezone.utc)
        return (
            ForecastSnapshot(
                provider="fake-precip-weather",
                variable=rule.variable,
                value=Decimal("1.4"),
                unit=rule.unit,
                issue_time=now,
                valid_time=now,
                market_id=market_id,
                location=rule.location,
                station=rule.station,
                lower_value=Decimal("1.3"),
                upper_value=Decimal("1.5"),
                fetched_at=now,
            ),
            {"fake": True},
        )


class FakePolymarketClient:
    def __init__(self, settings):
        self.settings = settings


class CandidateRule:
    tradable = True
    rejection_reason = None


def test_global_weather_cache_ttl_is_horizon_aware():
    rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 80-81F on July 14, 2026?",
        "Settlement source: NOAA station KNYC.",
    )

    assert _global_weather_cache_ttl(
        rule,
        datetime(2026, 7, 14, 14, tzinfo=timezone.utc),
    ) == timedelta(hours=2)
    assert _global_weather_cache_ttl(
        rule,
        datetime(2026, 7, 13, 14, tzinfo=timezone.utc),
    ) == timedelta(hours=6)


def test_market_workflow_routes_china_bucket_to_china_provider_and_pricing(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "workflow.db", MAX_ORDER_USDC=Decimal("1"))
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repo = Repository(connection)
        market = Market(
            id="shanghai-18c",
            slug="highest-temperature-in-shanghai-on-may-10-2026-18c",
            title="Highest temperature in Shanghai on May 10?",
            description="Outcome: 18°C. Resolved according to Wunderground on 10 May '26.",
            event_slug="highest-temperature-in-shanghai-on-may-10-2026",
            event_title="Highest temperature in Shanghai on May 10?",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
        )
        repo.upsert_market(
            type("ModuleMarket", (), {**market.__dict__, "module_id": "china_temp_bucket"})(),
            {"id": market.id},
        )
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.03"),
                best_ask=Decimal("0.04"),
                midpoint=Decimal("0.035"),
                spread=Decimal("0.01"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )
        repo.upsert_candidate(
            market.id, CandidateRule(), status="dry_run_ready", module_id="china_temp_bucket"
        )

        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FailingWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
            china_weather_provider_factory=FakeChinaWeatherProvider,
        )
        result = workflow.research_market(market.id)
        dry_run = workflow.dry_run_trade(market.id)
        connection.commit()

        analysis = repo.latest_analysis(market.id)
        order = repo.list_recent_order_intents(limit=1, market_id=market.id)[0]
        bucket_rule = repo.get_temperature_bucket_rule(market.id)
    finally:
        connection.close()

    assert "China research complete" in result.summary
    assert "Order intent" in dry_run.summary
    assert analysis["model_version"] == "china-temp-bucket-normal-v1"
    assert analysis["side"] == "buy_yes"
    assert order["dry_run"] == 1
    assert order["status"] == "dry_run"
    assert bucket_rule["city"] == "Shanghai"


def test_market_workflow_routes_global_temp_bucket_to_bucket_pricing(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "workflow.db", MAX_ORDER_USDC=Decimal("1"))
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repo = Repository(connection)
        market = Market(
            id="nyc-80-81f",
            slug="high-temperature-new-york-june-10-2026-80-81f",
            title="Will the high temperature in New York be 80-81F on June 10, 2026?",
            description="Settlement source: NOAA station KNYC.",
            event_slug="high-temperature-new-york-june-10-2026",
            event_title="High temperature in New York on June 10?",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
        )
        repo.upsert_market(
            type("ModuleMarket", (), {**market.__dict__, "module_id": "global_temp_bucket"})(),
            {"id": market.id},
        )
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.05"),
                best_ask=Decimal("0.08"),
                midpoint=Decimal("0.065"),
                spread=Decimal("0.03"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )
        repo.upsert_candidate(
            market.id, CandidateRule(), status="dry_run_ready", module_id="global_temp_bucket"
        )

        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FakeGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
            china_weather_provider_factory=FakeChinaWeatherProvider,
        )
        result = workflow.research_market(market.id)
        dry_run = workflow.dry_run_trade(market.id)
        connection.commit()

        analysis = repo.latest_analysis(market.id)
        orders = repo.list_recent_order_intents(limit=1, market_id=market.id)
        bucket_rule = repo.get_temperature_bucket_rule(market.id)
    finally:
        connection.close()

    assert "Global bucket research complete" in result.summary
    assert "Order skipped" in dry_run.summary
    assert analysis["model_version"] == "global-temp-bucket-multimodel-v8"
    assert analysis["decision"] == "watch"
    assert analysis["side"] is None
    assert orders == []
    assert "strict majority" in analysis["reasons"]
    assert bucket_rule["module_id"] == "global_temp_bucket"
    assert bucket_rule["city"] == "New York"
    assert bucket_rule["station_id"] == "KNYC"


def test_global_bucket_batch_reuses_forecast_for_sibling_markets(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "workflow.db", MAX_ORDER_USDC=Decimal("1"))
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    provider = CountingGlobalWeatherProvider()
    try:
        repo = Repository(connection)
        for market_id, bucket in (("nyc-80-81f", "80-81F"), ("nyc-82-83f", "82-83F")):
            market = Market(
                id=market_id,
                title=(f"Will the high temperature in New York be {bucket} on June 10, 2026?"),
                description="Settlement source: NOAA station KNYC.",
                yes_token_id=f"yes-{market_id}",
                no_token_id=f"no-{market_id}",
                is_weather=True,
            )
            repo.upsert_market(
                type(
                    "ModuleMarket",
                    (),
                    {**market.__dict__, "module_id": "global_temp_bucket"},
                )(),
                {"id": market.id},
            )
            repo.save_market_snapshot(
                MarketSnapshot(
                    market_id=market.id,
                    best_bid=Decimal("0.05"),
                    best_ask=Decimal("0.08"),
                    midpoint=Decimal("0.065"),
                    spread=Decimal("0.03"),
                    liquidity=Decimal("100"),
                    fetched_at=datetime.now(timezone.utc),
                ),
                {"market": market.id},
            )
            repo.upsert_candidate(
                market.id,
                CandidateRule(),
                status="dry_run_ready",
                module_id="global_temp_bucket",
            )

        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=lambda: provider,
            polymarket_client_factory=FakePolymarketClient,
        )

        now = datetime.now(timezone.utc)
        analyzed, failures = workflow.research_global_bucket_batch(
            ["nyc-80-81f", "nyc-82-83f"], now=now
        )
        analyzed_again, failures_again = workflow.research_global_bucket_batch(
            ["nyc-80-81f", "nyc-82-83f"], now=now + timedelta(minutes=5)
        )

        assert analyzed == 2
        assert failures == []
        assert analyzed_again == 2
        assert failures_again == []
        assert provider.calls == 1
        assert repo.latest_forecast("nyc-80-81f") is not None
        assert repo.latest_forecast("nyc-82-83f") is not None
        assert repo.latest_analysis("nyc-80-81f") is not None
        assert repo.latest_analysis("nyc-82-83f") is not None
        source_signal = connection.execute(
            """
            SELECT raw_payload FROM model_signals
            WHERE market_id = 'nyc-80-81f' AND model_version = 'global-temp-source-v2'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        source_payload = json.loads(source_signal["raw_payload"])
        assert source_payload["station"] == "KNYC"
        assert source_payload["raw_yes_probability"] is not None
        assert source_payload["applied_bias"] == 0.0
        forecast_rows = connection.execute(
            "SELECT COUNT(*) AS count FROM weather_forecasts"
        ).fetchone()["count"]
        assert forecast_rows == 2
    finally:
        connection.close()


def test_partial_batch_uses_complete_gamma_event_for_top_candidate_votes(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "complete-event.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    now = datetime.now(timezone.utc)
    target_date = (now + timedelta(days=2)).date()
    target_label = f"{target_date.strftime('%B')} {target_date.day}, {target_date.year}"
    event_slug = (
        "highest-temperature-in-new-york-on-"
        f"{target_date.strftime('%B').lower()}-{target_date.day}-{target_date.year}"
    )
    buckets = {
        "nyc-80": "80-81F",
        "nyc-82": "82-83F",
        "nyc-84": "84-85F",
    }
    event_markets = [{"id": market_id} for market_id in buckets]
    try:
        repo = Repository(connection)
        for market_id, bucket in buckets.items():
            market = Market(
                id=market_id,
                title=(
                    f"Will the highest temperature in New York be {bucket} "
                    f"on {target_label}?"
                ),
                description="Settlement source: NOAA station KNYC.",
                event_slug=event_slug,
                event_title=f"Highest temperature in New York on {target_label}",
                yes_token_id=f"yes-{market_id}",
                no_token_id=f"no-{market_id}",
                is_weather=True,
            )
            repo.upsert_market(
                type(
                    "ModuleMarket",
                    (),
                    {**market.__dict__, "module_id": "global_temp_bucket"},
                )(),
                {
                    "id": market_id,
                    "events": [{"slug": event_slug, "markets": event_markets}],
                },
            )
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id="nyc-80",
                best_bid=Decimal("0.04"),
                best_ask=Decimal("0.05"),
                midpoint=Decimal("0.045"),
                spread=Decimal("0.01"),
                liquidity=Decimal("100"),
                fetched_at=now,
            ),
            {"market": "nyc-80"},
        )
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=SplitTopCandidateGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
        )

        analyzed, failures = workflow.research_global_bucket_batch(["nyc-80"], now=now)
        first = repo.latest_analysis("nyc-80")
        repriced, reprice_failures, slow_reason, _ = workflow.reprice_global_bucket_group_cached(
            ["nyc-80"],
            now=now + timedelta(minutes=1),
        )
        latest = repo.latest_analysis("nyc-80")

        assert analyzed == 1
        assert failures == []
        assert "top_candidate_family_supporters=1/3" in first["reasons"]
        assert "event_bucket_context=complete siblings=3 expected=3" in first["reasons"]
        assert repriced == 1, (reprice_failures, slow_reason)
        assert reprice_failures == []
        assert slow_reason is None
        assert "top_candidate_family_supporters=1/3" in latest["reasons"]
        assert latest["decision"] == "watch"
    finally:
        connection.close()


def test_incomplete_gamma_event_cannot_grant_low_price_top_rank(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "incomplete-event.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    now = datetime.now(timezone.utc)
    target_date = (now + timedelta(days=2)).date()
    target_label = f"{target_date.strftime('%B')} {target_date.day}, {target_date.year}"
    event_slug = (
        "highest-temperature-in-new-york-on-"
        f"{target_date.strftime('%B').lower()}-{target_date.day}-{target_date.year}"
    )
    try:
        repo = Repository(connection)
        market = Market(
            id="nyc-80",
            title=(
                "Will the highest temperature in New York be 80-81F "
                f"on {target_label}?"
            ),
            description="Settlement source: NOAA station KNYC.",
            event_slug=event_slug,
            event_title=f"Highest temperature in New York on {target_label}",
            yes_token_id="yes-nyc-80",
            no_token_id="no-nyc-80",
            is_weather=True,
        )
        repo.upsert_market(
            type(
                "ModuleMarket",
                (),
                {**market.__dict__, "module_id": "global_temp_bucket"},
            )(),
            {
                "id": market.id,
                "events": [
                    {
                        "slug": event_slug,
                        "markets": [{"id": "nyc-80"}, {"id": "nyc-82"}],
                    }
                ],
            },
        )
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.04"),
                best_ask=Decimal("0.05"),
                midpoint=Decimal("0.045"),
                spread=Decimal("0.01"),
                liquidity=Decimal("100"),
                fetched_at=now,
            ),
            {"market": market.id},
        )
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FakeGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
        )

        analyzed, failures = workflow.research_global_bucket_batch([market.id], now=now)
        analysis = repo.latest_analysis(market.id)

        assert analyzed == 1
        assert failures == []
        assert analysis["decision"] == "watch"
        assert "event_bucket_context=incomplete siblings=1 expected=2" in analysis["reasons"]
        assert "low_price_top_candidate_majority=False; supporters=None/None" in analysis["reasons"]
    finally:
        connection.close()


def test_global_bucket_batch_uses_stale_multimodel_cache_on_429(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "cache-fallback.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    provider = FlakyGlobalWeatherProvider()
    now = datetime.now(timezone.utc)
    try:
        repo = Repository(connection)
        _seed_d0_global_market(repo, "nyc-cache-fallback")
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=lambda: provider,
            polymarket_client_factory=FakePolymarketClient,
        )

        analyzed, failures = workflow.research_global_bucket_batch(["nyc-cache-fallback"], now=now)
        provider.fail = True
        analyzed_cached, failures_cached = workflow.research_global_bucket_batch(
            ["nyc-cache-fallback"], now=now + timedelta(hours=6, minutes=1)
        )

        latest = repo.latest_analysis("nyc-cache-fallback")
        assert analyzed == 1
        assert failures == []
        assert analyzed_cached == 1
        assert failures_cached == []
        assert provider.calls == 2
        assert "weather_cache_status=stale_if_error" in latest["reasons"]
        assert "HTTP 429 ensemble quota exhausted" in latest["reasons"]
        forecast_rows = connection.execute(
            "SELECT COUNT(*) AS count FROM weather_forecasts"
        ).fetchone()["count"]
        assert forecast_rows == 1
    finally:
        connection.close()


def test_fresh_legacy_forecast_cache_is_upgraded_with_awc_taf_without_ensemble_refetch(
    tmp_path,
):
    settings = Settings(DATABASE_PATH=tmp_path / "taf-cache-upgrade.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    now = datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Seoul be 27C on July 23, 2026?",
        "Settlement source: Wunderground station RKSI.",
    )
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_high",
        value=Decimal("27"),
        unit="C",
        issue_time=now,
        valid_time=now + timedelta(days=1),
        market_id="seoul-27",
        location="Seoul",
        station="RKSI",
        fetched_at=now,
    )
    legacy_payload = {
        "target_date": "2026-07-23",
        "model_members": {
            "ecmwf": [27.0],
            "gfs": [27.0],
            "icon": [28.0],
        },
        "model_count": 3,
    }
    primary = MagicMock()
    taf = MagicMock()
    taf.fetch_forecast.return_value = (
        ForecastSnapshot(
            provider="awc-taf",
            variable="temperature_high",
            value=Decimal("27"),
            unit="C",
            issue_time=now,
            valid_time=now + timedelta(days=1),
            market_id="seoul-27",
            location="Seoul",
            station="RKSI",
            fetched_at=now,
        ),
        {
            "provider": "awc-taf",
            "provider_cache_status": "network_fresh",
            "station": "RKSI",
            "issue_time": now.isoformat(),
            "valid_time": (now + timedelta(days=1)).isoformat(),
        },
    )
    try:
        workflow = MarketWorkflowService(
            settings,
            Repository(connection),
            weather_provider_factory=lambda: primary,
            polymarket_client_factory=FakePolymarketClient,
            awc_forecast_provider_factory=lambda: taf,
        )

        _cached_forecast, upgraded = workflow._fetch_global_weather_with_cache(
            "seoul-27",
            rule,
            now=now + timedelta(minutes=1),
            cached=(forecast, legacy_payload, timedelta(minutes=1)),
        )

        primary.fetch_forecast.assert_not_called()
        taf.fetch_forecast.assert_called_once()
        assert upgraded["cache_status"] == "fresh_cache"
        assert upgraded["model_count"] == 4
        assert upgraded["model_members"]["reference_awc-taf"] == [27.0]
        assert upgraded["pricing_references"]["awc_taf"]["status"] == "available"
    finally:
        connection.close()


def test_cached_reprice_excludes_stale_awc_taf_from_entry_decision(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "stale-taf-reprice.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    now = datetime.now(timezone.utc)
    target_date = (now + timedelta(days=2)).date()
    target_label = f"{target_date.strftime('%B')} {target_date.day}, {target_date.year}"
    market_id = "nyc-stale-taf"
    try:
        repo = Repository(connection)
        market = Market(
            id=market_id,
            title=(
                "Will the highest temperature in New York be 80-81F on "
                f"{target_label}?"
            ),
            description="Settlement source: NOAA station KNYC.",
            event_slug=(
                "highest-temperature-in-new-york-on-"
                f"{target_date.strftime('%B').lower()}-{target_date.day}-{target_date.year}"
            ),
            yes_token_id=f"yes-{market_id}",
            no_token_id=f"no-{market_id}",
            is_weather=True,
        )
        repo.upsert_market(
            type(
                "ModuleMarket",
                (),
                {**market.__dict__, "module_id": "global_temp_bucket"},
            )(),
            {"id": market_id},
        )
        rule = parse_global_temperature_bucket_rule(market.title, market.description)
        repo.save_temperature_bucket_rule(market_id, rule, module_id="global_temp_bucket")
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id=market_id,
                best_bid=Decimal("0.08"),
                best_ask=Decimal("0.10"),
                midpoint=Decimal("0.09"),
                spread=Decimal("0.02"),
                liquidity=Decimal("100"),
                fetched_at=now,
            ),
            {"market": market_id},
        )
        repo.save_forecast(
            ForecastSnapshot(
                provider="open-meteo-ensemble",
                variable="temperature_high",
                value=Decimal("80.5"),
                unit="F",
                issue_time=now,
                valid_time=now + timedelta(days=2),
                market_id=market_id,
                location="New York",
                station="KNYC",
                fetched_at=now,
            ),
            {
                "coordinate_source": "awc_stationinfo",
                "forecast_station": "KNYC",
                "target_date": target_date.isoformat(),
                "model_members": {
                    "ecmwf": [80.5],
                    "icon": [80.5],
                    "gefs": [80.5],
                    "reference_awc-taf": [90.0],
                },
                "pricing_references": {
                    "awc_taf": {
                        "status": "available",
                        "provider_cache_status": "fresh_cache",
                        "station": "KNYC",
                        "issue_time": (now - timedelta(hours=13)).isoformat(),
                        "valid_time": (now + timedelta(days=2)).isoformat(),
                        "value": 90.0,
                        "unit": "F",
                    }
                },
            },
        )
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FailingWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
        )

        analyzed, failures, slow_reason, _ = workflow.reprice_global_bucket_group_cached(
            [market_id],
            now=now,
        )
        analysis = repo.latest_analysis(market_id)

        assert analyzed == 1
        assert failures == []
        assert slow_reason is None
        assert analysis["decision"] == "trade"
        assert "awc_taf_pricing_status=issue_time_stale; included=False" in analysis["reasons"]
        assert "reference_awc-taf" not in next(
            reason
            for reason in json.loads(analysis["reasons"])
            if reason.startswith("model_probabilities=")
        )
    finally:
        connection.close()


def test_station_market_rejects_legacy_city_coordinate_forecast_cache(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "station-cache.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repo = Repository(connection)
        _seed_d0_global_market(repo, "station-cache")
        market = repo.get_market("station-cache")
        rule = parse_global_temperature_bucket_rule(market["title"], market["description"])
        forecast, payload = FakeGlobalWeatherProvider().fetch_forecast("station-cache", rule)
        legacy_payload = {
            **payload,
            "coordinate_source": "city_geocode",
            "forecast_station": None,
            "target_date": rule.target_date,
        }
        repo.save_forecast(forecast, legacy_payload)
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FakeGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
        )

        assert (
            workflow._cached_global_weather(
                "station-cache",
                rule,
                now=forecast.fetched_at + timedelta(seconds=1),
            )
            is None
        )
    finally:
        connection.close()


def test_d0_batch_uses_fresh_observed_max_before_local_noon(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "d0-observed.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    now = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)  # 10:00 New York
    observations = FakeD0ObservationProvider(now)
    try:
        repo = Repository(connection)
        _seed_d0_global_market(repo, "nyc-d0-observed")
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FakeGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
            observation_provider_factory=lambda: observations,
        )

        analyzed, failures = workflow.research_global_bucket_batch(["nyc-d0-observed"], now=now)

        analysis = repo.latest_analysis("nyc-d0-observed")
        assert analyzed == 1
        assert failures == []
        assert observations.calls == 1
        assert analysis["model_version"] == "global-temp-bucket-multimodel-v8"
        assert "D0 observed max-to-date=80F" in analysis["reasons"]
        assert repo.latest_observation("nyc-d0-observed") is not None
    finally:
        connection.close()


def test_d0_batch_after_local_noon_uses_observation_and_normal_consensus(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "d0-noon.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    now = datetime(2026, 7, 14, 17, 0, tzinfo=timezone.utc)  # 13:00 New York
    observations = FakeD0ObservationProvider(now - timedelta(minutes=15))
    try:
        repo = Repository(connection)
        _seed_d0_global_market(repo, "nyc-d0-noon")
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FakeGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
            observation_provider_factory=lambda: observations,
        )

        analyzed, failures = workflow.research_global_bucket_batch(["nyc-d0-noon"], now=now)

        analysis = repo.latest_analysis("nyc-d0-noon")
        assert analyzed == 1
        assert failures == []
        assert observations.calls == 1
        assert analysis["decision"] == "trade"
        assert analysis["model_version"] == "global-temp-bucket-multimodel-v8"
        assert "supporting_models=3/3 required=2" in analysis["reasons"]
    finally:
        connection.close()


def test_d0_batch_adds_hourly_conditioned_source_and_persists_source_signals(tmp_path, monkeypatch):
    settings = Settings(DATABASE_PATH=tmp_path / "d0-hourly.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    now = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)
    observations = FakeD0ObservationProvider(now - timedelta(minutes=10), value=Decimal("80"))

    def hourly_context(self, **kwargs):
        return {
            "source": "open-meteo-hourly",
            "timezone": "America/New_York",
            "target_date": "2026-07-14",
            "fetched_at": now.isoformat(),
            "local_now": "2026-07-14T10:00:00-04:00",
            "unit": "F",
            "remaining_peak": "81.2",
            "remaining_peak_time": "2026-07-14T15:00:00-04:00",
            "all_day_forecast_peak": "81.2",
            "hours_to_remaining_peak": "5",
            "post_forecast_peak": False,
            "peak_cloud_cover": 20.0,
            "peak_shortwave_radiation": 700.0,
            "peak_wind_speed": 8.0,
            "records": [],
        }

    monkeypatch.setattr(
        "polymarket_weather_arb.services.market_workflow_service.OpenMeteoProvider.fetch_hourly_context",
        hourly_context,
    )
    try:
        repo = Repository(connection)
        _seed_d0_global_market(repo, "nyc-d0-hourly")
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FakeGlobalWeatherWithCoordinates,
            polymarket_client_factory=FakePolymarketClient,
            observation_provider_factory=lambda: observations,
        )

        analyzed, failures = workflow.research_global_bucket_batch(["nyc-d0-hourly"], now=now)

        analysis = repo.latest_analysis("nyc-d0-hourly")
        signals = repo.list_model_signals(
            market_id="nyc-d0-hourly", model_version="global-temp-source-v2"
        )
        assert analyzed == 1
        assert failures == []
        assert "reference_hourly-open-meteo" in {signal["forecast_provider"] for signal in signals}
        reasons = json.loads(analysis["reasons"])
        assert any("D0 hourly context" in reason for reason in reasons)
        assert any("remaining_peak=81.2F" in reason for reason in reasons)
        assert any("excluded from independent-model quorum" in reason for reason in reasons)
        assert any("supporting_models=3/3" in reason for reason in reasons)
        hourly_signal = next(
            signal
            for signal in signals
            if signal["forecast_provider"] == "reference_hourly-open-meteo"
        )
        hourly_payload = json.loads(hourly_signal["raw_payload"])
        assert hourly_payload["calibration_phase"] == "D0_pre_peak"
        assert hourly_payload["lead_hours"] == 5.0
        assert hourly_payload["source_family"] == "d0-hourly"
        assert Decimal(str(hourly_signal["market_price"])) == Decimal("0.065")
        persisted = json.loads(repo.latest_forecast("nyc-d0-hourly")["raw_payload"])
        assert persisted["d0_hourly_context"]["conditioned_final_peak"] == "81.2"
    finally:
        connection.close()


def test_d0_hourly_context_anchors_forecast_and_limits_impossible_warming():
    now = datetime(2026, 7, 18, 5, 22, tzinfo=timezone.utc)
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Wuhan be 32C on July 18, 2026?",
        "Settlement source: Wunderground station ZHHH.",
    )
    observation = WeatherObservation(
        market_id="wuhan-32",
        provider="AWC",
        station="ZHHH",
        variable="temperature_high",
        value=Decimal("28"),
        unit="C",
        observed_at=datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc),
        quality_status="AWC",
        fetched_at=now,
    )
    observation_context = D0ObservationContext(
        observation=observation,
        raw_payload={
            "latest_observation_at": "2026-07-18T05:00:00+00:00",
            "observations": [
                {
                    "timestamp": "2026-07-18T04:30:00+00:00",
                    "value": "28",
                    "unit": "C",
                },
                {
                    "timestamp": "2026-07-18T05:00:00+00:00",
                    "value": "28",
                    "unit": "C",
                },
            ],
        },
    )
    hourly = {
        "remaining_peak": "32.1",
        "post_forecast_peak": False,
        "records": [
            {"time": "2026-07-18T12:00:00+08:00", "temperature": 30.5},
            {"time": "2026-07-18T13:00:00+08:00", "temperature": 31.5},
            {"time": "2026-07-18T14:00:00+08:00", "temperature": 32.1},
            {"time": "2026-07-18T15:00:00+08:00", "temperature": 31.8},
        ],
    }

    conditioned = _condition_d0_hourly_context(
        hourly,
        observation_context=observation_context,
        rule=rule,
        now=now,
    )

    assert Decimal(conditioned["forecast_at_observation"]) == Decimal("31.5")
    assert Decimal(conditioned["forecast_anchor_error"]) == Decimal("-3.5")
    assert Decimal(conditioned["max_warming_rate_per_hour"]) == Decimal("1.5")
    assert Decimal(conditioned["conditioned_final_peak"]) < Decimal("30")
    assert Decimal(conditioned["trajectory_upper_bound"]) < Decimal("31.5")
    assert conditioned["trajectory_limited"] is True


def test_d0_hourly_context_keeps_reachable_rising_bucket():
    now = datetime(2026, 7, 18, 5, 15, tzinfo=timezone.utc)
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Wuhan be 32C on July 18, 2026?",
        "Settlement source: Wunderground station ZHHH.",
    )
    observation = WeatherObservation(
        market_id="wuhan-32",
        provider="AWC",
        station="ZHHH",
        variable="temperature_high",
        value=Decimal("30"),
        unit="C",
        observed_at=datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc),
        quality_status="AWC",
        fetched_at=now,
    )
    context = D0ObservationContext(
        observation=observation,
        raw_payload={
            "latest_observation_at": "2026-07-18T05:00:00+00:00",
            "observations": [
                {
                    "timestamp": "2026-07-18T04:30:00+00:00",
                    "value": "29",
                    "unit": "C",
                },
                {
                    "timestamp": "2026-07-18T05:00:00+00:00",
                    "value": "30",
                    "unit": "C",
                },
            ],
        },
    )
    hourly = {
        "remaining_peak": "32.0",
        "post_forecast_peak": False,
        "records": [
            {"time": "2026-07-18T13:00:00+08:00", "temperature": 30.2},
            {"time": "2026-07-18T14:00:00+08:00", "temperature": 32.0},
        ],
    }

    conditioned = _condition_d0_hourly_context(
        hourly,
        observation_context=context,
        rule=rule,
        now=now,
    )

    assert Decimal(conditioned["conditioned_final_peak"]) > Decimal("31.5")
    assert Decimal(conditioned["trajectory_upper_bound"]) > Decimal("32")


def test_d0_hourly_context_keeps_current_station_bias_for_remaining_day():
    now = datetime(2026, 7, 18, 5, 22, tzinfo=timezone.utc)
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Wuhan be 32C on July 18, 2026?",
        "Settlement source: Wunderground station ZHHH.",
    )
    observation = WeatherObservation(
        market_id="wuhan-32",
        provider="AWC",
        station="ZHHH",
        variable="temperature_high",
        value=Decimal("28"),
        unit="C",
        observed_at=datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc),
        quality_status="AWC",
        fetched_at=now,
    )
    context = D0ObservationContext(
        observation=observation,
        raw_payload={
            "latest_observation_at": "2026-07-18T05:00:00+00:00",
            "observations": [
                {
                    "timestamp": "2026-07-18T04:00:00+00:00",
                    "value": "28",
                    "unit": "C",
                },
                {
                    "timestamp": "2026-07-18T05:00:00+00:00",
                    "value": "28",
                    "unit": "C",
                },
            ],
        },
    )
    hourly = {
        "remaining_peak": "32.1",
        "post_forecast_peak": False,
        "records": [
            {"time": "2026-07-18T13:00:00+08:00", "temperature": 29.9},
            {"time": "2026-07-18T14:00:00+08:00", "temperature": 32.1},
            {"time": "2026-07-18T15:00:00+08:00", "temperature": 31.6},
            {"time": "2026-07-18T16:00:00+08:00", "temperature": 32.1},
        ],
    }

    conditioned = _condition_d0_hourly_context(
        hourly,
        observation_context=context,
        rule=rule,
        now=now,
    )

    assert Decimal(conditioned["forecast_anchor_error"]) == Decimal("-1.9")
    assert Decimal(conditioned["conditioned_final_peak"]) == Decimal("30.2")
    assert conditioned["trajectory_limited"] is True


def test_singapore_d0_peak_lock_requires_cooling_after_forecast_peak():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Singapore be 31C on July 20, 2026?",
        "Settlement source: Wunderground station WSSS.",
    )
    observation = WeatherObservation(
        market_id="singapore-31",
        provider="AWC",
        station="WSSS",
        variable="temperature_high",
        value=Decimal("31"),
        unit="C",
        observed_at=datetime(2026, 7, 20, 4, 20, tzinfo=timezone.utc),
        quality_status="AWC",
        fetched_at=datetime(2026, 7, 20, 4, 25, tzinfo=timezone.utc),
    )
    context = D0ObservationContext(
        observation=observation,
        raw_payload={
            "latest_observation_at": "2026-07-20T04:20:00+00:00",
            "observations": [
                {"timestamp": "2026-07-20T03:20:00+00:00", "value": "30.8", "unit": "C"},
                {"timestamp": "2026-07-20T04:20:00+00:00", "value": "31", "unit": "C"},
            ],
        },
    )
    hourly = {
        "remaining_peak": "31.6",
        "remaining_peak_time": "2026-07-20T12:00:00+08:00",
        "all_day_forecast_peak_time": "2026-07-20T12:00:00+08:00",
        "post_forecast_peak": True,
        "records": [
            {"time": "2026-07-20T11:00:00+08:00", "temperature": 31.2},
            {"time": "2026-07-20T12:00:00+08:00", "temperature": 31.6},
            {"time": "2026-07-20T13:00:00+08:00", "temperature": 31.2},
        ],
    }

    conditioned = _condition_d0_hourly_context(
        hourly,
        observation_context=context,
        rule=rule,
        now=datetime(2026, 7, 20, 4, 25, tzinfo=timezone.utc),
    )

    assert conditioned["peak_lock_confirmed"] is False
    assert Decimal(conditioned["peak_lock_cooling_amount"]) == Decimal("0")


def test_singapore_d0_peak_lock_confirms_after_sustained_cooling():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Singapore be 31C on July 20, 2026?",
        "Settlement source: Wunderground station WSSS.",
    )
    now = datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc)
    observation = WeatherObservation(
        market_id="singapore-31",
        provider="AWC",
        station="WSSS",
        variable="temperature_high",
        value=Decimal("31"),
        unit="C",
        observed_at=now,
        quality_status="AWC",
        fetched_at=now,
    )
    context = D0ObservationContext(
        observation=observation,
        raw_payload={
            "latest_observation_at": now.isoformat(),
            "observations": [
                {"timestamp": "2026-07-20T04:00:00+00:00", "value": "31", "unit": "C"},
                {"timestamp": now.isoformat(), "value": "30.4", "unit": "C"},
            ],
        },
    )
    hourly = {
        "remaining_peak": "30.7",
        "remaining_peak_time": "2026-07-20T13:00:00+08:00",
        "all_day_forecast_peak_time": "2026-07-20T12:00:00+08:00",
        "post_forecast_peak": True,
        "records": [
            {"time": "2026-07-20T12:00:00+08:00", "temperature": 31.1},
            {"time": "2026-07-20T13:00:00+08:00", "temperature": 30.7},
            {"time": "2026-07-20T14:00:00+08:00", "temperature": 30.2},
        ],
    }

    conditioned = _condition_d0_hourly_context(
        hourly, observation_context=context, rule=rule, now=now
    )

    assert conditioned["peak_lock_confirmed"] is True
    assert Decimal(conditioned["peak_lock_minutes_after_forecast_peak"]) == Decimal("60")
    assert Decimal(conditioned["recent_trend_per_hour"]) < 0


def test_nws_and_awc_duplicate_timestamp_is_one_station_sample():
    rule = type(
        "Rule",
        (),
        {
            "station": "KNYC",
            "variable": "temperature_high",
            "unit": "F",
            "target_date": "2026-07-14",
        },
    )()
    observed_at = datetime(2026, 7, 14, 18, tzinfo=timezone.utc)
    nws = WeatherObservation(
        market_id="m1",
        provider="noaa",
        station="KNYC",
        variable="temperature_high",
        value=Decimal("86"),
        unit="F",
        observed_at=observed_at,
        quality_status="V",
    )
    awc = WeatherObservation(
        market_id="m1",
        provider="awc-metar",
        station="KNYC",
        variable="temperature_high",
        value=Decimal("86"),
        unit="F",
        observed_at=observed_at,
        quality_status="AWC",
    )
    raw_record = {
        "timestamp": observed_at.isoformat(),
        "value": "86",
        "unit": "F",
    }

    observation, raw = _merge_station_observations(
        "m1",
        rule,
        [
            (
                nws,
                {
                    "station": "KNYC",
                    "source": "nws",
                    "observations": [{**raw_record, "quality_status": "V"}],
                },
            ),
            (
                awc,
                {
                    "station": "KNYC",
                    "source": "awc",
                    "observations": [{**raw_record, "quality_status": "AWC"}],
                },
            ),
        ],
        [],
    )

    assert observation.quality_status == "V"
    assert raw["observation_count"] == 1


def test_us_station_matrix_uses_awc_as_wunderground_aligned_primary(tmp_path):
    database = Database(tmp_path / "us-station-routing.db")
    database.init_schema()
    connection = database.connect()
    try:
        workflow = MarketWorkflowService(
            Settings(_env_file=None),
            Repository(connection),
            weather_provider_factory=FakeGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
        )
        nws = MagicMock()
        awc = MagicMock()
        workflow.observation_provider_factory = lambda: nws
        workflow.awc_observation_provider_factory = lambda: awc
        observed_at = datetime(2026, 7, 17, 18, tzinfo=timezone.utc)

        for station in ("KLGA", "KORD"):
            rule = _station_rule(station, "F")
            nws.fetch_observation.return_value = _station_evidence(
                station, "noaa", "V", observed_at, unit="F", value="91.4"
            )
            awc.fetch_observation.return_value = _station_evidence(
                station, "awc-metar", "AWC", observed_at, unit="F", value="89.06"
            )

            observation, raw = workflow._fetch_d0_station_observation(f"market-{station}", rule)

            assert observation.station == station
            assert observation.quality_status == "AWC"
            assert observation.value == Decimal("89.06")
            assert raw["observation_count"] == 1
            assert raw["sources"] == ["awc-metar"]
            assert raw["settlement_source"] is False
            assert raw["settlement_proxy"] is True
            assert raw["settlement_provider"] == "wunderground"
        nws.fetch_observation.assert_not_called()
        assert awc.fetch_observation.call_count == 2
    finally:
        connection.close()


def test_global_station_matrix_skips_nws_and_uses_awc(tmp_path):
    database = Database(tmp_path / "global-station-routing.db")
    database.init_schema()
    connection = database.connect()
    try:
        workflow = MarketWorkflowService(
            Settings(_env_file=None),
            Repository(connection),
            weather_provider_factory=FakeGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
        )
        nws = MagicMock()
        awc = MagicMock()
        workflow.observation_provider_factory = lambda: nws
        workflow.awc_observation_provider_factory = lambda: awc
        observed_at = datetime(2026, 7, 17, 6, tzinfo=timezone.utc)

        for station in ("ZSPD", "ZUUU", "RKSI", "EGLC"):
            awc.fetch_observation.return_value = _station_evidence(
                station, "awc-metar", "AWC", observed_at, unit="C"
            )

            observation, raw = workflow._fetch_d0_station_observation(
                f"market-{station}", _station_rule(station, "C")
            )

            assert observation.station == station
            assert observation.quality_status == "AWC"
            assert raw["sources"] == ["awc-metar"]
        nws.fetch_observation.assert_not_called()
        assert awc.fetch_observation.call_count == 4
    finally:
        connection.close()


def test_us_station_falls_back_to_nws_when_awc_is_unavailable(tmp_path):
    database = Database(tmp_path / "us-awc-fallback.db")
    database.init_schema()
    connection = database.connect()
    try:
        workflow = MarketWorkflowService(
            Settings(_env_file=None),
            Repository(connection),
            weather_provider_factory=FakeGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
        )
        nws = MagicMock()
        awc = MagicMock()
        workflow.observation_provider_factory = lambda: nws
        workflow.awc_observation_provider_factory = lambda: awc
        awc.fetch_observation.side_effect = ValueError("AWC station unavailable")
        nws.fetch_observation.return_value = _station_evidence(
            "KLGA",
            "noaa",
            "V",
            datetime(2026, 7, 17, 18, tzinfo=timezone.utc),
            unit="F",
        )

        observation, raw = workflow._fetch_d0_station_observation(
            "market-KLGA", _station_rule("KLGA", "F")
        )

        assert observation.station == "KLGA"
        assert observation.quality_status == "V"
        assert raw["sources"] == ["noaa"]
        assert any("awc: AWC station unavailable" in warning for warning in raw["warnings"])
        assert any("NWS five-minute observations" in warning for warning in raw["warnings"])
    finally:
        connection.close()


def _station_rule(station: str, unit: str):
    return SimpleNamespace(
        station=station,
        variable="temperature_high",
        unit=unit,
        target_date="2026-07-17",
    )


def _station_evidence(
    station: str,
    provider: str,
    quality_status: str,
    observed_at: datetime,
    *,
    unit: str,
    value: str | None = None,
):
    observation = WeatherObservation(
        market_id=f"market-{station}",
        provider=provider,
        station=station,
        variable="temperature_high",
        value=Decimal(value)
        if value is not None
        else (Decimal("86") if unit == "F" else Decimal("30")),
        unit=unit,
        observed_at=observed_at,
        quality_status=quality_status,
    )
    return observation, {
        "source": provider,
        "station": station,
        "timezone": "UTC",
        "observations": [
            {
                "timestamp": observed_at.isoformat(),
                "value": str(observation.value),
                "unit": unit,
                "quality_status": quality_status,
            }
        ],
        "warnings": [],
    }


def test_d0_batch_blocks_when_official_observation_is_unavailable(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "d0-unavailable.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    now = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)
    observations = UnavailableD0ObservationProvider()
    try:
        repo = Repository(connection)
        _seed_d0_global_market(repo, "nyc-d0-unavailable")
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FakeGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
            observation_provider_factory=lambda: observations,
        )

        analyzed, failures = workflow.research_global_bucket_batch(["nyc-d0-unavailable"], now=now)

        analysis = repo.latest_analysis("nyc-d0-unavailable")
        assert analyzed == 1
        assert failures == []
        assert observations.calls == 1
        assert analysis["decision"] == "reject"
        assert "requires official observed max-to-date" in analysis["reasons"]
    finally:
        connection.close()


def test_global_bucket_batch_failure_supersedes_stale_positive_edge(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "workflow.db")
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repo = Repository(connection)
        market = Market(
            id="dallas-stale-edge",
            title="Will the high temperature in Dallas be 100-101F on July 12, 2026?",
            description="Settlement source: Wunderground.",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
        )
        repo.upsert_market(
            type(
                "ModuleMarket",
                (),
                {**market.__dict__, "module_id": "global_temp_bucket"},
            )(),
            {"id": market.id},
        )
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.05"),
                best_ask=Decimal("0.08"),
                midpoint=Decimal("0.065"),
                spread=Decimal("0.03"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )
        repo.upsert_candidate(
            market.id,
            CandidateRule(),
            status="dry_run_ready",
            module_id="global_temp_bucket",
        )
        repo.save_analysis(
            Analysis(
                market_id=market.id,
                model_version="stale-positive-edge",
                fair_lower=Decimal("0.8"),
                fair_upper=Decimal("0.9"),
                reference_price=Decimal("0.08"),
                edge=Decimal("0.7"),
                side="buy_yes",
                decision="trade",
                reasons=["stale"],
            )
        )
        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=UnavailableGlobalWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
        )

        analyzed, failures = workflow.research_global_bucket_batch([market.id])
        latest = repo.latest_analysis(market.id)

        assert analyzed == 0
        assert failures == [f"{market.id}: forecast does not include target day"]
        assert latest["model_version"] == "global-temp-bucket-unavailable-v1"
        assert latest["decision"] == "reject"
        assert latest["edge"] == 0
    finally:
        connection.close()


def test_market_workflow_routes_precip_snow_through_threshold_analysis(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "workflow.db", MAX_ORDER_USDC=Decimal("1"))
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    try:
        repo = Repository(connection)
        market = Market(
            id="rain-m1",
            slug="rain-new-york-may-8-2026",
            title="Will rainfall in New York exceed 1 inch on May 8, 2026?",
            description="According to NOAA station KNYC.",
            event_slug="rain-new-york-may-8-2026",
            event_title="Rainfall in New York on May 8?",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
        )
        repo.upsert_market(
            type("ModuleMarket", (), {**market.__dict__, "module_id": "precip_snow"})(),
            {"id": market.id},
        )
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.30"),
                best_ask=Decimal("0.40"),
                midpoint=Decimal("0.35"),
                spread=Decimal("0.10"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )
        repo.upsert_candidate(
            market.id, CandidateRule(), status="dry_run_ready", module_id="precip_snow"
        )

        workflow = MarketWorkflowService(
            settings,
            repo,
            weather_provider_factory=FakePrecipWeatherProvider,
            polymarket_client_factory=FakePolymarketClient,
            china_weather_provider_factory=FakeChinaWeatherProvider,
        )
        result = workflow.research_market(market.id)
        dry_run = workflow.dry_run_trade(market.id)
        connection.commit()

        analysis = repo.latest_analysis(market.id)
        order = repo.list_recent_order_intents(limit=1, market_id=market.id)[0]
        rule = repo.get_resolution_rule(market.id)
    finally:
        connection.close()

    assert "Research complete" in result.summary
    assert "Order intent" in dry_run.summary
    assert analysis["model_version"] == "threshold-interval-v1"
    assert analysis["side"] == "buy_yes"
    assert order["dry_run"] == 1
    assert rule["variable"] == "precipitation"


def test_forecast_calibration_phase_uses_local_peak_lead_not_coarse_d0_bucket():
    rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 80-81F on July 14, 2026?",
        "Settlement source: NOAA station KNYC.",
    )
    pre_peak_context = {
        "remaining_peak_time": "2026-07-14T15:00:00-04:00",
        "hours_to_remaining_peak": "5",
        "post_forecast_peak": False,
    }
    near_peak_context = {
        **pre_peak_context,
        "hours_to_remaining_peak": "1.5",
    }
    post_peak_context = {
        **pre_peak_context,
        "post_forecast_peak": True,
    }

    assert (
        _forecast_calibration_phase(
            rule,
            datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
            pre_peak_context,
        )
        == "D0_early"
    )
    assert (
        _forecast_calibration_phase(
            rule,
            datetime(2026, 7, 14, 14, tzinfo=timezone.utc),
            pre_peak_context,
        )
        == "D0_pre_peak"
    )
    assert (
        _forecast_calibration_phase(
            rule,
            datetime(2026, 7, 14, 17, 30, tzinfo=timezone.utc),
            near_peak_context,
        )
        == "D0_near_peak"
    )
    assert (
        _forecast_calibration_phase(
            rule,
            datetime(2026, 7, 14, 20, tzinfo=timezone.utc),
            post_peak_context,
        )
        == "D0_post_peak"
    )
    assert _forecast_lead_hours(
        rule,
        datetime(2026, 7, 14, 14, tzinfo=timezone.utc),
        pre_peak_context,
    ) == Decimal("5.0")


def _seed_d0_global_market(repo: Repository, market_id: str) -> None:
    market = Market(
        id=market_id,
        title="Will the high temperature in New York be 80-81F on July 14, 2026?",
        description="Settlement source: NOAA station KNYC.",
        event_slug="high-temperature-new-york-july-14-2026",
        event_title="High temperature in New York on July 14?",
        yes_token_id=f"yes-{market_id}",
        no_token_id=f"no-{market_id}",
        is_weather=True,
    )
    repo.upsert_market(
        type("ModuleMarket", (), {**market.__dict__, "module_id": "global_temp_bucket"})(),
        {"id": market.id},
    )
    repo.save_market_snapshot(
        MarketSnapshot(
            market_id=market.id,
            best_bid=Decimal("0.05"),
            best_ask=Decimal("0.08"),
            midpoint=Decimal("0.065"),
            spread=Decimal("0.03"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {"market": market.id},
    )
    repo.upsert_candidate(
        market.id,
        CandidateRule(),
        status="dry_run_ready",
        module_id="global_temp_bucket",
    )
