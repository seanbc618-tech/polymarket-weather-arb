from decimal import Decimal

import pytest

from polymarket_weather_arb.domain.rules import parse_resolution_rule

NOAA_STATION_DESCRIPTION = "Resolution source: NOAA station KNYC."


def test_parse_clear_temperature_rule():
    rule = parse_resolution_rule(
        "Will the high temperature in New York exceed 80°F on May 8, 2026?",
        NOAA_STATION_DESCRIPTION,
    )

    assert rule.tradable is True
    assert rule.location == "New York"
    assert rule.source == "NOAA"
    assert rule.station == "KNYC"
    assert rule.variable == "temperature_high"
    assert rule.operator == ">="
    assert rule.threshold == Decimal("80")
    assert rule.unit == "F"
    assert rule.window_start == "2026-05-08"


def test_parse_rejects_unclear_source():
    rule = parse_resolution_rule("Will it rain in Boston tomorrow?")

    assert rule.tradable is False
    assert "unclear settlement source" in rule.rejection_reason


def test_parse_precipitation_rule():
    rule = parse_resolution_rule(
        "Will rainfall in Miami exceed 0.5 inches on May 8, 2026?",
        "Resolved according to NWS observations.",
    )

    assert rule.variable == "precipitation"
    assert rule.threshold == Decimal("0.5")
    assert rule.unit == "in"
    assert rule.source == "NWS"


@pytest.mark.parametrize(
    "phrase",
    [
        "below",
        "under",
        "less than",
    ],
)
def test_temperature_below_operator_phrases_fold_to_lte(phrase):
    rule = parse_resolution_rule(
        f"Will the high temperature be {phrase} 80F on May 8, 2026?",
        NOAA_STATION_DESCRIPTION,
    )

    assert rule.operator == "<="


@pytest.mark.parametrize(
    "phrase",
    [
        "at least",
        "above",
        "over",
        "exceed",
        "greater than",
    ],
)
def test_temperature_above_operator_phrases_fold_to_gte(phrase):
    rule = parse_resolution_rule(
        f"Will the high temperature be {phrase} 80F on May 8, 2026?",
        NOAA_STATION_DESCRIPTION,
    )

    assert rule.operator == ">="


@pytest.mark.parametrize("operator_text, expected", [(">", ">="), ("<", "<=")])
def test_strict_temperature_operators_currently_fold_to_inclusive(operator_text, expected):
    rule = parse_resolution_rule(
        f"Will the high temperature be {operator_text} 80F on May 8, 2026?",
        NOAA_STATION_DESCRIPTION,
    )

    assert rule.operator == expected


@pytest.mark.parametrize(
    "unit_text",
    [
        "80C",
        "80°C",
        "80 degrees Celsius",
    ],
)
def test_temperature_celsius_unit_variants_parse_as_c(unit_text):
    rule = parse_resolution_rule(
        f"Will the high temperature exceed {unit_text} on May 8, 2026?",
        NOAA_STATION_DESCRIPTION,
    )

    assert rule.unit == "C"


@pytest.mark.parametrize(
    "title",
    [
        "Will the low temperature be below 40F on May 8, 2026?",
        "Will the minimum temperature be below 40F on May 8, 2026?",
    ],
)
def test_low_temperature_phrases_parse_as_temperature_low(title):
    rule = parse_resolution_rule(title, NOAA_STATION_DESCRIPTION)

    assert rule.variable == "temperature_low"
    assert rule.operator == "<="


def test_parse_below_80f_high_temperature_is_tradable_lte_rule():
    rule = parse_resolution_rule(
        "Will the high temperature be below 80F on May 8, 2026?",
        NOAA_STATION_DESCRIPTION,
    )

    assert rule.tradable is True
    assert rule.variable == "temperature_high"
    assert rule.operator == "<="
    assert rule.threshold == Decimal("80")
    assert rule.unit == "F"


def test_parse_rejects_temperature_bucket_range_for_plain_threshold_workflow():
    rule = parse_resolution_rule(
        "Will the highest temperature in Miami be between 84-85°F on July 9?",
        (
            "This market will resolve to the temperature range that contains the highest "
            "temperature recorded at the Miami Intl Airport Station. The resolution source "
            "will be Wunderground."
        ),
    )

    assert rule.tradable is False
    assert "temperature bucket/range markets require bucket workflow" in (
        rule.rejection_reason or ""
    )


@pytest.mark.parametrize(
    "title",
    [
        "Will any Category 4 hurricane make landfall in the US in before 2027?",
        "Will 2026 be the hottest year on record?",
        "Will the minimum Arctic sea ice extent this summer be less than 4m square kilometers?",
        "Named storm forms before hurricane season?",
    ],
)
def test_real_broad_weather_markets_are_rejected_for_mvp(title):
    rule = parse_resolution_rule(
        title, "This is a real broad weather/climate-style Polymarket sample."
    )

    assert rule.tradable is False
    assert rule.rejection_reason
