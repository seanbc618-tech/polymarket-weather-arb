from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polymarket_weather_arb.adapters.http_client import build_httpx_client
from polymarket_weather_arb.adapters.http_reader import cached_json_read, safe_http_read
from polymarket_weather_arb.adapters.weather.awc_metar import fetch_awc_station_location
from polymarket_weather_arb.domain.market_eligibility import resolve_market_timezone
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_FORECAST_STALE_SECONDS = 12 * 60 * 60
D0_HOURLY_CACHE_SECONDS = 45 * 60
D0_HOURLY_STALE_SECONDS = 6 * 60 * 60


@lru_cache(maxsize=256)
def geocode_location(location: str) -> tuple[float, float, str, dict[str, Any]]:
    """Resolve a city and its verified IANA timezone for discovery and forecasting."""
    normalized = str(location or "").strip()
    if not normalized:
        raise ValueError("Open-Meteo geocoding requires a location")
    with build_httpx_client(timeout=20) as client:
        response = safe_http_read(
            client,
            "GET",
            GEOCODING_URL,
            params={"name": normalized, "count": 1, "language": "en"},
        )
        response.raise_for_status()
    payload = response.json()
    results = payload.get("results") or []
    if not results:
        raise ValueError(f"Open-Meteo could not geocode location: {normalized}")
    result = results[0]
    tz_name = result.get("timezone")
    if not tz_name:
        raise ValueError(f"Open-Meteo geocoding missing timezone for location: {normalized}")
    try:
        ZoneInfo(str(tz_name))
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Open-Meteo geocoding returned invalid timezone '{tz_name}' for location: {normalized}"
        ) from exc
    return float(result["latitude"]), float(result["longitude"]), str(tz_name), payload


class OpenMeteoProvider:
    name = "open-meteo"

    def fetch_forecast(
        self, market_id: str, rule: ResolutionRule
    ) -> tuple[ForecastSnapshot, dict[str, Any]]:
        if not rule.location:
            raise ValueError("Open-Meteo forecast requires a location")
        variable = rule.variable
        if variable not in {"temperature_high", "temperature_low", "precipitation", "snowfall"}:
            raise ValueError(f"unsupported Open-Meteo variable: {variable}")
        latitude, longitude, tz_name, geo_payload, coordinate_source = self._forecast_location(rule)
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": tz_name,
            "forecast_days": 8,
            # Around UTC midnight, the Americas can still be trading the prior
            # local weather day. Keep that day in the response so a valid live
            # market does not fail analysis solely because UTC advanced first.
            "past_days": 1,
            "temperature_unit": "fahrenheit" if (rule.unit or "F") == "F" else "celsius",
        }
        daily_field = _daily_field(variable)
        params["daily"] = daily_field
        now = datetime.now(timezone.utc)
        cache_ttl = _forecast_cache_ttl_seconds(rule, tz_name, now=now)
        with build_httpx_client(timeout=20) as client:
            payload, fetched_at, cache_status = cached_json_read(
                client,
                FORECAST_URL,
                cache_namespace="open-meteo-daily",
                ttl_seconds=cache_ttl,
                stale_if_error_seconds=DAILY_FORECAST_STALE_SECONDS,
                params=params,
            )
        daily = payload.get("daily") or {}
        times = daily.get("time") or []
        values = daily.get(daily_field) or []
        if not times or not values:
            raise ValueError(f"Open-Meteo returned no daily {daily_field} forecast")
        index = _select_day_index(times, rule.window_start)
        # Parse the local date string and attach the local timezone.
        # e.g., "2026-07-13" -> 2026-07-13 00:00:00 local time
        valid_time = datetime.fromisoformat(times[index]).replace(tzinfo=ZoneInfo(tz_name))
        value = Decimal(str(values[index]))
        unit = _unit_for(variable, rule.unit)
        snapshot = ForecastSnapshot(
            market_id=market_id,
            provider=self.name,
            location=rule.location,
            station=rule.station,
            variable=variable or daily_field,
            value=value,
            lower_value=None,
            upper_value=None,
            unit=unit,
            issue_time=fetched_at,
            valid_time=valid_time,
            fetched_at=fetched_at,
        )
        return snapshot, {
            "target_date": rule.window_start[:10] if rule.window_start else None,
            "geocoding": geo_payload,
            "latitude": latitude,
            "longitude": longitude,
            "timezone": tz_name,
            "coordinate_source": coordinate_source,
            "forecast_station": rule.station,
            "forecast": payload,
            "provider_cache_status": cache_status,
            "provider_cache_ttl_seconds": cache_ttl,
            "source_grade": "research_forecast",
            "official_signal": False,
        }

    def _geocode(self, location: str) -> tuple[float, float, str, dict[str, Any]]:
        return geocode_location(location)

    def _forecast_location(
        self, rule: ResolutionRule
    ) -> tuple[float, float, str, dict[str, Any], str]:
        station = str(rule.station or "").strip().upper()
        if station:
            latitude, longitude, station_payload = fetch_awc_station_location(station)
            timezone_name = str(getattr(rule, "settlement_timezone", "") or "")
            timezone_name = timezone_name or resolve_market_timezone(
                title=rule.raw_text, location_hint=station
            )
            if not timezone_name:
                raise ValueError(f"timezone is unknown for forecast station {station}")
            try:
                ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(
                    f"invalid timezone '{timezone_name}' for forecast station {station}"
                ) from exc
            return (
                latitude,
                longitude,
                timezone_name,
                {"station_info": station_payload},
                "awc_stationinfo",
            )
        latitude, longitude, timezone_name, payload = self._geocode(str(rule.location))
        return latitude, longitude, timezone_name, payload, "city_geocode"

    def fetch_hourly_context(
        self,
        *,
        latitude: float,
        longitude: float,
        timezone_name: str,
        target_date: str,
        unit: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Fetch one compact local-day hourly trajectory for D0 conditioning."""
        zone = ZoneInfo(timezone_name)
        local_now = now.astimezone(zone)
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone_name,
            "start_date": target_date,
            "end_date": target_date,
            "temperature_unit": "fahrenheit" if unit.upper() == "F" else "celsius",
            "hourly": "temperature_2m,cloud_cover,shortwave_radiation,wind_speed_10m",
        }
        with build_httpx_client(timeout=20) as client:
            payload, fetched_at, cache_status = cached_json_read(
                client,
                FORECAST_URL,
                cache_namespace="open-meteo-d0-hourly",
                ttl_seconds=D0_HOURLY_CACHE_SECONDS,
                stale_if_error_seconds=D0_HOURLY_STALE_SECONDS,
                params=params,
            )
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        temperatures = hourly.get("temperature_2m") or []
        if not times or len(times) != len(temperatures):
            raise ValueError("Open-Meteo returned no usable D0 hourly temperatures")
        records: list[dict[str, Any]] = []
        for index, raw_time in enumerate(times):
            if temperatures[index] is None:
                continue
            local_time = datetime.fromisoformat(str(raw_time)).replace(tzinfo=zone)
            records.append(
                {
                    "time": local_time.isoformat(),
                    "temperature": float(temperatures[index]),
                    "cloud_cover": _hourly_value(hourly, "cloud_cover", index),
                    "shortwave_radiation": _hourly_value(hourly, "shortwave_radiation", index),
                    "wind_speed": _hourly_value(hourly, "wind_speed_10m", index),
                }
            )
        if not records:
            raise ValueError("Open-Meteo returned an empty D0 hourly trajectory")
        remaining = [
            record
            for record in records
            if datetime.fromisoformat(record["time"])
            >= local_now.replace(minute=0, second=0, microsecond=0)
        ]
        if not remaining:
            remaining = [records[-1]]
        peak = max(remaining, key=lambda record: record["temperature"])
        peak_time = datetime.fromisoformat(peak["time"])
        all_day_peak = max(records, key=lambda record: record["temperature"])
        return {
            "source": "open-meteo-hourly",
            "source_grade": "research_forecast",
            "timezone": timezone_name,
            "target_date": target_date,
            "fetched_at": fetched_at.isoformat(),
            "provider_cache_status": cache_status,
            "provider_cache_ttl_seconds": D0_HOURLY_CACHE_SECONDS,
            "local_now": local_now.isoformat(),
            "unit": unit.upper(),
            "remaining_peak": str(peak["temperature"]),
            "remaining_peak_time": peak["time"],
            "all_day_forecast_peak": str(all_day_peak["temperature"]),
            "all_day_forecast_peak_time": all_day_peak["time"],
            "hours_to_remaining_peak": str(
                max(0.0, (peak_time - local_now).total_seconds() / 3600)
            ),
            "post_forecast_peak": local_now > datetime.fromisoformat(all_day_peak["time"]),
            "peak_cloud_cover": peak["cloud_cover"],
            "peak_shortwave_radiation": peak["shortwave_radiation"],
            "peak_wind_speed": peak["wind_speed"],
            "records": records,
        }


def _daily_field(variable: str | None) -> str:
    if variable == "temperature_low":
        return "temperature_2m_min"
    if variable == "temperature_high":
        return "temperature_2m_max"
    if variable == "precipitation":
        return "precipitation_sum"
    if variable == "snowfall":
        return "snowfall_sum"
    raise ValueError(f"unsupported variable: {variable}")


def _forecast_cache_ttl_seconds(
    rule: ResolutionRule,
    timezone_name: str,
    *,
    now: datetime,
) -> int:
    """Refresh D0 more often while respecting the 6-12 hour model cadence."""
    raw_target = getattr(rule, "window_start", None) or getattr(rule, "target_date", None)
    try:
        target_day = datetime.fromisoformat(str(raw_target)[:10]).date()
        local_day = now.astimezone(ZoneInfo(timezone_name)).date()
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return 6 * 60 * 60
    return 2 * 60 * 60 if target_day == local_day else 6 * 60 * 60


def _unit_for(variable: str | None, requested: str | None) -> str:
    if variable and variable.startswith("temperature"):
        return requested or "F"
    if variable == "snowfall":
        return "cm"
    return "mm"


def _select_day_index(times: list[str], window_start: str | None) -> int:
    if not window_start:
        return 0
    normalized = window_start[:10]
    for index, day in enumerate(times):
        if day == normalized:
            return index
    raise ValueError(f"forecast does not include target day {normalized}")


def _hourly_value(hourly: dict[str, Any], field: str, index: int) -> float | None:
    values = hourly.get(field)
    if not isinstance(values, list) or index >= len(values) or values[index] is None:
        return None
    return float(values[index])
