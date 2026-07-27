from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.pricing import analyze_price
from polymarket_weather_arb.domain.probability import (
    ProbabilityInterval,
    estimate_probability_interval,
)
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot


def _rule():
    return ResolutionRule(
        raw_text="test",
        location="New York",
        station="KNYC",
        source="NOAA",
        variable="temperature_high",
        operator=">=",
        threshold=Decimal("80"),
        unit="F",
        window_start=None,
        window_end=None,
        confidence=0.95,
        tradable=True,
        rejection_reason=None,
    )


def test_probability_interval_for_threshold_event():
    forecast = ForecastSnapshot(
        market_id="m1",
        provider="test",
        location="New York",
        station="KNYC",
        variable="temperature_high",
        value=Decimal("84"),
        lower_value=Decimal("82"),
        upper_value=Decimal("86"),
        unit="F",
        issue_time=datetime.now(timezone.utc),
        valid_time=datetime.now(timezone.utc),
        fetched_at=datetime.now(timezone.utc),
    )

    interval = estimate_probability_interval(_rule(), forecast)

    assert interval.lower > Decimal("0.5")
    assert interval.upper == Decimal("1")


def test_price_analysis_requires_conservative_edge():
    interval = ProbabilityInterval(Decimal("0.70"), Decimal("0.80"), ["test"])

    analysis = analyze_price(
        market_id="m1",
        interval=interval,
        best_bid=Decimal("0.50"),
        best_ask=Decimal("0.60"),
        min_edge=Decimal("0.05"),
        slippage_buffer=Decimal("0.02"),
    )

    assert analysis.decision == "trade"
    assert analysis.side == "buy_yes"
    assert analysis.edge == Decimal("0.08")


def test_price_analysis_watches_without_edge():
    interval = ProbabilityInterval(Decimal("0.52"), Decimal("0.58"), ["test"])

    analysis = analyze_price(
        market_id="m1",
        interval=interval,
        best_bid=Decimal("0.50"),
        best_ask=Decimal("0.54"),
        min_edge=Decimal("0.05"),
        slippage_buffer=Decimal("0.02"),
    )

    assert analysis.decision == "watch"
    assert analysis.side is None
