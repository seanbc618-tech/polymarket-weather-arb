from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.adapters.polymarket.translator import translate_market
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import (
    OrderAttempt,
    OrderIntent,
    build_close_confirm_phrase,
    exit_order_idempotency_key,
)
from polymarket_weather_arb.services.circuit_breaker_service import live_execution_blocked
from polymarket_weather_arb.services.compliance_service import ComplianceService
from polymarket_weather_arb.services.live_monitor_service import is_fresh_reconciliation
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.storage.repositories import Repository

# Polymarket CLOB common tick; also accept finer 0.001 ticks used by smoke paths.
_PRICE_TICK = Decimal("0.001")
_OPENISH_ORDER_STATUSES = {"open", "live", "unmatched", "active", "pending", "submitted"}


class PositionExitService:
    def __init__(self, repository: Repository, polymarket_client: PolymarketClient) -> None:
        self.repository = repository
        self.polymarket_client = polymarket_client

    def preview_close(
        self,
        *,
        settings: Settings,
        market_id: str,
        outcome: str,
        size: Decimal | None = None,
        percent: Decimal | None = None,
    ) -> dict[str, Any]:
        market_row = self.repository.get_market(market_id)
        if not market_row:
            raise ValueError(f"Market {market_id} not found")

        latest = self.repository.latest_successful_reconciliation()
        reconciliation_fresh = is_fresh_reconciliation(latest)
        if not reconciliation_fresh:
            raise ValueError("Reconciliation missing or stale. Run reconcile first.")

        positions = self.repository.list_positions(market_id=market_id, nonzero_only=True)
        target_position = None
        for p in positions:
            if p["outcome"].lower() == outcome.lower():
                target_position = p
                break

        if not target_position:
            raise ValueError(
                f"No nonzero position found for outcome {outcome} in market {market_id}"
            )

        actual_size = Decimal(str(target_position["size"]))
        if actual_size <= 0:
            raise ValueError("Position size is zero or negative.")

        if size is not None:
            close_size = Decimal(str(size))
        elif percent is not None:
            if not (Decimal("1") <= percent <= Decimal("100")):
                raise ValueError("Percent must be between 1 and 100")
            close_size = actual_size * (percent / Decimal("100"))
        else:
            close_size = actual_size

        if close_size <= 0:
            raise ValueError("Close size must be > 0")
        if close_size > actual_size:
            raise ValueError(f"Close size {close_size} exceeds actual position size {actual_size}")

        market_payload = json.loads(market_row["raw_payload"])
        market = translate_market(market_payload)

        if outcome.lower() == "yes":
            token_id = market.yes_token_id
        elif outcome.lower() == "no":
            token_id = market.no_token_id
        else:
            raise ValueError("Outcome must be YES or NO")

        if not token_id:
            raise ValueError(f"Cannot map token ID for outcome {outcome}")

        snapshot, _ = self.polymarket_client.get_token_order_book(token_id)
        best_bid = snapshot.best_bid

        if best_bid is None:
            raise ValueError(f"No bids on the order book for token {token_id}")

        quote_age = (datetime.now(timezone.utc) - snapshot.fetched_at).total_seconds()

        if quote_age > settings.stale_order_book_seconds:
            raise ValueError(
                f"Order book quote is stale ({quote_age:.1f}s > "
                f"{settings.stale_order_book_seconds}s); refresh and retry"
            )

        estimated_usdc = best_bid * close_size
        min_acceptable_price = max(Decimal("0"), best_bid - settings.slippage_buffer)

        return {
            "market_id": market_id,
            "outcome": outcome,
            "token_id": token_id,
            "actual_size": actual_size,
            "close_size": close_size,
            "best_bid": best_bid,
            "estimated_usdc": estimated_usdc,
            "quote_age_s": quote_age,
            "reconciliation_fresh": reconciliation_fresh,
            "min_acceptable_price": min_acceptable_price,
            "max_slippage": settings.slippage_buffer,
        }

    def close_live(
        self,
        *,
        settings: Settings,
        market_id: str,
        outcome: str,
        price: Decimal,
        size: Decimal,
        size_text: str,
        max_slippage: Decimal,
        confirm: str | None = None,
        compliance_service: ComplianceService | None = None,
        on_submitted: Callable[[int], None] | None = None,
        auto_exit: bool = False,
        auto_exit_profile_name: str | None = None,
        auto_rationale: str | None = None,
    ) -> dict[str, Any]:
        """Limit SELL exit. Manual path requires exact confirm; auto path is gated.

        Manual (default): exact confirm phrase required before any SDK mutation.
        Auto (``auto_exit=True``): requires a full-live profile or
        ``AUTO_EXIT_ENABLED`` and skips human confirm; still applies all live
        safety gates and revalidates quote.

        Does not auto-retry, auto-cancel, or auto-continue selling after submit.

        After the exchange accepts a SELL, this method never raises: post-submit
        get_order/reconcile failures are recorded as submitted_unverified or
        reconcile_failed so local audit rows are not lost to outer rollbacks.
        ``on_submitted`` is invoked immediately after the submitted attempt is
        persisted so callers can commit the SQLite transaction before verification.
        """
        outcome_norm = outcome.strip().upper()
        if outcome_norm not in {"YES", "NO"}:
            raise ValueError("Outcome must be YES or NO")

        # 1) Manual confirm or auto-exit gate — before SDK mutation.
        if auto_exit:
            if auto_exit_profile_name != "full-live" and not settings.auto_exit_enabled:
                raise ValueError("AUTO_EXIT_ENABLED=false blocks automatic SELL exits")
        else:
            expected = build_close_confirm_phrase(
                market_id=market_id, outcome=outcome_norm, size_text=size_text
            )
            if confirm != expected:
                raise ValueError(
                    f"Confirm phrase mismatch. Expected exact: {expected!r}. "
                    "No SELL mutation was attempted."
                )

        if size <= 0:
            raise ValueError("Close size must be > 0; naked or negative SELL is forbidden")
        self._validate_price(price)
        if max_slippage < 0:
            raise ValueError("max-slippage must be non-negative")

        # 2) Live gates (credentials / compliance / kill switch / circuit breaker).
        if settings.trading_disabled:
            raise ValueError("TRADING_DISABLED=true blocks live SELL exits")
        settings.ensure_live_trading_ready()
        compliance = (compliance_service or ComplianceService(settings)).check_live_allowed()
        if not compliance.ok:
            raise ValueError(f"Compliance blocked live SELL: {compliance.reason}")
        breaker = live_execution_blocked(self.repository)
        if breaker:
            raise ValueError(breaker)

        # 3) Fresh reconciliation + real position (no short / no oversell).
        latest = self.repository.latest_successful_reconciliation()
        if not is_fresh_reconciliation(latest):
            raise ValueError("Reconciliation missing or stale. Run reconcile first.")

        market_row = self.repository.get_market(market_id)
        if not market_row:
            raise ValueError(f"Market {market_id} not found")

        positions = self.repository.list_positions(market_id=market_id, nonzero_only=True)
        target_position = None
        for p in positions:
            if str(p["outcome"]).upper() == outcome_norm:
                target_position = p
                break
        if not target_position:
            raise ValueError(
                f"No nonzero position found for outcome {outcome_norm} in market {market_id}"
            )
        actual_size = Decimal(str(target_position["size"]))
        if actual_size <= 0:
            raise ValueError("Position size is zero or negative; naked SELL forbidden")
        if size > actual_size:
            raise ValueError(
                f"Close size {size} exceeds actual position size {actual_size}; oversell blocked"
            )

        market_payload = json.loads(market_row["raw_payload"])
        market = translate_market(market_payload)
        token_id = market.yes_token_id if outcome_norm == "YES" else market.no_token_id
        if not token_id:
            raise ValueError(f"Cannot map token ID for outcome {outcome_norm}")

        side = f"sell_{outcome_norm.lower()}"
        idempotency_key = exit_order_idempotency_key(
            market_id=market_id,
            outcome=outcome_norm,
            token_id=token_id,
            reconciliation_id=str(latest["id"]),
        )
        duplicate = self.repository.active_live_order_intent(market_id, side)
        if duplicate is not None:
            raise ValueError(
                f"Duplicate active SELL blocked: intent {duplicate['id']} "
                f"for market {market_id} side {side}"
            )
        open_order = self.repository.active_open_order(
            market_id=market_id, token_id=token_id, side=side
        )
        if open_order is not None:
            raise ValueError(
                f"Duplicate active SELL blocked: open order "
                f"{open_order['exchange_order_id']} for token {token_id}"
            )

        # 4) Fresh token-specific order book + slippage vs best bid.
        best_bid = self._require_fresh_bid(
            token_id=token_id,
            price=price,
            max_slippage=max_slippage,
            stale_order_book_seconds=settings.stale_order_book_seconds,
        )
        # Auto-exit must re-check quote/edge immediately before mutation.
        if auto_exit:
            best_bid = self._require_fresh_bid(
                token_id=token_id,
                price=price,
                max_slippage=max_slippage,
                stale_order_book_seconds=settings.stale_order_book_seconds,
            )

        # 5) Persist exit intent before any mutation network call.
        notional = price * size
        if auto_exit:
            rationale = (
                f"auto-exit SELL outcome={outcome_norm} "
                f"max_slippage={max_slippage} best_bid={best_bid}"
            )
            if auto_rationale:
                rationale = f"{rationale}; {auto_rationale}"
        else:
            rationale = (
                f"confirmed close-live SELL outcome={outcome_norm} "
                f"max_slippage={max_slippage} best_bid={best_bid}"
            )
        intent = OrderIntent(
            market_id=market_id,
            side=side,
            token_id=token_id,
            limit_price=price,
            size=size,
            notional=notional,
            rationale=rationale,
            dry_run=False,
            status="submitted",
            idempotency_key=idempotency_key,
        )
        intent_id, created = self.repository.save_order_intent_once(intent)
        if not created:
            raise ValueError(
                f"Duplicate SELL attempt blocked: intent {intent_id} already used "
                f"reconciliation {latest['id']}"
            )
        request_payload: dict[str, object] = {
            "step": "submit",
            "market_id": market_id,
            "token_id": token_id,
            "outcome": outcome_norm,
            "side": "SELL",
            "price": str(price),
            "size": str(size),
            "max_slippage": str(max_slippage),
            "best_bid": str(best_bid),
            "confirm": confirm,
            "auto_exit": auto_exit,
        }

        place_sell = getattr(self.polymarket_client, "place_sell_limit_order", None)
        if not callable(place_sell):
            error = "client missing place_sell_limit_order; refusing ambiguous BUY path"
            self.repository.save_order_attempt(
                OrderAttempt(
                    intent_id=intent_id,
                    request_payload=request_payload,
                    response_payload=None,
                    status="failed",
                    error=error,
                )
            )
            self.repository.update_order_intent_status(intent_id, "failed")
            raise ValueError(error)

        try:
            submit_response = place_sell(
                token_id=token_id,
                price=str(price),
                size=str(size),
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
            self.repository.update_order_intent_status(intent_id, "failed")
            return {
                "ok": False,
                "intent_id": intent_id,
                "order_id": None,
                "status": "failed",
                "error": str(exc),
                "token_id": token_id,
                "side": side,
            }

        self.repository.save_order_attempt(
            OrderAttempt(
                intent_id=intent_id,
                request_payload=request_payload,
                response_payload=submit_response if isinstance(submit_response, dict) else None,
                status="submitted",
            )
        )
        self.repository.update_order_intent_status(intent_id, "submitted")
        # Durable audit FIRST: exchange already accepted the SELL. Roundtrip
        # binding must never block or roll back this trail.
        if on_submitted is not None:
            on_submitted(intent_id)

        warnings: list[str] = []
        try:
            self.repository.record_roundtrip_sell_intent(market_id, intent_id, status="sell_open")
        except Exception as exc:
            warnings.append(
                f"roundtrip sell binding failed after exchange submit (order audit retained): {exc}"
            )

        order_id = str(
            (submit_response or {}).get("order_id") or (submit_response or {}).get("id") or ""
        )
        final_status = "submitted"
        verified = False
        warning: str | None = "; ".join(warnings) if warnings else None
        checked_order: dict[str, Any] | None = None
        reconciliation: dict[str, Any] | None = None

        if not order_id:
            final_status = "submitted_unverified"
            missing_id_msg = (
                "SELL was submitted but exchange response had no order id; "
                "status is unverified — do not re-submit; run reconcile/get-order manually"
            )
            warning = f"{warning}; {missing_id_msg}" if warning else missing_id_msg
            self.repository.update_order_intent_status(intent_id, final_status)
            return {
                "ok": True,
                "verified": False,
                "intent_id": intent_id,
                "order_id": None,
                "status": final_status,
                "warning": warning,
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
                "best_bid": best_bid,
                "checked_order": None,
                "reconciliation": None,
                "submit_response": submit_response,
            }

        # Post-submit verification must never raise: exchange already has the order.
        try:
            checked_order = self.polymarket_client.get_order(order_id)
            self.repository.save_order_attempt(
                OrderAttempt(
                    intent_id=intent_id,
                    request_payload={"step": "check", "order_id": order_id},
                    response_payload=checked_order if isinstance(checked_order, dict) else None,
                    status="checked",
                )
            )
            order_status = str(
                (checked_order or {}).get("status") or (submit_response or {}).get("status") or ""
            ).lower()
            final_status = "open" if order_status in _OPENISH_ORDER_STATUSES else order_status
        except Exception as exc:
            self.repository.save_order_attempt(
                OrderAttempt(
                    intent_id=intent_id,
                    request_payload={"step": "check", "order_id": order_id},
                    response_payload=None,
                    status="check_failed",
                    error=str(exc),
                )
            )
            final_status = "submitted_unverified"
            check_msg = (
                f"SELL submitted as order {order_id} but get_order failed: {exc}. "
                "Local audit retained; do not re-submit — verify on exchange and reconcile."
            )
            warning = f"{warning}; {check_msg}" if warning else check_msg
            self.repository.update_order_intent_status(intent_id, final_status)
            return {
                "ok": True,
                "verified": False,
                "intent_id": intent_id,
                "order_id": order_id,
                "status": final_status,
                "warning": warning,
                "error": str(exc),
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
                "best_bid": best_bid,
                "checked_order": None,
                "reconciliation": None,
                "submit_response": submit_response,
            }

        try:
            reconciliation = ReconciliationService(
                self.polymarket_client, self.repository
            ).reconcile()
        except Exception as exc:
            reconciliation = {"status": "adapter-error", "error": str(exc)}

        recon_status = str((reconciliation or {}).get("status") or "")
        if recon_status == "ok":
            self.repository.save_order_attempt(
                OrderAttempt(
                    intent_id=intent_id,
                    request_payload={"step": "reconcile", "order_id": order_id},
                    response_payload=reconciliation,
                    status="reconciled",
                )
            )
            verified = True
        else:
            recon_error = (reconciliation or {}).get("error") or recon_status or "unknown"
            self.repository.save_order_attempt(
                OrderAttempt(
                    intent_id=intent_id,
                    request_payload={"step": "reconcile", "order_id": order_id},
                    response_payload=reconciliation,
                    status="reconcile_failed",
                    error=str(recon_error),
                )
            )
            final_status = "reconcile_failed"
            recon_msg = (
                f"SELL submitted as order {order_id} but reconciliation status="
                f"{recon_status or 'unknown'} ({recon_error}). "
                "Order may be live; do not re-submit — re-run reconcile manually."
            )
            warning = f"{warning}; {recon_msg}" if warning else recon_msg

        self.repository.update_order_intent_status(intent_id, final_status)
        if recon_status == "ok":
            try:
                from polymarket_weather_arb.services.roundtrip_status_service import (
                    RoundtripStatusService,
                )

                RoundtripStatusService(self.repository).get_status(market_id)
            except Exception as exc:
                roundtrip_msg = (
                    "roundtrip status refresh failed after successful SELL reconciliation "
                    f"(order audit retained): {exc}"
                )
                warning = f"{warning}; {roundtrip_msg}" if warning else roundtrip_msg
        return {
            "ok": True,
            "verified": verified,
            "intent_id": intent_id,
            "order_id": order_id,
            "status": final_status,
            "warning": warning,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "best_bid": best_bid,
            "checked_order": checked_order,
            "reconciliation": reconciliation,
            "submit_response": submit_response,
        }

    def _require_fresh_bid(
        self,
        *,
        token_id: str,
        price: Decimal,
        max_slippage: Decimal,
        stale_order_book_seconds: int,
    ) -> Decimal:
        snapshot, _ = self.polymarket_client.get_token_order_book(token_id)
        best_bid = snapshot.best_bid
        if best_bid is None:
            raise ValueError(f"No bids on the order book for token {token_id}")
        quote_age = (datetime.now(timezone.utc) - snapshot.fetched_at).total_seconds()
        if quote_age > stale_order_book_seconds:
            raise ValueError(
                f"Order book quote is stale ({quote_age:.1f}s > "
                f"{stale_order_book_seconds}s); refresh and retry"
            )
        if price < best_bid:
            slippage = best_bid - price
            if slippage > max_slippage:
                raise ValueError(
                    f"SELL price {price} is {slippage} below best bid {best_bid}, "
                    f"exceeding max-slippage {max_slippage}"
                )
        return best_bid

    @staticmethod
    def _validate_price(price: Decimal) -> None:
        if price <= 0 or price >= 1:
            raise ValueError("limit price must be between 0 and 1 exclusive")
        try:
            quantized = price.quantize(_PRICE_TICK)
        except InvalidOperation as exc:
            raise ValueError(f"invalid price tick for {price}") from exc
        if quantized != price:
            raise ValueError(
                f"price {price} is not on tick size {_PRICE_TICK}; adjust to a valid tick"
            )
