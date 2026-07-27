"""Full-live /app autopilot unlock and lifecycle tests (offline, mocked exchange)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import handle_dashboard_post, render_dashboard_path
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION
from polymarket_weather_arb.profiles import get_profile, settings_for_profile
from polymarket_weather_arb.services.auto_exit_service import AutoExitService
from polymarket_weather_arb.services.autopilot_service import (
    AutopilotService,
    profile_name_for_app_mode,
)
from polymarket_weather_arb.services.compliance_service import ComplianceDecision, ComplianceService
from polymarket_weather_arb.services.fixture_service import load_market_fixture
from polymarket_weather_arb.services.full_auto_service import (
    clear_legacy_global_live_overrides,
)
from polymarket_weather_arb.services.order_lifecycle_service import OrderLifecycleService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeClient:
    def list_markets(self, *, limit=100, offset=0):
        return []

    def get_event_markets_by_slug(self, slug):
        return []

    def get_balances(self):
        return {"usdc": "100"}

    def get_orders(self):
        return []

    def get_trades(self):
        return []

    def get_positions(self):
        return []

    def get_order_book(self, market):
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

    def get_token_order_book(self, token_id):
        return self.get_order_book(type("M", (), {"id": token_id})())

    def cancel_order(self, order_id):
        return {"status": "cancelled", "orderID": order_id}

    def place_limit_order(self, **kwargs):
        return {"order_id": "buy-1", "status": "live"}

    def place_sell_limit_order(self, **kwargs):
        return {"order_id": "sell-1", "status": "live"}

    def get_order(self, order_id):
        return {"id": order_id, "status": "LIVE"}


def _repo(tmp_path, **overrides):
    base = dict(
        _env_file=None,
        database_path=tmp_path / "full-live.db",
        trading_disabled=False,
        auto_exit_enabled=True,
        polymarket_private_key="k",
        polymarket_funder="0xf",
        compliance_check_enabled=False,
        max_order_usdc=Decimal("20"),
        max_daily_usdc=Decimal("80"),
        max_market_usdc=Decimal("40"),
        min_edge=Decimal("0.06"),
        live_market_ids="",
    )
    base.update(overrides)
    settings = Settings(**base)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    return settings, Repository(connection), connection


def _fresh_book(bid: str = "0.12"):
    return (
        MarketSnapshot(
            market_id="token_book",
            best_bid=Decimal(bid),
            best_ask=Decimal("0.13"),
            midpoint=Decimal("0.125"),
            spread=Decimal("0.01"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {},
    )


def _save_value_exit_confirmations(repository: Repository, market_id: str) -> None:
    now = datetime.now(timezone.utc)
    for index, revision in enumerate(("full-live-r1", "full-live-r2")):
        analysis_id = repository.save_analysis(
            Analysis(
                market_id=market_id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.00"),
                fair_upper=Decimal("0.05"),
                reference_price=Decimal("0.12"),
                edge=Decimal("-0.07"),
                side=None,
                decision="watch",
                reasons=["settlement-core value test"],
                created_at=now + timedelta(microseconds=index),
            )
        )
        repository.connection.execute(
            """
            UPDATE model_signals
            SET raw_payload = json_set(raw_payload, '$.forecast_revision', ?)
            WHERE analysis_id = ?
            """,
            (revision, analysis_id),
        )


def test_profile_name_for_app_mode_maps_full_and_micro():
    assert profile_name_for_app_mode("full_live") == "full-live"
    assert profile_name_for_app_mode("micro_live") == "micro-live"


def test_app_renders_full_live_selectable_zh_and_en(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    connection.close()
    zh = render_dashboard_path(settings, "/app?lang=zh")
    en = render_dashboard_path(settings, "/app?lang=en")
    assert zh.status.value == 200
    assert en.status.value == 200
    assert 'data-mode="full_live"' in zh.body
    assert "正式实盘" in zh.body
    assert "配置上限 · 自动进出" in zh.body or "自动管理" in zh.body
    assert "未开放" not in zh.body or "配置上限" in zh.body
    assert "当前锁定" not in zh.body
    assert "Locked in this slice" not in en.body
    assert "Unavailable" not in en.body or "Configured caps" in en.body
    assert 'name="app_mode" value="full_live"' in zh.body
    assert "选择" in zh.body
    assert "Choose" in en.body


def test_selecting_full_live_persists_mode_and_stops_loop(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.ensure_autopilot_state()
    repository.update_autopilot_state(enabled=True, app_mode="paper", mode="dry_run")
    connection.commit()
    connection.close()

    class FakeAutopilot:
        def __init__(self, repository):
            self.repository = repository

        def set_app_mode(self, app_mode):
            mode = "live" if app_mode in {"micro_live", "full_live"} else "dry_run"
            self.repository.update_autopilot_state(
                enabled=False, mode=mode, app_mode=app_mode
            )

    response = handle_dashboard_post(
        settings,
        "/app/mode?lang=zh",
        b"app_mode=full_live&lang=zh",
        None,
        autopilot_service_factory=lambda repo: FakeAutopilot(repo),
    )
    assert response.status.value in {302, 303}

    database = Database(settings.database_path)
    conn = database.connect()
    try:
        state = Repository(conn).get_autopilot_state()
        assert state["app_mode"] == "full_live"
        assert state["mode"] == "live"
        assert bool(state["enabled"]) is False
    finally:
        conn.close()


def test_start_keeps_full_live_app_mode(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.ensure_autopilot_state()
    repository.update_autopilot_state(
        enabled=False, mode="live", app_mode="full_live"
    )
    connection.commit()
    connection.close()

    class FakeAutopilot:
        def __init__(self, repository):
            self.repository = repository

        def set_enabled(self, enabled):
            self.repository.update_autopilot_state(enabled=enabled)

        def tick(self):
            raise AssertionError("Start must not run a tick immediately")

    response = handle_dashboard_post(
        settings,
        "/app/toggle?lang=zh",
        b"enabled=1&lang=zh",
        None,
        autopilot_service_factory=lambda repo: FakeAutopilot(repo),
    )
    assert response.status.value in {302, 303}

    database = Database(settings.database_path)
    conn = database.connect()
    try:
        state = Repository(conn).get_autopilot_state()
        assert bool(state["enabled"]) is True
        assert state["app_mode"] == "full_live"
        assert state["mode"] == "live"
    finally:
        conn.close()


def test_full_live_mode_clears_legacy_star_overrides(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.upsert_strategy_override(
        market_id="*", profile="micro-live", live_auto_enabled=True
    )
    repository.upsert_strategy_override(
        market_id="*", profile="full-live", live_auto_enabled=True
    )
    clear_legacy_global_live_overrides(repository)
    assert repository.get_strategy_override("*", "micro-live") is None
    assert repository.get_strategy_override("*", "full-live") is None
    connection.close()


def test_nonempty_live_market_ids_narrows_execute(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
    )
    market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
    settings = settings.model_copy(update={"live_market_ids": "other-only"})
    service = AutopilotService(settings, repository, client=FakeClient())
    repository.update_autopilot_state(mode="live", app_mode="full_live")
    intent_id, reasons = service._execute_live(market_id, repository.latest_analysis(market_id))
    assert intent_id is None
    assert reasons == ["market is not whitelisted in LIVE_MARKET_IDS"]
    connection.close()


def test_full_live_uses_configured_caps_micro_keeps_tight(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    full = settings_for_profile(settings, get_profile("full-live"))
    micro = settings_for_profile(settings, get_profile("micro-live"))
    assert full.max_order_usdc == Decimal("20")
    assert full.min_edge == Decimal("0.06")
    assert micro.max_order_usdc == Decimal("5")
    assert micro.min_edge == Decimal("0.10")
    connection.close()


def test_full_live_execute_calls_trading_service_once(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
    )
    market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
    repository.save_reconciliation("ok", {"status": "ok"})
    repository.update_autopilot_state(mode="live", app_mode="full_live")
    connection.commit()

    calls: list[tuple] = []

    def fake_trade(self, **kwargs):
        calls.append(kwargs)
        return 42, ["live order submitted"]

    monkeypatch.setattr(
        "polymarket_weather_arb.services.trading_service.TradingService.trade",
        fake_trade,
    )
    service = AutopilotService(settings, repository, client=FakeClient())
    analysis = repository.latest_analysis(market_id)
    intent_id, reasons = service._execute_live(market_id, analysis)
    assert intent_id == 42
    assert "live order submitted" in reasons
    assert len(calls) == 1
    assert calls[0]["dry_run"] is False
    connection.close()


def test_does_not_reenter_market_with_position(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    market = Market(
        id="owned",
        title="Will the highest temperature in Atlanta be 92-93°F on July 11, 2026?",
        description="Resolved according to Wunderground.",
        yes_token_id="yes-token",
        no_token_id="no-token",
        is_weather=True,
    )
    repository.upsert_market(
        type("ModuleMarket", (), {**market.__dict__, "module_id": "global_temp_bucket"})(),
        {"id": market.id},
    )
    repository.upsert_candidate(
        market.id,
        type("Rule", (), {"tradable": True, "rejection_reason": None})(),
        status="dry_run_ready",
        module_id="global_temp_bucket",
    )
    repository.save_analysis(
        Analysis(
            market_id=market.id,
            model_version="t",
            fair_lower=Decimal("0.40"),
            fair_upper=Decimal("0.50"),
            reference_price=Decimal("0.10"),
            edge=Decimal("0.28"),
            side="buy_yes",
            decision="trade",
            reasons=["owned"],
        )
    )
    repository.replace_positions(
        [
            {
                "market": market.id,
                "token_id": "yes-token",
                "outcome": "Yes",
                "size": "5",
                "current_value": "0.5",
            }
        ]
    )
    service = AutopilotService(settings, repository, client=FakeClient())
    assert service._select_market() is None
    connection.close()


def test_stale_orders_managed_via_order_lifecycle(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    repository.update_autopilot_state(mode="live", app_mode="full_live")
    service = AutopilotService(settings, repository, client=FakeClient())
    called: list[int] = []

    def fake_cancel(self, stale_threshold_seconds=300):
        from polymarket_weather_arb.services.order_lifecycle_service import CancelStaleResult

        called.append(stale_threshold_seconds)
        return CancelStaleResult(cancelled=[{"order_id": "stale-1"}], failures=[])

    monkeypatch.setattr(OrderLifecycleService, "cancel_stale_orders", fake_cancel)
    notes = service._maybe_manage_stale_orders(app_mode="full_live")
    assert called
    assert any("cancelled_stale_orders=1" in n for n in notes)
    connection.close()


def test_full_live_repeated_value_signal_does_not_reach_position_exit(tmp_path):
    settings, repository, connection = _repo(
        tmp_path, auto_exit_max_position_usdc=Decimal("5")
    )
    market_id = "m-exit"
    repository.upsert_market(
        Market(
            id=market_id,
            title="Auto exit market",
            is_weather=True,
            slug=market_id,
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
        ),
        {
            "id": market_id,
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token", "no-token"],
        },
    )
    repository.save_reconciliation("ok", {"status": "ok"})
    repository.replace_positions(
        [{"market": market_id, "outcome": "Yes", "size": "5", "avgPrice": "0.13"}]
    )
    _save_value_exit_confirmations(repository, market_id)
    client = Mock()
    client.place_limit_order.side_effect = RuntimeError("BUY path forbidden")
    client.place_sell_limit_order.return_value = {"order_id": "auto-sell-1", "status": "live"}
    client.get_token_order_book.side_effect = lambda token_id: _fresh_book()
    client.get_order.return_value = {"id": "auto-sell-1", "status": "LIVE"}
    client.get_balances.return_value = {"balance": 1}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []

    compliance = Mock(spec=ComplianceService)
    compliance.check_live_allowed.return_value = ComplianceDecision(
        ok=True, status="check_disabled", reason="test"
    )
    result = AutoExitService(repository, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=compliance,
    )
    assert result.executed == 0
    assert result.attempted == 0
    client.place_sell_limit_order.assert_not_called()
    connection.close()


def test_full_live_large_position_still_rejects_model_only_exit(tmp_path):
    """Position size cannot turn repeated model evidence into a SELL signal."""
    settings, repository, connection = _repo(
        tmp_path, auto_exit_max_position_usdc=Decimal("0.10")
    )
    market_id = "m-big"
    repository.upsert_market(
        Market(
            id=market_id,
            title="Big position",
            is_weather=True,
            slug=market_id,
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
        ),
        {
            "id": market_id,
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token", "no-token"],
        },
    )
    repository.save_reconciliation("ok", {"status": "ok"})
    repository.replace_positions(
        [{"market": market_id, "outcome": "Yes", "size": "5", "avgPrice": "0.13"}]
    )
    _save_value_exit_confirmations(repository, market_id)
    client = Mock()
    client.place_limit_order.side_effect = RuntimeError("BUY path forbidden")
    client.place_sell_limit_order.return_value = {"order_id": "auto-sell-big", "status": "live"}
    client.get_token_order_book.side_effect = lambda token_id: _fresh_book()
    client.get_order.return_value = {"id": "auto-sell-big", "status": "LIVE"}
    client.get_balances.return_value = {"balance": 1}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []
    compliance = Mock(spec=ComplianceService)
    compliance.check_live_allowed.return_value = ComplianceDecision(
        ok=True, status="check_disabled", reason="test"
    )

    micro = AutoExitService(repository, client).run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=compliance,
    )
    assert micro.executed == 0
    client.place_sell_limit_order.assert_not_called()

    full = AutoExitService(repository, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=compliance,
    )
    assert full.executed == 0
    assert full.attempted == 0
    client.place_sell_limit_order.assert_not_called()
    assert not full.intent_ids
    connection.close()


def test_one_tick_at_most_one_buy(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    repository.save_reconciliation("ok", {"status": "ok"})
    service = AutopilotService(settings, repository, client=FakeClient())
    service.ensure_state(mode="live")
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    connection.commit()

    buy_calls = []

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
    monkeypatch.setattr(service, "_maybe_manage_stale_orders", lambda **_k: [])
    monkeypatch.setattr(service, "_refresh_position_analyses", lambda: None)
    monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))
    monkeypatch.setattr(service, "_select_market", lambda: "m1")
    monkeypatch.setattr(service, "_prepare_market", lambda _m: None)

    analysis = Analysis(
        market_id="m1",
        model_version="t",
        fair_lower=Decimal("0.4"),
        fair_upper=Decimal("0.5"),
        reference_price=Decimal("0.1"),
        edge=Decimal("0.3"),
        side="buy_yes",
        decision="trade",
        reasons=["edge"],
    )
    repository.upsert_market(
        Market(
            id="m1",
            title="Will the highest temperature in NYC be above 80 on May 8?",
            description="NOAA",
            yes_token_id="y",
            no_token_id="n",
            is_weather=True,
        ),
        {"id": "m1"},
    )
    repository.save_analysis(analysis)

    def fake_execute(market_id, analysis_row):
        buy_calls.append(market_id)
        return 7, ["live order submitted"]

    monkeypatch.setattr(service, "_execute_live", fake_execute)
    monkeypatch.setattr(service, "_notify_buy_submitted", lambda *a, **k: None)

    result = service.tick()
    assert len(buy_calls) == 1
    assert result.intent_id == 7
    connection.close()


def test_idle_tick_sends_no_telegram(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    events: list[dict] = []

    def notifier(payload):
        events.append(payload)

    service = AutopilotService(
        settings, repository, client=FakeClient(), notifier=notifier
    )
    service.ensure_state(mode="live")
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    repository.save_reconciliation("ok", {"status": "ok"})
    connection.commit()

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
    monkeypatch.setattr(service, "_maybe_manage_stale_orders", lambda **_k: [])
    monkeypatch.setattr(service, "_refresh_position_analyses", lambda: None)
    monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))
    monkeypatch.setattr(service, "_select_market", lambda: None)

    result = service.tick()
    assert result.status == "idle"
    assert events == []
    connection.close()


def test_background_cycle_failure_does_not_kill_next(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    service = AutopilotService(settings, repository, client=FakeClient())
    service.ensure_state(mode="dry_run")
    repository.update_autopilot_state(enabled=True, mode="dry_run")
    connection.commit()

    calls = {"n": 0}

    def flaky_select():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return None

    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
        lambda self, **kwargs: 0,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
        lambda self, **kwargs: 0,
    )
    monkeypatch.setattr(service, "_select_market", flaky_select)

    first = service.tick()
    assert first.status == "failed"
    second = service.tick()
    assert second.status == "idle"
    connection.close()


def test_stale_last_tick_visible_on_app(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.ensure_autopilot_state()
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    repository.update_autopilot_state(
        enabled=True,
        mode="live",
        app_mode="full_live",
        tick_seconds=300,
        last_tick_at=old,
        last_tick_status="idle",
    )
    connection.commit()
    connection.close()

    response = render_dashboard_path(settings, "/app?lang=en")
    assert response.status.value == 200
    assert 'data-stale-tick="1"' in response.body
    assert "Stale last tick" in response.body


def test_auto_exit_accepts_full_live_and_micro_live_profiles(tmp_path):
    settings, repository, connection = _repo(tmp_path, AUTO_EXIT_ENABLED=True)
    for name in ("micro-live", "full-live"):
        ok, blockers = AutoExitService._gates_open(
            settings=settings,
            profile_name=name,
            allow_auto_exit=True,
        )
        assert ok is True, blockers
    connection.close()
