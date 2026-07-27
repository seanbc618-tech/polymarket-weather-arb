# Resolution Audit + Global Circuit Breaker — Design Audit

**Date**: 2026-07-09  
**Auditor**: Grok (read-only design review; no `src/` or `tests/` changes)  
**Scope**: P0 slice #2 after Exit Guardian dry-run + live order idempotency (361 tests passing on `main`)  
**Key files reviewed**:
- `src/polymarket_weather_arb/services/settlement_service.py`
- `src/polymarket_weather_arb/services/calibration_service.py`
- `src/polymarket_weather_arb/services/live_readiness_service.py`
- `src/polymarket_weather_arb/services/autopilot_service.py`
- `src/polymarket_weather_arb/services/trading_service.py`
- `src/polymarket_weather_arb/storage/db.py`
- `src/polymarket_weather_arb/storage/repositories.py`
- `src/polymarket_weather_arb/adapters/polymarket/client.py`
- `src/polymarket_weather_arb/dashboard_ui/calibration.py`
- `tests/test_settlement_service.py`
- `tests/test_live_readiness_service.py`
- `tests/test_autopilot_service.py`
- Supporting: `domain/markets.py`, `services/operator_daemon.py`, `services/live_launchpad_service.py`, `services/compliance_service.py`, `docs/reviews/settlement-observation-audit.md`

---

# Executive Summary

This slice closes the **settlement truth gap** identified in `audit_report.md` and prior reviews: the project can locally infer YES/NO via NWS observation backfill (`SettlementService.backfill_market`), but it never compares that inference to **Polymarket’s actual resolved winner**. Parser drift, sample-based extrema vs official daily summary, or UMA/oracle edge cases can produce silent calibration lies and, worse, continued live trading on a broken rules model.

**Recommendation**: add a **`ResolutionAuditService`** that, for eligible markets, compares local settlement truth to Gamma API resolution metadata and persists every run in **`resolution_audits`**. On a **confirmed mismatch**, trip a **global circuit breaker** stored in **`system_safety_state`** (singleton row, DB-persisted). The breaker must **hard-block all live/autopilot execution paths** until an operator manually clears it with an auditable note. Dry-run, discovery, preview, and research flows stay available.

**Polymarket truth source**: **Gamma API market payload** — not CLOB order books, not Data API positions. Use `closed`, `umaResolutionStatus`, `outcomes`, and `outcomePrices` (verified against live closed weather markets, e.g. Taipei/Qingdao July 2026 buckets).

**Current adapter gap**: `GammaPolymarketClient` has no `get_market(id)` and `list_markets` only queries `active=true&closed=false`, so resolved markets are invisible today. Implementation must add a read-only Gamma fetch + pure `parse_polymarket_winner()` helper.

**Local truth source**: prefer recomputation from **`weather_observations` + `resolution_rules`** when present; fall back to **`model_signals.resolved_outcome`** (what `CalibrationService` already scores). Do not treat manual dashboard settle and observation backfill as equivalent in audit metadata (`local_source` column).

**Safety posture**: fail-closed on **mismatch**; fail-open-ish on **unavailable** (market not resolved yet, API ambiguity) — log and retry, do not trip. `TRADING_DISABLED` remains the env kill switch; circuit breaker is an orthogonal, automatic, DB-backed trip.

---

# Current Local Settlement Flow

## End-to-end path today

```
Market discovered → resolution_rules saved
        ↓
Forecast + analysis → model_signals (outcome_status=pending)
        ↓
[Optional] SettlementService.backfill_market
        ↓
NoaaProvider.fetch_observation → weather_observations row
        ↓
repository.settle_model_signals_for_market(...)
        ↓
model_signals: outcome_status=resolved, resolved_outcome, settlement_value, settlement_source
        ↓
CalibrationService reads resolved signals → Brier / hit rate / trust status
```

## `SettlementService` behavior (`settlement_service.py`)

| Method | Persists observation? | Settles signals? | Output |
|--------|----------------------|------------------|--------|
| `preview_market` | No | No | `SettlementPreviewResult.would_resolve_outcome` |
| `backfill_market` | Yes (`save_observation`) | Yes (`settle_model_signals_for_market`) | `SettlementBackfillResult.resolved_outcome` |

Resolution logic is deterministic: `_matches_rule(observation.value, rule)` with operators `>`, `>=`, `<`, `<=`. Rule comes from `resolution_rules` table or on-the-fly `parse_resolution_rule(market title, description)` (backfill persists; preview does not).

## Alternate settle paths (same DB effect, different provenance)

| Path | Entry | Writes |
|------|-------|--------|
| CLI `settlement-backfill --market` | `cli.py` | observation + signals |
| Dashboard `/calibration/backfill` | `dashboard.py` | observation + signals |
| CLI `calibration-settle --outcome` | `cli.py` | signals only |
| Dashboard `/calibration/settle` | `dashboard.py` | signals only |

All paths converge on `repository.settle_model_signals_for_market`, which updates **all** `model_signals` for the `market_id` — no per-signal provenance flag today.

## Where local “resolved outcome” lives

| Store | Fields | Role |
|-------|--------|------|
| **`model_signals`** | `outcome_status`, `resolved_outcome`, `settlement_value`, `settlement_source`, `settled_at` | **Primary consumer** for calibration (`calibration_service.py` lines 89–90) |
| **`weather_observations`** | `value`, `quality_status`, `raw_payload` (forensic trail) | Source data for backfill; can recompute outcome |
| **`resolution_rules`** | `operator`, `threshold`, `variable`, `window_*` | Rule used to map observation → yes/no |
| **Transient** | `SettlementBackfillResult`, `SettlementPreviewResult` | Not persisted |

**Not stored**: a dedicated `market_resolutions` or Polymarket-fetched winner column on `markets`. `markets.raw_payload` holds discovery-time Gamma JSON only; it is not refreshed after close.

## Calibration dependency

`CalibrationService` trusts any row with `outcome_status == "resolved"` and `resolved_outcome in {"yes","no"}`. A wrong backfill permanently poisons Brier/hit-rate and downstream `CalibrationTrust` used by live launchpad and LLM advisor context. **Resolution audit is the feedback loop that prevents poisoned calibration from enabling live trading.**

## Known local settlement hazards (from prior audits)

These are **inputs** to audit, not blockers for building audit:

- Sample max/min vs official NWS daily summary (`docs/reviews/settlement-observation-audit.md`)
- Parser operator drift (`docs/reviews/rule-operator-backfill-audit.md`)
- Manual settle indistinguishable from observation backfill in `model_signals`

---

# Polymarket Resolution Truth Source

## Answer: which API / fields?

| Source | Use for resolution audit? | Rationale |
|--------|---------------------------|-----------|
| **Gamma API `GET /markets/{id}`** | **Yes — primary** | Authoritative market metadata including close + settlement prices |
| **Gamma `outcomes` + `outcomePrices`** | **Yes — derive winner** | JSON strings; winner = outcome index with price ≈ 1 |
| **Gamma `closed`** | **Yes — gate** | Must be `true` before comparing |
| **Gamma `umaResolutionStatus`** | **Yes — gate** | Modern markets: `"resolved"`; reject `proposed` / `disputed` / null for strict mode |
| **Gamma `closedTime`, `umaEndDate`, `acceptingOrders`** | Supporting metadata | Audit payload + staleness hints |
| **Gamma `active`** | **Do not use alone** | Observed `active: true` while `closed: true` on resolved markets |
| **CLOB `/book`** | **No** | Post-resolution books may be empty or stale; not settlement truth |
| **Data API `/positions`** | **No** | User holdings, not market winner |
| **CLOB condition/token IDs** | Mapping only | Map yes/no token index; not winner by themselves |

### Live API evidence (2026-07-09)

Closed weather markets (e.g. `id=2812780`, “highest temperature in Taipei…”) return:

```json
{
  "closed": true,
  "umaResolutionStatus": "resolved",
  "outcomes": "[\"Yes\", \"No\"]",
  "outcomePrices": "[\"0\", \"1\"]",
  "acceptingOrders": false
}
```

Interpretation: index 0 (Yes) = 0, index 1 (No) = 1 → **NO wins**.

Recent crypto hourly markets show the same pattern with `automaticallyResolved: true`.

Legacy markets (pre-UMA) may have `umaResolutionStatus: null` and `outcomePrices: ["0","0"]` — treat as **unavailable/ambiguous**, not mismatch.

## Proposed winner parser (pure domain function)

Add `domain/polymarket_resolution.py` (name illustrative):

```python
@dataclass(frozen=True)
class PolymarketResolution:
    status: str          # resolved | pending | ambiguous | unavailable
    winner: str | None   # "yes" | "no" | None
    outcome_index: int | None
    outcome_prices: tuple[Decimal, ...]
    outcome_labels: tuple[str, ...]
    uma_status: str | None
    closed: bool
    reason: str
```

**Algorithm (yes/no markets only, v1)**:

1. Parse `outcomes` and `outcomePrices` via existing `_jsonish_list` pattern from `domain/markets.py`.
2. If `closed` is not true → `status=pending`.
3. If `umaResolutionStatus` present and not in `{"resolved"}` → `status=pending` (or `disputed` if values like `disputed` appear).
4. If fewer than 2 prices or labels → `status=unavailable`.
5. Convert prices to `Decimal`; find indices where `price >= 0.99`.
6. Exactly one winner index → map label to `yes`/`no` (case-insensitive match on `"yes"`/`"no"`; else use index 0/1 convention matching `_extract_yes_no_token_ids`).
7. Zero winners with both prices `< 0.01` → legacy edge case; `status=ambiguous`.
8. Multiple winners ≥ 0.99 → `status=ambiguous`.

**Stability**: For `umaResolutionStatus == "resolved"` on current weather bucket markets, `outcomePrices` are stable at `0`/`1`. During resolution window, prices may be mid-range — parser returns `pending`, audit returns `unavailable`.

## Current adapter gaps

`GammaPolymarketClient` today (`client.py`):

- `list_markets`: `active=true`, `closed=false` only — **never sees resolved markets**.
- No `get_market(market_id)`.
- No `get_event_markets_by_slug` resolution refresh for stored IDs.
- `parse_market_payload` ignores `closed`, `outcomePrices`, `umaResolutionStatus`.

`PolymarketClient` Protocol (`base.py`) lacks resolution methods.

## Required new client methods

| Method | HTTP | Purpose |
|--------|------|---------|
| `get_market(market_id: str) -> tuple[Market, dict[str, Any]]` | `GET {gamma}/markets/{id}` | Fetch single market including closed/resolved |
| `get_market_by_slug(slug: str) -> tuple[Market, dict[str, Any]]` | `GET {gamma}/markets/slug/{slug}` | Fallback when DB stores slug |
| (optional) `list_closed_markets(limit, offset)` | `GET {gamma}/markets?closed=true` | Batch audit sweeps |

Return full raw payload for `resolution_audits.raw_polymarket_payload`. Optionally upsert `markets` row with refreshed `status`/`raw_payload` — **do not** auto-mutate `model_signals`.

---

# Data Model Proposal

## Table: `resolution_audits`

Append-only audit log. One market may have many rows (re-audit after clear).

```sql
CREATE TABLE IF NOT EXISTS resolution_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    local_outcome TEXT,              -- yes | no | NULL
    polymarket_outcome TEXT,         -- yes | no | NULL
    status TEXT NOT NULL,            -- match | mismatch | unavailable | ambiguous | skipped
    local_source TEXT NOT NULL,      -- recomputed_observation | model_signals | manual_settle | none
    polymarket_source TEXT NOT NULL DEFAULT 'gamma_outcome_prices',
    local_settlement_value REAL,
    local_settlement_source TEXT,
    observation_id INTEGER,
    signal_count INTEGER NOT NULL DEFAULT 0,
    live_intent_count INTEGER NOT NULL DEFAULT 0,
    polymarket_closed INTEGER,
    polymarket_uma_status TEXT,
    polymarket_outcome_prices TEXT,  -- JSON
    polymarket_outcome_labels TEXT,  -- JSON
    raw_local_payload TEXT,          -- JSON: rule snapshot, observation excerpt, signal row ids
    raw_polymarket_payload TEXT NOT NULL,
    trip_breaker INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(id),
    FOREIGN KEY (observation_id) REFERENCES weather_observations(id)
);

CREATE INDEX IF NOT EXISTS idx_resolution_audits_market_created
ON resolution_audits(market_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_resolution_audits_status_created
ON resolution_audits(status, created_at DESC);
```

### `local_source` derivation priority

1. **`recomputed_observation`**: latest `weather_observations` + `resolution_rules` → `_matches_rule` (strongest forensic match to backfill path).
2. **`model_signals`**: latest resolved `model_signals.resolved_outcome` if no observation.
3. **`manual_settle`**: resolved signals but no observation and `settlement_source` not matching observation providers (heuristic).
4. **`none`**: no local outcome → audit `status=skipped` or `unavailable`.

### `status` semantics

| status | Meaning | Trips breaker? |
|--------|---------|----------------|
| `match` | Local yes/no equals Polymarket yes/no | No |
| `mismatch` | Both sides definitive and differ | **Yes** |
| `unavailable` | PM not resolved, or local missing, or fetch error | No |
| `ambiguous` | PM prices not decisive | No (warn) |
| `skipped` | Demo market, non-weather, non-yes/no module | No |

## Table: `system_safety_state`

Singleton (mirrors `autopilot_state` pattern). **Do not overload `autopilot_state`** — autopilot tick metadata and global safety are separate concerns.

```sql
CREATE TABLE IF NOT EXISTS system_safety_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    circuit_breaker_tripped INTEGER NOT NULL DEFAULT 0,
    tripped_at TEXT,
    tripped_reason TEXT,
    tripped_by TEXT,                 -- resolution_audit | operator_cli | operator_dashboard
    tripping_audit_id INTEGER,
    last_audit_run_at TEXT,
    last_audit_summary TEXT,         -- e.g. "2 mismatch, 5 match, 12 unavailable"
    cleared_at TEXT,
    cleared_by TEXT,
    clear_note TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tripping_audit_id) REFERENCES resolution_audits(id)
);

INSERT OR IGNORE INTO system_safety_state (id) VALUES (1);
```

### Interaction with `TRADING_DISABLED`

| Control | Layer | Cleared by |
|---------|-------|------------|
| `TRADING_DISABLED=true` | Env / config | Operator edits `.env`, restarts |
| Circuit breaker tripped | SQLite `system_safety_state` | Explicit `circuit-breaker clear` + note |

Both must block live. Dashboard should show both prominently on `/app`, `/live`, `/beginner`.

## Optional v2 columns (not required for P0)

- `markets.polymarket_resolved_outcome` cache — convenience only; audits table is source of truth.
- `model_signals.settlement_provenance` — distinguish manual vs backfill (helps audit, not P0).

---

# Circuit Breaker Proposal

## Design principles

1. **Global**: one trip stops all live/autopilot execution, not per-market ignore.
2. **Persistent**: survives process restart (SQLite, not in-memory).
3. **Manual clear only**: re-audit match does **not** auto-clear (operator must acknowledge).
4. **Conservative trip**: any **confirmed mismatch** on an audited market trips immediately.
5. **Dry-run unaffected**: paper/observe/research/calibration backfill preview continue.

## Events that trip the breaker

| Event | Trip? |
|-------|-------|
| `resolution_audits.status = mismatch` (local and PM both yes/no) | **Yes — default** |
| Second mismatch same market after manual clear without fix | **Yes** |
| `ambiguous` Polymarket prices | No (warn + retry later) |
| `unavailable` (PM pending, local missing) | No |
| Gamma API timeout / 5xx on audit job | No trip; increment failure counter; if **live readiness audit check** fails N times, optional future enhancement |
| Demo markets (`demo-` prefix) | Never audit / never trip |

### Mismatch scope filter (recommended v1)

Audit candidates:

- `module_id = 'weather'` (defer `china_temp_bucket` bucket semantics to v2), AND
- at least one of:
  - `model_signals.outcome_status = 'resolved'`
  - `weather_observations` row exists
  - live `order_intents` with `dry_run=0` exist for market

Trip on mismatch when:

- market has **any live intent** OR **resolved local outcome from `recomputed_observation` or `model_signals` with `settlement_source` indicating backfill** OR operator-configured `RESOLUTION_AUDIT_STRICT=true` (any mismatch trips).

Default for P0: **trip on any mismatch** for markets in the candidate set (user requirement: mismatch → global fuse).

## Unlock procedure (manual only)

1. Operator investigates audit row (`resolution_audits.raw_*`, calibration preview, Polymarket UI).
2. Root cause fixed (parser, observation method, or accept PM oracle).
3. Run `uv run polymarket-weather operator circuit-breaker clear --note "..."` (new CLI).
4. Optional: dashboard POST `/app/circuit-breaker/clear` with confirmation phrase `CLEAR BREAKER`.
5. Re-run `uv run polymarket-weather resolution-audit --market <id>` to verify `match`.
6. Re-run `live-readiness`; only then set `TRADING_DISABLED=false` if applicable.

**Never** auto-clear on match. **Never** clear via env var alone without note.

## Service: `CircuitBreakerService`

```python
class CircuitBreakerService:
    def is_tripped(self) -> bool: ...
    def trip(self, *, reason: str, by: str, audit_id: int | None) -> None: ...
    def clear(self, *, by: str, note: str) -> None: ...  # requires non-empty note
    def snapshot(self) -> CircuitBreakerSnapshot: ...
```

Single choke-point helper:

```python
def live_execution_blocked(repository) -> str | None:
    """Returns blocker reason or None."""
```

Used by trading, autopilot, daemon, automation.

---

# Integration Points

## Must read breaker state (hard block when tripped)

| Path | File | Integration |
|------|------|-------------|
| Autopilot tick (live mode) | `autopilot_service.py` `collect_blockers`, `tick` | Add `circuit_breaker_tripped` blocker before discovery |
| Autopilot first-run checks | `autopilot_service.py` `first_run_checks` | New check `resolution_circuit_breaker` |
| Live readiness | `live_readiness_service.py` `check` | New check after compliance |
| Trading live submit | `trading_service.py` `trade` | Early return when `not dry_run` and tripped |
| Operator daemon live auto | `operator_daemon.py` `_execute_live_actions` | Skip all `trade_live` when tripped |
| Automation `trade_live` execute | `automation_service.py` | Gate in execute path |
| CLI `trade --live` | `cli.py` | Gate before `TradingService.trade` |
| Live launchpad | `live_launchpad_service.py` `build_live_launchpad_snapshot` | Add gate to `blockers` + `can_execute` |
| Dashboard `/app` | `dashboard_ui/app.py` via `AutopilotService.snapshot` | Surfaced in blockers + first-run panel |
| Dashboard `/live` | `dashboard_ui/live.py` (or equivalent) | Banner + disable execute buttons |

## Should run audit (scheduled / on-demand)

| Trigger | Suggested entry |
|---------|-----------------|
| CLI | `resolution-audit --market`, `--pending`, `--since-days 7` |
| Autopilot | End of tick when `mode=dry_run` only: audit up to N pending closed markets (no live side effects) |
| Operator daemon | Separate slow loop every 30–60 min (configurable) |
| Post backfill | After `settlement_service.backfill_market` commits: enqueue single-market audit if PM closed |
| Dashboard | `/calibration` section: “Resolution audits” table + mismatch badge |

## Warning only (no trip, no live block)

| Condition | Behavior |
|-----------|----------|
| Polymarket `closed=false` or `umaResolutionStatus != resolved` | `status=unavailable`; retry later |
| Local outcome missing | `status=unavailable`; dashboard warn |
| `outcomePrices` ambiguous | `status=ambiguous`; log warning |
| Audit API error on single market | Record `unavailable`; continue batch |
| Demo/fixture markets | `status=skipped` |
| Dry-run-only markets (no resolve, no live intents) mismatch | **Config**: warn only vs trip — **recommend trip only when live intents or backfill provenance** to reduce false positives during research |
| Calibration manual settle without observation | Audit if PM resolved; mismatch trips only if strict mode |

## Hard block live (summary)

Live blocked when **any** of:

- `TRADING_DISABLED=true`
- Compliance geoblock fail
- Circuit breaker tripped
- Existing risk / reconciliation / whitelist / idempotency gates

Circuit breaker does **not** block:

- `dry_run=True` trades
- `settlement-backfill --preview`
- Discovery / analyze / observe mode
- Exit guardian dry-run recommendations

---

# Tests To Add

## Domain / adapter

| Test file | Cases |
|-----------|-------|
| `tests/test_polymarket_resolution.py` | YES wins `[1,0]`; NO wins `[0,1]`; pending when not closed; ambiguous `[0,0]`; legacy null uma; non-yes/no labels |
| `tests/test_polymarket_client.py` (extend) | `get_market` httpx mock; 404 → clear error |

## Resolution audit service

| Test file | Cases |
|-----------|-------|
| `tests/test_resolution_audit_service.py` | match → no trip; mismatch → trip + audit row; unavailable → no trip; skipped demo; uses recomputed observation over stale signal; stores raw payloads |

## Circuit breaker

| Test file | Cases |
|-----------|-------|
| `tests/test_circuit_breaker_service.py` | trip idempotent; clear requires note; snapshot reflects state |

## Integration gates

| Test file | Cases |
|-----------|-------|
| `tests/test_autopilot_service.py` (extend) | live tick blocked when breaker tripped |
| `tests/test_live_readiness_service.py` (extend) | readiness fails when tripped |
| `tests/test_trading_service.py` (extend) | live rejected with breaker reason; dry_run still ok |
| `tests/test_operator_daemon.py` or `test_cli_operator.py` | daemon skips live actions when tripped |

## Fixtures

Add `fixtures/markets/resolved-weather-taipei-no.json` with sanitized Gamma payload (`outcomePrices: ["0","1"]`, `umaResolutionStatus: resolved`) for offline tests.

**Target**: +25–35 tests; keep total green (361 + new).

---

# Implementation Plan For Claude Code / Codex

Follow the same slice style as `docs/superpowers/plans/2026-07-08-p0-exit-guardian-idempotency.md`: small PR-sized tasks, TDD, `uv run pytest -q` after each task.

## Task 1 — Domain parser + unit tests

- [ ] Create `domain/polymarket_resolution.py` with `parse_polymarket_resolution(payload) -> PolymarketResolution`
- [ ] Create `tests/test_polymarket_resolution.py` with fixture payloads (resolved NO, resolved YES, pending, ambiguous)
- [ ] Run: `uv run pytest tests/test_polymarket_resolution.py -q`

## Task 2 — Gamma client `get_market`

- [ ] Add `get_market(market_id)` to `GammaPolymarketClient`
- [ ] Extend `PolymarketClient` Protocol in `base.py`
- [ ] Add httpx mock test
- [ ] Run: `uv run pytest tests/test_polymarket_client.py -q` (or nearest existing client test file)

## Task 3 — Schema + repositories

- [ ] Bump `SCHEMA_VERSION` in `db.py`; add `resolution_audits`, `system_safety_state`
- [ ] Repository methods: `save_resolution_audit`, `list_resolution_audits`, `latest_resolution_audit`, `get_system_safety_state`, `trip_circuit_breaker`, `clear_circuit_breaker`
- [ ] Extend `tests/test_db.py` for new tables + singleton insert

## Task 4 — `ResolutionAuditService`

- [ ] Create `services/resolution_audit_service.py`
  - `audit_market(market_id) -> ResolutionAuditResult`
  - `audit_pending(limit) -> list[ResolutionAuditResult]`
  - `_local_outcome(market_id) -> (outcome, source, raw_local)`
  - `_polymarket_outcome(market_id) -> PolymarketResolution`
- [ ] On mismatch: call `CircuitBreakerService.trip`
- [ ] Tests in `tests/test_resolution_audit_service.py`

## Task 5 — `CircuitBreakerService` + `live_execution_blocked` helper

- [ ] Create `services/circuit_breaker_service.py`
- [ ] Tests in `tests/test_circuit_breaker_service.py`

## Task 6 — Wire live gates

- [ ] `trading_service.py`: check breaker when `not dry_run`
- [ ] `autopilot_service.py`: `collect_blockers` + `first_run_checks`
- [ ] `live_readiness_service.py`: new check
- [ ] `operator_daemon.py`: `_execute_live_actions`
- [ ] `live_launchpad_service.py`: blockers
- [ ] Extend existing tests (autopilot, live readiness, trading)

## Task 7 — CLI + operator commands

- [ ] `resolution-audit` root command (or `operator resolution-audit`)
- [ ] `operator circuit-breaker status|clear`
- [ ] Register in `cli.py` / `cli_commands/operator.py`
- [ ] `tests/test_cli_operator.py` coverage

## Task 8 — Dashboard visibility (minimal P0)

- [ ] `/app` first-run check + blocker banner for tripped breaker
- [ ] `/calibration` read-only table: recent `resolution_audits` (last 20)
- [ ] i18n strings in `dashboard_ui/i18n.py`
- [ ] `tests/test_dashboard_calibration.py` or `test_dashboard_app.py` smoke

## Task 9 — Optional autopilot/daemon audit hook

- [ ] Dry-run autopilot tick tail: `audit_pending(limit=3)` — **only when breaker not tripped**
- [ ] Document env `RESOLUTION_AUDIT_ON_TICK=true` default false

## Task 10 — Verification

- [ ] `uv run pytest -q`
- [ ] `uv run ruff check src/ tests/`
- [ ] Manual: fetch real closed weather market ID from Gamma, run `resolution-audit --market <id>` against DB with intentional wrong local outcome → confirm trip + live block

**Estimated touch count**: ~15 files, ~800–1200 LOC, similar magnitude to Exit Guardian slice.

---

# Risks And Open Questions

| Risk | Mitigation |
|------|------------|
| **Sample-based local high ≠ PM oracle** even when parser is correct | Track `local_source`; show observation raw in dashboard; long-term: official daily summary source (see `official-daily-extrema-source-strategy.md`) |
| **False positive trip** on research-only markets | Trip only when `live_intent_count > 0` or strict env flag; document operator clear path |
| **False negative** (no trip when parser wrong but no backfill yet) | Expand audit candidates to markets with live intents even if local unresolved — compare only when local outcome exists OR skip until backfill |
| **China bucket / multi-outcome markets** | V1 limit to yes/no `weather` module; bucket markets need per-bucket PM question text vs exact temperature match — separate audit module later |
| **Gamma API rate limits** on batch sweeps | `audit_pending(limit=10)` per tick; backoff |
| **`umaResolutionStatus` null on old markets** | `status=ambiguous`; never trip |
| **Manual settle typos** | `local_source=manual_settle`; surface prominently in audit UI |
| **Breaker tripped but operator only uses CLI trade** | Must gate **all** `dry_run=False` paths (Task 6 checklist) |

### Open questions for product owner

1. **Strictness default**: trip on *any* mismatch for resolved local backfill, or only when live capital touched?
2. **Auto-audit on backfill commit**: immediate compare vs nightly batch?
3. **Clear authorization**: single operator note sufficient, or require re-audit `match` before clear?
4. **Notification**: wire into existing `operator_daemon` notifier on trip?

---

# Do Not Change / Safety Invariants

The implementation slice **must preserve** these existing guarantees:

1. **Limit orders only** — no market orders.
2. **Hard risk caps** in `domain/risk.py` — circuit breaker is additive, not a replacement.
3. **`TRADING_DISABLED`** env kill switch remains independent and must still block live when true.
4. **Live trading gates unchanged in ordering**: compliance → breaker (new) → credentials → reconciliation fresh → whitelist → override `live_auto_enabled` → settlement-grade forecast → risk engine → idempotency / duplicate open order guard.
5. **Exit Guardian stays dry-run** — no auto cancel/close in this slice.
6. **Settlement backfill** remains calibration/research; audit must not silently rewrite `model_signals` to match Polymarket.
7. **Beginner `/beginner` page** must never gain live execution; may show breaker status read-only.
8. **Fail closed on mismatch trip**; never auto-clear breaker.
9. **Append-only audit log** — do not delete `resolution_audits` rows on clear.
10. **361 existing tests** must stay green after each task.

---

## Appendix: Core Question Checklist

| # | Question | Answer |
|---|----------|--------|
| 1 | PM resolved outcome source? | **Gamma API**: `closed`, `umaResolutionStatus`, `outcomes`, `outcomePrices` |
| 2 | Adapter sufficient today? | **No** — add `get_market` + parser |
| 3 | New client methods? | `get_market(id)`, optional `get_market_by_slug`, optional `list_closed_markets` |
| 4 | Local settlement stored where? | **`model_signals.resolved_*`**, **`weather_observations`**, backfill result ephemeral |
| 5 | New table? | **`resolution_audits`** (+ **`system_safety_state`**) |
| 6 | Circuit breaker design? | **`system_safety_state` singleton**; trip on mismatch; manual clear with note |
| 7 | Who reads breaker? | Autopilot, live-readiness, trading live, daemon, launchpad, `/app` |
| 8 | Warning only? | PM pending, local missing, ambiguous prices, API errors, demo markets |
| 9 | Hard block live? | Breaker tripped (+ existing kill switches) |
| 10 | Tests? | See **Tests To Add** (+ fixture JSON) |