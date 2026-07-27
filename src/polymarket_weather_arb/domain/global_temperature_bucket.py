from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from polymarket_weather_arb.domain.market_eligibility import resolve_market_timezone

SOURCE_PATTERNS = {
    "NOAA": re.compile(r"\bNOAA\b", re.I),
    "NWS": re.compile(r"\bNWS\b|National Weather Service", re.I),
    "Open-Meteo": re.compile(r"\bOpen[- ]Meteo\b", re.I),
    "Wunderground": re.compile(r"\bWunderground\b|\bWeather Underground\b|wunderground\.com", re.I),
}


@dataclass(frozen=True)
class GlobalTemperatureBucketRule:
    raw_text: str
    location: str | None
    station: str | None
    source: str | None
    variable: str | None
    bucket_center: Decimal | None
    bucket_lower: Decimal | None
    bucket_upper: Decimal | None
    unit: str | None
    target_date: str | None
    settlement_timezone: str
    confidence: float
    tradable: bool
    rejection_reason: str | None
    bucket_kind: str = "exact"

    @property
    def city(self) -> str | None:
        return self.location

    @property
    def city_cn(self) -> str | None:
        return None

    @property
    def station_id(self) -> str | None:
        return self.station

    @property
    def bucket_center_c(self) -> Decimal | None:
        return self.bucket_center

    @property
    def bucket_lower_c(self) -> Decimal | None:
        return self.bucket_lower

    @property
    def bucket_upper_c(self) -> Decimal | None:
        return self.bucket_upper

    @property
    def window_start(self) -> str | None:
        return self.target_date

    @property
    def window_end(self) -> str | None:
        return self.target_date


def parse_global_temperature_bucket_rule(
    title: str,
    description: str | None = None,
) -> GlobalTemperatureBucketRule:
    raw_text = f"{title}\n{description or ''}".strip()
    text = _normalize(raw_text)
    location = _extract_location(text)
    station = _extract_station(text)
    source = _extract_source(text)
    variable = _extract_variable(text)
    center, lower, upper, unit, bucket_kind = _extract_bucket(text)
    target_date = _extract_target_date(text)
    settlement_timezone = resolve_market_timezone(
        title=title,
        location_hint=station or location,
    )
    rejection_reasons: list[str] = []

    if location is None and station is None:
        rejection_reasons.append("unclear location/station")
    if source is None:
        rejection_reasons.append("unclear settlement source")
    if variable != "temperature_high":
        rejection_reasons.append("unsupported or unclear temperature variable")
    if center is None or lower is None or upper is None or unit is None:
        rejection_reasons.append("unclear 1 degree temperature bucket")
    if target_date is None:
        rejection_reasons.append("unclear target date")
    if settlement_timezone is None:
        rejection_reasons.append("unclear settlement timezone")

    confidence = _confidence(
        location, station, source, variable, center, target_date, rejection_reasons
    )
    tradable = not rejection_reasons and confidence >= 0.85
    return GlobalTemperatureBucketRule(
        raw_text=raw_text,
        location=location,
        station=station,
        source=source,
        variable=variable,
        bucket_center=center,
        bucket_lower=lower,
        bucket_upper=upper,
        unit=unit,
        target_date=target_date,
        settlement_timezone=settlement_timezone or "",
        confidence=confidence,
        tradable=tradable,
        rejection_reason="; ".join(rejection_reasons) if rejection_reasons else None,
        bucket_kind=bucket_kind,
    )


def with_settlement_timezone(
    rule: GlobalTemperatureBucketRule,
    timezone_name: str,
) -> GlobalTemperatureBucketRule:
    """Apply a verified IANA timezone without weakening any other parser rejection."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    normalized = str(timezone_name or "").strip()
    if not normalized:
        return rule
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        return rule

    reasons = [
        reason.strip()
        for reason in str(rule.rejection_reason or "").split(";")
        if reason.strip() and reason.strip() != "unclear settlement timezone"
    ]
    confidence = _confidence(
        rule.location,
        rule.station,
        rule.source,
        rule.variable,
        rule.bucket_center,
        rule.target_date,
        reasons,
    )
    return replace(
        rule,
        settlement_timezone=normalized,
        confidence=confidence,
        tradable=not reasons and confidence >= 0.85,
        rejection_reason="; ".join(reasons) if reasons else None,
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("–", "-").replace("—", "-")).strip()


def _extract_source(text: str) -> str | None:
    for source, pattern in SOURCE_PATTERNS.items():
        if pattern.search(text):
            return source
    return None


def _extract_station(text: str) -> str | None:
    match = re.search(r"wunderground\.com/history/daily/[^ ]+/([A-Z]{4})\b", text, re.I)
    if match:
        return match.group(1).upper()
    match = re.search(r"(?:station|airport)\s+([A-Z]{3,4})\b", text, re.I)
    if match:
        return _explicit_station_code(match.group(1))
    match = re.search(r"\b([A-Z]{3,4})\b\s+(?:station|airport|observing station)", text, re.I)
    if match:
        return _explicit_station_code(match.group(1))
    return None


def _explicit_station_code(raw: str) -> str | None:
    # Case-insensitive prose matching previously interpreted "the station" as
    # ICAO code THE. Outside an authoritative URL, require an explicit
    # uppercase code rather than accepting ordinary English words.
    value = str(raw).strip()
    if not value.isupper():
        return None
    normalized = value.upper()
    return normalized if normalized not in {"NOAA", "NWS"} else None


def _extract_location(text: str) -> str | None:
    patterns = [
        r"\bin\s+([A-Z][A-Za-z .-]+?)\s+(?:be|on\b|for\b|according\b|\?)",
        r"\bfor\s+([A-Z][A-Za-z .-]+?)\s+(?:be|on\b|according\b|\?)",
        r"\bwill\s+([A-Z][A-Za-z .-]+?)\s+(?:high|max(?:imum)?|temperature)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip(" .,-")
    return None


def _extract_variable(text: str) -> str | None:
    if re.search(r"\b(?:low|lowest|min(?:imum)?)\b", text, re.I):
        return "temperature_low"
    if re.search(r"\b(?:high|highest|max(?:imum)?|temperature|temp)\b", text, re.I):
        return "temperature_high"
    return None


def _extract_bucket(
    text: str,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, str | None, str]:
    range_match = re.search(
        r"(?<!\d)(-?\d{1,3})(?:\.0)?\s*(?:-|to|and)\s*(-?\d{1,3})(?:\.0)?\s*(?:°\s*)?([CF])\b",
        text,
        re.I,
    )
    if range_match:
        lower = Decimal(range_match.group(1))
        upper = Decimal(range_match.group(2))
        if upper < lower:
            lower, upper = upper, lower
        if upper - lower != Decimal("1"):
            return None, None, None, None, "unknown"
        unit = range_match.group(3).upper()
        center = (lower + upper) / Decimal("2")
        return center, lower, upper, unit, "range"

    exact_match = re.search(r"(?<!\d)(-?\d{1,3})(?:\.0)?\s*(?:°\s*)?([CF])\b", text, re.I)
    if exact_match:
        center = Decimal(exact_match.group(1))
        unit = exact_match.group(2).upper()
        suffix = text[exact_match.end() : exact_match.end() + 32]
        if re.match(r"\s*(?:or\s+)?(?:below|lower|less|under)\b", suffix, re.I):
            bucket_kind = "lower_tail"
        elif re.match(r"\s*(?:or\s+)?(?:above|higher|more|over)\b", suffix, re.I):
            bucket_kind = "upper_tail"
        else:
            bucket_kind = "exact"
        return (
            center,
            center - Decimal("0.5"),
            center + Decimal("0.5"),
            unit,
            bucket_kind,
        )

    return None, None, None, None, "unknown"


def settlement_bucket_bounds(
    rule: GlobalTemperatureBucketRule,
) -> tuple[Decimal | None, Decimal | None]:
    """Return half-open latent-temperature bounds for a whole-degree market bucket."""
    if rule.bucket_lower is None or rule.bucket_upper is None:
        return None, None
    if rule.bucket_kind == "lower_tail":
        return None, rule.bucket_upper
    if rule.bucket_kind == "upper_tail":
        return rule.bucket_lower, None
    if rule.bucket_kind == "range":
        return rule.bucket_lower - Decimal("0.5"), rule.bucket_upper + Decimal("0.5")
    return rule.bucket_lower, rule.bucket_upper


def observation_source_tolerance(unit: str | None) -> Decimal:
    """Guard comparisons between official observations and settlement feeds."""
    return Decimal("0.6") if str(unit or "").upper() == "C" else Decimal("1.0")


def settlement_bucket_contains(rule: GlobalTemperatureBucketRule, value: Decimal) -> bool:
    """Match one latent temperature against mutually exclusive half-open bounds."""
    lower, upper = settlement_bucket_bounds(rule)
    return (lower is None or value >= lower) and (upper is None or value < upper)


def _extract_target_date(text: str) -> str | None:
    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if iso_match:
        return iso_match.group(1)

    month_match = re.search(
        r"\b(?:on|for)\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s*20\d{2})",
        text,
        re.I,
    )
    if month_match:
        return _normalize_month_date(month_match.group(1))

    short_year_match = re.search(
        r"\b(\d{1,2})\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)\.?\s+'?(\d{2})\b",
        text,
        re.I,
    )
    if short_year_match:
        return _normalize_short_year_date(
            short_year_match.group(1), short_year_match.group(2), short_year_match.group(3)
        )
    return None


def _normalize_month_date(raw: str) -> str | None:
    value = raw.replace(".", "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _normalize_short_year_date(day: str, month: str, year: str) -> str | None:
    value = f"{month} {day}, 20{year}"
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _confidence(
    location: str | None,
    station: str | None,
    source: str | None,
    variable: str | None,
    center: Decimal | None,
    target_date: str | None,
    rejection_reasons: list[str],
) -> float:
    score = Decimal("0.20")
    if location or station:
        score += Decimal("0.20")
    if source:
        score += Decimal("0.20")
    if variable:
        score += Decimal("0.15")
    if center is not None:
        score += Decimal("0.15")
    if target_date:
        score += Decimal("0.15")
    score -= Decimal("0.10") * len(rejection_reasons)
    return float(max(Decimal("0"), min(Decimal("1"), score)))
