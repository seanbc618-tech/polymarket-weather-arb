"""Token-aware market_snapshots: YES/NO isolation and legacy NULL handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_weather_arb.domain.markets import Market, MarketSnapshot
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def _repo(tmp_path):
    database = Database(tmp_path / "token-snap.db")
    database.init_schema()
    connection = database.connect()
    return Repository(connection), connection


def _seed_market(repository: Repository, market_id: str = "m1") -> None:
    repository.upsert_market(
        Market(
            id=market_id,
            title="Highest temperature in NYC on July 20, 2026 84-85°F",
            yes_token_id="yes-token-1",
            no_token_id="no-token-1",
            is_weather=True,
        ),
        {"id": market_id},
        module_id="global_temp_bucket",
    )


def test_schema_adds_token_id_and_index(tmp_path):
    repository, connection = _repo(tmp_path)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(market_snapshots)")}
    assert "token_id" in columns
    indexes = {row["name"] for row in connection.execute("PRAGMA index_list(market_snapshots)")}
    assert "idx_market_snapshots_market_token_fetched" in indexes
    connection.close()


def test_yes_and_no_snapshots_do_not_overwrite(tmp_path):
    repository, connection = _repo(tmp_path)
    _seed_market(repository)
    now = datetime.now(timezone.utc)
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id="m1",
            best_bid=Decimal("0.40"),
            best_ask=Decimal("0.42"),
            midpoint=Decimal("0.41"),
            spread=Decimal("0.02"),
            liquidity=Decimal("10"),
            fetched_at=now,
            token_id="yes-token-1",
        ),
        {"side": "yes"},
        token_id="yes-token-1",
    )
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id="m1",
            best_bid=Decimal("0.55"),
            best_ask=Decimal("0.58"),
            midpoint=Decimal("0.565"),
            spread=Decimal("0.03"),
            liquidity=Decimal("12"),
            fetched_at=now,
            token_id="no-token-1",
        ),
        {"side": "no"},
        token_id="no-token-1",
    )
    yes = repository.latest_market_snapshot("m1", token_id="yes-token-1")
    no = repository.latest_market_snapshot("m1", token_id="no-token-1")
    assert yes is not None and float(yes["best_bid"]) == 0.40
    assert no is not None and float(no["best_bid"]) == 0.55
    assert yes["token_id"] == "yes-token-1"
    assert no["token_id"] == "no-token-1"
    connection.close()


def test_identical_snapshot_payload_is_coalesced(tmp_path):
    repository, connection = _repo(tmp_path)
    _seed_market(repository)
    now = datetime.now(timezone.utc)
    snapshot = MarketSnapshot(
        market_id="m1",
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.42"),
        midpoint=Decimal("0.41"),
        spread=Decimal("0.02"),
        liquidity=Decimal("10"),
        fetched_at=now,
        token_id="yes-token-1",
    )

    repository.save_market_snapshot(snapshot, {"source": "same"})
    repository.save_market_snapshot(snapshot, {"source": "same"})

    assert connection.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 1
    connection.close()


def test_unchanged_quote_advances_freshness_without_appending_history(tmp_path):
    repository, connection = _repo(tmp_path)
    _seed_market(repository)
    first_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    second_at = datetime.now(timezone.utc)
    for fetched_at, sequence in ((first_at, 1), (second_at, 2)):
        repository.save_market_snapshot(
            MarketSnapshot(
                market_id="m1",
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.42"),
                midpoint=Decimal("0.41"),
                spread=Decimal("0.02"),
                liquidity=Decimal("10"),
                fetched_at=fetched_at,
                token_id="yes-token-1",
            ),
            {"source": "gamma-summary", "sequence": sequence},
        )

    rows = connection.execute(
        "SELECT fetched_at, raw_payload FROM market_snapshots"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["fetched_at"] == second_at.isoformat()
    assert '"sequence": 2' in rows[0]["raw_payload"]
    connection.close()


def test_identical_quotes_from_different_sources_remain_distinct(tmp_path):
    repository, connection = _repo(tmp_path)
    _seed_market(repository)
    now = datetime.now(timezone.utc)
    snapshot = MarketSnapshot(
        market_id="m1",
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.42"),
        midpoint=Decimal("0.41"),
        spread=Decimal("0.02"),
        liquidity=Decimal("10"),
        fetched_at=now,
        token_id="yes-token-1",
    )
    repository.save_market_snapshot(snapshot, {"source": "gamma-summary"})
    repository.save_market_snapshot(snapshot, {"source": "polymarket_stream"})

    assert connection.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 2
    connection.close()


def test_snapshot_retention_keeps_latest_token_row(tmp_path):
    repository, connection = _repo(tmp_path)
    _seed_market(repository)
    now = datetime.now(timezone.utc)
    for age_days, bid in ((10, "0.10"), (9, "0.20"), (0, "0.30")):
        repository.save_market_snapshot(
            MarketSnapshot(
                market_id="m1",
                best_bid=Decimal(bid),
                best_ask=Decimal("0.40"),
                midpoint=Decimal("0.35"),
                spread=Decimal("0.10"),
                liquidity=Decimal("10"),
                fetched_at=now - timedelta(days=age_days),
                token_id="yes-token-1",
            ),
            {"age_days": age_days},
        )

    pruned = repository.prune_old_market_snapshots(keep_days=7, batch_size=10)

    rows = connection.execute(
        "SELECT best_bid FROM market_snapshots ORDER BY fetched_at"
    ).fetchall()
    assert pruned == 2
    assert [float(row["best_bid"]) for row in rows] == [0.3]
    connection.close()


def test_token_lookup_ignores_legacy_null_rows(tmp_path):
    repository, connection = _repo(tmp_path)
    _seed_market(repository)
    now = datetime.now(timezone.utc)
    # Legacy row without token_id.
    connection.execute(
        """
        INSERT INTO market_snapshots (
            market_id, token_id, best_bid, best_ask, midpoint, spread, liquidity,
            fetched_at, raw_payload
        ) VALUES ('m1', NULL, 0.11, 0.12, 0.115, 0.01, 1, ?, '{}')
        """,
        (now.isoformat(),),
    )
    connection.commit()
    # Token-specific lookup must not fall back to the NULL legacy row.
    assert repository.latest_market_snapshot("m1", token_id="yes-token-1") is None
    # Unscoped lookup still sees history.
    legacy = repository.latest_market_snapshot("m1")
    assert legacy is not None
    assert legacy["token_id"] is None
    connection.close()


def test_save_accepts_token_from_snapshot_or_kwarg(tmp_path):
    repository, connection = _repo(tmp_path)
    _seed_market(repository)
    now = datetime.now(timezone.utc)
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id="m1",
            best_bid=Decimal("0.2"),
            best_ask=Decimal("0.21"),
            midpoint=Decimal("0.205"),
            spread=Decimal("0.01"),
            liquidity=None,
            fetched_at=now,
            token_id="from-snapshot",
        ),
        {},
    )
    row = repository.latest_market_snapshot("m1", token_id="from-snapshot")
    assert row is not None
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id="m1",
            best_bid=Decimal("0.3"),
            best_ask=Decimal("0.31"),
            midpoint=Decimal("0.305"),
            spread=Decimal("0.01"),
            liquidity=None,
            fetched_at=now,
        ),
        {},
        token_id="from-kwarg",
    )
    row2 = repository.latest_market_snapshot("m1", token_id="from-kwarg")
    assert row2 is not None
    assert float(row2["best_bid"]) == 0.3
    connection.close()


def test_pricing_snapshot_ignores_other_outcome_token(tmp_path):
    """Core pricing must not treat a NO quote as the YES market price."""
    repository, connection = _repo(tmp_path)
    _seed_market(repository)
    now = datetime.now(timezone.utc)
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id="m1",
            best_bid=Decimal("0.10"),
            best_ask=Decimal("0.12"),
            midpoint=Decimal("0.11"),
            spread=Decimal("0.02"),
            liquidity=None,
            fetched_at=now,
            token_id="yes-token-1",
        ),
        {"side": "yes"},
        token_id="yes-token-1",
    )
    # Newer NO quote must not win unscoped-looking pricing reads.
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id="m1",
            best_bid=Decimal("0.88"),
            best_ask=Decimal("0.90"),
            midpoint=Decimal("0.89"),
            spread=Decimal("0.02"),
            liquidity=None,
            fetched_at=now,
            token_id="no-token-1",
        ),
        {"side": "no"},
        token_id="no-token-1",
    )
    unscoped = repository.latest_market_snapshot("m1")
    assert unscoped is not None
    assert unscoped["token_id"] == "no-token-1"
    assert float(unscoped["best_ask"]) == 0.90

    yes = repository.latest_pricing_snapshot("m1")
    assert yes is not None
    assert yes["token_id"] == "yes-token-1"
    assert float(yes["best_ask"]) == 0.12

    no = repository.latest_pricing_snapshot("m1", outcome="NO")
    assert no is not None
    assert float(no["best_ask"]) == 0.90

    # Missing YES token data does not fall back to the other outcome's quote.
    connection.execute("DELETE FROM market_snapshots WHERE token_id = 'yes-token-1'")
    connection.commit()
    assert repository.latest_pricing_snapshot("m1") is None
    # Still sees NO when asked explicitly.
    assert repository.latest_pricing_snapshot("m1", outcome="NO") is not None
    connection.close()


def test_pricing_snapshot_freshness_is_side_specific(tmp_path):
    """Stale YES must not inherit a fresher NO timestamp (manual trade gate)."""
    from datetime import timedelta

    repository, connection = _repo(tmp_path)
    _seed_market(repository)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    new = datetime.now(timezone.utc)
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id="m1",
            best_bid=Decimal("0.10"),
            best_ask=Decimal("0.12"),
            midpoint=Decimal("0.11"),
            spread=Decimal("0.02"),
            liquidity=None,
            fetched_at=old,
            token_id="yes-token-1",
        ),
        {},
        token_id="yes-token-1",
    )
    repository.save_market_snapshot(
        MarketSnapshot(
            market_id="m1",
            best_bid=Decimal("0.88"),
            best_ask=Decimal("0.90"),
            midpoint=Decimal("0.89"),
            spread=Decimal("0.02"),
            liquidity=None,
            fetched_at=new,
            token_id="no-token-1",
        ),
        {},
        token_id="no-token-1",
    )
    unscoped = repository.latest_market_snapshot("m1")
    assert unscoped is not None
    assert unscoped["token_id"] == "no-token-1"
    yes = repository.latest_pricing_snapshot("m1", side="buy_yes")
    assert yes is not None
    assert yes["token_id"] == "yes-token-1"
    assert yes["fetched_at"].startswith(old.isoformat()[:19])
    connection.close()


def test_legacy_db_repairs_token_column(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy-snap.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE markets (
                id TEXT PRIMARY KEY,
                slug TEXT,
                title TEXT NOT NULL,
                description TEXT,
                event_slug TEXT,
                yes_token_id TEXT,
                no_token_id TEXT,
                close_time TEXT,
                status TEXT,
                is_weather INTEGER NOT NULL DEFAULT 0,
                raw_payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                best_bid REAL,
                best_ask REAL,
                midpoint REAL,
                spread REAL,
                liquidity REAL,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                raw_payload TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    Database(db_path).init_schema()
    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(market_snapshots)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(market_snapshots)")}
    finally:
        connection.close()
    assert "token_id" in columns
    assert "idx_market_snapshots_market_token_fetched" in indexes
