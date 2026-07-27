import sqlite3
import threading
import time

from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.storage.db import SCHEMA_VERSION, Database
from polymarket_weather_arb.storage.repositories import Repository


def test_init_schema_migrates_existing_markets_table(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
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
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    Database(db_path).init_schema()

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(markets)")}
    finally:
        connection.close()
    assert {"event_title", "category", "tags", "module_id"} <= columns


def test_init_schema_migrates_china_bucket_module_metadata(tmp_path):
    db_path = tmp_path / "legacy-china.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE markets (
                id TEXT PRIMARY KEY,
                slug TEXT,
                title TEXT NOT NULL,
                description TEXT,
                event_slug TEXT,
                event_title TEXT,
                category TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                yes_token_id TEXT,
                no_token_id TEXT,
                close_time TEXT,
                status TEXT,
                is_weather INTEGER NOT NULL DEFAULT 0,
                raw_payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE market_candidates (
                market_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                tradable INTEGER NOT NULL,
                rejection_reason TEXT,
                best_bid REAL,
                best_ask REAL,
                spread REAL,
                snapshot_fetched_at TEXT,
                rule_updated_at TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO markets (id, title, tags, raw_payload)
            VALUES ('shanghai-18c', 'Highest temperature in Shanghai on May 10?', '[]', '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO market_candidates (market_id, status, tradable, notes)
            VALUES ('shanghai-18c', 'dry_run_ready', 1, 'module=china_temp_bucket; bucket=17.5-18.5C')
            """
        )
        connection.commit()
    finally:
        connection.close()

    Database(db_path).init_schema()

    connection = sqlite3.connect(db_path)
    try:
        market_columns = {row[1] for row in connection.execute("PRAGMA table_info(markets)")}
        candidate_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(market_candidates)")
        }
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        market_module = connection.execute(
            "SELECT module_id FROM markets WHERE id = 'shanghai-18c'"
        ).fetchone()[0]
        candidate_module = connection.execute(
            "SELECT module_id FROM market_candidates WHERE market_id = 'shanghai-18c'"
        ).fetchone()[0]
    finally:
        connection.close()

    assert "module_id" in market_columns
    assert "module_id" in candidate_columns
    assert "temperature_bucket_rules" in tables
    assert "idx_temperature_bucket_rules_city_date" in indexes
    assert "idx_temperature_bucket_rules_date_city" in indexes
    assert "idx_markets_module_updated" in indexes
    assert "idx_market_candidates_module_status" in indexes
    assert "idx_weather_forecasts_market_fetched" in indexes
    assert "idx_weather_observations_market_observed" in indexes
    assert "idx_analyses_market_created" in indexes
    assert "idx_order_intents_market_created" in indexes
    assert "idx_resolution_audits_market_created" in indexes
    assert market_module == "china_temp_bucket"
    assert candidate_module == "china_temp_bucket"


def test_init_schema_adds_strategy_overrides_table(tmp_path):
    db_path = tmp_path / "overrides.db"

    Database(db_path).init_schema()

    connection = sqlite3.connect(db_path)
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'strategy_overrides'"
        ).fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(strategy_overrides)")}
    finally:
        connection.close()
    assert table is not None
    assert {"market_id", "profile", "min_edge", "live_auto_enabled"} <= columns


def test_init_schema_adds_model_signals_table(tmp_path):
    db_path = tmp_path / "signals.db"

    Database(db_path).init_schema()

    connection = sqlite3.connect(db_path)
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'model_signals'"
        ).fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(model_signals)")}
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    finally:
        connection.close()

    assert table is not None
    assert {
        "analysis_id",
        "market_id",
        "model_version",
        "forecast_provider",
        "yes_probability",
        "outcome_status",
        "resolved_outcome",
        "settlement_source",
    } <= columns
    assert "idx_model_signals_model_provider" in indexes
    assert "idx_model_signals_market_created" in indexes


def test_init_schema_records_version_and_skips_repeat_migration(tmp_path):
    db_path = tmp_path / "versioned.db"

    Database(db_path).init_schema()
    Database(db_path).init_schema()

    connection = sqlite3.connect(db_path)
    try:
        version = connection.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()[0]
    finally:
        connection.close()
    assert version == SCHEMA_VERSION


def test_init_schema_repairs_incomplete_version_four_safety_tables(tmp_path):
    db_path = tmp_path / "incomplete-v4.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE schema_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            )
            """
        )
        connection.execute("INSERT INTO schema_meta (id, version) VALUES (1, 4)")
        connection.execute(
            """
            CREATE TABLE resolution_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                polymarket_closed INTEGER NOT NULL,
                polymarket_uma_status TEXT NOT NULL,
                polymarket_resolved_outcome TEXT,
                local_resolved_outcome TEXT,
                match INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE system_safety_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                circuit_breaker_tripped INTEGER NOT NULL DEFAULT 0,
                circuit_breaker_reason TEXT,
                tripped_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    Database(db_path).init_schema()

    database = Database(db_path)
    connection = database.connect()
    try:
        audit_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(resolution_audits)")
        }
        safety_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(system_safety_state)")
        }
        repository = Repository(connection)
        repository.trip_circuit_breaker("test trip", by="test", audit_id=42)
        repository.clear_circuit_breaker(by="test", note="verified repair")
        connection.commit()
    finally:
        connection.close()

    assert {
        "status",
        "local_source",
        "polymarket_source",
        "raw_local_payload",
        "raw_polymarket_payload",
        "trip_breaker",
    } <= audit_columns
    assert {
        "tripped_by",
        "tripping_audit_id",
        "clear_note",
        "cleared_at",
        "cleared_by",
    } <= safety_columns


def test_init_schema_adds_autopilot_app_mode(tmp_path):
    db_path = tmp_path / "app-mode.db"

    Database(db_path).init_schema()

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(autopilot_state)")}
        app_mode = connection.execute(
            "SELECT app_mode FROM autopilot_state WHERE id = 1"
        ).fetchone()[0]
    finally:
        connection.close()

    assert "app_mode" in columns
    assert app_mode == "paper"


def test_init_schema_migrates_autopilot_app_mode_from_version_one(tmp_path):
    db_path = tmp_path / "legacy-app-mode.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE schema_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            )
            """
        )
        connection.execute("INSERT INTO schema_meta (id, version) VALUES (1, 1)")
        connection.execute(
            """
            CREATE TABLE autopilot_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'dry_run',
                tick_seconds INTEGER NOT NULL DEFAULT 300,
                last_tick_at TEXT,
                last_tick_status TEXT,
                last_error TEXT,
                tick_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO autopilot_state (id, enabled, mode, tick_seconds, tick_count)
            VALUES (1, 0, 'dry_run', 300, 0)
            """
        )
        connection.commit()
    finally:
        connection.close()

    Database(db_path).init_schema()

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(autopilot_state)")}
        version = connection.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()[0]
    finally:
        connection.close()

    assert "app_mode" in columns
    assert version == SCHEMA_VERSION


def test_init_schema_repairs_incomplete_current_version_autopilot_state(tmp_path):
    db_path = tmp_path / "incomplete-current-autopilot.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE schema_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_meta (id, version) VALUES (1, ?)",
            (SCHEMA_VERSION,),
        )
        connection.execute(
            """
            CREATE TABLE autopilot_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'dry_run',
                app_mode TEXT NOT NULL DEFAULT 'paper',
                tick_seconds INTEGER NOT NULL DEFAULT 300,
                last_tick_at TEXT,
                last_tick_status TEXT,
                last_error TEXT,
                tick_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO autopilot_state (id, enabled, mode, app_mode, tick_seconds, tick_count)
            VALUES (1, 1, 'live', 'full_live', 300, 7)
            """
        )
        connection.commit()
    finally:
        connection.close()

    Database(db_path).init_schema()

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(autopilot_state)")}
        state = connection.execute(
            "SELECT enabled, mode, app_mode, tick_count FROM autopilot_state WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()

    assert {
        "process_started_at",
        "latest_useful_tick_at",
        "last_tick_duration_ms",
        "deferred_candidates_count",
    } <= columns
    assert state == (1, "live", "full_live", 7)


def test_init_schema_migrates_order_intent_idempotency_from_version_two(tmp_path):
    db_path = tmp_path / "legacy-idempotency.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE schema_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            )
            """
        )
        connection.execute("INSERT INTO schema_meta (id, version) VALUES (1, 2)")
        connection.execute(
            """
            CREATE TABLE order_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                token_id TEXT,
                limit_price REAL NOT NULL,
                size REAL NOT NULL,
                notional REAL NOT NULL,
                rationale TEXT NOT NULL,
                dry_run INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    Database(db_path).init_schema()

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(order_intents)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(order_intents)")}
        version = connection.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()[0]
    finally:
        connection.close()

    assert "idempotency_key" in columns
    assert "entry_policy_version" in columns
    assert "idx_order_intents_idempotency_key" in indexes
    assert "idx_order_intents_active_live" in indexes
    assert "idx_order_intents_entry_policy" in indexes
    assert version == SCHEMA_VERSION


def test_init_schema_retries_when_database_is_locked(tmp_path):
    db_path = tmp_path / "locked.db"
    Database(db_path).init_schema()

    started = threading.Event()
    errors: list[Exception] = []

    def hold_write_lock() -> None:
        connection = sqlite3.connect(db_path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN IMMEDIATE")
            started.set()
            time.sleep(1.5)
            connection.commit()
        finally:
            connection.close()

    def init_while_locked() -> None:
        started.wait(timeout=5)
        try:
            Database(db_path).init_schema()
        except Exception as exc:  # pragma: no cover - test failure path
            errors.append(exc)

    writer = threading.Thread(target=hold_write_lock)
    reader = threading.Thread(target=init_while_locked)
    writer.start()
    reader.start()
    writer.join(timeout=10)
    reader.join(timeout=15)

    assert not errors


def test_purge_demo_markets_uses_weather_forecasts_table(tmp_path):
    db_path = tmp_path / "purge-demo.db"
    database = Database(db_path)
    database.init_schema()
    connection = database.connect()
    try:
        repository = Repository(connection)
        repository.upsert_market(
            Market(
                id="demo-market",
                title="Demo weather market",
                slug="demo-market",
                description="Demo",
                yes_token_id="yes",
                no_token_id="no",
                is_weather=True,
            ),
            {"id": "demo-market"},
        )
        connection.commit()

        purged = repository.purge_demo_markets()
        connection.commit()

        assert purged == 1
    finally:
        connection.close()
