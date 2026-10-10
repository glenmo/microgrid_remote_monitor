#!/usr/bin/env bash
# The installer must take the key on stdin (never argv, which sudo logs) and write it mode 600.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/units"
for c in systemctl journalctl; do printf '#!/bin/sh\nexit 0\n' > "$T/bin/$c"; chmod +x "$T/bin/$c"; done
export PATH="$T/bin:$PATH" WEATHER_PUSH_ENV_FILE="$T/weather-push.env" WEATHER_PUSH_UNIT_DIR="$T/units"
echo "s3cret-key" | bash "$HERE/../install_weather_push.sh" >/dev/null
grep -qx "WEATHER_API_KEY=s3cret-key" "$T/weather-push.env" || { echo "FAIL: key not written from stdin"; exit 1; }
[[ "$(stat -f %Lp "$T/weather-push.env" 2>/dev/null || stat -c %a "$T/weather-push.env")" == 600 ]] || { echo "FAIL: env file not 600"; exit 1; }
grep -q "EnvironmentFile=$T/weather-push.env" "$T/units/weather-push.service" || { echo "FAIL: unit not written"; exit 1; }
echo "" | bash "$HERE/../install_weather_push.sh" >/dev/null 2>&1 && grep -qx "WEATHER_API_KEY=s3cret-key" "$T/weather-push.env" || { echo "FAIL: re-install should keep the existing key"; exit 1; }
echo PASS
