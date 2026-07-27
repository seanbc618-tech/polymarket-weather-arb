# System Structure And Live Monitor Design

Date: 2026-06-01

## Context

The project now has a usable Cockpit, a market workbench, module workflows, China temperature bucket support, and smaller CLI command groups. The next bottleneck is maintainability across three large coordination surfaces:

- `dashboard.py` is still a single large file that owns routing, layout, page rendering, browser forms, i18n, and POST behavior.
- `repositories.py` is a broad SQL facade for markets, candidates, automation, exchange state, reconciliation, and overrides.
- Live automation is intentionally gated, but the system does not yet expose a clear operator-facing explanation of each live gate and skip reason.

The goal of this batch is structural clarity plus better live observability, not loosening trading safety.

## Goals

1. Split dashboard rendering and POST behavior into smaller modules while preserving every current route and browser safety rule.
2. Add a read-only live monitor snapshot that explains live automation readiness and blocker reasons.
3. Keep browser live execution disabled. The UI may show live readiness, but must not approve or execute `trade_live`.
4. Split repository code behind a compatibility facade so existing services can migrate gradually.
5. Split the remaining large operator command module after live monitor and repository boundaries stabilize.

## Non-Goals

- Do not add browser live trading buttons.
- Do not loosen micro-live, whitelist, reconciliation, risk, position, or override gates.
- Do not replace the stdlib dashboard server with a new framework.
- Do not redesign the trading engine or order placement path.
- Do not require a database migration unless a narrow live monitor field truly needs persistence.

## Selected Sequence

### Slice 5: Dashboard Structure

Keep `src/polymarket_weather_arb/dashboard.py` as the public entry point for `serve_dashboard`, `render_dashboard_path`, `handle_dashboard_post`, and compatibility imports used by tests.

Create a dashboard package with focused modules:

- `dashboard_ui/i18n.py`: translations and `_t`.
- `dashboard_ui/html.py`: HTML escaping, tables, cards, page shell, links, display formatting.
- `dashboard_ui/routes.py`: GET route dispatch helpers.
- `dashboard_ui/actions.py`: POST handler helpers and browser safety gates.
- `dashboard_ui/overview.py`: Cockpit homepage rendering.
- `dashboard_ui/markets.py`: market lists, module market lists, and market detail/workbench.
- `dashboard_ui/automation.py`: actions, runs, operator console, overrides.
- `dashboard_ui/exchange.py`: open orders, positions, risk report, reconciliation, setup.

The first slice should be mostly mechanical. It should keep tests focused on route output and POST safety rather than visual redesign.

### Slice 6: Live Monitor Snapshot

Add a service such as `src/polymarket_weather_arb/services/live_monitor_service.py`.

Suggested dataclasses:

```python
@dataclass(frozen=True)
class LiveGate:
    name: str
    ok: bool
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class LiveActionReadiness:
    action_id: str
    market_id: str | None
    status: str
    gates: list[LiveGate]
    can_auto_execute: bool


@dataclass(frozen=True)
class LiveMonitorSnapshot:
    profile: str
    allow_live_auto: bool
    risk_status: str
    reconciliation_fresh: bool
    open_orders_count: int
    positions_count: int
    nonzero_positions_count: int
    pending_live_actions: list[LiveActionReadiness]
    blockers: list[str]
```

The snapshot should evaluate the same gates already enforced by `OperatorDaemon._can_auto_live()`:

- profile is `micro-live`
- daemon live auto is enabled
- action kind is `trade_live`
- market is whitelisted
- strategy override has `live_auto_enabled=True`
- reconciliation is fresh when required
- risk guard status is `ok`
- nonzero positions are blocked when that gate is enabled

The dashboard and CLI should be able to show why a live action was skipped without changing whether it is skipped.

### Slice 7: Repository Facade Split

Keep `Repository` as the public compatibility object, but move SQL methods into focused components:

- `repositories/markets.py`
- `repositories/candidates.py`
- `repositories/automation.py`
- `repositories/exchange.py`
- `repositories/reconciliation.py`
- `repositories/overrides.py`

`storage/repositories.py` should become a facade that composes those components or delegates to mixins. Existing imports of `Repository` remain valid throughout this batch.

### Slice 8: Operator Command Refinement

After live monitor exists, split `cli_commands/operator.py` into focused operator modules:

- `operator_commands/daemon.py`
- `operator_commands/queue.py`
- `operator_commands/exchange.py`
- `operator_commands/overrides.py`
- `operator_commands/demo.py`

Keep `operator_app` as the exported Typer group so `cli.py` does not change again.

## Safety Requirements

- Browser POST handlers must continue to reject `trade_live` proposals, approvals, and execution.
- Daemon live execution must continue to require every existing gate.
- New monitor code must call read-only repository methods or pure gate evaluators only.
- Any live readiness UI must be framed as status and blockers, not as an execution affordance.
- Existing CLI command names and routes must remain compatible.

## Testing Strategy

Each slice should have one failing test before production edits.

Dashboard split:

- Route smoke for `/`, `/markets`, `/markets/<id>`, `/actions`, `/operator`, `/reconciliation`.
- POST safety tests for blocking browser `trade_live`.

Live monitor:

- Unit tests for each gate reason.
- A daemon regression that skipped live actions include monitor-compatible reasons.
- Dashboard render test showing blockers but no live execute form.

Repository split:

- Existing repository tests remain the main compatibility suite.
- Add focused tests only when delegation introduces a new boundary.

Operator split:

- CLI registration tests similar to the previous command group split.
- Existing operator CLI tests remain the compatibility suite.

## Commit Strategy

1. Commit this design spec alone.
2. Commit one implementation plan per slice or one combined plan with slice boundaries.
3. Commit each slice separately:
   - Dashboard structure.
   - Live monitor snapshot.
   - Repository facade split.
   - Operator command refinement.
4. Run `uv run pytest -q` and `uv run --extra dev ruff check src/ tests/` before each implementation commit.
