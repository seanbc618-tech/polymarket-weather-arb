from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from polymarket_weather_arb.adapters.http_client import build_httpx_client
from polymarket_weather_arb.adapters.http_reader import cached_json_read, safe_http_read
from polymarket_weather_arb.domain.global_temperature_bucket import GlobalTemperatureBucketRule
from polymarket_weather_arb.domain.market_eligibility import resolve_market_timezone
from polymarket_weather_arb.domain.weather import (
    ForecastSnapshot,
    WeatherObservation,
    normalize_value,
)

AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"
AWC_TAF_URL = "https://aviationweather.gov/api/data/taf"
AWC_STATION_INFO_URL = "https://aviationweather.gov/api/data/stationinfo"
AWC_TAF_CACHE_TTL_SECONDS = 30 * 60
AWC_TAF_STALE_SECONDS = 12 * 60 * 60
_TAF_TEMPERATURE = re.compile(
    r"\b(?P<kind>TX|TN)(?P<negative>M)?(?P<value>\d{2})/"
    r"(?P<day>\d{2})(?P<hour>\d{2})Z\b"
)


@lru_cache(maxsize=128)
def fetch_awc_station_location(station: str) -> tuple[float, float, dict[str, Any]]:
    """Resolve the exact ICAO observation site used by station-based markets."""
    station = str(station or "").strip().upper()
    if len(station) != 4 or not station.isalpha():
        raise ValueError("AWC station lookup requires an exact four-letter ICAO station")
    headers = {
        "User-Agent": "polymarket-weather-arb/awc-station (weather market research)",
        "Accept": "application/json",
    }
    with build_httpx_client(timeout=20) as client:
        response = safe_http_read(
            client,
            "GET",
            AWC_STATION_INFO_URL,
            headers=headers,
            params={"ids": station, "format": "json"},
        )
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"AWC returned malformed station metadata for {station}")
    match = next(
        (
            item
            for item in payload
            if isinstance(item, dict)
            and str(item.get("icaoId") or item.get("id") or "").upper() == station
        ),
        None,
    )
    if match is None or match.get("lat") is None or match.get("lon") is None:
        raise ValueError(f"AWC returned no exact coordinates for station {station}")
    return float(match["lat"]), float(match["lon"]), dict(match)


class AwcMetarProvider:
    """Worldwide exact-station METAR observations from AviationWeather.gov."""

    name = "awc-metar"

    def fetch_observation(
        self,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
    ) -> tuple[WeatherObservation, dict[str, Any]]:
        station = str(rule.station or "").strip().upper()
        if len(station) != 4 or not station.isalpha():
            raise ValueError("AWC observation requires the exact four-letter ICAO station")
        if rule.variable not in {"temperature_high", "temperature_low"}:
            raise ValueError(f"unsupported AWC observation variable: {rule.variable}")
        timezone_name = str(rule.settlement_timezone or "") or resolve_market_timezone(
            title=rule.raw_text, location_hint=station
        )
        if not timezone_name:
            raise ValueError(f"timezone is unknown for AWC station {station}")
        zone = ZoneInfo(timezone_name)
        target_date = str(rule.target_date or "")[:10]
        if not target_date:
            raise ValueError("AWC observation requires a target date")

        headers = {
            "User-Agent": "polymarket-weather-arb/awc-metar (weather market research)",
            "Accept": "application/json",
        }
        with build_httpx_client(timeout=20) as client:
            response = safe_http_read(
                client,
                "GET",
                AWC_METAR_URL,
                headers=headers,
                params={"ids": station, "format": "json", "hours": 24},
            )
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("AWC returned a malformed METAR response")

        records: list[tuple[Decimal, datetime, str | None]] = []
        station_coordinates: dict[str, float] | None = None
        for item in payload:
            if not isinstance(item, dict) or str(item.get("icaoId") or "").upper() != station:
                continue
            if item.get("lat") is not None and item.get("lon") is not None:
                station_coordinates = {
                    "latitude": float(item["lat"]),
                    "longitude": float(item["lon"]),
                }
            observed_at = _awc_datetime(item)
            if (
                observed_at is None
                or observed_at.astimezone(zone).date().isoformat() != target_date
            ):
                continue
            raw_temp = item.get("temp")
            if raw_temp is None:
                continue
            records.append((Decimal(str(raw_temp)), observed_at, item.get("rawOb")))
        if not records:
            raise ValueError(f"AWC returned no target-day METAR temperatures for {station}")

        selected = max(records, key=lambda item: item[0])
        if rule.variable == "temperature_low":
            selected = min(records, key=lambda item: item[0])
        target_unit = rule.unit or "C"
        value = normalize_value(selected[0], rule.variable, "C", target_unit)
        fetched_at = datetime.now(timezone.utc)
        observation = WeatherObservation(
            market_id=market_id,
            provider=self.name,
            station=station,
            variable=rule.variable,
            value=value,
            unit=target_unit,
            observed_at=selected[1],
            quality_status="AWC",
            fetched_at=fetched_at,
        )
        compact = [
            {
                "timestamp": observed_at.isoformat(),
                "value": str(temp),
                "unit": "C",
                "quality_status": "AWC",
                "raw_metar": raw_metar,
            }
            for temp, observed_at, raw_metar in sorted(records, key=lambda item: item[1])
        ]
        return observation, {
            "source": "awc-metar",
            "station": station,
            "target_date": target_date,
            "timezone": timezone_name,
            "station_coordinates": station_coordinates,
            "source_grade": "settlement_observation",
            "official_signal": True,
            "settlement_source": False,
            "settlement_proxy": True,
            "settlement_provider": "wunderground",
            "settlement_alignment": "exact_station_metar_reports",
            "observation_count": len(compact),
            "sample_count": len(compact),
            "extrema_method": "max_of_exact_station_metar_samples_in_local_day",
            "extrema_source": "aviationweather_metar_api",
            "latest_observation_at": max(item[1] for item in records).isoformat(),
            "observations": compact,
            "warnings": [
                "METAR extrema are a settlement proxy, not Wunderground's final daily summary"
            ],
        }


class AwcTafProvider:
    """Exact-airport TAF maximum/minimum temperature guidance.

    TAF is a terminal forecast, not the settlement product and not a complete
    daily-temperature model. Its TX/TN groups are therefore one bounded
    station-aligned pricing reference alongside the numerical models.
    """

    name = "awc-taf"
    source_grade = "official_forecast"

    def fetch_forecast(
        self,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
    ) -> tuple[ForecastSnapshot, dict[str, Any]]:
        station = str(rule.station or "").strip().upper()
        if len(station) != 4 or not station.isalpha():
            raise ValueError("AWC TAF requires the exact four-letter ICAO station")
        if rule.variable not in {"temperature_high", "temperature_low"}:
            raise ValueError(f"unsupported AWC TAF variable: {rule.variable}")
        timezone_name = str(rule.settlement_timezone or "") or resolve_market_timezone(
            title=rule.raw_text,
            location_hint=station,
        )
        if not timezone_name:
            raise ValueError(f"timezone is unknown for AWC TAF station {station}")
        zone = ZoneInfo(timezone_name)
        target_date = str(rule.target_date or "")[:10]
        if not target_date:
            raise ValueError("AWC TAF requires a target date")

        headers = {
            "User-Agent": "polymarket-weather-arb/awc-taf (weather market research)",
            "Accept": "application/json",
        }
        with build_httpx_client(timeout=20) as client:
            payload, fetched_at, cache_status = cached_json_read(
                client,
                AWC_TAF_URL,
                cache_namespace="awc-taf",
                ttl_seconds=AWC_TAF_CACHE_TTL_SECONDS,
                stale_if_error_seconds=AWC_TAF_STALE_SECONDS,
                headers=headers,
                params={"ids": station, "format": "json"},
            )
        if not isinstance(payload, list):
            raise ValueError(f"AWC returned malformed TAF data for {station}")
        report = next(
            (
                item
                for item in payload
                if isinstance(item, dict)
                and str(item.get("icaoId") or "").strip().upper() == station
                and int(item.get("mostRecent", 1) or 0) == 1
            ),
            None,
        )
        if report is None:
            raise ValueError(f"AWC returned no current exact-station TAF for {station}")
        issue_time = _taf_issue_time(report)
        if issue_time is None:
            raise ValueError(f"AWC TAF has no parseable issue time for {station}")
        raw_taf = str(report.get("rawTAF") or "").strip()
        kind = "TX" if rule.variable == "temperature_high" else "TN"
        candidates = _taf_temperature_candidates(
            raw_taf,
            kind=kind,
            issue_time=issue_time,
            zone=zone,
            target_date=target_date,
            valid_from=_epoch_datetime(report.get("validTimeFrom")),
            valid_to=_epoch_datetime(report.get("validTimeTo")),
        )
        if not candidates:
            raise ValueError(
                f"AWC TAF for {station} has no {kind} group for local date {target_date}"
            )
        selected = max(candidates, key=lambda item: item[0])
        if kind == "TN":
            selected = min(candidates, key=lambda item: item[0])
        target_unit = str(rule.unit or "C").upper()
        value = normalize_value(selected[0], rule.variable, "C", target_unit)
        snapshot = ForecastSnapshot(
            provider=self.name,
            variable=rule.variable,
            value=value,
            unit=target_unit,
            issue_time=issue_time,
            valid_time=selected[1],
            market_id=market_id,
            location=rule.location,
            station=station,
            fetched_at=fetched_at,
        )
        return snapshot, {
            "provider": self.name,
            "source_grade": self.source_grade,
            "decision_role": "pricing_reference",
            "station": station,
            "target_date": target_date,
            "timezone": timezone_name,
            "value": float(value),
            "unit": target_unit,
            "temperature_group": kind,
            "issue_time": issue_time.isoformat(),
            "valid_time": selected[1].isoformat(),
            "provider_cache_status": cache_status,
            "settlement_source": False,
            "settlement_alignment": "exact_airport_terminal_forecast",
            "raw_taf": raw_taf,
            "warning": (
                "TAF TX/TN is one station-aligned forecast vote, not a daily-summary "
                "settlement value"
            ),
        }


def _awc_datetime(item: dict[str, Any]) -> datetime | None:
    obs_time = item.get("obsTime")
    if isinstance(obs_time, (int, float)):
        return datetime.fromtimestamp(obs_time, tz=timezone.utc)
    for key in ("reportTime", "receiptTime"):
        raw = item.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _taf_issue_time(item: dict[str, Any]) -> datetime | None:
    for key in ("issueTime", "bulletinTime", "dbPopTime"):
        raw = item.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return _epoch_datetime(item.get("validTimeFrom"))


def _epoch_datetime(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _taf_temperature_candidates(
    raw_taf: str,
    *,
    kind: str,
    issue_time: datetime,
    zone: ZoneInfo,
    target_date: str,
    valid_from: datetime | None,
    valid_to: datetime | None,
) -> list[tuple[Decimal, datetime]]:
    candidates: list[tuple[Decimal, datetime]] = []
    for match in _TAF_TEMPERATURE.finditer(raw_taf):
        if match.group("kind") != kind:
            continue
        when = _taf_day_hour(
            issue_time,
            day=int(match.group("day")),
            hour=int(match.group("hour")),
            valid_from=valid_from,
            valid_to=valid_to,
        )
        if when is None or when.astimezone(zone).date().isoformat() != target_date:
            continue
        value = Decimal(match.group("value"))
        if match.group("negative"):
            value = -value
        candidates.append((value, when))
    return candidates


def _taf_day_hour(
    issue_time: datetime,
    *,
    day: int,
    hour: int,
    valid_from: datetime | None,
    valid_to: datetime | None,
) -> datetime | None:
    if day < 1 or day > 31 or hour < 0 or hour > 24:
        return None
    issue_time = issue_time.astimezone(timezone.utc)
    candidates: list[datetime] = []
    for offset in (-1, 0, 1):
        year, month = _shift_month(issue_time.year, issue_time.month, offset)
        if day > calendar.monthrange(year, month)[1]:
            continue
        candidate = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(hours=hour)
        candidates.append(candidate)
    if not candidates:
        return None
    in_window = [
        candidate
        for candidate in candidates
        if (valid_from is None or candidate >= valid_from - timedelta(hours=1))
        and (valid_to is None or candidate <= valid_to + timedelta(hours=1))
    ]
    pool = in_window or candidates
    return min(pool, key=lambda candidate: abs(candidate - issue_time))


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    zero_based = year * 12 + (month - 1) + offset
    return divmod(zero_based, 12)[0], divmod(zero_based, 12)[1] + 1
