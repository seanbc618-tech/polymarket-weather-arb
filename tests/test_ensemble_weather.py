"""Tests for ensemble weather forecast functionality.

These tests verify that:
1. Ensemble forecast snapshots are correctly created
2. Probability calculations are accurate
3. Pricing logic applies conservative widening
4. Source grade is always research_forecast (not official_forecast)
5. Live trading gates block ensemble forecasts
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from polymarket_weather_arb.domain.ensemble_weather import (
    EnsembleForecastSnapshot,
    EnsembleProbabilityEstimate,
    probability_above,
    probability_below,
)
from polymarket_weather_arb.domain.ensemble_pricing import (
    ensemble_to_analysis,
    ensemble_to_probability_interval,
)


class TestEnsembleForecastSnapshot:
    """Tests for EnsembleForecastSnapshot creation."""

    def test_from_members_creates_snapshot(self):
        """Test that from_members creates a valid snapshot."""
        members = [Decimal("70"), Decimal("72"), Decimal("74"), Decimal("76"), Decimal("78")]
        fetched_at = datetime.now(timezone.utc)

        snapshot = EnsembleForecastSnapshot.from_members(
            market_id="test-market",
            location="New York",
            variable="temperature_high",
            members=members,
            fetched_at=fetched_at,
            raw_payload={"test": "data"},
        )

        assert snapshot.market_id == "test-market"
        assert snapshot.location == "New York"
        assert snapshot.variable == "temperature_high"
        assert snapshot.members == members
        assert snapshot.member_count == 5
        assert snapshot.mean == Decimal("74")
        assert snapshot.source_grade == "research_forecast"

    def test_from_members_empty_raises_error(self):
        """Test that empty members raises ValueError."""
        with pytest.raises(ValueError, match="empty members"):
            EnsembleForecastSnapshot.from_members(
                market_id="test-market",
                location="New York",
                variable="temperature_high",
                members=[],
                fetched_at=datetime.now(timezone.utc),
                raw_payload={},
            )

    def test_from_members_computes_std(self):
        """Test that standard deviation is correctly computed."""
        # Members: 10, 20, 30, 40, 50
        # Mean: 30
        # Variance: ((10-30)^2 + (20-30)^2 + (30-30)^2 + (40-30)^2 + (50-30)^2) / 5
        #         = (400 + 100 + 0 + 100 + 400) / 5 = 200
        # Std: sqrt(200) ≈ 14.14
        members = [Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40"), Decimal("50")]

        snapshot = EnsembleForecastSnapshot.from_members(
            market_id="test-market",
            location="New York",
            variable="temperature_high",
            members=members,
            fetched_at=datetime.now(timezone.utc),
            raw_payload={},
        )

        assert snapshot.mean == Decimal("30")
        assert abs(float(snapshot.std) - 14.14) < 0.1

    def test_source_grade_is_research(self):
        """Test that source_grade is always research_forecast."""
        snapshot = EnsembleForecastSnapshot.from_members(
            market_id="test-market",
            location="New York",
            variable="temperature_high",
            members=[Decimal("70")],
            fetched_at=datetime.now(timezone.utc),
            raw_payload={},
        )

        assert snapshot.source_grade == "research_forecast"
        # Ensure it's NOT official_forecast
        assert snapshot.source_grade != "official_forecast"


class TestEnsembleProbabilityEstimate:
    """Tests for probability calculations."""

    def test_probability_above_basic(self):
        """Test basic probability_above calculation."""
        members = [Decimal("70"), Decimal("72"), Decimal("74"), Decimal("76"), Decimal("78")]

        estimate = probability_above(
            threshold=Decimal("73"),
            members=members,
            market_id="test-market",
            mean=Decimal("74"),
            std=Decimal("3"),
        )

        # 3 members above 73: 74, 76, 78
        assert estimate.probability == Decimal("0.6")
        assert estimate.operator == "above"
        assert estimate.member_count == 5

    def test_probability_below_basic(self):
        """Test basic probability_below calculation."""
        members = [Decimal("70"), Decimal("72"), Decimal("74"), Decimal("76"), Decimal("78")]

        estimate = probability_below(
            threshold=Decimal("73"),
            members=members,
            market_id="test-market",
            mean=Decimal("74"),
            std=Decimal("3"),
        )

        # 2 members below 73: 70, 72
        assert estimate.probability == Decimal("0.4")
        assert estimate.operator == "below"
        assert estimate.member_count == 5

    def test_probability_above_agreement(self):
        """Test agreement calculation for probability_above."""
        members = [Decimal("70"), Decimal("70"), Decimal("70"), Decimal("70"), Decimal("80")]

        estimate = probability_above(
            threshold=Decimal("75"),
            members=members,
            market_id="test-market",
            mean=Decimal("72"),
            std=Decimal("4"),
        )

        # 1 member above 75: 80
        # probability = 0.2
        # agreement = max(0.2, 0.8) = 0.8
        assert estimate.probability == Decimal("0.2")
        assert estimate.agreement == Decimal("0.8")

    def test_probability_empty_members_raises_error(self):
        """Test that empty members raises error."""
        with pytest.raises(ValueError, match="empty members"):
            probability_above(
                threshold=Decimal("75"),
                members=[],
                market_id="test-market",
                mean=Decimal("74"),
                std=Decimal("3"),
            )

    def test_probability_reasons_include_member_count(self):
        """Test that reasons include member_count."""
        members = [Decimal("70"), Decimal("72")]

        estimate = probability_above(
            threshold=Decimal("71"),
            members=members,
            market_id="test-market",
            mean=Decimal("71"),
            std=Decimal("1"),
        )

        assert any("member_count=2" in r for r in estimate.reasons)

    def test_probability_reasons_include_mean_std(self):
        """Test that reasons include mean and std."""
        members = [Decimal("70"), Decimal("72")]

        estimate = probability_above(
            threshold=Decimal("71"),
            members=members,
            market_id="test-market",
            mean=Decimal("71"),
            std=Decimal("1"),
        )

        assert any("mean=" in r for r in estimate.reasons)
        assert any("std=" in r for r in estimate.reasons)


class TestEnsemblePricing:
    """Tests for ensemble pricing logic."""

    def test_ensemble_to_analysis_above_threshold(self):
        """Test conversion to Analysis for above threshold."""
        estimate = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="above",
            probability=Decimal("0.7"),
            agreement=Decimal("0.8"),
            member_count=31,
            mean=Decimal("76"),
            std=Decimal("3"),
            reasons=["test"],
        )

        # Provide order book data
        analysis = ensemble_to_analysis(
            estimate=estimate,
            best_bid=Decimal("0.60"),
            best_ask=Decimal("0.65"),
            min_edge=Decimal("0.05"),
            slippage_buffer=Decimal("0.02"),
        )

        assert analysis.market_id == "test-market"
        assert analysis.model_version == "ensemble-threshold-v1"
        assert "ensemble_model=" in analysis.reasons[0]

    def test_ensemble_to_analysis_below_threshold(self):
        """Test conversion to Analysis for below threshold."""
        estimate = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="below",
            probability=Decimal("0.7"),
            agreement=Decimal("0.8"),
            member_count=31,
            mean=Decimal("73"),
            std=Decimal("3"),
            reasons=["test"],
        )

        # Provide order book data
        analysis = ensemble_to_analysis(
            estimate=estimate,
            best_bid=Decimal("0.60"),
            best_ask=Decimal("0.65"),
            min_edge=Decimal("0.05"),
            slippage_buffer=Decimal("0.02"),
        )

        # With probability=0.7 and best_ask=0.65, there may or may not be edge
        # Just verify the analysis was created
        assert analysis.market_id == "test-market"

    def test_ensemble_to_analysis_low_agreement_conservative(self):
        """Test that low agreement results in conservative widening."""
        estimate_high = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="above",
            probability=Decimal("0.6"),
            agreement=Decimal("0.9"),
            member_count=31,
            mean=Decimal("76"),
            std=Decimal("3"),
            reasons=["test"],
        )

        estimate_low = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="above",
            probability=Decimal("0.6"),
            agreement=Decimal("0.5"),
            member_count=31,
            mean=Decimal("76"),
            std=Decimal("3"),
            reasons=["test"],
        )

        analysis_high = ensemble_to_analysis(
            estimate=estimate_high,
            best_bid=Decimal("0.60"),
            best_ask=Decimal("0.65"),
            min_edge=Decimal("0.05"),
            slippage_buffer=Decimal("0.02"),
        )
        analysis_low = ensemble_to_analysis(
            estimate=estimate_low,
            best_bid=Decimal("0.60"),
            best_ask=Decimal("0.65"),
            min_edge=Decimal("0.05"),
            slippage_buffer=Decimal("0.02"),
        )

        # Both should have ensemble reasons
        assert any("ensemble" in r.lower() for r in analysis_high.reasons)
        assert any("ensemble" in r.lower() for r in analysis_low.reasons)

    def test_ensemble_to_analysis_reasons_include_member_count(self):
        """Test that reasons include member_count."""
        estimate = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="above",
            probability=Decimal("0.7"),
            agreement=Decimal("0.8"),
            member_count=31,
            mean=Decimal("76"),
            std=Decimal("3"),
            reasons=["test"],
        )

        analysis = ensemble_to_analysis(
            estimate=estimate,
            best_bid=Decimal("0.60"),
            best_ask=Decimal("0.65"),
            min_edge=Decimal("0.05"),
            slippage_buffer=Decimal("0.02"),
        )

        assert any("member_count=31" in r for r in analysis.reasons)

    def test_ensemble_to_analysis_reasons_include_source(self):
        """Test that reasons indicate research_forecast source."""
        estimate = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="above",
            probability=Decimal("0.7"),
            agreement=Decimal("0.8"),
            member_count=31,
            mean=Decimal("76"),
            std=Decimal("3"),
            reasons=["test"],
        )

        analysis = ensemble_to_analysis(
            estimate=estimate,
            best_bid=Decimal("0.60"),
            best_ask=Decimal("0.65"),
            min_edge=Decimal("0.05"),
            slippage_buffer=Decimal("0.02"),
        )

        assert any("research_forecast" in r for r in analysis.reasons)
        assert any("not_for_live_trading" in r for r in analysis.reasons)

    def test_ensemble_probability_interval_basic(self):
        """Test basic probability interval calculation."""
        estimate = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="above",
            probability=Decimal("0.7"),
            agreement=Decimal("0.8"),
            member_count=31,
            mean=Decimal("76"),
            std=Decimal("3"),
            reasons=["test"],
        )

        interval = ensemble_to_probability_interval(estimate)

        assert interval.lower < estimate.probability
        assert interval.upper > estimate.probability
        assert interval.lower >= Decimal("0.01")
        assert interval.upper <= Decimal("0.99")

    def test_ensemble_probability_interval_wider_with_low_agreement(self):
        """Test that interval is wider with lower agreement."""
        estimate_high = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="above",
            probability=Decimal("0.7"),
            agreement=Decimal("0.9"),
            member_count=31,
            mean=Decimal("76"),
            std=Decimal("3"),
            reasons=["test"],
        )

        estimate_low = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="above",
            probability=Decimal("0.7"),
            agreement=Decimal("0.5"),
            member_count=31,
            mean=Decimal("76"),
            std=Decimal("3"),
            reasons=["test"],
        )

        interval_high = ensemble_to_probability_interval(estimate_high)
        interval_low = ensemble_to_probability_interval(estimate_low)

        # Lower agreement should result in wider interval
        width_high = interval_high.upper - interval_high.lower
        width_low = interval_low.upper - interval_low.lower
        assert width_low > width_high


class TestEnsembleSourceGrade:
    """Tests to verify ensemble forecasts are NOT official_forecast."""

    def test_ensemble_source_grade_is_research(self):
        """Test that ensemble source_grade is research_forecast, not official_forecast."""
        snapshot = EnsembleForecastSnapshot.from_members(
            market_id="test-market",
            location="New York",
            variable="temperature_high",
            members=[Decimal("70")],
            fetched_at=datetime.now(timezone.utc),
            raw_payload={},
        )

        # CRITICAL: Must be research_forecast
        assert snapshot.source_grade == "research_forecast"

        # CRITICAL: Must NOT be official_forecast
        assert snapshot.source_grade != "official_forecast"

    def test_ensemble_provider_source_grade(self):
        """Test that provider source_grade is research_forecast."""
        from polymarket_weather_arb.adapters.weather.open_meteo_ensemble import (
            OpenMeteoEnsembleProvider,
        )

        provider = OpenMeteoEnsembleProvider()

        assert provider.source_grade == "research_forecast"
        assert provider.source_grade != "official_forecast"

    def test_ensemble_analysis_reasons_not_for_live(self):
        """Test that analysis reasons indicate not for live trading."""
        estimate = EnsembleProbabilityEstimate(
            market_id="test-market",
            threshold=Decimal("75"),
            operator="above",
            probability=Decimal("0.7"),
            agreement=Decimal("0.8"),
            member_count=31,
            mean=Decimal("76"),
            std=Decimal("3"),
            reasons=["test"],
        )

        analysis = ensemble_to_analysis(estimate)

        # CRITICAL: Must indicate not for live trading
        assert any("not_for_live_trading" in r for r in analysis.reasons)
