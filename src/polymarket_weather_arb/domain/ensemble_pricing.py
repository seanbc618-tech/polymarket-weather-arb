"""Ensemble weather forecast pricing logic.

This module converts ensemble probability estimates into Analysis objects
that can be used for dry-run order intents. It uses the same pricing
logic as the standard pricing module, but with ensemble-specific
probability intervals.

IMPORTANT: This is for research/dry-run only. Ensemble forecasts are
NOT official_forecast / settlement_observation and cannot be used for live trading.
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_weather_arb.domain.ensemble_weather import EnsembleProbabilityEstimate
from polymarket_weather_arb.domain.pricing import Analysis, analyze_price
from polymarket_weather_arb.domain.probability import ProbabilityInterval


def ensemble_to_probability_interval(
    estimate: EnsembleProbabilityEstimate,
    widening_factor: Decimal = Decimal("0.05"),
) -> ProbabilityInterval:
    """Convert ensemble probability estimate to ProbabilityInterval.

    This applies conservative widening based on ensemble agreement.
    Lower agreement = wider interval = more conservative.

    Args:
        estimate: Ensemble probability estimate
        widening_factor: Base widening factor (default: 0.05)

    Returns:
        ProbabilityInterval with conservative bounds
    """
    base_probability = estimate.probability

    # Apply conservative widening based on agreement
    # Lower agreement = wider interval
    agreement_factor = estimate.agreement
    if agreement_factor < Decimal("0.6"):
        widening = widening_factor * 3  # Very conservative
    elif agreement_factor < Decimal("0.7"):
        widening = widening_factor * 2  # Moderately conservative
    elif agreement_factor < Decimal("0.8"):
        widening = widening_factor * 1.5  # Somewhat conservative
    else:
        widening = widening_factor  # Less conservative

    # Calculate interval bounds
    lower = max(Decimal("0.01"), base_probability - widening)
    upper = min(Decimal("0.99"), base_probability + widening)

    # Build reasons
    reasons = [
        f"ensemble_model={estimate.model_version}",
        f"member_count={estimate.member_count}",
        f"mean={estimate.mean:.2f}",
        f"std={estimate.std:.2f}",
        f"agreement={estimate.agreement:.2f}",
        f"threshold={estimate.threshold:.2f}",
        f"operator={estimate.operator}",
        f"probability={estimate.probability:.2f}",
        f"widening={widening:.2f}",
        "source=research_forecast_ensemble",
        "not_for_live_trading",
    ]

    return ProbabilityInterval(
        lower=lower,
        upper=upper,
        reasons=reasons,
        model_version=estimate.model_version,
    )


def ensemble_to_analysis(
    estimate: EnsembleProbabilityEstimate,
    best_bid: Decimal | None = None,
    best_ask: Decimal | None = None,
    min_edge: Decimal = Decimal("0.05"),
    slippage_buffer: Decimal = Decimal("0.02"),
) -> Analysis:
    """Convert ensemble probability estimate to Analysis for dry-run.

    This function uses the standard analyze_price logic with ensemble
    probability intervals. It requires order book data (best_bid/best_ask)
    to calculate proper edge.

    Args:
        estimate: Ensemble probability estimate
        best_bid: Best bid price from order book
        best_ask: Best ask price from order book
        min_edge: Minimum edge required (from settings)
        slippage_buffer: Slippage buffer (from settings)

    Returns:
        Analysis object suitable for dry-run
    """
    # Convert ensemble estimate to ProbabilityInterval
    interval = ensemble_to_probability_interval(estimate)

    # Use standard analyze_price logic
    analysis = analyze_price(
        market_id=estimate.market_id,
        interval=interval,
        best_bid=best_bid,
        best_ask=best_ask,
        min_edge=min_edge,
        slippage_buffer=slippage_buffer,
    )

    # Add ensemble-specific reasons
    ensemble_reasons = [
        "ensemble_source=research_forecast",
        "not_for_live_trading",
    ]

    return Analysis(
        market_id=analysis.market_id,
        model_version=analysis.model_version,
        fair_lower=analysis.fair_lower,
        fair_upper=analysis.fair_upper,
        reference_price=analysis.reference_price,
        edge=analysis.edge,
        side=analysis.side,
        decision=analysis.decision,
        reasons=analysis.reasons + ensemble_reasons,
        created_at=analysis.created_at,
    )
