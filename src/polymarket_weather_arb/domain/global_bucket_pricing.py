from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from math import erf, sqrt

from polymarket_weather_arb.domain.fees import expected_taker_fee_per_share
from polymarket_weather_arb.domain.global_temperature_bucket import (
    GlobalTemperatureBucketRule,
    observation_source_tolerance,
    settlement_bucket_bounds,
)
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.probability import (
    ProbabilityInterval,
    clamp_probability,
    widen_interval,
)
from polymarket_weather_arb.domain.weather import ForecastSnapshot, normalize_value
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION

MODEL_VERSION = "global-temp-bucket-normal-v1"
OBSERVED_MODEL_VERSION = "global-temp-bucket-observed-v1"
MULTIMODEL_VERSION = GLOBAL_BUCKET_MODEL_VERSION
MIN_CONSENSUS_FAMILIES = 3
MAX_ROBUST_DISPERSION = Decimal("0.25")
MAX_ENTRY_SPREAD_RATIO = Decimal("0.40")
MIN_ENTRY_SPREAD_ALLOWANCE = Decimal("0.02")
EXTREME_PRICE_THRESHOLD = Decimal("0.005")
EXTREME_PRICE_MIN_MODEL_PROBABILITY = Decimal("0.40")
LOW_PRICE_BUCKET_THRESHOLD = Decimal("0.10")
D0_STRONG_CONTRADICTION_PROBABILITY = Decimal("0.05")
TAF_STRONG_CONTRADICTION_PROBABILITY = Decimal("0.05")
CONSERVATIVE_PROBABILITY_QUANTILE = Decimal("0.25")
ADVISORY_SOURCE_WEIGHT = Decimal("0.35")


@dataclass(frozen=True)
class GlobalBucketPricingConfig:
    min_edge: Decimal = Decimal("0.05")
    slippage_buffer: Decimal = Decimal("0.01")


@dataclass(frozen=True)
class SourceFamilyConsensus:
    probabilities: dict[str, Decimal]
    weights: dict[str, Decimal]
    members: dict[str, tuple[str, ...]]
    advisory_families: frozenset[str]
    source_weights: dict[str, Decimal]

    @property
    def eligible_probabilities(self) -> dict[str, Decimal]:
        return {
            family: probability
            for family, probability in self.probabilities.items()
            if family not in self.advisory_families
        }

    @property
    def eligible_weights(self) -> dict[str, Decimal]:
        return {
            family: weight
            for family, weight in self.weights.items()
            if family not in self.advisory_families
        }


def weather_source_family(source: str) -> str:
    """Collapse correlated weather feeds into one independent evidence family."""
    normalized = source.casefold().replace("_", "-")
    if "awc-taf" in normalized or normalized.endswith("taf"):
        return "aviation-taf"
    if "hourly" in normalized:
        return "d0-hourly"
    if "ecmwf" in normalized or "aifs" in normalized:
        return "ecmwf"
    if any(name in normalized for name in ("gfs", "gefs", "ncep", "noaa", "nws")):
        return "ncep"
    if "icon" in normalized or "dwd" in normalized:
        return "dwd"
    if any(name in normalized for name in ("gem", "eccc", "cmc")):
        return "eccc"
    if "google-weather" in normalized or "reference-open-meteo" in normalized:
        return "consumer-reference"
    return normalized


def estimate_global_bucket_probability_interval(
    rule: GlobalTemperatureBucketRule,
    forecast: ForecastSnapshot,
    now: datetime | None = None,
    *,
    observed_max: Decimal | None = None,
    observed_max_unit: str | None = None,
    model_members: dict[str, list[Decimal]] | None = None,
    source_sigmas: dict[str, Decimal] | None = None,
    source_weights: dict[str, Decimal] | None = None,
) -> ProbabilityInterval:
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    if not rule.tradable:
        return ProbabilityInterval(
            Decimal("0"), Decimal("1"), ["rule is not tradable"], model_version=MODEL_VERSION
        )
    if rule.bucket_lower is None or rule.bucket_upper is None or rule.unit is None:
        return ProbabilityInterval(
            Decimal("0"), Decimal("1"), ["bucket rule is incomplete"], model_version=MODEL_VERSION
        )
    bucket_lower, bucket_upper = settlement_bucket_bounds(rule)
    if forecast.variable != rule.variable:
        return ProbabilityInterval(
            Decimal("0"),
            Decimal("1"),
            ["forecast variable does not match bucket rule"],
            model_version=MODEL_VERSION,
        )

    normalized_observed_max = _normalized_observed_max(
        rule,
        observed_max=observed_max,
        observed_max_unit=observed_max_unit,
    )
    if _observed_max_excludes_bucket(rule, normalized_observed_max):
        return ProbabilityInterval(
            Decimal("0"),
            Decimal("0"),
            [_observed_max_exclusion_reason(rule, normalized_observed_max)],
            model_version=(OBSERVED_MODEL_VERSION if not model_members else MULTIMODEL_VERSION),
        )

    if model_members:
        reference_sigma = _forecast_sigma(forecast, now, rule.unit)
        return _estimate_multimodel_interval(
            rule,
            model_members,
            observed_max=observed_max,
            observed_max_unit=observed_max_unit,
            reference_sigma=reference_sigma,
            source_sigmas=source_sigmas,
            source_weights=source_weights,
        )

    value = normalize_value(forecast.value, forecast.variable, forecast.unit, rule.unit)
    lower_value = (
        normalize_value(forecast.lower_value, forecast.variable, forecast.unit, rule.unit)
        if forecast.lower_value is not None
        else value - _default_mu_band(rule.unit)
    )
    upper_value = (
        normalize_value(forecast.upper_value, forecast.variable, forecast.unit, rule.unit)
        if forecast.upper_value is not None
        else value + _default_mu_band(rule.unit)
    )
    fetched_at = (
        forecast.fetched_at
        if forecast.fetched_at.tzinfo
        else forecast.fetched_at.replace(tzinfo=timezone.utc)
    )
    valid_time = (
        forecast.valid_time
        if forecast.valid_time.tzinfo
        else forecast.valid_time.replace(tzinfo=timezone.utc)
    )
    age_seconds = max(0.0, (now - fetched_at).total_seconds())
    hours_to_valid = max(0.0, (valid_time - now).total_seconds() / 3600)
    sigma = _sigma(hours_to_valid, rule.unit)
    if age_seconds > 6 * 3600:
        sigma += _default_mu_band(rule.unit)
        reasons.append("forecast is older than six hours")

    model_version = MODEL_VERSION
    if normalized_observed_max is not None:
        model_version = OBSERVED_MODEL_VERSION

    probability_fn = _bucket_probability
    probability_args: tuple[Decimal, ...] = ()
    if normalized_observed_max is not None:
        effective_observed_floor = normalized_observed_max - observation_source_tolerance(rule.unit)
        probability_fn = _bucket_probability_above_observed_floor
        probability_args = (effective_observed_floor,)
    else:
        effective_observed_floor = None

    fair = probability_fn(
        value,
        bucket_lower,
        bucket_upper,
        sigma,
        *probability_args,
    )
    conservative_sigma = sigma + _default_mu_band(rule.unit)
    lower = min(
        probability_fn(
            lower_value,
            bucket_lower,
            bucket_upper,
            conservative_sigma,
            *probability_args,
        ),
        probability_fn(
            upper_value,
            bucket_lower,
            bucket_upper,
            conservative_sigma,
            *probability_args,
        ),
        fair,
    )
    upper = max(
        probability_fn(
            lower_value,
            bucket_lower,
            bucket_upper,
            conservative_sigma,
            *probability_args,
        ),
        probability_fn(
            upper_value,
            bucket_lower,
            bucket_upper,
            conservative_sigma,
            *probability_args,
        ),
        fair,
    )
    if normalized_observed_max is not None:
        reasons.append(
            f"D0 observed max-to-date={normalized_observed_max}{rule.unit}; "
            f"source-tolerant floor={effective_observed_floor}{rule.unit} "
            f"(tolerance={observation_source_tolerance(rule.unit)}{rule.unit})"
        )
    observation_excludes_bucket = (
        normalized_observed_max is not None
        and bucket_upper is not None
        and normalized_observed_max >= bucket_upper
    )
    if rule.confidence < 0.95 and not observation_excludes_bucket:
        lower, upper = widen_interval(lower, upper, Decimal("0.05"))
        reasons.append("bucket rule confidence below strict threshold")
    if not reasons:
        reasons.append(f"global temperature bucket model sigma={sigma}{rule.unit}")
    return ProbabilityInterval(
        lower=clamp_probability(lower),
        upper=clamp_probability(upper),
        reasons=reasons,
        model_version=model_version,
    )


def analyze_global_bucket_price(
    market_id: str,
    rule: GlobalTemperatureBucketRule,
    forecast: ForecastSnapshot,
    best_ask: Decimal | None,
    config: GlobalBucketPricingConfig | None = None,
    now: datetime | None = None,
    *,
    observed_max: Decimal | None = None,
    observed_max_unit: str | None = None,
    model_members: dict[str, list[Decimal]] | None = None,
    external_probability: Decimal | None = None,
    external_weight: Decimal = Decimal("0"),
    best_bid: Decimal | None = None,
    fees_enabled: bool = False,
    fee_rate: Decimal | None = None,
    source_weights: dict[str, Decimal] | None = None,
    source_sigmas: dict[str, Decimal] | None = None,
    conditioning_probability: Decimal | None = None,
    conditioning_weight: Decimal = Decimal("0"),
    d0_trajectory_upper_bound: Decimal | None = None,
    d0_post_peak: bool = False,
    d0_peak_lock_confirmed: bool | None = None,
    top_candidate_supporters: int | None = None,
    top_candidate_model_count: int | None = None,
) -> Analysis:
    config = config or GlobalBucketPricingConfig()
    interval = estimate_global_bucket_probability_interval(
        rule,
        forecast,
        now=now,
        observed_max=observed_max,
        observed_max_unit=observed_max_unit,
        model_members=model_members,
        source_sigmas=source_sigmas,
        source_weights=source_weights,
    )
    reasons = list(interval.reasons)

    normalized_observed_max = _normalized_observed_max(
        rule,
        observed_max=observed_max,
        observed_max_unit=observed_max_unit,
    )
    if _observed_max_excludes_bucket(rule, normalized_observed_max):
        return Analysis(
            market_id,
            interval.model_version,
            Decimal("0"),
            Decimal("0"),
            best_ask,
            Decimal("0"),
            None,
            "reject",
            reasons,
            fair_probability=Decimal("0"),
        )

    blended_lower = interval.lower
    blended_upper = interval.upper

    base_model_count = _independent_source_family_count(model_members, source_weights)
    conditioning_ratio = Decimal("0")
    if conditioning_probability is not None and base_model_count:
        bounded_conditioning_weight = min(max(conditioning_weight, Decimal("0")), Decimal("0.75"))
        # D0 hourly state is a conditioning distribution, not another forecast
        # family vote. Diluting 0.75 by the family count made a nominal 75%
        # observation weight contribute only ~13% with five models.
        conditioning_ratio = bounded_conditioning_weight
        if conditioning_ratio > 0:
            quant_ratio = Decimal("1") - conditioning_ratio
            blended_lower = (
                blended_lower * quant_ratio + conditioning_probability * conditioning_ratio
            )
            blended_upper = (
                blended_upper * quant_ratio + conditioning_probability * conditioning_ratio
            )
            reasons.append(
                f"D0 hourly conditioning probability={conditioning_probability:.4f} "
                f"weight={bounded_conditioning_weight:.4f} blend_ratio={conditioning_ratio:.4f}; "
                "excluded from independent-model quorum"
            )

    blend_ratio, base_models = _external_blend_ratio(
        model_members,
        external_weight,
        source_weights=source_weights,
    )
    if blend_ratio > 0 and external_probability is not None:
        quant_ratio = Decimal("1") - blend_ratio
        previous_lower = blended_lower
        previous_upper = blended_upper
        blended_lower = previous_lower * quant_ratio + external_probability * blend_ratio
        blended_upper = previous_upper * quant_ratio + external_probability * blend_ratio
        reasons.append(
            f"applied external_probability={external_probability:.4f} with weight={external_weight:.2f} "
            f"(blend_ratio={blend_ratio:.4f} against {base_models} base models, "
            f"blended lower from {previous_lower:.4f} to {blended_lower:.4f}, upper from {previous_upper:.4f} to {blended_upper:.4f})"
        )
    elif external_probability is not None:
        reasons.append(
            f"external_probability={external_probability:.4f} provided but weight is zero; "
            "pricing unchanged (calibration-only)"
        )
    probability_map: dict[str, Decimal] = {}
    if model_members and rule.unit is not None:
        _, probability_map, _ = _multimodel_probabilities(
            rule,
            model_members,
            observed_max=observed_max,
            observed_max_unit=observed_max_unit,
            reference_sigma=_forecast_sigma(forecast, now or datetime.now(timezone.utc), rule.unit),
            source_sigmas=source_sigmas,
        )
    model_probabilities = list(probability_map.values())
    family_consensus = _source_family_consensus(probability_map, source_weights)
    family_probability_map = family_consensus.eligible_probabilities
    family_weights = family_consensus.eligible_weights
    family_probabilities = list(family_probability_map.values())
    taf_probability = probability_map.get("reference_awc-taf")
    taf_weight = family_consensus.source_weights.get("reference_awc-taf")
    taf_calibrated = (
        taf_probability is not None
        and taf_weight is not None
        and taf_weight >= Decimal("0.5")
        and "aviation-taf" not in family_consensus.advisory_families
    )
    taf_not_strongly_contradictory = (
        taf_probability is None or taf_probability >= TAF_STRONG_CONTRADICTION_PROBABILITY
    )
    robust_dispersion = (
        _robust_probability_dispersion(family_probabilities)
        if family_probabilities
        else Decimal("0")
    )
    raw_disagreement = (
        max(family_probabilities) - min(family_probabilities)
        if family_probabilities
        else Decimal("0")
    )
    fair_mid = (
        _weighted_mean(family_probability_map, family_weights)
        if family_probabilities
        else (interval.lower + interval.upper) / Decimal("2")
    )
    consensus_probability = (
        _decimal_quantile(family_probabilities, Decimal("0.5"))
        if family_probabilities
        else fair_mid
    )
    decision_probability = (
        _decimal_quantile(family_probabilities, CONSERVATIVE_PROBABILITY_QUANTILE)
        if family_probabilities
        else interval.lower
    )
    if conditioning_ratio > 0 and conditioning_probability is not None:
        quant_ratio = Decimal("1") - conditioning_ratio
        fair_mid = fair_mid * quant_ratio + conditioning_probability * conditioning_ratio
        consensus_probability = (
            consensus_probability * quant_ratio + conditioning_probability * conditioning_ratio
        )
        decision_probability = (
            decision_probability * quant_ratio + conditioning_probability * conditioning_ratio
        )
    if blend_ratio > 0 and external_probability is not None:
        quant_ratio = Decimal("1") - blend_ratio
        fair_mid = fair_mid * quant_ratio + external_probability * blend_ratio
        consensus_probability = (
            consensus_probability * quant_ratio + external_probability * blend_ratio
        )
        decision_probability = (
            decision_probability * quant_ratio + external_probability * blend_ratio
        )
    if best_ask is None:
        return Analysis(
            market_id,
            interval.model_version,
            blended_lower,
            blended_upper,
            None,
            Decimal("0"),
            None,
            "reject",
            reasons + ["missing ask"],
            fair_probability=fair_mid,
        )
    if "forecast variable does not match bucket rule" in reasons:
        return Analysis(
            market_id,
            interval.model_version,
            blended_lower,
            blended_upper,
            best_ask,
            Decimal("0"),
            None,
            "reject",
            reasons,
            fair_probability=fair_mid,
        )
    if observed_max is not None and blended_upper == 0:
        return Analysis(
            market_id,
            interval.model_version,
            blended_lower,
            blended_upper,
            best_ask,
            Decimal("0"),
            None,
            "reject",
            reasons,
            fair_probability=fair_mid,
        )
    rate = fee_rate if fees_enabled and fee_rate is not None else Decimal("0")
    entry_fee = expected_taker_fee_per_share(price=best_ask, fee_rate=rate)
    exit_fee_reference = best_bid if best_bid is not None else best_ask
    exit_fee = expected_taker_fee_per_share(price=exit_fee_reference, fee_rate=rate)
    fee_drag = entry_fee + exit_fee
    support_threshold = best_ask + config.slippage_buffer + fee_drag
    supporting_models = sum(probability >= support_threshold for probability in model_probabilities)
    model_count = len(model_probabilities)
    required_model_supporters = (2 * model_count + 2) // 3 if model_count else 0
    model_support_ratio = (
        Decimal(supporting_models) / Decimal(model_count) if model_count else Decimal("0")
    )
    supporting_families = sum(
        probability >= support_threshold for probability in family_probabilities
    )
    family_count = len(family_probabilities)
    required_family_supporters = (2 * family_count + 2) // 3 if family_count else 0
    support_ratio = (
        Decimal(supporting_families) / Decimal(family_count) if family_count else Decimal("0")
    )
    total_weight = sum(family_weights.values(), Decimal("0"))
    supporting_weight = sum(
        family_weights[family]
        for family, probability in family_probability_map.items()
        if probability >= support_threshold
    )
    weighted_support_ratio = supporting_weight / total_weight if total_weight > 0 else Decimal("0")
    gross_edge = decision_probability - best_ask - config.slippage_buffer
    edge = gross_edge - fee_drag
    market_midpoint = (best_bid + best_ask) / Decimal("2") if best_bid is not None else best_ask
    entry_spread = max(best_ask - best_bid, Decimal("0")) if best_bid is not None else None
    entry_spread_allowance = max(
        MIN_ENTRY_SPREAD_ALLOWANCE,
        best_ask * MAX_ENTRY_SPREAD_RATIO,
    )
    entry_spread_ok = entry_spread is None or entry_spread <= entry_spread_allowance
    consensus_reasons = [
        f"consensus_probability_median={consensus_probability:.4f}",
        f"decision_probability_conservative={decision_probability:.4f}",
        f"model_risk_haircut={max(Decimal('0'), consensus_probability - decision_probability):.4f}",
        f"supporting_models={supporting_models}/{model_count} required={required_model_supporters}",
        f"model_support_ratio={model_support_ratio:.4f}",
        f"supporting_families={supporting_families}/{family_count} "
        f"required={required_family_supporters}",
        f"support_ratio={support_ratio:.4f}",
        f"weighted_support_ratio={weighted_support_ratio:.4f}",
        "source_weights="
        + ",".join(
            f"{model}:{weight:.4f}" for model, weight in family_consensus.source_weights.items()
        ),
        "family_weights="
        + ",".join(f"{family}:{weight:.4f}" for family, weight in family_weights.items()),
        f"entry_robust_dispersion={robust_dispersion:.4f}",
        f"entry_max_robust_dispersion={MAX_ROBUST_DISPERSION:.4f}",
        f"entry_raw_disagreement={raw_disagreement:.4f}",
        "entry_absolute_probability_floor=disabled; conservative fee-aware edge is decisive",
        f"model_support_threshold={support_threshold:.4f}",
        f"decision_gross_edge={gross_edge:.4f}",
        f"decision_net_edge={edge:.4f}",
        f"consensus_gross_edge={gross_edge:.4f}",
        f"consensus_net_edge={edge:.4f}",
        f"market_baseline_midpoint={market_midpoint:.4f}",
        f"weather_vs_market_midpoint={fair_mid - market_midpoint:.4f}",
        "market_baseline_role=benchmark_only_until_v8_has_20_resolved_events",
        (
            f"entry_spread={entry_spread:.4f} allowance={entry_spread_allowance:.4f} "
            f"bid={best_bid} ask={best_ask}"
            if entry_spread is not None
            else "entry_spread=unknown because best bid is unavailable"
        ),
    ]
    if taf_probability is not None:
        consensus_reasons.extend(
            [
                (
                    "awc_taf_entry_role="
                    f"{'calibrated_vote_and_veto' if taf_calibrated else 'advisory_veto_only'} "
                    f"weight={taf_weight}"
                ),
                (
                    "awc_taf_strong_contradiction_check="
                    f"{taf_not_strongly_contradictory}; probability={taf_probability:.4f} "
                    f"minimum={TAF_STRONG_CONTRADICTION_PROBABILITY:.4f}"
                ),
            ]
        )
    if fees_enabled and rate > 0:
        consensus_reasons.append(
            f"net edge after taker fees rate={rate} entry_fee={entry_fee:.7f} "
            f"exit_fee={exit_fee:.7f} exit_reference={exit_fee_reference}"
        )
    if family_count < MIN_CONSENSUS_FAMILIES:
        return Analysis(
            market_id,
            interval.model_version,
            blended_lower,
            blended_upper,
            best_ask,
            edge,
            None,
            "watch",
            reasons
            + consensus_reasons
            + [
                "evidence_status=insufficient_models",
                f"requires at least {MIN_CONSENSUS_FAMILIES} independent source families",
            ],
            gross_edge=gross_edge,
            entry_fee_per_share=entry_fee,
            exit_fee_per_share=exit_fee,
            fair_probability=fair_mid,
        )
    # Compare the weighted two-thirds boundary exactly. A rounded decimal literal
    # made an exact 4/6 vote look slightly smaller than the configured threshold.
    weighted_quorum_met = total_weight > 0 and supporting_weight * Decimal(
        "3"
    ) >= total_weight * Decimal("2")
    quorum_met = supporting_families >= required_family_supporters and weighted_quorum_met
    dispersion_ok = robust_dispersion <= MAX_ROBUST_DISPERSION
    conditioning_not_strongly_contradictory = not (
        conditioning_probability is not None
        and conditioning_weight > 0
        and conditioning_probability < D0_STRONG_CONTRADICTION_PROBABILITY
    )
    if conditioning_probability is not None and conditioning_weight > 0:
        consensus_reasons.append(
            "D0 hourly strong-contradiction check="
            f"{conditioning_not_strongly_contradictory}; "
            f"probability={conditioning_probability:.4f} "
            f"minimum={D0_STRONG_CONTRADICTION_PROBABILITY:.4f}"
        )
    bucket_lower, bucket_upper = settlement_bucket_bounds(rule)
    trajectory_ok = not (
        d0_trajectory_upper_bound is not None
        and bucket_lower is not None
        and bucket_lower > d0_trajectory_upper_bound
    )
    if d0_trajectory_upper_bound is not None:
        consensus_reasons.append(
            f"D0 trajectory upper bound={d0_trajectory_upper_bound}{rule.unit}"
        )
    post_peak_bucket_ok = not (
        d0_post_peak
        and normalized_observed_max is not None
        and not _value_in_bucket(normalized_observed_max, bucket_lower, bucket_upper)
    )
    if d0_post_peak and normalized_observed_max is not None:
        consensus_reasons.append(
            f"D0 post-peak observed bucket check={post_peak_bucket_ok}; "
            f"observed_max={normalized_observed_max}{rule.unit} "
            f"bucket=[{bucket_lower},{bucket_upper}){rule.unit}"
        )
    post_peak_lock_ok = not (
        d0_post_peak
        and normalized_observed_max is not None
        and _value_in_bucket(normalized_observed_max, bucket_lower, bucket_upper)
        and d0_peak_lock_confirmed is not True
    )
    if d0_post_peak and normalized_observed_max is not None and post_peak_bucket_ok:
        consensus_reasons.append(
            "D0 post-peak target-bucket peak lock="
            f"{post_peak_lock_ok}; confirmed={d0_peak_lock_confirmed}"
        )
    extreme_price_agreement_ok = not (
        best_ask <= EXTREME_PRICE_THRESHOLD
        and (
            not family_probabilities
            or min(family_probabilities) < EXTREME_PRICE_MIN_MODEL_PROBABILITY
        )
    )
    if best_ask <= EXTREME_PRICE_THRESHOLD:
        consensus_reasons.append(
            f"extreme_price_agreement={extreme_price_agreement_ok}; ask={best_ask} "
            f"minimum_family_probability="
            f"{min(family_probabilities) if family_probabilities else None} "
            f"required={EXTREME_PRICE_MIN_MODEL_PROBABILITY}"
        )
    low_price_top_rank_ok = not (best_ask < LOW_PRICE_BUCKET_THRESHOLD) or (
        top_candidate_model_count is not None
        and top_candidate_model_count > 0
        and top_candidate_supporters is not None
        and top_candidate_supporters * 2 > top_candidate_model_count
    )
    if best_ask < LOW_PRICE_BUCKET_THRESHOLD:
        consensus_reasons.append(
            "low_price_top_candidate_majority="
            f"{low_price_top_rank_ok}; supporters={top_candidate_supporters}"
            f"/{top_candidate_model_count}; ask={best_ask}"
        )
    if (
        quorum_met
        and dispersion_ok
        and taf_not_strongly_contradictory
        and conditioning_not_strongly_contradictory
        and trajectory_ok
        and post_peak_bucket_ok
        and post_peak_lock_ok
        and extreme_price_agreement_ok
        and low_price_top_rank_ok
        and entry_spread_ok
        and edge >= config.min_edge
    ):
        return Analysis(
            market_id,
            interval.model_version,
            blended_lower,
            blended_upper,
            best_ask,
            edge,
            "buy_yes",
            "trade",
            reasons
            + consensus_reasons
            + [
                "two-thirds independent-family quorum, robust dispersion, entry spread, "
                "and conservative fee-aware net edge clear entry thresholds"
            ],
            gross_edge=gross_edge,
            entry_fee_per_share=entry_fee,
            exit_fee_per_share=exit_fee,
            fair_probability=fair_mid,
        )
    failed_checks = []
    if not quorum_met:
        failed_checks.append(
            "fewer than two-thirds of independent source families support the entry price"
        )
    if not dispersion_ok:
        failed_checks.append("robust model dispersion exceeds 0.25")
    if not taf_not_strongly_contradictory:
        failed_checks.append(
            "station-aligned TAF bucket probability is below 0.05 and strongly "
            "contradicts a new entry"
        )
    if not conditioning_not_strongly_contradictory:
        failed_checks.append(
            "D0 hourly bucket probability is below 0.05 and strongly contradicts a new entry"
        )
    if not trajectory_ok:
        failed_checks.append(
            f"D0 trajectory upper bound {d0_trajectory_upper_bound}{rule.unit} "
            f"is below bucket lower bound {bucket_lower}{rule.unit}"
        )
    if not post_peak_bucket_ok:
        failed_checks.append(
            "D0 forecast peak has passed and observed maximum is outside this bucket; "
            "do not open a neighboring bucket after the expected daily peak"
        )
    if not extreme_price_agreement_ok:
        failed_checks.append(
            f"extreme-price entry at {best_ask} requires every model probability at or above "
            f"{EXTREME_PRICE_MIN_MODEL_PROBABILITY}"
        )
    if not low_price_top_rank_ok:
        failed_checks.append(
            "bucket below 0.10 requires a strict majority of independent weather families "
            "to rank it as the event's first candidate"
        )
    if not entry_spread_ok:
        failed_checks.append(
            f"entry spread {entry_spread} exceeds allowed {entry_spread_allowance}; "
            "do not cross an illiquid bucket book"
        )
    if edge < config.min_edge:
        failed_checks.append("conservative fee-aware net edge does not clear minimum edge")
    return Analysis(
        market_id,
        interval.model_version,
        blended_lower,
        blended_upper,
        best_ask,
        edge,
        None,
        "watch",
        reasons + consensus_reasons + failed_checks,
        gross_edge=gross_edge,
        entry_fee_per_share=entry_fee,
        exit_fee_per_share=exit_fee,
        fair_probability=fair_mid,
    )


def _external_blend_ratio(
    model_members: dict[str, list[Decimal]] | None,
    external_weight: Decimal,
    *,
    source_weights: dict[str, Decimal] | None = None,
) -> tuple[Decimal, int]:
    """Translate a fractional model vote into its final ensemble share."""
    base_models = _independent_source_family_count(model_members, source_weights) or 1
    bounded_weight = min(max(external_weight, Decimal("0")), Decimal("0.5"))
    if bounded_weight == 0:
        return Decimal("0"), base_models
    return bounded_weight / (Decimal(base_models) + bounded_weight), base_models


def _independent_source_family_count(
    model_members: dict[str, list[Decimal]] | None,
    source_weights: dict[str, Decimal] | None,
) -> int:
    active_sources = {
        source: Decimal("0") for source, members in (model_members or {}).items() if members
    }
    return len(_source_family_consensus(active_sources, source_weights).eligible_probabilities)


def _estimate_multimodel_interval(
    rule: GlobalTemperatureBucketRule,
    model_members: dict[str, list[Decimal]],
    *,
    observed_max: Decimal | None,
    observed_max_unit: str | None,
    reference_sigma: Decimal,
    source_sigmas: dict[str, Decimal] | None = None,
    source_weights: dict[str, Decimal] | None = None,
) -> ProbabilityInterval:
    if rule.bucket_lower is None or rule.bucket_upper is None or rule.unit is None:
        return ProbabilityInterval(
            Decimal("0"), Decimal("1"), ["bucket rule is incomplete"], MULTIMODEL_VERSION
        )
    observed_floor, model_probabilities, total_members = _multimodel_probabilities(
        rule,
        model_members,
        observed_max=observed_max,
        observed_max_unit=observed_max_unit,
        reference_sigma=reference_sigma,
        source_sigmas=source_sigmas,
    )
    if _observed_max_excludes_bucket(rule, observed_floor):
        return ProbabilityInterval(
            Decimal("0"),
            Decimal("0"),
            [_observed_max_exclusion_reason(rule, observed_floor)],
            MULTIMODEL_VERSION,
        )
    if not model_probabilities:
        return ProbabilityInterval(
            Decimal("0"), Decimal("1"), ["multi-model forecast has no members"], MULTIMODEL_VERSION
        )

    consensus = _source_family_consensus(model_probabilities, source_weights)
    eligible = consensus.eligible_probabilities
    eligible_weights = consensus.eligible_weights
    if not eligible:
        return ProbabilityInterval(
            Decimal("0"),
            Decimal("1"),
            ["multi-model forecast has no independent calibrated source families"],
            MULTIMODEL_VERSION,
        )
    probabilities = list(eligible.values())
    fair = _weighted_mean(eligible, eligible_weights)
    disagreement = max(probabilities) - min(probabilities)
    # A raw min/max range lets one deterministic point forecast collapse the
    # lower bound. MAD/IQR preserve genuine broad disagreement while resisting
    # one outlying model-level vote.
    robust_dispersion = _robust_probability_dispersion(probabilities)
    uncertainty = Decimal("0.04") + robust_dispersion
    if len(probabilities) == 1:
        uncertainty = max(uncertainty, Decimal("0.12"))
    lower = clamp_probability(fair - uncertainty)
    upper = clamp_probability(fair + uncertainty)
    reasons = [
        f"multi-model local-day ensemble families={len(probabilities)} members={total_members}",
        f"family_probability_mean={fair:.4f}",
        f"model_probability_mean={fair:.4f}",
        f"family_disagreement={disagreement:.4f}",
        f"family_robust_dispersion={robust_dispersion:.4f}",
        f"model_robust_dispersion={robust_dispersion:.4f}",
        f"deterministic_reference_sigma={reference_sigma}{rule.unit}",
        "model_probabilities="
        + ",".join(
            f"{model}:{probability:.4f}" for model, probability in model_probabilities.items()
        ),
        "source_families="
        + ",".join(
            f"{family}:[{'|'.join(members)}]" for family, members in consensus.members.items()
        ),
        "family_probabilities="
        + ",".join(f"{family}:{probability:.4f}" for family, probability in eligible.items()),
    ]
    if consensus.advisory_families:
        reasons.append(
            "advisory_families_excluded_from_pricing_quorum="
            + ",".join(sorted(consensus.advisory_families))
        )
    if observed_floor is not None:
        effective_floor = observed_floor - observation_source_tolerance(rule.unit)
        reasons.append(
            f"D0 observed max-to-date={observed_floor}{rule.unit}; "
            f"source-tolerant ensemble floor={effective_floor}{rule.unit} "
            f"(tolerance={observation_source_tolerance(rule.unit)}{rule.unit})"
        )
    return ProbabilityInterval(lower, upper, reasons, MULTIMODEL_VERSION)


def _multimodel_probabilities(
    rule: GlobalTemperatureBucketRule,
    model_members: dict[str, list[Decimal]],
    *,
    observed_max: Decimal | None,
    observed_max_unit: str | None,
    reference_sigma: Decimal,
    source_sigmas: dict[str, Decimal] | None = None,
) -> tuple[Decimal | None, dict[str, Decimal], int]:
    if rule.bucket_lower is None or rule.bucket_upper is None or rule.unit is None:
        return None, {}, 0
    bucket_lower, bucket_upper = settlement_bucket_bounds(rule)
    observed_floor = (
        normalize_value(
            observed_max, rule.variable or "temperature_high", observed_max_unit, rule.unit
        )
        if observed_max is not None and observed_max_unit is not None
        else observed_max
    )
    effective_observed_floor = (
        observed_floor - observation_source_tolerance(rule.unit)
        if observed_floor is not None
        else None
    )
    probabilities: dict[str, Decimal] = {}
    total_members = 0
    hard_excluded = _observed_max_excludes_bucket(rule, observed_floor)
    for model, raw_members in model_members.items():
        members = [Decimal(str(value)) for value in raw_members]
        if not members:
            continue
        if hard_excluded:
            probabilities[model] = Decimal("0")
            total_members += len(members)
            continue
        if model.startswith("reference_") and len(members) == 1:
            point_value = members[0]
            point_sigma = (source_sigmas or {}).get(model, reference_sigma)
            if effective_observed_floor is None:
                probability = _bucket_probability(
                    point_value,
                    bucket_lower,
                    bucket_upper,
                    point_sigma,
                )
            else:
                probability = _bucket_probability_above_observed_floor(
                    point_value,
                    bucket_lower,
                    bucket_upper,
                    point_sigma,
                    effective_observed_floor,
                )
            probabilities[model] = probability
            total_members += 1
            continue
        if effective_observed_floor is not None:
            members = [max(value, effective_observed_floor) for value in members]
        hits = sum(_value_in_bucket(value, bucket_lower, bucket_upper) for value in members)
        probabilities[model] = Decimal(hits) / Decimal(len(members))
        total_members += len(members)
    return observed_floor, probabilities, total_members


def _normalized_observed_max(
    rule: GlobalTemperatureBucketRule,
    *,
    observed_max: Decimal | None,
    observed_max_unit: str | None,
) -> Decimal | None:
    if observed_max is None or rule.unit is None:
        return None
    return normalize_value(
        observed_max,
        rule.variable or "temperature_high",
        observed_max_unit or rule.unit,
        rule.unit,
    )


def _observed_max_excludes_bucket(
    rule: GlobalTemperatureBucketRule,
    normalized_observed_max: Decimal | None,
) -> bool:
    if normalized_observed_max is None:
        return False
    _bucket_lower, bucket_upper = settlement_bucket_bounds(rule)
    return bucket_upper is not None and normalized_observed_max >= bucket_upper


def _observed_max_exclusion_reason(
    rule: GlobalTemperatureBucketRule,
    normalized_observed_max: Decimal | None,
) -> str:
    _bucket_lower, bucket_upper = settlement_bucket_bounds(rule)
    return (
        f"D0 observed max-to-date={normalized_observed_max}{rule.unit} already exceeds bucket "
        f"upper bound {bucket_upper}{rule.unit} (the bound is exclusive); "
        "source tolerance, hourly conditioning, and external votes cannot reopen an "
        "impossible bucket"
    )


def global_bucket_model_probabilities(
    rule: GlobalTemperatureBucketRule,
    forecast: ForecastSnapshot,
    model_members: dict[str, list[Decimal]],
    *,
    now: datetime,
    observed_max: Decimal | None = None,
    observed_max_unit: str | None = None,
    source_sigmas: dict[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    """Expose the exact source votes used by pricing for calibration persistence."""
    if rule.unit is None:
        return {}
    _, probabilities, _ = _multimodel_probabilities(
        rule,
        model_members,
        observed_max=observed_max,
        observed_max_unit=observed_max_unit,
        reference_sigma=_forecast_sigma(forecast, now, rule.unit),
        source_sigmas=source_sigmas,
    )
    return probabilities


def apply_temperature_biases(
    model_members: dict[str, list[Decimal]],
    biases: dict[str, Decimal],
) -> dict[str, list[Decimal]]:
    """Apply auditable additive source corrections without mutating provider payloads."""
    return {
        model: [Decimal(str(value)) + Decimal(str(biases.get(model, 0))) for value in members]
        for model, members in model_members.items()
    }


def global_bucket_top_candidate_votes(
    rules: dict[str, GlobalTemperatureBucketRule],
    forecast: ForecastSnapshot,
    model_members: dict[str, list[Decimal]],
    *,
    now: datetime,
    observed_max: Decimal | None = None,
    observed_max_unit: str | None = None,
    source_sigmas: dict[str, Decimal] | None = None,
    source_weights: dict[str, Decimal] | None = None,
) -> dict[str, tuple[int, int]]:
    """Count independent source families that rank each sibling bucket first."""
    family_probabilities_by_market: dict[str, dict[str, Decimal]] = {}
    for market_id, rule in rules.items():
        source_probabilities = global_bucket_model_probabilities(
            rule,
            forecast,
            model_members,
            now=now,
            observed_max=observed_max,
            observed_max_unit=observed_max_unit,
            source_sigmas=source_sigmas,
        )
        family_probabilities_by_market[market_id] = _source_family_consensus(
            source_probabilities, source_weights
        ).eligible_probabilities
    families = sorted(
        {
            family
            for probabilities in family_probabilities_by_market.values()
            for family in probabilities
        }
    )
    votes = {market_id: 0 for market_id in rules}
    usable_families = 0
    for family in families:
        ranked = {
            market_id: probabilities.get(family)
            for market_id, probabilities in family_probabilities_by_market.items()
            if family in probabilities
        }
        if not ranked:
            continue
        top_probability = max(ranked.values())
        if top_probability <= 0:
            continue
        usable_families += 1
        leaders = [
            market_id for market_id, probability in ranked.items() if probability == top_probability
        ]
        if len(leaders) == 1:
            votes[leaders[0]] += 1
    return {market_id: (supporters, usable_families) for market_id, supporters in votes.items()}


def _forecast_sigma(forecast: ForecastSnapshot, now: datetime, unit: str) -> Decimal:
    effective_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    valid_time = (
        forecast.valid_time
        if forecast.valid_time.tzinfo
        else forecast.valid_time.replace(tzinfo=timezone.utc)
    )
    hours_to_valid = max(0.0, (valid_time - effective_now).total_seconds() / 3600)
    return _sigma(hours_to_valid, unit)


def _robust_probability_dispersion(probabilities: list[Decimal]) -> Decimal:
    if len(probabilities) <= 1:
        return Decimal("0")
    median = _decimal_quantile(probabilities, Decimal("0.5"))
    mad = _decimal_quantile(
        [abs(probability - median) for probability in probabilities],
        Decimal("0.5"),
    ) * Decimal("1.4826")
    iqr = (
        _decimal_quantile(probabilities, Decimal("0.75"))
        - _decimal_quantile(probabilities, Decimal("0.25"))
    ) / Decimal("1.349")
    return max(mad, iqr)


def _source_family_consensus(
    probabilities: dict[str, Decimal],
    source_weights: dict[str, Decimal] | None,
) -> SourceFamilyConsensus:
    configured = _configured_source_weights(probabilities, source_weights)
    grouped: dict[str, list[str]] = {}
    for source in probabilities:
        grouped.setdefault(weather_source_family(source), []).append(source)

    family_probabilities: dict[str, Decimal] = {}
    family_weights: dict[str, Decimal] = {}
    family_members: dict[str, tuple[str, ...]] = {}
    advisory: set[str] = set()
    for family, sources in grouped.items():
        family_members[family] = tuple(sorted(sources))
        total_weight = sum((configured[source] for source in sources), Decimal("0"))
        if total_weight <= 0:
            probability = sum(
                (probabilities[source] for source in sources), Decimal("0")
            ) / Decimal(len(sources))
        else:
            probability = (
                sum(
                    (probabilities[source] * configured[source] for source in sources), Decimal("0")
                )
                / total_weight
            )
        # Multiple correlated feeds improve the estimate inside a family but
        # never create extra independent voting power.
        family_weight = total_weight / Decimal(len(sources))
        family_probabilities[family] = probability
        family_weights[family] = family_weight
        if family_weight < Decimal("0.5") or family == "d0-hourly":
            advisory.add(family)
    return SourceFamilyConsensus(
        probabilities=family_probabilities,
        weights=family_weights,
        members=family_members,
        advisory_families=frozenset(advisory),
        source_weights=configured,
    )


def _configured_source_weights(
    probabilities: dict[str, Decimal],
    source_weights: dict[str, Decimal] | None,
) -> dict[str, Decimal]:
    return {
        source: min(
            Decimal("1.5"),
            max(
                Decimal("0.05"),
                Decimal(
                    str(
                        (source_weights or {}).get(
                            source,
                            ADVISORY_SOURCE_WEIGHT
                            if weather_source_family(source) == "aviation-taf"
                            else Decimal("1"),
                        )
                    )
                ),
            ),
        )
        for source in probabilities
    }


def _weighted_mean(values: dict[str, Decimal], weights: dict[str, Decimal]) -> Decimal:
    total = sum(weights.values(), Decimal("0"))
    if total <= 0:
        return sum(values.values()) / Decimal(len(values))
    return sum(values[model] * weights[model] for model in values) / total


def _decimal_quantile(values: list[Decimal], quantile: Decimal) -> Decimal:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = Decimal(len(ordered) - 1) * quantile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - Decimal(lower_index)
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def _sigma(hours_to_valid: float, unit: str) -> Decimal:
    if unit == "C":
        if hours_to_valid <= 24:
            return Decimal("0.60")
        if hours_to_valid <= 48:
            return Decimal("0.90")
        if hours_to_valid <= 120:
            return Decimal("1.40")
        return Decimal("1.80")
    if hours_to_valid <= 24:
        return Decimal("1.20")
    if hours_to_valid <= 48:
        return Decimal("1.80")
    if hours_to_valid <= 120:
        return Decimal("2.80")
    return Decimal("3.60")


def _default_mu_band(unit: str) -> Decimal:
    return Decimal("0.4") if unit == "C" else Decimal("0.8")


def _bucket_probability(
    mu: Decimal,
    lower: Decimal | None,
    upper: Decimal | None,
    sigma: Decimal,
) -> Decimal:
    if sigma <= 0:
        return Decimal("1") if _value_in_bucket(mu, lower, upper) else Decimal("0")
    lower_cdf = 0.0 if lower is None else _normal_cdf((lower - mu) / sigma)
    upper_cdf = 1.0 if upper is None else _normal_cdf((upper - mu) / sigma)
    probability = upper_cdf - lower_cdf
    return clamp_probability(Decimal(str(probability)))


def _bucket_probability_above_observed_floor(
    mu: Decimal,
    lower: Decimal | None,
    upper: Decimal | None,
    sigma: Decimal,
    observed_floor: Decimal,
) -> Decimal:
    """P(bucket | final daily high >= observed max-to-date)."""
    if upper is not None and observed_floor >= upper:
        return Decimal("0")
    if sigma <= 0:
        return (
            Decimal("1")
            if _value_in_bucket(mu, lower, upper) and mu >= observed_floor
            else Decimal("0")
        )
    denominator = 1.0 - _normal_cdf((observed_floor - mu) / sigma)
    if denominator <= 1e-12:
        return Decimal("0")
    effective_lower = observed_floor if lower is None else max(lower, observed_floor)
    upper_cdf = 1.0 if upper is None else _normal_cdf((upper - mu) / sigma)
    numerator = upper_cdf - _normal_cdf((effective_lower - mu) / sigma)
    return clamp_probability(Decimal(str(max(0.0, numerator / denominator))))


def _value_in_bucket(
    value: Decimal,
    lower: Decimal | None,
    upper: Decimal | None,
) -> bool:
    return (lower is None or value >= lower) and (upper is None or value < upper)


def _normal_cdf(value: Decimal) -> float:
    z = float(value)
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))
