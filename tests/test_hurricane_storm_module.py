from polymarket_weather_arb.domain.hurricane_storm import classify_hurricane_storm_market


def test_classify_hurricane_storm_market_is_research_only():
    result = classify_hurricane_storm_market(
        "Will Hurricane Alice make landfall in Florida by June 10, 2026?",
        "Resolved according to NOAA/NHC advisories.",
    )

    assert result.research_only is True
    assert result.tradable is False
    assert result.source == "NHC"
    assert "storm pricing requires dedicated NHC adapter" in result.reasons


def test_classify_hurricane_storm_ignores_non_storm_weather():
    result = classify_hurricane_storm_market(
        "Will the high temperature in New York exceed 80F?",
        "Resolved according to NOAA station KNYC.",
    )

    assert result.research_only is False
    assert result.source is None
