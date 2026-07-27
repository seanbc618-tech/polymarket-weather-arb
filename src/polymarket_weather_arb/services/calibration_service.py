from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from sqlite3 import Row
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polymarket_weather_arb.domain.execution import WEATHER_ENTRY_POLICY_VERSION
from polymarket_weather_arb.storage.repositories import Repository, is_predictive_model_version

_ENTRY_POLICY_MIN_RESOLVED_EVENTS = 20
_MODEL_SIZING_MIN_RESOLVED_EVENTS = 20
_MODEL_TRUST_CACHE_SECONDS = 300


@dataclass(frozen=True)
class CalibrationGroup:
    model_version: str
    forecast_provider: str
    horizon: str
    total_signals: int
    resolved_signals: int
    brier_score: Decimal | None
    hit_rate: Decimal | None
    average_edge: Decimal
    status: str
    effective_weight: Decimal
    distinct_events: int
    malformed_rate: Decimal
    weight_reason: str


@dataclass(frozen=True)
class CalibrationReport:
    groups: list[CalibrationGroup]


@dataclass(frozen=True)
class CalibrationTrust:
    model_version: str | None
    forecast_provider: str | None
    horizon: str
    total_signals: int
    resolved_signals: int
    brier_score: Decimal | None
    hit_rate: Decimal | None
    status: str
    effective_weight: Decimal
    distinct_events: int
    malformed_rate: Decimal
    weight_reason: str


@dataclass(frozen=True)
class WeatherSourceCalibration:
    weights: dict[str, Decimal]
    distinct_events: dict[str, int]
    brier_scores: dict[str, Decimal | None]
    reason: str


@dataclass(frozen=True)
class WeatherSourceBiasCalibration:
    biases: dict[str, Decimal]
    sigmas: dict[str, Decimal]
    samples: dict[str, int]
    scopes: dict[str, str]
    reason: str


@dataclass(frozen=True)
class EntryPerformanceCalibration:
    entry_policy_version: str
    policy_samples: int
    horizon: str
    price_band: str
    horizon_samples: int
    price_band_samples: int
    horizon_win_rate: Decimal | None
    price_band_win_rate: Decimal | None
    multiplier: Decimal
    reason: str


@dataclass(frozen=True)
class D0ModelSizingCalibration:
    model_version: str
    forecast_provider: str | None
    distinct_events: int
    brier_score: Decimal | None
    hit_rate: Decimal | None
    multiplier: Decimal
    reason: str


class CalibrationService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self._trust_cache: dict[tuple[str, str, str], tuple[float, CalibrationTrust]] = {}

    def report(self) -> CalibrationReport:
        # Calibration must include historical resolved samples. A newest-N
        # window silently drops older outcomes once pending research signals
        # accumulate, making a trained model appear permanently uncalibrated.
        rows = [
            row
            for row in self.repository.list_model_signals(limit=None)
            if is_predictive_model_version(row["model_version"])
        ]
        grouped: dict[tuple[str, str, str], list[Row]] = {}
        for row in rows:
            key = (
                row["model_version"],
                row["forecast_provider"] or "unknown",
                _row_horizon(row),
            )
            grouped.setdefault(key, []).append(row)
        groups = [
            _group_from_rows(model_version, forecast_provider, horizon, signals)
            for (model_version, forecast_provider, horizon), signals in sorted(grouped.items())
        ]
        return CalibrationReport(groups=groups)

    def trust_for_latest_signal(self, market_id: str) -> CalibrationTrust:
        latest = self.repository.latest_model_signal(market_id)
        if latest is None:
            return CalibrationTrust(
                model_version=None,
                forecast_provider=None,
                horizon="unknown",
                total_signals=0,
                resolved_signals=0,
                brier_score=None,
                hit_rate=None,
                status="unknown",
                effective_weight=Decimal("0"),
                distinct_events=0,
                malformed_rate=Decimal("0"),
                weight_reason="no model signal",
            )
        return self.trust_for_model(
            model_version=latest["model_version"],
            forecast_provider=latest["forecast_provider"] or "unknown",
            horizon=_row_horizon(latest),
        )

    def trust_for_model(
        self,
        *,
        model_version: str,
        forecast_provider: str,
        horizon: str | None = None,
    ) -> CalibrationTrust:
        cache_key = (model_version, forecast_provider, horizon) if horizon is not None else None
        if cache_key is not None:
            cached = self._trust_cache.get(cache_key)
            if cached is not None and cached[0] > monotonic():
                return cached[1]
        rows = self.repository.list_model_signals(
            limit=None,
            model_version=model_version,
            forecast_provider=forecast_provider,
        )
        rows = [row for row in rows if is_predictive_model_version(row["model_version"])]
        available_horizons = sorted({_row_horizon(row) for row in rows})
        if horizon is None and len(available_horizons) == 1:
            horizon = available_horizons[0]
        if horizon is None:
            return CalibrationTrust(
                model_version=model_version,
                forecast_provider=forecast_provider,
                horizon="mixed",
                total_signals=len(rows),
                resolved_signals=0,
                brier_score=None,
                hit_rate=None,
                status="unknown",
                effective_weight=Decimal("0"),
                distinct_events=0,
                malformed_rate=Decimal("0"),
                weight_reason="forecast horizon is required for calibration trust",
            )
        rows = [row for row in rows if _row_horizon(row) == horizon]
        group = _group_from_rows(model_version, forecast_provider, horizon, rows)
        trust = CalibrationTrust(
            model_version=group.model_version,
            forecast_provider=group.forecast_provider,
            horizon=group.horizon,
            total_signals=group.total_signals,
            resolved_signals=group.resolved_signals,
            brier_score=group.brier_score,
            hit_rate=group.hit_rate,
            status=group.status,
            effective_weight=group.effective_weight,
            distinct_events=group.distinct_events,
            malformed_rate=group.malformed_rate,
            weight_reason=group.weight_reason,
        )
        if cache_key is not None:
            self._trust_cache[cache_key] = (
                monotonic() + _MODEL_TRUST_CACHE_SECONDS,
                trust,
            )
        return trust

    def weather_source_weights(
        self,
        *,
        city: str,
        horizon: str,
        providers: list[str],
        calibration_phase: str = "unknown",
    ) -> WeatherSourceCalibration:
        default = _default_weather_source_weights(providers)
        scopes = (
            (
                city,
                horizon,
                f"city={city} horizon={horizon} phase={calibration_phase}",
            ),
            (
                None,
                horizon,
                f"all cities horizon={horizon} phase={calibration_phase}",
            ),
        )
        last_distinct = {provider: 0 for provider in providers}
        last_briers: dict[str, Decimal | None] = {provider: None for provider in providers}
        for scoped_city, scoped_horizon, scope_label in scopes:
            rows = self.repository.list_resolved_weather_source_signals(
                city=scoped_city,
                horizon=scoped_horizon,
                calibration_phase=calibration_phase,
                providers=providers,
            )
            calibration, eligible = _weather_source_calibration(
                rows=rows,
                providers=providers,
                scope_label=scope_label,
            )
            last_distinct = calibration.distinct_events
            last_briers = calibration.brier_scores
            if eligible >= 2:
                return calibration
        return WeatherSourceCalibration(
            default,
            last_distinct,
            last_briers,
            "equal family-neutral weights until at least two peer sources have 20 distinct "
            "resolved events at city or all-city scope for the same forecast phase; "
            "uncalibrated AWC TAF remains advisory",
        )

    def weather_source_biases(
        self,
        *,
        city: str,
        station: str | None,
        horizon: str,
        unit: str,
        providers: list[str],
        calibration_phase: str = "unknown",
    ) -> WeatherSourceBiasCalibration:
        """Estimate shrunken additive temperature bias from resolved bucket events."""
        biases = {provider: Decimal("0") for provider in providers}
        sigmas: dict[str, Decimal] = {}
        samples = {provider: 0 for provider in providers}
        scopes = {provider: "uncalibrated" for provider in providers}
        pending = set(providers)
        scope_specs = []
        if station:
            scope_specs.append(
                (
                    None,
                    station,
                    10,
                    f"station={station} horizon={horizon} phase={calibration_phase}",
                )
            )
        # Additive temperature errors are local. A pooled all-city correction
        # can move an airport forecast in the wrong direction, so only exact
        # station or city history may alter the temperature itself.
        scope_specs.append(
            (city, None, 20, f"city={city} horizon={horizon} phase={calibration_phase}")
        )
        for scoped_city, scoped_station, minimum, label in scope_specs:
            if not pending:
                break
            rows = self.repository.list_resolved_weather_source_signals(
                city=scoped_city,
                station=scoped_station,
                horizon=horizon,
                calibration_phase=calibration_phase,
                unit=unit,
                providers=sorted(pending),
            )
            raw_biases = _weather_source_temperature_biases(rows, sorted(pending))
            for provider, values in raw_biases.items():
                samples[provider] = max(samples[provider], len(values))
                if len(values) < minimum:
                    continue
                raw_bias = _median(values)
                shrink = Decimal(len(values)) / (Decimal(len(values)) + Decimal("20"))
                cap = Decimal("1") if unit.upper() == "C" else Decimal("2")
                biases[provider] = min(cap, max(-cap, raw_bias * shrink))
                residuals = [value - raw_bias for value in values]
                robust_sigma = _median([abs(value) for value in residuals]) * Decimal("1.4826")
                sigma_floor = Decimal("0.35") if unit.upper() == "C" else Decimal("0.70")
                sigma_cap = Decimal("2.50") if unit.upper() == "C" else Decimal("4.50")
                sigmas[provider] = min(sigma_cap, max(sigma_floor, robust_sigma))
                scopes[provider] = label
                pending.discard(provider)
        calibrated = [provider for provider in providers if scopes[provider] != "uncalibrated"]
        reason = (
            "shrunken additive source bias by exact station/city and forecast horizon"
            if calibrated
            else (
                "zero bias until 10 station or 20 city resolved events are available for "
                "this forecast horizon; all-city additive bias is disabled"
            )
        )
        return WeatherSourceBiasCalibration(biases, sigmas, samples, scopes, reason)

    def entry_performance(
        self,
        *,
        horizon: str,
        reference_price: Decimal,
        entry_policy_version: str = WEATHER_ENTRY_POLICY_VERSION,
    ) -> EntryPerformanceCalibration:
        """Return a reduction-only sizing multiplier from resolved live entries."""
        reference_price = Decimal(str(reference_price))
        price_band = _entry_price_band(reference_price)
        observations: list[tuple[str, str, bool, Decimal]] = []
        for row in self.repository.list_resolved_live_bucket_entries(
            entry_policy_version=entry_policy_version
        ):
            row_horizon = _entry_horizon(row)
            if row_horizon not in {"D0", "D1", "D2"}:
                continue
            price = Decimal(str(row["entry_price"] or 0))
            if price <= 0:
                continue
            observations.append(
                (
                    row_horizon,
                    _entry_price_band(price),
                    str(row["resolved_outcome"]).lower() == "yes",
                    price,
                )
            )
        horizon_rows = [row for row in observations if row[0] == horizon]
        band_rows = [row for row in observations if row[1] == price_band]
        policy_samples = len(observations)
        if policy_samples < _ENTRY_POLICY_MIN_RESOLVED_EVENTS:
            multiplier = Decimal("1")
        else:
            horizon_multiplier = _performance_multiplier(horizon_rows)
            band_multiplier = _performance_multiplier(band_rows)
            multiplier = min(horizon_multiplier, band_multiplier)
        horizon_rate = _win_rate(horizon_rows)
        band_rate = _win_rate(band_rows)
        return EntryPerformanceCalibration(
            entry_policy_version=entry_policy_version,
            policy_samples=policy_samples,
            horizon=horizon,
            price_band=price_band,
            horizon_samples=len(horizon_rows),
            price_band_samples=len(band_rows),
            horizon_win_rate=horizon_rate,
            price_band_win_rate=band_rate,
            multiplier=multiplier,
            reason=(
                f"entry_policy_version={entry_policy_version} "
                f"policy_samples={policy_samples}/{_ENTRY_POLICY_MIN_RESOLVED_EVENTS}; "
                f"history multiplier={multiplier} horizon={horizon} "
                f"samples={len(horizon_rows)} win_rate={horizon_rate}; "
                f"price_band={price_band} samples={len(band_rows)} win_rate={band_rate}; "
                "legacy policy versions excluded; reduction-only after this policy "
                "has at least 20 resolved events"
            ),
        )

    def d0_model_sizing(
        self,
        *,
        market_id: str,
        model_version: str,
        horizon: str,
    ) -> D0ModelSizingCalibration:
        """Backward-compatible entry point for horizon-aware model sizing."""
        return self.weather_model_sizing(
            market_id=market_id,
            model_version=model_version,
            horizon=horizon,
        )

    def weather_model_sizing(
        self,
        *,
        market_id: str,
        model_version: str,
        horizon: str,
    ) -> D0ModelSizingCalibration:
        """Return a reduction-only multiplier from event-level horizon Brier history."""
        if horizon not in {"D0", "D1", "D2"}:
            return D0ModelSizingCalibration(
                model_version=model_version,
                forecast_provider=None,
                distinct_events=0,
                brier_score=None,
                hit_rate=None,
                multiplier=Decimal("1"),
                reason=f"weather-model Brier sizing inactive for horizon={horizon}",
            )
        latest = self.repository.latest_model_signal(
            market_id,
            model_version=model_version,
        )
        if latest is None:
            return D0ModelSizingCalibration(
                model_version=model_version,
                forecast_provider=None,
                distinct_events=0,
                brier_score=None,
                hit_rate=None,
                multiplier=Decimal("1"),
                reason=(
                    f"{horizon} weather-model Brier sizing neutral: "
                    "current model signal is unavailable"
                ),
            )
        signal_horizon = _row_horizon(latest)
        provider = str(latest["forecast_provider"] or "").strip() or None
        if signal_horizon != horizon or provider is None:
            return D0ModelSizingCalibration(
                model_version=model_version,
                forecast_provider=provider,
                distinct_events=0,
                brier_score=None,
                hit_rate=None,
                multiplier=Decimal("1"),
                reason=(
                    f"{horizon} weather-model Brier sizing neutral: current model signal "
                    f"lacks matching provider context (signal_horizon={signal_horizon})"
                ),
            )
        trust = self.trust_for_model(
            model_version=model_version,
            forecast_provider=provider,
            horizon=horizon,
        )
        multiplier = _model_brier_sizing_multiplier(
            distinct_events=trust.distinct_events,
            brier_score=trust.brier_score,
        )
        return D0ModelSizingCalibration(
            model_version=model_version,
            forecast_provider=provider,
            distinct_events=trust.distinct_events,
            brier_score=trust.brier_score,
            hit_rate=trust.hit_rate,
            multiplier=multiplier,
            reason=(
                f"{horizon} event-level Brier sizing model={model_version} provider={provider} "
                f"events={trust.distinct_events}/{_MODEL_SIZING_MIN_RESOLVED_EVENTS} "
                f"brier={trust.brier_score} hit_rate={trust.hit_rate} "
                f"multiplier={multiplier}; reduction-only"
            ),
        )


def _weather_source_calibration(
    *,
    rows: list[Row],
    providers: list[str],
    scope_label: str,
) -> tuple[WeatherSourceCalibration, int]:
    default = _default_weather_source_weights(providers)
    matching: dict[str, list[Row]] = {provider: [] for provider in providers}
    for row in rows:
        provider = str(row["forecast_provider"] or "")
        if provider not in matching:
            continue
        try:
            json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        matching[provider].append(row)

    distinct: dict[str, int] = {}
    briers: dict[str, Decimal | None] = {}
    for provider, provider_rows in matching.items():
        metrics = [_event_metrics(rows) for rows in _latest_event_groups(provider_rows).values()]
        scores = [metric[0] for metric in metrics if metric is not None]
        distinct[provider] = len(scores)
        briers[provider] = _average(scores)

    eligible = [
        provider
        for provider in providers
        if distinct.get(provider, 0) >= 20 and briers.get(provider) is not None
    ]
    if len(eligible) < 2:
        return WeatherSourceCalibration(
            default, distinct, briers, f"insufficient {scope_label}"
        ), len(eligible)
    skills = {
        provider: Decimal("1") / max(briers[provider], Decimal("0.01")) for provider in eligible
    }
    mean_skill = sum(skills.values()) / Decimal(len(skills))
    weights = dict(default)
    for provider in eligible:
        relative = skills[provider] / mean_skill
        shrink = Decimal(distinct[provider]) / (Decimal(distinct[provider]) + Decimal("50"))
        weights[provider] = min(
            Decimal("1.5"),
            max(Decimal("0.5"), Decimal("1") + (relative - Decimal("1")) * shrink),
        )
    eligible_mean = sum(weights[provider] for provider in eligible) / Decimal(len(eligible))
    for provider in eligible:
        weights[provider] = min(
            Decimal("1.5"), max(Decimal("0.5"), weights[provider] / eligible_mean)
        )
    return (
        WeatherSourceCalibration(
            weights,
            distinct,
            briers,
            f"bounded inverse-Brier skill using {scope_label}",
        ),
        len(eligible),
    )


def _default_weather_source_weights(providers: list[str]) -> dict[str, Decimal]:
    return {
        provider: (Decimal("0.35") if provider.casefold() == "reference_awc-taf" else Decimal("1"))
        for provider in providers
    }


def _group_from_rows(
    model_version: str,
    forecast_provider: str,
    horizon: str,
    rows: list[Row],
) -> CalibrationGroup:
    resolved = [
        row
        for row in rows
        if row["decision"] not in {"error", "invalid"}
        and row["outcome_status"] == "resolved"
        and row["resolved_outcome"] in {"yes", "no"}
    ]
    resolved_event_groups = _latest_event_groups(resolved)
    metrics = [_event_metrics(event_rows) for event_rows in resolved_event_groups.values()]
    valid_metrics = [metric for metric in metrics if metric is not None]
    brier_score = _average([metric[0] for metric in valid_metrics])
    hit_rate = _average([metric[1] for metric in valid_metrics])
    average_edge = _average([_decimal(row["edge"]) for row in rows]) or Decimal("0")

    num_distinct = len(valid_metrics)

    all_event_groups = _event_groups(rows)
    malformed_count = sum(
        any(row["decision"] in {"error", "invalid"} for row in event_rows)
        for event_rows in all_event_groups.values()
    )
    malformed_rate = (
        Decimal(malformed_count) / Decimal(len(all_event_groups))
        if all_event_groups
        else Decimal("0")
    )

    effective_weight, weight_reason = _calculate_weight_with_reason(
        num_distinct, brier_score, hit_rate, malformed_rate
    )

    return CalibrationGroup(
        model_version=model_version,
        forecast_provider=forecast_provider,
        horizon=horizon,
        total_signals=len(rows),
        resolved_signals=len(resolved),
        brier_score=brier_score,
        hit_rate=hit_rate,
        average_edge=average_edge,
        status=_status(num_distinct, brier_score, hit_rate),
        effective_weight=effective_weight,
        distinct_events=num_distinct,
        malformed_rate=_clean_decimal(malformed_rate),
        weight_reason=weight_reason,
    )


def _event_groups(rows: list[Row]) -> dict[str, list[Row]]:
    groups: dict[str, list[Row]] = {}
    for row in rows:
        event_identity = None
        try:
            payload = json.loads(row["raw_payload"])
            event_identity = payload.get("event_identity")
        except (TypeError, json.JSONDecodeError):
            pass
        key = f"event:{event_identity}" if event_identity else f"market:{row['market_id']}"
        groups.setdefault(key, []).append(row)
    return groups


def _latest_event_groups(rows: list[Row]) -> dict[str, list[Row]]:
    latest: dict[tuple[str, str], Row] = {}
    for event_key, event_rows in _event_groups(rows).items():
        for row in event_rows:
            key = (event_key, str(row["market_id"]))
            current = latest.get(key)
            if current is None or (str(row["created_at"]), int(row["id"])) > (
                str(current["created_at"]),
                int(current["id"]),
            ):
                latest[key] = row
    grouped: dict[str, list[Row]] = {}
    for (event_key, _market_id), row in latest.items():
        grouped.setdefault(event_key, []).append(row)
    return grouped


def _event_metrics(rows: list[Row]) -> tuple[Decimal, Decimal] | None:
    valid = [
        row
        for row in rows
        if row["resolved_outcome"] in {"yes", "no"} and row["decision"] not in {"error", "invalid"}
    ]
    winners = [row for row in valid if row["resolved_outcome"] == "yes"]
    if len(winners) != 1:
        return None
    probabilities = [
        min(Decimal("1"), max(Decimal("0"), _decimal(row["yes_probability"]))) for row in valid
    ]
    total = sum(probabilities, Decimal("0"))
    if total > Decimal("1"):
        probabilities = [probability / total for probability in probabilities]
        other_probability = Decimal("0")
    else:
        other_probability = Decimal("1") - total
    outcomes = [Decimal("1") if row["resolved_outcome"] == "yes" else Decimal("0") for row in valid]
    # Half the multiclass squared error preserves the familiar binary Brier
    # scale [0, 1] without letting events with more temperature buckets accrue
    # a mechanically larger score. Missing distribution mass is an explicit
    # losing "other" class rather than silently renormalized away.
    brier = (
        sum(
            ((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)),
            Decimal("0"),
        )
        + other_probability**2
    ) / Decimal("2")
    ranked = probabilities + [other_probability]
    top = max(ranked)
    top_indexes = [index for index, probability in enumerate(ranked) if probability == top]
    winner_index = next(index for index, outcome in enumerate(outcomes) if outcome == 1)
    hit = Decimal("1") if top_indexes == [winner_index] else Decimal("0")
    return _clean_decimal(brier), hit


def _hit(row: Row) -> bool:
    probability = _decimal(row["yes_probability"])
    predicted = "yes" if probability >= Decimal("0.5") else "no"
    return predicted == row["resolved_outcome"]


def _row_horizon(row: Row) -> str:
    try:
        payload = json.loads(row["raw_payload"])
    except (TypeError, json.JSONDecodeError):
        return "unknown"
    horizon = str(payload.get("horizon") or "unknown") if isinstance(payload, dict) else "unknown"
    return horizon if horizon in {"D0", "D1", "D2"} else "unknown"


def _weather_source_temperature_biases(
    rows: list[Row], providers: list[str]
) -> dict[str, list[Decimal]]:
    grouped: dict[str, list[Row]] = {provider: [] for provider in providers}
    for row in rows:
        provider = str(row["forecast_provider"] or "")
        if provider in grouped:
            grouped[provider].append(row)
    result: dict[str, list[Decimal]] = {provider: [] for provider in providers}
    for provider, provider_rows in grouped.items():
        for event_rows in _latest_event_groups(provider_rows).values():
            winners = [row for row in event_rows if row["resolved_outcome"] == "yes"]
            if len(winners) != 1:
                continue
            weighted_total = Decimal("0")
            probability_total = Decimal("0")
            usable = True
            for row in event_rows:
                try:
                    center = Decimal(str(row["rule_bucket_center"]))
                    payload = json.loads(row["raw_payload"])
                    raw_probability = (
                        payload.get("raw_yes_probability") if isinstance(payload, dict) else None
                    )
                    probability = Decimal(
                        str(row["yes_probability"] if raw_probability is None else raw_probability)
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    usable = False
                    break
                if probability < 0 or probability > 1:
                    usable = False
                    break
                weighted_total += center * probability
                probability_total += probability
            if not usable or probability_total < Decimal("0.5"):
                continue
            try:
                actual_center = Decimal(str(winners[0]["rule_bucket_center"]))
            except (TypeError, ValueError):
                continue
            predicted_center = weighted_total / probability_total
            result[provider].append(actual_center - predicted_center)
    return result


def _entry_horizon(row: Row) -> str:
    timezone_name = str(row["settlement_timezone"] or "")
    try:
        entered_at = datetime.fromisoformat(str(row["entered_at"]).replace("Z", "+00:00"))
        if entered_at.tzinfo is None:
            entered_at = entered_at.replace(tzinfo=timezone.utc)
        target_date = datetime.fromisoformat(str(row["target_date"])).date()
        local_day = entered_at.astimezone(ZoneInfo(timezone_name)).date()
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return "unknown"
    delta = (target_date - local_day).days
    return f"D{delta}" if delta in {0, 1, 2} else "other"


def _entry_price_band(price: Decimal) -> str:
    if price < Decimal("0.10"):
        return "lt_0.10"
    if price < Decimal("0.25"):
        return "0.10_0.24"
    return "gte_0.25"


def _performance_multiplier(rows: list[tuple[str, str, bool, Decimal]]) -> Decimal:
    if len(rows) < 5:
        return Decimal("1")
    wins = sum(1 for row in rows if row[2])
    smoothed_win_rate = Decimal(wins + 1) / Decimal(len(rows) + 2)
    mean_price = sum((row[3] for row in rows), Decimal("0")) / Decimal(len(rows))
    if smoothed_win_rate <= mean_price:
        return Decimal("0.50")
    if smoothed_win_rate <= mean_price + Decimal("0.05"):
        return Decimal("0.75")
    return Decimal("1")


def _model_brier_sizing_multiplier(
    *,
    distinct_events: int,
    brier_score: Decimal | None,
) -> Decimal:
    if distinct_events < _MODEL_SIZING_MIN_RESOLVED_EVENTS or brier_score is None:
        return Decimal("1")
    if brier_score > Decimal("0.35"):
        return Decimal("0.25")
    if brier_score > Decimal("0.27"):
        return Decimal("0.50")
    if brier_score > Decimal("0.24"):
        return Decimal("0.75")
    return Decimal("1")


def _win_rate(rows: list[tuple[str, str, bool, Decimal]]) -> Decimal | None:
    if not rows:
        return None
    return _clean_decimal(Decimal(sum(1 for row in rows if row[2])) / Decimal(len(rows)))


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return _clean_decimal(sum(values) / Decimal(len(values)))


def _status(
    resolved_count: int,
    brier_score: Decimal | None,
    hit_rate: Decimal | None,
) -> str:
    if resolved_count == 0 or brier_score is None or hit_rate is None:
        return "unknown"
    if resolved_count < 20:
        return "collecting"
    if resolved_count >= 20 and brier_score <= Decimal("0.20") and hit_rate >= Decimal("0.55"):
        return "trusted"
    if brier_score <= Decimal("0.25") and hit_rate >= Decimal("0.50"):
        return "promising"
    return "weak"


def _calculate_weight(
    num_distinct: int,
    brier_score: Decimal | None,
    hit_rate: Decimal | None,
    malformed_rate: Decimal,
) -> Decimal:
    return _calculate_weight_with_reason(num_distinct, brier_score, hit_rate, malformed_rate)[0]


def _calculate_weight_with_reason(
    num_distinct: int,
    brier_score: Decimal | None,
    hit_rate: Decimal | None,
    malformed_rate: Decimal,
) -> tuple[Decimal, str]:
    if brier_score is None or hit_rate is None:
        return Decimal("0"), "waiting for resolved events"
    if malformed_rate > Decimal("0.10"):
        return Decimal("0"), "malformed response rate exceeds 10%"
    if brier_score > Decimal("0.27"):
        return Decimal("0"), "Brier score exceeds 0.27"

    if num_distinct >= 100 and brier_score <= Decimal("0.20") and hit_rate >= Decimal("0.58"):
        return Decimal("0.50"), "100+ resolved events meet the 0.50 tier"
    if num_distinct >= 50 and brier_score <= Decimal("0.22") and hit_rate >= Decimal("0.55"):
        return Decimal("0.25"), "50+ resolved events meet the 0.25 tier"
    if num_distinct >= 20 and brier_score <= Decimal("0.24") and hit_rate >= Decimal("0.52"):
        return Decimal("0.10"), "20+ resolved events meet the 0.10 tier"

    if num_distinct < 20:
        return Decimal("0"), f"needs {20 - num_distinct} more distinct resolved events"
    return Decimal("0"), "resolved quality does not meet a weight tier"


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _clean_decimal(value: Decimal) -> Decimal:
    text = format(value.normalize(), "f")
    return Decimal(text.rstrip("0").rstrip(".") if "." in text else text)
