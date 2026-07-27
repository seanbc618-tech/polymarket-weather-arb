# System Structure And Live Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve maintainability and operator safety visibility by splitting dashboard structure first, then adding read-only live monitor gate explanations.

**Architecture:** Keep public entry points compatible while moving focused behavior into new modules. `dashboard.py` remains the stdlib HTTP/server facade. Dashboard rendering helpers move first into `dashboard_ui/`, then live monitor status is added through a read-only service consumed by CLI/dashboard surfaces.

**Tech Stack:** Python 3.14 in the local venv, stdlib HTTP dashboard, Typer, SQLite, pytest, ruff.

---

## File Structure

- Create `src/polymarket_weather_arb/dashboard_ui/__init__.py`
  - Package marker for dashboard view/action modules.
- Create `src/polymarket_weather_arb/dashboard_ui/html.py`
  - Owns HTML escaping, page shell, links, tables, cards, display formatting, and scalar form parsing helpers.
- Create `src/polymarket_weather_arb/dashboard_ui/i18n.py`
  - Owns translations and `_t`.
- Create `src/polymarket_weather_arb/dashboard_ui/overview.py`
  - Owns Cockpit homepage rendering.
- Create `src/polymarket_weather_arb/dashboard_ui/exchange.py`
  - Owns open orders, positions, fills, risk, reconciliation, setup, and doctor rendering.
- Create `src/polymarket_weather_arb/services/live_monitor_service.py`
  - Owns read-only live gate snapshot and blocker reasons.
- Modify `src/polymarket_weather_arb/dashboard.py`
  - Keep `serve_dashboard`, `render_dashboard_path`, `handle_dashboard_post`, `DashboardResponse`, and `DashboardError`.
  - Import moved rendering helpers and keep compatibility aliases only when tests or internal callers still need them.
- Modify `tests/test_dashboard.py`, `tests/test_dashboard_market_workflow.py`, and `tests/test_operator_daemon.py`
  - Add focused tests for moved renderers and live monitor blockers.

## Slice 5A: Dashboard i18n And HTML Helpers

**Files:**
- Create: `src/polymarket_weather_arb/dashboard_ui/__init__.py`
- Create: `src/polymarket_weather_arb/dashboard_ui/i18n.py`
- Create: `src/polymarket_weather_arb/dashboard_ui/html.py`
- Modify: `src/polymarket_weather_arb/dashboard.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing import boundary test**

Add this test to `tests/test_dashboard.py`:

```python
def test_dashboard_uses_split_i18n_and_html_helpers():
    from polymarket_weather_arb import dashboard
    from polymarket_weather_arb.dashboard_ui import html, i18n

    assert dashboard._t is i18n._t
    assert dashboard.render_page is html.render_page
    assert dashboard._table is html._table
```

- [ ] **Step 2: Run RED test**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_uses_split_i18n_and_html_helpers -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'polymarket_weather_arb.dashboard_ui'`.

- [ ] **Step 3: Move translations to `i18n.py`**

Move the `TRANSLATIONS` dictionary and `_t(lang, key, **kwargs)` from `dashboard.py` into `dashboard_ui/i18n.py`.

Import `_t` back into `dashboard.py`:

```python
from polymarket_weather_arb.dashboard_ui.i18n import _t
```

- [ ] **Step 4: Move HTML helpers to `html.py`**

Move these helpers from `dashboard.py` into `dashboard_ui/html.py`:

```python
render_page
_section
_table
_definition_table
_render_flash
_href
_hidden_lang
_status_label
_kind_label
_bool_label
_display_time
_tags_label
_json_list_label
_duration_label
_dash
_short_note
_note_cell
_e
```

Also move constants used only by those helpers, including page CSS and nav rendering fragments if they are embedded in `render_page`.

Import them back into `dashboard.py` from `dashboard_ui.html`.

- [ ] **Step 5: Run GREEN checks**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_uses_split_i18n_and_html_helpers tests/test_dashboard.py::test_dashboard_renders_bilingual_overview_and_actions -q
```

Expected: PASS.

- [ ] **Step 6: Commit Slice 5A**

Run:

```bash
git add src/polymarket_weather_arb/dashboard.py src/polymarket_weather_arb/dashboard_ui tests/test_dashboard.py
git commit -m "Split dashboard i18n and HTML helpers"
```

## Slice 5B: Dashboard Overview And Exchange Renderers

**Files:**
- Create: `src/polymarket_weather_arb/dashboard_ui/overview.py`
- Create: `src/polymarket_weather_arb/dashboard_ui/exchange.py`
- Modify: `src/polymarket_weather_arb/dashboard.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing renderer boundary test**

Add this test to `tests/test_dashboard.py`:

```python
def test_dashboard_uses_split_overview_and_exchange_renderers():
    from polymarket_weather_arb import dashboard
    from polymarket_weather_arb.dashboard_ui import exchange, overview

    assert dashboard.render_overview is overview.render_overview
    assert dashboard.render_open_orders is exchange.render_open_orders
    assert dashboard.render_reconciliation is exchange.render_reconciliation
```

- [ ] **Step 2: Run RED test**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_uses_split_overview_and_exchange_renderers -q
```

Expected: FAIL because `overview.py` and `exchange.py` do not exist or do not export these functions.

- [ ] **Step 3: Move Cockpit overview functions**

Move these functions into `dashboard_ui/overview.py`:

```python
render_overview
_cockpit_next_action_card
_cockpit_mode_card
_cockpit_pipeline
_cockpit_top_candidates
_cockpit_blockers
_cockpit_actions
_cockpit_runs
_cockpit_label
_cockpit_action_label
_cockpit_step_label
```

Import dependencies explicitly from `dashboard_ui.html`, `dashboard_ui.i18n`, `services.cockpit_service`, and `Repository`.

- [ ] **Step 4: Move exchange/setup renderers**

Move these functions into `dashboard_ui/exchange.py`:

```python
render_open_orders
render_positions
render_fills
render_orders
render_risk
render_reconciliation
render_doctor
render_setup
_doctor_problems
_live_credentials_configured
```

Keep browser safety text and route output unchanged.

- [ ] **Step 5: Import moved functions back through dashboard facade**

Update `dashboard.py` to import moved functions:

```python
from polymarket_weather_arb.dashboard_ui.overview import render_overview
from polymarket_weather_arb.dashboard_ui.exchange import (
    render_doctor,
    render_fills,
    render_open_orders,
    render_orders,
    render_positions,
    render_reconciliation,
    render_risk,
    render_setup,
)
```

- [ ] **Step 6: Run dashboard tests**

Run:

```bash
uv run pytest tests/test_dashboard.py tests/test_dashboard_market_workflow.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Slice 5B**

Run:

```bash
git add src/polymarket_weather_arb/dashboard.py src/polymarket_weather_arb/dashboard_ui tests/test_dashboard.py
git commit -m "Split dashboard overview and exchange renderers"
```

## Slice 6: Live Monitor Snapshot

**Files:**
- Create: `src/polymarket_weather_arb/services/live_monitor_service.py`
- Modify: `src/polymarket_weather_arb/services/operator_daemon.py`
- Modify: `src/polymarket_weather_arb/dashboard.py` or `src/polymarket_weather_arb/dashboard_ui/exchange.py`
- Test: `tests/test_live_monitor_service.py`
- Test: `tests/test_operator_daemon.py`

- [ ] **Step 1: Write failing live monitor service tests**

Create `tests/test_live_monitor_service.py`:

```python
from datetime import datetime, timezone
from types import SimpleNamespace

from polymarket_weather_arb.domain.markets import Market
from polymarket_weather_arb.profiles import get_profile
from polymarket_weather_arb.services.live_monitor_service import build_live_monitor_snapshot
from polymarket_weather_arb.storage.db import Database
from polymarket_weather_arb.storage.repositories import Repository


def test_live_monitor_explains_missing_override_and_whitelist(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        repo.upsert_market(_market(), {"id": "m1"})
        repo.create_automation_action(_action("act_live_1", "m1"))

        snapshot = build_live_monitor_snapshot(
            repo,
            profile=get_profile("micro-live"),
            allow_live_auto=True,
            live_market_ids={"other"},
            require_fresh_reconciliation=False,
            block_live_on_positions=True,
        )

        action = snapshot.pending_live_actions[0]
        assert action.can_auto_execute is False
        assert any(gate.name == "whitelist" and not gate.ok for gate in action.gates)
        assert any(gate.name == "override" and not gate.ok for gate in action.gates)
        assert "market is not whitelisted" in snapshot.blockers
    finally:
        connection.close()


def test_live_monitor_reports_ready_when_all_gates_pass(tmp_path):
    _, connection, repo = _repo(tmp_path)
    try:
        repo.upsert_market(_market(), {"id": "m1"})
        repo.create_automation_action(_action("act_live_2", "m1"))
        repo.save_reconciliation("ok", {"test": True})
        repo.upsert_strategy_override(market_id="m1", profile="micro-live", live_auto_enabled=True)

        snapshot = build_live_monitor_snapshot(
            repo,
            profile=get_profile("micro-live"),
            allow_live_auto=True,
            live_market_ids={"m1"},
            require_fresh_reconciliation=True,
            block_live_on_positions=True,
        )

        assert snapshot.risk_status == "ok"
        assert snapshot.reconciliation_fresh is True
        assert snapshot.pending_live_actions[0].can_auto_execute is True
        assert snapshot.blockers == []
    finally:
        connection.close()
```

Include local `_repo`, `_market`, and `_action` helpers in the test file.

- [ ] **Step 2: Run RED test**

Run:

```bash
uv run pytest tests/test_live_monitor_service.py -q
```

Expected: FAIL because `live_monitor_service.py` does not exist.

- [ ] **Step 3: Implement live monitor dataclasses and builder**

Create `src/polymarket_weather_arb/services/live_monitor_service.py` with:

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

Implement `build_live_monitor_snapshot(repository, *, profile, allow_live_auto, live_market_ids, require_fresh_reconciliation=True, block_live_on_positions=True)`.

- [ ] **Step 4: Share gate evaluation with daemon**

Add a pure helper such as `live_action_gates(...)` in `live_monitor_service.py`. Update `OperatorDaemon._can_auto_live()` to call it and return `all(gate.ok for gate in gates)`.

- [ ] **Step 5: Run live monitor and daemon tests**

Run:

```bash
uv run pytest tests/test_live_monitor_service.py tests/test_operator_daemon.py -q
```

Expected: PASS.

- [ ] **Step 6: Show live monitor status in existing safe UI**

Add a read-only section to `render_setup()` or `render_operator()` showing:

- profile
- allow_live_auto=false for browser tick
- pending live action count
- blocker list

The section must not include submit buttons for live actions.

- [ ] **Step 7: Run dashboard safety tests**

Run:

```bash
uv run pytest tests/test_dashboard.py tests/test_dashboard_market_workflow.py tests/test_live_monitor_service.py tests/test_operator_daemon.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Slice 6**

Run:

```bash
git add src/polymarket_weather_arb/services/live_monitor_service.py src/polymarket_weather_arb/services/operator_daemon.py src/polymarket_weather_arb/dashboard.py src/polymarket_weather_arb/dashboard_ui tests/test_live_monitor_service.py tests/test_operator_daemon.py tests/test_dashboard.py
git commit -m "Add live monitor gate snapshot"
```

## Slice 7 And 8 Follow-Up Boundaries

Do not start these until Slice 5 and Slice 6 are green and committed.

- Repository split should begin with a plan that keeps `Repository` as facade.
- Operator command refinement should begin after live monitor output is stable, because operator daemon/status commands should consume that snapshot.

## Final Verification

After all implemented slices in this batch:

```bash
uv run pytest -q
uv run --extra dev ruff check src/ tests/
.venv/bin/ruff format --check src/polymarket_weather_arb/dashboard.py src/polymarket_weather_arb/dashboard_ui src/polymarket_weather_arb/services/live_monitor_service.py tests/test_live_monitor_service.py tests/test_dashboard.py tests/test_operator_daemon.py
git diff --check
```
