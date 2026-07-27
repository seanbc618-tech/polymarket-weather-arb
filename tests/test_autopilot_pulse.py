"""Phase 2 multi-cadence pulse: serial work classes, cached reprice, fail-stop."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import _run_autopilot_background
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.source_grade import SETTLEMENT_OBSERVATION
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION
from polymarket_weather_arb.domain.weather import ForecastSnapshot, WeatherObservation
from polymarket_weather_arb.services.autopilot_service import (
    CAPITAL_INTERVAL_SECONDS,
    EXIT_INTERVAL_SECONDS,
    GLOBAL_BUCKET_CANDIDATE_SCAN_LIMIT,
    PULSE_SECONDS,
    AutopilotPulseState,
    AutopilotService,
    _risk_adjusted_entry_cap,
)
from polymarket_weather_arb.services.auto_exit_service import AutoExitService, AutoExitTickResult
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


_TEST_TARGET_DATE = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()


class FakeClient:
    def close(self) -> None:
        return None


def _settings(tmp_path, **updates) -> Settings:
    base = dict(
        _env_file=None,
        database_path=tmp_path / "pulse.db",
        MAX_ORDER_USDC=Decimal("1"),
        MAX_DAILY_USDC=Decimal("5"),
        MAX_MARKET_USDC=Decimal("2"),
        MIN_EDGE=Decimal("0.05"),
        TRADING_DISABLED=True,
    )
    base.update(updates)
    return Settings(**base)


def _repo(tmp_path, **settings_kw):
    settings = _settings(tmp_path, **settings_kw)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    return settings, Repository(connection), connection, database


def _seed_global_bucket(
    repository: Repository,
    connection,
    *,
    market_id: str,
    city: str,
    target_date: str | None = None,
    with_forecast: bool = True,
    with_position: bool = False,
) -> None:
    target_date = target_date or _TEST_TARGET_DATE
    target = datetime.fromisoformat(target_date).date()
    # Include year so global bucket rule parser accepts the target date.
    title = f"Highest temperature in {city} on {target:%B} {target.day}, {target.year} 84-85°F"
    repository.upsert_market(
        Market(
            id=market_id,
            title=title,
            description="This market resolves to the temperature range recorded at the "
            "station. Resolution source Wunderground.",
            yes_token_id=f"y-{market_id}",
            no_token_id=f"n-{market_id}",
            is_weather=True,
        ),
        {"id": market_id},
        module_id="global_temp_bucket",
    )
    now = datetime.now(timezone.utc)
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id=market_id,
            best_bid=Decimal("0.10"),
            best_ask=Decimal("0.12"),
            midpoint=Decimal("0.11"),
            spread=Decimal("0.02"),
            liquidity=Decimal("100"),
            fetched_at=now,
        ),
        {"source": "test"},
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO temperature_bucket_rules (
            market_id, module_id, city, target_date, variable, unit,
            bucket_lower_c, bucket_upper_c, bucket_center_c, source,
            settlement_timezone, confidence, tradable, raw_text
        ) VALUES (?, 'global_temp_bucket', ?, ?, 'temperature_max', 'F',
                  28.8, 29.5, 29.2, 'wunderground',
                  'America/New_York', 0.9, 1, ?)
        """,
        (market_id, city, target_date, title),
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO market_candidates
        (market_id, module_id, status, tradable, best_bid, best_ask, notes)
        VALUES (?, 'global_temp_bucket', 'dry_run_ready', 1, 0.10, 0.12, 'ready')
        """,
        (market_id,),
    )
    if with_forecast:
        members = {
            "ecmwf_ifs025": [84.0, 85.0, 86.0],
            "gfs_seamless": [83.0, 84.0, 85.0],
            "icon_seamless": [84.0, 84.5, 85.0],
        }
        forecast = ForecastSnapshot(
            provider="open-meteo-ensemble",
            variable="temperature_max",
            value=Decimal("84.5"),
            unit="F",
            issue_time=now - timedelta(minutes=10),
            valid_time=now + timedelta(hours=6),
            market_id=market_id,
            location=city,
            lower_value=Decimal("83"),
            upper_value=Decimal("86"),
            fetched_at=now - timedelta(minutes=5),
        )
        repository.save_forecast(
            forecast,
            {
                "model_members": members,
                "target_date": target_date,
                "revision": "rev-1",
                "cache_status": "network_fresh",
            },
        )
    if with_position:
        connection.execute(
            """
            INSERT OR REPLACE INTO positions (market_id, outcome, size, notional)
            VALUES (?, 'YES', 5, 2.0)
            """,
            (market_id,),
        )
    connection.commit()


def test_background_pulse_wake_is_two_seconds(tmp_path, monkeypatch):
    database = Database(tmp_path / "wake.db")
    database.init_schema()
    connection = database.connect()
    Repository(connection).update_autopilot_state(enabled=True, mode="dry_run")
    connection.commit()
    connection.close()

    clock = [1000.0]
    sleeps: list[float] = []
    paths: list[str] = []

    def pulse(self, pulse_state, *, monotonic=None, stream_bridge=None, **_kwargs):
        paths.append("pulse")
        clock[0] += 0.2
        return SimpleNamespace(status="idle")

    monkeypatch.setattr(AutopilotService, "pulse", pulse)
    _run_autopilot_background(
        database,
        Settings(_env_file=None, database_path=database.path),
        None,
        tick_seconds=300,
        sleep=lambda s: sleeps.append(s) or clock.__setitem__(0, clock[0] + s),
        monotonic=lambda: clock[0],
        max_cycles=2,
        use_pulse=True,
    )
    assert len(sleeps) == 1
    assert abs(sleeps[0] - (PULSE_SECONDS - 0.2)) < 1e-6
    assert len(paths) == 2


def test_pulse_refuses_overlap(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="dry_run", app_mode="paper")
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())
    pulse_state = AutopilotPulseState(in_flight=True)
    result = service.pulse(pulse_state)
    assert result.status == "skipped"
    assert "in flight" in result.reason
    connection.close()


def test_weather_failure_respects_open_meteo_daily_cooldown(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path)
    repository.ensure_autopilot_state()
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(service, "_backfill_resolved_model_signals", lambda: None)
    monkeypatch.setattr(
        service,
        "_prepare_global_bucket_candidates",
        lambda **_kwargs: (0, 0, 0, 1),
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.open_meteo_cooldown_remaining",
        lambda: 3600,
    )
    pulse_state = AutopilotPulseState(
        next_discovery_at=9999,
        next_weather_refresh_at=0,
        next_history_maintenance_at=9999,
    )

    result = service._pulse_slow_refresh(
        pulse_state,
        app_mode="paper",
        live_mode=False,
        tick_start_ms=100_000,
        monotonic=lambda: 100.0,
    )

    assert result.status == "failed"
    assert pulse_state.next_weather_refresh_at == 3700.0
    connection.close()


def test_position_refresh_uses_cached_group_during_open_meteo_cooldown(
    tmp_path, monkeypatch, caplog
):
    settings, repository, connection, _db = _repo(tmp_path)
    _seed_global_bucket(
        repository,
        connection,
        market_id="held-cooldown",
        city="Denver",
        with_position=True,
    )
    service = AutopilotService(settings, repository, client=FakeClient())
    prepare = Mock(side_effect=AssertionError("position refresh must not hit weather providers"))
    cached_reprice = Mock(return_value=(11, [], None, "cached-revision"))
    monkeypatch.setattr(service, "_prepare_market", prepare)
    monkeypatch.setattr(
        service.workflow,
        "reprice_global_bucket_group_cached",
        cached_reprice,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.open_meteo_cooldown_remaining",
        lambda: 600,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.domain.market_eligibility.is_market_orderable",
        lambda **_kwargs: True,
    )

    with caplog.at_level(logging.INFO):
        service._refresh_position_analyses()

    prepare.assert_not_called()
    cached_reprice.assert_called_once()
    assert cached_reprice.call_args.args[0][0] == "held-cooldown"
    messages = [record.message for record in caplog.records]
    summaries = [
        message
        for message in messages
        if "position analysis cache refresh during Open-Meteo cooldown" in message
    ]
    assert len(summaries) == 1
    assert "analyzed=11" in summaries[0]
    connection.close()


def test_pulse_capital_priority_over_slow_and_reprice(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(
        tmp_path,
        TRADING_DISABLED=False,
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        AUTO_EXIT_ENABLED=True,
        COMPLIANCE_CHECK_ENABLED=True,
    )
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    repository.save_reconciliation("ok", {"status": "ok"})
    connection.commit()

    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(
        service,
        "collect_blockers",
        lambda **_k: type("B", (), {"blocked": False, "items": []})(),
    )
    calls: list[str] = []

    def capital(*_a, **_k):
        calls.append("capital")
        return SimpleNamespace(
            status="ok",
            action="skip",
            market_id=None,
            edge=None,
            reason="capital",
            blockers=[],
            discovered=0,
            intent_id=None,
            is_useful=True,
            duration_ms=1,
            deferred_count=0,
        )

    monkeypatch.setattr(service, "_pulse_capital_maintenance", capital)
    monkeypatch.setattr(
        service, "_pulse_slow_refresh", lambda *_a, **_k: calls.append("slow") or capital()
    )
    monkeypatch.setattr(
        service, "_pulse_cached_reprice", lambda *_a, **_k: calls.append("reprice") or capital()
    )

    pulse_state = AutopilotPulseState(
        next_capital_at=0.0,
        next_discovery_at=0.0,
        next_weather_refresh_at=0.0,
        next_reprice_at=0.0,
    )
    service.pulse(pulse_state, monotonic=lambda: 100.0)
    assert calls == ["capital"]
    connection.close()


def test_pulse_recon_fail_stop_blocks_mutation(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(
        tmp_path,
        TRADING_DISABLED=False,
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        AUTO_EXIT_ENABLED=True,
    )
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(
        service,
        "collect_blockers",
        lambda **_k: type("B", (), {"blocked": False, "items": []})(),
    )
    mutations: list[str] = []
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
        lambda self: {"status": "error", "failed_stage": "balances", "new_fills": []},
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
        lambda *_a, **_k: mutations.append("buy") or (1, ["nope"]),
    )

    pulse_state = AutopilotPulseState(next_capital_at=0.0)
    result = service.pulse(pulse_state, monotonic=lambda: 10.0)
    assert result.status == "failed"
    assert "fail-stop" in result.reason
    assert mutations == []
    connection.close()


def test_cached_reprice_uses_persisted_inputs_only(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path)
    market_id = "bucket-a"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Miami")

    service = AutopilotService(settings, repository, client=FakeClient())
    network = Mock(side_effect=AssertionError("cached reprice must not hit network weather"))
    monkeypatch.setattr(service.workflow, "_fetch_global_weather_uncached", network)
    monkeypatch.setattr(
        service.workflow,
        "observation_provider_factory",
        lambda: Mock(fetch_observation=Mock(side_effect=AssertionError("no obs fetch"))),
    )
    monkeypatch.setattr(
        service.workflow,
        "_cached_d0_observation_context",
        lambda *a, **k: type(
            "D0", (), {"observation": None, "raw_payload": None, "block_reason": None}
        )(),
    )

    analyzed, failures, slow, revision = service.workflow.reprice_global_bucket_group_cached(
        [market_id]
    )
    network.assert_not_called()
    assert slow is None, (slow, failures)
    assert analyzed >= 1
    assert revision is not None
    assert repository.latest_analysis(market_id) is not None
    connection.close()


def test_cached_reprice_marks_slow_refresh_when_forecast_missing(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path)
    market_id = "bucket-missing"
    _seed_global_bucket(
        repository, connection, market_id=market_id, city="Seattle", with_forecast=False
    )
    service = AutopilotService(settings, repository, client=FakeClient())
    analyzed, _failures, slow, _revision = service.workflow.reprice_global_bucket_group_cached(
        [market_id]
    )
    assert analyzed == 0
    assert slow is not None
    assert any(token in slow for token in ("forecast", "incomplete", "missing"))
    connection.close()


def test_cached_reprice_rejects_stale_order_book(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path)
    market_id = "bucket-stale-book"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Miami")
    connection.execute(
        "UPDATE market_snapshots SET fetched_at = ? WHERE market_id = ?",
        ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), market_id),
    )
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())

    analyzed, _failures, slow, _revision = service.workflow.reprice_global_bucket_group_cached(
        [market_id]
    )

    assert analyzed == 0
    assert slow == f"stale_order_book:{market_id}"
    connection.close()


def test_cached_d0_context_accepts_awc_exact_station_observation(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path)
    market_id = "bucket-awc"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Miami")
    service = AutopilotService(settings, repository, client=FakeClient())
    market = repository.get_market(market_id)
    rule = service.workflow._global_rule(market_id, market)
    now = datetime.fromisoformat(f"{_TEST_TARGET_DATE}T16:00:00+00:00")
    repository.save_observation(
        WeatherObservation(
            provider="awc-metar",
            variable="temperature_high",
            value=Decimal("84"),
            unit="F",
            observed_at=now - timedelta(minutes=10),
            market_id=market_id,
            station="KMIA",
            quality_status="AWC",
            fetched_at=now - timedelta(minutes=5),
        ),
        {
            "official_signal": True,
            "source_grade": SETTLEMENT_OBSERVATION,
            "observations": [],
        },
    )
    connection.commit()

    context = service.workflow._cached_d0_observation_context(market_id, rule, now=now)

    assert context.block_reason is None
    assert context.observation is not None
    assert context.observation.quality_status == "AWC"
    connection.close()


def test_pulse_health_updates_liveness_without_fabricating_decision(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path)
    repository.update_autopilot_state(
        enabled=True,
        mode="dry_run",
        app_mode="paper",
        last_tick_duration_ms=12345,
        deferred_candidates_count=17,
    )
    repository.update_autopilot_state(reset_tick_count=True)
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(
        service,
        "collect_blockers",
        lambda **_k: type("B", (), {"blocked": False, "items": []})(),
    )
    far = 1e12
    pulse_state = AutopilotPulseState(
        next_capital_at=far,
        next_exit_at=far,
        next_discovery_at=far,
        next_weather_refresh_at=far,
        next_reprice_at=far,
    )
    result = service.pulse(pulse_state, monotonic=lambda: 1000.0)
    assert result.status == "idle"
    assert "health pulse" in result.reason
    state = repository.get_autopilot_state()
    assert int(state["tick_count"] or 0) == 0
    assert state["last_tick_status"] == "idle"
    assert state["last_tick_at"] is not None
    assert state["last_tick_duration_ms"] == 12345
    assert state["deferred_candidates_count"] == 17
    assert repository.list_autopilot_decisions(limit=1) == []
    connection.close()


def test_select_reprice_group_prefers_live_activity(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path)
    _seed_global_bucket(
        repository,
        connection,
        market_id="m-idle",
        city="Austin",
        target_date=_TEST_TARGET_DATE,
    )
    _seed_global_bucket(
        repository,
        connection,
        market_id="m-live",
        city="Boston",
        target_date=_TEST_TARGET_DATE,
        with_position=True,
    )
    service = AutopilotService(settings, repository, client=FakeClient())
    group, ids = service._select_reprice_group(AutopilotPulseState())
    assert group is not None
    assert group[0] == "Boston"
    assert "m-live" in ids
    connection.close()


def test_reprice_rotation_reserves_two_of_three_slots_for_non_live_groups(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path)
    _seed_global_bucket(
        repository,
        connection,
        market_id="m-live",
        city="Boston",
        target_date=_TEST_TARGET_DATE,
        with_position=True,
    )
    _seed_global_bucket(
        repository,
        connection,
        market_id="m-idle",
        city="Austin",
        target_date=_TEST_TARGET_DATE,
    )
    service = AutopilotService(settings, repository, client=FakeClient())

    for cursor in (1, 2):
        group, ids = service._select_reprice_group(AutopilotPulseState(rotation_cursor=cursor))

        assert group is not None
        assert group[0] == "Austin"
        assert ids == ["m-idle"]
    connection.close()


def test_closed_candidate_is_retired_before_cached_reprice_or_weather_research(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path)
    _seed_global_bucket(
        repository, connection, market_id="m-closed", city="Atlanta", target_date="2026-07-16"
    )
    connection.execute(
        "UPDATE markets SET raw_payload = ? WHERE id = ?",
        ('{"closed": true, "acceptingOrders": false}', "m-closed"),
    )
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())

    group, ids = service._select_reprice_group(AutopilotPulseState())
    analyzed, backlog, deferred, failures = service._prepare_global_bucket_candidates()

    candidate = connection.execute(
        "SELECT status, notes FROM market_candidates WHERE market_id = ?", ("m-closed",)
    ).fetchone()
    assert group is None
    assert ids == []
    assert (analyzed, backlog, deferred, failures) == (0, 0, 0, 0)
    assert candidate["status"] == "expired"
    assert "closed=true" in candidate["notes"]
    connection.close()


def test_weather_batch_failures_log_one_summary_per_refresh(tmp_path, monkeypatch, caplog):
    settings, repository, connection, _db = _repo(tmp_path)
    _seed_global_bucket(repository, connection, market_id="m-log", city="Atlanta")
    service = AutopilotService(settings, repository, client=FakeClient())
    llm_flags = []

    def research(_market_ids, *, allow_llm=True, **_kwargs):
        llm_flags.append(allow_llm)
        return 0, ["first failure", "second failure", "third failure"]

    monkeypatch.setattr(
        service.workflow,
        "research_global_bucket_batch",
        research,
    )

    with caplog.at_level(logging.WARNING):
        result = service._prepare_global_bucket_candidates()

    summaries = [
        record.message
        for record in caplog.records
        if "global bucket analysis skipped" in record.message
    ]
    assert result[-1] == 3
    assert llm_flags == [False]
    assert summaries == ["global bucket analysis skipped: failures=3 first=first failure"]
    connection.close()


def test_weather_refresh_scans_complete_candidate_universe(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path)
    _seed_global_bucket(repository, connection, market_id="m-universe", city="Atlanta")
    service = AutopilotService(settings, repository, client=FakeClient())
    original = repository.list_candidates
    observed_limits: list[int] = []

    def list_candidates(*, limit=50, status=None, module_id=None):
        observed_limits.append(limit)
        return original(limit=limit, status=status, module_id=module_id)

    monkeypatch.setattr(repository, "list_candidates", list_candidates)
    monkeypatch.setattr(
        service.workflow,
        "research_global_bucket_batch",
        lambda _market_ids, **_kwargs: (1, []),
    )

    service._prepare_global_bucket_candidates(max_groups=1)

    assert observed_limits == [GLOBAL_BUCKET_CANDIDATE_SCAN_LIMIT]
    assert GLOBAL_BUCKET_CANDIDATE_SCAN_LIMIT >= 5000
    connection.close()


def test_staged_entry_headroom_never_exceeds_market_risk_cap(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path, MAX_MARKET_USDC=Decimal("1"))
    _seed_global_bucket(repository, connection, market_id="m-cap", city="Atlanta")
    connection.execute(
        """
        INSERT INTO order_intents (
            market_id, token_id, side, limit_price, size, notional,
            rationale, dry_run, status
        ) VALUES ('m-cap', 'y-m-cap', 'buy_yes', 0.01, 100, 1, 'filled', 0, 'filled')
        """
    )
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())

    assert service._staged_entry_headroom(repository.get_market("m-cap")) == Decimal("0")
    connection.close()


def test_risk_adjusted_entry_cap_scales_edge_quorum_dispersion_and_stale_cache():
    assert _risk_adjusted_entry_cap(
        Decimal("2"),
        {
            "edge": "0.06",
            "reasons": json.dumps(
                [
                    "supporting_models=4/6 required=4",
                    "entry_robust_dispersion=0.22",
                ]
            ),
        },
    ) == Decimal("1.00")
    assert _risk_adjusted_entry_cap(
        Decimal("2"),
        {
            "edge": "0.20",
            "reasons": json.dumps(
                [
                    "supporting_models=5/6 required=4",
                    "entry_robust_dispersion=0.10",
                ]
            ),
        },
    ) == Decimal("2.00")
    assert _risk_adjusted_entry_cap(
        Decimal("2"),
        {
            "edge": "0.20",
            "reasons": json.dumps(
                [
                    "supporting_models=6/6 required=4",
                    "entry_robust_dispersion=0.10",
                ]
            ),
        },
    ) == Decimal("2")
    assert _risk_adjusted_entry_cap(
        Decimal("2"),
        {
            "edge": "0.20",
            "reasons": json.dumps(
                [
                    "supporting_models=6/6 required=4",
                    "entry_robust_dispersion=0.10",
                    "weather_cache_status=stale_if_error",
                ]
            ),
        },
    ) == Decimal("0.50")
    assert _risk_adjusted_entry_cap(
        Decimal("2"),
        {
            "reference_price": "0.09",
            "edge": "0.20",
            "reasons": json.dumps(
                [
                    "supporting_models=6/6 required=4",
                    "entry_robust_dispersion=0.10",
                ]
            ),
        },
    ) == Decimal("1.00")
    assert _risk_adjusted_entry_cap(
        Decimal("2"),
        {
            "reference_price": "0.20",
            "edge": "0.20",
            "reasons": json.dumps(
                [
                    "supporting_models=6/6 required=4",
                    "entry_robust_dispersion=0.10",
                ]
            ),
        },
        history_multiplier=Decimal("0.50"),
    ) == Decimal("1")


def test_weather_entry_v4_cap_uses_conservative_probability_and_fractional_kelly():
    assert _risk_adjusted_entry_cap(
        Decimal("10"),
        {
            "reference_price": "0.10",
            "edge": "0.20",
            "reasons": json.dumps(
                [
                    "supporting_families=4/4 required=3",
                    "entry_robust_dispersion=0.08",
                    "decision_probability_conservative=0.40",
                    "model_risk_haircut=0.05",
                ]
            ),
        },
        max_order_usdc=Decimal("4"),
        max_daily_usdc=Decimal("100"),
    ) == Decimal("4")
    assert _risk_adjusted_entry_cap(
        Decimal("10"),
        {
            "reference_price": "0.20",
            "edge": "0.20",
            "reasons": json.dumps(
                [
                    "supporting_families=3/4 required=3",
                    "entry_robust_dispersion=0.08",
                    "decision_probability_conservative=0.30",
                    "model_risk_haircut=0.15",
                ]
            ),
        },
        max_order_usdc=Decimal("4"),
        max_daily_usdc=Decimal("100"),
    ) == Decimal("2")


def test_capital_mutation_forces_immediate_reconciliation_pulse(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
        lambda self: {"status": "ok", "new_fills": []},
    )
    monkeypatch.setattr(
        service,
        "collect_blockers",
        lambda **_k: type("B", (), {"blocked": False, "items": []})(),
    )
    monkeypatch.setattr(service, "_maybe_manage_stale_orders", lambda **_k: [])
    monkeypatch.setattr(service, "_refresh_position_analyses", lambda: None)
    monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (1, 1))
    pulse_state = AutopilotPulseState(next_capital_at=0.0)

    service._pulse_capital_maintenance(
        pulse_state,
        app_mode="full_live",
        live_mode=True,
        tick_start_ms=100_000,
        monotonic=lambda: 100.0,
    )

    assert pulse_state.capital_due_after_mutation is True
    assert pulse_state.next_capital_at == 100.0
    connection.close()


def test_auto_exit_failure_is_reported_once_as_execution_risk(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(
        tmp_path,
        TRADING_DISABLED=False,
        AUTO_EXIT_ENABLED=False,
    )
    events: list[dict[str, object]] = []
    service = AutopilotService(
        settings,
        repository,
        client=FakeClient(),
        notifier=lambda payload: events.append(payload),
    )
    monkeypatch.setattr(
        AutoExitService,
        "run_tick",
        lambda self, **kwargs: AutoExitTickResult(
            enabled_gates_ok=True,
            attempted=1,
            failures=["m1: exchange rejected SELL"],
        ),
    )

    assert service._maybe_auto_exit(app_mode="full_live") == (0, 1)
    assert len(events) == 1
    assert events[0]["daemon_event"] == "app_execution_risk"
    assert events[0]["status"] == "failed"
    assert events[0]["items"] == ["m1: exchange rejected SELL"]
    connection.close()


def test_auto_exit_no_bid_skip_does_not_emit_execution_risk(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(
        tmp_path,
        TRADING_DISABLED=False,
        AUTO_EXIT_ENABLED=False,
    )
    events: list[dict[str, object]] = []
    service = AutopilotService(
        settings,
        repository,
        client=FakeClient(),
        notifier=lambda payload: events.append(payload),
    )
    monkeypatch.setattr(
        AutoExitService,
        "run_tick",
        lambda self, **kwargs: AutoExitTickResult(
            enabled_gates_ok=True,
            attempted=0,
            skipped=["m1: no best bid; auto-exit deferred without SELL attempt"],
            failures=[],
        ),
    )

    assert service._maybe_auto_exit(app_mode="full_live") == (0, 0)
    assert events == []
    connection.close()


def test_idle_capital_maintenance_reconciles_on_three_minute_cadence(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
        lambda self: {"status": "ok", "new_fills": []},
    )
    monkeypatch.setattr(
        service,
        "collect_blockers",
        lambda **_k: type("B", (), {"blocked": False, "items": []})(),
    )
    monkeypatch.setattr(service, "_maybe_manage_stale_orders", lambda **_k: [])
    monkeypatch.setattr(service, "_refresh_position_analyses", lambda: None)
    monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service._jittered_delay",
        lambda seconds: float(seconds),
    )
    pulse_state = AutopilotPulseState(next_capital_at=0.0)

    service._pulse_capital_maintenance(
        pulse_state,
        app_mode="full_live",
        live_mode=True,
        tick_start_ms=100_000,
        monotonic=lambda: 100.0,
    )

    assert CAPITAL_INTERVAL_SECONDS == 180
    assert EXIT_INTERVAL_SECONDS == 180
    assert pulse_state.capital_due_after_mutation is False
    assert pulse_state.next_capital_at == 280.0
    assert pulse_state.next_exit_at == 280.0
    connection.close()


def test_reconciliation_failure_backs_off_capital_and_exit_together(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
        lambda self: {"status": "adapter-error", "failed_stage": "positions", "new_fills": []},
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service._jittered_delay",
        lambda seconds: float(seconds),
    )
    monkeypatch.setattr(service, "_notify_reconciliation_failure", lambda **_kwargs: None)
    pulse_state = AutopilotPulseState(next_capital_at=0.0, next_exit_at=0.0)

    first = service._pulse_capital_maintenance(
        pulse_state,
        app_mode="full_live",
        live_mode=True,
        tick_start_ms=100_000,
        monotonic=lambda: 100.0,
    )
    assert first.status == "failed"
    assert pulse_state.reconciliation_failure_count == 1
    assert pulse_state.next_capital_at == 115.0
    assert pulse_state.next_exit_at == 115.0

    second = service._pulse_capital_maintenance(
        pulse_state,
        app_mode="full_live",
        live_mode=True,
        tick_start_ms=115_000,
        monotonic=lambda: 115.0,
    )
    assert second.status == "failed"
    assert pulse_state.reconciliation_failure_count == 2
    assert pulse_state.next_capital_at == 145.0
    assert pulse_state.next_exit_at == 145.0
    connection.close()


def test_execution_blocker_backs_off_capital_and_exit_together(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path, TRADING_DISABLED=True)
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.ReconciliationService.reconcile",
        lambda self: {"status": "ok", "new_fills": []},
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service._jittered_delay",
        lambda seconds: float(seconds),
    )
    pulse_state = AutopilotPulseState(next_capital_at=0.0, next_exit_at=0.0)

    result = service._pulse_capital_maintenance(
        pulse_state,
        app_mode="micro_live",
        live_mode=True,
        tick_start_ms=100_000,
        monotonic=lambda: 100.0,
    )

    assert result.status == "blocked"
    assert "TRADING_DISABLED=true" in result.blockers
    assert pulse_state.next_capital_at == 280.0
    assert pulse_state.next_exit_at == 280.0
    connection.close()


def test_group_quote_refresh_failure_blocks_cached_reprice(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path)
    _seed_global_bucket(repository, connection, market_id="m-a", city="Miami")
    _seed_global_bucket(repository, connection, market_id="m-b", city="Miami")
    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(
        service,
        "_refresh_group_order_books",
        lambda _ids, **_kwargs: ["m-b: HTTP 429"],
    )
    reprice = Mock(side_effect=AssertionError("partial quote refresh must not reprice"))
    monkeypatch.setattr(service.workflow, "reprice_global_bucket_group_cached", reprice)
    monkeypatch.setattr(
        service,
        "_select_reprice_group",
        lambda _state: (("Miami", "2026-07-20"), ["m-a", "m-b"]),
    )
    pulse_state = AutopilotPulseState()

    result = service._pulse_cached_reprice(
        pulse_state,
        app_mode="paper",
        live_mode=False,
        tick_start_ms=100_000,
        monotonic=lambda: 100.0,
    )

    assert result.status == "failed"
    assert "cached reprice blocked" in result.reason
    reprice.assert_not_called()
    connection.close()


def test_rejected_fast_candidate_does_not_call_live_network_gate(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path)
    market_id = "m-watch"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Miami")
    repository.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="watch-v1",
            fair_lower=Decimal("0.05"),
            fair_upper=Decimal("0.08"),
            reference_price=Decimal("0.12"),
            edge=Decimal("0"),
            side=None,
            decision="reject",
            reasons=["no edge"],
        )
    )
    connection.commit()
    service = AutopilotService(settings, repository, client=FakeClient())
    monkeypatch.setattr(service, "_select_market", lambda: market_id)
    monkeypatch.setattr(
        service,
        "collect_blockers",
        Mock(side_effect=AssertionError("reject must not call compliance/geoblock")),
    )

    result = service._pulse_maybe_enter(
        app_mode="full_live",
        live_mode=True,
        discovered=0,
        tick_start_ms=100_000,
        monotonic=lambda: 100.0,
        deferred_count=0,
        failures=0,
        default_reason="cached watch",
        preferred_market_ids={market_id},
    )

    assert result is None
    connection.close()


def test_live_global_bucket_reprices_better_quote_before_submit(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path, TRADING_DISABLED=False)
    market_id = "m-reprice-before-submit"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Miami")
    repository.save_analysis(
        Analysis(
            market_id=market_id,
            model_version=GLOBAL_BUCKET_MODEL_VERSION,
            fair_lower=Decimal("0.40"),
            fair_upper=Decimal("0.50"),
            reference_price=Decimal("0.12"),
            edge=Decimal("0.25"),
            side="buy_yes",
            decision="trade",
            reasons=["test"],
        )
    )
    repository.update_autopilot_state(mode="live", app_mode="full_live")
    repository.upsert_strategy_override(market_id="*", profile="full-live", live_auto_enabled=True)
    connection.commit()
    client = Mock()
    client.get_token_order_book.return_value = (
        MarketSnapshot(
            market_id="token-book",
            best_bid=Decimal("0.09"),
            best_ask=Decimal("0.10"),
            midpoint=Decimal("0.095"),
            spread=Decimal("0.02"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {"source": "test"},
    )
    client.place_limit_order.return_value = {"order_id": "order-better-quote"}
    service = AutopilotService(settings, repository, client=client)
    service._revalidate_d0_live_analysis = lambda market_id, analysis: (analysis, None)
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service._forecast_source_grade",
        lambda _row: "research_forecast",
    )

    def reprice(market_ids):
        assert market_ids == [market_id]
        repository.save_analysis(
            Analysis(
                market_id=market_id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.40"),
                fair_upper=Decimal("0.50"),
                reference_price=Decimal("0.10"),
                edge=Decimal("0.27"),
                side="buy_yes",
                decision="trade",
                reasons=["cached_event_group_reprice", "supporting_models=6/6 required=4"],
            )
        )
        return 1, [], None, "revision-1"

    service.workflow.reprice_global_bucket_group_cached = reprice
    repository.save_reconciliation("ok", {"status": "ok"})
    connection.commit()

    intent_id, reasons = service._execute_live(market_id, repository.latest_analysis(market_id))

    assert intent_id is not None
    assert reasons == ["live order submitted"]
    intent = repository.get_order_intent(intent_id)
    assert Decimal(str(intent["limit_price"])) == Decimal("0.10")
    assert intent["entry_policy_version"] == "weather-entry-v5"
    assert intent["idempotency_key"].endswith(":forecast:rev-1")
    assert "entry opportunity=forecast:rev-1" in intent["rationale"]
    client.place_limit_order.assert_called_once()
    connection.close()


def test_live_global_bucket_rejects_when_fresh_quote_reprice_removes_edge(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path, TRADING_DISABLED=False)
    market_id = "m-reprice-removes-edge"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Miami")
    repository.save_analysis(
        Analysis(
            market_id=market_id,
            model_version=GLOBAL_BUCKET_MODEL_VERSION,
            fair_lower=Decimal("0.40"),
            fair_upper=Decimal("0.50"),
            reference_price=Decimal("0.12"),
            edge=Decimal("0.25"),
            side="buy_yes",
            decision="trade",
            reasons=["test"],
        )
    )
    repository.update_autopilot_state(mode="live", app_mode="full_live")
    connection.commit()
    client = Mock()
    client.get_token_order_book.return_value = (
        MarketSnapshot(
            market_id="token-book",
            best_bid=Decimal("0.28"),
            best_ask=Decimal("0.30"),
            midpoint=Decimal("0.29"),
            spread=Decimal("0.02"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {"source": "test"},
    )
    service = AutopilotService(settings, repository, client=client)
    service._revalidate_d0_live_analysis = lambda market_id, analysis: (analysis, None)

    def reprice(_market_ids):
        repository.save_analysis(
            Analysis(
                market_id=market_id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.20"),
                fair_upper=Decimal("0.25"),
                reference_price=Decimal("0.30"),
                edge=Decimal("0"),
                side=None,
                decision="reject",
                reasons=["fresh quote removed fee-aware edge"],
            )
        )
        return 1, [], None, "revision-2"

    service.workflow.reprice_global_bucket_group_cached = reprice

    intent_id, reasons = service._execute_live(market_id, repository.latest_analysis(market_id))

    assert intent_id is None
    assert "fresh quote reprice no longer supports entry" in reasons[0]
    client.place_limit_order.assert_not_called()
    connection.close()


def test_live_global_bucket_blocks_overage_stale_if_error_forecast(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path, TRADING_DISABLED=False)
    market_id = "m-stale-weather"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Singapore")
    repository.save_analysis(
        Analysis(
            market_id=market_id,
            model_version=GLOBAL_BUCKET_MODEL_VERSION,
            fair_lower=Decimal("0.40"),
            fair_upper=Decimal("0.50"),
            reference_price=Decimal("0.12"),
            edge=Decimal("0.20"),
            side="buy_yes",
            decision="trade",
            reasons=[
                "weather_cache_status=stale_if_error",
                "supporting_models=6/6 required=4",
                "entry_robust_dispersion=0.10",
            ],
        )
    )
    connection.execute(
        "UPDATE weather_forecasts SET fetched_at = ? WHERE market_id = ?",
        ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), market_id),
    )
    repository.update_autopilot_state(mode="live", app_mode="full_live")
    repository.upsert_strategy_override(market_id="*", profile="full-live", live_auto_enabled=True)
    connection.commit()
    client = Mock()
    service = AutopilotService(settings, repository, client=client)
    service._revalidate_d0_live_analysis = lambda market_id, analysis: (analysis, None)

    intent_id, reasons = service._execute_live(market_id, repository.latest_analysis(market_id))

    assert intent_id is None
    assert "stale-if-error forecast is too old" in reasons[0]
    client.get_token_order_book.assert_not_called()
    connection.close()


def test_live_global_bucket_rejects_operational_entry_gated_analysis(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path, TRADING_DISABLED=False)
    market_id = "m-entry-gated-old-model"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Seoul")
    repository.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="global-temp-bucket-multimodel-v6-entry-gated",
            fair_lower=Decimal("0.40"),
            fair_upper=Decimal("0.50"),
            reference_price=Decimal("0.12"),
            edge=Decimal("0.25"),
            side="buy_yes",
            decision="trade",
            reasons=["entry_gate_only=observation unavailable"],
        )
    )
    repository.replace_positions(
        [
            {
                "market": market_id,
                "token_id": "yes-token",
                "outcome": "Yes",
                "size": "5",
                "current_value": "0.35",
            }
        ]
    )
    repository.update_autopilot_state(mode="live", app_mode="full_live")
    connection.commit()
    client = Mock()
    service = AutopilotService(settings, repository, client=client)
    service._revalidate_d0_live_analysis = lambda market_id, analysis: (analysis, None)

    analysis = repository.latest_analysis(market_id)
    assert (
        service._candidate_entry_rejection_reason(repository.get_market(market_id), analysis)
        is not None
    )
    assert service._select_market() is None
    intent_id, reasons = service._execute_live(market_id, analysis)

    assert intent_id is None
    assert "requires current model version" in reasons[0]
    client.get_token_order_book.assert_not_called()
    client.place_limit_order.assert_not_called()
    connection.close()


def test_v5_live_entry_rejects_edge_below_point_ten_and_ask_below_point_zero_five(
    tmp_path,
):
    settings, repository, connection, _db = _repo(tmp_path, TRADING_DISABLED=False)
    market_id = "m-v5-entry-floor"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Miami")
    service = AutopilotService(settings, repository, client=Mock())
    market = repository.get_market(market_id)

    repository.save_analysis(
        Analysis(
            market_id=market_id,
            model_version=GLOBAL_BUCKET_MODEL_VERSION,
            fair_lower=Decimal("0.30"),
            fair_upper=Decimal("0.40"),
            reference_price=Decimal("0.12"),
            edge=Decimal("0.09"),
            side="buy_yes",
            decision="trade",
            reasons=["V5 floor"],
        )
    )
    rejection = service._v5_live_entry_rejection_reason(
        market, repository.latest_analysis(market_id)
    )
    assert rejection is not None
    assert "below 0.10" in rejection

    repository.save_analysis(
        Analysis(
            market_id=market_id,
            model_version=GLOBAL_BUCKET_MODEL_VERSION,
            fair_lower=Decimal("0.30"),
            fair_upper=Decimal("0.40"),
            reference_price=Decimal("0.04"),
            edge=Decimal("0.20"),
            side="buy_yes",
            decision="trade",
            reasons=["V5 price floor"],
        )
    )
    rejection = service._v5_live_entry_rejection_reason(
        market, repository.latest_analysis(market_id)
    )
    assert rejection is not None
    assert "ask is below 0.05" in rejection
    connection.close()


def test_v5_live_entry_pauses_d0(tmp_path, monkeypatch):
    settings, repository, connection, _db = _repo(tmp_path, TRADING_DISABLED=False)
    market_id = "m-v5-d0"
    _seed_global_bucket(repository, connection, market_id=market_id, city="Seoul")
    repository.save_analysis(
        Analysis(
            market_id=market_id,
            model_version=GLOBAL_BUCKET_MODEL_VERSION,
            fair_lower=Decimal("0.30"),
            fair_upper=Decimal("0.40"),
            reference_price=Decimal("0.12"),
            edge=Decimal("0.20"),
            side="buy_yes",
            decision="trade",
            reasons=["V5 D0 pause"],
        )
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service._staged_entry_horizon",
        lambda *_args, **_kwargs: "D0",
    )
    service = AutopilotService(settings, repository, client=Mock())

    rejection = service._v5_live_entry_rejection_reason(
        repository.get_market(market_id),
        repository.latest_analysis(market_id),
    )

    assert rejection == "weather-entry-v5 pauses D0 live entry"
    connection.close()


def test_v5_prior_filled_buy_freezes_same_event_reentry_and_sibling_rotation(tmp_path):
    settings, repository, connection, _db = _repo(tmp_path, TRADING_DISABLED=False)
    for market_id in ("atlanta-low", "atlanta-high"):
        _seed_global_bucket(repository, connection, market_id=market_id, city="Atlanta")
        repository.save_analysis(
            Analysis(
                market_id=market_id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.30"),
                fair_upper=Decimal("0.40"),
                reference_price=Decimal("0.12"),
                edge=Decimal("0.20"),
                side="buy_yes",
                decision="trade",
                reasons=["V5 one entry campaign"],
            )
        )
    connection.execute(
        """
        INSERT INTO order_intents (
            market_id, side, token_id, limit_price, size, notional,
            rationale, dry_run, status, entry_policy_version, created_at
        ) VALUES (
            'atlanta-low', 'buy_yes', 'y-atlanta-low', 0.12, 5, 0.6,
            'filled V5 entry', 0, 'filled', 'weather-entry-v5', ?
        )
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    repository.update_autopilot_state(mode="live", app_mode="full_live")
    connection.commit()
    service = AutopilotService(settings, repository, client=Mock())

    same = repository.prior_accepted_live_buy_in_event("atlanta-low")
    sibling = repository.prior_accepted_live_buy_in_event("atlanta-high")
    assert same is not None and same["bought_market_id"] == "atlanta-low"
    assert sibling is not None and sibling["bought_market_id"] == "atlanta-low"
    rejection = service._v5_live_entry_rejection_reason(
        repository.get_market("atlanta-high"),
        repository.latest_analysis("atlanta-high"),
    )
    assert rejection is not None
    assert "freezes scale-in/re-entry" in rejection
    assert service._select_market() is None
    connection.close()
