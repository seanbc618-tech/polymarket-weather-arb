"""Google Weather API adapter for optional deterministic forecast evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import threading
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polymarket_weather_arb.adapters.http_client import build_httpx_client
from polymarket_weather_arb.adapters.http_reader import safe_http_read
from polymarket_weather_arb.domain.market_eligibility import resolve_market_timezone
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot, normalize_value

FORECAST_DAYS_URL = "https://weather.googleapis.com/v1/forecast/days:lookup"
_COVERAGE_LOCK = threading.Lock()
_UNSUPPORTED_COORDINATES: set[tuple[float, float]] = set()
# Google Weather daily forecasts are currently unavailable in these regions.
# Avoid paying for and logging a known 404 on every process restart.
_UNSUPPORTED_DAILY_FORECAST_TIMEZONES = frozenset({"Asia/Seoul", "Asia/Shanghai", "Asia/Tokyo"})


class GoogleWeatherCoverageUnavailable(ValueError):
    """Raised when Google Weather does not cover a coordinate."""


class GoogleWeatherProvider:
    """Fetch a compact daily high/low forecast without persisting raw API data."""

    name = "google-weather"
    source_grade = "research_forecast"

    def __init__(self, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("Google Weather API key is required")
        self.api_key = api_key.strip()

    def fetch_forecast(
        self,
        market_id: str,
        rule: ResolutionRule,
        *,
        latitude: float,
        longitude: float,
    ) -> tuple[ForecastSnapshot, dict[str, Any]]:
        target_date = _target_date(rule)
        timezone_hint = str(getattr(rule, "settlement_timezone", "") or "")
        timezone_hint = timezone_hint or resolve_market_timezone(
            title=str(getattr(rule, "raw_text", "") or ""),
            location_hint=str(getattr(rule, "location", "") or ""),
        )
        if timezone_hint in _UNSUPPORTED_DAILY_FORECAST_TIMEZONES:
            raise GoogleWeatherCoverageUnavailable(
                f"Google Weather daily forecast coverage unavailable for {timezone_hint}"
            )
        coordinates = (round(float(latitude), 4), round(float(longitude), 4))
        params = {
            "location.latitude": latitude,
            "location.longitude": longitude,
            "days": 3,
        }
        # A header keeps the credential out of request URLs and HTTP error text.
        # The dashboard creates a fresh workflow each cycle, so unsupported
        # coverage must be cached at the adapter/process boundary. The lock also
        # prevents concurrent sibling buckets from probing the same coordinate.
        with _COVERAGE_LOCK:
            if coordinates in _UNSUPPORTED_COORDINATES:
                raise GoogleWeatherCoverageUnavailable(
                    "Google Weather coverage unavailable for location (cached)"
                )
            with build_httpx_client(timeout=20) as client:
                response = safe_http_read(
                    client,
                    "GET",
                    FORECAST_DAYS_URL,
                    headers={"X-Goog-Api-Key": self.api_key},
                    params=params,
                )
            if response.status_code == 404:
                _UNSUPPORTED_COORDINATES.add(coordinates)
                raise GoogleWeatherCoverageUnavailable(
                    "Google Weather coverage unavailable for location"
                )
        response.raise_for_status()
        response_payload = response.json()
        day = _forecast_day(response_payload, target_date)
        field = "minTemperature" if rule.variable == "temperature_low" else "maxTemperature"
        temperature = day.get(field)
        if not isinstance(temperature, dict) or temperature.get("degrees") is None:
            raise ValueError(f"Google Weather response missing {field} for {target_date}")

        source_unit = _google_unit(str(temperature.get("unit") or ""))
        target_unit = (rule.unit or source_unit).upper()
        value = normalize_value(
            Decimal(str(temperature["degrees"])),
            rule.variable or "temperature_high",
            source_unit,
            target_unit,
        )
        timezone_name = str((response_payload.get("timeZone") or {}).get("id") or "")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Google Weather response missing a valid IANA timezone") from exc

        fetched_at = datetime.now(timezone.utc)
        valid_time = datetime.fromisoformat(target_date).replace(hour=12, tzinfo=zone)
        snapshot = ForecastSnapshot(
            market_id=market_id,
            provider=self.name,
            location=rule.location,
            station=None,
            variable=rule.variable or "temperature_high",
            value=value,
            lower_value=None,
            upper_value=None,
            unit=target_unit,
            issue_time=fetched_at,
            valid_time=valid_time,
            fetched_at=fetched_at,
        )
        # Store only the derived forecast and provenance. Do not retain the full
        # vendor response until its long-term data retention terms are reviewed.
        raw_payload: dict[str, Any] = {
            "provider": self.name,
            "source_grade": self.source_grade,
            "decision_role": "pricing_reference",
            "target_date": target_date,
            "timezone": timezone_name,
            "latitude": latitude,
            "longitude": longitude,
            "variable": snapshot.variable,
            "value": float(value),
            "unit": target_unit,
        }
        return snapshot, raw_payload


def _target_date(rule: ResolutionRule) -> str:
    raw = getattr(rule, "window_start", None) or getattr(rule, "target_date", None)
    if not raw:
        raise ValueError("Google Weather forecast requires a target date")
    return str(raw)[:10]


def _forecast_day(payload: dict[str, Any], target_date: str) -> dict[str, Any]:
    for day in payload.get("forecastDays") or []:
        display = day.get("displayDate") or {}
        try:
            day_value = (
                datetime(int(display["year"]), int(display["month"]), int(display["day"]))
                .date()
                .isoformat()
            )
        except (KeyError, TypeError, ValueError):
            continue
        if day_value == target_date:
            return day
    raise ValueError(f"Google Weather response does not include target day {target_date}")


def _google_unit(value: str) -> str:
    normalized = value.upper()
    if normalized == "CELSIUS":
        return "C"
    if normalized == "FAHRENHEIT":
        return "F"
    raise ValueError(f"unsupported Google Weather temperature unit: {value or 'missing'}")
