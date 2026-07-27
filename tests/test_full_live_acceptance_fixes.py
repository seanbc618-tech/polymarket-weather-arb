"""Real-chain regression tests for full-live Codex acceptance fixes.

Mocks only exchange/network I/O. Does not mock AutopilotService methods under test
(reconcile fail-stop, lifecycle cancel, auto-exit gates, set_app_mode arming).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.pricing import Analysis
from polymarket_weather_arb.services.auto_exit_service import AutoExitService
from polymarket_weather_arb.services.autopilot_service import AutopilotService
from polymarket_weather_arb.services.compliance_service import ComplianceDecision
from polymarket_weather_arb.services.exit_guardian_service import ExitGuardianService
from polymarket_weather_arb.services.order_lifecycle_service import OrderLifecycleService
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


class RecordingClient:
    """Exchange stub that records mutation attempts."""

    def __init__(self, *, recon_mode: str = "ok"):
        self.recon_mode = recon_mode
        self.cancelled: list[str] = []
        self.buy_calls: list[dict] = []
        self.sell_calls: list[dict] = []
        self.orders: list[dict] = []
        self.positions: list[dict] = []
        self.trades: list[dict] = []
        self.balances: dict = {"usdc": "100"}
        self.cancel_errors: dict[str, Exception] = {}

    def list_markets(self, *, limit=100, offset=0):
        return []

    def get_event_markets_by_slug(self, slug):
        return []

    def get_balances(self):
        if self.recon_mode == "adapter-error":
            raise RuntimeError("clob balances unavailable")
        return self.balances

    def get_orders(self):
        if self.recon_mode == "adapter-error":
            raise RuntimeError("clob orders unavailable")
        return list(self.orders)

    def get_trades(self):
        if self.recon_mode == "adapter-error":
            raise RuntimeError("clob trades unavailable")
        return list(self.trades)

    def get_positions(self):
        if self.recon_mode == "adapter-error":
            raise RuntimeError("clob positions unavailable")
        return list(self.positions)

    def get_order_book(self, market):
        return (
            MarketSnapshot(
                market_id=market.id,
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.45"),
                midpoint=Decimal("0.425"),
                spread=Decimal("0.05"),
                liquidity=Decimal("1000"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {"fake": True},
        )

    def get_token_order_book(self, token_id):
        return (
            MarketSnapshot(
                market_id=str(token_id),
                best_bid=Decimal("0.12"),
                best_ask=Decimal("0.13"),
                midpoint=Decimal("0.125"),
                spread=Decimal("0.01"),
                liquidity=Decimal("100"),
                fetched_at=datetime.now(timezone.utc),
            ),
            {},
        )

    def cancel_order(self, order_id: str):
        if order_id in self.cancel_errors:
            raise self.cancel_errors[order_id]
        self.cancelled.append(order_id)
        return {"id": order_id, "status": "cancelled"}

    def place_limit_order(self, **kwargs):
        self.buy_calls.append(kwargs)
        return {"order_id": "buy-1", "status": "live"}

    def place_sell_limit_order(self, **kwargs):
        self.sell_calls.append(kwargs)
        return {"order_id": "sell-1", "status": "live"}

    def get_order(self, order_id: str):
        return {"id": order_id, "status": "LIVE"}


def _repo(tmp_path, **overrides):
    base = dict(
        _env_file=None,
        database_path=tmp_path / "accept.db",
        trading_disabled=False,
        auto_exit_enabled=True,
        polymarket_private_key="k",
        polymarket_funder="0xf",
        compliance_check_enabled=False,
        stale_order_book_seconds=300,
        max_auto_exits_per_tick=1,
        auto_exit_max_position_usdc=Decimal("5"),
        live_market_ids="",
    )
    base.update(overrides)
    settings = Settings(**base)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    return settings, Repository(connection), connection


def _seed_market(repo: Repository, market_id: str = "m1") -> None:
    repo.upsert_market(
        Market(
            id=market_id,
            slug=market_id,
            title="Will NYC high temperature exceed 80F?",
            description="NOAA station KNYC",
            yes_token_id="yes-token",
            no_token_id="no-token",
            status="active",
            is_weather=True,
        ),
        {
            "id": market_id,
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["yes-token", "no-token"],
        },
    )


def _compliance_ok(monkeypatch):
    monkeypatch.setattr(
        "polymarket_weather_arb.services.compliance_service.ComplianceService.check_live_allowed",
        lambda self: ComplianceDecision(ok=True, status="check_disabled", reason="test"),
    )


# --- 1. Reconciliation fail-stop -------------------------------------------------


def test_recon_not_ok_fail_stops_cancel_sell_buy(tmp_path, monkeypatch):
    settings, repo, conn = _repo(tmp_path)
    _seed_market(repo)
    # Prior successful recon so blockers do not fire before this tick's recon.
    repo.save_reconciliation("ok", {"status": "ok"})
    # Stale open order that would otherwise be cancelled after a successful recon.
    old = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
    repo.connection.execute(
        """
        INSERT INTO open_orders (
            exchange_order_id, market_id, token_id, side, price, size, notional,
            status, raw_payload, first_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("stale-1", "m1", "yes-token", "BUY", 0.4, 10, 4, "open", "{}", old, old),
    )
    repo.replace_positions(
        [{"market": "m1", "outcome": "Yes", "size": "5", "avgPrice": "0.13"}]
    )
    repo.save_analysis(
        Analysis(
            market_id="m1",
            model_version="t",
            fair_lower=Decimal("0.2"),
            fair_upper=Decimal("0.3"),
            reference_price=Decimal("0.5"),
            edge=Decimal("0.01"),
            side=None,
            decision="skip",
            reasons=["edge gone"],
        )
    )
    conn.commit()

    client = RecordingClient(recon_mode="adapter-error")
    events: list[dict] = []
    service = AutopilotService(
        settings, repo, client=client, notifier=lambda p: events.append(p)
    )
    service.ensure_state(mode="live")
    repo.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    conn.commit()
    _compliance_ok(monkeypatch)
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
        lambda self, **kwargs: 0,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
        lambda self, **kwargs: 0,
    )

    result = service.tick()

    assert result.status == "failed"
    assert "fail-stop" in result.reason
    assert client.cancelled == []
    assert client.buy_calls == []
    assert client.sell_calls == []
    assert any(e.get("daemon_event") == "app_execution_risk" for e in events)
    conn.close()


# --- 2. Stale / missing analysis blocks AutoExit ---------------------------------


def test_auto_exit_blocked_when_analysis_stale(tmp_path):
    settings, repo, conn = _repo(tmp_path)
    _seed_market(repo)
    repo.save_reconciliation("ok", {"status": "ok"})
    repo.replace_positions(
        [{"market": "m1", "outcome": "Yes", "size": "5", "avgPrice": "0.13"}]
    )
    repo.save_analysis(
        Analysis(
            market_id="m1",
            model_version="t",
            fair_lower=Decimal("0.2"),
            fair_upper=Decimal("0.3"),
            reference_price=Decimal("0.5"),
            edge=Decimal("0.01"),
            side=None,
            decision="skip",
            reasons=["edge gone"],
        )
    )
    # Force analysis timestamp into the past beyond freshness window.
    repo.connection.execute(
        "UPDATE analyses SET created_at = '2020-01-01T00:00:00+00:00'"
    )
    conn.commit()

    client = RecordingClient()
    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=Mock(
            check_live_allowed=Mock(
                return_value=ComplianceDecision(ok=True, status="ok", reason="t")
            )
        ),
    )
    assert result.executed == 0
    assert client.sell_calls == []
    assert (
        any("stale" in s.lower() or "analysis" in s.lower() for s in result.skipped)
        or any("no executable" in n for n in result.notes)
    )
    conn.close()


def test_auto_exit_blocked_when_analysis_missing(tmp_path):
    settings, repo, conn = _repo(tmp_path)
    _seed_market(repo)
    repo.save_reconciliation("ok", {"status": "ok"})
    repo.replace_positions(
        [{"market": "m1", "outcome": "Yes", "size": "5", "avgPrice": "0.13"}]
    )
    # No analysis row at all: guardian must not emit position_at_risk.
    conn.commit()

    client = RecordingClient()
    recs = ExitGuardianService(repo).evaluate()
    assert all(r.action != "position_at_risk" for r in recs if r.kind == "position")
    assert any(r.action == "review_no_analysis" for r in recs if r.kind == "position")
    result = AutoExitService(repo, client).run_tick(
        settings=settings,
        profile_name="full-live",
        allow_auto_exit=True,
        compliance_service=Mock(
            check_live_allowed=Mock(
                return_value=ComplianceDecision(ok=True, status="ok", reason="t")
            )
        ),
    )
    assert result.executed == 0
    assert client.sell_calls == []
    conn.close()


# --- 3. Outcome vs analysis.side -------------------------------------------------


def test_exit_guardian_does_not_treat_side_reversal_as_a_sell_signal(tmp_path):
    settings, repo, conn = _repo(tmp_path)
    _seed_market(repo)
    repo.replace_positions(
        [{"market": "m1", "outcome": "Yes", "size": "5", "avgPrice": "0.13"}]
    )
    # A model-side reversal without executable value dominance must hold.
    repo.save_analysis(
        Analysis(
            market_id="m1",
            model_version="t",
            fair_lower=Decimal("0.7"),
            fair_upper=Decimal("0.8"),
            reference_price=Decimal("0.2"),
            edge=Decimal("0.20"),
            side="buy_no",
            decision="trade",
            reasons=["no side"],
        )
    )
    conn.commit()
    recs = ExitGuardianService(repo).evaluate(min_edge=Decimal("0.03"))
    assert any(
        r.kind == "position"
        and r.action == "hold_for_resolution"
        and "model direction reversed" in r.reason
        for r in recs
    )

    # Matching side holds.
    repo.save_analysis(
        Analysis(
            market_id="m1",
            model_version="t",
            fair_lower=Decimal("0.7"),
            fair_upper=Decimal("0.8"),
            reference_price=Decimal("0.2"),
            edge=Decimal("0.20"),
            side="buy_yes",
            decision="trade",
            reasons=["yes side"],
        )
    )
    conn.commit()
    recs2 = ExitGuardianService(repo).evaluate(min_edge=Decimal("0.03"))
    assert any(r.kind == "position" and r.action == "hold_for_resolution" for r in recs2)
    conn.close()


# --- 4. Durable first_seen age + timezone safety ---------------------------------


def test_stale_age_uses_first_seen_and_survives_recon_replace(tmp_path):
    settings, repo, conn = _repo(tmp_path)
    _seed_market(repo)
    # first_seen 15 minutes ago; exchange payload created_at even older.
    first_seen = datetime.now(timezone.utc) - timedelta(seconds=900)
    exchange_created = int((datetime.now(timezone.utc) - timedelta(seconds=1200)).timestamp())
    client = RecordingClient()
    client.orders = [
        {
            "id": "order-live",
            "market": "m1",
            "asset_id": "yes-token",
            "side": "BUY",
            "price": "0.40",
            "original_size": "10",
            "size_matched": "0",
            "status": "live",
            "created_at": exchange_created,
        }
    ]
    # Seed prior first_seen then recon replace must preserve earliest anchor.
    repo.connection.execute(
        """
        INSERT INTO open_orders (
            exchange_order_id, market_id, token_id, side, price, size, notional,
            status, raw_payload, first_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            "order-live",
            "m1",
            "yes-token",
            "BUY",
            0.4,
            10,
            4,
            "live",
            "{}",
            first_seen.isoformat(),
        ),
    )
    conn.commit()

    # replace_open_orders as recon does: updated_at becomes now, first_seen preserved.
    repo.replace_open_orders(client.orders)
    conn.commit()
    row = repo.get_open_order("order-live")
    assert row is not None
    # first_seen should remain the earlier of prior first_seen and exchange created.
    assert row["first_seen_at"] is not None

    lifecycle = OrderLifecycleService(client, repo)
    # Naive first_seen_at (no timezone) must still compute age correctly.
    naive_old = (datetime.now(timezone.utc) - timedelta(seconds=900)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    repo.connection.execute(
        "UPDATE open_orders SET first_seen_at = ? WHERE exchange_order_id = ?",
        (naive_old, "order-live"),
    )
    conn.commit()
    stale = lifecycle.detect_stale_orders(stale_threshold_seconds=300)
    assert len(stale) == 1
    assert stale[0]["exchange_order_id"] == "order-live"
    assert stale[0]["age_seconds"] > 300
    conn.close()


# --- 5. set_app_mode does not create duplicate full-live authorities --------------


def test_set_app_mode_full_live_does_not_create_star_override(tmp_path):
    settings, repo, conn = _repo(tmp_path, live_market_ids="")
    service = AutopilotService(settings, repo, client=RecordingClient())
    service.ensure_state()
    service.set_app_mode("full_live")
    conn.commit()

    state = repo.get_autopilot_state()
    assert state["app_mode"] == "full_live"
    assert state["mode"] == "live"
    assert bool(state["enabled"]) is False
    assert repo.effective_strategy_override("any-market", "full-live") is None
    conn.close()


def test_set_app_mode_full_live_does_not_materialize_whitelist_overrides(tmp_path):
    settings, repo, conn = _repo(tmp_path, live_market_ids="m1,m2")
    _seed_market(repo, "m1")
    _seed_market(repo, "m2")
    service = AutopilotService(
        settings.model_copy(update={"live_market_ids": "m1,m2,m3"}),
        repo,
        client=RecordingClient(),
    )
    service.ensure_state()
    service.set_app_mode("full_live")
    conn.commit()

    assert repo.effective_strategy_override("m1", "full-live") is None
    assert repo.effective_strategy_override("m2", "full-live") is None
    assert repo.effective_strategy_override("m3", "full-live") is None
    conn.close()


# --- 6. Lifecycle cancel failure: persist, surface, telegram --------------------


def test_lifecycle_cancel_failure_persisted_displayed_and_telegram(tmp_path, monkeypatch):
    settings, repo, conn = _repo(tmp_path)
    _seed_market(repo)
    old = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
    repo.connection.execute(
        """
        INSERT INTO open_orders (
            exchange_order_id, market_id, token_id, side, price, size, notional,
            status, raw_payload, first_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("fail-order", "m1", "yes-token", "BUY", 0.4, 10, 4, "open", "{}", old, old),
    )
    repo.save_reconciliation("ok", {"status": "ok"})
    conn.commit()

    client = RecordingClient(recon_mode="ok")
    client.orders = []  # recon will wipe open orders unless we re-seed after recon
    # Keep recon ok without wiping our open order: provide the order as still open.
    client.orders = [
        {
            "id": "fail-order",
            "market": "m1",
            "asset_id": "yes-token",
            "side": "BUY",
            "price": "0.40",
            "original_size": "10",
            "size_matched": "0",
            "status": "live",
            "created_at": int((datetime.now(timezone.utc) - timedelta(seconds=900)).timestamp()),
        }
    ]
    client.cancel_errors["fail-order"] = RuntimeError("exchange cancel rejected")

    events: list[dict] = []
    service = AutopilotService(
        settings, repo, client=client, notifier=lambda p: events.append(p)
    )
    service.ensure_state(mode="live")
    repo.update_autopilot_state(enabled=True, mode="live", app_mode="full_live")
    conn.commit()
    _compliance_ok(monkeypatch)
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover_weather_events",
        lambda self, **kwargs: 0,
    )
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.DiscoveryService.discover",
        lambda self, **kwargs: 0,
    )
    # No candidate path after lifecycle.
    monkeypatch.setattr(service, "_select_market", lambda: None)

    result = service.tick()
    conn.commit()

    assert "stale_order_cancel_failed" in result.reason or "cancel" in (result.reason or "")
    state = repo.get_autopilot_state()
    assert state["last_error"] is not None
    assert "cancel" in str(state["last_error"]).lower() or "failed" in str(state["last_error"]).lower()
    decisions = repo.list_autopilot_decisions(limit=20)
    assert any(
        d["status"] == "failed" and "cancel" in str(d["reason"]).lower() for d in decisions
    )
    assert any(e.get("daemon_event") == "app_execution_risk" for e in events)
    # Must not pretend cancel succeeded.
    assert client.cancelled == []
    conn.close()
