# Cockpit Thin Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Slice 1 of the cockpit thin-slice design: a read-model-backed dashboard homepage that shows next action, candidate pipeline, blockers, top candidates, and recent activity without adding live controls.

**Architecture:** Add a focused `cockpit_service.py` that derives a `CockpitSnapshot` from repository reads only. Update `dashboard.py` so `/` renders that snapshot instead of the generic overview while preserving existing routes and command behavior.

**Tech Stack:** Python 3.12, stdlib SQLite/http dashboard, Typer CLI, pytest.

---

## File Structure

- Create `src/polymarket_weather_arb/services/cockpit_service.py`
  - Owns homepage read models and next-action selection.
  - Performs no network calls and creates no orders/actions.
- Modify `src/polymarket_weather_arb/dashboard.py`
  - Imports `build_cockpit_snapshot`.
  - Replaces `render_overview()` content with Cockpit sections.
  - Adds only small rendering helpers if needed.
- Modify `tests/test_dashboard.py`
  - Updates existing overview expectations.
  - Adds a focused Cockpit test covering next action, pipeline, blockers, and no live controls.
- Create `tests/test_cockpit_service.py`
  - Tests read model behavior independently of HTML rendering.

## Task 1: Cockpit Service Read Model

**Files:**
- Create: `src/polymarket_weather_arb/services/cockpit_service.py`
- Test: `tests/test_cockpit_service.py`

- [ ] **Step 1: Write failing service tests**

```python
def test_cockpit_suggests_discovery_when_no_candidates(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / 'cockpit.db')
    Database(settings.database_path).init_schema()
    snapshot = _snapshot(settings)
    assert snapshot.next_action.label == 'Run discovery'
    assert snapshot.next_action.href == '/discovery'
    assert snapshot.pipeline.found == 0


def test_cockpit_prioritizes_ready_candidates_and_missing_signal_blocker(tmp_path):
    settings = Settings(DATABASE_PATH=tmp_path / 'cockpit.db')
    _seed_china_candidate(settings, with_snapshot=True, with_forecast=False)
    snapshot = _snapshot(settings)
    assert snapshot.next_action.label == 'Refresh missing signals'
    assert snapshot.pipeline.found == 1
    assert snapshot.pipeline.parsed == 1
    assert snapshot.pipeline.quoted == 1
    assert snapshot.pipeline.signal_ready == 0
    assert snapshot.pipeline.analyzed == 0
    assert snapshot.top_candidates[0].market_id == 'shanghai-18c'
    assert any('signal' in blocker.message.lower() for blocker in snapshot.blockers)
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest tests/test_cockpit_service.py -q
```

Expected: fails because `polymarket_weather_arb.services.cockpit_service` does not exist.

- [ ] **Step 3: Implement minimal read model**

Create dataclasses:

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


@dataclass(frozen=True)
class CockpitSnapshot:
    next_action: NextActionSuggestion
    pipeline: CandidatePipelineSummary
    top_candidates: list[CandidateSummary]
    blockers: list[BlockerSummary]
    recent_actions: list[ActionSummary]
    recent_runs: list[RunSummary]
    mode: str
    profile: str
```

Implement `build_cockpit_snapshot(repository, *, profile='dry-run-demo')`.

- [ ] **Step 4: Run service tests to verify GREEN**

Run:

```bash
uv run pytest tests/test_cockpit_service.py -q
```

Expected: all tests in `tests/test_cockpit_service.py` pass.

## Task 2: Cockpit Dashboard Homepage

**Files:**
- Modify: `src/polymarket_weather_arb/dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing dashboard test**

Add assertions that `/` renders:

```python
assert '操作台' in zh_overview.body
assert '下一步' in zh_overview.body
assert '候选漏斗' in zh_overview.body
assert '阻塞' in zh_overview.body
assert 'trade_live' not in zh_overview.body
assert 'Live Auto' not in zh_overview.body
```

- [ ] **Step 2: Run dashboard test to verify RED**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_renders_bilingual_overview_and_actions -q
```

Expected: fails because the old overview does not contain the new Cockpit labels.

- [ ] **Step 3: Render Cockpit from `render_overview()`**

Update `render_overview(repository, settings, lang, current_path)` to:

- Build a snapshot with `build_cockpit_snapshot(repository)`.
- Render `Next action`, `Current mode`, `Candidate pipeline`, `Top candidates`, `Blockers and failures`, and recent activity cards.
- Keep existing `render_page()` wrapper and language support.
- Do not add live action forms or live execution buttons.

- [ ] **Step 4: Run dashboard test to verify GREEN**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_renders_bilingual_overview_and_actions -q
```

Expected: selected test passes.

## Task 3: Focused Cockpit Regression Coverage

**Files:**
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Add a seeded cockpit rendering test**

Add a test that seeds a China candidate with a quote but no forecast, renders `/?lang=zh`, and asserts:

```python
assert 'Refresh missing signals' in page.body or '刷新缺失信号' in page.body
assert 'shanghai-18c' in page.body
assert 'Found' in page.body or '发现' in page.body
assert 'Signal' in page.body or '信号' in page.body
assert 'trade_live' not in page.body
```

- [ ] **Step 2: Run test to verify RED if labels are incomplete**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_cockpit_shows_pipeline_and_blockers -q
```

Expected: fails until dashboard rendering exposes the seeded candidate and blocker.

- [ ] **Step 3: Fill missing renderer details**

Add the missing rows/labels/links to `render_overview()` while keeping rendering simple and stdlib-only.

- [ ] **Step 4: Run focused dashboard tests**

Run:

```bash
uv run pytest tests/test_cockpit_service.py tests/test_dashboard.py::test_dashboard_renders_bilingual_overview_and_actions tests/test_dashboard.py::test_dashboard_cockpit_shows_pipeline_and_blockers -q
```

Expected: all selected tests pass.

## Task 4: Existing Dashboard Workflow Regression

**Files:**
- No planned production changes unless tests reveal regressions.

- [ ] **Step 1: Run dashboard/workflow tests**

Run:

```bash
uv run pytest tests/test_dashboard.py tests/test_dashboard_market_workflow.py tests/test_cockpit_service.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Fix regressions only inside Cockpit scope**

If failures occur, adjust only `cockpit_service.py`, overview rendering, or tests whose old expectations are replaced by Cockpit behavior.

## Task 5: Manual Browser Smoke

**Files:**
- No planned file changes.

- [ ] **Step 1: Start dashboard**

Run:

```bash
uv run polymarket-weather dashboard --host 127.0.0.1 --port 8765
```

- [ ] **Step 2: Open dashboard**

Open:

```text
http://127.0.0.1:8765/?lang=zh
```

Expected:

- Homepage shows Cockpit sections.
- Primary action links to a real page.
- No live execution controls appear.
- Candidate and blocker text fits in cards and table cells.

## Task 6: Final Verification

**Files:**
- No planned file changes.

- [ ] **Step 1: Run focused verification**

Run:

```bash
uv run pytest tests/test_cockpit_service.py tests/test_dashboard.py tests/test_dashboard_market_workflow.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Inspect diff**

Run:

```bash
git diff -- src/polymarket_weather_arb/services/cockpit_service.py src/polymarket_weather_arb/dashboard.py tests/test_cockpit_service.py tests/test_dashboard.py docs/superpowers/plans/2026-05-31-cockpit-thin-slice.md
```

Expected: diff only contains Cockpit Slice 1 changes.
