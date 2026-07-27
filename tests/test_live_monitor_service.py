from datetime import datetime, timezone
from types import SimpleNamespace

from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.profiles import get_profile
from polymarket_weather_arb.services.live_monitor_service import build_live_monitor_snapshot
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_live_monitor_explains_missing_override_and_whitelist(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        repo.upsert_market(_market(), {"id": "m1"})
        repo.create_automation_action(_action("act_live_1", "m1"))

        snapshot = build_live_monitor_snapshot(
            repo,
            profile=get_profile("micro-live"),
            allow_live_auto=True,
            live_market_ids={"other"},
            require_fresh_reconciliation=False,
            block_live_on_positions=True,
        )

        action = snapshot.pending_live_actions[0]
        assert action.can_auto_execute is False
        assert any(gate.name == "whitelist" and not gate.ok for gate in action.gates)
        assert any(gate.name == "override" and not gate.ok for gate in action.gates)
        assert "market is not whitelisted" in snapshot.blockers
    finally:
        connection.close()


def test_live_monitor_reports_ready_when_all_gates_pass(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        repo.upsert_market(_market(), {"id": "m1"})
        repo.create_automation_action(_action("act_live_2", "m1"))
        repo.save_reconciliation("ok", {"test": True})
        repo.upsert_strategy_override(market_id="m1", profile="micro-live", live_auto_enabled=True)

        snapshot = build_live_monitor_snapshot(
            repo,
            profile=get_profile("micro-live"),
            allow_live_auto=True,
            live_market_ids={"m1"},
            require_fresh_reconciliation=True,
            block_live_on_positions=True,
        )

        assert snapshot.risk_status == "ok"
        assert snapshot.reconciliation_fresh is True
        assert snapshot.pending_live_actions[0].can_auto_execute is True
        assert snapshot.blockers == []
    finally:
        connection.close()


def _repo(tmp_path):
    database = Database(tmp_path / "live-monitor.db")
    database.init_schema()
    connection = database.connect()
    return database, connection, Repository(connection)


def _market() -> Market:
    return Market(
        id="m1",
        slug="m1",
        title="Will the high temperature in New York exceed 80°F on May 8, 2026?",
        description="NOAA station KNYC",
        yes_token_id="yes-token",
        no_token_id="no-token",
        status="active",
        is_weather=True,
    )


def _action(action_id: str, market_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=action_id,
        kind="trade_live",
        market_id=market_id,
        reason="manual live review",
        command_preview=f"trade --market {market_id}",
        idempotency_key=None,
        requested_by="test",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc).replace(year=2099),
    )
