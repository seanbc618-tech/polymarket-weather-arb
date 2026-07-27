"""Phase 3 pulse integration: stream signals stay serial; no second strategy path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from polymarket_weather_arb.adapters.polymarket.stream import StreamQuote, StreamUserHint
from polymarket_weather_arb.config import Settings
from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.domain.weather import ForecastSnapshot
from polymarket_weather_arb.services.autopilot_service import (
    AutopilotPulseState,
    AutopilotService,
)
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


_TEST_TARGET_DATE = (datetime.now(timezone.utc) + timedelta(days=1)).date()
_TEST_TARGET_ISO = _TEST_TARGET_DATE.isoformat()
_TEST_TARGET_LABEL = f"{_TEST_TARGET_DATE:%B} {_TEST_TARGET_DATE.day}, {_TEST_TARGET_DATE.year}"


class FakeBridge:
    def __init__(self) -> None:
        self._batch = SimpleNamespace(
            quotes={},
            reconcile_due=False,
            user_hints=[],
            tick_size=[],
            resolved=[],
            dropped=0,
            coalesced=0,
        )
        self._token_map: dict[str, str] = {}
        self._fresh: set[str] = set()
        self._backfill: set[str] = set()
        self.desired: tuple[str, ...] = ()
        self.set_calls = 0

    def drain(self):
        batch = self._batch
        self._batch = SimpleNamespace(
            quotes={},
            reconcile_due=False,
            user_hints=[],
            tick_size=[],
            resolved=[],
            dropped=0,
            coalesced=0,
        )
        return batch

    def token_to_market(self):
        return dict(self._token_map)

    def is_token_fresh(self, token_id: str) -> bool:
        token = str(token_id)
        if token in self._backfill:
            return False
        return token in self._fresh

    def needs_rest_backfill(self, token_id: str) -> bool:
        return str(token_id) in self._backfill

    def mark_rest_verified(self, token_id: str) -> None:
        self._backfill.discard(str(token_id))

    def set_desired_tokens(self, token_ids, *, token_to_market=None):
        self.set_calls += 1
        self.desired = tuple(token_ids)
        if token_to_market:
            self._token_map.update(token_to_market)
        return True

    def push_quote(self, token_id: str, market_id: str, bid: str, ask: str) -> None:
        self._token_map[token_id] = market_id
        self._batch.quotes[token_id] = StreamQuote(
            token_id=token_id,
            best_bid=Decimal(bid),
            best_ask=Decimal(ask),
            midpoint=(Decimal(bid) + Decimal(ask)) / 2,
            spread=Decimal(ask) - Decimal(bid),
            liquidity=None,
            received_at=1.0,
            source_type="best_bid_ask",
        )
        self._fresh.add(token_id)

    def push_user(self) -> None:
        self._batch.reconcile_due = True
        self._batch.user_hints.append(
            StreamUserHint(kind="trade", received_at=1.0, event_type="trade", status="MATCHED")
        )


def _settings(tmp_path, **updates) -> Settings:
    base = dict(
        _env_file=None,
        database_path=tmp_path / "stream-pulse.db",
        MAX_ORDER_USDC=Decimal("1"),
        MAX_DAILY_USDC=Decimal("5"),
        MAX_MARKET_USDC=Decimal("2"),
        MIN_EDGE=Decimal("0.05"),
        TRADING_DISABLED=True,
    )
    base.update(updates)
    return Settings(**base)


def _repo(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.init_schema()
    connection = database.connect()
    return settings, Repository(connection), connection


def _seed_group(repository: Repository, connection, *, market_id: str, city: str) -> None:
    title = f"Highest temperature in {city} on {_TEST_TARGET_LABEL} 84-85°F"
    repository.upsert_market(
        Market(
            id=market_id,
            title=title,
            description="Resolution source Wunderground.",
            yes_token_id=f"y-{market_id}",
            no_token_id=f"n-{market_id}",
            is_weather=True,
        ),
        {"id": market_id},
        module_id="global_temp_bucket",
    )
    now = datetime.now(timezone.utc)
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id=market_id,
            best_bid=Decimal("0.10"),
            best_ask=Decimal("0.12"),
            midpoint=Decimal("0.11"),
            spread=Decimal("0.02"),
            liquidity=Decimal("100"),
            fetched_at=now,
            token_id=f"y-{market_id}",
        ),
        {"source": "test"},
        token_id=f"y-{market_id}",
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO temperature_bucket_rules (
            market_id, module_id, city, target_date, variable, unit,
            bucket_lower_c, bucket_upper_c, bucket_center_c, source,
            settlement_timezone, confidence, tradable, raw_text
        ) VALUES (?, 'global_temp_bucket', ?, ?, 'temperature_max', 'F',
                  28.8, 29.5, 29.2, 'wunderground',
                  'America/New_York', 0.9, 1, ?)
        """,
        (market_id, city, _TEST_TARGET_ISO, title),
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO market_candidates
        (market_id, module_id, status, tradable, best_bid, best_ask, notes)
        VALUES (?, 'global_temp_bucket', 'dry_run_ready', 1, 0.10, 0.12, 'ready')
        """,
        (market_id,),
    )
    members = {
        "ecmwf_ifs025": [84.0, 85.0, 86.0],
        "gfs_seamless": [83.0, 84.0, 85.0],
        "icon_seamless": [84.0, 84.5, 85.0],
    }
    forecast = ForecastSnapshot(
        provider="open-meteo-ensemble",
        variable="temperature_max",
        value=Decimal("84.5"),
        unit="F",
        issue_time=now - timedelta(minutes=10),
        valid_time=now + timedelta(hours=6),
        market_id=market_id,
        location=city,
        lower_value=Decimal("83"),
        upper_value=Decimal("86"),
        fetched_at=now - timedelta(minutes=5),
    )
    repository.save_forecast(
        forecast,
        {
            "model_members": members,
            "target_date": _TEST_TARGET_ISO,
            "revision": "rev-1",
            "cache_status": "network_fresh",
        },
    )
    connection.commit()


def test_user_event_marks_capital_due_without_writing_fills(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="micro_live")
    client = Mock()
    service = AutopilotService(settings, repository, client=client)
    bridge = FakeBridge()
    bridge.push_user()
    pulse_state = AutopilotPulseState()
    # Capital will try recon; make it fail so we prove no fill write and fail-stop.
    client.get_balances.side_effect = RuntimeError("no network")
    # Minimal recon path — patch ReconciliationService via client methods if needed.
    # Use a dry path: capital_due from stream, but live recon uses client.
    fills_before = connection.execute("SELECT COUNT(*) AS c FROM fills").fetchone()["c"]
    # Force capital path by setting next times far and mutation flag via stream.
    pulse_state.next_capital_at = 1e18
    pulse_state.next_exit_at = 1e18
    pulse_state.next_discovery_at = 1e18
    pulse_state.next_weather_refresh_at = 1e18
    pulse_state.next_reprice_at = 1e18
    # ReconciliationService will fail without full mocks — still must not write fills.
    try:
        service.pulse(pulse_state, monotonic=lambda: 100.0, stream_bridge=bridge)
    except Exception:
        pass
    fills_after = connection.execute("SELECT COUNT(*) AS c FROM fills").fetchone()["c"]
    assert fills_after == fills_before
    assert pulse_state.capital_due_after_mutation is True or pulse_state.last_path == "capital"
    connection.close()


def test_stream_quote_persists_token_and_schedules_group_reprice(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="dry_run", app_mode="paper")
    _seed_group(repository, connection, market_id="m-seoul", city="Seoul")
    client = Mock()
    # Should not REST when stream quote applied and reprice uses fresh token.
    client.get_token_order_book.side_effect = AssertionError("REST book should be skipped")
    service = AutopilotService(settings, repository, client=client)
    bridge = FakeBridge()
    bridge.push_quote("y-m-seoul", "m-seoul", "0.20", "0.22")
    pulse_state = AutopilotPulseState()
    pulse_state.next_capital_at = 1e18
    pulse_state.next_exit_at = 1e18
    pulse_state.next_discovery_at = 1e18
    pulse_state.next_weather_refresh_at = 1e18
    pulse_state.next_reprice_at = 0.0  # allow reprice
    # First pulse: apply quote (dry_run so capital not due).
    service.pulse(pulse_state, monotonic=lambda: 10.0, stream_bridge=bridge)
    row = repository.latest_market_snapshot("m-seoul", token_id="y-m-seoul")
    assert row is not None
    assert float(row["best_bid"]) == 0.20
    assert row["token_id"] == "y-m-seoul"
    assert ("Seoul", _TEST_TARGET_ISO) in pulse_state.pending_reprice_groups or (
        pulse_state.last_path == "cached_reprice"
    )
    connection.close()


def test_unchanged_bbo_does_not_schedule_reprice(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="dry_run", app_mode="paper")
    _seed_group(repository, connection, market_id="m1", city="Paris")
    service = AutopilotService(settings, repository, client=Mock())
    bridge = FakeBridge()
    # Seed already has 0.10/0.12 for y-m1
    bridge.push_quote("y-m1", "m1", "0.10", "0.12")
    pulse_state = AutopilotPulseState()
    pulse_state.next_capital_at = 1e18
    pulse_state.next_exit_at = 1e18
    pulse_state.next_discovery_at = 1e18
    pulse_state.next_weather_refresh_at = 1e18
    pulse_state.next_reprice_at = 1e18
    service.pulse(pulse_state, monotonic=lambda: 5.0, stream_bridge=bridge)
    assert ("Paris", _TEST_TARGET_ISO) not in pulse_state.pending_reprice_groups
    connection.close()


def test_changed_bbo_queues_group_without_bypassing_reprice_budget(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="dry_run", app_mode="paper")
    _seed_group(repository, connection, market_id="m1", city="Seoul")
    client = Mock()
    service = AutopilotService(settings, repository, client=client)
    bridge = FakeBridge()
    bridge.push_quote("y-m1", "m1", "0.20", "0.22")
    pulse_state = AutopilotPulseState()
    pulse_state.next_capital_at = 1e18
    pulse_state.next_exit_at = 1e18
    pulse_state.next_discovery_at = 1e18
    pulse_state.next_weather_refresh_at = 1e18
    pulse_state.next_reprice_at = 100.0

    result = service.pulse(pulse_state, monotonic=lambda: 10.0, stream_bridge=bridge)

    assert result.status == "idle"
    assert pulse_state.last_path == "health"
    assert ("Seoul", _TEST_TARGET_ISO) in pulse_state.pending_reprice_groups
    client.get_token_order_book.assert_not_called()
    connection.close()


def test_duplicate_user_events_collapse_to_one_capital_flag(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="live", app_mode="paper")
    service = AutopilotService(settings, repository, client=Mock())
    bridge = FakeBridge()
    bridge.push_user()
    bridge.push_user()
    pulse_state = AutopilotPulseState()
    service._ingest_stream_signals(pulse_state, bridge)
    assert pulse_state.capital_due_after_mutation is True
    # Second drain empty — flag remains until capital path clears it.
    service._ingest_stream_signals(pulse_state, bridge)
    assert pulse_state.capital_due_after_mutation is True
    connection.close()


def test_stream_fresh_token_skips_rest_book_read(tmp_path):
    import time

    settings, repository, connection = _repo(tmp_path)
    _seed_group(repository, connection, market_id="m2", city="Berlin")
    client = Mock()
    service = AutopilotService(settings, repository, client=client)
    bridge = FakeBridge()
    bridge._fresh.add("y-m2")
    bridge._token_map["y-m2"] = "m2"
    # Skip only after a recent REST verify and while stream BBO is still fresh.
    pulse_state = AutopilotPulseState()
    pulse_state.stream_rest_verified_at["y-m2"] = time.monotonic()
    failures = service._refresh_group_order_books(
        ["m2"], pulse_state=pulse_state, stream_bridge=bridge
    )
    assert failures == []
    assert pulse_state.stream_rest_skips == 1
    assert pulse_state.stream_rest_reads == 0
    client.get_token_order_book.assert_not_called()
    connection.close()


def test_stream_rest_verification_survives_one_fair_rotation(tmp_path, monkeypatch):
    settings, repository, connection = _repo(tmp_path)
    _seed_group(repository, connection, market_id="m-rotation", city="Berlin")
    client = Mock()
    service = AutopilotService(settings, repository, client=client)
    bridge = FakeBridge()
    bridge._fresh.add("y-m-rotation")
    bridge._token_map["y-m-rotation"] = "m-rotation"
    pulse_state = AutopilotPulseState()
    pulse_state.stream_rest_verified_at["y-m-rotation"] = 400.0
    monkeypatch.setattr(
        "polymarket_weather_arb.services.autopilot_service.time.monotonic",
        lambda: 1000.0,
    )

    failures = service._refresh_group_order_books(
        ["m-rotation"], pulse_state=pulse_state, stream_bridge=bridge
    )

    assert failures == []
    assert pulse_state.stream_rest_skips == 1
    assert pulse_state.stream_rest_reads == 0
    client.get_token_order_book.assert_not_called()
    connection.close()


def test_recent_persisted_stream_quote_survives_subscription_rotation(tmp_path):
    import time

    settings, repository, connection = _repo(tmp_path)
    _seed_group(repository, connection, market_id="m-rotated", city="Berlin")
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id="m-rotated",
            best_bid=Decimal("0.10"),
            best_ask=Decimal("0.12"),
            midpoint=Decimal("0.11"),
            spread=Decimal("0.02"),
            liquidity=Decimal("10"),
            fetched_at=datetime.now(timezone.utc),
            token_id="y-m-rotated",
        ),
        {
            "source": "polymarket_stream",
            "source_type": "best_bid_ask",
            "token_id": "y-m-rotated",
        },
        token_id="y-m-rotated",
    )
    client = Mock()
    service = AutopilotService(settings, repository, client=client)
    bridge = FakeBridge()
    pulse_state = AutopilotPulseState()
    pulse_state.stream_rest_verified_at["y-m-rotated"] = time.monotonic()

    failures = service._refresh_group_order_books(
        ["m-rotated"],
        pulse_state=pulse_state,
        stream_bridge=bridge,
    )

    assert failures == []
    assert pulse_state.stream_rest_skips == 1
    assert pulse_state.stream_rest_reads == 0
    client.get_token_order_book.assert_not_called()
    connection.close()


def test_stream_periodic_rest_verify_and_backfill(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    _seed_group(repository, connection, market_id="m3", city="Oslo")
    client = Mock()
    client.get_token_order_book.return_value = (
        MarketSnapshot(
            market_id="m3",
            best_bid=Decimal("0.10"),
            best_ask=Decimal("0.12"),
            midpoint=Decimal("0.11"),
            spread=Decimal("0.02"),
            liquidity=Decimal("1"),
            fetched_at=datetime.now(timezone.utc),
            token_id="y-m3",
        ),
        {"token_id": "y-m3"},
    )
    service = AutopilotService(settings, repository, client=client)
    bridge = FakeBridge()
    bridge._fresh.add("y-m3")
    bridge._backfill.add("y-m3")
    pulse_state = AutopilotPulseState()
    failures = service._refresh_group_order_books(
        ["m3"], pulse_state=pulse_state, stream_bridge=bridge
    )
    assert failures == []
    assert pulse_state.stream_rest_reads == 1
    assert "y-m3" not in bridge._backfill
    client.get_token_order_book.assert_called_once()
    connection.close()


def test_tick_once_does_not_require_stream_bridge(tmp_path):
    settings, repository, connection = _repo(tmp_path)
    repository.update_autopilot_state(enabled=True, mode="dry_run", app_mode="observe")
    service = AutopilotService(settings, repository, client=Mock())
    # Full tick path must remain usable for --once without starting WebSockets.
    result = service.tick()
    assert result is not None
    connection.close()
