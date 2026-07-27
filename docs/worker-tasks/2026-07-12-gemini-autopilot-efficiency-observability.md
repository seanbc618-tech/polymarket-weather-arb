# Gemini 3.1 Pro Task: Autopilot Efficiency, Settlement Routing, And Logs

## Dispatch Order

Start **only after** Grok 4.5 has completed
`docs/worker-tasks/2026-07-12-grok-live-runtime-correctness.md`, pushed its
commits, and provided the final commit hash. Pull/rebase normally onto that exact
`origin/main`. Do not force-push or rewrite Grok's commits.

Before planning or editing, read:

1. `AGENTS.md`
2. `CLAUDE.md`
3. `docs/agent-worker-standards.md`
4. Grok's task and completion report/commits
5. `docs/runbooks/full-auto-micro-live.md`

Run `git status --short` first. Preserve `audit_report.md`. No real BUY, SELL,
cancellation, settlement mutation, or account mutation is permitted.

## Objective

Make the existing Autopilot loop operationally efficient and diagnosable after
Grok fixes economic/execution correctness:

- expired positions stop wasting forecast calls and enter the existing settlement
  visibility path;
- a configured 300-second cycle no longer routinely takes about eight minutes;
- terminal/API diagnostics survive after the terminal closes;
- `/app` distinguishes process liveness from useful Autopilot progress.

This task is deliberately lower-risk than Grok's task. Do not modify fee formulas,
BUY/SELL semantics, candidate eligibility policy, or auto-exit dust policy except
where a narrow integration test is required.

Expected new services, schedulers, database tables, strategy engines, and product
modes: **zero**.

## Mandatory Reuse Map

- `AutopilotService.tick()` and `run_loop()`: the only `/app` autonomous cycle.
- `MarketWorkflowService` batch/research methods: forecast and analysis.
- Existing market/candidate/forecast/analysis timestamps in `Repository`: caching
  and bounded work selection.
- Existing settlement/backfill/resolution audit services and calibration UI:
  expired/resolved handling and operator visibility.
- Existing CLI/dashboard startup logging: persistent runtime diagnostics.
- Existing `/app` status, blocker, position, and decision panels: liveness and
  recovery information.

Do not create another daemon, queue, workflow service, log database, settlement
engine, or dashboard application.

## Slice 1: Route Expired Positions Away From Forecast Refresh

1. Before refreshing analysis for a current position, classify its market using
   authoritative raw state, close/end time, and parsed target date.
2. Open current/future positions continue through normal forecast refresh and
   `ExitGuardianService` analysis.
3. Closed, resolving, or target-date-past positions must not call a forecast API
   that cannot serve the historical date.
4. Reuse the existing settlement observation/backfill/resolution-audit read path
   to expose a precise state such as awaiting observation, awaiting Polymarket
   resolution, redeemable, or unavailable. Do not auto-backfill, redeem, or settle
   as part of this task.
5. `/app` must show the reason and existing operator destination/action. Do not
   turn an expired position into a generic fatal tick failure.
6. Preserve reconciliation as the source of truth for whether the position still
   exists.

## Slice 2: Bound The Synchronous Tick

Keep the current synchronous architecture. Do not introduce asyncio, worker
queues, background executors, or a second scheduler.

1. Measure and log duration for existing phases: discovery, reconciliation,
   order lifecycle, position refresh/exit, candidate analysis, and entry.
2. Reuse freshness timestamps so one tick does not refetch unchanged geocoding,
   market metadata, forecasts, and analyses unnecessarily.
3. Analyze only open current/future market groups. Respect Grok's eligibility
   policy rather than adding another date/closed implementation.
4. Keep bounded group/market selection and prioritize:
   - actual open positions and open orders;
   - today's/next relevant weather markets;
   - highest executable net-Edge candidates;
   - lower-ranked research candidates only within remaining budget.
5. Add an explicit per-cycle work budget/deadline using existing settings where
   possible. If one small setting is unavoidable, document its operator, default,
   UI exposure, and removal path. Prefer deriving a budget from `tick_seconds`.
6. A deferred candidate is not a failed tick. Record a concise deferred count and
   process it in a later tick.
7. Reconciliation and lifecycle/exit work must not be skipped merely because
   discovery or low-priority research consumed the budget.
8. Do not change the invariant of at most one new BUY per tick.

Acceptance target for deterministic mocked tests: routine bounded work completes
inside the configured cycle budget, and an intentionally slow research provider
cannot prevent reconciliation/position management from running.

## Slice 3: Persistent, Redacted Runtime Logs

1. Configure the existing Autopilot startup path to write structured, rotating
   local logs under an existing data/log directory while retaining useful console
   output.
2. Use Python's existing logging facilities. Do not create a logging service,
   telemetry server, or database table.
3. Include timestamp, level, component, tick identifier, phase, market ID when
   applicable, elapsed time, retry attempt, and concise exception reason.
4. Never log private keys, API secrets, signed payloads, auth headers, full wallet
   credentials, or Telegram tokens. Add a redaction test.
5. Rotation must have bounded disk usage and behave correctly across restart.
6. Log these material events:
   - startup configuration without secrets;
   - tick/phase start and finish;
   - HTTP retries/final failures;
   - reconciliation fail-stop;
   - BUY/SELL submission outcome identifiers;
   - fill reconciliation summary;
   - fatal loop exception and clean Ctrl-C shutdown.
7. Do not add Telegram heartbeats. Existing material-only Telegram behavior stays
   unchanged.
8. Update the existing runbook with log location and the exact tail/diagnostic
   commands. Do not create another runbook.

## Slice 4: Useful Liveness In `/app`

Reuse the current state/read model and existing panels. Show:

- process/session started time when available;
- latest tick time and age;
- latest **useful** tick outcome (reconciled, position-managed, candidate analyzed,
  entry submitted), rather than treating repeated expired-data failures as work;
- configured tick interval and observed recent cycle duration;
- stale warning after two configured tick intervals;
- current phase/recent material failure and a concrete recovery hint;
- number of candidates deferred by the cycle budget.

Do not add a second status page or a new heartbeat table. Derive from existing
state/decisions/log metadata where feasible. A schema migration requires stopping
for Codex review before implementation.

## Required Offline Tests

At minimum prove:

1. expired/closed position causes no forecast provider call;
2. current open position still refreshes and can reach existing exit evaluation;
3. expired position remains visible with settlement/resolution status and no
   exchange mutation;
4. stale historical candidates are not repeatedly researched after Grok's fix;
5. reconciliation and position management run before optional research budget is
   exhausted;
6. slow optional research is deferred without failing the complete tick;
7. phase timing and deferred counts are recorded;
8. rotating log file survives restart and stays within configured bounds;
9. secrets and auth payloads are redacted;
10. Ctrl-C records clean shutdown and does not alter order state;
11. `/app` renders healthy, running-slow, and stale states in English and Chinese;
12. Telegram still does not send idle/heartbeat messages;
13. Grok's closed-market, fee, intent, and dust regressions remain green.

All network behavior and exchange mutations must be mocked. Do not read or modify
the live database or `.env`.

## Commit And Completion Gate

Prefer separable commits:

1. expired-position settlement routing;
2. bounded synchronous phases and caching;
3. rotating redacted logging and `/app` liveness/runbook.

Before reporting completion run:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

Push normally to `main`; never use `git push --force`.

The completion report must include the Grok baseline commit, phase timings before
and after under mocked deterministic load, reused components, any new file/class/
setting with justification, exact tests/results, commit hashes, remaining
worktree state, and the explicit statement that no real trading mutation ran.
