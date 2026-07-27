# Autopilot App Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current Autopilot worktree into a clean, committed, beginner-first local app without weakening advanced safety gates.

**Architecture:** Keep the existing weather/research/trading core. Productize `/app` as the default beginner surface, add first-run readiness checks, expose four user-facing modes, and keep Operator, Live Launchpad, and Calibration as advanced pages.

**Tech Stack:** Python, Typer, stdlib HTTP dashboard, SQLite, pytest, ruff.

---

### Task 1: Commit Current Verified Autopilot Baseline

**Files:**
- Stage all current weather-app/autopilot/deploy/LLM changes.
- Keep the crypto project document deletion as an explicit cleanup commit.

- [ ] **Step 1: Verify baseline**

Run:

```bash
uv run ruff check src/ tests/ scripts/
uv run pytest -q
```

Expected: lint passes and the full suite passes.

- [ ] **Step 2: Commit product baseline**

Run:

```bash
git add .env.example deploy scripts src tests docs/superpowers
git commit -m "Build beginner autopilot app shell"
```

Expected: Autopilot, `/app`, deploy, LLM advisor, DB schema, and tests are committed.

- [ ] **Step 3: Commit repo cleanup**

Run:

```bash
git add docs/crypto-threshold-project
git commit -m "Remove crypto threshold blueprint from weather repo"
```

Expected: crypto blueprint deletion is separate from weather app changes.

### Task 2: Make `/app` the Beginner Entry

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `src/polymarket_weather_arb/dashboard.py`
- Test: `tests/test_dashboard_app.py`

- [ ] **Step 1: Update tests**

Add assertions that root redirects to `/app`, README mentions `uv run polymarket-weather autopilot start`, and old beginner/operator pages are advanced paths.

- [ ] **Step 2: Update docs**

Make Quick Start:

```bash
uv sync
uv run polymarket-weather init-db
uv run polymarket-weather autopilot start
```

State that advanced pages remain available at `/beginner-legacy`, `/live`, `/calibration`, `/actions`, `/overrides`.

### Task 3: Add First-Run Readiness Panel

**Files:**
- Modify: `src/polymarket_weather_arb/services/autopilot_service.py`
- Modify: `src/polymarket_weather_arb/dashboard_ui/app.py`
- Test: `tests/test_autopilot_service.py`
- Test: `tests/test_dashboard_app.py`

- [ ] **Step 1: Add snapshot checks**

Expose database, weather provider, Polymarket read config, compliance setting, reconciliation freshness, trading disabled state, and live credential readiness in the app snapshot.

- [ ] **Step 2: Render first-run panel**

Show each check with ok/warn/blocked status and one short user-facing explanation. Do not trigger network calls during page render.

### Task 4: Add Four Product Modes

**Files:**
- Modify: `src/polymarket_weather_arb/services/autopilot_service.py`
- Modify: `src/polymarket_weather_arb/storage/db.py`
- Modify: `src/polymarket_weather_arb/storage/repositories.py`
- Modify: `src/polymarket_weather_arb/dashboard.py`
- Modify: `src/polymarket_weather_arb/dashboard_ui/app.py`
- Test: `tests/test_autopilot_service.py`
- Test: `tests/test_dashboard_app.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Store app mode**

Add `app_mode` to `autopilot_state`, with allowed values `observe`, `paper`, `micro_live`, and `full_live`.

- [ ] **Step 2: Map app mode to execution**

Use `observe` for analysis-only ticks, `paper` for dry-run trade intents, `micro_live` for live mode with existing micro-live gates, and keep `full_live` locked/rejected.

- [ ] **Step 3: Render mode controls**

Show all four modes. Default to `paper`. Mark `full_live` as locked until a future risk-control slice.

### Task 5: Final Verification and Clean Worktree

**Files:**
- All touched files.

- [ ] **Step 1: Run final verification**

Run:

```bash
uv run ruff check src/ tests/ scripts/
uv run pytest -q
git status --short
```

Expected: lint passes, tests pass, and worktree is clean after commits.

- [ ] **Step 2: Commit implementation**

Run:

```bash
git add README.md README.zh-CN.md src tests docs/superpowers/plans/2026-07-08-autopilot-app-cleanup.md
git commit -m "Polish autopilot app onboarding"
```

Expected: all planned changes are committed with no leftover files.
