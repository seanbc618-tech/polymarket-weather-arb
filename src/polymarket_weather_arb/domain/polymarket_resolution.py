from dataclasses import dataclass
import hashlib
import json
from typing import Any


from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class PolymarketResolutionState:
    closed: bool
    uma_resolution_status: str
    outcomes: list[str]
    outcome_prices: list[str]

    def _get_winner_idx(self) -> int | None:
        if not self.closed:
            return None
        if self.uma_resolution_status != "resolved":
            return None
        if not self.outcome_prices:
            return None
        if len(self.outcomes) != len(self.outcome_prices):
            return None

        winner_idx = None
        for i, price_str in enumerate(self.outcome_prices):
            try:
                price = Decimal(price_str)
            except InvalidOperation:
                return None

            if price >= Decimal("0.99"):
                if winner_idx is not None:
                    # Ambiguous: multiple winners
                    return None
                winner_idx = i

        return winner_idx

    @property
    def is_resolved(self) -> bool:
        return self.resolved_outcome is not None

    @property
    def resolved_outcome(self) -> str | None:
        idx = self._get_winner_idx()
        if idx is None:
            return None
        outcome = self.outcomes[idx].strip().lower()
        if outcome in ("yes", "no"):
            return outcome
        return None


def parse_resolution_state(payload: dict[str, Any]) -> PolymarketResolutionState:
    closed = payload.get("closed", False)
    uma_resolution_status = payload.get("umaResolutionStatus", "unresolved")

    outcomes_raw = payload.get("outcomes", "[]")
    if isinstance(outcomes_raw, str):
        try:
            outcomes = json.loads(outcomes_raw)
        except Exception:
            outcomes = []
    else:
        outcomes = outcomes_raw

    outcome_prices_raw = payload.get("outcomePrices", "[]")
    if isinstance(outcome_prices_raw, str):
        try:
            outcome_prices = json.loads(outcome_prices_raw)
        except Exception:
            outcome_prices = []
    else:
        outcome_prices = outcome_prices_raw

    return PolymarketResolutionState(
        closed=bool(closed),
        uma_resolution_status=str(uma_resolution_status).lower(),
        outcomes=[str(o) for o in outcomes] if outcomes else [],
        outcome_prices=[str(p) for p in outcome_prices] if outcome_prices else [],
    )


def compact_resolution_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Audit-grade Gamma resolution fields plus a hash of the full response."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    keys = (
        "id",
        "conditionId",
        "question",
        "slug",
        "eventSlug",
        "closed",
        "active",
        "acceptingOrders",
        "endDate",
        "umaResolutionStatus",
        "outcomes",
        "outcomePrices",
        "resolvedBy",
        "resolutionSource",
        "negRisk",
        "clobTokenIds",
    )
    compact = {key: payload[key] for key in keys if key in payload}
    compact["payload_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return compact
