from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import (
    _run_autopilot_background,
    handle_dashboard_post,
    render_dashboard_path,
)
from polymarket_weather_arb.domain.china_temperature_bucket import (
    parse_china_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.markets import MarketSnapshot
from polymarket_weather_arb.services.automation_service import AutomationService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, cwd, timeout, capture_output, text):
        self.calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="dashboard dry-run ok", stderr="")


class RecordingWorkflow:
    def __init__(self):
        self.calls = []

    def research_market(self, market_id):
        self.calls.append(("research_market", market_id))
        return SimpleNamespace(market_id=market_id, summary="researched", details=[])

    def dry_run_trade(self, market_id):
        self.calls.append(("dry_run_trade", market_id))
        return SimpleNamespace(market_id=market_id, summary="dashboard dry-run ok", details=[])


class RecordingWorkflowFactory:
    def __init__(self):
        self.workflows = []

    def __call__(self, repo):
        workflow = RecordingWorkflow()
        self.workflows.append(workflow)
        return workflow


def test_dashboard_uses_split_i18n_and_html_helpers():
    from polymarket_weather_arb import dashboard
    from polymarket_weather_arb.dashboard_ui import html, i18n

    assert dashboard._t is i18n._t
    assert dashboard.render_page is html.render_page
    assert dashboard._table is html._table


def test_autopilot_background_recovers_after_database_connect_failure(tmp_path):
    real_database = Database(tmp_path / "dashboard.db")
    real_database.init_schema()

    class TrackingConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.closed = True
            self.connection.close()

    class FlakyDatabase:
        def __init__(self):
            self.calls = 0
            self.successful_connection = None

        def connect(self):
            self.calls += 1
            if self.calls == 1:
                from sqlite3 import OperationalError

                raise OperationalError("unable to open database file")
            self.successful_connection = TrackingConnection(real_database.connect())
            return self.successful_connection

    database = FlakyDatabase()
    settings = Settings(_env_file=None, database_path=real_database.path)

    _run_autopilot_background(
        database,  # type: ignore[arg-type]
        settings,
        None,
        tick_seconds=0,
        sleep=lambda _: None,
        max_cycles=2,
        use_pulse=False,
    )

    assert database.calls == 2
    assert database.successful_connection.closed is True


def test_autopilot_background_uses_fixed_start_to_start_cadence(tmp_path, monkeypatch):
    from polymarket_weather_arb.services.autopilot_service import AutopilotService

    database = Database(tmp_path / "dashboard-cadence.db")
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    repository.ensure_autopilot_state(mode="dry_run", tick_seconds=300)
    repository.update_autopilot_state(enabled=True, mode="dry_run")
    connection.commit()
    connection.close()

    clock = [1000.0]
    sleeps = []

    def tick(_self):
        clock[0] += 100.0

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

    assert sleeps == [200.0]
    assert clock[0] == 1400.0


def _stub_autopilot_tick_no_network(monkeypatch) -> None:
    """Keep background lifecycle tests offline (no discovery/recon HTTP)."""
    from polymarket_weather_arb.services import autopilot_service as ap_mod

    def offline_tick(self):
        from polymarket_weather_arb.services.autopilot_service import AutopilotTickResult

        result = AutopilotTickResult(
            status="idle",
            action="skip",
            market_id=None,
            edge=None,
            reason="offline-test",
            blockers=[],
            discovered=0,
            is_useful=True,
        )
        self._record_tick(result, discovered=0)
        return result

    monkeypatch.setattr(ap_mod.AutopilotService, "tick", offline_tick)


def test_autopilot_background_reuses_and_closes_shared_client(tmp_path, monkeypatch):
    """Two enabled cycles share one GammaPolymarketClient; closed once at exit."""
    from polymarket_weather_arb import dashboard as dashboard_mod
    from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient

    _stub_autopilot_tick_no_network(monkeypatch)

    database = Database(tmp_path / "shared-client.db")
    database.init_schema()
    connection = database.connect()
    Repository(connection).update_autopilot_state(enabled=True, mode="dry_run", app_mode="paper")
    connection.commit()
    connection.close()

    constructed: list[GammaPolymarketClient] = []
    close_calls: list[object] = []

    class TrackingGamma(GammaPolymarketClient):
        def __init__(self, settings):
            super().__init__(settings)
            constructed.append(self)

        def close(self):
            close_calls.append(self)
            super().close()

    monkeypatch.setattr(dashboard_mod, "GammaPolymarketClient", TrackingGamma)

    settings = Settings(
        _env_file=None,
        database_path=database.path,
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xfunder",
    )

    _run_autopilot_background(
        database,
        settings,
        None,
        tick_seconds=0,
        sleep=lambda _: None,
        max_cycles=2,
        use_pulse=False,
    )

    # Paper/dry_run still constructs one shared adapter for enabled cycles.
    assert len(constructed) == 1
    assert constructed[0].secure_client_create_count == 0  # offline tick: no auth session
    assert close_calls == constructed


def test_autopilot_background_shares_rest_credentials_with_user_stream(tmp_path, monkeypatch):
    from polymarket_weather_arb import dashboard as dashboard_mod
    from polymarket_weather_arb.adapters.polymarket import stream as stream_mod
    from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
    from polymarket_weather_arb.services import autopilot_service as autopilot_mod

    database = Database(tmp_path / "shared-stream-credentials.db")
    database.init_schema()
    connection = database.connect()
    Repository(connection).update_autopilot_state(enabled=True, mode="live")
    connection.commit()
    connection.close()

    credentials = object()
    bridges = []

    class TrackingGamma(GammaPolymarketClient):
        def stream_api_credentials(self):
            return credentials

    class FakeBridge:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            bridges.append(self)

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(dashboard_mod, "GammaPolymarketClient", TrackingGamma)
    monkeypatch.setattr(stream_mod, "PolymarketStreamBridge", FakeBridge)
    monkeypatch.setattr(
        autopilot_mod.AutopilotService,
        "pulse",
        lambda self, pulse_state, **_kwargs: None,
    )

    settings = Settings(
        _env_file=None,
        database_path=database.path,
        POLYMARKET_PRIVATE_KEY="private-key",
        POLYMARKET_FUNDER="0xfunder",
        POLYMARKET_MARKET_STREAM_ENABLED=True,
    )
    _run_autopilot_background(
        database,
        settings,
        None,
        tick_seconds=300,
        sleep=lambda _seconds: None,
        max_cycles=1,
        use_pulse=True,
    )

    assert len(bridges) == 1
    assert bridges[0].kwargs["api_credentials"] is credentials


def test_autopilot_background_keeps_exchange_stream_off_by_default(tmp_path, monkeypatch):
    from polymarket_weather_arb.adapters.polymarket import stream as stream_mod
    from polymarket_weather_arb.services import autopilot_service as autopilot_mod

    database = Database(tmp_path / "stream-disabled-by-default.db")
    database.init_schema()
    connection = database.connect()
    Repository(connection).update_autopilot_state(enabled=True, mode="live")
    connection.commit()
    connection.close()

    bridge_created = False
    received_streams = []

    class UnexpectedBridge:
        def __init__(self, **_kwargs):
            nonlocal bridge_created
            bridge_created = True

    def capture_pulse(self, _pulse_state, *, stream_bridge=None, **_kwargs):
        received_streams.append(stream_bridge)
        return None

    monkeypatch.setattr(stream_mod, "PolymarketStreamBridge", UnexpectedBridge)
    monkeypatch.setattr(autopilot_mod.AutopilotService, "pulse", capture_pulse)

    settings = Settings(
        _env_file=None,
        database_path=database.path,
        POLYMARKET_PRIVATE_KEY="private-key",
        POLYMARKET_FUNDER="0xfunder",
    )
    assert settings.polymarket_market_stream_enabled is False

    _run_autopilot_background(
        database,
        settings,
        None,
        tick_seconds=300,
        sleep=lambda _seconds: None,
        max_cycles=1,
        use_pulse=True,
    )

    assert bridge_created is False
    assert received_streams == [None]


def test_autopilot_background_settings_replace_closes_old_client(tmp_path, monkeypatch):
    from polymarket_weather_arb import dashboard as dashboard_mod
    from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient

    _stub_autopilot_tick_no_network(monkeypatch)

    database = Database(tmp_path / "settings-replace.db")
    database.init_schema()
    connection = database.connect()
    Repository(connection).update_autopilot_state(enabled=True, mode="dry_run", app_mode="paper")
    connection.commit()
    connection.close()

    constructed: list[GammaPolymarketClient] = []
    closed: list[GammaPolymarketClient] = []

    class TrackingGamma(GammaPolymarketClient):
        def __init__(self, settings):
            super().__init__(settings)
            constructed.append(self)

        def close(self):
            closed.append(self)
            super().close()

    monkeypatch.setattr(dashboard_mod, "GammaPolymarketClient", TrackingGamma)

    settings_a = Settings(_env_file=None, database_path=database.path, MAX_ORDER_USDC=1)
    settings_b = Settings(_env_file=None, database_path=database.path, MAX_ORDER_USDC=1)
    # Distinct objects so identity-based replace detection fires.
    assert settings_a is not settings_b

    cycle = {"n": 0}

    def active(_fallback):
        cycle["n"] += 1
        return settings_a if cycle["n"] <= 1 else settings_b

    monkeypatch.setattr(dashboard_mod, "active_dashboard_settings", active)

    _run_autopilot_background(
        database,
        settings_a,
        None,
        tick_seconds=0,
        sleep=lambda _: None,
        max_cycles=2,
        use_pulse=False,
    )

    assert len(constructed) == 2
    assert constructed[0] in closed  # old client closed on settings replace
    assert constructed[1] in closed  # final close at runner exit


def test_autopilot_background_disabled_creates_no_client(tmp_path, monkeypatch):
    from polymarket_weather_arb import dashboard as dashboard_mod
    from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient

    database = Database(tmp_path / "disabled.db")
    database.init_schema()
    connection = database.connect()
    Repository(connection).update_autopilot_state(enabled=False)
    connection.commit()
    connection.close()

    constructed: list[object] = []

    class TrackingGamma(GammaPolymarketClient):
        def __init__(self, settings):
            constructed.append(1)
            super().__init__(settings)

    monkeypatch.setattr(dashboard_mod, "GammaPolymarketClient", TrackingGamma)
    settings = Settings(_env_file=None, database_path=database.path)
    _run_autopilot_background(
        database,
        settings,
        None,
        tick_seconds=0,
        sleep=lambda _: None,
        max_cycles=2,
        use_pulse=False,
    )
    assert constructed == []


def test_autopilot_background_failed_cycle_does_not_leak_shared_client(tmp_path, monkeypatch):
    from polymarket_weather_arb import dashboard as dashboard_mod
    from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
    from polymarket_weather_arb.services import autopilot_service as ap_mod

    database = Database(tmp_path / "fail-cycle.db")
    database.init_schema()
    connection = database.connect()
    Repository(connection).update_autopilot_state(enabled=True, mode="dry_run")
    connection.commit()
    connection.close()

    closed: list[object] = []
    constructed: list[GammaPolymarketClient] = []

    class TrackingGamma(GammaPolymarketClient):
        def __init__(self, settings):
            super().__init__(settings)
            constructed.append(self)

        def close(self):
            closed.append(self)
            super().close()

    monkeypatch.setattr(dashboard_mod, "GammaPolymarketClient", TrackingGamma)

    def fail_tick(self):
        raise RuntimeError("tick boom")

    monkeypatch.setattr(ap_mod.AutopilotService, "tick", fail_tick)

    settings = Settings(_env_file=None, database_path=database.path)
    _run_autopilot_background(
        database,
        settings,
        None,
        tick_seconds=0,
        sleep=lambda _: None,
        max_cycles=2,
        use_pulse=False,
    )
    # Shared client survives failed cycles and is closed once at exit.
    assert len(constructed) == 1
    assert closed == constructed


def test_dashboard_uses_split_overview_renderer():
    from polymarket_weather_arb import dashboard
    from polymarket_weather_arb.dashboard_ui import overview

    assert dashboard.render_overview is overview.render_overview
    assert dashboard._cockpit_step_label is overview._cockpit_step_label
    assert dashboard._render_market_readiness is overview._render_market_readiness


def test_dashboard_uses_split_exchange_renderers():
    from polymarket_weather_arb import dashboard
    from polymarket_weather_arb.dashboard_ui import exchange

    assert dashboard.render_open_orders is exchange.render_open_orders
    assert dashboard.render_positions is exchange.render_positions
    assert dashboard.render_fills is exchange.render_fills
    assert dashboard.render_risk is exchange.render_risk
    assert dashboard.render_reconciliation is exchange.render_reconciliation


def test_dashboard_uses_split_market_renderers():
    from polymarket_weather_arb import dashboard
    from polymarket_weather_arb.dashboard_ui import markets

    assert dashboard.render_markets is markets.render_markets
    assert dashboard.render_candidates is markets.render_candidates
    assert dashboard.render_market_detail is markets.render_market_detail
    assert dashboard._orders_table is markets._orders_table


def test_dashboard_uses_split_automation_renderers():
    from polymarket_weather_arb import dashboard
    from polymarket_weather_arb.dashboard_ui import automation

    assert dashboard.render_actions is automation.render_actions
    assert dashboard.render_action_detail is automation.render_action_detail
    assert dashboard.render_runs is automation.render_runs
    assert dashboard.render_operator is automation.render_operator
    assert dashboard.render_overrides is automation.render_overrides
    assert dashboard._action_controls is automation._action_controls


def test_dashboard_uses_split_admin_renderers():
    from polymarket_weather_arb import dashboard
    from polymarket_weather_arb.dashboard_ui import admin

    assert dashboard.render_discovery is admin.render_discovery
    assert dashboard.render_orders is admin.render_orders
    assert dashboard.render_profiles is admin.render_profiles
    assert dashboard.render_doctor is admin.render_doctor
    assert dashboard.render_fixtures is admin.render_fixtures
    assert dashboard.render_modules is admin.render_modules
    assert dashboard.render_setup is admin.render_setup
    assert dashboard._resolve_fixture_path is admin._resolve_fixture_path


def test_dashboard_uses_split_calibration_renderer():
    from polymarket_weather_arb import dashboard
    from polymarket_weather_arb.dashboard_ui import calibration

    assert dashboard.render_calibration is calibration.render_calibration


def test_dashboard_renders_bilingual_overview_and_actions(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_market(settings)

    zh_overview = render_dashboard_path(settings, "/overview-legacy?lang=zh")
    en_overview = render_dashboard_path(settings, "/overview-legacy?lang=en")
    zh_actions = render_dashboard_path(settings, "/actions?lang=zh")

    assert zh_overview.status.value == 200
    assert '<html lang="zh-CN">' in zh_overview.body
    assert "Polymarket 天气交易控制台" in zh_overview.body
    assert "操作台" in zh_overview.body
    assert "下一步" in zh_overview.body
    assert "候选漏斗" in zh_overview.body
    assert "阻塞" in zh_overview.body
    assert "/actions?lang=zh" in zh_overview.body
    assert "busy-toast" in zh_overview.body
    assert "执行中，请保持页面打开。" in zh_overview.body
    assert "trade_live" not in zh_overview.body
    assert "Live Auto" not in zh_overview.body
    assert en_overview.status.value == 200
    assert '<html lang="en">' in en_overview.body
    assert "Polymarket Weather Dashboard" in en_overview.body
    assert zh_actions.status.value == 200
    assert "提案下一项" in zh_actions.body


def test_dashboard_renders_action_detail_controls_and_timeline(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    action = _seed_action(settings, kind="dry-run")

    response = render_dashboard_path(settings, f"/actions/{action['id']}?lang=zh")

    assert response.status.value == 200
    assert action["id"] in response.body
    assert "时间线" in response.body
    assert "批准" in response.body
    assert "拒绝" in response.body
    assert "created" in response.body


def test_dashboard_renders_exchange_state_and_overrides(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        _upsert_market(repo)
        repo.replace_open_orders(
            [
                {
                    "id": "order-1",
                    "market": "m1",
                    "side": "BUY",
                    "price": "0.25",
                    "size": "10",
                    "status": "live",
                }
            ]
        )
        repo.save_reconciled_fills(
            [
                {
                    "id": "fill-1",
                    "market": "m1",
                    "side": "BUY",
                    "price": "0.20",
                    "size": "5",
                    "timestamp": "2026-05-06",
                }
            ]
        )
        repo.replace_positions(
            [{"market": "m1", "outcome": "Yes", "size": "5", "notional": "1.25"}]
        )
        repo.upsert_strategy_override(
            market_id="m1", profile="micro-live", min_edge="0.12", live_auto_enabled=True
        )
        connection.commit()
    finally:
        connection.close()

    open_orders = render_dashboard_path(settings, "/open-orders?lang=zh")
    positions = render_dashboard_path(settings, "/positions?lang=zh")
    fills = render_dashboard_path(settings, "/fills?lang=zh")
    overrides = render_dashboard_path(settings, "/overrides?lang=zh")

    assert open_orders.status.value == 200
    assert "order-1" in open_orders.body
    assert positions.status.value == 200
    assert "Yes" in positions.body
    assert fills.status.value == 200
    assert "fill-1" in fills.body
    assert overrides.status.value == 200
    assert "策略覆盖" in overrides.body
    assert "micro-live" in overrides.body
    assert "0.12" in overrides.body
    assert "删除" in overrides.body


def test_beginner_page_does_not_call_compliance_network(tmp_path, monkeypatch):
    from polymarket_weather_arb.services.compliance_service import ComplianceService

    settings = Settings(
        DATABASE_PATH=tmp_path / "dashboard.db",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=True,
    )
    Database(settings.database_path).init_schema()

    def fail_if_called(self):
        raise AssertionError("beginner render must not call geoblock network check")

    monkeypatch.setattr(ComplianceService, "check_live_allowed", fail_if_called)

    response = render_dashboard_path(settings, "/beginner-legacy?lang=en")

    assert response.status.value == 200
    assert "Run live-readiness to perform the current geoblock check" in response.body


def test_open_orders_refresh_error_redirects_back_to_open_orders(tmp_path):
    settings = Settings(
        DATABASE_PATH=tmp_path / "dashboard.db",
        POLYMARKET_PRIVATE_KEY=None,
        POLYMARKET_FUNDER=None,
    )
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(settings, "/open-orders/refresh?lang=en", b"lang=en")

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/open-orders?lang=en")
    assert "flash=flash.error" in response.headers["Location"]
    assert "POLYMARKET_PRIVATE_KEY" in response.headers["Location"]


def test_dashboard_post_approves_rejects_and_runs_non_live_actions(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    action = _seed_action(settings, kind="dry-run")

    approved = handle_dashboard_post(
        settings, f"/actions/{action['id']}/approve?lang=zh", b"lang=zh", None
    )

    assert approved.status.value == 303
    assert approved.headers["Location"].startswith(f"/actions/{action['id']}?lang=zh")
    assert _action_status(settings, action["id"]) == "approved"

    runner = RecordingRunner()
    workflow_factory = RecordingWorkflowFactory()
    run = handle_dashboard_post(
        settings,
        f"/actions/{action['id']}/run?lang=zh",
        b"lang=zh",
        None,
        automation_service_factory=lambda repo: AutomationService(
            repo, runner=runner, workflow_factory=workflow_factory
        ),
    )

    assert run.status.value == 303
    assert runner.calls == []
    assert workflow_factory.workflows[0].calls == [
        ("research_market", "m1"),
        ("dry_run_trade", "m1"),
    ]
    assert _action_status(settings, action["id"]) == "executed"

    rejected_action = _seed_action(settings, kind="analyze")
    rejected = handle_dashboard_post(
        settings,
        f"/actions/{rejected_action['id']}/reject?lang=zh",
        urlencode({"lang": "zh", "reason": "not now"}).encode(),
        None,
    )

    assert rejected.status.value == 303
    assert _action_status(settings, rejected_action["id"]) == "rejected"


def test_dashboard_blocks_live_approval_and_execution(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    live_action = _seed_action(settings, kind="trade-live")

    approve = handle_dashboard_post(
        settings, f"/actions/{live_action['id']}/approve?lang=zh", b"lang=zh", None
    )

    assert approve.status.value == 303
    assert "level=error" in approve.headers["Location"]
    assert _action_status(settings, live_action["id"]) == "pending"

    _approve_directly(settings, live_action["id"])
    run = handle_dashboard_post(
        settings, f"/actions/{live_action['id']}/run?lang=zh", b"lang=zh", None
    )

    assert run.status.value == 303
    assert "level=error" in run.headers["Location"]
    assert _action_status(settings, live_action["id"]) == "approved"
    detail = render_dashboard_path(settings, f"/actions/{live_action['id']}?lang=zh")
    assert "浏览器禁止直接执行 live action" in detail.body


def test_dashboard_post_manages_strategy_overrides(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_market(settings)

    saved = handle_dashboard_post(
        settings,
        "/overrides/set?lang=zh",
        urlencode(
            {
                "lang": "zh",
                "market": "m1",
                "profile": "micro-live",
                "min_edge": "0.12",
                "max_order_usdc": "3",
                "live_auto": "true",
                "notes": "tiny live test",
            }
        ).encode(),
        None,
    )
    page = render_dashboard_path(settings, "/overrides?lang=zh")

    assert saved.status.value == 303
    assert "flash.override_saved" in saved.headers["Location"]
    assert "tiny live test" in page.body
    assert "yes / 是" in page.body

    deleted = handle_dashboard_post(
        settings,
        "/overrides/delete?lang=zh",
        urlencode({"lang": "zh", "market": "m1", "profile": "micro-live"}).encode(),
        None,
    )

    assert deleted.status.value == 303
    assert "m1</td><td>micro-live" not in render_dashboard_path(settings, "/overrides?lang=zh").body


def test_dashboard_propose_next_rejects_live_kind(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_market(settings)

    response = handle_dashboard_post(
        settings,
        "/actions/propose-next?lang=zh",
        urlencode({"lang": "zh", "kind": "trade_live"}).encode(),
        None,
    )

    assert response.status.value == 303
    assert "level=error" in response.headers["Location"]
    assert _actions_count(settings) == 0


def test_dashboard_wraps_long_action_errors(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    action = _seed_action(settings, kind="dry-run")
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        repo.approve_automation_action(action["id"], "test", "2026-05-08T00:00:00+00:00")
        repo.claim_approved_automation_action(action["id"], "2026-05-08T00:00:00+00:00")
        repo.mark_automation_action_executing(action["id"], ["x"], "2026-05-08T00:00:00+00:00")
        repo.mark_automation_action_failed(
            action["id"],
            1,
            "Traceback " + ("/very/long/path/without/breaks" * 20),
            "2026-05-08T00:00:01+00:00",
        )
        connection.commit()
    finally:
        connection.close()

    actions = render_dashboard_path(settings, "/actions?lang=zh")
    detail = render_dashboard_path(settings, f"/actions/{action['id']}?lang=zh")

    assert actions.status.value == 200
    assert "note-cell" in actions.body
    assert "Traceback" in actions.body
    assert detail.status.value == 200
    assert "note-cell" in detail.body


def test_dashboard_cockpit_shows_pipeline_and_blockers(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_china_cockpit_candidate(settings)

    page = render_dashboard_path(settings, "/overview-legacy?lang=zh")

    assert page.status.value == 200
    assert "刷新缺失信号" in page.body
    assert "shanghai-18c" in page.body
    assert "发现 Found" in page.body
    assert "信号 Signal" in page.body
    assert "missing signal or forecast" in page.body
    assert "trade_live" not in page.body


def test_dashboard_operator_shows_read_only_live_monitor(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    action = _seed_action(settings, kind="trade-live")

    page = render_dashboard_path(settings, "/operator?lang=zh")

    assert page.status.value == 200
    assert "Live 监控" in page.body
    assert "allow_live_auto=false" in page.body
    assert "pending_live_actions=1" in page.body
    assert action["id"] in page.body
    assert "Live Action Gate 明细" in page.body
    assert "失败 Gates" in page.body
    assert "live_auto" in page.body
    assert "whitelist" in page.body
    assert "浏览器禁止直接执行 live action" not in page.body


def _seed_market(settings: Settings) -> None:
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        _upsert_market(repo)
        connection.commit()
    finally:
        connection.close()


def _seed_action(settings: Settings, *, kind: str):
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        _upsert_market(repo)
        action = AutomationService(repo).propose(kind=kind, market_id="m1")
        connection.commit()
        return action
    finally:
        connection.close()


def _upsert_market(repo: Repository) -> None:
    repo.upsert_market(
        SimpleNamespace(
            id="m1",
            slug="m1",
            title="Dashboard market",
            description="NOAA station KNYC",
            event_slug=None,
            event_title=None,
            category=None,
            tags=(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            close_time=None,
            status="active",
            is_weather=True,
        ),
        {"id": "m1"},
    )


def _seed_china_cockpit_candidate(settings: Settings) -> None:
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        market = SimpleNamespace(
            id="shanghai-18c",
            slug="highest-temperature-in-shanghai-on-may-10-2026-18c",
            title="Highest temperature in Shanghai on May 10?",
            description="Outcome: 18°C. Resolved according to Wunderground on 10 May '26.",
            event_slug="highest-temperature-in-shanghai-on-may-10-2026",
            event_title="Highest temperature in Shanghai on May 10?",
            category=None,
            tags=(),
            yes_token_id="yes-token",
            no_token_id="no-token",
            close_time=None,
            status="active",
            is_weather=True,
            module_id="china_temp_bucket",
        )
        repo.upsert_market(market, {"id": market.id})
        rule = parse_china_temperature_bucket_rule(market.title, market.description)
        repo.save_temperature_bucket_rule(market.id, rule)
        snapshot = MarketSnapshot(
            market_id=market.id,
            best_bid=Decimal("0.05"),
            best_ask=Decimal("0.08"),
            midpoint=Decimal("0.065"),
            spread=Decimal("0.03"),
            liquidity=Decimal("40"),
            fetched_at=datetime.now(timezone.utc),
        )
        repo.save_market_snapshot(snapshot, {"id": market.id})
        repo.upsert_candidate(
            market.id,
            SimpleNamespace(tradable=True, rejection_reason=None),
            snapshot,
            status="dry_run_ready",
            notes="module=china_temp_bucket",
            module_id="china_temp_bucket",
        )
        connection.commit()
    finally:
        connection.close()


def _action_status(settings: Settings, action_id: str) -> str:
    database = Database(settings.database_path)
    connection = database.connect()
    try:
        action = Repository(connection).get_automation_action(action_id)
        assert action is not None
        return action["status"]
    finally:
        connection.close()


def _approve_directly(settings: Settings, action_id: str) -> None:
    database = Database(settings.database_path)
    connection = database.connect()
    try:
        Repository(connection).approve_automation_action(
            action_id, "test", datetime.now(timezone.utc).isoformat()
        )
        connection.commit()
    finally:
        connection.close()


def _actions_count(settings: Settings) -> int:
    database = Database(settings.database_path)
    connection = database.connect()
    try:
        return len(Repository(connection).list_automation_actions())
    finally:
        connection.close()


def test_dashboard_renders_beginner_mode_with_safe_actions(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db", TRADING_DISABLED=True)
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(settings, "/beginner-legacy?lang=zh")

    assert response.status.value == 200
    assert "新手模式" in response.body
    assert "安全演练" in response.body
    assert "Live 交易已锁定" in response.body
    assert 'action="/beginner/rehearse?lang=zh"' in response.body
    assert "/live?lang=zh" in response.body
    assert "Live Launchpad" in response.body
    assert "trade_live" not in response.body


def test_dashboard_beginner_rehearsal_records_dry_run_only(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db", TRADING_DISABLED=True)
    Database(settings.database_path).init_schema()

    response = handle_dashboard_post(settings, "/beginner/rehearse?lang=zh", b"lang=zh", None)

    assert response.status.value == 303
    assert response.headers["Location"].startswith("/beginner?lang=zh")
    connection = Database(settings.database_path).connect()
    try:
        repo = Repository(connection)
        intents = repo.list_recent_order_intents(market_id="demo-weather-nyc-high-2026-05-08")
        assert intents
        assert intents[0]["dry_run"] == 1
        assert intents[0]["status"] == "dry_run"
    finally:
        connection.close()
