from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.global_bucket_pricing import analyze_global_bucket_price
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
    settlement_bucket_bounds,
    settlement_bucket_contains,
    with_settlement_timezone,
)
from polymarket_weather_arb.domain.weather import ForecastSnapshot


def test_parse_global_temperature_bucket_fahrenheit_range():
    rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 80-81F on June 10, 2026?",
        "Settlement source: NOAA station KNYC.",
    )

    assert rule.tradable is True
    assert rule.location == "New York"
    assert rule.station == "KNYC"
    assert rule.source == "NOAA"
    assert rule.variable == "temperature_high"
    assert rule.bucket_lower == Decimal("80")
    assert rule.bucket_upper == Decimal("81")
    assert rule.bucket_center == Decimal("80.5")
    assert rule.bucket_kind == "range"
    assert settlement_bucket_bounds(rule) == (Decimal("79.5"), Decimal("81.5"))
    assert rule.unit == "F"
    assert rule.target_date == "2026-06-10"
    assert rule.settlement_timezone == "America/New_York"


def test_global_bucket_uses_city_local_settlement_timezone():
    shanghai = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Shanghai be 31C on July 21, 2026?",
        "Settlement source: Wunderground.",
    )
    seoul = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Seoul be 26C on July 21, 2026?",
        "Settlement source: Wunderground.",
    )

    assert shanghai.tradable is True
    assert shanghai.settlement_timezone == "Asia/Shanghai"
    assert seoul.tradable is True
    assert seoul.settlement_timezone == "Asia/Seoul"


def test_global_bucket_rejects_unknown_settlement_timezone_instead_of_using_utc():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Unknownville be 24C on July 21, 2026?",
        "Settlement source: Wunderground.",
    )

    assert rule.tradable is False
    assert rule.settlement_timezone == ""
    assert "unclear settlement timezone" in (rule.rejection_reason or "")


def test_verified_dynamic_timezone_only_clears_timezone_rejection():
    cape_town = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Cape Town be 21C on July 20, 2026?",
        (
            "Settlement source: Wunderground. "
            "https://www.wunderground.com/history/daily/za/cape-town/FACT"
        ),
    )

    qualified = with_settlement_timezone(cape_town, "Africa/Johannesburg")

    assert qualified.station == "FACT"
    assert qualified.settlement_timezone == "Africa/Johannesburg"
    assert qualified.tradable is True
    assert qualified.rejection_reason is None

    ambiguous = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Unknownville be 21C on July 20, 2026?",
        "No settlement source is specified.",
    )
    still_rejected = with_settlement_timezone(ambiguous, "Africa/Johannesburg")
    assert still_rejected.tradable is False
    assert "unclear settlement source" in (still_rejected.rejection_reason or "")


def test_parse_global_temperature_bucket_tails_are_unbounded_and_half_open():
    description = "Settlement source: Wunderground station KNYC."
    lower_tail = parse_global_temperature_bucket_rule(
        "Will the highest temperature in New York be 79°F or below on June 10, 2026?",
        description,
    )
    upper_tail = parse_global_temperature_bucket_rule(
        "Will the highest temperature in New York be 108°F or higher on June 10, 2026?",
        description,
    )

    assert lower_tail.bucket_kind == "lower_tail"
    assert settlement_bucket_bounds(lower_tail) == (None, Decimal("79.5"))
    assert settlement_bucket_contains(lower_tail, Decimal("-100")) is True
    assert settlement_bucket_contains(lower_tail, Decimal("79.5")) is False
    assert upper_tail.bucket_kind == "upper_tail"
    assert settlement_bucket_bounds(upper_tail) == (Decimal("107.5"), None)
    assert settlement_bucket_contains(upper_tail, Decimal("107.5")) is True
    assert settlement_bucket_contains(upper_tail, Decimal("200")) is True


def test_parse_global_temperature_bucket_celsius_exact():
    rule = parse_global_temperature_bucket_rule(
        "Will London max temperature be 24C on 2026-06-10?",
        "Resolved by Open-Meteo forecast verification.",
    )

    assert rule.tradable is True
    assert rule.location == "London"
    assert rule.source == "Open-Meteo"
    assert rule.variable == "temperature_high"
    assert rule.bucket_lower == Decimal("23.5")
    assert rule.bucket_upper == Decimal("24.5")
    assert rule.bucket_center == Decimal("24")
    assert rule.unit == "C"
    assert rule.target_date == "2026-06-10"


def test_parse_global_temperature_bucket_polymarket_wunderground_description():
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Miami be between 84-85°F on July 9?",
        (
            "This market will resolve to the temperature range that contains the highest "
            "temperature recorded at the Miami Intl Airport Station in degrees Fahrenheit "
            "on 9 Jul '26. The resolution source for this market will be information from "
            "Wunderground, available here: "
            "https://www.wunderground.com/history/daily/us/fl/miami/KMIA."
        ),
    )

    assert rule.tradable is True
    assert rule.location == "Miami"
    assert rule.station == "KMIA"
    assert rule.source == "Wunderground"
    assert rule.bucket_lower == Decimal("84")
    assert rule.bucket_upper == Decimal("85")
    assert rule.target_date == "2026-07-09"


def test_global_bucket_parser_does_not_treat_the_station_as_icao_code():
    rule = parse_global_temperature_bucket_rule(
        "Highest temperature in Miami on July 20, 2026 84-85F",
        "This market resolves to the temperature range recorded at the station. "
        "Resolution source Wunderground.",
    )

    assert rule.location == "Miami"
    assert rule.station is None
    assert rule.tradable is True


def test_parse_global_temperature_bucket_rejects_unclear_bucket():
    rule = parse_global_temperature_bucket_rule(
        "Will the temperature in New York be hot on June 10, 2026?",
        "Settlement source: NOAA station KNYC.",
    )

    assert rule.tradable is False
    assert "unclear 1 degree temperature bucket" in (rule.rejection_reason or "")


def test_global_bucket_price_requires_multimodel_consensus_before_trade():
    rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 80-81F on June 10, 2026?",
        "Settlement source: NOAA station KNYC.",
    )
    now = datetime(2026, 6, 9, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="NOAA",
        variable="temperature_high",
        value=Decimal("80.5"),
        unit="F",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="m1",
        location="New York",
        station="KNYC",
    )

    analysis = analyze_global_bucket_price("m1", rule, forecast, Decimal("0.08"), now=now)

    assert analysis.decision == "watch"
    assert analysis.side is None
    assert analysis.model_version == "global-temp-bucket-normal-v1"
    assert analysis.reference_price == Decimal("0.08")
    assert any(
        "requires at least 3 independent source families" in reason for reason in analysis.reasons
    )


def test_global_bucket_price_rejects_mismatched_variable():
    rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 80-81F on June 10, 2026?",
        "Settlement source: NOAA station KNYC.",
    )
    now = datetime(2026, 6, 9, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="NOAA",
        variable="precipitation",
        value=Decimal("0.5"),
        unit="in",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="m1",
        location="New York",
        station="KNYC",
    )

    analysis = analyze_global_bucket_price("m1", rule, forecast, Decimal("0.08"), now=now)

    assert analysis.decision == "reject"
    assert "forecast variable does not match bucket rule" in analysis.reasons


def test_d0_observed_max_above_bucket_makes_probability_zero():
    rule = parse_global_temperature_bucket_rule(
        "Will the high temperature in New York be 80-81F on June 10, 2026?",
        "Settlement source: NOAA station KNYC.",
    )
    now = datetime(2026, 6, 10, 14, tzinfo=timezone.utc)
    forecast = ForecastSnapshot(
        provider="NOAA",
        variable="temperature_high",
        value=Decimal("80.5"),
        unit="F",
        issue_time=now,
        valid_time=now,
        fetched_at=now,
        market_id="m1",
        location="New York",
        station="KNYC",
    )

    analysis = analyze_global_bucket_price(
        "m1",
        rule,
        forecast,
        Decimal("0.001"),
        now=now,
        observed_max=Decimal("83"),
        observed_max_unit="F",
    )

    assert analysis.model_version == "global-temp-bucket-observed-v1"
    assert analysis.decision == "reject"
    assert analysis.fair_upper == 0
    assert any("already exceeds bucket upper bound" in reason for reason in analysis.reasons)
