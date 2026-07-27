from datetime import datetime, timezone
from decimal import Decimal

import pytest

from polymarket_weather_arb.adapters.weather.china_official import (
    ChinaCityWeatherSource,
    ChinaOfficialWeatherProvider,
)
from polymarket_weather_arb.domain.china_bucket_pricing import (
    ChinaBucketPricingConfig,
    analyze_china_bucket_price,
    estimate_china_bucket_probability_interval,
)
from polymarket_weather_arb.domain.china_temperature_bucket import (
    parse_china_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.weather import ForecastSnapshot


def test_china_official_weather_provider_extracts_daily_high_signal():
    rule = parse_china_temperature_bucket_rule(
        "Highest temperature in Shanghai on May 10?",
        "Outcome: 18°C. Resolved according to Wunderground on 10 May '26. Slug: highest-temperature-in-shanghai-on-may-10-2026",
    )
    payload = {
        "issue_time": "2026-05-09T08:00:00+08:00",
        "daily": [
            {"date": "2026-05-09", "temperature_high_c": 22},
            {
                "date": "2026-05-10",
                "temperature_high_c": "18.2",
                "temperature_high_lower_c": "17.8",
                "temperature_high_upper_c": "18.6",
                "valid_time": "2026-05-10T23:59:00+08:00",
            },
        ],
    }
    provider = ChinaOfficialWeatherProvider(
        fetch_json=lambda url: payload,
        sources={
            "Shanghai": ChinaCityWeatherSource(
                "Shanghai", "ZSPD", "https://official.test/shanghai.json"
            )
        },
    )

    snapshot, raw = provider.fetch_forecast("m1", rule)

    assert snapshot.provider == "china-configured-weather-signal"
    assert snapshot.location == "Shanghai"
    assert snapshot.station == "ZSPD"
    assert snapshot.variable == "temperature_high"
    assert snapshot.value == Decimal("18.2")
    assert snapshot.lower_value == Decimal("17.8")
    assert snapshot.upper_value == Decimal("18.6")
    assert snapshot.unit == "C"
    assert snapshot.issue_time == datetime(2026, 5, 9, 0, 0, tzinfo=timezone.utc)
    assert raw["configured_signal"] is True
    assert raw["official_signal"] is False
    assert raw["station_id"] == "ZSPD"
    assert raw["selected_forecast"]["date"] == "2026-05-10"


def test_china_official_weather_provider_rejects_missing_target_date():
    rule = parse_china_temperature_bucket_rule(
        "Highest temperature in Wuhan on May 10?",
        "Outcome: 23°C. Resolved according to Wunderground on 10 May '26. Slug: highest-temperature-in-wuhan-on-may-10-2026",
    )
    provider = ChinaOfficialWeatherProvider(
        fetch_json=lambda url: {"daily": []},
        sources={
            "Wuhan": ChinaCityWeatherSource("Wuhan", "ZHHH", "https://official.test/wuhan.json")
        },
    )

    with pytest.raises(ValueError, match="no forecast"):
        provider.fetch_forecast("m1", rule)


def test_china_weather_provider_uses_open_meteo_fallback_when_no_configured_url():
    rule = parse_china_temperature_bucket_rule(
        "Highest temperature in Wuhan on May 10?",
        "Outcome: 23°C. Resolved according to Wunderground on 10 May '26. Slug: highest-temperature-in-wuhan-on-may-10-2026",
    )
    payload = {"daily": {"time": ["2026-05-10"], "temperature_2m_max": ["23.4"]}}
    requested_urls = []
    provider = ChinaOfficialWeatherProvider(
        fetch_json=lambda url: requested_urls.append(url) or payload,
    )

    snapshot, raw = provider.fetch_forecast("m1", rule)

    assert snapshot.provider == "open-meteo-china-signal"
    assert snapshot.location == "Wuhan"
    assert snapshot.station == "ZHHH"
    assert snapshot.value == Decimal("23.4")
    assert snapshot.lower_value == Decimal("22.2")
    assert snapshot.upper_value == Decimal("24.6")
    assert raw["official_signal"] is False
    assert raw["source_type"] == "open_meteo_forecast"
    assert "timezone=Asia%2FShanghai" in requested_urls[0]
    assert "start_date=2026-05-10" in requested_urls[0]
    assert "forecast_days" not in requested_urls[0]
    assert "past_days" not in requested_urls[0]


def test_china_official_weather_provider_requires_configured_source_url():
    rule = parse_china_temperature_bucket_rule(
        "Highest temperature in Wuhan on May 10?",
        "Outcome: 23°C. Resolved according to Wunderground on 10 May '26. Slug: highest-temperature-in-wuhan-on-may-10-2026",
    )
    provider = ChinaOfficialWeatherProvider(
        fetch_json=lambda url: {"daily": []}, use_open_meteo_fallback=False
    )

    with pytest.raises(ValueError, match="source URL is not configured"):
        provider.fetch_forecast("m1", rule)


def test_china_bucket_probability_prefers_near_forecast_bucket():
    rule = parse_china_temperature_bucket_rule(
        "Highest temperature in Shanghai on May 10?",
        "Outcome: 18°C. Resolved according to Wunderground on 10 May '26. Slug: highest-temperature-in-shanghai-on-may-10-2026",
    )
    forecast = _forecast(value="18.1", lower="17.9", upper="18.3")

    interval = estimate_china_bucket_probability_interval(
        rule, forecast, now=datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
    )

    assert interval.model_version == "china-temp-bucket-normal-v1"
    assert interval.lower > Decimal("0.35")
    assert interval.upper > interval.lower
    assert "China city temperature signal" in interval.reasons[0]


def test_china_bucket_price_trades_low_ask_yes_only():
    rule = parse_china_temperature_bucket_rule(
        "Highest temperature in Shanghai on May 10?",
        "Outcome: 18°C. Resolved according to Wunderground on 10 May '26. Slug: highest-temperature-in-shanghai-on-may-10-2026",
    )
    forecast = _forecast(value="18.1", lower="18.0", upper="18.2")

    analysis = analyze_china_bucket_price(
        "m1",
        rule,
        forecast,
        best_ask=Decimal("0.05"),
        config=ChinaBucketPricingConfig(
            min_edge=Decimal("0.05"), slippage_buffer=Decimal("0.01"), max_auto_ask=Decimal("0.10")
        ),
        now=datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
    )

    assert analysis.decision == "trade"
    assert analysis.side == "buy_yes"
    assert analysis.reference_price == Decimal("0.05")
    assert analysis.edge > Decimal("0.05")


def test_china_bucket_price_rejects_ask_above_cap():
    rule = parse_china_temperature_bucket_rule(
        "Highest temperature in Shanghai on May 10?",
        "Outcome: 18°C. Resolved according to Wunderground on 10 May '26. Slug: highest-temperature-in-shanghai-on-may-10-2026",
    )
    forecast = _forecast(value="18.1", lower="18.0", upper="18.2")

    analysis = analyze_china_bucket_price("m1", rule, forecast, best_ask=Decimal("0.11"))

    assert analysis.decision == "reject"
    assert analysis.side is None
    assert "ask above China bucket cap" in analysis.reasons


def _forecast(value: str, lower: str, upper: str) -> ForecastSnapshot:
    return ForecastSnapshot(
        provider="china-official-signal",
        variable="temperature_high",
        value=Decimal(value),
        lower_value=Decimal(lower),
        upper_value=Decimal(upper),
        unit="C",
        issue_time=datetime(2026, 5, 9, 0, 0, tzinfo=timezone.utc),
        valid_time=datetime(2026, 5, 10, 15, 59, tzinfo=timezone.utc),
        market_id="m1",
        location="Shanghai",
        station="ZSPD",
        fetched_at=datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
    )
