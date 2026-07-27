from __future__ import annotations

from typing import Protocol

from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot


class WeatherProvider(Protocol):
    name: str

    def fetch_forecast(
        self, market_id: str, rule: ResolutionRule
    ) -> tuple[ForecastSnapshot, dict[str, object]]: ...
