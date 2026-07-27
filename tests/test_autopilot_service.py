from decimal import Decimal
from datetime import date, timedelta
from pathlib import Path

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import OrderIntent, live_order_opportunity_key
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.global_temperature_bucket import (
    parse_global_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION
from polymarket_weather_arb.services.autopilot_service import (
    AutopilotService,
    _action_from_analysis_side,
    _weather_analysis_max_age,
)
from polymarket_weather_arb.services.calibration_service import (
    CalibrationService,
    D0ModelSizingCalibration,
    EntryPerformanceCalibration,
)
from polymarket_weather_arb.services.market_workflow_service import analysis_from_row
from polymarket_weather_arb.services.llm_advisor_service import LlmAdvisorService
from polymarket_weather_arb.services.fixture_service import load_market_fixture
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeClient:
    def list_markets(self, *, limit=100, offset=0):
        return []

    def get_event_markets_by_slug(self, slug):
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


def _repo(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "autopilot.db")
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    return settings, Repository(connection), connection


def test_run_loop_uses_fixed_start_to_start_cadence(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        service = AutopilotService(settings, repository, client=FakeClient())
        service.ensure_state(mode="dry_run", tick_seconds=300)
        repository.update_autopilot_state(enabled=True, mode="dry_run")
        connection.commit()
        clock = [1000.0]
        sleeps = []

        def tick():
            clock[0] += 100.0

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        monkeypatch.setattr(service, "tick", tick)
        service.run_loop(
            tick_seconds=300,
            sleep=sleep,
            monotonic=lambda: clock[0],
            max_ticks=2,
        )

        assert sleeps == [200.0]
        assert clock[0] == 1400.0
    finally:
        connection.close()


def test_d0_analysis_refreshes_each_five_minutes(monkeypatch):
    monkeypatch.setattr(
        "polymarket_weather_arb.domain.market_eligibility.try_local_weather_day",
        lambda **_: date(2026, 7, 17),
    )

    assert _weather_analysis_max_age(city="New York", target_date="2026-07-17") == timedelta(
        minutes=5
    )
    assert _weather_analysis_max_age(city="New York", target_date="2026-07-18") == timedelta(
        minutes=30
    )


def test_live_d0_entry_revalidates_weather_and_blocks_stale_trade(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    market_id = "nyc-d0-live-revalidate"
    title = "Will the highest temperature in New York City be 82-83°F on July 19, 2026?"
    description = "Settlement source: Wunderground station KLGA."
    try:
        repository.upsert_market(
            Market(
                id=market_id,
                title=title,
                description=description,
                yes_token_id="yes-d0",
                no_token_id="no-d0",
                is_weather=True,
            ),
            {"id": market_id},
            module_id="global_temp_bucket",
        )
        repository.save_temperature_bucket_rule(
            market_id,
            parse_global_temperature_bucket_rule(title, description),
            module_id="global_temp_bucket",
        )
        repository.save_analysis(
            Analysis(
                market_id=market_id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.30"),
                fair_upper=Decimal("0.50"),
                reference_price=Decimal("0.10"),
                edge=Decimal("0.25"),
                side="buy_yes",
                decision="trade",
                reasons=["old pre-peak evidence"],
            )
        )
        repository.update_autopilot_state(mode="live", app_mode="full_live")
        connection.commit()
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(
            "polymarket_weather_arb.domain.market_eligibility.try_local_weather_day",
            lambda **_: date(2026, 7, 19),
        )
        calls = []

        def revalidate(market_ids, *, allow_llm=True, **_kwargs):
            calls.append((market_ids, allow_llm))
            repository.save_analysis(
                Analysis(
                    market_id=market_id,
                    model_version=GLOBAL_BUCKET_MODEL_VERSION,
                    fair_lower=Decimal("0"),
                    fair_upper=Decimal("0.10"),
                    reference_price=Decimal("0.10"),
                    edge=Decimal("0"),
                    side=None,
                    decision="watch",
                    reasons=["post-peak evidence blocks entry"],
                )
            )
            return 1, []

        monkeypatch.setattr(service.workflow, "research_global_bucket_batch", revalidate)

        intent_id, reasons = service._execute_live(
            market_id,
            repository.latest_analysis(market_id),
        )

        assert intent_id is None
        assert "no longer supports entry" in reasons[0]
        assert calls == [([market_id], False)]
    finally:
        connection.close()


def test_autopilot_backfills_cached_polymarket_resolution_without_network(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        repository.upsert_market(
            Market(id="resolved", title="resolved bucket", is_weather=True),
            {
                "closed": True,
                "umaResolutionStatus": "resolved",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0", "1"]',
            },
            module_id="global_temp_bucket",
        )
        repository.connection.execute(
            "INSERT INTO model_signals "
            "(market_id, model_version, forecast_provider, yes_probability, fair_lower, "
            "fair_upper, edge, decision, outcome_status, raw_payload) "
            "VALUES ('resolved', 'v5', 'ensemble', 0.8, 0.7, 0.9, 0.5, "
            "'trade', 'pending', '{}')"
        )
        client = FakeClient()
        service = AutopilotService(settings, repository, client=client)

        audited, settled = service._backfill_resolved_model_signals()

        assert audited == 1
        assert settled == 1
        signal = repository.latest_model_signal("resolved")
        assert signal["resolved_outcome"] == "no"
        assert signal["settlement_source"] == "polymarket_gamma_resolution"
    finally:
        connection.close()


def test_autopilot_tick_dry_run_records_decision(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(service, "_select_market", lambda: market_id)
        service.ensure_state(mode="dry_run")
        repository.update_autopilot_state(enabled=True, mode="dry_run")
        connection.commit()

        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )

        result = service.tick()
        connection.commit()

        assert result.status in {"executed", "skipped", "idle"}
        decisions = repository.list_autopilot_decisions(limit=5)
        assert len(decisions) >= 1
        assert decisions[0]["mode"] == "dry_run"
    finally:
        connection.close()


def test_autopilot_blocked_when_trading_disabled(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        settings = settings.model_copy(update={"trading_disabled": True})
        service = AutopilotService(settings, repository, client=FakeClient())
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live")
        connection.commit()

        result = service.tick()
        connection.commit()

        assert result.status == "blocked"
        assert "TRADING_DISABLED=true" in result.reason
    finally:
        connection.close()


def test_paper_mode_runs_when_live_trading_is_disabled(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        settings = settings.model_copy(update={"trading_disabled": True})
        service = AutopilotService(settings, repository, client=FakeClient())

        blockers = service.collect_blockers(live_mode=False, app_mode="paper")

        assert "TRADING_DISABLED=true" not in blockers.items
    finally:
        connection.close()


def test_autopilot_live_execute_empty_whitelist_is_open(tmp_path):
    """Empty LIVE_MARKET_IDS does not block; next gate is live_auto override."""
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        service = AutopilotService(settings, repository, client=FakeClient())

        intent_id, reasons = service._execute_live(market_id, None)

        assert intent_id is None
        assert reasons == ["live auto override is not enabled"]
    finally:
        connection.close()


def test_autopilot_live_execute_restricted_whitelist_blocks(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        settings = settings.model_copy(update={"live_market_ids": "other-market-only"})
        service = AutopilotService(settings, repository, client=FakeClient())

        intent_id, reasons = service._execute_live(market_id, None)

        assert intent_id is None
        assert reasons == ["market is not whitelisted in LIVE_MARKET_IDS"]
    finally:
        connection.close()


def test_autopilot_live_execute_requires_micro_live_override(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        settings = settings.model_copy(update={"live_market_ids": market_id})
        service = AutopilotService(settings, repository, client=FakeClient())

        intent_id, reasons = service._execute_live(market_id, None)

        assert intent_id is None
        assert reasons == ["live auto override is not enabled"]
    finally:
        connection.close()


def test_autopilot_selects_global_bucket_by_edge(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        market = Market(
            id="global-edge",
            title="Will the highest temperature in Atlanta be 92-93°F on December 31, 2099?",
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
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.40"),
                fair_upper=Decimal("0.50"),
                reference_price=Decimal("0.10"),
                edge=Decimal("0.28"),
                side="buy_yes",
                decision="trade",
                reasons=["rank me"],
            )
        )

        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(
            service,
            "_staged_entry_headroom",
            lambda _market: Decimal("2"),
        )

        assert service._select_market() == market.id
    finally:
        connection.close()


def test_scale_in_dust_does_not_starve_best_new_candidate(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        markets = (
            (
                "held-dust",
                "Atlanta",
                "atlanta-event",
                Decimal("0.70"),
            ),
            (
                "new-opportunity",
                "Miami",
                "miami-event",
                Decimal("0.50"),
            ),
        )
        for market_id, city, event_slug, edge in markets:
            market = Market(
                id=market_id,
                title=(f"Will the highest temperature in {city} be 90-91°F on December 31, 2099?"),
                description="Resolved according to Wunderground.",
                event_slug=event_slug,
                yes_token_id=f"yes-{market_id}",
                no_token_id=f"no-{market_id}",
                is_weather=True,
            )
            repository.upsert_market(
                type(
                    "ModuleMarket",
                    (),
                    {**market.__dict__, "module_id": "global_temp_bucket"},
                )(),
                {"id": market.id, "orderMinSize": "5"},
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
                    model_version=GLOBAL_BUCKET_MODEL_VERSION,
                    fair_lower=Decimal("0.60"),
                    fair_upper=Decimal("0.70"),
                    reference_price=(
                        Decimal("0.02") if market.id == "held-dust" else Decimal("0.10")
                    ),
                    edge=edge,
                    side="buy_yes",
                    decision="trade",
                    reasons=["rank by executable edge"],
                )
            )

        repository.replace_positions(
            [
                {
                    "market": "held-dust",
                    "token_id": "yes-held-dust",
                    "outcome": "Yes",
                    "size": "10",
                    "current_value": "1",
                }
            ]
        )
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(
            service,
            "_staged_entry_headroom",
            lambda market: (
                Decimal("0.0000005") if str(market["id"]) == "held-dust" else Decimal("2")
            ),
        )

        assert service._select_market() == "new-opportunity"
    finally:
        connection.close()


def test_recorded_forecast_revision_does_not_starve_new_live_candidate(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        for market_id, city, edge in (
            ("held-recorded", "Seoul", Decimal("0.70")),
            ("new-revision", "Miami", Decimal("0.40")),
        ):
            market = Market(
                id=market_id,
                title=(f"Will the highest temperature in {city} be 90-91°F on December 31, 2099?"),
                description="Resolved according to Wunderground.",
                event_slug=f"{city.lower()}-event",
                yes_token_id=f"yes-{market_id}",
                no_token_id=f"no-{market_id}",
                is_weather=True,
            )
            repository.upsert_market(
                type(
                    "ModuleMarket",
                    (),
                    {**market.__dict__, "module_id": "global_temp_bucket"},
                )(),
                {"id": market.id, "orderMinSize": "5"},
            )
            repository.upsert_candidate(
                market.id,
                type("Rule", (), {"tradable": True, "rejection_reason": None})(),
                status="dry_run_ready",
                module_id="global_temp_bucket",
            )
            analysis_id = repository.save_analysis(
                Analysis(
                    market_id=market.id,
                    model_version=GLOBAL_BUCKET_MODEL_VERSION,
                    fair_lower=Decimal("0.60"),
                    fair_upper=Decimal("0.70"),
                    reference_price=Decimal("0.10"),
                    edge=edge,
                    side="buy_yes",
                    decision="trade",
                    reasons=["rank live opportunities"],
                )
            )
            connection.execute(
                "UPDATE model_signals SET raw_payload = ? WHERE analysis_id = ?",
                ('{"forecast_revision":"revision-1"}', analysis_id),
            )

        repository.replace_positions(
            [
                {
                    "market": "held-recorded",
                    "token_id": "yes-held-recorded",
                    "outcome": "Yes",
                    "size": "10",
                    "current_value": "1",
                }
            ]
        )
        repository.save_order_intent(
            OrderIntent(
                market_id="held-recorded",
                side="buy_yes",
                token_id="yes-held-recorded",
                limit_price=Decimal("0.10"),
                size=Decimal("10"),
                notional=Decimal("1"),
                rationale="already traded this forecast revision",
                dry_run=False,
                status="filled",
                idempotency_key=live_order_opportunity_key(
                    market_id="held-recorded",
                    side="buy_yes",
                    token_id="yes-held-recorded",
                    opportunity_id="forecast:revision-1",
                ),
            )
        )
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(
            service,
            "_candidate_entry_rejection_reason",
            lambda *_args: None,
        )

        assert service._select_market() == "new-revision"
    finally:
        connection.close()


def test_unexecutable_best_edge_does_not_starve_executable_candidate(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        markets = (
            ("too-expensive", "Dallas", Decimal("0.54"), Decimal("0.90")),
            ("executable", "Atlanta", Decimal("0.20"), Decimal("0.40")),
        )
        for market_id, city, price, edge in markets:
            market = Market(
                id=market_id,
                title=(f"Will the highest temperature in {city} be 90-91°F on December 31, 2099?"),
                description="Resolved according to Wunderground.",
                event_slug=f"{city.lower()}-event",
                yes_token_id=f"yes-{market_id}",
                no_token_id=f"no-{market_id}",
                is_weather=True,
            )
            repository.upsert_market(
                type(
                    "ModuleMarket",
                    (),
                    {**market.__dict__, "module_id": "global_temp_bucket"},
                )(),
                {"id": market.id, "orderMinSize": "5"},
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
                    model_version=GLOBAL_BUCKET_MODEL_VERSION,
                    fair_lower=Decimal("0.60"),
                    fair_upper=Decimal("0.70"),
                    reference_price=price,
                    edge=edge,
                    side="buy_yes",
                    decision="trade",
                    reasons=["rank executable opportunities"],
                )
            )

        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(
            service,
            "_staged_entry_headroom",
            lambda _market: Decimal("2"),
        )

        assert service._select_market() == "executable"
    finally:
        connection.close()


def test_exhausted_stage_cap_does_not_starve_executable_candidate(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        markets = (
            ("cap-reached", "London", Decimal("0.30"), Decimal("0.80")),
            ("cap-available", "Atlanta", Decimal("0.20"), Decimal("0.40")),
        )
        for market_id, city, price, edge in markets:
            market = Market(
                id=market_id,
                title=(f"Will the highest temperature in {city} be 90-91°F on December 31, 2099?"),
                description="Resolved according to Wunderground.",
                event_slug=f"{city.lower()}-event",
                yes_token_id=f"yes-{market_id}",
                no_token_id=f"no-{market_id}",
                is_weather=True,
            )
            repository.upsert_market(
                type(
                    "ModuleMarket",
                    (),
                    {**market.__dict__, "module_id": "global_temp_bucket"},
                )(),
                {"id": market.id, "orderMinSize": "5"},
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
                    model_version=GLOBAL_BUCKET_MODEL_VERSION,
                    fair_lower=Decimal("0.60"),
                    fair_upper=Decimal("0.70"),
                    reference_price=price,
                    edge=edge,
                    side="buy_yes",
                    decision="trade",
                    reasons=["rank available stage caps"],
                )
            )

        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(
            service,
            "_staged_entry_headroom",
            lambda market: Decimal("0") if str(market["id"]) == "cap-reached" else Decimal("2"),
        )

        assert service._select_market() == "cap-available"
    finally:
        connection.close()


def test_autopilot_does_not_reenter_market_with_reconciled_position(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        market = Market(
            id="already-owned",
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
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.40"),
                fair_upper=Decimal("0.50"),
                reference_price=Decimal("0.10"),
                edge=Decimal("0.28"),
                side="buy_yes",
                decision="trade",
                reasons=["do not average in"],
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
    finally:
        connection.close()


def test_autopilot_persists_exchange_minimum_blocker_once(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "minimum-blocker.db",
        MAX_ORDER_USDC=Decimal("2"),
        MAX_MARKET_USDC=Decimal("6"),
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    try:
        market = Market(
            id="minimum-blocked",
            title=("Will the highest temperature in Miami be 90-91°F on December 31, 2099?"),
            description="Resolved according to Wunderground.",
            yes_token_id="yes-minimum",
            no_token_id="no-minimum",
            is_weather=True,
        )
        repository.upsert_market(
            type(
                "ModuleMarket",
                (),
                {**market.__dict__, "module_id": "global_temp_bucket"},
            )(),
            {"id": market.id, "orderMinSize": "10", "minimumOrderSize": "10"},
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
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.60"),
                fair_upper=Decimal("0.70"),
                reference_price=Decimal("0.30"),
                edge=Decimal("0.25"),
                side="buy_yes",
                decision="trade",
                reasons=["valid signal constrained by exchange minimum"],
            )
        )
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(
            service,
            "_risk_adjusted_entry_headroom",
            lambda *_args: Decimal("2"),
        )

        assert service._select_market() is None
        assert service._select_market() is None
        decisions = [
            row
            for row in repository.list_autopilot_decisions(limit=10)
            if row["action"] == "entry_minimum_blocked"
        ]
        assert len(decisions) == 1
        assert decisions[0]["market_id"] == market.id
        assert "order below exchange minimum" in decisions[0]["reason"]
    finally:
        connection.close()


def test_minimum_viable_entry_uplift_reaches_exchange_minimum(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "minimum-uplift.db",
        MAX_ORDER_USDC=Decimal("4"),
        MAX_MARKET_USDC=Decimal("10"),
        MAX_DAILY_USDC=Decimal("100"),
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    try:
        market = Market(
            id="minimum-uplift",
            title=("Will the highest temperature in Cape Town be 24°C on December 31, 2099?"),
            yes_token_id="yes-minimum-uplift",
            no_token_id="no-minimum-uplift",
            is_weather=True,
        )
        repository.upsert_market(
            type(
                "ModuleMarket",
                (),
                {**market.__dict__, "module_id": "global_temp_bucket"},
            )(),
            {
                "id": market.id,
                "orderMinSize": "5",
                "feesEnabled": True,
                "feeType": "weather",
            },
        )
        repository.save_analysis(
            Analysis(
                market_id=market.id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.35"),
                fair_upper=Decimal("0.45"),
                reference_price=Decimal("0.29"),
                edge=Decimal("0.07"),
                side="buy_yes",
                decision="trade",
                reasons=["supporting_families=4/5"],
            )
        )
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(service, "_staged_entry_headroom", lambda _market: Decimal("10"))
        market_row = repository.get_market(market.id)
        analysis_row = repository.latest_analysis(market.id)

        headroom = service._risk_adjusted_entry_headroom(market_row, analysis_row)

        assert headroom == Decimal("1.5014900")
        assert (
            service._staged_entry_rejection_reason(
                market_row,
                analysis_row,
                headroom=headroom,
            )
            is None
        )
    finally:
        connection.close()


def test_minimum_viable_entry_fee_rounding_buffer_survives_price_quantization(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "minimum-fee-quantum.db",
        MAX_ORDER_USDC=Decimal("4"),
        MAX_DAILY_USDC=Decimal("100"),
        MAX_MARKET_USDC=Decimal("10"),
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    try:
        market = Market(
            id="minimum-fee-quantum",
            title="Will the highest temperature in Cape Town be 24°C on December 31, 2099?",
            yes_token_id="yes-minimum-fee-quantum",
            no_token_id="no-minimum-fee-quantum",
            is_weather=True,
        )
        repository.upsert_market(
            type(
                "ModuleMarket",
                (),
                {**market.__dict__, "module_id": "global_temp_bucket"},
            )(),
            {
                "id": market.id,
                "orderMinSize": "5",
                "feesEnabled": True,
                "feeType": "weather",
            },
        )
        repository.save_analysis(
            Analysis(
                market_id=market.id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.35"),
                fair_upper=Decimal("0.45"),
                reference_price=Decimal("0.03"),
                edge=Decimal("0.07"),
                side="buy_yes",
                decision="trade",
                reasons=[],
            )
        )
        service = AutopilotService(settings, repository, client=FakeClient())
        market_row = repository.get_market(market.id)
        analysis_row = repository.latest_analysis(market.id)
        headroom = service._minimum_viable_entry_headroom(
            market_row,
            analysis_row,
            adjusted=Decimal("1"),
            staged_headroom=Decimal("4"),
        )
        from polymarket_weather_arb.domain.execution import build_proposed_order

        order = build_proposed_order(
            analysis_from_row(analysis_row),
            market_row["yes_token_id"],
            market_row["no_token_id"],
            headroom,
            market_payload={
                "orderMinSize": "5",
                "feesEnabled": True,
                "feeType": "weather",
            },
        )

        assert order is not None
        assert order.size >= Decimal("5")
    finally:
        connection.close()


def test_minimum_viable_entry_uplift_never_breaches_hard_order_cap(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "minimum-hard-cap.db",
        MAX_ORDER_USDC=Decimal("1.40"),
        MAX_MARKET_USDC=Decimal("10"),
        MAX_DAILY_USDC=Decimal("100"),
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    try:
        market = Market(
            id="minimum-hard-cap",
            title=("Will the highest temperature in Cape Town be 24°C on December 31, 2099?"),
            yes_token_id="yes-minimum-hard-cap",
            no_token_id="no-minimum-hard-cap",
            is_weather=True,
        )
        repository.upsert_market(
            type(
                "ModuleMarket",
                (),
                {**market.__dict__, "module_id": "global_temp_bucket"},
            )(),
            {"id": market.id, "orderMinSize": "5"},
        )
        repository.save_analysis(
            Analysis(
                market_id=market.id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.35"),
                fair_upper=Decimal("0.45"),
                reference_price=Decimal("0.29"),
                edge=Decimal("0.07"),
                side="buy_yes",
                decision="trade",
                reasons=["supporting_families=4/5"],
            )
        )
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(service, "_staged_entry_headroom", lambda _market: Decimal("10"))
        market_row = repository.get_market(market.id)
        analysis_row = repository.latest_analysis(market.id)

        headroom = service._risk_adjusted_entry_headroom(market_row, analysis_row)

        assert headroom == Decimal("1.00")
        rejection = service._staged_entry_rejection_reason(
            market_row,
            analysis_row,
            headroom=headroom,
        )
        assert rejection is not None
        assert "effective entry headroom 1.00" in rejection
    finally:
        connection.close()


def test_autopilot_combines_entry_history_with_d0_brier_reduction(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service._staged_entry_horizon",
            lambda *_args, **_kwargs: "D0",
        )
        monkeypatch.setattr(
            CalibrationService,
            "entry_performance",
            lambda *_args, **_kwargs: EntryPerformanceCalibration(
                entry_policy_version="weather-entry-v5",
                policy_samples=0,
                horizon="D0",
                price_band="0.10_0.24",
                horizon_samples=0,
                price_band_samples=0,
                horizon_win_rate=None,
                price_band_win_rate=None,
                multiplier=Decimal("1"),
                reason="entry history neutral",
            ),
        )
        monkeypatch.setattr(
            CalibrationService,
            "weather_model_sizing",
            lambda *_args, **_kwargs: D0ModelSizingCalibration(
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                forecast_provider="global-weather",
                distinct_events=31,
                brier_score=Decimal("0.3467"),
                hit_rate=Decimal("0.3871"),
                multiplier=Decimal("0.50"),
                reason="D0 event-level Brier sizing multiplier=0.50",
            ),
        )

        calibration = service._entry_performance_calibration(
            {"id": "m-d0", "title": "Highest temperature in Seoul on July 23?"},
            {
                "model_version": GLOBAL_BUCKET_MODEL_VERSION,
                "reference_price": "0.20",
            },
        )

        assert calibration.multiplier == Decimal("0.50")
        assert "entry history neutral" in calibration.reason
        assert "D0 event-level Brier sizing multiplier=0.50" in calibration.reason
    finally:
        connection.close()


def test_weak_model_history_is_not_uplifted_back_to_exchange_minimum(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "weak-model-minimum.db",
        MAX_ORDER_USDC=Decimal("4"),
        MAX_MARKET_USDC=Decimal("10"),
        MAX_DAILY_USDC=Decimal("100"),
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    try:
        market = Market(
            id="weak-model-minimum",
            title="Will the highest temperature in Seoul be 31°C on December 31, 2099?",
            yes_token_id="yes-weak-model",
            no_token_id="no-weak-model",
            is_weather=True,
        )
        repository.upsert_market(
            type(
                "ModuleMarket",
                (),
                {**market.__dict__, "module_id": "global_temp_bucket"},
            )(),
            {"id": market.id, "orderMinSize": "5"},
        )
        repository.save_analysis(
            Analysis(
                market_id=market.id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.35"),
                fair_upper=Decimal("0.45"),
                reference_price=Decimal("0.20"),
                edge=Decimal("0.12"),
                side="buy_yes",
                decision="trade",
                reasons=["supporting_families=4/5"],
            )
        )
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(service, "_staged_entry_headroom", lambda _market: Decimal("4"))
        monkeypatch.setattr(
            service,
            "_entry_performance_calibration",
            lambda *_args, **_kwargs: EntryPerformanceCalibration(
                entry_policy_version="weather-entry-v5",
                policy_samples=36,
                horizon="D1",
                price_band="0.10_0.24",
                horizon_samples=36,
                price_band_samples=20,
                horizon_win_rate=Decimal("0.2778"),
                price_band_win_rate=Decimal("0.30"),
                multiplier=Decimal("0.25"),
                reason="D1 event-level Brier sizing multiplier=0.25",
            ),
        )
        market_row = repository.get_market(market.id)
        analysis_row = repository.latest_analysis(market.id)

        headroom = service._risk_adjusted_entry_headroom(market_row, analysis_row)

        assert headroom < Decimal("1")
        rejection = service._staged_entry_rejection_reason(
            market_row,
            analysis_row,
            headroom=headroom,
        )
        assert rejection is not None
        assert "order below exchange minimum" in rejection
    finally:
        connection.close()


def test_full_live_v5_uses_configured_cap_with_legacy_calibration_shadow(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "full-live-v5-sizing.db",
        MAX_ORDER_USDC=Decimal("2"),
        MAX_MARKET_USDC=Decimal("10"),
        MAX_DAILY_USDC=Decimal("100"),
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    try:
        market = Market(
            id="full-live-v5-sizing",
            title="Will the highest temperature in Seoul be 31°C on December 31, 2099?",
            yes_token_id="yes-full-live-v5",
            no_token_id="no-full-live-v5",
            is_weather=True,
        )
        repository.upsert_market(
            type(
                "ModuleMarket",
                (),
                {**market.__dict__, "module_id": "global_temp_bucket"},
            )(),
            {"id": market.id, "orderMinSize": "5"},
        )
        repository.save_analysis(
            Analysis(
                market_id=market.id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.35"),
                fair_upper=Decimal("0.45"),
                reference_price=Decimal("0.20"),
                edge=Decimal("0.12"),
                side="buy_yes",
                decision="trade",
                reasons=["supporting_families=4/5"],
            )
        )
        repository.update_autopilot_state(
            enabled=True,
            mode="live",
            app_mode="full_live",
        )
        connection.commit()
        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(service, "_staged_entry_headroom", lambda _market: Decimal("4"))
        monkeypatch.setattr(
            service,
            "_entry_performance_calibration",
            lambda *_args, **_kwargs: EntryPerformanceCalibration(
                entry_policy_version="weather-entry-v5",
                policy_samples=36,
                horizon="D1",
                price_band="0.10_0.24",
                horizon_samples=36,
                price_band_samples=20,
                horizon_win_rate=Decimal("0.2778"),
                price_band_win_rate=Decimal("0.30"),
                multiplier=Decimal("0.25"),
                reason="D1 event-level Brier sizing multiplier=0.25",
            ),
        )
        market_row = repository.get_market(market.id)
        analysis_row = repository.latest_analysis(market.id)

        headroom = service._risk_adjusted_entry_headroom(market_row, analysis_row)

        assert headroom == Decimal("2")
        assert (
            service._staged_entry_rejection_reason(
                market_row,
                analysis_row,
                headroom=headroom,
            )
            is None
        )
    finally:
        connection.close()


def test_autopilot_selects_best_edge_but_skips_event_with_active_sibling(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        markets = (
            ("ny-80", "New York", "80-81", "ny-event", Decimal("0.30")),
            ("ny-82", "New York", "82-83", "ny-event", Decimal("0.50")),
            ("mia-90", "Miami", "90-91", "miami-event", Decimal("0.20")),
        )
        for market_id, city, bucket, event_slug, edge in markets:
            market = Market(
                id=market_id,
                title=(
                    f"Will the highest temperature in {city} be {bucket}°F on December 31, 2099?"
                ),
                description="Resolved according to Wunderground.",
                event_slug=event_slug,
                event_title=f"Highest temperature in {city} on December 31, 2099",
                yes_token_id=f"yes-{market_id}",
                no_token_id=f"no-{market_id}",
                is_weather=True,
            )
            repository.upsert_market(
                type(
                    "ModuleMarket",
                    (),
                    {**market.__dict__, "module_id": "global_temp_bucket"},
                )(),
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
                    model_version=GLOBAL_BUCKET_MODEL_VERSION,
                    fair_lower=Decimal("0.40"),
                    fair_upper=Decimal("0.60"),
                    reference_price=Decimal("0.10"),
                    edge=edge,
                    side="buy_yes",
                    decision="trade",
                    reasons=["rank by edge"],
                )
            )

        service = AutopilotService(settings, repository, client=FakeClient())
        monkeypatch.setattr(
            service,
            "_staged_entry_headroom",
            lambda _market: Decimal("2"),
        )
        assert service._select_market() == "ny-82"

        repository.replace_positions(
            [
                {
                    "market": "ny-80",
                    "token_id": "yes-ny-80",
                    "outcome": "Yes",
                    "size": "5",
                    "current_value": "1",
                }
            ]
        )
        monkeypatch.setattr(
            service,
            "_staged_entry_headroom",
            lambda market: Decimal("0") if str(market["id"]) == "ny-80" else Decimal("2"),
        )

        assert service._select_market() == "mia-90"
    finally:
        connection.close()


def test_autopilot_action_matches_bucket_analysis_side():
    assert _action_from_analysis_side("buy_yes") == "buy_yes"
    assert _action_from_analysis_side("yes") == "buy_yes"
    assert _action_from_analysis_side("buy_no") == "buy_no"
    assert _action_from_analysis_side("no") == "buy_no"


def test_autopilot_successful_tick_clears_previous_error(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        service = AutopilotService(settings, repository, client=FakeClient())
        service.ensure_state(mode="dry_run")
        repository.update_autopilot_state(enabled=True, mode="dry_run", last_error="old failure")
        connection.commit()
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(service, "_select_market", lambda: None)

        result = service.tick()
        connection.commit()

        assert result.status == "idle"
        assert repository.get_autopilot_state()["last_error"] is None
    finally:
        connection.close()


def test_autopilot_does_not_report_rejected_intent_as_executed(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        service = AutopilotService(settings, repository, client=FakeClient())
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()

        monkeypatch.setattr(
            service,
            "collect_blockers",
            lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
        )
        monkeypatch.setattr(service, "_select_market", lambda: market_id)
        monkeypatch.setattr(service, "_prepare_market", lambda _market_id: None)
        monkeypatch.setattr(
            service,
            "_execute_live",
            lambda _market_id, _analysis: (99, ["reconciliation state is stale"]),
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
            lambda self: {"status": "ok"},
        )

        result = service.tick()

        assert result.intent_id == 99
        assert result.status == "rejected"
        assert "reconciliation state is stale" in result.reason
    finally:
        connection.close()


def test_autopilot_observe_mode_records_analysis_without_order_intent(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        service = AutopilotService(settings, repository, client=FakeClient())
        service.ensure_state(mode="dry_run")
        repository.update_autopilot_state(enabled=True, mode="dry_run", app_mode="observe")
        connection.commit()
        monkeypatch.setattr(service, "_select_market", lambda: market_id)
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )

        result = service.tick()
        connection.commit()

        assert result.status == "observed"
        assert result.action == "observe"
        assert repository.list_recent_order_intents(limit=10, market_id=market_id) == []
    finally:
        connection.close()


def test_autopilot_full_live_has_no_decorative_lock_when_ready(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        settings = settings.model_copy(
            update={
                "trading_disabled": False,
                "auto_exit_enabled": True,
                "polymarket_private_key": "k",
                "polymarket_funder": "0xf",
                "compliance_check_enabled": True,
            }
        )
        repository.save_reconciliation("ok", {"status": "ok"})
        connection.commit()
        service = AutopilotService(settings, repository, client=FakeClient())
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
        connection.commit()

        blockers = service.collect_blockers(live_mode=True, app_mode="full_live")
        assert "formal full-live mode is locked" not in blockers.items
        assert not any("locked" in item for item in blockers.items)

        checks = {c.name: c for c in service.first_run_checks(app_mode="full_live")}
        assert checks["full_live"].ok is True
        assert checks["full_live"].status == "ready"

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
        monkeypatch.setattr(
            "polymarket_weather_arb.services.compliance_service.ComplianceService.check_live_allowed",
            lambda self: type(
                "D", (), {"ok": True, "reason": "test", "status": "check_disabled"}
            )(),
        )
        monkeypatch.setattr(service, "_select_market", lambda: None)
        monkeypatch.setattr(service, "_maybe_manage_stale_orders", lambda **_k: [])
        monkeypatch.setattr(service, "_refresh_position_analyses", lambda: None)
        monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_k: (0, 0))

        result = service.tick()
        assert result.status == "idle"
        assert "formal full-live mode is locked" not in result.reason
    finally:
        connection.close()


def test_autopilot_full_live_real_blockers_still_block(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        settings = settings.model_copy(update={"trading_disabled": True, "auto_exit_enabled": True})
        service = AutopilotService(settings, repository, client=FakeClient())
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
        connection.commit()

        result = service.tick()
        assert result.status == "blocked"
        assert "TRADING_DISABLED=true" in result.reason

        settings2 = settings.model_copy(
            update={
                "trading_disabled": False,
                "auto_exit_enabled": False,
                "polymarket_private_key": "k",
                "polymarket_funder": "0xf",
            }
        )
        service2 = AutopilotService(settings2, repository, client=FakeClient())
        blockers = service2.collect_blockers(live_mode=True, app_mode="full_live")
        assert not any("AUTO_EXIT_ENABLED" in item for item in blockers.items)
    finally:
        connection.close()


def test_live_tick_refreshes_stale_reconciliation_instead_of_self_blocking(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)

    class HealthyLiveClient(FakeClient):
        def __init__(self):
            self.account_reads: list[str] = []

        def get_balances(self):
            self.account_reads.append("balances")
            return {"balance": "10", "allowances": {}}

        def get_orders(self):
            self.account_reads.append("orders")
            return []

        def get_trades(self):
            self.account_reads.append("trades")
            return []

        def get_positions(self):
            self.account_reads.append("positions")
            return []

    try:
        settings = settings.model_copy(
            update={
                "trading_disabled": False,
                "auto_exit_enabled": True,
                "polymarket_private_key": "k",
                "polymarket_funder": "0xf",
            }
        )
        repository.save_reconciliation("ok", {"status": "old-ok"})
        connection.execute(
            "UPDATE reconciliations SET created_at = ?",
            ("2020-01-01 00:00:00",),
        )
        client = HealthyLiveClient()
        service = AutopilotService(settings, repository, client=client)
        service.ensure_state(mode="live")
        repository.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
        connection.commit()

        # Readiness/snapshot callers still see the stale state.
        monkeypatch.setattr(
            "polymarket_weather_arb.services.compliance_service.ComplianceService.check_live_allowed",
            lambda self: type("D", (), {"ok": True, "reason": "test"})(),
        )
        assert (
            "reconciliation is stale"
            in service.collect_blockers(
                live_mode=True,
                app_mode="full_live",
            ).items
        )

        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(service, "_select_market", lambda: None)
        monkeypatch.setattr(service, "_maybe_manage_stale_orders", lambda **_kwargs: [])
        monkeypatch.setattr(service, "_refresh_position_analyses", lambda: None)
        monkeypatch.setattr(service, "_maybe_auto_exit", lambda **_kwargs: (0, 0))

        result = service.tick()
        connection.commit()

        assert result.status == "idle"
        assert client.account_reads == ["balances", "orders", "trades", "positions"]
        latest = repository.latest_successful_reconciliation()
        assert latest is not None
        assert latest["created_at"] != "2020-01-01 00:00:00"
    finally:
        connection.close()


class SkipLlmAdvisor(LlmAdvisorService):
    def __init__(self, settings, repository):
        super().__init__(settings, repository, client=_SkipClient())

    @property
    def enabled(self) -> bool:
        return True


class _SkipClient:
    provider = "fake"
    model = "fake"

    def complete_json(self, *, system: str, user: str) -> dict[str, object]:
        return {"action": "skip", "confidence": 0.95, "reason": "too risky"}


class FailingLlmAdvisor(LlmAdvisorService):
    @property
    def enabled(self) -> bool:
        return True

    def evaluate(self, market_id, analysis_row):
        raise RuntimeError("provider timeout")


def test_autopilot_records_llm_skip_without_vetoing_quant(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        settings = settings.model_copy(update={"llm_enabled": True, "llm_api_key": "x"})
        service = AutopilotService(
            settings,
            repository,
            client=FakeClient(),
            llm_advisor=SkipLlmAdvisor(settings, repository),
        )
        monkeypatch.setattr(service, "_select_market", lambda: market_id)
        service.ensure_state(mode="dry_run")
        repository.update_autopilot_state(enabled=True, mode="dry_run")
        connection.commit()
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )
        result = service.tick()
        connection.commit()
        assert result.status == "executed"
        latest = repository.list_autopilot_decisions(limit=1)[0]
        assert latest["llm_reason"] == "skip: too risky"
    finally:
        connection.close()


def test_autopilot_continues_when_llm_review_fails(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        service = AutopilotService(
            settings,
            repository,
            client=FakeClient(),
            llm_advisor=FailingLlmAdvisor(settings, repository),
        )
        monkeypatch.setattr(service, "_select_market", lambda: market_id)
        service.ensure_state(mode="dry_run")
        repository.update_autopilot_state(enabled=True, mode="dry_run")
        connection.commit()
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
            lambda self, **kwargs: 0,
        )
        monkeypatch.setattr(
            "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
            lambda self, **kwargs: 0,
        )

        result = service.tick()

        assert result.status == "executed"
        assert "LLM review unavailable: provider timeout" in result.reason
    finally:
        connection.close()


def test_autopilot_snapshot_reports_live_lock_without_blocking_paper(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        settings = settings.model_copy(update={"trading_disabled": True})
        service = AutopilotService(settings, repository, client=FakeClient())
        service.ensure_state()
        snapshot = service.snapshot()
        assert "TRADING_DISABLED=true" not in snapshot.blockers
        checks = {check.name: check for check in snapshot.first_run_checks}
        assert checks["trading_disabled"].ok is False
        assert checks["trading_disabled"].status == "on"
    finally:
        connection.close()


def test_autopilot_breaker_only_blocks_live(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    try:
        from polymarket_weather_arb.services.circuit_breaker_service import CircuitBreakerService

        CircuitBreakerService(repository).trip("Resolution mismatch testing")

        service = AutopilotService(settings, repository, client=FakeClient())
        service.ensure_state(mode="dry_run")
        repository.update_autopilot_state(enabled=True, mode="dry_run", app_mode="paper")
        connection.commit()

        monkeypatch.setattr(service, "_select_market", lambda: None)

        result_paper = service.tick()
        assert result_paper.status != "blocked"

        repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
        connection.commit()

        result_live = service.tick()
        assert result_live.status == "blocked"
        assert any("Resolution mismatch testing" in b for b in result_live.blockers)
    finally:
        connection.close()


def test_select_fair_analysis_groups():
    from polymarket_weather_arb.services.autopilot_service import select_fair_analysis_groups
    from datetime import datetime, timezone, timedelta

    # Use a fixed 'now'
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    today = now.date()
    d0_slug = f"{today.year}-{today.month:02d}-{today.day:02d}"
    d1 = today + timedelta(days=1)
    d1_slug = f"{d1.year}-{d1.month:02d}-{d1.day:02d}"
    d2 = today + timedelta(days=2)
    d2_slug = f"{d2.year}-{d2.month:02d}-{d2.day:02d}"
    d_minus_1 = today - timedelta(days=1)
    d_minus_1_slug = f"{d_minus_1.year}-{d_minus_1.month:02d}-{d_minus_1.day:02d}"

    groups = [
        ("shanghai", d_minus_1_slug),
        ("shanghai", d0_slug),
        ("miami", d0_slug),
        ("london", d1_slug),
        ("chicago", d1_slug),
        ("seoul", d2_slug),
    ]

    filtered = select_fair_analysis_groups(groups, rotation_slot=0, now=now)

    assert len(filtered) == 5
    assert not any(g[1] == d_minus_1_slug for g in filtered)

    # Interleaving order should be D1, D0, D2
    assert filtered[0][1] == d1_slug
    assert filtered[1][1] == d0_slug
    assert filtered[2][1] == d2_slug
    assert filtered[3][1] == d1_slug
    assert filtered[4][1] == d0_slug


def test_select_fair_analysis_groups_nyc_midnight():
    from polymarket_weather_arb.services.autopilot_service import select_fair_analysis_groups
    from datetime import datetime, timezone

    # NYC UTC midnight. In NYC it is July 13, 20:00.
    now = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
    # Local day is July 13.
    groups = [
        ("nyc", "2026-07-12"),  # D-1
        ("nyc", "2026-07-13"),  # D0
        ("nyc", "2026-07-14"),  # D1
        ("nyc", "2026-07-15"),  # D2
        ("nyc", "2026-07-16"),  # D3
    ]
    filtered = select_fair_analysis_groups(groups, rotation_slot=0, now=now)
    assert len(filtered) == 3
    dates = [g[1] for g in filtered]
    assert "2026-07-12" not in dates
    assert "2026-07-13" in dates
    assert "2026-07-14" in dates
    assert "2026-07-15" in dates
    assert "2026-07-16" not in dates


def test_select_fair_analysis_groups_taipei_paris_unknown_retained():
    from polymarket_weather_arb.services.autopilot_service import select_fair_analysis_groups
    from datetime import datetime, timezone

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    groups = [
        ("taipei", "2026-07-14"),
        ("paris", "2026-07-15"),
        ("shanghai", "2026-07-15"),  # D1
    ]
    filtered = select_fair_analysis_groups(groups, rotation_slot=0, now=now)
    # Should interleave D1 then "unknown"
    assert len(filtered) == 3
    # First is D1 because order is [1, 0, 2, "unknown"]
    assert filtered[0][0] == "shanghai"
    # Then unknown
    assert filtered[1][0] in {"taipei", "paris"}
    assert filtered[2][0] in {"taipei", "paris"}
