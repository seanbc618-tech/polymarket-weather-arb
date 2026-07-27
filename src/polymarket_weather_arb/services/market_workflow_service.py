from __future__ import annotations

import concurrent.futures
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlite3 import Row
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polymarket_weather_arb.adapters.polymarket.base import PolymarketClient
from polymarket_weather_arb.adapters.weather.base import WeatherProvider
from polymarket_weather_arb.adapters.weather.awc_metar import (
    AWC_TAF_STALE_SECONDS,
    AwcMetarProvider,
    AwcTafProvider,
)
from polymarket_weather_arb.adapters.weather.china_official import ChinaOfficialWeatherProvider
from polymarket_weather_arb.adapters.weather.google_weather import GoogleWeatherProvider
from polymarket_weather_arb.adapters.weather.noaa import NoaaProvider
from polymarket_weather_arb.adapters.weather.open_meteo import OpenMeteoProvider
from polymarket_weather_arb.adapters.weather.open_meteo_ensemble import (
    OpenMeteoEnsembleProvider,
)
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.china_bucket_pricing import (
    ChinaBucketPricingConfig,
    analyze_china_bucket_price,
)
from polymarket_weather_arb.domain.china_temperature_bucket import (
    ChinaTemperatureBucketRule,
    parse_china_temperature_bucket_rule,
)
from polymarket_weather_arb.domain.fees import extract_market_fee_schedule
from polymarket_weather_arb.domain.global_bucket_pricing import (
    GlobalBucketPricingConfig,
    apply_temperature_biases,
    analyze_global_bucket_price,
    global_bucket_model_probabilities,
    global_bucket_top_candidate_votes,
    weather_source_family,
)
from polymarket_weather_arb.domain.global_temperature_bucket import (
    GlobalTemperatureBucketRule,
    parse_global_temperature_bucket_rule,
    settlement_bucket_bounds,
    with_settlement_timezone,
)
from polymarket_weather_arb.domain.market_eligibility import resolve_market_timezone
from polymarket_weather_arb.domain.llm_decision import LLM_WEATHER_MODEL_VERSION
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.risk import RiskContext
from polymarket_weather_arb.domain.rules import (
    ResolutionRule,
    enrich_rule_from_market_title,
    parse_resolution_rule,
)
from polymarket_weather_arb.domain.source_grade import (
    SETTLEMENT_OBSERVATION,
    normalize_source_grade,
)
from polymarket_weather_arb.domain.weather import (
    ForecastSnapshot,
    WeatherObservation,
    normalize_value,
)
from polymarket_weather_arb.services.analysis_service import AnalysisService, snapshot_from_row
from polymarket_weather_arb.services.ensemble_workflow import EnsembleWorkflow
from polymarket_weather_arb.services.trading_service import TradingService, age_seconds
from polymarket_weather_arb.storage.repositories import Repository


class PolymarketClientFactory(Protocol):
    def __call__(self, settings: Settings) -> PolymarketClient: ...


class WeatherProviderFactory(Protocol):
    def __call__(self) -> WeatherProvider: ...


class ChinaWeatherProviderFactory(Protocol):
    def __call__(self) -> ChinaOfficialWeatherProvider: ...


class ObservationProviderFactory(Protocol):
    def __call__(self) -> NoaaProvider: ...


class AwcObservationProviderFactory(Protocol):
    def __call__(self) -> AwcMetarProvider: ...


class AwcForecastProviderFactory(Protocol):
    def __call__(self) -> AwcTafProvider: ...


@dataclass(frozen=True)
class MarketWorkflowResult:
    market_id: str
    summary: str
    details: list[str]


@dataclass(frozen=True)
class RiskReport:
    daily_live_notional: Decimal
    exposures: list[tuple[str, Decimal]]


@dataclass(frozen=True)
class D0ObservationContext:
    observation: WeatherObservation | None = None
    raw_payload: dict[str, object] | None = None
    block_reason: str | None = None


D0_OBSERVATION_MAX_AGE = timedelta(hours=2)
GLOBAL_WEATHER_D0_CACHE_TTL = timedelta(hours=2)
GLOBAL_WEATHER_LATER_CACHE_TTL = timedelta(hours=6)
GLOBAL_WEATHER_STALE_IF_ERROR = timedelta(hours=12)
BUCKET_SWITCH_MIN_TARGET_PROBABILITY = Decimal("0.40")
BUCKET_SWITCH_MIN_PROBABILITY_ADVANTAGE = Decimal("0.10")
BUCKET_SWITCH_MIN_EDGE_ADVANTAGE = Decimal("0.15")


class MarketWorkflowService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        weather_provider_factory: WeatherProviderFactory,
        polymarket_client_factory: PolymarketClientFactory,
        china_weather_provider_factory: ChinaWeatherProviderFactory | None = None,
        observation_provider_factory: ObservationProviderFactory | None = None,
        awc_observation_provider_factory: AwcObservationProviderFactory | None = None,
        awc_forecast_provider_factory: AwcForecastProviderFactory | None = None,
        llm_advisor: Any | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.weather_provider_factory = weather_provider_factory
        self.polymarket_client_factory = polymarket_client_factory
        self.llm_advisor = llm_advisor
        if china_weather_provider_factory is not None:
            self.china_weather_provider_factory = china_weather_provider_factory
        elif hasattr(ChinaOfficialWeatherProvider, "from_settings"):
            self.china_weather_provider_factory = lambda: (
                ChinaOfficialWeatherProvider.from_settings(settings)
            )
        else:
            self.china_weather_provider_factory = ChinaOfficialWeatherProvider
        self._default_observation_provider = observation_provider_factory is None
        self.observation_provider_factory = observation_provider_factory or NoaaProvider
        self.awc_observation_provider_factory = awc_observation_provider_factory or AwcMetarProvider
        self.awc_forecast_provider_factory = awc_forecast_provider_factory or AwcTafProvider

    def inspect_market(self, market_id: str) -> MarketWorkflowResult:
        market = self._market(market_id)
        module_id = _module_id(market)
        if module_id == "china_temp_bucket":
            rule = parse_china_temperature_bucket_rule(market["title"], market["description"])
            if rule.tradable:
                self.repository.save_temperature_bucket_rule(market_id, rule)
            self.repository.upsert_candidate(
                market_id,
                _candidate_rule(rule),
                self.repository.latest_pricing_snapshot(market_id),
                status="dry_run_ready" if rule.tradable else "needs_review",
                notes=_china_rule_summary(rule),
                module_id="china_temp_bucket",
            )
            status = "tradable" if rule.tradable else "needs_review"
            return MarketWorkflowResult(
                market_id, f"China bucket rule {status}", [_china_rule_summary(rule)]
            )
        if module_id == "global_temp_bucket":
            rule = self._global_rule(market_id, market)
            self.repository.upsert_candidate(
                market_id,
                _candidate_rule(rule),
                self.repository.latest_pricing_snapshot(market_id),
                status="dry_run_ready" if rule.tradable else "needs_review",
                notes=_global_rule_summary(rule),
                module_id="global_temp_bucket",
            )
            status = "tradable" if rule.tradable else "needs_review"
            return MarketWorkflowResult(
                market_id, f"Global bucket rule {status}", [_global_rule_summary(rule)]
            )
        rule = parse_resolution_rule(market["title"], market["description"])
        self.repository.save_resolution_rule(market_id, rule)
        status = "tradable" if rule.tradable else "rejected"
        return MarketWorkflowResult(
            market_id=market_id,
            summary=f"rule {status}",
            details=[_rule_summary(rule)],
        )

    def refresh_weather(self, market_id: str) -> MarketWorkflowResult:
        market = self._market(market_id)
        module_id = _module_id(market)
        if module_id == "china_temp_bucket":
            rule = self._china_rule(market_id, market)
            forecast = self._refresh_china_weather(market_id, rule)
            return MarketWorkflowResult(
                market_id=market_id,
                summary=f"China forecast refreshed from {forecast.provider}",
                details=[f"{forecast.variable}={forecast.value}{forecast.unit}"],
            )
        if module_id == "global_temp_bucket":
            rule = self._global_rule(market_id, market)
            forecast = self._refresh_global_weather(market_id, rule)
            return MarketWorkflowResult(
                market_id=market_id,
                summary=f"Global bucket forecast refreshed from {forecast.provider}",
                details=[f"{forecast.variable}={forecast.value}{forecast.unit}"],
            )
        rule = parse_resolution_rule(market["title"], market["description"])
        self.repository.save_resolution_rule(market_id, rule)

        # Check if ensemble provider is enabled
        if self._should_use_ensemble(rule):
            ensemble_workflow = EnsembleWorkflow(self.settings, self.repository)
            snapshot = ensemble_workflow.refresh_weather(market_id, rule)
            return MarketWorkflowResult(
                market_id=market_id,
                summary="Ensemble forecast refreshed from open-meteo-ensemble",
                details=[
                    f"{snapshot.variable}={snapshot.mean:.1f}±{snapshot.std:.1f}{snapshot.unit}",
                    f"members={snapshot.member_count}",
                    "source_grade=research_forecast",
                ],
            )

        forecast = AnalysisService(
            self.settings, self.weather_provider_factory(), self.repository
        ).refresh_weather(market_id, rule)
        return MarketWorkflowResult(
            market_id=market_id,
            summary=f"forecast refreshed from {forecast.provider}",
            details=[f"{forecast.variable}={forecast.value}{forecast.unit}"],
        )

    def analyze(self, market_id: str) -> MarketWorkflowResult:
        market = self._market(market_id)
        module_id = _module_id(market)
        if module_id == "china_temp_bucket":
            analysis = self._analyze_china_market(market_id, self._china_rule(market_id, market))
        elif module_id == "global_temp_bucket":
            analysis = self._analyze_global_bucket_market(
                market_id, self._global_rule(market_id, market)
            )
        else:
            analysis = self._analyze_market(market_id, market)
        return MarketWorkflowResult(
            market_id=market_id,
            summary=f"Decision: {analysis.decision} side={analysis.side} edge={analysis.edge}",
            details=analysis.reasons,
        )

    def research_market(self, market_id: str) -> MarketWorkflowResult:
        market = self._market(market_id)
        module_id = _module_id(market)
        if module_id == "china_temp_bucket":
            rule = self._china_rule(market_id, market)
            if not rule.tradable:
                self.repository.upsert_candidate(
                    market_id,
                    _candidate_rule(rule),
                    status="needs_review",
                    notes=rule.rejection_reason,
                    module_id="china_temp_bucket",
                )
                raise ValueError(
                    rule.rejection_reason or "market is not supported by this analyzer"
                )
            analysis = self._analyze_china_market(market_id, rule)
            self.repository.upsert_candidate(
                market_id,
                _candidate_rule(rule),
                self.repository.latest_pricing_snapshot(market_id),
                status="dry_run_ready",
                notes=_china_rule_summary(rule),
                module_id="china_temp_bucket",
            )
            return MarketWorkflowResult(
                market_id=market_id,
                summary=f"China research complete: {analysis.decision} side={analysis.side} edge={analysis.edge}",
                details=analysis.reasons,
            )
        if module_id == "global_temp_bucket":
            rule = self._global_rule(market_id, market)
            if not rule.tradable:
                self.repository.upsert_candidate(
                    market_id,
                    _candidate_rule(rule),
                    status="needs_review",
                    notes=rule.rejection_reason,
                    module_id="global_temp_bucket",
                )
                raise ValueError(
                    rule.rejection_reason or "market is not supported by this analyzer"
                )
            analysis = self._analyze_global_bucket_market(market_id, rule)
            self.repository.upsert_candidate(
                market_id,
                _candidate_rule(rule),
                self.repository.latest_pricing_snapshot(market_id),
                status="dry_run_ready",
                notes=_global_rule_summary(rule),
                module_id="global_temp_bucket",
            )
            return MarketWorkflowResult(
                market_id=market_id,
                summary=f"Global bucket research complete: {analysis.decision} side={analysis.side} edge={analysis.edge}",
                details=analysis.reasons,
            )
        rule = parse_resolution_rule(market["title"], market["description"])
        self.repository.save_resolution_rule(market_id, rule)
        if not rule.tradable:
            self.repository.upsert_candidate(
                market_id, rule, status="needs_review", notes=rule.rejection_reason
            )
            raise ValueError(rule.rejection_reason or "market is not supported by this analyzer")
        analysis = self._analyze_market(market_id, market)
        self.repository.upsert_candidate(
            market_id,
            rule,
            self.repository.latest_pricing_snapshot(market_id),
            status="dry_run_ready",
        )
        return MarketWorkflowResult(
            market_id=market_id,
            summary=f"Research complete: {analysis.decision} side={analysis.side} edge={analysis.edge}",
            details=analysis.reasons,
        )

    def _analyze_market(self, market_id: str, market: Row) -> Analysis:
        snapshot_row = self.repository.latest_pricing_snapshot(market_id)
        if snapshot_row is None:
            raise ValueError(f"market has no order book snapshot: {market_id}")
        rule = enrich_rule_from_market_title(
            parse_resolution_rule(market["title"], market["description"]),
            market["title"],
        )
        self.repository.save_resolution_rule(market_id, rule)

        # Check if ensemble provider is enabled (for research/dry-run only)
        if self._should_use_ensemble(rule):
            ensemble_workflow = EnsembleWorkflow(self.settings, self.repository)
            return ensemble_workflow.analyze(market_id, rule, snapshot_row)

        service = AnalysisService(self.settings, self.weather_provider_factory(), self.repository)
        forecast = service.refresh_weather(market_id, rule)
        return service.analyze(market_id, rule, forecast, snapshot_from_row(snapshot_row))

    def _should_use_ensemble(self, rule: ResolutionRule) -> bool:
        """Check if ensemble provider should be used for this rule.

        Ensemble is only used for:
        - temperature_high or temperature_low variables
        - When WEATHER_PROVIDER is set to 'open-meteo-ensemble'

        Returns:
            True if ensemble should be used, False otherwise
        """
        if self.settings.weather_provider != "open-meteo-ensemble":
            return False
        if rule.variable not in ("temperature_high", "temperature_low"):
            return False
        return True

    def _analyze_china_market(self, market_id: str, rule: ChinaTemperatureBucketRule) -> Analysis:
        snapshot_row = self.repository.latest_pricing_snapshot(market_id)
        if snapshot_row is None:
            raise ValueError(f"market has no order book snapshot: {market_id}")
        forecast = self._refresh_china_weather(market_id, rule)
        snapshot = snapshot_from_row(snapshot_row)
        analysis = analyze_china_bucket_price(
            market_id,
            rule,
            forecast,
            snapshot.best_ask,
            ChinaBucketPricingConfig(
                min_edge=self.settings.min_edge, slippage_buffer=self.settings.slippage_buffer
            ),
        )
        self.repository.save_analysis(analysis)
        return analysis

    def _analyze_global_bucket_market(
        self, market_id: str, rule: GlobalTemperatureBucketRule
    ) -> Analysis:
        snapshot_row = self.repository.latest_pricing_snapshot(market_id)
        if snapshot_row is None:
            raise ValueError(f"market has no order book snapshot: {market_id}")
        now = datetime.now(timezone.utc)
        d0 = self._d0_observation_context(market_id, rule, now=now)
        if d0.block_reason:
            return self._save_global_bucket_guard_rejection(market_id, d0.block_reason)
        forecast, forecast_payload = self._refresh_global_weather(market_id, rule)
        if d0.observation is not None:
            forecast, forecast_payload = self._attach_d0_hourly_context(
                (forecast, forecast_payload),
                rule=rule,
                observation_context=d0,
                now=now,
            )
            self._save_global_forecast_if_changed(forecast, forecast_payload)
        if d0.observation is not None and d0.raw_payload is not None:
            self.repository.save_observation(d0.observation, d0.raw_payload)
        return self._price_global_bucket_market(
            market_id,
            rule,
            forecast,
            forecast_payload,
            now=now,
            observation=d0.observation,
        )

    def _price_global_bucket_market(
        self,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
        forecast: ForecastSnapshot,
        forecast_payload: dict[str, Any],
        *,
        now: datetime | None = None,
        observation: WeatherObservation | None = None,
        external_probability: Decimal | None = None,
        external_weight: Decimal = Decimal("0"),
        extra_reasons: list[str] | None = None,
        source_calibration_cache: dict[tuple[str, str, str, tuple[str, ...]], Any] | None = None,
        source_bias_cache: dict[tuple[str, str, str, str, str, tuple[str, ...]], Any] | None = None,
        sibling_rules: dict[str, GlobalTemperatureBucketRule] | None = None,
    ) -> Analysis:
        snapshot_row = self.repository.latest_pricing_snapshot(market_id)
        if snapshot_row is None:
            raise ValueError(f"market has no order book snapshot: {market_id}")
        snapshot = snapshot_from_row(snapshot_row)
        market_row = self.repository.get_market(market_id)
        market_payload: dict[str, Any] = {}
        if market_row is not None:
            try:
                parsed_payload = json.loads(market_row["raw_payload"])
            except (TypeError, json.JSONDecodeError):
                parsed_payload = {}
            if isinstance(parsed_payload, dict):
                market_payload = parsed_payload
        fee_schedule = extract_market_fee_schedule(market_payload)

        effective_now = now or datetime.now(timezone.utc)
        raw_models = forecast_payload.get("model_members") if forecast_payload else None
        model_members = None
        taf_reference_usable, taf_reference_status = _awc_taf_reference_state(
            forecast_payload,
            now=effective_now,
        )
        if isinstance(raw_models, dict):
            model_members = {}
            for m, raw_members in raw_models.items():
                if isinstance(raw_members, list) and raw_members:
                    model_members[str(m)] = [Decimal(str(v)) for v in raw_members if v is not None]
            if not taf_reference_usable:
                model_members.pop("reference_awc-taf", None)
            if not model_members:
                model_members = None

        raw_model_members = model_members
        source_weights: dict[str, Decimal] = {}
        source_biases: dict[str, Decimal] = {}
        bias_reason = "no model members available for source bias correction"
        calibration_reason = "no model members available for source calibration"
        horizon = _forecast_horizon(rule, effective_now)
        d0_hourly_payload = forecast_payload.get("d0_hourly_context")
        calibration_phase = _forecast_calibration_phase(
            rule,
            effective_now,
            d0_hourly_payload if isinstance(d0_hourly_payload, dict) else None,
        )
        lead_hours = _forecast_lead_hours(
            rule,
            effective_now,
            d0_hourly_payload if isinstance(d0_hourly_payload, dict) else None,
        )
        calibration_providers = list(model_members or {})
        if isinstance(d0_hourly_payload, dict):
            calibration_providers.append("reference_hourly-open-meteo")
        if model_members:
            from polymarket_weather_arb.services.calibration_service import CalibrationService

            calibration_key = (
                str(rule.location or rule.station or "unknown").casefold(),
                horizon,
                calibration_phase,
                tuple(sorted(calibration_providers)),
            )
            calibration = (
                source_calibration_cache.get(calibration_key)
                if source_calibration_cache is not None
                else None
            )
            if calibration is None:
                calibration = CalibrationService(self.repository).weather_source_weights(
                    city=str(rule.location or rule.station or "unknown"),
                    horizon=horizon,
                    providers=calibration_providers,
                    calibration_phase=calibration_phase,
                )
                if source_calibration_cache is not None:
                    source_calibration_cache[calibration_key] = calibration
            source_weights = calibration.weights
            calibration_reason = calibration.reason
            bias_key = (
                str(rule.location or "unknown").casefold(),
                str(rule.station or "").casefold(),
                horizon,
                calibration_phase,
                str(rule.unit).upper(),
                tuple(sorted(calibration_providers)),
            )
            bias_calibration = (
                source_bias_cache.get(bias_key) if source_bias_cache is not None else None
            )
            if bias_calibration is None:
                bias_calibration = CalibrationService(self.repository).weather_source_biases(
                    city=str(rule.location or rule.station or "unknown"),
                    station=rule.station,
                    horizon=horizon,
                    unit=str(rule.unit),
                    providers=calibration_providers,
                    calibration_phase=calibration_phase,
                )
                if source_bias_cache is not None:
                    source_bias_cache[bias_key] = bias_calibration
            source_biases = bias_calibration.biases
            bias_reason = bias_calibration.reason
            model_members = apply_temperature_biases(model_members, source_biases)
        source_sigmas: dict[str, Decimal] = dict(bias_calibration.sigmas) if model_members else {}
        if model_members and "reference_awc-taf" in model_members:
            source_sigmas.setdefault(
                "reference_awc-taf",
                Decimal("0.75") if str(rule.unit).upper() == "C" else Decimal("1.35"),
            )
        if isinstance(d0_hourly_payload, dict):
            source_sigmas.setdefault(
                "reference_hourly-open-meteo",
                _d0_hourly_sigma(d0_hourly_payload, str(rule.unit)),
            )
        conditioning_probability = None
        raw_conditioning_probability = None
        conditioning_weight = Decimal("0")
        trajectory_upper_bound = None
        if isinstance(d0_hourly_payload, dict):
            raw_conditioned_peak = Decimal(str(d0_hourly_payload["conditioned_final_peak"]))
            raw_hourly_probabilities = global_bucket_model_probabilities(
                rule,
                forecast,
                {"reference_hourly-open-meteo": [raw_conditioned_peak]},
                now=effective_now,
                observed_max=observation.value if observation is not None else None,
                observed_max_unit=observation.unit if observation is not None else None,
                source_sigmas=source_sigmas,
            )
            raw_conditioning_probability = raw_hourly_probabilities.get(
                "reference_hourly-open-meteo"
            )
            conditioned_peak = raw_conditioned_peak
            conditioned_peak += source_biases.get("reference_hourly-open-meteo", Decimal("0"))
            hourly_probabilities = global_bucket_model_probabilities(
                rule,
                forecast,
                {"reference_hourly-open-meteo": [conditioned_peak]},
                now=effective_now,
                observed_max=observation.value if observation is not None else None,
                observed_max_unit=observation.unit if observation is not None else None,
                source_sigmas=source_sigmas,
            )
            conditioning_probability = hourly_probabilities.get("reference_hourly-open-meteo")
            conditioning_weight = Decimal("0.75") * source_weights.get(
                "reference_hourly-open-meteo", Decimal("1")
            )
            raw_trajectory_upper_bound = d0_hourly_payload.get("trajectory_upper_bound")
            if raw_trajectory_upper_bound is not None:
                trajectory_upper_bound = Decimal(str(raw_trajectory_upper_bound))

        top_candidate_votes = (
            global_bucket_top_candidate_votes(
                sibling_rules,
                forecast,
                model_members,
                now=effective_now,
                observed_max=observation.value if observation is not None else None,
                observed_max_unit=observation.unit if observation is not None else None,
                source_sigmas=source_sigmas,
                source_weights=source_weights,
            )
            if sibling_rules and model_members
            else {}
        )
        top_supporters, top_model_count = top_candidate_votes.get(market_id, (None, None))
        analysis = analyze_global_bucket_price(
            market_id,
            rule,
            forecast,
            snapshot.best_ask,
            GlobalBucketPricingConfig(
                min_edge=self.settings.min_edge, slippage_buffer=self.settings.slippage_buffer
            ),
            now=now,
            observed_max=observation.value if observation is not None else None,
            observed_max_unit=observation.unit if observation is not None else None,
            model_members=model_members,
            external_probability=external_probability,
            external_weight=external_weight,
            best_bid=snapshot.best_bid,
            fees_enabled=fee_schedule.fees_enabled,
            fee_rate=fee_schedule.fee_rate,
            source_weights=source_weights,
            source_sigmas=source_sigmas,
            conditioning_probability=conditioning_probability,
            conditioning_weight=conditioning_weight,
            d0_trajectory_upper_bound=trajectory_upper_bound,
            d0_post_peak=(
                bool(d0_hourly_payload.get("post_forecast_peak"))
                if isinstance(d0_hourly_payload, dict)
                else False
            ),
            d0_peak_lock_confirmed=(
                bool(d0_hourly_payload.get("peak_lock_confirmed"))
                if isinstance(d0_hourly_payload, dict)
                else None
            ),
            top_candidate_supporters=top_supporters,
            top_candidate_model_count=top_model_count,
        )
        d0_hourly = forecast_payload.get("d0_hourly_context")
        evidence_reasons = [
            f"weather_source_calibration horizon={horizon} phase={calibration_phase} "
            f"lead_hours={lead_hours}: {calibration_reason}",
            f"weather_source_bias horizon={horizon} phase={calibration_phase}: {bias_reason}; "
            + ",".join(
                f"{source}={bias:+.3f}{rule.unit}" for source, bias in source_biases.items()
            ),
            "weather_source_sigma="
            + ",".join(
                f"{source}:{sigma:.3f}{rule.unit}" for source, sigma in source_sigmas.items()
            ),
        ]
        awc_taf = _pricing_reference(forecast_payload, "awc_taf")
        if awc_taf and awc_taf.get("status") != "unavailable":
            evidence_reasons.append(
                "awc_taf_target="
                f"{awc_taf.get('value')}{awc_taf.get('unit')} "
                f"station={awc_taf.get('station')} "
                f"issue_time={awc_taf.get('issue_time')} "
                f"valid_time={awc_taf.get('valid_time')} "
                f"cache_status={awc_taf.get('provider_cache_status')}"
            )
            evidence_reasons.append(
                f"awc_taf_pricing_status={taf_reference_status}; included={taf_reference_usable}"
            )
        if top_model_count:
            evidence_reasons.append(
                f"top_candidate_family_supporters={top_supporters or 0}/{top_model_count}"
            )
        if isinstance(d0_hourly, dict):
            evidence_reasons.append(
                "D0 hourly context "
                f"station={d0_hourly.get('station')} local_time={d0_hourly.get('local_now')} "
                f"observed_max={d0_hourly.get('observed_max')}{rule.unit} "
                f"current={d0_hourly.get('current_temperature')}{rule.unit} "
                f"trend_per_hour={d0_hourly.get('recent_trend_per_hour')} "
                f"remaining_peak={d0_hourly.get('remaining_peak')}{rule.unit} "
                f"peak_time={d0_hourly.get('remaining_peak_time')} "
                f"hours_to_peak={d0_hourly.get('hours_to_remaining_peak')} "
                f"post_peak={d0_hourly.get('post_forecast_peak')} "
                f"forecast_at_observation={d0_hourly.get('forecast_at_observation')} "
                f"forecast_anchor_error={d0_hourly.get('forecast_anchor_error')} "
                f"max_warming_rate={d0_hourly.get('max_warming_rate_per_hour')} "
                f"trajectory_limited={d0_hourly.get('trajectory_limited')} "
                f"trajectory_upper_bound={d0_hourly.get('trajectory_upper_bound')} "
                f"cloud={d0_hourly.get('peak_cloud_cover')} "
                f"radiation={d0_hourly.get('peak_shortwave_radiation')} "
                f"wind={d0_hourly.get('peak_wind_speed')} "
                f"hourly_sigma={source_sigmas.get('reference_hourly-open-meteo')}"
            )
        elif forecast_payload.get("d0_hourly_warning"):
            evidence_reasons.append(
                f"D0 hourly evidence degraded: {forecast_payload['d0_hourly_warning']}"
            )
        analysis = replace(analysis, reasons=analysis.reasons + evidence_reasons)
        cache_status = str(forecast_payload.get("cache_status") or "")
        if cache_status:
            cache_reason = f"weather_cache_status={cache_status}"
            fallback_reason = forecast_payload.get("cache_fallback_reason")
            if fallback_reason:
                cache_reason += f" reason={fallback_reason}"
            analysis = replace(analysis, reasons=analysis.reasons + [cache_reason])
        if extra_reasons:
            analysis = replace(analysis, reasons=analysis.reasons + extra_reasons)
        if model_members:
            source_probabilities = global_bucket_model_probabilities(
                rule,
                forecast,
                model_members,
                now=effective_now,
                observed_max=observation.value if observation is not None else None,
                observed_max_unit=observation.unit if observation is not None else None,
                source_sigmas=source_sigmas,
            )
            raw_source_probabilities = global_bucket_model_probabilities(
                rule,
                forecast,
                raw_model_members or model_members,
                now=effective_now,
                observed_max=observation.value if observation is not None else None,
                observed_max_unit=observation.unit if observation is not None else None,
                source_sigmas=source_sigmas,
            )
            if conditioning_probability is not None:
                source_probabilities["reference_hourly-open-meteo"] = conditioning_probability
                if raw_conditioning_probability is not None:
                    raw_source_probabilities["reference_hourly-open-meteo"] = (
                        raw_conditioning_probability
                    )
            event_identity = f"{rule.location}_{rule.target_date}_{rule.variable}_{rule.unit}"
            revision = _stable_forecast_revision(forecast, forecast_payload)
            for source, probability in source_probabilities.items():
                source_revision = _source_signal_revision(
                    revision,
                    forecast_payload=forecast_payload,
                    observation=observation,
                )
                self.repository.save_weather_source_signal(
                    market_id=market_id,
                    source=source,
                    yes_probability=probability,
                    event_identity=event_identity,
                    forecast_revision=source_revision,
                    city=str(rule.location or rule.station or "unknown"),
                    station=rule.station,
                    horizon=horizon,
                    target_date=str(rule.target_date),
                    source_role=(
                        "hourly"
                        if "hourly" in source
                        else "reference"
                        if source.startswith("reference_")
                        else "ensemble"
                    ),
                    now=effective_now,
                    raw_yes_probability=raw_source_probabilities.get(source),
                    applied_bias=source_biases.get(source, Decimal("0")),
                    unit=rule.unit,
                    calibration_phase=calibration_phase,
                    lead_hours=lead_hours,
                    market_probability=(
                        (snapshot.best_bid + snapshot.best_ask) / Decimal("2")
                        if snapshot.best_bid is not None and snapshot.best_ask is not None
                        else snapshot.best_ask
                    ),
                    source_family=weather_source_family(source),
                )
        self.repository.save_analysis(analysis)
        return analysis

    def research_global_bucket_batch(
        self,
        market_ids: list[str],
        *,
        now: datetime | None = None,
        allow_llm: bool = True,
    ) -> tuple[int, list[str]]:
        """Analyze bucket siblings while fetching one forecast per city/date group."""
        forecast_cache: dict[
            tuple[str | None, str | None, str | None, str | None],
            tuple[ForecastSnapshot, dict[str, object]],
        ] = {}
        observation_cache: dict[
            tuple[str | None, str | None, str | None, str | None], D0ObservationContext
        ] = {}
        analyzed = 0
        failures: list[str] = []
        now = now or datetime.now(timezone.utc)

        # Phase 1: Group by location/date and prepare representative rules
        groups_to_fetch = {}
        parsed_rules = {}
        for market_id in market_ids:
            try:
                market = self._market(market_id)
                if _module_id(market) != "global_temp_bucket":
                    continue
                rule = self._global_rule(market_id, market)
                if not rule.tradable:
                    raise ValueError(rule.rejection_reason or "bucket rule is not tradable")
                parsed_rules[market_id] = rule
                key = (rule.location, rule.target_date, rule.variable, rule.unit)
                if key not in groups_to_fetch:
                    groups_to_fetch[key] = (market_id, rule)
            except Exception as exc:
                failures.append(f"{market_id}: {exc}")
                parsed_rules[market_id] = exc

        persisted_weather_cache = {
            key: self._cached_global_weather(market_id, rule, now=now)
            for key, (market_id, rule) in groups_to_fetch.items()
        }

        # Phase 2: Parallel network fetch
        def _fetch_group(key, market_id, rule, persisted):
            try:
                d0 = self._d0_observation_context(market_id, rule, now=now)
                if d0.block_reason:
                    return key, d0, None, None
                cached = self._fetch_global_weather_with_cache(
                    market_id,
                    rule,
                    now=now,
                    cached=persisted,
                )
                if d0.observation is not None:
                    cached = self._attach_d0_hourly_context(
                        cached,
                        rule=rule,
                        observation_context=d0,
                        now=now,
                    )
                return key, d0, cached, None
            except Exception as exc:
                return key, None, None, exc

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_key = {
                executor.submit(
                    _fetch_group,
                    key,
                    m_id,
                    rule,
                    persisted_weather_cache[key],
                ): key
                for key, (m_id, rule) in groups_to_fetch.items()
            }
            for future in concurrent.futures.as_completed(future_to_key):
                key, d0, cached, exc = future.result()
                if exc:
                    observation_cache[key] = exc
                else:
                    observation_cache[key] = d0
                    forecast_cache[key] = cached

        # Phase 3: Optional LLM evaluation (one new group call per batch/tick)
        llm_called_this_tick = False
        llm_signals: dict[str, Row] = {}
        source_calibration_cache: dict[tuple[str, str, str, tuple[str, ...]], Any] = {}
        source_bias_cache: dict[tuple[str, str, str, str, str, tuple[str, ...]], Any] = {}
        group_to_markets = {}
        for market_id in market_ids:
            if market_id in parsed_rules and not isinstance(parsed_rules[market_id], Exception):
                rule = parsed_rules[market_id]
                key = (rule.location, rule.target_date, rule.variable, rule.unit)
                group_to_markets.setdefault(key, []).append(market_id)
        group_sibling_rules: dict[
            tuple[str | None, str | None, str | None, str | None],
            dict[str, GlobalTemperatureBucketRule],
        ] = {}
        group_context_reasons: dict[
            tuple[str | None, str | None, str | None, str | None],
            str,
        ] = {}
        for key, group_market_ids in group_to_markets.items():
            sibling_rules, context_reason = self._global_event_sibling_rules(
                group_market_ids[0],
                parsed_rules=parsed_rules,
            )
            group_sibling_rules[key] = sibling_rules
            group_context_reasons[key] = context_reason

        llm_advisor = getattr(self, "llm_advisor", None)
        if allow_llm and llm_advisor and llm_advisor.enabled:
            for key, group_market_ids in group_to_markets.items():
                cached = forecast_cache.get(key)
                if not cached or isinstance(cached, Exception):
                    continue
                d0_or_exc = observation_cache.get(key)
                if isinstance(d0_or_exc, Exception) or (d0_or_exc and d0_or_exc.block_reason):
                    continue

                base_forecast, raw_payload = cached
                forecast_revision = _stable_forecast_revision(base_forecast, raw_payload)
                event_identity = f"{key[0]}_{key[1]}_{key[2]}_{key[3]}"

                if not llm_advisor.provider or not llm_advisor.model:
                    continue
                forecast_provider = f"llm:{llm_advisor.provider}:{llm_advisor.model}"
                current_signals = {
                    market_id: self.repository.model_signal_for_revision(
                        market_id=market_id,
                        model_version=LLM_WEATHER_MODEL_VERSION,
                        forecast_provider=forecast_provider,
                        event_identity=event_identity,
                        forecast_revision=forecast_revision,
                    )
                    for market_id in group_market_ids
                }

                if any(signal is None for signal in current_signals.values()):
                    if llm_called_this_tick:
                        continue
                    llm_called_this_tick = True
                    sibling_markets = []
                    for m_id in group_market_ids:
                        market = self.repository.get_market(m_id)
                        if market:
                            sibling_markets.append(dict(market))

                    decision = llm_advisor.evaluate_group(
                        event_identity,
                        sibling_markets,
                        now,
                        forecast_evidence={
                            market_id: (base_forecast, raw_payload)
                            for market_id in group_market_ids
                        },
                        observation_evidence=_llm_observation_evidence(d0_or_exc),
                    )
                    if decision is not None:
                        for m_id in group_market_ids:
                            prob = decision.bucket_probabilities.get(m_id, Decimal("0"))
                            self.repository.save_llm_model_signal(
                                market_id=m_id,
                                provider=decision.provider,
                                model=decision.model,
                                yes_probability=prob,
                                confidence=decision.confidence,
                                reason=decision.reason,
                                event_identity=event_identity,
                                forecast_revision=forecast_revision,
                                now=now,
                                decision=decision.decision,
                                other_probability=decision.other_probability,
                                distribution_total=decision.distribution_total,
                                source_forecast_time=base_forecast.fetched_at.isoformat(),
                                raw_response=decision.raw_response,
                                bucket_probabilities=decision.bucket_probabilities,
                                horizon=_forecast_horizon(parsed_rules[m_id], now),
                            )
                        current_signals = {
                            market_id: self.repository.model_signal_for_revision(
                                market_id=market_id,
                                model_version=LLM_WEATHER_MODEL_VERSION,
                                forecast_provider=forecast_provider,
                                event_identity=event_identity,
                                forecast_revision=forecast_revision,
                            )
                            for market_id in group_market_ids
                        }

                llm_signals.update(
                    {
                        market_id: signal
                        for market_id, signal in current_signals.items()
                        if signal is not None
                    }
                )

        # Phase 4: Sequential DB writes
        for market_id in market_ids:
            if market_id not in parsed_rules:
                continue
            rule_or_exc = parsed_rules[market_id]
            if isinstance(rule_or_exc, Exception):
                self._mark_market_failed(market_id, rule_or_exc)
                continue

            rule = rule_or_exc
            key = (rule.location, rule.target_date, rule.variable, rule.unit)

            d0_or_exc = observation_cache.get(key)
            if isinstance(d0_or_exc, Exception):
                self._mark_market_failed(market_id, d0_or_exc)
                failures.append(f"{market_id}: {d0_or_exc}")
                continue

            d0 = d0_or_exc
            if d0 is None:
                continue

            try:
                if d0.block_reason:
                    analysis = self._save_global_bucket_guard_rejection(market_id, d0.block_reason)
                    self.repository.upsert_candidate(
                        market_id,
                        _candidate_rule(rule),
                        self.repository.latest_pricing_snapshot(market_id),
                        status="dry_run_ready",
                        notes="; ".join(analysis.reasons),
                        module_id="global_temp_bucket",
                    )
                    analyzed += 1
                    continue

                cached = forecast_cache.get(key)
                if not cached:
                    raise ValueError("Forecast missing after successful fetch phase")
                base_forecast, raw_payload = cached
                forecast = replace(base_forecast, market_id=market_id)

                observation = None
                if d0.observation is not None and d0.raw_payload is not None:
                    observation = replace(d0.observation, market_id=market_id)
                    self.repository.save_observation(observation, d0.raw_payload)

                # Use only a valid signal for this exact provider/event/revision.
                from polymarket_weather_arb.services.calibration_service import CalibrationService

                external_probability = None
                external_weight = Decimal("0")
                llm_reasons: list[str] = []
                pricing_payload = dict(raw_payload)
                signal = llm_signals.get(market_id)
                if signal is not None:
                    trust = CalibrationService(self.repository).trust_for_model(
                        model_version=LLM_WEATHER_MODEL_VERSION,
                        forecast_provider=str(signal["forecast_provider"]),
                        horizon=_forecast_horizon(rule, now),
                    )
                    stale = _signal_is_stale(
                        signal,
                        now=now,
                        max_age_seconds=int(self.settings.stale_forecast_seconds),
                    )
                    signal_valid = signal["decision"] == "advisory" and not stale
                    if signal_valid:
                        external_probability = Decimal(str(signal["yes_probability"]))
                        external_weight = trust.effective_weight
                    availability = "stale" if stale else str(signal["decision"])
                    applied = external_probability is not None and external_weight > 0
                    llm_reasons.append(
                        "LLM vote "
                        f"status={availability} probability={external_probability if external_probability is not None else 'unavailable'} "
                        f"weight={external_weight} distinct_events={trust.distinct_events} "
                        f"brier={trust.brier_score} hit_rate={trust.hit_rate}; "
                        f"{'applied' if applied else 'pricing unchanged'} ({trust.weight_reason})"
                    )
                    pricing_payload["llm_vote"] = {
                        "model_version": LLM_WEATHER_MODEL_VERSION,
                        "forecast_provider": signal["forecast_provider"],
                        "status": availability,
                        "yes_probability": (
                            float(external_probability)
                            if external_probability is not None
                            else None
                        ),
                        "effective_weight": float(external_weight),
                        "distinct_events": trust.distinct_events,
                        "brier_score": (
                            float(trust.brier_score) if trust.brier_score is not None else None
                        ),
                        "hit_rate": float(trust.hit_rate) if trust.hit_rate is not None else None,
                        "applied": applied,
                        "weight_reason": trust.weight_reason,
                    }
                self._save_global_forecast_if_changed(forecast, pricing_payload)

                analysis = self._price_global_bucket_market(
                    market_id,
                    rule,
                    forecast,
                    pricing_payload,
                    now=now,
                    observation=d0.observation if d0 else None,
                    external_probability=external_probability,
                    external_weight=external_weight,
                    extra_reasons=llm_reasons + [group_context_reasons[key]],
                    source_calibration_cache=source_calibration_cache,
                    source_bias_cache=source_bias_cache,
                    sibling_rules=group_sibling_rules[key],
                )
                self.repository.upsert_candidate(
                    market_id,
                    _candidate_rule(rule),
                    self.repository.latest_pricing_snapshot(market_id),
                    status="dry_run_ready",
                    notes="; ".join(analysis.reasons),
                    module_id="global_temp_bucket",
                )
                analyzed += 1
            except Exception as exc:
                self._mark_market_failed(market_id, exc)
                failures.append(f"{market_id}: {exc}")

        self._apply_bucket_switch_hysteresis(market_ids)
        return analyzed, failures

    def reprice_global_bucket_group_cached(
        self,
        market_ids: list[str],
        *,
        now: datetime | None = None,
    ) -> tuple[int, list[str], str | None, str | None]:
        """Reprice one event-group from persisted forecast + order-book only.

        Returns ``(analyzed_count, failures, slow_refresh_reason, forecast_revision)``.

        Network-free by contract: no geocoding, weather provider, Gamma, CLOB, or
        LLM calls. Incomplete/stale cached inputs yield a slow-refresh reason
        instead of silently fetching upstream.
        """
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not market_ids:
            return 0, [], "empty_group", None

        parsed_rules: dict[str, GlobalTemperatureBucketRule] = {}
        failures: list[str] = []
        for market_id in market_ids:
            try:
                market = self._market(market_id)
                if _module_id(market) != "global_temp_bucket":
                    failures.append(f"{market_id}: not global_temp_bucket")
                    continue
                rule = self._global_rule(market_id, market)
                if not rule.tradable:
                    raise ValueError(rule.rejection_reason or "bucket rule is not tradable")
                snapshot = self.repository.latest_pricing_snapshot(market_id)
                if snapshot is None:
                    return (
                        0,
                        failures,
                        f"missing_order_book:{market_id}",
                        None,
                    )
                snapshot_at = _parse_payload_datetime(snapshot["fetched_at"])
                snapshot_age = now - snapshot_at if snapshot_at is not None else None
                if (
                    snapshot_age is None
                    or snapshot_age < -timedelta(minutes=5)
                    or snapshot_age > timedelta(seconds=int(self.settings.stale_order_book_seconds))
                ):
                    return 0, failures, f"stale_order_book:{market_id}", None
                parsed_rules[market_id] = rule
            except Exception as exc:
                failures.append(f"{market_id}: {exc}")

        if not parsed_rules:
            return 0, failures, "no_tradable_siblings", None

        # One coherent revision for the whole (city, date) group.
        sample_id, sample_rule = next(iter(parsed_rules.items()))
        group_key = (
            sample_rule.location,
            sample_rule.target_date,
            sample_rule.variable,
            sample_rule.unit,
        )
        for market_id, rule in parsed_rules.items():
            key = (rule.location, rule.target_date, rule.variable, rule.unit)
            if key != group_key:
                return 0, failures, "mixed_event_group", None

        sibling_rules, event_context_reason = self._global_event_sibling_rules(
            sample_id,
            parsed_rules=parsed_rules,
        )
        cached = self._cached_global_weather(sample_id, sample_rule, now=now)
        if cached is None:
            return 0, failures, f"missing_or_incomplete_forecast:{sample_id}", None
        base_forecast, raw_payload, age = cached
        if age > _global_weather_cache_ttl(rule, now):
            return 0, failures, f"stale_forecast:{sample_id}", None
        base_forecast, raw_payload = self._mark_cached_weather(
            base_forecast,
            raw_payload,
            market_id=sample_id,
            status="cached_reprice",
        )
        forecast_revision = _stable_forecast_revision(base_forecast, raw_payload)

        d0 = self._cached_d0_observation_context(sample_id, sample_rule, now=now)
        if d0.block_reason and d0.block_reason.startswith("needs_observation_refresh:"):
            return 0, failures, d0.block_reason, forecast_revision

        analyzed = 0
        source_calibration_cache: dict[tuple[str, str, str, tuple[str, ...]], Any] = {}
        source_bias_cache: dict[tuple[str, str, str, str, str, tuple[str, ...]], Any] = {}
        for market_id, rule in parsed_rules.items():
            try:
                if d0.block_reason:
                    analysis = self._save_global_bucket_guard_rejection(market_id, d0.block_reason)
                    self.repository.upsert_candidate(
                        market_id,
                        _candidate_rule(rule),
                        self.repository.latest_pricing_snapshot(market_id),
                        status="dry_run_ready",
                        notes="; ".join(analysis.reasons),
                        module_id="global_temp_bucket",
                    )
                    analyzed += 1
                    continue

                forecast = replace(base_forecast, market_id=market_id)
                pricing_payload = {
                    **raw_payload,
                    "cache_status": "cached_reprice",
                    "forecast_revision": forecast_revision,
                }
                # Persist revision tag without re-fetch; skip write if identical.
                self._save_global_forecast_if_changed(forecast, pricing_payload)
                analysis = self._price_global_bucket_market(
                    market_id,
                    rule,
                    forecast,
                    pricing_payload,
                    now=now,
                    observation=d0.observation,
                    extra_reasons=[
                        "cached_event_group_reprice",
                        f"forecast_revision={forecast_revision}",
                        event_context_reason,
                    ],
                    source_calibration_cache=source_calibration_cache,
                    source_bias_cache=source_bias_cache,
                    sibling_rules=sibling_rules,
                )
                self.repository.upsert_candidate(
                    market_id,
                    _candidate_rule(rule),
                    self.repository.latest_pricing_snapshot(market_id),
                    status="dry_run_ready",
                    notes="; ".join(analysis.reasons),
                    module_id="global_temp_bucket",
                )
                analyzed += 1
            except Exception as exc:
                self._mark_market_failed(market_id, exc)
                failures.append(f"{market_id}: {exc}")

        self._apply_bucket_switch_hysteresis(list(parsed_rules.keys()))
        return analyzed, failures, None, forecast_revision

    def _cached_d0_observation_context(
        self,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
        *,
        now: datetime,
    ) -> D0ObservationContext:
        """D0 guard using only persisted observations (no official-station fetch)."""
        timezone_name = _rule_timezone(rule)
        if not timezone_name:
            return D0ObservationContext(
                block_reason="market timezone unknown; D0 entry safety cannot be evaluated",
            )
        local_now = now.astimezone(ZoneInfo(timezone_name))
        try:
            target_date = datetime.fromisoformat(str(rule.target_date)).date()
        except (TypeError, ValueError):
            return D0ObservationContext(
                block_reason="target date is invalid; D0 entry safety cannot be evaluated",
            )
        if target_date != local_now.date():
            return D0ObservationContext()

        row = self.repository.latest_observation(market_id)
        if row is None:
            return D0ObservationContext(
                block_reason=f"needs_observation_refresh:{market_id}",
            )
        try:
            raw_payload = json.loads(row["raw_payload"]) if row["raw_payload"] else {}
        except (TypeError, json.JSONDecodeError):
            raw_payload = {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        observed_at = _parse_payload_datetime(row["observed_at"])
        fetched_at = _parse_payload_datetime(row["fetched_at"]) or observed_at
        if observed_at is None or fetched_at is None:
            return D0ObservationContext(
                block_reason=f"needs_observation_refresh:{market_id}",
            )
        age = now - fetched_at
        if age < -timedelta(minutes=5) or age > D0_OBSERVATION_MAX_AGE:
            return D0ObservationContext(
                block_reason=f"needs_observation_refresh:{market_id}",
            )
        if not raw_payload.get("official_signal") or normalize_source_grade(
            str(raw_payload.get("source_grade") or "")
        ) != normalize_source_grade(SETTLEMENT_OBSERVATION):
            return D0ObservationContext(
                block_reason="D0 entry requires an official observation source",
            )
        quality = str(row["quality_status"] or "")
        if quality not in {"V", "AWC"}:
            return D0ObservationContext(
                block_reason=f"D0 observed maximum quality is {quality or 'unknown'}, not verified",
            )
        observation = WeatherObservation(
            provider=str(row["provider"]),
            variable=str(row["variable"]),
            value=Decimal(str(row["value"])),
            unit=str(row["unit"]),
            observed_at=observed_at,
            market_id=market_id,
            station=row["station"],
            quality_status=quality,
            fetched_at=fetched_at,
        )
        return D0ObservationContext(observation=observation, raw_payload=raw_payload)

    def _mark_market_failed(self, market_id: str, exc: Exception) -> None:
        reason = f"forecast/analysis failed: {exc}"
        self.repository.mark_candidate(market_id, "dry_run_ready", reason)
        self.repository.save_analysis(
            Analysis(
                market_id=market_id,
                model_version="global-temp-bucket-unavailable-v1",
                fair_lower=Decimal("0"),
                fair_upper=Decimal("0"),
                reference_price=None,
                edge=Decimal("0"),
                side=None,
                decision="reject",
                reasons=[reason],
            )
        )

    def _apply_bucket_switch_hysteresis(self, market_ids: list[str]) -> None:
        """Mark only probability-dominant bucket changes for coordinated review."""
        groups: dict[tuple[str, str], list[str]] = {}
        for market_id in market_ids:
            row = self.repository.get_temperature_bucket_rule(market_id)
            if row is None:
                continue
            key = (str(row["city"] or "").casefold(), str(row["target_date"] or ""))
            groups.setdefault(key, []).append(market_id)

        for group_ids in groups.values():
            held = [
                market_id
                for market_id in group_ids
                if self.repository.list_positions(limit=1, market_id=market_id, nonzero_only=True)
            ]
            if len(held) != 1:
                continue
            active_id = held[0]
            rows = {
                market_id: self.repository.latest_analysis(market_id) for market_id in group_ids
            }
            trade_rows = {
                market_id: (row, _analysis_consensus_probability(row))
                for market_id, row in rows.items()
                if row is not None and str(row["decision"]) in {"buy", "trade"}
            }
            trade_rows = {
                market_id: (row, probability)
                for market_id, (row, probability) in trade_rows.items()
                if probability is not None and probability >= BUCKET_SWITCH_MIN_TARGET_PROBABILITY
            }
            active = rows.get(active_id)
            if not trade_rows or active is None:
                continue
            active_probability = _analysis_consensus_probability(active)
            if active_probability is None:
                continue
            best_id = max(
                trade_rows,
                key=lambda market_id: (
                    trade_rows[market_id][1],
                    _analysis_strategy_score(trade_rows[market_id][0]),
                ),
            )
            if best_id == active_id:
                continue
            best, best_probability = trade_rows[best_id]
            probability_advantage = best_probability - active_probability
            if probability_advantage < BUCKET_SWITCH_MIN_PROBABILITY_ADVANTAGE:
                continue
            edge_advantage = _analysis_strategy_score(best) - _analysis_strategy_score(active)
            if edge_advantage < BUCKET_SWITCH_MIN_EDGE_ADVANTAGE:
                continue
            self.repository.save_analysis(
                Analysis(
                    market_id=active_id,
                    model_version=f"{active['model_version']}-switch",
                    fair_lower=Decimal(str(active["fair_lower"])),
                    fair_upper=Decimal(str(active["fair_upper"])),
                    reference_price=(
                        Decimal(str(active["reference_price"]))
                        if active["reference_price"] is not None
                        else None
                    ),
                    edge=Decimal(str(active["edge"])),
                    side=None,
                    decision="watch",
                    reasons=json.loads(active["reasons"])
                    + [
                        f"rebalance_target={best_id}",
                        (
                            f"switch_probability_advantage={probability_advantage:.4f}; "
                            f"held={active_probability:.4f} target={best_probability:.4f}"
                        ),
                        (
                            f"switch_score_advantage={edge_advantage:.4f}; "
                            "coordinated target entry required before current bucket exit"
                        ),
                    ],
                )
            )

    def _d0_observation_context(
        self,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
        *,
        now: datetime,
    ) -> D0ObservationContext:
        timezone_name = _rule_timezone(rule)
        if not timezone_name:
            return D0ObservationContext(
                block_reason="market timezone unknown; D0 entry safety cannot be evaluated",
            )
        local_now = now.astimezone(ZoneInfo(timezone_name))
        try:
            target_date = datetime.fromisoformat(str(rule.target_date)).date()
        except (TypeError, ValueError):
            return D0ObservationContext(
                block_reason="target date is invalid; D0 entry safety cannot be evaluated",
            )
        if target_date != local_now.date():
            return D0ObservationContext()
        try:
            observation, raw_payload = self._fetch_d0_station_observation(market_id, rule)
        except Exception as exc:
            return D0ObservationContext(
                block_reason=f"D0 entry requires official observed max-to-date: {exc}",
            )
        payload = dict(raw_payload)
        if not payload.get("official_signal") or normalize_source_grade(
            str(payload.get("source_grade") or "")
        ) != normalize_source_grade(SETTLEMENT_OBSERVATION):
            return D0ObservationContext(
                block_reason="D0 entry requires an official observation source",
            )
        if observation.quality_status not in {"V", "AWC"}:
            quality = observation.quality_status or "unknown"
            return D0ObservationContext(
                block_reason=f"D0 observed maximum quality is {quality}, not verified",
            )
        latest_at = _latest_observation_at(payload)
        if latest_at is None:
            return D0ObservationContext(
                block_reason="D0 observation payload has no timestamped samples",
            )
        age = now - latest_at
        if age < -timedelta(minutes=5) or age > D0_OBSERVATION_MAX_AGE:
            return D0ObservationContext(
                block_reason=(
                    f"D0 observed maximum feed is stale or future-dated "
                    f"(latest sample {latest_at.isoformat()})"
                ),
            )
        return D0ObservationContext(
            observation=observation,
            raw_payload=payload,
        )

    def _fetch_d0_station_observation(
        self,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
    ) -> tuple[WeatherObservation, dict[str, Any]]:
        if not self._default_observation_provider:
            return self.observation_provider_factory().fetch_observation(market_id, rule)

        errors: list[str] = []
        station = str(rule.station or "").upper()
        # Wunderground history is built from station reports, not the NWS
        # five-minute sample stream. Taking the maximum across both feeds made
        # a transient KDAL NWS sample (91.4F) override the METAR high (89.06F),
        # while Polymarket resolved from Wunderground at 88-89F. Use AWC's exact
        # METAR/SPECI reports as the settlement-aligned D0 proxy and keep NWS as
        # a US-only availability fallback.
        try:
            awc = self.awc_observation_provider_factory().fetch_observation(market_id, rule)
            return _merge_station_observations(
                market_id,
                rule,
                [awc],
                errors,
            )
        except Exception as exc:
            errors.append(f"awc: {exc}")
        if station.startswith("K"):
            try:
                nws = self.observation_provider_factory().fetch_observation(market_id, rule)
                return _merge_station_observations(
                    market_id,
                    rule,
                    [nws],
                    errors
                    + [
                        "AWC unavailable; NWS five-minute observations are a degraded "
                        "settlement proxy"
                    ],
                )
            except Exception as exc:
                errors.append(f"nws: {exc}")
        raise ValueError("; ".join(errors) or "no official station observation source")

    def _save_global_bucket_guard_rejection(self, market_id: str, reason: str) -> Analysis:
        if self.repository.list_positions(limit=1, market_id=market_id, nonzero_only=True):
            previous = self.repository.latest_analysis(market_id)
            if previous is not None:
                model_version = str(previous["model_version"])
                if not model_version.endswith("-entry-gated"):
                    model_version = f"{model_version}-entry-gated"
                previous_reasons = json.loads(previous["reasons"])
                entry_reason = f"entry_gate_only={reason}; existing position analysis preserved"
                if entry_reason not in previous_reasons:
                    previous_reasons.append(entry_reason)
                analysis = Analysis(
                    market_id=market_id,
                    model_version=model_version,
                    fair_lower=Decimal(str(previous["fair_lower"])),
                    fair_upper=Decimal(str(previous["fair_upper"])),
                    reference_price=(
                        Decimal(str(previous["reference_price"]))
                        if previous["reference_price"] is not None
                        else None
                    ),
                    edge=Decimal(str(previous["edge"])),
                    side=previous["side"],
                    decision=previous["decision"],
                    reasons=previous_reasons,
                )
                self.repository.save_analysis(analysis)
                return analysis
        analysis = Analysis(
            market_id=market_id,
            model_version="global-temp-bucket-d0-guard-v1",
            fair_lower=Decimal("0"),
            fair_upper=Decimal("0"),
            reference_price=None,
            edge=Decimal("0"),
            side=None,
            decision="reject",
            reasons=[reason],
        )
        self.repository.save_analysis(analysis)
        return analysis

    def _refresh_china_weather(
        self, market_id: str, rule: ChinaTemperatureBucketRule
    ) -> ForecastSnapshot:
        forecast, raw_payload = self.china_weather_provider_factory().fetch_forecast(
            market_id, rule
        )
        self.repository.save_forecast(forecast, raw_payload)
        return forecast

    def _refresh_global_weather(
        self, market_id: str, rule: GlobalTemperatureBucketRule
    ) -> tuple[ForecastSnapshot, dict[str, Any]]:
        forecast, raw_payload = self._fetch_global_weather(market_id, rule)
        self._save_global_forecast_if_changed(forecast, raw_payload)
        return forecast, raw_payload

    def _fetch_global_weather(
        self,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
        *,
        now: datetime | None = None,
    ) -> tuple[ForecastSnapshot, dict[str, object]]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cached = self._cached_global_weather(market_id, rule, now=now)
        return self._fetch_global_weather_with_cache(
            market_id,
            rule,
            now=now,
            cached=cached,
        )

    def _fetch_global_weather_with_cache(
        self,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
        *,
        now: datetime,
        cached: tuple[ForecastSnapshot, dict[str, object], timedelta] | None,
    ) -> tuple[ForecastSnapshot, dict[str, object]]:
        if cached is not None and cached[2] <= _global_weather_cache_ttl(rule, now):
            cached_payload = self._upgrade_cached_awc_taf_reference(
                market_id=market_id,
                rule=rule,
                payload=cached[1],
                now=now,
            )
            return self._mark_cached_weather(
                cached[0],
                cached_payload,
                market_id=market_id,
                status="fresh_cache",
            )

        try:
            fresh = self._fetch_global_weather_uncached(market_id, rule)
        except Exception as exc:
            if cached is not None and cached[2] <= GLOBAL_WEATHER_STALE_IF_ERROR:
                cached_payload = self._upgrade_cached_awc_taf_reference(
                    market_id=market_id,
                    rule=rule,
                    payload=cached[1],
                    now=now,
                )
                return self._mark_cached_weather(
                    cached[0],
                    cached_payload,
                    market_id=market_id,
                    status="stale_if_error",
                    reason=str(exc),
                )
            raise

        forecast, payload = fresh
        if payload.get("multimodel_fallback_reason"):
            if cached is not None and cached[2] <= GLOBAL_WEATHER_STALE_IF_ERROR:
                cached_payload = self._upgrade_cached_awc_taf_reference(
                    market_id=market_id,
                    rule=rule,
                    payload=cached[1],
                    now=now,
                )
                return self._mark_cached_weather(
                    cached[0],
                    cached_payload,
                    market_id=market_id,
                    status="stale_if_error",
                    reason=str(payload["multimodel_fallback_reason"]),
                )
            return forecast, {
                **payload,
                "cache_status": "degraded_no_multimodel_cache",
            }

        stable_payload = {
            **payload,
            "revision": payload.get("revision")
            or f"global-weather:{forecast.fetched_at.isoformat()}",
            "cache_status": "network_fresh",
        }
        return forecast, stable_payload

    def _fetch_global_weather_uncached(
        self, market_id: str, rule: GlobalTemperatureBucketRule
    ) -> tuple[ForecastSnapshot, dict[str, object]]:
        provider = self.weather_provider_factory()
        try:
            reference_forecast, reference_payload = provider.fetch_forecast(market_id, rule)
        except ValueError as exc:
            if getattr(provider, "name", "") != "noaa":
                raise
            reference_forecast, reference_payload = OpenMeteoProvider().fetch_forecast(
                market_id, rule
            )
            reference_payload = {
                **dict(reference_payload),
                "fallback_from": "noaa-nws",
                "fallback_reason": str(exc),
            }
        if getattr(provider, "name", "") not in {"noaa", "open-meteo"}:
            return reference_forecast, dict(reference_payload)

        try:
            ensemble, ensemble_payload = OpenMeteoEnsembleProvider().fetch_forecast(market_id, rule)
        except Exception as exc:
            return reference_forecast, {
                **dict(reference_payload),
                "multimodel_fallback_reason": str(exc),
            }

        model_members = dict(ensemble_payload.get("model_members") or {})
        reference_value = normalize_value(
            reference_forecast.value,
            reference_forecast.variable,
            reference_forecast.unit,
            rule.unit,
        )
        model_members[f"reference_{reference_forecast.provider}"] = [float(reference_value)]
        payload = {
            **ensemble_payload,
            "model_members": model_members,
            "model_count": len(model_members),
            "reference_forecast": {
                "provider": reference_forecast.provider,
                "value": float(reference_value),
                "unit": rule.unit,
                "source_grade": dict(reference_payload).get("source_grade"),
            },
            "pricing_references": {},
        }
        self._attach_awc_taf_reference(
            market_id=market_id,
            rule=rule,
            model_members=model_members,
            payload=payload,
        )
        if self.settings.google_weather_api_key:
            latitude = float(ensemble_payload["latitude"])
            longitude = float(ensemble_payload["longitude"])
            try:
                google_forecast, google_payload = GoogleWeatherProvider(
                    self.settings.google_weather_api_key
                ).fetch_forecast(
                    market_id,
                    rule,
                    latitude=latitude,
                    longitude=longitude,
                )
                google_value = normalize_value(
                    google_forecast.value,
                    google_forecast.variable,
                    google_forecast.unit,
                    rule.unit,
                )
                model_members["reference_google-weather"] = [float(google_value)]
                payload["model_members"] = model_members
                payload["model_count"] = len(model_members)
                payload["pricing_references"]["google_weather"] = {
                    **google_payload,
                    "value": float(google_value),
                    "unit": rule.unit,
                }
            except Exception as exc:
                # Optional research evidence must never break the quantitative
                # owner when the vendor is unavailable.
                payload.setdefault("pricing_reference_warnings", []).append(
                    f"google-weather: {exc}"
                )
        return replace(ensemble, raw_payload=payload), payload

    def _upgrade_cached_awc_taf_reference(
        self,
        *,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
        payload: dict[str, object],
        now: datetime,
    ) -> dict[str, object]:
        """Add the v7 TAF reference without refetching an expensive ensemble."""
        references = payload.get("pricing_references")
        usable, _ = _awc_taf_reference_state(payload, now=now)
        if isinstance(references, dict) and "awc_taf" in references and usable:
            return payload
        model_members = payload.get("model_members")
        if not isinstance(model_members, dict) or not model_members:
            return payload
        upgraded: dict[str, Any] = {
            **payload,
            "model_members": dict(model_members),
            "pricing_references": dict(references) if isinstance(references, dict) else {},
        }
        upgraded["model_members"].pop("reference_awc-taf", None)
        upgraded["pricing_references"].pop("awc_taf", None)
        self._attach_awc_taf_reference(
            market_id=market_id,
            rule=rule,
            model_members=upgraded["model_members"],
            payload=upgraded,
        )
        return upgraded

    def _attach_awc_taf_reference(
        self,
        *,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
        model_members: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        references = payload.setdefault("pricing_references", {})
        if not rule.station:
            references["awc_taf"] = {
                "status": "unavailable",
                "reason": "market has no exact ICAO settlement station",
            }
            return
        try:
            taf_forecast, taf_payload = self.awc_forecast_provider_factory().fetch_forecast(
                market_id,
                rule,
            )
            taf_value = normalize_value(
                taf_forecast.value,
                taf_forecast.variable,
                taf_forecast.unit,
                str(rule.unit),
            )
            model_members["reference_awc-taf"] = [float(taf_value)]
            payload["model_members"] = model_members
            payload["model_count"] = len(model_members)
            references["awc_taf"] = {
                **taf_payload,
                "status": "available",
                "value": float(taf_value),
                "unit": rule.unit,
            }
        except Exception as exc:
            references["awc_taf"] = {
                "status": "unavailable",
                "station": rule.station,
                "target_date": rule.target_date,
                "reason": str(exc),
            }
            payload.setdefault("pricing_reference_warnings", []).append(f"awc-taf: {exc}")

    def _attach_d0_hourly_context(
        self,
        cached: tuple[ForecastSnapshot, dict[str, object]],
        *,
        rule: GlobalTemperatureBucketRule,
        observation_context: D0ObservationContext,
        now: datetime,
    ) -> tuple[ForecastSnapshot, dict[str, object]]:
        forecast, raw_payload = cached
        payload = dict(raw_payload)
        try:
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
            timezone_name = str(payload["timezone"])
            hourly = OpenMeteoProvider().fetch_hourly_context(
                latitude=latitude,
                longitude=longitude,
                timezone_name=timezone_name,
                target_date=str(rule.target_date),
                unit=str(rule.unit),
                now=now,
            )
            hourly = _condition_d0_hourly_context(
                hourly,
                observation_context=observation_context,
                rule=rule,
                now=now,
            )
            payload["d0_hourly_context"] = hourly
        except Exception as exc:
            payload["d0_hourly_warning"] = str(exc)
        # ForecastSnapshot has no raw_payload field; keep evidence on the payload dict.
        return forecast, payload

    def _cached_global_weather(
        self,
        market_id: str,
        rule: GlobalTemperatureBucketRule,
        *,
        now: datetime,
    ) -> tuple[ForecastSnapshot, dict[str, object], timedelta] | None:
        row = self.repository.latest_forecast(market_id)
        if row is None:
            return None
        try:
            payload = json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        expected_station = str(rule.station or "").strip().upper()
        if expected_station and (
            str(payload.get("forecast_station") or "").strip().upper() != expected_station
            or payload.get("coordinate_source") != "awc_stationinfo"
        ):
            # Forecasts fetched by city name can land on a grid tens of
            # kilometres from the airport used for settlement. Do not reuse
            # those legacy rows after station-aware forecasting is available.
            return None
        model_members = payload.get("model_members")
        if not isinstance(model_members, dict):
            return None
        usable_models = sum(
            1 for members in model_members.values() if isinstance(members, list) and members
        )
        if usable_models < 3:
            return None
        target_date = str(payload.get("target_date") or "")[:10]
        if target_date and target_date != str(rule.target_date or "")[:10]:
            return None
        fetched_at = _parse_payload_datetime(row["fetched_at"])
        issue_time = _parse_payload_datetime(row["issue_time"])
        valid_time = _parse_payload_datetime(row["valid_time"])
        if fetched_at is None or issue_time is None or valid_time is None:
            return None
        age = now - fetched_at
        if age < timedelta(0) or age > GLOBAL_WEATHER_STALE_IF_ERROR:
            return None
        snapshot = ForecastSnapshot(
            provider=str(row["provider"]),
            variable=str(row["variable"]),
            value=Decimal(str(row["value"])),
            unit=str(row["unit"]),
            issue_time=issue_time,
            valid_time=valid_time,
            market_id=market_id,
            location=row["location"],
            station=row["station"],
            lower_value=(
                Decimal(str(row["lower_value"])) if row["lower_value"] is not None else None
            ),
            upper_value=(
                Decimal(str(row["upper_value"])) if row["upper_value"] is not None else None
            ),
            fetched_at=fetched_at,
        )
        return snapshot, payload, age

    @staticmethod
    def _mark_cached_weather(
        forecast: ForecastSnapshot,
        payload: dict[str, object],
        *,
        market_id: str,
        status: str,
        reason: str | None = None,
    ) -> tuple[ForecastSnapshot, dict[str, object]]:
        cached_payload = {
            **payload,
            "revision": payload.get("revision")
            or f"global-weather:{forecast.fetched_at.isoformat()}",
            "cache_status": status,
        }
        if reason:
            cached_payload["cache_fallback_reason"] = reason
        return replace(forecast, market_id=market_id), cached_payload

    def _save_global_forecast_if_changed(
        self,
        forecast: ForecastSnapshot,
        payload: dict[str, object],
    ) -> None:
        latest = self.repository.latest_forecast(str(forecast.market_id))
        if (
            latest is not None
            and str(latest["provider"]) == forecast.provider
            and str(latest["fetched_at"]) == forecast.fetched_at.isoformat()
        ):
            self.repository.update_forecast_raw_payload(int(latest["id"]), payload)
            return
        self.repository.save_forecast(forecast, payload)

    def _china_rule(self, market_id: str, market: Row) -> ChinaTemperatureBucketRule:
        row = self.repository.get_temperature_bucket_rule(market_id)
        if row is not None:
            return _china_rule_from_row(row)
        rule = parse_china_temperature_bucket_rule(market["title"], market["description"])
        if rule.tradable:
            self.repository.save_temperature_bucket_rule(market_id, rule)
        return rule

    def _global_rule(self, market_id: str, market: Row) -> GlobalTemperatureBucketRule:
        rule = parse_global_temperature_bucket_rule(market["title"], market["description"])
        stored = self.repository.get_temperature_bucket_rule(market_id)
        if stored is not None and stored["settlement_timezone"]:
            rule = with_settlement_timezone(rule, str(stored["settlement_timezone"]))
        if rule.tradable:
            self.repository.save_temperature_bucket_rule(
                market_id, rule, module_id="global_temp_bucket"
            )
        return rule

    def _global_event_sibling_rules(
        self,
        market_id: str,
        *,
        parsed_rules: dict[str, GlobalTemperatureBucketRule | Exception],
    ) -> tuple[dict[str, GlobalTemperatureBucketRule], str]:
        sibling_ids, expected_ids = self.repository.global_temperature_event_market_ids(market_id)
        if market_id not in sibling_ids:
            sibling_ids.append(market_id)

        rules: dict[str, GlobalTemperatureBucketRule] = {}
        sample_rule = parsed_rules.get(market_id)
        sample_key = (
            (
                sample_rule.location,
                sample_rule.target_date,
                sample_rule.variable,
                sample_rule.unit,
            )
            if isinstance(sample_rule, GlobalTemperatureBucketRule)
            else None
        )
        for sibling_id in sibling_ids:
            rule_or_exc = parsed_rules.get(sibling_id)
            if rule_or_exc is None:
                try:
                    sibling_market = self._market(sibling_id)
                    if _module_id(sibling_market) != "global_temp_bucket":
                        continue
                    rule_or_exc = self._global_rule(sibling_id, sibling_market)
                except Exception:
                    continue
            if not isinstance(rule_or_exc, GlobalTemperatureBucketRule) or not rule_or_exc.tradable:
                continue
            sibling_key = (
                rule_or_exc.location,
                rule_or_exc.target_date,
                rule_or_exc.variable,
                rule_or_exc.unit,
            )
            if sample_key is not None and sibling_key != sample_key:
                continue
            rules[sibling_id] = rule_or_exc

        persisted_ids = frozenset(rules)
        complete = expected_ids is None or expected_ids.issubset(persisted_ids)
        if not complete:
            missing = sorted(expected_ids - persisted_ids)
            reason = (
                "event_bucket_context=incomplete "
                f"siblings={len(persisted_ids)} expected={len(expected_ids)} "
                f"missing={','.join(missing[:5])}"
            )
            return {}, reason
        expected = str(len(expected_ids)) if expected_ids is not None else "unknown"
        source = "gamma_event" if expected_ids is not None else "legacy_persisted"
        return (
            rules,
            f"event_bucket_context=complete siblings={len(rules)} expected={expected} source={source}",
        )

    def dry_run_trade(self, market_id: str) -> MarketWorkflowResult:
        market = self._market(market_id)
        analysis_row = self.repository.latest_analysis(market_id)
        if analysis_row is None:
            raise ValueError(f"market has no analysis: {market_id}")
        analysis = analysis_from_row(analysis_row)
        snapshot_row = self.repository.latest_pricing_snapshot(
            market_id, side=str(analysis.side or "buy_yes")
        )
        forecast_row = self.repository.latest_forecast(market_id)
        module_id = _module_id(market)
        if module_id == "china_temp_bucket":
            rule = self._china_rule(market_id, market)
        elif module_id == "global_temp_bucket":
            rule = self._global_rule(market_id, market)
        else:
            rule = parse_resolution_rule(market["title"], market["description"])
        context = risk_context(
            self.repository,
            market_id,
            datetime.now(timezone.utc).date().isoformat(),
            snapshot_row,
            forecast_row,
            rule,
            reconciliation_fresh=True,
        )
        intent_id, reasons = TradingService(
            self.settings,
            self.polymarket_client_factory(self.settings),
            self.repository,
        ).trade(
            analysis=analysis,
            yes_token_id=market["yes_token_id"],
            no_token_id=market["no_token_id"],
            context=context,
            dry_run=True,
        )
        summary = (
            f"Order intent: {intent_id}"
            if intent_id is not None
            else "Order skipped: latest analysis does not produce an executable order"
        )
        return MarketWorkflowResult(
            market_id=market_id,
            summary=summary,
            details=reasons,
        )

    def _market(self, market_id: str) -> Row:
        market = self.repository.get_market(market_id)
        if market is None:
            raise ValueError(f"unknown market: {market_id}")
        return market


def build_risk_report(repository: Repository) -> RiskReport:
    today = datetime.now(timezone.utc).date().isoformat()
    exposures = [
        (row["id"], repository.market_exposure(row["id"]))
        for row in repository.list_weather_markets()
    ]
    return RiskReport(
        daily_live_notional=repository.daily_order_notional(today), exposures=exposures
    )


def risk_context(
    repository: Repository,
    market_id: str,
    today: str,
    snapshot_row: Row | None,
    forecast_row: Row | None,
    rule: ResolutionRule | ChinaTemperatureBucketRule | GlobalTemperatureBucketRule,
    *,
    reconciliation_fresh: bool,
) -> RiskContext:
    return RiskContext(
        daily_live_notional=repository.daily_order_notional(today),
        market_live_exposure=repository.market_exposure(market_id),
        order_book_age_seconds=age_seconds(snapshot_row["fetched_at"]) if snapshot_row else None,
        forecast_age_seconds=age_seconds(forecast_row["fetched_at"]) if forecast_row else None,
        rule_tradable=rule.tradable,
        unsupported_variable=_unsupported_variable(rule),
        reconciliation_fresh=reconciliation_fresh,
    )


def analysis_from_row(row: Row) -> Analysis:
    return Analysis(
        market_id=row["market_id"],
        model_version=row["model_version"],
        fair_lower=Decimal(str(row["fair_lower"])),
        fair_upper=Decimal(str(row["fair_upper"])),
        reference_price=Decimal(str(row["reference_price"]))
        if row["reference_price"] is not None
        else None,
        edge=Decimal(str(row["edge"])),
        side=row["side"],
        decision=row["decision"],
        reasons=json.loads(row["reasons"]),
        created_at=datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc),
    )


def _rule_summary(rule: ResolutionRule) -> str:
    if not rule.tradable:
        return rule.rejection_reason or "rule rejected"
    location = rule.location or rule.station or "-"
    return f"{rule.variable} {rule.operator} {rule.threshold} {rule.unit} in {location}"


def _china_rule_summary(rule: ChinaTemperatureBucketRule) -> str:
    if not rule.tradable:
        return rule.rejection_reason or "China bucket rule rejected"
    return f"module=china_temp_bucket; city={rule.city}; bucket={rule.bucket_lower_c}-{rule.bucket_upper_c}C; target_date={rule.target_date}; source={rule.source}"


def _global_rule_summary(rule: GlobalTemperatureBucketRule) -> str:
    if not rule.tradable:
        return rule.rejection_reason or "Global bucket rule rejected"
    return f"module=global_temp_bucket; location={rule.location}; bucket={rule.bucket_lower}-{rule.bucket_upper}{rule.unit}; target_date={rule.target_date}; source={rule.source}"


def _china_rule_from_row(row: Row) -> ChinaTemperatureBucketRule:
    return ChinaTemperatureBucketRule(
        raw_text=row["raw_text"],
        city=row["city"],
        city_cn=row["city_cn"],
        station_id=row["station_id"],
        source=row["source"],
        variable=row["variable"],
        bucket_center_c=Decimal(str(row["bucket_center_c"])),
        bucket_lower_c=Decimal(str(row["bucket_lower_c"])),
        bucket_upper_c=Decimal(str(row["bucket_upper_c"])),
        target_date=row["target_date"],
        settlement_timezone=row["settlement_timezone"],
        confidence=float(row["confidence"]),
        tradable=bool(row["tradable"]),
        rejection_reason=row["rejection_reason"],
    )


def _candidate_rule(rule: ChinaTemperatureBucketRule | GlobalTemperatureBucketRule):
    return type(
        "CandidateRule",
        (),
        {
            "tradable": rule.tradable,
            "rejection_reason": rule.rejection_reason,
        },
    )()


def _module_id(market: Row) -> str:
    return market["module_id"] or "weather"


def _model_members_from_forecast(forecast: object) -> dict[str, list[Decimal]] | None:
    payload = getattr(forecast, "raw_payload", None)
    if not isinstance(payload, dict):
        return None
    raw_models = payload.get("model_members")
    if not isinstance(raw_models, dict):
        return None
    models: dict[str, list[Decimal]] = {}
    for model, raw_members in raw_models.items():
        if not isinstance(raw_members, list):
            continue
        members = [Decimal(str(value)) for value in raw_members if value is not None]
        if members:
            models[str(model)] = members
    return models or None


def _analysis_strategy_score(row: Row) -> Decimal:
    return Decimal(str(row["edge"] or 0))


def _analysis_consensus_probability(row: Row) -> Decimal | None:
    raw_reasons = row["reasons"]
    try:
        reasons = json.loads(raw_reasons) if isinstance(raw_reasons, str) else raw_reasons
    except (json.JSONDecodeError, TypeError):
        reasons = []
    for reason in reasons if isinstance(reasons, (list, tuple)) else []:
        text = str(reason)
        if not text.startswith("consensus_probability_median="):
            continue
        try:
            return Decimal(text.partition("=")[2].strip())
        except Exception:
            return None
    lower = Decimal(str(row["fair_lower"]))
    upper = Decimal(str(row["fair_upper"]))
    return (lower + upper) / Decimal("2")


def _forecast_horizon(rule: GlobalTemperatureBucketRule, now: datetime) -> str:
    timezone_name = _rule_timezone(rule)
    if not timezone_name or not rule.target_date:
        return "unknown"
    try:
        effective_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        local_day = effective_now.astimezone(ZoneInfo(timezone_name)).date()
        target_day = datetime.fromisoformat(str(rule.target_date)).date()
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return "unknown"
    delta = (target_day - local_day).days
    return f"D{delta}" if delta in {0, 1, 2} else "other"


def _forecast_lead_hours(
    rule: GlobalTemperatureBucketRule,
    now: datetime,
    d0_context: dict[str, Any] | None,
) -> Decimal | None:
    timezone_name = _rule_timezone(rule)
    if not timezone_name or not rule.target_date:
        return None
    try:
        zone = ZoneInfo(timezone_name)
        effective_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        target_day = datetime.fromisoformat(str(rule.target_date)).date()
        target_peak = datetime(
            target_day.year,
            target_day.month,
            target_day.day,
            15,
            tzinfo=zone,
        )
        if d0_context and d0_context.get("remaining_peak_time"):
            parsed_peak = datetime.fromisoformat(
                str(d0_context["remaining_peak_time"]).replace("Z", "+00:00")
            )
            target_peak = (
                parsed_peak.replace(tzinfo=zone)
                if parsed_peak.tzinfo is None
                else parsed_peak.astimezone(zone)
            )
        lead = Decimal(str((target_peak - effective_now.astimezone(zone)).total_seconds() / 3600))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None
    return lead.quantize(Decimal("0.1"))


def _forecast_calibration_phase(
    rule: GlobalTemperatureBucketRule,
    now: datetime,
    d0_context: dict[str, Any] | None,
) -> str:
    horizon = _forecast_horizon(rule, now)
    lead = _forecast_lead_hours(rule, now, d0_context)
    if horizon == "D0":
        if d0_context and bool(d0_context.get("post_forecast_peak")):
            return "D0_post_peak"
        if d0_context:
            try:
                hours_to_peak = Decimal(str(d0_context.get("hours_to_remaining_peak")))
            except (TypeError, ValueError):
                hours_to_peak = None
            if hours_to_peak is not None and hours_to_peak <= Decimal("2"):
                return "D0_near_peak"
        timezone_name = _rule_timezone(rule)
        try:
            local_hour = now.astimezone(ZoneInfo(str(timezone_name))).hour
        except (ValueError, ZoneInfoNotFoundError):
            local_hour = 12
        if local_hour < 9:
            return "D0_early"
        if d0_context is None and local_hour >= 16:
            return "D0_late_unobserved"
        return "D0_pre_peak"
    if horizon == "D1":
        return "D1_late" if lead is not None and lead < Decimal("36") else "D1_early"
    if horizon == "D2":
        return "D2_late" if lead is not None and lead < Decimal("60") else "D2_early"
    return horizon


def _global_weather_cache_ttl(
    rule: GlobalTemperatureBucketRule,
    now: datetime,
) -> timedelta:
    return (
        GLOBAL_WEATHER_D0_CACHE_TTL
        if _forecast_horizon(rule, now) == "D0"
        else GLOBAL_WEATHER_LATER_CACHE_TTL
    )


def _rule_timezone(rule: GlobalTemperatureBucketRule) -> str | None:
    return str(rule.settlement_timezone or "") or resolve_market_timezone(
        title=rule.raw_text,
        location_hint=rule.station or rule.location,
    )


def _d0_hourly_sigma(context: dict[str, Any], unit: str) -> Decimal:
    post_peak = bool(context.get("post_forecast_peak"))
    try:
        hours_to_peak = Decimal(str(context.get("hours_to_remaining_peak") or 0))
    except Exception:
        hours_to_peak = Decimal("6")
    if unit.upper() == "C":
        if post_peak:
            base = Decimal("0.60")
        elif hours_to_peak <= 2:
            base = Decimal("0.70")
        else:
            base = Decimal("0.85")
        error_cap = Decimal("1.00")
    else:
        if post_peak:
            base = Decimal("1.10")
        elif hours_to_peak <= 2:
            base = Decimal("1.30")
        else:
            base = Decimal("1.55")
        error_cap = Decimal("1.80")
    try:
        anchor_error = abs(Decimal(str(context.get("forecast_anchor_error") or 0)))
    except Exception:
        anchor_error = Decimal("0")
    return base + min(anchor_error * Decimal("0.25"), error_cap)


def _unsupported_variable(
    rule: ResolutionRule | ChinaTemperatureBucketRule | GlobalTemperatureBucketRule,
) -> bool:
    if isinstance(rule, ChinaTemperatureBucketRule):
        return rule.variable != "temperature_high"
    if isinstance(rule, GlobalTemperatureBucketRule):
        return rule.variable != "temperature_high"
    return rule.variable is None


def _latest_observation_at(payload: dict[str, object]) -> datetime | None:
    timestamps: list[datetime] = []
    direct = payload.get("latest_observation_at")
    if direct:
        parsed = _parse_payload_datetime(direct)
        if parsed is not None:
            timestamps.append(parsed)
    observations = payload.get("observations")
    if isinstance(observations, list):
        for item in observations:
            if not isinstance(item, dict):
                continue
            parsed = _parse_payload_datetime(item.get("timestamp"))
            if parsed is not None:
                timestamps.append(parsed)
    return max(timestamps, default=None)


def _parse_payload_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pricing_reference(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    references = payload.get("pricing_references")
    if not isinstance(references, dict):
        return None
    reference = references.get(name)
    return reference if isinstance(reference, dict) else None


def _awc_taf_reference_state(
    payload: dict[str, Any],
    *,
    now: datetime,
) -> tuple[bool, str]:
    reference = _pricing_reference(payload, "awc_taf")
    if reference is None or reference.get("status") == "unavailable":
        return False, "unavailable"
    cache_status = str(reference.get("provider_cache_status") or "").strip()
    if cache_status not in {"network_fresh", "fresh_cache"}:
        return False, f"cache_{cache_status or 'unknown'}"
    issue_time = _parse_payload_datetime(reference.get("issue_time"))
    if issue_time is None:
        return False, "issue_time_missing"
    effective_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age = effective_now.astimezone(timezone.utc) - issue_time
    if age < -timedelta(minutes=5):
        return False, "issue_time_in_future"
    if age > timedelta(seconds=AWC_TAF_STALE_SECONDS):
        return False, "issue_time_stale"
    return True, "fresh"


def _merge_station_observations(
    market_id: str,
    rule: GlobalTemperatureBucketRule,
    evidence: list[tuple[WeatherObservation, dict[str, Any]]],
    errors: list[str],
) -> tuple[WeatherObservation, dict[str, Any]]:
    """Merge NWS/AWC copies into one exact-station evidence stream."""
    expected_station = str(rule.station or "").upper()
    records: dict[tuple[str, str], dict[str, Any]] = {}
    warnings: list[str] = []
    source_names: list[str] = []
    timezone_name = None
    for observation, payload in evidence:
        station = str(payload.get("station") or observation.station or "").upper()
        if expected_station and station != expected_station:
            raise ValueError(
                f"official observation station mismatch: expected {expected_station}, got {station}"
            )
        source_names.append(str(payload.get("source") or observation.provider))
        timezone_name = timezone_name or payload.get("timezone")
        warnings.extend(str(item) for item in payload.get("warnings") or [])
        for item in payload.get("observations") or []:
            if not isinstance(item, dict) or not item.get("timestamp") or item.get("value") is None:
                continue
            key = (station, str(item["timestamp"]))
            existing = records.get(key)
            if existing is None or (
                existing.get("quality_status") != "V" and item.get("quality_status") == "V"
            ):
                records[key] = {**item, "station": station}
    if not records:
        selected = max(evidence, key=lambda item: item[0].value)
        if rule.variable == "temperature_low":
            selected = min(evidence, key=lambda item: item[0].value)
        return selected

    normalized: list[tuple[Decimal, datetime, dict[str, Any]]] = []
    for item in records.values():
        parsed = _parse_payload_datetime(item["timestamp"])
        if parsed is None:
            continue
        value = normalize_value(
            Decimal(str(item["value"])),
            rule.variable or "temperature_high",
            str(item.get("unit") or rule.unit or "C"),
            str(rule.unit or "C"),
        )
        normalized.append((value, parsed, item))
    if not normalized:
        raise ValueError("official station evidence has no timestamped temperature samples")
    selected_value, selected_at, selected_item = max(normalized, key=lambda item: item[0])
    if rule.variable == "temperature_low":
        selected_value, selected_at, selected_item = min(normalized, key=lambda item: item[0])
    quality = "V" if selected_item.get("quality_status") == "V" else "AWC"
    observation = WeatherObservation(
        market_id=market_id,
        provider="official-station-observations",
        station=expected_station,
        variable=rule.variable or "temperature_high",
        value=selected_value,
        unit=rule.unit or "C",
        observed_at=selected_at,
        quality_status=quality,
        fetched_at=datetime.now(timezone.utc),
    )
    compact = [
        {
            **item,
            "value": str(value),
            "unit": rule.unit or "C",
            "timestamp": observed_at.isoformat(),
        }
        for value, observed_at, item in sorted(normalized, key=lambda entry: entry[1])
    ]
    return observation, {
        "source": "merged-exact-station-observations",
        "sources": sorted(set(source_names)),
        "station": expected_station,
        "target_date": rule.target_date,
        "timezone": timezone_name,
        "source_grade": SETTLEMENT_OBSERVATION,
        "official_signal": True,
        "settlement_source": False,
        "settlement_proxy": True,
        "settlement_provider": "wunderground",
        "settlement_alignment": "exact_station_reports",
        "observation_count": len(compact),
        "sample_count": len(compact),
        "extrema_method": "sample_extrema_of_deduplicated_exact_station_observations",
        "selected_observation": selected_item,
        "latest_observation_at": max(item[1] for item in normalized).isoformat(),
        "observations": compact,
        "warnings": list(dict.fromkeys(warnings + errors)),
    }


def _condition_d0_hourly_context(
    hourly: dict[str, Any],
    *,
    observation_context: D0ObservationContext,
    rule: GlobalTemperatureBucketRule,
    now: datetime,
) -> dict[str, Any]:
    observation = observation_context.observation
    if observation is None:
        raise ValueError("D0 hourly conditioning requires an official observation")
    observed_max = normalize_value(
        observation.value,
        observation.variable,
        observation.unit,
        str(rule.unit),
    )
    remaining_peak = Decimal(str(hourly["remaining_peak"]))
    raw_observations = (observation_context.raw_payload or {}).get("observations") or []
    timeline: list[tuple[datetime, Decimal]] = []
    for item in raw_observations:
        if not isinstance(item, dict) or item.get("value") is None:
            continue
        observed_at = _parse_payload_datetime(item.get("timestamp"))
        if observed_at is None:
            continue
        value = normalize_value(
            Decimal(str(item["value"])),
            observation.variable,
            str(item.get("unit") or observation.unit),
            str(rule.unit),
        )
        timeline.append((observed_at, value))
    timeline.sort(key=lambda item: item[0])
    current_temperature = timeline[-1][1] if timeline else observed_max
    trend_per_hour = None
    if len(timeline) >= 2:
        earlier = next(
            (
                item
                for item in reversed(timeline[:-1])
                if timeline[-1][0] - item[0] >= timedelta(minutes=30)
            ),
            timeline[-2],
        )
        elapsed_hours = Decimal(
            str(max((timeline[-1][0] - earlier[0]).total_seconds() / 3600, 1 / 60))
        )
        trend_per_hour = (timeline[-1][1] - earlier[1]) / elapsed_hours
    latest_at = _latest_observation_at(observation_context.raw_payload or {})
    anchor_at = timeline[-1][0] if timeline else latest_at
    forecast_timeline = _hourly_temperature_timeline(hourly)
    forecast_at_observation = (
        _interpolate_hourly_temperature(forecast_timeline, anchor_at)
        if anchor_at is not None
        else None
    )
    anchor_error = (
        current_temperature - forecast_at_observation
        if forecast_at_observation is not None
        else None
    )
    conditioned_peak = max(observed_max, remaining_peak)
    max_warming_rate = None
    trajectory_limited = False
    trajectory_upper_bound = None
    if anchor_at is not None and forecast_at_observation is not None:
        max_warming_rate = _d0_max_warming_rate(
            trend_per_hour=trend_per_hour,
            unit=str(rule.unit),
            post_peak=bool(hourly.get("post_forecast_peak")),
        )
        adjusted_temperatures: list[Decimal] = []
        for forecast_at, forecast_temperature in forecast_timeline:
            if forecast_at < anchor_at:
                continue
            elapsed_hours = Decimal(str(max((forecast_at - anchor_at).total_seconds() / 3600, 0)))
            # A same-day station miss is evidence about the entire remaining
            # trajectory. Keep the current bias until later observations show
            # that the forecast has caught up; decaying it within a few hours
            # recreated the original overconfident peak.
            bias_adjusted = forecast_temperature + anchor_error
            warming_ceiling = current_temperature + max_warming_rate * elapsed_hours
            adjusted = min(bias_adjusted, warming_ceiling)
            if adjusted < forecast_temperature - Decimal("0.05"):
                trajectory_limited = True
            adjusted_temperatures.append(adjusted)
        if adjusted_temperatures:
            conditioned_peak = max(observed_max, max(adjusted_temperatures))
            base_margin = Decimal("1.0") if str(rule.unit).upper() == "C" else Decimal("1.8")
            bias_margin = min(abs(anchor_error) * Decimal("0.25"), base_margin)
            trajectory_upper_bound = conditioned_peak + base_margin + bias_margin
    bucket_lower, bucket_upper = settlement_bucket_bounds(rule)
    observed_in_bucket = (
        bucket_lower is not None
        and (bucket_upper is None or observed_max < bucket_upper)
        and observed_max >= bucket_lower
    )
    cooling_amount = observed_max - current_temperature
    required_cooling = Decimal("0.5") if str(rule.unit).upper() == "C" else Decimal("0.9")
    peak_time = _parse_payload_datetime(hourly.get("all_day_forecast_peak_time"))
    minutes_after_peak = (
        Decimal(str((now - peak_time).total_seconds() / 60)) if peak_time is not None else None
    )
    peak_lock_confirmed = bool(
        hourly.get("post_forecast_peak")
        and observed_in_bucket
        and minutes_after_peak is not None
        and minutes_after_peak >= Decimal("30")
        and cooling_amount >= required_cooling
        and trend_per_hour is not None
        and trend_per_hour <= 0
        and bucket_upper is not None
        and conditioned_peak < bucket_upper
    )
    return {
        **hourly,
        "station": observation.station,
        "observed_max": str(observed_max),
        "current_temperature": str(current_temperature),
        "recent_trend_per_hour": str(trend_per_hour) if trend_per_hour is not None else None,
        "conditioned_final_peak": str(conditioned_peak),
        "forecast_at_observation": (
            str(forecast_at_observation) if forecast_at_observation is not None else None
        ),
        "forecast_anchor_error": str(anchor_error) if anchor_error is not None else None,
        "max_warming_rate_per_hour": (
            str(max_warming_rate) if max_warming_rate is not None else None
        ),
        "trajectory_limited": trajectory_limited,
        "trajectory_upper_bound": (
            str(trajectory_upper_bound) if trajectory_upper_bound is not None else None
        ),
        "peak_lock_confirmed": peak_lock_confirmed,
        "peak_lock_observed_in_bucket": observed_in_bucket,
        "peak_lock_minutes_after_forecast_peak": (
            str(minutes_after_peak) if minutes_after_peak is not None else None
        ),
        "peak_lock_cooling_amount": str(cooling_amount),
        "peak_lock_required_cooling": str(required_cooling),
        "observation_age_seconds": (
            max(0, int((now - latest_at).total_seconds())) if latest_at else None
        ),
    }


def _hourly_temperature_timeline(
    hourly: dict[str, Any],
) -> list[tuple[datetime, Decimal]]:
    timeline: list[tuple[datetime, Decimal]] = []
    records = hourly.get("records")
    if not isinstance(records, list):
        return timeline
    for record in records:
        if not isinstance(record, dict) or record.get("temperature") is None:
            continue
        forecast_at = _parse_payload_datetime(record.get("time"))
        if forecast_at is None:
            continue
        try:
            temperature = Decimal(str(record["temperature"]))
        except Exception:
            continue
        timeline.append((forecast_at, temperature))
    timeline.sort(key=lambda item: item[0])
    return timeline


def _interpolate_hourly_temperature(
    timeline: list[tuple[datetime, Decimal]],
    target: datetime,
) -> Decimal | None:
    if not timeline:
        return None
    if target <= timeline[0][0]:
        return timeline[0][1]
    if target >= timeline[-1][0]:
        return timeline[-1][1]
    for index in range(1, len(timeline)):
        right_at, right_value = timeline[index]
        if target > right_at:
            continue
        left_at, left_value = timeline[index - 1]
        span = (right_at - left_at).total_seconds()
        if span <= 0:
            return right_value
        ratio = Decimal(str((target - left_at).total_seconds() / span))
        return left_value + (right_value - left_value) * ratio
    return timeline[-1][1]


def _d0_max_warming_rate(
    *,
    trend_per_hour: Decimal | None,
    unit: str,
    post_peak: bool,
) -> Decimal:
    if unit.upper() == "C":
        baseline = Decimal("1.5")
        trend_margin = Decimal("0.75")
        physical_cap = Decimal("2.5")
        post_peak_cap = Decimal("0.75")
    else:
        baseline = Decimal("2.7")
        trend_margin = Decimal("1.35")
        physical_cap = Decimal("4.5")
        post_peak_cap = Decimal("1.35")
    positive_trend = max(trend_per_hour or Decimal("0"), Decimal("0"))
    rate = min(physical_cap, max(baseline, positive_trend + trend_margin))
    return min(rate, post_peak_cap) if post_peak else rate


def _stable_forecast_revision(
    forecast: ForecastSnapshot,
    raw_payload: dict[str, object],
) -> str:
    provider_revision = raw_payload.get("revision")
    if provider_revision:
        return f"provider:{provider_revision}"
    evidence = {
        "provider": forecast.provider,
        "location": forecast.location,
        "station": forecast.station,
        "variable": forecast.variable,
        "value": str(forecast.value),
        "lower_value": str(forecast.lower_value) if forecast.lower_value is not None else None,
        "upper_value": str(forecast.upper_value) if forecast.upper_value is not None else None,
        "unit": forecast.unit,
        "valid_time": forecast.valid_time.isoformat(),
        "payload": raw_payload,
    }
    canonical = json.dumps(
        evidence,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _source_signal_revision(
    forecast_revision: str,
    *,
    forecast_payload: dict[str, Any],
    observation: WeatherObservation | None,
) -> str:
    evidence = {"forecast_revision": forecast_revision}
    if observation is not None:
        evidence["observed_extrema"] = str(observation.value)
        evidence["observed_extrema_at"] = observation.observed_at.isoformat()
    hourly = forecast_payload.get("d0_hourly_context")
    if isinstance(hourly, dict):
        evidence["hourly_fetched_at"] = hourly.get("fetched_at")
        evidence["hourly_conditioned_final_peak"] = hourly.get("conditioned_final_peak")
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _signal_is_stale(
    signal: Row,
    *,
    now: datetime,
    max_age_seconds: int,
) -> bool:
    created_at = _parse_payload_datetime(signal["created_at"])
    if created_at is None:
        return True
    age = now.astimezone(timezone.utc) - created_at
    return age < -timedelta(minutes=5) or age > timedelta(seconds=max_age_seconds)


def _llm_observation_evidence(context: D0ObservationContext | None) -> dict[str, object]:
    if context is None or context.observation is None:
        return {}
    observation = context.observation
    return {
        "provider": observation.provider,
        "station": observation.station,
        "variable": observation.variable,
        "value": float(observation.value),
        "unit": observation.unit,
        "observed_at": observation.observed_at.isoformat(),
        "quality_status": observation.quality_status,
        "raw": context.raw_payload or {},
    }
