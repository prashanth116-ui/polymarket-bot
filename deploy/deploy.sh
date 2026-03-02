#!/usr/bin/env bash
# Deploy code updates to the droplet.
# Usage: ./deploy/deploy.sh [DROPLET_IP]

set -euo pipefail

DROPLET_IP="${1:-}"
APP_DIR="/opt/polymarket-bot"

if [ -z "$DROPLET_IP" ]; then
    # Try to read from settings
    if command -v python3 &>/dev/null; then
        DROPLET_IP=$(python3 -c "
import yaml
with open('config/settings.yaml') as f:
    s = yaml.safe_load(f)
print(s.get('deploy', {}).get('droplet_ip', ''))
" 2>/dev/null || true)
    fi
fi

if [ -z "$DROPLET_IP" ]; then
    echo "Usage: ./deploy/deploy.sh DROPLET_IP"
    echo "Or set deploy.droplet_ip in config/settings.yaml"
    exit 1
fi

echo "=== Deploying to $DROPLET_IP ==="

# Pre-deploy: check current status
echo "Pre-deploy status:"
ssh "root@$DROPLET_IP" "
    systemctl is-active polymarket-bot || echo 'Service not running'
    cd $APP_DIR && git log --oneline -1 2>/dev/null || echo 'No git history'
"

# Pull latest code
echo ""
echo "Pulling latest code..."
ssh "root@$DROPLET_IP" "
    cd $APP_DIR &&
    git fetch origin &&
    git reset --hard origin/main &&
    chown -R polybot:polybot $APP_DIR
"

# Install dependencies
echo "Installing dependencies..."
ssh "root@$DROPLET_IP" "
    cd $APP_DIR &&
    $APP_DIR/venv/bin/pip install -q -r requirements.txt
"

# Restart service
echo "Restarting service..."
ssh "root@$DROPLET_IP" "systemctl restart polymarket-bot"

# Post-deploy: verify health
echo ""
echo "Post-deploy verification..."
sleep 3
ssh "root@$DROPLET_IP" "
    systemctl is-active polymarket-bot &&
    echo 'Service: running' ||
    echo 'Service: FAILED'

    cd $APP_DIR && git log --oneline -1
"

echo ""
echo "=== Deploy complete ==="
