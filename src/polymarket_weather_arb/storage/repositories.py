from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlite3 import Connection, IntegrityError, Row
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polymarket_weather_arb.domain.fees import expected_buy_fee, resolve_fill_fee
from polymarket_weather_arb.domain.llm_decision import LLM_WEATHER_MODEL_VERSION
from polymarket_weather_arb.domain.market_eligibility import is_market_orderable
from polymarket_weather_arb.domain.polymarket_resolution import compact_resolution_payload
from polymarket_weather_arb.domain.position_inventory import account_fill_view
from polymarket_weather_arb.domain.strategy_versions import WEATHER_SOURCE_MODEL_VERSION
from polymarket_weather_arb.storage.repository_automation import AutomationRepository


_EXCHANGE_SIZE_QUANTIZATION_TOLERANCE = Decimal("0.01")
_FILL_SIZE_COMPARISON_TOLERANCE = Decimal("0.000000001")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def to_json(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, sort_keys=True)


def _bucket_horizon_at(*, target_date: object, timezone_name: object, at: datetime) -> str:
    try:
        event_day = datetime.fromisoformat(str(target_date)).date()
        local_day = at.astimezone(ZoneInfo(str(timezone_name))).date()
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return "unknown"
    delta = (event_day - local_day).days
    return f"D{delta}" if delta in {0, 1, 2} else "other"


class Repository:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.automation = AutomationRepository(connection, to_json=to_json)

    def get_circuit_breaker_state(self) -> Row:
        row = self.connection.execute("SELECT * FROM system_safety_state WHERE id = 1").fetchone()
        if not row:
            self.connection.execute(
                "INSERT OR IGNORE INTO system_safety_state (id, circuit_breaker_tripped) VALUES (1, 0)"
            )
            row = self.connection.execute(
                "SELECT * FROM system_safety_state WHERE id = 1"
            ).fetchone()
        return row

    def trip_circuit_breaker(
        self, reason: str, by: str = "system", audit_id: int | None = None
    ) -> None:
        self.connection.execute(
            """
            UPDATE system_safety_state
            SET circuit_breaker_tripped = 1,
                circuit_breaker_reason = ?,
                tripped_at = CURRENT_TIMESTAMP,
                tripped_by = ?,
                tripping_audit_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (reason, by, audit_id),
        )

    def clear_circuit_breaker(self, by: str, note: str) -> None:
        self.connection.execute(
            """
            UPDATE system_safety_state
            SET circuit_breaker_tripped = 0,
                circuit_breaker_reason = NULL,
                tripped_at = NULL,
                tripped_by = NULL,
                tripping_audit_id = NULL,
                clear_note = ?,
                cleared_at = CURRENT_TIMESTAMP,
                cleared_by = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (note, by),
        )

    def save_resolution_audit(self, audit: Any) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO resolution_audits (
                market_id, polymarket_closed, polymarket_uma_status, polymarket_resolved_outcome,
                local_resolved_outcome, match, status, local_source, polymarket_source,
                raw_local_payload, raw_polymarket_payload, trip_breaker
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit.market_id,
                int(audit.polymarket_closed),
                audit.polymarket_uma_status,
                audit.polymarket_resolved_outcome,
                audit.local_resolved_outcome,
                int(audit.match),
                audit.status,
                audit.local_source,
                audit.polymarket_source,
                audit.raw_local_payload,
                audit.raw_polymarket_payload,
                int(audit.trip_breaker),
            ),
        )
        return int(cursor.lastrowid)

    def latest_resolution_audit(self, market_id: str) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM resolution_audits
            WHERE market_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()

    def upsert_market(
        self,
        market: Any,
        raw_payload: Any,
        *,
        module_id: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO markets (
                id, slug, title, description, event_slug, event_title, category, tags,
                yes_token_id, no_token_id, close_time, status, is_weather, module_id, raw_payload, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                slug=excluded.slug,
                title=excluded.title,
                description=excluded.description,
                event_slug=excluded.event_slug,
                event_title=excluded.event_title,
                category=excluded.category,
                tags=excluded.tags,
                yes_token_id=excluded.yes_token_id,
                no_token_id=excluded.no_token_id,
                close_time=excluded.close_time,
                status=excluded.status,
                is_weather=excluded.is_weather,
                module_id=excluded.module_id,
                raw_payload=excluded.raw_payload,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                market.id,
                market.slug,
                market.title,
                market.description,
                market.event_slug,
                market.event_title,
                market.category,
                to_json(list(market.tags)),
                market.yes_token_id,
                market.no_token_id,
                market.close_time,
                market.status,
                int(market.is_weather),
                module_id or getattr(market, "module_id", None) or "weather",
                to_json(raw_payload),
            ),
        )

    def save_market_snapshot(
        self,
        snapshot: Any,
        raw_payload: Any,
        *,
        token_id: str | None = None,
    ) -> None:
        resolved_token = token_id
        if resolved_token is None:
            resolved_token = getattr(snapshot, "token_id", None)
        if resolved_token is not None:
            resolved_token = str(resolved_token) or None
        raw_json = to_json(raw_payload)
        source = str(raw_payload.get("source") or "") if isinstance(raw_payload, dict) else ""
        latest_same_source = self.connection.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = ? AND token_id IS ?
              AND COALESCE(json_extract(raw_payload, '$.source'), '') = ?
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """,
            (
                snapshot.market_id,
                resolved_token,
                source,
            ),
        ).fetchone()
        quote_values = (
            _decimal_or_none(snapshot.best_bid),
            _decimal_or_none(snapshot.best_ask),
            _decimal_or_none(snapshot.midpoint),
            _decimal_or_none(snapshot.spread),
            _decimal_or_none(snapshot.liquidity),
        )
        if latest_same_source is not None and all(
            _numeric_values_equal(latest_same_source[column], value)
            for column, value in zip(
                ("best_bid", "best_ask", "midpoint", "spread", "liquidity"),
                quote_values,
                strict=True,
            )
        ):
            # Identical quotes are one state whose freshness advances. Updating
            # avoids an append-only heartbeat history while retaining BBO changes.
            if str(snapshot.fetched_at.isoformat()) >= str(latest_same_source["fetched_at"]):
                self.connection.execute(
                    """
                    UPDATE market_snapshots
                    SET fetched_at = ?, raw_payload = ?
                    WHERE id = ?
                    """,
                    (snapshot.fetched_at.isoformat(), raw_json, latest_same_source["id"]),
                )
            return
        self.connection.execute(
            """
            INSERT INTO market_snapshots (
                market_id, token_id, best_bid, best_ask, midpoint, spread, liquidity,
                fetched_at, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.market_id,
                resolved_token,
                _decimal_or_none(snapshot.best_bid),
                _decimal_or_none(snapshot.best_ask),
                _decimal_or_none(snapshot.midpoint),
                _decimal_or_none(snapshot.spread),
                _decimal_or_none(snapshot.liquidity),
                snapshot.fetched_at.isoformat(),
                raw_json,
            ),
        )

    def save_resolution_rule(self, market_id: str, rule: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO resolution_rules (
                market_id, location, station, source, variable, operator, threshold, unit,
                window_start, window_end, confidence, tradable, rejection_reason, raw_text, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(market_id) DO UPDATE SET
                location=excluded.location,
                station=excluded.station,
                source=excluded.source,
                variable=excluded.variable,
                operator=excluded.operator,
                threshold=excluded.threshold,
                unit=excluded.unit,
                window_start=excluded.window_start,
                window_end=excluded.window_end,
                confidence=excluded.confidence,
                tradable=excluded.tradable,
                rejection_reason=excluded.rejection_reason,
                raw_text=excluded.raw_text,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                market_id,
                rule.location,
                rule.station,
                rule.source,
                rule.variable,
                rule.operator,
                _decimal_or_none(rule.threshold),
                rule.unit,
                rule.window_start,
                rule.window_end,
                float(rule.confidence),
                int(rule.tradable),
                rule.rejection_reason,
                rule.raw_text,
            ),
        )

    def save_temperature_bucket_rule(
        self, market_id: str, rule: Any, *, module_id: str = "china_temp_bucket"
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO temperature_bucket_rules (
                market_id, module_id, city, city_cn, station_id, settlement_station_id, source,
                variable, unit, bucket_center_c, bucket_lower_c, bucket_upper_c, target_date,
                settlement_timezone, confidence, tradable, rejection_reason, raw_text, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(market_id) DO UPDATE SET
                module_id=excluded.module_id,
                city=excluded.city,
                city_cn=excluded.city_cn,
                station_id=excluded.station_id,
                settlement_station_id=excluded.settlement_station_id,
                source=excluded.source,
                variable=excluded.variable,
                unit=excluded.unit,
                bucket_center_c=excluded.bucket_center_c,
                bucket_lower_c=excluded.bucket_lower_c,
                bucket_upper_c=excluded.bucket_upper_c,
                target_date=excluded.target_date,
                settlement_timezone=excluded.settlement_timezone,
                confidence=excluded.confidence,
                tradable=excluded.tradable,
                rejection_reason=excluded.rejection_reason,
                raw_text=excluded.raw_text,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                market_id,
                module_id,
                rule.city,
                rule.city_cn,
                rule.station_id,
                rule.station_id,
                rule.source,
                rule.variable,
                getattr(rule, "unit", "C") or "C",
                _decimal_or_none(rule.bucket_center_c),
                _decimal_or_none(rule.bucket_lower_c),
                _decimal_or_none(rule.bucket_upper_c),
                rule.target_date,
                rule.settlement_timezone,
                float(rule.confidence),
                int(rule.tradable),
                rule.rejection_reason,
                rule.raw_text,
            ),
        )

    def get_temperature_bucket_rule(self, market_id: str) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM temperature_bucket_rules WHERE market_id = ?", (market_id,)
        ).fetchone()

    def list_temperature_bucket_rules(
        self, limit: int = 100, city: str | None = None, target_date: str | None = None
    ) -> list[Row]:
        clauses = []
        params: list[Any] = []
        if city:
            clauses.append("city = ?")
            params.append(city)
        if target_date:
            clauses.append("target_date = ?")
            params.append(target_date)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM temperature_bucket_rules
                {where}
                ORDER BY target_date DESC, city ASC, bucket_center_c ASC
                LIMIT ?
                """,
                params,
            )
        )

    def global_temperature_event_market_ids(
        self,
        market_id: str,
    ) -> tuple[list[str], frozenset[str] | None]:
        """Return every persisted sibling plus Gamma's expected event membership.

        The nested Gamma event payload is the completeness contract when it is
        available. Older fixtures and legacy rows may not contain it, so callers
        can still use the persisted sibling list while treating the expected set
        as unknown.
        """
        market = self.connection.execute(
            """
            SELECT
                m.event_slug,
                m.event_title,
                m.raw_payload,
                r.city,
                r.target_date,
                r.variable,
                r.unit
            FROM markets m
            LEFT JOIN temperature_bucket_rules r ON r.market_id = m.id
            WHERE m.id = ?
            """,
            (market_id,),
        ).fetchone()
        if market is None:
            return [], None

        event_slug = str(market["event_slug"] or "").strip()
        expected_ids = _gamma_event_market_ids(
            _json_loads(market["raw_payload"]),
            event_slug=event_slug,
        )
        if event_slug:
            rows = self.connection.execute(
                """
                SELECT m.id
                FROM markets m
                LEFT JOIN temperature_bucket_rules r ON r.market_id = m.id
                WHERE m.module_id = 'global_temp_bucket'
                  AND m.event_slug = ?
                ORDER BY
                    r.bucket_lower_c IS NULL ASC,
                    r.bucket_lower_c ASC,
                    r.bucket_upper_c ASC,
                    m.id ASC
                """,
                (event_slug,),
            ).fetchall()
        else:
            city = str(market["city"] or "").strip()
            target_date = str(market["target_date"] or "").strip()
            variable = str(market["variable"] or "").strip()
            unit = str(market["unit"] or "").strip()
            if not city or not target_date or not variable or not unit:
                return [market_id], expected_ids
            rows = self.connection.execute(
                """
                SELECT m.id
                FROM markets m
                JOIN temperature_bucket_rules r ON r.market_id = m.id
                WHERE m.module_id = 'global_temp_bucket'
                  AND r.tradable = 1
                  AND LOWER(TRIM(r.city)) = LOWER(TRIM(?))
                  AND r.target_date = ?
                  AND r.variable = ?
                  AND UPPER(r.unit) = UPPER(?)
                ORDER BY r.bucket_lower_c ASC, r.bucket_upper_c ASC, m.id ASC
                """,
                (city, target_date, variable, unit),
            ).fetchall()
        return [str(row["id"]) for row in rows], expected_ids

    def list_global_temperature_catalog(self) -> list[Row]:
        """Return the learned global weather event catalog from existing market/rule rows."""
        return list(
            self.connection.execute(
                """
                SELECT DISTINCT
                    m.event_slug,
                    t.city,
                    t.station_id,
                    t.settlement_timezone
                FROM markets m
                JOIN temperature_bucket_rules t ON t.market_id = m.id
                WHERE m.module_id = 'global_temp_bucket'
                  AND t.city IS NOT NULL
                  AND t.city != ''
                ORDER BY t.city ASC, m.event_slug ASC
                """
            )
        )

    def save_forecast(self, forecast: Any, raw_payload: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO weather_forecasts (
                market_id, provider, location, station, issue_time, valid_time, variable,
                value, lower_value, upper_value, unit, raw_payload, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                forecast.market_id,
                forecast.provider,
                forecast.location,
                forecast.station,
                forecast.issue_time.isoformat(),
                forecast.valid_time.isoformat(),
                forecast.variable,
                float(forecast.value),
                _decimal_or_none(forecast.lower_value),
                _decimal_or_none(forecast.upper_value),
                forecast.unit,
                to_json(raw_payload),
                forecast.fetched_at.isoformat(),
            ),
        )

    def update_forecast_raw_payload(self, forecast_id: int, raw_payload: Any) -> None:
        self.connection.execute(
            "UPDATE weather_forecasts SET raw_payload = ? WHERE id = ?",
            (to_json(raw_payload), forecast_id),
        )

    def save_observation(self, observation: Any, raw_payload: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO weather_observations (
                market_id, provider, station, observed_at, variable, value, unit,
                quality_status, raw_payload, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.market_id,
                observation.provider,
                observation.station,
                observation.observed_at.isoformat(),
                observation.variable,
                float(observation.value),
                observation.unit,
                observation.quality_status,
                to_json(raw_payload),
                observation.fetched_at.isoformat(),
            ),
        )

    def latest_observation(self, market_id: str) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM weather_observations
            WHERE market_id = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()

    def list_recent_observations(self, limit: int = 50, market_id: str | None = None) -> list[Row]:
        if market_id:
            return list(
                self.connection.execute(
                    """
                    SELECT * FROM weather_observations
                    WHERE market_id = ?
                    ORDER BY fetched_at DESC, observed_at DESC, id DESC
                    LIMIT ?
                    """,
                    (market_id, limit),
                )
            )
        return list(
            self.connection.execute(
                """
                SELECT * FROM weather_observations
                ORDER BY fetched_at DESC, observed_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def save_analysis(self, analysis: Any) -> int:
        reasons_json = to_json(analysis.reasons)
        is_cached_bucket_reprice = (
            str(analysis.model_version).startswith("global-temp-bucket-multimodel-v")
            and "cached_event_group_reprice" in analysis.reasons
        )
        if is_cached_bucket_reprice:
            duplicate_cutoff = (analysis.created_at - timedelta(seconds=60)).isoformat()
            duplicate = self.connection.execute(
                """
                SELECT id FROM analyses
                WHERE market_id = ? AND model_version = ?
                  AND fair_lower = ? AND fair_upper = ? AND reference_price IS ?
                  AND edge = ? AND side IS ? AND decision = ? AND reasons = ?
                  AND created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (
                    analysis.market_id,
                    analysis.model_version,
                    float(analysis.fair_lower),
                    float(analysis.fair_upper),
                    _decimal_or_none(analysis.reference_price),
                    float(analysis.edge),
                    analysis.side,
                    analysis.decision,
                    reasons_json,
                    duplicate_cutoff,
                ),
            ).fetchone()
            if duplicate is not None:
                return int(duplicate["id"])
        cursor = self.connection.execute(
            """
            INSERT INTO analyses (
                market_id, model_version, fair_lower, fair_upper, reference_price,
                edge, side, decision, reasons, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis.market_id,
                analysis.model_version,
                float(analysis.fair_lower),
                float(analysis.fair_upper),
                _decimal_or_none(analysis.reference_price),
                float(analysis.edge),
                analysis.side,
                analysis.decision,
                reasons_json,
                analysis.created_at.isoformat(),
            ),
        )
        analysis_id = int(cursor.lastrowid)
        self._save_model_signal_for_analysis(analysis_id, analysis)
        return analysis_id

    def _save_model_signal_for_analysis(self, analysis_id: int, analysis: Any) -> None:
        if not is_predictive_model_version(getattr(analysis, "model_version", None)):
            return
        forecast = self.latest_forecast(analysis.market_id)
        forecast_provider = forecast["provider"] if forecast is not None else None
        forecast_payload = _json_loads(forecast["raw_payload"]) if forecast is not None else {}
        if not isinstance(forecast_payload, dict):
            forecast_payload = {}
        source_grade = forecast_payload.get("source_grade")
        fair_probability = getattr(analysis, "fair_probability", None)
        yes_probability = (
            Decimal(str(fair_probability))
            if fair_probability is not None
            else (Decimal(str(analysis.fair_lower)) + Decimal(str(analysis.fair_upper)))
            / Decimal("2")
        )
        raw_payload = {
            "forecast_id": forecast["id"] if forecast is not None else None,
            "forecast_revision": (
                forecast_payload.get("revision")
                or forecast_payload.get("forecast_revision")
                or (forecast["fetched_at"] if forecast is not None else None)
            ),
        }
        bucket_rule = self.get_temperature_bucket_rule(analysis.market_id)
        if bucket_rule is not None:
            event_parts = (
                bucket_rule["city"],
                bucket_rule["target_date"],
                bucket_rule["variable"],
                bucket_rule["unit"],
            )
            if all(event_parts):
                raw_payload["event_identity"] = "_".join(str(part) for part in event_parts)
            raw_payload["horizon"] = _bucket_horizon_at(
                target_date=bucket_rule["target_date"],
                timezone_name=bucket_rule["settlement_timezone"],
                at=analysis.created_at,
            )
            raw_payload["city"] = bucket_rule["city"]
            raw_payload["station"] = bucket_rule["station_id"]
        self.connection.execute(
            """
            INSERT INTO model_signals (
                analysis_id, market_id, model_version, forecast_provider, source_grade,
                yes_probability, fair_lower, fair_upper, market_price, edge, side,
                decision, outcome_status, raw_payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(analysis_id) DO UPDATE SET
                market_id=excluded.market_id,
                model_version=excluded.model_version,
                forecast_provider=excluded.forecast_provider,
                source_grade=excluded.source_grade,
                yes_probability=excluded.yes_probability,
                fair_lower=excluded.fair_lower,
                fair_upper=excluded.fair_upper,
                market_price=excluded.market_price,
                edge=excluded.edge,
                side=excluded.side,
                decision=excluded.decision,
                raw_payload=excluded.raw_payload
            """,
            (
                analysis_id,
                analysis.market_id,
                analysis.model_version,
                forecast_provider,
                source_grade,
                float(yes_probability),
                float(analysis.fair_lower),
                float(analysis.fair_upper),
                _decimal_or_none(analysis.reference_price),
                float(analysis.edge),
                analysis.side,
                analysis.decision,
                to_json(raw_payload),
                analysis.created_at.isoformat(),
            ),
        )

    def prune_old_market_snapshots(
        self,
        *,
        keep_days: int = 7,
        batch_size: int = 5000,
    ) -> int:
        """Bound quote history while retaining the latest row per market/token."""
        keep_days = max(1, int(keep_days))
        batch_size = max(1, int(batch_size))
        cursor = self.connection.execute(
            """
            DELETE FROM market_snapshots
            WHERE id IN (
                SELECT old.id
                FROM market_snapshots old
                WHERE julianday(old.fetched_at) < julianday('now', ?)
                  AND old.id != (
                      SELECT latest.id
                      FROM market_snapshots latest
                      WHERE latest.market_id = old.market_id
                        AND latest.token_id IS old.token_id
                      ORDER BY latest.fetched_at DESC, latest.id DESC
                      LIMIT 1
                  )
                ORDER BY old.id
                LIMIT ?
            )
            """,
            (f"-{keep_days} days", batch_size),
        )
        return max(0, int(cursor.rowcount))

    def prune_superseded_pending_weather_source_signals(
        self,
        *,
        batch_size: int = 5000,
    ) -> int:
        """Keep only the latest unresolved source revision per forecast horizon."""
        batch_size = max(1, int(batch_size))
        cursor = self.connection.execute(
            """
            DELETE FROM model_signals
            WHERE id IN (
                SELECT id FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                market_id,
                                forecast_provider,
                                json_extract(raw_payload, '$.event_identity'),
                                json_extract(raw_payload, '$.horizon'),
                                COALESCE(json_extract(raw_payload, '$.calibration_phase'), 'unknown')
                            ORDER BY created_at DESC, id DESC
                        ) AS revision_rank
                    FROM model_signals
                    WHERE model_version = ?
                      AND outcome_status = 'pending'
                      AND json_extract(raw_payload, '$.event_identity') IS NOT NULL
                      AND json_extract(raw_payload, '$.horizon') IS NOT NULL
                ) ranked
                WHERE revision_rank > 1
                LIMIT ?
            )
            """,
            (WEATHER_SOURCE_MODEL_VERSION, batch_size),
        )
        return max(0, int(cursor.rowcount))

    def prune_old_weather_forecasts(self, *, keep_days: int = 14, batch_size: int = 5000) -> int:
        """Bound reconstructable forecast history, retaining each latest horizon."""
        cursor = self.connection.execute(
            """
            DELETE FROM weather_forecasts
            WHERE id IN (
                SELECT old.id FROM weather_forecasts old
                WHERE julianday(old.fetched_at) < julianday('now', ?)
                  AND old.id != (
                      SELECT latest.id FROM weather_forecasts latest
                      WHERE latest.market_id IS old.market_id
                        AND latest.provider = old.provider
                        AND latest.valid_time = old.valid_time
                        AND latest.variable = old.variable
                      ORDER BY latest.fetched_at DESC, latest.id DESC LIMIT 1
                  )
                ORDER BY old.id LIMIT ?
            )
            """,
            (f"-{max(1, int(keep_days))} days", max(1, int(batch_size))),
        )
        return max(0, int(cursor.rowcount))

    def prune_old_weather_observations(self, *, keep_days: int = 14, batch_size: int = 5000) -> int:
        """Bound observation refresh history while preserving latest settlement evidence."""
        cursor = self.connection.execute(
            """
            DELETE FROM weather_observations
            WHERE id IN (
                SELECT old.id FROM weather_observations old
                WHERE julianday(old.fetched_at) < julianday('now', ?)
                  AND old.id != (
                      SELECT latest.id FROM weather_observations latest
                      WHERE latest.market_id IS old.market_id
                        AND latest.provider = old.provider
                        AND latest.station IS old.station
                        AND latest.variable = old.variable
                      ORDER BY latest.observed_at DESC, latest.id DESC LIMIT 1
                  )
                ORDER BY old.id LIMIT ?
            )
            """,
            (f"-{max(1, int(keep_days))} days", max(1, int(batch_size))),
        )
        return max(0, int(cursor.rowcount))

    def prune_unreferenced_analyses(self, *, keep_days: int = 14, batch_size: int = 5000) -> int:
        """Delete old analyses only when no calibration signal references them."""
        cursor = self.connection.execute(
            """
            DELETE FROM analyses
            WHERE id IN (
                SELECT old.id FROM analyses old
                WHERE julianday(old.created_at) < julianday('now', ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM model_signals signal WHERE signal.analysis_id = old.id
                  )
                  AND old.id != (
                      SELECT latest.id FROM analyses latest
                      WHERE latest.market_id = old.market_id
                        AND latest.model_version = old.model_version
                      ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1
                  )
                ORDER BY old.id LIMIT ?
            )
            """,
            (f"-{max(1, int(keep_days))} days", max(1, int(batch_size))),
        )
        return max(0, int(cursor.rowcount))

    def prune_superseded_unavailable_resolution_audits(self, *, batch_size: int = 5000) -> int:
        """Keep only the newest unavailable read per market/source."""
        cursor = self.connection.execute(
            """
            DELETE FROM resolution_audits
            WHERE id IN (
                SELECT old.id FROM resolution_audits old
                WHERE old.status = 'unavailable'
                  AND old.id != (
                      SELECT latest.id FROM resolution_audits latest
                      WHERE latest.market_id = old.market_id
                        AND latest.polymarket_source IS old.polymarket_source
                        AND latest.status = 'unavailable'
                      ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1
                  )
                ORDER BY old.id LIMIT ?
            )
            """,
            (max(1, int(batch_size)),),
        )
        return max(0, int(cursor.rowcount))

    def compact_resolution_audit_payloads(self, *, batch_size: int = 1000) -> int:
        """Replace legacy oversized Gamma payload copies with auditable summaries."""
        rows = self.connection.execute(
            """
            SELECT id, raw_polymarket_payload
            FROM resolution_audits
            WHERE LENGTH(COALESCE(raw_polymarket_payload, '')) > 4096
            ORDER BY id LIMIT ?
            """,
            (max(1, int(batch_size)),),
        ).fetchall()
        changed = 0
        for row in rows:
            try:
                payload = json.loads(row["raw_polymarket_payload"])
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            self.connection.execute(
                "UPDATE resolution_audits SET raw_polymarket_payload = ? WHERE id = ?",
                (to_json(compact_resolution_payload(payload)), row["id"]),
            )
            changed += 1
        return changed

    def latest_model_signal(
        self,
        market_id: str,
        model_version: str | None = None,
        *,
        forecast_provider: str | None = None,
        decision: str | None = None,
    ) -> Row | None:
        clauses = ["market_id = ?"]
        params: list[Any] = [market_id]
        if model_version:
            clauses.append("model_version = ?")
            params.append(model_version)
        if forecast_provider:
            clauses.append("forecast_provider = ?")
            params.append(forecast_provider)
        if decision:
            clauses.append("decision = ?")
            params.append(decision)
        return self.connection.execute(
            f"""
            SELECT * FROM model_signals
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()

    def model_signal_for_revision(
        self,
        *,
        market_id: str,
        model_version: str,
        forecast_provider: str,
        event_identity: str,
        forecast_revision: str,
    ) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM model_signals
            WHERE market_id = ?
              AND model_version = ?
              AND forecast_provider = ?
              AND json_extract(raw_payload, '$.event_identity') = ?
              AND json_extract(raw_payload, '$.forecast_revision') = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (
                market_id,
                model_version,
                forecast_provider,
                event_identity,
                forecast_revision,
            ),
        ).fetchone()

    def list_model_signals(
        self,
        *,
        limit: int | None = 1000,
        market_id: str | None = None,
        model_version: str | None = None,
        forecast_provider: str | None = None,
    ) -> list[Row]:
        clauses = []
        params: list[Any] = []
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id)
        if model_version:
            clauses.append("model_version = ?")
            params.append(model_version)
        if forecast_provider:
            clauses.append("forecast_provider = ?")
            params.append(forecast_provider)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            params.append(limit)
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM model_signals
                {where}
                ORDER BY created_at DESC, id DESC
                {limit_clause}
                """,
                tuple(params),
            )
        )

    def list_resolved_weather_source_signals(
        self,
        *,
        city: str | None,
        station: str | None = None,
        horizon: str | None,
        calibration_phase: str | None = None,
        unit: str | None = None,
        providers: list[str],
    ) -> list[Row]:
        if not providers:
            return []
        placeholders = ",".join("?" for _ in providers)
        clauses = [
            "model_version = ?",
            "outcome_status = 'resolved'",
        ]
        params: list[Any] = [WEATHER_SOURCE_MODEL_VERSION]
        if city is not None:
            clauses.append(
                "lower(COALESCE(json_extract(ms.raw_payload, '$.city'), r.city)) = lower(?)"
            )
            params.append(city)
        if station is not None:
            clauses.append(
                "lower(COALESCE(json_extract(ms.raw_payload, '$.station'), r.station_id, '')) = lower(?)"
            )
            params.append(station)
        if horizon is not None:
            clauses.append("json_extract(ms.raw_payload, '$.horizon') = ?")
            params.append(horizon)
        if calibration_phase is not None:
            clauses.append(
                "COALESCE(json_extract(ms.raw_payload, '$.calibration_phase'), 'unknown') = ?"
            )
            params.append(calibration_phase)
        if unit is not None:
            clauses.append(
                "upper(COALESCE(json_extract(ms.raw_payload, '$.unit'), r.unit)) = upper(?)"
            )
            params.append(unit)
        clauses.append(f"forecast_provider IN ({placeholders})")
        params.extend(providers)
        return list(
            self.connection.execute(
                f"""
                SELECT ms.*,
                       r.city AS rule_city,
                       r.station_id AS rule_station,
                       r.unit AS rule_unit,
                       r.bucket_center_c AS rule_bucket_center,
                       r.bucket_lower_c AS rule_bucket_lower,
                       r.bucket_upper_c AS rule_bucket_upper
                FROM model_signals ms
                LEFT JOIN temperature_bucket_rules r ON r.market_id = ms.market_id
                WHERE {" AND ".join(clauses)}
                ORDER BY ms.created_at DESC, ms.id DESC
                """,
                tuple(params),
            )
        )

    def save_llm_model_signal(
        self,
        market_id: str,
        provider: str,
        model: str,
        yes_probability: Decimal,
        confidence: Decimal,
        reason: str,
        event_identity: str,
        forecast_revision: str,
        now: datetime,
        *,
        decision: str = "advisory",
        other_probability: Decimal | None = None,
        distribution_total: Decimal | None = None,
        source_forecast_time: str | None = None,
        raw_response: str | None = None,
        bucket_probabilities: dict[str, Decimal] | None = None,
        horizon: str | None = None,
    ) -> None:
        model_version = LLM_WEATHER_MODEL_VERSION
        forecast_provider = f"llm:{provider}:{model}"
        # Check for duplicates across this event + revision
        existing = self.connection.execute(
            """
            SELECT 1 FROM model_signals
            WHERE market_id = ?
              AND forecast_provider = ?
              AND json_extract(raw_payload, '$.event_identity') = ?
              AND json_extract(raw_payload, '$.forecast_revision') = ?
            """,
            (market_id, forecast_provider, event_identity, forecast_revision),
        ).fetchone()

        if existing:
            return

        raw_payload = {
            "event_identity": event_identity,
            "forecast_revision": forecast_revision,
            "confidence": float(confidence),
            "reason": reason,
        }
        if horizon is not None:
            raw_payload["horizon"] = horizon
        if other_probability is not None:
            raw_payload["other_probability"] = float(other_probability)
        if distribution_total is not None:
            raw_payload["distribution_total"] = float(distribution_total)
        if source_forecast_time is not None:
            raw_payload["source_forecast_time"] = source_forecast_time
        if raw_response is not None:
            raw_payload["raw_response"] = raw_response
        if bucket_probabilities is not None:
            raw_payload["bucket_probabilities"] = {
                k: float(v) for k, v in bucket_probabilities.items()
            }

        self.connection.execute(
            """
            INSERT INTO model_signals (
                analysis_id, market_id, model_version, forecast_provider, source_grade,
                yes_probability, fair_lower, fair_upper, market_price, edge, side,
                decision, outcome_status, raw_payload, created_at
            ) VALUES (NULL, ?, ?, ?, 'research_forecast', ?, ?, ?, NULL, 0, NULL, ?, 'pending', ?, ?)
            """,
            (
                market_id,
                model_version,
                forecast_provider,
                float(yes_probability),
                float(yes_probability),
                float(yes_probability),
                decision,
                to_json(raw_payload),
                now.isoformat(),
            ),
        )

    def save_weather_source_signal(
        self,
        *,
        market_id: str,
        source: str,
        yes_probability: Decimal,
        event_identity: str,
        forecast_revision: str,
        city: str,
        station: str | None = None,
        horizon: str,
        target_date: str,
        source_role: str,
        now: datetime,
        raw_yes_probability: Decimal | None = None,
        applied_bias: Decimal | None = None,
        unit: str | None = None,
        calibration_phase: str | None = None,
        lead_hours: Decimal | None = None,
        market_probability: Decimal | None = None,
        source_family: str | None = None,
    ) -> None:
        model_version = WEATHER_SOURCE_MODEL_VERSION
        existing = self.connection.execute(
            """
            SELECT 1 FROM model_signals
            WHERE market_id = ? AND model_version = ? AND forecast_provider = ?
              AND json_extract(raw_payload, '$.event_identity') = ?
              AND json_extract(raw_payload, '$.forecast_revision') = ?
              AND COALESCE(json_extract(raw_payload, '$.calibration_phase'), 'unknown') = ?
            """,
            (
                market_id,
                model_version,
                source,
                event_identity,
                forecast_revision,
                calibration_phase or "unknown",
            ),
        ).fetchone()
        if existing:
            return
        payload = {
            "event_identity": event_identity,
            "forecast_revision": forecast_revision,
            "city": city,
            "horizon": horizon,
            "target_date": target_date,
            "source_role": source_role,
            "calibration_phase": calibration_phase or "unknown",
        }
        if station:
            payload["station"] = station
        if unit:
            payload["unit"] = unit
        if raw_yes_probability is not None:
            payload["raw_yes_probability"] = float(raw_yes_probability)
        if applied_bias is not None:
            payload["applied_bias"] = float(applied_bias)
        if lead_hours is not None:
            payload["lead_hours"] = float(lead_hours)
        if market_probability is not None:
            payload["market_probability"] = float(market_probability)
        if source_family:
            payload["source_family"] = source_family
        pending = self.connection.execute(
            """
            SELECT id FROM model_signals
            WHERE market_id = ? AND model_version = ? AND forecast_provider = ?
              AND outcome_status = 'pending'
              AND json_extract(raw_payload, '$.event_identity') = ?
              AND json_extract(raw_payload, '$.horizon') = ?
              AND COALESCE(json_extract(raw_payload, '$.calibration_phase'), 'unknown') = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (
                market_id,
                model_version,
                source,
                event_identity,
                horizon,
                calibration_phase or "unknown",
            ),
        ).fetchone()
        if pending is not None:
            # Calibration consumes the latest revision per event/market/horizon.
            # Replace an unresolved revision instead of accumulating samples that
            # the scorer deliberately ignores. Resolved history is never touched.
            self.connection.execute(
                """
                UPDATE model_signals
                SET yes_probability = ?, fair_lower = ?, fair_upper = ?,
                    market_price = ?, raw_payload = ?, created_at = ?
                WHERE id = ?
                """,
                (
                    float(yes_probability),
                    float(yes_probability),
                    float(yes_probability),
                    float(market_probability) if market_probability is not None else None,
                    to_json(payload),
                    now.isoformat(),
                    pending["id"],
                ),
            )
            self.connection.execute(
                """
                DELETE FROM model_signals
                WHERE market_id = ? AND model_version = ? AND forecast_provider = ?
                  AND outcome_status = 'pending' AND id != ?
                  AND json_extract(raw_payload, '$.event_identity') = ?
                  AND json_extract(raw_payload, '$.horizon') = ?
                  AND COALESCE(json_extract(raw_payload, '$.calibration_phase'), 'unknown') = ?
                """,
                (
                    market_id,
                    model_version,
                    source,
                    pending["id"],
                    event_identity,
                    horizon,
                    calibration_phase or "unknown",
                ),
            )
            return
        self.connection.execute(
            """
            INSERT INTO model_signals (
                analysis_id, market_id, model_version, forecast_provider, source_grade,
                yes_probability, fair_lower, fair_upper, market_price, edge, side,
                decision, outcome_status, raw_payload, created_at
            ) VALUES (NULL, ?, ?, ?, 'research_forecast', ?, ?, ?, ?, 0, NULL,
                      'advisory', 'pending', ?, ?)
            """,
            (
                market_id,
                model_version,
                source,
                float(yes_probability),
                float(yes_probability),
                float(yes_probability),
                float(market_probability) if market_probability is not None else None,
                to_json(payload),
                now.isoformat(),
            ),
        )

    def settle_model_signals_for_market(
        self,
        market_id: str,
        *,
        resolved_outcome: str,
        settlement_value: Decimal | float | str | None = None,
        settlement_source: str | None = None,
    ) -> int:
        outcome = resolved_outcome.strip().lower()
        if outcome not in {"yes", "no"}:
            raise ValueError("resolved_outcome must be yes or no")
        cursor = self.connection.execute(
            """
            UPDATE model_signals
            SET outcome_status = 'resolved',
                resolved_outcome = ?,
                settlement_value = ?,
                settlement_source = ?,
                settled_at = CURRENT_TIMESTAMP
            WHERE market_id = ?
            """,
            (
                outcome,
                _decimal_or_none(settlement_value),
                settlement_source,
                market_id,
            ),
        )
        return int(cursor.rowcount)

    def backfill_model_signal_event_identity(self, market_id: str) -> int:
        """Attach the canonical bucket event identity to legacy signal rows."""
        rule = self.get_temperature_bucket_rule(market_id)
        if rule is None:
            return 0
        parts = (rule["city"], rule["target_date"], rule["variable"], rule["unit"])
        if not all(parts):
            return 0
        event_identity = "_".join(str(part) for part in parts)
        cursor = self.connection.execute(
            """
            UPDATE model_signals
            SET raw_payload = json_set(
                    CASE WHEN json_valid(raw_payload) THEN raw_payload ELSE '{}' END,
                    '$.event_identity',
                    ?
                )
            WHERE market_id = ?
              AND json_extract(
                    CASE WHEN json_valid(raw_payload) THEN raw_payload ELSE '{}' END,
                    '$.event_identity'
                  ) IS NULL
            """,
            (event_identity, market_id),
        )
        return int(cursor.rowcount)

    def save_risk_decision(self, decision: Any) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO risk_decisions (
                market_id, accepted, proposed_side, proposed_price, proposed_size,
                proposed_notional, reasons, max_order_usdc, max_daily_usdc, max_market_usdc, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.market_id,
                int(decision.accepted),
                decision.proposed_side,
                float(decision.proposed_price),
                float(decision.proposed_size),
                float(decision.proposed_notional),
                to_json(decision.reasons),
                float(decision.max_order_usdc),
                float(decision.max_daily_usdc),
                float(decision.max_market_usdc),
                decision.created_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def save_order_intent(self, intent: Any) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO order_intents (
                market_id, side, token_id, limit_price, size, notional,
                rationale, dry_run, status, idempotency_key, entry_policy_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent.market_id,
                intent.side,
                intent.token_id,
                float(intent.limit_price),
                float(intent.size),
                float(intent.notional),
                intent.rationale,
                int(intent.dry_run),
                intent.status,
                getattr(intent, "idempotency_key", None),
                getattr(intent, "entry_policy_version", None),
                intent.created_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def save_order_intent_once(self, intent: Any) -> tuple[int, bool]:
        """Insert an intent once, returning the existing row on an idempotency race."""
        key = getattr(intent, "idempotency_key", None)
        if not key:
            return self.save_order_intent(intent), True
        try:
            return self.save_order_intent(intent), True
        except IntegrityError as exc:
            if "order_intents.idempotency_key" not in str(exc):
                raise
        row = self.connection.execute(
            "SELECT id FROM order_intents WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"idempotent order intent disappeared for key {key}")
        return int(row["id"]), False

    def order_intent_by_idempotency_key(self, key: str) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM order_intents WHERE idempotency_key = ? LIMIT 1",
            (key,),
        ).fetchone()

    def active_live_order_intent(self, market_id: str, side: str) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM order_intents
            WHERE market_id = ?
              AND side = ?
              AND dry_run = 0
              AND status IN (
                  'submitted',
                  'open',
                  'partially_filled',
                  'pending',
                  'submitted_unverified',
                  'reconcile_failed'
              )
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (market_id, side),
        ).fetchone()

    def active_open_order(
        self,
        *,
        market_id: str,
        token_id: str | None,
        side: str | None = None,
    ) -> Row | None:
        if not token_id:
            return None
        return self.connection.execute(
            """
            SELECT * FROM open_orders
            WHERE market_id = ?
              AND token_id = ?
              AND LOWER(COALESCE(status, 'open')) NOT IN (
                  'cancelled', 'canceled', 'filled', 'matched', 'failed', 'rejected', 'closed'
              )
            ORDER BY updated_at DESC, exchange_order_id ASC
            LIMIT 1
            """,
            (market_id, token_id),
        ).fetchone()

    def save_order_attempt(self, attempt: Any) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO order_attempts (
                intent_id, request_payload, response_payload, status, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.intent_id,
                to_json(attempt.request_payload),
                to_json(attempt.response_payload) if attempt.response_payload is not None else None,
                attempt.status,
                attempt.error,
                attempt.created_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def update_order_intent_status(self, intent_id: int, status: str) -> None:
        self.connection.execute(
            "UPDATE order_intents SET status = ? WHERE id = ?",
            (status, intent_id),
        )

    def upsert_candidate(
        self,
        market_id: str,
        rule: Any,
        snapshot: Any | None = None,
        status: str | None = None,
        notes: str | None = None,
        module_id: str = "weather",
    ) -> None:
        candidate_status = status or ("dry_run_ready" if rule.tradable else "rejected")
        self.connection.execute(
            """
            INSERT INTO market_candidates (
                market_id, status, tradable, rejection_reason, best_bid, best_ask, spread,
                snapshot_fetched_at, rule_updated_at, notes, module_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(market_id) DO UPDATE SET
                status=excluded.status,
                tradable=excluded.tradable,
                rejection_reason=excluded.rejection_reason,
                best_bid=excluded.best_bid,
                best_ask=excluded.best_ask,
                spread=excluded.spread,
                snapshot_fetched_at=excluded.snapshot_fetched_at,
                rule_updated_at=excluded.rule_updated_at,
                notes=excluded.notes,
                module_id=excluded.module_id,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                market_id,
                candidate_status,
                int(rule.tradable),
                rule.rejection_reason,
                _decimal_or_none(_snapshot_value(snapshot, "best_bid")) if snapshot else None,
                _decimal_or_none(_snapshot_value(snapshot, "best_ask")) if snapshot else None,
                _decimal_or_none(_snapshot_value(snapshot, "spread")) if snapshot else None,
                _snapshot_fetched_at(snapshot) if snapshot else None,
                notes,
                module_id,
            ),
        )

    def list_candidates(
        self, limit: int = 50, status: str | None = None, module_id: str | None = None
    ) -> list[Row]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("c.status = ?")
            params.append(status)
        if module_id:
            clauses.append("c.module_id = ?")
            params.append(module_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        return list(
            self.connection.execute(
                f"""
                SELECT
                    c.*,
                    m.title,
                    m.slug,
                    t.city,
                    t.city_cn,
                    t.source,
                    t.bucket_center_c,
                    t.bucket_lower_c,
                    t.bucket_upper_c,
                    t.target_date,
                    t.station_id,
                    t.settlement_timezone
                FROM market_candidates c
                JOIN markets m ON m.id = c.market_id
                LEFT JOIN temperature_bucket_rules t ON t.market_id = c.market_id
                {where}
                ORDER BY c.updated_at DESC, c.market_id ASC
                LIMIT ?
                """,
                params,
            )
        )

    def mark_candidate(self, market_id: str, status: str, notes: str | None = None) -> None:
        self.connection.execute(
            """
            UPDATE market_candidates
            SET status = ?, notes = COALESCE(?, notes), updated_at = CURRENT_TIMESTAMP
            WHERE market_id = ?
            """,
            (status, notes, market_id),
        )

    def replace_open_orders(self, orders: list[dict[str, Any]]) -> int:
        # Preserve durable first_seen_at across full replace so stale age survives recon.
        prior_first_seen: dict[str, str] = {}
        try:
            for row in self.connection.execute(
                "SELECT exchange_order_id, first_seen_at FROM open_orders"
            ):
                if row["first_seen_at"]:
                    prior_first_seen[str(row["exchange_order_id"])] = str(row["first_seen_at"])
        except Exception:
            # Column may not exist mid-migration; fall through without prior map.
            prior_first_seen = {}
        self.connection.execute("DELETE FROM open_orders")
        count = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for order in orders:
            order_id = _string_field(order, "id", "order_id", "orderID")
            if not order_id:
                continue
            token_id = _string_field(order, "asset_id", "assetId", "token_id", "tokenId")
            external_market_id = _string_field(
                order, "market", "market_id", "condition_id", "conditionId"
            )
            market_id = self.resolve_local_market_id(external_market_id, token_id)
            price = _float_field(order, "price", "limit_price", "limitPrice")
            size = _float_field(order, "remaining_size", "remainingSize")
            if size is None:
                original_size = _float_field(order, "original_size", "originalSize", "size")
                matched_size = _float_field(order, "size_matched", "sizeMatched", "matched_size")
                if original_size is not None and matched_size is not None:
                    size = max(0.0, original_size - matched_size)
                else:
                    size = original_size
            notional = _notional(order, price, size)
            exchange_created = _exchange_order_created_at_iso(order)
            first_seen = prior_first_seen.get(order_id) or exchange_created or now_iso
            # Prefer the earliest durable anchor when exchange created_at is older.
            first_seen = _earliest_iso(first_seen, exchange_created) or first_seen
            self.connection.execute(
                """
                INSERT INTO open_orders (
                    exchange_order_id, market_id, token_id, side, price, size, notional,
                    status, raw_payload, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(exchange_order_id) DO UPDATE SET
                    market_id=excluded.market_id,
                    token_id=excluded.token_id,
                    side=excluded.side,
                    price=excluded.price,
                    size=excluded.size,
                    notional=excluded.notional,
                    status=excluded.status,
                    raw_payload=excluded.raw_payload,
                    first_seen_at=COALESCE(open_orders.first_seen_at, excluded.first_seen_at),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    order_id,
                    market_id or external_market_id,
                    token_id,
                    _string_field(order, "side"),
                    price,
                    size,
                    notional,
                    _string_field(order, "status"),
                    to_json(order),
                    first_seen,
                ),
            )
            count += 1
        return count

    def save_reconciled_fills(
        self, fills: list[dict[str, Any]]
    ) -> tuple[int, list[dict[str, Any]]]:
        """Upsert exchange fills.

        Returns ``(rows_touched, newly_inserted)`` where ``newly_inserted`` contains
        durable identifiers for fills that did not previously exist
        (de-duplicated by ``exchange_fill_id``). Used for one-time fill
        notifications without a separate notification-history table.
        """
        count = 0
        newly_inserted: list[dict[str, Any]] = []
        known_order_ids = self._known_exchange_order_ids()
        for fill in fills:
            exchange_fill_id = _string_field(
                fill,
                "id",
                "trade_id",
                "tradeId",
                "transactionHash",
                "transaction_hash",
            )
            if _exchange_trade_is_pending(fill):
                # A matched-but-not-broadcast trade is useful reconciliation
                # evidence, but it is not a confirmed fill yet. It will be
                # inserted when the exchange advances it to MINED/CONFIRMED.
                continue
            if _exchange_trade_is_explicitly_invalid(fill):
                if exchange_fill_id:
                    # Self-heal rows written before invalid exchange statuses were filtered.
                    self.connection.execute(
                        "DELETE FROM fills WHERE exchange_fill_id = ?", (exchange_fill_id,)
                    )
                continue
            account_fill = _account_fill_view(fill, known_order_ids)
            external_market_id = _string_field(
                account_fill, "market", "market_id", "condition_id", "conditionId"
            )
            token_id = _string_field(account_fill, "asset_id", "assetId", "token_id", "tokenId")
            market_id = self.resolve_local_market_id(external_market_id, token_id)
            price = _float_field(account_fill, "price")
            size = _float_field(account_fill, "size", "quantity", "matched_amount")
            if not exchange_fill_id or not market_id or price is None or size is None:
                continue
            if self.get_market(market_id) is None:
                continue
            existing = self.connection.execute(
                "SELECT id FROM fills WHERE exchange_fill_id = ?", (exchange_fill_id,)
            ).fetchone()
            order_id = _string_field(
                account_fill,
                "order_id",
                "orderId",
                "orderID",
                "taker_order_id",
                "maker_order_id",
            )
            side = _string_field(account_fill, "side") or "unknown"
            market_row = self.get_market(market_id)
            market_payload = (
                _json_loads(market_row["raw_payload"]) if market_row is not None else {}
            )
            if not isinstance(market_payload, dict):
                market_payload = {}
            fee_resolution = resolve_fill_fee(
                fill=fill,
                account_fill=account_fill,
                market_payload=market_payload,
                known_order_ids=known_order_ids,
            )
            raw_payload = dict(fill)
            if account_fill is not fill:
                raw_payload["_account_fill"] = {
                    key: account_fill.get(key)
                    for key in ("order_id", "side", "price", "size", "token_id", "outcome")
                }
            raw_payload["_fee_resolution"] = {
                "fee": str(fee_resolution.fee),
                "role": fee_resolution.role,
                "method": fee_resolution.method,
                "fee_rate": (
                    str(fee_resolution.fee_rate) if fee_resolution.fee_rate is not None else None
                ),
                "evidence": fee_resolution.evidence,
            }
            values = (
                exchange_fill_id,
                order_id,
                market_id,
                side,
                price,
                size,
                float(fee_resolution.fee),
                to_json(raw_payload),
                _string_field(
                    fill,
                    "created_at",
                    "createdAt",
                    "timestamp",
                    "matchTime",
                    "matched_at",
                    "updated_at",
                )
                or "",
            )
            if existing:
                self.connection.execute(
                    """
                    UPDATE fills
                    SET order_id = ?, market_id = ?, side = ?, price = ?, size = ?, fee = ?,
                        raw_payload = ?, filled_at = ?
                    WHERE exchange_fill_id = ?
                    """,
                    (
                        values[1],
                        values[2],
                        values[3],
                        values[4],
                        values[5],
                        values[6],
                        values[7],
                        values[8],
                        exchange_fill_id,
                    ),
                )
            else:
                cursor = self.connection.execute(
                    """
                    INSERT INTO fills (
                        exchange_fill_id, order_id, market_id, side, price, size, fee,
                        raw_payload, filled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                newly_inserted.append(
                    {
                        "id": int(cursor.lastrowid),
                        "exchange_fill_id": exchange_fill_id,
                        "order_id": order_id,
                        "market_id": market_id,
                        "side": side,
                        "price": price,
                        "size": size,
                    }
                )
            count += 1
        return count, newly_inserted

    def _known_exchange_order_ids(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT response_payload, request_payload FROM order_attempts"
        ).fetchall()
        order_ids: set[str] = set()
        for row in rows:
            for field in ("response_payload", "request_payload"):
                payload = _json_loads(row[field])
                if not isinstance(payload, dict):
                    continue
                order_id = _string_field(payload, "order_id", "orderID", "orderId", "id")
                if order_id and order_id not in {"ok", "true", "false"}:
                    order_ids.add(order_id)
        return order_ids

    def _exchange_matched_size_for_intent(self, intent_id: int, side: str) -> Decimal | None:
        """Return the exchange-quantized size for a terminal matched attempt."""
        rows = self.connection.execute(
            """
            SELECT response_payload
            FROM order_attempts
            WHERE intent_id = ?
            ORDER BY id DESC
            """,
            (intent_id,),
        ).fetchall()
        amount_keys = (
            ("taking_amount", "takingAmount")
            if side.lower().startswith("buy")
            else ("making_amount", "makingAmount")
        )
        for row in rows:
            payload = _json_loads(row["response_payload"])
            if not isinstance(payload, dict):
                continue
            exchange_status = _string_field(payload, "status", "order_status", "orderStatus")
            if not exchange_status or exchange_status.lower() not in {"filled", "matched"}:
                continue
            matched_size = _float_field(payload, *amount_keys)
            if matched_size is not None and matched_size > 0:
                return Decimal(str(matched_size))
        return None

    def reconcile_live_order_intent_statuses(
        self, exchange_trades: list[dict[str, Any]] | None = None
    ) -> int:
        """Align live intent lifecycle with reconciled orders and account-perspective fills."""
        intents = self.connection.execute(
            """
            SELECT id, side, size, status,
                   datetime(created_at) <= datetime('now', '-5 minutes')
                       AS missing_order_is_terminal
            FROM order_intents
            WHERE dry_run = 0
              AND status IN (
                  'submitted', 'open', 'filled', 'matched', 'partially_filled', 'pending',
                  'partially_filled_closed', 'submitted_unverified',
                  'reconcile_failed'
              )
            """
        ).fetchall()
        open_order_ids = {
            str(row["exchange_order_id"])
            for row in self.connection.execute("SELECT exchange_order_id FROM open_orders")
        }
        known_order_ids = self._known_exchange_order_ids()
        pending_trade_order_ids: set[str] = set()
        for trade in exchange_trades or []:
            if not _exchange_trade_is_pending(trade):
                continue
            account_fill = _account_fill_view(trade, known_order_ids)
            order_id = _string_field(
                account_fill,
                "order_id",
                "orderId",
                "orderID",
                "taker_order_id",
                "maker_order_id",
            )
            if order_id:
                pending_trade_order_ids.add(order_id)
        changed = 0
        for intent in intents:
            order_ids = self.get_order_ids_for_intent(int(intent["id"]))
            if not order_ids:
                continue
            placeholders = ",".join("?" for _ in order_ids)
            row = self.connection.execute(
                f"SELECT COALESCE(SUM(size), 0) AS filled_size FROM fills "
                f"WHERE order_id IN ({placeholders})",
                tuple(order_ids),
            ).fetchone()
            filled_size = Decimal(str(row["filled_size"] or 0))
            target_size = Decimal(str(intent["size"] or 0))
            exchange_matched_size = self._exchange_matched_size_for_intent(
                int(intent["id"]), str(intent["side"])
            )
            if filled_size + _FILL_SIZE_COMPARISON_TOLERANCE >= target_size:
                status = "filled"
            elif any(order_id in open_order_ids for order_id in order_ids):
                status = "partially_filled" if filled_size > 0 else "open"
            elif (
                exchange_matched_size is not None
                and filled_size + _FILL_SIZE_COMPARISON_TOLERANCE >= exchange_matched_size
            ):
                # The SDK may quantize an otherwise complete order by one centish
                # share. Larger terminal remainders are genuine partial fills and
                # must remain visible as such in the audit trail.
                status = (
                    "filled"
                    if target_size - exchange_matched_size <= _EXCHANGE_SIZE_QUANTIZATION_TOLERANCE
                    else "partially_filled_closed"
                )
            elif any(order_id in pending_trade_order_ids for order_id in order_ids):
                # Do not terminalize an exchange-matched trade while its
                # on-chain transaction is still pending confirmation.
                status = "partially_filled" if filled_size > 0 else "submitted"
            elif filled_size > 0:
                status = (
                    "partially_filled_closed"
                    if bool(intent["missing_order_is_terminal"])
                    else "partially_filled"
                )
            elif bool(intent["missing_order_is_terminal"]):
                # The exchange no longer lists this accepted order and no
                # account-perspective fill exists. After an indexing grace
                # period, converge the stale local intent to a terminal state.
                status = "cancelled"
            else:
                continue
            if status != str(intent["status"]):
                self.update_order_intent_status(int(intent["id"]), status)
                changed += 1
        return changed

    def market_has_live_activity(self, market_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT
                EXISTS(SELECT 1 FROM positions WHERE market_id = ? AND ABS(size) > 0)
                OR EXISTS(SELECT 1 FROM open_orders WHERE market_id = ? AND size > 0)
                OR EXISTS(
                    SELECT 1 FROM order_intents
                    WHERE market_id = ? AND dry_run = 0
                      AND status IN (
                          'submitted', 'open', 'partially_filled', 'pending',
                          'submitted_unverified', 'reconcile_failed'
                      )
                ) AS active
            """,
            (market_id, market_id, market_id),
        ).fetchone()
        return bool(row["active"])

    def active_live_sibling_market(self, market_id: str) -> Row | None:
        """Return active exposure in the same city/date bucket event."""
        market = self.connection.execute(
            """
            SELECT m.*, t.city AS bucket_city, t.target_date AS bucket_target_date
            FROM markets m
            LEFT JOIN temperature_bucket_rules t ON t.market_id = m.id
            WHERE m.id = ?
            """,
            (market_id,),
        ).fetchone()
        if market is None:
            return None
        event_key = str(market["event_slug"] or market["event_title"] or "").strip()
        bucket_city = str(market["bucket_city"] or "").strip().casefold()
        bucket_target_date = str(market["bucket_target_date"] or "").strip()
        if not event_key and not (bucket_city and bucket_target_date):
            return None
        return self.connection.execute(
            """
            SELECT sibling.id, sibling.title, sibling.event_slug, sibling.event_title
            FROM markets sibling
            LEFT JOIN temperature_bucket_rules sibling_rule
              ON sibling_rule.market_id = sibling.id
            WHERE sibling.id != ?
              AND (
                  (
                      ? != '' AND ? != ''
                      AND LOWER(TRIM(sibling_rule.city)) = ?
                      AND sibling_rule.target_date = ?
                  )
                  OR (
                      ? != ''
                      AND COALESCE(NULLIF(sibling.event_slug, ''), sibling.event_title) = ?
                  )
              )
              AND (
                  EXISTS(
                      SELECT 1 FROM positions p
                      WHERE p.market_id = sibling.id AND ABS(p.size) > 0
                  )
                  OR EXISTS(
                      SELECT 1 FROM open_orders oo
                      WHERE oo.market_id = sibling.id AND oo.size > 0
                  )
                  OR EXISTS(
                      SELECT 1 FROM order_intents oi
                      WHERE oi.market_id = sibling.id AND oi.dry_run = 0
                        AND oi.status IN (
                            'submitted', 'open', 'partially_filled', 'pending',
                            'submitted_unverified', 'reconcile_failed'
                        )
                  )
              )
            ORDER BY sibling.id
            LIMIT 1
            """,
            (
                market_id,
                bucket_city,
                bucket_target_date,
                bucket_city,
                bucket_target_date,
                event_key,
                event_key,
            ),
        ).fetchone()

    def prior_accepted_live_buy_in_event(self, market_id: str) -> Row | None:
        """Return any accepted live BUY for this market's city/date event.

        Unlike ``active_live_sibling_market``, this intentionally looks through
        closed exposure.  Weather entry V5 permits only one accepted entry
        campaign per event, so a later forecast revision cannot scale in,
        re-enter the same bucket, or rotate into a sibling bucket.
        """
        return self.connection.execute(
            """
            WITH target AS (
                SELECT
                    COALESCE(NULLIF(m.event_slug, ''), m.event_title, '') AS event_key,
                    LOWER(TRIM(COALESCE(t.city, ''))) AS bucket_city,
                    COALESCE(t.target_date, '') AS bucket_target_date
                FROM markets m
                LEFT JOIN temperature_bucket_rules t ON t.market_id = m.id
                WHERE m.id = ?
            )
            SELECT
                oi.*,
                bought.id AS bought_market_id,
                bought.title AS bought_market_title
            FROM order_intents oi
            JOIN markets bought ON bought.id = oi.market_id
            LEFT JOIN temperature_bucket_rules bought_rule
              ON bought_rule.market_id = bought.id
            CROSS JOIN target
            WHERE oi.dry_run = 0
              AND LOWER(oi.side) LIKE 'buy%'
              AND oi.status IN (
                  'submitted', 'filled', 'open', 'matched', 'partially_filled',
                  'partially_filled_closed', 'pending', 'submitted_unverified',
                  'reconcile_failed'
              )
              AND (
                  oi.market_id = ?
                  OR (
                      target.bucket_city != ''
                      AND target.bucket_target_date != ''
                      AND LOWER(TRIM(COALESCE(bought_rule.city, '')))
                          = target.bucket_city
                      AND COALESCE(bought_rule.target_date, '')
                          = target.bucket_target_date
                  )
                  OR (
                      target.event_key != ''
                      AND COALESCE(
                          NULLIF(bought.event_slug, ''),
                          bought.event_title,
                          ''
                      ) = target.event_key
                  )
              )
            ORDER BY oi.created_at ASC, oi.id ASC
            LIMIT 1
            """,
            (market_id, market_id),
        ).fetchone()

    def get_order_intent(self, intent_id: int) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM order_intents WHERE id = ?",
            (intent_id,),
        ).fetchone()

    def replace_positions(self, positions: list[dict[str, Any]]) -> int:
        self.connection.execute("DELETE FROM positions")
        count = 0
        for position in positions:
            external_market_id = _string_field(
                position, "market", "market_id", "condition_id", "conditionId"
            )
            token_id = _string_field(position, "asset_id", "assetId", "token_id", "tokenId")
            market_id = self.resolve_local_market_id(external_market_id, token_id)
            if not market_id:
                continue
            size = _float_field(position, "size", "shares", "quantity", "balance")
            if size is None:
                continue
            notional = _float_field(position, "notional", "currentValue", "current_value", "value")
            if notional is None:
                price = _float_field(position, "avgPrice", "avg_price", "price", "curPrice")
                notional = abs(size) * price if price is not None else 0
            self.connection.execute(
                """
                INSERT INTO positions (market_id, outcome, size, notional, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(market_id, outcome) DO UPDATE SET
                    size=excluded.size,
                    notional=excluded.notional,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    market_id,
                    _string_field(position, "outcome", "side", "asset_id", "assetId") or "unknown",
                    size,
                    abs(notional),
                ),
            )
            count += 1
        return count

    def resolve_local_market_id(
        self, external_market_id: str | None, token_id: str | None = None
    ) -> str | None:
        """Map Gamma/Data/CLOB identifiers to the local Gamma market id."""
        if external_market_id and self.get_market(external_market_id) is not None:
            return external_market_id

        if token_id:
            row = self.connection.execute(
                """
                SELECT id FROM markets
                WHERE yes_token_id = ? OR no_token_id = ?
                ORDER BY updated_at DESC, id ASC
                LIMIT 1
                """,
                (token_id, token_id),
            ).fetchone()
            if row is not None:
                return str(row["id"])

        if not external_market_id:
            return None
        for row in self.connection.execute("SELECT id, raw_payload FROM markets"):
            payload = _json_loads(row["raw_payload"])
            if not isinstance(payload, dict):
                continue
            condition_id = _string_field(payload, "conditionId", "condition_id")
            if condition_id == external_market_id:
                return str(row["id"])
        return None

    def get_open_order(self, exchange_order_id: str) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM open_orders WHERE exchange_order_id = ?", (exchange_order_id,)
        ).fetchone()

    def mark_open_order_status(
        self, exchange_order_id: str, status: str, raw_payload: dict[str, Any] | None = None
    ) -> None:
        self.connection.execute(
            """
            UPDATE open_orders
            SET status = ?, raw_payload = COALESCE(?, raw_payload), updated_at = CURRENT_TIMESTAMP
            WHERE exchange_order_id = ?
            """,
            (status, to_json(raw_payload) if raw_payload is not None else None, exchange_order_id),
        )

    def list_open_orders(self, limit: int = 100, market_id: str | None = None) -> list[Row]:
        clauses = []
        params: list[Any] = []
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM open_orders
                {where}
                ORDER BY updated_at DESC, exchange_order_id ASC
                LIMIT ?
                """,
                tuple(params),
            )
        )

    def list_positions(
        self,
        limit: int = 100,
        market_id: str | None = None,
        nonzero_only: bool = False,
    ) -> list[Row]:
        clauses = []
        params: list[Any] = []
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id)
        if nonzero_only:
            clauses.append("ABS(size) > 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM positions
                {where}
                ORDER BY updated_at DESC, market_id ASC, outcome ASC
                LIMIT ?
                """,
                tuple(params),
            )
        )

    def list_fills(self, limit: int = 100, market_id: str | None = None) -> list[Row]:
        clauses = []
        params: list[Any] = []
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM fills
                {where}
                ORDER BY filled_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            )
        )

    def nonzero_positions_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM positions WHERE ABS(size) > 0"
        ).fetchone()
        return int(row["count"]) if row else 0

    def upsert_strategy_override(
        self,
        *,
        market_id: str = "*",
        profile: str = "*",
        min_edge: Decimal | float | str | None = None,
        max_order_usdc: Decimal | float | str | None = None,
        max_daily_usdc: Decimal | float | str | None = None,
        max_market_usdc: Decimal | float | str | None = None,
        live_auto_enabled: bool | None = None,
        notes: str | None = None,
    ) -> Row:
        self.connection.execute(
            """
            INSERT INTO strategy_overrides (
                market_id, profile, min_edge, max_order_usdc, max_daily_usdc,
                max_market_usdc, live_auto_enabled, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(market_id, profile) DO UPDATE SET
                min_edge=excluded.min_edge,
                max_order_usdc=excluded.max_order_usdc,
                max_daily_usdc=excluded.max_daily_usdc,
                max_market_usdc=excluded.max_market_usdc,
                live_auto_enabled=excluded.live_auto_enabled,
                notes=excluded.notes,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                market_id,
                profile,
                _float_or_none(min_edge),
                _float_or_none(max_order_usdc),
                _float_or_none(max_daily_usdc),
                _float_or_none(max_market_usdc),
                None if live_auto_enabled is None else int(live_auto_enabled),
                notes,
            ),
        )
        row = self.get_strategy_override(market_id, profile)
        if row is None:
            raise RuntimeError("failed to upsert strategy override")
        return row

    def get_strategy_override(self, market_id: str, profile: str) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM strategy_overrides WHERE market_id = ? AND profile = ?",
            (market_id, profile),
        ).fetchone()

    def effective_strategy_override(self, market_id: str, profile: str) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM strategy_overrides
            WHERE (market_id = ? AND profile = ?)
               OR (market_id = ? AND profile = '*')
               OR (market_id = '*' AND profile = ?)
               OR (market_id = '*' AND profile = '*')
            ORDER BY
                CASE
                    WHEN market_id = ? AND profile = ? THEN 0
                    WHEN market_id = ? AND profile = '*' THEN 1
                    WHEN market_id = '*' AND profile = ? THEN 2
                    ELSE 3
                END,
                updated_at DESC,
                id DESC
            LIMIT 1
            """,
            (market_id, profile, market_id, profile, market_id, profile, market_id, profile),
        ).fetchone()

    def list_strategy_overrides(self, limit: int = 100) -> list[Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM strategy_overrides
                ORDER BY updated_at DESC, market_id ASC, profile ASC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def delete_strategy_override(self, market_id: str, profile: str) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM strategy_overrides WHERE market_id = ? AND profile = ?",
            (market_id, profile),
        )
        return bool(cursor.rowcount)

    def create_automation_action(self, action: Any) -> Row:
        return self.automation.create_automation_action(action)

    def get_automation_action(self, action_id: str) -> Row | None:
        return self.automation.get_automation_action(action_id)

    def approve_automation_action(self, action_id: str, actor: str, now: str) -> bool:
        return self.automation.approve_automation_action(action_id, actor, now)

    def reject_automation_action(
        self, action_id: str, actor: str, reason: str | None, now: str
    ) -> bool:
        return self.automation.reject_automation_action(action_id, actor, reason, now)

    def expire_automation_actions(self, now: str) -> int:
        return self.automation.expire_automation_actions(now)

    def claim_approved_automation_action(self, action_id: str, now: str) -> Row | None:
        return self.automation.claim_approved_automation_action(action_id, now)

    def claim_next_approved_automation_action(self, now: str) -> Row | None:
        return self.automation.claim_next_approved_automation_action(now)

    def mark_automation_action_executing(
        self, action_id: str, argv: list[str], started_at: str
    ) -> None:
        self.automation.mark_automation_action_executing(action_id, argv, started_at)

    def mark_automation_action_executed(
        self,
        action_id: str,
        return_code: int,
        result_summary: str,
        now: str,
        duration_ms: int | None = None,
    ) -> None:
        self.automation.mark_automation_action_executed(
            action_id, return_code, result_summary, now, duration_ms
        )

    def mark_automation_action_failed(
        self,
        action_id: str,
        return_code: int | None,
        failure_reason: str,
        now: str,
        duration_ms: int | None = None,
    ) -> None:
        self.automation.mark_automation_action_failed(
            action_id, return_code, failure_reason, now, duration_ms
        )

    def append_automation_audit_event(
        self, action_id: str, event: str, actor: str | None, details: Any
    ) -> None:
        self.automation.append_automation_audit_event(action_id, event, actor, details)

    def list_automation_audit_events(self, action_id: str) -> list[Row]:
        return self.automation.list_automation_audit_events(action_id)

    def list_automation_actions(
        self,
        limit: int = 20,
        status: str | None = None,
        kind: str | None = None,
        market_id: str | None = None,
    ) -> list[Row]:
        return self.automation.list_automation_actions(limit, status, kind, market_id)

    def automation_status_counts(self) -> list[Row]:
        return self.automation.automation_status_counts()

    def candidate_status_counts(self) -> list[Row]:
        return list(
            self.connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM market_candidates
                GROUP BY status
                ORDER BY status ASC
                """
            )
        )

    def latest_reconciliation(self) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM reconciliations
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

    def list_recent_runs(self, limit: int = 20) -> list[Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM runs
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def start_run(self, command: str, status: str = "running") -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO runs (command, status, started_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (command, status),
        )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        self.connection.execute(
            """
            UPDATE runs
            SET status = ?, finished_at = CURRENT_TIMESTAMP, error = ?
            WHERE id = ?
            """,
            (status, error, run_id),
        )

    def next_dry_run_ready_candidate(self) -> Row | None:
        return self.connection.execute(
            """
            SELECT c.*, m.title, m.slug
            FROM market_candidates c
            JOIN markets m ON m.id = c.market_id
            WHERE c.status = 'dry_run_ready'
            ORDER BY c.updated_at DESC, c.market_id ASC
            LIMIT 1
            """
        ).fetchone()

    def latest_action_for_market(self, market_id: str, kind: str | None = None) -> Row | None:
        return self.automation.latest_action_for_market(market_id, kind)

    def latest_action_by_status(self, *statuses: str) -> Row | None:
        return self.automation.latest_action_by_status(*statuses)

    def latest_failed_action(self) -> Row | None:
        return self.automation.latest_failed_action()

    def latest_forecast(self, market_id: str) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM weather_forecasts
            WHERE market_id = ?
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()

    def latest_market_snapshot(self, market_id: str, token_id: str | None = None) -> Row | None:
        """Return the newest quote row for a market, optionally token-scoped.

        When ``token_id`` is supplied, only that outcome token is considered.
        Legacy rows with ``token_id IS NULL`` are never substituted for a
        requested token — callers must REST-refresh when a token-specific
        snapshot is missing.
        """
        if token_id is not None:
            token = str(token_id)
            return self.connection.execute(
                """
                SELECT * FROM market_snapshots
                WHERE market_id = ? AND token_id = ?
                ORDER BY fetched_at DESC, id DESC
                LIMIT 1
                """,
                (market_id, token),
            ).fetchone()
        return self.connection.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = ?
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()

    def latest_pricing_snapshot(
        self,
        market_id: str,
        *,
        outcome: str | None = None,
        side: str | None = None,
    ) -> Row | None:
        """Token-scoped quote for pricing, edge, and entry decisions.

        Resolves the outcome token from the local market row and returns only
        that token's snapshot. Never falls back to the other outcome, to a
        legacy ``token_id IS NULL`` row, or to an undifferentiated latest row.
        Missing token-specific data returns None so callers REST-refresh.
        """
        market = self.get_market(market_id)
        if market is None:
            return None
        resolved_outcome = outcome
        if resolved_outcome is None and side is not None:
            side_norm = str(side).strip().lower()
            if side_norm in {"buy_no", "sell_no", "no", "n"}:
                resolved_outcome = "NO"
            else:
                resolved_outcome = "YES"
        if resolved_outcome is None:
            resolved_outcome = "YES"
        outcome_norm = str(resolved_outcome).strip().upper()
        if outcome_norm in {"NO", "N"}:
            token_id = market["no_token_id"]
        elif outcome_norm in {"YES", "Y"}:
            token_id = market["yes_token_id"]
        else:
            # Explicit asset id: only accept exact yes/no token match.
            yes = str(market["yes_token_id"] or "")
            no = str(market["no_token_id"] or "")
            raw = str(resolved_outcome)
            if yes and raw == yes:
                token_id = yes
            elif no and raw == no:
                token_id = no
            else:
                return None
        if not token_id:
            return None
        row = self.latest_market_snapshot(market_id, token_id=str(token_id))
        if row is not None:
            return row
        # Safe legacy path: only when this market has never stored a token-tagged
        # quote. Once any token_id is present (YES or NO), never fall back to
        # NULL/unscoped history — that is how YES/NO pollution happens.
        tagged = self.connection.execute(
            """
            SELECT 1 FROM market_snapshots
            WHERE market_id = ? AND token_id IS NOT NULL AND token_id != ''
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()
        if tagged is not None:
            return None
        return self.connection.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = ? AND (token_id IS NULL OR token_id = '')
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()

    def latest_analysis(self, market_id: str) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM analyses
            WHERE market_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()

    def forecast_revision_for_analysis(self, analysis_id: int) -> str | None:
        """Return the forecast revision that produced one persisted analysis."""
        row = self.connection.execute(
            """
            SELECT json_extract(raw_payload, '$.forecast_revision') AS forecast_revision
            FROM model_signals
            WHERE analysis_id = ?
            LIMIT 1
            """,
            (analysis_id,),
        ).fetchone()
        if row is None or row["forecast_revision"] is None:
            return None
        revision = str(row["forecast_revision"]).strip()
        return revision or None

    def list_recent_analyses(self, market_id: str, limit: int = 3) -> list[Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM analyses
                WHERE market_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (market_id, limit),
            )
        )

    def get_market(self, market_id: str) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM markets WHERE id = ?", (market_id,)
        ).fetchone()

    def get_resolution_rule(self, market_id: str) -> Row | None:
        return self.connection.execute(
            "SELECT * FROM resolution_rules WHERE market_id = ?", (market_id,)
        ).fetchone()

    def list_weather_markets(self) -> list[Row]:
        return self.list_markets(module_id="weather")

    def list_markets(self, limit: int = 100, module_id: str | None = None) -> list[Row]:
        if module_id:
            return list(
                self.connection.execute(
                    "SELECT * FROM markets WHERE module_id = ? ORDER BY updated_at DESC, id ASC LIMIT ?",
                    (module_id, limit),
                )
            )
        return list(
            self.connection.execute(
                "SELECT * FROM markets ORDER BY updated_at DESC, id ASC LIMIT ?",
                (limit,),
            )
        )

    def list_weather_market_overview(
        self, limit: int = 100, module_id: str | None = None
    ) -> list[Row]:
        clauses = ["m.is_weather = 1"]
        params: list[Any] = []
        if module_id:
            clauses.append("m.module_id = ?")
            params.append(module_id)
        where = "WHERE " + " AND ".join(clauses)
        params.append(limit)
        return list(
            self.connection.execute(
                f"""
                SELECT
                    m.*,
                    c.status AS candidate_status,
                    s.best_bid,
                    s.best_ask,
                    s.spread,
                    s.fetched_at AS snapshot_fetched_at,
                    t.city,
                    t.city_cn,
                    t.source,
                    t.bucket_center_c,
                    t.bucket_lower_c,
                    t.bucket_upper_c,
                    t.target_date
                FROM markets m
                LEFT JOIN market_candidates c ON c.market_id = m.id
                LEFT JOIN market_snapshots s ON s.id = (
                    SELECT id FROM market_snapshots
                    WHERE market_id = m.id
                    ORDER BY fetched_at DESC, id DESC
                    LIMIT 1
                )
                LEFT JOIN temperature_bucket_rules t ON t.market_id = m.id
                {where}
                ORDER BY m.updated_at DESC, m.id ASC
                LIMIT ?
                """,
                params,
            )
        )

    def daily_order_notional(self, day_prefix: str) -> Decimal:
        rows = self.connection.execute(
            """
            SELECT oi.notional, oi.limit_price, oi.size, m.raw_payload
            FROM order_intents oi
            LEFT JOIN markets m ON m.id = oi.market_id
            WHERE oi.dry_run = 0
              AND LOWER(oi.side) LIKE 'buy%'
              AND oi.status IN (
                  'submitted', 'filled', 'open', 'matched', 'partially_filled',
                  'partially_filled_closed', 'pending', 'submitted_unverified',
                  'reconcile_failed'
              )
              AND oi.created_at LIKE ?
            """,
            (f"{day_prefix}%",),
        ).fetchall()
        return sum((self._buy_cash_at_risk(row) for row in rows), Decimal("0"))

    def market_exposure(self, market_id: str) -> Decimal:
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(notional), 0) AS total
            FROM positions
            WHERE market_id = ?
            """,
            (market_id,),
        ).fetchone()
        reconciled_total = Decimal(str(row["total"] or 0))
        if reconciled_total:
            return reconciled_total
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(notional), 0) AS total
            FROM order_intents
            WHERE dry_run = 0 AND status IN ('submitted', 'filled', 'open') AND market_id = ?
            """,
            (market_id,),
        ).fetchone()
        return Decimal(str(row["total"] or 0))

    def live_buy_notional_for_market(self, market_id: str) -> Decimal:
        """Fee-adjusted capital committed by accepted BUY intents for this market."""
        rows = self.connection.execute(
            """
            SELECT oi.notional, oi.limit_price, oi.size, m.raw_payload
            FROM order_intents oi
            LEFT JOIN markets m ON m.id = oi.market_id
            WHERE oi.market_id = ? AND oi.dry_run = 0 AND LOWER(oi.side) LIKE 'buy%'
              AND oi.status IN (
                  'submitted', 'filled', 'open', 'matched', 'partially_filled',
                  'partially_filled_closed', 'pending', 'submitted_unverified',
                  'reconcile_failed'
              )
            """,
            (market_id,),
        ).fetchall()
        return sum((self._buy_cash_at_risk(row) for row in rows), Decimal("0"))

    def list_resolved_live_bucket_entries(
        self, *, entry_policy_version: str
    ) -> list[dict[str, Any]]:
        """Return resolved live entries priced from exact reconciled BUY fills.

        Intent price and size describe what we tried to buy, not what the exchange
        filled. Historical sizing therefore requires an exact intent -> order id ->
        reconciled fill chain and ignores unlinked legacy fills.
        """
        candidates = self.connection.execute(
            """
            SELECT oi.id AS intent_id,
                   oi.market_id,
                   oi.entry_policy_version,
                   r.target_date,
                   r.settlement_timezone,
                   (
                       SELECT ms.resolved_outcome
                       FROM model_signals ms
                       WHERE ms.market_id = oi.market_id
                         AND ms.outcome_status = 'resolved'
                         AND ms.resolved_outcome IN ('yes', 'no')
                       ORDER BY ms.created_at DESC, ms.id DESC
                       LIMIT 1
                   ) AS resolved_outcome
            FROM order_intents oi
            JOIN temperature_bucket_rules r ON r.market_id = oi.market_id
            WHERE oi.dry_run = 0
              AND oi.entry_policy_version = ?
              AND LOWER(oi.side) LIKE 'buy%'
              AND oi.status IN (
                  'filled', 'matched', 'partially_filled', 'partially_filled_closed'
              )
            ORDER BY oi.created_at ASC, oi.id ASC
            """,
            (entry_policy_version,),
        ).fetchall()
        campaigns: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            outcome = str(candidate["resolved_outcome"] or "").lower()
            if outcome not in {"yes", "no"}:
                continue
            market_id = str(candidate["market_id"])
            campaign = campaigns.setdefault(
                market_id,
                {
                    "market_id": market_id,
                    "entry_policy_version": candidate["entry_policy_version"],
                    "target_date": candidate["target_date"],
                    "settlement_timezone": candidate["settlement_timezone"],
                    "resolved_outcome": outcome,
                    "filled_size": Decimal("0"),
                    "filled_notional": Decimal("0"),
                    "entered_at": None,
                },
            )
            order_ids = self.get_order_ids_for_intent(int(candidate["intent_id"]))
            if not order_ids:
                continue
            placeholders = ",".join("?" for _ in order_ids)
            fills = self.connection.execute(
                f"""
                SELECT * FROM fills
                WHERE market_id = ? AND order_id IN ({placeholders})
                ORDER BY filled_at ASC, id ASC
                """,  # noqa: S608 - placeholders contain only literal question marks.
                (market_id, *order_ids),
            ).fetchall()
            for fill in fills:
                view = account_fill_view(dict(fill))
                if not str(view.get("side") or "").upper().startswith("BUY"):
                    continue
                price = Decimal(str(view.get("price") or 0))
                size = Decimal(str(view.get("size") or 0))
                if price <= 0 or size <= 0:
                    continue
                campaign["filled_size"] += size
                campaign["filled_notional"] += price * size
                filled_at = fill["filled_at"]
                if filled_at and (
                    campaign["entered_at"] is None or str(filled_at) < campaign["entered_at"]
                ):
                    campaign["entered_at"] = str(filled_at)
        results: list[dict[str, Any]] = []
        for campaign in campaigns.values():
            filled_size = campaign.pop("filled_size")
            filled_notional = campaign.pop("filled_notional")
            if filled_size <= 0 or campaign["entered_at"] is None:
                continue
            campaign["entry_price"] = filled_notional / filled_size
            results.append(campaign)
        return sorted(results, key=lambda row: row["entered_at"], reverse=True)

    @staticmethod
    def _buy_cash_at_risk(row: Row) -> Decimal:
        notional = Decimal(str(row["notional"] or 0))
        try:
            payload = json.loads(row["raw_payload"] or "{}")
        except Exception:
            payload = {}
        fee = expected_buy_fee(
            shares=Decimal(str(row["size"] or 0)),
            price=Decimal(str(row["limit_price"] or 0)),
            market_payload=payload if isinstance(payload, dict) else {},
        )
        return notional + fee

    def _list_order_intents_with_fill_progress(
        self,
        *,
        limit: int,
        market_id: str | None = None,
        after_id: int | None = None,
        ascending: bool = False,
    ) -> list[Row]:
        """Read intent targets beside exact reconciled fills linked by order id."""
        clauses: list[str] = []
        params: list[Any] = []
        if market_id is not None:
            clauses.append("oi.market_id = ?")
            params.append(market_id)
        if after_id is not None:
            clauses.append("oi.id > ?")
            params.append(max(0, int(after_id)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        direction = "ASC" if ascending else "DESC"
        params.append(max(1, min(int(limit), 500)))
        return list(
            self.connection.execute(
                f"""
                SELECT oi.*,
                       COALESCE((
                           SELECT SUM(f.size)
                           FROM fills f
                           WHERE EXISTS (
                               SELECT 1
                               FROM order_attempts oa
                               WHERE oa.intent_id = oi.id
                                 AND f.order_id IN (
                                     json_extract(oa.response_payload, '$.order_id'),
                                     json_extract(oa.response_payload, '$.orderID'),
                                     json_extract(oa.response_payload, '$.orderId'),
                                     json_extract(oa.response_payload, '$.id'),
                                     json_extract(oa.request_payload, '$.order_id'),
                                     json_extract(oa.request_payload, '$.orderID'),
                                     json_extract(oa.request_payload, '$.orderId'),
                                     json_extract(oa.request_payload, '$.id')
                                 )
                               )
                       ), 0) AS filled_size
                FROM order_intents oi
                {where}
                ORDER BY oi.created_at {direction}, oi.id {direction}
                LIMIT ?
                """,
                tuple(params),
            )
        )

    def list_recent_order_intents(self, limit: int = 20, market_id: str | None = None) -> list[Row]:
        return self._list_order_intents_with_fill_progress(
            limit=limit,
            market_id=market_id,
        )

    def list_recent_risk_decisions(
        self, limit: int = 100, market_id: str | None = None
    ) -> list[Row]:
        if market_id:
            return list(
                self.connection.execute(
                    """
                    SELECT * FROM risk_decisions
                    WHERE market_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (market_id, limit),
                )
            )
        return list(
            self.connection.execute(
                """
                SELECT * FROM risk_decisions
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def latest_dry_run_rehearsal(self) -> dict[str, Any] | None:
        """获取最近一次 dry_run 演练的完整信息（order_intent + risk_decision + market）"""
        row = self.connection.execute(
            """
            SELECT
                oi.id as intent_id,
                oi.market_id,
                oi.side,
                oi.limit_price,
                oi.size,
                oi.notional,
                oi.rationale,
                oi.created_at as intent_created_at,
                rd.id as risk_id,
                rd.accepted,
                rd.reasons,
                m.title as market_title
            FROM order_intents oi
            LEFT JOIN risk_decisions rd ON rd.market_id = oi.market_id
                AND rd.created_at <= oi.created_at
            LEFT JOIN markets m ON m.id = oi.market_id
            WHERE oi.dry_run = 1
            ORDER BY oi.created_at DESC, oi.id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_reconciliations(self, limit: int = 20) -> list[Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM reconciliations
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def save_reconciliation(self, status: str, details: Any) -> int:
        cursor = self.connection.execute(
            "INSERT INTO reconciliations (status, details, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (status, to_json(details)),
        )
        return int(cursor.lastrowid)

    def latest_successful_reconciliation(self) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM reconciliations
            WHERE status = 'ok'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

    def ensure_autopilot_state(
        self,
        *,
        mode: str = "dry_run",
        app_mode: str = "paper",
        tick_seconds: int = 300,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO autopilot_state (id, enabled, mode, app_mode, tick_seconds, tick_count)
            VALUES (1, 0, ?, ?, ?, 0)
            """,
            (mode, app_mode, tick_seconds),
        )

    def get_autopilot_state(self) -> Row | None:
        return self.connection.execute("SELECT * FROM autopilot_state WHERE id = 1").fetchone()

    def update_autopilot_state(
        self,
        *,
        enabled: bool | None = None,
        mode: str | None = None,
        app_mode: str | None = None,
        tick_seconds: int | None = None,
        last_tick_at: str | None = None,
        last_tick_status: str | None = None,
        last_error: str | None = None,
        process_started_at: str | None = None,
        latest_useful_tick_at: str | None = None,
        last_tick_duration_ms: int | None = None,
        deferred_candidates_count: int | None = None,
        exchange_stream_status: str | None = None,
        exchange_stream_updated_at: str | None = None,
        exchange_stream_detail: str | None = None,
        last_portfolio_digest_at: str | None = None,
        clear_last_error: bool = False,
        increment_tick_count: bool = False,
        reset_tick_count: bool = False,
    ) -> None:
        self.ensure_autopilot_state()
        fields: list[str] = []
        values: list[object] = []
        if enabled is not None:
            fields.append("enabled = ?")
            values.append(int(enabled))
        if mode is not None:
            fields.append("mode = ?")
            values.append(mode)
        if app_mode is not None:
            fields.append("app_mode = ?")
            values.append(app_mode)
        if tick_seconds is not None:
            fields.append("tick_seconds = ?")
            values.append(tick_seconds)
        if last_tick_at is not None:
            fields.append("last_tick_at = ?")
            values.append(last_tick_at)
        if last_tick_status is not None:
            fields.append("last_tick_status = ?")
            values.append(last_tick_status)
        if last_error is not None:
            fields.append("last_error = ?")
            values.append(last_error)
        elif clear_last_error:
            fields.append("last_error = NULL")
        if process_started_at is not None:
            fields.append("process_started_at = ?")
            values.append(process_started_at)
        if latest_useful_tick_at is not None:
            fields.append("latest_useful_tick_at = ?")
            values.append(latest_useful_tick_at)
        if last_tick_duration_ms is not None:
            fields.append("last_tick_duration_ms = ?")
            values.append(last_tick_duration_ms)
        if deferred_candidates_count is not None:
            fields.append("deferred_candidates_count = ?")
            values.append(deferred_candidates_count)
        if exchange_stream_status is not None:
            fields.append("exchange_stream_status = ?")
            values.append(str(exchange_stream_status)[:32])
        if exchange_stream_updated_at is not None:
            fields.append("exchange_stream_updated_at = ?")
            values.append(exchange_stream_updated_at)
        if exchange_stream_detail is not None:
            # Bound JSON snapshot; never store credentials.
            fields.append("exchange_stream_detail = ?")
            values.append(str(exchange_stream_detail)[:4000])
        if last_portfolio_digest_at is not None:
            fields.append("last_portfolio_digest_at = ?")
            values.append(last_portfolio_digest_at)
        if increment_tick_count:
            fields.append("tick_count = tick_count + 1")
        if reset_tick_count:
            fields.append("tick_count = 0")
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        self.connection.execute(
            f"UPDATE autopilot_state SET {', '.join(fields)} WHERE id = 1",
            values,
        )

    def purge_demo_markets(self) -> int:
        rows = self.connection.execute("SELECT id FROM markets WHERE id LIKE 'demo-%'").fetchall()
        if not rows:
            return 0
        self.connection.execute("DELETE FROM autopilot_decisions WHERE market_id LIKE 'demo-%'")
        self.connection.execute("DELETE FROM market_candidates WHERE market_id LIKE 'demo-%'")
        self.connection.execute("DELETE FROM analyses WHERE market_id LIKE 'demo-%'")
        self.connection.execute("DELETE FROM weather_forecasts WHERE market_id LIKE 'demo-%'")
        self.connection.execute("DELETE FROM market_snapshots WHERE market_id LIKE 'demo-%'")
        return len(rows)

    def clear_autopilot_history(self) -> None:
        self.ensure_autopilot_state()
        self.connection.execute("DELETE FROM autopilot_decisions")
        self.connection.execute(
            """
            UPDATE autopilot_state
            SET tick_count = 0,
                last_tick_at = NULL,
                last_tick_status = NULL,
                last_error = NULL,
                last_portfolio_digest_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    def save_autopilot_decision(
        self,
        *,
        market_id: str | None,
        action: str,
        mode: str,
        edge: Decimal | None,
        reason: str,
        blockers: list[str],
        status: str,
        intent_id: int | None = None,
        discovered: int = 0,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_confidence: Decimal | None = None,
        llm_reason: str | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO autopilot_decisions (
                market_id, action, mode, edge, reason, blockers, status, intent_id, discovered,
                llm_provider, llm_model, llm_confidence, llm_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                action,
                mode,
                _float_or_none(edge),
                reason,
                to_json(blockers),
                status,
                intent_id,
                discovered,
                llm_provider,
                llm_model,
                _float_or_none(llm_confidence),
                llm_reason,
            ),
        )
        return int(cursor.lastrowid)

    def list_autopilot_decisions(self, *, limit: int = 20) -> list[Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM autopilot_decisions
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def has_recent_autopilot_decision(
        self,
        *,
        market_id: str,
        action: str,
        since_minutes: int = 60,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM autopilot_decisions
            WHERE market_id = ? AND action = ?
              AND datetime(created_at) >= datetime('now', ?)
            LIMIT 1
            """,
            (market_id, action, f"-{max(1, int(since_minutes))} minutes"),
        ).fetchone()
        return row is not None

    def latest_autopilot_decision_for_action(
        self,
        *,
        market_id: str,
        action: str,
    ) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM autopilot_decisions
            WHERE market_id = ? AND action = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (market_id, action),
        ).fetchone()

    def update_autopilot_decision_outcome(
        self,
        decision_id: int,
        *,
        status: str,
        reason: str,
        blockers: list[str] | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE autopilot_decisions
            SET status = ?, reason = ?, blockers = ?
            WHERE id = ?
            """,
            (
                status,
                reason,
                to_json(blockers or []),
                int(decision_id),
            ),
        )

    def list_autopilot_decisions_after(self, *, after_id: int = 0, limit: int = 100) -> list[Row]:
        """Bounded ascending cursor over decision events for local stream polling."""
        limit = max(1, min(int(limit), 500))
        after_id = max(0, int(after_id))
        return list(
            self.connection.execute(
                """
                SELECT * FROM autopilot_decisions
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (after_id, limit),
            )
        )

    def list_fills_after(self, *, after_id: int = 0, limit: int = 100) -> list[Row]:
        """Bounded ascending cursor over confirmed fills for local stream polling."""
        limit = max(1, min(int(limit), 500))
        after_id = max(0, int(after_id))
        return list(
            self.connection.execute(
                """
                SELECT * FROM fills
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (after_id, limit),
            )
        )

    def list_order_intents_after(self, *, after_id: int = 0, limit: int = 100) -> list[Row]:
        """Bounded ascending cursor over order intents for local stream polling."""
        return self._list_order_intents_with_fill_progress(
            limit=limit,
            after_id=after_id,
            ascending=True,
        )

    def list_order_attempts_after(self, *, after_id: int = 0, limit: int = 100) -> list[Row]:
        """Bounded ascending cursor over order attempts for local stream polling."""
        limit = max(1, min(int(limit), 500))
        after_id = max(0, int(after_id))
        return list(
            self.connection.execute(
                """
                SELECT id, intent_id, status, error, created_at
                FROM order_attempts
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (after_id, limit),
            )
        )

    def list_analyses_after(self, *, after_id: int = 0, limit: int = 100) -> list[Row]:
        """Bounded ascending cursor over analyses for honest edge/price history."""
        limit = max(1, min(int(limit), 500))
        after_id = max(0, int(after_id))
        return list(
            self.connection.execute(
                """
                SELECT id, market_id, model_version, fair_lower, fair_upper,
                       reference_price, edge, side, decision, created_at
                FROM analyses
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (after_id, limit),
            )
        )

    def stream_cursor_high_water(self) -> dict[str, int]:
        """Latest ids for stream reconnect bootstrap (0 when empty)."""
        tables = (
            ("after_decision_id", "autopilot_decisions"),
            ("after_fill_id", "fills"),
            ("after_intent_id", "order_intents"),
            ("after_attempt_id", "order_attempts"),
            ("after_analysis_id", "analyses"),
        )
        out: dict[str, int] = {}
        for key, table in tables:
            row = self.connection.execute(f"SELECT MAX(id) AS max_id FROM {table}").fetchone()
            out[key] = int(row["max_id"] or 0) if row is not None else 0
        return out

    def best_weather_candidate_by_edge(self, *, min_edge: float = 0.05) -> Row | None:
        # Load all trade-ready rows, filter orderable first, then rank by edge.
        # Never truncate before eligibility — a closed top-50 must not hide open #51.
        rows = self.connection.execute(
            """
            SELECT c.market_id, c.status, a.edge, a.decision, a.side,
                   a.fair_lower, a.fair_upper, a.model_version, a.reasons,
                   m.title,
                   m.close_time, m.raw_payload
            FROM market_candidates c
            JOIN markets m ON m.id = c.market_id
            LEFT JOIN analyses a ON a.market_id = c.market_id
                AND a.id = (
                    SELECT id FROM analyses
                    WHERE market_id = c.market_id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                )
            WHERE c.status = 'dry_run_ready'
              AND m.module_id IN ('weather', 'global_temp_bucket')
              AND m.id NOT LIKE 'demo-%'
              AND a.decision IN ('buy', 'trade')
              AND a.edge >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM positions p
                  WHERE p.market_id = m.id AND ABS(p.size) > 0
              )
              AND NOT EXISTS (
                  SELECT 1 FROM open_orders oo
                  WHERE oo.market_id = m.id AND oo.size > 0
              )
              AND NOT EXISTS (
                  SELECT 1 FROM order_intents oi
                  WHERE oi.market_id = m.id AND oi.dry_run = 0
                    AND oi.status IN (
                        'submitted', 'open', 'partially_filled', 'pending',
                        'submitted_unverified', 'reconcile_failed'
                    )
              )
            """,
            (min_edge,),
        ).fetchall()
        orderable = [
            row
            for row in rows
            if self._row_is_orderable_candidate(row)
            and self.active_live_sibling_market(str(row["market_id"])) is None
        ]
        if not orderable:
            return None
        orderable.sort(key=_weather_opportunity_sort_key)
        return orderable[0]

    def list_ranked_weather_opportunities(self, *, limit: int = 20) -> list[Row]:
        rows = list(
            self.connection.execute(
                """
                SELECT
                    c.market_id,
                    c.module_id,
                    c.best_bid,
                    c.best_ask,
                    c.rejection_reason,
                    c.notes,
                    m.title,
                    m.close_time,
                    m.raw_payload,
                    a.fair_lower,
                    a.fair_upper,
                    a.reference_price,
                    a.edge,
                    a.side,
                    a.decision,
                    a.reasons AS analysis_reasons,
                    f.value AS forecast_value,
                    f.unit AS forecast_unit,
                    f.provider AS forecast_provider,
                    f.raw_payload AS forecast_raw_payload
                FROM market_candidates c
                JOIN markets m ON m.id = c.market_id
                LEFT JOIN analyses a ON a.id = (
                    SELECT id FROM analyses
                    WHERE market_id = c.market_id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                )
                LEFT JOIN weather_forecasts f ON f.id = (
                    SELECT id FROM weather_forecasts
                    WHERE market_id = c.market_id
                    ORDER BY fetched_at DESC, id DESC
                    LIMIT 1
                )
                WHERE c.status = 'dry_run_ready'
                  AND m.module_id IN ('weather', 'global_temp_bucket')
                  AND m.id NOT LIKE 'demo-%'
                """
            )
        )
        orderable = [row for row in rows if self._row_is_orderable_candidate(row)]
        orderable.sort(
            key=lambda r: (
                r["edge"] is None,
                -(float(r["edge"]) if r["edge"] is not None else 0.0),
                r["best_ask"] is None,
            )
        )
        return orderable[:limit]

    def _row_is_orderable_candidate(self, row: Row) -> bool:
        payload = _json_loads(row["raw_payload"]) if "raw_payload" in row.keys() else {}
        if not isinstance(payload, dict):
            payload = {}
        title = row["title"] if "title" in row.keys() else None
        close_time = row["close_time"] if "close_time" in row.keys() else None
        return is_market_orderable(
            raw_payload=payload,
            title=title,
            close_time=close_time,
        )

    def get_markets_needing_resolution_audit(self, *, limit: int = 5) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT ms.market_id
            FROM model_signals ms
            JOIN markets m ON ms.market_id = m.id
            WHERE ms.outcome_status = 'pending'
              AND m.module_id IN ('weather', 'china_temp_bucket', 'global_temp_bucket')
              AND m.id NOT LIKE 'demo-%'
              AND json_extract(m.raw_payload, '$.closed') = 1
              AND lower(json_extract(m.raw_payload, '$.umaResolutionStatus')) = 'resolved'
            GROUP BY ms.market_id
            ORDER BY MIN(m.close_time), ms.market_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [r["market_id"] for r in rows]

    def get_event_slugs_needing_resolution_audit(self, *, limit: int = 3) -> list[str]:
        """Return expired events with pending signals and no recent resolution read."""
        rows = self.connection.execute(
            """
            SELECT m.event_slug
            FROM model_signals ms
            JOIN markets m ON ms.market_id = m.id
            LEFT JOIN temperature_bucket_rules bucket ON bucket.market_id = m.id
            WHERE ms.outcome_status = 'pending'
              AND m.module_id IN ('weather', 'china_temp_bucket', 'global_temp_bucket')
              AND m.id NOT LIKE 'demo-%'
              AND m.event_slug IS NOT NULL
              AND m.event_slug != ''
              AND (
                    lower(COALESCE(m.status, '')) IN ('closed', 'resolved')
                    OR json_extract(m.raw_payload, '$.closed') = 1
                    OR (
                        bucket.target_date IS NOT NULL
                        AND date(bucket.target_date) < date('now')
                    )
                    OR (
                        bucket.target_date IS NULL
                        AND m.close_time IS NOT NULL
                        AND datetime(m.close_time) <= datetime('now')
                    )
                  )
              AND NOT EXISTS (
                    SELECT 1
                    FROM markets event_market
                    JOIN resolution_audits recent
                      ON recent.market_id = event_market.id
                    WHERE event_market.event_slug = m.event_slug
                      AND recent.polymarket_source = 'gamma_api'
                      AND recent.created_at >= datetime('now', '-6 hours')
                  )
            GROUP BY m.event_slug
            ORDER BY MIN(m.close_time), m.event_slug
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [str(row["event_slug"]) for row in rows]

    _TERMINAL_ROUNDTRIP_STATUSES = frozenset({"completed", "failed"})

    def create_roundtrip_run(self, market_id: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO roundtrip_runs (market_id, status)
            VALUES (?, 'ready_to_buy')
            """,
            (market_id,),
        )
        return int(cursor.lastrowid)

    def update_roundtrip_run_buy(self, run_id: int, buy_intent_id: int, status: str) -> None:
        self.connection.execute(
            """
            UPDATE roundtrip_runs
            SET buy_intent_id = ?, status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (buy_intent_id, status, run_id),
        )

    def update_roundtrip_run_sell(self, run_id: int, sell_intent_id: int, status: str) -> None:
        self.connection.execute(
            """
            UPDATE roundtrip_runs
            SET sell_intent_id = ?, status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (sell_intent_id, status, run_id),
        )

    def update_roundtrip_run_status(self, run_id: int, status: str) -> None:
        self.connection.execute(
            """
            UPDATE roundtrip_runs
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, run_id),
        )

    def get_active_roundtrip_run(self, market_id: str) -> Row | None:
        return self.connection.execute(
            """
            SELECT * FROM roundtrip_runs
            WHERE market_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (market_id,),
        ).fetchone()

    def list_roundtrip_markets_needing_status_refresh(self, limit: int = 100) -> list[str]:
        """Return latest SELL-linked runs whose derived status is not terminal."""
        rows = self.connection.execute(
            """
            SELECT run.market_id
            FROM roundtrip_runs run
            JOIN (
                SELECT market_id, MAX(id) AS latest_id
                FROM roundtrip_runs
                GROUP BY market_id
            ) latest ON latest.latest_id = run.id
            WHERE run.sell_intent_id IS NOT NULL
              AND run.status NOT IN ('completed', 'failed')
            ORDER BY run.updated_at DESC, run.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [str(row["market_id"]) for row in rows]

    def ensure_open_roundtrip_run(self, market_id: str) -> int:
        """Return latest open run for market, or create a new one if none/terminal."""
        run = self.get_active_roundtrip_run(market_id)
        if run is None or str(run["status"]) in self._TERMINAL_ROUNDTRIP_STATUSES:
            return self.create_roundtrip_run(market_id)
        return int(run["id"])

    def record_roundtrip_buy_intent(
        self, market_id: str, buy_intent_id: int, *, status: str = "buy_open"
    ) -> int:
        """Attach a live BUY intent to the market's open roundtrip run (create if needed)."""
        run = self.get_active_roundtrip_run(market_id)
        if (
            run is not None
            and str(run["status"]) not in self._TERMINAL_ROUNDTRIP_STATUSES
            and run["buy_intent_id"] is not None
            and int(run["buy_intent_id"]) != int(buy_intent_id)
            and run["sell_intent_id"] is not None
        ):
            # Previous run already has buy+sell; start a fresh run for a new BUY.
            run_id = self.create_roundtrip_run(market_id)
        else:
            run_id = self.ensure_open_roundtrip_run(market_id)
        self.update_roundtrip_run_buy(run_id, buy_intent_id, status)
        return run_id

    def record_roundtrip_sell_intent(
        self, market_id: str, sell_intent_id: int, *, status: str = "sell_open"
    ) -> int:
        """Attach a live SELL intent to the market's open roundtrip run (create if needed)."""
        run_id = self.ensure_open_roundtrip_run(market_id)
        self.update_roundtrip_run_sell(run_id, sell_intent_id, status)
        return run_id

    def get_order_ids_for_intent(self, intent_id: int) -> list[str]:
        """Collect exchange order ids from attempts for an intent.

        Accepts common Polymarket / adapter field names:
        ``order_id`` (polymarket-client adapter), ``orderID`` / ``orderId``, and ``id``.
        """
        rows = self.connection.execute(
            """
            SELECT response_payload, request_payload FROM order_attempts
            WHERE intent_id = ?
              AND status IN (
                  'submitted', 'checked', 'reconciled',
                  'reconcile_failed', 'check_failed', 'cancelled'
              )
            """,
            (intent_id,),
        ).fetchall()
        order_ids: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for field in ("response_payload", "request_payload"):
                payload = _json_loads(row[field])
                if not isinstance(payload, dict):
                    continue
                # Prefer explicit order id keys over generic "id".
                oid = _string_field(payload, "order_id", "orderID", "orderId", "id")
                if oid and oid not in seen and oid not in {"ok", "true", "false"}:
                    seen.add(oid)
                    order_ids.append(oid)
        return order_ids

    def roundtrip_intent_ids(self, run_id: int, *, side_prefix: str) -> list[int]:
        """All live intents belonging to one roundtrip campaign.

        ``roundtrip_runs`` stores the latest BUY and SELL pointers for lifecycle
        status. Intent IDs provide stable campaign boundaries so earlier scale-in
        or principal-recovery legs are not lost when those pointers advance.
        """
        run = self.connection.execute(
            "SELECT id, market_id, sell_intent_id, status FROM roundtrip_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            return []
        previous = self.connection.execute(
            """
            SELECT sell_intent_id
            FROM roundtrip_runs
            WHERE market_id = ? AND id < ? AND sell_intent_id IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (run["market_id"], run_id),
        ).fetchone()
        following = self.connection.execute(
            """
            SELECT buy_intent_id
            FROM roundtrip_runs
            WHERE market_id = ? AND id > ? AND buy_intent_id IS NOT NULL
            ORDER BY id ASC LIMIT 1
            """,
            (run["market_id"], run_id),
        ).fetchone()
        lower_id = int(previous["sell_intent_id"]) if previous is not None else 0
        upper_id = int(following["buy_intent_id"]) if following is not None else None
        if (
            upper_id is None
            and str(run["status"]).lower() in self._TERMINAL_ROUNDTRIP_STATUSES
            and run["sell_intent_id"] is not None
        ):
            # A later unbound BUY must not leak back into a completed campaign.
            upper_id = int(run["sell_intent_id"]) + 1
        params: list[Any] = [run["market_id"], f"{side_prefix.lower()}%", lower_id]
        upper_clause = ""
        if upper_id is not None:
            upper_clause = "AND id < ?"
            params.append(upper_id)
        rows = self.connection.execute(
            f"""
            SELECT id
            FROM order_intents
            WHERE market_id = ? AND dry_run = 0
              AND LOWER(side) LIKE ? AND id > ?
              {upper_clause}
            ORDER BY id ASC
            """,  # noqa: S608 - upper_clause is an internal constant only.
            params,
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def get_order_ids_for_intents(self, intent_ids: list[int]) -> list[str]:
        order_ids: list[str] = []
        seen: set[str] = set()
        for intent_id in intent_ids:
            for order_id in self.get_order_ids_for_intent(intent_id):
                if order_id not in seen:
                    seen.add(order_id)
                    order_ids.append(order_id)
        return order_ids

    def order_token_ids_for_market(self, market_id: str) -> dict[str, str]:
        """Map exchange order_id → token_id for intents on this market.

        Used by inventory campaign matching so maker/taker legs without top-level
        outcome still link via intent token.
        """
        intents = self.connection.execute(
            """
            SELECT id, token_id FROM order_intents
            WHERE market_id = ? AND token_id IS NOT NULL AND token_id != ''
            """,
            (market_id,),
        ).fetchall()
        mapping: dict[str, str] = {}
        for intent in intents:
            token_id = str(intent["token_id"])
            for order_id in self.get_order_ids_for_intent(int(intent["id"])):
                mapping[str(order_id)] = token_id
        return mapping


def _snapshot_value(snapshot: Any, key: str) -> Any:
    if isinstance(snapshot, Row):
        return snapshot[key]
    return getattr(snapshot, key)


def _snapshot_fetched_at(snapshot: Any) -> str:
    value = _snapshot_value(snapshot, "fetched_at")
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _string_field(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return str(value)
    return None


def _account_fill_view(fill: dict[str, Any], known_order_ids: set[str]) -> dict[str, Any]:
    """Return the trade leg that belongs to an order submitted by this application."""
    taker_order_id = _string_field(fill, "taker_order_id", "order_id", "orderId", "orderID")
    if taker_order_id in known_order_ids:
        return fill
    maker_orders = fill.get("maker_orders")
    if not isinstance(maker_orders, list):
        return fill
    for maker_order in maker_orders:
        if not isinstance(maker_order, dict):
            continue
        order_id = _string_field(maker_order, "order_id", "orderId", "orderID", "id")
        if order_id not in known_order_ids:
            continue
        return {
            **fill,
            **maker_order,
            "order_id": order_id,
            "size": _string_field(maker_order, "matched_amount", "size", "quantity"),
        }
    return fill


def _exchange_trade_is_explicitly_invalid(fill: dict[str, Any]) -> bool:
    status = str(fill.get("status") or fill.get("state") or "").strip().upper()
    return status in {"FAILED", "REJECTED", "CANCELLED", "CANCELED"}


def _exchange_trade_is_pending(fill: dict[str, Any]) -> bool:
    status = str(fill.get("status") or fill.get("state") or "").strip().upper()
    status = status.removeprefix("TRADE_STATUS_")
    return status in {"MATCHED_NOT_BROADCASTED", "RETRYING"}


def _float_field(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _notional(payload: dict[str, Any], price: float | None, size: float | None) -> float | None:
    value = _float_field(payload, "notional", "amount", "value")
    if value is not None:
        return value
    if price is None or size is None:
        return None
    return abs(price * size)


def _exchange_order_created_at_iso(order: dict[str, Any]) -> str | None:
    """Best-effort exchange creation timestamp for durable stale-order age."""
    for key in ("created_at", "createdAt", "timestamp", "created_time", "createdTime"):
        value = order.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            # Polymarket may use unix seconds or milliseconds.
            ts = float(value)
            if ts > 1e12:
                ts = ts / 1000.0
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                continue
        text = str(value).strip()
        try:
            if text.isdigit() or (text.replace(".", "", 1).isdigit() and text.count(".") <= 1):
                ts = float(text)
                if ts > 1e12:
                    ts = ts / 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            continue
    return None


def _earliest_iso(*values: str | None) -> str | None:
    best: datetime | None = None
    best_raw: str | None = None
    for value in values:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if best is None or parsed < best:
            best = parsed
            best_raw = parsed.astimezone(timezone.utc).isoformat()
    return best_raw


def _json_loads(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _gamma_event_market_ids(
    payload: Any,
    *,
    event_slug: str,
) -> frozenset[str] | None:
    if not isinstance(payload, dict):
        return None
    events = payload.get("events")
    if not isinstance(events, list):
        return None
    matching = [
        event
        for event in events
        if isinstance(event, dict)
        and (not event_slug or str(event.get("slug") or "").strip() == event_slug)
    ]
    if not matching and len(events) == 1 and isinstance(events[0], dict):
        matching = [events[0]]
    if len(matching) != 1:
        return None
    markets = matching[0].get("markets")
    if not isinstance(markets, list):
        return None
    market_ids = frozenset(
        str(market.get("id")).strip()
        for market in markets
        if isinstance(market, dict) and market.get("id") not in {None, ""}
    )
    return market_ids or None


def _weather_opportunity_sort_key(row: Row) -> tuple[bool, float, str]:
    edge = float(row["edge"] or 0)
    return row["edge"] is None, -edge, str(row["market_id"])


def is_predictive_model_version(model_version: object) -> bool:
    value = str(model_version or "").strip().lower()
    if not value:
        return False
    operational_markers = (
        "unavailable",
        "settlement-route",
        "d0-guard",
        "-entry-gated",
        "-switch",
    )
    return not any(marker in value for marker in operational_markers)


def _decimal_or_none(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _numeric_values_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (ArithmeticError, ValueError):
        return False


def _float_or_none(value: Decimal | float | str | None) -> float | None:
    return None if value is None else float(value)
