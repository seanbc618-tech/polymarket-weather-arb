from __future__ import annotations

import json

from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_runtime_history_retention_preserves_latest_and_audit_sources(tmp_path):
    database = Database(tmp_path / "retention.db")
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    try:
        connection.execute(
            "INSERT INTO markets (id, title, raw_payload) VALUES ('m1', 'Weather', '{}')"
        )
        for fetched_at, value in (
            ("2000-01-01T00:00:00+00:00", 1),
            ("2000-01-02T00:00:00+00:00", 2),
            ("2999-01-01T00:00:00+00:00", 3),
        ):
            connection.execute(
                """
                INSERT INTO weather_forecasts (
                    market_id, provider, issue_time, valid_time, variable, value,
                    unit, raw_payload, fetched_at
                ) VALUES ('m1', 'source', ?, '2026-07-20', 'temperature_high', ?,
                          'C', '{}', ?)
                """,
                (fetched_at, value, fetched_at),
            )
            connection.execute(
                """
                INSERT INTO weather_observations (
                    market_id, provider, station, observed_at, variable, value,
                    unit, raw_payload, fetched_at
                ) VALUES ('m1', 'source', 'STN', ?, 'temperature_high', ?, 'C', '{}', ?)
                """,
                (fetched_at, value, fetched_at),
            )

        old_analysis = connection.execute(
            """
            INSERT INTO analyses (
                market_id, model_version, fair_lower, fair_upper, edge,
                decision, reasons, created_at
            ) VALUES ('m1', 'old-free', 0, 0, 0, 'watch', '[]', '2000-01-01')
            """
        ).lastrowid
        referenced_analysis = connection.execute(
            """
            INSERT INTO analyses (
                market_id, model_version, fair_lower, fair_upper, edge,
                decision, reasons, created_at
            ) VALUES ('m1', 'referenced', 0, 0, 0, 'watch', '[]', '2000-01-01')
            """
        ).lastrowid
        connection.execute(
            """
            INSERT INTO model_signals (
                analysis_id, market_id, model_version, yes_probability,
                fair_lower, fair_upper, edge, decision, raw_payload, created_at
            ) VALUES (?, 'm1', 'referenced', 0.5, 0.5, 0.5, 0, 'watch', '{}', '2000-01-01')
            """,
            (referenced_analysis,),
        )
        connection.execute(
            """
            INSERT INTO analyses (
                market_id, model_version, fair_lower, fair_upper, edge,
                decision, reasons, created_at
            ) VALUES ('m1', 'old-free', 0, 0, 0, 'watch', '[]', '2999-01-01')
            """
        )

        large_payload = json.dumps(
            {"id": "m1", "closed": False, "description": "x" * 10_000}
        )
        for created_at in ("2000-01-01", "2000-01-02"):
            connection.execute(
                """
                INSERT INTO resolution_audits (
                    market_id, polymarket_closed, polymarket_uma_status, match,
                    status, polymarket_source, raw_polymarket_payload, trip_breaker,
                    created_at
                ) VALUES ('m1', 0, 'unresolved', 1, 'unavailable', 'gamma_api', ?, 0, ?)
                """,
                (large_payload, created_at),
            )
        connection.execute(
            """
            INSERT INTO resolution_audits (
                market_id, polymarket_closed, polymarket_uma_status, match,
                status, polymarket_source, raw_polymarket_payload, trip_breaker,
                created_at
            ) VALUES ('m1', 1, 'resolved', 1, 'match', 'gamma_api', '{}', 0, '2000-01-03')
            """
        )

        assert repository.prune_old_weather_forecasts(keep_days=14) == 2
        assert repository.prune_old_weather_observations(keep_days=14) == 2
        assert repository.prune_unreferenced_analyses(keep_days=14) == 1
        assert repository.prune_superseded_unavailable_resolution_audits() == 1
        assert repository.compact_resolution_audit_payloads() == 1

        assert connection.execute("SELECT COUNT(*) FROM weather_forecasts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM weather_observations").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM analyses WHERE id = ?", (old_analysis,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM analyses WHERE id = ?", (referenced_analysis,)
        ).fetchone()[0] == 1
        audits = connection.execute(
            "SELECT status, raw_polymarket_payload FROM resolution_audits ORDER BY id"
        ).fetchall()
        assert [row["status"] for row in audits] == ["unavailable", "match"]
        compact = json.loads(audits[0]["raw_polymarket_payload"])
        assert len(compact["payload_sha256"]) == 64
        assert "description" not in compact
    finally:
        connection.close()
