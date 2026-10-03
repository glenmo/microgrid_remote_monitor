#!/usr/bin/env bash
#
# Set or change the password for the on/off button on Octagon Comfort.
#
# Usage (on pignus, from this directory):
#   sudo bash set_password.sh
#
# Only a scrypt hash is stored, as OCTAGON_CONTROL_HASH in /etc/octagon-monitor.env.
# To turn the button off, delete that line and restart octagon-monitor.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="/etc/octagon-monitor.env"

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "$ENV_FILE is missing; run install.sh first." >&2; exit 1; }

read -rsp "New password: " pw1; echo
read -rsp "Again: " pw2; echo
[[ "$pw1" == "$pw2" ]] || { echo "They don't match." >&2; exit 1; }
[[ ${#pw1} -ge 8 ]] || { echo "Use at least 8 characters." >&2; exit 1; }

HASH="$(printf '%s\n' "$pw1" | "$APP_DIR/venv/bin/python" "$APP_DIR/app.py" --hash-password)"
unset pw1 pw2

tmp="$(mktemp "$ENV_FILE.XXXXXX")"
grep -v '^OCTAGON_CONTROL_HASH=' "$ENV_FILE" > "$tmp" || true
printf 'OCTAGON_CONTROL_HASH=%s\n' "$HASH" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"

systemctl restart octagon-monitor.service
echo "Password set; octagon-monitor restarted."
