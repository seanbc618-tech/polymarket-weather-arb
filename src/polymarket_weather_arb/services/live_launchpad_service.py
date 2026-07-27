from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from sqlite3 import Row
from urllib.parse import parse_qs, urlparse

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import build_proposed_order
from polymarket_weather_arb.domain.risk import RiskContext, RiskEngine
from polymarket_weather_arb.profiles import (
    get_profile,
    live_auto_enabled_by_override,
    settings_for_override,
    settings_for_profile,
)
from polymarket_weather_arb.services.market_workflow_service import analysis_from_row
from polymarket_weather_arb.services.live_monitor_service import is_fresh_reconciliation
from polymarket_weather_arb.services.live_readiness_service import (
    LiveReadinessCheck,
    LiveReadinessService,
)
from polymarket_weather_arb.services.calibration_service import CalibrationService
from polymarket_weather_arb.services.module_credibility_service import build_module_credibility
from polymarket_weather_arb.services.order_lifecycle_service import OrderLifecycleService
from polymarket_weather_arb.services.trading_service import age_seconds
from polymarket_weather_arb.storage.repositories import Repository

MICRO_LIVE_PREVIEW_NOTIONAL = Decimal("2")


@dataclass(frozen=True)
class LiveLaunchpadGate:
    name: str
    ok: bool
    status: str
    detail: str


@dataclass(frozen=True)
class LiveLaunchpadCandidate:
    market_id: str
    title: str
    module_id: str
    profile: str
    best_bid: str | None
    best_ask: str | None
    latest_analysis_id: int | None
    latest_dry_run_id: int | None
    max_order_usdc: str
    whitelisted: bool
    override_enabled: bool
    reconciliation_fresh: bool
    credibility_rule_status: str
    credibility_source_grade: str
    credibility_live_eligibility: str
    calibration_status: str
    calibration_total_signals: int
    calibration_resolved_signals: int
    calibration_brier_score: str | None
    calibration_hit_rate: str | None
    gates: list[LiveLaunchpadGate]
    blockers: list[str]
    can_preview: bool


@dataclass(frozen=True)
class LiveLaunchpadPreview:
    market_id: str
    side: str
    token_id: str | None
    limit_price: str
    size: str
    notional: str
    max_loss: str
    accepted: bool
    rationale: str
    risk_reasons: list[str]


@dataclass(frozen=True)
class LiveLaunchpadSnapshot:
    readiness_gates: list[LiveLaunchpadGate]
    reconciliation_status: str
    open_orders_count: int
    stale_open_orders_count: int
    open_orders_notional: str
    positions_count: int
    nonzero_positions_count: int
    position_total_exposure: str
    position_max_market_exposure: str
    position_concentration_risk: str
    position_market_exposures: dict[str, str]
    max_order_usdc: str
    max_daily_usdc: str
    max_market_usdc: str
    live_market_ids: list[str]
    candidates: list[LiveLaunchpadCandidate]
    preview: LiveLaunchpadPreview | None
    pending_live_action_id: str | None
    can_execute: bool
    blockers: list[str]


def build_live_launchpad_snapshot(
    repository: Repository,
    settings: Settings,
    *,
    check_exchange: bool = False,
    live_market_ids: set[str] | None = None,
    preview_market_id: str | None = None,
) -> LiveLaunchpadSnapshot:
    profile = get_profile("micro-live")
    effective_settings = settings_for_profile(settings, profile)
    readiness = LiveReadinessService(settings, repository, client=None).check(
        check_exchange=check_exchange
    )
    readiness_gates = [_readiness_gate(check) for check in readiness]
    latest_success = repository.latest_successful_reconciliation()
    reconciliation_fresh = is_fresh_reconciliation(latest_success)
    reconciliation_status = "fresh" if reconciliation_fresh else "missing_or_stale"
    order_lifecycle = OrderLifecycleService(_ReadOnlyOrderLifecycleClient(), repository)
    order_statistics = order_lifecycle.get_order_statistics()
    position_risk = order_lifecycle.get_position_risk_summary()
    allowed_market_ids = (
        live_market_ids if live_market_ids is not None else live_market_ids_from_settings(settings)
    )
    launchpad_candidates = [
        _candidate_from_row(
            repository,
            row,
            settings=effective_settings,
            profile_name=profile.name,
            live_market_ids=allowed_market_ids,
            reconciliation_fresh=reconciliation_fresh,
            max_order_usdc=_format_decimal(effective_settings.max_order_usdc),
        )
        for row in _candidate_rows(repository)
    ]
    preview = _preview_for_market(
        repository,
        settings=effective_settings,
        candidates=launchpad_candidates,
        profile_name=profile.name,
        reconciliation_fresh=reconciliation_fresh,
        preview_market_id=preview_market_id,
    )
    blockers = _unique(
        [gate.detail for gate in readiness_gates if not gate.ok]
        + [blocker for candidate in launchpad_candidates for blocker in candidate.blockers]
    )
    return LiveLaunchpadSnapshot(
        readiness_gates=readiness_gates,
        reconciliation_status=reconciliation_status,
        open_orders_count=int(order_statistics["total_orders"]),
        stale_open_orders_count=int(order_statistics["stale_orders"]),
        open_orders_notional=_format_decimal(order_statistics["total_notional"]),
        positions_count=int(position_risk["total_positions"]),
        nonzero_positions_count=int(position_risk["nonzero_positions"]),
        position_total_exposure=_format_decimal(position_risk["total_exposure"]),
        position_max_market_exposure=_format_decimal(position_risk["max_market_exposure"]),
        position_concentration_risk=_format_decimal(position_risk["concentration_risk"]),
        position_market_exposures={
            str(market_id): _format_decimal(exposure)
            for market_id, exposure in position_risk["market_exposures"].items()
        },
        max_order_usdc=_format_decimal(effective_settings.max_order_usdc),
        max_daily_usdc=_format_decimal(effective_settings.max_daily_usdc),
        max_market_usdc=_format_decimal(effective_settings.max_market_usdc),
        live_market_ids=sorted(allowed_market_ids),
        candidates=launchpad_candidates,
        preview=preview,
        pending_live_action_id=_pending_live_action_id(repository),
        can_execute=(preview is not None and preview.accepted and len(blockers) == 0),
        blockers=blockers,
    )


class _ReadOnlyOrderLifecycleClient:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"Live Launchpad snapshot cannot call exchange client method {name}")


def live_market_ids_from_settings(settings: Settings) -> set[str]:
    result: set[str] = set()
    for raw_item in settings.live_market_ids.replace("\n", ",").split(","):
        item = raw_item.strip()
        if not item:
            continue
        result.add(_extract_market_id(item))
    return result


def _readiness_gate(check: LiveReadinessCheck) -> LiveLaunchpadGate:
    return LiveLaunchpadGate(check.name, check.ok, check.status, check.detail)


def _candidate_rows(repository: Repository) -> list[Row]:
    rows: list[Row] = []
    for row in repository.list_markets(limit=100):
        market_id = row["id"]
        analysis = repository.latest_analysis(market_id)
        if analysis is None:
            continue
        if _latest_dry_run(repository, market_id) is None:
            continue
        # Require a quote for the analysis side (buy_no must not depend on YES).
        if (
            repository.latest_pricing_snapshot(
                market_id, side=str(analysis["side"] or "buy_yes")
            )
            is None
        ):
            continue
        rows.append(row)
    return rows


def _candidate_from_row(
    repository: Repository,
    row: Row,
    *,
    settings: Settings,
    profile_name: str,
    live_market_ids: set[str],
    reconciliation_fresh: bool,
    max_order_usdc: str,
) -> LiveLaunchpadCandidate:
    market_id = row["id"]
    analysis = repository.latest_analysis(market_id)
    snapshot = repository.latest_pricing_snapshot(
        market_id,
        side=str(analysis["side"] or "buy_yes") if analysis is not None else "buy_yes",
    )
    dry_run = _latest_dry_run(repository, market_id)
    override = repository.effective_strategy_override(market_id, profile_name)
    candidate_settings = settings_for_override(settings, override)
    override_enabled = live_auto_enabled_by_override(override)
    whitelisted = market_id in live_market_ids
    credibility = _candidate_credibility(repository, market_id, row["module_id"], analysis)
    calibration = CalibrationService(repository).trust_for_latest_signal(market_id)
    module_live_ok = credibility.live_eligibility in {
        "candidate_gate_required",
        "micro_live_ready",
    }
    gates = [
        LiveLaunchpadGate(
            "whitelist",
            whitelisted,
            "ok" if whitelisted else "blocked",
            "market is whitelisted" if whitelisted else "market is not whitelisted",
        ),
        LiveLaunchpadGate(
            "override",
            override_enabled,
            "ok" if override_enabled else "blocked",
            "live auto override is enabled"
            if override_enabled
            else "live auto override is not enabled",
        ),
        LiveLaunchpadGate(
            "reconciliation",
            reconciliation_fresh,
            "fresh" if reconciliation_fresh else "blocked",
            "fresh reconciliation is present"
            if reconciliation_fresh
            else "fresh reconciliation is missing",
        ),
        LiveLaunchpadGate(
            "analysis",
            analysis is not None,
            "ok" if analysis is not None else "blocked",
            "analysis exists" if analysis is not None else "analysis is missing",
        ),
        LiveLaunchpadGate(
            "dry_run",
            dry_run is not None,
            "ok" if dry_run is not None else "blocked",
            "dry-run exists" if dry_run is not None else "dry-run is missing",
        ),
        LiveLaunchpadGate(
            "module_credibility",
            module_live_ok,
            "ok" if module_live_ok else "blocked",
            f"module live eligibility is {credibility.live_eligibility}"
            if module_live_ok
            else f"module live eligibility is {credibility.live_eligibility}",
        ),
    ]
    blockers = [gate.detail for gate in gates if not gate.ok]
    return LiveLaunchpadCandidate(
        market_id=market_id,
        title=row["title"],
        module_id=row["module_id"],
        profile=profile_name,
        best_bid=str(snapshot["best_bid"]) if snapshot["best_bid"] is not None else None,
        best_ask=str(snapshot["best_ask"]) if snapshot["best_ask"] is not None else None,
        latest_analysis_id=analysis["id"] if analysis else None,
        latest_dry_run_id=dry_run["id"] if dry_run else None,
        max_order_usdc=_format_decimal(candidate_settings.max_order_usdc)
        if override is not None
        else max_order_usdc,
        whitelisted=whitelisted,
        override_enabled=override_enabled,
        reconciliation_fresh=reconciliation_fresh,
        credibility_rule_status=credibility.rule_status,
        credibility_source_grade=credibility.source_grade,
        credibility_live_eligibility=credibility.live_eligibility,
        calibration_status=calibration.status,
        calibration_total_signals=calibration.total_signals,
        calibration_resolved_signals=calibration.resolved_signals,
        calibration_brier_score=_format_decimal(calibration.brier_score)
        if calibration.brier_score is not None
        else None,
        calibration_hit_rate=_format_decimal(calibration.hit_rate)
        if calibration.hit_rate is not None
        else None,
        gates=gates,
        blockers=blockers,
        can_preview=not blockers,
    )


def _format_decimal(value: object) -> str:
    decimal = Decimal(str(value))
    text = format(decimal.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _latest_dry_run(repository: Repository, market_id: str) -> Row | None:
    for row in repository.list_recent_order_intents(limit=50, market_id=market_id):
        if row["dry_run"]:
            return row
    return None


def _candidate_credibility(
    repository: Repository, market_id: str, module_id: str, analysis: Row | None
):
    rule = (
        repository.get_temperature_bucket_rule(market_id)
        if module_id in {"china_temp_bucket", "global_temp_bucket"}
        else repository.get_resolution_rule(market_id)
    )
    source = rule["source"] if rule is not None and "source" in rule.keys() else None
    forecast = repository.latest_forecast(market_id)
    return build_module_credibility(
        module_id=module_id,
        rule_confidence=float(rule["confidence"])
        if rule is not None and rule["confidence"] is not None
        else None,
        source=source,
        source_grade=_source_grade(source),
        forecast_age_seconds=age_seconds(forecast["fetched_at"]) if forecast else None,
        analysis_model=analysis["model_version"] if analysis is not None else None,
    )


def _source_grade(source: str | None) -> str:
    # Heuristic for launchpad display only. Live trading still requires an
    # explicit official_forecast grade on the saved forecast payload.
    if source in {"NOAA", "NWS", "NHC", "CMA", "NMC", "Wunderground", "weather.com.cn"}:
        return "official_forecast"
    if source:
        return "research_forecast"
    return "unknown"


def _preview_for_market(
    repository: Repository,
    *,
    settings: Settings,
    candidates: list[LiveLaunchpadCandidate],
    profile_name: str,
    reconciliation_fresh: bool,
    preview_market_id: str | None,
) -> LiveLaunchpadPreview | None:
    if not preview_market_id:
        return None
    candidate = next((item for item in candidates if item.market_id == preview_market_id), None)
    if candidate is None or not candidate.can_preview:
        return None
    market = repository.get_market(candidate.market_id)
    analysis_row = repository.latest_analysis(candidate.market_id)
    snapshot_row = repository.latest_pricing_snapshot(
        candidate.market_id,
        side=str(analysis_row["side"] or "buy_yes") if analysis_row is not None else "buy_yes",
    )
    if market is None or analysis_row is None:
        return None
    override = repository.effective_strategy_override(candidate.market_id, profile_name)
    candidate_settings = settings_for_override(settings, override)
    max_notional = min(candidate_settings.max_order_usdc, MICRO_LIVE_PREVIEW_NOTIONAL)
    # Preview is non-mutating; size under the cap without failing on exchange
    # minima (those are enforced in TradingService immediately before SDK).
    order = build_proposed_order(
        analysis_from_row(analysis_row),
        market["yes_token_id"],
        market["no_token_id"],
        max_notional,
        enforce_exchange_minimum=False,
    )
    if order is None:
        return None
    risk_decision = RiskEngine(candidate_settings).evaluate(
        order,
        _risk_context(
            repository,
            market_id=candidate.market_id,
            snapshot_row=snapshot_row,
            reconciliation_fresh=reconciliation_fresh,
        ),
    )
    return LiveLaunchpadPreview(
        market_id=candidate.market_id,
        side=order.side,
        token_id=order.token_id,
        limit_price=_format_decimal(order.limit_price),
        size=_format_decimal(order.size),
        notional=_format_decimal(order.notional),
        max_loss=_format_decimal(order.cash_at_risk),
        accepted=risk_decision.accepted,
        rationale="Preview only. No live order has been placed.",
        risk_reasons=risk_decision.reasons,
    )


def _risk_context(
    repository: Repository,
    *,
    market_id: str,
    snapshot_row: Row | None,
    reconciliation_fresh: bool,
) -> RiskContext:
    forecast_row = repository.latest_forecast(market_id)
    rule_row = repository.get_resolution_rule(market_id)
    today = datetime.now(timezone.utc).date().isoformat()
    return RiskContext(
        daily_live_notional=repository.daily_order_notional(today),
        market_live_exposure=repository.market_exposure(market_id),
        order_book_age_seconds=age_seconds(snapshot_row["fetched_at"]) if snapshot_row else None,
        forecast_age_seconds=age_seconds(forecast_row["fetched_at"]) if forecast_row else None,
        rule_tradable=bool(rule_row and rule_row["tradable"]),
        unsupported_variable=bool(rule_row and rule_row["variable"] is None),
        reconciliation_fresh=reconciliation_fresh,
    )


def _extract_market_id(value: str) -> str:
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    for key in ("market_id", "marketId", "id", "tid"):
        if query.get(key):
            return query[key][0].strip()
    return value


def _pending_live_action_id(repository: Repository) -> str | None:
    row = repository.latest_action_by_status("pending")
    if row is not None and row["kind"] == "trade_live":
        return row["id"]
    return None


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
