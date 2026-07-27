from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection, OperationalError, Row, connect
from typing import Iterator

SCHEMA_VERSION = 6

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS roundtrip_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    buy_intent_id INTEGER,
    sell_intent_id INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id),
    FOREIGN KEY (buy_intent_id) REFERENCES order_intents(id),
    FOREIGN KEY (sell_intent_id) REFERENCES order_intents(id)
);

CREATE TABLE IF NOT EXISTS reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS markets (
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
    module_id TEXT NOT NULL DEFAULT 'weather',
    raw_payload TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT,
    best_bid REAL,
    best_ask REAL,
    midpoint REAL,
    spread REAL,
    liquidity REAL,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_payload TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS resolution_rules (
    market_id TEXT PRIMARY KEY,
    location TEXT,
    station TEXT,
    source TEXT,
    variable TEXT,
    operator TEXT,
    threshold REAL,
    unit TEXT,
    window_start TEXT,
    window_end TEXT,
    confidence REAL NOT NULL,
    tradable INTEGER NOT NULL,
    rejection_reason TEXT,
    raw_text TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS market_candidates (
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
    module_id TEXT NOT NULL DEFAULT 'weather',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS temperature_bucket_rules (
    market_id TEXT PRIMARY KEY,
    module_id TEXT NOT NULL DEFAULT 'china_temp_bucket',
    city TEXT NOT NULL,
    city_cn TEXT,
    station_id TEXT,
    settlement_station_id TEXT,
    source TEXT,
    variable TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'C',
    bucket_center_c REAL NOT NULL,
    bucket_lower_c REAL NOT NULL,
    bucket_upper_c REAL NOT NULL,
    target_date TEXT NOT NULL,
    settlement_timezone TEXT NOT NULL,
    confidence REAL NOT NULL,
    tradable INTEGER NOT NULL,
    rejection_reason TEXT,
    raw_text TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_temperature_bucket_rules_city_date
ON temperature_bucket_rules(city, target_date, bucket_center_c);

CREATE INDEX IF NOT EXISTS idx_temperature_bucket_rules_date_city
ON temperature_bucket_rules(target_date, city, bucket_center_c);

CREATE TABLE IF NOT EXISTS weather_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    provider TEXT NOT NULL,
    location TEXT,
    station TEXT,
    issue_time TEXT NOT NULL,
    valid_time TEXT NOT NULL,
    variable TEXT NOT NULL,
    value REAL NOT NULL,
    lower_value REAL,
    upper_value REAL,
    unit TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_weather_forecasts_market_fetched
ON weather_forecasts(market_id, fetched_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    provider TEXT NOT NULL,
    station TEXT,
    observed_at TEXT NOT NULL,
    variable TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    quality_status TEXT,
    raw_payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_weather_observations_market_observed
ON weather_observations(market_id, observed_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    fair_lower REAL NOT NULL,
    fair_upper REAL NOT NULL,
    reference_price REAL,
    edge REAL NOT NULL,
    side TEXT,
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_analyses_market_created
ON analyses(market_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS model_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER UNIQUE,
    market_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    forecast_provider TEXT,
    source_grade TEXT,
    yes_probability REAL NOT NULL,
    fair_lower REAL NOT NULL,
    fair_upper REAL NOT NULL,
    market_price REAL,
    edge REAL NOT NULL,
    side TEXT,
    decision TEXT NOT NULL,
    outcome_status TEXT NOT NULL DEFAULT 'pending',
    resolved_outcome TEXT,
    settlement_value REAL,
    settlement_source TEXT,
    settled_at TEXT,
    raw_payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (analysis_id) REFERENCES analyses(id),
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_model_signals_model_provider
ON model_signals(model_version, forecast_provider, created_at);

CREATE INDEX IF NOT EXISTS idx_model_signals_market_created
ON model_signals(market_id, created_at);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    proposed_side TEXT NOT NULL,
    proposed_price REAL NOT NULL,
    proposed_size REAL NOT NULL,
    proposed_notional REAL NOT NULL,
    reasons TEXT NOT NULL,
    max_order_usdc REAL NOT NULL,
    max_daily_usdc REAL NOT NULL,
    max_market_usdc REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS order_intents (
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
    idempotency_key TEXT,
    entry_policy_version TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_order_intents_active_live
ON order_intents(market_id, side, dry_run, status, created_at);

CREATE INDEX IF NOT EXISTS idx_order_intents_market_created
ON order_intents(market_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS order_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id INTEGER NOT NULL,
    request_payload TEXT NOT NULL,
    response_payload TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (intent_id) REFERENCES order_intents(id)
);

CREATE TABLE IF NOT EXISTS open_orders (
    exchange_order_id TEXT PRIMARY KEY,
    market_id TEXT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    notional REAL,
    status TEXT,
    raw_payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange_fill_id TEXT UNIQUE,
    order_id TEXT,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    raw_payload TEXT,
    filled_at TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS positions (
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    size REAL NOT NULL,
    notional REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market_id, outcome),
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS strategy_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL DEFAULT '*',
    profile TEXT NOT NULL DEFAULT '*',
    min_edge REAL,
    max_order_usdc REAL,
    max_daily_usdc REAL,
    max_market_usdc REAL,
    live_auto_enabled INTEGER,
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(market_id, profile)
);

CREATE TABLE IF NOT EXISTS automation_actions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    market_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    command_preview TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    requested_by TEXT,
    approved_by TEXT,
    rejected_by TEXT,
    failure_reason TEXT,
    result_summary TEXT,
    return_code INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    approved_at TEXT,
    rejected_at TEXT,
    claimed_at TEXT,
    executed_at TEXT,
    failed_at TEXT,
    execution_started_at TEXT,
    execution_finished_at TEXT,
    execution_duration_ms INTEGER,
    execution_argv TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_automation_actions_status_expiry
ON automation_actions(status, expires_at, created_at);

CREATE INDEX IF NOT EXISTS idx_automation_actions_market_history
ON automation_actions(market_id, created_at);

CREATE TABLE IF NOT EXISTS automation_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (action_id) REFERENCES automation_actions(id)
);

CREATE INDEX IF NOT EXISTS idx_automation_audit_events_action
ON automation_audit_events(action_id, created_at);

CREATE TABLE IF NOT EXISTS resolution_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    polymarket_closed INTEGER NOT NULL,
    polymarket_uma_status TEXT NOT NULL,
    polymarket_resolved_outcome TEXT,
    local_resolved_outcome TEXT,
    match INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_resolution_audits_market_created
ON resolution_audits(market_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS system_safety_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    circuit_breaker_tripped INTEGER NOT NULL DEFAULT 0,
    circuit_breaker_reason TEXT,
    tripped_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def connect(self) -> Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = connect(self.path, timeout=30)
        connection.row_factory = Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def init_schema(self) -> None:
        last_error: OperationalError | None = None
        for attempt in range(6):
            try:
                with self.transaction() as connection:
                    connection.executescript(SCHEMA)
                    version = _schema_version(connection)
                    if version < SCHEMA_VERSION:
                        _migrate_schema(connection)
                        _ensure_order_intent_idempotency(connection)
                        _ensure_resolution_audit_and_safety_schema(connection)
                        _ensure_open_orders_first_seen(connection)
                        _ensure_order_intent_policy_schema(connection)
                        _set_schema_version(connection, SCHEMA_VERSION)
                    else:
                        _ensure_order_intent_idempotency(connection)
                        _ensure_resolution_audit_and_safety_schema(connection)
                        _ensure_open_orders_first_seen(connection)
                        _ensure_order_intent_policy_schema(connection)
                        _ensure_schema_meta(connection, SCHEMA_VERSION)
                    # Repair additive columns even when an older release already
                    # stamped the current schema version before adding them.
                    _ensure_autopilot_state_schema(connection)
                    _ensure_market_snapshot_token_schema(connection)
                    _ensure_order_intent_policy_schema(connection)
                return
            except OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() or attempt >= 5:
                    raise
                time.sleep(min(5.0, 0.25 * (2**attempt)))
        if last_error is not None:
            raise last_error

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _schema_version(connection: Connection) -> int:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "schema_meta" in tables:
        row = connection.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
        return int(row["version"]) if row is not None else 0
    if "autopilot_decisions" not in tables:
        return 0
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(autopilot_decisions)")}
    state_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(autopilot_state)")
    }
    intent_columns = {row["name"] for row in connection.execute("PRAGMA table_info(order_intents)")}
    if "system_safety_state" in tables:
        return SCHEMA_VERSION
    if (
        "llm_reason" in columns
        and "app_mode" in state_columns
        and "idempotency_key" in intent_columns
    ):
        return 3
    if "llm_reason" in columns and "app_mode" in state_columns:
        return 2
    if "llm_reason" in columns:
        return 1
    return 0


def _ensure_schema_meta(connection: Connection, version: int) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_meta (id, version) VALUES (1, ?)",
        (version,),
    )


def _set_schema_version(connection: Connection, version: int) -> None:
    _ensure_schema_meta(connection, version)
    connection.execute("UPDATE schema_meta SET version = ? WHERE id = 1", (version,))


def _ensure_open_orders_first_seen(connection: Connection) -> None:
    """Durable first-seen timestamp for stale-order age (survives recon upserts)."""
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "open_orders" not in tables:
        return
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(open_orders)")}
    if "first_seen_at" not in columns:
        connection.execute(
            "ALTER TABLE open_orders ADD COLUMN first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )
        # Seed from existing updated_at so already-open orders keep a stable age anchor.
        connection.execute(
            """
            UPDATE open_orders
            SET first_seen_at = COALESCE(NULLIF(first_seen_at, ''), updated_at, CURRENT_TIMESTAMP)
            """
        )


def _ensure_order_intent_idempotency(connection: Connection) -> None:
    order_intent_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(order_intents)")
    }
    if "idempotency_key" not in order_intent_columns:
        connection.execute("ALTER TABLE order_intents ADD COLUMN idempotency_key TEXT")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_order_intents_idempotency_key
        ON order_intents(idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_order_intents_active_live
        ON order_intents(market_id, side, dry_run, status, created_at)
        """
    )


def _ensure_order_intent_policy_schema(connection: Connection) -> None:
    """Tag new BUY intents with the entry policy that produced them.

    Legacy rows intentionally remain NULL so repaired strategies never inherit
    historical sizing penalties from older entry logic.
    """
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "order_intents" not in tables:
        return
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(order_intents)")}
    if "entry_policy_version" not in columns:
        connection.execute("ALTER TABLE order_intents ADD COLUMN entry_policy_version TEXT")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_order_intents_entry_policy
        ON order_intents(entry_policy_version, dry_run, status, created_at)
        """
    )


def _ensure_resolution_audit_and_safety_schema(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS resolution_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            polymarket_closed INTEGER NOT NULL,
            polymarket_uma_status TEXT NOT NULL,
            polymarket_resolved_outcome TEXT,
            local_resolved_outcome TEXT,
            match INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'unavailable',
            local_source TEXT,
            polymarket_source TEXT,
            raw_local_payload TEXT,
            raw_polymarket_payload TEXT,
            trip_breaker INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
        """
    )
    audit_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(resolution_audits)")
    }
    if "status" not in audit_columns:
        connection.execute(
            "ALTER TABLE resolution_audits ADD COLUMN status TEXT NOT NULL DEFAULT 'unavailable'"
        )
    if "local_source" not in audit_columns:
        connection.execute("ALTER TABLE resolution_audits ADD COLUMN local_source TEXT")
    if "polymarket_source" not in audit_columns:
        connection.execute("ALTER TABLE resolution_audits ADD COLUMN polymarket_source TEXT")
    if "raw_local_payload" not in audit_columns:
        connection.execute("ALTER TABLE resolution_audits ADD COLUMN raw_local_payload TEXT")
    if "raw_polymarket_payload" not in audit_columns:
        connection.execute("ALTER TABLE resolution_audits ADD COLUMN raw_polymarket_payload TEXT")
    if "trip_breaker" not in audit_columns:
        connection.execute(
            "ALTER TABLE resolution_audits ADD COLUMN trip_breaker INTEGER NOT NULL DEFAULT 0"
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS system_safety_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            circuit_breaker_tripped INTEGER NOT NULL DEFAULT 0,
            circuit_breaker_reason TEXT,
            tripped_at TEXT,
            tripped_by TEXT,
            tripping_audit_id INTEGER,
            clear_note TEXT,
            cleared_at TEXT,
            cleared_by TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    safety_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(system_safety_state)")
    }
    if "tripped_by" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN tripped_by TEXT")
    if "tripping_audit_id" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN tripping_audit_id INTEGER")
    if "clear_note" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN clear_note TEXT")
    if "cleared_at" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN cleared_at TEXT")
    if "cleared_by" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN cleared_by TEXT")

    connection.execute(
        """
        INSERT OR IGNORE INTO system_safety_state (id, circuit_breaker_tripped)
        VALUES (1, 0)
        """
    )


def _ensure_autopilot_state_schema(connection: Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(autopilot_state)")}
    if "app_mode" not in columns:
        connection.execute(
            "ALTER TABLE autopilot_state ADD COLUMN app_mode TEXT NOT NULL DEFAULT 'paper'"
        )
    if "process_started_at" not in columns:
        connection.execute("ALTER TABLE autopilot_state ADD COLUMN process_started_at TEXT")
    if "latest_useful_tick_at" not in columns:
        connection.execute("ALTER TABLE autopilot_state ADD COLUMN latest_useful_tick_at TEXT")
    if "last_tick_duration_ms" not in columns:
        connection.execute("ALTER TABLE autopilot_state ADD COLUMN last_tick_duration_ms INTEGER")
    if "deferred_candidates_count" not in columns:
        connection.execute(
            "ALTER TABLE autopilot_state ADD COLUMN deferred_candidates_count INTEGER"
        )
    if "exchange_stream_status" not in columns:
        connection.execute(
            "ALTER TABLE autopilot_state ADD COLUMN exchange_stream_status "
            "TEXT NOT NULL DEFAULT 'disabled'"
        )
    if "exchange_stream_updated_at" not in columns:
        connection.execute("ALTER TABLE autopilot_state ADD COLUMN exchange_stream_updated_at TEXT")
    if "exchange_stream_detail" not in columns:
        connection.execute("ALTER TABLE autopilot_state ADD COLUMN exchange_stream_detail TEXT")
    if "last_portfolio_digest_at" not in columns:
        connection.execute("ALTER TABLE autopilot_state ADD COLUMN last_portfolio_digest_at TEXT")
    connection.execute(
        """
        INSERT OR IGNORE INTO autopilot_state (id, enabled, mode, app_mode, tick_seconds, tick_count)
        VALUES (1, 0, 'dry_run', 'paper', 300, 0)
        """
    )


def _ensure_market_snapshot_token_schema(connection: Connection) -> None:
    """Additive token_id on market_snapshots for YES/NO quote isolation.

    Source of truth: exchange asset/token ID from REST books or typed SDK events.
    Retention: identical to existing market_snapshots history; NULL rows remain.
    """
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "market_snapshots" not in tables:
        return
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(market_snapshots)")}
    if "token_id" not in columns:
        connection.execute("ALTER TABLE market_snapshots ADD COLUMN token_id TEXT")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_token_fetched
        ON market_snapshots(market_id, token_id, fetched_at DESC, id DESC)
        """
    )


def _migrate_schema(connection: Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_weather_forecasts_market_fetched
        ON weather_forecasts(market_id, fetched_at DESC, id DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_weather_observations_market_observed
        ON weather_observations(market_id, observed_at DESC, id DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_analyses_market_created
        ON analyses(market_id, created_at DESC, id DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_order_intents_market_created
        ON order_intents(market_id, created_at DESC, id DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_resolution_audits_market_created
        ON resolution_audits(market_id, created_at DESC, id DESC)
        """
    )
    market_columns = {row["name"] for row in connection.execute("PRAGMA table_info(markets)")}
    if "event_title" not in market_columns:
        connection.execute("ALTER TABLE markets ADD COLUMN event_title TEXT")
    if "category" not in market_columns:
        connection.execute("ALTER TABLE markets ADD COLUMN category TEXT")
    if "tags" not in market_columns:
        connection.execute("ALTER TABLE markets ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
    if "module_id" not in market_columns:
        connection.execute(
            "ALTER TABLE markets ADD COLUMN module_id TEXT NOT NULL DEFAULT 'weather'"
        )

    candidate_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(market_candidates)")
    }
    if "module_id" not in candidate_columns:
        connection.execute(
            "ALTER TABLE market_candidates ADD COLUMN module_id TEXT NOT NULL DEFAULT 'weather'"
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS temperature_bucket_rules (
            market_id TEXT PRIMARY KEY,
            module_id TEXT NOT NULL DEFAULT 'china_temp_bucket',
            city TEXT NOT NULL,
            city_cn TEXT,
            station_id TEXT,
            settlement_station_id TEXT,
            source TEXT,
            variable TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT 'C',
            bucket_center_c REAL NOT NULL,
            bucket_lower_c REAL NOT NULL,
            bucket_upper_c REAL NOT NULL,
            target_date TEXT NOT NULL,
            settlement_timezone TEXT NOT NULL,
            confidence REAL NOT NULL,
            tradable INTEGER NOT NULL,
            rejection_reason TEXT,
            raw_text TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
        """
    )
    bucket_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(temperature_bucket_rules)")
    }
    if "unit" not in bucket_columns:
        connection.execute(
            "ALTER TABLE temperature_bucket_rules ADD COLUMN unit TEXT NOT NULL DEFAULT 'C'"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_temperature_bucket_rules_city_date
        ON temperature_bucket_rules(city, target_date, bucket_center_c)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_temperature_bucket_rules_date_city
        ON temperature_bucket_rules(target_date, city, bucket_center_c)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_markets_module_updated
        ON markets(module_id, updated_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_candidates_module_status
        ON market_candidates(module_id, status, updated_at)
        """
    )
    connection.execute(
        """
        UPDATE markets
        SET module_id = 'china_temp_bucket'
        WHERE module_id = 'weather'
          AND (
              id IN (SELECT market_id FROM temperature_bucket_rules WHERE module_id = 'china_temp_bucket')
              OR id IN (
                  SELECT market_id FROM market_candidates
                  WHERE notes LIKE '%module=china_temp_bucket%'
              )
          )
        """
    )
    connection.execute(
        """
        UPDATE market_candidates
        SET module_id = 'china_temp_bucket'
        WHERE module_id = 'weather'
          AND (
              market_id IN (SELECT market_id FROM temperature_bucket_rules WHERE module_id = 'china_temp_bucket')
              OR notes LIKE '%module=china_temp_bucket%'
          )
        """
    )

    fill_columns = {row["name"] for row in connection.execute("PRAGMA table_info(fills)")}
    if "raw_payload" not in fill_columns:
        connection.execute("ALTER TABLE fills ADD COLUMN raw_payload TEXT")

    order_intent_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(order_intents)")
    }
    if "idempotency_key" not in order_intent_columns:
        connection.execute("ALTER TABLE order_intents ADD COLUMN idempotency_key TEXT")
    if "entry_policy_version" not in order_intent_columns:
        connection.execute("ALTER TABLE order_intents ADD COLUMN entry_policy_version TEXT")
    _ensure_order_intent_idempotency(connection)

    automation_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(automation_actions)")
    }
    if "execution_started_at" not in automation_columns:
        connection.execute("ALTER TABLE automation_actions ADD COLUMN execution_started_at TEXT")
    if "execution_finished_at" not in automation_columns:
        connection.execute("ALTER TABLE automation_actions ADD COLUMN execution_finished_at TEXT")
    if "execution_duration_ms" not in automation_columns:
        connection.execute(
            "ALTER TABLE automation_actions ADD COLUMN execution_duration_ms INTEGER"
        )
    if "execution_argv" not in automation_columns:
        connection.execute("ALTER TABLE automation_actions ADD COLUMN execution_argv TEXT")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS model_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER UNIQUE,
            market_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            forecast_provider TEXT,
            source_grade TEXT,
            yes_probability REAL NOT NULL,
            fair_lower REAL NOT NULL,
            fair_upper REAL NOT NULL,
            market_price REAL,
            edge REAL NOT NULL,
            side TEXT,
            decision TEXT NOT NULL,
            outcome_status TEXT NOT NULL DEFAULT 'pending',
            resolved_outcome TEXT,
            settlement_value REAL,
            settlement_source TEXT,
            settled_at TEXT,
            raw_payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (analysis_id) REFERENCES analyses(id),
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_model_signals_model_provider
        ON model_signals(model_version, forecast_provider, created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_model_signals_market_created
        ON model_signals(market_id, created_at)
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL DEFAULT '*',
            profile TEXT NOT NULL DEFAULT '*',
            min_edge REAL,
            max_order_usdc REAL,
            max_daily_usdc REAL,
            max_market_usdc REAL,
            live_auto_enabled INTEGER,
            notes TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(market_id, profile)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS autopilot_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'dry_run',
            app_mode TEXT NOT NULL DEFAULT 'paper',
            tick_seconds INTEGER NOT NULL DEFAULT 300,
            last_tick_at TEXT,
            last_tick_status TEXT,
            last_error TEXT,
            tick_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            process_started_at TEXT,
            latest_useful_tick_at TEXT,
            last_tick_duration_ms INTEGER,
            deferred_candidates_count INTEGER,
            exchange_stream_status TEXT NOT NULL DEFAULT 'disabled',
            exchange_stream_updated_at TEXT,
            exchange_stream_detail TEXT,
            last_portfolio_digest_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS autopilot_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            action TEXT NOT NULL,
            mode TEXT NOT NULL,
            edge REAL,
            reason TEXT NOT NULL,
            blockers TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            intent_id INTEGER,
            discovered INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_autopilot_decisions_created
        ON autopilot_decisions(created_at DESC)
        """
    )
    _ensure_autopilot_state_schema(connection)
    autopilot_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(autopilot_decisions)")
    }
    if "llm_provider" not in autopilot_columns:
        connection.execute("ALTER TABLE autopilot_decisions ADD COLUMN llm_provider TEXT")
    if "llm_model" not in autopilot_columns:
        connection.execute("ALTER TABLE autopilot_decisions ADD COLUMN llm_model TEXT")
    if "llm_confidence" not in autopilot_columns:
        connection.execute("ALTER TABLE autopilot_decisions ADD COLUMN llm_confidence REAL")
    if "llm_reason" not in autopilot_columns:
        connection.execute("ALTER TABLE autopilot_decisions ADD COLUMN llm_reason TEXT")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS resolution_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            polymarket_closed INTEGER NOT NULL,
            polymarket_uma_status TEXT NOT NULL,
            polymarket_resolved_outcome TEXT,
            local_resolved_outcome TEXT,
            match INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'unavailable',
            local_source TEXT,
            polymarket_source TEXT,
            raw_local_payload TEXT,
            raw_polymarket_payload TEXT,
            trip_breaker INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
        """
    )
    audit_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(resolution_audits)")
    }
    if "status" not in audit_columns:
        connection.execute(
            "ALTER TABLE resolution_audits ADD COLUMN status TEXT NOT NULL DEFAULT 'unavailable'"
        )
    if "local_source" not in audit_columns:
        connection.execute("ALTER TABLE resolution_audits ADD COLUMN local_source TEXT")
    if "polymarket_source" not in audit_columns:
        connection.execute("ALTER TABLE resolution_audits ADD COLUMN polymarket_source TEXT")
    if "raw_local_payload" not in audit_columns:
        connection.execute("ALTER TABLE resolution_audits ADD COLUMN raw_local_payload TEXT")
    if "raw_polymarket_payload" not in audit_columns:
        connection.execute("ALTER TABLE resolution_audits ADD COLUMN raw_polymarket_payload TEXT")
    if "trip_breaker" not in audit_columns:
        connection.execute(
            "ALTER TABLE resolution_audits ADD COLUMN trip_breaker INTEGER NOT NULL DEFAULT 0"
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS system_safety_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            circuit_breaker_tripped INTEGER NOT NULL DEFAULT 0,
            circuit_breaker_reason TEXT,
            tripped_at TEXT,
            tripped_by TEXT,
            tripping_audit_id INTEGER,
            clear_note TEXT,
            cleared_at TEXT,
            cleared_by TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    safety_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(system_safety_state)")
    }
    if "tripped_by" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN tripped_by TEXT")
    if "tripping_audit_id" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN tripping_audit_id INTEGER")
    if "clear_note" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN clear_note TEXT")
    if "cleared_at" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN cleared_at TEXT")
    if "cleared_by" not in safety_columns:
        connection.execute("ALTER TABLE system_safety_state ADD COLUMN cleared_by TEXT")

    connection.execute(
        """
        INSERT OR IGNORE INTO system_safety_state (id, circuit_breaker_tripped)
        VALUES (1, 0)
        """
    )
