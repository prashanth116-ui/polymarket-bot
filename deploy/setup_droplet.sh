#!/usr/bin/env bash
# Initial setup for a fresh Ubuntu droplet.
# Run once: ssh root@DROPLET_IP 'bash -s' < deploy/setup_droplet.sh

set -euo pipefail

APP_DIR="/opt/polymarket-bot"
APP_USER="polybot"

echo "=== Polymarket Bot — Droplet Setup ==="

# System packages
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git tmux jq ufw curl

# Firewall — SSH only
ufw allow OpenSSH
ufw --force enable

# Create app user (no login shell)
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -s /usr/sbin/nologin -m -d "$APP_DIR" "$APP_USER"
    echo "Created user $APP_USER"
fi

# Clone repo
if [ ! -d "$APP_DIR/.git" ]; then
    git clone https://github.com/YOUR_ORG/polymarket-bot.git "$APP_DIR"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
else
    echo "Repo already cloned at $APP_DIR"
fi

# Python virtualenv
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Create log directory
mkdir -p "$APP_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/logs"

# Create .env placeholder
if [ ! -f "$APP_DIR/.env" ]; then
    cat > "$APP_DIR/.env" <<'ENVEOF'
# Polymarket Bot Environment
POLY_PRIVATE_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ANTHROPIC_API_KEY=
ENVEOF
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "Created .env at $APP_DIR/.env — fill in credentials"
fi

# Install systemd service
cp "$APP_DIR/deploy/polymarket-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable polymarket-bot

# Log rotation
cat > /etc/logrotate.d/polymarket-bot <<'LOGEOF'
/opt/polymarket-bot/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
LOGEOF

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Edit $APP_DIR/.env with your credentials"
echo "  2. Start: systemctl start polymarket-bot"
echo "  3. Logs:  journalctl -u polymarket-bot -f"
