from datetime import datetime, timezone
from types import SimpleNamespace

from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository
from polymarket_weather_arb.storage.repository_automation import AutomationRepository


def test_repository_exposes_automation_subrepository(tmp_path):
    database = Database(tmp_path / "repo-structure.db")
    database.init_schema()
    connection = database.connect()
    try:
        repo = Repository(connection)
        assert isinstance(repo.automation, AutomationRepository)
        repo.upsert_market(
            Market(
                id="m1",
                slug="m1",
                title="Structure test market",
                description="NOAA station KNYC",
                yes_token_id="yes-token",
                no_token_id="no-token",
                status="active",
                is_weather=True,
            ),
            {"id": "m1"},
        )

        action = repo.create_automation_action(
            SimpleNamespace(
                id="act_repo_structure_1",
                kind="dry_run",
                market_id="m1",
                reason="structure test",
                command_preview="trade --dry-run",
                idempotency_key=None,
                requested_by="test",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc).replace(year=2099),
            )
        )

        assert repo.automation.get_automation_action(action["id"])["id"] == action["id"]
        assert repo.get_automation_action(action["id"])["id"] == action["id"]
    finally:
        connection.close()
