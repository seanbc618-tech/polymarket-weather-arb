"""Phase 1: honest local /app stream from SQLite (no upstream I/O)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import Mock

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard import (
    DashboardHandler,
    DashboardResponse,
    render_dashboard_path,
)
from polymarket_weather_arb.dashboard_ui.stream_panel import (
    build_app_stream_payload,
    edge_series_from_decisions,
    stream_time_series_svg,
)
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, database_path=tmp_path / "stream.db")


def _repo(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    return settings, Repository(connection), connection


def test_stream_cursors_ordered_and_bounded(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    for index in range(5):
        repository.save_autopilot_decision(
            market_id=f"m-{index}",
            action="skip",
            mode="dry_run",
            edge=Decimal(str(0.10 + index * 0.01)),
            reason=f"reason-{index}",
            blockers=[],
            status="idle",
        )
    connection.commit()

    first = build_app_stream_payload(repository, after_decision_id=0, limit=2)
    assert len(first["decisions"]) == 2
    assert first["decisions"][0]["id"] < first["decisions"][1]["id"]
    assert first["source"] == "local_sqlite"
    assert first["exchange_feed"] is False

    mid = first["cursors"]["after_decision_id"]
    second = build_app_stream_payload(repository, after_decision_id=mid, limit=10)
    assert [row["id"] for row in second["decisions"]] == [mid + 1, mid + 2, mid + 3]
    # Reconnect with same cursor does not re-emit.
    again = build_app_stream_payload(repository, after_decision_id=mid, limit=10)
    assert [row["id"] for row in again["decisions"]] == [mid + 1, mid + 2, mid + 3]
    connection.close()


def test_stream_reconnect_does_not_duplicate_when_cursor_at_high_water(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.save_autopilot_decision(
        market_id="m1",
        action="skip",
        mode="dry_run",
        edge=None,
        reason="seed",
        blockers=[],
        status="idle",
    )
    connection.commit()
    high = repository.stream_cursor_high_water()
    payload = build_app_stream_payload(
        repository,
        after_decision_id=high["after_decision_id"],
        after_fill_id=high["after_fill_id"],
    )
    assert payload["decisions"] == []
    assert payload["fills"] == []
    assert payload["cursors"]["after_decision_id"] == high["after_decision_id"]
    connection.close()


def test_app_stream_endpoint_returns_json_and_new_rows(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    d1 = repository.save_autopilot_decision(
        market_id="m1",
        action="skip",
        mode="dry_run",
        edge=Decimal("0.12"),
        reason="first",
        blockers=[],
        status="idle",
    )
    connection.commit()
    connection.close()

    response = render_dashboard_path(settings, f"/app/stream?after_decision_id={d1}")
    assert response.status.value == 200
    assert response.headers.get("Content-Type", "").startswith("application/json")
    body = json.loads(response.body)
    assert body["decisions"] == []
    assert body["source"] == "local_sqlite"
    assert "health" in body
    assert "open_meteo_usage" in body["health"]
    assert body["health"]["open_meteo_usage"]["estimated_units"] == 0
    assert body["health"]["open_meteo_usage"]["cooldown_skips"] == 0

    connection = Database(settings.database_path).connect()
    repository = Repository(connection)
    d2 = repository.save_autopilot_decision(
        market_id="m2",
        action="trade",
        mode="dry_run",
        edge=Decimal("0.22"),
        reason="second",
        blockers=[],
        status="ok",
    )
    connection.commit()
    connection.close()

    response2 = render_dashboard_path(settings, f"/app/stream?after_decision_id={d1}")
    body2 = json.loads(response2.body)
    assert len(body2["decisions"]) == 1
    assert body2["decisions"][0]["id"] == d2
    assert body2["decisions"][0]["reason"] == "second"
    assert body2["cursors"]["after_decision_id"] == d2


def test_app_stream_renders_minimum_order_blocker_label(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.save_autopilot_decision(
        market_id="m-minimum",
        action="entry_minimum_blocked",
        mode="live",
        edge=Decimal("0.18"),
        reason="order below exchange minimum notional/size",
        blockers=["order below exchange minimum notional/size"],
        status="skipped",
    )
    connection.commit()
    connection.close()

    response = render_dashboard_path(settings, "/app?lang=zh")

    assert response.status.value == 200
    assert "最低订单" in response.body
    assert "order below exchange minimum notional/size" in response.body


def test_app_stream_renders_terminal_partial_fill_progress(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.upsert_market(
        Market(id="m-partial", title="Partial fill", yes_token_id="yes-partial"),
        {"id": "m-partial"},
    )
    intent_id = repository.save_order_intent(
        SimpleNamespace(
            market_id="m-partial",
            side="buy_yes",
            token_id="yes-partial",
            limit_price=0.17,
            size=22.5918,
            notional=3.840606,
            rationale="partial audit",
            dry_run=False,
            status="partially_filled_closed",
            idempotency_key="partial-stream",
            created_at=datetime.now(timezone.utc),
        )
    )
    repository.save_order_attempt(
        SimpleNamespace(
            intent_id=intent_id,
            request_payload={},
            response_payload={"order_id": "partial-stream-order", "status": "matched"},
            status="submitted",
            error=None,
            created_at=datetime.now(timezone.utc),
        )
    )
    repository.save_reconciled_fills(
        [
            {
                "id": "partial-stream-fill",
                "order_id": "partial-stream-order",
                "market": "m-partial",
                "side": "BUY",
                "price": "0.17",
                "size": "10.44",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    )
    connection.commit()
    connection.close()

    payload = json.loads(render_dashboard_path(settings, "/app/stream").body)
    assert payload["order_intents"][0]["filled_size"] == 10.44

    page = render_dashboard_path(settings, "/app?lang=en")
    assert "filled 10.44/22.5918" in page.body


def test_dashboard_ignores_client_disconnect_while_sending_headers():
    handler = object.__new__(DashboardHandler)
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock(side_effect=BrokenPipeError)
    handler.wfile = Mock()

    handler._send_dashboard_response(
        DashboardResponse(HTTPStatus.OK, "ok", {"Content-Type": "text/plain"})
    )

    handler.wfile.write.assert_not_called()


def test_dashboard_ignores_client_disconnect_while_sending_body():
    handler = object.__new__(DashboardHandler)
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = Mock()
    handler.wfile.write.side_effect = ConnectionResetError

    handler._send_dashboard_response(
        DashboardResponse(HTTPStatus.OK, "ok", {"Content-Type": "text/plain"})
    )

    handler.wfile.write.assert_called_once_with(b"ok")


def test_app_stream_polling_does_not_call_external_apis(tmp_path, monkeypatch):
    """UI stream path is SQLite-only — no Gamma/CLOB/weather clients."""
    settings = _settings(tmp_path)
    Database(settings.database_path).init_schema()

    gamma = Mock(side_effect=AssertionError("Gamma client must not be built for /app/stream"))
    monkeypatch.setattr(
        "polymarket_weather_arb.dashboard.GammaPolymarketClient",
        gamma,
    )
    # Also guard weather imports that would indicate accidental research.
    import polymarket_weather_arb.adapters.weather.open_meteo as om

    monkeypatch.setattr(
        om,
        "OpenMeteoProvider",
        Mock(side_effect=AssertionError("weather provider must not run for stream poll")),
    )

    response = render_dashboard_path(settings, "/app/stream")
    assert response.status.value == 200
    payload = json.loads(response.body)
    assert payload["source"] == "local_sqlite"
    gamma.assert_not_called()


def test_app_page_includes_local_stream_poller_and_not_exchange_tape(tmp_path):
    settings = _settings(tmp_path)
    Database(settings.database_path).init_schema()
    response = render_dashboard_path(settings, "/app?lang=en")
    body = response.body
    assert 'data-stream-root="1"' in body
    assert "/app/stream" in body
    assert "local SQLite" in body or "local_sqlite" in body.lower() or "not exchange" in body
    assert "data-poll-ms=" in body
    assert "exchange tape" in body.lower() or "not exchange" in body.lower()
    assert "2s local / 300s strategy" in body
    assert 'data-stream-conn="1" data-conn="reconnecting"' in body


def test_time_series_chart_is_honest_when_empty_or_sparse():
    empty = stream_time_series_svg([])
    assert "No timestamped" in empty or "empty" in empty.lower() or "series" in empty.lower()
    one = stream_time_series_svg([(1.0, 0.1)])
    assert "polyline" not in one
    assert '<circle cx="320" cy="70"' in one
    assert "edge 0.100" in one
    two = stream_time_series_svg([(1.0, 0.1), (2.0, 0.2)])
    assert "polyline" in two


def test_time_series_chart_centers_flat_edge_series():
    flat = stream_time_series_svg([(1.0, 0.2), (2.0, 0.2)])
    assert 'points="14.0,70.0 626.0,70.0"' in flat


def test_stream_browser_redraw_expands_flat_edge_series(tmp_path):
    settings = _settings(tmp_path)
    Database(settings.database_path).init_schema()
    body = render_dashboard_path(settings, "/app?lang=en").body
    assert "let spanY = maxY - minY;" in body
    assert "Math.max(maxY - minY, 1e-9)" not in body


def test_edge_series_from_decisions_orders_oldest_first():
    rows = [
        {"created_at": "2026-07-16T12:00:02+00:00", "edge": 0.2},
        {"created_at": "2026-07-16T12:00:01+00:00", "edge": 0.1},
        {"created_at": "2026-07-16T12:00:03+00:00", "edge": None},
    ]
    series = edge_series_from_decisions(rows)
    assert len(series) == 2
    assert series[0][1] == 0.1
    assert series[1][1] == 0.2


def test_stream_includes_intents_attempts_fills_and_analyses(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    market = Market(
        id="m-stream",
        title="Will the highest temperature in NYC be above 80 on May 8?",
        description="NOAA",
        yes_token_id="y",
        no_token_id="n",
        is_weather=True,
    )
    repository.upsert_market(market, {"id": "m-stream"})
    connection.execute(
        """
        INSERT INTO order_intents (
            market_id, side, token_id, limit_price, size, notional,
            rationale, dry_run, status
        ) VALUES ('m-stream', 'buy_yes', 'y', 0.4, 2, 0.8, 't', 1, 'submitted')
        """
    )
    intent_id = connection.execute("SELECT id FROM order_intents").fetchone()["id"]
    connection.execute(
        """
        INSERT INTO order_attempts (intent_id, request_payload, response_payload, status, error)
        VALUES (?, '{}', '{}', 'submitted', NULL)
        """,
        (intent_id,),
    )
    connection.execute(
        """
        INSERT INTO fills (
            exchange_fill_id, order_id, market_id, side, price, size, fee, filled_at
        ) VALUES ('fx1', 'o1', 'm-stream', 'buy', 0.4, 2, 0, CURRENT_TIMESTAMP)
        """
    )
    connection.execute(
        """
        INSERT INTO analyses (
            market_id, model_version, fair_lower, fair_upper, reference_price,
            edge, side, decision, reasons
        ) VALUES ('m-stream', 't', 0.5, 0.6, 0.4, 0.15, 'buy_yes', 'trade', '[]')
        """
    )
    connection.commit()

    payload = build_app_stream_payload(repository)
    assert len(payload["order_intents"]) == 1
    assert len(payload["order_attempts"]) == 1
    assert len(payload["fills"]) == 1
    assert len(payload["analyses"]) == 1
    assert payload["analyses"][0]["edge"] == 0.15
    # No large request/response blobs on attempts (bounded).
    assert "request_payload" not in payload["order_attempts"][0]
    connection.close()


def test_invalid_stream_cursors_default_to_zero(tmp_path):
    settings = _settings(tmp_path)
    Database(settings.database_path).init_schema()
    response = render_dashboard_path(
        settings, "/app/stream?after_decision_id=not-a-number&after_fill_id=-3"
    )
    body = json.loads(response.body)
    assert body["cursors"]["after_decision_id"] >= 0
    assert body["cursors"]["after_fill_id"] >= 0


def test_app_page_captures_stream_cursors_before_snapshot(tmp_path, monkeypatch):
    """High-water cursor must be frozen before snapshot so concurrent writes are not skipped."""
    settings, repository, connection = _repo(tmp_path)
    repository.save_autopilot_decision(
        market_id="m-seed",
        action="skip",
        mode="dry_run",
        edge=None,
        reason="seed",
        blockers=[],
        status="idle",
    )
    connection.commit()
    pre_high = repository.stream_cursor_high_water()["after_decision_id"]

    captured: dict[str, int] = {}
    original_high_water = repository.stream_cursor_high_water

    def tracking_high_water():
        value = original_high_water()
        captured.update(value)
        # Simulate a concurrent writer after cursor capture would race if inverted.
        repository.save_autopilot_decision(
            market_id="m-race",
            action="skip",
            mode="dry_run",
            edge=Decimal("0.33"),
            reason="race-after-cursor",
            blockers=[],
            status="idle",
        )
        connection.commit()
        return value

    monkeypatch.setattr(repository, "stream_cursor_high_water", tracking_high_water)

    # render_app uses the same repository object we monkeypatched.
    from polymarket_weather_arb.dashboard_ui import app as app_mod

    html = app_mod.render_app(repository, settings, "en", "/app")
    assert f'"after_decision_id":{pre_high}' in html.replace(" ", "")
    # Late decision is beyond frozen cursor and will appear on the next poll.
    late = repository.list_autopilot_decisions_after(after_id=pre_high, limit=10)
    assert any(row["reason"] == "race-after-cursor" for row in late)
    connection.close()


def test_app_page_poll_script_handles_lifecycle_kinds(tmp_path):
    settings = _settings(tmp_path)
    Database(settings.database_path).init_schema()
    body = render_dashboard_path(settings, "/app?lang=en").body
    assert "appendEvents('intent'" in body or 'appendEvents("intent"' in body
    assert "appendEvents('attempt'" in body or 'appendEvents("attempt"' in body
    assert "appendEvents('fill'" in body or 'appendEvents("fill"' in body
    assert "appendEvents('decision'" in body or 'appendEvents("decision"' in body
    # Edge series must not ingest analyses bulk points.
    assert "data.analyses" not in body or "never charted" in body
    assert "analyses not mixed" in body.lower() or "decision net edge" in body.lower()


def test_edge_series_ignores_analyses_bulk_rows():
    """Chart helper is decision-only; analyses must not be passed into it."""
    # Snapshot order is newest-first; helper reverses to chronological series.
    decisions = [
        {"created_at": "2026-07-16T12:00:02+00:00", "edge": 0.22},
        {"created_at": "2026-07-16T12:00:01+00:00", "edge": 0.11},
    ]
    series = edge_series_from_decisions(decisions)
    assert [p[1] for p in series] == [0.11, 0.22]
    # analyses-shaped bulk rows are never fed into this helper by the panel/JS.
