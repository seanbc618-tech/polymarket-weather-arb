#!/usr/bin/env bash
# Packaged-app smoke test using an isolated HOME / Application Support tree.
# Never performs real BUY/SELL/cancel/approval.
#
# Honesty rules (no false positives):
# - setup POST failures fail the smoke
# - never hand-write setup_complete
# - duplicate launch must exit promptly
# - Quit uses /desktop/quit (CSRF), not only external kill
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${DIST_DIR:-$ROOT/dist}"
APP_NAME="Polymarket Weather"
APP_PATH="${APP_PATH:-$DIST_DIR/${APP_NAME}.app}"
BINARY="$APP_PATH/Contents/MacOS/${APP_NAME}"

if [[ ! -x "$BINARY" && ! -f "$BINARY" ]]; then
  echo "ERROR: packaged binary not found at $BINARY" >&2
  echo "Run scripts/build_macos_app.sh first." >&2
  exit 1
fi

SMOKE_ROOT="${SMOKE_ROOT:-$(mktemp -d -t pwa-smoke)}"
export HOME="$SMOKE_ROOT/home"
mkdir -p "$HOME"
export POLYMARKET_DESKTOP=1
export POLYMARKET_DESKTOP_DATA_ROOT="$HOME/Library/Application Support/Polymarket Weather"
export POLYMARKET_DESKTOP_PORT="${POLYMARKET_DESKTOP_PORT:-18765}"
export MAX_ORDER_USDC=1
export MAX_DAILY_USDC=5
export MAX_MARKET_USDC=2
export TRADING_DISABLED=true
export AUTOPILOT_MODE=dry_run

echo "==> Smoke HOME=$HOME"
echo "==> Data root=$POLYMARKET_DESKTOP_DATA_ROOT"

extract_csrf() {
  python3 - <<'PY' "$1"
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'name="csrf_token" value="([^"]+)"', html)
if not m:
    raise SystemExit("csrf missing")
print(m.group(1))
PY
}

post_setup() {
  local path="$1"
  shift
  local headers="$SMOKE_ROOT/post-$(echo "$path" | tr '/' '_').headers"
  local body="$SMOKE_ROOT/post-$(echo "$path" | tr '/' '_').body"
  local code
  code="$(
    curl -sS -o "$body" -D "$headers" -w "%{http_code}" \
      -X POST "http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}${path}" \
      -H "Host: 127.0.0.1:${POLYMARKET_DESKTOP_PORT}" \
      -H "Origin: http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}" \
      "$@"
  )"
  if [[ "$code" != "303" && "$code" != "302" && "$code" != "200" ]]; then
    echo "ERROR: POST ${path} failed HTTP ${code}" >&2
    cat "$headers" >&2 || true
    cat "$body" >&2 || true
    exit 1
  fi
  if grep -qi "level=error" "$headers" 2>/dev/null; then
    echo "ERROR: POST ${path} redirected with error flash" >&2
    cat "$headers" >&2
    exit 1
  fi
  echo "$headers"
}

wait_http() {
  local path="$1"
  local out="$2"
  local ok=0
  for _ in $(seq 1 90); do
    if curl -fsS "http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}${path}" -o "$out"; then
      ok=1
      break
    fi
    PORT_FILE="$POLYMARKET_DESKTOP_DATA_ROOT/runtime/port"
    if [[ -f "$PORT_FILE" ]]; then
      ACTUAL_PORT="$(tr -d '[:space:]' <"$PORT_FILE" || true)"
      if [[ -n "${ACTUAL_PORT:-}" ]] && curl -fsS "http://127.0.0.1:${ACTUAL_PORT}${path}" -o "$out"; then
        POLYMARKET_DESKTOP_PORT="$ACTUAL_PORT"
        ok=1
        break
      fi
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "ERROR: packaged app exited early" >&2
      cat "$LOG" >&2 || true
      exit 1
    fi
    sleep 0.5
  done
  if [[ "$ok" != "1" ]]; then
    echo "ERROR: ${path} never became healthy" >&2
    cat "$LOG" >&2 || true
    exit 1
  fi
}

LOG="$SMOKE_ROOT/launcher.out"
# Prefer lifecycle controls when available; smoke still exercises HTTP quit.
# --no-status-menu keeps CI headless; status menu is verified at import/build time.
(
  cd "$SMOKE_ROOT"
  "$BINARY" --no-browser --no-status-menu --port "$POLYMARKET_DESKTOP_PORT"
) >"$LOG" 2>&1 &
PID=$!

cleanup() {
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    sleep 1
    kill -9 "$PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "==> Waiting for /setup"
wait_http "/setup?lang=en" "$SMOKE_ROOT/setup.html"
echo "==> /setup opened on first launch (port=${POLYMARKET_DESKTOP_PORT})"
grep -q "csrf_token" "$SMOKE_ROOT/setup.html"
grep -q "Setup\|首次设置\|Local health\|本地健康" "$SMOKE_ROOT/setup.html"
if grep -E '0x[a-fA-F0-9]{64}|[0-9]{6,12}:[A-Za-z0-9_-]{20,}' "$SMOKE_ROOT/setup.html"; then
  echo "ERROR: secret-like material in /setup HTML" >&2
  exit 1
fi

CSRF="$(extract_csrf "$SMOKE_ROOT/setup.html")"

echo "==> Setup health"
post_setup "/setup/health" \
  --data-urlencode "csrf_token=${CSRF}" \
  --data-urlencode "lang=en" >/dev/null

echo "==> Setup paper mode"
post_setup "/setup/mode" \
  --data-urlencode "csrf_token=${CSRF}" \
  --data-urlencode "lang=en" \
  --data-urlencode "app_mode=paper" >/dev/null

# Wallet step may be skipped for paper-only smoke (no private key required).
echo "==> Setup risk paper preset"
post_setup "/setup/risk" \
  --data-urlencode "csrf_token=${CSRF}" \
  --data-urlencode "lang=en" \
  --data-urlencode "risk_preset=paper" >/dev/null

echo "==> Setup weather"
post_setup "/setup/weather" \
  --data-urlencode "csrf_token=${CSRF}" \
  --data-urlencode "lang=en" \
  --data-urlencode "weather_provider=open-meteo" >/dev/null

echo "==> Setup complete"
post_setup "/setup/complete" \
  --data-urlencode "csrf_token=${CSRF}" \
  --data-urlencode "lang=en" >/dev/null

# Must be created by setup/complete, not this script.
if [[ ! -f "$POLYMARKET_DESKTOP_DATA_ROOT/runtime/setup_complete" ]]; then
  echo "ERROR: setup_complete marker missing after /setup/complete" >&2
  exit 1
fi

curl -fsS "http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}/app?lang=en" -o "$SMOKE_ROOT/app.html"
grep -q "Polymarket Weather\|Autopilot\|自动" "$SMOKE_ROOT/app.html"
echo "==> /app loads after real setup completion"

# Resume: mode step should keep paper selected.
curl -fsS "http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}/setup?step=mode&lang=en" -o "$SMOKE_ROOT/mode.html"
grep -q 'value="paper" checked\|value="paper"  checked' "$SMOKE_ROOT/mode.html" \
  || grep -q 'name="app_mode" value="paper" checked' "$SMOKE_ROOT/mode.html"
echo "==> Setup mode resume keeps paper selected"

echo "==> Duplicate launch must exit"
DUPE_LOG="$SMOKE_ROOT/dupe.out"
(
  "$BINARY" --no-browser --no-status-menu --port "$POLYMARKET_DESKTOP_PORT"
) >"$DUPE_LOG" 2>&1 &
DUPE_PID=$!
# Allow focus-and-exit path a few seconds.
for _ in $(seq 1 20); do
  if ! kill -0 "$DUPE_PID" 2>/dev/null; then
    break
  fi
  sleep 0.25
done
if kill -0 "$DUPE_PID" 2>/dev/null; then
  echo "ERROR: duplicate process still running (single-instance failed)" >&2
  kill "$DUPE_PID" 2>/dev/null || true
  cat "$DUPE_LOG" >&2 || true
  exit 1
fi
curl -fsS "http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}/app?lang=en" -o /dev/null
echo "==> Duplicate exited; primary still healthy"

echo "==> Quit via /desktop/quit"
# Refresh CSRF from a live page (process token is stable, but keep honest).
curl -fsS "http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}/setup?lang=en" -o "$SMOKE_ROOT/setup2.html"
CSRF2="$(extract_csrf "$SMOKE_ROOT/setup2.html")"
# Quit may close the server mid-response; accept connection drop after request.
curl -sS -o /dev/null -D "$SMOKE_ROOT/quit.headers" \
  -X POST "http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}/desktop/quit" \
  -H "Host: 127.0.0.1:${POLYMARKET_DESKTOP_PORT}" \
  -H "Origin: http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}" \
  --data-urlencode "csrf_token=${CSRF2}" \
  --data-urlencode "lang=en" || true

# Wait for process exit from graceful quit (not external kill first).
for _ in $(seq 1 30); do
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  sleep 0.2
done
if kill -0 "$PID" 2>/dev/null; then
  echo "ERROR: process still alive after /desktop/quit" >&2
  exit 1
fi
if curl -fsS --max-time 1 "http://127.0.0.1:${POLYMARKET_DESKTOP_PORT}/app?lang=en" -o /dev/null 2>/dev/null; then
  echo "ERROR: server still responding after quit" >&2
  exit 1
fi
echo "==> Quit via /desktop/quit stopped the process"

# Relaunch with retained Application Support.
(
  "$BINARY" --no-browser --no-status-menu --port "$POLYMARKET_DESKTOP_PORT"
) >"$SMOKE_ROOT/relaunch.out" 2>&1 &
PID=$!
wait_http "/app?lang=en" "$SMOKE_ROOT/app2.html"
echo "==> Relaunch opens /app with retained data"

DB="$POLYMARKET_DESKTOP_DATA_ROOT/data/polymarket_weather.db"
if [[ -f "$DB" ]] && command -v sqlite3 >/dev/null 2>&1; then
  LIVE_ORDERS="$(sqlite3 "$DB" "SELECT COUNT(*) FROM order_intents WHERE dry_run=0;" 2>/dev/null || echo 0)"
  if [[ "${LIVE_ORDERS}" != "0" && "${LIVE_ORDERS}" != "" ]]; then
    echo "ERROR: unexpected live order intents during smoke: $LIVE_ORDERS" >&2
    exit 1
  fi
fi

echo "==> Packaged-app smoke PASSED"
echo "SMOKE_ROOT=$SMOKE_ROOT"
echo "No real exchange mutation executed."
