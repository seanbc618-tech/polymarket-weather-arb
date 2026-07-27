# Gemini Task: Opportunity Funnel and Verified PnL Feedback

## Read First

Read `AGENTS.md`, `CLAUDE.md`, and `docs/agent-worker-standards.md` before
editing. The worker standard is authoritative.

## Objective

Make the system answer two operator questions from the data it already has:

1. Why did discovered weather markets not become live orders?
2. Which completed live trading roundtrips actually made or lost money?

The first slice is measurement and visibility, not a new trading strategy. It must
turn rejected opportunities and verified realized PnL into feedback for improving
the current quantitative workflow.

## Existing Components To Reuse

- `services/cockpit_service.py`: existing candidate pipeline summary, top
  candidates, and blockers. Extend this read model; do not create a parallel
  dashboard service.
- `dashboard_ui/app.py`: primary `/app` view and decision feed.
- `dashboard_ui/overview.py`: existing dashboard overview/Cockpit rendering.
- `storage/repositories.py`: candidate statuses, analyses, risk decisions, order
  intents, order attempts, fills, positions, market snapshots, and roundtrip runs.
- `services/roundtrip_status_service.py`: exact BUY/SELL fill linkage for a
  completed roundtrip.
- `services/order_lifecycle_service.py`: existing order and exposure summaries.
- `services/reconciliation_service.py`: exchange truth for fills and positions.

Do not create another scheduler, execution service, analytics database, event
store, trade journal, or separate dashboard application.

## Slice A: Opportunity Funnel

Extend the existing Cockpit read model and `/app` with an `Opportunity Funnel`
section for the most recent bounded window of local data. It must show counts for
one clearly-defined terminal path per market/opportunity:

`discovered -> rule tradable -> quote available -> forecast available -> analyzed
-> quant trade signal -> live submitted -> exchange fill`

Requirements:

1. Use the existing tables; a read/query helper in `Repository` is acceptable.
   Do not add a persistence table in Slice A.
2. Prevent double counting: document the identity rule and use the latest relevant
   record per market/opportunity in the chosen time window.
3. Show the top rejection/blocker reasons with counts. Normalize only known,
   stable categories such as non-tradable rule, missing quote, missing forecast,
   quant skip/edge, source-grade rejection, stale reconciliation, live gate,
   duplicate active order, and exchange submission failure. Preserve the raw
   reason as drill-down text; do not hide it behind an invented generic label.
4. The UI must distinguish:
   - no candidate existed;
   - candidate existed but lacked data;
   - quantitative model declined it;
   - execution gate declined it;
   - order was submitted but not yet filled.
5. Keep this read-only. No candidate status or strategy override may be changed by
   viewing this section.

## Slice B: Verified Realized PnL

Add a compact `Verified Realized PnL` section to `/app` and a repository/read-model
calculation. It may show zero when there are no completed roundtrips.

Scope and accounting rules:

1. Compute realized PnL only for roundtrips that `RoundtripStatusService` can
   prove are `completed`: fresh reconciliation, no relevant open orders, zero
   remaining position, and exact BUY and SELL fills linked to that run's intent
   order IDs.
2. Cash accounting for matched quantity:
   - BUY cost = `price * size + fee`;
   - SELL proceeds = `price * size - fee`;
   - realized PnL = matched SELL proceeds minus matched BUY cost.
3. Handle partial fills conservatively. Match only the quantity that can be
   evidenced on both sides. Surface leftover quantity as `unrealized/unknown`,
   never fold it into realized PnL.
4. Group by market and total; show completed-roundtrip count, gross buy cost,
   gross sell proceeds, fees, realized PnL, and last completed timestamp.
5. Do not call an unfilled order, a `submitted_unverified` order, a nonzero
   position, or an unresolved market "realized PnL".
6. Do not fabricate mark-to-market. For this slice, show existing reconciled
   exposure separately and label mark-to-market as unavailable when there is no
   token-specific current quote. A future slice may add token-level marks through
   the existing Polymarket client and snapshot path.
7. Do not write a PnL table in Slice B. It must be derived from fills, intents,
   roundtrip runs, and reconciliation data so corrections from exchange reads are
   reflected automatically.

## UX Requirements

- Put the funnel and verified PnL in the primary `/app` operator view, not only
  in Advanced Mode.
- Use short English labels in the interface; existing Chinese/English i18n
  conventions must remain intact.
- Empty states should say what evidence is missing and link to the existing
  relevant view (`/candidates`, `/orders`, `/positions`, or `/reconcile`), not
  imply a system error.
- Do not add marketing copy, decorative cards within cards, or a second dashboard
  layout.

## Explicit Non-Goals

- No strategy changes, new trading thresholds, LLM changes, or live order changes.
- No auto-optimization based on PnL yet.
- No backfill or inference from historical wallet transactions outside the current
  local database.
- No claimed PnL for the old database that was intentionally archived and wiped.
- No real network mutation or actual trading.

## Tests Required

Use SQLite fixtures and mocked exchange data. At minimum cover:

1. empty database: all funnel/PnL counts are zero and UI renders a useful empty state;
2. one market at each funnel stage, proving no double counting;
3. top blocker grouping with raw-reason drill-down;
4. a completed exact BUY->SELL roundtrip with fees, including expected realized PnL;
5. partial BUY/SELL fills: only matched quantity contributes to realized PnL;
6. submitted/open/unverified/nonzero-position runs are excluded from realized PnL;
7. multiple completed markets aggregate correctly;
8. `/app` renders both sections in English and Chinese;
9. no database schema migration or new table is introduced unless a written
   justification proves the existing ledger cannot support the result.

Run targeted tests, `uv run ruff check src/ tests/`, `uv run pytest -q`, and
`git diff --check`.

## Completion Report

Report the exact existing data sources reused, accounting assumptions, any new
repository read methods, every new file/class/table/setting (expected: no new table
or setting), exact test results, commit hash, remaining working-tree changes, and
the explicit statement that no real trading mutation ran.

