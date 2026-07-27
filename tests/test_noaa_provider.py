"""Tests for NOAA weather provider and source grade handling."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from polymarket_weather_arb.adapters.weather.noaa import NoaaProvider
from polymarket_weather_arb.domain.rules import ResolutionRule


def _make_rule(**overrides) -> ResolutionRule:
    """创建测试用的 ResolutionRule"""
    defaults = {
        "raw_text": "NYC temperature high",
        "location": "New York",
        "source": "OKX/82,75",  # NYC gridpoint
        "station": "OKX",
        "variable": "temperature_high",
        "threshold": None,
        "operator": None,
        "window_start": "2026-06-03",
        "window_end": None,
        "unit": "F",
        "confidence": 0.9,
        "tradable": True,
        "rejection_reason": None,
    }
    defaults.update(overrides)
    return ResolutionRule(**defaults)


def _mock_noaa_response(periods: list[dict]) -> dict:
    """模拟 NOAA API 响应"""
    return {
        "properties": {
            "periods": periods,
        }
    }


@patch("httpx.Client")
def test_noaa_returns_official_forecast(mock_client_class):
    """测试 NOAA forecast 返回 official_forecast（非 settlement observation）"""
    # 模拟 NOAA API 响应
    mock_response = MagicMock()
    mock_response.json.return_value = _mock_noaa_response(
        [
            {
                "startTime": "2026-06-03T06:00:00-04:00",
                "isDaytime": True,
                "temperature": 75,
                "temperatureUnit": "F",
                "name": "Tuesday",
            },
            {
                "startTime": "2026-06-03T18:00:00-04:00",
                "isDaytime": False,
                "temperature": 62,
                "temperatureUnit": "F",
                "name": "Tuesday Night",
            },
        ]
    )
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule()
    snapshot, raw_payload = provider.fetch_forecast("test-market", rule)

    # 验证 snapshot
    assert snapshot.value == Decimal("75")
    assert snapshot.unit == "F"
    assert snapshot.station == "OKX/82,75"

    # 验证 source grade
    assert raw_payload["source_grade"] == "official_forecast"
    assert raw_payload["official_signal"] is True
    assert raw_payload["source"] == "noaa_nws"


@patch("httpx.Client")
def test_noaa_selects_daytime_for_high(mock_client_class):
    """测试 NOAA 选择白天周期作为 high temperature"""
    mock_response = MagicMock()
    mock_response.json.return_value = _mock_noaa_response(
        [
            {
                "startTime": "2026-06-03T06:00:00-04:00",
                "isDaytime": True,
                "temperature": 75,
                "temperatureUnit": "F",
                "name": "Tuesday",
            },
            {
                "startTime": "2026-06-03T18:00:00-04:00",
                "isDaytime": False,
                "temperature": 62,
                "temperatureUnit": "F",
                "name": "Tuesday Night",
            },
        ]
    )
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(variable="temperature_high")
    snapshot, _ = provider.fetch_forecast("test-market", rule)

    assert snapshot.value == Decimal("75")  # 白天温度


@patch("httpx.Client")
def test_noaa_selects_nighttime_for_low(mock_client_class):
    """测试 NOAA 选择夜间周期作为 low temperature"""
    mock_response = MagicMock()
    mock_response.json.return_value = _mock_noaa_response(
        [
            {
                "startTime": "2026-06-03T06:00:00-04:00",
                "isDaytime": True,
                "temperature": 75,
                "temperatureUnit": "F",
                "name": "Tuesday",
            },
            {
                "startTime": "2026-06-03T18:00:00-04:00",
                "isDaytime": False,
                "temperature": 62,
                "temperatureUnit": "F",
                "name": "Tuesday Night",
            },
        ]
    )
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(variable="temperature_low")
    snapshot, _ = provider.fetch_forecast("test-market", rule)

    assert snapshot.value == Decimal("62")  # 夜间温度


def test_noaa_requires_location():
    """测试 NOAA 需要 location"""
    provider = NoaaProvider()
    rule = _make_rule(location=None)

    with pytest.raises(ValueError, match="requires a location"):
        provider.fetch_forecast("test-market", rule)


@patch("httpx.Client")
def test_noaa_can_resolve_from_location(mock_client_class):
    """测试 NOAA 可以从城市名称解析 gridpoint"""
    mock_response = MagicMock()
    mock_response.json.return_value = _mock_noaa_response(
        [
            {
                "startTime": "2026-06-03T06:00:00-04:00",
                "isDaytime": True,
                "temperature": 75,
                "temperatureUnit": "F",
                "name": "Wednesday",
            },
        ]
    )
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source=None, location="New York")
    snapshot, raw_payload = provider.fetch_forecast("test-market", rule)

    assert snapshot.value == Decimal("75")
    assert raw_payload["source_grade"] == "official_forecast"


@patch("httpx.Client")
def test_noaa_resolves_station_id_to_gridpoint(mock_client_class):
    """测试 NOAA 可以将观测站 ID 解析为 gridpoint"""
    mock_response = MagicMock()
    mock_response.json.return_value = _mock_noaa_response(
        [
            {
                "startTime": "2026-06-03T06:00:00-04:00",
                "isDaytime": True,
                "temperature": 75,
                "temperatureUnit": "F",
                "name": "Wednesday",
            },
        ]
    )
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="KNYC", station="KNYC")
    snapshot, raw_payload = provider.fetch_forecast("test-market", rule)

    # 验证使用了正确的 gridpoint
    assert snapshot.value == Decimal("75")
    assert raw_payload["source_grade"] == "official_forecast"


@patch("httpx.Client")
def test_noaa_precipitation(mock_client_class):
    """测试 NOAA 降雨数据"""
    mock_response = MagicMock()
    mock_response.json.return_value = _mock_noaa_response(
        [
            {
                "startTime": "2026-06-03T06:00:00-04:00",
                "isDaytime": True,
                "temperature": 75,
                "temperatureUnit": "F",
                "name": "Tuesday",
                "quantitativePrecipitation": {
                    "value": 5.2,
                    "unitCode": "wmoUnit:mm",
                },
            },
        ]
    )
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(variable="precipitation")
    snapshot, raw_payload = provider.fetch_forecast("test-market", rule)

    assert snapshot.value == Decimal("5.2")
    assert snapshot.unit == "mm"
    assert raw_payload["source_grade"] == "official_forecast"


@patch("httpx.Client")
def test_noaa_snowfall(mock_client_class):
    """测试 NOAA 降雪数据"""
    mock_response = MagicMock()
    mock_response.json.return_value = _mock_noaa_response(
        [
            {
                "startTime": "2026-06-03T06:00:00-04:00",
                "isDaytime": True,
                "temperature": 32,
                "temperatureUnit": "F",
                "name": "Tuesday",
                "snowfallAmount": {
                    "value": 2.5,
                    "unitCode": "wmoUnit:cm",
                },
            },
        ]
    )
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(variable="snowfall")
    snapshot, raw_payload = provider.fetch_forecast("test-market", rule)

    assert snapshot.value == Decimal("2.5")
    assert snapshot.unit == "cm"
    assert raw_payload["source_grade"] == "official_forecast"


def test_noaa_rejects_unsupported_variable():
    """测试 NOAA 拒绝不支持的变量"""
    provider = NoaaProvider()
    rule = _make_rule(variable="wind_speed")

    with pytest.raises(ValueError, match="unsupported NOAA variable"):
        provider.fetch_forecast("test-market", rule)


@patch("httpx.Client")
def test_noaa_fetch_observation_returns_temperature_high_from_station_observations(
    mock_client_class,
):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {
                        "value": 27.2,
                        "unitCode": "wmoUnit:degC",
                        "qualityControl": "V",
                    },
                }
            },
            {
                "properties": {
                    "timestamp": "2026-06-03T18:00:00+00:00",
                    "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
            {
                "properties": {
                    "timestamp": "2026-06-03T22:00:00+00:00",
                    "temperature": {"value": 29.4, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")

    observation, raw_payload = provider.fetch_observation("test-market", rule)

    assert observation.value == Decimal("86")
    assert observation.unit == "F"
    assert observation.station == "KNYC"
    assert observation.provider == "noaa"
    assert observation.variable == "temperature_high"
    assert observation.quality_status == "V"
    assert raw_payload["source"] == "nws-observation"
    assert raw_payload["source_grade"] == "settlement_observation"
    assert raw_payload["official_signal"] is True
    assert raw_payload["settlement_source"] is True
    assert raw_payload["observation_count"] == 3
    mock_client.get.assert_called_once()
    assert "stations/KNYC/observations" in mock_client.get.call_args.args[0]


@patch("httpx.Client")
def test_noaa_fetch_observation_rejects_missing_temperature_values(mock_client_class):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [{"properties": {"temperature": {"value": None}}}]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_low", unit="F")

    with pytest.raises(ValueError, match="no usable NWS temperature observations"):
        provider.fetch_observation("test-market", rule)


@patch("httpx.Client")
def test_noaa_fetch_observation_includes_compact_observations_in_raw(mock_client_class):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {"value": 27.2, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
            {
                "properties": {
                    "timestamp": "2026-06-03T18:00:00+00:00",
                    "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")
    _, raw_payload = provider.fetch_observation("test-market", rule)

    assert "observations" in raw_payload
    compact = raw_payload["observations"]
    assert len(compact) == 2
    assert compact[0]["timestamp"] == "2026-06-03T13:00:00+00:00"
    assert compact[0]["quality_status"] == "V"
    assert "value" in compact[0]
    assert "unit" in compact[0]
    assert raw_payload["latest_observation_at"] == "2026-06-03T18:00:00+00:00"
    # query_start / query_end present for audit
    assert "query_start" in raw_payload
    assert "query_end" in raw_payload


@patch("httpx.Client")
def test_noaa_fetch_observation_records_sample_extrema_provenance(mock_client_class):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {"value": 27.2, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
            {
                "properties": {
                    "timestamp": "2026-06-03T18:00:00+00:00",
                    "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")
    _, raw_payload = provider.fetch_observation("test-market", rule)

    assert raw_payload["extrema_method"] == "max_of_observation_samples_in_local_window"
    assert raw_payload["extrema_source"] == "nws_observations_api"
    assert raw_payload["sample_count"] == 2
    assert raw_payload["sample_extrema_value"] == "30.0"
    assert raw_payload["sample_extrema_unit"] == "C"
    assert raw_payload["official_daily_value"] is None
    assert any("sample-based, not official daily summary" in w for w in raw_payload["warnings"])


@patch("httpx.Client")
def test_noaa_fetch_observation_records_min_sample_extrema_method_for_lows(mock_client_class):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T08:00:00+00:00",
                    "temperature": {"value": 18.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
            {
                "properties": {
                    "timestamp": "2026-06-03T14:00:00+00:00",
                    "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_low", unit="F")
    _, raw_payload = provider.fetch_observation("test-market", rule)

    assert raw_payload["extrema_method"] == "min_of_observation_samples_in_local_window"
    assert raw_payload["sample_extrema_value"] == "18.0"
    assert raw_payload["sample_extrema_unit"] == "C"


@patch("httpx.Client")
def test_noaa_fetch_observation_warns_on_low_coverage(mock_client_class):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {"value": 27.2, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")
    _, raw_payload = provider.fetch_observation("test-market", rule)

    warnings = raw_payload["warnings"]
    assert any("low observation coverage" in warning for warning in warnings)


@patch("httpx.Client")
def test_noaa_fetch_observation_warns_on_non_v_selected_quality(mock_client_class):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {"value": 27.2, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
            {
                "properties": {
                    "timestamp": "2026-06-03T18:00:00+00:00",
                    "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "X",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")
    _, raw_payload = provider.fetch_observation("test-market", rule)

    warnings = raw_payload["warnings"]
    assert any("selected observation quality is X" in w for w in warnings)


@patch("httpx.Client")
def test_noaa_fetch_observation_warns_on_missing_selected_quality(mock_client_class):
    """Selected observation with missing/None qualityControl triggers unknown quality warning."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {"value": 27.2, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
            {
                "properties": {
                    "timestamp": "2026-06-03T18:00:00+00:00",
                    "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": None,
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")
    _, raw_payload = provider.fetch_observation("test-market", rule)

    warnings = raw_payload["warnings"]
    assert any("selected observation quality is unknown" in w for w in warnings)


@patch("httpx.Client")
def test_noaa_fetch_observation_knyc_local_day_window(mock_client_class):
    """KNYC (America/New_York) local-day window: 2026-06-03 00:00 EDT = 04:00 UTC."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {
                        "value": 27.2,
                        "unitCode": "wmoUnit:degC",
                        "qualityControl": "V",
                    },
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")
    observation, raw_payload = provider.fetch_observation("test-market", rule)

    assert observation.quality_status == "V"
    assert not any("quality is unknown" in warning for warning in raw_payload["warnings"])
    assert raw_payload["timezone"] == "America/New_York"
    assert raw_payload["query_start"] == "2026-06-03T04:00:00Z"
    assert raw_payload["query_end"] == "2026-06-04T03:59:59Z"
    assert "2026-06-03T00:00:00" in raw_payload["local_start"]
    assert "2026-06-03T23:59:59" in raw_payload["local_end"]
    # Verify the API was called with the correct UTC window
    call_params = mock_client.get.call_args
    assert call_params.kwargs.get("params", {}).get("start") == "2026-06-03T04:00:00Z"
    assert call_params.kwargs.get("params", {}).get("end") == "2026-06-04T03:59:59Z"


@patch("httpx.Client")
def test_noaa_fetch_observation_klax_local_day_window(mock_client_class):
    """KLAX (America/Los_Angeles) local-day window: 2026-06-03 00:00 PDT = 07:00 UTC."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T17:00:00+00:00",
                    "temperature": {"value": 22.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KLAX", variable="temperature_high", unit="F")
    _, raw_payload = provider.fetch_observation("test-market", rule)

    assert raw_payload["timezone"] == "America/Los_Angeles"
    assert raw_payload["query_start"] == "2026-06-03T07:00:00Z"
    assert raw_payload["query_end"] == "2026-06-04T06:59:59Z"


@patch("httpx.Client")
def test_noaa_fetch_observation_raw_payload_has_timezone_audit_fields(mock_client_class):
    """raw_payload includes timezone, local_start, local_end, query_start, query_end."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {"value": 27.2, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")
    _, raw_payload = provider.fetch_observation("test-market", rule)

    assert "timezone" in raw_payload
    assert "local_start" in raw_payload
    assert "local_end" in raw_payload
    assert "query_start" in raw_payload
    assert "query_end" in raw_payload
    assert raw_payload["timezone"] == "America/New_York"


@patch("httpx.Client")
def test_noaa_fetch_observation_fallback_utc_when_timezone_unknown(mock_client_class):
    """Stations without timezone mapping fall back to UTC with a warning."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {"value": 27.2, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    # Use a station not in the mapping to trigger fallback
    rule = _make_rule(source="NOAA", station="KXYZ", variable="temperature_high", unit="F")

    # Patch _resolve_station to return KXYZ without raising
    with patch.object(provider, "_resolve_station", return_value="KXYZ"):
        _, raw_payload = provider.fetch_observation("test-market", rule)

    assert raw_payload["timezone"] is None
    assert raw_payload["query_start"] == "2026-06-03T00:00:00Z"
    assert raw_payload["query_end"] == "2026-06-03T23:59:59Z"
    warnings = raw_payload["warnings"]
    assert any("station timezone unknown" in w for w in warnings)


@patch("httpx.Client")
def test_noaa_fetch_observation_high_low_selection_still_correct_with_local_window(
    mock_client_class,
):
    """High/low selection works correctly with timezone-aware query window."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T08:00:00+00:00",
                    "temperature": {"value": 18.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
            {
                "properties": {
                    "timestamp": "2026-06-03T14:00:00+00:00",
                    "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
            {
                "properties": {
                    "timestamp": "2026-06-03T20:00:00+00:00",
                    "temperature": {"value": 25.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()

    # Test high
    rule_high = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")
    obs_high, _ = provider.fetch_observation("test-market", rule_high)
    assert obs_high.value == Decimal("86")  # 30C -> 86F

    # Test low
    rule_low = _make_rule(source="NOAA", station="KNYC", variable="temperature_low", unit="F")
    obs_low, _ = provider.fetch_observation("test-market", rule_low)
    assert obs_low.value == Decimal("64.4")  # 18C -> 64.4F


@patch("httpx.Client")
def test_noaa_fetch_observation_dst_spring_forward_knyc(mock_client_class):
    """KNYC spring-forward 2026-03-08: local midnight EST (UTC-5) = 05:00 UTC.
    Local 23:59:59 EDT (UTC-4) = next day 03:59:59 UTC.
    """
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-03-08T08:00:00+00:00",
                    "temperature": {"value": 10.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(
        source="NOAA",
        station="KNYC",
        variable="temperature_high",
        unit="F",
        window_start="2026-03-08",
    )
    _, raw_payload = provider.fetch_observation("test-market", rule)

    assert raw_payload["timezone"] == "America/New_York"
    # Local 2026-03-08 00:00 EST = 05:00 UTC
    assert raw_payload["query_start"] == "2026-03-08T05:00:00Z"
    # Local 2026-03-08 23:59:59 EDT = 2026-03-09 03:59:59 UTC
    assert raw_payload["query_end"] == "2026-03-09T03:59:59Z"


@patch("httpx.Client")
def test_noaa_fetch_observation_dst_fall_back_knyc(mock_client_class):
    """KNYC fall-back 2026-11-01: local midnight EDT (UTC-4) = 04:00 UTC.
    Local 23:59:59 EST (UTC-5) = next day 04:59:59 UTC.
    """
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-11-01T12:00:00+00:00",
                    "temperature": {"value": 15.0, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    rule = _make_rule(
        source="NOAA",
        station="KNYC",
        variable="temperature_high",
        unit="F",
        window_start="2026-11-01",
    )
    _, raw_payload = provider.fetch_observation("test-market", rule)

    assert raw_payload["timezone"] == "America/New_York"
    # Local 2026-11-01 00:00 EDT = 04:00 UTC
    assert raw_payload["query_start"] == "2026-11-01T04:00:00Z"
    # Local 2026-11-01 23:59:59 EST = 2026-11-02 04:59:59 UTC
    assert raw_payload["query_end"] == "2026-11-02T04:59:59Z"


@patch("httpx.Client")
def test_noaa_fetch_observation_invalid_timezone_fallback(mock_client_class):
    """Invalid timezone in mapping falls back to UTC with a clear warning."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-06-03T13:00:00+00:00",
                    "temperature": {"value": 27.2, "unitCode": "wmoUnit:degC"},
                    "qualityControl": "V",
                }
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client_class.return_value = mock_client

    provider = NoaaProvider()
    # Inject an invalid timezone for KNYC
    provider._station_mapping["mappings"]["KNYC"]["timezone"] = "Bad/Timezone"
    rule = _make_rule(source="NOAA", station="KNYC", variable="temperature_high", unit="F")

    # Must not raise ZoneInfoNotFoundError
    _, raw_payload = provider.fetch_observation("test-market", rule)

    assert raw_payload["timezone"] is None
    assert raw_payload["query_start"] == "2026-06-03T00:00:00Z"
    assert raw_payload["query_end"] == "2026-06-03T23:59:59Z"
    warnings = raw_payload["warnings"]
    assert any("station timezone invalid: Bad/Timezone" in w for w in warnings)
    assert any("UTC calendar-day window used" in w for w in warnings)
