from __future__ import annotations

import logging
import threading
from dataclasses import replace
from itertools import islice
from time import monotonic
from typing import Any, Callable

from polymarket_weather_arb.adapters.http_client import build_httpx_client
from polymarket_weather_arb.adapters.http_reader import safe_http_read
from polymarket_weather_arb.adapters.polymarket.translator import (
    translate_market,
    translate_order_book,
)
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot

logger = logging.getLogger(__name__)

# D2 events can appear later in the day, so missing slugs are not permanent.
# A two-hour retry still discovers D2 events comfortably before their target
# day while cutting repeated guaranteed 404s from generated city/date slugs.
MISSING_EVENT_SLUG_TTL_SECONDS = 2 * 60 * 60
# A missing CLOB book can appear later, so keep this cache much shorter than the
# Gamma event cache. Five minutes matches the strategy cadence and prevents the
# same absent token from being requested by every fast pulse.
MISSING_ORDER_BOOK_TTL_SECONDS = 5 * 60


class OrderNotFoundError(LookupError):
    """Raised when an authenticated order lookup finds no open/known order."""

    def __init__(self, order_id: str, *, detail: str | None = None) -> None:
        self.order_id = order_id
        self.detail = detail
        message = f"order not found: {order_id}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


class GammaPolymarketClient:
    """Gamma/CLOB adapter. Owns at most one authenticated SecureClient per instance."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gamma_base = settings.polymarket_gamma_api_base.rstrip("/")
        self.clob_base = settings.polymarket_clob_api_base.rstrip("/")
        self.data_base = settings.polymarket_data_api_base.rstrip("/")
        self._secure_client: Any | None = None
        # Serializes create/use/invalidate/close so concurrent auth failures cannot
        # close a client still in use or a replacement created by another thread.
        self._secure_op_lock = threading.RLock()
        self._event_slug_cache_lock = threading.Lock()
        self._missing_event_slugs: dict[str, float] = {}
        self._missing_order_books: dict[str, float] = {}
        # Creation counter for tests/ops evidence (no credentials stored).
        self.secure_client_create_count = 0

    def close(self) -> None:
        """Close the cached authenticated SDK client, if any.

        Waits for any in-flight authenticated operation; never closes a client
        while an operation still holds it.
        """
        with self._secure_op_lock:
            client = self._secure_client
            self._secure_client = None
            if client is not None:
                _close_client(client)

    def list_markets(
        self, limit: int = 100, offset: int = 0
    ) -> list[tuple[Market, dict[str, Any]]]:
        params = {"active": "true", "closed": "false", "limit": str(limit), "offset": str(offset)}
        with build_httpx_client(timeout=20, settings=self.settings) as client:
            response = safe_http_read(
                client, "GET", f"{self.gamma_base}/markets", self.settings, params=params
            )
            response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("markets", [])
        markets = []
        for item in items:
            if isinstance(item, dict):
                markets.append((translate_market(item), item))
        return markets

    def get_event_markets_by_slug(self, slug: str) -> list[tuple[Market, dict[str, Any]]]:
        now = monotonic()
        with self._event_slug_cache_lock:
            expires_at = self._missing_event_slugs.get(slug)
            if expires_at is not None and expires_at > now:
                return []
            if expires_at is not None:
                self._missing_event_slugs.pop(slug, None)
        with build_httpx_client(timeout=20, settings=self.settings) as client:
            response = safe_http_read(
                client, "GET", f"{self.gamma_base}/events/slug/{slug}", self.settings
            )
            if response.status_code == 404:
                with self._event_slug_cache_lock:
                    self._missing_event_slugs[slug] = now + MISSING_EVENT_SLUG_TTL_SECONDS
                return []
            response.raise_for_status()
        with self._event_slug_cache_lock:
            self._missing_event_slugs.pop(slug, None)
        event = response.json()
        raw_markets = event.get("markets") or [] if isinstance(event, dict) else []
        markets = []
        for item in raw_markets:
            if isinstance(item, dict):
                payload = {**item, "events": [event]}
                markets.append((translate_market(payload), payload))
        return markets

    def find_markets_by_condition_ids(
        self, condition_ids: list[str]
    ) -> list[tuple[Market, dict[str, Any]]]:
        if not condition_ids:
            return []
        with build_httpx_client(timeout=20, settings=self.settings) as client:
            response = safe_http_read(
                client,
                "GET",
                f"{self.gamma_base}/markets",
                self.settings,
                params={"condition_ids": ",".join(condition_ids)},
            )
            response.raise_for_status()
        payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("markets", [])
        return [(translate_market(item), item) for item in items if isinstance(item, dict)]

    def get_market(self, market_id: str) -> tuple[Market, dict[str, Any]] | None:
        with build_httpx_client(timeout=20, settings=self.settings) as client:
            response = safe_http_read(
                client, "GET", f"{self.gamma_base}/markets/{market_id}", self.settings
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
        payload = response.json()
        if not payload:
            return None
        return translate_market(payload), payload

    def get_order_book(self, market: Market) -> tuple[MarketSnapshot, dict[str, Any]]:
        token_id = market.yes_token_id or market.no_token_id
        if not token_id:
            raise ValueError(f"market {market.id} has no CLOB token id")
        snapshot, payload = self.get_token_order_book(str(token_id))
        return replace(snapshot, market_id=market.id), payload

    def get_token_order_book(self, token_id: str) -> tuple[MarketSnapshot, dict[str, Any]]:
        now = monotonic()
        with self._event_slug_cache_lock:
            expires_at = self._missing_order_books.get(token_id)
            if expires_at is not None and expires_at > now:
                payload = {"token_id": token_id, "not_found": True, "negative_cache": True}
                return translate_order_book("token_book", payload, token_id=str(token_id)), payload
            if expires_at is not None:
                self._missing_order_books.pop(token_id, None)
        with build_httpx_client(timeout=20, settings=self.settings) as client:
            response = safe_http_read(
                client,
                "GET",
                f"{self.clob_base}/book",
                self.settings,
                params={"token_id": token_id},
            )
            if response.status_code == 404:
                with self._event_slug_cache_lock:
                    self._missing_order_books[token_id] = now + MISSING_ORDER_BOOK_TTL_SECONDS
                payload = {"token_id": token_id, "not_found": True, "negative_cache": False}
                return translate_order_book("token_book", payload, token_id=str(token_id)), payload
            response.raise_for_status()
        payload = response.json()
        with self._event_slug_cache_lock:
            self._missing_order_books.pop(token_id, None)
        return translate_order_book("token_book", payload, token_id=str(token_id)), payload

    def place_limit_order(
        self,
        *,
        token_id: str,
        side: str,
        price: str,
        size: str,
    ) -> dict[str, Any]:
        if side not in {"buy_yes", "buy_no"}:
            raise ValueError(f"unsupported side for limit order: {side}")
        # Mutations never auto-retry after ambiguous failure.
        response = self._secure_mutation(
            lambda client: client.place_limit_order(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
        )
        return _model_to_dict(response)

    def place_sell_limit_order(
        self,
        *,
        token_id: str,
        price: str,
        size: str,
    ) -> dict[str, Any]:
        """Place a SELL limit order. Must not reuse BUY path semantics."""
        response = self._secure_mutation(
            lambda client: client.place_limit_order(
                token_id=token_id,
                price=price,
                size=size,
                side="SELL",
            )
        )
        return _model_to_dict(response)

    def get_balances(self) -> dict[str, Any]:
        response = self._secure_read(
            lambda client: client.get_balance_allowance(asset_type="COLLATERAL")
        )
        return _model_to_dict(response)

    def validate_order_signing(self) -> dict[str, Any]:
        try:
            self.settings.ensure_live_trading_ready()
        except ValueError as exc:
            return {"ok": False, "status": "missing-credentials", "detail": str(exc)}
        try:
            from polymarket import SecureClient  # noqa: F401
        except ImportError:
            return {
                "ok": False,
                "status": "missing-sdk",
                "detail": "polymarket-client is not importable",
            }
        return {
            "ok": True,
            "status": "wallet-path-configured",
            "detail": "polymarket-client will sign orders with wallet=POLYMARKET_FUNDER",
        }

    def get_positions(self) -> list[dict[str, Any]]:
        positions = self._secure_read(
            lambda client: client.list_positions(user=self.settings.polymarket_funder)
        )
        return _paginator_items(positions)

    def get_orders(self) -> list[dict[str, Any]]:
        orders = self._secure_read(lambda client: client.list_open_orders())
        return _paginator_items(orders)

    def get_order(self, order_id: str) -> dict[str, Any]:
        try:
            order = self._secure_read(lambda client: client.get_order(order_id=order_id))
        except OrderNotFoundError:
            raise
        except Exception as exc:
            if _is_order_absent_error(exc, order_id=order_id):
                raise OrderNotFoundError(order_id, detail=_redacted_error_text(exc)) from exc
            raise
        payload = _model_to_dict(order)
        if not payload or _order_payload_absent(payload):
            raise OrderNotFoundError(order_id, detail="empty or not-open order payload")
        return payload

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        response = self._secure_mutation(lambda client: client.cancel_order(order_id=order_id))
        return _model_to_dict(response)

    def validate_redemption_signing(self) -> dict[str, Any]:
        """Report whether this wallet can use the official redemption path."""
        try:
            self.settings.ensure_live_trading_ready()
        except ValueError as exc:
            return {"ok": False, "status": "missing-credentials", "detail": str(exc)}
        try:
            with self._secure_op_lock:
                client = self._get_or_create_secure_client_locked()
                wallet_type_value = getattr(client, "wallet_type", None)
                if not wallet_type_value:
                    # Compatibility fallback for older SDKs and test doubles.
                    context = getattr(client, "_ctx", None)
                    wallet_type_value = getattr(context, "wallet_type", None)
                wallet_type = str(wallet_type_value or "unknown")
        except Exception as exc:
            return {
                "ok": False,
                "status": "wallet-readiness-failed",
                "detail": _redacted_error_text(exc),
            }
        if wallet_type == "EOA":
            return {
                "ok": True,
                "status": "eoa-direct-ready",
                "wallet_type": wallet_type,
                "detail": "official SDK will broadcast the CTF redemption from the EOA",
            }
        if wallet_type not in {"POLY_PROXY", "GNOSIS_SAFE", "DEPOSIT_WALLET"}:
            return {
                "ok": False,
                "status": "unsupported-wallet-type",
                "wallet_type": wallet_type,
                "detail": f"official SDK redemption wallet type is unsupported: {wallet_type}",
            }
        if not self.settings.builder_credentials_ready():
            return {
                "ok": False,
                "status": "builder-credentials-not-ready",
                "wallet_type": wallet_type,
                "detail": (
                    "gasless redemption requires the complete Builder credential triple; "
                    f"status={self.settings.builder_credentials_status()}"
                ),
            }
        return {
            "ok": True,
            "status": "gasless-builder-ready",
            "wallet_type": wallet_type,
            "detail": "official SDK gasless redemption is configured",
        }

    def redeem_positions(
        self,
        *,
        condition_id: str,
        on_submitted: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Redeem one resolved condition and wait for a terminal transaction outcome.

        This is a mutation and is never replayed by the adapter. ``on_submitted``
        runs immediately after the SDK returns a transaction handle so callers
        can durably record its identifiers before waiting for confirmation.
        """
        condition_id = str(condition_id or "").strip()
        if not condition_id:
            raise ValueError("condition_id is required for redemption")

        def redeem(client: Any) -> dict[str, Any]:
            handle = client.redeem_positions(
                condition_id=condition_id,
                metadata=f"weather-autopilot redeem {condition_id}",
            )
            submitted = {
                "condition_id": condition_id,
                "transaction_id": getattr(handle, "transaction_id", None),
                "transaction_hash": getattr(handle, "transaction_hash", None),
            }
            if on_submitted is not None:
                on_submitted(dict(submitted))
            outcome = handle.wait()
            return {
                **submitted,
                "transaction_id": getattr(
                    outcome,
                    "transaction_id",
                    submitted["transaction_id"],
                ),
                "transaction_hash": getattr(
                    outcome,
                    "transaction_hash",
                    submitted["transaction_hash"],
                ),
                "status": "confirmed",
            }

        return self._secure_mutation(redeem)

    def get_trades(self) -> list[dict[str, Any]]:
        return self._secure_read(_account_trade_items)

    def stream_api_credentials(self) -> Any:
        """Return the cached SDK credentials for the process-local user stream.

        The dashboard composition root calls this once before opening the async
        User Channel. Reusing these credentials prevents two official SDK
        clients from concurrently running the create-or-derive bootstrap.
        """
        with self._secure_op_lock:
            client = self._get_or_create_secure_client_locked()
            return client.credentials

    def _secure_read(self, operation: Callable[[Any], Any]) -> Any:
        """Idempotent authenticated read. Recreate the secure client once on auth failure."""
        with self._secure_op_lock:
            client = self._get_or_create_secure_client_locked()
            try:
                return operation(client)
            except Exception as exc:
                if not _is_auth_error(exc):
                    raise
                logger.warning(
                    "authenticated read auth failure; invalidating secure client once: %s",
                    _redacted_error_text(exc),
                )
                self._invalidate_secure_client_locked()
                client = self._get_or_create_secure_client_locked()
                return operation(client)

    def _secure_mutation(self, operation: Callable[[Any], Any]) -> Any:
        """Exchange mutation path. Never auto-replays after failure."""
        with self._secure_op_lock:
            client = self._get_or_create_secure_client_locked()
            return operation(client)

    def _get_or_create_secure_client_locked(self):
        """Create or return cached SecureClient. Caller must hold ``_secure_op_lock``."""
        if self._secure_client is not None:
            return self._secure_client
        try:
            from polymarket import SecureClient
        except ImportError as exc:
            raise RuntimeError("authenticated Polymarket calls require polymarket-client") from exc

        self.settings.ensure_live_trading_ready()
        logger.info("creating authenticated SecureClient (session reuse)")
        create_kwargs: dict[str, Any] = {
            "private_key": self.settings.polymarket_private_key or "",
            "wallet": self.settings.polymarket_funder,
        }
        if self.settings.builder_credentials_ready():
            from polymarket.auth import BuilderApiKey

            create_kwargs["api_key"] = BuilderApiKey(
                key=self.settings.builder_api_key or "",
                secret=self.settings.builder_secret or "",
                passphrase=self.settings.builder_pass_phrase or "",
            )
        self._secure_client = SecureClient.create(**create_kwargs)
        self.secure_client_create_count += 1
        return self._secure_client

    def _invalidate_secure_client_locked(self) -> None:
        """Drop and close the cached client. Caller must hold ``_secure_op_lock``."""
        client = self._secure_client
        self._secure_client = None
        if client is not None:
            _close_client(client)


def _model_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise ValueError(f"unexpected Polymarket response type: {type(value).__name__}")


def _paginator_items(value: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [_model_to_dict(item) for item in value]
    if hasattr(value, "iter_items"):
        return [_model_to_dict(item) for item in islice(value.iter_items(), limit)]
    if hasattr(value, "first_page"):
        page = value.first_page()
        return [_model_to_dict(item) for item in getattr(page, "items", [])[:limit]]
    return [_model_to_dict(value)]


def _account_trade_items(client: Any) -> list[dict[str, Any]]:
    """Read account trades while containing one known beta-SDK schema mismatch.

    The CLOB API can return a ``MATCHED_NOT_BROADCASTED`` trade before a
    transaction hash exists. polymarket-client 0.1.0b16 requires that field and
    rejects the whole page. Retry the same authenticated read through the SDK
    transport only when the validation error is exclusively that missing field;
    every other field still passes the official ``ClobTrade`` model.
    """
    try:
        return _paginator_items(client.list_account_trades())
    except Exception as exc:
        if not _only_missing_transaction_hash(exc):
            raise
        context = getattr(client, "_ctx", None)
        transport = getattr(context, "secure_clob", None)
        get_json = getattr(transport, "get_json", None)
        if not callable(get_json):
            raise
        raw_page = get_json("/data/trades", params={})
        try:
            items = _validated_pending_trade_compat_items(raw_page)
        except Exception:
            raise exc
        logger.warning(
            "normalized %s account trade(s) with pending transaction hash",
            sum(1 for item in items if item.get("_transaction_hash_pending")),
        )
        return items


def _only_missing_transaction_hash(exc: BaseException) -> bool:
    if type(exc).__name__ != "UnexpectedResponseError":
        return False
    cause = exc.__cause__
    errors = getattr(cause, "errors", None)
    if not callable(errors):
        return False
    details = errors()
    if not isinstance(details, list) or not details:
        return False
    return all(
        detail.get("type") == "missing"
        and tuple(detail.get("loc") or ())[-1:] == ("transaction_hash",)
        for detail in details
        if isinstance(detail, dict)
    ) and all(isinstance(detail, dict) for detail in details)


def _validated_pending_trade_compat_items(raw_page: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_page, dict) or not isinstance(raw_page.get("data"), list):
        raise ValueError("account trades compatibility response malformed")
    from polymarket.models.clob.account import ClobTrade

    items: list[dict[str, Any]] = []
    for raw_item in raw_page["data"]:
        if not isinstance(raw_item, dict):
            raise ValueError("account trade compatibility item malformed")
        payload = dict(raw_item)
        transaction_hash_missing = "transaction_hash" not in payload
        status = str(payload.get("status") or "").removeprefix("TRADE_STATUS_").upper()
        if transaction_hash_missing:
            if status != "MATCHED_NOT_BROADCASTED":
                raise ValueError("only pending trades may omit transaction_hash")
            payload["transaction_hash"] = ""
        validated = ClobTrade.model_validate(payload).model_dump(mode="json")
        if transaction_hash_missing:
            validated["transaction_hash"] = None
            validated["_transaction_hash_pending"] = True
        items.append(validated)
    return items


def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _is_auth_error(exc: BaseException) -> bool:
    """Detect confirmed authentication/credential failures on idempotent reads."""
    name = type(exc).__name__
    if name in {"RequestRejectedError"}:
        status = getattr(exc, "status", None)
        if status in {401, 403}:
            return True
    text = str(exc).lower()
    markers = (
        "unauthorized",
        "unauthenticated",
        "invalid api key",
        "api key",
        "authentication",
        "auth failed",
        "forbidden",
        "credential",
        "derive-api-key",
        "api-key",
    )
    if any(marker in text for marker in markers):
        # Avoid treating ordinary order/business rejections as auth expiry.
        if "order" in text and "api" not in text and "auth" not in text:
            return False
        return True
    return False


def _is_order_absent_error(exc: BaseException, *, order_id: str) -> bool:
    """Return True only for proven missing-order responses, not schema drift."""
    name = type(exc).__name__
    if name == "RequestRejectedError":
        status = getattr(exc, "status", None)
        if status == 404:
            return True
        if status == 400:
            text = str(exc).lower()
            return _message_means_order_absent(text, order_id=order_id)
        return False

    # SDK wraps OpenOrder.parse_response(None) as UnexpectedResponseError whose
    # Pydantic cause has input_value=None. Malformed {} / partial dicts have
    # non-None input and must remain adapter/schema errors.
    if name in {"UnexpectedResponseError", "ValidationError"}:
        if _validation_error_input_is_none(exc):
            return True
        # Do not treat other UnexpectedResponseError / ValidationError as missing.
        return False

    text = str(exc).lower()
    if _message_means_order_absent(text, order_id=order_id):
        return True
    return False


def _message_means_order_absent(text: str, *, order_id: str | None = None) -> bool:
    markers = (
        "no order",
        "order not found",
        "unknown order",
        "order does not exist",
        "could not find order",
    )
    if any(marker in text for marker in markers):
        return True
    # Some APIs return only "not found" plus the requested order id. Do not
    # classify unrelated failures such as "API key not found" as a missing order.
    return bool(order_id and order_id.lower() in text and "not found" in text)


def _validation_error_input_is_none(exc: BaseException) -> bool:
    """True when a ValidationError chain proves the response body was exactly None."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "ValidationError" and hasattr(current, "errors"):
            try:
                errors = current.errors()
            except Exception:
                errors = None
            if errors:
                # parse_response(None) yields model_type with input None on every error.
                if all(err.get("input") is None for err in errors):
                    return True
        current = current.__cause__ or getattr(current, "__context__", None)
    return False


def _order_payload_absent(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("state") or "").lower()
    if status in {"not_found", "not-found", "missing"}:
        return True
    if payload.get("found") is False:
        return True
    # A bare error-shaped body with no order identifiers.
    if "error" in payload and not payload.get("id") and not payload.get("order_id"):
        err = str(payload.get("error") or "").lower()
        return _message_means_order_absent(err)
    return False


def _redacted_error_text(exc: BaseException) -> str:
    text = str(exc)
    # Never echo credential-like material if an SDK error embeds it.
    for token in ("private_key", "api_key", "secret", "password", "bearer "):
        if token in text.lower():
            return f"{type(exc).__name__}: [redacted]"
    if len(text) > 240:
        return f"{type(exc).__name__}: {text[:240]}..."
    return f"{type(exc).__name__}: {text}"
