from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import handle_dashboard_post, render_dashboard_path
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.china_temperature_bucket import (
    parse_china_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.services.market_workflow_service import MarketWorkflowResult
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeDiscoveryService:
    def __init__(self, repo: Repository):
        self.repo = repo

    def discover(
        self, limit: int = 100, pages: int = 1, *, include_unsupported: bool = False
    ) -> int:
        assert include_unsupported is False
        market = _market()
        snapshot = MarketSnapshot(
            market_id=market.id,
            best_bid=Decimal("0.45"),
            best_ask=Decimal("0.50"),
            midpoint=Decimal("0.475"),
            spread=Decimal("0.05"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        )
        self.repo.upsert_market(market, {"id": market.id, "limit": limit, "pages": pages})
        from polymarket_weather_arb.domain.rules import parse_resolution_rule

        rule = parse_resolution_rule(market.title, market.description)
        self.repo.save_resolution_rule(market.id, rule)
        self.repo.save_market_snapshot(snapshot, {"market": market.id})
        self.repo.upsert_candidate(market.id, rule, snapshot)
        return 1


class EmptyDiscoveryService:
    def __init__(self, repo: Repository):
        self.repo = repo

    def discover(
        self, limit: int = 100, pages: int = 1, *, include_unsupported: bool = False
    ) -> int:
        return 0


class FakeWorkflowService:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.live_called = False
        self.research_called = False

    def inspect_market(self, market_id: str) -> MarketWorkflowResult:
        from polymarket_weather_arb.domain.rules import parse_resolution_rule

        market = self.repo.get_market(market_id)
        rule = parse_resolution_rule(market["title"], market["description"])
        self.repo.save_resolution_rule(market_id, rule)
        return MarketWorkflowResult(market_id, "inspected", [])

    def refresh_weather(self, market_id: str) -> MarketWorkflowResult:
        self.repo.save_forecast(_forecast(market_id), {"fake": True})
        return MarketWorkflowResult(market_id, "weather", [])

    def analyze(self, market_id: str) -> MarketWorkflowResult:
        self.repo.save_analysis(
            Analysis(
                market_id=market_id,
                model_version="test",
                fair_lower=Decimal("0.80"),
                fair_upper=Decimal("0.90"),
                reference_price=Decimal("0.50"),
                edge=Decimal("0.30"),
                side="buy_yes",
                decision="trade",
                reasons=["fake edge"],
            )
        )
        return MarketWorkflowResult(market_id, "analysis", [])

    def research_market(self, market_id: str) -> MarketWorkflowResult:
        self.research_called = True
        self.inspect_market(market_id)
        self.refresh_weather(market_id)
        self.analyze(market_id)
        return MarketWorkflowResult(market_id, "research", [])

    def dry_run_trade(self, market_id: str) -> MarketWorkflowResult:
        self.repo.connection.execute(
            """
            INSERT INTO order_intents (market_id, side, token_id, limit_price, size, notional, rationale, dry_run, status)
            VALUES (?, 'buy_yes', 'yes-token', 0.50, 10, 5, 'fake dry run', 1, 'dry_run')
            """,
            (market_id,),
        )
        return MarketWorkflowResult(market_id, "Order intent: 1", [])

    def trade_live(self, market_id: str) -> None:
        self.live_called = True


class SkippedDryRunWorkflowService(FakeWorkflowService):
    def dry_run_trade(self, market_id: str) -> MarketWorkflowResult:
        return MarketWorkflowResult(
            market_id, "Order skipped: latest analysis does not produce an executable order", []
        )


class FakeOperatorDaemon:
    def __init__(self, repo: Repository, profile):
        self.repo = repo
        self.profile = profile

    def tick(self, **kwargs):
        assert kwargs["auto_live"] is False
        assert kwargs["discover"] is False
        assert self.profile.name == "dry-run-demo"
        return SimpleNamespace(auto_live_executed_action_ids=[])


@dataclass
class FakeReconciliationService:
    repo: Repository

    def reconcile(self) -> dict[str, object]:
        result = {"status": "adapter-error", "error": "missing credentials"}
        self.repo.save_reconciliation("adapter-error", result)
        return result


def test_dashboard_discovery_candidates_and_market_pages(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    discovery = render_dashboard_path(settings, "/discovery?lang=zh")
    posted = handle_dashboard_post(
        settings,
        "/discovery/run?lang=zh",
        urlencode({"lang": "zh", "limit": "10", "pages": "1"}).encode(),
        None,
        discovery_service_factory=FakeDiscoveryService,
    )
    candidates = render_dashboard_path(settings, "/candidates?lang=zh")
    markets = render_dashboard_path(settings, "/markets?lang=en")
    detail = render_dashboard_path(settings, "/markets/m1?lang=zh")

    assert discovery.status.value == 200
    assert "市场扫描" in discovery.body
    assert "scan-modal" in discovery.body
    assert "中国温度桶" in discovery.body
    assert "max_ask" in discovery.body
    assert "扫描中..." in discovery.body
    assert posted.status.value == 303
    assert posted.headers["Location"].startswith("/candidates?")
    assert "dry_run_ready" in candidates.body
    assert "研究市场" in candidates.body
    assert "Weather Markets" in markets.body
    assert "New York" in detail.body
    assert "决策状态" in detail.body
    assert "missing signal or forecast" in detail.body
    assert "研究市场" in detail.body
    assert "高级调试操作" in detail.body
    assert "模拟交易" in detail.body


def test_dashboard_discovery_empty_result_is_not_reported_as_success(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    posted = handle_dashboard_post(
        settings,
        "/discovery/run?lang=zh",
        urlencode({"lang": "zh", "limit": "10", "pages": "1"}).encode(),
        None,
        discovery_service_factory=EmptyDiscoveryService,
    )
    page = render_dashboard_path(settings, posted.headers["Location"])

    assert posted.status.value == 303
    assert "flash.discovery_empty" in posted.headers["Location"]
    assert "扫描没有找到可操作市场" in page.body

    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        repo.upsert_market(
            Market(
                id="broad-weather",
                title="Will global temperatures be above normal in 2026?",
                description="According to NOAA.",
                is_weather=True,
            ),
            {"id": "broad-weather"},
        )
        connection.commit()
    finally:
        connection.close()

    missing_location = handle_dashboard_post(
        settings,
        "/markets/broad-weather/analyze?lang=zh",
        b"lang=zh",
        None,
    )

    assert missing_location.status.value == 303
    assert "error.market_needs_snapshot" in missing_location.headers["Location"]

    page = render_dashboard_path(settings, missing_location.headers["Location"])
    assert "市场还没有盘口快照" in page.body


def test_dashboard_dry_run_researches_market_when_analysis_is_missing(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_market(settings)

    dry_run = handle_dashboard_post(
        settings,
        "/markets/m1/trade-dry-run?lang=zh",
        urlencode({"lang": "zh", "live": "true"}).encode(),
        None,
        market_workflow_service_factory=FakeWorkflowService,
    )
    detail = render_dashboard_path(settings, "/markets/m1?lang=zh")
    orders = render_dashboard_path(settings, "/orders?lang=zh")

    assert dry_run.status.value == 303
    assert "fake edge" in detail.body
    assert "fake dry run" in orders.body


def test_dashboard_china_bucket_metadata_and_module_filters(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        market = Market(
            id="shanghai-18c",
            slug="highest-temperature-in-shanghai-on-may-10-2026-18c",
            title="Highest temperature in Shanghai on May 10?",
            description="Outcome: 18°C. Resolved according to Wunderground on 10 May '26.",
            event_slug="highest-temperature-in-shanghai-on-may-10-2026",
            event_title="Highest temperature in Shanghai on May 10?",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
        )
        module_market = type(
            "ModuleMarket", (), {**market.__dict__, "module_id": "china_temp_bucket"}
        )()
        repo.upsert_market(module_market, {"id": market.id})
        snapshot = MarketSnapshot(
            market_id=market.id,
            best_bid=Decimal("0.03"),
            best_ask=Decimal("0.04"),
            midpoint=Decimal("0.035"),
            spread=Decimal("0.01"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        )
        rule = parse_china_temperature_bucket_rule(market.title, market.description)
        repo.save_temperature_bucket_rule(market.id, rule)
        repo.save_market_snapshot(snapshot, {"market": market.id})
        repo.upsert_candidate(
            market.id,
            type("CandidateRule", (), {"tradable": True, "rejection_reason": None})(),
            snapshot,
            status="dry_run_ready",
            notes="module=china_temp_bucket; bucket=17.5-18.5C",
            module_id="china_temp_bucket",
        )
        connection.commit()
    finally:
        connection.close()

    candidates = render_dashboard_path(settings, "/candidates?lang=zh&module=china_temp_bucket")
    markets = render_dashboard_path(settings, "/markets?lang=zh&module=china_temp_bucket")
    detail = render_dashboard_path(settings, "/markets/shanghai-18c?lang=zh")

    assert candidates.status.value == 200
    assert "中国温度桶" in candidates.body
    assert "Shanghai 17.5-18.5C" in candidates.body
    assert markets.status.value == 200
    assert "Shanghai 17.5-18.5C" in markets.body
    assert "中国温度桶规则" in detail.body
    assert "Asia/Shanghai" in detail.body


def test_dashboard_dry_run_reports_skipped_when_analysis_has_no_order(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_market(settings)

    dry_run = handle_dashboard_post(
        settings,
        "/markets/m1/trade-dry-run?lang=zh",
        b"lang=zh",
        None,
        market_workflow_service_factory=SkippedDryRunWorkflowService,
    )
    page = render_dashboard_path(settings, dry_run.headers["Location"])

    assert dry_run.status.value == 303
    assert "flash.dry_run_skipped" in dry_run.headers["Location"]
    assert "模拟交易已跳过" in page.body

    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_market(settings)

    marked = handle_dashboard_post(
        settings,
        "/candidates/m1/mark?lang=zh",
        urlencode({"lang": "zh", "status": "reviewed", "notes": "looks good"}).encode(),
        None,
    )
    inspected = handle_dashboard_post(
        settings,
        "/markets/m1/inspect?lang=zh",
        b"lang=zh",
        None,
        market_workflow_service_factory=FakeWorkflowService,
    )
    refreshed = handle_dashboard_post(
        settings,
        "/markets/m1/refresh-weather?lang=zh",
        b"lang=zh",
        None,
        market_workflow_service_factory=FakeWorkflowService,
    )
    analyzed = handle_dashboard_post(
        settings,
        "/markets/m1/analyze?lang=zh",
        b"lang=zh",
        None,
        market_workflow_service_factory=FakeWorkflowService,
    )
    researched = handle_dashboard_post(
        settings,
        "/markets/m1/research?lang=zh",
        b"lang=zh",
        None,
        market_workflow_service_factory=FakeWorkflowService,
    )
    dry_run = handle_dashboard_post(
        settings,
        "/markets/m1/trade-dry-run?lang=zh",
        urlencode({"lang": "zh", "dry_run": "false", "live": "true"}).encode(),
        None,
        market_workflow_service_factory=FakeWorkflowService,
    )
    detail = render_dashboard_path(settings, "/markets/m1?lang=zh")
    orders = render_dashboard_path(settings, "/orders?lang=zh")

    assert marked.status.value == 303
    assert "reviewed" in render_dashboard_path(settings, "/candidates?lang=zh").body
    assert "looks good" in render_dashboard_path(settings, "/candidates?lang=zh").body
    assert inspected.status.value == 303
    assert refreshed.status.value == 303
    assert analyzed.status.value == 303
    assert researched.status.value == 303
    assert dry_run.status.value == 303
    assert "fake edge" in detail.body
    assert "fake dry run" in orders.body
    assert "yes / 是" in orders.body


def test_dashboard_risk_reconciliation_profiles_and_setup_pages(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_market(settings)

    reconciliation = handle_dashboard_post(
        settings,
        "/reconciliation/run?lang=zh",
        b"lang=zh",
        None,
        reconciliation_service_factory=FakeReconciliationService,
    )
    setup = handle_dashboard_post(settings, "/setup/init-db?lang=zh", b"lang=zh", None)

    risk_page = render_dashboard_path(settings, "/risk?lang=zh")
    reconciliation_page = render_dashboard_path(settings, "/reconciliation?lang=zh")
    profiles_page = render_dashboard_path(settings, "/profiles?lang=zh")
    setup_page = render_dashboard_path(settings, "/setup?lang=zh")

    assert reconciliation.status.value == 303
    assert setup.status.value == 303
    assert "风控报告" in risk_page.body
    assert "m1" in risk_page.body
    assert "adapter-error" in reconciliation_page.body
    assert "micro-live" in profiles_page.body
    assert str(settings.database_path) in setup_page.body
    assert "secret" not in setup_page.body.lower()
    # Expanded first-run /setup flow (desktop-ready).
    assert "首次设置" in setup_page.body or "Setup" in setup_page.body
    assert "csrf_token" in setup_page.body


def test_dashboard_doctor_omits_obsolete_relayer_readiness(tmp_path, monkeypatch):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()

    doctor = render_dashboard_path(settings, "/doctor?lang=zh")

    assert doctor.status.value == 200
    assert "Relayer" not in doctor.body
    assert "secret" not in doctor.body.lower()


def test_dashboard_remaining_safe_pages_and_module_aliases(tmp_path, monkeypatch):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    _seed_market(settings)
    fixture_dir = tmp_path / "fixtures" / "markets"
    fixture_dir.mkdir(parents=True)
    fixture_path = fixture_dir / "demo.json"
    fixture_path.write_text(
        json.dumps(
            {
                "raw_market": {
                    "id": "fixture-m1",
                    "question": "Will the high temperature in New York exceed 80°F on May 8, 2026?",
                    "slug": "fixture-m1",
                    "description": "According to NOAA station KNYC.",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["yes-token", "no-token"]',
                    "events": [{"category": "Climate", "tags": [{"label": "Weather"}]}],
                }
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    doctor = render_dashboard_path(settings, "/doctor?lang=zh")
    fixtures = render_dashboard_path(settings, "/fixtures?lang=zh")
    modules = render_dashboard_path(settings, "/modules?lang=zh")
    alias_markets = render_dashboard_path(settings, "/modules/weather/markets?lang=zh")
    loaded = handle_dashboard_post(
        settings,
        "/fixtures/load?lang=zh",
        urlencode({"lang": "zh", "fixture": str(fixture_path), "demo_analysis": "true"}).encode(),
        None,
    )
    tick = handle_dashboard_post(
        settings,
        "/operator/tick?lang=zh",
        urlencode({"lang": "zh", "profile": "dry-run-demo", "auto_live": "true"}).encode(),
        None,
        operator_daemon_factory=FakeOperatorDaemon,
    )

    assert doctor.status.value == 200
    assert "Doctor 检查" in doctor.body
    assert fixtures.status.value == 200
    assert "demo.json" in fixtures.body
    assert loaded.status.value == 303
    assert render_dashboard_path(settings, "/markets/fixture-m1?lang=zh").status.value == 200
    assert tick.status.value == 303
    assert "天气" in modules.body
    assert "中国温度桶" in modules.body
    assert alias_markets.status.value == 200
    assert "m1" in alias_markets.body
    assert (
        "中国温度桶"
        in render_dashboard_path(settings, "/modules/china_temp_bucket/markets?lang=zh").body
    )
    assert (
        "m1"
        not in render_dashboard_path(settings, "/modules/china_temp_bucket/markets?lang=zh").body
    )


def test_dashboard_fixture_load_rejects_path_traversal(tmp_path, monkeypatch):
    settings = Settings(DATABASE_PATH=tmp_path / "dashboard.db")
    Database(settings.database_path).init_schema()
    monkeypatch.chdir(tmp_path)

    response = handle_dashboard_post(
        settings,
        "/fixtures/load?lang=zh",
        urlencode({"lang": "zh", "fixture": "../../.env"}).encode(),
        None,
    )

    assert response.status.value == 303
    assert "level=error" in response.headers["Location"]


def _seed_market(settings: Settings) -> None:
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        market = _market()
        repo.upsert_market(market, {"id": market.id})
        from polymarket_weather_arb.domain.rules import parse_resolution_rule

        rule = parse_resolution_rule(market.title, market.description)
        repo.save_resolution_rule(market.id, rule)
        repo.upsert_candidate(market.id, rule, status="dry_run_ready")
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.45"),
                best_ask=Decimal("0.50"),
                midpoint=Decimal("0.475"),
                spread=Decimal("0.05"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )
        connection.commit()
    finally:
        connection.close()


def _market() -> Market:
    return Market(
        id="m1",
        slug="m1",
        title="Will the high temperature in New York exceed 80°F on May 8, 2026?",
        description="According to NOAA station KNYC.",
        event_slug=None,
        event_title=None,
        category="Weather",
        tags=("weather",),
        yes_token_id="yes-token",
        no_token_id="no-token",
        close_time="2026-05-08",
        status="active",
        is_weather=True,
    )


def _forecast(market_id: str) -> ForecastSnapshot:
    now = datetime.now(timezone.utc)
    return ForecastSnapshot(
        provider="fake-weather",
        variable="temperature_high",
        value=Decimal("85"),
        unit="F",
        issue_time=now,
        valid_time=now,
        market_id=market_id,
        location="New York",
        station="KNYC",
        lower_value=Decimal("83"),
        upper_value=Decimal("87"),
        fetched_at=now,
    )
