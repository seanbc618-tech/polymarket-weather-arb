"""Phase 0 baseline: characterize the existing serial autopilot scheduler.

These tests lock current behavior before multi-cadence work (Phase 2).
No real network mutation.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import _run_autopilot_background
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeClient:
    def close(self) -> None:
        return None


def _settings(tmp_path, **updates) -> Settings:
    base = dict(
        _env_file=None,
        database_path=tmp_path / "baseline.db",
        MAX_ORDER_USDC=Decimal("1"),
        MAX_DAILY_USDC=Decimal("5"),
        MAX_MARKET_USDC=Decimal("2"),
        TRADING_DISABLED=False,
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        AUTO_EXIT_ENABLED=True,
        COMPLIANCE_CHECK_ENABLED=True,
    )
    base.update(updates)
    return Settings(**base)


def test_background_scheduler_ticks_are_serial_non_overlapping(tmp_path, monkeypatch):
    """One running scheduler owns work; a cycle never re-enters while active."""
    database = Database(tmp_path / "serial.db")
    database.init_schema()
    connection = database.connect()
    Repository(connection).update_autopilot_state(enabled=True, mode="dry_run")
    connection.commit()
    connection.close()

    concurrent = {"n": 0, "max": 0}
    tick_calls = {"n": 0}

    def tick(self):
        tick_calls["n"] += 1
        concurrent["n"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["n"])
        # Nested work would raise max > 1 if the runner overlapped ticks.
        concurrent["n"] -= 1
        return SimpleNamespace(status="idle")

    monkeypatch.setattr(AutopilotService, "tick", tick)
    _run_autopilot_background(
        database,
        Settings(_env_file=None, database_path=database.path),
        None,
        tick_seconds=0,
        sleep=lambda _: None,
        max_cycles=3,
        use_pulse=False,
    )
    assert tick_calls["n"] == 3
    assert concurrent["max"] == 1


def test_long_cycle_does_not_overlap_next_start_to_start(tmp_path, monkeypatch):
    """Start-to-start spacing: long tick shortens sleep rather than overlapping."""
    database = Database(tmp_path / "cadence.db")
    database.init_schema()
    connection = database.connect()
    Repository(connection).update_autopilot_state(enabled=True, mode="dry_run")
    connection.commit()
    connection.close()

    clock = [1000.0]
    sleeps: list[float] = []

    def tick(_self):
        clock[0] += 250.0  # long cycle relative to 300s cadence

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(AutopilotService, "tick", tick)
    _run_autopilot_background(
        database,
        Settings(_env_file=None, database_path=database.path),
        None,
        tick_seconds=300,
        sleep=sleep,
        monotonic=lambda: clock[0],
        max_cycles=2,
        use_pulse=False,
    )
    assert sleeps == [50.0]
    assert clock[0] == 1550.0


def test_reconciliation_failure_fail_stops_cancel_sell_and_buy(tmp_path, monkeypatch):
    """Capital cycle fail-stop: failed recon blocks cancel / SELL / BUY."""
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    service = AutopilotService(settings, repository, client=FakeClient())
    service.ensure_state(mode="live")
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    repository.save_reconciliation("ok", {"status": "old-ok"})
    connection.commit()

    mutations: list[str] = []

    # Isolate capital fail-stop from location/compliance gates used in other envs.
    monkeypatch.setattr(
        service,
        "collect_blockers",
        lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
        lambda self: {"status": "error", "failed_stage": "positions", "new_fills": []},
    )
    monkeypatch.setattr(
        service,
        "_maybe_manage_stale_orders",
        lambda **_k: mutations.append("cancel") or [],
    )
    monkeypatch.setattr(
        service,
        "_maybe_auto_exit",
        lambda **_k: mutations.append("sell") or (0, 0),
    )
    monkeypatch.setattr(
        service,
        "_execute_live",
        lambda *_a, **_k: mutations.append("buy") or (1, ["should not run"]),
    )
    monkeypatch.setattr(service, "_select_market", lambda: "m1")
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
        lambda self, **kwargs: 0,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
        lambda self, **kwargs: 0,
    )

    result = service.tick()
    assert result.status == "failed"
    assert "fail-stop" in result.reason
    assert mutations == []
    connection.close()


def test_duplicate_live_intent_blocks_second_entry(tmp_path):
    """Idempotency baseline: active live intent still blocks a sibling entry."""
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    market = Market(
        id="m-dup",
        title="Will the highest temperature in NYC be above 80 on May 8?",
        description="NOAA",
        yes_token_id="y",
        no_token_id="n",
        is_weather=True,
    )
    repository.upsert_market(market, {"id": "m-dup"})
    repository.connection.execute(
        """
        INSERT INTO order_intents (
            market_id, side, token_id, limit_price, size, notional,
            rationale, dry_run, status, idempotency_key
        ) VALUES (?, 'buy_yes', 'y', 0.4, 5, 2.0, 'active', 0, 'submitted', 'k1')
        """,
        ("m-dup",),
    )
    connection.commit()
    assert repository.active_live_order_intent("m-dup", "buy_yes") is not None
    # Second entry path uses the same active-intent gate.
    assert repository.active_live_order_intent("m-dup", "buy_yes")["status"] == "submitted"
    connection.close()


def test_exchange_accepted_attempt_remains_after_later_bookkeeping_failure(tmp_path):
    """Durability baseline: accepted attempt row survives post-accept failure."""
    from unittest.mock import Mock

    import pytest

    from polymarket_weather_arb.domain.risk import RiskContext
    from polymarket_weather_arb.domain.source_grade import OFFICIAL_FORECAST
    from polymarket_weather_arb.services.trading_service import TradingService

    settings = _settings(
        tmp_path,
        MAX_ORDER_USDC=Decimal("5"),
        MAX_DAILY_USDC=Decimal("100"),
        MAX_MARKET_USDC=Decimal("50"),
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    market = Market(
        id="m-ok",
        title="Will the highest temperature in NYC be above 80 on May 8?",
        description="NOAA",
        yes_token_id="y",
        no_token_id="n",
        is_weather=True,
    )
    payload = {"id": "m-ok", "yes_token_id": "y", "no_token_id": "n"}
    repository.upsert_market(market, payload)
    client = Mock()
    client.place_limit_order.return_value = {"orderID": "ex-1", "status": "live"}
    client.validate_order_signing = Mock(return_value={"ok": True})

    service = TradingService(settings, client, repository)
    analysis = Analysis(
        market_id="m-ok",
        model_version="t",
        fair_lower=Decimal("0.9"),
        fair_upper=Decimal("0.95"),
        reference_price=Decimal("0.50"),
        edge=Decimal("0.4"),
        side="buy_yes",
        decision="trade",
        reasons=["t"],
    )
    context = RiskContext(
        daily_live_notional=Decimal("0"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=1,
        forecast_age_seconds=1,
        rule_tradable=True,
        reconciliation_fresh=True,
    )

    def on_submitted(_intent_id):
        raise RuntimeError("caller commit failed")

    with pytest.raises(RuntimeError, match="caller commit failed"):
        service.trade(
            analysis=analysis,
            yes_token_id="y",
            no_token_id="n",
            context=context,
            dry_run=False,
            source_grade=OFFICIAL_FORECAST,
            market_payload=payload,
            on_submitted=on_submitted,
        )

    attempt = connection.execute(
        "SELECT status FROM order_attempts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert attempt is not None
    assert attempt["status"] == "submitted"
    intent = connection.execute(
        "SELECT status FROM order_intents ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert intent["status"] == "submitted"
    connection.close()
