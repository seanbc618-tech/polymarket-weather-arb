from __future__ import annotations

from polymarket_weather_arb.modules.base import MarketModule

GLOBAL_TEMP_BUCKET_MODULE = MarketModule(
    id="global_temp_bucket",
    label_key="module.global_temp_bucket.label",
    description_key="module.global_temp_bucket.description",
    supports_discovery=True,
    supports_analysis=True,
    supports_dry_run=True,
    live_eligibility="micro_live_ready",
    requires_official_source=False,
    requires_settlement_grade=False,
    min_rule_confidence=0.85,
    promotion_criteria=[
        "Rule confidence >= 0.85",
        "Forecast source and provenance are persisted for every decision",
        "Unit handling (C/F/K) verified",
        "Fresh reconciliation",
        "Market whitelisted in LIVE_MARKET_IDS",
        "Strategy override with live_auto_enabled=True",
        "Risk caps passed",
    ],
    blockers=[
        "Forecast provider cannot resolve the market location",
        "Rule confidence below 0.85",
        "Missing or stale reconciliation",
        "Market not whitelisted",
    ],
)
