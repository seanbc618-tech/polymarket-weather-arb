# Grok Worker Task: Refresh The macOS Beginner DMG

## Objective

Rebuild and honestly verify the existing Scheme A macOS beginner application
from the latest `main`, so the DMG shared with friends includes all runtime fixes
through commit `9828b90`, especially:

- live ticks may refresh stale reconciliation instead of self-blocking;
- `/app` owns automatic Telegram notifications while the legacy
  `operator daemon` requires explicit `--notify-telegram`;
- one-sided CLOB order books no longer fail with `None + Decimal`.

This is a packaging and release-refresh task. Do not redesign the desktop app,
add an updater, create another launcher, or change trading strategy/risk logic.

## Mandatory Starting Point

1. Work in `/path/to/polymarket-weather-arb`.
2. Read `AGENTS.md` and `docs/agent-worker-standards.md` first.
3. Confirm `HEAD` and `origin/main` contain commit `9828b90` or a reviewed newer
   commit. Run `git status --short` and preserve unrelated work.
4. Do not force-push and do not rewrite published history.
5. Do not execute any real BUY, SELL, cancel, approval, allowance, deposit, or
   other exchange mutation.

## Reuse Map

Reuse these existing owners:

- `scripts/build_macos_app.sh`: the only DMG build path;
- `scripts/smoke_packaged_app.sh`: the packaged-app lifecycle smoke test;
- `packaging/macos/polymarket_weather.spec`: the existing PyInstaller bundle;
- `desktop/launcher.py`: the existing lifecycle shell;
- `/setup`, `/app`, `AutopilotService`, `Settings`, and the existing SQLite
  repository unchanged as product owners;
- `docs/runbooks/macos-beginner-app.md`: the only distribution runbook.

Do not create a second build script, smoke script, launcher, settings model,
database, Telegram transport, scheduler, BUY path, or SELL path.

## Required Work

### 1. Establish A Distinct Release Version

Use version `0.1.1` for this refreshed friend build.

Keep the smallest coherent version change across the existing version sources:

- `pyproject.toml`;
- `scripts/build_macos_app.sh` default artifact version;
- `packaging/macos/polymarket_weather.spec` bundle version and Info.plist values;
- examples in `docs/runbooks/macos-beginner-app.md`;
- existing packaging tests.

Do not introduce a version service or runtime dependency. If you minimally remove
hard-coded drift by deriving an existing build value from one source, explain the
change and keep it shell/PyInstaller compatible. Otherwise update all current
sources consistently and add a regression assertion that catches future drift.

### 2. Run Source Quality Gates Before Packaging

Run:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
```

The expected baseline after commit `9828b90` is 689 passing tests. A different
count is acceptable only if every added/removed test is explained.

### 3. Build Only Through The Existing Script

Build natively on this Mac using the existing path:

```bash
APP_VERSION=0.1.1 ./scripts/build_macos_app.sh
```

Expected local artifacts:

```text
dist/Polymarket Weather.app
dist/Polymarket Weather-0.1.1.dmg
```

Do not commit `dist/`, `build/`, the `.app`, or the `.dmg`. Do not bundle `.env`,
SQLite databases, logs, API keys, wallet keys, Telegram tokens, or credentials.

### 4. Verify The Built Artifact, Not Only Source Tests

Provide command evidence for all of the following:

1. `CFBundleShortVersionString` and `CFBundleVersion` are `0.1.1`.
2. Bundle identifier remains `com.seanbc.polymarket-weather`.
3. The executable architecture is reported (`arm64` is expected on the current
   build Mac; do not claim Intel compatibility unless separately built/tested).
4. `codesign --verify --deep --strict` result is reported truthfully. Ad-hoc
   signing is acceptable; do not claim notarization.
5. The bundle scan finds no forbidden config, DB, log, private-key, PEM, or
   secret-like material.
6. The DMG mounts, contains `Polymarket Weather.app` and the Applications link,
   and cleanly unmounts.
7. `./scripts/smoke_packaged_app.sh` passes using its isolated HOME and reports
   zero live order intents.
8. A second launch focuses/exits without creating another backend, and Quit
   stops the packaged process, as exercised by the existing smoke.

Do not weaken smoke assertions to make the build pass. Fix a real packaging bug
through the existing owner if one is found.

### 5. Verify Upgrade Preservation

Using an isolated temporary `POLYMARKET_DESKTOP_DATA_ROOT`, verify that replacing
the old app bundle with the `0.1.1` bundle does not erase the existing
Application Support config, setup marker, database, or logs. Never use or modify
the user's real `~/Library/Application Support/Polymarket Weather` during this
test.

This may be a documented manual acceptance sequence if automating it would add a
second test harness. Do not add migration machinery unless a real failure is
observed.

### 6. Produce A Shareable Checksum

Create a local SHA-256 checksum next to the DMG, for example:

```bash
shasum -a 256 "dist/Polymarket Weather-0.1.1.dmg"
```

Report the exact hash, artifact size, architecture, signing status, and absolute
artifact path. The checksum file may remain local under `dist/`; do not commit
the binary artifact.

### 7. Update The Existing Runbook

Update `docs/runbooks/macos-beginner-app.md` only as needed so it accurately says:

- current example artifact is `0.1.1`;
- users must Quit the old app before replacing it;
- Application Support data is retained across upgrades;
- this build is native to its build architecture;
- ad-hoc/unsigned distribution may require right-click Open and is not notarized;
- the friend should delete/replace the old DMG rather than assume it auto-updates.

Do not create a competing distribution guide.

## Acceptance Tests For The Two Latest Fixes

The source test suite must include and pass the existing regressions proving:

- a legacy daemon does not automatically claim `/app` Telegram configuration;
- explicit legacy `--notify-telegram` still works;
- bid-only and ask-only order books parse without exceptions;
- completely empty books remain non-tradable/unknown rather than inventing
  liquidity.

Do not send a real Telegram message and do not hit a live order mutation to test
these behaviors.

## Git And Artifact Rules

- Commit only necessary version, packaging, test, and existing-runbook changes.
- Normal push to `origin/main`; no force-push.
- Keep generated `dist/` and `build/` artifacts out of Git.
- Do not publish a GitHub Release or upload the DMG anywhere unless the user
  separately requests it.
- Leave the worktree clean after commit, excluding already-known unrelated user
  files if any; list them explicitly.

## Required Worker Report

Report:

1. objective completed;
2. exact starting and ending commit hashes;
3. existing components reused;
4. every source file changed and why;
5. new files/classes/tables/settings introduced (expected: none besides this
   task document already present);
6. duplicate code removed (expected: none, unless version drift is minimally
   consolidated);
7. exact ruff, pytest, diff-check, build, codesign, DMG mount, and packaged-smoke
   results;
8. exact DMG absolute path, byte/MB size, SHA-256, architecture, bundle version,
   and signing/notarization status;
9. upgrade-preservation verification result;
10. commit hash, push result, and final `git status --short`;
11. explicit statement that no real trading mutation was executed.

Stop and ask for review if the task appears to require a new launcher/framework,
an auto-updater, a database migration, real account access, or weakening any
packaged smoke assertion.
