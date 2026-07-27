import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from polymarket_weather_arb.adapters.weather import google_weather as google_weather_module
from polymarket_weather_arb.adapters.weather.google_weather import (
    GoogleWeatherCoverageUnavailable,
    GoogleWeatherProvider,
)
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
)
from polymarket_weather_arb.dashboard_ui.markets import _render_forecast


def _rule(variable: str = "high", *, city: str = "New York", station: str = "KNYC"):
    title = f"Will the {variable} temperature in {city} be 80-81F on July 16, 2026?"
    return parse_global_temperature_bucket_rule(
        title, f"Settlement source: official station {station}."
    )


def _client(monkeypatch, payload, *, status_code: int = 200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.google_weather.build_httpx_client",
        lambda **_: client,
    )
    return client


def test_google_weather_selects_target_day_and_keeps_key_out_of_url(monkeypatch):
    client = _client(
        monkeypatch,
        {
            "timeZone": {"id": "America/New_York"},
            "forecastDays": [
                {
                    "displayDate": {"year": 2026, "month": 7, "day": 15},
                    "maxTemperature": {"unit": "CELSIUS", "degrees": 25},
                },
                {
                    "displayDate": {"year": 2026, "month": 7, "day": 16},
                    "maxTemperature": {"unit": "CELSIUS", "degrees": 27},
                },
            ],
        },
    )

    snapshot, raw = GoogleWeatherProvider("secret-key").fetch_forecast(
        "m1", _rule(), latitude=40.7, longitude=-74.0
    )

    assert snapshot.value == Decimal("80.6")
    assert snapshot.unit == "F"
    assert raw["decision_role"] == "pricing_reference"
    assert "forecastDays" not in raw
    call = client.get.call_args
    assert call.args[0].endswith("/forecast/days:lookup")
    assert call.kwargs["headers"] == {"X-Goog-Api-Key": "secret-key"}
    assert "key" not in call.kwargs["params"]


def test_google_weather_rejects_missing_target_day(monkeypatch):
    _client(
        monkeypatch,
        {
            "timeZone": {"id": "America/New_York"},
            "forecastDays": [
                {
                    "displayDate": {"year": 2026, "month": 7, "day": 15},
                    "maxTemperature": {"unit": "CELSIUS", "degrees": 25},
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="does not include target day"):
        GoogleWeatherProvider("secret-key").fetch_forecast(
            "m1", _rule(), latitude=40.7, longitude=-74.0
        )


def test_google_weather_404_is_cached_across_provider_instances(monkeypatch):
    client = _client(monkeypatch, {}, status_code=404)
    monkeypatch.setattr(google_weather_module, "_UNSUPPORTED_COORDINATES", set())

    with pytest.raises(GoogleWeatherCoverageUnavailable, match="coverage unavailable"):
        GoogleWeatherProvider("secret-key").fetch_forecast(
            "m1", _rule(), latitude=31.22222, longitude=121.45806
        )
    with pytest.raises(GoogleWeatherCoverageUnavailable, match="cached"):
        GoogleWeatherProvider("secret-key").fetch_forecast(
            "m2", _rule(), latitude=31.222219, longitude=121.458061
        )

    assert client.get.call_count == 1


@pytest.mark.parametrize(
    ("city", "station", "latitude", "longitude"),
    [
        ("Shanghai", "ZSPD", 31.146, 121.8),
        ("Seoul", "RKSI", 37.469, 126.451),
        ("Tokyo", "RJTT", 35.55, 139.78),
    ],
)
def test_google_weather_skips_known_unsupported_daily_forecast_regions(
    monkeypatch, city, station, latitude, longitude
):
    client = _client(monkeypatch, {})

    with pytest.raises(GoogleWeatherCoverageUnavailable, match="coverage unavailable"):
        GoogleWeatherProvider("secret-key").fetch_forecast(
            "m-unsupported",
            _rule(city=city, station=station),
            latitude=latitude,
            longitude=longitude,
        )

    client.get.assert_not_called()


def test_market_forecast_renders_google_pricing_reference():
    html = _render_forecast(
        {
            "provider": "open-meteo-ensemble",
            "variable": "temperature_high",
            "value": 80.5,
            "unit": "F",
            "location": "New York",
            "station": None,
            "fetched_at": "2026-07-14T12:00:00+00:00",
            "raw_payload": json.dumps(
                {
                    "source_grade": "research_forecast",
                    "pricing_references": {
                        "google_weather": {
                            "value": 81.2,
                            "unit": "F",
                            "target_date": "2026-07-16",
                            "timezone": "America/New_York",
                        }
                    },
                }
            ),
        },
        "en",
    )

    assert "Google Weather (pricing reference)" in html
    assert "81.2 F" in html
    assert "2026-07-16" in html
    assert "America/New_York" in html
