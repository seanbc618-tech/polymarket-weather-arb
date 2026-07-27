from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import MarketSnapshot, parse_market_payload
from polymarket_weather_arb.domain.pricing import analyze_price
from polymarket_weather_arb.domain.probability import estimate_probability_interval
from polymarket_weather_arb.domain.rules import ResolutionRule, parse_resolution_rule
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.storage.repositories import Repository, to_json


def import_market_json(input_path: Path, output_dir: Path) -> Path:
    payload = json.loads(input_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("market fixture input must be a JSON object")
    market = parse_market_payload(payload)
    rule = parse_resolution_rule(market.title, market.description)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{_safe_slug(market.slug or market.id)}.json"
    fixture = {
        "market": {
            "id": market.id,
            "slug": market.slug,
            "title": market.title,
            "description": market.description,
            "event_slug": market.event_slug,
            "event_title": market.event_title,
            "category": market.category,
            "tags": list(market.tags),
            "is_weather": market.is_weather,
        },
        "parsed_rule": _rule_fixture(rule),
        "raw_market": payload,
    }
    output_path.write_text(to_json(fixture) + "\n")
    return output_path


def load_market_fixture(
    input_path: Path,
    repository: Repository,
    settings: Settings,
    *,
    demo_analysis: bool = False,
) -> str:
    fixture = json.loads(input_path.read_text())
    if not isinstance(fixture, dict):
        raise ValueError("market fixture must be a JSON object")
    raw_market = fixture.get("raw_market")
    if not isinstance(raw_market, dict):
        raise ValueError("market fixture must include raw_market object")

    market = parse_market_payload(raw_market)
    rule = parse_resolution_rule(market.title, market.description)
    repository.upsert_market(market, raw_market)
    repository.save_resolution_rule(market.id, rule)
    if demo_analysis:
        _save_demo_analysis(market.id, rule, repository, settings)
    repository.upsert_candidate(
        market.id,
        rule,
        repository.latest_market_snapshot(market.id),
        status="dry_run_ready" if demo_analysis and rule.tradable else None,
    )
    return market.id


def _save_demo_analysis(
    market_id: str, rule: ResolutionRule, repository: Repository, settings: Settings
) -> None:
    if not rule.tradable or rule.threshold is None or rule.variable is None or rule.unit is None:
        raise ValueError("demo analysis requires a tradable threshold fixture")

    now = datetime.now(timezone.utc)
    forecast = ForecastSnapshot(
        market_id=market_id,
        provider="fixture-demo",
        location=rule.location,
        station=rule.station,
        variable=rule.variable,
        value=_demo_forecast_value(rule),
        unit=rule.unit,
        issue_time=now,
        valid_time=now,
        fetched_at=now,
    )
    snapshot = MarketSnapshot(
        market_id=market_id,
        best_bid=Decimal("0.19"),
        best_ask=Decimal("0.20"),
        midpoint=Decimal("0.195"),
        spread=Decimal("0.01"),
        liquidity=Decimal("1000"),
        fetched_at=now,
    )
    repository.save_forecast(forecast, {"source": "fixture-demo"})
    repository.save_market_snapshot(snapshot, {"source": "fixture-demo"})
    interval = estimate_probability_interval(rule, forecast, now=now)
    analysis = analyze_price(
        market_id=market_id,
        interval=interval,
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
        min_edge=settings.min_edge,
        slippage_buffer=settings.slippage_buffer,
    )
    repository.save_analysis(analysis)


def _demo_forecast_value(rule: ResolutionRule) -> Decimal:
    margin = (
        Decimal("10") if rule.variable and rule.variable.startswith("temperature") else Decimal("1")
    )
    if rule.operator == "<=":
        return (rule.threshold or Decimal("0")) - margin
    return (rule.threshold or Decimal("0")) + margin


def _rule_fixture(rule: ResolutionRule) -> dict[str, Any]:
    return {
        "location": rule.location,
        "station": rule.station,
        "source": rule.source,
        "variable": rule.variable,
        "operator": rule.operator,
        "threshold": str(rule.threshold) if rule.threshold is not None else None,
        "unit": rule.unit,
        "window_start": rule.window_start,
        "window_end": rule.window_end,
        "confidence": rule.confidence,
        "tradable": rule.tradable,
        "rejection_reason": rule.rejection_reason,
    }


def _safe_slug(value: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "-" for char in value)
    safe = "-".join(part for part in safe.split("-") if part)
    return safe[:120] or "market"
