# Antigravity Task: Autopilot Cycle Efficiency Without A New Scheduler

## Objective

Reduce a representative full weather research tick from about 60 seconds toward
30-35 seconds while preserving coverage, fair D0/D1/D2 rotation, provider retry
behavior, account-first reconciliation/exit ordering, and all BUY/SELL safety
invariants.

This is a separate commit from LLM voting. Do not mix the two objectives.

## Measured Baseline

From the 2026-07-14 isolated dry-run:

- total tick: `59,625 ms`;
- discovery: about `20,538 ms`, `275` persisted markets, `0` CLOB fallbacks;
- candidate analysis: about `37,301 ms`, `28` ready city/date groups;
- selected groups: `8`;
- analyzed sibling markets: `88`;
- reported deferred: `212` markets;
- the `212` value is mainly rotation backlog caused by the group cap, not a
  timeout or failure.

Use this baseline in the final comparison. Do not claim improvement from unit
tests alone.

## Mandatory Reading

- `AGENTS.md`
- `docs/agent-worker-standards.md`
- `services/autopilot_service.py`
- `services/discovery_service.py`
- `services/market_workflow_service.py`
- `adapters/http_reader.py`
- `storage/repositories.py`
- `tests/test_runtime_efficiency.py`
- `tests/test_discovery_service.py`
- `tests/test_market_workflow_service.py`
- `tests/test_autopilot_service.py`

## Reuse Map And Prohibitions

- `AutopilotService` remains the only scheduler/loop owner.
- `DiscoveryService` remains discovery owner.
- `MarketWorkflowService` remains forecast/analysis owner.
- Reuse current `tick_count`, phase logging, candidate rotation and HTTP retry.
- Do not create a scheduler, queue service, worker daemon, table, persistent mode,
  or async execution engine.
- Do not use a shared SQLite connection from worker threads.
- Do not alter TradingService, PositionExitService, reconciliation ordering,
  exposure limits, whitelist behavior, or order submission.
- Do not increase the number of Google/NOAA/Open-Meteo requests per city/date.

## Required Changes

### 1. Correct the backlog vocabulary and metrics

Separate these concepts in logs and UI using existing state/log surfaces:

- `rotation_backlog`: valid markets intentionally left for later fair rotation;
- `budget_deferred`: work skipped because remaining tick budget was exhausted;
- `failed`: requests/analyses that actually failed.

The persisted `deferred_candidates_count` may remain a total for compatibility,
but logs and `/app` must explain its components. A rotation backlog must not be
displayed as an execution failure.

### 2. Decouple expensive metadata discovery cadence

Use existing `autopilot_state.tick_count`; do not add settings or tables.

- Keep reconciliation, stale-order management, position refresh and auto-exit on
  every live tick and before research.
- Run full weather event slug discovery once every 3 ticks.
- On intervening ticks, analyze persisted candidates immediately.
- Force a full discovery earlier only when the persisted D0/D1/D2 candidate pool
  is empty or clearly below the existing useful group target.
- Preserve current D0/D1/D2 local-time filtering and fair rotation.
- Restart behavior must be deterministic from persisted `tick_count`.

Target: non-discovery ticks should save roughly the measured 20 seconds without
delaying new-market discovery by more than two normal tick intervals.

### 3. Add bounded parallelism only around pure network forecast reads

Parallelize unique city/date evidence fetches with a bounded standard-library
executor (`max_workers <= 3`). No new async framework.

Required structure:

1. Main thread reads repository rows, parses rules, selects groups and determines
   D0 observation requirements.
2. Worker calls perform only external read-only forecast/observation HTTP work
   and return immutable data/results.
3. Main thread alone writes forecasts, observations, analyses and candidates to
   SQLite in deterministic group order.

Never pass `Repository`, SQLite connection/cursor/Row, or transaction callbacks
into worker threads. Provider failures remain isolated to their group.

Within a city/date, preserve one shared forecast fetch for all sibling buckets.
NOAA, ensemble and Google request counts must not increase.

### 4. Preserve provider degradation semantics

- Google failure: continue with remaining models and record warning.
- NOAA forecast fallback behavior remains unchanged.
- Ensemble failure retains the existing deterministic fallback.
- HTTP 429/retry/backoff remains owned by `safe_http_read`.
- Bounded concurrency must not retry outside that owner or bypass rate limits.

### 5. Keep fair coverage

- Live-position groups remain eligible every tick.
- D0, D1 and D2 fairness remains deterministic.
- Rotation backlog drains across successive ticks; no city/date starvation.
- Unknown-timezone scraped markets retain their existing fallback queue behavior.
- Do not reduce coverage merely to produce a lower duration number.

## Tests Required

1. full discovery occurs on the selected cadence;
2. empty candidate pool forces discovery on an otherwise skipped tick;
3. reconciliation/exit still run every live tick;
4. three delayed fake group fetches run in bounded parallel time;
5. maximum active network workers never exceeds 3;
6. all repository writes occur on the calling/main thread;
7. result persistence order is deterministic despite completion order;
8. one provider/group failure does not cancel other groups;
9. request counts per provider/group do not increase;
10. sibling buckets still share one forecast result;
11. D0/D1/D2 rotation covers every group over successive ticks;
12. logs distinguish rotation backlog, budget deferral and failures;
13. live BUY/SELL paths are never called by efficiency tests;
14. existing fail-stop reconciliation tests remain green.

## Performance Acceptance

Use deterministic fake delays for the hard test gate:

- bounded parallel batch wall time must be less than 60% of equivalent sequential
  wall time;
- no more than three simultaneous network fetch workers;
- zero additional provider requests.

After unit/full tests, run one isolated real-data dry-run against a copied `/tmp`
database with trading, Telegram and auto-exit forced off. Report phase times. Do
not fail CI solely on external network timing, but the worker report must compare
the new phase durations with the measured baseline above.

## Verification

```bash
uv run pytest tests/test_runtime_efficiency.py tests/test_discovery_service.py \
  tests/test_market_workflow_service.py tests/test_autopilot_service.py -q
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

For the isolated smoke, copy the database to `/tmp` and explicitly set:

```bash
AUTOPILOT_MODE=dry_run
TRADING_DISABLED=true
AUTO_EXIT_ENABLED=false
TELEGRAM_NOTIFY_ENABLED=false
```

No real BUY/SELL, no live daemon restart, no force-push. Commit and normal-push
only after all gates pass. Report exact files, tests, before/after timings and any
remaining backlog.

