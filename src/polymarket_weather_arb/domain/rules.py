from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal

SUPPORTED_VARIABLES = {"temperature_high", "temperature_low", "precipitation", "snowfall"}
SOURCE_PATTERNS = {
    "NOAA": re.compile(r"\bNOAA\b", re.I),
    "NWS": re.compile(r"\bNWS\b|National Weather Service", re.I),
    "Meteostat": re.compile(r"\bMeteostat\b", re.I),
    "Open-Meteo": re.compile(r"\bOpen[- ]Meteo\b", re.I),
    "Wunderground": re.compile(r"\bWunderground\b|\bWeather Underground\b", re.I),
}


@dataclass(frozen=True)
class ResolutionRule:
    raw_text: str
    location: str | None
    station: str | None
    source: str | None
    variable: str | None
    operator: str | None
    threshold: Decimal | None
    unit: str | None
    window_start: str | None
    window_end: str | None
    confidence: float
    tradable: bool
    rejection_reason: str | None


def parse_resolution_rule(title: str, description: str | None = None) -> ResolutionRule:
    raw_text = f"{title}\n{description or ''}".strip()
    text = _normalize(raw_text)
    location = _extract_location(text)
    source = _extract_source(text)
    station = _extract_station(text)
    variable, operator, threshold, unit = _extract_threshold_event(text)
    window_start, window_end = _extract_window(text)
    rejection_reasons = []

    if _looks_multi_location(text):
        rejection_reasons.append("multi-location markets are unsupported")
    if variable is None or variable not in SUPPORTED_VARIABLES:
        rejection_reasons.append("unsupported or unclear weather variable")
    if threshold is None or operator is None:
        rejection_reasons.append("unclear threshold/operator")
    if _looks_temperature_bucket(text):
        rejection_reasons.append("temperature bucket/range markets require bucket workflow")
    if location is None and station is None:
        rejection_reasons.append("unclear location/station")
    if source is None:
        rejection_reasons.append("unclear settlement source")
    if _looks_long_cycle(text):
        rejection_reasons.append("long-cycle market is unsupported")

    confidence = _confidence(location, source, variable, threshold, rejection_reasons)
    tradable = not rejection_reasons and confidence >= 0.75
    return ResolutionRule(
        raw_text=raw_text,
        location=location,
        station=station,
        source=source,
        variable=variable,
        operator=operator,
        threshold=threshold,
        unit=unit,
        window_start=window_start,
        window_end=window_end,
        confidence=confidence,
        tradable=tradable,
        rejection_reason="; ".join(rejection_reasons) if rejection_reasons else None,
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("–", "-").replace("—", "-")).strip()


def _extract_source(text: str) -> str | None:
    for source, pattern in SOURCE_PATTERNS.items():
        if pattern.search(text):
            return source
    return None


def _extract_station(text: str) -> str | None:
    match = re.search(r"(?:station|airport)\s+([A-Z]{3,4})\b", text, re.I)
    if match:
        return match.group(1).upper()
    match = re.search(r"\b([A-Z]{3,4})\b\s+(?:station|airport|observing station)", text, re.I)
    if match and match.group(1).upper() not in {"NOAA", "NWS"}:
        return match.group(1).upper()
    # Match ICAO airport codes (4 letters starting with Z for China, K for US, etc.)
    match = re.search(r"\b([A-Z]{4})\b", text)
    if match and match.group(1).upper() not in {"NOAA", "NWS", "OPEN"}:
        return match.group(1).upper()
    return None


def _extract_location(text: str) -> str | None:
    patterns = [
        r"\bin\s+([A-Z][A-Za-z .-]+?)(?:\s+on\b|\s+by\b|\s+according\b|\s+reach\b|\s+exceed\b|\?|,|$)",
        r"\bfor\s+([A-Z][A-Za-z .-]+?)(?:\s+on\b|\s+by\b|\s+according\b|\?|,|$)",
        r"\bwill\s+([A-Z][A-Za-z .-]+?)\s+(?:reach|exceed|get|record)",
        # Match "at the <City> ... Station" pattern
        r"\bat\s+the\s+([A-Z][A-Za-z .-]+?)(?:\s+station\b|\s+airport\b)",
        # Match city name before airport station patterns
        r"\b([A-Z][A-Za-z]+)\s+(?:Pudong|Heathrow|International)?\s*(?:International)?\s*Airport\s+Station\b",
        # Match "in <City> be" pattern (for temperature markets)
        r"\bin\s+([A-Z][A-Za-z]+)\s+be\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip(" .,-")
    return None


def _extract_threshold_event(
    text: str,
) -> tuple[str | None, str | None, Decimal | None, str | None]:
    temperature = re.search(
        r"(?:(?P<qualifier>high|max(?:imum)?|low|min(?:imum)?)\s*)?(?:temperature|temp)[^0-9+-]*(?:at least|above|over|exceed(?:s)?|greater than|>=|>|below|under|less than|<=|<)?\s*(?P<threshold>-?\d+(?:\.\d+)?)\s*(?:degrees?\s*)?(?P<unit>[CF]|°F|°C|Fahrenheit|Celsius)?",
        text,
        re.I,
    )
    if temperature:
        prefix = text[max(0, temperature.start() - 20) : temperature.start()].lower()
        qualifier = (temperature.group("qualifier") or prefix).lower()
        variable = (
            "temperature_low"
            if any(word in qualifier for word in ("low", "minimum", "min"))
            else "temperature_high"
        )
        operator = _extract_operator(text, temperature.start()) or ">="
        unit = _normalize_unit(temperature.group("unit")) or "F"
        return variable, operator, Decimal(temperature.group("threshold")), unit

    precipitation = re.search(
        r"(?:rain(?:fall)?|precipitation|precip)[^0-9]*(?:at least|above|over|exceed(?:s)?|greater than|>=|>)?\s*(\d+(?:\.\d+)?)\s*(inches|inch|in|mm|millimeters?)",
        text,
        re.I,
    )
    if precipitation:
        return (
            "precipitation",
            _extract_operator(text, precipitation.start()) or ">=",
            Decimal(precipitation.group(1)),
            _normalize_unit(precipitation.group(2)),
        )

    snowfall = re.search(
        r"(?:snow(?:fall)?)[^0-9]*(?:at least|above|over|exceed(?:s)?|greater than|>=|>)?\s*(\d+(?:\.\d+)?)\s*(inches|inch|in|cm|centimeters?)",
        text,
        re.I,
    )
    if snowfall:
        return (
            "snowfall",
            _extract_operator(text, snowfall.start()) or ">=",
            Decimal(snowfall.group(1)),
            _normalize_unit(snowfall.group(2)),
        )

    return None, None, None, None


def _extract_operator(text: str, index: int) -> str | None:
    window = text[max(0, index - 50) : index + 80].lower()
    if any(token in window for token in ("below", "under", "less than", "<=", "<")):
        return "<="
    if any(
        token in window
        for token in ("at least", "above", "over", "exceed", "greater than", ">=", ">")
    ):
        return ">="
    return None


def _looks_temperature_bucket(text: str) -> bool:
    if not re.search(r"\b(?:temperature|temp|high|highest|max(?:imum)?)\b", text, re.I):
        return False
    if re.search(
        r"\bbetween\s+-?\d{1,3}(?:\.0)?\s*(?:-|to|and)\s*-?\d{1,3}(?:\.0)?\s*(?:°\s*)?[CF]\b",
        text,
        re.I,
    ):
        return True
    if re.search(
        r"(?<!\d)-?\d{1,3}(?:\.0)?\s*(?:-|to|and)\s*-?\d{1,3}(?:\.0)?\s*(?:°\s*)?[CF]\b",
        text,
        re.I,
    ):
        return True
    return bool(re.search(r"\btemperature range\b", text, re.I))


def _normalize_unit(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.lower().replace("°", "").strip()
    if value in {"f", "fahrenheit"}:
        return "F"
    if value in {"c", "celsius"}:
        return "C"
    if value in {"inch", "inches", "in"}:
        return "in"
    if value in {"mm", "millimeter", "millimeters"}:
        return "mm"
    if value in {"cm", "centimeter", "centimeters"}:
        return "cm"
    return raw


def _extract_window(text: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"\b(on|for)\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)",
        text,
        re.I,
    )
    if match:
        parsed = _normalize_window_date(match.group(2))
        return parsed, parsed
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if match:
        return match.group(1), match.group(1)
    return None, None


def _normalize_window_date(raw: str) -> str:
    value = raw.replace(".", "").strip()
    if not re.search(r"\b\d{4}\b", value):
        return value
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return parsed.date().isoformat()
    return raw


def _looks_multi_location(text: str) -> bool:
    return bool(re.search(r"\b(?:and|or)\s+[A-Z][A-Za-z .-]+\s+(?:and|or)\b", text)) or bool(
        re.search(r"\bmultiple cities\b|\bany of these cities\b", text, re.I)
    )


def _looks_long_cycle(text: str) -> bool:
    return bool(re.search(r"\bseason|winter|summer|monthly|yearly|annual\b", text, re.I))


_TITLE_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def event_date_from_market_title(title: str, *, today: date | None = None) -> date | None:
    today = today or datetime.now(timezone.utc).date()
    match = re.search(
        r"on ([A-Za-z]+) (\d{1,2})(?:, (\d{4}))?\??",
        title,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    month_name = match.group(1).lower()
    month = _TITLE_MONTHS.get(month_name)
    if month is None:
        return None
    day = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else today.year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def enrich_rule_from_market_title(rule: ResolutionRule, title: str) -> ResolutionRule:
    location = rule.location
    city_match = re.search(r"temperature in (.+?) be ", title, flags=re.IGNORECASE)
    if city_match:
        location = city_match.group(1).strip()
    event_day = event_date_from_market_title(title)
    if event_day is None:
        return replace(rule, location=location)
    iso_day = event_day.isoformat()
    return replace(rule, location=location, window_start=iso_day, window_end=iso_day)


def _confidence(
    location: str | None,
    source: str | None,
    variable: str | None,
    threshold: Decimal | None,
    rejection_reasons: list[str],
) -> float:
    score = 0.25
    if location:
        score += 0.2
    if source:
        score += 0.2
    if variable:
        score += 0.15
    if threshold is not None:
        score += 0.15
    score -= 0.1 * len(rejection_reasons)
    return max(0.0, min(1.0, score))
