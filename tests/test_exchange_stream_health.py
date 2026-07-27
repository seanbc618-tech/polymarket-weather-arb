"""Exchange stream health fields on autopilot_state and /app/stream payload."""

from __future__ import annotations

import json
from decimal import Decimal

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.dashboard_ui.stream_panel import build_app_stream_payload
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _repo(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "stream-health.db",
        MAX_ORDER_USDC=Decimal("1"),
        MAX_DAILY_USDC=Decimal("5"),
        MAX_MARKET_USDC=Decimal("2"),
    )
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    return settings, Repository(connection), connection


def test_schema_has_exchange_stream_columns(tmp_path):
    _settings, repository, connection = _repo(tmp_path)
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(autopilot_state)")
    }
    assert "exchange_stream_status" in columns
    assert "exchange_stream_updated_at" in columns
    assert "exchange_stream_detail" in columns
    connection.close()


def test_persist_stream_health_disabled_without_bridge(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    service = AutopilotService(settings, repository, client=None)
    service._persist_stream_health(None)
    state = repository.get_autopilot_state()
    assert state is not None
    assert state["exchange_stream_status"] == "disabled"
    detail = json.loads(state["exchange_stream_detail"] or "{}")
    assert detail.get("local_transport") == "sqlite"
    assert detail.get("rest_fallback_active") is True
    blob = json.dumps(detail).lower()
    assert "private" not in blob
    assert "secret" not in blob
    connection.close()


def test_stream_payload_separates_local_and_exchange(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.update_autopilot_state(
        enabled=True,
        exchange_stream_status="live",
        exchange_stream_updated_at="2026-07-17T00:00:00+00:00",
        exchange_stream_detail=json.dumps(
            {
                "subscribed_token_count": 4,
                "rest_fallback_active": False,
                "coalesced": 12,
                "private_key": "MUST_NOT_APPEAR",
            }
        ),
    )
    payload = build_app_stream_payload(repository, after_decision_id=0, after_fill_id=0)
    assert payload["source"] == "local_sqlite"
    assert payload["exchange_feed"] is False
    health = payload["health"]
    assert health["local_transport"] == "sqlite"
    assert health["exchange_stream_status"] == "live"
    assert health["exchange_stream"]["subscribed_token_count"] == 4
    assert health["exchange_stream"]["rest_fallback_active"] is False
    assert "MUST_NOT_APPEAR" not in json.dumps(payload)
    assert "private_key" not in json.dumps(health["exchange_stream"])
    connection.close()
