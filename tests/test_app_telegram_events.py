"""Focused tests: /app Autopilot Telegram material-event notifications only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.services.fixture_service import load_market_fixture
from polymarket_weather_arb.services.reconciliation_service import ReconciliationService
from polymarket_weather_arb.services.telegram_notifier import (
    TelegramNotifier,
    classify_payload_level,
    format_telegram_message,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeClient:
    def list_markets(self, *, limit=100, offset=0):
        return []

    def get_event_markets_by_slug(self, slug):
        return []

    def get_balances(self):
        return {"usdc": "10"}

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def get_positions(self):
        return []

    def get_order_book(self, market):
        from datetime import datetime, timezone

        from polymarket_weather_arb.domain.markets import MarketSnapshot

        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.45"),
                midpoint=Decimal("0.425"),
                spread=Decimal("0.05"),
                liquidity=Decimal("1000"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"fake": True},
        )


def _repo(tmp_path, **settings_kwargs):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "app-telegram.db",
        **settings_kwargs,
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    return settings, Repository(connection), connection


def _telegram_settings(**kwargs):
    base = {
        "TELEGRAM_NOTIFY_ENABLED": True,
        "TELEGRAM_BOT_TOKEN": "test-bot-token-not-a-secret-for-assert",
        "TELEGRAM_CHAT_ID": "999001",
        "TELEGRAM_NOTIFY_MIN_LEVEL": "trade",
    }
    base.update(kwargs)
    return base


def _stub_idle_discovery(service, monkeypatch):
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
        lambda self, **kwargs: 0,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
        lambda self, **kwargs: 0,
    )
    monkeypatch.setattr(service, "_select_market", lambda: None)


def _seed_portfolio_digest(repository, connection) -> None:
    connection.execute(
        """
        INSERT INTO markets (id, title, yes_token_id, no_token_id, raw_payload)
        VALUES (
            'digest-market',
            'Will the highest temperature in Test City be 30°C on July 22, 2026?',
            'digest-yes',
            'digest-no',
            '{}'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO temperature_bucket_rules (
            market_id, module_id, city, source, variable, unit,
            bucket_center_c, bucket_lower_c, bucket_upper_c, target_date,
            settlement_timezone, confidence, tradable, raw_text
        ) VALUES (
            'digest-market', 'global_temp_bucket', 'Test City', 'Wunderground',
            'temperature_high', 'C', 30, 29.5, 30.5, '2026-07-22',
            'UTC', 1, 1, 'Test City 30°C'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO positions (market_id, outcome, size, notional)
        VALUES ('digest-market', 'YES', 6, 1.8)
        """
    )
    connection.execute(
        """
        INSERT INTO reconciliations (status, details, created_at)
        VALUES ('ok', '{}', datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO order_intents (
            id, market_id, side, token_id, limit_price, size, notional,
            rationale, dry_run, status
        ) VALUES
            (101, 'digest-market', 'buy_yes', 'digest-yes', 0.2, 10, 2,
             'digest buy', 0, 'filled'),
            (102, 'digest-market', 'sell_yes', 'digest-yes', 0.3, 4, 1.2,
             'digest sell', 0, 'matched')
        """
    )
    connection.execute(
        """
        INSERT INTO order_attempts (intent_id, status, request_payload, response_payload)
        VALUES
            (101, 'submitted', '{}', '{"orderID": "digest-buy-order"}'),
            (102, 'submitted', '{}', '{"orderID": "digest-sell-order"}')
        """
    )
    connection.execute(
        """
        INSERT INTO fills (
            exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at
        ) VALUES
            ('digest-buy-fill', 'digest-market', 'digest-buy-order',
             'BUY', 0.2, 10, 0.1, datetime('now')),
            ('digest-sell-fill', 'digest-market', 'digest-sell-order',
             'SELL', 0.3, 4, 0.05, datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO roundtrip_runs (market_id, buy_intent_id, sell_intent_id, status)
        VALUES ('digest-market', 101, 102, 'sell_open')
        """
    )
    connection.commit()


def test_routine_app_tick_sends_nothing(tmp_path, monkeypatch):
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)
        service.ensure_state(mode="dry_run")
        repository.update_autopilot_state(enabled=True, mode="dry_run", app_mode="paper")
        connection.commit()
        _stub_idle_discovery(service, monkeypatch)

        result = service.tick()
        connection.commit()

        assert result.status == "idle"
        assert sent == []
        assert notifier.sent == []
    finally:
        connection.close()


def test_submitted_live_buy_sends_exactly_one_submitted_event(tmp_path, monkeypatch):
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()

        monkeypatch.setattr(
            service,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        monkeypatch.setattr(service, "_select_market", lambda: market_id)
        monkeypatch.setattr(service, "_prepare_market", lambda _m: None)
        monkeypatch.setattr(
            service,
            "_execute_live",
            lambda _m, _a: (42, ["live order submitted"]),
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
            lambda self: {"status": "ok", "new_fills": []},
        )
        monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))

        result = service.tick()

        assert result.status == "executed"
        assert len(sent) == 1
        assert "买入已提交" in sent[0]
        assert "状态: 已提交" in sent[0]
        assert "profit" not in sent[0].lower()
        assert "matched" not in sent[0].lower()
        assert "completed" not in sent[0].lower()
    finally:
        connection.close()


def test_auto_exit_sell_sends_exactly_one_submitted_event(tmp_path, monkeypatch):
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(
        tmp_path,
        **_telegram_settings(),
        auto_exit_enabled=True,
    )
    try:
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()

        monkeypatch.setattr(
            service,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
            lambda self: {"status": "ok", "new_fills": []},
        )
        monkeypatch.setattr(service, "_select_market", lambda: None)

        class FakeExitResult:
            executed = 1
            attempted = 1
            submissions = [
                {
                    "ok": True,
                    "status": "submitted",
                    "verified": True,
                    "intent_id": 7,
                    "order_id": "ord-sell-1",
                    "market_id": "m-weather-1",
                    "side": "SELL",
                    "outcome": "YES",
                    "price": Decimal("0.55"),
                    "size": Decimal("10"),
                    "warning": None,
                }
            ]

        class FakeAutoExit:
            def __init__(self, *a, **k):
                pass

            def run_tick(self, **kwargs):
                return FakeExitResult()

        monkeypatch.setattr(
            "polymarket_weather_arb.services.auto_exit_service.AutoExitService",
            FakeAutoExit,
        )

        result = service.tick()

        assert result.auto_exit_executed == 1
        assert len(sent) == 1
        assert "卖出已提交" in sent[0]
        assert "状态: 已提交" in sent[0]
        assert "ord-sell-1" in sent[0]
        assert "profit" not in sent[0].lower()
    finally:
        connection.close()


def test_new_fill_notified_once_across_ticks_and_service_instances(tmp_path, monkeypatch):
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        # Point trade market id to local fixture market for resolve_local_market_id.
        trade = {
            "id": "fill-unique-abc",
            "order_id": "order-abc",
            "market": market_id,
            "side": "BUY",
            "price": "0.42",
            "size": "5",
            "fee": "0",
            "timestamp": "2026-07-11T00:00:00+00:00",
        }

        class FillClient(FakeClient):
            def get_trades(self):
                return [trade]

            def get_balances(self):
                return {"usdc": "10"}

            def get_orders(self):
                return []

            def get_positions(self):
                return []

        # First reconcile: insert fill.
        recon1 = ReconciliationService(FillClient(), repository).reconcile()
        connection.commit()
        assert recon1["fills_stored"] == 1
        assert len(recon1["new_fills"]) == 1
        assert recon1["new_fills"][0]["exchange_fill_id"] == "fill-unique-abc"

        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FillClient(), notifier=notifier)
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()
        monkeypatch.setattr(
            service,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        _stub_idle_discovery(service, monkeypatch)
        monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))

        # Tick 1 after insert already done: reconcile sees same fill -> no new.
        # Seed: notify path only for new_fills from this tick's reconcile.
        # Run tick: fill already in DB so new_fills empty on second reconcile.
        result1 = service.tick()
        assert result1.status == "idle"
        assert sent == []  # already persisted before tick

        # Simulate first-time discovery during a tick by clearing and re-inserting via reconcile.
        # Fresh service instance should still not re-notify the same exchange_fill_id.
        service2 = AutopilotService(settings, repository, client=FillClient(), notifier=notifier)
        monkeypatch.setattr(
            service2,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        _stub_idle_discovery(service2, monkeypatch)
        monkeypatch.setattr(service2, "_maybe_auto_exit", lambda **_k: (0, 0))
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        service2.tick()
        assert sent == []

        # Direct unit: first insert notifies once; second reconcile of same id notifies zero.
        sent.clear()
        notifier2 = TelegramNotifier(settings, sender=fake_sender)
        # Wipe fills table content for controlled insert via save path.
        connection.execute("DELETE FROM fills")
        connection.commit()
        service3 = AutopilotService(settings, repository, client=FillClient(), notifier=notifier2)
        monkeypatch.setattr(
            service3,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        _stub_idle_discovery(service3, monkeypatch)
        monkeypatch.setattr(service3, "_maybe_auto_exit", lambda **_k: (0, 0))
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        service3.tick()
        assert len(sent) == 1
        assert "成交确认" in sent[0]
        assert "状态: 已成交" in sent[0]
        assert "fill-unique-abc" in sent[0]

        # Same fill again on next tick / new service: no second notification.
        service4 = AutopilotService(settings, repository, client=FillClient(), notifier=notifier2)
        monkeypatch.setattr(
            service4,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        _stub_idle_discovery(service4, monkeypatch)
        monkeypatch.setattr(service4, "_maybe_auto_exit", lambda **_k: (0, 0))
        service4.tick()
        assert len(sent) == 1
    finally:
        connection.close()


class _ConnectionCommitGate:
    """Wrap sqlite3.Connection so tests can fail or observe commit order."""

    def __init__(self, inner, *, fail_from: int | None = None, order: list[str] | None = None):
        self._inner = inner
        self._fail_from = fail_from
        self._order = order
        self._n = 0

    def commit(self) -> None:
        self._n += 1
        if self._order is not None:
            self._order.append("commit")
        if self._fail_from is not None and self._n >= self._fail_from:
            raise RuntimeError("simulated reconciliation commit failure")
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_portfolio_digest_sends_holdings_after_fresh_reconciliation(tmp_path):
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        _seed_portfolio_digest(repository, connection)
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)
        now = datetime.now(timezone.utc)

        service._maybe_notify_portfolio_digest(now=now)
        service._flush_notifier()

        assert len(sent) == 1
        assert "天气持仓 · 4小时摘要" in sent[0]
        assert "Test City · 30°C · YES" in sent[0]
        assert "持仓周期估算: +$0.85 (+40.5%)" in sent[0]
        assert "当前持仓估值: $1.80" in sent[0]
        assert (
            "当地目标日结束" in sent[0]
            or "目标日 2026-07-22" in sent[0]
            or "2026-07-22 已结束 · 等待结算" in sent[0]
        )
        state = repository.get_autopilot_state()
        assert state is not None
        assert state["last_portfolio_digest_at"] == now.isoformat()
    finally:
        connection.close()


def test_portfolio_digest_interval_survives_restart(tmp_path):
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    now = datetime.now(timezone.utc)
    try:
        _seed_portfolio_digest(repository, connection)
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)
        service._maybe_notify_portfolio_digest(now=now)
        service._flush_notifier()
        assert len(sent) == 1
    finally:
        connection.close()

    reopened = Database(settings.database_path).connect()
    try:
        repository2 = Repository(reopened)
        notifier2 = TelegramNotifier(settings, sender=fake_sender)
        service2 = AutopilotService(
            settings,
            repository2,
            client=FakeClient(),
            notifier=notifier2,
        )

        service2._maybe_notify_portfolio_digest(now=now + timedelta(hours=1))
        service2._flush_notifier()
        assert len(sent) == 1

        service2._maybe_notify_portfolio_digest(now=now + timedelta(hours=4, seconds=1))
        service2._flush_notifier()
        assert len(sent) == 2
    finally:
        reopened.close()


def test_portfolio_digest_skips_empty_or_stale_portfolio(tmp_path):
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)
        service._maybe_notify_portfolio_digest()
        service._flush_notifier()
        assert sent == []

        _seed_portfolio_digest(repository, connection)
        connection.execute("UPDATE reconciliations SET created_at = '2000-01-01T00:00:00+00:00'")
        connection.commit()
        service._maybe_notify_portfolio_digest()
        service._flush_notifier()
        assert sent == []
        assert repository.get_autopilot_state()["last_portfolio_digest_at"] is None
    finally:
        connection.close()


def test_portfolio_digest_runs_only_after_successful_reconciliation(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        service = AutopilotService(settings, repository, client=FakeClient())
        calls: list[str] = []
        monkeypatch.setattr(
            service,
            "_maybe_notify_portfolio_digest",
            lambda: calls.append("digest"),
        )

        service._commit_reconciliation_then_notify_fills(
            {"status": "adapter-error", "new_fills": []}
        )
        assert calls == []

        service._commit_reconciliation_then_notify_fills({"status": "ok", "new_fills": []})
        assert calls == ["digest"]
    finally:
        connection.close()


def test_portfolio_digest_commit_failure_does_not_send(tmp_path):
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        _seed_portfolio_digest(repository, connection)
        repository.connection = _ConnectionCommitGate(connection, fail_from=1)
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)

        service._maybe_notify_portfolio_digest()
        service._flush_notifier()

        assert sent == []
        assert (
            connection.execute(
                "SELECT last_portfolio_digest_at FROM autopilot_state WHERE id = 1"
            ).fetchone()["last_portfolio_digest_at"]
            is None
        )
    finally:
        connection.close()


def test_fill_notify_skipped_when_reconciliation_commit_fails(tmp_path, monkeypatch):
    """Commit failure after reconcile must not emit fill Telegram."""
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        trade = {
            "id": "fill-commit-fail-1",
            "order_id": "order-fail-1",
            "market": market_id,
            "side": "BUY",
            "price": "0.41",
            "size": "3",
            "fee": "0",
            "timestamp": "2026-07-11T01:00:00+00:00",
        }

        class FillClient(FakeClient):
            def get_trades(self):
                return [trade]

        # Live tick commits reconciliation first; fail that durable commit.
        repository.connection = _ConnectionCommitGate(connection, fail_from=1)

        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FillClient(), notifier=notifier)
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        # Bypass gate for setup durability.
        connection.commit()

        monkeypatch.setattr(
            service,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        _stub_idle_discovery(service, monkeypatch)
        monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))

        result = service.tick()

        assert sent == []
        assert result.status == "failed"
        # Rolled-back insert must not be durable on the real connection.
        row = connection.execute(
            "SELECT 1 FROM fills WHERE exchange_fill_id = ?",
            ("fill-commit-fail-1",),
        ).fetchone()
        assert row is None
    finally:
        connection.close()


def test_successful_reconciliation_refreshes_roundtrip_before_commit(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    service = AutopilotService(settings, repository, client=FakeClient())
    calls: list[str] = []

    def refresh_status(_self, market_id):
        calls.append(market_id)
        return None

    monkeypatch.setattr(
        "polymarket_weather_arb.services.roundtrip_status_service."
        "RoundtripStatusService.get_status",
        refresh_status,
    )

    service._commit_reconciliation_then_notify_fills(
        {
            "status": "ok",
            "new_fills": [
                {"exchange_fill_id": "fill-1", "market_id": "market-1"},
                {"exchange_fill_id": "fill-2", "market_id": "market-1"},
            ],
        }
    )

    assert calls == ["market-1"]
    connection.close()


def test_successful_reconciliation_heals_sell_open_run_without_new_fill(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    service = AutopilotService(settings, repository, client=FakeClient())
    calls: list[str] = []

    monkeypatch.setattr(
        repository,
        "list_roundtrip_markets_needing_status_refresh",
        lambda limit=100: ["market-stale"],
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.roundtrip_status_service."
        "RoundtripStatusService.get_status",
        lambda _self, market_id: calls.append(market_id),
    )

    service._commit_reconciliation_then_notify_fills({"status": "ok", "new_fills": []})

    assert calls == ["market-stale"]
    connection.close()


def test_roundtrip_recovery_scan_failure_does_not_block_reconciliation_commit(
    tmp_path, monkeypatch
):
    settings, repository, connection = _repo(tmp_path)
    service = AutopilotService(settings, repository, client=FakeClient())
    repository.save_reconciliation("ok", {"marker": "must-be-durable"})

    def fail_scan(limit=100):
        raise RuntimeError("roundtrip scan unavailable")

    monkeypatch.setattr(
        repository,
        "list_roundtrip_markets_needing_status_refresh",
        fail_scan,
    )

    service._commit_reconciliation_then_notify_fills({"status": "ok", "new_fills": []})
    connection.close()

    reopened = Database(settings.database_path).connect()
    try:
        row = reopened.execute(
            "SELECT details FROM reconciliations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert "must-be-durable" in row["details"]
    finally:
        reopened.close()


def test_recon_alert_state_is_durable_before_failure_and_recovery_notify(tmp_path, monkeypatch):
    """Separate /app services dedupe and recover through the real tick path."""
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    class BalanceFailureClient(FakeClient):
        def get_balances(self):
            raise RuntimeError("balances timeout")

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        repository.ensure_autopilot_state(
            mode="live",
            app_mode="micro_live",
            tick_seconds=300,
        )
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()

        def configure(service):
            monkeypatch.setattr(
                service,
                "collect_blockers",
                lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
            )
            monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_kwargs: (0, 0))
            _stub_idle_discovery(service, monkeypatch)

        first_notifier = TelegramNotifier(settings, sender=fake_sender)
        first = AutopilotService(
            settings,
            repository,
            client=BalanceFailureClient(),
            notifier=first_notifier,
        )
        configure(first)
        assert first.tick().status == "failed"
        assert len(sent) == 1

        # A new service instance reads the committed signature and stays quiet.
        second_notifier = TelegramNotifier(settings, sender=fake_sender)
        second = AutopilotService(
            settings,
            repository,
            client=BalanceFailureClient(),
            notifier=second_notifier,
        )
        configure(second)
        assert second.tick().status == "failed"
        assert len(sent) == 1

        # A third, healthy instance durably clears the signature before sending
        # one recovery. A fourth healthy instance must remain quiet.
        third_notifier = TelegramNotifier(settings, sender=fake_sender)
        third = AutopilotService(
            settings,
            repository,
            client=FakeClient(),
            notifier=third_notifier,
        )
        configure(third)
        assert third.tick().status == "idle"
        assert len(sent) == 2
        assert "对账已恢复" in sent[-1]

        fourth_notifier = TelegramNotifier(settings, sender=fake_sender)
        fourth = AutopilotService(
            settings,
            repository,
            client=FakeClient(),
            notifier=fourth_notifier,
        )
        configure(fourth)
        assert fourth.tick().status == "idle"
        assert len(sent) == 2
    finally:
        connection.close()


def test_recon_failure_notification_skipped_when_signature_commit_fails(tmp_path, monkeypatch):
    """Never flush a failure alert whose dedupe signature was not committed."""
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    class BalanceFailureClient(FakeClient):
        def get_balances(self):
            raise RuntimeError("balances timeout")

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        repository.ensure_autopilot_state(
            mode="live",
            app_mode="micro_live",
            tick_seconds=300,
        )
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()

        # Reconciliation durability is commit #1; alert signature is commit #2.
        repository.connection = _ConnectionCommitGate(connection, fail_from=2)
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(
            settings,
            repository,
            client=BalanceFailureClient(),
            notifier=notifier,
        )
        monkeypatch.setattr(
            service,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )

        result = service.tick()

        assert result.status == "failed"
        assert sent == []
        assert notifier.sent == []
        assert "state commit failed" in result.reason
    finally:
        connection.close()


def test_fill_notify_only_after_successful_commit_and_restart_dedup(tmp_path, monkeypatch):
    """Fill Telegram only after durable commit; restart must not re-notify same id."""
    sent: list[str] = []
    order: list[str] = []

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        fill_id = "fill-restart-dedup-1"
        trade = {
            "id": fill_id,
            "order_id": "order-restart-1",
            "market": market_id,
            "side": "BUY",
            "price": "0.43",
            "size": "4",
            "fee": "0",
            "timestamp": "2026-07-11T02:00:00+00:00",
        }

        class FillClient(FakeClient):
            def get_trades(self):
                return [trade]

        def fake_sender(token, chat_id, text):
            # At send time the fill row must already be committed/readable.
            row = connection.execute(
                "SELECT exchange_fill_id FROM fills WHERE exchange_fill_id = ?",
                (fill_id,),
            ).fetchone()
            assert row is not None
            order.append("send")
            sent.append(text)
            return {"ok": True}

        repository.connection = _ConnectionCommitGate(connection, order=order)

        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FillClient(), notifier=notifier)
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()
        order.clear()

        monkeypatch.setattr(
            service,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        _stub_idle_discovery(service, monkeypatch)
        monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))

        service.tick()

        assert len(sent) == 1
        assert "成交确认" in sent[0]
        assert fill_id in sent[0]
        # At least one commit must precede the fill send (post-reconcile durable write).
        assert "commit" in order
        assert "send" in order
        assert order.index("commit") < order.index("send")

        # Simulate process restart: new connection + service, same durable fills table.
        connection.close()
        database = Database(settings.database_path)
        connection2 = database.connect()
        repository2 = Repository(connection2)
        sent_after_restart: list[str] = []

        def sender2(token, chat_id, text):
            sent_after_restart.append(text)
            return {"ok": True}

        notifier2 = TelegramNotifier(settings, sender=sender2)
        service2 = AutopilotService(settings, repository2, client=FillClient(), notifier=notifier2)
        monkeypatch.setattr(
            service2,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        _stub_idle_discovery(service2, monkeypatch)
        monkeypatch.setattr(service2, "_maybe_auto_exit", lambda **_k: (0, 0))
        repository2.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection2.commit()

        service2.tick()
        assert sent_after_restart == []
        # Durable row still present after restart.
        assert (
            connection2.execute(
                "SELECT 1 FROM fills WHERE exchange_fill_id = ?",
                (fill_id,),
            ).fetchone()
            is not None
        )
        connection2.close()
    finally:
        try:
            connection.close()
        except Exception:
            pass


def test_telegram_sender_failure_does_not_change_successful_tick(tmp_path, monkeypatch):
    def boom_sender(token, chat_id, text):
        raise RuntimeError("telegram down")

    settings, repository, connection = _repo(tmp_path, **_telegram_settings())
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        notifier = TelegramNotifier(settings, sender=boom_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()

        monkeypatch.setattr(
            service,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        monkeypatch.setattr(service, "_select_market", lambda: market_id)
        monkeypatch.setattr(service, "_prepare_market", lambda _m: None)
        monkeypatch.setattr(
            service,
            "_execute_live",
            lambda _m, _a: (11, ["live order submitted"]),
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
            lambda self: {"status": "ok", "new_fills": []},
        )
        monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))

        result = service.tick()
        connection.commit()

        assert result.status == "executed"
        assert result.intent_id == 11
        assert notifier.errors
        assert "telegram down" in notifier.errors[0]
    finally:
        connection.close()


def test_missing_telegram_config_no_send_no_tick_failure(tmp_path, monkeypatch):
    sent: list[str] = []

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    # Not ready: disabled.
    settings, repository, connection = _repo(
        tmp_path,
        TELEGRAM_NOTIFY_ENABLED=False,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="1",
    )
    try:
        from polymarket_weather_arb.dashboard import build_app_telegram_notifier

        assert build_app_telegram_notifier(settings) is None

        # Even if a notifier is injected but not ready, flush sends nothing.
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)
        service.ensure_state(mode="dry_run")
        repository.update_autopilot_state(enabled=True, mode="dry_run")
        connection.commit()
        _stub_idle_discovery(service, monkeypatch)

        result = service.tick()
        assert result.status == "idle"
        assert sent == []
    finally:
        connection.close()


def test_event_payloads_contain_no_secret_configuration_values(tmp_path, monkeypatch):
    sent: list[str] = []
    secret_token = "super-secret-bot-token-xyz"
    secret_key = "0xdeadbeefprivatekey"

    def fake_sender(token, chat_id, text):
        sent.append(text)
        return {"ok": True}

    settings, repository, connection = _repo(
        tmp_path,
        TELEGRAM_NOTIFY_ENABLED=True,
        TELEGRAM_BOT_TOKEN=secret_token,
        TELEGRAM_CHAT_ID="12345",
        TELEGRAM_NOTIFY_MIN_LEVEL="trade",
        POLYMARKET_PRIVATE_KEY=secret_key,
    )
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        notifier = TelegramNotifier(settings, sender=fake_sender)
        service = AutopilotService(settings, repository, client=FakeClient(), notifier=notifier)
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()

        monkeypatch.setattr(
            service,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        monkeypatch.setattr(service, "_select_market", lambda: market_id)
        monkeypatch.setattr(service, "_prepare_market", lambda _m: None)
        monkeypatch.setattr(
            service,
            "_execute_live",
            lambda _m, _a: (3, ["live order submitted"]),
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
            lambda self: {
                "status": "ok",
                "new_fills": [
                    {
                        "exchange_fill_id": "f1",
                        "order_id": "o1",
                        "market_id": market_id,
                        "side": "BUY",
                        "price": 0.4,
                        "size": 2,
                    }
                ],
            },
        )
        monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))

        service.tick()

        assert len(sent) == 2  # fill + buy
        blob = "\n".join(sent)
        assert secret_token not in blob
        assert secret_key not in blob
        assert "private" not in blob.lower()
        assert "POLYMARKET_PRIVATE_KEY" not in blob
    finally:
        connection.close()


def test_classify_app_events_are_trade_or_risk():
    assert classify_payload_level({"daemon_event": "app_buy_submitted"}) == "trade"
    assert classify_payload_level({"daemon_event": "app_sell_submitted"}) == "trade"
    assert classify_payload_level({"daemon_event": "app_fill"}) == "trade"
    assert classify_payload_level({"daemon_event": "app_portfolio_digest"}) == "trade"
    assert classify_payload_level({"daemon_event": "app_order_unverified"}) == "risk"


def test_format_app_message_includes_useful_fields_only():
    text = format_telegram_message(
        {
            "daemon_event": "app_buy_submitted",
            "status": "submitted",
            "summary": "实盘买入限价单已提交",
            "market_id": "m1",
            "side": "BUY",
            "price": "0.44",
            "size": "5",
            "intent_id": 9,
        }
    )
    assert "事件: 买入已提交" in text
    assert "状态: 已提交" in text
    assert "方向: 买入" in text
    assert "价格: 0.44" in text
    assert "意图ID: 9" in text
    assert "说明: 实盘买入限价单已提交" in text


def test_save_reconciled_fills_returns_only_newly_inserted(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        fill = {
            "id": "ex-fill-1",
            "order_id": "ord-1",
            "market": market_id,
            "side": "BUY",
            "price": "0.3",
            "size": "4",
        }
        count1, new1 = repository.save_reconciled_fills([fill])
        connection.commit()
        assert count1 == 1
        assert [row["exchange_fill_id"] for row in new1] == ["ex-fill-1"]

        count2, new2 = repository.save_reconciled_fills([fill])
        connection.commit()
        assert count2 == 1
        assert new2 == []
    finally:
        connection.close()
