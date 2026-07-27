# Live Launchpad Design

Date: 2026-06-02

## Context

The beginner dry-run path now works: the operator can open the dashboard, run the safe rehearsal, and verify dry-run order intents. Live readiness also passes in the current HK environment: credentials are configured, compliance is allowed, the CLOB SDK is installed, exchange reads work, and reconciliation is fresh.

The remaining problem is operator clarity. Live trading controls are spread across market pages, automation actions, overrides, open orders, positions, reconciliation, CLI commands, and daemon gates. This is safe in the sense that many gates block live execution, but it is hard for a human operator to understand what is missing, what can be previewed, and what would actually place a real order.

The chosen direction is a dedicated `Live Launchpad` page. It should concentrate live-readiness status, account state, market eligibility, micro-live sizing, order preview, and final confirmation into one intentionally narrow workflow.

## Goals

1. Add one clear browser entry point for live preparation and micro-live execution.
2. Keep live order placement impossible until every existing safety gate passes.
3. Make the current live blockers visible in plain operator language.
4. Support a small live preview flow before any real order is approved or executed.
5. Preserve all existing CLI and daemon safety gates.
6. Keep beginner and dry-run pages safe and non-live.

## Non-Goals

- Do not enable broad live automation.
- Do not remove or loosen compliance, reconciliation, risk, whitelist, override, or position gates.
- Do not add market orders.
- Do not bypass the existing `TradingService` and risk validation path.
- Do not make the dashboard daemon auto-live by default.
- Do not support arbitrary order sizing from the browser in this slice.

## Selected Approach

Add a new `Live Launchpad` dashboard route and renderer:

- `GET /live` shows a single-page live readiness workflow.
- `POST /live/refresh` refreshes read-only exchange state.
- `POST /live/preview` creates or displays a live order preview without placing an order.
- `POST /live/propose` creates a pending `trade_live` automation action after final browser confirmation.
- `POST /live/execute` executes only an already-approved pending live action, and only if all live gates still pass.

The initial implementation should be a thin slice. If the existing automation service cannot safely support browser live execution without a larger change, the first slice may stop at `preview` and `propose`, with `execute` disabled and documented as a follow-up. The UI must never pretend execution is available when it is not.

## Page Model

Add a live launchpad read model, likely in `src/polymarket_weather_arb/services/live_launchpad_service.py`.

Suggested dataclasses:

```python
@dataclass(frozen=True)
class LiveLaunchpadGate:
    name: str
    ok: bool
    status: str
    detail: str


@dataclass(frozen=True)
class LiveLaunchpadCandidate:
    market_id: str
    title: str
    profile: str
    best_bid: str | None
    best_ask: str | None
    latest_analysis_id: int | None
    latest_dry_run_id: int | None
    gates: list[LiveLaunchpadGate]
    can_preview: bool


@dataclass(frozen=True)
class LiveLaunchpadPreview:
    market_id: str
    side: str
    limit_price: str
    size: str
    notional: str
    rationale: str
    risk_reasons: list[str]
    expires_at: str | None


@dataclass(frozen=True)
class LiveLaunchpadSnapshot:
    readiness_gates: list[LiveLaunchpadGate]
    reconciliation_status: str
    open_orders_count: int
    positions_count: int
    nonzero_positions_count: int
    max_order_usdc: str
    max_daily_usdc: str
    max_market_usdc: str
    candidates: list[LiveLaunchpadCandidate]
    preview: LiveLaunchpadPreview | None
    pending_live_action_id: str | None
    can_execute: bool
    blockers: list[str]
```

Exact names may change, but the renderer should consume a single snapshot rather than stitching together many repository calls.

## Required Gates

The launchpad must evaluate and display these gates before preview or execution:

- Live credentials are configured.
- Compliance check currently allows the runtime country.
- CLOB SDK is installed.
- Exchange read check succeeds.
- Latest successful reconciliation is fresh.
- Risk guard status is `ok`.
- Profile is `micro-live`.
- Market is explicitly live-whitelisted.
- Matching strategy override has `live_auto_enabled=True`.
- Candidate has a recent analysis and at least one dry-run record.
- No nonzero positions exist when the position blocker is enabled.
- Proposed order fits hard risk caps and the selected profile caps.

If any gate fails, the relevant button is disabled and the page shows the specific blocker.

## Browser Workflow

### 1. Read-Only Status

At the top of `/live`, show:

- compliance status,
- reconciliation freshness,
- open order count,
- position count,
- nonzero position count,
- micro-live caps,
- risk guard status,
- last exchange refresh time.

The primary action in this section is `Refresh exchange state`. It must only perform read-only exchange calls and persist the reconciliation/open-order/position/fill state already supported by existing services.

### 2. Candidate Selection

Show only markets that are close to live readiness:

- latest analysis exists,
- latest dry-run exists,
- current quote or snapshot exists,
- market has or can have a matching micro-live override.

Each candidate row should show:

- market title,
- module,
- best bid/ask,
- latest dry-run status,
- override status,
- blockers,
- a `Preview` button when all preview gates pass.

### 3. Live Preview

Preview generation must not place an order. It should reuse the existing workflow and risk code to produce a limit-order intent shape, but keep it clearly marked as a preview until final proposal.

The preview panel must show:

- market title,
- side,
- limit price,
- size,
- notional,
- expected maximum loss,
- risk reasons,
- source grade,
- forecast or official signal freshness,
- expiration or staleness warning if applicable.

The first implementation should default to a very small micro-live size. A recommended initial browser cap is `2 USDC`, even if the hard profile cap is higher.

### 4. Final Confirmation

Before creating a live action or executing anything, the browser must require:

- an explicit checkbox acknowledging real money risk,
- a typed confirmation phrase such as `LIVE 2 USDC`,
- a visible summary of the exact market, side, price, size, and notional.

The page should then create a pending `trade_live` action rather than immediately executing if the execution path is not yet fully reviewed.

### 5. Execution

Execution from the browser is allowed only after a follow-up implementation proves the route can reuse all existing live gates. If enabled, it must:

- re-run live readiness,
- re-run reconciliation freshness check,
- re-run risk validation,
- verify action status is approved,
- verify the preview has not gone stale,
- place a limit order only,
- redirect to open orders and show the resulting exchange order id or failure reason.

Until this is implemented and tested, `/live` should show execution as locked and point the operator to the CLI-approved path.

## UI Requirements

- Add `Live` or `Live Launchpad` to the dashboard navigation.
- Keep the page operational and compact, not a marketing page.
- Use direct status tables, gate chips, and disabled buttons with reasons.
- Do not hide blockers behind hover-only UI.
- Make the dangerous action visually distinct, but do not use alarming styling for read-only actions.
- Beginner page remains dry-run only and should link to `/live` only as a next step after dry-run success.

## Error Handling

- Every POST handler returns to `/live` with a translated flash message and detail.
- Exchange/API failures show the failing gate and do not mark readiness as passed.
- Preview staleness disables final proposal.
- Missing credentials, missing override, and missing whitelist are separate blockers.
- Any unexpected ValueError from live services is surfaced as a detail, not converted into a fake i18n key.

## Testing Strategy

Add tests before implementation edits:

- `/live` renders with locked execution by default.
- `/live` shows passed readiness when mocked checks pass.
- `/live/refresh` persists read-only exchange state and redirects back to `/live`.
- Preview is blocked when reconciliation is stale.
- Preview is blocked when the market is not whitelisted.
- Preview is blocked when no micro-live override exists.
- Browser proposal requires the confirmation phrase.
- Browser execution remains disabled until the execution route has full gate coverage.
- No beginner route can approve or execute `trade_live`.

Regression checks:

```bash
uv run pytest -q
uv run ruff check src/ tests/ scripts/rehearse_live_readiness.py scripts/backup_restore_check.py scripts/check_deployment_files.py
bash -n scripts/install_systemd_units.sh
```

## Implementation Slices

1. Add the read-only `/live` page and snapshot service.
2. Add refresh/readiness POST behavior and tests.
3. Add candidate selection and preview-only flow.
4. Add pending live action proposal with typed confirmation.
5. Review whether browser execution should be enabled or remain CLI-only.

## Commit Strategy

1. Commit this design spec alone.
2. Commit one implementation plan after the spec is approved.
3. Commit each slice separately with tests.
4. Push directly to `main` after verified commits, matching the current project workflow.
