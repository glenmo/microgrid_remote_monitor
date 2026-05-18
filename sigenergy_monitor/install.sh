#!/usr/bin/env bash
#
# Install the Sigenergy SigenStor monitoring dashboard as a systemd service.
#
# Usage:
#   sudo bash install.sh
#   sudo bash install.sh --sigen-host 192.168.55.131 --port 8902
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="sigenergy-monitor.service"
SERVICE_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# $HOME is /root when running under sudo — resolve the invoking user's home.
SERVICE_USER_HOME="$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)"

# Defaults
HOST="0.0.0.0"
PORT="8902"
SIGEN_HOST="192.168.55.131"
SIGEN_PORT="502"
SIGEN_PLANT_SLAVE="247"
SIGEN_INV_SLAVE="1"
SIGEN_POLL="5.0"
SMA_WATCH_DIR="${SERVICE_USER_HOME}/sma"
SMA_POLL="10.0"
CSV_INTERVAL="5.0"
CSV_DIR="${APP_DIR}/data"
BATTERY_KWH="32.0"
SITE_NAME="Sigenergy SigenStor Monitoring"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)               HOST="$2";              shift 2 ;;
    --port)               PORT="$2";              shift 2 ;;
    --sigen-host)         SIGEN_HOST="$2";        shift 2 ;;
    --sigen-port)         SIGEN_PORT="$2";        shift 2 ;;
    --sigen-plant-slave)  SIGEN_PLANT_SLAVE="$2"; shift 2 ;;
    --sigen-inv-slave)    SIGEN_INV_SLAVE="$2";   shift 2 ;;
    --sigen-poll)         SIGEN_POLL="$2";        shift 2 ;;
    --sma-watch-dir)      SMA_WATCH_DIR="$2";     shift 2 ;;
    --sma-poll)           SMA_POLL="$2";          shift 2 ;;
    --csv-interval)       CSV_INTERVAL="$2";      shift 2 ;;
    --csv-dir)            CSV_DIR="$2";           shift 2 ;;
    --battery-kwh)        BATTERY_KWH="$2";       shift 2 ;;
    --site-name)          SITE_NAME="$2";         shift 2 ;;
    --user)               SERVICE_USER="$2";      shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo ">> App dir:               $APP_DIR"
echo ">> Sigenergy:             $SIGEN_HOST:$SIGEN_PORT (plant=$SIGEN_PLANT_SLAVE inv=$SIGEN_INV_SLAVE)"
echo ">> SMA watch dir:         $SMA_WATCH_DIR"
echo ">> Listen:                $HOST:$PORT"
echo ">> CSV dir:               $CSV_DIR"
echo ">> Site:                  $SITE_NAME"
echo ">> Battery nameplate:     $BATTERY_KWH kWh"

if [[ ! -d "$APP_DIR/venv" ]]; then
  echo ">> Creating venv"
  "$PYTHON_BIN" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

mkdir -p "$CSV_DIR"
chown -R "$SERVICE_USER" "$APP_DIR/venv" "$CSV_DIR" || true

UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
echo ">> Writing $UNIT_PATH"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Sigenergy SigenStor Monitoring dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/app.py \\
    --host ${HOST} \\
    --port ${PORT} \\
    --sigen-host ${SIGEN_HOST} \\
    --sigen-port ${SIGEN_PORT} \\
    --sigen-plant-slave ${SIGEN_PLANT_SLAVE} \\
    --sigen-inv-slave ${SIGEN_INV_SLAVE} \\
    --sigen-poll ${SIGEN_POLL} \\
    --sma-watch-dir ${SMA_WATCH_DIR} \\
    --sma-poll ${SMA_POLL} \\
    --csv-interval ${CSV_INTERVAL} \\
    --csv-dir ${CSV_DIR} \\
    --battery-kwh ${BATTERY_KWH} \\
    --site-name "${SITE_NAME}"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl --no-pager status "$SERVICE_NAME" | head -10 || true

HN="$(hostname -f 2>/dev/null || hostname)"
if [[ "$HN" == *.* ]]; then
  URL="http://${HN}:${PORT}/"
else
  URL="http://${HN}.local:${PORT}/"
fi
echo
echo ">> Open: $URL"
echo
echo "Diagnostic (one-shot register dump):"
echo "  cd $APP_DIR && ./venv/bin/python sigen_reader.py --once --sigen-host $SIGEN_HOST"
