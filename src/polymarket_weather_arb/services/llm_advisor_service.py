from __future__ import annotations

import json
from decimal import Decimal
from sqlite3 import Row

from polymarket_weather_arb.adapters.llm.base import LlmClient
from polymarket_weather_arb.adapters.llm.factory import build_llm_client
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.llm_decision import (
    ALLOWED_LLM_ACTIONS,
    LlmTradeDecision,
    LlmGroupDecision,
)
from datetime import datetime
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
    settlement_bucket_bounds,
)
from polymarket_weather_arb.domain.rules import parse_resolution_rule
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.services.calibration_service import CalibrationService
from polymarket_weather_arb.storage.repositories import Repository


def build_system_prompt(language: str = "zh") -> str:
    reason_language = (
        'Write the "reason" field in Simplified Chinese (简体中文), concise and trader-friendly.'
        if language == "zh"
        else 'Write the "reason" field in concise English.'
    )
    return f"""You are an independent weather evidence reviewer and candidate router.
You review settlement wording, forecast evidence, model disagreement, and market context.
Respond with JSON only using this schema:
{{
  "action": "buy_yes" | "buy_no" | "skip",
  "confidence": 0.0-1.0,
  "reason": "short explanation"
}}
Rules:
- {reason_language}
- action and confidence must stay in English schema values; only reason is localized.
- Use skip to flag ambiguity, weak calibration, thin edge, or missing evidence.
- Form an independent opinion without seeing or guessing the quantitative engine's action.
- For temperature buckets, buy_yes means the named interval is the most defensible candidate.
- Use confidence below 0.5 when the evidence is incomplete or internally inconsistent.
- Your output is advisory only. It cannot place, block, or modify an order.
"""


def build_group_system_prompt(language: str = "zh") -> str:
    reason_language = (
        'Write the "reason" field in Simplified Chinese (简体中文), concise and trader-friendly.'
        if language == "zh"
        else 'Write the "reason" field in concise English.'
    )
    return f"""You are an independent weather evidence reviewer and probability forecaster.
You review settlement wording, forecast evidence, and market context for a complete set of sibling temperature buckets covering a single event.
Respond with JSON only using this exact schema:
{{
  "bucket_probabilities": [
    {{"market_id": "string", "yes_probability": 0.0-1.0}}
  ],
  "other_probability": 0.0-1.0,
  "confidence": 0.0-1.0,
  "reason": "short explanation"
}}
Rules:
- {reason_language}
- The sum of all yes_probability values and other_probability MUST equal exactly 1.0 (or within 0.98 to 1.02).
- other_probability represents the chance that the temperature falls completely outside any of the provided bucket bounds.
- Use confidence below 0.5 when evidence is highly uncertain or contradictory.
- Form an independent probability distribution.
- Your output is advisory only and will be blended quantitatively.
"""


class LlmAdvisorService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        client: LlmClient | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self._client = client if client is not None else build_llm_client(settings)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.llm_enabled and self._client is not None)

    @property
    def provider(self) -> str | None:
        return self._client.provider if self._client is not None else None

    @property
    def model(self) -> str | None:
        return self._client.model if self._client is not None else None

    def evaluate(self, market_id: str, analysis_row: Row) -> LlmTradeDecision | None:
        if not self.enabled or self._client is None:
            return None
        market = self.repository.get_market(market_id)
        if market is None:
            raise ValueError(f"unknown market: {market_id}")
        snapshot_row = self.repository.latest_pricing_snapshot(market_id)
        forecast_row = self.repository.latest_forecast(market_id)
        if str(market["module_id"] or "") == "global_temp_bucket":
            rule_payload = _global_bucket_rule_payload(
                parse_global_temperature_bucket_rule(market["title"], market["description"])
            )
        else:
            rule_payload = _threshold_rule_payload(
                parse_resolution_rule(market["title"], market["description"])
            )
        trust = CalibrationService(self.repository).trust_for_latest_signal(market_id)
        payload = {
            "market": {
                "id": market_id,
                "title": market["title"],
                "description": market["description"],
            },
            "rule": rule_payload,
            "order_book": _snapshot_payload(snapshot_row),
            "forecast": _forecast_payload(forecast_row),
            "calibration": {
                "model_version": trust.model_version,
                "forecast_provider": trust.forecast_provider,
                "total_signals": trust.total_signals,
                "resolved_signals": trust.resolved_signals,
                "brier_score": float(trust.brier_score) if trust.brier_score is not None else None,
                "hit_rate": float(trust.hit_rate) if trust.hit_rate is not None else None,
                "status": trust.status,
            },
            "policy": {
                "min_edge": float(self.settings.min_edge),
                "min_confidence": float(self.settings.llm_min_confidence),
            },
        }
        raw = self._client.complete_json(
            system=build_system_prompt(self.settings.llm_language),
            user=json.dumps(payload, ensure_ascii=False, indent=2),
        )
        return _parse_decision(
            raw,
            provider=self._client.provider,
            model=self._client.model,
        )

    def evaluate_group(
        self,
        event_identity: str,
        sibling_markets: list[dict],
        now: datetime,
        *,
        forecast_evidence: dict[str, tuple[ForecastSnapshot, dict[str, object]]] | None = None,
        observation_evidence: dict[str, object] | None = None,
    ) -> LlmGroupDecision | None:
        if not self.enabled or self._client is None:
            return None
        if not sibling_markets:
            return None

        payload_markets = []
        for market in sibling_markets:
            snapshot_row = self.repository.latest_pricing_snapshot(market["id"])
            forecast_row = self.repository.latest_forecast(market["id"])
            current_evidence = (forecast_evidence or {}).get(market["id"])
            rule = parse_global_temperature_bucket_rule(market["title"], market["description"])
            payload_markets.append(
                {
                    "market_id": market["id"],
                    "rule": _global_bucket_rule_payload(rule),
                    "order_book": _snapshot_payload(snapshot_row),
                    "forecast": (
                        _forecast_snapshot_payload(*current_evidence)
                        if current_evidence is not None
                        else _forecast_payload(forecast_row)
                    ),
                }
            )

        payload = {
            "event_identity": event_identity,
            "markets": payload_markets,
            "observation": observation_evidence or {},
            "policy": {
                "min_confidence": float(self.settings.llm_min_confidence),
            },
            "system_time": now.isoformat(),
        }

        try:
            raw = self._client.complete_json(
                system=build_group_system_prompt(self.settings.llm_language),
                user=json.dumps(payload, ensure_ascii=False, indent=2),
            )
            return _parse_group_decision(
                raw, sibling_markets, provider=self._client.provider, model=self._client.model
            )
        except Exception as e:
            return LlmGroupDecision(
                bucket_probabilities={},
                other_probability=Decimal("0"),
                confidence=Decimal("0"),
                reason=f"api error: {e}",
                provider=self._client.provider,
                model=self._client.model,
                decision="error",
            )


def _parse_decision(raw: dict[str, object], *, provider: str, model: str) -> LlmTradeDecision:
    action = str(raw.get("action", "skip")).strip().lower()
    if action not in ALLOWED_LLM_ACTIONS:
        action = "skip"
    confidence_raw = raw.get("confidence", 0)
    try:
        confidence = Decimal(str(confidence_raw))
    except Exception:
        confidence = Decimal("0")
    reason = str(raw.get("reason", "")).strip() or "no reason provided"
    return LlmTradeDecision(
        action=action,
        confidence=confidence,
        reason=reason,
        provider=provider,
        model=model,
        raw_response=json.dumps(raw, ensure_ascii=False),
    )


def _parse_group_decision(
    raw: dict[str, object], sibling_markets: list[dict], *, provider: str, model: str
) -> LlmGroupDecision:
    raw_response = json.dumps(raw, ensure_ascii=False)
    try:
        bucket_probabilities = {}
        raw_probs = raw.get("bucket_probabilities", [])
        for item in raw_probs:
            market_id = str(item.get("market_id"))
            if market_id in bucket_probabilities:
                raise ValueError("duplicate market_id in bucket_probabilities")
            bucket_probabilities[market_id] = Decimal(str(item.get("yes_probability", 0)))

        other_probability = Decimal(str(raw.get("other_probability", 0)))
        confidence = Decimal(str(raw.get("confidence", 0)))
        reason = str(raw.get("reason", "")).strip() or "no reason provided"

        expected_ids = {m["id"] for m in sibling_markets}
        if set(bucket_probabilities.keys()) != expected_ids:
            raise ValueError("missing or extra market_ids in bucket_probabilities")

        return LlmGroupDecision(
            bucket_probabilities=bucket_probabilities,
            other_probability=other_probability,
            confidence=confidence,
            reason=reason,
            provider=provider,
            model=model,
            raw_response=raw_response,
        )
    except Exception as e:
        return LlmGroupDecision(
            bucket_probabilities={},
            other_probability=Decimal("0"),
            confidence=Decimal("0"),
            reason=f"invalid response: {e}",
            provider=provider,
            model=model,
            raw_response=raw_response,
            decision="invalid",
        )


def _json_number(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _snapshot_payload(snapshot_row: Row | None) -> dict[str, object]:
    if snapshot_row is None:
        return {}
    return {
        "best_bid": _json_number(snapshot_row["best_bid"]),
        "best_ask": _json_number(snapshot_row["best_ask"]),
        "midpoint": _json_number(snapshot_row["midpoint"]),
        "spread": _json_number(snapshot_row["spread"]),
        "fetched_at": snapshot_row["fetched_at"],
    }


def _forecast_payload(forecast_row: Row | None) -> dict[str, object]:
    if forecast_row is None:
        return {}
    payload: dict[str, object] = {
        "provider": forecast_row["provider"],
        "variable": forecast_row["variable"],
        "value": _json_number(forecast_row["value"]),
        "lower_value": _json_number(forecast_row["lower_value"]),
        "upper_value": _json_number(forecast_row["upper_value"]),
        "unit": forecast_row["unit"],
        "fetched_at": forecast_row["fetched_at"],
    }
    try:
        payload["raw"] = json.loads(forecast_row["raw_payload"])
    except (TypeError, json.JSONDecodeError):
        payload["raw"] = {}
    return payload


def _forecast_snapshot_payload(
    forecast: ForecastSnapshot,
    raw_payload: dict[str, object],
) -> dict[str, object]:
    return {
        "provider": forecast.provider,
        "variable": forecast.variable,
        "value": _json_number(forecast.value),
        "lower_value": _json_number(forecast.lower_value),
        "upper_value": _json_number(forecast.upper_value),
        "unit": forecast.unit,
        "valid_time": forecast.valid_time.isoformat(),
        "fetched_at": forecast.fetched_at.isoformat(),
        "raw": raw_payload,
    }


def _threshold_rule_payload(rule) -> dict[str, object]:
    return {
        "kind": "threshold",
        "location": rule.location,
        "station": rule.station,
        "variable": rule.variable,
        "operator": rule.operator,
        "threshold": float(rule.threshold) if rule.threshold is not None else None,
        "unit": rule.unit,
        "tradable": rule.tradable,
        "rejection_reason": rule.rejection_reason,
    }


def _global_bucket_rule_payload(rule) -> dict[str, object]:
    settlement_lower, settlement_upper = settlement_bucket_bounds(rule)
    return {
        "kind": "temperature_bucket",
        "location": rule.location,
        "station": rule.station,
        "variable": rule.variable,
        "bucket_kind": rule.bucket_kind,
        "bucket_lower": float(settlement_lower) if settlement_lower is not None else None,
        "bucket_upper": float(settlement_upper) if settlement_upper is not None else None,
        "target_date": rule.target_date,
        "source": rule.source,
        "unit": rule.unit,
        "tradable": rule.tradable,
        "rejection_reason": rule.rejection_reason,
    }
