# Hybrid Streaming Autopilot Implementation Plan

## Implementation Status

- Phase 0 complete: serial scheduler and fail-stop behavior are characterized by tests.
- Phase 1 complete: `/app/stream` polls bounded SQLite deltas every two seconds and
  never calls weather, Gamma, CLOB, or LLM providers.
- Phase 2 complete: the existing dashboard scheduler now owns one serial
  multi-cadence pulse loop. Capital maintenance, slow weather/discovery refresh,
  bounded REST quote refresh, and cached sibling-group repricing remain separate
  due work classes.
- Phase 3 complete: official `polymarket` SDK Market/User Channel events feed a
  thin `PolymarketStreamBridge` into the existing serial Autopilot pulse. Quotes
  are token-aware; User Channel only schedules reconciliation. REST remains the
  startup, stale, pre-submit, and recon fallback. `/app/stream` stays SQLite-only.

The production implementation preserves the original full-cycle `tick()` API,
all existing BUY/SELL owners, live gates, risk caps, and reconciliation fail-stop.
No new scheduler, strategy service, table, or configuration switch was added.

## Objective

Evolve `/app` from a 300-second batch-looking console into an honest streaming
operator experience without turning every visual refresh into a full weather,
LLM, discovery, reconciliation, or trading cycle.

The target behavior is:

- UI events update every 1-2 seconds from local persisted state;
- one complete city/date event group can be repriced from cached inputs every
  1-3 seconds;
- expensive upstream data is refreshed only when its source can provide new
  information;
- order, fill, and relevant order-book changes can eventually arrive through
  Polymarket WebSocket channels;
- all BUY and SELL mutations continue to flow only through `TradingService` and
  `PositionExitService`;
- `AutopilotService` remains the only autonomous strategy owner.

This is an execution-efficiency and operator-visibility change. It must not
change probability formulas, entry thresholds, risk caps, exit policy, or live
eligibility semantics in the same slice.

## Why Not Run The Existing Tick Every Second

The current `AutopilotService.tick()` performs capital reconciliation, stale
order handling, position refresh, automatic exit evaluation, discovery,
multi-provider forecast research, LLM review, candidate selection, and possible
live execution. One invocation can therefore fan out into many remote requests.

Running that method once per second would create:

- overlapping or re-entrant live mutation attempts;
- duplicate forecast, geocoding, Gamma, CLOB, NOAA, Google, and LLM calls;
- provider rate-limit bursts and unnecessary paid API usage;
- incoherent bucket distributions if sibling markets are evaluated at different
  input revisions;
- repeated reconciliation and excess SQLite writes;
- a busy-looking UI that does not represent newly available weather data.

The correct unit of fast work is a complete `(city, target_date)` event group
using cached weather inputs, not one independent bucket and not one full tick.

## Authoritative Ownership And Reuse Map

Existing owners to extend:

- `dashboard._run_autopilot_background`: the one process-level scheduler loop;
- `AutopilotService`: phase orchestration, candidate selection, and all decisions;
- `MarketWorkflowService.research_global_bucket_batch`: sibling-group pricing;
- `Repository`: persisted state and read-only stream queries;
- `dashboard.py` and `dashboard_ui/stream_panel.py`: `/app` stream transport and
  rendering;
- `GammaPolymarketClient`: existing REST exchange adapter and final pre-submit
  verification path;
- `TradingService` and `PositionExitService`: unchanged exclusive live mutation
  owners.

Do not create:

- a second daemon, scheduler, strategy engine, BUY path, SELL path, or
  reconciliation loop;
- a separate stream database or duplicate decision ledger;
- a background worker that can trade independently of `AutopilotService`;
- fake chart samples, demo throughput, or generated activity events.

One new adapter file is permitted only in Phase 3 for the external Polymarket
WebSocket protocol. It must feed snapshots/events to existing owners and must
not make trading decisions.

## Desired Cadences

| Work | Initial cadence | Notes |
| --- | ---: | --- |
| `/app` local event refresh | 2 seconds | SQLite/read-only HTTP only |
| cached event-group repricing | 2 seconds per group | no weather/Gamma/LLM call |
| capital reconciliation | 60 seconds | also immediately after mutation/fill |
| open position/exit evaluation | 10-30 seconds | only held/open assets |
| REST order-book fallback | 15-30 seconds | top candidates and live inventory |
| weather market discovery | 5-10 minutes | retain fair D0/D1/D2 rotation |
| D0 official observations | 5-10 minutes | only supported stations/cities |
| weather forecasts | 15-30 minutes | keyed by provider/location/revision |
| LLM group vote | new forecast revision only | at most one top group at a time |

Cadences must be jittered where they make upstream calls. A fixed wall-clock
boundary must not produce synchronized request bursts.

## Phase 0: Baseline And Characterization

### Scope

Before changing behavior, capture tests for the existing scheduler and measure:

- start-to-start tick spacing;
- phase duration for reconciliation, exit, discovery, and analysis;
- remote request counts by provider and phase;
- deferred group backlog;
- decision, intent, attempt, fill, and position deltas per tick;
- behavior when reconciliation fails or the process is interrupted.

### Required tests

- one running scheduler owns live work at a time;
- a long cycle does not overlap the next cycle;
- reconciliation failure prevents cancel, SELL, and BUY for that capital cycle;
- duplicate-order/idempotency behavior remains unchanged;
- exchange-accepted attempts remain durable if later stream bookkeeping fails.

### Acceptance

The baseline must reproduce current behavior without a real network mutation.

## Phase 1: Honest Streaming UI From Local State

### Scope

Add a read-only delta endpoint under the existing dashboard, for example:

`GET /app/stream?after_decision_id=<id>&after_fill_id=<id>`

It should return only persisted rows newer than the supplied cursors plus the
latest autopilot health snapshot. Poll it from `/app` every two seconds. Prefer
small JSON delta polling for the first slice; it is easier to bound, test, stop,
and recover than a long-lived SSE response in the current stdlib server.

### Repository reuse

Add bounded Repository queries over existing tables:

- `autopilot_decisions` for decision events;
- `fills` for confirmed executions;
- `order_intents` and `order_attempts` for submitted/open/cancelled state;
- `autopilot_state` for tick health;
- `analyses`/market snapshots for real edge and price history when present.

Do not add a table in this phase. If a chart cannot be derived honestly from an
existing timestamped record, show no chart rather than synthesizing points.

### UI behavior

- append new feed rows without full-page reload;
- cap rendered history to a fixed number of points/rows;
- display connection state: `live`, `reconnecting`, or `stale`;
- use browser-local time formatting while retaining UTC in tooltips/details;
- replace the current funnel-shaped sparkline with a true time-axis series;
- keep the opportunity funnel as a separate categorical visualization;
- never label the local DB stream as a live exchange feed.

### Suggested real charts

- selected candidate net edge over time;
- market ask/bid versus model median probability;
- groups analyzed and deferred per minute;
- tick/phase duration and failures;
- confirmed fills and reconciled position value.

### Acceptance

- `/app` visibly updates within three seconds after a test row is committed;
- no external API call occurs as a result of UI polling;
- refreshing/reconnecting does not duplicate rows;
- an idle system remains visually honest instead of generating activity;
- existing setup, controls, CSRF, safety state, and navigation remain intact.

## Phase 2: One Canonical Multi-Cadence Scheduler

### Scope

Refactor `dashboard._run_autopilot_background` and `AutopilotService` into a
single serial pulse loop with due work classes. Do not create an independent
queue worker.

The scheduler wakes approximately every two seconds, but most wakes perform no
remote request. It evaluates due timestamps and runs at most one of these paths:

1. capital maintenance: reconciliation, stale order handling, and live inventory;
2. slow input refresh: discovery, weather forecast, observation, and LLM revision;
3. cached event-group repricing;
4. no-op health pulse.

Capital maintenance always has priority. Live mutation remains serialized under
the existing service call and database transaction boundaries.

### Event-group queue

- queue identity is `(city, target_date, forecast_revision)`;
- all sibling buckets are evaluated together;
- live positions and open orders receive priority;
- remaining D0/D1/D2 groups keep the existing fair rotation;
- a newer forecast revision supersedes an unprocessed older revision;
- at most one group is being repriced by the scheduler at a time;
- a group cannot submit more than one live entry while an active intent exists;
- queue state should initially be derived from existing candidates/analyses and
  kept in process memory; do not add persistence until restart behavior proves
  that it is required.

### Cached-only fast path

Add or extract one clearly named method on the existing workflow owner that:

- loads the latest complete provider forecast revision for the group;
- loads the latest persisted order-book snapshot;
- recomputes fee-aware probability/edge for all sibling buckets;
- persists analyses and an autopilot decision event;
- performs no geocoding, weather, Gamma, CLOB REST, or LLM request.

If cached inputs are stale or incomplete, enqueue/mark the appropriate slow
refresh and return a visible rejection reason. Do not silently fetch upstream
inside the cached-only method.

### Provider budgets

Reuse existing retry/backoff helpers and caches. Add one process-level budget per
provider only if no existing owner can enforce it. Each budget must support:

- minimum interval and bounded concurrency;
- jitter;
- `Retry-After` when supplied;
- exponential backoff for retryable failures;
- cooldown after repeated 429/5xx responses;
- permanent/long-lived negative caching for documented unsupported coordinates;
- metrics for attempted, served-from-cache, deferred, failed, and rate-limited.

Never retry 4xx errors that are not transient.

### Compatibility

Keep `AutopilotService.tick()` as the deterministic full-cycle API used by CLI
`--once` and existing tests. It may call the same extracted phase methods. The
background scheduler may call those phases separately, but must not duplicate
their business rules.

### Acceptance

- UI receives at least one honest local decision/health event every few seconds
  while work exists;
- Google/Open-Meteo/NOAA/Gamma request volume follows slow cadences rather than
  the two-second pulse;
- one complete rotation processes every eligible group without starvation;
- bucket probabilities for one event use one coherent input revision;
- no overlapping reconciliation or mutation path is possible;
- a 429 cools down only the affected provider and does not kill the process;
- all existing live risk, idempotency, order durability, and exit tests pass.

## Phase 3: Polymarket WebSocket Inputs

### Scope

After Phase 2 is stable overnight, add one external-protocol adapter for:

- Market Channel subscriptions for assets belonging to open positions, open
  orders, and a bounded Top-N candidate set;
- User Channel subscriptions for authenticated order and trade lifecycle events.

The adapter may normalize and persist exchange events. It must not select a
strategy action or call a BUY/SELL service.

### Subscription policy

- always subscribe to held positions and open orders;
- subscribe to only a bounded Top-N candidate set;
- update subscriptions when the ranked set changes, with debounce;
- trigger cached repricing only on a meaningful top-of-book change;
- retain REST as startup snapshot, reconnect backfill, and stale-feed fallback.

### Reliability requirements

- heartbeat and stale-feed detection;
- exponential reconnect with jitter;
- resubscribe after reconnect;
- event de-duplication;
- REST snapshot reconciliation after a sequence gap or reconnect;
- never infer a confirmed fill solely from a market price event;
- User Channel credentials remain server-side and redacted from logs/UI;
- WebSocket loss degrades to bounded REST polling and a visible warning.

### Acceptance

- simulated disconnect/reconnect does not duplicate fills or intents;
- a best-bid/ask change updates the relevant cached market only;
- a user order/fill event is reconciled before capital-dependent mutation;
- no WebSocket message can bypass TradingService, PositionExitService, or
  reconciliation;
- REST traffic falls measurably for subscribed markets.

## Test Plan

Run targeted tests throughout, then the complete gates:

```bash
UV_CACHE_DIR=/tmp/pwa-uv-cache uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 \
  UV_CACHE_DIR=/tmp/pwa-uv-cache uv run pytest -q
git diff --check
git status --short
```

Add focused tests for:

- local stream cursor ordering, reconnect, and bounded payloads;
- no external request caused by `/app` polling;
- scheduler due-time ordering and non-overlap;
- coherent sibling-group revisions;
- queue fairness and superseded work;
- per-provider cooldown and 429 isolation;
- reconciliation fail-stop across a fast scheduler pulse;
- duplicate-order prevention under repeated quote events;
- WebSocket reconnect, de-duplication, stale detection, and REST backfill;
- process restart with open order/position state;
- UI layout and truthful empty/stale states.

## Rollout Plan

1. Merge Phase 1 with strategy cadence unchanged at 300 seconds.
2. Run the UI locally for several hours and verify zero extra upstream traffic.
3. Merge Phase 2 with live trading disabled in tests and paper mode observation.
4. Run one overnight paper/observe session and compare provider call counts,
   backlog, tick failures, and candidate coverage against the baseline.
5. Run micro-live with existing caps; inspect every intent, attempt, fill, and
   exit before enabling full live.
6. Add Phase 3 only after Phase 2 is stable. Start with positions/open orders and
   a very small Top-N subscription set.

Each phase should be its own reviewable commit or PR. Do not combine a pricing
formula change, threshold change, or risk-cap change with this scheduler work.

## Stop Conditions

Stop for review if implementation:

- creates a second scheduler or strategy owner;
- allows concurrent live mutation phases;
- needs a new table before existing ledgers have been proven insufficient;
- changes entry/exit economics to make a scheduler test pass;
- causes UI polling to call an upstream provider;
- cannot prove sibling buckets use the same forecast revision;
- executes a real BUY or SELL without an exact current user confirmation.

## Expected Effort

- Phase 0 and Phase 1: 0.5-1.5 engineering days;
- Phase 2: 2-4 engineering days plus an overnight observation run;
- Phase 3: 4-7 engineering days plus reconnect/failure testing.

The recommended evening starting point is Phase 0 plus Phase 1 only. It improves
the console immediately and establishes honest time-series data without putting
the capital path or provider quotas at risk.
