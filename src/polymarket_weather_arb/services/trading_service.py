from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import (
    OrderAttempt,
    build_order_intent,
    build_proposed_order,
    live_order_idempotency_key,
    preflight_buy_rejection_reason,
)
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import RiskContext, RiskEngine
from polymarket_weather_arb.domain.source_grade import (
    RESEARCH_FORECAST,
    UNKNOWN,
    is_live_eligible_forecast_grade,
    live_forecast_rejection_reason,
    normalize_source_grade,
)
from polymarket_weather_arb.services.circuit_breaker_service import live_execution_blocked
from polymarket_weather_arb.storage.repositories import Repository


class TradingService:
    def __init__(
        self, settings: Settings, client: PolymarketClient, repository: Repository
    ) -> None:
        self.settings = settings
        self.client = client
        self.repository = repository
        self.risk_engine = RiskEngine(settings)

    def trade(
        self,
        *,
        analysis: Analysis,
        yes_token_id: str | None,
        no_token_id: str | None,
        context: RiskContext,
        dry_run: bool,
        source_grade: str = UNKNOWN,
        allow_research_forecast_live: bool = False,
        on_submitted: Callable[[int], None] | None = None,
        market_payload: dict | None = None,
        max_notional_override: Decimal | None = None,
        opportunity_id: str | None = None,
    ) -> tuple[int | None, list[str]]:
        max_notional = min(self.settings.max_order_usdc, Decimal("25"))
        if max_notional_override is not None:
            max_notional = min(max_notional, max(Decimal("0"), max_notional_override))
        payload = market_payload
        if payload is None:
            market_row = self.repository.get_market(analysis.market_id)
            if market_row is not None:
                import json

                try:
                    payload = json.loads(market_row["raw_payload"])
                except Exception:
                    payload = None

        preflight_reason = preflight_buy_rejection_reason(
            analysis, max_notional, market_payload=payload
        )

        # Size under cap first so risk/source-grade gates still run; exchange
        # minima are enforced immediately before any SDK mutation below.
        order = build_proposed_order(
            analysis,
            yes_token_id,
            no_token_id,
            max_notional,
            market_payload=payload,
            enforce_exchange_minimum=False,
        )
        if order is None:
            return None, [
                preflight_reason or "latest analysis does not produce an executable order"
            ]

        decision = self.risk_engine.evaluate(order, context)
        self.repository.save_risk_decision(decision)
        if not decision.accepted:
            # risk_decisions is sufficient for a repeatedly saturated market.
            # Keep rejected intents for other blockers because existing operator
            # flows use them to explain recoverable readiness failures.
            if any("market exposure exceeds" in reason for reason in decision.reasons):
                return None, decision.reasons
            intent = build_order_intent(order, "; ".join(decision.reasons), dry_run, "rejected")
            intent_id = self.repository.save_order_intent(intent)
            return intent_id, decision.reasons
        live_source_allowed = is_live_eligible_forecast_grade(source_grade) or (
            allow_research_forecast_live
            and normalize_source_grade(source_grade) == RESEARCH_FORECAST
        )
        if not dry_run and not live_source_allowed:
            reason = live_forecast_rejection_reason(source_grade) or (
                "forecast source is not official_forecast"
            )
            intent = build_order_intent(order, reason, dry_run, "rejected")
            intent_id = self.repository.save_order_intent(intent)
            return intent_id, [reason]

        if not dry_run:
            sibling = self.repository.active_live_sibling_market(order.market_id)
            if sibling is not None:
                reason = (
                    f"event already has active exposure in market {sibling['id']}; "
                    "only the best bucket per city/date may be active. "
                    "Close or cancel the existing bucket and reconcile before retrying"
                )
                intent = build_order_intent(order, reason, dry_run, "rejected")
                intent_id = self.repository.save_order_intent(intent)
                return intent_id, [reason]
            blocker = live_execution_blocked(self.repository)
            if blocker:
                intent = build_order_intent(order, blocker, dry_run, "rejected")
                intent_id = self.repository.save_order_intent(intent)
                return intent_id, [blocker]
            signing_blocker = self._order_signing_blocker()
            if signing_blocker:
                intent = build_order_intent(order, signing_blocker, dry_run, "rejected")
                intent_id = self.repository.save_order_intent(intent)
                return intent_id, [signing_blocker]
        # Exchange minimum preflight (after policy gates, before SDK / durable submit).
        normalized = build_proposed_order(
            analysis,
            yes_token_id,
            no_token_id,
            max_notional,
            market_payload=payload,
            enforce_exchange_minimum=True,
        )
        if normalized is None:
            reason = preflight_reason or (
                "order below exchange minimum notional/size under configured cap"
            )
            intent = build_order_intent(order, reason, dry_run, "rejected")
            intent_id = self.repository.save_order_intent(intent)
            return intent_id, [reason]
        order = normalized
        if opportunity_id is None:
            opportunity_time = analysis.created_at
            if opportunity_time.tzinfo is None:
                opportunity_time = opportunity_time.replace(tzinfo=timezone.utc)
            opportunity_id = opportunity_time.astimezone(timezone.utc).isoformat()
        idempotency_key = (
            live_order_idempotency_key(
                order,
                opportunity_id=opportunity_id,
            )
            if not dry_run
            else None
        )
        if not dry_run:
            duplicate = self.repository.active_live_order_intent(order.market_id, order.side)
            if duplicate is not None:
                reason = f"duplicate active live order intent for market {order.market_id} side {order.side}"
                intent_id = self.repository.save_order_intent(
                    build_order_intent(order, reason, dry_run, "rejected")
                )
                return intent_id, [reason]
            open_order = self.repository.active_open_order(
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
            )
            if open_order is not None:
                reason = (
                    f"duplicate exchange open order {open_order['exchange_order_id']} "
                    f"for market {order.market_id} token {order.token_id}"
                )
                intent_id = self.repository.save_order_intent(
                    build_order_intent(order, reason, dry_run, "rejected")
                )
                return intent_id, [reason]

        status = "dry_run" if dry_run else "submitted"
        intent = build_order_intent(
            order,
            "; ".join(analysis.reasons),
            dry_run,
            status,
            idempotency_key=idempotency_key,
        )
        if dry_run:
            intent_id = self.repository.save_order_intent(intent)
        else:
            intent_id, created = self.repository.save_order_intent_once(intent)
            if not created:
                return intent_id, [
                    f"duplicate analyzed opportunity already recorded as intent {intent_id}"
                ]
        request_payload = {
            "token_id": order.token_id,
            "side": order.side,
            "price": str(order.limit_price),
            "size": str(order.size),
            "order_type": order.order_type,
            "notional": str(order.notional),
        }
        if dry_run:
            self.repository.save_order_attempt(
                OrderAttempt(
                    intent_id=intent_id,
                    request_payload=request_payload,
                    response_payload={"dry_run": True},
                    status="dry_run",
                )
            )
            return intent_id, ["dry-run order recorded"]

        try:
            response = self.client.place_limit_order(
                token_id=order.token_id or "",
                side=order.side,
                price=str(order.limit_price),
                size=str(order.size),
            )
        except Exception as exc:
            self.repository.save_order_attempt(
                OrderAttempt(
                    intent_id=intent_id,
                    request_payload=request_payload,
                    response_payload=None,
                    status="failed",
                    error=str(exc),
                )
            )
            # Failed SDK call must not leave a duplicate-blocking active intent.
            self.repository.update_order_intent_status(intent_id, "failed")
            return intent_id, [f"live order failed: {exc}"]

        accepted, _order_id, attempt_status, reject_tag = _parse_buy_submit_response(response)
        if not accepted:
            # Explicit exchange reject (e.g. post_only would-cross) or missing
            # order id must not leave a ghost "submitted" intent.
            self.repository.save_order_attempt(
                OrderAttempt(
                    intent_id=intent_id,
                    request_payload=request_payload,
                    response_payload=response if isinstance(response, dict) else None,
                    status=attempt_status,
                    error=reject_tag,
                )
            )
            self.repository.update_order_intent_status(intent_id, "failed")
            detail = reject_tag or "exchange did not accept buy order"
            return intent_id, [f"live order not accepted: {detail}"]

        self.repository.save_order_attempt(
            OrderAttempt(
                intent_id=intent_id,
                request_payload=request_payload,
                response_payload=response if isinstance(response, dict) else None,
                status="submitted",
                error=None,
            )
        )
        if on_submitted is not None:
            on_submitted(intent_id)
        reasons = ["live order submitted"]
        # Roundtrip binding is best-effort only. Exchange already accepted the
        # order; a roundtrip write failure must not undo order audit rows.
        if str(order.side).lower().startswith("buy"):
            try:
                self.repository.record_roundtrip_buy_intent(
                    order.market_id, intent_id, status="buy_open"
                )
            except Exception as exc:
                reasons.append(
                    f"roundtrip buy binding failed after exchange submit "
                    f"(order audit retained): {exc}"
                )
        return intent_id, reasons

    def _order_signing_blocker(self) -> str | None:
        validator = getattr(self.client, "validate_order_signing", None)
        if validator is None:
            return None
        result = validator()
        if not isinstance(result, dict) or result.get("ok"):
            return None
        return str(result.get("detail") or result.get("status") or "order signing path is blocked")


def _parse_buy_submit_response(
    response: object,
) -> tuple[bool, str | None, str, str | None]:
    """Classify a BUY place_limit_order payload.

    Returns ``(accepted, order_id, attempt_status, reject_tag)``.

    Only an explicit non-false acceptance **and** a non-empty exchange order id
    count as accepted. Explicit ``ok=False`` rejects (including post-only
    would-cross) fail the intent so they cannot block freezes as ghost
    ``submitted`` rows. Network errors are handled by the caller via exceptions
    and must not be retried here.
    """
    if not isinstance(response, dict):
        return False, None, "failed", "non_dict_response"

    ok = response.get("ok")
    if ok is False:
        tag = _classify_buy_reject_tag(response)
        return False, None, "rejected", tag

    order_id = str(
        response.get("order_id") or response.get("orderID") or response.get("id") or ""
    ).strip()
    if not order_id:
        return False, None, "failed", "missing_order_id"

    # ok True, or absent/legacy status payloads with an order id.
    return True, order_id, "submitted", None


def _classify_buy_reject_tag(response: dict) -> str:
    """Map exchange reject payload to a stable attempt tag (not intent status)."""
    blob_parts = [
        str(response.get("error") or ""),
        str(response.get("message") or ""),
        str(response.get("reason") or ""),
        str(response.get("status") or ""),
        str(response.get("errorMsg") or ""),
    ]
    blob = " ".join(blob_parts).lower()
    if any(
        key in blob
        for key in (
            "post_only",
            "post-only",
            "would cross",
            "would_cross",
            "crosses the book",
            "cross the book",
            "invalid post-only",
        )
    ):
        return "post_only_would_cross"
    if blob.strip():
        return "exchange_rejected"
    return "exchange_rejected_ok_false"


def age_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - parsed).total_seconds())
