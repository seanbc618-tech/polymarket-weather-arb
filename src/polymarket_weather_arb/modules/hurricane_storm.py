from __future__ import annotations

from polymarket_weather_arb.modules.base import MarketModule

HURRICANE_STORM_MODULE = MarketModule(
    id="hurricane_storm",
    label_key="module.hurricane_storm.label",
    description_key="module.hurricane_storm.description",
    supports_discovery=True,
    supports_analysis=False,
    supports_dry_run=False,
    live_eligibility="research_only",
    requires_official_source=True,
    requires_settlement_grade=True,
    min_rule_confidence=0.90,
    promotion_criteria=[
        "NHC (National Hurricane Center) official data source",
        "Event type model (named storm, landfall, category, track)",
        "Settlement rule parser for storm markets",
        "Probability model for storm events",
        "Rule confidence >= 0.90",
        "Fresh reconciliation",
        "Market whitelisted in LIVE_MARKET_IDS",
        "Strategy override with live_auto_enabled=True",
    ],
    blockers=[
        "No NHC/official source adapter",
        "No event type model",
        "No settlement rule parser",
        "No probability model",
        "Storm markets cannot use temperature threshold model",
    ],
)
