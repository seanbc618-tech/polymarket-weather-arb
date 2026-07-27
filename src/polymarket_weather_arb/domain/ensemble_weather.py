"""Ensemble weather forecast domain models.

This module defines data structures for ensemble weather forecasts,
specifically using Open-Meteo's GFS ensemble (31 members) for
probability estimation in research/dry-run mode.

IMPORTANT: Ensemble forecasts are research_forecast only, NOT official_forecast
or settlement_observation. They cannot be used for live trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class EnsembleForecastSnapshot:
    """Snapshot of an ensemble weather forecast.

    This represents a 31-member GFS ensemble forecast for a specific
    location, variable, and time window. Used for probability estimation
    in research/dry-run mode only.

    Attributes:
        market_id: Polymarket market ID
        location: City or location name
        variable: Weather variable (temperature_high, temperature_low)
        members: List of individual ensemble member values
        mean: Mean of all ensemble members
        std: Standard deviation of all ensemble members
        member_count: Number of ensemble members (typically 31)
        fetched_at: When the forecast was fetched
        raw_payload: Raw API response for audit trail
        source_grade: Always 'research_forecast' for ensemble forecasts
        issue_time: When the forecast was issued
        valid_time: When the forecast is valid for
        provider: Provider name (for compatibility with ForecastSnapshot)
        station: Station ID (for compatibility with ForecastSnapshot)
        value: Mean value (for compatibility with ForecastSnapshot)
        lower_value: Mean - std (for compatibility with ForecastSnapshot)
        upper_value: Mean + std (for compatibility with ForecastSnapshot)
        unit: Unit of measurement (for compatibility with ForecastSnapshot)
    """

    market_id: str
    location: str
    variable: str
    members: list[Decimal]
    mean: Decimal
    std: Decimal
    member_count: int
    fetched_at: datetime
    raw_payload: dict
    source_grade: str = "research_forecast"
    issue_time: datetime | None = None
    valid_time: datetime | None = None
    provider: str = "open-meteo-ensemble"
    station: str | None = None
    value: Decimal = Decimal("0")
    lower_value: Decimal = Decimal("0")
    upper_value: Decimal = Decimal("0")
    unit: str = "F"

    @classmethod
    def from_members(
        cls,
        market_id: str,
        location: str,
        variable: str,
        members: list[Decimal],
        fetched_at: datetime,
        raw_payload: dict,
        issue_time: datetime | None = None,
        valid_time: datetime | None = None,
        unit: str = "F",
    ) -> EnsembleForecastSnapshot:
        """Create an EnsembleForecastSnapshot from a list of member values.

        Args:
            market_id: Polymarket market ID
            location: City or location name
            variable: Weather variable
            members: List of ensemble member values
            fetched_at: When the forecast was fetched
            raw_payload: Raw API response
            issue_time: When the forecast was issued (defaults to fetched_at)
            valid_time: When the forecast is valid for (defaults to fetched_at)
            unit: Unit of measurement (default: F)

        Returns:
            EnsembleForecastSnapshot with computed mean, std, and member_count
        """
        if not members:
            raise ValueError("Cannot create EnsembleForecastSnapshot with empty members")

        # Default issue_time and valid_time to fetched_at if not provided
        if issue_time is None:
            issue_time = fetched_at
        if valid_time is None:
            valid_time = fetched_at

        member_count = len(members)
        mean = sum(members) / member_count
        variance = sum((m - mean) ** 2 for m in members) / member_count
        std = variance.sqrt() if hasattr(variance, "sqrt") else Decimal(str(float(variance) ** 0.5))

        # Compute value, lower_value, upper_value for compatibility
        value = mean
        lower_value = mean - std
        upper_value = mean + std

        return cls(
            market_id=market_id,
            location=location,
            variable=variable,
            members=members,
            mean=mean,
            std=std,
            member_count=member_count,
            fetched_at=fetched_at,
            raw_payload=raw_payload,
            source_grade="research_forecast",
            issue_time=issue_time,
            valid_time=valid_time,
            provider="open-meteo-ensemble",
            station=None,
            value=value,
            lower_value=lower_value,
            upper_value=upper_value,
            unit=unit,
        )


@dataclass(frozen=True)
class EnsembleProbabilityEstimate:
    """Probability estimate derived from ensemble forecast.

    This represents the probability that a weather variable will be
    above or below a threshold, based on ensemble member agreement.

    Attributes:
        market_id: Polymarket market ID
        threshold: The threshold value
        operator: 'above' or 'below'
        probability: Estimated probability (0-1)
        agreement: Fraction of ensemble members that agree
        member_count: Number of ensemble members
        mean: Ensemble mean
        std: Ensemble standard deviation
        model_version: Model version identifier
        reasons: List of factors that influenced the estimate
    """

    market_id: str
    threshold: Decimal
    operator: str  # 'above' or 'below'
    probability: Decimal
    agreement: Decimal
    member_count: int
    mean: Decimal
    std: Decimal
    model_version: str = "ensemble-threshold-v1"
    reasons: list[str] = ()

    def __post_init__(self):
        if self.operator not in ("above", "below"):
            raise ValueError(f"operator must be 'above' or 'below', got {self.operator}")
        if not (0 <= self.probability <= 1):
            raise ValueError(f"probability must be between 0 and 1, got {self.probability}")
        if not (0 <= self.agreement <= 1):
            raise ValueError(f"agreement must be between 0 and 1, got {self.agreement}")


def probability_above(
    threshold: Decimal,
    members: list[Decimal],
    market_id: str,
    mean: Decimal,
    std: Decimal,
) -> EnsembleProbabilityEstimate:
    """Calculate probability that value will be above threshold.

    Args:
        threshold: The threshold value
        members: List of ensemble member values
        market_id: Polymarket market ID
        mean: Ensemble mean
        std: Ensemble standard deviation

    Returns:
        EnsembleProbabilityEstimate with probability and agreement
    """
    if not members:
        raise ValueError("Cannot calculate probability with empty members")

    above_count = sum(1 for m in members if m > threshold)
    probability = Decimal(str(above_count / len(members)))
    agreement = max(probability, 1 - probability)

    reasons = [
        f"member_count={len(members)}",
        f"mean={mean:.2f}",
        f"std={std:.2f}",
        f"threshold={threshold:.2f}",
        f"above_count={above_count}",
        f"agreement={agreement:.2f}",
    ]

    return EnsembleProbabilityEstimate(
        market_id=market_id,
        threshold=threshold,
        operator="above",
        probability=probability,
        agreement=agreement,
        member_count=len(members),
        mean=mean,
        std=std,
        reasons=reasons,
    )


def probability_below(
    threshold: Decimal,
    members: list[Decimal],
    market_id: str,
    mean: Decimal,
    std: Decimal,
) -> EnsembleProbabilityEstimate:
    """Calculate probability that value will be below threshold.

    Args:
        threshold: The threshold value
        members: List of ensemble member values
        market_id: Polymarket market ID
        mean: Ensemble mean
        std: Ensemble standard deviation

    Returns:
        EnsembleProbabilityEstimate with probability and agreement
    """
    if not members:
        raise ValueError("Cannot calculate probability with empty members")

    below_count = sum(1 for m in members if m < threshold)
    probability = Decimal(str(below_count / len(members)))
    agreement = max(probability, 1 - probability)

    reasons = [
        f"member_count={len(members)}",
        f"mean={mean:.2f}",
        f"std={std:.2f}",
        f"threshold={threshold:.2f}",
        f"below_count={below_count}",
        f"agreement={agreement:.2f}",
    ]

    return EnsembleProbabilityEstimate(
        market_id=market_id,
        threshold=threshold,
        operator="below",
        probability=probability,
        agreement=agreement,
        member_count=len(members),
        mean=mean,
        std=std,
        reasons=reasons,
    )
