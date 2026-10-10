#!/usr/bin/env bash
#
# Install the Lodge weather pusher on desky (reads tracker_analysis, POSTs to pignus /weather/).
#
# Usage:
#   sudo bash install_weather_push.sh --key <WEATHER_API_KEY from pignus /etc/weather-monitor.env>
#   sudo bash install_weather_push.sh            # re-install, keeping the existing key
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="weather-push.service"
SERVICE_USER="${SUDO_USER:-$USER}"
ENV_FILE="/etc/weather-push.env"
KEY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key) KEY="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -n "$KEY" ]]; then
  umask 077
  printf 'WEATHER_API_KEY=%s\nWEATHER_PUSH_URL=https://monitor.mooramoora.org.au/weather/api/push\n' "$KEY" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
elif [[ ! -f "$ENV_FILE" ]]; then
  echo "No $ENV_FILE yet: pass --key <WEATHER_API_KEY from pignus>" >&2
  exit 1
fi

cat > "/etc/systemd/system/$SERVICE_NAME" <<UNIT
[Unit]
Description=Push Meteostation readings to the Lodge weather page
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $APP_DIR/weather_push.py
Restart=always
RestartSec=30
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 3
systemctl --no-pager status "$SERVICE_NAME" | head -8 || true
journalctl -u "$SERVICE_NAME" -n 5 --no-pager
