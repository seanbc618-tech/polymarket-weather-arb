#!/usr/bin/env bash
set -euo pipefail

# Install systemd units for Polymarket Weather Arb
# This script should be run as root on the target VPS

INSTALL_DIR="/opt/polymarket-weather-arb"
ENV_FILE="/etc/polymarket-weather-arb.env"
SERVICE_USER="polymarket-weather"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== Polymarket Weather Arb systemd installer ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root"
    exit 1
fi

# Check if install directory exists
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Error: Install directory $INSTALL_DIR does not exist"
    echo "Please clone the repository to $INSTALL_DIR first"
    exit 1
fi

# Check if virtualenv exists
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    echo "Error: Virtual environment not found at $INSTALL_DIR/.venv"
    echo "Please run 'uv sync' in $INSTALL_DIR first"
    exit 1
fi

# Create service user if it doesn't exist
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Creating service user: $SERVICE_USER"
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
else
    echo "Service user $SERVICE_USER already exists"
fi

# Create backups directory
echo "Creating backups directory..."
mkdir -p "$INSTALL_DIR/backups"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/backups"

# Create data directory if it doesn't exist
echo "Creating data directory..."
mkdir -p "$INSTALL_DIR/data"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data"

# Check if environment file exists
if [ ! -f "$ENV_FILE" ]; then
    echo "Warning: Environment file not found at $ENV_FILE"
    echo "Please create it from deploy/env/hk-live.example.env:"
    echo "  sudo cp deploy/env/hk-live.example.env $ENV_FILE"
    echo "  sudo nano $ENV_FILE"
    echo ""
    echo "Required variables:"
    echo "  DATABASE_PATH=/opt/polymarket-weather-arb/data/polymarket_weather.db"
    echo "  TRADING_DISABLED=true"
    echo ""
else
    echo "Environment file found at $ENV_FILE"
fi

# Copy systemd units
echo "Installing systemd units..."
cp "$INSTALL_DIR/deploy/systemd/polymarket-weather-"*.service "$SYSTEMD_DIR/"
cp "$INSTALL_DIR/deploy/systemd/polymarket-weather-"*.timer "$SYSTEMD_DIR/"

# Reload systemd
echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enable and start backup timer
echo "Enabling backup timer..."
systemctl enable --now polymarket-weather-backup.timer

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "1. Review and edit $ENV_FILE"
echo "2. Initialize the database:"
echo "   sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/polymarket-weather init-db"
echo "3. Start autopilot (recommended):"
echo "   sudo systemctl enable --now polymarket-weather-autopilot.service"
echo "4. Legacy operator daemon (optional):"
echo "   sudo systemctl enable --now polymarket-weather-daemon.service"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status polymarket-weather-autopilot"
echo "  sudo journalctl -u polymarket-weather-autopilot -f"
echo "  sudo systemctl status polymarket-weather-backup.timer"
echo ""
echo "IMPORTANT: Keep TRADING_DISABLED=true until you've reviewed:"
echo "  - polymarket-weather doctor --live"
echo "  - polymarket-weather live-readiness"
