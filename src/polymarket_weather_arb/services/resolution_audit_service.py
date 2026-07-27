import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
from polymarket_weather_arb.domain.polymarket_resolution import (
    compact_resolution_payload,
    parse_resolution_state,
)
from polymarket_weather_arb.services.circuit_breaker_service import CircuitBreakerService
from polymarket_weather_arb.storage.repositories import Repository
from polymarket_weather_arb.services.settlement_service import _matches_rule


@dataclass(frozen=True)
class ResolutionAuditResult:
    market_id: str
    match: bool
    polymarket_closed: bool
    polymarket_uma_status: str
    polymarket_resolved_outcome: Optional[str]
    local_resolved_outcome: Optional[str]
    status: str
    local_source: Optional[str]
    polymarket_source: Optional[str]
    raw_local_payload: Optional[str]
    raw_polymarket_payload: Optional[str]
    trip_breaker: bool
    updated_signals: int = 0


class ResolutionAuditService:
    def __init__(
        self,
        repository: Repository,
        polymarket_client: GammaPolymarketClient,
        circuit_breaker_service: CircuitBreakerService,
    ):
        self.repository = repository
        self.polymarket_client = polymarket_client
        self.circuit_breaker_service = circuit_breaker_service

    def audit_market(self, market_id: str) -> ResolutionAuditResult:
        result = self.polymarket_client.get_market(market_id)
        if result is None:
            return ResolutionAuditResult(
                market_id=market_id,
                match=True,
                polymarket_closed=False,
                polymarket_uma_status="missing",
                polymarket_resolved_outcome=None,
                local_resolved_outcome=None,
                status="unavailable",
                local_source=None,
                polymarket_source="gamma_api",
                raw_local_payload=None,
                raw_polymarket_payload=None,
                trip_breaker=False,
            )

        _, raw_payload = result
        return self._audit_payload(market_id, raw_payload, polymarket_source="gamma_api")

    def audit_event(self, event_slug: str) -> list[ResolutionAuditResult]:
        """Refresh one Gamma event and audit all sibling markets with pending signals."""
        results: list[ResolutionAuditResult] = []
        for market, raw_payload in self.polymarket_client.get_event_markets_by_slug(event_slug):
            existing = self.repository.get_market(market.id)
            self.repository.upsert_market(
                market,
                raw_payload,
                module_id=str(existing["module_id"]) if existing is not None else None,
            )
            latest = self.repository.latest_model_signal(market.id)
            if latest is None or latest["outcome_status"] != "pending":
                continue
            results.append(
                self._audit_payload(market.id, raw_payload, polymarket_source="gamma_api")
            )
        return results

    def audit_cached_market(self, market_id: str) -> ResolutionAuditResult:
        """Audit a locally cached Gamma payload without making a network request."""
        market = self.repository.get_market(market_id)
        if market is None:
            raise ValueError(f"market not found: {market_id}")
        raw_payload = json.loads(market["raw_payload"] or "{}")
        if not isinstance(raw_payload, dict):
            raise ValueError(f"market payload is not an object: {market_id}")
        return self._audit_payload(
            market_id,
            raw_payload,
            polymarket_source="gamma_cached_payload",
        )

    def _audit_payload(
        self,
        market_id: str,
        raw_payload: dict,
        *,
        polymarket_source: str,
    ) -> ResolutionAuditResult:
        state = parse_resolution_state(raw_payload)

        # Local computation
        local_outcome = None
        local_source = "none"
        raw_local_payload = None

        # 1. Priority: recompute from observation
        observation = self.repository.latest_observation(market_id)
        rule_row = self.repository.get_resolution_rule(market_id)
        if (
            observation
            and rule_row
            and rule_row["tradable"]
            and rule_row["variable"]
            and rule_row["threshold"] is not None
            and rule_row["operator"] is not None
        ):
            # Reconstruct minimal rule object for _matches_rule
            class DummyRule:
                threshold = Decimal(str(rule_row["threshold"]))
                operator = rule_row["operator"]

            matches = _matches_rule(observation["value"], DummyRule())
            local_outcome = "yes" if matches else "no"
            local_source = "recomputed_observation"
            raw_local_payload = json.dumps(
                {"value": str(observation["value"]), "rule": dict(rule_row)}
            )
        else:
            # 2. Fallback: model_signals
            signal = self.repository.latest_model_signal(market_id)
            if (
                signal
                and signal["outcome_status"] == "resolved"
                and signal["settlement_source"] != "polymarket_gamma_resolution"
            ):
                local_outcome = signal["resolved_outcome"]
                local_source = "model_signals"
                raw_local_payload = json.dumps({"resolved_outcome": local_outcome})

        if not state.is_resolved:
            status = "unavailable"
            if (
                state.closed
                and state.uma_resolution_status == "resolved"
                and not state.resolved_outcome
            ):
                status = "ambiguous"

            audit = ResolutionAuditResult(
                market_id=market_id,
                match=True,
                polymarket_closed=state.closed,
                polymarket_uma_status=state.uma_resolution_status,
                polymarket_resolved_outcome=None,
                local_resolved_outcome=local_outcome,
                status=status,
                local_source=local_source,
                polymarket_source=polymarket_source,
                raw_local_payload=raw_local_payload,
                raw_polymarket_payload=(
                    json.dumps(compact_resolution_payload(raw_payload)) if raw_payload else None
                ),
                trip_breaker=False,
            )
            self.repository.save_resolution_audit(audit)
            return audit

        polymarket_outcome = state.resolved_outcome

        if not local_outcome:
            status = "unavailable"
            match = True
        elif local_outcome == polymarket_outcome:
            status = "match"
            match = True
        else:
            status = "mismatch"
            match = False

        trip_breaker = not match
        audit = ResolutionAuditResult(
            market_id=market_id,
            match=match,
            polymarket_closed=state.closed,
            polymarket_uma_status=state.uma_resolution_status,
            polymarket_resolved_outcome=polymarket_outcome,
            local_resolved_outcome=local_outcome,
            status=status,
            local_source=local_source,
            polymarket_source=polymarket_source,
            raw_local_payload=raw_local_payload,
            raw_polymarket_payload=(
                json.dumps(compact_resolution_payload(raw_payload)) if raw_payload else None
            ),
            trip_breaker=trip_breaker,
        )
        audit_id = self.repository.save_resolution_audit(audit)

        if trip_breaker:
            self.circuit_breaker_service.trip(
                reason=f"Resolution mismatch on {market_id}: local={local_outcome}, polymarket={polymarket_outcome}",
                by="resolution_audit",
                audit_id=audit_id,
            )
        latest_signal = self.repository.latest_model_signal(market_id)
        self.repository.backfill_model_signal_event_identity(market_id)
        updated_signals = 0
        if latest_signal is not None and latest_signal["outcome_status"] == "pending":
            updated_signals = self.repository.settle_model_signals_for_market(
                market_id,
                resolved_outcome=polymarket_outcome,
                settlement_source="polymarket_gamma_resolution",
            )
        return ResolutionAuditResult(
            **{
                **audit.__dict__,
                "updated_signals": updated_signals,
            }
        )
