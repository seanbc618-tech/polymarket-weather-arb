"""Thin Polymarket WebSocket bridge over the official SDK public stream API.

Owns one daemon thread with one asyncio event loop solely to host
``AsyncPublicClient`` / ``AsyncSecureClient`` subscriptions. It normalizes
typed SDK events into a bounded, coalesced in-memory queue and never:

- opens SQLite or touches Repository;
- runs strategy, pricing, BUY, SELL, cancel, or reconciliation;
- imports ``polymarket._internal`` or hand-rolls WebSocket frames.

All database and strategy work stays serialized in the Autopilot pulse loop.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

# Candidate Market Channel capacity for Top-N complete sibling groups. Four
# complete weather events still cover every sibling bucket needed for pricing,
# while keeping the official SDK's per-event parsing load bounded. Remaining
# groups continue through the fair rotation and the existing REST path.
STREAM_CANDIDATE_GROUP_CAP = 4

# How long a market token may go without a trustworthy book/BBO before REST
# fallback is preferred for that token.
MARKET_TOKEN_STALE_SECONDS = 45.0

# Even when the Market Channel looks healthy, re-verify each token via REST
# at least this often (startup/reconnect/stale recovery remain separate).
STREAM_REST_VERIFY_SECONDS = 90.0

# Bounded diagnostic ring (non-coalescible) plus coalesced quote slots.
_MAX_DIAGNOSTIC_EVENTS = 64
_QUEUE_GET_TIMEOUT = 0.05
_SHUTDOWN_JOIN_SECONDS = 5.0

StreamStatus = str  # disabled | connecting | live | degraded | stale


@dataclass(frozen=True)
class StreamQuote:
    """Normalized top-of-book for one outcome token."""

    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    midpoint: Decimal | None
    spread: Decimal | None
    liquidity: Decimal | None
    received_at: float
    source_type: str  # book | best_bid_ask | price_change
    condition_id: str | None = None


@dataclass(frozen=True)
class StreamUserHint:
    """Account activity hint — never treated as exchange fill/order truth."""

    kind: str  # order | trade
    received_at: float
    event_type: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class StreamTickSizeHint:
    token_id: str
    received_at: float
    condition_id: str | None = None


@dataclass(frozen=True)
class StreamResolvedHint:
    condition_id: str
    received_at: float
    token_ids: tuple[str, ...] = ()


NormalizedStreamEvent = StreamQuote | StreamUserHint | StreamTickSizeHint | StreamResolvedHint


@dataclass
class StreamDrainBatch:
    """Coalesced signals drained by the serial Autopilot pulse."""

    quotes: dict[str, StreamQuote] = field(default_factory=dict)
    reconcile_due: bool = False
    user_hints: list[StreamUserHint] = field(default_factory=list)
    tick_size: list[StreamTickSizeHint] = field(default_factory=list)
    resolved: list[StreamResolvedHint] = field(default_factory=list)
    dropped: int = 0
    coalesced: int = 0


@dataclass
class StreamHealth:
    status: StreamStatus = "disabled"
    updated_at: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        """Non-secret health snapshot for SQLite /app."""
        detail = dict(self.detail)
        # Never leak credentials or raw frames.
        for key in list(detail.keys()):
            lowered = key.lower()
            if any(
                secret in lowered
                for secret in ("key", "secret", "credential", "private", "auth", "password")
            ):
                detail.pop(key, None)
        return {
            "status": self.status,
            "updated_at": self.updated_at,
            "detail": detail,
        }


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _mid_spread(
    best_bid: Decimal | None, best_ask: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / Decimal("2"), best_ask - best_bid
    return None, None


def normalize_sdk_events(event: Any) -> list[NormalizedStreamEvent]:
    """Map a typed official SDK event into zero or more normalized records.

    ``price_change`` payloads may carry multiple tokens; each BBO update becomes
    its own quote so the coalescing queue retains both YES and NO. Unknown event
    variants yield an empty list (caller counts them as unknown).
    """
    topic = getattr(event, "topic", None)
    event_type = getattr(event, "type", None)
    payload = getattr(event, "payload", None)
    if payload is None:
        return []
    received_at = time.monotonic()

    if topic == "market" or event_type in {
        "book",
        "best_bid_ask",
        "price_change",
        "tick_size_change",
        "market_resolved",
    }:
        if event_type == "book":
            token_id = str(getattr(payload, "token_id", "") or "")
            if not token_id:
                return []
            bids = getattr(payload, "bids", ()) or ()
            asks = getattr(payload, "asks", ()) or ()
            best_bid = None
            best_ask = None
            bid_liq = Decimal("0")
            ask_liq = Decimal("0")
            if bids:
                # Book levels are typically sorted; take best bid = highest price.
                prices = [_decimal_or_none(getattr(level, "price", None)) for level in bids]
                sizes = [_decimal_or_none(getattr(level, "size", None)) for level in bids]
                valid = [(p, s) for p, s in zip(prices, sizes) if p is not None]
                if valid:
                    best_bid = max(p for p, _ in valid)
                    bid_liq = sum((s or Decimal("0")) for _, s in valid)
            if asks:
                prices = [_decimal_or_none(getattr(level, "price", None)) for level in asks]
                sizes = [_decimal_or_none(getattr(level, "size", None)) for level in asks]
                valid = [(p, s) for p, s in zip(prices, sizes) if p is not None]
                if valid:
                    best_ask = min(p for p, _ in valid)
                    ask_liq = sum((s or Decimal("0")) for _, s in valid)
            midpoint, spread = _mid_spread(best_bid, best_ask)
            liquidity = bid_liq + ask_liq if bids or asks else None
            return [
                StreamQuote(
                    token_id=token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    midpoint=midpoint,
                    spread=spread,
                    liquidity=liquidity,
                    received_at=received_at,
                    source_type="book",
                    condition_id=str(getattr(payload, "market", "") or "") or None,
                )
            ]
        if event_type == "best_bid_ask":
            token_id = str(getattr(payload, "token_id", "") or "")
            if not token_id:
                return []
            best_bid = _decimal_or_none(getattr(payload, "best_bid", None))
            best_ask = _decimal_or_none(getattr(payload, "best_ask", None))
            spread = _decimal_or_none(getattr(payload, "spread", None))
            midpoint, computed_spread = _mid_spread(best_bid, best_ask)
            return [
                StreamQuote(
                    token_id=token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    midpoint=midpoint,
                    spread=spread if spread is not None else computed_spread,
                    liquidity=None,
                    received_at=received_at,
                    source_type="best_bid_ask",
                    condition_id=str(getattr(payload, "market", "") or "") or None,
                )
            ]
        if event_type == "price_change":
            changes = getattr(payload, "price_changes", ()) or ()
            quotes: list[NormalizedStreamEvent] = []
            condition_id = str(getattr(payload, "market", "") or "") or None
            for change in changes:
                token_id = str(getattr(change, "token_id", "") or "")
                if not token_id:
                    continue
                best_bid = _decimal_or_none(getattr(change, "best_bid", None))
                best_ask = _decimal_or_none(getattr(change, "best_ask", None))
                if best_bid is None and best_ask is None:
                    continue
                midpoint, spread = _mid_spread(best_bid, best_ask)
                quotes.append(
                    StreamQuote(
                        token_id=token_id,
                        best_bid=best_bid,
                        best_ask=best_ask,
                        midpoint=midpoint,
                        spread=spread,
                        liquidity=None,
                        received_at=received_at,
                        source_type="price_change",
                        condition_id=condition_id,
                    )
                )
            return quotes
        if event_type == "tick_size_change":
            token_id = str(getattr(payload, "token_id", "") or "")
            if not token_id:
                return []
            return [
                StreamTickSizeHint(
                    token_id=token_id,
                    received_at=received_at,
                    condition_id=str(getattr(payload, "market", "") or "") or None,
                )
            ]
        if event_type == "market_resolved":
            condition_id = str(
                getattr(payload, "market", None) or getattr(payload, "id", None) or ""
            )
            if not condition_id:
                return []
            raw_tokens = getattr(payload, "token_ids", None) or ()
            token_ids = tuple(str(t) for t in raw_tokens if t is not None)
            return [
                StreamResolvedHint(
                    condition_id=condition_id,
                    received_at=received_at,
                    token_ids=token_ids,
                )
            ]
        return []

    if topic == "user" or event_type in {"order", "trade"}:
        if event_type == "order":
            return [
                StreamUserHint(
                    kind="order",
                    received_at=received_at,
                    event_type=str(getattr(payload, "order_event_type", "") or "") or None,
                    status=str(getattr(payload, "status", "") or "") or None,
                )
            ]
        if event_type == "trade":
            return [
                StreamUserHint(
                    kind="trade",
                    received_at=received_at,
                    event_type="trade",
                    status=str(getattr(payload, "status", "") or "") or None,
                )
            ]
        return []

    return []


def normalize_sdk_event(
    event: Any,
) -> NormalizedStreamEvent | None:
    """Compatibility wrapper: first normalized record, or None if empty/unknown."""
    items = normalize_sdk_events(event)
    return items[0] if items else None


class _CoalescingQueue:
    """Thread-safe bounded queue: quotes coalesce by token_id; user → reconcile flag."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotes: OrderedDict[str, StreamQuote] = OrderedDict()
        self._reconcile_due = False
        self._user_hints: list[StreamUserHint] = []
        self._tick_size: list[StreamTickSizeHint] = []
        self._resolved: list[StreamResolvedHint] = []
        self.dropped = 0
        self.coalesced = 0
        self.unchanged_quotes = 0
        self.ignored_quotes = 0
        self.unknown_events = 0
        self.market_events = 0
        self.user_events = 0

    def publish(self, item: Any) -> bool:
        with self._lock:
            if item is None:
                self.unknown_events += 1
                return False
            if isinstance(item, StreamQuote):
                self.market_events += 1
                existing = self._quotes.get(item.token_id)
                if existing is not None and _same_effective_quote(existing, item):
                    self.unchanged_quotes += 1
                    return False
                if existing is not None:
                    self.coalesced += 1
                self._quotes[item.token_id] = item
                self._quotes.move_to_end(item.token_id)
                return True
            if isinstance(item, StreamUserHint):
                self.user_events += 1
                # Collapse duplicate account activity into one reconcile request.
                if self._reconcile_due:
                    self.coalesced += 1
                self._reconcile_due = True
                if len(self._user_hints) < _MAX_DIAGNOSTIC_EVENTS:
                    self._user_hints.append(item)
                else:
                    self.dropped += 1
                return True
            if isinstance(item, StreamTickSizeHint):
                self.market_events += 1
                if len(self._tick_size) < _MAX_DIAGNOSTIC_EVENTS:
                    self._tick_size.append(item)
                else:
                    self.dropped += 1
                return True
            if isinstance(item, StreamResolvedHint):
                self.market_events += 1
                self._reconcile_due = True
                if len(self._resolved) < _MAX_DIAGNOSTIC_EVENTS:
                    self._resolved.append(item)
                else:
                    self.dropped += 1
                return True
            self.unknown_events += 1
            return False

    def ignore_quote(self) -> None:
        with self._lock:
            self.ignored_quotes += 1

    def drain(self) -> StreamDrainBatch:
        with self._lock:
            batch = StreamDrainBatch(
                quotes=dict(self._quotes),
                reconcile_due=self._reconcile_due,
                user_hints=list(self._user_hints),
                tick_size=list(self._tick_size),
                resolved=list(self._resolved),
                dropped=self.dropped,
                coalesced=self.coalesced,
            )
            self._quotes.clear()
            self._reconcile_due = False
            self._user_hints.clear()
            self._tick_size.clear()
            self._resolved.clear()
            return batch

    def counters(self) -> dict[str, int]:
        with self._lock:
            return {
                "pending_quotes": len(self._quotes),
                "dropped": self.dropped,
                "coalesced": self.coalesced,
                "unchanged_quotes": self.unchanged_quotes,
                "ignored_quotes": self.ignored_quotes,
                "unknown_events": self.unknown_events,
                "market_events": self.market_events,
                "user_events": self.user_events,
            }


def _same_effective_quote(previous: StreamQuote, current: StreamQuote) -> bool:
    """Ignore timestamp/source churn when the executable quote did not change.

    A price-change record has no depth. When it repeats an existing book/BBO,
    retaining the prior liquidity is more useful than replacing it with None.
    A real BBO or depth change still enters the queue immediately.
    """
    if (
        previous.best_bid != current.best_bid
        or previous.best_ask != current.best_ask
        or previous.condition_id != current.condition_id
    ):
        return False
    if current.liquidity is None:
        return True
    return previous.liquidity == current.liquidity


@dataclass(frozen=True)
class DesiredSubscription:
    """Derived Market Channel token set (not persisted)."""

    token_ids: tuple[str, ...]
    token_to_market: Mapping[str, str]
    held_tokens: frozenset[str]
    open_order_tokens: frozenset[str]
    candidate_tokens: frozenset[str]

    def __bool__(self) -> bool:
        return bool(self.token_ids)


def select_stream_tokens(
    *,
    positions: Sequence[Mapping[str, Any]],
    open_orders: Sequence[Mapping[str, Any]],
    ranked_opportunities: Sequence[Mapping[str, Any]],
    market_rows: Mapping[str, Mapping[str, Any]],
    candidate_group_cap: int = STREAM_CANDIDATE_GROUP_CAP,
    rotation_slot: int = 0,
) -> DesiredSubscription:
    """Build the Market Channel token set from Repository-shaped rows.

    Priority: nonzero positions → open orders → Top-N complete sibling groups
    from ranked opportunities with fair D0/D1/D2 rotation for residual capacity.
    Held/open tokens are never displaced by candidates.
    """
    token_to_market: dict[str, str] = {}
    held: set[str] = set()
    open_tok: set[str] = set()
    candidate_tok: set[str] = set()

    def _token_for_outcome(market_id: str, outcome: str | None) -> str | None:
        """Map YES/NO/token-id only; never guess the opposite outcome."""
        market = market_rows.get(market_id)
        if market is None:
            return None
        yes = str(market.get("yes_token_id") or "") or None
        no = str(market.get("no_token_id") or "") or None
        raw = str(outcome or "").strip()
        if not raw:
            return None
        upper = raw.upper()
        if upper in {"YES", "Y"}:
            return yes
        if upper in {"NO", "N"}:
            return no
        # positions.outcome may store the exchange asset/token id.
        if yes and raw == yes:
            return yes
        if no and raw == no:
            return no
        return None

    for pos in positions:
        try:
            size = float(pos.get("size") or 0)
        except (TypeError, ValueError):
            size = 0.0
        if abs(size) <= 0:
            continue
        market_id = str(pos.get("market_id") or "")
        if not market_id:
            continue
        token = _token_for_outcome(market_id, pos.get("outcome"))
        if not token:
            continue
        held.add(token)
        token_to_market[token] = market_id

    for order in open_orders:
        try:
            size = float(order.get("size") or 0)
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue
        market_id = str(order.get("market_id") or "")
        token = order.get("token_id")
        if token:
            token_s = str(token)
            open_tok.add(token_s)
            if market_id:
                token_to_market[token_s] = market_id
            continue
        if not market_id:
            continue
        # Only accept an explicit outcome/token; do not default to YES.
        token = _token_for_outcome(market_id, order.get("outcome") or order.get("side"))
        if token:
            open_tok.add(token)
            token_to_market[token] = market_id

    # Complete (city, target_date) groups from ranked opportunities.
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for opp in ranked_opportunities:
        market_id = str(opp.get("market_id") or "")
        if not market_id or market_id not in market_rows:
            continue
        market = market_rows[market_id]
        city = str(opp.get("city") or market.get("city") or market_id)
        target_date = str(opp.get("target_date") or market.get("target_date") or "")
        if not target_date:
            # Try temperature bucket rule fields if present on market_rows.
            target_date = str(market.get("target_date") or "")
        if not target_date:
            continue
        side = str(opp.get("side") or "buy_yes").lower()
        if side in {"buy_no", "sell_no", "no"}:
            token = market.get("no_token_id")
        else:
            token = market.get("yes_token_id")
        if not token:
            continue
        groups.setdefault((city, target_date), []).append((market_id, str(token)))

    # Prefer fair D0/D1/D2 order; append remaining groups so far-horizon
    # opportunities are not dropped when the fair helper filters them.
    all_groups = sorted(groups.keys())
    ordered_groups: list[tuple[str, str]] = []
    if all_groups:
        try:
            from polymarket_weather_arb.services.autopilot_service import (
                select_fair_analysis_groups,
            )

            fair = select_fair_analysis_groups(list(all_groups), rotation_slot)
            seen = set(fair)
            ordered_groups = list(fair) + [g for g in all_groups if g not in seen]
        except Exception:
            ordered_groups = list(all_groups)

    protected = held | open_tok
    selected_groups = 0
    for group in ordered_groups:
        if selected_groups >= candidate_group_cap:
            break
        members = groups.get(group) or []
        if not members:
            continue
        for market_id, token in members:
            if token in protected:
                # Still map, but does not consume candidate capacity uniquely.
                token_to_market[token] = market_id
                continue
            candidate_tok.add(token)
            token_to_market[token] = market_id
        selected_groups += 1

    ordered_tokens = tuple(
        dict.fromkeys([*sorted(held), *sorted(open_tok), *sorted(candidate_tok)])
    )
    return DesiredSubscription(
        token_ids=ordered_tokens,
        token_to_market=dict(token_to_market),
        held_tokens=frozenset(held),
        open_order_tokens=frozenset(open_tok),
        candidate_tokens=frozenset(candidate_tok),
    )


class PolymarketStreamBridge:
    """Daemon-thread host for official SDK market/user stream subscriptions.

    Shadow-safe: may run without affecting Autopilot until the pulse drains it.
    """

    def __init__(
        self,
        *,
        private_key: str | None = None,
        funder: str | None = None,
        api_credentials: Any | None = None,
        enable_user_channel: bool | None = None,
        public_client_factory: Callable[[], Any] | None = None,
        secure_client_factory: Callable[[], Any] | None = None,
        loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
    ) -> None:
        self._private_key = private_key
        self._funder = funder
        self._api_credentials = api_credentials
        self._enable_user = (
            bool(private_key and funder)
            if enable_user_channel is None
            else bool(enable_user_channel)
        )
        self._public_client_factory = public_client_factory
        self._secure_client_factory = secure_client_factory
        self._loop_factory = loop_factory or asyncio.new_event_loop

        self._queue = _CoalescingQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = threading.Event()
        self._started = False

        self._desired_tokens: tuple[str, ...] = ()
        self._token_to_market: dict[str, str] = {}
        self._token_last_quote_at: dict[str, float] = {}
        # Tokens that still need a REST snapshot after resubscribe/reconnect.
        self._rest_backfill_tokens: set[str] = set()
        self._last_market_event_at: float | None = None
        self._last_user_event_at: float | None = None
        self._status: StreamStatus = "disabled"
        self._status_detail: str = ""
        self._subscription_generation = 0
        self._subscription_inflight_generations: set[int] = set()
        self._active_tokens: set[str] = set()
        self._reader_error_count = 0

        # Async handles owned by the bridge loop thread only.
        self._client: Any | None = None
        self._handle: Any | None = None
        self._reader_task: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------------ public

    @property
    def started(self) -> bool:
        return self._started and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the daemon thread + asyncio loop. Idempotent."""
        with self._lock:
            if self._started and self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._set_status("connecting", "starting bridge thread")
            self._thread = threading.Thread(
                target=self._thread_main,
                name="polymarket-stream-bridge",
                daemon=True,
            )
            self._started = True
            self._thread.start()

    def stop(self, *, timeout: float = _SHUTDOWN_JOIN_SECONDS) -> None:
        """Close handles/clients/loop/thread within a bounded timeout."""
        self._stop_event.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                # Signal the loop thread; _async_main finally closes resources.
                loop.call_soon_threadsafe(loop.stop)
            except Exception as exc:
                logger.warning("stream bridge loop stop: %s", exc)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        # Best-effort sync close if the loop never finished shutdown.
        client = self._client
        handle = self._handle
        self._client = None
        self._handle = None
        self._reader_task = None
        if handle is not None:
            try:
                close = getattr(handle, "close", None)
                if close is not None:
                    close()
            except Exception:
                pass
        if client is not None:
            try:
                close = getattr(client, "close", None) or getattr(client, "aclose", None)
                if close is not None:
                    result = close()
                    # Ignore awaitables from a dead loop.
                    if asyncio.iscoroutine(result):
                        result.close()
            except Exception:
                pass
        with self._lock:
            self._started = False
            self._thread = None
            self._loop = None
            self._set_status("disabled", "stopped")

    def set_desired_tokens(
        self,
        token_ids: Sequence[str],
        *,
        token_to_market: Mapping[str, str] | None = None,
    ) -> bool:
        """Update the desired Market Channel set. Returns True if changed.

        Debounced: no resubscribe when the set is unchanged.
        """
        # Subscription semantics are set-based. Stable sorting prevents a fair
        # rotation from reopening the same socket merely because token order changed.
        cleaned = tuple(sorted({str(t) for t in token_ids if t}))
        mapping = {str(k): str(v) for k, v in (token_to_market or {}).items() if k and v}
        with self._lock:
            if cleaned == self._desired_tokens and mapping == self._token_to_market:
                return False
            self._desired_tokens = cleaned
            self._token_to_market = mapping
            self._subscription_generation += 1
            generation = self._subscription_generation
        loop = self._loop
        if loop is not None and loop.is_running() and not self._stop_event.is_set():
            try:
                asyncio.run_coroutine_threadsafe(self._apply_subscription(generation), loop)
            except Exception as exc:
                logger.warning("stream resubscribe schedule failed: %s", exc)
                self._set_status("degraded", f"resubscribe schedule failed: {exc}")
        return True

    def desired_tokens(self) -> tuple[str, ...]:
        with self._lock:
            return self._desired_tokens

    def token_to_market(self) -> dict[str, str]:
        with self._lock:
            return dict(self._token_to_market)

    def drain(self) -> StreamDrainBatch:
        """Non-blocking drain for the serial pulse."""
        batch = self._queue.drain()
        # Track last quote times for staleness.
        now = time.monotonic()
        for token_id, quote in batch.quotes.items():
            self._token_last_quote_at[token_id] = max(
                quote.received_at,
                self._token_last_quote_at.get(token_id, quote.received_at),
            )
            self._last_market_event_at = max(
                quote.received_at,
                self._last_market_event_at or quote.received_at,
            )
        if batch.user_hints:
            self._last_user_event_at = batch.user_hints[-1].received_at
        if batch.quotes and self._status in {"connecting", "degraded", "stale"}:
            self._set_status("live", "receiving market quotes")
        # Staleness is judged per subscribed token, not quiet user channel.
        with self._lock:
            desired = self._desired_tokens
        if desired and self._status == "live":
            stale_tokens = [
                t
                for t in desired
                if now - self._token_last_quote_at.get(t, 0.0) > MARKET_TOKEN_STALE_SECONDS
                and self._token_last_quote_at.get(t) is not None
            ]
            # Only mark stale when we previously had quotes and they aged out.
            had_any = any(t in self._token_last_quote_at for t in desired)
            if had_any and len(stale_tokens) == len(
                [t for t in desired if t in self._token_last_quote_at]
            ):
                self._set_status("stale", f"all {len(stale_tokens)} tokens past BBO TTL")
        return batch

    def health(self) -> StreamHealth:
        counters = self._queue.counters()
        with self._lock:
            status = self._status
            desired = self._desired_tokens
        detail = {
            "subscribed_token_count": len(desired),
            "market_last_event_at": self._last_market_event_at,
            "user_last_event_at": self._last_user_event_at,
            "coalesced": counters["coalesced"],
            "unchanged_quotes": counters["unchanged_quotes"],
            "ignored_quotes": counters["ignored_quotes"],
            "dropped": counters["dropped"],
            "unknown_events": counters["unknown_events"],
            "market_events": counters["market_events"],
            "user_events": counters["user_events"],
            "rest_fallback_active": self.rest_fallback_active(),
            "user_channel_enabled": self._enable_user,
            "reader_errors": self._reader_error_count,
            "rest_backfill_pending_count": len(self._rest_backfill_tokens),
            "detail": self._status_detail,
        }
        return StreamHealth(status=status, updated_at=time.time(), detail=detail)

    def is_token_fresh(self, token_id: str, *, now: float | None = None) -> bool:
        """True when this token recently received a trustworthy BBO/book."""
        mono = time.monotonic() if now is None else now
        token = str(token_id)
        if token in self._rest_backfill_tokens:
            return False
        last = self._token_last_quote_at.get(token)
        if last is None:
            return False
        return (mono - last) <= MARKET_TOKEN_STALE_SECONDS

    def needs_rest_backfill(self, token_id: str) -> bool:
        """True after resubscribe/reconnect until REST snapshot is recorded."""
        return str(token_id) in self._rest_backfill_tokens

    def mark_rest_verified(self, token_id: str) -> None:
        """Clear reconnect/resubscribe backfill after a successful REST book."""
        self._rest_backfill_tokens.discard(str(token_id))

    def subscription_generation(self) -> int:
        return self._subscription_generation

    def rest_fallback_active(self) -> bool:
        with self._lock:
            status = self._status
            desired = self._desired_tokens
        if status != "live" or not desired:
            return True
        return any(not self.is_token_fresh(token_id) for token_id in desired)

    # --------------------------------------------------------------- internals

    def _set_status(self, status: StreamStatus, detail: str = "") -> None:
        self._status = status
        self._status_detail = detail[:240]

    def _thread_main(self) -> None:
        loop = self._loop_factory()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.create_task(self._async_main())
            loop.run_forever()
        except Exception as exc:
            logger.exception("stream bridge loop crashed: %s", exc)
            self._set_status("degraded", f"loop crashed: {exc}")
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _async_main(self) -> None:
        try:
            await self._open_client()
            generation = self._subscription_generation
            await self._apply_subscription(generation)
            if self._status == "connecting":
                self._set_status("live", "bridge ready")
            while not self._stop_event.is_set():
                await asyncio.sleep(0.25)
        except Exception as exc:
            logger.warning("stream bridge async main failed: %s", exc)
            self._set_status("degraded", f"startup failed: {exc}")
            # Keep the loop alive so stop() can shut down cleanly; REST remains.
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
        finally:
            try:
                await self._async_shutdown()
            except Exception:
                pass

    async def _open_client(self) -> None:
        if self._client is not None:
            return
        if self._enable_user and self._private_key:
            factory = self._secure_client_factory
            if factory is not None:
                self._client = factory()
            else:
                from polymarket import AsyncSecureClient

                create_kwargs: dict[str, Any] = {
                    "private_key": self._private_key,
                    "wallet": self._funder,
                }
                if self._api_credentials is not None:
                    create_kwargs["credentials"] = self._api_credentials
                self._client = await AsyncSecureClient.create(
                    **create_kwargs,
                )
                self._api_credentials = None
        else:
            factory = self._public_client_factory
            if factory is not None:
                self._client = factory()
            else:
                from polymarket import AsyncPublicClient

                self._client = AsyncPublicClient()
        # Never log key material; status only.
        self._set_status("connecting", "client open")

    async def _apply_subscription(self, generation: int) -> None:
        """Replace subscription handle: open new, then close old (overlap OK)."""
        if self._stop_event.is_set():
            return
        if generation != self._subscription_generation:
            return
        # Startup and set_desired_tokens() can schedule the same generation.
        # Let only one of them open a socket; newer generations may still overlap.
        if generation in self._subscription_inflight_generations:
            return
        self._subscription_inflight_generations.add(generation)
        try:
            await self._replace_subscription(generation)
        finally:
            self._subscription_inflight_generations.discard(generation)

    async def _replace_subscription(self, generation: int) -> None:
        with self._lock:
            tokens = self._desired_tokens
        try:
            await self._open_client()
            specs: list[Any] = []
            if tokens:
                from polymarket.streams import MarketSpec

                # BBO events are materially cheaper to normalize than every
                # individual trade-level price_change update. Initial books and
                # periodic REST verification remain the depth/freshness backstop.
                specs.append(MarketSpec(token_ids=list(tokens), custom_feature_enabled=True))
            if self._enable_user:
                from polymarket.streams import UserSpec

                specs.append(UserSpec())
            if not specs:
                # Nothing to subscribe; close previous handle if any.
                await self._close_handle()
                self._activate_subscription_tokens(())
                self._set_status("live", "no tokens subscribed; REST fallback")
                return

            client = self._client
            if client is None:
                self._set_status("degraded", "client missing")
                return
            subscription = client.subscribe(specs if len(specs) > 1 else specs[0])
            new_handle = await subscription if inspect.isawaitable(subscription) else subscription
            if self._stop_event.is_set() or generation != self._subscription_generation:
                await self._close_subscription_handle(new_handle)
                return
            old_handle = self._handle
            old_task = self._reader_task
            self._handle = new_handle
            self._reader_task = asyncio.create_task(
                self._read_handle(new_handle, generation),
                name="polymarket-stream-reader",
            )
            # Close previous after new is live (short overlap de-duped by queue).
            # CancelledError is BaseException — must not skip old_handle.close().
            if old_task is not None:
                old_task.cancel()
                try:
                    await old_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if old_handle is not None:
                await self._close_subscription_handle(old_handle)
            # The old handle remains live until the replacement is installed, so
            # overlapping tokens keep their verified REST snapshot and stream
            # freshness. Only newly added tokens require a fresh REST backfill.
            # Reader errors already mark every desired token for backfill.
            self._activate_subscription_tokens(tokens)
            self._set_status(
                "live",
                f"subscribed tokens={len(tokens)} user={self._enable_user}",
            )
        except Exception as exc:
            self._reader_error_count += 1
            logger.warning("stream subscribe failed: %s", exc)
            self._set_status("degraded", f"subscribe failed: {exc}")

    def _activate_subscription_tokens(self, tokens: Sequence[str]) -> None:
        active = {str(token) for token in tokens if token}
        added = active - self._active_tokens
        removed = self._active_tokens - active
        self._rest_backfill_tokens.intersection_update(active)
        self._rest_backfill_tokens.update(added)
        for token in added | removed:
            self._token_last_quote_at.pop(token, None)
        self._active_tokens = active

    async def _read_handle(self, handle: Any, generation: int) -> None:
        try:
            async for event in handle:
                if self._stop_event.is_set() or generation != self._subscription_generation:
                    break
                try:
                    items = normalize_sdk_events(event)
                except Exception:
                    self._queue.publish(None)
                    continue
                if not items:
                    self._queue.publish(None)
                    continue
                desired = set(self.desired_tokens())
                latest_by_token: dict[str, StreamQuote] = {}
                other_items: list[NormalizedStreamEvent] = []
                for normalized in items:
                    if isinstance(normalized, StreamQuote):
                        if normalized.token_id not in desired:
                            self._queue.ignore_quote()
                            continue
                        latest_by_token[normalized.token_id] = normalized
                    else:
                        other_items.append(normalized)
                for normalized in [*latest_by_token.values(), *other_items]:
                    self._queue.publish(normalized)
                    if isinstance(normalized, StreamQuote):
                        self._last_market_event_at = normalized.received_at
                        self._token_last_quote_at[normalized.token_id] = normalized.received_at
                    elif isinstance(normalized, StreamUserHint):
                        self._last_user_event_at = normalized.received_at
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reader_error_count += 1
            logger.warning("stream reader stopped: %s", exc)
            if not self._stop_event.is_set():
                # Invisible reconnect: force REST until books are re-verified.
                with self._lock:
                    self._rest_backfill_tokens.update(self._desired_tokens)
                self._set_status("degraded", f"reader error: {exc}")

    async def _close_handle(self) -> None:
        task = self._reader_task
        handle = self._handle
        self._reader_task = None
        self._handle = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        await self._close_subscription_handle(handle)

    @staticmethod
    async def _close_subscription_handle(handle: Any | None) -> None:
        if handle is None:
            return
        try:
            close = getattr(handle, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:
            logger.debug("stream handle close failed: %s", exc)

    async def _async_shutdown(self) -> None:
        await self._close_handle()
        client = self._client
        self._client = None
        if client is not None:
            close = getattr(client, "close", None) or getattr(client, "aclose", None)
            if close is not None:
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
