#!/usr/bin/env bash
#
# Install the tracker_analysis dashboard as a systemd service.
#
# Usage:
#   sudo bash install.sh                                              # defaults
#   sudo bash install.sh --solis-url http://rubberduck.local:5000/api/solis/data
#   sudo bash install.sh --sma-host 192.168.55.126 --port 8901
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="tracker-analysis.service"
SERVICE_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Defaults
HOST="0.0.0.0"
PORT="8901"
SOLIS_URL="http://rubberduck.local:5000/api/solis/data"
SMA_WATCH_DIR="${HOME}/sma"
SMA_POLL="10.0"
SOLIS_POLL="1.0"
CSV_INTERVAL="5.0"
CSV_DIR="${APP_DIR}/data"
LAT="-37.4"
LON="144.9"
STRING_KWP="16.965"
SITE_NAME="Arctech Solar Tracker with Longi Mono and Bifacial Module Analysis"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)          HOST="$2";          shift 2 ;;
    --port)          PORT="$2";          shift 2 ;;
    --solis-url)     SOLIS_URL="$2";     shift 2 ;;
    --sma-watch-dir) SMA_WATCH_DIR="$2"; shift 2 ;;
    --sma-poll)      SMA_POLL="$2";      shift 2 ;;
    --solis-poll)    SOLIS_POLL="$2";    shift 2 ;;
    --csv-interval)  CSV_INTERVAL="$2";  shift 2 ;;
    --csv-dir)       CSV_DIR="$2";       shift 2 ;;
    --lat)           LAT="$2";           shift 2 ;;
    --lon)           LON="$2";           shift 2 ;;
    --string-kwp)    STRING_KWP="$2";    shift 2 ;;
    --site-name)     SITE_NAME="$2";     shift 2 ;;
    --user)          SERVICE_USER="$2";  shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo ">> App dir:               $APP_DIR"
echo ">> Solis upstream:        $SOLIS_URL"
echo ">> SMA watch dir:         $SMA_WATCH_DIR"
echo ">> Listen:                $HOST:$PORT"
echo ">> CSV dir:               $CSV_DIR"
echo ">> Site:                  $SITE_NAME"
echo ">> String kWp:            $STRING_KWP"
echo ">> Lat / Lon:             $LAT / $LON"

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
Description=Mooramoora tracker bifacial-vs-mono performance dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/app.py \\
    --host ${HOST} \\
    --port ${PORT} \\
    --solis-url ${SOLIS_URL} \\
    --sma-watch-dir ${SMA_WATCH_DIR} \\
    --sma-poll ${SMA_POLL} \\
    --solis-poll ${SOLIS_POLL} \\
    --csv-interval ${CSV_INTERVAL} \\
    --csv-dir ${CSV_DIR} \\
    --lat ${LAT} \\
    --lon ${LON} \\
    --string-kwp ${STRING_KWP} \\
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
echo ">> Open: $URL"
echo
echo "If SMA fields read null, check that WebBox FTP-Push is configured:"
echo "  WebBox UI → Settings → Data trans. → Use FTP-Push: yes"
echo "  FTP server: <desky's IP on the .55 network>"
echo "  Upload directory: sma   (must match --sma-watch-dir leaf name)"
echo "  Username/password: matching a writable user on this machine"
echo
echo "Test the parser against an existing zip:"
echo "  ./venv/bin/python sma_ftp.py --parse $SMA_WATCH_DIR/<some-file>.zip"
