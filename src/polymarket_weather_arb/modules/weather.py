from __future__ import annotations

from polymarket_weather_arb.modules.base import MarketModule

WEATHER_MODULE = MarketModule(
    id="weather",
    label_key="module.weather.label",
    description_key="module.weather.description",
    supports_discovery=True,
    supports_analysis=True,
    supports_dry_run=True,
    live_eligibility="candidate_gate_required",
    requires_official_source=True,
    requires_settlement_grade=True,
    min_rule_confidence=0.85,
    promotion_criteria=[
        "Rule confidence >= 0.85",
        "Settlement-grade forecast source (NOAA/NWS or official)",
        "Fresh reconciliation",
        "Market whitelisted in LIVE_MARKET_IDS",
        "Strategy override with live_auto_enabled=True",
        "Risk caps passed",
    ],
    blockers=[
        "Open-Meteo or demo data source",
        "Rule confidence below 0.85",
        "Missing or stale reconciliation",
        "Market not whitelisted",
        "No strategy override",
    ],
)
