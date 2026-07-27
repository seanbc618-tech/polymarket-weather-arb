"""Acceptance blockers from 2026-07-12 full-live review (offline only)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.market_eligibility import (
    evaluate_market_orderability,
    local_weather_day,
    resolve_market_timezone,
)
from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.logging_config import (
    ExpectedSdkAuthBootstrapFilter,
    redact_text,
    setup_persistent_logging,
)
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _db(tmp_path):
    path = tmp_path / "blockers.db"
    database = Database(path)
    database.init_schema()
    conn = database.connect()
    return database, conn, Repository(conn)


def _rule():
    return ResolutionRule(
        raw_text="t",
        location="Chicago",
        station="KORD",
        source="NOAA",
        variable="temperature_high",
        operator=">=",
        threshold=Decimal("80"),
        unit="F",
        window_start=None,
        window_end=None,
        confidence=0.9,
        tradable=True,
        rejection_reason=None,
    )


def test_expired_position_writes_settlement_analysis_without_audit_or_breaker(tmp_path):
    _, conn, repo = _db(tmp_path)
    market_id = "expired-pos"
    payload = {
        "id": market_id,
        "closed": True,
        "acceptingOrders": False,
        "umaResolutionStatus": "unresolved",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.5","0.5"]',
    }
    repo.upsert_market(
        Market(
            id=market_id,
            title="Highest temperature in Chicago on July 11, 2026?",
            yes_token_id="y",
            no_token_id="n",
            is_weather=True,
            close_time="2026-07-11T00:00:00Z",
        ),
        payload,
    )
    repo.replace_positions(
        [{"market": market_id, "outcome": "Yes", "size": "5", "avgPrice": "0.2"}]
    )
    client = Mock()
    client.get_market = Mock(side_effect=AssertionError("must not call network audit"))
    service = AutopilotService(
        Settings(DATABASE_PATH=tmp_path / "blockers.db"),
        repo,
        client=client,
    )
    service._handle_expired_position(market_id)

    analysis = repo.latest_analysis(market_id)
    assert analysis is not None
    assert analysis["decision"] == "skip"
    reasons = analysis["reasons"]
    if isinstance(reasons, str):
        reasons = json.loads(reasons)
    assert any("settlement state:" in r for r in reasons)
    # No resolution audit row and breaker still clear.
    assert conn.execute("SELECT COUNT(*) AS n FROM resolution_audits").fetchone()["n"] == 0
    assert not bool(repo.get_circuit_breaker_state()["circuit_breaker_tripped"])
    client.get_market.assert_not_called()
    conn.close()


@pytest.mark.parametrize(
    ("held_outcome", "outcome_prices", "expected_status"),
    [
        ("Yes", ["1", "0"], "settled_win_redeemable"),
        ("Yes", ["0", "1"], "settled_loss_zero_value"),
    ],
)
def test_resolved_position_status_compares_held_outcome_to_winner(
    tmp_path, held_outcome, outcome_prices, expected_status
):
    _, conn, repo = _db(tmp_path)
    market_id = f"resolved-{expected_status}"
    payload = {
        "id": market_id,
        "closed": True,
        "acceptingOrders": False,
        "umaResolutionStatus": "resolved",
        "outcomes": ["Yes", "No"],
        "outcomePrices": outcome_prices,
    }
    repo.upsert_market(
        Market(
            id=market_id,
            title="Highest temperature in Chicago on July 11, 2026?",
            yes_token_id="y",
            no_token_id="n",
            is_weather=True,
            close_time="2026-07-11T00:00:00Z",
        ),
        payload,
    )
    repo.replace_positions(
        [
            {
                "market": market_id,
                "outcome": held_outcome,
                "size": "5",
                "avgPrice": "0.2",
            }
        ]
    )
    service = AutopilotService(
        Settings(DATABASE_PATH=tmp_path / "blockers.db"),
        repo,
        client=Mock(),
    )

    service._handle_expired_position(market_id)

    analysis = repo.latest_analysis(market_id)
    reasons = json.loads(analysis["reasons"])
    assert reasons == [f"Position expired/closed; settlement state: {expected_status}"]
    conn.close()


def test_position_refresh_routes_past_local_day_without_order_book_call(tmp_path):
    _, conn, repo = _db(tmp_path)
    market_id = "past-shanghai-position"
    repo.upsert_market(
        Market(
            id=market_id,
            title="Highest temperature in Shanghai on July 11, 2020?",
            yes_token_id="y",
            no_token_id="n",
            is_weather=True,
            close_time=None,
        ),
        {
            "id": market_id,
            "closed": False,
            "active": True,
            "acceptingOrders": True,
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.5", "0.5"],
        },
    )
    repo.replace_positions(
        [{"market": market_id, "outcome": "Yes", "size": "5", "avgPrice": "0.2"}]
    )
    service = AutopilotService(
        Settings(DATABASE_PATH=tmp_path / "blockers.db"),
        repo,
        client=Mock(),
    )
    service._prepare_market = Mock(side_effect=AssertionError("order book path must be skipped"))

    service._refresh_position_analyses()

    service._prepare_market.assert_not_called()
    analysis = repo.latest_analysis(market_id)
    assert "settlement_pending" in analysis["reasons"]
    conn.close()


def test_redaction_covers_0x_private_key_and_telegram_token():
    pk = "0x" + ("a" * 64)
    token = "123456789:ABCDEFghijklmnopQRSTUVwxyz-123456"
    address = "0x" + ("b" * 40)
    text = f"pk={pk} token={token} address={address}"
    redacted = redact_text(text)
    assert pk not in redacted
    assert token not in redacted
    assert address not in redacted
    assert "[REDACTED_PK]" in redacted
    assert "[REDACTED_ADDRESS]" in redacted


def test_redaction_covers_prefixed_secrets_bearer_and_relayer():
    secrets = {
        "POLYMARKET_CLOB_API_KEY": "sk-live-abc123XYZ",
        "my_custom_api_secret": "super-secret-value",
        "RELAYER_API_KEY": "relayer-key-001",
        "builder_relayer_credential": "cred-xyz",
        "ACCESS_TOKEN": "access-token-value",
        "password": "hunter2",
    }
    line = " ".join(f"{k}={v}" for k, v in secrets.items())
    line += " Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    line += " also Bearer rawtokenvalue123"
    redacted = redact_text(line)
    for value in secrets.values():
        assert value not in redacted
    assert "eyJhbGciOiJIUzI1NiJ9.payload.sig" not in redacted
    assert "rawtokenvalue123" not in redacted
    assert "POLYMARKET_CLOB_API_KEY=[REDACTED]" in redacted
    assert "RELAYER_API_KEY=[REDACTED]" in redacted
    assert "[REDACTED_BEARER]" in redacted


def test_setup_persistent_logging_creates_rotating_file(tmp_path):
    log_file = setup_persistent_logging(tmp_path)
    assert log_file.exists() or log_file.parent.exists()
    import logging

    logging.getLogger("acceptance").info("hello from test")
    # handler may buffer; force flush
    for h in logging.getLogger().handlers:
        h.flush()
    assert log_file.exists()


def test_expected_sdk_auth_bootstrap_filter_is_narrow():
    import logging

    filter_ = ExpectedSdkAuthBootstrapFilter()

    def record(name, message):
        return logging.LogRecord(name, logging.INFO, __file__, 1, message, (), None)

    expected = record(
        "httpx",
        'HTTP Request: POST https://clob.polymarket.com/auth/api-key "HTTP/2 400 Bad Request"',
    )
    unrelated_400 = record(
        "httpx",
        'HTTP Request: POST https://clob.polymarket.com/order "HTTP/2 400 Bad Request"',
    )
    auth_401 = record(
        "httpx",
        'HTTP Request: POST https://clob.polymarket.com/auth/api-key "HTTP/2 401 Unauthorized"',
    )

    assert filter_.filter(expected) is False
    assert filter_.filter(unrelated_400) is True
    assert filter_.filter(auth_401) is True


def test_chicago_local_day_not_utc_midnight_cutoff():
    # 2026-07-12 02:00 UTC is still 2026-07-11 evening in Chicago.
    now = datetime(2026, 7, 12, 2, 0, tzinfo=timezone.utc)
    local = local_weather_day(title="Highest temperature in Chicago on July 11, 2026?", now=now)
    assert local == datetime(2026, 7, 11, tzinfo=ZoneInfo("America/Chicago")).date()
    assert resolve_market_timezone(title="temperature in Chicago on July 11") == "America/Chicago"

    result = evaluate_market_orderability(
        raw_payload={"closed": False, "acceptingOrders": True},
        title="Highest temperature in Chicago on July 11, 2026?",
        close_time=(now + timedelta(days=1)).isoformat(),
        now=now,
        check_target_date=True,
    )
    # Local day is still July 11 → target date is not before local day.
    assert result.orderable is True

    # After Chicago midnight, same market is expired by target date.
    later = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    result2 = evaluate_market_orderability(
        raw_payload={"closed": False, "acceptingOrders": True},
        title="Highest temperature in Chicago on July 11, 2026?",
        close_time=(later + timedelta(days=1)).isoformat(),
        now=later,
        check_target_date=True,
    )
    assert result2.orderable is False
    assert "local day" in (result2.reason or "")


def test_unknown_city_skips_target_date_expiry_without_utc_fallback():
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    title = "Highest temperature in Unknownville on July 1, 2020?"
    assert resolve_market_timezone(title=title) is None
    # Open exchange signals + past calendar title → still orderable (no UTC expiry).
    open_result = evaluate_market_orderability(
        raw_payload={"closed": False, "acceptingOrders": True},
        title=title,
        close_time=(now + timedelta(days=2)).isoformat(),
        now=now,
        check_target_date=True,
    )
    assert open_result.orderable is True
    # Exchange closed still expires.
    closed_result = evaluate_market_orderability(
        raw_payload={"closed": True, "acceptingOrders": False},
        title=title,
        now=now,
        check_target_date=True,
    )
    assert closed_result.orderable is False
    assert "closed" in (closed_result.reason or "")


def test_short_city_aliases_do_not_match_inside_global_city_or_station_names():
    assert resolve_market_timezone(title="temperature in Milan on July 20") is None
    assert (
        resolve_market_timezone(
            title="temperature in Helsinki on July 20",
            location_hint="EFHK",
        )
        is None
    )
    assert resolve_market_timezone(location_hint="LA") == "America/Los_Angeles"
    assert (
        resolve_market_timezone(title="temperature in Los Angeles on July 20")
        == "America/Los_Angeles"
    )


def test_candidate_selection_filters_before_rank_limit(tmp_path):
    """Open market beyond edge top-50 must still be selectable."""
    _, conn, repo = _db(tmp_path)
    # 55 closed high-edge markets
    for i in range(55):
        mid = f"closed-{i}"
        title = f"Highest temperature in Seoul on July 1, 2020? #{i}"
        repo.upsert_market(
            Market(
                id=mid,
                title=title,
                yes_token_id=f"y{i}",
                no_token_id=f"n{i}",
                is_weather=True,
                close_time="2020-07-01T00:00:00Z",
            ),
            {"id": mid, "closed": True, "acceptingOrders": False},
        )
        conn.execute("UPDATE markets SET module_id='weather' WHERE id=?", (mid,))
        repo.upsert_candidate(mid, _rule(), status="dry_run_ready", module_id="weather")
        repo.save_analysis(
            Analysis(
                market_id=mid,
                model_version="t",
                fair_lower=Decimal("0.9"),
                fair_upper=Decimal("0.95"),
                reference_price=Decimal("0.1"),
                edge=Decimal("0.90") - Decimal(i) * Decimal("0.001"),
                side="buy_yes",
                decision="trade",
                reasons=["closed high"],
            )
        )
    # Open lower-edge market
    future = (datetime.now(timezone.utc) + timedelta(days=5)).date()
    open_id = "open-51"
    title = f"Highest temperature in Tokyo on {future.strftime('%B')} {future.day}, {future.year}?"
    repo.upsert_market(
        Market(
            id=open_id,
            title=title,
            yes_token_id="yo",
            no_token_id="no",
            is_weather=True,
            close_time=(datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        ),
        {
            "id": open_id,
            "closed": False,
            "acceptingOrders": True,
            "endDate": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        },
    )
    conn.execute("UPDATE markets SET module_id='weather' WHERE id=?", (open_id,))
    repo.upsert_candidate(open_id, _rule(), status="dry_run_ready", module_id="weather")
    repo.save_analysis(
        Analysis(
            market_id=open_id,
            model_version="t",
            fair_lower=Decimal("0.7"),
            fair_upper=Decimal("0.8"),
            reference_price=Decimal("0.4"),
            edge=Decimal("0.08"),
            side="buy_yes",
            decision="trade",
            reasons=["open lower"],
        )
    )
    best = repo.best_weather_candidate_by_edge(min_edge=0.05)
    assert best is not None
    assert best["market_id"] == open_id
    conn.close()


def test_tick_runs_recon_before_discovery_when_live(tmp_path, monkeypatch):
    _, conn, repo = _db(tmp_path)
    settings = Settings(
        DATABASE_PATH=tmp_path / "blockers.db",
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
        AUTO_EXIT_ENABLED=True,
    )
    order: list[str] = []

    class Client:
        def list_markets(self, *a, **k):
            return []

        def get_event_markets_by_slug(self, slug):
            return []

        def get_balances(self):
            order.append("recon")
            return {"usdc": "10"}

        def get_orders(self):
            return []

        def get_trades(self):
            return []

        def get_positions(self):
            return []

        def get_token_order_book(self, token_id):
            raise RuntimeError("unused")

        def get_order_book(self, market):
            raise RuntimeError("unused")

    def fake_discover_events(self, *a, **k):
        order.append("discovery")
        return 0

    def fake_discover(self, *a, **k):
        order.append("discovery")
        return 0

    monkeypatch.setattr(
        "polymarket_weather_arb.services.discovery_service.DiscoveryService.discover_weather_events",
        fake_discover_events,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.discovery_service.DiscoveryService.discover",
        fake_discover,
    )

    # Seed a prior successful recon so collect_blockers allows the tick body.
    repo.save_reconciliation("ok", {"status": "ok"})
    service = AutopilotService(settings, repo, client=Client())
    service.ensure_state(mode="live", tick_seconds=300)
    repo.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
    service.tick()
    assert "recon" in order
    # When both run, recon must appear before discovery.
    if "discovery" in order:
        assert order.index("recon") < order.index("discovery")
    conn.close()


def test_autopilot_start_sets_process_started_at(tmp_path):
    """process_started_at is written on the same path autopilot start uses."""
    _, conn, repo = _db(tmp_path)
    from polymarket_weather_arb.services.autopilot_service import _now_iso

    repo.ensure_autopilot_state(mode="live", app_mode="full_live", tick_seconds=300)
    started = _now_iso()
    repo.update_autopilot_state(process_started_at=started)
    state = repo.get_autopilot_state()
    assert state is not None
    assert dict(state).get("process_started_at") == started
    conn.close()


def test_full_tick_settlement_route_never_calls_sell(tmp_path, monkeypatch):
    """Closed-position settlement-route must not become auto-exit SELL on a full tick."""
    _, conn, repo = _db(tmp_path)
    market_id = "settlement-hold"
    payload = {
        "id": market_id,
        "closed": True,
        "acceptingOrders": False,
        "umaResolutionStatus": "unresolved",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.5","0.5"]',
        "clobTokenIds": '["yes-token","no-token"]',
    }
    repo.upsert_market(
        Market(
            id=market_id,
            title="Highest temperature in Seoul on July 11, 2026?",
            yes_token_id="yes-token",
            no_token_id="no-token",
            is_weather=True,
            close_time="2026-07-11T00:00:00Z",
            status="closed",
        ),
        payload,
    )
    conn.execute("UPDATE markets SET module_id='weather' WHERE id=?", (market_id,))
    repo.replace_positions(
        [{"market": market_id, "outcome": "Yes", "size": "8", "avgPrice": "0.15"}]
    )
    # Stale non-settlement analysis would look like edge-gone; settlement route must win.
    repo.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="old",
            fair_lower=Decimal("0.2"),
            fair_upper=Decimal("0.3"),
            reference_price=Decimal("0.5"),
            edge=Decimal("0.01"),
            side=None,
            decision="skip",
            reasons=["edge gone"],
        )
    )
    repo.save_reconciliation("ok", {"status": "ok"})

    sell_calls: list[dict] = []

    class Client:
        def list_markets(self, *a, **k):
            return []

        def get_event_markets_by_slug(self, slug):
            return []

        def get_balances(self):
            return {"usdc": "10"}

        def get_orders(self):
            return []

        def get_trades(self):
            return []

        def get_positions(self):
            return [
                {
                    "market": market_id,
                    "outcome": "Yes",
                    "size": "8",
                    "avgPrice": "0.15",
                }
            ]

        def get_token_order_book(self, token_id):
            from polymarket_weather_arb.domain.markets import MarketSnapshot

            return (
                MarketSnapshot(
                    market_id="token_book",
                    best_bid=Decimal("0.12"),
                    best_ask=Decimal("0.13"),
                    midpoint=Decimal("0.125"),
                    spread=Decimal("0.01"),
                    liquidity=Decimal("100"),
                    fetched_at=datetime.now(timezone.utc),
                ),
                {},
            )

        def get_order_book(self, market):
            return self.get_token_order_book("x")

        def place_limit_order(self, **kwargs):
            raise AssertionError(f"BUY must not run: {kwargs}")

        def place_sell_limit_order(self, **kwargs):
            sell_calls.append(kwargs)
            raise AssertionError(f"SELL must not run for settlement route: {kwargs}")

        def get_market(self, market_id):
            raise AssertionError("resolution audit network path forbidden")

        def get_order(self, order_id):
            return {"id": order_id, "status": "LIVE"}

        def cancel_order(self, order_id):
            raise AssertionError("cancel must not run in this test")

    def no_discover(self, *a, **k):
        return 0

    monkeypatch.setattr(
        "polymarket_weather_arb.services.discovery_service.DiscoveryService.discover_weather_events",
        no_discover,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.discovery_service.DiscoveryService.discover",
        no_discover,
    )

    settings = Settings(
        DATABASE_PATH=tmp_path / "blockers.db",
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
        AUTO_EXIT_ENABLED=True,
        MAX_AUTO_EXITS_PER_TICK=3,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("50"),
        AUTO_EXIT_MAX_SLIPPAGE=Decimal("0.05"),
        MIN_EDGE=Decimal("0.05"),
    )
    service = AutopilotService(settings, repo, client=Client())
    service.ensure_state(mode="live", tick_seconds=300)
    repo.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    monkeypatch.setattr(
        service,
        "collect_blockers",
        lambda **_kwargs: type("B", (), {"blocked": False, "items": []})(),
    )

    result = service.tick()

    # Settlement analysis written by position refresh path.
    analysis = repo.latest_analysis(market_id)
    assert analysis is not None
    assert analysis["model_version"] == "settlement-route-v1"
    assert analysis["decision"] == "skip"

    from polymarket_weather_arb.services.exit_guardian_service import ExitGuardianService

    recs = ExitGuardianService(repo).evaluate(min_edge=Decimal("0.05"))
    pos_recs = [r for r in recs if r.kind == "position" and r.market_id == market_id]
    assert pos_recs
    assert pos_recs[0].action == "settlement_pending"
    assert pos_recs[0].action != "position_at_risk"

    assert sell_calls == []
    assert result.auto_exit_executed == 0
    conn.close()
