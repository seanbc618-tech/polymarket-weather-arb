# Cockpit Thin Slice Design

Date: 2026-05-31

## Context

`polymarket-weather-arb` has grown from a CLI-first weather market research MVP into a local operator tool with discovery, market modules, dashboard pages, automation actions, reconciliation, profiles, overrides, and China temperature bucket support.

The current problem is not one missing feature. The user-facing workflow is hard to follow, and the implementation has several large coordination files:

- `src/polymarket_weather_arb/dashboard.py` mixes routing, HTML rendering, forms, CSS, i18n, and workflow behavior.
- `src/polymarket_weather_arb/cli.py` mixes root commands, operator commands, automation commands, profile commands, and formatting helpers.
- `src/polymarket_weather_arb/storage/repositories.py` is a broad repository for almost every SQL operation.
- China temperature bucket support is present, but module-specific behavior still leaks into workflow, dashboard, and command paths.

The first optimization should address UI clarity and code boundaries together, without attempting a full rewrite.

## Goals

1. Make the dashboard default page answer: what should the operator do now, why, and what is blocked?
2. Establish a thin module workflow boundary so `weather` and `china_temp_bucket` actions can be invoked through the same shape.
3. Improve the market detail experience into a decision workbench for research and dry-run operation.
4. Keep existing CLI commands stable while making internal code easier to split later.
5. Keep live trading controls out of this slice. Live safety and configuration clarity are a later phase.

## Non-Goals

- Do not add live trading UI controls.
- Do not loosen any safety gate.
- Do not migrate to React, FastAPI, or another web framework.
- Do not split the entire repository layer yet.
- Do not redesign the trading or risk engines.
- Do not add new strategy modules beyond making the existing module boundary cleaner.

## Selected Approach

Use a thin-slice strategy:

1. Build a new Cockpit homepage backed by a dedicated read model.
2. Add a small module workflow protocol and adapt China temperature bucket first.
3. Clean up the market detail page using the same readiness and workflow concepts.
4. Split CLI implementation files only after the user-facing path is clearer.

This improves visible usability quickly while creating a narrow backend boundary that future modules can use.

## Cockpit Homepage

The dashboard root page should become a hybrid operator cockpit. It should prioritize one next action while still showing the state of the candidate pipeline.

### Required Sections

- **Next action**
  - One primary recommendation such as "review dry-run-ready candidates", "run discovery", "configure China weather source", or "inspect latest failed action".
  - Include a short reason and a target URL.
  - The action must be non-live in this slice.

- **Current mode**
  - Show research/dry-run status and active profile.
  - Live status may be shown as blocked or out of scope, but no live execution controls are added.

- **Candidate pipeline**
  - Show counts for stages: `Found`, `Parsed`, `Quoted`, `Signal`, `Analyzed`, `Dry-run`.
  - The stage labels are product-facing summaries, not necessarily new database states.
  - The pipeline should expose where the strategy is currently blocked.

- **Top candidates**
  - Show a small list of high-priority candidate markets.
  - Include module, title or compact label, ask/bid if available, status, and next missing step.
  - Link each candidate to the market workbench or candidate list.

- **Blockers and failures**
  - Show current blocking reasons, for example missing China weather source, no order book snapshot, no analysis, or latest failed action.
  - Link to the relevant setup, market, or action page.

- **Recent activity and freshness**
  - Show recent automation actions, recent runs, and freshness indicators for quotes, forecasts, and reconciliation.
  - Reconciliation should be informational in this slice, not a live-enabling control.

## Cockpit Read Model

Add a small service such as `src/polymarket_weather_arb/services/cockpit_service.py`.

Suggested dataclasses:

```python
@dataclass(frozen=True)
class NextActionSuggestion:
    label: str
    reason: str
    href: str
    action_kind: str | None = None
    market_id: str | None = None


@dataclass(frozen=True)
class CandidatePipelineSummary:
    found: int
    parsed: int
    quoted: int
    signal_ready: int
    analyzed: int
    dry_run: int
    blockers: list[str]


@dataclass(frozen=True)
class CockpitSnapshot:
    next_action: NextActionSuggestion
    pipeline: CandidatePipelineSummary
    top_candidates: list[CandidateSummary]
    blockers: list[BlockerSummary]
    recent_actions: list[ActionSummary]
    recent_runs: list[RunSummary]
    mode: str
    profile: str | None
```

The exact dataclass names can change during implementation, but the rule should remain: the dashboard renderer consumes one `CockpitSnapshot` instead of directly stitching together many repository calls.

## Module Workflow Boundary

Add a small protocol, not a large framework. The goal is to stop dashboard, CLI, and automation code from knowing each module's internal action sequence.

Suggested shape:

```python
class ModuleWorkflow(Protocol):
    module_id: str

    def discover_candidates(self, options: DiscoveryOptions) -> DiscoveryResult: ...
    def inspect(self, market_id: str) -> MarketWorkflowResult: ...
    def refresh_signal(self, market_id: str) -> MarketWorkflowResult: ...
    def analyze(self, market_id: str) -> MarketWorkflowResult: ...
    def dry_run(self, market_id: str) -> MarketWorkflowResult: ...
    def readiness(self, market_id: str) -> MarketReadiness: ...
    def summarize_market(self, market_id: str) -> MarketSummary: ...
```

Implementation notes:

- `WeatherWorkflow` should wrap the existing standard weather parser, weather refresh, analysis, and dry-run path.
- `ChinaTempBucketWorkflow` should wrap China rule parsing, event-slug discovery, official signal refresh, bucket pricing, and dry-run.
- The module registry should be able to resolve a module id into capabilities and a workflow factory.
- Existing `MarketWorkflowResult` can remain the common result type.
- Existing service logic can be bridged initially; the first implementation does not need to perfectly separate every dependency.

## Market Workbench

The market detail page should become a decision workbench rather than a loose collection of records and buttons.

Required sections:

- Market identity and module label.
- Rule or bucket interpretation.
- Latest quote and quote freshness.
- Signal or forecast readiness.
- Latest analysis and decision reasons.
- Dry-run status and recent order intents.
- Related automation actions.
- Clear blockers if an action cannot run.

Button behavior should be readiness-driven:

- If no rule exists, show `Inspect`.
- If no quote exists, point the user back to discovery or refresh.
- If signal is missing, show `Refresh signal` when supported.
- If analysis is missing and prerequisites are ready, show `Analyze`.
- If analysis exists, show `Dry run`.
- Live controls remain absent.

## Implementation Slices

### Slice 1: Cockpit Read Model and Homepage

Add `CockpitSnapshot` and render it at `/`.

Likely files:

- Add `src/polymarket_weather_arb/services/cockpit_service.py`.
- Update `src/polymarket_weather_arb/dashboard.py` so `render_overview()` becomes the cockpit renderer.
- Add focused tests for next-action selection, pipeline counts, blockers, and links.

### Slice 2: Module Workflow Protocol and China Adapter

Add a minimal module workflow resolver and adapt China temperature bucket first.

Likely files:

- Update `src/polymarket_weather_arb/modules/base.py`.
- Update `src/polymarket_weather_arb/modules/registry.py`.
- Add or adapt workflow classes under `src/polymarket_weather_arb/modules/` or `src/polymarket_weather_arb/services/`.
- Update dashboard POST handlers and CLI internals to call the resolver where practical.

### Slice 3: Market Workbench Cleanup

Refactor market detail rendering around readiness and decision state.

Likely files:

- Update `render_market_detail()` in `dashboard.py`.
- Add small helper functions or view-model dataclasses if the renderer grows.
- Add tests for visible blockers and button availability.

### Slice 4: CLI Grouping Cleanup

Keep command names compatible, but move implementation into smaller command modules.

Likely files:

- Add command group modules under `src/polymarket_weather_arb/commands/`.
- Keep `src/polymarket_weather_arb/cli.py` as the Typer app assembler.
- Preserve existing documented commands.

## Verification

Automated checks:

```bash
uv run pytest tests/test_dashboard.py tests/test_dashboard_market_workflow.py
uv run pytest tests/test_market_workflow_service.py tests/test_cli_operator.py
uv run pytest
```

Add or update tests for:

- Cockpit next-action fallback order.
- Candidate pipeline counts.
- Blocker display for missing China weather source and missing snapshot.
- Market workbench action availability.
- Module workflow resolver behavior for `weather` and `china_temp_bucket`.

Manual smoke:

1. Start the dashboard in Chinese.
2. Confirm `/` shows the Cockpit, not the old generic overview.
3. Confirm the primary next action is understandable without reading documentation.
4. Click from Cockpit to candidates and market detail.
5. Confirm no live execution control appears.
6. Confirm blockers link to useful places.

## Compatibility Promises

- Existing CLI command names remain stable.
- Existing SQLite schema should not change in Slice 1 unless a blocker is found.
- Existing tests should remain meaningful; new tests should focus on read models and readiness decisions.
- Live trading remains disabled from browser UI.

## Implementation Decisions

- `CandidatePipelineSummary.signal_ready` means a latest forecast or signal row exists for the market. Missing provider configuration should appear as a blocker, not as a separate pipeline stage.
- `CockpitSnapshot` should be built from repository reads and pure readiness helpers only. It must not trigger network calls, discovery, analysis, or order creation.
- Module workflow classes should initially live under `src/polymarket_weather_arb/services/module_workflows.py` or a small `services/module_workflows/` package. The existing `modules/` package should remain the registry and metadata layer until the boundary proves stable.
- Slice 1 should not require a schema migration. If pipeline counts cannot be derived perfectly from existing rows, use conservative derived counts and document the approximation in tests.
