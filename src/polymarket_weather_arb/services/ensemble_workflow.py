"""Ensemble weather workflow implementation.

This module provides ensemble-specific workflow logic,
extracted from MarketWorkflowService to reduce complexity.
"""

from __future__ import annotations

from decimal import Decimal
from sqlite3 import Row

from polymarket_weather_arb.adapters.weather.open_meteo_ensemble import OpenMeteoEnsembleProvider
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.ensemble_pricing import ensemble_to_analysis
from polymarket_weather_arb.domain.ensemble_weather import (
    EnsembleForecastSnapshot,
    probability_above,
    probability_below,
)
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.storage.repositories import Repository


class EnsembleWorkflow:
    """Workflow for ensemble weather forecasts."""

    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository
        self.provider = OpenMeteoEnsembleProvider()

    def refresh_weather(self, market_id: str, rule: ResolutionRule) -> EnsembleForecastSnapshot:
        """Refresh ensemble weather forecast."""
        snapshot, raw_payload = self.provider.fetch_forecast(market_id, rule)

        self.repository.save_forecast(
            snapshot,
            {**raw_payload, "source_grade": "research_forecast", "provider": "open-meteo-ensemble"},
        )

        return snapshot

    def analyze(
        self,
        market_id: str,
        rule: ResolutionRule,
        snapshot_row: Row,
    ) -> Analysis:
        """Analyze market using ensemble forecast."""
        snapshot = self.refresh_weather(market_id, rule)

        if rule.threshold is None:
            raise ValueError("Ensemble analysis requires a threshold")

        threshold = Decimal(str(rule.threshold))

        # Calculate probability based on operator
        if rule.operator in (">", ">="):
            estimate = probability_above(
                threshold=threshold,
                members=snapshot.members,
                market_id=market_id,
                mean=snapshot.mean,
                std=snapshot.std,
            )
        elif rule.operator in ("<", "<="):
            estimate = probability_below(
                threshold=threshold,
                members=snapshot.members,
                market_id=market_id,
                mean=snapshot.mean,
                std=snapshot.std,
            )
        else:
            raise ValueError(f"Unsupported operator for ensemble: {rule.operator}")

        # Get order book data
        best_bid = (
            Decimal(str(snapshot_row["best_bid"])) if snapshot_row["best_bid"] is not None else None
        )
        best_ask = (
            Decimal(str(snapshot_row["best_ask"])) if snapshot_row["best_ask"] is not None else None
        )

        # Convert to Analysis
        analysis = ensemble_to_analysis(
            estimate=estimate,
            best_bid=best_bid,
            best_ask=best_ask,
            min_edge=self.settings.min_edge,
            slippage_buffer=self.settings.slippage_buffer,
        )

        self.repository.save_analysis(analysis)
        return analysis
