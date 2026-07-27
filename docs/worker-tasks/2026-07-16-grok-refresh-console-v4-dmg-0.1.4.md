# Grok Worker Task: Refresh Console V4 macOS DMG 0.1.4

## Objective

Package the already-accepted `/app` console V4 component integration from commit
`c81d4b4` as a beginner macOS release. Produce and honestly verify separate
native artifacts for Apple Silicon and Intel Macs.

This is a packaging-only task. Do not redesign `/app`, edit trading strategy,
pricing, execution, reconciliation, database semantics, desktop configuration,
or safety behavior. Do not execute any real account or trading mutation.

## Mandatory Baseline

Read `AGENTS.md`, `docs/agent-worker-standards.md`,
`docs/runbooks/macos-beginner-app.md`, `scripts/build_macos_app.sh`,
`scripts/smoke_packaged_app.sh`, `packaging/macos/polymarket_weather.spec`, and
`tests/test_packaged_manifest.py` before editing.

Run and report:

```bash
git status --short
git rev-parse HEAD
git merge-base --is-ancestor c81d4b4 HEAD
```

The ancestor check must succeed. Preserve unrelated files. Never force-push.

## Existing Owners To Reuse

- `scripts/build_macos_app.sh`: only app/DMG build implementation.
- `scripts/smoke_packaged_app.sh`: only packaged lifecycle smoke.
- `packaging/macos/polymarket_weather.spec`: only PyInstaller bundle spec.
- `.github/workflows/package-macos-x86_64.yml`: existing Intel build workflow.
- `docs/runbooks/macos-beginner-app.md`: install and troubleshooting owner.
- `tests/test_packaged_manifest.py`: packaging contract owner.

Do not create a second launcher, setup UI, build script, smoke script, workflow,
configuration store, or update mechanism.

## Required Work

1. Bump the current release from `0.1.3` to `0.1.4` in the existing authoritative
   version sources, build defaults, packaging tests, current runbook examples,
   and a new short `docs/releases/0.1.4.md`. Do not rewrite historical reports.
2. Build `Polymarket Weather-0.1.4-macos-arm64.dmg` on the current Apple Silicon
   host using the existing build script. Produce its `.sha256` file.
3. Run the existing packaged-app smoke against the app extracted from that DMG.
   It must exercise first-run setup, paper mode, `/app`, pause, duplicate launch,
   quit, and relaunch using the packaged executable, not source code.
4. Trigger the existing `package-macos-x86_64.yml` workflow for a real Intel
   build. The job must assert `uname -m == x86_64`, call the same build and smoke
   scripts, and upload the DMG plus checksum. Do not simulate Intel by renaming an
   arm64 artifact. If a native Intel runner is unavailable, report a blocker.
5. Verify each artifact on its matching architecture with `file`, `lipo -archs`,
   `codesign --verify --deep --strict`, packaged smoke, DMG contents, and SHA-256.
6. Keep `docs/releases/0.1.4.md` short and beginner-friendly. Mention the compact
   V4 console, collapsed completed Setup/mode controls, first-screen checks,
   safety/PnL/positions/funnel, compact real-data tables, and expandable history.
   Include which download is for Apple Silicon versus Intel and the upgrade path.

## Acceptance Gates

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

Also open the packaged app and verify `/app?lang=zh` at desktop width:

- no demo scenario controls;
- completed Setup uses the compact command toolbar;
- checks and safety are side by side;
- PnL follows immediately;
- positions and funnel use real persisted data;
- candidate markets show eight rows before expansion;
- recent runs show six rows before expansion;
- mode changes, pause, and one-tick controls still work without a real trade.

## Git And Artifact Discipline

Commit source metadata, tests, runbook, release note, and any narrowly required
workflow adjustment. Do not commit `dist/`, `build/`, `.app`, `.dmg`, checksum
artifacts, screenshots, logs, databases, caches, `.env`, or credentials. Push
normally to `origin/main`; never force-push. Do not start or stop the user's live
daemon.

The final worker report must follow `docs/agent-worker-standards.md`: exact reused
components, files changed, test results, commit hash, artifact architecture and
checksum evidence, remaining working-tree changes, and an explicit statement
that no real trading mutation was executed.
