from __future__ import annotations

from polymarket_weather_arb.modules.base import MarketModule

CHINA_TEMP_BUCKET_MODULE = MarketModule(
    id="china_temp_bucket",
    label_key="module.china_temp_bucket.label",
    description_key="module.china_temp_bucket.description",
    supports_discovery=True,
    supports_analysis=True,
    supports_dry_run=True,
    live_eligibility="candidate_gate_required",
    requires_official_source=True,
    requires_settlement_grade=True,
    min_rule_confidence=0.85,
    promotion_criteria=[
        "Rule confidence >= 0.85",
        "Official China weather station data",
        "Fresh reconciliation",
        "Market whitelisted in LIVE_MARKET_IDS",
        "Strategy override with live_auto_enabled=True",
        "Risk caps passed",
    ],
    blockers=[
        "Open-Meteo fallback data",
        "Rule confidence below 0.85",
        "Missing or stale reconciliation",
        "Market not whitelisted",
        "No strategy override",
    ],
)
