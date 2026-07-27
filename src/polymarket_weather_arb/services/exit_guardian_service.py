"""Exit policy advisor: settlement core over reconciled inventory.

Recommendations only — never places orders. AutoExitService may execute
evidence-backed ``exit_full`` through PositionExitService.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from sqlite3 import Row
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polymarket_weather_arb.domain.fees import (
    WEATHER_TAKER_FEE_RATE,
    extract_market_fee_schedule,
    expected_taker_fee_per_share,
)
from polymarket_weather_arb.domain.global_temperature_bucket import (
    observation_source_tolerance,
    parse_global_temperature_bucket_rule,
    settlement_bucket_bounds,
)
from polymarket_weather_arb.domain.market_eligibility import (
    is_market_orderable,
    resolve_market_timezone,
)
from polymarket_weather_arb.domain.position_inventory import (
    CampaignInventory,
    build_campaign_inventory,
)
from polymarket_weather_arb.domain.rules import event_date_from_market_title, parse_resolution_rule
from polymarket_weather_arb.domain.strategy_versions import WEATHER_EXIT_POLICY_VERSION
from polymarket_weather_arb.storage.repositories import Repository

_ANALYSIS_MAX_AGE = timedelta(minutes=30)
_NEAR_SETTLEMENT_HOURS = 6
_D0_WINNER_LOCK_MAX_OBSERVATION_AGE = timedelta(hours=1)
_D0_WINNER_LOCK_MIN_PROBABILITY = Decimal("0.80")
_D0_WINNER_LOCK_MAX_DISAGREEMENT = Decimal("0.20")
_D0_WINNER_LOCK_MIN_OBSERVATIONS = 12


@dataclass(frozen=True)
class ExitRecommendation:
    kind: str
    action: str
    market_id: str
    reason: str
    execute: bool = False
    exchange_order_id: str | None = None
    outcome: str | None = None
    token_id: str | None = None
    side: str | None = None
    notional: Decimal | None = None
    age_seconds: int | None = None
    latest_decision: str | None = None
    latest_edge: Decimal | None = None
    position_hold_edge: Decimal | None = None
    # Profit-protection ladder fields (optional for open-order recommendations).
    policy_stage: str | None = None
    recommended_size: Decimal | None = None
    actual_position_size: Decimal | None = None
    verified_buy_cost: Decimal | None = None
    verified_sell_proceeds: Decimal | None = None
    unrecovered_cash: Decimal | None = None
    runner_size_after: Decimal | None = None
    best_bid: Decimal | None = None
    executable_value: Decimal | None = None
    expected_fee: Decimal | None = None
    max_payout: Decimal | None = None
    accounting_verified: bool | None = None
    evidence_reason: str | None = None
    time_to_window_end: str | None = None
    settlement_state: str | None = None
    policy_version: str = WEATHER_EXIT_POLICY_VERSION
    hold_value_upper: Decimal | None = None
    net_sell_per_share: Decimal | None = None


class ExitGuardianService:
    """Exit advisor for open orders and positions (no mutations)."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def evaluate(
        self,
        *,
        stale_threshold_seconds: int = 300,
        min_edge: Decimal = Decimal("0.03"),
        best_bids: dict[tuple[str, str], Decimal] | None = None,
        bid_depths: dict[tuple[str, str], Decimal] | None = None,
        now: datetime | None = None,
    ) -> list[ExitRecommendation]:
        recommendations: list[ExitRecommendation] = []
        recommendations.extend(
            self._evaluate_open_order(order, stale_threshold_seconds, min_edge)
            for order in self.repository.list_open_orders(limit=1000)
        )
        recommendations.extend(
            self._evaluate_position(
                position,
                min_edge,
                best_bid=(best_bids or {}).get(
                    (str(position["market_id"]), _outcome_key(position["outcome"]))
                ),
                available_depth=(bid_depths or {}).get(
                    (str(position["market_id"]), _outcome_key(position["outcome"]))
                ),
                now=now,
            )
            for position in self.repository.list_positions(limit=1000, nonzero_only=True)
        )
        return recommendations

    def _evaluate_open_order(
        self,
        order: Row,
        stale_threshold_seconds: int,
        min_edge: Decimal,
    ) -> ExitRecommendation:
        market_id = str(order["market_id"] or "")
        exchange_order_id = str(order["exchange_order_id"])
        age = _order_age_seconds(order)
        analysis = self.repository.latest_analysis(market_id) if market_id else None
        decision = str(analysis["decision"]) if analysis is not None else None
        edge = _decimal_or_none(analysis["edge"] if analysis is not None else None)
        base = {
            "kind": "open_order",
            "market_id": market_id,
            "exchange_order_id": exchange_order_id,
            "token_id": order["token_id"],
            "side": order["side"],
            "notional": _decimal_or_none(order["notional"]),
            "age_seconds": age,
            "latest_decision": decision,
            "latest_edge": edge,
        }
        if age is not None and age > stale_threshold_seconds:
            return ExitRecommendation(
                action="cancel_stale",
                reason=f"order is stale: age_seconds={age} threshold={stale_threshold_seconds}",
                **base,
            )
        if analysis is None:
            return ExitRecommendation(
                action="review_no_analysis",
                reason="no latest analysis is available for this open order",
                **base,
            )
        if decision != "trade":
            return ExitRecommendation(
                action="cancel_edge_gone",
                reason=f"latest analysis decision={decision}",
                **base,
            )
        if edge is not None and edge < min_edge:
            return ExitRecommendation(
                action="cancel_edge_gone",
                reason=f"edge {edge} below min {min_edge}",
                **base,
            )
        return ExitRecommendation(
            action="keep_order",
            reason="latest analysis remains tradable",
            **base,
        )

    def _evaluate_position(
        self,
        position: Row,
        min_edge: Decimal,
        *,
        best_bid: Decimal | None = None,
        available_depth: Decimal | None = None,
        now: datetime | None = None,
    ) -> ExitRecommendation:
        market_id = str(position["market_id"])
        outcome = str(position["outcome"])
        pos_size = abs(Decimal(str(position["size"] or 0)))
        analysis = self.repository.latest_analysis(market_id)
        decision = str(analysis["decision"]) if analysis is not None else None
        edge = _decimal_or_none(analysis["edge"] if analysis is not None else None)
        analysis_side = str(analysis["side"]) if analysis is not None and analysis["side"] else None
        fair_lower = _decimal_or_none(analysis["fair_lower"] if analysis is not None else None)
        fair_upper = _decimal_or_none(analysis["fair_upper"] if analysis is not None else None)
        market_row = self.repository.get_market(market_id)
        payload = _market_payload(market_row)
        yes_token = market_row["yes_token_id"] if market_row is not None else None
        no_token = market_row["no_token_id"] if market_row is not None else None
        token_id = yes_token if _normalize_outcome(outcome) == "YES" else no_token

        inventory = self._campaign_for_position(
            market_id=market_id,
            outcome=outcome,
            position_size=pos_size,
            yes_token_id=yes_token,
            no_token_id=no_token,
        )
        fee_schedule = extract_market_fee_schedule(payload)
        fee_rate = fee_schedule.fee_rate or (
            WEATHER_TAKER_FEE_RATE if fee_schedule.fees_enabled else Decimal("0")
        )
        expected_fee = (
            expected_taker_fee_per_share(price=best_bid, fee_rate=fee_rate) * pos_size
            if best_bid is not None and pos_size > 0
            else None
        )
        executable = best_bid * pos_size if best_bid is not None else None
        max_payout = pos_size  # $1.00 per share
        fair_probability = _analysis_consensus_probability(analysis)
        net_sell_per_share = (
            best_bid - expected_taker_fee_per_share(price=best_bid, fee_rate=fee_rate)
            if best_bid is not None
            else None
        )
        hold_edge = (
            fair_probability - net_sell_per_share
            if fair_probability is not None and net_sell_per_share is not None
            else None
        )
        hold_value_upper = _held_probability_upper(
            fair_lower=fair_lower,
            fair_upper=fair_upper,
            outcome=outcome,
        )

        base_fields = {
            "kind": "position",
            "market_id": market_id,
            "outcome": outcome,
            "token_id": token_id,
            "notional": _decimal_or_none(position["notional"]),
            "latest_decision": decision,
            "latest_edge": edge,
            "position_hold_edge": hold_edge,
            "hold_value_upper": hold_value_upper,
            "net_sell_per_share": net_sell_per_share,
            "actual_position_size": pos_size,
            "verified_buy_cost": inventory.verified_buy_cost,
            "verified_sell_proceeds": inventory.verified_sell_proceeds,
            "unrecovered_cash": inventory.unrecovered_cash,
            "best_bid": best_bid,
            "executable_value": executable,
            "expected_fee": expected_fee,
            "max_payout": max_payout,
            "accounting_verified": inventory.accounting_verified,
            "evidence_reason": inventory.evidence_reason,
        }

        # 1) Settlement / closed routing
        settlement = _settlement_route_reason(analysis) if analysis is not None else None
        orderable = is_market_orderable(
            raw_payload=payload,
            title=market_row["title"] if market_row is not None else None,
            close_time=market_row["close_time"] if market_row is not None else None,
            now=now,
            check_target_date=True,
        )
        if settlement is not None or not orderable:
            return ExitRecommendation(
                action="settlement_pending",
                policy_stage="settlement",
                reason=settlement or "market closed/non-orderable; settlement pending; no SELL",
                settlement_state=settlement or "non_orderable",
                **base_fields,
            )

        # 2) Evidence freshness
        if analysis is None or not _analysis_is_fresh(analysis, now=now):
            return ExitRecommendation(
                action="review_no_analysis",
                policy_stage="evidence",
                reason=(
                    "no latest analysis is available for this position"
                    if analysis is None
                    else "analysis stale; auto-exit blocked"
                ),
                **base_fields,
            )

        # A provider outage or incomplete model set is not a bearish signal.
        # Keep the degraded analysis for audit/UI, but never let it drive a SELL.
        unavailable_evidence = _analysis_evidence_unavailable_reason(analysis)
        if unavailable_evidence is not None:
            return ExitRecommendation(
                action="review_no_analysis",
                policy_stage="evidence",
                reason=f"analysis evidence unavailable; auto-exit blocked: {unavailable_evidence}",
                **base_fields,
            )

        # Reliable irreversible observations and a strict exact-bucket D0 lock
        # outrank model churn, principal recovery, and rebalance suggestions.
        observation_override = self._observation_override(
            market_id=market_id,
            title=market_row["title"] if market_row is not None else None,
            description=market_row["description"] if market_row is not None else None,
            outcome=outcome,
            model_supports=True,
        )
        if observation_override == "exit_full":
            return ExitRecommendation(
                action="exit_full",
                policy_stage="official_observation",
                recommended_size=pos_size,
                runner_size_after=Decimal("0"),
                reason="reliable official observation invalidates held outcome",
                **base_fields,
            )
        if observation_override == "hold_for_resolution":
            return ExitRecommendation(
                action="hold_for_resolution",
                policy_stage="official_observation",
                runner_size_after=pos_size,
                reason="reliable official observation irreversibly locks held outcome",
                **base_fields,
            )
        winner_lock = self._d0_exact_bucket_winner_lock(
            market_row=market_row,
            analysis=analysis,
            outcome=outcome,
            now=now,
        )
        if winner_lock is not None:
            return ExitRecommendation(
                action="hold_for_resolution",
                policy_stage="d0_winner_lock",
                runner_size_after=pos_size,
                reason=winner_lock,
                **base_fields,
            )

        # 3) V5 validation policy: models never authorize a SELL. A direction
        # reversal, negative hold edge, D0/TAF contradiction, executable bid,
        # profit target, or repeated forecast revision remains research
        # evidence only. The sole automatic strategy exit above is a reliable
        # irreversible official observation that makes the held bucket
        # impossible. Closed/resolved markets route to settlement instead.
        direction_ok = outcome_matches_analysis_side(outcome, analysis_side)
        hold_edge_negative = hold_edge is not None and hold_edge < 0
        if decision not in {"trade", "watch"}:
            return ExitRecommendation(
                action="review_no_analysis",
                policy_stage="evidence",
                reason=f"analysis decision={decision}; auto-exit blocked pending review",
                **base_fields,
            )

        # 4) Near-settlement labeling. Official evidence has first priority;
        # every model-only path still holds through resolution.
        near = self._near_settlement_recommendation(
            market_row=market_row,
            payload=payload,
            outcome=outcome,
            pos_size=pos_size,
            model_supports=direction_ok and not hold_edge_negative,
            base_fields=base_fields,
            now=now,
        )
        if near is not None:
            return near

        rebalance_target = _rebalance_target(analysis)
        detail = "full position is protected from profit, principal-recovery, and dust exits"
        if rebalance_target:
            detail = (
                f"rebalance candidate {rebalance_target} is not an independent exit signal"
            )
        elif not direction_ok:
            detail = "model direction reversed but V5 forbids model-only SELL"
        elif hold_edge_negative:
            detail = "model hold edge is negative but V5 forbids model-only SELL"
        elif not inventory.accounting_verified:
            detail = f"inventory accounting is unverified: {inventory.evidence_reason}"
        return ExitRecommendation(
            action="hold_for_resolution",
            policy_stage="settlement_core",
            recommended_size=None,
            runner_size_after=pos_size,
            reason=f"settlement core: {detail}; hold full reconciled position for resolution",
            **base_fields,
        )

    def _campaign_for_position(
        self,
        *,
        market_id: str,
        outcome: str,
        position_size: Decimal,
        yes_token_id: str | None,
        no_token_id: str | None,
    ) -> CampaignInventory:
        rows = self.repository.list_fills(limit=5000, market_id=market_id)
        fills: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw = item.get("raw_payload")
            if isinstance(raw, str):
                try:
                    item["raw_payload"] = json.loads(raw)
                except Exception:
                    pass
            fills.append(item)
        order_tokens = self.repository.order_token_ids_for_market(market_id)
        return build_campaign_inventory(
            fills,
            market_id=market_id,
            outcome=outcome,
            position_size=position_size,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            order_token_ids=order_tokens,
        )

    def _near_settlement_recommendation(
        self,
        *,
        market_row: Row | None,
        payload: dict[str, Any],
        outcome: str,
        pos_size: Decimal,
        model_supports: bool,
        base_fields: dict[str, Any],
        now: datetime | None,
    ) -> ExitRecommendation | None:
        if market_row is None:
            return None
        title = market_row["title"]
        description = market_row["description"] if "description" in market_row.keys() else None
        tz_name = resolve_market_timezone(title=title) or payload.get("timezone")
        if isinstance(tz_name, str):
            tz_name = tz_name.strip() or None
        else:
            tz_name = None
        if not tz_name:
            return None  # unknown timezone: no near-settlement inference
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        try:
            local_now = current.astimezone(ZoneInfo(tz_name))
        except ZoneInfoNotFoundError:
            return None

        window_end = _observation_window_end(
            title=title,
            description=description,
            tz_name=tz_name,
            local_now=local_now,
        )
        if window_end is None:
            return None
        time_left = window_end - local_now
        if time_left > timedelta(hours=_NEAR_SETTLEMENT_HOURS):
            return None
        if time_left <= timedelta(0):
            return ExitRecommendation(
                action="settlement_pending",
                policy_stage="near_settlement",
                settlement_state="window_closed",
                time_to_window_end="0",
                reason="observation/trading window closed; settlement pending; no SELL",
                **base_fields,
            )

        tts = str(int(time_left.total_seconds()))
        # Official observation may override a still-bullish model when quality is reliable.
        obs_override = self._observation_override(
            market_id=str(market_row["id"]),
            title=title,
            description=description,
            outcome=outcome,
            model_supports=model_supports,
        )
        if obs_override == "exit_full":
            return ExitRecommendation(
                action="exit_full",
                policy_stage="near_settlement",
                recommended_size=pos_size,
                runner_size_after=Decimal("0"),
                time_to_window_end=tts,
                reason=(
                    "near-settlement: reliable official observation invalidates held outcome "
                    "(overrides model support)"
                ),
                **base_fields,
            )
        if obs_override == "hold_for_resolution":
            return ExitRecommendation(
                action="hold_for_resolution",
                policy_stage="near_settlement",
                runner_size_after=pos_size,
                time_to_window_end=tts,
                reason=(
                    "near-settlement: reliable official observation locks held outcome; "
                    "hold for resolution"
                ),
                **base_fields,
            )

        if not model_supports:
            return ExitRecommendation(
                action="hold_for_resolution",
                policy_stage="near_settlement",
                recommended_size=None,
                runner_size_after=pos_size,
                time_to_window_end=tts,
                reason=(
                    "near-settlement: model no longer supports held outcome, but "
                    "V5 forbids model-only SELL"
                ),
                **base_fields,
            )
        return ExitRecommendation(
            action="hold_for_resolution",
            policy_stage="near_settlement",
            recommended_size=None,
            runner_size_after=pos_size,
            time_to_window_end=tts,
            reason=(
                "near-settlement: no settlement-grade official evidence invalidates "
                "the held outcome; hold full position for resolution"
            ),
            **base_fields,
        )

    def _d0_exact_bucket_winner_lock(
        self,
        *,
        market_row: Row | None,
        analysis: Any,
        outcome: str,
        now: datetime | None,
    ) -> str | None:
        """Return an audit reason when an exact D0 bucket is strongly locked."""
        if market_row is None or _normalize_outcome(outcome) != "YES":
            return None
        market_id = str(market_row["id"])
        bucket = self.repository.get_temperature_bucket_rule(market_id)
        if bucket is None:
            return None
        title = str(market_row["title"] or "")
        description = market_row["description"]
        rule = parse_global_temperature_bucket_rule(title, description)
        if rule.bucket_kind != "exact" or rule.variable != "temperature_high":
            return None
        lower, upper = settlement_bucket_bounds(rule)
        if lower is None or upper is None:
            return None

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        tz_name = resolve_market_timezone(title=title) or _row_value(bucket, "settlement_timezone")
        try:
            target_day = datetime.fromisoformat(str(_row_value(bucket, "target_date"))).date()
            local_day = current.astimezone(ZoneInfo(str(tz_name))).date()
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            return None
        if target_day != local_day:
            return None

        observation = self.repository.latest_observation(market_id)
        quality_status = (
            _row_value(observation, "quality_status") if observation is not None else None
        )
        if observation is None or not quality_status or not _observation_quality_ok(quality_status):
            return None
        expected_station = (
            str(
                _row_value(bucket, "settlement_station_id")
                or _row_value(bucket, "station_id")
                or ""
            )
            .strip()
            .upper()
        )
        observed_station = str(_row_value(observation, "station") or "").strip().upper()
        if expected_station and observed_station != expected_station:
            return None
        if str(_row_value(observation, "unit") or "").upper() != str(rule.unit or "").upper():
            return None
        fetched_at = _parse_datetime(_row_value(observation, "fetched_at"))
        if fetched_at is None:
            return None
        age = current - fetched_at
        if age < timedelta(0) or age > _D0_WINNER_LOCK_MAX_OBSERVATION_AGE:
            return None
        observation_payload = _json_object(_row_value(observation, "raw_payload"))
        warnings = observation_payload.get("warnings")
        if isinstance(warnings, list) and any(
            _observation_warning_blocks_winner_lock(str(warning)) for warning in warnings
        ):
            return None
        observations = observation_payload.get("observations")
        if (
            not isinstance(observations, list)
            or len(observations) < _D0_WINNER_LOCK_MIN_OBSERVATIONS
        ):
            return None
        latest_observation_at = _parse_datetime(observation_payload.get("latest_observation_at"))
        if latest_observation_at is None:
            return None
        latest_age = current - latest_observation_at
        if latest_age < timedelta(0) or latest_age > _D0_WINNER_LOCK_MAX_OBSERVATION_AGE:
            return None
        observed_max = _decimal_or_none(_row_value(observation, "value"))
        if observed_max is None or not (lower <= observed_max < upper):
            return None

        probability = _analysis_consensus_probability(analysis)
        disagreement = _analysis_reason_decimal(analysis, "model_disagreement")
        if probability is None or probability < _D0_WINNER_LOCK_MIN_PROBABILITY:
            return None
        if disagreement is None or disagreement > _D0_WINNER_LOCK_MAX_DISAGREEMENT:
            return None
        context = _analysis_d0_hourly_context(analysis)
        if not context or context.get("post_peak", "").lower() != "true":
            return None
        context_station = str(context.get("station") or "").strip().upper()
        if expected_station and context_station != expected_station:
            return None
        remaining_peak = _context_temperature(context.get("remaining_peak"), rule.unit)
        current_temperature = _context_temperature(context.get("current"), rule.unit)
        recent_trend = _context_temperature(context.get("trend_per_hour"), None)
        if remaining_peak is None or remaining_peak >= upper:
            return None
        if current_temperature is None or current_temperature > observed_max:
            return None
        if recent_trend is None or recent_trend > 0:
            return None
        return (
            f"D0 winner lock: observed max {observed_max}{rule.unit} is inside held bucket; "
            f"post-peak conditioned peak {remaining_peak}{rule.unit} stays below {upper}{rule.unit} "
            f"with non-rising trend {recent_trend}{rule.unit}/h; "
            f"consensus={probability:.4f} disagreement={disagreement:.4f}"
        )

    def _observation_override(
        self,
        *,
        market_id: str,
        title: str | None,
        description: str | None,
        outcome: str,
        model_supports: bool,
    ) -> str | None:
        """Return exit_full / hold_for_resolution when official obs is reliable enough.

        Official observation can override a model that still supports the held
        direction when coverage/quality is good.
        """
        observation = self.repository.latest_observation(market_id)
        if observation is None:
            return None
        quality = _row_value(observation, "quality_status")
        if not _observation_quality_ok(quality):
            return None  # poor coverage cannot claim lock
        obs_val = _decimal_or_none(_row_value(observation, "value"))
        if obs_val is None:
            return None

        held = _normalize_outcome(outcome)
        if held is None:
            return None

        # Global temp bucket rules when present.
        bucket = self.repository.get_temperature_bucket_rule(market_id)
        if bucket is not None:
            lower = _decimal_or_none(
                bucket["bucket_lower_c"] if "bucket_lower_c" in bucket.keys() else None
            )
            upper = _decimal_or_none(
                bucket["bucket_upper_c"] if "bucket_upper_c" in bucket.keys() else None
            )
            variable = str(
                bucket["variable"] if "variable" in bucket.keys() else "temperature_high"
            )
            parsed_bucket = parse_global_temperature_bucket_rule(
                title or str(_row_value(bucket, "raw_text") or ""),
                description,
            )
            tolerance = observation_source_tolerance(
                parsed_bucket.unit or _row_value(observation, "unit")
            )
            parsed_lower, parsed_upper = settlement_bucket_bounds(parsed_bucket)
            if parsed_bucket.bucket_lower is not None and parsed_bucket.bucket_upper is not None:
                lower, upper = parsed_lower, parsed_upper
            bucket_kind = parsed_bucket.bucket_kind
            if variable == "temperature_high" and bucket_kind == "upper_tail":
                if lower is not None and obs_val - tolerance >= lower:
                    return "hold_for_resolution" if held == "YES" else "exit_full"
            if upper is not None and variable == "temperature_high":
                # Daily high already above bucket upper → YES for this bucket is impossible.
                # This must match entry pricing's strict impossibility rule. Source
                # tolerance may delay a favorable upper-tail lock, but it must not
                # reopen an exact/range bucket that the observed maximum exceeded.
                if obs_val >= upper:
                    return "exit_full" if held == "YES" else "hold_for_resolution"
                # Daily high already inside/above lower for a closed day would need window end;
                # with reliable high obs still below lower, YES not yet locked.
            # A recognized bucket title must never fall through to the generic
            # threshold parser. Range/exact text such as "98-99F" can otherwise
            # be misread as "high >= 98F" and falsely lock an already-lost YES.
            if bucket_kind in {"exact", "range", "lower_tail", "upper_tail"}:
                return None

        rule = parse_resolution_rule(title or "", description)
        if rule.variable not in {"temperature_high", "temperature_low"} or rule.threshold is None:
            return None
        thr = Decimal(str(rule.threshold))
        op = rule.operator
        if rule.variable == "temperature_high":
            conservative_obs = obs_val - observation_source_tolerance(
                rule.unit or _row_value(observation, "unit")
            )
            # "high >= thr" YES is locked once obs >= thr.
            if op in {">", ">="}:
                locked_yes = conservative_obs >= thr if op == ">=" else conservative_obs > thr
                if locked_yes:
                    return "hold_for_resolution" if held == "YES" else "exit_full"
            # "high < thr" YES impossible once obs >= thr.
            if op in {"<", "<="}:
                broken = conservative_obs >= thr if op == "<" else conservative_obs > thr
                if broken:
                    return "exit_full" if held == "YES" else "hold_for_resolution"
        return None


def _observation_window_end(
    *,
    title: str | None,
    description: str | None,
    tz_name: str,
    local_now: datetime,
) -> datetime | None:
    rule = parse_resolution_rule(title or "", description)
    end_date = None
    if rule.window_end:
        try:
            end_date = datetime.fromisoformat(str(rule.window_end)[:10]).date()
        except ValueError:
            end_date = None
    if end_date is None:
        end_date = event_date_from_market_title(title or "", today=local_now.date())
    if end_date is None:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return None
    # End of local observation day.
    return datetime.combine(end_date, time(23, 59, 59), tzinfo=tz)


def _settlement_route_reason(analysis: Row | None) -> str | None:
    if analysis is None:
        return None
    try:
        model = str(analysis["model_version"] or "")
    except (KeyError, IndexError, TypeError):
        model = ""
    reasons_raw = None
    try:
        reasons_raw = analysis["reasons"]
    except (KeyError, IndexError, TypeError):
        reasons_raw = None
    reasons_text = reasons_raw if isinstance(reasons_raw, str) else str(reasons_raw or "")
    if model == "settlement-route-v1" or "settlement state:" in reasons_text:
        if "settlement state:" in reasons_text:
            try:
                parsed = json.loads(reasons_text) if reasons_text.startswith("[") else None
                if isinstance(parsed, list):
                    for item in parsed:
                        if "settlement state:" in str(item):
                            return str(item)
            except Exception:
                pass
            return reasons_text
        return "settlement-route-v1: hold for settlement visibility; no auto-exit"
    return None


def _analysis_evidence_unavailable_reason(analysis: Any) -> str | None:
    """Return an audit reason when an analysis cannot support an exit decision."""
    model = str(_row_value(analysis, "model_version") or "").strip().lower()
    if "unavailable" in model:
        return f"model_version={model}"

    raw_reasons = _row_value(analysis, "reasons")
    reasons: list[str]
    if isinstance(raw_reasons, str):
        try:
            parsed = json.loads(raw_reasons)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        reasons = [str(item) for item in parsed] if isinstance(parsed, list) else [raw_reasons]
    elif isinstance(raw_reasons, (list, tuple)):
        reasons = [str(item) for item in raw_reasons]
    else:
        reasons = []

    markers = (
        "evidence_status=insufficient_models",
        "requires at least 3 models",
        "requires at least 3 independent source families",
        "forecast/analysis failed:",
    )
    for reason in reasons:
        lowered = reason.lower()
        if any(marker in lowered for marker in markers):
            return reason
    return None


def _rebalance_target(analysis: Any) -> str | None:
    raw_reasons = _row_value(analysis, "reasons")
    if isinstance(raw_reasons, str):
        try:
            parsed = json.loads(raw_reasons)
        except (json.JSONDecodeError, TypeError):
            parsed = [raw_reasons]
    elif isinstance(raw_reasons, (list, tuple)):
        parsed = raw_reasons
    else:
        parsed = []
    for reason in parsed if isinstance(parsed, (list, tuple)) else []:
        text = str(reason)
        if text.startswith("rebalance_target="):
            target = text.partition("=")[2].strip()
            return target or None
    return None


def _analysis_consensus_probability(analysis: Any) -> Decimal | None:
    if analysis is None:
        return None
    raw_reasons = _row_value(analysis, "reasons")
    if isinstance(raw_reasons, str):
        try:
            parsed = json.loads(raw_reasons)
        except (json.JSONDecodeError, TypeError):
            parsed = [raw_reasons]
    elif isinstance(raw_reasons, (list, tuple)):
        parsed = raw_reasons
    else:
        parsed = []
    for reason in parsed if isinstance(parsed, (list, tuple)) else []:
        text = str(reason)
        if not text.startswith("consensus_probability_median="):
            continue
        try:
            return Decimal(text.partition("=")[2].strip())
        except Exception:
            continue
    lower = _decimal_or_none(_row_value(analysis, "fair_lower"))
    upper = _decimal_or_none(_row_value(analysis, "fair_upper"))
    if lower is None or upper is None:
        return None
    return (lower + upper) / Decimal("2")


def _held_probability_upper(
    *,
    fair_lower: Decimal | None,
    fair_upper: Decimal | None,
    outcome: object,
) -> Decimal | None:
    normalized = _normalize_outcome(outcome)
    if normalized == "YES":
        value = fair_upper
    elif normalized == "NO":
        value = Decimal("1") - fair_lower if fair_lower is not None else None
    else:
        value = None
    if value is None or value < 0 or value > 1:
        return None
    return value


def _analysis_reason_decimal(analysis: Any, key: str) -> Decimal | None:
    prefix = f"{key}="
    for reason in _analysis_reasons(analysis):
        if not reason.startswith(prefix):
            continue
        try:
            return Decimal(reason.partition("=")[2].strip())
        except Exception:
            return None
    return None


def _analysis_d0_hourly_context(analysis: Any) -> dict[str, str]:
    for reason in _analysis_reasons(analysis):
        if not reason.startswith("D0 hourly context "):
            continue
        return {
            match.group(1): match.group(2)
            for match in re.finditer(r"([a-zA-Z0-9_]+)=([^ ]+)", reason)
        }
    return {}


def _analysis_reasons(analysis: Any) -> list[str]:
    raw = _row_value(analysis, "reasons")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return [raw]
        return [str(item) for item in parsed] if isinstance(parsed, list) else [raw]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return []


def _context_temperature(value: object, unit: str | None) -> Decimal | None:
    text = str(value or "").strip()
    if not text or text.lower() == "none":
        return None
    suffix = str(unit or "").strip()
    if suffix and text.upper().endswith(suffix.upper()):
        text = text[: -len(suffix)]
    try:
        return Decimal(text)
    except Exception:
        return None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _observation_warning_blocks_winner_lock(warning: str) -> bool:
    text = warning.strip().lower()
    if not text:
        return False
    audit_only_markers = (
        "sample-based, not official daily summary",
        "sample-based, not the settlement provider's final daily summary",
        "settlement proxy, not wunderground's final daily summary",
    )
    return not any(marker in text for marker in audit_only_markers)


def outcome_matches_analysis_side(outcome: object, analysis_side: object) -> bool:
    outcome_norm = _normalize_outcome(outcome)
    side_norm = str(analysis_side or "").strip().lower()
    if outcome_norm == "YES":
        return side_norm in {"yes", "buy_yes", "y"}
    if outcome_norm == "NO":
        return side_norm in {"no", "buy_no", "n"}
    return False


def _normalize_outcome(value: object) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"YES", "Y"}:
        return "YES"
    if text in {"NO", "N"}:
        return "NO"
    return None


def _analysis_is_fresh(analysis: Any, *, now: datetime | None = None) -> bool:
    try:
        created_at = analysis["created_at"]
    except (KeyError, IndexError, TypeError):
        return False
    if not created_at:
        return False
    try:
        parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - parsed <= _ANALYSIS_MAX_AGE


def _order_age_seconds(order: Row) -> int | None:
    keys = order.keys() if hasattr(order, "keys") else []
    candidates: list[object] = []
    if "first_seen_at" in keys:
        candidates.append(order["first_seen_at"])
    if "updated_at" in keys:
        candidates.append(order["updated_at"])
    best: int | None = None
    for value in candidates:
        age = _age_seconds(str(value) if value is not None else None)
        if age is None:
            continue
        if best is None or age > best:
            best = age
    return best


def _age_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - parsed).total_seconds())


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _outcome_key(value: object) -> str:
    return str(value or "").strip().upper()


def _row_value(row: Any, key: str) -> Any:
    """Safe access for sqlite3.Row / mapping (Row has no .get)."""
    try:
        if hasattr(row, "keys") and key in row.keys():
            return row[key]
    except Exception:
        pass
    if isinstance(row, dict):
        return row.get(key)
    return None


def _observation_quality_ok(quality: object) -> bool:
    if quality is None or quality == "":
        return True  # missing treated as usable when value present
    text = str(quality).strip().upper()
    # NOAA V = verified; also accept common ok labels.
    return text in {"V", "OK", "GOOD", "OFFICIAL", "VERIFIED", "Z", "AWC"}


def _market_payload(market_row: Row | None) -> dict[str, Any]:
    if market_row is None:
        return {}
    try:
        raw = market_row["raw_payload"]
    except (KeyError, IndexError, TypeError):
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}
