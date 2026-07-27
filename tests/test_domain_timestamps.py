from dataclasses import MISSING

from polymarket_weather_arb.domain.execution import OrderAttempt, OrderIntent
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import RiskDecision
from polymarket_weather_arb.domain.weather import ForecastSnapshot, WeatherObservation


def test_runtime_timestamps_use_default_factories():
    fields = (
        (OrderIntent, "created_at"),
        (OrderAttempt, "created_at"),
        (Analysis, "created_at"),
        (RiskDecision, "created_at"),
        (ForecastSnapshot, "fetched_at"),
        (WeatherObservation, "fetched_at"),
    )

    for model, field_name in fields:
        field = model.__dataclass_fields__[field_name]
        assert field.default is MISSING
        assert field.default_factory is not MISSING
