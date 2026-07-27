# HK VPS production checklist

This checklist is for a small Hong Kong VPS running the canonical full-auto Autopilot service.

## 1. Server baseline

- Use a dedicated VPS in Hong Kong and a dedicated small Polymarket wallet.
- Install Python 3.12, git, systemd, and uv.
- Create a locked service user: `polymarket-weather`.
- Clone the repo to `/opt/polymarket-weather-arb` and run `uv sync --extra dev`.
- Keep SSH access key-based and avoid putting exchange secrets in shell history.

## 2. Environment file

- Copy `deploy/env/hk-live.example.env` to `/etc/polymarket-weather-arb.env`.
- Keep that file minimal. Copy individual keys from `.env.advanced.example` only
  when a documented operational need exists.
- Fill wallet credentials on the server only.
- Keep `TRADING_DISABLED=true` until all checks below pass.
- Keep `COMPLIANCE_ALLOWED_COUNTRIES=HK` for the Hong Kong deployment.
- Start with `MAX_ORDER_USDC=1`, `MAX_DAILY_USDC=5`, and `MAX_MARKET_USDC=2`.
- `LIVE_MARKET_IDS` is a Micro Live advanced override. Canonical Full Live scans all
  eligible weather candidates and does not require a global live override.

## 3. Offline rehearsal

Run the safe rehearsal first. It loads the bundled demo fixture, records a dry-run order intent, prints a risk report, and ends with offline live-readiness checks:

```bash
python scripts/rehearse_live_readiness.py
```

This script forces `TRADING_DISABLED=true` and `COMPLIANCE_CHECK_ENABLED=false` in its subprocess environment, so it cannot place live orders.

## 4. Read-only live readiness

After credentials are set, run read-only checks against the configured exchange APIs:

```bash
set -a
. /etc/polymarket-weather-arb.env
set +a
python scripts/rehearse_live_readiness.py --check-exchange
uv run polymarket-weather doctor --live
uv run polymarket-weather live-readiness
```

Do not continue if geoblock/compliance, credentials, reconciliation freshness, or exchange reads fail.

## 5. systemd services

- Install units from `deploy/systemd/`.
- Enable backups before daemon automation: `systemctl enable --now polymarket-weather-backup.timer`.
- The Autopilot unit uses `autopilot start --full-auto`, but cannot submit orders while `TRADING_DISABLED=true`.
- Keep the legacy `polymarket-weather-daemon.service` disabled; it is an advanced dry-run operator path, not a second production scheduler.
- Expose the dashboard only over SSH tunnel or a Cloudflare Access-protected
  Cloudflare Tunnel. The template binds to `127.0.0.1`; never expose port 8765.
- For Cloudflare access, set `DASHBOARD_PUBLIC_ORIGIN` to the exact HTTPS origin
  and follow `deploy/systemd/README.md`. Create the Access email allow policy
  before publishing the DNS route.

## 6. Live cutover gate

Only after dry-run operation is stable:

- Confirm backups are being created and can be opened with SQLite.
- Confirm `operator live-monitor`, `risk-report`, `operator open-orders`, and `operator positions --nonzero-only` look sane.
- Switch `TRADING_DISABLED=false` only for the small wallet smoke test.
- Confirm the production caps in `/etc/polymarket-weather-arb.env`, then set `TRADING_DISABLED=false` and restart only `polymarket-weather-autopilot.service`.

## 7. Rollback

If anything looks wrong:

- Set `TRADING_DISABLED=true` in `/etc/polymarket-weather-arb.env`.
- Restart the canonical service: `sudo systemctl restart polymarket-weather-autopilot.service`.
- Refresh orders: `uv run polymarket-weather operator refresh-open-orders`.
- Cancel outstanding orders with `operator cancel-order <exchange_order_id>`.
- Preserve the database and latest backup before making code or config changes.
