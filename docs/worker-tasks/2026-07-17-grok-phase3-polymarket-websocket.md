# Grok Worker Task: Phase 3 Polymarket WebSocket Inputs

## Objective

Complete Phase 3 of the hybrid streaming plan by feeding Polymarket Market
Channel and User Channel events into the existing serial Autopilot pulse loop.

The desired result is lower REST order-book traffic, faster cached repricing,
and faster reconciliation after account activity. This task must not create a
new strategy engine, scheduler, reconciliation path, BUY path, or SELL path.

This is an exchange-input and scheduling-efficiency slice. It must not change:

- probability formulas or weather-model weights;
- fee-aware edge formulas or entry thresholds;
- risk limits, exposure rules, or auto-exit policy;
- live eligibility, whitelist, or circuit-breaker semantics;
- the two-second `/app/stream` local SQLite transport.

No real BUY, SELL, cancel, redeem, or other exchange mutation may be executed.

## Current Baseline

At task-authoring time:

- branch: `main`;
- baseline commit: `9008c49` (`Silence benign dashboard client disconnects`);
- `polymarket-client`: `0.1.0b16`;
- Phase 1 is complete: `/app/stream` polls bounded local SQLite deltas;
- Phase 2 is complete: `dashboard._run_autopilot_background` owns one serial
  two-second pulse loop;
- `AutopilotService.pulse()` owns capital maintenance, slow refresh, cached
  repricing, and health pulses;
- bounded REST order-book refresh remains the current fallback;
- `TradingService` and `PositionExitService` remain the only mutation owners.

Before editing, pull current `main` normally and re-check these facts. Do not
force-push. Preserve all unrelated or untracked user files.

## Mandatory First Reads

Read these before planning or editing:

1. `AGENTS.md`
2. `docs/agent-worker-standards.md`
3. `docs/worker-tasks/2026-07-16-hybrid-streaming-autopilot.md`
4. `src/polymarket_weather_arb/dashboard.py`
5. `src/polymarket_weather_arb/services/autopilot_service.py`
6. `src/polymarket_weather_arb/adapters/polymarket/client.py`
7. `src/polymarket_weather_arb/storage/repositories.py`
8. `tests/test_autopilot_pulse.py`
9. `tests/test_app_stream.py`

Official protocol references:

- Market Channel: <https://docs.polymarket.com/market-data/websocket/market-channel>
- User Channel: <https://docs.polymarket.com/market-data/websocket/user-channel>
- WebSocket overview: <https://docs.polymarket.com/market-data/websocket/overview>

## Critical Discovery: Reuse The Official SDK

Do not hand-roll the WebSocket protocol and do not add a direct `websockets`
dependency. The installed official SDK already exposes:

```python
from polymarket import AsyncPublicClient, AsyncSecureClient
from polymarket.streams import MarketSpec, UserSpec
```

Verified current signatures:

```python
AsyncPublicClient.subscribe(MarketSpec(...))
AsyncSecureClient.create(private_key=..., wallet=...)
AsyncSecureClient.subscribe([MarketSpec(...), UserSpec(...)])
```

The SDK already owns:

- CLOB `PING` / `PONG` heartbeats;
- reconnect backoff;
- resubscription after reconnect;
- public event parsing into typed Pydantic models;
- authenticated User Channel credentials;
- bounded subscription queues.

Do not import from `polymarket._internal.*`. Do not copy SDK protocol frames,
credential derivation, heartbeat, reconnect, or parser code into this project.
If the public SDK API is insufficient, stop and report the exact missing public
capability before creating replacement infrastructure.

## Authoritative Reuse Map

| Concern | Existing owner to reuse |
| --- | --- |
| Process-level scheduling | `dashboard._run_autopilot_background` |
| Strategy phases and decisions | `AutopilotService` |
| Cached sibling-group repricing | `MarketWorkflowService` through existing Autopilot calls |
| Exchange REST fallback | `GammaPolymarketClient` |
| Exchange truth for balances/orders/fills/positions | `ReconciliationService` |
| Persisted quote snapshots and state | `Repository` and existing SQLite schema |
| Live BUY | `TradingService` only |
| Live SELL | `PositionExitService` only |
| Browser updates | existing `/app/stream` SQLite endpoint |

One new production adapter file is permitted:

`src/polymarket_weather_arb/adapters/polymarket/stream.py`

It may contain a thin `PolymarketStreamBridge` and its small normalized event
records. It must not contain strategy, SQL, BUY, SELL, reconciliation, or risk
logic. Do not create a new `*Service` or database table.

## Required Architecture

### One I/O listener, not a second strategy scheduler

The bridge may own one daemon thread with one asyncio event loop solely because
the official stream clients are async while the current product loop is sync.
That thread may only:

1. create and close official SDK async clients and subscription handles;
2. normalize typed SDK events;
3. coalesce them into a bounded thread-safe in-memory queue;
4. publish non-secret connection health.

It must never open SQLite, call Repository, reprice a market, reconcile the
account, or submit/cancel an order.

The existing Autopilot pulse thread must drain and apply queued events. All SQL,
repricing, reconciliation, and possible mutation therefore stay serialized in
the canonical pulse loop.

### Bounded and coalesced queue

Do not enqueue an unbounded event stream.

- Coalesce Market Channel quote events by `token_id`; keep only the newest
  pending top-of-book state per token.
- Coalesce User Channel activity into a durable-in-process `reconcile_due`
  signal plus bounded diagnostic metadata.
- Cap non-coalescible diagnostic events.
- Expose dropped/coalesced counters in health, without logging payload dumps.
- A full queue must never block the SDK reader thread.

### Official stream clients

- Public-only mode may use `AsyncPublicClient` with `MarketSpec`.
- When live credentials are valid, use `AsyncSecureClient.create` with the same
  `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_FUNDER` wallet path already used by
  the official sync client.
- Use `UserSpec()` without a markets filter unless the public SDK or live test
  proves this is unsupported. Account-wide user events avoid a race where a
  newly submitted order is not yet part of the subscription set.
- Credentials stay inside the adapter/SDK. Never return them, persist them,
  include them in health JSON, or log subscription auth frames.
- Do not add independent CLOB API-key settings. The official SDK derives and
  owns the authenticated credentials.

## P0 Prerequisite: Token-Aware Quote Persistence

The current `market_snapshots` table is keyed only by local `market_id`, while
WebSocket quote events are keyed by outcome `token_id`. Persisting YES and NO
quotes into one undifferentiated history can corrupt pricing and exit previews.

Before allowing WebSocket quotes to affect cached repricing:

1. Add nullable `token_id TEXT` to `market_snapshots` through the existing
   additive schema-repair pattern. Do not discard or rewrite history.
2. Add an index suitable for latest `(market_id, token_id)` lookup.
3. Extend `Repository.save_market_snapshot(..., token_id=None)` without breaking
   old callers.
4. Extend `Repository.latest_market_snapshot(market_id, token_id=None)`:
   - when `token_id` is supplied, return only that token's rows;
   - do not silently use a different outcome token;
   - execution-sensitive callers must pass the intended token.
5. Make all new REST and WebSocket snapshots persist the explicit token ID.
6. Existing legacy rows with `token_id IS NULL` remain historical. A missing
   token-specific snapshot must trigger the existing slow REST refresh rather
   than unsafe fallback to an ambiguous row.

Source of truth: exchange asset/token ID carried by REST or typed SDK events.
Retention: identical to existing `market_snapshots`; no new retention mechanism.

This migration is an acceptance blocker. Do not bypass it by assuming every
quote is YES or by converting a NO quote algebraically.

## Subscription Selection

Build the desired Market Channel token set from existing Repository data.

Priority order:

1. token IDs of every nonzero reconciled position;
2. token IDs of every open reconciled order;
3. token IDs needed by a bounded Top-N set of complete ranked weather event
   groups from `list_ranked_weather_opportunities` and existing group identity;
4. fair D0/D1/D2 rotation for remaining candidate capacity.

Rules:

- Held positions and open orders are never displaced by Top-N candidates.
- Top-N means complete `(city, target_date)` sibling groups, not arbitrary
  individual buckets.
- Begin with a code constant of 10 candidate groups. Do not add an environment
  setting in this slice.
- Subscribe only to token IDs actually needed by the analysis side, position,
  or open order. Do not subscribe to both outcomes by habit.
- De-duplicate token IDs and preserve a token-to-local-market mapping.
- Recompute the desired set inside an existing slow/capital pulse, not every
  two seconds.
- Debounce subscription changes. Do nothing when the desired set is unchanged.
- Use public SDK subscription handles. To replace a set, subscribe the new
  handle before closing the old handle, then rely on event de-duplication during
  the short overlap.

Do not create a persistent subscription table. The set is derived from current
positions, orders, and ranked opportunities after restart.

## Event Handling Semantics

### Market Channel

Support the typed official events needed for this project:

- `MarketBookEvent` for initial/full book bootstrap;
- `MarketBestBidAskEvent` for direct BBO changes;
- `MarketPriceChangeEvent` when it contains updated best bid/ask;
- `MarketTickSizeChangeEvent` as a reason to refresh/revalidate;
- `MarketResolvedEvent` as a reason to schedule settlement/reconciliation work.

Ignore unrelated event types safely and count them. Do not fail the stream for
an unknown future event type.

A quote event is meaningful only when its normalized best bid or best ask for
that exact token changed. Depth-only churn with unchanged BBO must not trigger
cached repricing.

Applying a meaningful quote in the serial pulse must:

1. resolve `token_id` to the existing local market;
2. persist a token-aware `MarketSnapshot` using the existing Repository;
3. mark that complete sibling group due for cached repricing;
4. coalesce repeated changes so at most one pending reprice exists per group;
5. never submit an order directly from the stream event.

### User Channel

Support typed `UserOrderEvent` and `UserTradeEvent` only as low-latency hints.

- Any placement/update/cancellation/trade lifecycle event sets
  `capital_due_after_mutation=True` and makes capital maintenance immediately
  due on the next serial pulse.
- Do not write a fill, order, position, or intent directly from a User Channel
  event.
- `MATCHED`, `MINED`, and even `CONFIRMED` messages do not replace the existing
  REST reconciliation source of truth.
- The next pulse must reconcile first. BUY, SELL, cancel, and repricing that
  depends on capital remain blocked if reconciliation fails.
- Duplicate User Channel messages must collapse into one reconciliation request.

This preserves the existing fail-stop and fill de-duplication behavior.

## Pulse Integration And Ordering

Extend the existing `AutopilotPulseState`; do not add another scheduler state
machine.

At each pulse:

1. drain/coalesce stream signals without blocking;
2. if a User Channel signal is pending, make capital maintenance due;
3. capital maintenance keeps highest priority;
4. only after successful reconciliation may token-aware market quotes be
   persisted and their groups repriced;
5. process at most one complete cached reprice group per pulse;
6. all existing idempotency and active-intent checks remain unchanged;
7. after any live mutation, immediate reconciliation behavior remains unchanged.

The stream bridge must be started and stopped by the existing dashboard
Autopilot process lifecycle. `Ctrl+C`, desktop Quit, test teardown, and startup
failure must close subscription handles, async clients, loop, and thread within
a bounded timeout.

Keep `AutopilotService.tick()` working for deterministic `--once` and existing
tests. `--once` does not need to start WebSocket streams.

## REST Fallback And Failover

WebSocket is an optimization, never the sole execution truth.

Keep REST for:

- startup snapshots before stream data is trusted;
- periodic bounded verification;
- reconnect/stale recovery;
- exact pre-submit order-book verification;
- reconciliation of balances, orders, fills, and positions;
- token-specific exit checks.

Required behavior:

- Before the first full `book` or trustworthy BBO for a token, use REST.
- If the bridge cannot start, authentication fails, or stream data becomes
  stale, mark the stream degraded and continue the existing bounded REST path.
- Stream failure must not stop Autopilot and must not disable reconciliation.
- Do not mark a quiet authenticated User Channel stale solely because no account
  event occurred.
- Public market staleness is judged per subscribed token using the last valid
  book/BBO timestamp plus periodic REST verification.
- Keep the final pre-submit REST recheck even when WebSocket is healthy.
- Demonstrate a measurable reduction in recurring REST book reads for healthy
  subscribed candidates; do not claim zero REST traffic.

Do not reach into `polymarket._internal` to detect socket state. Use only public
handle/client behavior plus timestamps and caught errors. If exact reconnect
state is not observable publicly, report `degraded/unknown` honestly and retain
REST fallback.

## Persisted Health And `/app`

Do not add a stream-history table. Add only the minimum additive fields to the
existing singleton `autopilot_state` needed for cross-thread dashboard reads:

- `exchange_stream_status`: `disabled`, `connecting`, `live`, `degraded`, or
  `stale`;
- `exchange_stream_updated_at`;
- `exchange_stream_detail`: bounded JSON containing only non-secret counts and
  timestamps, such as subscribed token count, market/user last-event time,
  coalesced/dropped count, and REST fallback state.

The fields are overwritten snapshots, not historical records. Reset them to
`disabled` or a clear stopped state on clean shutdown.

Update the existing `/app` health surface without adding a page:

- continue labeling `/app/stream` itself as local SQLite transport;
- separately show Exchange WS as `Live`, `Degraded`, `Stale`, or `Disabled`;
- show subscribed asset count and whether REST fallback is active;
- never show credentials, wallet secrets, raw subscription frames, or full
  event payloads;
- no browser JavaScript may connect directly to the authenticated User Channel.

Do not send Telegram messages for normal quote traffic. At most, reuse the
existing notifier for a deduplicated degraded/recovered state transition if it
can be done without creating a notification subsystem. Otherwise leave this to
logs and `/app`.

## Required Tests

Use fake official async clients/handles. Unit and integration tests must not
connect to real WebSockets or execute real exchange mutations.

### SDK and bridge

- imports and uses `AsyncPublicClient` / `AsyncSecureClient`, `MarketSpec`, and
  `UserSpec` through public APIs;
- no direct `websockets` import or new dependency;
- bridge startup and bounded shutdown;
- queue is bounded and quote events coalesce by token;
- secrets and auth payloads never enter logs or health state;
- unknown event variants are ignored/countable without killing the bridge.

### Subscription selection

- all nonzero-position and open-order tokens are included;
- Top-N selection uses complete sibling groups;
- duplicate tokens are removed;
- held/open tokens survive candidate cap pressure;
- unchanged sets do not resubscribe;
- changed sets replace handles without losing the new set;
- restart derives the set without a new persistence table.

### Token-aware snapshots

- YES and NO token snapshots for one local market cannot overwrite each other;
- execution-sensitive lookup returns only the requested token;
- legacy NULL-token rows are not used as another token's fresh quote;
- REST and WebSocket saves both populate token ID;
- missing token-specific data schedules existing REST refresh.

### Market events and repricing

- full book and BBO events normalize Decimal values correctly;
- unchanged BBO causes no reprice;
- repeated BBO changes for one token cause one coalesced pending group reprice;
- one token update reprices only its complete sibling group;
- stream callback never calls weather, LLM, BUY, SELL, cancel, or Repository;
- final live pre-submit REST verification still runs.

### User events and reconciliation

- order/trade event marks capital maintenance due;
- duplicate events cause one reconciliation request;
- no event directly inserts fills/orders/positions;
- reconciliation success persists exchange truth through existing methods;
- reconciliation failure fail-stops cancel/SELL/BUY for that capital pulse;
- a User Channel event received during cached work is handled on the next serial
  pulse without overlap.

### Failover and UI

- bridge startup failure retains REST fallback and does not kill Autopilot;
- stale market stream re-enables REST refresh;
- quiet User Channel is not falsely treated as a failure;
- health fields contain no secret material and are bounded;
- `/app` reports local SQLite transport separately from Exchange WS source;
- browser polling still performs zero upstream calls;
- the recent benign `BrokenPipeError` suppression remains covered.

## Optional Read-Only Smoke Tests

Only after all mocked tests pass:

1. Ask the user for explicit permission before any real network smoke test.
2. A public Market Channel smoke may subscribe to one currently active token for
   30-60 seconds and print only event type, token ID suffix, and BBO.
3. An authenticated User Channel smoke requires separate explicit permission.
4. Do not print credentials or full payloads.
5. Do not start `--full-auto` as part of smoke testing.
6. Do not place, cancel, or close an order.

If proxy behavior blocks the official SDK, report the exact error and verify
that REST fallback remains healthy. Do not disable TLS verification or build a
custom proxy/WebSocket implementation in this task.

## Commit Plan

Keep one behavioral objective per commit:

1. `Make market snapshots token-aware`
2. `Add official Polymarket stream bridge in shadow mode`
3. `Route stream signals through the serial Autopilot pulse`
4. `Expose stream health and REST fallback in app`

Normal commits and normal push only. Do not amend or force-push shared history.
Do not stage unrelated files.

## Quality Gates

Run targeted tests after each commit, then:

```bash
UV_CACHE_DIR=/tmp/pwa-uv-cache uv run ruff check src/ tests/

MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 \
  UV_CACHE_DIR=/tmp/pwa-uv-cache uv run pytest -q

git diff --check
git status --short
```

If local-socket browser/desktop tests fail with sandbox `PermissionError`, rerun
the same full suite in an environment allowed to bind a temporary loopback port.
Do not mark those tests passed without the rerun.

## Acceptance Criteria

Phase 3 is accepted only when all are true:

1. Official SDK public stream APIs are reused; no low-level WebSocket wheel was
   invented.
2. The bridge cannot access Repository, strategy, BUY, SELL, or cancel paths.
3. All DB and strategy work remains serialized in the existing pulse loop.
4. User events force reconciliation before capital-dependent work.
5. WebSocket messages never directly create a confirmed fill or position.
6. Token-aware quote persistence prevents YES/NO snapshot contamination.
7. Held positions and open orders are always subscribed.
8. Candidate subscriptions are bounded and group-aware.
9. Meaningful BBO changes trigger only the relevant complete group.
10. The final pre-submit REST verification is unchanged.
11. Stream failure visibly degrades to bounded REST polling.
12. `/app/stream` remains SQLite-only and does not expose credentials.
13. REST order-book reads fall measurably for healthy subscribed candidates.
14. Existing pricing, risk, idempotency, reconciliation, BUY, SELL, and exit
    tests remain green.
15. Clean shutdown leaves no stream thread or async task running.
16. No real trading mutation was executed.

## Stop Conditions

Stop and ask Codex for review before continuing if any of these occurs:

- implementation appears to require a new strategy service, scheduler, BUY,
  SELL, reconciliation path, or database table;
- the official SDK public stream API cannot support the required lifecycle;
- token-aware snapshot migration causes core pricing or exit ambiguity;
- a WebSocket event would need to mutate orders/fills/positions directly;
- authenticated streaming requires persisting derived API credentials;
- stream failure could disable the existing REST fallback;
- more than the one permitted adapter file or the three additive health fields
  appears necessary;
- a real-money action would be required to prove correctness;
- current live process must be stopped or restarted without user permission;
- a force-push or destructive git operation appears necessary.

## Required Worker Report

Return a report containing:

- objective completed or exact incomplete scope;
- current HEAD and all commit hashes;
- existing components reused;
- every production file added, with justification;
- every schema column added and its source/lifecycle;
- duplicate or obsolete code removed;
- official SDK APIs actually used;
- subscription selection and cap behavior;
- event-to-pulse ordering;
- REST fallback behavior and before/after request-count evidence;
- exact targeted and full test results;
- `ruff`, `git diff --check`, and `git status --short` results;
- remaining working-tree files and whether they pre-existed;
- whether any real network smoke ran;
- explicit statement: real trading mutation executed or not executed;
- explicit statement: force-push executed or not executed.

