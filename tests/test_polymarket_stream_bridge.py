"""Phase 3: official SDK stream bridge — no real WebSocket, no mutations."""

from __future__ import annotations

import asyncio
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from polymarket_weather_arb.adapters.polymarket import stream as stream_mod
from polymarket_weather_arb.adapters.polymarket.stream import (
    MARKET_TOKEN_STALE_SECONDS,
    STREAM_CANDIDATE_GROUP_CAP,
    PolymarketStreamBridge,
    StreamQuote,
    StreamUserHint,
    normalize_sdk_event,
    normalize_sdk_events,
    select_stream_tokens,
)


# --------------------------------------------------------------------------- fakes


class FakeHandle:
    def __init__(self, events: list[Any] | None = None, *, hang: bool = False) -> None:
        self.events = list(events or [])
        self.closed = False
        self._hang = hang
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        if self._index < len(self.events):
            event = self.events[self._index]
            self._index += 1
            await asyncio.sleep(0)
            return event
        if self._hang:
            while not self.closed:
                await asyncio.sleep(0.05)
            raise StopAsyncIteration
        raise StopAsyncIteration

    def close(self) -> None:
        self.closed = True


class FakePublicClient:
    def __init__(self, events: list[Any] | None = None) -> None:
        self.events = list(events or [])
        self.specs: list[Any] = []
        self.handles: list[FakeHandle] = []
        self.closed = False

    def subscribe(self, specs):
        self.specs.append(specs)
        handle = FakeHandle(self.events, hang=True)
        self.handles.append(handle)
        return handle

    def close(self) -> None:
        self.closed = True
        for handle in self.handles:
            handle.close()


class FakeSecureClient(FakePublicClient):
    @classmethod
    async def create(cls, **kwargs):
        # Credentials must never be stored on the instance for health/logs.
        client = cls()
        client.create_kwargs = {k: ("***" if k == "private_key" else v) for k, v in kwargs.items()}
        return client


class AsyncFakePublicClient(FakePublicClient):
    async def subscribe(self, specs):
        await asyncio.sleep(0)
        return super().subscribe(specs)


def _book_event(
    token_id: str = "tok-yes",
    bid: str = "0.40",
    ask: str = "0.45",
    market: str = "cond-1",
):
    return SimpleNamespace(
        topic="market",
        type="book",
        payload=SimpleNamespace(
            market=market,
            token_id=token_id,
            bids=(SimpleNamespace(price=Decimal(bid), size=Decimal("10")),),
            asks=(SimpleNamespace(price=Decimal(ask), size=Decimal("12")),),
        ),
    )


def _bbo_event(token_id: str = "tok-yes", bid: str = "0.41", ask: str = "0.46"):
    return SimpleNamespace(
        topic="market",
        type="best_bid_ask",
        payload=SimpleNamespace(
            market="cond-1",
            token_id=token_id,
            best_bid=Decimal(bid),
            best_ask=Decimal(ask),
            spread=Decimal(ask) - Decimal(bid),
            timestamp=None,
        ),
    )


def _price_change_event(token_id: str = "tok-yes", bid: str = "0.42", ask: str = "0.47"):
    return SimpleNamespace(
        topic="market",
        type="price_change",
        payload=SimpleNamespace(
            market="cond-1",
            price_changes=(
                SimpleNamespace(
                    token_id=token_id,
                    price=Decimal(bid),
                    size=Decimal("1"),
                    side="BUY",
                    best_bid=Decimal(bid),
                    best_ask=Decimal(ask),
                ),
            ),
            timestamp=None,
        ),
    )


def _user_order_event():
    return SimpleNamespace(
        topic="user",
        type="order",
        payload=SimpleNamespace(
            id="ord-1",
            order_event_type="PLACEMENT",
            status="LIVE",
            token_id="tok-yes",
            market="cond-1",
        ),
    )


def _user_trade_event():
    return SimpleNamespace(
        topic="user",
        type="trade",
        payload=SimpleNamespace(
            id="tr-1",
            status="MATCHED",
            token_id="tok-yes",
            market="cond-1",
        ),
    )


# --------------------------------------------------------------------------- normalize


def test_normalize_book_and_bbo_decimals():
    book = normalize_sdk_event(_book_event())
    assert isinstance(book, StreamQuote)
    assert book.token_id == "tok-yes"
    assert book.best_bid == Decimal("0.40")
    assert book.best_ask == Decimal("0.45")
    assert book.midpoint == Decimal("0.425")

    bbo = normalize_sdk_event(_bbo_event(bid="0.50", ask="0.55"))
    assert isinstance(bbo, StreamQuote)
    assert bbo.best_bid == Decimal("0.50")
    assert bbo.source_type == "best_bid_ask"

    pc = normalize_sdk_event(_price_change_event())
    assert isinstance(pc, StreamQuote)
    assert pc.source_type == "price_change"


def test_normalize_user_and_unknown():
    order = normalize_sdk_event(_user_order_event())
    assert isinstance(order, StreamUserHint)
    assert order.kind == "order"
    trade = normalize_sdk_event(_user_trade_event())
    assert isinstance(trade, StreamUserHint)
    assert trade.kind == "trade"
    unknown = normalize_sdk_event(
        SimpleNamespace(topic="market", type="future_event", payload=SimpleNamespace())
    )
    assert unknown is None


def test_price_change_emits_all_tokens():
    event = SimpleNamespace(
        topic="market",
        type="price_change",
        payload=SimpleNamespace(
            market="cond-1",
            price_changes=(
                SimpleNamespace(
                    token_id="tok-yes",
                    price=Decimal("0.12"),
                    size=Decimal("1"),
                    side="BUY",
                    best_bid=Decimal("0.11"),
                    best_ask=Decimal("0.12"),
                ),
                SimpleNamespace(
                    token_id="tok-no",
                    price=Decimal("0.90"),
                    size=Decimal("1"),
                    side="BUY",
                    best_bid=Decimal("0.88"),
                    best_ask=Decimal("0.90"),
                ),
            ),
            timestamp=None,
        ),
    )
    items = normalize_sdk_events(event)
    assert len(items) == 2
    by_token = {q.token_id: q for q in items if isinstance(q, StreamQuote)}
    assert set(by_token) == {"tok-yes", "tok-no"}
    assert by_token["tok-yes"].best_ask == Decimal("0.12")
    assert by_token["tok-no"].best_ask == Decimal("0.90")


def test_select_stream_tokens_no_cross_outcome_guess():
    markets = {
        "m1": {"yes_token_id": "y1", "no_token_id": "n1", "city": "A", "target_date": "2026-07-20"},
    }
    # Asset-id outcome must map to the matching token, not default YES.
    desired = select_stream_tokens(
        positions=[{"market_id": "m1", "outcome": "n1", "size": 3}],
        open_orders=[{"market_id": "m1", "size": 1}],  # no token_id, no outcome → skip
        ranked_opportunities=[],
        market_rows=markets,
    )
    assert "n1" in desired.token_ids
    assert "y1" not in desired.token_ids

    # Missing outcome must not invent YES.
    desired2 = select_stream_tokens(
        positions=[{"market_id": "m1", "outcome": None, "size": 3}],
        open_orders=[],
        ranked_opportunities=[],
        market_rows=markets,
    )
    assert desired2.token_ids == ()


def test_public_sdk_imports_only():
    """Bridge module must use public APIs and never import websockets / _internal."""
    import ast
    from pathlib import Path

    source = Path(stream_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not any(name == "websockets" or name.startswith("websockets.") for name in imported)
    assert not any(
        name == "polymarket._internal" or name.startswith("polymarket._internal.")
        for name in imported
    )
    # Public imports are used inside methods (lazy) — check the symbols exist.
    from polymarket import AsyncPublicClient, AsyncSecureClient
    from polymarket.streams import MarketSpec, UserSpec

    assert AsyncPublicClient is not None
    assert AsyncSecureClient is not None
    assert MarketSpec(token_ids=["1"]).token_ids == ("1",)
    assert UserSpec().markets is None


# --------------------------------------------------------------------------- queue / bridge


def test_queue_coalesces_quotes_by_token():
    bridge = PolymarketStreamBridge(public_client_factory=lambda: FakePublicClient())
    # Directly feed the internal queue (no network).
    bridge._queue.publish(
        StreamQuote(
            token_id="t1",
            best_bid=Decimal("0.1"),
            best_ask=Decimal("0.2"),
            midpoint=Decimal("0.15"),
            spread=Decimal("0.1"),
            liquidity=None,
            received_at=1.0,
            source_type="best_bid_ask",
        )
    )
    bridge._queue.publish(
        StreamQuote(
            token_id="t1",
            best_bid=Decimal("0.3"),
            best_ask=Decimal("0.4"),
            midpoint=Decimal("0.35"),
            spread=Decimal("0.1"),
            liquidity=None,
            received_at=2.0,
            source_type="best_bid_ask",
        )
    )
    bridge._queue.publish(
        StreamQuote(
            token_id="t2",
            best_bid=Decimal("0.5"),
            best_ask=Decimal("0.6"),
            midpoint=Decimal("0.55"),
            spread=Decimal("0.1"),
            liquidity=None,
            received_at=3.0,
            source_type="book",
        )
    )
    batch = bridge.drain()
    assert set(batch.quotes) == {"t1", "t2"}
    assert batch.quotes["t1"].best_bid == Decimal("0.3")
    assert batch.coalesced >= 1


def test_queue_ignores_unchanged_quote_and_preserves_known_liquidity():
    bridge = PolymarketStreamBridge(public_client_factory=lambda: FakePublicClient())
    original = StreamQuote(
        token_id="t1",
        best_bid=Decimal("0.10"),
        best_ask=Decimal("0.20"),
        midpoint=Decimal("0.15"),
        spread=Decimal("0.10"),
        liquidity=Decimal("22"),
        received_at=1.0,
        source_type="book",
        condition_id="c1",
    )
    duplicate = StreamQuote(
        token_id="t1",
        best_bid=Decimal("0.10"),
        best_ask=Decimal("0.20"),
        midpoint=Decimal("0.15"),
        spread=Decimal("0.10"),
        liquidity=None,
        received_at=2.0,
        source_type="best_bid_ask",
        condition_id="c1",
    )

    assert bridge._queue.publish(original) is True
    assert bridge._queue.publish(duplicate) is False

    batch = bridge.drain()
    assert batch.quotes["t1"].liquidity == Decimal("22")
    assert bridge.health().public_dict()["detail"]["unchanged_quotes"] == 1


def test_reader_uses_price_change_and_ignores_only_non_subscribed_quotes():
    bridge = PolymarketStreamBridge(public_client_factory=lambda: FakePublicClient())
    bridge._desired_tokens = ("tok-yes",)
    generation = bridge.subscription_generation()
    handle = FakeHandle(
        [
            _price_change_event(token_id="tok-yes"),
            _bbo_event(token_id="tok-other", bid="0.20", ask="0.21"),
            _bbo_event(token_id="tok-yes", bid="0.30", ask="0.31"),
        ]
    )

    asyncio.run(bridge._read_handle(handle, generation))

    batch = bridge.drain()
    assert set(batch.quotes) == {"tok-yes"}
    assert batch.quotes["tok-yes"].best_ask == Decimal("0.31")
    assert bridge.health().public_dict()["detail"]["ignored_quotes"] == 1
    assert bridge.health().public_dict()["detail"]["market_last_event_at"] is not None


def test_market_subscription_enables_custom_bbo_events():
    client = FakePublicClient()
    bridge = PolymarketStreamBridge(public_client_factory=lambda: client)
    bridge.start()
    try:
        bridge.set_desired_tokens(["tok-yes"])
        deadline = time.time() + 2.0
        while not client.specs and time.time() < deadline:
            time.sleep(0.02)
        market_spec = client.specs[-1]
        assert market_spec.custom_feature_enabled is True
    finally:
        bridge.stop(timeout=2.0)


def test_subscription_token_reordering_does_not_resubscribe():
    bridge = PolymarketStreamBridge(public_client_factory=lambda: FakePublicClient())
    assert bridge.set_desired_tokens(["t2", "t1"], token_to_market={"t1": "m1", "t2": "m2"}) is True
    generation = bridge.subscription_generation()

    assert (
        bridge.set_desired_tokens(["t1", "t2"], token_to_market={"t1": "m1", "t2": "m2"}) is False
    )
    assert bridge.subscription_generation() == generation


def test_user_events_coalesce_to_one_reconcile():
    bridge = PolymarketStreamBridge(public_client_factory=lambda: FakePublicClient())
    bridge._queue.publish(StreamUserHint(kind="order", received_at=1.0, event_type="PLACEMENT"))
    bridge._queue.publish(
        StreamUserHint(kind="trade", received_at=2.0, event_type="trade", status="MATCHED")
    )
    bridge._queue.publish(StreamUserHint(kind="order", received_at=3.0, event_type="UPDATE"))
    batch = bridge.drain()
    assert batch.reconcile_due is True
    assert batch.coalesced >= 2
    # Second drain has nothing.
    batch2 = bridge.drain()
    assert batch2.reconcile_due is False


def test_bridge_start_stop_and_subscribe_shadow():
    client = FakePublicClient(events=[_bbo_event(bid="0.11", ask="0.12")])
    bridge = PolymarketStreamBridge(public_client_factory=lambda: client)
    bridge.start()
    try:
        assert bridge.started
        changed = bridge.set_desired_tokens(["tok-yes"], token_to_market={"tok-yes": "m1"})
        assert changed is True
        # Unchanged set does not resubscribe generation bump as "changed".
        assert bridge.set_desired_tokens(["tok-yes"], token_to_market={"tok-yes": "m1"}) is False
        # Wait briefly for the reader to process the preloaded event.
        deadline = time.time() + 2.0
        batch = bridge.drain()
        while not batch.quotes and time.time() < deadline:
            time.sleep(0.05)
            batch = bridge.drain()
        # May or may not have drained depending on timing; health must be non-secret.
        health = bridge.health().public_dict()
        assert health["status"] in {"connecting", "live", "degraded", "stale"}
        detail_text = str(health)
        assert "private_key" not in detail_text.lower()
        assert "0x" not in detail_text  # no wallet dump expected from our detail
    finally:
        bridge.stop(timeout=2.0)
    assert not bridge.started
    assert bridge.health().status == "disabled"
    # Handle and/or client must be closed; allow either path depending on race.
    assert client.closed or not client.handles or all(h.closed for h in client.handles)


def test_bridge_awaits_async_sdk_subscribe_before_reading_handle():
    client = AsyncFakePublicClient(events=[_bbo_event(bid="0.21", ask="0.22")])
    bridge = PolymarketStreamBridge(public_client_factory=lambda: client)
    bridge.start()
    try:
        bridge.set_desired_tokens(["tok-yes"], token_to_market={"tok-yes": "m1"})
        deadline = time.time() + 2.0
        batch = bridge.drain()
        while not batch.quotes and time.time() < deadline:
            time.sleep(0.05)
            batch = bridge.drain()

        assert batch.quotes["tok-yes"].best_bid == Decimal("0.21")
        health = bridge.health().public_dict()
        assert health["status"] == "live"
        assert health["detail"]["market_events"] >= 1
        assert health["detail"]["reader_errors"] == 0
    finally:
        bridge.stop(timeout=2.0)


def test_bridge_startup_failure_degrades_not_raises():
    def boom():
        raise RuntimeError("cannot open client")

    bridge = PolymarketStreamBridge(public_client_factory=boom)
    bridge.start()
    try:
        deadline = time.time() + 2.0
        while bridge.health().status == "connecting" and time.time() < deadline:
            time.sleep(0.05)
        assert bridge.health().status in {"degraded", "connecting", "live"}
        # Drain never raises; REST fallback remains active.
        batch = bridge.drain()
        assert isinstance(batch.quotes, dict)
        assert bridge.rest_fallback_active()
    finally:
        bridge.stop(timeout=2.0)


def test_health_excludes_secret_keys():
    bridge = PolymarketStreamBridge(
        private_key="SECRET_KEY_MATERIAL",
        funder="0xabc",
        public_client_factory=lambda: FakePublicClient(),
        enable_user_channel=False,
    )
    health = bridge.health().public_dict()
    blob = str(health).lower()
    assert "secret_key_material" not in blob
    assert "private_key" not in blob


def test_user_stream_reuses_provided_api_credentials(monkeypatch):
    import polymarket

    credentials = object()

    class CapturingSecureClient(FakeSecureClient):
        create_calls = []

        @classmethod
        async def create(cls, **kwargs):
            cls.create_calls.append(kwargs)
            return cls()

    monkeypatch.setattr(polymarket, "AsyncSecureClient", CapturingSecureClient)
    bridge = PolymarketStreamBridge(
        private_key="private-key",
        funder="0xfunder",
        api_credentials=credentials,
        enable_user_channel=True,
    )

    asyncio.run(bridge._open_client())

    assert CapturingSecureClient.create_calls == [
        {
            "private_key": "private-key",
            "wallet": "0xfunder",
            "credentials": credentials,
        }
    ]
    assert bridge._api_credentials is None
    assert "credentials" not in str(bridge.health().public_dict()).lower()


# --------------------------------------------------------------------------- subscription selection


def test_select_stream_tokens_priority_and_cap():
    markets = {
        f"m{i}": {
            "yes_token_id": f"y{i}",
            "no_token_id": f"n{i}",
            "city": f"City{i % 3}",
            "target_date": f"2026-07-{20 + (i % 3)}",
        }
        for i in range(30)
    }
    # Position and open order must always be included.
    positions = [{"market_id": "m0", "outcome": "YES", "size": 5}]
    open_orders = [{"market_id": "m1", "token_id": "y1", "size": 2}]
    ranked = [
        {
            "market_id": f"m{i}",
            "side": "buy_yes",
            "city": markets[f"m{i}"]["city"],
            "target_date": markets[f"m{i}"]["target_date"],
        }
        for i in range(30)
    ]
    desired = select_stream_tokens(
        positions=positions,
        open_orders=open_orders,
        ranked_opportunities=ranked,
        market_rows=markets,
        candidate_group_cap=STREAM_CANDIDATE_GROUP_CAP,
    )
    assert "y0" in desired.token_ids
    assert "y1" in desired.token_ids
    assert "y0" in desired.held_tokens
    assert "y1" in desired.open_order_tokens
    # Cap limits complete groups, not individual tokens arbitrarily without groups.
    assert len(desired.token_ids) == len(set(desired.token_ids))
    # Held survives even if not in top candidates.
    assert "y0" in desired.token_ids


def test_select_stream_tokens_complete_sibling_groups():
    markets = {
        "a1": {
            "yes_token_id": "ya1",
            "no_token_id": "na1",
            "city": "Seoul",
            "target_date": "2026-07-21",
        },
        "a2": {
            "yes_token_id": "ya2",
            "no_token_id": "na2",
            "city": "Seoul",
            "target_date": "2026-07-21",
        },
        "b1": {
            "yes_token_id": "yb1",
            "no_token_id": "nb1",
            "city": "Tokyo",
            "target_date": "2026-07-22",
        },
    }
    ranked = [
        {"market_id": "a1", "side": "buy_yes", "city": "Seoul", "target_date": "2026-07-21"},
        {"market_id": "a2", "side": "buy_yes", "city": "Seoul", "target_date": "2026-07-21"},
        {"market_id": "b1", "side": "buy_yes", "city": "Tokyo", "target_date": "2026-07-22"},
    ]
    desired = select_stream_tokens(
        positions=[],
        open_orders=[],
        ranked_opportunities=ranked,
        market_rows=markets,
        candidate_group_cap=1,
    )
    # One complete group only — both Seoul siblings or neither as partial.
    # With cap=1 the first fair-ordered group is fully included.
    seoul = {"ya1", "ya2"}
    tokyo = {"yb1"}
    selected = set(desired.token_ids)
    assert selected == seoul or selected == tokyo or selected >= seoul
    if "ya1" in selected:
        assert "ya2" in selected


def test_no_websockets_dependency_declared():
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert "websockets" not in text or "polymarket" in text
    # Ensure we did not add a direct websockets dependency line.
    for line in text.splitlines():
        if line.strip().startswith("websockets"):
            pytest.fail("direct websockets dependency must not be added")


def test_rest_fallback_stays_active_until_all_stream_quotes_are_fresh():
    bridge = PolymarketStreamBridge(public_client_factory=lambda: FakePublicClient())
    bridge._status = "live"
    bridge._desired_tokens = ("t1", "t2")
    bridge._rest_backfill_tokens = {"t1", "t2"}
    bridge.mark_rest_verified("t1")
    assert bridge.needs_rest_backfill("t2")
    assert bridge.rest_fallback_active() is True
    bridge.mark_rest_verified("t2")
    assert bridge.needs_rest_backfill("t1") is False
    assert bridge.needs_rest_backfill("t2") is False

    # A completed REST backfill is not proof that the stream is carrying BBOs.
    assert bridge.rest_fallback_active() is True
    now = time.monotonic()
    bridge._token_last_quote_at = {"t1": now, "t2": now}
    assert bridge.rest_fallback_active() is False
    assert bridge.health().public_dict()["detail"]["rest_fallback_active"] is False

    bridge._token_last_quote_at["t2"] = now - MARKET_TOKEN_STALE_SECONDS - 1
    assert bridge.rest_fallback_active() is True
    assert bridge.health().public_dict()["detail"]["rest_fallback_active"] is True


def test_resubscribe_only_backfills_added_tokens_and_preserves_overlap_freshness():
    bridge = PolymarketStreamBridge(public_client_factory=lambda: FakePublicClient())
    now = time.monotonic()
    bridge._active_tokens = {"t1", "t2"}
    bridge._desired_tokens = ("t1", "t2")
    bridge._token_last_quote_at = {"t1": now, "t2": now}
    bridge._rest_backfill_tokens = set()

    bridge._activate_subscription_tokens(("t2", "t3"))

    assert bridge.needs_rest_backfill("t1") is False
    assert bridge.needs_rest_backfill("t2") is False
    assert bridge.needs_rest_backfill("t3") is True
    assert "t1" not in bridge._token_last_quote_at
    assert bridge._token_last_quote_at["t2"] == now


def test_resubscribe_closes_old_handle_despite_cancelled_error():
    """CancelledError is BaseException; old handle must still close."""

    class CancellingHandle(FakeHandle):
        def __init__(self):
            super().__init__(hang=True)
            self.closed = False

        async def __anext__(self):
            while not self.closed:
                await asyncio.sleep(0.01)
            raise StopAsyncIteration

    class Client:
        def __init__(self):
            self.handles: list[CancellingHandle] = []

        def subscribe(self, specs):
            handle = CancellingHandle()
            self.handles.append(handle)
            return handle

        def close(self):
            for h in self.handles:
                h.close()

    client = Client()
    bridge = PolymarketStreamBridge(public_client_factory=lambda: client)
    bridge.start()
    try:
        bridge.set_desired_tokens(["t1"])
        deadline = time.time() + 2.0
        while not client.handles and time.time() < deadline:
            time.sleep(0.05)
        first = client.handles[0]
        bridge.set_desired_tokens(["t1", "t2"])
        deadline = time.time() + 2.0
        while len(client.handles) < 2 and time.time() < deadline:
            time.sleep(0.05)
        # Give the bridge a moment to cancel/close the first handle.
        time.sleep(0.3)
        assert first.closed is True
        assert bridge.needs_rest_backfill("t1")
        assert bridge.needs_rest_backfill("t2")
    finally:
        bridge.stop(timeout=2.0)


def test_async_resubscribe_discards_late_stale_handle():
    class Client:
        def __init__(self):
            self.handles: list[FakeHandle] = []
            self.first_started = threading.Event()
            self.release_first = threading.Event()

        async def subscribe(self, specs):
            handle = FakeHandle(hang=True)
            self.handles.append(handle)
            if len(self.handles) == 1:
                self.first_started.set()
                while not self.release_first.is_set():
                    await asyncio.sleep(0.01)
            return handle

        def close(self):
            for handle in self.handles:
                handle.close()

    client = Client()
    bridge = PolymarketStreamBridge(public_client_factory=lambda: client)
    bridge.start()
    try:
        bridge.set_desired_tokens(["t1"])
        assert client.first_started.wait(timeout=2.0)
        bridge.set_desired_tokens(["t1", "t2"])

        deadline = time.time() + 2.0
        while len(client.handles) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert len(client.handles) == 2
        second = client.handles[1]

        client.release_first.set()
        deadline = time.time() + 2.0
        while (
            not client.handles[0].closed or bridge._handle is not second
        ) and time.time() < deadline:
            time.sleep(0.05)

        assert client.handles[0].closed is True
        assert bridge._handle is second
        assert bridge.needs_rest_backfill("t1")
        assert bridge.needs_rest_backfill("t2")
    finally:
        client.release_first.set()
        bridge.stop(timeout=2.0)
