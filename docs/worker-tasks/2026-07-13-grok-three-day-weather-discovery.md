# Grok Worker Task: Three-Day Weather Discovery And Local-Day Forecasts

## Objective

Make the canonical `/app` Autopilot discover and analyze the real weather-event
universe shown on Polymarket for approximately `D0`, `D1`, and `D2`, without a
business-level `limit=11` or `limit=22` silently excluding future dates and
cities. Keep each tick bounded through a fair request/time budget, not by always
taking the first N slugs. Correct Open-Meteo daily forecasts to use each city's
local calendar day rather than UTC calendar days.

The economic goal is earlier access to potentially mispriced next-day markets
while preserving truthful forecast dates and avoiding discovery starvation.

## Evidence And Current Defect

On 2026-07-13, the live [Polymarket weather page](https://polymarket.com/weather)
contained temperature events for July 13, 14, and 15 across substantially more
than the 11 cities in `WEATHER_EVENT_CITIES`.

Current code behaves differently:

1. `dynamic_weather_event_slugs()` puts generated UTC-current-day slugs first.
2. Autopilot calls `discover_weather_events(limit=11)`.
3. The first 11 entries are exactly the hard-coded current-day cities.
4. Scraped future dates and additional cities are normally never fetched.
5. The database therefore had 121 July 13 bucket markets and zero July 14
   markets at 2026-07-13 19:07 Asia/Shanghai.
6. `OpenMeteoProvider` requests `timezone=UTC`, so daily high/low may not match
   the settlement city's natural calendar day.

Changing `11` to `22` is explicitly **not** an acceptable completion. It still
means 11 hard-coded cities times two days and continues to exclude the actual
weather-page universe.

## Mandatory Starting Point

1. Work in `/path/to/polymarket-weather-arb`.
2. Read `AGENTS.md` and `docs/agent-worker-standards.md` before planning/editing.
3. Start from latest `origin/main`, containing at least commit `b92aac2`.
4. Run `git status --short`; preserve unrelated user files and generated DMGs.
5. Do not stop, restart, signal, or modify the currently running live process.
   Source changes take effect only after Codex acceptance and a later deliberate
   restart by the user.
6. Do not execute any real BUY, SELL, cancel, approval, allowance, deposit, or
   other exchange mutation.

## Reuse Map And Ownership

Extend existing owners only:

- `services/discovery_service.py`: weather-page slug extraction, fallback slug
  generation, event fetch selection, and discovery persistence;
- `services/autopilot_service.py`: tick time budget and fair candidate-analysis
  group selection;
- `domain/market_eligibility.py`: existing city/station timezone resolution and
  local-day helpers;
- `adapters/weather/open_meteo.py`: Open-Meteo geocoding and forecast request;
- `MarketWorkflowService`, existing probability/pricing logic, `TradingService`,
  and existing live gates remain unchanged owners;
- existing tests such as `test_discovery_service.py`,
  `test_runtime_efficiency.py`, `test_autopilot_service.py`, and provider tests.

Do not create a new discovery service, scheduler, queue worker, database table,
persistent mode, BUY/SELL path, settings model, or weather-page parser service.
Do not parse unstable Next.js internals into a parallel market adapter; continue
using the existing page slug extraction plus `get_event_markets_by_slug()`.

## Required Behavior

### 1. Live Weather Page Is The Primary Event Universe

- Extract, normalize, and deduplicate all matching temperature event slugs found
  on the live weather page.
- Support city slug segments containing lowercase letters, digits, and hyphens;
  do not retain the current single-word-only regex assumption.
- Preserve cities not present in `WEATHER_EVENT_CITIES` when they appear on the
  page and their returned markets parse as supported weather markets.
- Treat generated hard-coded slugs as a fallback for weather-page fetch failure
  or narrowly as gap-fill. They must not always occupy the front of a capped
  list and starve scraped events.
- Keep page/network failure isolated: one bad page or event endpoint must not
  terminate the whole Autopilot tick.

Do not use the page's ordering as a permanent priority signal.

### 2. Separate Coverage From Per-Tick Work Budget

The system must distinguish:

- **coverage set**: every relevant deduplicated weather-page slug for D0-D2;
- **work set**: the bounded subset whose Gamma event endpoint is refreshed this
  tick.

Use the existing Autopilot tick budget. A small internal safety ceiling on event
HTTP requests is allowed, but it is a resource budget, not a market-universe
limit. Do not expose a new user configuration flag unless an existing setting
can own it and a concrete operator need is demonstrated.

Suggested posture:

- allocate a bounded discovery phase from remaining tick time;
- stop issuing new event reads when that phase budget is exhausted;
- keep a reasonable emergency request ceiling, approximately 25–30 event reads
  per tick, to protect the 300-second loop;
- log observed slug count, selected/read count, D0/D1/D2 distribution, failures,
  deferred count, and elapsed time through existing phase logging.

Exact constants may differ if tests/runtime evidence supports them. The design
must not fetch hundreds of event endpoints sequentially in every tick.

### 3. Fair Rotation Without A New Scheduler Or Table

When the coverage set exceeds one tick's budget:

- do not repeatedly process the first N slugs;
- rotate deterministically across successive tick/time buckets;
- ensure cities and dates beyond the first page segment eventually receive a
  turn after restart-safe repeated page fetches;
- interleave date bands so D1 and D2 cannot be starved by a large D0 set;
- prioritize stale/unseen work using existing persisted markets, snapshots,
  forecasts, and analyses where practical.

Prefer a small pure selection helper with injectable `now`/rotation slot for
tests. Do not use Python's randomized `hash()` as a persistent ordering key.
Use stable sorting/rotation. A new database cursor/table is not justified for
this slice.

Acceptance property: with a fixed coverage set and budget smaller than that set,
successive deterministic slots select different groups and cover the entire set
within a bounded number of slots.

### 4. Analyze D0, D1, And D2 Without Group Starvation

`_prepare_global_bucket_candidates(max_groups=6, ...)` currently selects the
first city/date groups it encounters. Refine that existing method or a small
helper at the same layer so:

- eligible D0, D1, and D2 city/date groups all receive analysis capacity;
- D1 has meaningful recurring capacity because early-price discovery is the
  economic objective;
- different cities within one date band rotate fairly;
- already fresh groups do not consume unnecessary research calls;
- open-position analysis and AutoExit continue to run before new-entry research;
- time-budget exhaustion reports truthful deferred counts instead of silently
  dropping groups.

Do not add another analysis engine. Continue to call
`MarketWorkflowService.research_global_bucket_batch()`.

### 5. Preserve Existing Live Execution Ownership

This slice changes discovery, analysis scheduling, and forecast calendar
correctness. It must not:

- copy or bypass `TradingService`;
- copy or bypass `PositionExitService` / `AutoExitService`;
- loosen edge, quote freshness, whitelist/override, reconciliation, risk cap,
  idempotency, circuit-breaker, or compliance checks;
- add a hidden D2 live override or a second date-specific execution engine;
- make LLM output select or veto trades.

D0-D2 analyses flow through existing quantitative ranking and existing live
gates. Do not invent an uncalibrated forecast-horizon multiplier in this task.
If lead-time uncertainty needs a calibrated pricing change, report it as a
follow-up rather than guessing a coefficient.

### 6. Open-Meteo Must Use The City's Local Calendar Day

Update the existing `OpenMeteoProvider` flow:

- retain the timezone returned by the selected geocoding result;
- request daily forecast data using that valid IANA timezone;
- select the target date from the response's local `daily.time` values;
- make `ForecastSnapshot.valid_time` timezone-aware for local midnight rather
  than labeling local date text as UTC midnight;
- include selected timezone and target date in the existing raw audit payload;
- keep temperature units and daily max/min/precip/snow field behavior unchanged.

If geocoding has no valid IANA timezone, do not silently use UTC as if it were
the settlement local day. Use Open-Meteo's documented `timezone=auto` response
timezone if it can be verified from the response, or reject the forecast with a
clear error/warning. The selected timezone must be auditable.

Do not modify NOAA settlement-observation local-day behavior; that path already
has separate station timezone logic.

## Tests Required

Use mocked HTTP/exchange clients for all mutations. Add focused regressions that
prove at least:

1. scraped D0/D1/D2 slugs and cities outside the hard-coded fallback list remain
   in the coverage set;
2. multi-word/hyphenated city slugs are accepted and deduplicated;
3. generated fallback slugs do not precede and starve valid scraped slugs;
4. a failed weather page activates fallback behavior without killing discovery;
5. with more slugs than the request budget, two or more deterministic rotation
   slots select different subsets;
6. bounded successive slots cover every test slug without first-N starvation;
7. D1 and D2 are represented when D0 alone exceeds the per-tick budget;
8. candidate-analysis group selection gives D1 recurring capacity and rotates
   cities while respecting `max_groups` and time budget;
9. deferred counts equal actual unprocessed work;
10. Open-Meteo New York in July requests `America/New_York` and produces a
    timezone-aware DST offset for the selected local date;
11. an Asian city requests its IANA timezone and selects the same local date;
12. missing/invalid geocode timezone never silently becomes UTC settlement day;
13. temperature high/low and unit selection remain unchanged;
14. current live execution tests still prove no duplicate BUY/SELL path and all
    existing gates remain active.

Update mocks whose `discover_weather_events(limit=...)` signatures change, but
do not weaken assertions merely to accommodate the refactor.

## Read-Only Runtime Acceptance

After unit/full tests pass, perform one optional network-backed acceptance using
an isolated temporary database and forced non-live settings only. Do not use the
real database and do not start the dashboard server:

```bash
DATABASE_PATH=/tmp/pwa-three-day-discovery.db \
TRADING_DISABLED=true \
AUTOPILOT_MODE=dry_run \
MAX_ORDER_USDC=1 \
MAX_DAILY_USDC=5 \
MAX_MARKET_USDC=2 \
uv run polymarket-weather autopilot start --once
```

Before running, ensure that command cannot inherit a live mode from `.env` and
cannot mutate the exchange. If that cannot be proven, skip it and report why.

Report from the isolated DB:

- distinct event dates discovered;
- distinct cities discovered;
- market/candidate counts by D0/D1/D2;
- analyzed counts by D0/D1/D2;
- tick duration, event reads, and deferred work;
- confirmation that no live order intent or exchange mutation occurred.

Current live page contents are temporal; do not hard-code July 13/14/15 into
production tests.

## Quality Gates

Run:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

Run targeted discovery, runtime-efficiency, Autopilot, market-eligibility, and
Open-Meteo/provider tests separately before the full suite.

## Git Rules

- Keep this as one behavioral commit unless a small preparatory test-only commit
  materially improves review.
- Normal push to `origin/main`; never force-push.
- Do not rebuild or modify the friend DMG in this task. Packaging refresh happens
  only after Codex accepts the source change.
- Preserve generated `dist/` and `build/` artifacts and unrelated user files.
- Do not modify the user's real `.env`, SQLite database, Application Support
  data, open orders, positions, or running process.

## Required Worker Report

Include:

1. objective completed;
2. starting and ending commit hashes;
3. reuse map and exact owners extended;
4. every production/test/doc file changed and why;
5. new files/classes/tables/settings introduced, with justification (expected:
   no new service/table/setting);
6. old first-N or duplicate logic removed;
7. exact D0/D1/D2 coverage and fair-rotation behavior;
8. Open-Meteo timezone behavior and fallback/error policy;
9. targeted and full test commands with exact results;
10. optional isolated read-only runtime results, or explicit reason skipped;
11. tick/request-budget evidence and unresolved performance risks;
12. commit hash, normal push result, and final worktree status;
13. explicit statement that no real trading mutation was executed;
14. explicit statement that the running live process and real database were not
    stopped, restarted, signaled, or modified.

Stop and request review before proceeding if implementation appears to require a
new scheduler/service/table, exchange mutation, execution-gate change, database
migration, or a second weather-market parser.
