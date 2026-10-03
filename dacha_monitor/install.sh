#!/usr/bin/env bash
#
# Install Dacha Comfort (aircon status page) as a systemd service on pignus.
#
# Usage (from this directory, as the deploying user):
#   sudo bash install.sh            # first install: generates the push key
#   sudo bash install.sh --port 8126
#
# The push key lives in /etc/dacha-monitor.env (root-only). noisy needs the
# same value as `dacha_push_key` in its secrets.yaml.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="dacha-monitor.service"
SERVICE_USER="${SUDO_USER:-$USER}"
ENV_FILE="/etc/dacha-monitor.env"
PORT="8126"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo ">> App dir: $APP_DIR"
echo ">> Listen:  127.0.0.1:$PORT (Apache proxies /dacha/)"
echo ">> User:    $SERVICE_USER"

echo "[1/4] Python virtual environment"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
sudo -u "$SERVICE_USER" mkdir -p "$APP_DIR/data"

echo "[2/4] Push key ($ENV_FILE)"
if [[ ! -f "$ENV_FILE" ]]; then
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  umask 077
  printf 'DACHA_API_KEY=%s\nDACHA_TZ=Australia/Melbourne\n' "$KEY" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "   Generated a new key. Copy it into noisy's secrets.yaml as dacha_push_key:"
  echo "   sudo grep DACHA_API_KEY $ENV_FILE"
else
  echo "   Keeping existing key."
fi

echo "[3/4] systemd unit"
cat > "/etc/systemd/system/$SERVICE_NAME" <<UNIT
[Unit]
Description=Dacha Comfort aircon status page
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=DACHA_DB=$APP_DIR/data/dacha.db
ExecStart=$APP_DIR/venv/bin/python app.py --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "[4/4] Check"
sleep 2
curl -fsS "http://127.0.0.1:$PORT/healthz" && echo
echo
echo "Done. Apache needs the /dacha/ ProxyPass from server/monitor.mooramoora.org.au.conf:"
echo "   sudo cp ../server/monitor.mooramoora.org.au.conf /etc/apache2/sites-available/ && sudo apachectl configtest && sudo systemctl reload apache2"
