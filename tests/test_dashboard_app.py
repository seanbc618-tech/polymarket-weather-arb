from pathlib import Path

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import handle_dashboard_post, render_dashboard_path
from polymarket_weather_arb.dashboard_ui.app import _format_exit_value
from polymarket_weather_arb.services.fixture_service import load_market_fixture
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _settings(tmp_path):
    return Settings(_env_file=None, database_path=tmp_path / "app.db")


class FakeAutopilot:
    def __init__(self, repository):
        self.repository = repository

    def tick(self):
        self.repository.save_autopilot_decision(
            market_id=None,
            action="skip",
            mode="dry_run",
            edge=None,
            reason="fake tick",
            blockers=[],
            status="idle",
        )
        self.repository.update_autopilot_state(
            last_tick_status="idle",
            increment_tick_count=True,
            clear_last_error=True,
        )

    def set_app_mode(self, app_mode):
        mode = "live" if app_mode in {"micro_live", "full_live"} else "dry_run"
        self.repository.update_autopilot_state(enabled=False, mode=mode, app_mode=app_mode)


def _fake_autopilot(repository):
    return FakeAutopilot(repository)


def test_app_page_renders(tmp_path):
    settings = _settings(tmp_path)
    Database(settings.database_path).init_schema()
    response = render_dashboard_path(settings, "/app?lang=zh")
    assert response.status.value == 200
    assert "天气自动交易" in response.body
    assert "启动检查" in response.body
    assert "观察模式" in response.body
    assert "模拟交易" in response.body
    assert "微额实盘" in response.body
    assert "正式实盘" in response.body
    assert "高级模式" in response.body
    assert "启动自动交易" in response.body or "暂停" in response.body
    assert "最近运行记录（只读）" in response.body
    assert "不会改变买卖决策" in response.body
    assert "已发现" in response.body
    assert "机会漏斗流" in response.body
    assert "清空历史记录" in response.body


def test_app_recent_runs_are_collapsed_at_page_bottom(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    with database.transaction() as connection:
        repository = Repository(connection)
        repository.save_autopilot_decision(
            market_id="m1",
            action="skip",
            mode="dry_run",
            edge=None,
            reason="test",
            blockers=[],
            status="idle",
        )

    response = render_dashboard_path(settings, "/app?lang=zh")

    assert 'class="data-table run-table"' in response.body
    assert "data-list='recent-runs-primary'" in response.body
    assert '<details class="row-detail run-detail">' in response.body
    assert response.body.index('class="window window--aux advanced-panel"') < response.body.index(
        'id="recent-runs"'
    )


def test_completed_setup_uses_compact_console_and_collapses_beginner_controls(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    with database.transaction() as connection:
        repository = Repository(connection)
        repository.ensure_autopilot_state()
        repository.update_autopilot_state(app_mode="full_live", mode="live", enabled=False)

    response = render_dashboard_path(settings, "/app?lang=zh")
    body = response.body

    assert 'data-setup-complete="1"' in body
    assert 'class="command-toolbar"' in body
    assert 'id="setup-controls" class="setup-disclosure"' in body
    assert 'class="path-rail"' not in body
    assert body.index('data-od-id="row-checks-safety"') < body.index('id="panel-finance"')
    assert body.index('id="panel-finance"') < body.index('id="panel-stream"')
    assert body.index('id="panel-stream"') < body.index('data-od-id="row-position-funnel-streams"')
    assert body.index('data-od-id="row-position-funnel-streams"') < body.index(
        'id="setup-controls"'
    )


def test_app_limits_recent_runs_and_ranked_markets_with_expand_controls(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    with database.transaction() as connection:
        repository = Repository(connection)
        for index in range(10):
            repository.save_autopilot_decision(
                market_id=f"run-{index}",
                action="skip",
                mode="dry_run",
                edge=None,
                reason=f"run reason {index}",
                blockers=[],
                status="idle",
            )
        for index in range(12):
            market_id = f"bucket-{index}"
            connection.execute(
                "INSERT INTO markets (id, title, module_id, is_weather, raw_payload) "
                "VALUES (?, ?, 'global_temp_bucket', 1, '{}')",
                (market_id, f"Weather bucket {index}"),
            )
            connection.execute(
                "INSERT INTO market_candidates "
                "(market_id, module_id, status, tradable, best_bid, best_ask, notes) "
                "VALUES (?, 'global_temp_bucket', 'dry_run_ready', 1, 0.07, 0.08, 'ready')",
                (market_id,),
            )
            connection.execute(
                "INSERT INTO analyses "
                "(market_id, model_version, fair_lower, fair_upper, reference_price, "
                "edge, side, decision, reasons) "
                "VALUES (?, 'test-v1', 0.30, 0.45, 0.08, ?, 'buy_yes', 'skip', '[\"watch\"]')",
                (market_id, 0.20 - index / 1000),
            )

    response = render_dashboard_path(settings, "/app?lang=zh")
    body = response.body
    run_primary = body.split("data-list='recent-runs-primary'", 1)[1].split("</tbody>", 1)[0]
    ranked_primary = body.split("data-list='ranked-opportunities-primary'", 1)[1].split(
        "</tbody>", 1
    )[0]

    assert run_primary.count("<tr>") == 6
    assert ranked_primary.count("<tr>") == 8
    assert 'data-list="recent-runs-more"' in body
    assert 'data-list="ranked-opportunities-more"' in body


def test_exit_values_are_compact() -> None:
    assert _format_exit_value(None) == "—"
    assert _format_exit_value("12.345678") == "12.3457"
    assert _format_exit_value("0.0000") == "0"


def test_app_renders_ranked_bucket_forecast_probability_price_and_reason(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        connection.execute(
            """
            INSERT INTO markets (id, title, module_id, is_weather, raw_payload)
            VALUES ('bucket-1', 'Atlanta 92-93F', 'global_temp_bucket', 1, '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO market_candidates
                (market_id, module_id, status, tradable, best_bid, best_ask, notes)
            VALUES ('bucket-1', 'global_temp_bucket', 'dry_run_ready', 1, 0.07, 0.08,
                    'candidate ready')
            """
        )
        connection.execute(
            """
            INSERT INTO weather_forecasts
                (market_id, provider, issue_time, valid_time, variable, value, unit,
                 raw_payload)
            VALUES ('bucket-1', 'open-meteo', datetime('now'), datetime('now'),
                    'temperature_high', 92.5, 'F',
                    '{"source_grade":"research_forecast"}')
            """
        )
        connection.execute(
            """
            INSERT INTO analyses
                (market_id, model_version, fair_lower, fair_upper, reference_price,
                 edge, side, decision, reasons)
            VALUES ('bucket-1', 'global-temp-bucket-normal-v1', 0.30, 0.45, 0.08,
                    0.20, 'buy_yes', 'trade', '["conservative edge clears threshold"]')
            """
        )
        connection.commit()
    finally:
        connection.close()

    response = render_dashboard_path(settings, "/app?lang=zh")

    assert "天气机会排行" in response.body
    assert "92.5F (open-meteo)" in response.body
    assert "30.0%–45.0%" in response.body
    assert "0.080" in response.body
    assert "conservative edge clears threshold" in response.body


def test_app_page_renders_v3_hierarchy_and_real_safety_caps(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "app.db",
        max_order_usdc="40",
        max_daily_usdc="120",
        max_market_usdc="60",
    )
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(settings, "/app?lang=zh")

    assert response.status.value == 200
    assert 'class="path-rail"' in response.body
    assert 'id="command" class="window window--primary' in response.body
    assert 'id="checks" class="window window--path' in response.body
    assert 'id="safety" class="window window--gate' in response.body
    assert 'data-mode="observe"' in response.body
    assert 'data-mode="paper"' in response.body
    assert 'data-mode="micro_live"' in response.body
    assert 'data-mode="full_live"' in response.body
    assert "25 USDC" in response.body
    assert "100 USDC" in response.body
    assert "50 USDC" in response.body
    assert "仅限价单" in response.body
    assert 'action="/app/toggle?lang=zh"' in response.body
    assert 'action="/app/tick?lang=zh"' in response.body
    assert 'action="/app/reset-history?lang=zh"' in response.body


def test_app_page_renders_v3_labels_in_english(tmp_path):
    settings = _settings(tmp_path)
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(settings, "/app?lang=en")

    assert response.status.value == 200
    assert "Primary path" in response.body
    assert "Startup checks" in response.body
    assert "Safety Gate" in response.body
    assert "Limit orders only" in response.body
    assert "Funds at risk · gated" in response.body
    assert "Full live" in response.body
    assert "Locked in this slice" not in response.body
    assert 'name="app_mode" value="full_live"' in response.body


def test_app_page_renders_v4_finance_center_hierarchy(tmp_path):
    settings = _settings(tmp_path)
    Database(settings.database_path).init_schema()

    response = render_dashboard_path(settings, "/app?lang=zh")
    body = response.body

    assert response.status.value == 200
    assert 'data-od-id="panel-finance"' in body
    assert 'id="panel-finance"' in body
    assert "finance-panel" in body
    assert 'data-od-id="kpi-grid"' in body
    assert 'data-od-id="kpi-realized"' in body
    assert 'data-od-id="row-checks-safety"' in body
    assert 'data-od-id="row-position-funnel-streams"' in body
    assert 'data-od-id="panel-checks"' in body
    assert 'data-od-id="panel-safety"' in body
    assert 'class="skip-link"' in body
    assert 'id="main"' in body
    assert "data-run-state=" in body
    assert "demo-bar" not in body
    assert "状态演示" not in body
    # Finance center sits after checks/safety dual row, before ranked opportunities.
    assert body.index('data-od-id="row-checks-safety"') < body.index('id="panel-finance"')
    assert body.index('id="panel-finance"') < body.index('data-od-id="row-position-funnel-streams"')
    assert body.index('data-od-id="row-position-funnel-streams"') < body.index('id="recent-runs"')


def test_app_page_renders_v5_stream_monitor_hierarchy(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    with database.transaction() as connection:
        repository = Repository(connection)
        repository.ensure_autopilot_state()
        # Seed an operating console so V6 folds checks into the health rail.
        repository.update_autopilot_state(
            enabled=True,
            increment_tick_count=True,
            last_tick_status="ok",
        )
        repository.save_autopilot_decision(
            market_id="nyc-high",
            action="skip",
            mode="dry_run",
            edge=0.04,
            reason="edge below threshold",
            blockers=[],
            status="idle",
        )

    response = render_dashboard_path(settings, "/app?lang=zh")
    body = response.body

    assert response.status.value == 200
    assert 'data-od-id="panel-stream-live"' in body
    assert 'id="panel-stream"' in body
    assert "本地决策流监控" in body
    assert "事件流水" in body
    assert "edge below threshold" in body
    assert (
        "console v7" in body or "主控台 V7" in body or "console v6" in body or "主控台 V6" in body
    )
    assert "data:image/png;base64," in body
    assert "pnl-ledgers-disclosure" in body
    assert "demo-bar" not in body
    assert "12.4 evt/s" not in body
    # V7: finance → stream → magazine position/funnel streams
    assert body.index('id="panel-finance"') < body.index('id="panel-stream"')
    assert body.index('id="panel-stream"') < body.index('data-od-id="row-position-funnel-streams"')
    assert "ops-health-rail" in body
    assert 'data-od-id="row-checks-safety"' in body
    assert "采样节奏" in body
    assert "Open-Meteo" in body
    assert "data-open-meteo-usage" in body
    assert "/10k" in body
    assert "local cooldown skips=0" in body
    assert "more-ops-disclosure" in body
    assert 'data-od-id="panel-checks"' in body
    assert 'data-od-id="panel-safety"' in body
    # V7 magazine strips (not tables)
    assert 'data-od-id="panel-position-stream"' in body
    assert 'data-od-id="panel-funnel-stream"' in body
    assert "mag-stream" in body
    assert "持仓状态流" in body
    assert "机会漏斗流" in body
    assert "funnel-strip" in body
    assert "position-table" not in body
    assert "funnel-step-count" not in body


def test_app_separates_running_process_from_failed_capital_path(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    with database.transaction() as connection:
        repository = Repository(connection)
        repository.ensure_autopilot_state()
        repository.update_autopilot_state(
            enabled=True,
            mode="live",
            app_mode="full_live",
            increment_tick_count=True,
            last_tick_status="ok",
        )
        repository.save_reconciliation("ok", {"status": "ok"})
        repository.save_reconciliation(
            "adapter-error",
            {
                "status": "adapter-error",
                "failed_stage": "trades",
                "error_type": "UnexpectedResponseError",
            },
        )

    body = render_dashboard_path(settings, "/app?lang=zh").body
    run_start = body.index('data-od-id="chip-run"')
    capital_start = body.index('data-od-id="chip-capital"')
    mode_start = body.index('data-od-id="chip-mode"')
    run_chip = body[run_start:capital_start]
    capital_chip = body[capital_start:mode_start]

    assert 'data-run-state="blocked"' in body
    assert 'data-capital-status="adapter-error"' in body
    assert "进程" in run_chip
    assert "运行中" in run_chip
    assert "已阻断" not in run_chip
    assert "资金通道" in capital_chip
    assert "对账失败" in capital_chip
    assert "trades/UnexpectedResponseError" in body
    assert "capital path blocked: reconciliation adapter-error" in body


def test_app_last_tick_renders_browser_local_time_with_utc_reference(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    repository.ensure_autopilot_state()
    repository.update_autopilot_state(last_tick_at="2026-07-08T18:09:25.754038+00:00")
    connection.commit()
    connection.close()

    response = render_dashboard_path(settings, "/app?lang=zh")

    assert response.status.value == 200
    assert 'data-utc="2026-07-08T18:09:25.754038+00:00"' in response.body
    assert 'data-local-time-target="2026-07-08T18:09:25.754038+00:00"' in response.body
    assert "UTC: 2026-07-08 18:09:25" in response.body
    assert "2026-07-09 02:09:25 GMT+8" not in response.body


def test_root_redirects_to_app(tmp_path):
    settings = _settings(tmp_path)
    Database(settings.database_path).init_schema()
    response = render_dashboard_path(settings, "/?lang=zh")
    assert response.status.value in {302, 303}
    assert "/app?lang=zh" in response.headers["Location"]


def test_app_toggle_updates_state(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    repository.ensure_autopilot_state()
    connection.commit()
    connection.close()

    response = handle_dashboard_post(
        settings,
        "/app/toggle?lang=zh",
        b"enabled=1&lang=zh",
        None,
        autopilot_service_factory=_fake_autopilot,
    )
    assert response.status.value in {302, 303}
    assert "/app?lang=zh" in response.headers["Location"]

    connection = database.connect()
    repository = Repository(connection)
    state = repository.get_autopilot_state()
    assert state is not None
    assert bool(state["enabled"]) is True
    connection.close()


def test_app_mode_post_updates_state(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    repository.ensure_autopilot_state()
    repository.update_autopilot_state(enabled=True, app_mode="paper", mode="dry_run")
    connection.commit()
    connection.close()

    response = handle_dashboard_post(
        settings,
        "/app/mode?lang=zh",
        b"app_mode=micro_live&lang=zh",
        None,
        autopilot_service_factory=_fake_autopilot,
    )

    assert response.status.value in {302, 303}
    connection = database.connect()
    try:
        state = Repository(connection).get_autopilot_state()
        assert state["app_mode"] == "micro_live"
        assert state["mode"] == "live"
        assert bool(state["enabled"]) is False
    finally:
        connection.close()


def test_app_mode_post_selects_full_live(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    repository.ensure_autopilot_state()
    repository.update_autopilot_state(enabled=True, app_mode="paper", mode="dry_run")
    connection.commit()
    connection.close()

    response = handle_dashboard_post(
        settings,
        "/app/mode?lang=en",
        b"app_mode=full_live&lang=en",
        None,
        autopilot_service_factory=_fake_autopilot,
    )

    assert response.status.value in {302, 303}
    connection = database.connect()
    try:
        state = Repository(connection).get_autopilot_state()
        assert state["app_mode"] == "full_live"
        assert state["mode"] == "live"
        assert bool(state["enabled"]) is False
    finally:
        connection.close()


def test_app_start_does_not_run_tick_immediately(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "app.db",
        llm_enabled=False,
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    repository.ensure_autopilot_state()
    repository.update_autopilot_state(enabled=False)
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
    )
    load_market_fixture(fixture, repository, settings, demo_analysis=True)
    connection.commit()
    connection.close()

    response = handle_dashboard_post(
        settings,
        "/app/toggle?lang=zh",
        b"enabled=1&lang=zh",
        None,
        autopilot_service_factory=_fake_autopilot,
    )
    assert response.status.value in {302, 303}

    connection = database.connect()
    repository = Repository(connection)
    state = repository.get_autopilot_state()
    assert state is not None
    assert bool(state["enabled"]) is True
    assert int(state["tick_count"]) == 0
    assert repository.list_autopilot_decisions(limit=5) == []
    connection.close()


def test_app_tick_runs_one_controlled_tick(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()

    response = handle_dashboard_post(
        settings,
        "/app/tick?lang=zh",
        b"lang=zh",
        None,
        autopilot_service_factory=_fake_autopilot,
    )

    assert response.status.value in {302, 303}
    connection = database.connect()
    repository = Repository(connection)
    try:
        state = repository.get_autopilot_state()
        assert int(state["tick_count"]) == 1
        assert len(repository.list_autopilot_decisions(limit=5)) == 1
    finally:
        connection.close()


def test_app_reset_history_clears_decisions(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    repository.ensure_autopilot_state()
    repository.save_autopilot_decision(
        market_id="m1",
        action="skip",
        mode="live",
        edge=None,
        reason="test",
        blockers=[],
        status="skipped",
    )
    repository.update_autopilot_state(increment_tick_count=True, last_tick_status="skipped")
    connection.commit()
    connection.close()

    response = handle_dashboard_post(settings, "/app/reset-history?lang=zh", b"lang=zh", None)
    assert response.status.value in {302, 303}

    connection = database.connect()
    repository = Repository(connection)
    assert repository.list_autopilot_decisions(limit=10) == []
    state = repository.get_autopilot_state()
    assert state is not None
    assert int(state["tick_count"]) == 0
    assert state["last_tick_status"] is None
    connection.close()
