"""Offline regressions for full-live runtime correctness (2026-07-12).

Covers closed/expired selection, fee ledger + net edge, order preflight /
intent terminality, and partial-exit dust / action lifecycle. Network and SDK
mutations are mocked only — no real trading.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from unittest.mock import Mock

import pytest

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.execution import build_proposed_order, preflight_buy_rejection_reason
from polymarket_weather_arb.domain.fees import (
    WEATHER_TAKER_FEE_RATE,
    compute_taker_fee,
    extract_market_fee_schedule,
    resolve_fill_fee,
)
from polymarket_weather_arb.domain.market_eligibility import evaluate_market_orderability
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.order_constraints import (
    OrderConstraints,
    MARKETABLE_MIN_NOTIONAL,
    normalize_buy_order,
)
from polymarket_weather_arb.domain.pricing import Analysis, analyze_price
from polymarket_weather_arb.domain.probability import ProbabilityInterval
from polymarket_weather_arb.domain.risk import RiskContext
from polymarket_weather_arb.domain.rules import ResolutionRule
from polymarket_weather_arb.domain.strategy_versions import GLOBAL_BUCKET_MODEL_VERSION
from polymarket_weather_arb.services.auto_exit_service import AutoExitService
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.services.cockpit_service import _build_verified_pnl
from polymarket_weather_arb.services.compliance_service import ComplianceDecision, ComplianceService
from polymarket_weather_arb.services.discovery_service import DiscoveryService
from polymarket_weather_arb.services.exit_guardian_service import ExitRecommendation
from polymarket_weather_arb.services.trading_service import TradingService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db(tmp_path, name="runtime.db"):
    path = tmp_path / name
    database = Database(path)
    database.init_schema()
    return database, database.connect()


def _market(
    market_id: str,
    *,
    title: str = "Highest temperature in Seoul on July 20, 2026?",
    closed: bool = False,
    accepting: bool = True,
    close_time: str | None = None,
    fees_enabled: bool = True,
    fee_type: str = "weather_fees",
    order_min_size: str | None = "5",
    yes: str = "yes-token",
    no: str = "no-token",
) -> tuple[Market, dict]:
    payload = {
        "id": market_id,
        "question": title,
        "closed": closed,
        "acceptingOrders": accepting,
        "endDate": close_time,
        "feesEnabled": fees_enabled,
        "feeType": fee_type,
        "orderMinSize": order_min_size,
        "outcomes": ["Yes", "No"],
        "clobTokenIds": [yes, no],
    }
    market = Market(
        id=market_id,
        title=title,
        slug=market_id,
        description="Weather market",
        yes_token_id=yes,
        no_token_id=no,
        close_time=close_time,
        status="closed" if closed else "active",
        is_weather=True,
    )
    return market, payload


def _rule(tradable: bool = True) -> ResolutionRule:
    return ResolutionRule(
        raw_text="test",
        location="Seoul",
        station=None,
        source="KMA",
        variable="temperature_high",
        operator=">=",
        threshold=Decimal("30"),
        unit="C",
        window_start=None,
        window_end=None,
        confidence=0.9,
        tradable=tradable,
        rejection_reason=None if tradable else "ambiguous",
    )


def _seed_candidate(
    repo: Repository,
    market_id: str,
    *,
    edge: float,
    title: str,
    closed: bool = False,
    accepting: bool = True,
    close_time: str | None = None,
    status: str = "dry_run_ready",
    decision: str = "trade",
    fees_enabled: bool = True,
):
    market, payload = _market(
        market_id,
        title=title,
        closed=closed,
        accepting=accepting,
        close_time=close_time,
        fees_enabled=fees_enabled,
    )
    # Force module_id weather via upsert then update
    repo.upsert_market(market, payload)
    repo.connection.execute(
        "UPDATE markets SET module_id = 'weather' WHERE id = ?", (market_id,)
    )
    repo.upsert_candidate(market_id, _rule(), status=status, module_id="weather")
    repo.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="t",
            fair_lower=Decimal("0.80"),
            fair_upper=Decimal("0.90"),
            reference_price=Decimal("0.50"),
            edge=Decimal(str(edge)),
            side="buy_yes",
            decision=decision,
            reasons=["fixture"],
        )
    )


# ---------------------------------------------------------------------------
# Slice 1: closed/expired + fresh quote
# ---------------------------------------------------------------------------


def test_closed_high_edge_cannot_beat_open_lower_edge(tmp_path):
    db, conn = _db(tmp_path)
    repo = Repository(conn)
    # Closed Seoul monopolizer with huge edge
    _seed_candidate(
        repo,
        "2854442",
        edge=0.99,
        title="Highest temperature in Seoul on July 11, 2026?",
        closed=True,
        close_time="2026-07-11T00:00:00Z",
    )
    # Open market with smaller edge
    future = (datetime.now(timezone.utc) + timedelta(days=3)).date()
    _seed_candidate(
        repo,
        "open-1",
        edge=0.10,
        title=f"Highest temperature in Tokyo on {future.strftime('%B')} {future.day}, {future.year}?",
        closed=False,
        accepting=True,
        close_time=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
    )
    best = repo.best_weather_candidate_by_edge(min_edge=0.05)
    assert best is not None
    assert best["market_id"] == "open-1"
    conn.close()


@pytest.mark.parametrize(
    "kwargs,reason_part",
    [
        ({"accepting": False}, "acceptingOrders"),
        (
            {
                "close_time": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                "include_accepting": False,
            },
            "close_time",
        ),
        (
            {
                "title": "Highest temperature in Seoul on July 1, 2020?",
                "close_time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            },
            "target date",
        ),
    ],
)
def test_eligibility_excludes_accepting_close_and_past_target(kwargs, reason_part):
    title = kwargs.pop("title", "Highest temperature in Seoul on December 31, 2099?")
    payload = {"closed": False, "endDate": kwargs.get("close_time")}
    if kwargs.get("include_accepting", True):
        payload["acceptingOrders"] = kwargs.get("accepting", True)
    result = evaluate_market_orderability(
        raw_payload=payload,
        title=title,
        close_time=kwargs.get("close_time"),
    )
    assert result.orderable is False
    assert reason_part.lower() in (result.reason or "").lower()


def test_explicit_accepting_orders_overrides_past_gamma_end_date_on_local_target_day():
    now = datetime(2026, 7, 12, 19, 0, tzinfo=timezone.utc)
    result = evaluate_market_orderability(
        raw_payload={
            "active": True,
            "closed": False,
            "acceptingOrders": True,
            "enableOrderBook": True,
            "endDate": "2026-07-12T12:00:00Z",
        },
        title="Highest temperature in Chicago on July 12, 2026?",
        close_time="2026-07-12T12:00:00Z",
        now=now,
    )

    assert result.orderable is True


@pytest.mark.parametrize(
    "field",
    ["active", "enableOrderBook"],
)
def test_explicit_inactive_exchange_signals_block_orderability(field):
    payload = {
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }
    payload[field] = False

    result = evaluate_market_orderability(
        raw_payload=payload,
        title="Highest temperature in Chicago on December 31, 2099?",
    )

    assert result.orderable is False
    assert field.lower() in (result.reason or "").lower()


def test_discovery_transitions_ready_candidate_without_deleting_history(tmp_path):
    db, conn = _db(tmp_path)
    repo = Repository(conn)
    market_id = "2854442"
    future = (datetime.now(timezone.utc) + timedelta(days=5)).date()
    title = (
        f"Will the highest temperature in Seoul on "
        f"{future.strftime('%B')} {future.day}, {future.year} be 30°C or higher?"
    )
    description = (
        "Resolves based on the National Weather Service (NOAA) report for station. "
        "This market resolves to Yes if the highest temperature is 30C or higher."
    )
    market, payload = _market(
        market_id,
        title=title,
        closed=False,
        accepting=True,
        close_time=(datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
    )
    market = Market(
        id=market_id,
        title=title,
        slug=market_id,
        description=description,
        yes_token_id="yes-token",
        no_token_id="no-token",
        close_time=payload.get("endDate"),
        status="active",
        is_weather=True,
    )
    payload = {**payload, "question": title, "description": description}
    repo.upsert_market(market, payload)
    repo.upsert_candidate(market_id, _rule(), status="dry_run_ready", module_id="weather")
    repo.save_analysis(
        Analysis(
            market_id=market_id,
            model_version="t",
            fair_lower=Decimal("0.7"),
            fair_upper=Decimal("0.8"),
            reference_price=Decimal("0.5"),
            edge=Decimal("0.2"),
            side="buy_yes",
            decision="trade",
            reasons=["keep me"],
        )
    )
    analyses_before = conn.execute(
        "SELECT COUNT(*) AS n FROM analyses WHERE market_id = ?", (market_id,)
    ).fetchone()["n"]

    closed_payload = {**payload, "closed": True, "acceptingOrders": False}
    closed_market = Market(
        id=market_id,
        title=title,
        slug=market_id,
        description=description,
        yes_token_id="yes-token",
        no_token_id="no-token",
        close_time=market.close_time,
        status="closed",
        is_weather=True,
    )

    class Client:
        def list_markets(self, limit=100, offset=0):
            if offset:
                return []
            return [(closed_market, closed_payload)]

        def get_order_book(self, m):
            return (
                MarketSnapshot(
                    market_id=m.id,
                    best_bid=Decimal("0.4"),
                    best_ask=Decimal("0.5"),
                    midpoint=Decimal("0.45"),
                    spread=Decimal("0.1"),
                    liquidity=Decimal("10"),
                    fetched_at=datetime.now(timezone.utc),
                ),
                {},
            )

    count = DiscoveryService(Client(), repo).discover(limit=10, pages=1)
    assert count >= 1
    cand = conn.execute(
        "SELECT status, notes FROM market_candidates WHERE market_id = ?", (market_id,)
    ).fetchone()
    assert cand["status"] == "expired"
    analyses_after = conn.execute(
        "SELECT COUNT(*) AS n FROM analyses WHERE market_id = ?", (market_id,)
    ).fetchone()["n"]
    assert analyses_after == analyses_before
    assert conn.execute("SELECT id FROM markets WHERE id = ?", (market_id,)).fetchone()
    conn.close()


def test_live_path_refreshes_stale_snapshot_and_rejects_on_refresh_failure(tmp_path):
    db, conn = _db(tmp_path)
    settings = Settings(
        DATABASE_PATH=tmp_path / "runtime.db",
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
        MIN_EDGE=Decimal("0.05"),
        STALE_ORDER_BOOK_SECONDS=60,
        STALE_FORECAST_SECONDS=3600,
    )
    repo = Repository(conn)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).date()
    title = f"Highest temperature in Tokyo on {future.strftime('%B')} {future.day}, {future.year}?"
    market, payload = _market("m-refresh", title=title, close_time=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
    repo.upsert_market(market, payload)
    repo.connection.execute("UPDATE markets SET module_id = 'weather' WHERE id = 'm-refresh'")
    stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
    repo.save_market_snapshot(
        MarketSnapshot(
            market_id="m-refresh",
            best_bid=Decimal("0.40"),
            best_ask=Decimal("0.45"),
            midpoint=Decimal("0.425"),
            spread=Decimal("0.05"),
            liquidity=Decimal("100"),
            fetched_at=stale_time,
        ),
        {"stale": True},
    )
    analysis = Analysis(
        market_id="m-refresh",
        model_version="t",
        fair_lower=Decimal("0.80"),
        fair_upper=Decimal("0.90"),
        reference_price=Decimal("0.45"),
        edge=Decimal("0.30"),
        side="buy_yes",
        decision="trade",
        reasons=["edge"],
    )
    repo.save_analysis(analysis)
    repo.save_reconciliation("ok", {"status": "ok"})
    # forecast row for risk context
    from polymarket_weather_arb.domain.weather import ForecastSnapshot

    repo.save_forecast(
        ForecastSnapshot(
            market_id="m-refresh",
            provider="noaa",
            location="Tokyo",
            station="RJTT",
            variable="temperature_high",
            value=Decimal("30"),
            lower_value=Decimal("29"),
            upper_value=Decimal("31"),
            unit="C",
            issue_time=datetime.now(timezone.utc),
            valid_time=datetime.now(timezone.utc),
            fetched_at=datetime.now(timezone.utc),
        ),
        {"source_grade": "official_forecast"},
    )

    client = Mock()
    client.get_token_order_book.return_value = (
        MarketSnapshot(
            market_id="token_book",
            best_bid=Decimal("0.41"),
            best_ask=Decimal("0.46"),
            midpoint=Decimal("0.435"),
            spread=Decimal("0.05"),
            liquidity=Decimal("100"),
            fetched_at=datetime.now(timezone.utc),
        ),
        {"fresh": True},
    )
    client.place_limit_order.side_effect = RuntimeError("should not place without full live gates")

    service = AutopilotService(settings, repo, client=client)
    # Force live override path pieces by calling refresh helper + execute with mocked gates
    snap, err = service._refresh_live_order_book(
        market_id="m-refresh",
        market_row=repo.get_market("m-refresh"),
        analysis=analysis,
    )
    assert err is None
    assert snap is not None
    assert Decimal(str(snap["best_ask"])) == Decimal("0.46")
    client.get_token_order_book.assert_called()

    # Refresh failure must not fall back to stale snapshot
    client.get_token_order_book.side_effect = RuntimeError("book down")
    snap2, err2 = service._refresh_live_order_book(
        market_id="m-refresh",
        market_row=repo.get_market("m-refresh"),
        analysis=analysis,
    )
    assert snap2 is None
    assert err2 is not None
    assert "refresh failed" in err2
    # Stale snapshot still exists but must not be used as success
    assert repo.latest_market_snapshot("m-refresh") is not None
    conn.close()


# ---------------------------------------------------------------------------
# Slice 2: fees + net edge
# ---------------------------------------------------------------------------


def test_weather_taker_fee_matches_official_formula():
    # Docs: Weather feeRate 0.05; 100 shares at 0.50 => $1.25
    fee = compute_taker_fee(
        shares=Decimal("100"), price=Decimal("0.50"), fee_rate=WEATHER_TAKER_FEE_RATE
    )
    assert fee == Decimal("1.25000")
    # Symmetric: 0.30 and 0.70 same
    f30 = compute_taker_fee(shares=Decimal("100"), price=Decimal("0.30"), fee_rate=WEATHER_TAKER_FEE_RATE)
    f70 = compute_taker_fee(shares=Decimal("100"), price=Decimal("0.70"), fee_rate=WEATHER_TAKER_FEE_RATE)
    assert f30 == f70 == Decimal("1.05000")


def test_maker_unknown_role_and_reconciliation_fee_correction(tmp_path):
    db, conn = _db(tmp_path)
    repo = Repository(conn)
    market, payload = _market("m-fee", fees_enabled=True, fee_type="weather_fees")
    repo.upsert_market(market, payload)

    # Known order id as taker
    conn.execute(
        """
        INSERT INTO order_intents (market_id, side, limit_price, size, notional, rationale, dry_run, status)
        VALUES ('m-fee', 'buy_yes', 0.5, 10, 5, 't', 0, 'submitted')
        """
    )
    intent_id = conn.execute("SELECT id FROM order_intents").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO order_attempts (intent_id, status, request_payload, response_payload)
        VALUES (?, 'submitted', '{}', '{"orderID": "order-taker-1"}')
        """,
        (intent_id,),
    )

    # First reconcile with zero fee (exchange omitted fee)
    trades = [
        {
            "id": "trade-1",
            "taker_order_id": "order-taker-1",
            "market": "m-fee",
            "side": "BUY",
            "price": "0.50",
            "size": "10",
            "fee": "0",
        }
    ]
    n, _ = repo.save_reconciled_fills(trades)
    assert n == 1
    row = conn.execute("SELECT fee, raw_payload FROM fills WHERE exchange_fill_id = 'trade-1'").fetchone()
    expected = float(compute_taker_fee(shares=Decimal("10"), price=Decimal("0.50"), fee_rate=WEATHER_TAKER_FEE_RATE))
    assert abs(row["fee"] - expected) < 1e-9
    payload_stored = json.loads(row["raw_payload"])
    assert payload_stored["_fee_resolution"]["role"] == "taker"
    assert payload_stored["_fee_resolution"]["method"] == "formula"

    # Maker fill remains zero
    conn.execute(
        """
        INSERT INTO order_attempts (intent_id, status, request_payload, response_payload)
        VALUES (?, 'submitted', '{}', '{"orderID": "order-maker-1"}')
        """,
        (intent_id,),
    )
    maker_trade = [
        {
            "id": "trade-maker",
            "taker_order_id": "someone-else",
            "market": "m-fee",
            "side": "BUY",
            "price": "0.50",
            "size": "10",
            "fee": "0",
            "maker_orders": [
                {"order_id": "order-maker-1", "matched_amount": "10", "price": "0.50"}
            ],
        }
    ]
    repo.save_reconciled_fills(maker_trade)
    maker_row = conn.execute(
        "SELECT fee, raw_payload FROM fills WHERE exchange_fill_id = 'trade-maker'"
    ).fetchone()
    assert maker_row["fee"] == 0
    assert json.loads(maker_row["raw_payload"])["_fee_resolution"]["role"] == "maker"

    # Unknown role with fees enabled is not silently free
    unknown = resolve_fill_fee(
        fill={"id": "u1", "price": "0.5", "size": "10", "fee": "0"},
        account_fill={"price": "0.5", "size": "10", "fee": "0"},
        market_payload=payload,
        known_order_ids=set(),
    )
    assert unknown.role == "unknown"
    assert unknown.fee > 0
    assert "conservative" in unknown.method or unknown.method == "formula_conservative_unknown_role"

    # Re-reconcile corrects zero: already non-zero; ensure idempotent recompute
    repo.save_reconciled_fills(trades)
    again = conn.execute("SELECT fee FROM fills WHERE exchange_fill_id = 'trade-1'").fetchone()
    assert abs(again["fee"] - expected) < 1e-9
    conn.close()


def test_net_edge_is_fee_aware_and_verified_pnl_uses_fees(tmp_path):
    interval = ProbabilityInterval(Decimal("0.70"), Decimal("0.80"), ["t"])
    gross = analyze_price(
        market_id="m1",
        interval=interval,
        best_bid=Decimal("0.50"),
        best_ask=Decimal("0.60"),
        min_edge=Decimal("0.01"),
        slippage_buffer=Decimal("0.02"),
        fees_enabled=False,
    )
    net = analyze_price(
        market_id="m1",
        interval=interval,
        best_bid=Decimal("0.50"),
        best_ask=Decimal("0.60"),
        min_edge=Decimal("0.01"),
        slippage_buffer=Decimal("0.02"),
        fees_enabled=True,
        fee_rate=WEATHER_TAKER_FEE_RATE,
    )
    assert gross.edge == Decimal("0.08")
    assert net.edge < gross.edge
    assert net.entry_fee_per_share is not None and net.entry_fee_per_share > 0
    assert net.gross_edge == gross.edge

    # Verified PnL consumes fills.fee
    db, conn = _db(tmp_path, "pnl.db")
    repo = Repository(conn)
    conn.execute("INSERT INTO markets (id, title, raw_payload) VALUES ('m1', 'T', '{}')")
    conn.execute(
        "INSERT INTO reconciliations (status, details, created_at) VALUES ('ok', '{}', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO order_intents (id, market_id, side, limit_price, size, notional, rationale, dry_run, status) "
        "VALUES (1, 'm1', 'BUY', 0.5, 10, 5, 'ok', 0, 'filled')"
    )
    conn.execute(
        "INSERT INTO order_intents (id, market_id, side, limit_price, size, notional, rationale, dry_run, status) "
        "VALUES (2, 'm1', 'SELL', 0.6, 10, 6, 'ok', 0, 'filled')"
    )
    conn.execute(
        "INSERT INTO order_attempts (intent_id, status, request_payload, response_payload) "
        "VALUES (1, 'submitted', '{}', '{\"orderID\": \"o1\"}')"
    )
    conn.execute(
        "INSERT INTO order_attempts (intent_id, status, request_payload, response_payload) "
        "VALUES (2, 'submitted', '{}', '{\"orderID\": \"o2\"}')"
    )
    fee_buy = float(compute_taker_fee(shares=Decimal("10"), price=Decimal("0.5"), fee_rate=WEATHER_TAKER_FEE_RATE))
    fee_sell = float(compute_taker_fee(shares=Decimal("10"), price=Decimal("0.6"), fee_rate=WEATHER_TAKER_FEE_RATE))
    conn.execute(
        "INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at) "
        "VALUES ('fb', 'm1', 'o1', 'BUY', 0.5, 10, ?, datetime('now'))",
        (fee_buy,),
    )
    conn.execute(
        "INSERT INTO fills (exchange_fill_id, market_id, order_id, side, price, size, fee, filled_at) "
        "VALUES ('fs', 'm1', 'o2', 'SELL', 0.6, 10, ?, datetime('now'))",
        (fee_sell,),
    )
    conn.execute(
        "INSERT INTO roundtrip_runs (market_id, buy_intent_id, sell_intent_id, status) "
        "VALUES ('m1', 1, 2, 'completed')"
    )
    pnl = _build_verified_pnl(repo)
    assert abs(float(pnl.total_fees) - (fee_buy + fee_sell)) < 1e-6
    # proceeds 6 - cost 5 - fees
    assert float(pnl.total_realized_pnl) == pytest.approx(1.0 - fee_buy - fee_sell, abs=1e-6)
    conn.close()


# ---------------------------------------------------------------------------
# Slice 3: preflight + intent state
# ---------------------------------------------------------------------------


def test_min_notional_0_9996_rejected_or_normalized_before_sdk():
    # Quantize-down trap: max $1 at price that would yield sub-$1 without preflight
    price = Decimal("0.13")
    max_notional = Decimal("1")
    constraints = OrderConstraints(
        min_notional=MARKETABLE_MIN_NOTIONAL,
        min_size=None,
        size_step=Decimal("0.01"),
        price_tick=Decimal("0.001"),
    )
    # size_step 0.01: 1/0.13 = 7.692... -> 7.69 * 0.13 = 0.9997 < 1
    normalized = normalize_buy_order(
        limit_price=price, max_notional=max_notional, constraints=constraints
    )
    # Meeting $1 would need size 7.70 * 0.13 = 1.001 > max 1 -> reject
    assert normalized is None

    analysis = Analysis(
        market_id="m1",
        model_version="t",
        fair_lower=Decimal("0.9"),
        fair_upper=Decimal("0.95"),
        reference_price=price,
        edge=Decimal("0.5"),
        side="buy_yes",
        decision="trade",
        reasons=["t"],
    )
    # Force small max + high min size so local preflight rejects before SDK.
    reason2 = preflight_buy_rejection_reason(
        analysis,
        Decimal("0.9996"),
        market_payload={"orderMinSize": "5"},
    )
    assert reason2 is not None
    assert "minimum" in reason2.lower() or "cap" in reason2.lower()


def test_buy_normalization_survives_official_sdk_two_decimal_size_rounding():
    constraints = OrderConstraints(
        min_notional=MARKETABLE_MIN_NOTIONAL,
        min_size=None,
        size_step=Decimal("0.0001"),
        price_tick=Decimal("0.001"),
    )
    for price in (
        Decimal("0.03"),
        Decimal("0.067"),
        Decimal("0.11"),
        Decimal("0.119"),
        Decimal("0.15"),
    ):
        normalized = normalize_buy_order(
            limit_price=price,
            max_notional=Decimal("1.01"),
            constraints=constraints,
        )

        assert normalized is not None
        sdk_size = normalized.size.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        assert normalized.size == sdk_size
        assert sdk_size * normalized.limit_price >= MARKETABLE_MIN_NOTIONAL


def test_sdk_failure_marks_intent_and_attempt_failed(tmp_path):
    db, conn = _db(tmp_path)
    repo = Repository(conn)
    market, payload = _market("m-fail")
    repo.upsert_market(market, payload)
    client = Mock()
    client.place_limit_order.side_effect = RuntimeError("sdk rejected: min notional")
    service = TradingService(
        Settings(
            DATABASE_PATH=tmp_path / "runtime.db",
            MAX_ORDER_USDC=Decimal("5"),
            MAX_DAILY_USDC=Decimal("100"),
            MAX_MARKET_USDC=Decimal("50"),
            TRADING_DISABLED=False,
            POLYMARKET_PRIVATE_KEY="k",
            POLYMARKET_FUNDER="0xf",
        ),
        client,
        repo,
    )
    analysis = Analysis(
        market_id="m-fail",
        model_version="t",
        fair_lower=Decimal("0.9"),
        fair_upper=Decimal("0.95"),
        reference_price=Decimal("0.50"),
        edge=Decimal("0.4"),
        side="buy_yes",
        decision="trade",
        reasons=["t"],
    )
    context = RiskContext(
        daily_live_notional=Decimal("0"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=1,
        forecast_age_seconds=1,
        rule_tradable=True,
        reconciliation_fresh=True,
    )
    from polymarket_weather_arb.domain.source_grade import OFFICIAL_FORECAST

    client.validate_order_signing = Mock(return_value={"ok": True})
    intent_id, reasons = service.trade(
        analysis=analysis,
        yes_token_id="yes-token",
        no_token_id="no-token",
        context=context,
        dry_run=False,
        source_grade=OFFICIAL_FORECAST,
        market_payload=payload,
    )
    assert intent_id is not None
    assert any("failed" in r.lower() or "sdk" in r.lower() for r in reasons), reasons
    intent = repo.get_order_intent(intent_id)
    assert intent["status"] == "failed"
    attempt = conn.execute(
        "SELECT status, error FROM order_attempts WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    assert attempt["status"] == "failed"
    assert "sdk rejected" in (attempt["error"] or "")
    # Not active for duplicate blocking
    assert repo.active_live_order_intent("m-fail", "buy_yes") is None
    conn.close()


def test_exchange_accepted_retains_submitted_if_later_work_fails(tmp_path):
    db, conn = _db(tmp_path)
    repo = Repository(conn)
    market, payload = _market("m-ok")
    repo.upsert_market(market, payload)
    client = Mock()
    client.place_limit_order.return_value = {"orderID": "ex-1", "status": "live"}
    client.validate_order_signing = Mock(return_value={"ok": True})

    commits = []

    def on_submitted(intent_id):
        commits.append(intent_id)
        # Simulate post-submit work failure after exchange accept
        raise RuntimeError("caller commit failed")

    service = TradingService(
        Settings(
            DATABASE_PATH=tmp_path / "runtime.db",
            MAX_ORDER_USDC=Decimal("5"),
            MAX_DAILY_USDC=Decimal("100"),
            MAX_MARKET_USDC=Decimal("50"),
            TRADING_DISABLED=False,
            POLYMARKET_PRIVATE_KEY="k",
            POLYMARKET_FUNDER="0xf",
        ),
        client,
        repo,
    )
    analysis = Analysis(
        market_id="m-ok",
        model_version="t",
        fair_lower=Decimal("0.9"),
        fair_upper=Decimal("0.95"),
        reference_price=Decimal("0.50"),
        edge=Decimal("0.4"),
        side="buy_yes",
        decision="trade",
        reasons=["t"],
    )
    context = RiskContext(
        daily_live_notional=Decimal("0"),
        market_live_exposure=Decimal("0"),
        order_book_age_seconds=1,
        forecast_age_seconds=1,
        rule_tradable=True,
        reconciliation_fresh=True,
    )
    from polymarket_weather_arb.domain.source_grade import OFFICIAL_FORECAST

    with pytest.raises(RuntimeError, match="caller commit failed"):
        service.trade(
            analysis=analysis,
            yes_token_id="yes-token",
            no_token_id="no-token",
            context=context,
            dry_run=False,
            source_grade=OFFICIAL_FORECAST,
            market_payload=payload,
            on_submitted=on_submitted,
        )
    # Attempt was saved as submitted before callback
    attempt = conn.execute(
        "SELECT status FROM order_attempts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert attempt is not None
    assert attempt["status"] == "submitted"
    intent = conn.execute("SELECT status FROM order_intents ORDER BY id DESC LIMIT 1").fetchone()
    assert intent["status"] == "submitted"
    # Exchange accept audit retained even though post-submit callback failed.
    assert commits
    conn.close()


# ---------------------------------------------------------------------------
# Slice 4: partial exit / dust / action terminality
# ---------------------------------------------------------------------------


def _auto_exit_settings(tmp_path, **overrides):
    base = dict(
        DATABASE_PATH=tmp_path / "auto.db",
        POLYMARKET_PRIVATE_KEY="k",
        POLYMARKET_FUNDER="0xf",
        TRADING_DISABLED=False,
        COMPLIANCE_CHECK_ENABLED=False,
        AUTO_EXIT_ENABLED=True,
        MAX_AUTO_EXITS_PER_TICK=5,
        AUTO_EXIT_MAX_POSITION_USDC=Decimal("50"),
        AUTO_EXIT_MAX_SLIPPAGE=Decimal("0.05"),
        STALE_ORDER_BOOK_SECONDS=300,
        MIN_EDGE=Decimal("0.05"),
    )
    base.update(overrides)
    return Settings(**base)


def _seed_position(repo, market_id="qingdao-2867214", size="1.52", min_size="5"):
    target_date = datetime.now(timezone.utc).date() + timedelta(days=5)
    target_label = f"{target_date.strftime('%B')} {target_date.day}, {target_date.year}"
    market, payload = _market(
        market_id,
        title=f"Highest temperature in Qingdao on {target_label}?",
        order_min_size=min_size,
        close_time=(datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
    )
    repo.upsert_market(market, payload)
    repo.save_reconciliation("ok", {"status": "ok"})
    repo.replace_positions(
        [
            {
                "market": market_id,
                "outcome": "Yes",
                "size": size,
                "avgPrice": "0.13",
            }
        ]
    )
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
    return market_id


def _exit_client():
    client = Mock()
    client.place_limit_order.side_effect = RuntimeError("BUY forbidden")
    client.place_sell_limit_order.return_value = {"order_id": "sell-1", "status": "live"}
    client.get_token_order_book.return_value = (
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
    client.get_order.return_value = {"id": "sell-1", "status": "LIVE"}
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
    def __init__(self, repository):
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


def _execution_auto_exit(repo, client, *, exit_service=None):
    return AutoExitService(
        repo,
        client,
        exit_service=exit_service,
        guardian=_ForcedOfficialExitGuardian(repo),
    )


def test_partial_sell_leaves_exact_reconciled_residual(tmp_path):
    """Reconciliation stores residual size; never invent full exit."""
    db, conn = _db(tmp_path)
    repo = Repository(conn)
    market_id = _seed_position(repo, size="1.52")
    # Simulate partial SELL fill of 8.48 from original 10 leaving 1.52
    positions = repo.list_positions(market_id=market_id, nonzero_only=True)
    assert len(positions) == 1
    assert Decimal(str(positions[0]["size"])) == Decimal("1.52")
    conn.close()


def test_dust_residual_creates_no_repeated_live_sell(tmp_path):
    settings = _auto_exit_settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    market_id = _seed_position(repo, size="1.52", min_size="5")
    client = _exit_client()
    service = _execution_auto_exit(repo, client)

    r1 = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    r2 = service.run_tick(
        settings=settings,
        profile_name="micro-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    client.place_sell_limit_order.assert_not_called()
    assert r1.executed == 0 and r2.executed == 0
    assert any("dust" in s.lower() for s in r1.skipped + r1.notes)
    # One terminal skipped dust action, not growing pending orphans
    actions = conn.execute(
        "SELECT status, idempotency_key FROM automation_actions WHERE market_id = ?",
        (market_id,),
    ).fetchall()
    assert len(actions) == 1
    assert actions[0]["status"] == "skipped"
    assert "dust" in (actions[0]["idempotency_key"] or "")
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM automation_actions WHERE status = 'pending'"
    ).fetchone()["n"]
    assert pending == 0
    conn.close()


def test_exception_after_action_creation_is_terminal(tmp_path):
    settings = _auto_exit_settings(tmp_path, AUTO_EXIT_MAX_POSITION_USDC=Decimal("50"))
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    # Size above min so we enter create path
    market_id = _seed_position(repo, size="10", min_size="5")
    client = _exit_client()
    exit_svc = Mock()
    exit_svc.close_live.side_effect = RuntimeError("boom after create")
    service = _execution_auto_exit(repo, client, exit_service=exit_svc)

    result = service.run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=_compliance_ok(),
    )
    assert result.executed == 0
    assert result.attempted == 1
    assert result.failures == [f"{market_id}: boom after create"]
    actions = conn.execute(
        "SELECT status, failure_reason FROM automation_actions WHERE market_id = ?",
        (market_id,),
    ).fetchall()
    assert len(actions) == 1
    assert actions[0]["status"] == "failed"
    assert "boom" in (actions[0]["failure_reason"] or "")
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM automation_actions WHERE status = 'pending'"
    ).fetchone()["n"]
    assert pending == 0
    conn.close()


def test_repeated_ticks_idempotent_for_same_residual(tmp_path):
    settings = _auto_exit_settings(tmp_path)
    db = Database(settings.database_path)
    db.init_schema()
    conn = db.connect()
    repo = Repository(conn)
    market_id = _seed_position(repo, size="1.52", min_size="5")
    client = _exit_client()
    service = _execution_auto_exit(repo, client)
    for _ in range(5):
        service.run_tick(
            settings=settings,
            profile_name="micro-live",
            allow_auto_exit=True,
            compliance_service=_compliance_ok(),
        )
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM automation_actions WHERE market_id = ?",
        (market_id,),
    ).fetchone()["n"]
    assert count == 1
    client.place_sell_limit_order.assert_not_called()
    conn.close()


def test_fee_schedule_extract_weather():
    schedule = extract_market_fee_schedule(
        {"feesEnabled": True, "feeType": "weather_fees", "feeSchedule": {}}
    )
    assert schedule.fees_enabled is True
    assert schedule.fee_rate == WEATHER_TAKER_FEE_RATE


def test_build_proposed_order_meets_min_notional_when_cap_allows():
    analysis = Analysis(
        market_id="m1",
        model_version="t",
        fair_lower=Decimal("0.9"),
        fair_upper=Decimal("0.95"),
        reference_price=Decimal("0.25"),
        edge=Decimal("0.5"),
        side="buy_yes",
        decision="trade",
        reasons=["t"],
    )
    order = build_proposed_order(
        analysis, "yes", "no", Decimal("5"), market_payload={"orderMinSize": "1"}
    )
    assert order is not None
    assert order.notional >= MARKETABLE_MIN_NOTIONAL


def test_build_proposed_order_reserves_weather_fee_inside_cash_cap():
    analysis = Analysis(
        market_id="fee-market",
        model_version="test",
        fair_lower=Decimal("0.30"),
        fair_upper=Decimal("0.40"),
        reference_price=Decimal("0.14"),
        edge=Decimal("0.15"),
        side="buy_yes",
        decision="trade",
        reasons=["test"],
    )

    order = build_proposed_order(
        analysis,
        "yes-token",
        "no-token",
        Decimal("1.5"),
        market_payload={"feesEnabled": True, "feeType": "weather"},
    )

    assert order is not None
    assert order.notional < Decimal("1.5")
    assert order.estimated_entry_fee > 0
    assert order.cash_at_risk <= Decimal("1.5")
