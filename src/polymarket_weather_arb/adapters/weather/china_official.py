from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode


from polymarket_weather_arb.adapters.http_client import build_httpx_client

from polymarket_weather_arb.adapters.http_reader import safe_http_read

from polymarket_weather_arb.domain.china_temperature_bucket import ChinaTemperatureBucketRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot

FetchJson = Callable[[str], dict[str, Any]]

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CONFIGURED_SIGNAL_PROVIDER = "china-configured-weather-signal"
OPEN_METEO_SIGNAL_PROVIDER = "open-meteo-china-signal"


@dataclass(frozen=True)
class ChinaCityWeatherSource:
    city: str
    station_id: str
    url: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    official: bool = False


CHINA_CITY_SOURCES = {
    "Qingdao": ChinaCityWeatherSource("Qingdao", "ZSQD", latitude=36.0671, longitude=120.3826),
    "Chengdu": ChinaCityWeatherSource("Chengdu", "ZUUU", latitude=30.5728, longitude=104.0668),
    "Shanghai": ChinaCityWeatherSource("Shanghai", "ZSPD", latitude=31.2304, longitude=121.4737),
    "Wuhan": ChinaCityWeatherSource("Wuhan", "ZHHH", latitude=30.5928, longitude=114.3055),
}


class ChinaOfficialWeatherProvider:
    name = "china-weather-signal"

    def __init__(
        self,
        *,
        fetch_json: FetchJson | None = None,
        sources: dict[str, ChinaCityWeatherSource] | None = None,
        use_open_meteo_fallback: bool = True,
    ) -> None:
        self.fetch_json = fetch_json or _http_fetch_json
        self.sources = sources or CHINA_CITY_SOURCES
        self.use_open_meteo_fallback = use_open_meteo_fallback

    @classmethod
    def from_settings(cls, settings: Any) -> ChinaOfficialWeatherProvider:
        sources = {
            "Qingdao": _source_with_url(
                CHINA_CITY_SOURCES["Qingdao"], settings.china_weather_qingdao_url
            ),
            "Chengdu": _source_with_url(
                CHINA_CITY_SOURCES["Chengdu"], settings.china_weather_chengdu_url
            ),
            "Shanghai": _source_with_url(
                CHINA_CITY_SOURCES["Shanghai"], settings.china_weather_shanghai_url
            ),
            "Wuhan": _source_with_url(
                CHINA_CITY_SOURCES["Wuhan"], settings.china_weather_wuhan_url
            ),
        }
        return cls(
            sources=sources, use_open_meteo_fallback=settings.china_weather_open_meteo_fallback
        )

    def fetch_forecast(
        self, market_id: str, rule: ChinaTemperatureBucketRule
    ) -> tuple[ForecastSnapshot, dict[str, Any]]:
        if not rule.city:
            raise ValueError("China weather signal requires a parsed city")
        if rule.variable != "temperature_high":
            raise ValueError(f"unsupported China weather variable: {rule.variable}")
        if not rule.target_date:
            raise ValueError("China weather signal requires a target date")
        try:
            source = self.sources[rule.city]
        except KeyError as exc:
            raise ValueError(f"unsupported China weather city: {rule.city}") from exc

        if source.url:
            return self._fetch_configured_source(market_id, rule, source)
        if self.use_open_meteo_fallback:
            return self._fetch_open_meteo_signal(market_id, rule, source)
        raise ValueError(f"China weather source URL is not configured for {rule.city}")

    def _fetch_configured_source(
        self,
        market_id: str,
        rule: ChinaTemperatureBucketRule,
        source: ChinaCityWeatherSource,
    ) -> tuple[ForecastSnapshot, dict[str, Any]]:
        assert source.url is not None
        payload = self.fetch_json(source.url)
        forecast = _extract_daily_forecast(payload, rule.target_date or "")
        value = _decimal_field(forecast, "temperature_high_c")
        issue_time = _parse_datetime(str(payload.get("issue_time") or forecast.get("issue_time")))
        valid_time = _parse_datetime(
            str(forecast.get("valid_time") or f"{rule.target_date}T23:59:00+08:00")
        )
        fetched_at = datetime.now(timezone.utc)
        snapshot = ForecastSnapshot(
            market_id=market_id,
            provider=CONFIGURED_SIGNAL_PROVIDER,
            location=rule.city,
            station=source.station_id,
            variable=rule.variable,
            value=value,
            lower_value=_optional_decimal_field(forecast, "temperature_high_lower_c"),
            upper_value=_optional_decimal_field(forecast, "temperature_high_upper_c"),
            unit="C",
            issue_time=issue_time,
            valid_time=valid_time,
            fetched_at=fetched_at,
        )
        raw_payload = {
            "source_type": "configured_json",
            "source_url": source.url,
            "station_id": source.station_id,
            "configured_signal": True,
            "official_signal": source.official,
            "source_grade": "official_forecast" if source.official else "research_forecast",
            "payload": payload,
            "selected_forecast": forecast,
        }
        return snapshot, raw_payload

    def _fetch_open_meteo_signal(
        self,
        market_id: str,
        rule: ChinaTemperatureBucketRule,
        source: ChinaCityWeatherSource,
    ) -> tuple[ForecastSnapshot, dict[str, Any]]:
        if source.latitude is None or source.longitude is None:
            raise ValueError(
                f"China weather fallback coordinates are not configured for {rule.city}"
            )
        url = _open_meteo_url(source, rule.target_date or "")
        payload = self.fetch_json(url)
        date, value = _extract_open_meteo_high(payload, rule.target_date or "")
        uncertainty = Decimal("1.20")
        fetched_at = datetime.now(timezone.utc)
        snapshot = ForecastSnapshot(
            market_id=market_id,
            provider=OPEN_METEO_SIGNAL_PROVIDER,
            location=rule.city,
            station=source.station_id,
            variable=rule.variable,
            value=value,
            lower_value=value - uncertainty,
            upper_value=value + uncertainty,
            unit="C",
            issue_time=fetched_at,
            valid_time=_parse_datetime(f"{date}T23:59:00+08:00"),
            fetched_at=fetched_at,
        )
        raw_payload = {
            "source_type": "open_meteo_forecast",
            "source_url": url,
            "station_id": source.station_id,
            "configured_signal": False,
            "official_signal": False,
            "source_grade": "research_forecast",
            "coordinates": {"latitude": source.latitude, "longitude": source.longitude},
            "payload": payload,
            "selected_forecast": {"date": date, "temperature_high_c": str(value)},
        }
        return snapshot, raw_payload


def _http_fetch_json(url: str) -> dict[str, Any]:
    with build_httpx_client(timeout=20) as client:
        response = safe_http_read(client, "GET", url)
        response.raise_for_status()
        return response.json()


def _source_with_url(source: ChinaCityWeatherSource, url: str | None) -> ChinaCityWeatherSource:
    return ChinaCityWeatherSource(
        source.city,
        source.station_id,
        url=url,
        latitude=source.latitude,
        longitude=source.longitude,
        official=source.official,
    )


def _open_meteo_url(source: ChinaCityWeatherSource, target_date: str) -> str:
    params = {
        "latitude": str(source.latitude),
        "longitude": str(source.longitude),
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone": "Asia/Shanghai",
    }
    if target_date:
        params["start_date"] = target_date
        params["end_date"] = target_date
    else:
        params["past_days"] = "1"
        params["forecast_days"] = "16"
    return f"{OPEN_METEO_FORECAST_URL}?{urlencode(params)}"


def _extract_daily_forecast(payload: dict[str, Any], target_date: str) -> dict[str, Any]:
    forecasts = payload.get("daily") or payload.get("forecasts") or []
    if not isinstance(forecasts, list):
        raise ValueError("China weather payload daily forecast is not a list")
    for forecast in forecasts:
        if (
            isinstance(forecast, dict)
            and str(forecast.get("date") or forecast.get("target_date")) == target_date
        ):
            return forecast
    raise ValueError(f"China weather payload has no forecast for {target_date}")


def _extract_open_meteo_high(payload: dict[str, Any], target_date: str) -> tuple[str, Decimal]:
    daily = payload.get("daily") or {}
    if not isinstance(daily, dict):
        raise ValueError("Open-Meteo China payload daily forecast is not an object")
    dates = daily.get("time") or []
    values = daily.get("temperature_2m_max") or []
    if not isinstance(dates, list) or not isinstance(values, list):
        raise ValueError("Open-Meteo China payload has invalid daily arrays")
    for index, date in enumerate(dates):
        if str(date) == target_date and index < len(values) and values[index] is not None:
            return str(date), Decimal(str(values[index]))
    raise ValueError(f"Open-Meteo China payload has no temperature_2m_max for {target_date}")


def _decimal_field(payload: dict[str, Any], key: str) -> Decimal:
    if key not in payload or payload[key] is None:
        raise ValueError(f"China weather payload missing {key}")
    return Decimal(str(payload[key]))


def _optional_decimal_field(payload: dict[str, Any], key: str) -> Decimal | None:
    if key not in payload or payload[key] is None:
        return None
    return Decimal(str(payload[key]))


def _parse_datetime(value: str) -> datetime:
    if not value or value == "None":
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
