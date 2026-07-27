#!/usr/bin/env bash
set -euo pipefail

# One-shot HK VPS bootstrap for Autopilot production.
# Run on the VPS as root after cloning to /opt/polymarket-weather-arb.

INSTALL_DIR="/opt/polymarket-weather-arb"
ENV_FILE="/etc/polymarket-weather-arb.env"
SERVICE_USER="polymarket-weather"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Error: run as root on the HK VPS"
  exit 1
fi

if [ ! -d "$INSTALL_DIR/.git" ] && [ ! -f "$INSTALL_DIR/pyproject.toml" ]; then
  echo "Error: repository not found at $INSTALL_DIR"
  exit 1
fi

cd "$INSTALL_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="/root/.local/bin:$PATH"
fi

echo "=== Sync dependencies ==="
uv sync --extra dev

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/backups"

if [ ! -f "$ENV_FILE" ]; then
  echo "=== Creating $ENV_FILE from template ==="
  cp "$INSTALL_DIR/deploy/env/hk-live.example.env" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Edit secrets before enabling live trading:"
  echo "  sudo nano $ENV_FILE"
else
  echo "=== Using existing $ENV_FILE ==="
fi

echo "=== Initialize database ==="
sudo -u "$SERVICE_USER" bash -lc "set -a && source $ENV_FILE && set +a && $INSTALL_DIR/.venv/bin/polymarket-weather init-db"

echo "=== Install systemd units ==="
bash "$INSTALL_DIR/scripts/install_systemd_units.sh"

echo "=== Enable autopilot + backups ==="
systemctl enable --now polymarket-weather-backup.timer
systemctl enable --now polymarket-weather-autopilot.service

echo ""
echo "=== HK VPS deploy complete ==="
echo "Autopilot dashboard (on VPS): http://127.0.0.1:8765/app?lang=zh"
echo "From your laptop, open a tunnel:"
echo "  uv run polymarket-weather autopilot tunnel --host <vps-ip> --user root"
echo ""
echo "Before live trading:"
echo "  1. Fill POLYMARKET_* credentials in $ENV_FILE"
echo "  2. Set LLM_* if using the advisor"
echo "  3. Keep TRADING_DISABLED=true until rehearsal passes"
echo "  4. sudo -u $SERVICE_USER bash -lc 'cd $INSTALL_DIR && set -a && . $ENV_FILE && set +a && python scripts/rehearse_live_readiness.py --check-exchange'"
echo "  5. Set TRADING_DISABLED=false and restart: systemctl restart polymarket-weather-autopilot.service"