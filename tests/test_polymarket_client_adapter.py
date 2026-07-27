from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest
from polymarket.auth import BuilderApiKey

from polymarket_weather_arb.adapters.polymarket.client import (
    GammaPolymarketClient,
    MISSING_EVENT_SLUG_TTL_SECONDS,
    MISSING_ORDER_BOOK_TTL_SECONDS,
    OrderNotFoundError,
    _is_order_absent_error,
)
from polymarket_weather_arb.config import Settings


class FakeModel:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, mode="python"):
        return {**self.payload, "dump_mode": mode}


class FakePaginator:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        return iter(self.items)


class FakeRedeemHandle:
    transaction_id = "relay-1"
    transaction_hash = "0xsubmitted"

    def wait(self):
        return SimpleNamespace(
            transaction_id="relay-1",
            transaction_hash="0xconfirmed",
        )


class FakeSecureClient:
    create_calls = []
    instances = []

    def __init__(self):
        self.closed = False
        self.credentials = object()
        self.place_limit_order_calls = []
        self.list_positions_calls = []
        self.balance_calls = 0
        self.fail_balances_auth_once = False
        self.mutation_calls = 0
        self.mutation_should_fail = False
        self.redeem_calls = []
        self._ctx = SimpleNamespace(wallet_type="DEPOSIT_WALLET")

    @classmethod
    def create(cls, *, private_key, wallet, api_key=None):
        call = {"private_key": private_key, "wallet": wallet}
        if api_key is not None:
            call["api_key"] = api_key
        cls.create_calls.append(call)
        instance = cls()
        cls.instances.append(instance)
        return instance

    def place_limit_order(self, *, token_id, price, size, side):
        self.place_limit_order_calls.append(
            {"token_id": token_id, "price": price, "size": size, "side": side}
        )
        self.mutation_calls += 1
        if self.mutation_should_fail:
            raise RuntimeError("ambiguous network failure during order post")
        return FakeModel({"ok": True, "order_id": "order-1"})

    def get_balance_allowance(self, *, asset_type):
        self.balance_calls += 1
        if self.fail_balances_auth_once:
            self.fail_balances_auth_once = False
            raise RuntimeError("Unauthorized: invalid api key")
        return FakeModel(
            {"balance": 65030109, "allowances": {"spender": 1}, "asset_type": asset_type}
        )

    def list_open_orders(self):
        return FakePaginator([FakeModel({"id": "open-1"})])

    def list_account_trades(self):
        return FakePaginator([FakeModel({"id": "trade-1"})])

    def list_positions(self, *, user):
        self.list_positions_calls.append({"user": user})
        return FakePaginator([FakeModel({"market": "m1", "size": "2"})])

    def get_order(self, *, order_id):
        if order_id == "missing-order":
            raise RuntimeError("order not found")
        if order_id == "bad-shape":
            return object()  # no model_dump
        return FakeModel({"id": order_id, "status": "OPEN"})

    def cancel_order(self, *, order_id):
        self.mutation_calls += 1
        if self.mutation_should_fail:
            raise RuntimeError("ambiguous network failure during cancel")
        return FakeModel({"canceled": [order_id], "not_canceled": {}})

    def redeem_positions(self, *, condition_id, metadata):
        self.mutation_calls += 1
        self.redeem_calls.append(
            {"condition_id": condition_id, "metadata": metadata}
        )
        if self.mutation_should_fail:
            raise RuntimeError("ambiguous network failure during redemption")
        return FakeRedeemHandle()

    def close(self):
        self.closed = True


def test_live_order_uses_polymarket_client_wallet_path(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    response = client.place_limit_order(
        token_id="token-1", side="buy_yes", price="0.001", size="1000"
    )

    assert FakeSecureClient.create_calls == [{"private_key": "private-key", "wallet": "0xfunder"}]
    assert FakeSecureClient.instances[0].place_limit_order_calls == [
        {"token_id": "token-1", "price": "0.001", "size": "1000", "side": "BUY"}
    ]
    assert response == {"ok": True, "order_id": "order-1", "dump_mode": "json"}
    # Session is reused; close only on explicit adapter close.
    assert FakeSecureClient.instances[0].closed is False
    client.close()
    assert FakeSecureClient.instances[0].closed is True


def test_place_sell_limit_order_sends_side_sell(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    response = client.place_sell_limit_order(token_id="token-sell", price="0.42", size="15")

    assert FakeSecureClient.create_calls == [{"private_key": "private-key", "wallet": "0xfunder"}]
    assert FakeSecureClient.instances[0].place_limit_order_calls == [
        {"token_id": "token-sell", "price": "0.42", "size": "15", "side": "SELL"}
    ]
    assert response == {"ok": True, "order_id": "order-1", "dump_mode": "json"}
    assert FakeSecureClient.instances[0].closed is False


def test_place_limit_order_buy_path_unchanged_and_rejects_sell_side(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    with pytest.raises(ValueError, match="unsupported side"):
        client.place_limit_order(token_id="token-1", side="SELL", price="0.5", size="1")
    assert FakeSecureClient.instances == []


def test_authenticated_reads_reuse_one_secure_client(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    balances = client.get_balances()
    orders = client.get_orders()
    trades = client.get_trades()
    positions = client.get_positions()
    order = client.get_order("order-1")
    cancel = client.cancel_order("order-1")

    assert balances["asset_type"] == "COLLATERAL"
    assert orders == [{"id": "open-1", "dump_mode": "json"}]
    assert trades == [{"id": "trade-1", "dump_mode": "json"}]
    assert positions == [{"market": "m1", "size": "2", "dump_mode": "json"}]
    assert order == {"id": "order-1", "status": "OPEN", "dump_mode": "json"}
    assert cancel == {"canceled": ["order-1"], "not_canceled": {}, "dump_mode": "json"}
    assert len(FakeSecureClient.create_calls) == 1
    assert client.secure_client_create_count == 1
    assert FakeSecureClient.instances[0].list_positions_calls == [{"user": "0xfunder"}]
    assert FakeSecureClient.instances[0].closed is False


def test_builder_credentials_enable_gasless_redeem_and_persist_submission_callback(
    monkeypatch, tmp_path
):
    _install_fake_polymarket(monkeypatch)
    client = GammaPolymarketClient(
        Settings(
            _env_file=None,
            DATABASE_PATH=tmp_path / "db.sqlite",
            POLYMARKET_PRIVATE_KEY="private-key",
            POLYMARKET_FUNDER="0xfunder",
            BUILDER_API_KEY="builder-key",
            BUILDER_SECRET="builder-secret",
            BUILDER_PASS_PHRASE="builder-passphrase",
        )
    )
    submitted = []

    readiness = client.validate_redemption_signing()
    result = client.redeem_positions(
        condition_id="0xcondition",
        on_submitted=submitted.append,
    )

    assert readiness["ok"] is True
    assert readiness["status"] == "gasless-builder-ready"
    create_call = FakeSecureClient.create_calls[0]
    assert create_call["private_key"] == "private-key"
    assert create_call["wallet"] == "0xfunder"
    assert repr(create_call["api_key"]) == (
        "BuilderApiKey(key=<redacted>, secret=<redacted>, passphrase=<redacted>)"
    )
    assert submitted == [
        {
            "condition_id": "0xcondition",
            "transaction_id": "relay-1",
            "transaction_hash": "0xsubmitted",
        }
    ]
    assert result == {
        "condition_id": "0xcondition",
        "transaction_id": "relay-1",
        "transaction_hash": "0xconfirmed",
        "status": "confirmed",
    }
    assert FakeSecureClient.instances[0].redeem_calls == [
        {
            "condition_id": "0xcondition",
            "metadata": "weather-autopilot redeem 0xcondition",
        }
    ]


def test_deposit_wallet_redemption_readiness_fails_closed_without_builder_credentials(
    monkeypatch, tmp_path
):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    readiness = client.validate_redemption_signing()

    assert readiness["ok"] is False
    assert readiness["status"] == "builder-credentials-not-ready"
    assert readiness["wallet_type"] == "DEPOSIT_WALLET"


def test_redemption_readiness_fails_closed_for_unknown_wallet_type(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)
    client.get_balances()
    FakeSecureClient.instances[0]._ctx.wallet_type = "FUTURE_WALLET"

    readiness = client.validate_redemption_signing()

    assert readiness["ok"] is False
    assert readiness["status"] == "unsupported-wallet-type"
    assert readiness["wallet_type"] == "FUTURE_WALLET"


def test_account_trades_tolerate_only_pending_missing_transaction_hash(monkeypatch, tmp_path):
    from polymarket._internal.actions.account import parse_account_trades_page

    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)
    client.get_balances()
    instance = FakeSecureClient.instances[0]
    raw_page = {
        "data": [_raw_trade(status="MATCHED_NOT_BROADCASTED")],
        "next_cursor": None,
    }

    class BrokenPaginator:
        def iter_items(self):
            return iter(parse_account_trades_page(raw_page).items)

    class RawTransport:
        def __init__(self):
            self.calls = []

        def get_json(self, path, *, params):
            self.calls.append((path, params))
            return raw_page

    transport = RawTransport()
    instance._ctx = SimpleNamespace(secure_clob=transport)
    instance.list_account_trades = BrokenPaginator

    trades = client.get_trades()

    assert len(trades) == 1
    assert trades[0]["id"] == "pending-trade"
    assert trades[0]["status"] == "MATCHED_NOT_BROADCASTED"
    assert trades[0]["transaction_hash"] is None
    assert trades[0]["_transaction_hash_pending"] is True
    assert transport.calls == [("/data/trades", {})]


def test_account_trade_compat_does_not_hide_other_schema_errors(monkeypatch, tmp_path):
    from polymarket._internal.actions.account import parse_account_trades_page
    from polymarket.errors import UnexpectedResponseError

    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)
    client.get_balances()
    instance = FakeSecureClient.instances[0]
    malformed = _raw_trade(status="MATCHED_NOT_BROADCASTED")
    malformed.pop("owner")
    raw_page = {"data": [malformed], "next_cursor": None}

    class BrokenPaginator:
        def iter_items(self):
            return iter(parse_account_trades_page(raw_page).items)

    class RawTransport:
        def get_json(self, *_args, **_kwargs):
            raise AssertionError("fallback must not run for multiple missing fields")

    instance._ctx = SimpleNamespace(secure_clob=RawTransport())
    instance.list_account_trades = BrokenPaginator

    with pytest.raises(UnexpectedResponseError):
        client.get_trades()


def test_second_reconciliation_tick_reuses_secure_client(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    for _ in range(2):
        client.get_balances()
        client.get_orders()
        client.get_trades()
        client.get_positions()

    assert len(FakeSecureClient.create_calls) == 1
    assert client.secure_client_create_count == 1
    assert FakeSecureClient.instances[0].balance_calls == 2


def test_read_auth_expiry_invalidates_and_recreates_once(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    # First create + seed a healthy client, then force next balance call to auth-fail.
    client.get_orders()
    assert len(FakeSecureClient.instances) == 1
    FakeSecureClient.instances[0].fail_balances_auth_once = True

    balances = client.get_balances()

    assert balances["asset_type"] == "COLLATERAL"
    assert len(FakeSecureClient.create_calls) == 2
    assert client.secure_client_create_count == 2
    assert FakeSecureClient.instances[0].closed is True
    assert FakeSecureClient.instances[1].closed is False


def test_ambiguous_mutation_failure_is_not_replayed(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    # Seed session
    client.get_balances()
    FakeSecureClient.instances[0].mutation_should_fail = True

    with pytest.raises(RuntimeError, match="ambiguous network failure"):
        client.place_limit_order(token_id="t", side="buy_yes", price="0.1", size="1")
    with pytest.raises(RuntimeError, match="ambiguous network failure"):
        client.place_sell_limit_order(token_id="t", price="0.1", size="1")
    with pytest.raises(RuntimeError, match="ambiguous network failure"):
        client.cancel_order("order-x")

    # One call each — no automatic replay.
    assert FakeSecureClient.instances[0].mutation_calls == 3
    assert len(FakeSecureClient.create_calls) == 1


def test_absent_get_order_is_normalized(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    with pytest.raises(OrderNotFoundError, match="missing-order"):
        client.get_order("missing-order")
    # Non-None malformed payloads must not be silently treated as missing.
    with pytest.raises(ValueError, match="unexpected Polymarket response type"):
        client.get_order("bad-shape")


def test_get_order_none_body_is_not_found_empty_dict_is_adapter_error(monkeypatch, tmp_path):
    """Use real SDK exception/cause shapes for OpenOrder.parse_response."""
    from polymarket.errors import UnexpectedResponseError
    from polymarket.models.clob.account import OpenOrder

    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    def raise_none(_order_id=None, **kwargs):
        try:
            OpenOrder.parse_response(None)
        except UnexpectedResponseError:
            raise

    def raise_empty(_order_id=None, **kwargs):
        try:
            OpenOrder.parse_response({})
        except UnexpectedResponseError:
            raise

    client.get_balances()  # seed session
    FakeSecureClient.instances[0].get_order = lambda **kw: raise_none(**kw)
    with pytest.raises(OrderNotFoundError):
        client.get_order("gone")

    FakeSecureClient.instances[0].get_order = lambda **kw: raise_empty(**kw)
    with pytest.raises(UnexpectedResponseError):
        client.get_order("partial")


def test_unrelated_not_found_error_is_not_treated_as_missing_order():
    assert not _is_order_absent_error(
        RuntimeError("API key not found"),
        order_id="order-123",
    )
    assert _is_order_absent_error(
        RuntimeError("order not found"),
        order_id="order-123",
    )
    assert _is_order_absent_error(
        RuntimeError("resource order-123 not found"),
        order_id="order-123",
    )


def test_close_cannot_run_while_authenticated_op_in_flight(monkeypatch, tmp_path):
    import threading
    import time

    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)
    client.get_balances()
    assert len(FakeSecureClient.instances) == 1

    started = threading.Event()
    release = threading.Event()
    instance = FakeSecureClient.instances[0]

    def slow_balance(*, asset_type):
        started.set()
        assert release.wait(timeout=2)
        return FakeModel({"balance": 1, "allowances": {}, "asset_type": asset_type})

    instance.get_balance_allowance = slow_balance

    reader_errors: list[BaseException] = []
    close_finished = threading.Event()

    def reader():
        try:
            client.get_balances()
        except BaseException as exc:  # noqa: BLE001
            reader_errors.append(exc)

    def closer():
        client.close()
        close_finished.set()

    t_read = threading.Thread(target=reader)
    t_close = threading.Thread(target=closer)
    t_read.start()
    assert started.wait(timeout=2)
    t_close.start()
    # Close must block until the in-flight read finishes.
    time.sleep(0.05)
    assert not close_finished.is_set()
    assert instance.closed is False
    release.set()
    t_read.join(timeout=2)
    t_close.join(timeout=2)
    assert reader_errors == []
    assert close_finished.is_set()
    assert instance.closed is True


def test_auth_retry_serialized_under_op_lock(monkeypatch, tmp_path):
    import threading

    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)
    client.get_balances()
    first = FakeSecureClient.instances[0]
    first.fail_balances_auth_once = True

    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=2)
            results.append(client.get_balances())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert errors == []
    assert len(results) == 2
    # Seed + at least one recreate; never more than one live unclosed client.
    live = [inst for inst in FakeSecureClient.instances if not inst.closed]
    assert len(live) == 1
    client.close()


def test_validate_order_signing_accepts_wallet_path(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    result = client.validate_order_signing()

    assert result == {
        "ok": True,
        "status": "wallet-path-configured",
        "detail": "polymarket-client will sign orders with wallet=POLYMARKET_FUNDER",
    }


def test_missing_event_slug_uses_short_negative_cache(monkeypatch, tmp_path):
    import polymarket_weather_arb.adapters.polymarket.client as client_mod

    clock = {"now": 100.0}
    reads = []

    class Response:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("404 should be normalized before raise_for_status")

    class HttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(client_mod, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(client_mod, "build_httpx_client", lambda **_kwargs: HttpClient())

    def read(_client, method, url, settings, **_kwargs):
        reads.append((method, url, settings))
        return Response()

    monkeypatch.setattr(client_mod, "safe_http_read", read)
    client = _client(tmp_path)
    slug = "highest-temperature-in-missing-city-on-july-23-2026"

    assert client.get_event_markets_by_slug(slug) == []
    assert client.get_event_markets_by_slug(slug) == []
    assert len(reads) == 1

    clock["now"] += MISSING_EVENT_SLUG_TTL_SECONDS + 1
    assert client.get_event_markets_by_slug(slug) == []
    assert len(reads) == 2


def test_missing_token_order_book_uses_short_negative_cache(monkeypatch, tmp_path):
    import polymarket_weather_arb.adapters.polymarket.client as client_mod

    clock = {"now": 100.0}
    reads = []

    class Response:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("404 should be normalized before raise_for_status")

    class HttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(client_mod, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(client_mod, "build_httpx_client", lambda **_kwargs: HttpClient())

    def read(_client, method, url, settings, **_kwargs):
        reads.append((method, url, settings))
        return Response()

    monkeypatch.setattr(client_mod, "safe_http_read", read)
    client = _client(tmp_path)

    first, first_raw = client.get_token_order_book("missing-token")
    cached, cached_raw = client.get_token_order_book("missing-token")

    assert first.best_bid is None and first.best_ask is None
    assert cached.best_bid is None and cached.best_ask is None
    assert first_raw["negative_cache"] is False
    assert cached_raw["negative_cache"] is True
    assert len(reads) == 1

    clock["now"] += MISSING_ORDER_BOOK_TTL_SECONDS + 1
    client.get_token_order_book("missing-token")
    assert len(reads) == 2


def test_stream_credentials_reuse_authenticated_client(monkeypatch, tmp_path):
    _install_fake_polymarket(monkeypatch)
    client = _client(tmp_path)

    credentials = client.stream_api_credentials()
    balances = client.get_balances()

    assert credentials is FakeSecureClient.instances[0].credentials
    assert balances["balance"] == 65030109
    assert len(FakeSecureClient.create_calls) == 1


def test_gamma_http_reads_never_instantiate_settings(monkeypatch, tmp_path):
    """Hot-path HTTP helpers must use the adapter's Settings, not Settings()."""
    settings = Settings(
        DATABASE_PATH=tmp_path / "adapter.db",
        POLYMARKET_PRIVATE_KEY="private-key",
        POLYMARKET_FUNDER="0xfunder",
        HTTP_READ_MAX_RETRIES=0,
    )
    client = GammaPolymarketClient(settings)

    import polymarket_weather_arb.adapters.http_client as http_client_mod
    import polymarket_weather_arb.adapters.http_reader as http_reader_mod
    import polymarket_weather_arb.config as config_mod

    original_settings = config_mod.Settings
    calls = {"settings": 0}

    def boom(*args, **kwargs):
        calls["settings"] += 1
        raise AssertionError("Settings() must not be constructed on Gamma hot paths")

    monkeypatch.setattr(config_mod, "Settings", boom)
    monkeypatch.setattr(http_client_mod, "Settings", boom)
    monkeypatch.setattr(http_reader_mod, "Settings", boom)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return []

    class FakeHttpx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        http_client_mod,
        "build_httpx_client",
        lambda *a, **k: (
            FakeHttpx()
            if k.get("settings") is settings or (a and a[-1] is settings)
            else (_ for _ in ()).throw(AssertionError(f"missing settings: {a} {k}"))
        ),
    )
    # build_httpx_client is imported into client module — patch there too.
    import polymarket_weather_arb.adapters.polymarket.client as client_mod

    def build_with_settings(timeout=20, *, settings=None, **kwargs):
        if settings is None:
            raise AssertionError("settings required")
        return FakeHttpx()

    monkeypatch.setattr(client_mod, "build_httpx_client", build_with_settings)
    monkeypatch.setattr(
        client_mod,
        "safe_http_read",
        lambda c, m, u, s=None, **kw: (
            FakeResponse()
            if s is settings
            else (_ for _ in ()).throw(AssertionError("settings not passed to safe_http_read"))
        ),
    )

    assert client.list_markets() == []
    assert calls["settings"] == 0
    # restore not required; test ends
    _ = original_settings


def _client(tmp_path):
    return GammaPolymarketClient(
        Settings(
            DATABASE_PATH=tmp_path / "adapter.db",
            POLYMARKET_PRIVATE_KEY="private-key",
            POLYMARKET_FUNDER="0xfunder",
        )
    )


def _raw_trade(*, status):
    return {
        "id": "pending-trade",
        "market": "0xcondition",
        "asset_id": "yes-token",
        "owner": "0xowner",
        "maker_address": "0xmaker",
        "taker_order_id": "our-order",
        "side": "BUY",
        "trader_side": "TAKER",
        "price": "0.18",
        "size": "5.56",
        "outcome": "Yes",
        "status": status,
        "fee_rate_bps": "500",
        "bucket_index": 0,
        "maker_orders": [],
        "match_time": "2026-07-24T04:20:00Z",
        "last_update": "2026-07-24T04:20:01Z",
    }


def _install_fake_polymarket(monkeypatch):
    FakeSecureClient.create_calls = []
    FakeSecureClient.instances = []
    module = ModuleType("polymarket")
    module.SecureClient = FakeSecureClient
    auth_module = ModuleType("polymarket.auth")
    auth_module.BuilderApiKey = BuilderApiKey
    monkeypatch.setitem(sys.modules, "polymarket", module)
    monkeypatch.setitem(sys.modules, "polymarket.auth", auth_module)
