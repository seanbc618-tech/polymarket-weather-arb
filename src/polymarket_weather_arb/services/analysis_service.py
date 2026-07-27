from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.adapters.weather.base import WeatherProvider
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import MarketSnapshot
import json

from polymarket_weather_arb.domain.fees import extract_market_fee_schedule
from polymarket_weather_arb.domain.pricing import Analysis, analyze_price
from polymarket_weather_arb.domain.probability import estimate_probability_interval
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.storage.repositories import Repository


class AnalysisService:
    def __init__(
        self, settings: Settings, weather_provider: WeatherProvider, repository: Repository
    ) -> None:
        self.settings = settings
        self.weather_provider = weather_provider
        self.repository = repository

    def refresh_weather(self, market_id: str, rule: ResolutionRule) -> ForecastSnapshot:
        forecast, raw_payload = self.weather_provider.fetch_forecast(market_id, rule)
        self.repository.save_forecast(forecast, raw_payload)
        return forecast

    def analyze(
        self,
        market_id: str,
        rule: ResolutionRule,
        forecast: ForecastSnapshot,
        snapshot: MarketSnapshot,
    ) -> Analysis:
        interval = estimate_probability_interval(rule, forecast)
        fees_enabled = False
        fee_rate = None
        market_row = self.repository.get_market(market_id)
        if market_row is not None:
            try:
                payload = json.loads(market_row["raw_payload"])
            except Exception:
                payload = {}
            schedule = extract_market_fee_schedule(payload if isinstance(payload, dict) else {})
            fees_enabled = schedule.fees_enabled
            fee_rate = schedule.fee_rate
        analysis = analyze_price(
            market_id=market_id,
            interval=interval,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            min_edge=self.settings.min_edge,
            slippage_buffer=self.settings.slippage_buffer,
            fees_enabled=fees_enabled,
            fee_rate=fee_rate,
        )
        self.repository.save_analysis(analysis)
        return analysis


def snapshot_from_row(row) -> MarketSnapshot:
    return MarketSnapshot(
        market_id=row["market_id"],
        best_bid=_decimal(row["best_bid"]),
        best_ask=_decimal(row["best_ask"]),
        midpoint=_decimal(row["midpoint"]),
        spread=_decimal(row["spread"]),
        liquidity=_decimal(row["liquidity"]),
        fetched_at=_parse_datetime(row["fetched_at"]),
    )


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
