"""Tests for ensemble workflow integration.

These tests verify that:
1. Ensemble analyze/dry-run workflow can run end-to-end
2. Forecast and analysis are saved correctly
3. source_grade is research_forecast (not official_forecast)
4. Live trade is rejected when source_grade is research_forecast
5. Below/under market direction is handled correctly
6. Dashboard can display ensemble context
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch


from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.ensemble_weather import (
    EnsembleForecastSnapshot,
    probability_above,
    probability_below,
)
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import RiskContext
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.services.market_workflow_service import MarketWorkflowService
from polymarket_weather_arb.services.trading_service import TradingService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakePolymarketClient:
    def __init__(self):
        self.orders = []

    def list_markets(self, limit=100):
        return []

    def get_order_book(self, market):
        return {"bids": [{"price": "0.60"}], "asks": [{"price": "0.65"}]}

    def place_limit_order(self, *, token_id, side, price, size):
        self.orders.append({"token_id": token_id, "side": side, "price": price, "size": size})
        return {"status": "ok"}

    def get_balances(self):
        return {}

    def get_positions(self):
        return []


class FakeWeatherProvider:
    def fetch_forecast(self, market_id, rule):
        snapshot = ForecastSnapshot(
            market_id=market_id,
            provider="open-meteo",
            location=rule.location or "New York",
            station=None,
            variable=rule.variable or "temperature_high",
            value=Decimal("80"),
            lower_value=Decimal("76"),
            upper_value=Decimal("84"),
            unit="F",
            issue_time=datetime.now(timezone.utc),
            valid_time=datetime.now(timezone.utc),
            fetched_at=datetime.now(timezone.utc),
        )
        return snapshot, {"source": "test"}


def _setup_repo(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init_schema()
    connection = database.connect()
    repo = Repository(connection)
    return repo, connection


def _seed_market(repo, market_id="test-market"):
    """Seed a test market with snapshot."""
    market = Market(
        id=market_id,
        slug=market_id,
        title="Will the high temperature in New York exceed 80F on June 3?",
        description="NOAA station KNYC",
        yes_token_id="yes-token",
        no_token_id="no-token",
        status="active",
        is_weather=True,
    )
    repo.upsert_market(market, {"id": market_id})

    # Add snapshot with bid/ask
    snapshot = MarketSnapshot(
        market_id=market_id,
        best_bid=Decimal("0.60"),
        best_ask=Decimal("0.65"),
        midpoint=Decimal("0.625"),
        spread=Decimal("0.05"),
        liquidity=Decimal("100"),
        fetched_at=datetime.now(timezone.utc),
    )
    repo.save_market_snapshot(snapshot, {"market_id": market_id})


@patch("polymarket_weather_arb.adapters.weather.open_meteo_ensemble.build_httpx_client")
@patch("polymarket_weather_arb.adapters.weather.open_meteo_ensemble.fetch_awc_station_location")
def test_ensemble_analyze_workflow(mock_station_location, mock_client_class, tmp_path):
    """Test that ensemble analyze workflow can run end-to-end."""
    mock_station_location.return_value = (40.7128, -74.0060, {"icaoId": "KNYC"})

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "hourly": {
            "time": ["2026-06-03T00:00:00Z", "2026-06-03T01:00:00Z"],
            "temperature_2m_control": [20.0, 21.0],
            "temperature_2m_member01": [20.1, 21.1],
            "temperature_2m_member02": [19.9, 20.9],
            "temperature_2m_member03": [20.2, 21.2],
            "temperature_2m_member04": [19.8, 20.8],
            "temperature_2m_member05": [20.3, 21.3],
        }
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    repo, connection = _setup_repo(tmp_path)
    try:
        _seed_market(repo)

        settings = Settings(
            DATABASE_PATH=tmp_path / "test.db",
            WEATHER_PROVIDER="open-meteo-ensemble",
        )

        workflow = MarketWorkflowService(
            settings=settings,
            repository=repo,
            weather_provider_factory=FakeWeatherProvider,
            polymarket_client_factory=lambda s: FakePolymarketClient(),
        )

        # Test analyze
        result = workflow.analyze("test-market")

        # Verify result
        assert result.market_id == "test-market"

        # Verify forecast was saved
        forecast = repo.latest_forecast("test-market")
        assert forecast is not None
        assert forecast["provider"] == "open-meteo-ensemble"

        # Verify analysis was saved
        analysis = repo.latest_analysis("test-market")
        assert analysis is not None
        assert analysis["model_version"] == "ensemble-threshold-v1"

    finally:
        connection.close()


@patch("polymarket_weather_arb.adapters.weather.open_meteo_ensemble.build_httpx_client")
@patch("polymarket_weather_arb.adapters.weather.open_meteo_ensemble.fetch_awc_station_location")
def test_ensemble_source_grade_is_research(mock_station_location, mock_client_class, tmp_path):
    """Test that ensemble forecast has source_grade=research_forecast."""
    mock_station_location.return_value = (40.7128, -74.0060, {"icaoId": "KNYC"})

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "hourly": {
            "time": ["2026-06-03T00:00:00Z"],
            "temperature_2m_control": [20.0],
            "temperature_2m_member01": [20.1],
            "temperature_2m_member02": [19.9],
        }
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    repo, connection = _setup_repo(tmp_path)
    try:
        _seed_market(repo)

        settings = Settings(
            DATABASE_PATH=tmp_path / "test.db",
            WEATHER_PROVIDER="open-meteo-ensemble",
        )

        workflow = MarketWorkflowService(
            settings=settings,
            repository=repo,
            weather_provider_factory=FakeWeatherProvider,
            polymarket_client_factory=lambda s: FakePolymarketClient(),
        )

        # Run analyze
        workflow.analyze("test-market")

        # Verify forecast source_grade
        forecast = repo.latest_forecast("test-market")
        assert forecast is not None

        import json

        raw_payload = (
            json.loads(forecast["raw_payload"])
            if isinstance(forecast["raw_payload"], str)
            else forecast["raw_payload"]
        )
        assert raw_payload.get("source_grade") == "research_forecast"
        assert raw_payload.get("source_grade") != "official_forecast"

    finally:
        connection.close()


@patch("polymarket_weather_arb.adapters.weather.open_meteo_ensemble.build_httpx_client")
@patch("polymarket_weather_arb.adapters.weather.open_meteo_ensemble.fetch_awc_station_location")
def test_ensemble_dashboard_context_has_mean_std_agreement(
    mock_station_location, mock_client_class, tmp_path
):
    """Test that dashboard can display ensemble context with mean/std/agreement."""
    mock_station_location.return_value = (40.7128, -74.0060, {"icaoId": "KNYC"})

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "hourly": {
            "time": ["2026-06-03T00:00:00Z"],
            "temperature_2m_control": [20.0],
            "temperature_2m_member01": [20.1],
            "temperature_2m_member02": [19.9],
        }
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    repo, connection = _setup_repo(tmp_path)
    try:
        _seed_market(repo)

        settings = Settings(
            DATABASE_PATH=tmp_path / "test.db",
            WEATHER_PROVIDER="open-meteo-ensemble",
        )

        workflow = MarketWorkflowService(
            settings=settings,
            repository=repo,
            weather_provider_factory=FakeWeatherProvider,
            polymarket_client_factory=lambda s: FakePolymarketClient(),
        )

        # Run analyze
        workflow.analyze("test-market")

        # Verify forecast has ensemble context
        forecast = repo.latest_forecast("test-market")
        assert forecast is not None

        import json

        raw_payload = (
            json.loads(forecast["raw_payload"])
            if isinstance(forecast["raw_payload"], str)
            else forecast["raw_payload"]
        )

        # CRITICAL: Must have mean, std, agreement, member_count, source_grade, provider, unit
        assert "mean" in raw_payload, "raw_payload must contain mean"
        assert "std" in raw_payload, "raw_payload must contain std"
        assert "agreement" in raw_payload, "raw_payload must contain agreement"
        assert "member_count" in raw_payload, "raw_payload must contain member_count"
        assert "source_grade" in raw_payload, "raw_payload must contain source_grade"
        assert "provider" in raw_payload, "raw_payload must contain provider"
        assert "unit" in raw_payload, "raw_payload must contain unit"

        assert raw_payload["source_grade"] == "research_forecast"
        assert raw_payload["provider"] == "open-meteo-ensemble"
        assert isinstance(raw_payload["mean"], (int, float))
        assert isinstance(raw_payload["std"], (int, float))
        assert isinstance(raw_payload["agreement"], (int, float))

    finally:
        connection.close()


@patch("polymarket_weather_arb.adapters.weather.open_meteo_ensemble.build_httpx_client")
@patch("polymarket_weather_arb.adapters.weather.open_meteo_ensemble.fetch_awc_station_location")
def test_ensemble_dry_run_generates_intent(mock_station_location, mock_client_class, tmp_path):
    """Test that ensemble dry-run can generate order intent."""
    mock_station_location.return_value = (40.7128, -74.0060, {"icaoId": "KNYC"})

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "hourly": {
            "time": ["2026-06-03T00:00:00Z"],
            "temperature_2m_control": [25.0],  # ~77°F
            "temperature_2m_member01": [25.1],
            "temperature_2m_member02": [24.9],
            "temperature_2m_member03": [25.2],
            "temperature_2m_member04": [24.8],
            "temperature_2m_member05": [25.3],
        }
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    repo, connection = _setup_repo(tmp_path)
    try:
        _seed_market(repo)

        settings = Settings(
            DATABASE_PATH=tmp_path / "test.db",
            WEATHER_PROVIDER="open-meteo-ensemble",
        )

        fake_client = FakePolymarketClient()
        workflow = MarketWorkflowService(
            settings=settings,
            repository=repo,
            weather_provider_factory=FakeWeatherProvider,
            polymarket_client_factory=lambda s: fake_client,
        )

        # Run analyze first to create analysis
        workflow.analyze("test-market")

        # Then run dry-run trade
        workflow.dry_run_trade("test-market")

        # Verify dry-run order intent was created
        intents = repo.list_recent_order_intents(limit=10, market_id="test-market")
        dry_run_intents = [i for i in intents if i["dry_run"]]
        assert len(dry_run_intents) > 0, "Should have at least one dry-run order intent"

        # Verify fake client has no live orders
        assert len(fake_client.orders) == 0, "Fake client should have no live orders"

    finally:
        connection.close()


def test_below_market_direction():
    """Test that below/under market direction is handled correctly."""
    members = [Decimal("70"), Decimal("72"), Decimal("74"), Decimal("76"), Decimal("78")]

    # Below 75: 70, 72, 74 (3 members)
    estimate = probability_below(
        threshold=Decimal("75"),
        members=members,
        market_id="test-market",
        mean=Decimal("74"),
        std=Decimal("3"),
    )

    # P(below 75) = 3/5 = 0.6
    assert estimate.probability == Decimal("0.6")
    assert estimate.operator == "below"

    # Above 75: 76, 78 (2 members)
    estimate_above = probability_above(
        threshold=Decimal("75"),
        members=members,
        market_id="test-market",
        mean=Decimal("74"),
        std=Decimal("3"),
    )

    # P(above 75) = 2/5 = 0.4
    assert estimate_above.probability == Decimal("0.4")
    assert estimate_above.operator == "above"


def test_live_trade_rejected_with_research_forecast_source(tmp_path):
    """Test that live trade is rejected when source_grade is research_forecast."""
    repo, connection = _setup_repo(tmp_path)
    try:
        _seed_market(repo)

        # Save a forecast with research_forecast source
        forecast = ForecastSnapshot(
            market_id="test-market",
            provider="open-meteo-ensemble",
            location="New York",
            station=None,
            variable="temperature_high",
            value=Decimal("80"),
            lower_value=Decimal("76"),
            upper_value=Decimal("84"),
            unit="F",
            issue_time=datetime.now(timezone.utc),
            valid_time=datetime.now(timezone.utc),
            fetched_at=datetime.now(timezone.utc),
        )
        repo.save_forecast(forecast, {"source_grade": "research_forecast"})

        # Save analysis
        analysis = Analysis(
            market_id="test-market",
            model_version="ensemble-threshold-v1",
            fair_lower=Decimal("0.65"),
            fair_upper=Decimal("0.75"),
            reference_price=Decimal("0.70"),
            edge=Decimal("0.05"),
            side="buy_yes",
            decision="trade",
            reasons=["test"],
        )
        repo.save_analysis(analysis)

        # Create trading service
        fake_client = FakePolymarketClient()
        settings = Settings(DATABASE_PATH=tmp_path / "test.db")
        trading_service = TradingService(settings, fake_client, repo)

        # Create risk context
        risk_context = RiskContext(
            daily_live_notional=Decimal("0"),
            market_live_exposure=Decimal("0"),
            order_book_age_seconds=1,
            forecast_age_seconds=1,
            rule_tradable=True,
            reconciliation_fresh=True,
        )

        # Try live trade with research_forecast source
        intent_id, reasons = trading_service.trade(
            analysis=analysis,
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=risk_context,
            dry_run=False,
            source_grade="research_forecast",
        )

        # Verify trade was rejected
        assert intent_id is not None
        assert any("official_forecast" in r or "source" in r.lower() for r in reasons), (
            f"Should reject due to source grade, got reasons: {reasons}"
        )

        # Verify no live orders
        assert len(fake_client.orders) == 0, "Should have no live orders"

        # Verify order intent status is rejected
        intents = repo.list_recent_order_intents(limit=10, market_id="test-market")
        rejected_intents = [i for i in intents if i["status"] == "rejected"]
        assert len(rejected_intents) > 0, "Should have rejected order intent"

    finally:
        connection.close()


def test_from_members_defaults_issue_valid_time_to_fetched_at():
    """Test that from_members defaults issue_time/valid_time to fetched_at."""
    members = [Decimal("70"), Decimal("72"), Decimal("74")]
    fetched_at = datetime.now(timezone.utc)

    snapshot = EnsembleForecastSnapshot.from_members(
        market_id="test-market",
        location="New York",
        variable="temperature_high",
        members=members,
        fetched_at=fetched_at,
        raw_payload={"test": "data"},
    )

    # issue_time and valid_time should default to fetched_at
    assert snapshot.issue_time == fetched_at
    assert snapshot.valid_time == fetched_at
    assert snapshot.provider == "open-meteo-ensemble"
    assert snapshot.unit == "F"


def test_from_members_can_be_saved_to_repo(tmp_path):
    """Test that from_members result can be saved to repository."""
    repo, connection = _setup_repo(tmp_path)
    try:
        _seed_market(repo)

        members = [Decimal("70"), Decimal("72"), Decimal("74")]
        fetched_at = datetime.now(timezone.utc)

        snapshot = EnsembleForecastSnapshot.from_members(
            market_id="test-market",
            location="New York",
            variable="temperature_high",
            members=members,
            fetched_at=fetched_at,
            raw_payload={"test": "data"},
            unit="F",
        )

        # Save to repository - should not raise
        raw_payload = {
            "source_grade": "research_forecast",
            "provider": "open-meteo-ensemble",
            "member_count": 3,
            "mean": float(snapshot.mean),
            "std": float(snapshot.std),
            "agreement": 0.67,
            "unit": "F",
        }
        repo.save_forecast(snapshot, raw_payload)

        # Verify saved
        forecast = repo.latest_forecast("test-market")
        assert forecast is not None
        assert forecast["provider"] == "open-meteo-ensemble"

    finally:
        connection.close()
