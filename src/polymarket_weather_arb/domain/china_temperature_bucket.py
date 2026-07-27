from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

CITY_ALIASES = {
    "qingdao": ("Qingdao", "青岛", "ZSQD"),
    "chengdu": ("Chengdu", "成都", "ZUUU"),
    "shanghai": ("Shanghai", "上海", "ZSPD"),
    "wuhan": ("Wuhan", "武汉", "ZHHH"),
}

SOURCE_PATTERNS = {
    "Wunderground": re.compile(r"\bWunderground\b|wunderground\.com", re.I),
    "CMA": re.compile(r"\bCMA\b|China Meteorological Administration|中国气象局", re.I),
    "NMC": re.compile(r"\bNMC\b|National Meteorological Center|中央气象台", re.I),
    "weather.com.cn": re.compile(r"weather\.com\.cn|中国天气网", re.I),
}

VARIABLE_PATTERNS = {
    "temperature_low": re.compile(r"\b(?:low|lowest|min(?:imum)?)\b|最低气温|低温", re.I),
    "temperature_high": re.compile(r"\b(?:high|highest|max(?:imum)?)\b|最高气温|高温", re.I),
}


@dataclass(frozen=True)
class ChinaTemperatureBucketRule:
    raw_text: str
    city: str | None
    city_cn: str | None
    station_id: str | None
    source: str | None
    variable: str | None
    bucket_center_c: Decimal | None
    bucket_lower_c: Decimal | None
    bucket_upper_c: Decimal | None
    target_date: str | None
    settlement_timezone: str
    confidence: float
    tradable: bool
    rejection_reason: str | None


def parse_china_temperature_bucket_rule(
    title: str, description: str | None = None
) -> ChinaTemperatureBucketRule:
    raw_text = f"{title}\n{description or ''}".strip()
    text = _normalize(raw_text)
    city, city_cn, station_id = _extract_city(text)
    source = _extract_source(text)
    variable = _extract_variable(text)
    center, lower, upper = _extract_bucket(text)
    target_date = _extract_target_date(text)
    rejection_reasons = []

    if city is None:
        rejection_reasons.append("unsupported or unclear China city")
    if source is None:
        rejection_reasons.append("unclear official China weather source")
    if variable is None:
        rejection_reasons.append("unsupported or unclear temperature variable")
    elif variable != "temperature_high":
        rejection_reasons.append("unsupported or unclear temperature variable")
    if center is None or lower is None or upper is None:
        rejection_reasons.append("unclear 1C Celsius temperature bucket")
    if target_date is None:
        rejection_reasons.append("unclear target date")
    if _looks_non_celsius_bucket(text):
        rejection_reasons.append("temperature bucket must be Celsius")

    confidence = _confidence(city, source, variable, center, target_date, rejection_reasons)
    tradable = not rejection_reasons and confidence >= 0.85
    return ChinaTemperatureBucketRule(
        raw_text=raw_text,
        city=city,
        city_cn=city_cn,
        station_id=station_id,
        source=source,
        variable=variable,
        bucket_center_c=center,
        bucket_lower_c=lower,
        bucket_upper_c=upper,
        target_date=target_date,
        settlement_timezone="Asia/Shanghai",
        confidence=confidence,
        tradable=tradable,
        rejection_reason="; ".join(rejection_reasons) if rejection_reasons else None,
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("–", "-").replace("—", "-")).strip()


def _extract_city(text: str) -> tuple[str | None, str | None, str | None]:
    lower = text.lower()
    for city_key, (city, city_cn, station_id) in CITY_ALIASES.items():
        if re.search(rf"\b{re.escape(city_key)}\b", lower) or city_cn in text:
            return city, city_cn, station_id
    return None, None, None


def _extract_source(text: str) -> str | None:
    for source, pattern in SOURCE_PATTERNS.items():
        if pattern.search(text):
            return source
    return None


def _extract_variable(text: str) -> str | None:
    if VARIABLE_PATTERNS["temperature_low"].search(text):
        return "temperature_low"
    if VARIABLE_PATTERNS["temperature_high"].search(text):
        return "temperature_high"
    return None


def _extract_bucket(text: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    range_match = re.search(
        r"(?<!\d)(-?\d{1,2})(?:\.0)?\s*(?:-|to|and|至|到)\s*(-?\d{1,2})(?:\.0)?\s*(?:°\s*C|C\b|degrees?\s+Celsius|摄氏度|℃)",
        text,
        re.I,
    )
    if range_match:
        lower = Decimal(range_match.group(1))
        upper = Decimal(range_match.group(2))
        if upper < lower:
            lower, upper = upper, lower
        if upper - lower != Decimal("1"):
            return None, None, None
        center = (lower + upper) / Decimal("2")
        return center, lower, upper

    exact_match = re.search(
        r"(?<!\d)(-?\d{1,2})(?:\.0)?\s*(?:°\s*C|C\b|degrees?\s+Celsius|摄氏度|℃)(?:\s*(?:or\s+(?:below|higher)|或以下|或以上))?",
        text,
        re.I,
    )
    if exact_match:
        center = Decimal(exact_match.group(1))
        return center, center - Decimal("0.5"), center + Decimal("0.5")

    return None, None, None


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

    day_first_match = re.search(
        r"\b(\d{1,2})\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?)\s+\'?(\d{2}|20\d{2})\b",
        text,
        re.I,
    )
    if day_first_match:
        day, month, year = day_first_match.groups()
        normalized_year = f"20{year}" if len(year) == 2 else year
        return _normalize_month_date(f"{month} {day}, {normalized_year}")

    slug_match = re.search(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*-\d{1,2}-(20\d{2})\b",
        text,
        re.I,
    )
    title_month_match = re.search(
        r"\b(?:on|for)\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2})\b",
        text,
        re.I,
    )
    if slug_match and title_month_match:
        return _normalize_month_date(f"{title_month_match.group(1)}, {slug_match.group(1)}")

    cn_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    if cn_match:
        year, month, day = (int(value) for value in cn_match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"

    return None


def _normalize_month_date(raw: str) -> str | None:
    value = raw.replace(".", "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _looks_non_celsius_bucket(text: str) -> bool:
    return bool(
        re.search(
            r"(?<!\d)-?\d{1,3}(?:\.0)?\s*(?:°\s*F|F\b|degrees?\s+Fahrenheit|华氏度)", text, re.I
        )
    )


def _confidence(
    city: str | None,
    source: str | None,
    variable: str | None,
    center: Decimal | None,
    target_date: str | None,
    rejection_reasons: list[str],
) -> float:
    score = Decimal("0.25")
    if city:
        score += Decimal("0.15")
    if source:
        score += Decimal("0.2")
    if variable:
        score += Decimal("0.15")
    if center is not None:
        score += Decimal("0.15")
    if target_date:
        score += Decimal("0.15")
    score -= Decimal("0.08") * len(rejection_reasons)
    return float(max(Decimal("0"), min(Decimal("1"), score)))
