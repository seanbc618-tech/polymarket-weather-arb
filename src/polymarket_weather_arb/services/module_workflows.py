from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from polymarket_weather_arb.adapters.polymarket.client import GammaPolymarketClient
from polymarket_weather_arb.adapters.weather.open_meteo import OpenMeteoProvider
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.modules.registry import get_module
from polymarket_weather_arb.services.market_workflow_service import (
    MarketWorkflowResult,
    MarketWorkflowService,
)
from polymarket_weather_arb.storage.repositories import Repository


@dataclass(frozen=True)
class MarketReadiness:
    market_id: str
    module_id: str
    has_market: bool
    has_quote: bool
    has_signal: bool
    has_analysis: bool
    has_dry_run: bool
    next_step: str
    blockers: list[str]


class ModuleWorkflow(Protocol):
    module_id: str

    def inspect(self, market_id: str) -> MarketWorkflowResult: ...

    def refresh_signal(self, market_id: str) -> MarketWorkflowResult: ...

    def analyze(self, market_id: str) -> MarketWorkflowResult: ...

    def research(self, market_id: str) -> MarketWorkflowResult: ...

    def dry_run(self, market_id: str) -> MarketWorkflowResult: ...

    def readiness(self, market_id: str) -> MarketReadiness: ...


WorkflowServiceFactory = Callable[[Settings, Repository], MarketWorkflowService]


def resolve_module_workflow(
    settings: Settings,
    repository: Repository,
    *,
    module_id: str,
    workflow_service_factory: WorkflowServiceFactory | None = None,
) -> ModuleWorkflow:
    module = get_module(module_id)
    workflow_service = (
        workflow_service_factory(settings, repository)
        if workflow_service_factory is not None
        else MarketWorkflowService(
            settings,
            repository,
            weather_provider_factory=OpenMeteoProvider,
            polymarket_client_factory=GammaPolymarketClient,
        )
    )
    return _WorkflowAdapter(module.id, repository, workflow_service)


class _WorkflowAdapter:
    def __init__(
        self,
        module_id: str,
        repository: Repository,
        workflow_service: MarketWorkflowService,
    ) -> None:
        self.module_id = module_id
        self.repository = repository
        self.workflow_service = workflow_service

    def inspect(self, market_id: str) -> MarketWorkflowResult:
        return self.workflow_service.inspect_market(market_id)

    def refresh_signal(self, market_id: str) -> MarketWorkflowResult:
        return self.workflow_service.refresh_weather(market_id)

    def analyze(self, market_id: str) -> MarketWorkflowResult:
        return self.workflow_service.analyze(market_id)

    def research(self, market_id: str) -> MarketWorkflowResult:
        return self.workflow_service.research_market(market_id)

    def dry_run(self, market_id: str) -> MarketWorkflowResult:
        return self.workflow_service.dry_run_trade(market_id)

    def readiness(self, market_id: str) -> MarketReadiness:
        market = self.repository.get_market(market_id)
        if market is None:
            return MarketReadiness(
                market_id=market_id,
                module_id=self.module_id,
                has_market=False,
                has_quote=False,
                has_signal=False,
                has_analysis=False,
                has_dry_run=False,
                next_step="missing_market",
                blockers=["unknown market"],
            )
        has_quote = self.repository.latest_pricing_snapshot(market_id) is not None
        has_signal = self.repository.latest_forecast(market_id) is not None
        has_analysis = self.repository.latest_analysis(market_id) is not None
        has_dry_run = _has_dry_run(self.repository, market_id)
        blockers = _readiness_blockers(has_quote, has_signal, has_analysis)
        return MarketReadiness(
            market_id=market_id,
            module_id=_row_value(market, "module_id") or self.module_id,
            has_market=True,
            has_quote=has_quote,
            has_signal=has_signal,
            has_analysis=has_analysis,
            has_dry_run=has_dry_run,
            next_step=_next_step(has_quote, has_signal, has_analysis, has_dry_run),
            blockers=blockers,
        )


def _readiness_blockers(has_quote: bool, has_signal: bool, has_analysis: bool) -> list[str]:
    if not has_quote:
        return ["missing quote snapshot"]
    if not has_signal:
        return ["missing signal or forecast"]
    if not has_analysis:
        return ["missing analysis"]
    return []


def _next_step(has_quote: bool, has_signal: bool, has_analysis: bool, has_dry_run: bool) -> str:
    if not has_quote:
        return "refresh_quote"
    if not has_signal:
        return "refresh_signal"
    if not has_analysis:
        return "analyze"
    if not has_dry_run:
        return "dry_run"
    return "review"


def _has_dry_run(repository: Repository, market_id: str) -> bool:
    rows = repository.list_recent_order_intents(limit=1, market_id=market_id)
    return bool(rows and rows[0]["dry_run"])


def _row_value(row: object, key: str) -> object | None:
    if hasattr(row, "keys") and key in row.keys():
        return row[key]  # type: ignore[index]
    return getattr(row, key, None)
