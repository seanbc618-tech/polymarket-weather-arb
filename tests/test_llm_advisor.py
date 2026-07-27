from polymarket_weather_arb.domain.llm_decision import LlmGroupDecision
from polymarket_weather_arb.services.llm_advisor_service import _parse_group_decision
import json
from datetime import datetime, timezone
from decimal import Decimal


from polymarket_weather_arb.adapters.llm.anthropic_messages import AnthropicMessagesClient
from polymarket_weather_arb.adapters.llm.factory import build_llm_client, llm_runtime_label
from polymarket_weather_arb.adapters.llm.openai_compatible import OpenAICompatibleClient
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.services.fixture_service import load_market_fixture
from polymarket_weather_arb.services.llm_advisor_service import (
    LlmAdvisorService,
    _parse_decision,
    build_system_prompt,
)
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class FakeLlmClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, *, system: str, user: str) -> dict[str, object]:
        self.calls.append((system, user))
        return self.payload


def _repo(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "llm.db")
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    return settings, Repository(connection), connection


def test_build_llm_client_openai_preset(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "x.db",
        llm_enabled=True,
        llm_provider="openai",
        llm_api_key="test-key",
    )
    client = build_llm_client(settings)
    assert isinstance(client, OpenAICompatibleClient)
    assert client.provider == "openai"
    assert client.model == "gpt-4o-mini"


def test_build_llm_client_anthropic_preset(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "x.db",
        llm_enabled=True,
        llm_provider="anthropic",
        llm_api_key="test-key",
    )
    client = build_llm_client(settings)
    assert isinstance(client, AnthropicMessagesClient)


def test_build_llm_client_deepseek_uses_openai_compatible(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "x.db",
        llm_enabled=True,
        llm_provider="deepseek",
        llm_api_key="test-key",
        llm_model="deepseek-chat",
    )
    client = build_llm_client(settings)
    assert isinstance(client, OpenAICompatibleClient)
    assert client.provider == "deepseek"


def test_llm_runtime_label_missing_key(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "x.db",
        llm_enabled=True,
        llm_provider="grok",
    )
    assert llm_runtime_label(settings) == "missing_api_key"


def test_parse_decision_normalizes_invalid_action_to_skip():
    decision = _parse_decision(
        {"action": "hold", "confidence": 0.82, "reason": "strong edge"},
        provider="openai",
        model="gpt-4o-mini",
    )
    assert decision.action == "skip"
    assert decision.confidence == Decimal("0.82")


def test_build_system_prompt_requests_chinese_reason():
    prompt = build_system_prompt("zh")
    assert "简体中文" in prompt
    assert "Simplified Chinese" in prompt


def test_build_system_prompt_requests_english_reason():
    prompt = build_system_prompt("en")
    assert "concise English" in prompt
    assert "简体中文" not in prompt


def test_llm_prompt_makes_review_non_blocking():
    prompt = build_system_prompt("en")
    assert "advisory only" in prompt
    assert "cannot place, block, or modify an order" in prompt
    assert "disagreement" in prompt


def test_llm_advisor_evaluate_uses_client_payload(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        fixture = tmp_path.parent.parent / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        fixture = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "fixtures/markets/demo-weather-nyc-high-2026-05-08.json"
        )
        market_id = load_market_fixture(fixture, repository, settings, demo_analysis=True)
        analysis_row = repository.latest_analysis(market_id)
        client = FakeLlmClient({"action": "buy_yes", "confidence": 0.91, "reason": "looks good"})
        settings = settings.model_copy(update={"llm_enabled": True, "llm_api_key": "x"})
        advisor = LlmAdvisorService(settings, repository, client=client)
        decision = advisor.evaluate(market_id, analysis_row)
        assert decision is not None
        assert decision.action == "buy_yes"
        assert len(client.calls) == 1
        request = json.loads(client.calls[0][1])
        assert "quant_signal" not in request
        assert request["forecast"]
    finally:
        connection.close()


def test_group_advisor_uses_current_forecast_and_observation_evidence(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    try:
        client = FakeLlmClient(
            {
                "bucket_probabilities": [{"market_id": "m1", "yes_probability": 0.8}],
                "other_probability": 0.2,
                "confidence": 0.7,
                "reason": "current evidence",
            }
        )
        settings = settings.model_copy(update={"llm_enabled": True, "llm_api_key": "x"})
        advisor = LlmAdvisorService(settings, repository, client=client)
        now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        forecast = ForecastSnapshot(
            market_id="m1",
            provider="open-meteo-ensemble",
            variable="temperature_high",
            value=Decimal("80.5"),
            unit="F",
            issue_time=now,
            valid_time=now,
            fetched_at=now,
        )

        decision = advisor.evaluate_group(
            "new-york_2026-07-14_temperature_high_F",
            [
                {
                    "id": "m1",
                    "title": "Will the highest temperature in New York be 80-81F on July 14?",
                    "description": "Settlement source: NOAA station KNYC.",
                }
            ],
            now,
            forecast_evidence={"m1": (forecast, {"revision": "current-rev"})},
            observation_evidence={"value": 79, "unit": "F", "quality_status": "V"},
        )

        assert decision.decision == "advisory"
        request = json.loads(client.calls[0][1])
        assert request["markets"][0]["forecast"]["raw"]["revision"] == "current-rev"
        assert request["markets"][0]["rule"]["bucket_kind"] == "range"
        assert request["markets"][0]["rule"]["bucket_lower"] == 79.5
        assert request["markets"][0]["rule"]["bucket_upper"] == 81.5
        assert request["observation"]["value"] == 79
    finally:
        connection.close()


def test_openai_compatible_client_parses_json(httpx_mock):
    httpx_mock.add_response(
        json={
            "choices": [
                {"message": {"content": '{"action":"skip","confidence":0.2,"reason":"thin edge"}'}}
            ]
        }
    )
    client = OpenAICompatibleClient(
        provider="openai",
        api_base="https://api.openai.com/v1",
        api_key="k",
        model="gpt-4o-mini",
    )
    payload = client.complete_json(system="sys", user="user")
    assert payload["action"] == "skip"


def test_llm_advisor_group_decision_valid():
    decision = LlmGroupDecision(
        bucket_probabilities={"m1": Decimal("0.4"), "m2": Decimal("0.5")},
        other_probability=Decimal("0.1"),
        confidence=Decimal("0.8"),
        reason="looks good",
        provider="fake",
        model="model",
    )
    assert decision.confidence == Decimal("0.8")


def test_llm_advisor_group_decision_invalid_sum():
    import pytest

    with pytest.raises(ValueError, match="sum"):
        LlmGroupDecision(
            bucket_probabilities={"m1": Decimal("0.5"), "m2": Decimal("0.6")},
            other_probability=Decimal("0.1"),
            confidence=Decimal("0.8"),
            reason="looks good",
            provider="fake",
            model="model",
        )


def test_llm_advisor_group_decision_invalid_prob():
    import pytest

    with pytest.raises(ValueError):
        LlmGroupDecision(
            bucket_probabilities={"m1": Decimal("-0.1"), "m2": Decimal("0.5")},
            other_probability=Decimal("0.6"),
            confidence=Decimal("0.8"),
            reason="looks good",
            provider="fake",
            model="model",
        )


def test_parse_group_decision_valid():
    raw = {
        "bucket_probabilities": [
            {"market_id": "m1", "yes_probability": 0.4},
            {"market_id": "m2", "yes_probability": 0.5},
        ],
        "other_probability": 0.1,
        "confidence": 0.9,
        "reason": "r",
    }
    decision = _parse_group_decision(raw, [{"id": "m1"}, {"id": "m2"}], provider="p", model="m")
    assert decision is not None
    assert decision.confidence == Decimal("0.9")


def test_parse_group_decision_missing_market():
    raw = {
        "bucket_probabilities": [{"market_id": "m1", "yes_probability": 0.9}],
        "other_probability": 0.1,
        "confidence": 0.9,
        "reason": "r",
    }
    # sibling_markets has m2 which is missing from raw
    decision = _parse_group_decision(raw, [{"id": "m1"}, {"id": "m2"}], provider="p", model="m")
    assert decision.decision == "invalid"
    assert decision.reason.startswith("invalid response: missing or extra")


def test_parse_group_decision_extra_market():
    raw = {
        "bucket_probabilities": [
            {"market_id": "m1", "yes_probability": 0.4},
            {"market_id": "m2", "yes_probability": 0.5},
        ],
        "other_probability": 0.1,
        "confidence": 0.9,
        "reason": "r",
    }
    # sibling_markets only has m1
    decision = _parse_group_decision(raw, [{"id": "m1"}], provider="p", model="m")
    assert decision.decision == "invalid"
    assert decision.reason.startswith("invalid response: missing or extra")


def test_parse_group_decision_duplicate_market_is_invalid():
    raw = {
        "bucket_probabilities": [
            {"market_id": "m1", "yes_probability": 0.4},
            {"market_id": "m1", "yes_probability": 0.5},
            {"market_id": "m2", "yes_probability": 0.0},
        ],
        "other_probability": 0.1,
        "confidence": 0.9,
        "reason": "r",
    }

    decision = _parse_group_decision(
        raw,
        [{"id": "m1"}, {"id": "m2"}],
        provider="p",
        model="m",
    )

    assert decision.decision == "invalid"
    assert "duplicate market_id" in decision.reason
