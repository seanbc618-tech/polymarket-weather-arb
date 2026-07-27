from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot, normalize_value

MODEL_VERSION = "threshold-interval-v1"


@dataclass(frozen=True)
class ProbabilityInterval:
    lower: Decimal
    upper: Decimal
    reasons: list[str]
    model_version: str = MODEL_VERSION


def estimate_probability_interval(
    rule: ResolutionRule,
    forecast: ForecastSnapshot,
    now: datetime | None = None,
) -> ProbabilityInterval:
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    if not rule.tradable:
        return ProbabilityInterval(Decimal("0"), Decimal("1"), ["rule is not tradable"])
    if rule.threshold is None or rule.operator is None or rule.variable is None:
        return ProbabilityInterval(Decimal("0"), Decimal("1"), ["rule is incomplete"])

    value = normalize_value(
        forecast.value, rule.variable, forecast.unit, rule.unit or forecast.unit
    )
    lower_value = (
        normalize_value(
            forecast.lower_value, rule.variable, forecast.unit, rule.unit or forecast.unit
        )
        if forecast.lower_value is not None
        else value - default_uncertainty(rule.variable)
    )
    upper_value = (
        normalize_value(
            forecast.upper_value, rule.variable, forecast.unit, rule.unit or forecast.unit
        )
        if forecast.upper_value is not None
        else value + default_uncertainty(rule.variable)
    )

    if forecast.fetched_at.tzinfo is None:
        fetched_at = forecast.fetched_at.replace(tzinfo=timezone.utc)
    else:
        fetched_at = forecast.fetched_at
    age_seconds = (now - fetched_at).total_seconds()
    if age_seconds > 6 * 3600:
        reasons.append("forecast is older than six hours")
        lower_value -= default_uncertainty(rule.variable)
        upper_value += default_uncertainty(rule.variable)

    if rule.operator == ">=":
        lower = event_probability(lower_value, rule.threshold, rule.variable)
        upper = event_probability(upper_value, rule.threshold, rule.variable)
    elif rule.operator == "<=":
        lower = Decimal("1") - event_probability(upper_value, rule.threshold, rule.variable)
        upper = Decimal("1") - event_probability(lower_value, rule.threshold, rule.variable)
    else:
        return ProbabilityInterval(Decimal("0"), Decimal("1"), ["unsupported operator"])

    if rule.confidence < 0.9:
        reasons.append("rule confidence below high-confidence threshold")
        lower, upper = widen_interval(lower, upper, Decimal("0.10"))

    lower, upper = clamp_probability(lower), clamp_probability(upper)
    if lower > upper:
        lower, upper = upper, lower
    if not reasons:
        reasons.append("clear threshold event with current forecast interval")
    return ProbabilityInterval(lower=lower, upper=upper, reasons=reasons)


def event_probability(value: Decimal, threshold: Decimal, variable: str) -> Decimal:
    uncertainty = default_uncertainty(variable)
    lower_full = threshold - uncertainty
    upper_zero = threshold + uncertainty
    if value <= lower_full:
        return Decimal("0")
    if value >= upper_zero:
        return Decimal("1")
    return (value - lower_full) / (upper_zero - lower_full)


def default_uncertainty(variable: str) -> Decimal:
    if variable.startswith("temperature"):
        return Decimal("4")
    if variable == "precipitation":
        return Decimal("0.20")
    if variable == "snowfall":
        return Decimal("1.5")
    return Decimal("1")


def widen_interval(lower: Decimal, upper: Decimal, amount: Decimal) -> tuple[Decimal, Decimal]:
    return clamp_probability(lower - amount), clamp_probability(upper + amount)


def clamp_probability(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))
