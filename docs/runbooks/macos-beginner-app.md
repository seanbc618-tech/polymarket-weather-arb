# macOS Beginner App Runbook

Scheme A distribution: a native-looking macOS `.app` launcher that starts the
existing local dashboard (`/setup`, `/app`) without Terminal.

## What this is

- **Primary UI**: existing browser cockpit (`/app`) and setup flow (`/setup`)
- **Autonomous engine**: existing `AutopilotService`
- **BUY path**: existing `TradingService` only
- **SELL path**: existing `PositionExitService` only
- **Config model**: existing `Settings`
- **Persistence**: existing SQLite via `Repository`
- **Launcher role**: process lifecycle only (start server, open browser, status menu, quit)

This is **not** a second trading engine, settings system, or web app.

## Build (developer machine)

```bash
# From repository root, on macOS
APP_VERSION=0.1.4 ./scripts/build_macos_app.sh
```

Artifacts (current friend-share example: **0.1.4**):

```text
dist/Polymarket Weather-0.1.4-macos-arm64.dmg
dist/Polymarket Weather-0.1.4-macos-x86_64.dmg
```

Notes:

- Builds **natively for the host architecture only** (`uname -m`). An `arm64`
  build is not claimed to run on Intel, and an Intel build is not claimed for
  Apple Silicon, unless separately built and tested.
- Default build is **ad-hoc signed** for local / friend sharing. It is **not
  notarized**. Gatekeeper may require right-click → **Open** the first time.
- Optional Developer ID signing: `CODESIGN_IDENTITY="Developer ID Application: …" ./scripts/build_macos_app.sh`
- Optional notarization: also set `NOTARIZE_PROFILE` (notarytool keychain profile).
- **Never** commit signing credentials or notarization secrets.

Packaged smoke test (isolated HOME, no real trades):

```bash
./scripts/smoke_packaged_app.sh
```

## Install

1. Determine your Mac architecture: Click the **Apple Menu** -> **About This Mac**.
   - If it says **Apple M1/M2/M3** (or similar), you need the `arm64` version.
   - If it says **Intel**, you need the `x86_64` version.
2. Open the matching DMG file (e.g., `Polymarket Weather-0.1.4-macos-arm64.dmg` or `Polymarket Weather-0.1.4-macos-x86_64.dmg`).
3. Drag **Polymarket Weather.app** to **Applications**.
4. Eject the DMG.
5. Delete or replace any older DMG on disk; the app does **not** auto-update from
   a previous download.

## First launch

1. Double-click **Polymarket Weather**.
2. If Gatekeeper blocks an unsigned/ad-hoc build:
   - Right-click the app → **Open** → confirm, or
   - `xattr -dr com.apple.quarantine "/Applications/Polymarket Weather.app"`
3. The local server starts on `127.0.0.1` and the browser opens **`/setup`**.
4. Complete the setup wizard (Paper mode is the recommended default).
5. Finish setup → land on **`/app`**. Live trading remains **stopped**.

## Later launches

- Opens **`/app`** when setup was completed.
- Configuration and history persist under Application Support.
- If an instance is already healthy, a second double-click focuses it instead of
  starting a duplicate server.

## Status menu controls

- **Open Dashboard** — browser to `/setup` or `/app`
- **Pause Autopilot** — sets existing autopilot state `enabled=false`
- **View Logs** — opens the redacted rotating log
- **Quit** — cleanly stops the server/autopilot process

Closing a browser tab does **not** stop the backend. Use **Quit**.

## Data location

```text
~/Library/Application Support/Polymarket Weather/
  config.env          # non-secret only, mode 0600
  data/polymarket_weather.db
  logs/
  runtime/            # pid, port, lock, setup_complete
  diagnostics/
```

Secrets live in **macOS Keychain** under service `com.seanbc.polymarket-weather`:

- `POLYMARKET_PRIVATE_KEY`
- `GOOGLE_WEATHER_API_KEY`
- `LLM_API_KEY`
- `TELEGRAM_BOT_TOKEN`

The source checkout / CLI continue to use their existing defaults (repo `.env`)
unless `POLYMARKET_DESKTOP=1` / `POLYMARKET_DESKTOP_DATA_ROOT` is set.

## Upgrade

1. **Quit** the old app completely (status menu → **Quit**). Do not replace the
   bundle while it is still running.
2. Open the new DMG and replace `Polymarket Weather.app` (drag to Applications
   and confirm replace).
3. Application Support config, setup marker, database, and logs under
   `~/Library/Application Support/Polymarket Weather/` are **retained** across
   upgrades; only the app bundle is replaced.
4. Delete/replace the old DMG file on disk. There is **no auto-updater** — a new
   friend-share build is a new DMG you install manually.

No auto-update in v1.

## Uninstall

1. Quit the app.
2. Delete `/Applications/Polymarket Weather.app`.
3. Optional full wipe (destroys local history):

```bash
rm -rf "$HOME/Library/Application Support/Polymarket Weather"
# Optional: remove Keychain items for service com.seanbc.polymarket-weather
# via Keychain Access, or a future in-app reset with confirmation.
```

## Reset

- **Setup again**: open `/setup` from the app nav.
- **Diagnostics export**: use desktop diagnostics helper (versions, paths,
  readiness, redacted logs — never secret values).
- **Database**: deleting `data/polymarket_weather.db` loses local audit history.

## Logs

```text
~/Library/Application Support/Polymarket Weather/logs/autopilot.log
```

Logs use the existing redacting formatter (private keys, tokens, bearer headers).

## Troubleshooting

| Symptom | Recovery |
| --- | --- |
| App will not open (Gatekeeper) | Right-click → Open; or clear quarantine xattr |
| Browser does not open | Status menu → Open Dashboard; or visit `http://127.0.0.1:<port>/app` (port in `runtime/port`) |
| Port in use | Launcher selects a local fallback and writes `runtime/port` |
| Stale lock after crash | Relaunch; stale PID recovery clears lock files |
| Keychain unavailable | Secrets cannot be stored; configure on a standard macOS user session |
| Geoblock / compliance fail | Read-only; fix network/region/proxy under Advanced; config is kept |
| Live start blocked | Expected until credentials + reconciliation + risk gates + explicit /app start |

## Safety boundaries

- Setup completion **never** starts live Autopilot.
- No market orders; limit orders only via existing services.
- Sensitive setup POSTs require a per-process CSRF token and local Host/Origin.
- Localhost trust boundary remains: any local process/user can reach the bound
  port. This is not remote multi-user auth.
- Real BUY/SELL still require the existing live gates in `/app`.

## CLI coexistence

Advanced users keep:

```bash
uv run polymarket-weather autopilot start
uv run polymarket-weather doctor
```

CLI defaults are unchanged by the desktop app layout.
