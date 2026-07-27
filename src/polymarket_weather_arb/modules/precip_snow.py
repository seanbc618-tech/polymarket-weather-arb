from __future__ import annotations

from polymarket_weather_arb.modules.base import MarketModule

PRECIP_SNOW_MODULE = MarketModule(
    id="precip_snow",
    label_key="module.precip_snow.label",
    description_key="module.precip_snow.description",
    supports_discovery=True,
    supports_analysis=True,
    supports_dry_run=True,
    live_eligibility="dry_run_only",
    requires_official_source=True,
    requires_settlement_grade=True,
    min_rule_confidence=0.85,
    promotion_criteria=[
        "Rule confidence >= 0.85",
        "Official precipitation/snowfall data source (NOAA/NWS)",
        "Accumulation rules parsed correctly",
        "Unit handling (mm/inch/cm) verified",
        "Time window and accumulation logic tested",
        "Fresh reconciliation",
        "Market whitelisted in LIVE_MARKET_IDS",
        "Strategy override with live_auto_enabled=True",
        "Risk caps passed",
    ],
    blockers=[
        "No official precipitation/snowfall source",
        "Accumulation rules not parsed",
        "Unit handling not verified",
        "Rule confidence below 0.85",
        "Missing or stale reconciliation",
        "Market not whitelisted",
    ],
)
