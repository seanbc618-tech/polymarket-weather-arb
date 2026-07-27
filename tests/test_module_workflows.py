from __future__ import annotations

from types import SimpleNamespace

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.services.market_workflow_service import MarketWorkflowResult
from polymarket_weather_arb.services.module_workflows import (
    MarketReadiness,
    resolve_module_workflow,
)


class RecordingWorkflowService:
    def __init__(self):
        self.calls = []

    def inspect_market(self, market_id: str) -> MarketWorkflowResult:
        self.calls.append(("inspect_market", market_id))
        return MarketWorkflowResult(market_id, "inspected", [])

    def refresh_weather(self, market_id: str) -> MarketWorkflowResult:
        self.calls.append(("refresh_weather", market_id))
        return MarketWorkflowResult(market_id, "signal refreshed", [])

    def analyze(self, market_id: str) -> MarketWorkflowResult:
        self.calls.append(("analyze", market_id))
        return MarketWorkflowResult(market_id, "analyzed", [])

    def research_market(self, market_id: str) -> MarketWorkflowResult:
        self.calls.append(("research_market", market_id))
        return MarketWorkflowResult(market_id, "researched", [])

    def dry_run_trade(self, market_id: str) -> MarketWorkflowResult:
        self.calls.append(("dry_run_trade", market_id))
        return MarketWorkflowResult(market_id, "dry run", [])


class ReadinessRepo:
    def __init__(self):
        self.market = SimpleNamespace(id="m1", module_id="china_temp_bucket")

    def get_market(self, market_id: str):
        return self.market if market_id == "m1" else None

    def latest_market_snapshot(self, market_id: str):
        return {"market_id": market_id}

    def latest_pricing_snapshot(self, market_id: str, **_kwargs):
        return self.latest_market_snapshot(market_id)

    def latest_forecast(self, market_id: str):
        return None

    def latest_analysis(self, market_id: str):
        return None

    def list_recent_order_intents(self, limit: int = 1, market_id: str | None = None):
        return []


def test_resolve_module_workflow_delegates_standard_actions():
    service = RecordingWorkflowService()

    workflow = resolve_module_workflow(
        Settings(),
        ReadinessRepo(),
        module_id="china_temp_bucket",
        workflow_service_factory=lambda _settings, _repository: service,
    )

    assert workflow.module_id == "china_temp_bucket"
    assert workflow.inspect("m1").summary == "inspected"
    assert workflow.refresh_signal("m1").summary == "signal refreshed"
    assert workflow.analyze("m1").summary == "analyzed"
    assert workflow.research("m1").summary == "researched"
    assert workflow.dry_run("m1").summary == "dry run"
    assert service.calls == [
        ("inspect_market", "m1"),
        ("refresh_weather", "m1"),
        ("analyze", "m1"),
        ("research_market", "m1"),
        ("dry_run_trade", "m1"),
    ]


def test_module_workflow_readiness_is_read_only_and_stage_based():
    workflow = resolve_module_workflow(
        Settings(),
        ReadinessRepo(),
        module_id="china_temp_bucket",
        workflow_service_factory=lambda _settings, _repository: RecordingWorkflowService(),
    )

    readiness = workflow.readiness("m1")

    assert readiness == MarketReadiness(
        market_id="m1",
        module_id="china_temp_bucket",
        has_market=True,
        has_quote=True,
        has_signal=False,
        has_analysis=False,
        has_dry_run=False,
        next_step="refresh_signal",
        blockers=["missing signal or forecast"],
    )


def test_resolve_module_workflow_rejects_unknown_module():
    with pytest.raises(ValueError, match="unknown module"):
        resolve_module_workflow(
            Settings(),
            ReadinessRepo(),
            module_id="unknown",
            workflow_service_factory=lambda _settings, _repository: RecordingWorkflowService(),
        )
