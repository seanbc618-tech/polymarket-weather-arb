# Grok Worker Task: macOS Beginner App Distribution

## Objective

Turn the existing local Polymarket Weather application into a beginner-friendly
macOS product that can be installed and launched without Terminal, while keeping
the current `/app`, `/setup`, Autopilot, execution services, database, and CLI as
the only product implementation.

This is **Scheme A**:

- a native-looking macOS `.app` launcher;
- the existing browser UI remains the primary interface;
- first launch opens an expanded `/setup` flow;
- later launches open `/app`;
- secrets are stored in macOS Keychain;
- a local build produces an installable `.dmg`;
- the CLI remains supported for advanced users.

The application is trading software. Ease of use must not silently arm live
trading, weaken execution invariants, expose secrets, or create a second trading
system.

## Required Starting Point

1. Start from the latest `origin/main`, containing at least commit `8fb9ac7`.
2. Read `AGENTS.md` and `docs/agent-worker-standards.md` before planning or editing.
3. Run `git status --short` and preserve unrelated user work.
4. Inspect the current implementations and callers of:
   - `Settings` and `load_settings()`;
   - `serve_dashboard()`;
   - `/setup`, `/setup/init-db`, and `/app`;
   - Autopilot start/stop and mode persistence;
   - readiness, reconciliation, and circuit-breaker checks;
   - existing dashboard i18n and CSS/component helpers.
5. Write a short reuse map in the worker report before listing new files.

## Product And Ownership Constraints

The following are non-negotiable:

- `/app` remains the only beginner-facing operating cockpit.
- Extend the existing `/setup`; do not create `/wizard`, a second web app, or a
  parallel settings UI.
- `AutopilotService` remains the autonomous engine.
- `TradingService` remains the only normal BUY path.
- `PositionExitService` remains the only SELL path.
- `ReconciliationService` remains the exchange-state source of truth.
- `Settings` remains the runtime configuration model.
- `Repository` and the existing SQLite database remain the persistence boundary.
- The desktop launcher may own process lifecycle only. It must not contain
  strategy, pricing, risk, reconciliation, BUY, or SELL logic.
- Do not add another scheduler, trading engine, reconciliation path, strategy
  engine, settings model, or database.
- No real BUY, SELL, cancel, approval, deposit, allowance, or other exchange
  mutation may run during development or acceptance.

## User Experience

### Installation And Launch

A beginner should be able to:

1. install `Polymarket Weather.app` from a DMG;
2. double-click the app;
3. have the local server start automatically;
4. have the default browser open to `/setup` on first launch;
5. complete setup without using Terminal;
6. land on `/app`;
7. quit and relaunch without losing configuration or history.

On an already configured installation, launch should open `/app` directly.

### Required Lifecycle Controls

The packaged application must provide beginner-readable controls for:

- Open Dashboard;
- Pause Autopilot;
- View Logs;
- Quit.

Use the smallest proven macOS status-bar/menu mechanism that packages reliably.
The status item is a lifecycle shell only. Do not rebuild the cockpit as native
Swift, Electron, Tauri, or another frontend.

Closing a browser tab must not be represented as stopping the backend. `Quit`
must cleanly stop the server/autopilot process. Relaunching while an instance is
already healthy must focus/open the existing UI rather than start a duplicate.

If the proposed menu dependency cannot package reliably with the selected build
tool, stop and report the packaging evidence. Do not switch frameworks without
review.

## Desktop Data Layout

The packaged app must not write mutable runtime data into the app bundle. Use:

```text
~/Library/Application Support/Polymarket Weather/
  config.env
  data/polymarket_weather.db
  logs/
  runtime/
```

Requirements:

- create directories lazily with user-only permissions where practical;
- write non-secret config atomically;
- set `config.env` mode to `0600`;
- keep the database, logs, PID/lock, and selected port outside the app bundle;
- do not bundle the repository `.env`, any database, logs, caches, credentials,
  API keys, wallet material, or local user fixtures;
- preserve Application Support data across application upgrades;
- provide a redacted diagnostics export containing versions, readiness states,
  paths, and recent sanitized logs, but never secret values.

The normal source checkout and CLI must continue to use their existing defaults
unless an explicit desktop config path is supplied. Do not make all CLI users
silently migrate to `~/Library/Application Support`.

## Configuration Loading

Implement one shared settings-loading path with explicit desktop support. The
effective precedence for the packaged app must be:

1. explicit process environment;
2. secrets retrieved from macOS Keychain;
3. non-secret values from the desktop `config.env`;
4. existing `Settings` defaults.

Do not maintain a second settings dataclass or duplicate field validation.

Configuration writes must validate through the existing `Settings` model before
replacing the active file. A malformed write must leave the prior valid config
untouched and return a clear UI error.

## macOS Keychain

Add one narrow credential adapter, justified because the existing project has no
secure desktop secret store. Prefer `/usr/bin/security` invoked through an argv
list without `shell=True`. Use a stable service namespace such as:

```text
com.seanbc.polymarket-weather
```

At minimum, treat these as secrets when present:

- `POLYMARKET_PRIVATE_KEY`;
- `POLYMARKET_RELAYER_API_KEY`;
- `WEATHER_API_KEY`;
- `LLM_API_KEY`;
- `TELEGRAM_BOT_TOKEN`.

Search current `Settings` for any additional `secret`, `token`, `password`, or
private credential fields and include them consistently.

Security requirements:

- never write secret values to `config.env`;
- never return an existing secret value to HTML;
- render only `configured` / `not configured` status;
- use password input fields for new values;
- blank input means keep the existing secret, not erase it;
- expose deletion as a separate explicit action;
- never place secrets in URLs, query parameters, flash messages, exceptions,
  logs, diagnostic exports, test snapshots, or command output;
- redact subprocess failures before surfacing them;
- unit-test every Keychain command with subprocess mocked;
- a reset operation may delete only this app's named Keychain items and only
  after explicit confirmation.

## Expand The Existing `/setup`

Refactor the current status-only setup page into a resumable first-run flow. Keep
the route `/setup` and keep setup reachable later from `/app`.

Use the existing dashboard rendering, i18n, forms, styles, readiness services,
and configuration model. Do not create a separate UI framework.

### Step 1: Local App Health

Show and validate:

- application version;
- Application Support path;
- database path and schema initialization state;
- writable data/log directories;
- whether this is first run or an existing installation.

Database initialization must reuse the existing schema initialization path.

### Step 2: Operating Mode

Offer these user-facing choices:

- Observe;
- Paper Auto Trading, recommended default;
- Micro Live;
- Full Live.

Map them onto the existing Autopilot modes and settings. Do not add a parallel
persistent mode. Explain consequences in plain language, but do not imply profit.

Completing setup must never start live trading automatically. Even when Micro
Live or Full Live is selected, the system must remain stopped/paused until the
user uses the existing explicit start/confirmation path in `/app`.

### Step 3: Wallet And Polymarket

Allow configuration of the existing wallet path:

- private key entered as a password field and saved to Keychain;
- derive and display the signer address, never the key;
- configure or derive `POLYMARKET_FUNDER` using current supported behavior;
- keep signature type and advanced auth fields under Advanced Settings;
- validate credentials locally where possible;
- run read-only balance/auth checks through existing adapters/readiness logic;
- clearly distinguish balance authorization from order-signing authorization.

Do not create developer/relayer keys, approvals, or live orders from setup.

### Step 4: Connectivity And Compliance

Provide read-only tests for:

- Gamma market reads;
- CLOB reads;
- weather provider reads;
- geoblock/compliance status;
- optional HTTP proxy settings.

Proxy configuration belongs under Advanced Settings. A failed test must show a
specific recovery message and must not erase configuration.

### Step 5: Risk Presets

Reuse the existing risk settings and hard caps. Provide clear presets, including:

- Paper default: no live mutation;
- Starter Live: `MAX_ORDER_USDC=1`, with conservative daily/market limits;
- Cautious Live: `MAX_ORDER_USDC=2`, with explicit daily/market limits;
- Custom: advanced users only, still constrained by existing hard caps.

Display every value before saving. Include automatic-exit limits when live mode
is selected. Do not silently set `AUTO_EXIT_MAX_POSITION_USDC` below the selected
order cap, because that would make newly entered positions ineligible for exit.

Do not invent new risk gates. This step only configures existing controls.

### Step 6: Weather And Notifications

Configure existing providers and optional Telegram:

- weather provider with `Auto`/current supported providers;
- weather API key only when required, stored in Keychain;
- Telegram bot token in Keychain;
- chat ID as non-secret configuration;
- a read-only/test notification action that does not start Autopilot.

Reuse existing Telegram formatting and notifier code. Do not create a second
notification subsystem.

### Step 7: Readiness Review

Show one final checklist using existing checks wherever available:

- database initialized;
- weather provider configured and reachable;
- Polymarket market reads;
- compliance/geoblock result;
- reconciliation/read status;
- balance authorization path;
- order-signing authorization path;
- circuit breaker status;
- `TRADING_DISABLED` and selected Autopilot mode;
- live credentials status;
- Telegram status.

The review is read-only. It must not prove signing by placing an order. Label
checks accurately as local validation, read validation, or not yet verified by a
real trade.

Finishing setup persists configuration and redirects to `/app`. Live remains
stopped.

## Sensitive Local POST Protection

Localhost is not sufficient protection against browser-originated requests. Add
a narrow anti-CSRF mechanism for new sensitive setup writes, reset, quit, and
credential actions:

- random per-process token;
- token included in forms and verified with constant-time comparison;
- reject missing/invalid tokens;
- verify expected local `Host`/origin where available;
- do not put the token in URLs or logs.

Do not turn this slice into a dashboard-wide authentication rewrite. Document
the remaining localhost trust boundary.

## Desktop Launcher

The launcher is a small composition root, not a service layer. It must:

1. locate/create the desktop data root;
2. load desktop settings through the shared settings loader;
3. acquire a single-instance lock;
4. detect an already-running healthy instance and open it instead of duplicating
   the process;
5. bind only to `127.0.0.1`;
6. use the configured port or select an available local fallback;
7. persist the active port/PID information under `runtime/`;
8. start the existing dashboard server and existing Autopilot lifecycle;
9. open `/setup` or `/app` as appropriate;
10. handle SIGTERM and Quit cleanly;
11. write through the existing redacted rotating log configuration;
12. surface startup failure in a macOS-readable alert and log file.

The launcher must not run live Autopilot just because the app starts. It should
restore safe persisted state according to existing behavior, and tests must prove
that a first launch remains non-live and stopped.

## Packaging And Distribution

Use PyInstaller unless a repository-supported packager already exists. Add only
the minimum build dependency and configuration required.

Deliver:

- a PyInstaller spec or equivalent checked-in build definition;
- `scripts/build_macos_app.sh`;
- app metadata and icon wiring;
- a local DMG build step using macOS tooling;
- a concise operator runbook covering build, install, uninstall, upgrade, data
  location, logs, reset, Gatekeeper behavior, and troubleshooting.

Expected local artifacts:

```text
dist/Polymarket Weather.app
dist/Polymarket Weather-<version>.dmg
```

Packaging requirements:

- work on a clean macOS account without Python, `uv`, or repository access;
- include runtime dependencies needed by the app;
- exclude source `.env`, runtime DBs, logs, caches, test fixtures not required by
  production, and all credentials;
- build natively for the host architecture and document that Intel/Apple Silicon
  may require separate or universal builds;
- support an unsigned/ad-hoc local build with honest Gatekeeper instructions;
- optionally support Developer ID signing/notarization through environment
  variables, but never require or embed signing credentials;
- do not implement auto-update in v1;
- upgrades must preserve Application Support data;
- do not publish a GitHub Release or change repository visibility in this task.

## Existing UI And Open Design

The Open Design visual work is already fused into `/app`. Preserve it. Apply the
existing visual language to `/setup`, using current tokens/components. Do not
re-import the prototype directory, commit generated design source, or introduce a
second CSS system.

The setup UI must remain usable at common desktop widths and mobile-sized browser
windows. Secret values must never appear in rendered HTML.

## Tests And Acceptance

### Unit And Integration Tests

Add focused tests for:

- desktop data-path resolution without touching the real user home;
- config atomic write, `0600` permissions, validation failure, and precedence;
- Keychain get/set/delete argv with subprocess fully mocked;
- secret absence from config files, HTML, logs, flashes, errors, and diagnostics;
- setup first-run progression and resume behavior;
- mode and risk preset mapping to existing settings;
- setup completion never starts live trading;
- readiness checks with fake adapters and zero exchange mutations;
- CSRF rejection and successful local form posts;
- single-instance behavior and stale-lock recovery;
- configured-port conflict and local fallback;
- browser routing to `/setup` versus `/app`;
- Pause and Quit lifecycle behavior;
- corrupted config, unavailable Keychain, offline network, and unwritable path;
- upgrade/relaunch retaining config and database;
- packaged file manifest excluding `.env`, DB, logs, private key, and tokens.

### Packaged-App Smoke Test

Use an isolated temporary `HOME`/Application Support location and a mocked or
offline-safe mode. Verify:

1. the `.app` launches without repository Python or `uv`;
2. `/setup` opens on first launch;
3. Paper mode can be configured;
4. `/app` loads;
5. duplicate launch does not create a second server;
6. Quit stops the process;
7. relaunch opens `/app` with retained settings;
8. no real exchange mutation occurs.

If a full clean-machine test is unavailable, report exactly which boundary was
not verified. Do not claim beginner-ready distribution based only on unit tests.

### Required Gates

Run and report exact results:

```bash
uv run ruff check src/ tests/
MAX_ORDER_USDC=1 MAX_DAILY_USDC=5 MAX_MARKET_USDC=2 uv run pytest -q
git diff --check
git status --short
```

Also run the build script and packaged smoke check. Record artifact paths, sizes,
architecture, signing status, and whether macOS launched the built app.

## Commit Plan

Keep behavior reviewable. Use normal pushes only; never force-push.

1. **Desktop config and Keychain**
   - explicit desktop paths;
   - shared settings loading;
   - secure credential adapter;
   - tests.
2. **Existing `/setup` first-run flow**
   - setup forms and validation;
   - readiness review;
   - CSRF for sensitive desktop actions;
   - tests.
3. **Desktop launcher lifecycle**
   - single instance;
   - status menu;
   - browser opening;
   - pause/quit/log controls;
   - tests.
4. **macOS packaging and runbook**
   - PyInstaller/build definition;
   - DMG script;
   - packaged smoke test;
   - documentation.

Do not mix unrelated strategy, entry, exit, PnL, parser, or market-discovery
changes into these commits.

## Stop Conditions

Stop and request review before proceeding if:

- implementation requires a second web app or frontend framework;
- a second settings model/database/service layer appears necessary;
- an existing execution or reconciliation path must be duplicated;
- a new persistent database table appears necessary;
- setup would need to execute a live order or wallet approval;
- secrets cannot be kept out of files/logs/HTML;
- packaging requires checking credentials or signing identities into the repo;
- the packaged app cannot preserve the existing CLI behavior;
- more than one new production service class is proposed;
- a framework switch is proposed;
- a migration could damage existing live audit history.

## Required Worker Report

The final report must follow `docs/agent-worker-standards.md` and include:

- objective completed or incomplete;
- reuse map and existing components reused;
- every new file/class/dependency/setting with justification;
- duplicate or superseded code removed;
- production behavior changed;
- security model and known limitations;
- exact test, lint, build, and packaged smoke results;
- artifact paths, sizes, architecture, and signing/notarization status;
- clean-install boundary actually tested;
- commit hashes and remaining working-tree changes;
- unresolved risks and manual steps;
- explicit statement: real trading mutation executed or not executed;
- explicit statement: normal push or force-push.

Do not report completion merely because the unit test suite passes. The deliverable
is complete only when a fresh macOS user can install, launch, configure Paper
mode, quit, and relaunch without Terminal, with no secret leakage and no real
trade.
