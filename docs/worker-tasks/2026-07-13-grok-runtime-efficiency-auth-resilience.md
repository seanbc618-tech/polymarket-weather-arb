# Grok Task: Full-Live Runtime Efficiency, Auth Reuse, And Read Resilience

## Dispatch And Baseline

You are the primary implementation worker for the July 13 runtime follow-up.
Start from `main` at or after commit `219bec6`.

Before planning or editing, read:

1. `AGENTS.md`
2. `docs/agent-worker-standards.md`
3. `docs/runbooks/full-auto-micro-live.md`
4. commits `5bba6ef` and `219bec6` plus their tests
5. the current implementations of `GammaPolymarketClient`,
   `ReconciliationService`, `DiscoveryService`, and `AutopilotService`

Run `git status --short` first. Preserve all unrelated user work. Pull normally,
push normally to `main`, and never force-push.

No real BUY, SELL, cancellation, redemption, approval, or other exchange mutation
is permitted. All mutation tests must be mocked. Read-only network verification
may use a temporary database only and must be explicitly reported.

## Objective

Fix the remaining operational defects observed during the July 12-13 full-live
run without changing strategy, probability, Edge, sizing, or exit policy:

- a nominal 300-second tick routinely spent 165-244 seconds doing synchronous I/O;
- discovery fetched a CLOB order book for nearly every bucket market on every tick;
- each reconciliation created and closed four secure SDK clients, causing four
  `POST /auth/api-key -> 400 -> derive-api-key -> 200` sequences per tick;
- two transient reconciliation adapter errors had no useful failing-stage detail;
- a hot-path `Settings()` reload failed one complete tick because
  `LLM_ENABLED=''` could not parse as a boolean;
- repeated identical reconciliation failures can generate repeated Telegram risk
  messages without a distinct recovery notification;
- phase logs expose total duration but not enough per-phase evidence to prove the
  bottleneck is removed.

The existing daily-cap, stale-candidate, missing-order, and partial-order terminal
fixes are accepted baseline behavior and must remain green.

Expected new services, schedulers, database tables, product modes, BUY paths,
SELL paths, and strategy engines: **zero**.

## Mandatory Reuse Map

- `GammaPolymarketClient`: the only owner of official SDK client construction,
  authenticated reads, BUY, SELL, and cancellation adapter calls.
- `ReconciliationService`: the only owner of account-state reconciliation and
  fail-stop result semantics.
- `DiscoveryService`: the owner of Gamma metadata ingestion and candidate
  discovery. It must not become an execution engine.
- `MarketWorkflowService`: the existing batch forecast/analysis owner.
- `AutopilotService`: the only `/app` autonomous loop, phase ordering, material
  notification flushing, and work-budget owner.
- `TradingService` and `PositionExitService`: unchanged as the sole live BUY and
  SELL mutation paths.
- Existing `MarketSnapshot`, candidate rows, market raw payloads, rotating logs,
  and `autopilot_state`: reuse them. Add no telemetry table.

Before adding any class, file, setting, or persistent field, state why these
owners cannot contain the behavior. The expected answer for this task is that no
new persistent abstraction is needed.

## Slice 1: Reuse One Authenticated SDK Session

Inspect the installed official `polymarket-client` beta API before coding. Do not
guess constructor, credential, close, or refresh behavior.

1. `GammaPolymarketClient` must lazily create at most one authenticated
   `SecureClient` per adapter instance/credential set and reuse it across:
   - balances;
   - open orders;
   - account trades;
   - positions;
   - order lookup;
   - BUY/SELL/cancel calls.
2. Remove per-method create/close churn. Add a small explicit adapter `close()`
   lifecycle only if the SDK requires it; wire it into existing CLI/Autopilot
   shutdown rather than adding a manager service.
3. Make lazy initialization safe against concurrent dashboard reads. A small lock
   inside the adapter is acceptable; a client pool is not.
4. A confirmed authentication error on an idempotent read may invalidate and
   recreate the cached secure client once. Do not retry forever.
5. Never automatically replay BUY, SELL, or cancel after an ambiguous network or
   SDK failure. Exchange mutations retain current durability/idempotency rules.
6. Do not persist derived CLOB credentials in SQLite, `.env`, logs, or a new file.
   In-memory SDK reuse is sufficient.
7. Normalize an absent/not-open `get_order()` response into an explicit adapter
   result or precise not-found exception instead of leaking a Pydantic response-
   shape traceback. Do not weaken reconciliation truth.

Acceptance evidence:

- one adapter instance calling balances, orders, trades, and positions invokes
  SDK client creation exactly once;
- a second reconciliation tick reuses the same session;
- one simulated auth expiry on a read recreates once and succeeds;
- an ambiguous order mutation failure is not replayed;
- logs contain no credential material;
- a read-only runtime sample no longer shows four auth bootstrap sequences per
  tick. One initial create/derive sequence for the process is acceptable.

## Slice 2: Remove Hot-Path Settings Reload Failure

1. Pass the already-loaded `Settings` instance into every
   `build_httpx_client()` / `safe_http_read()` call owned by
   `GammaPolymarketClient`. Do not construct `Settings()` again inside those hot
   paths.
2. Let `DiscoveryService` reuse the settings already owned by its Polymarket
   client when fetching the weather page. Do not create a second config owner.
3. Make the optional `LLM_ENABLED` input tolerate a blank value as disabled, or
   prevent the desktop/config writer from producing the blank value. Choose one
   narrow source-of-truth fix and test it. Do not silently coerce invalid risk,
   trading, credential, or monetary settings.
4. Prove a transient blank optional LLM setting cannot terminate discovery,
   reconciliation, exit management, or the complete tick.

Do not redesign Settings or add a generic configuration framework.

## Slice 3: Two-Phase Discovery Without Per-Bucket CLOB Reads

The current bottleneck is `DiscoveryService._persist_discovered_market()` calling
`get_order_book()` for every bucket returned by every event. Real Gamma payloads
already contain preliminary `bestBid`, `bestAsk`, `lastTradePrice`, `spread`, and
liquidity fields.

1. Keep discovery metadata-first:
   - fetch event/Gamma metadata;
   - persist market lifecycle/rule/candidate state;
   - derive a preliminary `MarketSnapshot` from valid Gamma summary quote fields
     and persist it through the existing snapshot API with explicit provenance in
     `raw_payload` (for example `source='gamma-summary'`).
2. Do not fetch a token CLOB book for every discovered bucket when valid Gamma
   summary quotes exist.
3. If Gamma summary quotes are missing or invalid, do not fan out to all markets.
   Use an explicitly bounded fallback selected by current/future eligibility and
   candidate priority. Keep the bound small and test the call count.
4. Exact token-specific CLOB books remain mandatory for:
   - reconciled open positions and exits where currently required;
   - stale/open order lifecycle where currently required;
   - the final selected live entry immediately before `TradingService` submits.
5. Preliminary Gamma quotes must never be presented as the final executable live
   quote. Preserve `_refresh_live_order_book()` and stale-book rejection.
6. Preserve current event ordering (dynamic current dates before scraped stale
   dates), local-day eligibility, at-most-one BUY per tick, and batch forecast
   reuse.
7. Keep synchronous architecture. Do not introduce asyncio, threads, queues, a
   second scheduler, or WebSocket scope in this task.

Deterministic acceptance targets:

- an event fixture with 100+ bucket markets and valid Gamma quotes performs zero
  per-bucket CLOB book calls during discovery;
- missing-quote fallback has a tested strict upper bound, not proportional to all
  discovered buckets;
- final live entry still performs one fresh token-specific CLOB read and rejects
  on refresh failure;
- position reconciliation/exit always happens before optional discovery work;
- repeated fresh metadata/analysis does not trigger redundant forecast or book
  work;
- mocked routine tick duration is well below its derived budget and reports a
  truthful deferred count.

## Slice 4: Actionable Reconciliation Errors And Quiet Recovery

1. Keep fail-stop: any incomplete balance/order/trade/position read blocks cancel,
   exit, and entry for that tick.
2. In `ReconciliationService`, identify the failed read stage (`balances`,
   `orders`, `trades`, or `positions`) and return a concise, redacted error type
   and message. Do not duplicate the reconciliation pipeline.
3. Persist the same stage detail in the existing reconciliation row and log it.
4. Telegram behavior:
   - notify on transition from healthy to failed;
   - suppress identical repeated failures while still logging each tick;
   - notify once when reconciliation recovers;
   - preserve all BUY, SELL, and fill notifications.
5. Reuse existing notification de-duplication/state mechanisms. Add no heartbeat,
   notification table, or new cooldown setting unless reuse is demonstrably
   impossible and Codex approves first.
6. A commit failure after successful reconciliation remains fail-stop and must not
   permit later cancel/exit/entry work.

Required tests must prove each read stage failure prevents all later exchange
mutations, records the stage, deduplicates Telegram, and emits one recovery event.

## Slice 5: Phase Timing And Runtime Proof

1. Add start/finish duration logging around existing phases only:
   - compliance/blockers;
   - reconciliation;
   - stale-order lifecycle;
   - position refresh + auto-exit;
   - discovery metadata;
   - candidate analysis;
   - final entry preparation/execution.
2. Include tick identity, elapsed milliseconds, item/request counts when already
   available, deferred count, and concise failure reason. Keep redaction active.
3. Do not add schema fields merely for phase detail. Existing total duration and
   deferred fields remain the `/app` read model; detailed phase evidence belongs
   in rotating logs.
4. Update the existing full-auto runbook with commands to inspect:
   - auth bootstrap count;
   - phase durations;
   - reconciliation failure stage;
   - total tick duration;
   - deferred candidates.

Report before/after deterministic measurements and, if a read-only temporary-DB
network sample is run, report its environment and variability separately. Do not
claim a production latency guarantee from one network sample.

## Required Regression Tests

At minimum add or strengthen tests proving:

1. secure SDK client is created once across all reconciliation reads;
2. cached client is reused on a second tick;
3. read auth expiry invalidates/recreates once;
4. ambiguous BUY/SELL/cancel failure is never replayed;
5. absent `get_order` is normalized cleanly;
6. Gamma adapter HTTP reads never instantiate a replacement Settings object;
7. blank `LLM_ENABLED` cannot fail the tick;
8. 100+ Gamma-quoted markets cause zero discovery CLOB fan-out;
9. missing quote fallback is strictly bounded;
10. exact live-entry quote refresh remains mandatory;
11. all four reconciliation read-stage failures are fail-stop and identifiable;
12. repeated identical reconciliation failure sends one Telegram alert;
13. recovery sends one Telegram recovery alert;
14. phase durations and counts are logged without secrets;
15. daily cap counts `matched`, `partially_filled`, and
    `partially_filled_closed` BUY intents;
16. disappeared unfilled orders become `cancelled` after grace;
17. disappeared partial orders become `partially_filled_closed` and no longer
    block a later exit;
18. stale target-date forecast failures cannot restore an old positive Edge;
19. reconciliation/exit still precedes discovery and entry;
20. at most one new BUY is possible per tick.

All network and exchange mutation behavior must be mocked in tests. Do not read
or alter the user's live database or `.env`.

## Commit Structure And Completion Gate

Prefer four reviewable commits:

1. reuse authenticated SDK session and remove hot Settings reloads;
2. metadata-first bounded discovery;
3. reconciliation stage errors and notification recovery;
4. phase timing, runbook, and final regressions.

Before reporting completion run exactly:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

Push normally to `main`; never use `git push --force`.

The worker report must follow `docs/agent-worker-standards.md` and include:

- baseline and final commit hashes;
- existing components reused;
- new files/classes/settings/tables, expected to be none or explicitly justified;
- duplicate code removed;
- exact secure-client creation counts before/after;
- discovery CLOB call counts before/after on the same fixture;
- deterministic phase timings before/after;
- exact test and lint results;
- remaining worktree state;
- explicit statement that no real trading mutation was executed.

Stop and request Codex review before proceeding if the implementation appears to
require a second client adapter, reconciliation service, scheduler, execution
path, database table, persistent mode, or any real-money action.
