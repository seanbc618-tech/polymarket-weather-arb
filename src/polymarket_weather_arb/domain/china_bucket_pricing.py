from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from math import erf, sqrt

from polymarket_weather_arb.domain.china_temperature_bucket import ChinaTemperatureBucketRule
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.probability import (
    ProbabilityInterval,
    clamp_probability,
    widen_interval,
)
from polymarket_weather_arb.domain.weather import ForecastSnapshot

MODEL_VERSION = "china-temp-bucket-normal-v1"


@dataclass(frozen=True)
class ChinaBucketPricingConfig:
    min_edge: Decimal = Decimal("0.05")
    slippage_buffer: Decimal = Decimal("0.01")
    max_auto_ask: Decimal = Decimal("0.10")


def estimate_china_bucket_probability_interval(
    rule: ChinaTemperatureBucketRule,
    forecast: ForecastSnapshot,
    now: datetime | None = None,
) -> ProbabilityInterval:
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    if not rule.tradable:
        return ProbabilityInterval(
            Decimal("0"), Decimal("1"), ["rule is not tradable"], model_version=MODEL_VERSION
        )
    if rule.bucket_lower_c is None or rule.bucket_upper_c is None:
        return ProbabilityInterval(
            Decimal("0"), Decimal("1"), ["bucket rule is incomplete"], model_version=MODEL_VERSION
        )
    if forecast.unit != "C":
        return ProbabilityInterval(
            Decimal("0"),
            Decimal("1"),
            ["China bucket model requires Celsius forecast"],
            model_version=MODEL_VERSION,
        )
    if forecast.variable != rule.variable:
        return ProbabilityInterval(
            Decimal("0"),
            Decimal("1"),
            ["forecast variable does not match bucket rule"],
            model_version=MODEL_VERSION,
        )

    fetched_at = (
        forecast.fetched_at
        if forecast.fetched_at.tzinfo
        else forecast.fetched_at.replace(tzinfo=timezone.utc)
    )
    valid_time = (
        forecast.valid_time
        if forecast.valid_time.tzinfo
        else forecast.valid_time.replace(tzinfo=timezone.utc)
    )
    age_seconds = max(0.0, (now - fetched_at).total_seconds())
    hours_to_valid = max(0.0, (valid_time - now).total_seconds() / 3600)
    sigma = _sigma_c(hours_to_valid)
    if age_seconds > 6 * 3600:
        sigma += Decimal("0.30")
        reasons.append("official signal is older than six hours")

    fair = _bucket_probability(forecast.value, rule.bucket_lower_c, rule.bucket_upper_c, sigma)
    lower_mu = (
        forecast.lower_value
        if forecast.lower_value is not None
        else forecast.value - Decimal("0.4")
    )
    upper_mu = (
        forecast.upper_value
        if forecast.upper_value is not None
        else forecast.value + Decimal("0.4")
    )
    conservative_sigma = sigma + Decimal("0.25")
    lower = min(
        _bucket_probability(lower_mu, rule.bucket_lower_c, rule.bucket_upper_c, conservative_sigma),
        _bucket_probability(upper_mu, rule.bucket_lower_c, rule.bucket_upper_c, conservative_sigma),
        fair,
    )
    upper = max(
        _bucket_probability(lower_mu, rule.bucket_lower_c, rule.bucket_upper_c, conservative_sigma),
        _bucket_probability(upper_mu, rule.bucket_lower_c, rule.bucket_upper_c, conservative_sigma),
        fair,
    )
    if rule.confidence < 0.95:
        lower, upper = widen_interval(lower, upper, Decimal("0.05"))
        reasons.append("bucket rule confidence below strict threshold")
    if not reasons:
        reasons.append(f"{_forecast_signal_label(forecast.provider)} with sigma={sigma}C")
    return ProbabilityInterval(
        lower=clamp_probability(lower),
        upper=clamp_probability(upper),
        reasons=reasons,
        model_version=MODEL_VERSION,
    )


def analyze_china_bucket_price(
    market_id: str,
    rule: ChinaTemperatureBucketRule,
    forecast: ForecastSnapshot,
    best_ask: Decimal | None,
    config: ChinaBucketPricingConfig | None = None,
    now: datetime | None = None,
) -> Analysis:
    config = config or ChinaBucketPricingConfig()
    interval = estimate_china_bucket_probability_interval(rule, forecast, now=now)
    reasons = list(interval.reasons)
    if best_ask is None:
        return Analysis(
            market_id,
            interval.model_version,
            interval.lower,
            interval.upper,
            None,
            Decimal("0"),
            None,
            "reject",
            reasons + ["missing ask"],
        )
    if best_ask > config.max_auto_ask:
        return Analysis(
            market_id,
            interval.model_version,
            interval.lower,
            interval.upper,
            best_ask,
            Decimal("0"),
            None,
            "reject",
            reasons + ["ask above China bucket cap"],
        )
    edge = interval.lower - best_ask - config.slippage_buffer
    if edge >= config.min_edge:
        return Analysis(
            market_id,
            interval.model_version,
            interval.lower,
            interval.upper,
            best_ask,
            edge,
            "buy_yes",
            "trade",
            reasons + ["ask is below conservative bucket fair lower bound"],
        )
    return Analysis(
        market_id,
        interval.model_version,
        interval.lower,
        interval.upper,
        best_ask,
        edge,
        None,
        "watch",
        reasons + ["edge does not clear China bucket cost and safety buffer"],
    )


def _forecast_signal_label(provider: str) -> str:
    if provider == "open-meteo-china-signal":
        return "Open-Meteo China city forecast signal"
    if provider == "china-configured-weather-signal":
        return "configured China city weather signal"
    return "China city temperature signal"


def _sigma_c(hours_to_valid: float) -> Decimal:
    if hours_to_valid <= 24:
        return Decimal("0.60")
    if hours_to_valid <= 48:
        return Decimal("0.90")
    if hours_to_valid <= 120:
        return Decimal("1.40")
    return Decimal("1.80")


def _bucket_probability(mu: Decimal, lower: Decimal, upper: Decimal, sigma: Decimal) -> Decimal:
    if sigma <= 0:
        return Decimal("1") if lower <= mu <= upper else Decimal("0")
    probability = _normal_cdf((upper - mu) / sigma) - _normal_cdf((lower - mu) / sigma)
    return clamp_probability(Decimal(str(probability)))


def _normal_cdf(value: Decimal) -> float:
    z = float(value)
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))
