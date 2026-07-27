from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True)
class ForecastSnapshot:
    provider: str
    variable: str
    value: Decimal
    unit: str
    issue_time: datetime
    valid_time: datetime
    market_id: str | None = None
    location: str | None = None
    station: str | None = None
    lower_value: Decimal | None = None
    upper_value: Decimal | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class WeatherObservation:
    provider: str
    variable: str
    value: Decimal
    unit: str
    observed_at: datetime
    market_id: str | None = None
    station: str | None = None
    quality_status: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def convert_temperature(value: Decimal, from_unit: str, to_unit: str) -> Decimal:
    source = from_unit.upper()
    target = to_unit.upper()
    if source == target:
        return value
    if source == "C" and target == "F":
        return value * Decimal("9") / Decimal("5") + Decimal("32")
    if source == "F" and target == "C":
        return (value - Decimal("32")) * Decimal("5") / Decimal("9")
    raise ValueError(f"unsupported temperature conversion: {from_unit} to {to_unit}")


def convert_precipitation(value: Decimal, from_unit: str, to_unit: str) -> Decimal:
    source = from_unit.lower()
    target = to_unit.lower()
    if source == target:
        return value
    if source == "mm" and target == "in":
        return value / Decimal("25.4")
    if source == "in" and target == "mm":
        return value * Decimal("25.4")
    if source == "cm" and target == "in":
        return value / Decimal("2.54")
    if source == "in" and target == "cm":
        return value * Decimal("2.54")
    raise ValueError(f"unsupported precipitation conversion: {from_unit} to {to_unit}")


def normalize_value(value: Decimal, variable: str, from_unit: str, to_unit: str) -> Decimal:
    if variable.startswith("temperature"):
        return convert_temperature(value, from_unit, to_unit)
    if variable in {"precipitation", "snowfall"}:
        return convert_precipitation(value, from_unit, to_unit)
    return value
