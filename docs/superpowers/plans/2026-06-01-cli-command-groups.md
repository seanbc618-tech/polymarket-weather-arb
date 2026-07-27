# CLI Command Groups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the largest Typer command groups out of `cli.py` while preserving the existing `polymarket-weather` command surface.

**Architecture:** Keep `src/polymarket_weather_arb/cli.py` as the application entry point and root-command module for this slice. Move grouped commands into `src/polymarket_weather_arb/cli_commands/`, with shared settings, repository, workflow, console, formatting, and risk helpers in `cli_commands/common.py`. Register each command group through module-level `Typer` apps imported by `cli.py`.

**Tech Stack:** Python 3.11+, Typer, Rich, pytest `CliRunner`, uv.

---

### Task 1: Baseline Branch And Plan Commit

**Files:**
- Create: `docs/superpowers/plans/2026-06-01-cli-command-groups.md`

- [x] **Step 1: Confirm clean status**

Run: `git status -sb`
Expected: clean working tree on a non-main branch.

- [x] **Step 2: Create feature branch**

Run: `git switch -c codex/cli-command-groups`
Expected: branch created from the Slice 3 head.

- [ ] **Step 3: Commit the plan alone**

Run:
```bash
git add docs/superpowers/plans/2026-06-01-cli-command-groups.md
git commit -m "Document CLI command group split plan"
```

Expected: one documentation-only commit.

### Task 2: Command Group Registration Tests

**Files:**
- Modify: `tests/test_cli_operator.py`

- [ ] **Step 1: Write failing tests**

Add tests that assert grouped command modules are imported and registered independently:

```python
def test_cli_registers_command_group_modules():
    from polymarket_weather_arb import cli
    from polymarket_weather_arb.cli_commands import automation, fixtures, operator, profiles

    assert cli.automation_app is automation.automation_app
    assert cli.fixtures_app is fixtures.fixtures_app
    assert cli.operator_app is operator.operator_app
    assert cli.profiles_app is profiles.profiles_app


def test_cli_command_groups_keep_existing_help_names():
    result = runner.invoke(app, ['--help'])

    assert result.exit_code == 0
    assert 'automation' in result.stdout
    assert 'fixtures' in result.stdout
    assert 'operator' in result.stdout
    assert 'profiles' in result.stdout
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_cli_operator.py::test_cli_registers_command_group_modules -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'polymarket_weather_arb.cli_commands'`.

### Task 3: Shared CLI Helper Module

**Files:**
- Create: `src/polymarket_weather_arb/cli_commands/__init__.py`
- Create: `src/polymarket_weather_arb/cli_commands/common.py`
- Modify: `src/polymarket_weather_arb/cli.py`

- [ ] **Step 1: Create package marker**

Create `cli_commands/__init__.py` with:

```python
"""Typer command groups for the polymarket-weather CLI."""
```

- [ ] **Step 2: Move shared helpers to common**

Move these imports and helpers from `cli.py` into `cli_commands/common.py`: `Console`, settings/database/repository helpers, profile lookup, workflow factory, dashboard notifier, table/detail printers, risk context helpers, `_analysis_from_row`, and scalar formatting helpers. Keep the public names prefixed with `_` for compatibility within CLI internals.

- [ ] **Step 3: Re-export helpers in cli.py as needed**

Import shared helpers back into `cli.py` so root commands continue to work before group modules are extracted.

- [ ] **Step 4: Verify no behavior change**

Run: `uv run pytest tests/test_cli_operator.py::test_doctor_live_prints_relayer_readiness -q`
Expected: PASS.

### Task 4: Extract Profiles And Fixtures Groups

**Files:**
- Create: `src/polymarket_weather_arb/cli_commands/profiles.py`
- Create: `src/polymarket_weather_arb/cli_commands/fixtures.py`
- Modify: `src/polymarket_weather_arb/cli.py`

- [ ] **Step 1: Move profiles commands**

Create `profiles.py` with a module-level `profiles_app = typer.Typer(...)` and move `profiles_list()` and `profiles_show()` into it.

- [ ] **Step 2: Move fixtures commands**

Create `fixtures.py` with a module-level `fixtures_app = typer.Typer(...)` and move fixture import/load commands into it. Keep the root alias `fixtures-import-market-json` in `cli.py` for backward compatibility by importing `_import_market_json_command`.

- [ ] **Step 3: Register imported apps**

Update `cli.py` to import `fixtures_app` and `profiles_app` from the new modules and call `app.add_typer(...)` as before.

- [ ] **Step 4: Verify focused behavior**

Run: `uv run pytest tests/test_cli_operator.py::test_profiles_list_and_show tests/test_fixture_service.py -q`
Expected: PASS.

### Task 5: Extract Automation And Operator Groups

**Files:**
- Create: `src/polymarket_weather_arb/cli_commands/automation.py`
- Create: `src/polymarket_weather_arb/cli_commands/operator.py`
- Modify: `src/polymarket_weather_arb/cli.py`

- [ ] **Step 1: Move automation commands**

Create `automation.py` with `automation_app = typer.Typer(...)` and move `automation_propose`, `automation_approve`, `automation_reject`, `automation_status`, `automation_execute`, and `automation_execute_approved`.

- [ ] **Step 2: Move operator commands**

Create `operator.py` with `operator_app = typer.Typer(...)` and move `_operator_go_impl` plus all `operator_*` commands. Import shared helpers from `common.py`.

- [ ] **Step 3: Register imported apps**

Update `cli.py` to import `automation_app` and `operator_app` from the new modules and call `app.add_typer(...)` as before.

- [ ] **Step 4: Verify red test turns green**

Run: `uv run pytest tests/test_cli_operator.py::test_cli_registers_command_group_modules -q`
Expected: PASS.

- [ ] **Step 5: Verify operator behavior**

Run: `uv run pytest tests/test_cli_operator.py -q`
Expected: PASS.

### Task 6: Full Verification And Commit

**Files:**
- Modify: all files touched above.

- [ ] **Step 1: Run full tests**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Run lint**

Run: `uv run --extra dev ruff check src/ tests/`
Expected: `All checks passed!`

- [ ] **Step 3: Review staged diff**

Run: `git diff --stat` and `git diff --check`
Expected: command group extraction only; no whitespace errors.

- [ ] **Step 4: Commit implementation**

Run:
```bash
git add src/polymarket_weather_arb/cli.py src/polymarket_weather_arb/cli_commands tests/test_cli_operator.py
git commit -m "Split CLI command groups"
```

Expected: one implementation commit separate from the plan commit.
