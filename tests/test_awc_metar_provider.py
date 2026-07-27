from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from polymarket_weather_arb.adapters.weather.awc_metar import (
    AwcMetarProvider,
    AwcTafProvider,
    fetch_awc_station_location,
)
from polymarket_weather_arb.adapters.http_reader import (
    open_meteo_usage_snapshot,
    reset_http_reader_state,
)
from polymarket_weather_arb.adapters.weather.open_meteo import OpenMeteoProvider
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.market_eligibility import resolve_market_timezone


STATION_CASES = [
    ("KLGA", "New York", "America/New_York", "F"),
    ("KORD", "Chicago", "America/Chicago", "F"),
    ("ZSPD", "Shanghai", "Asia/Shanghai", "C"),
    ("ZUUU", "Chengdu", "Asia/Shanghai", "C"),
    ("RKSI", "Seoul", "Asia/Seoul", "C"),
    ("EGLC", "London", "Europe/London", "C"),
]


def _rule(station: str = "ZSPD"):
    return _station_rule(station, "Shanghai", "C")


def _station_rule(station: str, city: str, unit: str):
    bucket = "80F" if unit == "F" else "25C"
    city_slug = city.lower().replace(" ", "-")
    return parse_global_temperature_bucket_rule(
        f"Will the highest temperature in {city} be {bucket} on July 17, 2026?",
        f"Settlement source: Wunderground station {station}. "
        f"https://www.wunderground.com/history/daily/test/{city_slug}/{station}",
    )


def _client(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


def test_awc_metar_uses_exact_global_station_and_local_day(monkeypatch):
    records = [
        {
            "icaoId": "ZSPD",
            "lat": 31.1434,
            "lon": 121.8052,
            "obsTime": int(datetime(2026, 7, 17, 4, tzinfo=timezone.utc).timestamp()),
            "temp": 31.2,
            "rawOb": "METAR ZSPD 170400Z 31/24",
        },
        {
            "icaoId": "ZSPD",
            "obsTime": int(datetime(2026, 7, 17, 6, tzinfo=timezone.utc).timestamp()),
            "temp": 33.1,
            "rawOb": "METAR ZSPD 170600Z 33/24",
        },
    ]
    client = _client(records)
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.awc_metar.build_httpx_client",
        lambda **_: client,
    )

    observation, raw = AwcMetarProvider().fetch_observation("m1", _rule())

    assert observation.station == "ZSPD"
    assert observation.value == Decimal("33.1")
    assert observation.quality_status == "AWC"
    assert raw["timezone"] == "Asia/Shanghai"
    assert raw["settlement_source"] is False
    assert raw["settlement_proxy"] is True
    assert raw["settlement_provider"] == "wunderground"
    assert raw["settlement_alignment"] == "exact_station_metar_reports"
    assert raw["station_coordinates"] == {
        "latitude": 31.1434,
        "longitude": 121.8052,
    }
    assert raw["latest_observation_at"] == "2026-07-17T06:00:00+00:00"
    assert client.get.call_args.kwargs["params"]["ids"] == "ZSPD"


def test_awc_station_location_requires_exact_icao_match(monkeypatch):
    fetch_awc_station_location.cache_clear()
    client = _client(
        [
            {"icaoId": "ZSSS", "lat": 31.2, "lon": 121.3},
            {"icaoId": "ZSQD", "lat": 36.362, "lon": 120.087, "site": "Jiaodong"},
        ]
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.awc_metar.build_httpx_client",
        lambda **_: client,
    )

    latitude, longitude, payload = fetch_awc_station_location("zsqd")

    assert (latitude, longitude) == (36.362, 120.087)
    assert payload["icaoId"] == "ZSQD"
    assert client.get.call_args.kwargs["params"] == {"ids": "ZSQD", "format": "json"}
    fetch_awc_station_location.cache_clear()


def test_awc_metar_rejects_other_station_records(monkeypatch):
    client = _client(
        [
            {
                "icaoId": "ZSSS",
                "obsTime": int(datetime(2026, 7, 17, 6, tzinfo=timezone.utc).timestamp()),
                "temp": 33.1,
            }
        ]
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.awc_metar.build_httpx_client",
        lambda **_: client,
    )

    with pytest.raises(ValueError, match="no target-day METAR temperatures for ZSPD"):
        AwcMetarProvider().fetch_observation("m1", _rule())


def test_awc_taf_extracts_exact_station_local_day_high(monkeypatch):
    reset_http_reader_state()
    client = _client(
        [
            {
                "icaoId": "RKSI",
                "issueTime": "2026-07-16T18:00:00Z",
                "validTimeFrom": int(datetime(2026, 7, 16, 18, tzinfo=timezone.utc).timestamp()),
                "validTimeTo": int(datetime(2026, 7, 18, 6, tzinfo=timezone.utc).timestamp()),
                "mostRecent": 1,
                "rawTAF": ("TAF RKSI 161800Z 1618/1806 TX28/1704Z TN23/1620Z TX27/1804Z"),
            }
        ]
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.awc_metar.build_httpx_client",
        lambda **_: client,
    )

    forecast, raw = AwcTafProvider().fetch_forecast(
        "seoul-28",
        _station_rule("RKSI", "Seoul", "C"),
    )

    assert forecast.provider == "awc-taf"
    assert forecast.station == "RKSI"
    assert forecast.value == Decimal("28")
    assert forecast.valid_time == datetime(2026, 7, 17, 4, tzinfo=timezone.utc)
    assert raw["source_grade"] == "official_forecast"
    assert raw["decision_role"] == "pricing_reference"
    assert raw["settlement_source"] is False
    assert client.get.call_args.kwargs["params"] == {"ids": "RKSI", "format": "json"}
    reset_http_reader_state()


def test_awc_taf_resolves_month_boundary_and_negative_temperature(monkeypatch):
    reset_http_reader_state()
    rule = parse_global_temperature_bucket_rule(
        "Will the highest temperature in Seoul be -5C on August 1, 2026?",
        "Settlement source: Wunderground station RKSI.",
    )
    client = _client(
        [
            {
                "icaoId": "RKSI",
                "issueTime": "2026-07-31T18:00:00Z",
                "validTimeFrom": int(datetime(2026, 7, 31, 18, tzinfo=timezone.utc).timestamp()),
                "validTimeTo": int(datetime(2026, 8, 2, 6, tzinfo=timezone.utc).timestamp()),
                "mostRecent": 1,
                "rawTAF": "TAF RKSI 311800Z 3118/0206 TXM05/0104Z TNM09/3120Z",
            }
        ]
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.awc_metar.build_httpx_client",
        lambda **_: client,
    )

    forecast, _raw = AwcTafProvider().fetch_forecast("seoul-neg", rule)

    assert forecast.value == Decimal("-5")
    assert forecast.valid_time == datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
    reset_http_reader_state()


def test_awc_taf_without_target_day_tx_is_explicitly_unavailable(monkeypatch):
    reset_http_reader_state()


def test_awc_taf_cache_does_not_pollute_open_meteo_usage(monkeypatch):
    reset_http_reader_state()
    client = _client(
        [
            {
                "icaoId": "RKSI",
                "issueTime": "2026-07-16T18:00:00Z",
                "mostRecent": 1,
                "rawTAF": "TAF RKSI 161800Z 1618/1806 TX28/1704Z",
            }
        ]
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.awc_metar.build_httpx_client",
        lambda **_: client,
    )

    AwcTafProvider().fetch_forecast("seoul-28", _station_rule("RKSI", "Seoul", "C"))

    assert open_meteo_usage_snapshot()["network_requests"] == 0
    assert open_meteo_usage_snapshot()["cache_misses"] == 0
    reset_http_reader_state()
    client = _client(
        [
            {
                "icaoId": "RKSI",
                "issueTime": "2026-07-16T18:00:00Z",
                "mostRecent": 1,
                "rawTAF": "TAF RKSI 161800Z 1618/1806 18005KT 9999 SCT030",
            }
        ]
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.awc_metar.build_httpx_client",
        lambda **_: client,
    )

    with pytest.raises(ValueError, match="no TX group for local date 2026-07-17"):
        AwcTafProvider().fetch_forecast(
            "seoul-no-tx",
            _station_rule("RKSI", "Seoul", "C"),
        )
    reset_http_reader_state()


@pytest.mark.parametrize("station,city,timezone_name,unit", STATION_CASES)
def test_station_matrix_parses_exact_icao_and_resolves_timezone(station, city, timezone_name, unit):
    rule = _station_rule(station, city, unit)

    assert rule.tradable is True
    assert rule.station == station
    assert rule.location == city
    assert rule.target_date == "2026-07-17"
    assert rule.unit == unit
    assert resolve_market_timezone(location_hint=station) == timezone_name


@pytest.mark.parametrize("station,city,timezone_name,unit", STATION_CASES)
def test_awc_station_matrix_filters_by_station_local_day(
    monkeypatch, station, city, timezone_name, unit
):
    zone = ZoneInfo(timezone_name)
    previous_local = datetime(2026, 7, 16, 23, 30, tzinfo=zone).astimezone(timezone.utc)
    target_local = datetime(2026, 7, 17, 12, 0, tzinfo=zone).astimezone(timezone.utc)
    client = _client(
        [
            {
                "icaoId": station,
                "obsTime": int(previous_local.timestamp()),
                "temp": 40,
                "rawOb": f"METAR {station} previous-local-day",
            },
            {
                "icaoId": station,
                "obsTime": int(target_local.timestamp()),
                "temp": 25,
                "rawOb": f"METAR {station} target-local-day",
            },
            {
                "icaoId": "XXXX",
                "obsTime": int(target_local.timestamp()),
                "temp": 45,
                "rawOb": "METAR XXXX wrong-station",
            },
        ]
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.awc_metar.build_httpx_client",
        lambda **_: client,
    )

    observation, raw = AwcMetarProvider().fetch_observation(
        f"market-{station}", _station_rule(station, city, unit)
    )

    assert observation.station == station
    assert observation.value == (Decimal("77") if unit == "F" else Decimal("25"))
    assert observation.observed_at == target_local
    assert raw["timezone"] == timezone_name
    assert raw["observation_count"] == 1
    assert client.get.call_args.kwargs["params"]["ids"] == station


def test_open_meteo_hourly_context_uses_remaining_local_trajectory(monkeypatch):
    client = _client(
        {
            "hourly": {
                "time": ["2026-07-17T09:00", "2026-07-17T12:00", "2026-07-17T15:00"],
                "temperature_2m": [31.0, 32.0, 33.0],
                "cloud_cover": [10, 20, 30],
                "shortwave_radiation": [400, 700, 500],
                "wind_speed_10m": [4, 6, 8],
            }
        }
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.adapters.weather.open_meteo.build_httpx_client",
        lambda **_: client,
    )

    context = OpenMeteoProvider().fetch_hourly_context(
        latitude=31.1,
        longitude=121.8,
        timezone_name="Asia/Shanghai",
        target_date="2026-07-17",
        unit="C",
        now=datetime(2026, 7, 17, 2, tzinfo=timezone.utc),
    )

    assert context["local_now"] == "2026-07-17T10:00:00+08:00"
    assert context["remaining_peak"] == "33.0"
    assert context["remaining_peak_time"] == "2026-07-17T15:00:00+08:00"
    assert context["peak_cloud_cover"] == 30.0
