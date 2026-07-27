# Grok Follow-up: Runtime Acceptance Blockers

## Status

The implementation ending at `f1dd6cb` is **not yet accepted**. Its targeted
tests pass, but Codex found production-path gaps that the unit tests do not cover.
Start from current `main`. Read `AGENTS.md`, `docs/agent-worker-standards.md`, and
the original task before editing.

Do not change strategy, Edge, sizing, entry, exit, risk caps, or product modes.
Do not add a service, scheduler, database table, persistent setting, or trading
path. Do not execute any real exchange mutation. Push normally; never force-push.

## P0: Fix The Real `/app` Background Lifecycle

`dashboard._run_autopilot_background()` creates a new `AutopilotService` every
cycle. Since `f1dd6cb`, each service owns a cached `SecureClient`, but this path
neither reuses nor closes it. In the macOS `/app` path this causes one abandoned
SDK HTTP client per tick. It also means the report's "second tick reuses the same
session" claim is false for the user's primary product path.

Fix within the existing composition root:

1. Let `_run_autopilot_background()` own one `GammaPolymarketClient` and reuse it
   across cycles while the active `Settings` object is unchanged.
2. Inject that client into each cycle's `AutopilotService`; keep the fresh SQLite
   connection/repository behavior.
3. If desktop setup replaces the active `Settings`, close the old client exactly
   once and create a new client for the new settings on the next enabled cycle.
4. Close the shared client exactly once when the finite test loop exits, the
   background runner terminates, or an outer shutdown path ends.
5. Make `AutopilotService.close()` respect ownership: a service must not close an
   externally injected shared client. The composition root that created the
   shared client closes it.
6. A paused/disabled cycle must not create an authenticated SDK client.

Required tests must run `_run_autopilot_background(max_cycles=...)`, not merely
call the adapter twice:

- two enabled cycles with unchanged settings create one adapter/session and close
  it once at runner exit;
- a settings replacement closes the old adapter once and uses a new one;
- disabled cycles create no authenticated client;
- a failed cycle does not leak the shared client.

## P0: Make Reconciliation Telegram State Work Across `/app` Cycles

`AutopilotService._recon_alert_signature` currently starts as `None` for every
desktop cycle. Therefore repeated failures still notify every tick and a later
healthy cycle cannot emit recovery. The current test calls three private methods
on one service and does not exercise the production composition root.

Reuse existing `autopilot_state.last_tick_status` / `last_error` or pass a small
in-memory state value through the existing background composition root. Add no
table or schema field. The behavior must also recover correctly after process
restart, so persisted existing state is preferred.

The failure identity must include at least:

- reconciliation status;
- failed stage;
- redacted error type;
- a stable redacted error message/signature.

Suppress only an identical repeated failure. A `balances timeout` changing to a
`balances authentication failure` is material and must notify. On the first
healthy reconciliation after a recorded failure, send one recovery; later
healthy ticks stay quiet.

Required production-path tests:

- two separate `AutopilotService` instances (as `/app` creates them) see the same
  balances failure and send one failure notification total;
- changed error type/message in the same stage sends a new notification;
- a third, healthy service instance sends exactly one recovery;
- reconstructing services from the existing persisted state preserves this
  behavior without a new table/field;
- BUY, SELL, and fill notifications remain unchanged.

## P1: Make Cached SDK Concurrency Actually Safe

The current lock protects only creation/invalidation, not use. Two threads can
both hold client C1; one auth failure can close C1 while the other is using it,
and a second invalidation can close newly-created C2 underneath the first retry.

Use one existing adapter-local re-entrant operation lock (or equivalent minimal
mechanism) so authenticated reads and mutations cannot race with invalidation or
close. Do not build a pool. Preserve these invariants:

- one retry only for an idempotent authenticated read;
- zero automatic retries for BUY, SELL, or cancel;
- `close()` cannot close a client while an operation is using it;
- concurrent auth failures do not create/close clients out of order.

Add deterministic threaded tests using barriers/events; do not use live network.

## P1: Normalize Only Proven Missing Orders

`_is_order_absent_error()` currently maps every SDK `UnexpectedResponseError` and
every Pydantic `ValidationError` to `OrderNotFoundError`. This hides malformed or
changed exchange responses as an ordinary missing order.

The installed SDK wraps `OpenOrder.parse_response(None)` as
`UnexpectedResponseError` whose Pydantic cause has input `None`. In contrast,
`{}` or a partial order dict produces the same outer exception but a non-`None`
input and must remain an adapter/schema error.

Required behavior:

- HTTP 404, an explicit order-not-found response, or SDK parse failure whose
  underlying input is exactly `None` -> `OrderNotFoundError`;
- HTTP 400 only when its message explicitly means the order is absent;
- malformed `{}`, partial order payload, non-JSON response, and unrelated
  `UnexpectedResponseError` -> propagate as adapter error;
- preserve redaction.

Add tests using the real SDK exception/cause shapes, not only `RuntimeError` text.

## P1: Enforce One Discovery Fallback Budget Per Tick

`AutopilotService` calls `discover_weather_events()` and then `discover()` on the
same service. Both methods reset the fallback counter, so the real tick can make
up to 10 CLOB fallback calls while logs report only the second method's count.

Keep the standalone CLI methods independently usable, but let the combined
Autopilot discovery cycle reset once and share one total budget of
`DISCOVERY_CLOB_FALLBACK_LIMIT`. The phase log must report the true combined
count. Add a test that runs the same two calls as Autopilot with missing quotes
and proves total calls are `<= 5`, not 10.

## P2: Make Phase Evidence Truthful

`_log_phase()` currently emits `deferred_hint=-` unconditionally. Either pass the
real cumulative deferred count or remove the misleading field. Ensure the final
tick completion log contains the true total. Do not add schema fields.

## Acceptance Gates

Run and report:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

The Worker Report must include:

- baseline and final hashes;
- exact files changed and existing owners reused;
- `/app` two-cycle SecureClient create/close counts;
- cross-instance Telegram failure/dedup/recovery evidence;
- combined discovery fallback call count;
- exact tests and gates;
- remaining worktree state;
- explicit confirmation that no real trading mutation ran.

Stop and ask Codex before adding any new service/table/scheduler/settings or
changing a live BUY/SELL path.
