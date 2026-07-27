# Antigravity Worker Task: Refresh macOS Beginner DMG 0.1.2

## Objective

Build and verify a new macOS beginner distribution containing current `main`,
including commit `d649470` and the previously accepted three-day discovery work.
Release it as **0.1.2**.

This is packaging work only. Do not redesign the desktop app, create a new
launcher, change trading behavior, or execute any exchange mutation.

## Mandatory Context

Read these before editing:

1. `AGENTS.md`
2. `docs/agent-worker-standards.md`
3. `docs/runbooks/macos-beginner-app.md`
4. `scripts/build_macos_app.sh`
5. `scripts/smoke_packaged_app.sh`
6. `tests/test_packaged_manifest.py`

Confirm before starting:

```bash
git status --short
git log -3 --oneline
git rev-parse HEAD
```

`d649470` must be an ancestor of `HEAD`. Preserve any unrelated user changes.

## Existing Owners To Reuse

- Version sources: `pyproject.toml`,
  `src/polymarket_weather_arb/__init__.py`,
  `packaging/macos/polymarket_weather.spec`, and the default in
  `scripts/build_macos_app.sh`.
- Packaging: `scripts/build_macos_app.sh`.
- Honest packaged verification: `scripts/smoke_packaged_app.sh`.
- Distribution instructions: `docs/runbooks/macos-beginner-app.md`.
- Version consistency gate: `tests/test_packaged_manifest.py`.

Do not add a second build script, launcher, app bundle definition, smoke script,
updater, service, database table, or configuration system.

## Required Changes

### 1. Bump The Friend-Share Version To 0.1.2

Update every authoritative hard-coded packaging version from `0.1.1` to `0.1.2`:

- `pyproject.toml`
- `src/polymarket_weather_arb/__init__.py`
- `packaging/macos/polymarket_weather.spec`
- `scripts/build_macos_app.sh`
- `tests/test_packaged_manifest.py`
- current artifact names and upgrade examples in
  `docs/runbooks/macos-beginner-app.md`

Search for remaining stale release references:

```bash
rg -n '0\.1\.1|Polymarket Weather-0\.1\.1' \
  pyproject.toml src packaging scripts tests docs/runbooks/macos-beginner-app.md
```

Historical worker reports may keep their original version text. Do not rewrite
unrelated historical documents.

### 2. Prove The Bundle Contains Current Fixes

Before building, verify current source contains:

- opportunity-scoped BUY idempotency;
- reconciliation-scoped SELL idempotency;
- `settled_win_redeemable` / `settled_loss_zero_value`;
- D0/D1/D2 weather discovery and IANA Open-Meteo timezone handling;
- paper mode not blocked by `TRADING_DISABLED=true`.

Do not reimplement or modify these features. This step only guards against
building from an old checkout.

### 3. Run Source Gates

Use isolated test risk caps:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
```

Expected baseline at handoff: **705 tests pass**. A different count is acceptable
only when explained by deliberate test changes in this packaging slice.

### 4. Build With The Existing Script

Build only on macOS:

```bash
APP_VERSION=0.1.2 ./scripts/build_macos_app.sh
```

Expected artifacts:

- `dist/Polymarket Weather.app`
- `dist/Polymarket Weather-0.1.2.dmg`

Do not embed `.env`, SQLite databases, logs, private keys, Telegram tokens, API
credentials, or the user's Keychain data. Keep existing bundle scans enabled.

Do not claim notarization unless `NOTARIZE_PROFILE` was actually configured and
the notarization command succeeded. Ad-hoc signing is acceptable but must be
reported honestly.

### 5. Run Honest Packaged Smoke

Run the existing packaged smoke workflow against the newly built app:

```bash
APP_VERSION=0.1.2 ./scripts/smoke_packaged_app.sh
```

The smoke must verify the packaged binary, setup flow, paper mode, `/app`, pause,
duplicate-launch handling, quit, and relaunch. It must not manually forge success
state or silently fall back to source-tree execution.

No live BUY, SELL, cancellation, redemption, approval, or account mutation is
allowed. Disable Telegram during smoke so the user receives no test alerts.

### 6. Inspect Distribution Artifact

Report exact outputs for:

```bash
du -sh "dist/Polymarket Weather.app" "dist/Polymarket Weather-0.1.2.dmg"
file "dist/Polymarket Weather.app/Contents/MacOS/Polymarket Weather"
codesign --verify --deep --strict "dist/Polymarket Weather.app"
shasum -a 256 "dist/Polymarket Weather-0.1.2.dmg"
```

Also mount or open the DMG and confirm it contains exactly the app plus the
Applications shortcut. Do not launch a live/full-auto mode during inspection.

### 7. Git Discipline

- Commit source/docs/version changes only.
- Do not commit `dist/`, `build/`, generated `.app`, generated `.dmg`, logs,
  databases, caches, or credentials unless the repository already explicitly
  tracks a named release artifact (it currently should not).
- Normal `git push origin main`; never force-push.
- Finish with `git status --short` and explain every remaining path.

## Acceptance Checklist

1. All authoritative version sources say `0.1.2`.
2. `tests/test_packaged_manifest.py` passes.
3. Full pytest and ruff pass.
4. Existing build script succeeds without parallel packaging code.
5. App bundle contains AppKit and launches through the existing desktop path.
6. Honest packaged smoke passes.
7. Bundle scan finds no secrets, `.env`, DBs, or logs.
8. DMG filename is exactly `Polymarket Weather-0.1.2.dmg`.
9. Architecture, signing status, size, and SHA-256 are reported.
10. No real trading/account mutation occurred.

## Required Worker Report

Return:

- objective completed or exact blocker;
- existing components reused;
- files changed;
- version-consistency search result;
- source gate outputs;
- build command and result;
- packaged smoke result;
- app/DMG sizes, architecture, signing/notarization status, and SHA-256;
- commit hash and push result;
- final `git status --short`;
- explicit statement that no real trading mutation was executed.

