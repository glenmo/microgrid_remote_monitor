#!/usr/bin/env bash
#
# Install the Lodge weather pusher on desky (reads tracker_analysis, POSTs to pignus /weather/).
#
# Usage (the key is read from stdin, never the command line, which sudo logs):
#   ssh pignus sudo grep -oP '(?<=WEATHER_API_KEY=).*' /etc/weather-monitor.env | sudo bash install_weather_push.sh
#   sudo bash install_weather_push.sh < /dev/null   # re-install, keeping the existing key
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="weather-push.service"
SERVICE_USER="${SUDO_USER:-$USER}"
ENV_FILE="${WEATHER_PUSH_ENV_FILE:-/etc/weather-push.env}"
UNIT_DIR="${WEATHER_PUSH_UNIT_DIR:-/etc/systemd/system}"
KEY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) SERVICE_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ ! -t 0 ]]; then
  IFS= read -r KEY || true
elif [[ ! -f "$ENV_FILE" ]]; then
  read -rsp "WEATHER_API_KEY (from pignus /etc/weather-monitor.env): " KEY; echo
fi
if [[ -n "$KEY" ]]; then
  ( umask 077
    printf 'WEATHER_API_KEY=%s\nWEATHER_PUSH_URL=https://monitor.mooramoora.org.au/weather/api/push\n' "$KEY" > "$ENV_FILE" )
  chmod 600 "$ENV_FILE"
elif [[ ! -f "$ENV_FILE" ]]; then
  echo "No $ENV_FILE yet: pipe the WEATHER_API_KEY from pignus into this script" >&2
  exit 1
fi

cat > "$UNIT_DIR/$SERVICE_NAME" <<UNIT
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
