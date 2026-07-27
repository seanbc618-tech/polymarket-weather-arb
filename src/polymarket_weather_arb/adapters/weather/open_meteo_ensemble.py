"""Open-Meteo multi-model ensemble weather adapter.

The provider combines independent numerical weather models exposed by
Open-Meteo. Forecasts remain research-grade evidence; settlement observations
still come from the market's named official source.

API documentation: https://open-meteo.com/en/docs/ensemble-api
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from polymarket_weather_arb.adapters.http_client import build_httpx_client
from polymarket_weather_arb.adapters.http_reader import cached_json_read
from polymarket_weather_arb.adapters.weather.awc_metar import fetch_awc_station_location
from polymarket_weather_arb.adapters.weather.open_meteo import geocode_location
from polymarket_weather_arb.domain.ensemble_weather import EnsembleForecastSnapshot
from polymarket_weather_arb.domain.market_eligibility import resolve_market_timezone
from polymarket_weather_arb.domain.rules import ResolutionRule

ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODELS = (
    "gfs_seamless",
    "ecmwf_ifs025",
    "icon_seamless_eps",
    "gem_global",
)
GFS_MEMBERS = ["control"] + [f"member{i:02d}" for i in range(1, 31)]
_MEMBER_PREFIX = re.compile(r"^member\d+_(.+)$")
ENSEMBLE_STALE_SECONDS = 12 * 60 * 60


class OpenMeteoEnsembleProvider:
    """Fetch local-day high/low forecasts from several ensemble models."""

    name = "open-meteo-ensemble"
    source_grade = "research_forecast"

    def fetch_forecast(
        self,
        market_id: str,
        rule: ResolutionRule,
    ) -> tuple[EnsembleForecastSnapshot, dict[str, Any]]:
        if not rule.location:
            raise ValueError("Ensemble forecast requires a location")

        latitude, longitude, timezone_name, coordinate_source = self._forecast_location(rule)
        target_date = _target_date(rule)
        daily_variable = self._map_variable(rule.variable)
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": daily_variable,
            "models": ",".join(ENSEMBLE_MODELS),
            "timezone": timezone_name,
            # One response covers every active D0/D1/D2 market for this city.
            "forecast_days": 8,
            "past_days": 1,
        }

        cache_ttl = _ensemble_cache_ttl_seconds(target_date, timezone_name)
        with build_httpx_client(timeout=30) as client:
            payload, fetched_at, cache_status = cached_json_read(
                client,
                ENSEMBLE_API_URL,
                cache_namespace="open-meteo-ensemble-daily",
                ttl_seconds=cache_ttl,
                stale_if_error_seconds=ENSEMBLE_STALE_SECONDS,
                params=params,
            )

        model_members = _daily_model_members(
            payload,
            daily_variable,
            target_date=target_date,
        )
        if not model_members:
            # Backwards compatibility for stored fixtures and threshold tests
            # written against the old GFS hourly response.
            model_members = _legacy_hourly_members(payload, rule.variable)
        if not model_members:
            raise ValueError(f"No ensemble data returned for {daily_variable}")

        target_unit = (rule.unit or "F").upper()
        source_unit = _payload_temperature_unit(payload)
        converted = {
            model: [_convert_temperature(value, source_unit, target_unit) for value in members]
            for model, members in model_members.items()
            if members
        }
        flat_members = [value for members in converted.values() for value in members]
        if not flat_members:
            raise ValueError(f"No usable ensemble members returned for {daily_variable}")

        valid_time = _local_noon_utc(target_date, timezone_name)
        snapshot = EnsembleForecastSnapshot.from_members(
            market_id=market_id,
            location=rule.location,
            variable=rule.variable or "temperature_high",
            members=flat_members,
            fetched_at=fetched_at,
            raw_payload={},
            issue_time=fetched_at,
            valid_time=valid_time,
            unit=target_unit,
        )
        model_summaries = {model: _member_summary(members) for model, members in converted.items()}
        raw_payload: dict[str, Any] = {
            "source_grade": self.source_grade,
            "provider": self.name,
            "models_requested": list(ENSEMBLE_MODELS),
            "model_count": len(converted),
            "member_count": len(flat_members),
            "model_members": {
                model: [float(value) for value in members] for model, members in converted.items()
            },
            "model_summaries": model_summaries,
            "target_date": target_date,
            "timezone": timezone_name,
            "latitude": latitude,
            "longitude": longitude,
            "coordinate_source": coordinate_source,
            "forecast_station": rule.station,
            "daily_variable": daily_variable,
            "provider_cache_status": cache_status,
            "provider_cache_ttl_seconds": cache_ttl,
            "response_dates": list((payload.get("daily") or {}).get("time") or []),
            "mean": float(snapshot.mean),
            "std": float(snapshot.std),
            "agreement": float(
                max(
                    Decimal(sum(value > snapshot.mean for value in flat_members))
                    / Decimal(len(flat_members)),
                    Decimal(sum(value <= snapshot.mean for value in flat_members))
                    / Decimal(len(flat_members)),
                )
            ),
            "unit": target_unit,
        }
        snapshot = replace(snapshot, raw_payload=raw_payload)
        return snapshot, raw_payload

    def _map_variable(self, variable: str | None) -> str:
        if variable == "temperature_low":
            return "temperature_2m_min"
        return "temperature_2m_max"

    def _geocode(self, location: str) -> tuple[float, float, str]:
        latitude, longitude, timezone_name, _payload = geocode_location(location)
        return latitude, longitude, timezone_name

    def _forecast_location(self, rule: ResolutionRule) -> tuple[float, float, str, str]:
        station = str(rule.station or "").strip().upper()
        if station:
            latitude, longitude, _station_payload = fetch_awc_station_location(station)
            timezone_name = str(getattr(rule, "settlement_timezone", "") or "")
            timezone_name = timezone_name or resolve_market_timezone(
                title=rule.raw_text, location_hint=station
            )
            if not timezone_name:
                raise ValueError(f"Timezone is unknown for forecast station {station}")
            try:
                ZoneInfo(timezone_name)
            except Exception as exc:
                raise ValueError(
                    f"Invalid IANA timezone '{timezone_name}' for forecast station {station}"
                ) from exc
            return latitude, longitude, timezone_name, "awc_stationinfo"
        latitude, longitude, timezone_name = self._geocode(str(rule.location))
        return latitude, longitude, timezone_name, "city_geocode"


def _target_date(rule: ResolutionRule) -> str:
    raw = getattr(rule, "window_start", None) or getattr(rule, "target_date", None)
    if raw:
        return str(raw)[:10]
    return datetime.now(timezone.utc).date().isoformat()


def _daily_model_members(
    payload: dict[str, Any],
    variable: str,
    *,
    target_date: str | None = None,
) -> dict[str, list[Decimal]]:
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return {}
    selected_index: int | None = None
    times = daily.get("time")
    if target_date and isinstance(times, list):
        try:
            selected_index = [str(value)[:10] for value in times].index(target_date)
        except ValueError:
            return {}
    prefix = f"{variable}_"
    grouped: dict[str, list[Decimal]] = {}
    for key, values in daily.items():
        if not str(key).startswith(prefix) or not isinstance(values, list):
            continue
        suffix = str(key)[len(prefix) :]
        match = _MEMBER_PREFIX.match(suffix)
        model = match.group(1) if match else suffix
        selected_values = (
            [values[selected_index]]
            if selected_index is not None and selected_index < len(values)
            else values
        )
        valid = [Decimal(str(value)) for value in selected_values if value is not None]
        if valid:
            grouped.setdefault(model, []).extend(valid)
    return grouped


def _ensemble_cache_ttl_seconds(target_date: str, timezone_name: str) -> int:
    try:
        target_day = datetime.fromisoformat(target_date).date()
        local_day = datetime.now(ZoneInfo(timezone_name)).date()
    except (TypeError, ValueError):
        return 6 * 60 * 60
    return 2 * 60 * 60 if target_day == local_day else 6 * 60 * 60


def _legacy_hourly_members(
    payload: dict[str, Any], variable: str | None
) -> dict[str, list[Decimal]]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return {}
    members: list[Decimal] = []
    for member_name in GFS_MEMBERS:
        values = hourly.get(f"temperature_2m_{member_name}")
        if not isinstance(values, list):
            continue
        valid = [Decimal(str(value)) for value in values if value is not None]
        if not valid:
            continue
        members.append(max(valid) if variable != "temperature_low" else min(valid))
    return {"gfs_legacy": members} if members else {}


def _payload_temperature_unit(payload: dict[str, Any]) -> str:
    for units_key in ("daily_units", "hourly_units"):
        units = payload.get(units_key)
        if isinstance(units, dict):
            for key, value in units.items():
                if str(key).startswith("temperature_2m"):
                    return "F" if "°F" in str(value) else "C"
    return "C"


def _convert_temperature(value: Decimal, source_unit: str, target_unit: str) -> Decimal:
    if source_unit == target_unit:
        return value
    if source_unit == "C" and target_unit == "F":
        return value * Decimal("9") / Decimal("5") + Decimal("32")
    if source_unit == "F" and target_unit == "C":
        return (value - Decimal("32")) * Decimal("5") / Decimal("9")
    return value


def _local_noon_utc(target_date: str, timezone_name: str) -> datetime:
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = timezone.utc
    local_noon = datetime.fromisoformat(target_date).replace(hour=12, tzinfo=zone)
    return local_noon.astimezone(timezone.utc)


def _member_summary(members: list[Decimal]) -> dict[str, float | int]:
    mean = sum(members) / Decimal(len(members))
    variance = sum((value - mean) ** 2 for value in members) / Decimal(len(members))
    return {
        "count": len(members),
        "mean": float(mean),
        "std": float(variance.sqrt()),
        "min": float(min(members)),
        "max": float(max(members)),
    }
