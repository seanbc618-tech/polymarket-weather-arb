"""Shared market orderability eligibility for candidate selection and discovery.

Authoritative signals that a market cannot accept a new order:
- Gamma/raw ``closed=true``
- ``active=false``
- ``acceptingOrders=false``
- ``enableOrderBook=false``
- explicit closed time, or end time when current accepting status is unavailable
- parsed target/event date before the **local weather day** for the market city
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polymarket_weather_arb.domain.rules import event_date_from_market_title

# Fallback city -> IANA timezone when station mapping is unavailable.
_CITY_TIMEZONES: dict[str, str] = {
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "chicago": "America/Chicago",
    "miami": "America/New_York",
    "dallas": "America/Chicago",
    "atlanta": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "london": "Europe/London",
    "seoul": "Asia/Seoul",
    "shanghai": "Asia/Shanghai",
    "wuhan": "Asia/Shanghai",
    "chengdu": "Asia/Shanghai",
    "qingdao": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "tokyo": "Asia/Tokyo",
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "hk": "Asia/Hong_Kong",
}


@dataclass(frozen=True)
class MarketEligibility:
    orderable: bool
    reason: str | None = None

    @property
    def terminal_status(self) -> str | None:
        """Candidate status when not orderable; None when still selectable."""
        if self.orderable:
            return None
        return "expired"


def evaluate_market_orderability(
    *,
    raw_payload: dict[str, Any] | None,
    title: str | None = None,
    close_time: str | None = None,
    now: datetime | None = None,
    today: date | None = None,
    check_target_date: bool = True,
    location_hint: str | None = None,
    timezone_name: str | None = None,
) -> MarketEligibility:
    """Return whether a market may accept a new order from any selection path.

    Target-date comparison uses the market city/station **local calendar day**,
    not UTC midnight. Discovery lifecycle transitions should pass
    ``check_target_date=False`` so only exchange closed/accepting/end-time
    signals expire a candidate.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    payload = raw_payload if isinstance(raw_payload, dict) else {}

    closed = _truthy(payload.get("closed"))
    if closed:
        return MarketEligibility(False, "market closed=true")

    if "active" in payload and not _truthy(payload.get("active")):
        return MarketEligibility(False, "active=false")

    accepting: bool | None = None
    if "acceptingOrders" in payload:
        accepting = _truthy(payload.get("acceptingOrders"))
    elif "accepting_orders" in payload:
        accepting = _truthy(payload.get("accepting_orders"))
    if accepting is False:
        return MarketEligibility(False, "acceptingOrders=false")

    if "enableOrderBook" in payload and not _truthy(payload.get("enableOrderBook")):
        return MarketEligibility(False, "enableOrderBook=false")
    if "enable_order_book" in payload and not _truthy(payload.get("enable_order_book")):
        return MarketEligibility(False, "enableOrderBook=false")

    closed_at = _parse_datetime(payload.get("closedTime") or payload.get("closed_time"))
    if closed_at is not None and closed_at <= current:
        return MarketEligibility(False, f"closedTime in the past ({closed_at.isoformat()})")

    # Gamma endDate is the event horizon and can be earlier than the period in
    # which CLOB still explicitly accepts orders. Use it only as a fallback
    # when no current acceptingOrders signal is available.
    if accepting is None:
        end_raw = (
            close_time
            or payload.get("endDate")
            or payload.get("end_date")
            or payload.get("close_time")
        )
        end_at = _parse_datetime(end_raw)
        if end_at is not None and end_at <= current:
            return MarketEligibility(False, f"close_time in the past ({end_at.isoformat()})")

    if check_target_date:
        # Only apply target-date expiry when the local weather day is known.
        # Unknown city/timezone must NOT fall back to UTC and expire early.
        if today is not None:
            local_day: date | None = today
        else:
            local_day = try_local_weather_day(
                title=title,
                location_hint=location_hint,
                timezone_name=timezone_name or _timezone_from_payload(payload),
                now=current,
            )
        if local_day is not None:
            event_day = event_date_from_market_title(title or "", today=local_day)
            if event_day is not None and event_day < local_day:
                return MarketEligibility(
                    False,
                    f"target date {event_day.isoformat()} is before local day {local_day.isoformat()}",
                )

    return MarketEligibility(True, None)


def is_market_orderable(
    *,
    raw_payload: dict[str, Any] | None,
    title: str | None = None,
    close_time: str | None = None,
    now: datetime | None = None,
    today: date | None = None,
    check_target_date: bool = True,
    location_hint: str | None = None,
    timezone_name: str | None = None,
) -> bool:
    return evaluate_market_orderability(
        raw_payload=raw_payload,
        title=title,
        close_time=close_time,
        now=now,
        today=today,
        check_target_date=check_target_date,
        location_hint=location_hint,
        timezone_name=timezone_name,
    ).orderable


def try_local_weather_day(
    *,
    title: str | None = None,
    location_hint: str | None = None,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> date | None:
    """Local calendar day when timezone is known; None if it cannot be confirmed."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    tz_name = timezone_name or resolve_market_timezone(title=title, location_hint=location_hint)
    if not tz_name:
        return None
    try:
        local = current.astimezone(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        return None
    return local.date()


def local_weather_day(
    *,
    title: str | None = None,
    location_hint: str | None = None,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> date:
    """Calendar day for weather eligibility in the market's local timezone.

    Prefer ``try_local_weather_day`` when unknown cities must not default to UTC.
    This helper keeps a UTC fallback only for callers that need a non-optional date
    for ranking/display — eligibility never uses that fallback for expiry.
    """
    resolved = try_local_weather_day(
        title=title,
        location_hint=location_hint,
        timezone_name=timezone_name,
        now=now,
    )
    if resolved is not None:
        return resolved
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).date()


def resolve_market_timezone(
    *,
    title: str | None = None,
    location_hint: str | None = None,
) -> str | None:
    """Best-effort IANA timezone for a weather market title/city.

    Returns None when the city/station timezone cannot be confirmed so callers
    can skip target-date expiry instead of incorrectly using UTC.
    """
    mapping = _station_city_timezone_index()
    for raw in (location_hint, title):
        if not raw:
            continue
        text = str(raw).strip().lower()
        if text in mapping:
            return mapping[text]
        for key, tz in mapping.items():
            if _contains_location_alias(text, key):
                return tz
        for city, tz in _CITY_TIMEZONES.items():
            if _contains_location_alias(text, city):
                return tz
    # Title pattern: "temperature in Chicago"
    if title:
        match = re.search(r"temperature in ([A-Za-z .'-]+?)(?:\s+be\b|\s+on\b|\?|$)", title, re.I)
        if match:
            city = match.group(1).strip().lower()
            if city in _CITY_TIMEZONES:
                return _CITY_TIMEZONES[city]
            if city in mapping:
                return mapping[city]
            for key, tz in _CITY_TIMEZONES.items():
                if _contains_location_alias(city, key):
                    return tz
    return None


def _contains_location_alias(text: str, alias: str) -> bool:
    normalized = str(alias or "").strip().lower()
    if not normalized:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])",
            str(text or "").lower(),
        )
    )


@lru_cache(maxsize=1)
def _station_city_timezone_index() -> dict[str, str]:
    path = Path(__file__).resolve().parent.parent / "noaa_station_mapping.json"
    index: dict[str, str] = {k: v for k, v in _CITY_TIMEZONES.items()}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return index
    mappings = payload.get("mappings") if isinstance(payload, dict) else None
    station_timezones = payload.get("station_timezones") if isinstance(payload, dict) else None
    if isinstance(station_timezones, dict):
        for station, timezone_name in station_timezones.items():
            if station and timezone_name:
                index[str(station).strip().lower()] = str(timezone_name)
    if not isinstance(mappings, dict):
        return index
    for key, entry in mappings.items():
        if not isinstance(entry, dict):
            continue
        tz = entry.get("timezone")
        if not tz:
            continue
        index[str(key).strip().lower()] = str(tz)
        city = entry.get("city")
        if city:
            index[str(city).strip().lower()] = str(tz)
        for alias in entry.get("aliases") or []:
            index[str(alias).strip().lower()] = str(tz)
        station = entry.get("noaa_station")
        if station:
            index[str(station).strip().lower()] = str(tz)
    return index


def _timezone_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("timezone", "settlementTimezone", "settlement_timezone", "eventTimezone"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"", "0", "false", "no", "off", "null", "none"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return bool(text)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit() or (text.replace(".", "", 1).isdigit() and text.count(".") <= 1):
            try:
                ts = float(text)
                if ts > 1e12:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
