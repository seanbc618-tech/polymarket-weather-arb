from __future__ import annotations

from dataclasses import dataclass

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


@dataclass
class FakeRedeemClient:
    market: Market
    payload: dict
    readiness_ok: bool = True
    ambiguous_after_submit: bool = False

    def __post_init__(self) -> None:
        self.redeem_calls: list[str] = []
        self.market_reads: list[str] = []

    def validate_redemption_signing(self):
        if self.readiness_ok:
            return {
                "ok": True,
                "status": "gasless-builder-ready",
                "wallet_type": "DEPOSIT_WALLET",
            }
        return {
            "ok": False,
            "status": "builder-credentials-not-ready",
            "detail": "complete Builder credential triple is missing",
        }

    def get_market(self, market_id):
        self.market_reads.append(market_id)
        return self.market, dict(self.payload)

    def redeem_positions(self, *, condition_id, on_submitted=None):
        self.redeem_calls.append(condition_id)
        if on_submitted is not None:
            on_submitted(
                {
                    "condition_id": condition_id,
                    "transaction_id": "relay-1",
                    "transaction_hash": "0xsubmitted",
                }
            )
        if self.ambiguous_after_submit:
            raise RuntimeError("relayer confirmation timed out")
        return {
            "condition_id": condition_id,
            "transaction_id": "relay-1",
            "transaction_hash": "0xconfirmed",
            "status": "confirmed",
        }


def _service(tmp_path, *, readiness_ok=True, ambiguous_after_submit=False):
    database = Database(tmp_path / "auto-redeem.db")
    database.init_schema()
    connection = database.connect()
    repository = Repository(connection)
    market = Market(
        id="m-redeem",
        title="Will the high temperature be 30C?",
        slug="m-redeem",
        yes_token_id="yes-token",
        no_token_id="no-token",
        is_weather=True,
        status="closed",
    )
    payload = {
        "id": market.id,
        "conditionId": "0xcondition",
        "closed": True,
        "acceptingOrders": False,
        "umaResolutionStatus": "resolved",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["1", "0"],
        "clobTokenIds": ["yes-token", "no-token"],
    }
    repository.upsert_market(market, payload)
    repository.replace_positions(
        [
            {
                "market": market.id,
                "outcome": "Yes",
                "size": "8",
                "avgPrice": "0.20",
            }
        ]
    )
    repository.ensure_autopilot_state(mode="live", app_mode="full_live")
    repository.connection.commit()
    client = FakeRedeemClient(
        market,
        payload,
        readiness_ok=readiness_ok,
        ambiguous_after_submit=ambiguous_after_submit,
    )
    settings = Settings(
        _env_file=None,
        DATABASE_PATH=tmp_path / "auto-redeem.db",
        TRADING_DISABLED=False,
        POLYMARKET_PRIVATE_KEY="test-key",
        POLYMARKET_FUNDER="0xfunder",
    )
    return (
        AutopilotService(settings, repository, client=client),
        repository,
        connection,
        client,
    )


def test_full_live_auto_redeem_confirms_once_and_blocks_replay(tmp_path):
    service, repository, connection, client = _service(tmp_path)

    first = service._maybe_auto_redeem(app_mode="full_live")
    second = service._maybe_auto_redeem(app_mode="full_live")

    assert first == (1, 1)
    assert second == (0, 0)
    assert client.market_reads == ["m-redeem"]
    assert client.redeem_calls == ["0xcondition"]
    decision = repository.latest_autopilot_decision_for_action(
        market_id="m-redeem",
        action="auto_redeem",
    )
    assert decision["status"] == "redeemed"
    assert "0xconfirmed" in decision["reason"]
    connection.close()


def test_auto_redeem_missing_builder_credentials_fails_closed(tmp_path):
    service, repository, connection, client = _service(
        tmp_path,
        readiness_ok=False,
    )

    result = service._maybe_auto_redeem(app_mode="full_live")

    assert result == (0, 0)
    assert client.market_reads == []
    assert client.redeem_calls == []
    decision = repository.latest_autopilot_decision_for_action(
        market_id="m-redeem",
        action="auto_redeem",
    )
    assert decision["status"] == "blocked"
    assert "Builder credential triple is missing" in decision["reason"]
    connection.close()


def test_auto_redeem_ambiguous_submission_is_never_replayed(tmp_path):
    service, repository, connection, client = _service(
        tmp_path,
        ambiguous_after_submit=True,
    )

    first = service._maybe_auto_redeem(app_mode="full_live")
    second = service._maybe_auto_redeem(app_mode="full_live")

    assert first == (0, 1)
    assert second == (0, 0)
    assert client.redeem_calls == ["0xcondition"]
    decision = repository.latest_autopilot_decision_for_action(
        market_id="m-redeem",
        action="auto_redeem",
    )
    assert decision["status"] == "submitted_unverified"
    assert "automatic replay blocked" in decision["reason"]
    assert "relay-1" in decision["reason"]
    connection.close()


def test_auto_redeem_requires_fresh_polymarket_winner_match(tmp_path):
    service, repository, connection, client = _service(tmp_path)
    client.payload["outcomePrices"] = ["0", "1"]

    result = service._maybe_auto_redeem(app_mode="full_live")

    assert result == (0, 0)
    assert client.market_reads == ["m-redeem"]
    assert client.redeem_calls == []
    decision = repository.latest_autopilot_decision_for_action(
        market_id="m-redeem",
        action="auto_redeem",
    )
    assert decision["status"] == "blocked"
    assert "does not prove the held outcome wins" in decision["reason"]
    connection.close()


def test_auto_redeem_is_full_live_only(tmp_path):
    service, repository, connection, client = _service(tmp_path)

    assert service._maybe_auto_redeem(app_mode="micro_live") == (0, 0)
    assert client.market_reads == []
    assert client.redeem_calls == []
    assert (
        repository.latest_autopilot_decision_for_action(
            market_id="m-redeem",
            action="auto_redeem",
        )
        is None
    )
    connection.close()
