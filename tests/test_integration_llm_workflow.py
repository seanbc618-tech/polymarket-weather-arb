from decimal import Decimal
from datetime import datetime, timedelta, timezone
import json
from dataclasses import replace

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.services.llm_advisor_service import LlmAdvisorService
from polymarket_weather_arb.domain.llm_decision import LlmGroupDecision
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.services.calibration_service import CalibrationService
from polymarket_weather_arb.services.market_workflow_service import _stable_forecast_revision


class CandidateRule:
    tradable = True
    rejection_reason = None


class FakeWeatherProvider:
    name = "fake"
    revision = "rev-1"

    def fetch_forecast(self, market_id, rule):
        return (
            ForecastSnapshot(
                provider="noaa",
                variable="temperature_high",
                value=Decimal("80.5"),
                lower_value=None,
                upper_value=None,
                unit="F",
                valid_time=datetime(2026, 7, 14, tzinfo=timezone.utc),
                issue_time=datetime(2026, 7, 13, 12, tzinfo=timezone.utc),
                fetched_at=datetime(2026, 7, 13, 12, tzinfo=timezone.utc),
            ),
            {
                "model_members": {"noaa": [80.5], "ecmwf": [80.3], "gfs": [81.0]},
                "revision": self.revision,
            },
        )


class FakeClient(PolymarketClient):
    def __init__(self, settings):
        self.settings = settings


class FakeLlmAdvisor(LlmAdvisorService):
    def __init__(self, settings, repository):
        super().__init__(settings, repository)
        self.call_count = 0
        self.last_forecast_evidence = None

    @property
    def enabled(self):
        return True

    @property
    def provider(self):
        return "openai"

    @property
    def model(self):
        return "gpt-4o"

    def evaluate_group(self, event_identity, sibling_markets, now, **kwargs):
        self.call_count += 1
        self.last_forecast_evidence = kwargs.get("forecast_evidence")
        return LlmGroupDecision(
            bucket_probabilities={"nyc-80-81f": Decimal("0.8"), "nyc-82-83f": Decimal("0.2")},
            other_probability=Decimal("0.0"),
            confidence=Decimal("0.9"),
            reason="Integration test",
            provider="openai",
            model="gpt-4o",
            raw_response="{}",
            decision="advisory",
        )


class InvalidLlmAdvisor(FakeLlmAdvisor):
    def evaluate_group(self, event_identity, sibling_markets, now, **kwargs):
        self.call_count += 1
        return LlmGroupDecision(
            bucket_probabilities={},
            other_probability=Decimal("0"),
            confidence=Decimal("0"),
            reason="invalid response",
            provider=self.provider,
            model=self.model,
            decision="invalid",
        )


def test_forecast_revision_is_stable_for_identical_evidence() -> None:
    first = FakeWeatherProvider().fetch_forecast("m1", CandidateRule())[0]
    later_fetch = replace(first, fetched_at=first.fetched_at + timedelta(minutes=10))
    payload = {"model_members": {"gfs": [80.5], "ecmwf": [80.3]}}

    assert _stable_forecast_revision(first, payload) == _stable_forecast_revision(
        later_fetch, payload
    )
    assert _stable_forecast_revision(first, payload) != _stable_forecast_revision(
        first, {"model_members": {"gfs": [81.5], "ecmwf": [80.3]}}
    )


def test_full_llm_autopilot_integration(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / "integration.db", MAX_ORDER_USDC=Decimal("1"))
    Database(settings.database_path).init_schema()
    connection = Database(settings.database_path).connect()
    repo = Repository(connection)

    # 1. Seed two bucket markets
    for market_id, bucket in (("nyc-80-81f", "80-81F"), ("nyc-82-83f", "82-83F")):
        market = Market(
            id=market_id,
            title=f"Will the high temperature in New York be {bucket} on July 14, 2026?",
            description="Settlement source: NOAA station KNYC.",
            yes_token_id=f"yes-{market_id}",
            no_token_id=f"no-{market_id}",
            is_weather=True,
        )
        repo.upsert_market(
            type("ModuleMarket", (), {**market.__dict__, "module_id": "global_temp_bucket"})(),
            {"id": market.id},
        )
        repo.save_market_snapshot(
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.05"),
                best_ask=Decimal("0.85"),
                midpoint=Decimal("0.45"),
                spread=Decimal("0.01"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"market": market.id},
        )
        repo.upsert_candidate(
            market.id, CandidateRule(), status="dry_run_ready", module_id="global_temp_bucket"
        )

    # 2. Add some fake calibration history for the LLM to earn a weight of 0.5
    # (Needs 100+ events, Brier <= 0.24, hit rate >= 0.52 for max weight 0.5)
    for i in range(101):
        m_id = f"hist_m_{i}"
        market = Market(id=m_id, title=m_id, yes_token_id=f"y_{m_id}", no_token_id=f"n_{m_id}")
        repo.upsert_market(
            type("ModuleMarket", (), {**market.__dict__, "module_id": "global_temp_bucket"})(),
            {"id": market.id},
        )
        payload = {
            "event_identity": f"hist_{i}",
            "forecast_revision": "rev",
            "confidence": 0.9,
            "reason": "",
            "horizon": "D1",
        }
        repo.connection.execute(
            """INSERT INTO model_signals (market_id, model_version, forecast_provider, source_grade, yes_probability, fair_lower, fair_upper, edge, decision, outcome_status, resolved_outcome, raw_payload, created_at)
               VALUES (?, 'llm-weather-vote-v1', 'llm:openai:gpt-4o', 'research_forecast', 1.0, 1.0, 1.0, 0, 'advisory', 'resolved', 'yes', ?, ?)""",
            (m_id, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )
    repo.connection.commit()

    # 3. Spin up autopilot
    llm_advisor = FakeLlmAdvisor(settings, repo)
    autopilot = AutopilotService(
        settings, repo, client=FakeClient(settings), llm_advisor=llm_advisor
    )

    # 4. Trigger the workflow batch manually to avoid threading issues in test
    # This invokes Workflow which asks LLM -> persists signal -> estimates multi-model -> blends
    autopilot.workflow.weather_provider_factory = lambda: FakeWeatherProvider()

    # Run the batch logic
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    analyzed, failures = autopilot.workflow.research_global_bucket_batch(
        ["nyc-80-81f", "nyc-82-83f"], now=now
    )

    assert failures == []
    assert analyzed == 2
    assert llm_advisor.call_count == 1
    assert llm_advisor.last_forecast_evidence["nyc-80-81f"][1]["revision"] == "rev-1"

    # 5. Check LLM signal persistence
    CalibrationService(repo).trust_for_latest_signal("nyc-80-81f")
    sig1 = repo.latest_model_signal("nyc-80-81f", "llm-weather-vote-v1")
    assert sig1 is not None
    assert sig1["decision"] == "advisory"
    assert sig1["yes_probability"] == 0.8

    # 6. Check Analysis result to ensure blended probability is correct
    analysis = repo.latest_analysis("nyc-80-81f")
    assert analysis is not None

    # The models are NOAA (80.5), ECMWF (80.3), GFS (81.0)
    # The "80-81F" bucket covers these.
    # LLM gave 0.8 for 80-81F.
    # NOAA and GFS share the NCEP source family, so v8 has two independent
    # families here. This remains an explicit research invocation; production
    # Autopilot passes allow_llm=False.

    reasons = json.loads(analysis["reasons"])
    assert any("blend_ratio" in r for r in reasons)
    assert any("against 2 base models" in r for r in reasons)
    assert any("weight=0.50" in r for r in reasons)

    # A second pass over identical evidence reuses the persisted revision even
    # though the latest generic model signal is now the quant analysis.
    analyzed, failures = autopilot.workflow.research_global_bucket_batch(
        ["nyc-80-81f", "nyc-82-83f"], now=now + timedelta(minutes=5)
    )
    assert failures == []
    assert analyzed == 2
    assert llm_advisor.call_count == 1
    reasons = json.loads(repo.latest_analysis("nyc-80-81f")["reasons"])
    assert any("LLM vote status=advisory" in reason for reason in reasons)
    assert any("blend_ratio" in reason for reason in reasons)

    # The same old revision becomes calibration-only once its signal is stale.
    autopilot.workflow.research_global_bucket_batch(
        ["nyc-80-81f", "nyc-82-83f"], now=now + timedelta(hours=7)
    )
    reasons = json.loads(repo.latest_analysis("nyc-80-81f")["reasons"])
    assert not any("applied external_probability" in reason for reason in reasons)
    assert any("LLM vote status=stale" in reason for reason in reasons)

    # A malformed review is audited but must never become a zero-probability vote.
    FakeWeatherProvider.revision = "rev-2"
    invalid_advisor = InvalidLlmAdvisor(settings, repo)
    autopilot.workflow.llm_advisor = invalid_advisor
    autopilot.workflow.research_global_bucket_batch(
        ["nyc-80-81f", "nyc-82-83f"], now=now + timedelta(hours=7, minutes=1)
    )
    reasons = json.loads(repo.latest_analysis("nyc-80-81f")["reasons"])
    assert invalid_advisor.call_count == 1
    assert not any("applied external_probability" in reason for reason in reasons)
    assert any("LLM vote status=invalid" in reason for reason in reasons)
    invalid_signal = repo.latest_model_signal(
        "nyc-80-81f",
        "llm-weather-vote-v1",
        forecast_provider="llm:openai:gpt-4o",
    )
    assert invalid_signal["decision"] == "invalid"
    trust = CalibrationService(repo).trust_for_model(
        model_version="llm-weather-vote-v1",
        forecast_provider="llm:openai:gpt-4o",
    )
    assert trust.malformed_rate > 0
    assert trust.effective_weight == Decimal("0.50")
