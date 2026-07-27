# Antigravity Worker Task: macOS DMG 0.1.3 Dual Architecture

## Objective

Refresh the beginner macOS distribution from the current `main` and publish two
honestly verified native artifacts:

- `Polymarket Weather-0.1.3-macos-arm64.dmg`
- `Polymarket Weather-0.1.3-macos-x86_64.dmg`

Also write a short, beginner-friendly `0.1.3` feature update note. This is a
packaging and release slice only. Do not change trading strategy, pricing,
execution, reconciliation, desktop setup behavior, or database semantics.

No real BUY, SELL, cancellation, redemption, approval, or account mutation is
allowed.

## Mandatory Reading And Baseline

Read before planning or editing:

1. `AGENTS.md`
2. `docs/agent-worker-standards.md`
3. `docs/runbooks/macos-beginner-app.md`
4. `scripts/build_macos_app.sh`
5. `scripts/smoke_packaged_app.sh`
6. `packaging/macos/polymarket_weather.spec`
7. `tests/test_packaged_manifest.py`

Run and report:

```bash
git status --short
git log -5 --oneline
git rev-parse HEAD
git merge-base --is-ancestor d025223 HEAD
```

`d025223` must be an ancestor of the build. Preserve all unrelated user files
and generated artifacts. Never force-push.

## Reuse Map

Use the existing owners:

- `scripts/build_macos_app.sh`: the only app/DMG build implementation;
- `scripts/smoke_packaged_app.sh`: the only packaged-app lifecycle smoke;
- `packaging/macos/polymarket_weather.spec`: the only PyInstaller bundle spec;
- `docs/runbooks/macos-beginner-app.md`: install/upgrade/troubleshooting owner;
- `tests/test_packaged_manifest.py`: packaging contract tests;
- existing GitHub Actions infrastructure, if a remote Intel builder is needed.

Do not create a second launcher, desktop UI, build implementation, smoke script,
configuration system, updater, service, table, or trading path.

A new manual GitHub Actions packaging workflow is permitted only if needed to
obtain a real `x86_64` macOS builder. It must call the existing build and smoke
scripts rather than duplicate their logic.

## Required Work

### 1. Bump The Distribution Version To 0.1.3

Update the existing authoritative version sources consistently:

- `pyproject.toml`
- `src/polymarket_weather_arb/__init__.py`
- `packaging/macos/polymarket_weather.spec`
- default version in `scripts/build_macos_app.sh`
- `tests/test_packaged_manifest.py`
- current examples in `docs/runbooks/macos-beginner-app.md`

Do not rewrite historical worker reports or old release documents merely because
they mention `0.1.2`.

### 2. Make The Existing Build Architecture-Aware

Extend `scripts/build_macos_app.sh` minimally so every artifact carries the
architecture actually present in the packaged executable.

Required behavior:

- normalize Apple Silicon as `arm64` and Intel as `x86_64`;
- derive the artifact suffix from the actual build host/toolchain, not a display
  label supplied by the caller;
- reject unsupported or mismatched architecture requests with a clear error;
- retain current secret scans, AppKit import check, signing, and optional
  notarization behavior;
- avoid output collisions between architectures;
- produce a SHA-256 checksum next to each DMG;
- keep `DIST_DIR` and `APP_PATH` overrides usable by the existing smoke script.

Preferred DMG names:

```text
Polymarket Weather-0.1.3-macos-arm64.dmg
Polymarket Weather-0.1.3-macos-x86_64.dmg
```

Corresponding checksum files may use `.sha256`.

Do not claim a universal binary. Do not use `target_arch`, Rosetta, `arch
-x86_64`, or filename renaming as proof of Intel compatibility unless the full
Python interpreter, PyInstaller bootloader, native dependencies, packaged app,
and smoke all run as `x86_64`.

### 3. Obtain A Real Intel Build Environment

The current Apple Silicon machine cannot honestly validate an Intel PyInstaller
bundle by changing a flag. Use one of these, in priority order:

1. a real Intel Mac;
2. an available GitHub-hosted macOS `x86_64` runner;
3. another explicitly verified Intel macOS builder.

If using GitHub Actions:

- create or extend one manual `workflow_dispatch` packaging workflow;
- select currently available runner labels only after verifying their documented
  architecture;
- begin each job with `uname -m` and fail unless it equals the expected arch;
- use Python 3.12 and `uv` as the project already does;
- call `scripts/build_macos_app.sh` and `scripts/smoke_packaged_app.sh`;
- upload DMG and checksum as workflow artifacts;
- never expose `.env`, Keychain contents, API keys, Telegram secrets, databases,
  or logs;
- force paper/dry-run settings and disable Telegram for smoke;
- use no repository trading secrets for this workflow.

If a real Intel builder is unavailable, complete the architecture-aware source
work and ARM artifact, but report Intel as a blocker. Never mark the objective
complete and never manufacture an `x86_64` artifact.

### 4. Verify Each Architecture Independently

For both `arm64` and `x86_64`, run on the matching architecture:

```bash
file "<app>/Contents/MacOS/Polymarket Weather"
lipo -archs "<app>/Contents/MacOS/Polymarket Weather"
codesign --verify --deep --strict "<app>"
APP_PATH="<app>" ./scripts/smoke_packaged_app.sh
shasum -a 256 "<dmg>"
```

Also mount each DMG and verify it contains only:

- `Polymarket Weather.app`
- `Applications` symlink

The packaged smoke must exercise the packaged executable itself: first-run
setup, paper mode, `/app`, pause/lifecycle behavior, duplicate launch, quit, and
relaunch. It must not fall back to source-tree execution or forge completion
markers.

For each app, report:

- build host and `uname -m`;
- executable architecture from `file` and `lipo`;
- app and DMG size;
- bundle version;
- signing identity/status;
- notarization status;
- SHA-256;
- packaged-smoke result.

### 5. Write A Short Feature Update Note

Create `docs/releases/0.1.3.md`. Keep it short enough to send directly to a
beginner friend. Include:

- three-day D0/D1/D2 weather-market discovery with local-time handling;
- Google Weather and multiple numerical weather sources contributing to the
  quantitative estimate when configured;
- calibrated LLM weather vote, initially weight zero and gaining bounded weight
  only from resolved-event evidence;
- hardened pricing-chain behavior: stale, invalid, or mismatched LLM signals do
  not affect live pricing;
- two downloads: Apple Silicon (`arm64`) and Intel (`x86_64`), including how to
  check Apple menu -> About This Mac;
- upgrade steps: Quit old app, replace it from the new DMG, retain Application
  Support data;
- current limitation: ad-hoc/non-notarized build if that remains true.

Do not claim guaranteed profit, perfect forecasts, production certification, or
Intel support before the Intel smoke passes.

### 6. Update Existing Runbook And Tests

Update `docs/runbooks/macos-beginner-app.md` to explain:

- which DMG Apple Silicon users choose;
- which DMG Intel users choose;
- that the two builds are native and separate;
- the exact current filenames;
- unchanged data retention and Gatekeeper behavior.

Extend `tests/test_packaged_manifest.py` to catch:

- version drift;
- missing architecture suffixes;
- architecture assertion in the build path;
- both artifact names in the runbook/release note;
- accidental claims of universal compatibility.

Keep tests structural and deterministic. Do not test by executing real account or
network mutations.

## Source Quality Gates

Run with risk caps isolated from the user's `.env`:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

Current baseline before this packaging slice: `741 passed`. Explain any changed
count with the exact tests added or removed.

## Git And Artifact Discipline

- Commit source, workflow, tests, runbook, and release-note changes only.
- Do not commit `dist/`, `build/`, `.app`, `.dmg`, checksum artifacts, logs,
  databases, caches, credentials, or Keychain exports.
- One behavioral objective per commit where practical.
- Push normally to `origin/main`; never force-push.
- Do not interrupt or start the user's live daemon.
- Do not execute any real trading mutation.

## Acceptance Checklist

1. Current source includes commit `d025223`.
2. All authoritative versions are `0.1.3`.
3. Existing build/spec/smoke owners are reused; no parallel packaging engine.
4. ARM DMG is built and smoked on `arm64`.
5. Intel DMG is built and smoked on `x86_64`.
6. Artifact filenames include architecture and cannot overwrite each other.
7. `file`, `lipo`, bundle version, signing, size, and SHA-256 are reported for
   both builds.
8. Both DMGs mount and contain only the app and Applications shortcut.
9. Secret scans remain active and no user/runtime data is bundled.
10. `docs/releases/0.1.3.md` is concise, accurate, and names both downloads.
11. Ruff, full pytest, and diff-check pass.
12. No real trading/account mutation occurred.

## Required Worker Report

Return all of the following:

- objective completed, or the exact architecture/build blocker;
- existing components reused;
- new files introduced and why;
- files changed and duplicate code removed;
- exact version-consistency search result;
- source quality-gate outputs;
- ARM build host, commands, architecture evidence, smoke, size, signing,
  notarization, and SHA-256;
- Intel build host, commands, architecture evidence, smoke, size, signing,
  notarization, and SHA-256;
- DMG mount/contents verification for both;
- path to `docs/releases/0.1.3.md` and its final text;
- commit hashes and normal push result;
- final `git status --short`;
- explicit statement: real trading mutation executed or not executed;
- explicit statement: live daemon started/interrupted or not.
