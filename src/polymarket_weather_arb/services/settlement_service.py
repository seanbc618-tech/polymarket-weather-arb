from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from polymarket_weather_arb.domain.rules import ResolutionRule, parse_resolution_rule
from polymarket_weather_arb.domain.weather import WeatherObservation


class ObservationProvider(Protocol):
    def fetch_observation(
        self,
        market_id: str,
        rule: ResolutionRule,
    ) -> tuple[WeatherObservation, dict[str, Any]]: ...


@dataclass(frozen=True)
class SettlementBackfillResult:
    market_id: str
    resolved_outcome: str
    observation_value: Decimal
    observation_unit: str
    settlement_source: str
    updated_signals: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettlementPreviewResult:
    market_id: str
    station: str | None
    variable: str | None
    observed_value: Decimal
    unit: str
    observed_at: object
    quality_status: str | None
    would_resolve_outcome: str
    settlement_source: str
    rule_operator: str | None
    rule_threshold: Decimal | None
    warnings: tuple[str, ...] = ()


class SettlementService:
    def __init__(self, repository: Any, observation_provider: ObservationProvider) -> None:
        self.repository = repository
        self.observation_provider = observation_provider

    def preview_market(self, market_id: str) -> SettlementPreviewResult:
        """Fetch the observation and compute the would-be outcome without saving anything."""
        rule = self._load_rule(market_id, persist_parsed=False)
        self._validate_rule(rule)
        observation, raw_payload = self.observation_provider.fetch_observation(market_id, rule)
        would_resolve = "yes" if _matches_rule(observation.value, rule) else "no"
        settlement_source = str(raw_payload.get("source") or observation.provider)
        warnings = tuple(raw_payload.get("warnings") or [])
        return SettlementPreviewResult(
            market_id=market_id,
            station=observation.station,
            variable=observation.variable,
            observed_value=observation.value,
            unit=observation.unit,
            observed_at=observation.observed_at,
            quality_status=observation.quality_status,
            would_resolve_outcome=would_resolve,
            settlement_source=settlement_source,
            rule_operator=rule.operator,
            rule_threshold=rule.threshold,
            warnings=warnings,
        )

    def backfill_market(self, market_id: str) -> SettlementBackfillResult:
        rule = self._load_rule(market_id, persist_parsed=True)
        self._validate_rule(rule)
        observation, raw_payload = self.observation_provider.fetch_observation(market_id, rule)
        self.repository.save_observation(observation, raw_payload)

        resolved_outcome = "yes" if _matches_rule(observation.value, rule) else "no"
        settlement_source = str(raw_payload.get("source") or observation.provider)
        warnings = tuple(raw_payload.get("warnings") or [])
        updated = self.repository.settle_model_signals_for_market(
            market_id,
            resolved_outcome=resolved_outcome,
            settlement_value=observation.value,
            settlement_source=settlement_source,
        )
        return SettlementBackfillResult(
            market_id=market_id,
            resolved_outcome=resolved_outcome,
            observation_value=observation.value,
            observation_unit=observation.unit,
            settlement_source=settlement_source,
            updated_signals=updated,
            warnings=warnings,
        )

    def _load_rule(self, market_id: str, *, persist_parsed: bool = True) -> ResolutionRule:
        row = self.repository.get_resolution_rule(market_id)
        if row is not None:
            return ResolutionRule(
                raw_text=row["raw_text"],
                location=row["location"],
                station=row["station"],
                source=row["source"],
                variable=row["variable"],
                operator=row["operator"],
                threshold=Decimal(str(row["threshold"])) if row["threshold"] is not None else None,
                unit=row["unit"],
                window_start=row["window_start"],
                window_end=row["window_end"],
                confidence=float(row["confidence"]),
                tradable=bool(row["tradable"]),
                rejection_reason=row["rejection_reason"],
            )

        market = self.repository.get_market(market_id)
        if market is None:
            raise ValueError(f"market not found: {market_id}")
        rule = parse_resolution_rule(market["title"], market["description"])
        if persist_parsed:
            self.repository.save_resolution_rule(market_id, rule)
        return rule

    @staticmethod
    def _validate_rule(rule: ResolutionRule) -> None:
        if not rule.tradable:
            raise ValueError(f"rule is not tradable: {rule.rejection_reason or 'unknown reason'}")
        if not rule.variable:
            raise ValueError("settlement backfill requires a weather variable")
        if rule.threshold is None or rule.operator is None:
            raise ValueError("settlement backfill requires threshold and operator")
        if rule.unit is None:
            raise ValueError("settlement backfill requires a unit")


def _matches_rule(value: Decimal, rule: ResolutionRule) -> bool:
    threshold = rule.threshold
    if threshold is None:
        raise ValueError("settlement backfill requires threshold")
    if rule.operator == ">":
        return value > threshold
    if rule.operator == ">=":
        return value >= threshold
    if rule.operator == "<":
        return value < threshold
    if rule.operator == "<=":
        return value <= threshold
    raise ValueError(f"unsupported settlement operator: {rule.operator}")
