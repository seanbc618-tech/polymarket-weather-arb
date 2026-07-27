"""Runtime efficiency: discovery quotes, recon stages, telegram dedup, phase logs, settings."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.services.discovery_service import (
    DISCOVERY_CLOB_FALLBACK_LIMIT,
    DiscoveryService,
    snapshot_from_gamma_summary,
)
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


_TEST_MARKET_DATE = (datetime.now(timezone.utc) + timedelta(days=1)).date()
_TEST_MARKET_LABEL = f"{_TEST_MARKET_DATE:%B} {_TEST_MARKET_DATE.day}, {_TEST_MARKET_DATE.year}"
_TEST_MARKET_SOURCE_DATE = (
    f"{_TEST_MARKET_DATE.day} {_TEST_MARKET_DATE:%b} '{str(_TEST_MARKET_DATE.year)[2:]}"
)
_TEST_MARKET_SLUG_DATE = (
    f"{_TEST_MARKET_DATE:%B}-{_TEST_MARKET_DATE.day}-{_TEST_MARKET_DATE.year}".lower()
)


def test_blank_llm_enabled_is_disabled(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "")
    settings = Settings(_env_file=None, DATABASE_PATH=":memory:")
    assert settings.llm_enabled is False


def test_blank_llm_enabled_does_not_block_settings_reload(tmp_path, monkeypatch):
    """Hot-path Settings construction with blank optional LLM must not raise."""
    monkeypatch.setenv("LLM_ENABLED", "")
    monkeypatch.delenv("MAX_ORDER_USDC", raising=False)
    settings = Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "x.db"))
    assert settings.llm_enabled is False
    # Risk/money fields still parse strictly when invalid.
    monkeypatch.setenv("MAX_ORDER_USDC", "not-a-number")
    with pytest.raises(Exception):
        Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "y.db"))


def test_gamma_summary_snapshot_from_payload():
    snap = snapshot_from_gamma_summary(
        "m1",
        {"bestBid": "0.12", "bestAsk": "0.15", "spread": "0.03", "liquidity": "500"},
    )
    assert snap is not None
    snapshot, raw = snap
    assert snapshot.best_bid == Decimal("0.12")
    assert snapshot.best_ask == Decimal("0.15")
    assert raw["source"] == "gamma-summary"


class _GammaQuotedClient:
    def __init__(self, n: int = 120):
        self.n = n
        self.order_book_calls = 0

    def list_markets(self, limit: int = 100, offset: int = 0):
        if offset > 0:
            return []
        markets = []
        for i in range(self.n):
            lo = 84 + (i % 10)
            hi = lo + 1
            markets.append(
                (
                    Market(
                        id=f"bucket-{i}",
                        slug=f"miami-temp-{lo}-{hi}",
                        title=(
                            f"Will the highest temperature in Miami be between "
                            f"{lo}-{hi}°F on {_TEST_MARKET_LABEL}?"
                        ),
                        description=(
                            "This market will resolve to the temperature range that contains "
                            "the highest temperature recorded at the Miami Intl Airport Station "
                            f"in degrees Fahrenheit on {_TEST_MARKET_SOURCE_DATE}. "
                            "The resolution source for this "
                            "market will be information from Wunderground."
                        ),
                        yes_token_id=f"yes-{i}",
                        no_token_id=f"no-{i}",
                        is_weather=True,
                    ),
                    {
                        "id": f"bucket-{i}",
                        "bestBid": "0.10",
                        "bestAsk": "0.12",
                        "spread": "0.02",
                        "liquidity": "100",
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                        "enableOrderBook": True,
                    },
                )
            )
        return markets[:limit]

    def get_order_book(self, market: Market):
        self.order_book_calls += 1
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.10"),
                best_ask=Decimal("0.12"),
                midpoint=Decimal("0.11"),
                spread=Decimal("0.02"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )


def test_discovery_zero_clob_calls_when_gamma_quotes_present(tmp_path):
    database = Database(tmp_path / "disc.db")
    database.init_schema()
    connection = database.connect()
    client = _GammaQuotedClient(n=120)
    try:
        repo = Repository(connection)
        count = DiscoveryService(client, repo).discover(limit=120, pages=1)
        assert count >= 100
        assert client.order_book_calls == 0
        snap = repo.latest_market_snapshot("bucket-0")
        assert snap is not None
        assert Decimal(str(snap["best_bid"])) == Decimal("0.10")
    finally:
        connection.close()


class _MissingQuoteClient:
    def __init__(self, n: int = 40):
        self.n = n
        self.order_book_calls = 0

    def list_markets(self, limit: int = 100, offset: int = 0):
        if offset > 0:
            return []
        out = []
        for i in range(self.n):
            lo = 70 + (i % 5)
            hi = lo + 1
            out.append(
                (
                    Market(
                        id=f"missing-{i}",
                        slug=f"miami-temp-missing-{i}",
                        title=(
                            f"Will the highest temperature in Miami be between "
                            f"{lo}-{hi}°F on {_TEST_MARKET_LABEL}?"
                        ),
                        description=(
                            "This market will resolve to the temperature range that contains "
                            "the highest temperature recorded at the Miami Intl Airport Station "
                            f"in degrees Fahrenheit on {_TEST_MARKET_SOURCE_DATE}. "
                            "The resolution source for this "
                            "market will be information from Wunderground."
                        ),
                        yes_token_id=f"yes-{i}",
                        no_token_id=f"no-{i}",
                        is_weather=True,
                    ),
                    {
                        "id": f"missing-{i}",
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                        "enableOrderBook": True,
                    },
                )
            )
        return out[:limit]

    def get_order_book(self, market: Market):
        self.order_book_calls += 1
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.20"),
                best_ask=Decimal("0.22"),
                midpoint=Decimal("0.21"),
                spread=Decimal("0.02"),
                liquidity=Decimal("50"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )


def test_discovery_missing_quote_fallback_is_strictly_bounded(tmp_path):
    database = Database(tmp_path / "disc2.db")
    database.init_schema()
    connection = database.connect()
    client = _MissingQuoteClient(n=40)
    try:
        repo = Repository(connection)
        service = DiscoveryService(client, repo)
        count = service.discover(limit=40, pages=1)
        assert count == 40
        assert client.order_book_calls == DISCOVERY_CLOB_FALLBACK_LIMIT
        assert service.clob_book_calls == DISCOVERY_CLOB_FALLBACK_LIMIT
        assert client.order_book_calls < 40
    finally:
        connection.close()


class _StageFailClient:
    def __init__(self, fail_stage: str):
        self.fail_stage = fail_stage
        self.calls: list[str] = []

    def get_balances(self):
        self.calls.append("balances")
        if self.fail_stage == "balances":
            raise ValueError("balance adapter boom")
        return {"usdc": "10"}

    def get_orders(self):
        self.calls.append("orders")
        if self.fail_stage == "orders":
            raise ValueError("orders adapter boom")
        return []

    def get_trades(self):
        self.calls.append("trades")
        if self.fail_stage == "trades":
            raise ValueError("trades adapter boom")
        return []

    def get_positions(self):
        self.calls.append("positions")
        if self.fail_stage == "positions":
            raise NotImplementedError("positions adapter pending")
        return []


@pytest.mark.parametrize("stage", ["balances", "orders", "trades", "positions"])
def test_reconciliation_stage_failures_are_fail_stop_and_identifiable(tmp_path, stage):
    database = Database(tmp_path / f"recon-{stage}.db")
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        client = _StageFailClient(stage)
        result = ReconciliationService(client, repo).reconcile()
        assert result["failed_stage"] == stage
        assert result["status"] in {"adapter-error", "adapter-pending"}
        assert stage in str(result["error"])
        # No later stages after failure.
        assert stage in client.calls
        later = {"balances": ["orders", "trades", "positions"], "orders": ["trades", "positions"], "trades": ["positions"], "positions": []}[
            stage
        ]
        for name in later:
            assert name not in client.calls
        row = repo.latest_reconciliation()
        assert row is not None
        assert row["status"] == result["status"]
        details = row["details"] if "details" in row.keys() else ""
        assert stage in str(details)
    finally:
        connection.close()


def test_reconciliation_ok_has_no_failed_stage(tmp_path):
    database = Database(tmp_path / "recon-ok.db")
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)

        class OkClient:
            def get_balances(self):
                return {"usdc": "1"}

            def get_orders(self):
                return []

            def get_trades(self):
                return []

            def get_positions(self):
                return []

        result = ReconciliationService(OkClient(), repo).reconcile()
        assert result["status"] == "ok"
        assert result["failed_stage"] is None
    finally:
        connection.close()


def test_recon_telegram_dedup_across_service_instances(tmp_path):
    """Simulate /app: each cycle builds a new AutopilotService on shared DB state."""
    from polymarket_weather_arb.services.autopilot_service import (
        AutopilotService,
        build_recon_alert_signature,
    )

    database = Database(tmp_path / "tg.db")
    database.init_schema()
    connection = database.connect()
    payloads: list[dict] = []

    def notifier(payload):
        payloads.append(dict(payload))

    try:
        repo = Repository(connection)
        settings = Settings(
            DATABASE_PATH=tmp_path / "tg.db",
            TRADING_DISABLED=True,
            POLYMARKET_PRIVATE_KEY="k",
            POLYMARKET_FUNDER="0x1",
        )
        recon_timeout = {
            "status": "adapter-error",
            "failed_stage": "balances",
            "error_type": "TimeoutError",
            "error": "stage=balances: balances timeout",
        }
        recon_auth = {
            "status": "adapter-error",
            "failed_stage": "balances",
            "error_type": "RuntimeError",
            "error": "stage=balances: balances authentication failure",
        }
        sig_timeout = build_recon_alert_signature(recon_timeout)
        sig_auth = build_recon_alert_signature(recon_auth)
        assert sig_timeout != sig_auth

        # Instance A: first failure notifies.
        a = AutopilotService(settings, repo, notifier=notifier)
        prior = a._load_recon_alert_signature()
        assert prior is None
        repo.update_autopilot_state(last_error=sig_timeout, last_tick_status="failed")
        a._notify_reconciliation_failure(
            signature=sig_timeout,
            prior_signature=prior,
            recon_status="adapter-error",
            failed_stage="balances",
            reason="timeout",
        )
        assert sum(1 for p in payloads if p.get("status") == "failed") == 1

        # Instance B (new service, same DB): identical failure suppressed.
        b = AutopilotService(settings, repo, notifier=notifier)
        prior_b = b._load_recon_alert_signature()
        assert prior_b == sig_timeout
        b._notify_reconciliation_failure(
            signature=sig_timeout,
            prior_signature=prior_b,
            recon_status="adapter-error",
            failed_stage="balances",
            reason="timeout again",
        )
        assert sum(1 for p in payloads if p.get("status") == "failed") == 1

        # Same stage, different error type/message: material, notify again.
        c = AutopilotService(settings, repo, notifier=notifier)
        prior_c = c._load_recon_alert_signature()
        c._notify_reconciliation_failure(
            signature=sig_auth,
            prior_signature=prior_c,
            recon_status="adapter-error",
            failed_stage="balances",
            reason="auth",
        )
        repo.update_autopilot_state(last_error=sig_auth, last_tick_status="failed")
        assert sum(1 for p in payloads if p.get("status") == "failed") == 2

        # Healthy instance: exactly one recovery; second healthy stays quiet.
        d = AutopilotService(settings, repo, notifier=notifier)
        d._notify_reconciliation_recovery_if_needed()
        e = AutopilotService(settings, repo, notifier=notifier)
        e._notify_reconciliation_recovery_if_needed()
        assert sum(1 for p in payloads if p.get("status") == "recovered") == 1

        # BUY/SELL/fill path still uses the plain _notify channel unchanged.
        e._notify(
            "app_buy_submitted",
            {"status": "submitted", "summary": "buy", "kind": "trade_event"},
        )
        e._notify(
            "app_sell_submitted",
            {"status": "submitted", "summary": "sell", "kind": "trade_event"},
        )
        e._notify(
            "app_fill",
            {"status": "filled", "summary": "fill", "kind": "trade_event"},
        )
        assert sum(1 for p in payloads if p.get("daemon_event") == "app_buy_submitted") == 1
        assert sum(1 for p in payloads if p.get("daemon_event") == "app_sell_submitted") == 1
        assert sum(1 for p in payloads if p.get("daemon_event") == "app_fill") == 1
    finally:
        connection.close()


def test_combined_discovery_fallback_budget_shared_like_autopilot(tmp_path, monkeypatch):
    """Autopilot calls weather-events then discover; total CLOB fallbacks <= 5."""
    database = Database(tmp_path / "budget.db")
    database.init_schema()
    connection = database.connect()
    client = _MissingQuoteClient(n=40)

    def event_markets(slug: str):
        return client.list_markets(limit=15, offset=0)

    client.get_event_markets_by_slug = event_markets  # type: ignore[method-assign]
    try:
        repo = Repository(connection)
        discovery = DiscoveryService(client, repo)
        monkeypatch.setattr(
            discovery,
            "_fetch_weather_event_slugs",
            lambda now=None: (
                [f"highest-temperature-in-miami-on-{_TEST_MARKET_SLUG_DATE}"],
                [],
            ),
        )
        # Same call pattern as AutopilotService discovery phase.
        discovery.reset_fallback_budget()
        discovery.discover_weather_events(limit=1, reset_fallback_budget=False)
        discovery.discover(limit=40, pages=1, reset_fallback_budget=False)
        total = client.order_book_calls
        assert total == DISCOVERY_CLOB_FALLBACK_LIMIT
        assert discovery.clob_book_calls == total
        assert total <= 5
        assert total < 10
    finally:
        connection.close()


def test_phase_duration_logging_has_no_secrets(caplog, tmp_path):
    from polymarket_weather_arb.services.autopilot_service import AutopilotService

    database = Database(tmp_path / "phase.db")
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        settings = Settings(
            DATABASE_PATH=tmp_path / "phase.db",
            POLYMARKET_PRIVATE_KEY="super-secret-key-xyz",
            POLYMARKET_FUNDER="0xfunder",
        )
        service = AutopilotService(settings, repo)
        with caplog.at_level(logging.INFO):
            service._log_phase("tick-1", "reconciliation", __import__("time").monotonic(), items=4)
        text = " ".join(r.message for r in caplog.records)
        assert "phase=reconciliation" in text
        assert "elapsed_ms=" in text
        assert "super-secret-key-xyz" not in text
        assert "private" not in text.lower() or "private_key" not in text.lower()
    finally:
        connection.close()


def test_secure_client_create_once_across_recon_reads(monkeypatch, tmp_path):
    import sys
    from types import ModuleType

    from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient

    class LocalFake:
        create_calls: list = []

        def __init__(self):
            self.closed = False

        @classmethod
        def create(cls, *, private_key, wallet):
            cls.create_calls.append(1)
            return cls()

        def get_balance_allowance(self, *, asset_type):
            return SimpleNamespace(model_dump=lambda mode="json": {"balance": 1})

        def list_open_orders(self):
            return []

        def list_account_trades(self):
            return []

        def list_positions(self, *, user):
            return []

        def close(self):
            self.closed = True

    LocalFake.create_calls = []
    module = ModuleType("polymarket")
    module.SecureClient = LocalFake
    monkeypatch.setitem(sys.modules, "polymarket", module)

    client = GammaPolymarketClient(
        Settings(
            DATABASE_PATH=tmp_path / "c.db",
            POLYMARKET_PRIVATE_KEY="private-key",
            POLYMARKET_FUNDER="0xfunder",
        )
    )
    database = Database(tmp_path / "c.db")
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        ReconciliationService(client, repo).reconcile()
        ReconciliationService(client, repo).reconcile()
        assert len(LocalFake.create_calls) == 1
        assert client.secure_client_create_count == 1
    finally:
        connection.close()
        client.close()
