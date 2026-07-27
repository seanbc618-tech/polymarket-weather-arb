"""Guarded auto-exit policy tests (offline only; no real network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION
from polymarket_weather_arb.services.auto_exit_service import AutoExitService
from polymarket_weather_arb.services.compliance_service import ComplianceDecision, ComplianceService
from polymarket_weather_arb.services.exit_guardian_service import ExitRecommendation
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        DATABASE_PATH=tmp_path / "auto-exit.db",
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
        AUTO_EXIT_ENABLED=False,
        MAX_AUTO_EXITS_PER_TICK=1,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("1"),
        AUTO_EXIT_MAX_SLIPPAGE=Decimal("0.02"),
        STALE_ORDER_BOOK_SECONDS=300,
        MIN_EDGE=Decimal("0.05"),
    )
    base.update(overrides)
    return Settings(**base)


def _seed(repo: Repository, market_id: str = "m-exit") -> None:
    repo.upsert_market(
        Market(
            id=market_id,
            title="Auto exit market",
            is_weather=True,
            slug=market_id,
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
        ),
        {
            "id": market_id,
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token", "no-token"],
        },
    )
    repo.save_reconciliation("ok", {"status": "ok"})
    repo.replace_positions(
        [
            {
                "market": market_id,
                "outcome": "Yes",
                "size": "5",
                "avgPrice": "0.13",
            }
        ]
    )
    _seed_value_confirmations(repo, market_id)


def _seed_value_confirmations(repo: Repository, market_id: str) -> None:
    now = datetime.now(timezone.utc)
    for index, revision in enumerate((f"{market_id}-r1", f"{market_id}-r2")):
        analysis_id = repo.save_analysis(
            Analysis(
                market_id=market_id,
                model_version=GLOBAL_BUCKET_MODEL_VERSION,
                fair_lower=Decimal("0.00"),
                fair_upper=Decimal("0.05"),
                reference_price=Decimal("0.12"),
                edge=Decimal("-0.07"),
                side=None,
                decision="watch",
                reasons=["settlement-core value test"],
                created_at=now + timedelta(microseconds=index),
            )
        )
        repo.connection.execute(
            """
            UPDATE model_signals
            SET raw_payload = json_set(raw_payload, '$.forecast_revision', ?)
            WHERE analysis_id = ?
            """,
            (revision, analysis_id),
        )


def _fresh_book(bid: str = "0.12"):
    return (
        MarketSnapshot(
            market_id="token_book",
            best_bid=Decimal(bid),
            best_ask=Decimal("0.13"),
            midpoint=Decimal("0.125"),
            spread=Decimal("0.01"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {},
    )


def _client():
    client = Mock()
    client.place_limit_order.side_effect = RuntimeError("BUY path forbidden")
    client.place_sell_limit_order.return_value = {
        "order_id": "auto-sell-1",
        "status": "live",
    }
    client.get_token_order_book.side_effect = lambda token_id: _fresh_book()
    client.get_order.return_value = {"id": "auto-sell-1", "status": "LIVE"}
    client.get_balances.return_value = {"balance": 1}
    client.get_orders.return_value = []
    client.get_trades.return_value = []
    client.get_positions.return_value = []
    return client


def _compliance_ok():
    svc = Mock(spec=ComplianceService)
    svc.check_live_allowed.return_value = ComplianceDecision(
        ok=True, status="check_disabled", reason="test"
    )
    return svc


class _ForcedOfficialExitGuardian:
    """Isolate AutoExit execution plumbing from the real V5 policy evaluator."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def evaluate(self, *, best_bids=None, **_kwargs):
        bids = best_bids or {}
        recommendations = []
        for position in self.repository.list_positions(limit=1000, nonzero_only=True):
            market_id = str(position["market_id"])
            outcome = str(position["outcome"]).upper()
            size = Decimal(str(position["size"]))
            recommendations.append(
                ExitRecommendation(
                    kind="position",
                    action="exit_full",
                    market_id=market_id,
                    outcome=outcome,
                    reason="settlement-grade official observation invalidates held outcome",
                    policy_stage="official_observation",
                    recommended_size=size,
                    actual_position_size=size,
                    best_bid=bids.get((market_id, outcome)),
                )
            )
        return recommendations


def _execution_service(repo, client, *, exit_service=None):
    return AutoExitService(
        repo,
        client,
        exit_service=exit_service,
        guardian=_ForcedOfficialExitGuardian(repo),
    )


def test_auto_exit_default_off_zero_sell(tmp_path):
    settings = _settings(tmp_path, AUTO_EXIT_ENABLED=False)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo)
    client = _client()
    service = AutoExitService(repo, client)
    result = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert result.enabled_gates_ok is False
    assert result.executed == 0
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_missing_any_switch_blocks(tmp_path):
    settings = _settings(tmp_path, AUTO_EXIT_ENABLED=True)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo)
    client = _client()
    service = _execution_service(repo, client)

    # no daemon flag
    r1 = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=False,
        compliance_service=_compliance_ok(),
    )
    assert r1.executed == 0
    assert any("allow_auto_exit=false" in n for n in r1.notes)

    # wrong profile
    r2 = service.run_tick(
        settings=settings,
        profile_name="balanced",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert r2.executed == 0
    assert any("not an auto-exit live profile" in n for n in r2.notes)
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_hold_position_does_not_sell(tmp_path):
    settings = _settings(tmp_path, AUTO_EXIT_ENABLED=True)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo)
    # Override analysis to hold
    repo.save_analysis(
        Analysis(
            market_id="m-exit",
            model_version="t",
            fair_lower=Decimal("0.8"),
            fair_upper=Decimal("0.9"),
            reference_price=Decimal("0.12"),
            edge=Decimal("0.20"),
            side="buy_yes",
            decision="trade",
            reasons=["still good"],
        )
    )
    client = _client()
    service = AutoExitService(repo, client)
    result = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert result.executed == 0
    assert any("no executable exit" in n or "no position_at_risk" in n for n in result.notes)
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_oversized_position_blocked(tmp_path):
    settings = _settings(
        tmp_path,
        AUTO_EXIT_ENABLED=True,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("0.10"),
    )
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo)
    # 5 * 0.12 = 0.6 > 0.10
    client = _client()
    service = _execution_service(repo, client)
    result = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert result.executed == 0
    assert result.attempted >= 1 or any("exceeds AUTO_EXIT" in s for s in result.skipped)
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_max_one_per_tick(tmp_path):
    settings = _settings(
        tmp_path,
        AUTO_EXIT_ENABLED=True,
        MAX_AUTO_EXITS_PER_TICK=1,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("5"),
    )
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo, "m1")
    # Second market without replace_positions wiping m1
    repo.upsert_market(
        Market(
            id="m2",
            title="Auto exit market 2",
            is_weather=True,
            slug="m2",
            yes_token_id="yes-token-2",
            no_token_id="no-token-2",
            status="active",
        ),
        {
            "id": "m2",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token-2", "no-token-2"],
        },
    )
    conn.execute(
        """
        INSERT INTO positions (market_id, outcome, size, notional, updated_at)
        VALUES ('m2', 'Yes', 5.0, 0.65, CURRENT_TIMESTAMP)
        """
    )
    _seed_value_confirmations(repo, "m2")
    client = _client()
    service = _execution_service(repo, client)
    result = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert result.executed == 1
    assert client.place_sell_limit_order.call_count == 1
    assert any("max_auto_exits_per_tick" in s for s in result.skipped)
    conn.close()


def test_auto_exit_success_once_records_audit_and_idempotent(tmp_path):
    settings = _settings(
        tmp_path,
        AUTO_EXIT_ENABLED=True,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("5"),
    )
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo)
    client = _client()
    service = _execution_service(repo, client)
    commits: list[int] = []
    result = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
        on_submitted=lambda i: (commits.append(i), conn.commit()),
    )
    conn.commit()
    assert result.executed == 1
    assert result.intent_ids
    assert result.action_ids
    client.place_sell_limit_order.assert_called_once()
    client.place_limit_order.assert_not_called()

    # Second tick: active sell intent blocks duplicate
    result2 = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert result2.executed == 0
    assert client.place_sell_limit_order.call_count == 1

    events = repo.list_automation_audit_events(result.action_ids[0])
    names = [e["event"] for e in events]
    assert "auto_exit_candidate" in names
    assert "auto_exit_submitted" in names
    conn.close()


def test_auto_exit_stale_recon_blocked(tmp_path):
    settings = _settings(
        tmp_path,
        AUTO_EXIT_ENABLED=True,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("5"),
    )
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo)
    # Make recon stale by rewriting timestamp far past
    conn.execute("UPDATE reconciliations SET created_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    client = _client()
    service = _execution_service(repo, client)
    result = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert result.executed == 0
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_quote_change_blocks_before_mutation(tmp_path):
    settings = _settings(
        tmp_path,
        AUTO_EXIT_ENABLED=True,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("5"),
        AUTO_EXIT_MAX_SLIPPAGE=Decimal("0.01"),
    )
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo)
    client = _client()
    # Planning uses 0.12; revalidation returns much lower bid so price is too adverse
    books = [
        _fresh_book("0.12"),
        _fresh_book("0.12"),
        _fresh_book("0.20"),  # revalidation in close_live auto path
        _fresh_book("0.20"),
    ]
    client.get_token_order_book.side_effect = lambda token_id: (
        books.pop(0) if books else _fresh_book("0.20")
    )
    service = _execution_service(repo, client)
    # Plan price from first book 0.12; if revalidation best_bid jumps to 0.20,
    # price 0.12 is 0.08 below bid > max_slippage 0.01
    result = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert result.executed == 0
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_auto_exit_no_best_bid_is_persisted_skip_not_failure(tmp_path):
    settings = _settings(
        tmp_path,
        AUTO_EXIT_ENABLED=True,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("5"),
    )
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo)
    client = _client()
    no_bid = (
        MarketSnapshot(
            market_id="token_book",
            best_bid=None,
            best_ask=Decimal("0.13"),
            midpoint=None,
            spread=None,
            liquidity=Decimal("0"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {},
    )
    books = [_fresh_book("0.12"), no_bid]
    client.get_token_order_book.side_effect = lambda _token: books.pop(0) if books else no_bid

    result = _execution_service(repo, client).run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    conn.commit()

    assert result.executed == 0
    assert result.attempted == 0
    assert result.failures == []
    assert any("auto-exit deferred" in item for item in result.skipped)
    client.place_sell_limit_order.assert_not_called()
    actions = repo.list_automation_actions(kind="auto_exit", market_id="m-exit")
    assert len(actions) == 1
    assert actions[0]["status"] == "skipped"
    assert actions[0]["idempotency_key"] == "auto-exit-no-bid:m-exit:YES"

    second = _execution_service(repo, client).run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert second.failures == []
    assert len(repo.list_automation_actions(kind="auto_exit", market_id="m-exit")) == 1
    conn.close()


def test_auto_exit_no_bid_positions_do_not_starve_executable_full_exit(tmp_path):
    settings = _settings(
        tmp_path,
        AUTO_EXIT_ENABLED=True,
        MAX_AUTO_EXITS_PER_TICK=1,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("5"),
    )
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo, "m-no-bid-1")
    for market_id, suffix in (("m-no-bid-2", "2"), ("m-live-bid", "3")):
        repo.upsert_market(
            Market(
                id=market_id,
                title=f"Auto exit market {suffix}",
                is_weather=True,
                slug=market_id,
                yes_token_id=f"yes-token-{suffix}",
                no_token_id=f"no-token-{suffix}",
                status="active",
            ),
            {
                "id": market_id,
                "outcomes": ["Yes", "No"],
                "clobTokenIds": [f"yes-token-{suffix}", f"no-token-{suffix}"],
            },
        )
        conn.execute(
            """
            INSERT INTO positions (market_id, outcome, size, notional, updated_at)
            VALUES (?, 'Yes', 5, 0.65, CURRENT_TIMESTAMP)
            """,
            (market_id,),
        )
        _seed_value_confirmations(repo, market_id)

    client = _client()

    def book_for(token_id):
        if token_id in {"yes-token", "yes-token-2"}:
            return (
                MarketSnapshot(
                    market_id="token_book",
                    best_bid=None,
                    best_ask=Decimal("0.01"),
                    midpoint=None,
                    spread=None,
                    liquidity=Decimal("0"),
                    fetched_at=datetime.now(timezone.utc),
                ),
                {},
            )
        return _fresh_book("0.12")

    client.get_token_order_book.side_effect = book_for
    result = _execution_service(repo, client).run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )

    assert result.executed == 1
    assert result.attempted == 1
    client.place_sell_limit_order.assert_called_once()
    conn.close()


def test_status_snapshot_never_arms_by_default(tmp_path):
    settings = _settings(tmp_path)
    snap = AutoExitService.status_snapshot(
        settings=settings, profile_name="balanced", allow_auto_exit=False
    )
    assert snap["auto_exit_enabled"] is False
    assert snap["armed"] is False


def test_status_snapshot_full_live_implies_auto_exit(tmp_path):
    settings = _settings(tmp_path, AUTO_EXIT_ENABLED=False)
    snap = AutoExitService.status_snapshot(
        settings=settings, profile_name="full-live", allow_auto_exit=True
    )
    assert snap["auto_exit_enabled"] is True
    assert snap["armed"] is True


def test_full_live_executes_auto_exit_when_env_switch_is_false(tmp_path):
    settings = _settings(
        tmp_path,
        AUTO_EXIT_ENABLED=False,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("5"),
    )
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    _seed(repo)
    client = _client()

    result = _execution_service(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )

    assert result.enabled_gates_ok is True
    assert result.executed == 1
    client.place_sell_limit_order.assert_called_once()
    conn.close()
