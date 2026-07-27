# Agent Worker Standards

This document is authoritative for every coding agent working in this repository,
including Codex, Claude Code, Grok, Antigravity/Gemini, and `.pi`.

## Product Objective

Build a weather-market system that can autonomously discover, evaluate, enter,
manage, and exit positive-expected-value Polymarket trades, while producing an
accurate audit trail and usable operator visibility.

The system exists to improve repeatable, risk-adjusted trading returns. It does
not exist to accumulate dashboards, safety abstractions, configuration switches,
or speculative infrastructure. No agent may claim or imply guaranteed profit.

Risk controls are justified only when they protect capital or execution integrity,
such as preventing duplicate orders, overselling, stale-price execution, account
drift, or unbounded loss. Do not add policy gates merely because they sound safe.

## Canonical Product Path

Treat these components as the existing implementation to extend:

- `/app` and `AutopilotService`: primary beginner-facing product and autonomous loop.
- `MarketWorkflowService`: market research, forecast, and analysis orchestration.
- `TradingService`: the only normal live BUY submission path.
- `PositionExitService`: the only live SELL/close submission path.
- `ReconciliationService`: source of truth for exchange balances, orders, fills,
  and positions.
- `ExitGuardianService`: exit recommendations only.
- `AutoExitService`: thin automatic orchestration over ExitGuardian and
  PositionExitService; it must not become another execution engine.
- `OperatorDaemon` and operator CLI: advanced operations, diagnostics, and legacy
  queue workflows. Do not add a second strategy engine here.
- `Repository` and the existing SQLite schema: persistence boundary.

When two entry shells need the same behavior, extract or reuse one policy function.
Do not copy validation, whitelist, strategy, pricing, or order-state logic between
the operator CLI and `/app`.

## Reuse Before Creation

Before adding a file, class, table, setting, state machine, or command:

1. Search the repository with `rg` for the behavior and adjacent nouns.
2. Identify the current owner from the canonical product path above.
3. Extend that owner or add a small helper at its existing layer.
4. State why reuse is impossible before creating a new abstraction.
5. Delete superseded code in the same slice; do not leave parallel paths behind.

A new `*Service`, database table, or persistent mode requires a short written
justification in the task summary covering ownership, callers, and why existing
components cannot own it. "Cleaner", "safer", or "more scalable" alone is not a
justification.

## Change Budget

Prefer the smallest end-to-end change that improves one of these outcomes:

- better market coverage or parsing accuracy;
- better probability estimates or expected-value ranking;
- more reliable BUY/fill/SELL lifecycle management;
- faster autonomous cycle time without duplicate execution;
- clearer rejected-opportunity and realized-performance feedback;
- better reconciliation and exchange-state accuracy.

For each production file added, ask whether an existing file can be extended. For
each new configuration flag, define who sets it, where it is displayed, and how it
is removed. For each new database field or table, define its source of truth and
retention lifecycle.

## Safety Without Safety Theater

Keep hard execution invariants close to the mutation they protect:

- idempotency and duplicate-order checks belong in BUY/SELL execution services;
- position and oversell checks belong in the SELL path;
- exchange acceptance must be durably recorded before post-submit verification;
- reconciliation determines actual orders, fills, and positions;
- limit and exposure checks belong in the risk/execution boundary.

Do not create a new global gate when a local invariant is sufficient. Do not make
old failed demo actions, stale UI state, or optional research data permanently
block profitable execution. A control must expose a concrete failure reason and a
recovery path.

## LLM Boundary

LLM output is untrusted interpretation, not exchange truth and not the primary
pricing engine. It may review ambiguous rule text, explain rejected opportunities,
or annotate a quantitative candidate. It must not place orders, invent prices,
override reconciliation, relax execution invariants, or silently veto trades.

Until a task explicitly changes this policy, quantitative models own entry and
exit decisions. Persist the provider, model, action, confidence, and concise reason
whenever an LLM result is displayed or used.

## Required Workflow

Before editing:

1. Read this document and `AGENTS.md`.
2. Run `git status --short`; preserve unrelated and untracked user work.
3. Inspect callers, tests, and the current production path.
4. Write a brief reuse map: existing owner, reused APIs, files changed, files not
   being created.

During implementation:

1. Keep one behavioral objective per commit.
2. Do not run real BUY or SELL orders unless the user gives an exact, current
   confirmation for that order.
3. Keep network mutation tests mocked. Test exchange-accepted-but-verification-
   failed states explicitly.
4. Distinguish `rejected`, `submitted`, `open`, `matched/filled`, `cancelled`, and
   `submitted_unverified`; never label an intent successful merely because it has
   an ID.
5. Update an existing runbook instead of creating a competing one.

Before completion:

1. Run targeted tests for the changed behavior.
2. Run `uv run ruff check src/ tests/`.
3. Run `uv run pytest -q` with test risk caps isolated from `.env` when needed.
4. Run `git diff --check` and `git status --short`.
5. Report exact tests, unresolved risks, and whether any real network mutation ran.

## Worker Handoff Format

Every worker report must include:

- objective completed;
- existing components reused;
- new files/classes/tables/settings introduced, with justification;
- old or duplicate code removed;
- production behavior changed;
- tests and exact results;
- git commit hash and remaining working-tree changes;
- explicit statement: real trading mutation executed or not executed.

Reports that only say "all tests pass" are incomplete.

Durable cross-agent handoffs (Grok → Codex and peers) are files under `docs/`,
especially `docs/reviews/` and `docs/worker-tasks/`. On this machine Codex can
read that tree via the `agent-handoff` MCP server. See `docs/agent-handoff.md`
for the MCP config, path map, and suggested report header.

## Stop Conditions

Stop and ask for review before proceeding when:

- the change creates a second BUY, SELL, reconciliation, scheduler, or strategy path;
- more than one new service or table appears necessary for a single slice;
- a safety control can block live execution without a visible recovery path;
- a migration would discard live audit history;
- implementation requires real-money execution;
- current code contradicts this ownership map.
