from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
import json
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


from polymarket_weather_arb.adapters.http_client import build_httpx_client

from polymarket_weather_arb.adapters.http_reader import safe_http_read

from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import (
    ForecastSnapshot,
    WeatherObservation,
    normalize_value,
)


class NoaaProvider:
    name = "noaa"
    _gridpoint_pattern = re.compile(r"^[A-Z]{3}/\d+,\d+$")
    _station_pattern = re.compile(r"^K[A-Z]{3}$")

    def __init__(self, station_mapping_path: Path | None = None):
        self._station_mapping = self._load_station_mapping(station_mapping_path)

    def _load_station_mapping(self, path: Path | None) -> dict[str, Any]:
        if path is None:
            # 从 src/polymarket_weather_arb/ 目录加载
            path = Path(__file__).parent.parent.parent / "noaa_station_mapping.json"
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _resolve_gridpoint(self, location: str, source: str | None) -> str:
        """解析 gridpoint，支持观测站 ID 和城市名称"""
        if source:
            # 如果 source 是观测站 ID（如 KNYC），转换为 gridpoint
            if self._station_pattern.match(source.upper()):
                return self._station_to_gridpoint(source.upper())
            # 如果 source 已经是 gridpoint 格式，直接使用
            if self._gridpoint_pattern.match(source.upper()):
                return source.upper()

        # 尝试从城市名称解析
        if location:
            gridpoint = self._location_to_gridpoint(location)
            if gridpoint:
                return gridpoint

        raise ValueError(
            "NOAA forecast requires a source (NWS gridpoint or station ID) "
            "or a recognized city name"
        )

    def _resolve_station(
        self, location: str | None, source: str | None, station: str | None
    ) -> str:
        """Resolve an NWS station ID for historical observations."""
        for candidate in (station, source):
            if candidate and self._station_pattern.match(candidate.upper()):
                return candidate.upper()

        if location:
            mappings = self._station_mapping.get("mappings", {})
            location_lower = location.lower()
            for city_data in mappings.values():
                if city_data.get("city", "").lower() == location_lower:
                    return city_data["noaa_station"]
                for alias in city_data.get("aliases", []):
                    if alias.lower() == location_lower:
                        return city_data["noaa_station"]

        raise ValueError("NOAA observation requires an NWS station ID or a recognized city name")

    def _resolve_timezone(self, station: str) -> str | None:
        """Return IANA timezone string for the station, or None if not found."""
        mappings = self._station_mapping.get("mappings", {})
        # Direct key match (e.g. "KNYC")
        entry = mappings.get(station)
        if entry and entry.get("timezone"):
            return entry["timezone"]
        # Search by noaa_station field
        for city_data in mappings.values():
            if city_data.get("noaa_station") == station and city_data.get("timezone"):
                return city_data["timezone"]
        return None

    def _station_to_gridpoint(self, station_id: str) -> str:
        """将观测站 ID 转换为 gridpoint"""
        mappings = self._station_mapping.get("mappings", {})
        for city_data in mappings.values():
            if city_data.get("noaa_station") == station_id:
                return city_data["noaa_gridpoint"]
        raise ValueError(f"Unknown NOAA station: {station_id}")

    def _location_to_gridpoint(self, location: str) -> str | None:
        """从城市名称解析 gridpoint"""
        mappings = self._station_mapping.get("mappings", {})
        location_lower = location.lower()

        for city_data in mappings.values():
            # 匹配城市名称
            if city_data.get("city", "").lower() == location_lower:
                return city_data["noaa_gridpoint"]
            # 匹配别名
            for alias in city_data.get("aliases", []):
                if alias.lower() == location_lower:
                    return city_data["noaa_gridpoint"]

        return None

    def fetch_forecast(
        self, market_id: str, rule: ResolutionRule
    ) -> tuple[ForecastSnapshot, dict[str, Any]]:
        if not rule.location:
            raise ValueError("NOAA forecast requires a location")

        gridpoint = self._resolve_gridpoint(rule.location, rule.source)
        variable = rule.variable
        if variable not in {"temperature_high", "temperature_low", "precipitation", "snowfall"}:
            raise ValueError(f"unsupported NOAA variable: {variable}")

        # 获取 NOAA/NWS forecast
        forecast_url = f"https://api.weather.gov/gridpoints/{gridpoint}/forecast"
        headers = {
            "User-Agent": "polymarket-weather-arb (contact@example.com)",
            "Accept": "application/geo+json",
        }

        with build_httpx_client(timeout=30) as client:
            response = safe_http_read(client, "GET", forecast_url, headers=headers)
            response.raise_for_status()

        payload = response.json()
        periods = payload.get("properties", {}).get("periods", [])
        if not periods:
            raise ValueError(f"NOAA returned no forecast periods for {gridpoint}")

        # 选择正确的日期
        target_date = rule.window_start
        selected_period = self._select_period(periods, target_date, variable)

        # 根据变量类型提取值
        if variable in {"temperature_high", "temperature_low"}:
            value = selected_period.get("temperature")
            if value is None:
                raise ValueError("NOAA forecast missing temperature")
            value = Decimal(str(value))
            unit = selected_period.get("temperatureUnit", "F")
        elif variable == "precipitation":
            # NOAA 返回 probabilityOfPrecipitation 和 quantitativePrecipitation
            # 优先使用 quantitativePrecipitation（累积量）
            precip = selected_period.get("quantitativePrecipitation")
            if precip and precip.get("value") is not None:
                value = Decimal(str(precip["value"]))
                unit = precip.get("unitCode", "mm").split(":")[-1]
            else:
                # 如果没有 quantitativePrecipitation，使用 probabilityOfPrecipitation
                prob_precip = selected_period.get("probabilityOfPrecipitation")
                if prob_precip and prob_precip.get("value") is not None:
                    value = Decimal(str(prob_precip["value"]))
                    unit = "%"
                else:
                    # 如果都没有，默认为 0% 降雨概率
                    value = Decimal("0")
                    unit = "%"
        elif variable == "snowfall":
            snowfall = selected_period.get("snowfallAmount")
            if snowfall and snowfall.get("value") is not None:
                value = Decimal(str(snowfall["value"]))
                unit = snowfall.get("unitCode", "cm").split(":")[-1]
            else:
                # 如果没有降雪数据，默认为 0
                value = Decimal("0")
                unit = "cm"
        else:
            raise ValueError(f"unsupported NOAA variable: {variable}")

        # 构建 ForecastSnapshot
        valid_time_str = selected_period.get("startTime", "")
        try:
            valid_time = datetime.fromisoformat(valid_time_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            valid_time = datetime.now(timezone.utc)

        snapshot = ForecastSnapshot(
            market_id=market_id,
            provider=self.name,
            location=rule.location,
            station=gridpoint,
            variable=variable,
            value=value,
            lower_value=None,
            upper_value=None,
            unit=unit,
            issue_time=datetime.now(timezone.utc),
            valid_time=valid_time,
            fetched_at=datetime.now(timezone.utc),
        )

        # NOAA/NWS forecast product is an official forecast, not a settlement observation.
        raw_payload = {
            "source": "noaa_nws",
            "gridpoint": gridpoint,
            "period": selected_period,
            "source_grade": "official_forecast",
            "official_signal": True,
        }

        return snapshot, raw_payload

    def fetch_observation(
        self, market_id: str, rule: ResolutionRule
    ) -> tuple[WeatherObservation, dict[str, Any]]:
        variable = rule.variable
        if variable not in {"temperature_high", "temperature_low"}:
            raise ValueError(f"unsupported NOAA observation variable: {variable}")

        station = self._resolve_station(rule.location, rule.source, rule.station)
        target_date = (rule.window_start or datetime.now(timezone.utc).date().isoformat())[:10]

        tz_name = self._resolve_timezone(station)
        local_start, local_end, start, end, resolved_tz, warnings = _local_day_bounds(
            target_date, tz_name
        )
        observations_url = f"https://api.weather.gov/stations/{station}/observations"
        headers = {
            "User-Agent": "polymarket-weather-arb (contact@example.com)",
            "Accept": "application/geo+json",
        }

        with build_httpx_client(timeout=30) as client:
            response = safe_http_read(
                client,
                "GET",
                observations_url,
                headers=headers,
                params={"start": start, "end": end},
            )
            response.raise_for_status()

        payload = response.json()
        features = payload.get("features", [])
        observations: list[tuple[Decimal, str, str, str | None]] = []
        for feature in features:
            properties = feature.get("properties", {})
            temperature = properties.get("temperature") or {}
            raw_value = temperature.get("value")
            if raw_value is None:
                continue
            unit = _unit_from_nws_unit_code(temperature.get("unitCode"), default="C")
            timestamp = properties.get("timestamp")
            # NWS attaches quality control to the measured quantity. Keep the
            # legacy top-level fallback for recorded fixtures and older payloads.
            quality_status = temperature.get("qualityControl") or properties.get("qualityControl")
            observations.append((Decimal(str(raw_value)), unit, timestamp, quality_status))

        if not observations:
            raise ValueError(f"NOAA returned no usable NWS temperature observations for {station}")

        if variable == "temperature_high":
            selected_value, selected_unit, observed_at_text, quality_status = max(
                observations, key=lambda item: item[0]
            )
            extrema_method = "max_of_observation_samples_in_local_window"
        else:
            selected_value, selected_unit, observed_at_text, quality_status = min(
                observations, key=lambda item: item[0]
            )
            extrema_method = "min_of_observation_samples_in_local_window"

        target_unit = rule.unit or selected_unit
        value = normalize_value(selected_value, variable, selected_unit, target_unit)
        observed_at = _parse_datetime(observed_at_text)
        observation = WeatherObservation(
            market_id=market_id,
            provider=self.name,
            station=station,
            variable=variable,
            value=value,
            unit=target_unit,
            observed_at=observed_at,
            quality_status=quality_status,
            fetched_at=datetime.now(timezone.utc),
        )
        compact_observations = [
            {
                "timestamp": ts,
                "value": str(val),
                "unit": u,
                "quality_status": qs,
            }
            for val, u, ts, qs in observations
        ]
        parsed_observation_times = [
            parsed
            for _, _, timestamp, _ in observations
            if (parsed := _parse_datetime_or_none(timestamp)) is not None
        ]
        latest_observation_at = max(parsed_observation_times, default=None)

        if len(observations) < 12:
            warnings.append(f"low observation coverage: {len(observations)} usable records")
        if not quality_status:
            warnings.append("selected observation quality is unknown")
        elif quality_status != "V":
            warnings.append(f"selected observation quality is {quality_status}")
        warnings.append(
            "extrema_method is sample-based, not official daily summary; review observations before backfill"
        )

        raw_payload = {
            "source": "nws-observation",
            "station": station,
            "target_date": target_date,
            "timezone": resolved_tz,
            "local_start": local_start.isoformat(),
            "local_end": local_end.isoformat(),
            "query_start": start,
            "query_end": end,
            "source_grade": "settlement_observation",
            "official_signal": True,
            "settlement_source": True,
            "observation_count": len(observations),
            "extrema_method": extrema_method,
            "extrema_source": "nws_observations_api",
            "sample_count": len(observations),
            "sample_extrema_value": str(selected_value),
            "sample_extrema_unit": selected_unit,
            "official_daily_value": None,
            "selected_observation": {
                "timestamp": observed_at_text,
                "value": str(selected_value),
                "unit": selected_unit,
                "quality_status": quality_status,
            },
            "latest_observation_at": (
                latest_observation_at.isoformat() if latest_observation_at else None
            ),
            "observations": compact_observations,
            "warnings": warnings,
        }
        return observation, raw_payload

    def _select_period(self, periods: list[dict], target_date: str | None, variable: str) -> dict:
        """选择正确的预报周期"""
        if not target_date:
            # 默认返回第一个周期
            return periods[0]

        target_date_normalized = target_date[:10]  # YYYY-MM-DD

        # 降雨/降雪需要找第一个匹配的周期（不区分白天/夜间）
        if variable in {"precipitation", "snowfall"}:
            for period in periods:
                start_time = period.get("startTime", "")
                if start_time[:10] == target_date_normalized:
                    return period
            return periods[0]

        # 温度需要区分白天/夜间
        for period in periods:
            start_time = period.get("startTime", "")
            if start_time[:10] == target_date_normalized:
                # 对于 temperature_low，需要找夜间周期
                if variable == "temperature_low" and period.get("isDaytime", True):
                    continue
                # 对于 temperature_high，需要找白天周期
                if variable == "temperature_high" and not period.get("isDaytime", True):
                    continue
                return period

        # 如果没找到匹配的，返回第一个
        return periods[0]


def _local_day_bounds(
    target_date: str,
    tz_name: str | None,
) -> tuple[datetime, datetime, str, str, str | None, list[str]]:
    """Compute local-day start/end as UTC query strings.

    Returns (local_start, local_end, query_start_z, query_end_z, resolved_tz, warnings).
    Falls back to UTC calendar day if tz_name is None or invalid.
    """
    target = datetime.fromisoformat(target_date).date()
    warnings: list[str] = []

    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            # Invalid IANA timezone string — fall back to UTC
            warnings.append(f"station timezone invalid: {tz_name}; UTC calendar-day window used")
            tz = None
    else:
        tz = None

    if tz is not None:
        local_start = datetime.combine(target, time.min, tzinfo=tz)
        local_end = datetime.combine(target, time.max, tzinfo=tz)
        start_utc = local_start.astimezone(timezone.utc)
        end_utc = local_end.astimezone(timezone.utc)
        start = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        end = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        resolved_tz = tz_name
    else:
        local_start = datetime.combine(target, time.min, tzinfo=timezone.utc)
        local_end = datetime.combine(target, time.max, tzinfo=timezone.utc)
        start = f"{target_date}T00:00:00Z"
        end = f"{target_date}T23:59:59Z"
        resolved_tz = None
        if not warnings:
            warnings.append("station timezone unknown; UTC calendar-day window used")

    return local_start, local_end, start, end, resolved_tz, warnings


def _unit_from_nws_unit_code(unit_code: str | None, *, default: str) -> str:
    if not unit_code:
        return default
    unit = unit_code.split(":")[-1]
    if unit == "degC":
        return "C"
    if unit == "degF":
        return "F"
    return unit


def _parse_datetime(value: str | None) -> datetime:
    return _parse_datetime_or_none(value) or datetime.now(timezone.utc)


def _parse_datetime_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
