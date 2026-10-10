#!/usr/bin/env python3
"""Push the SMA Meteostation readings from tracker_analysis to the Lodge weather page.

Runs on desky next to tracker_analysis (:8901), which decodes the Sunny WebBox's
FTP pushes. Every 60 s it reads /api/state and POSTs the weather subset to
weather_monitor on pignus (/weather/api/push). The WebBox only produces a new
reading every 15 min; pignus stores each reading time once.

Stdlib only. Env: WEATHER_API_KEY (required), WEATHER_PUSH_URL, WEATHER_STATE_URL.
"""

import json
import logging
import os
import time
import urllib.request
from datetime import datetime

log = logging.getLogger("weather_push")

PUSH_URL = os.environ.get("WEATHER_PUSH_URL", "https://monitor.mooramoora.org.au/weather/api/push")
STATE_URL = os.environ.get("WEATHER_STATE_URL", "http://127.0.0.1:8901/api/state")
API_KEY = os.environ.get("WEATHER_API_KEY", "")
INTERVAL_S = 60
MAX_AGE_S = 1800  # same as tracker_analysis's stale_after_s

# push key: (tracker_analysis live key, scale, decimals)
MAP = {
    "ambient_c": ("amb_temp_c", 1, 2),
    "module_c": ("mod_temp_c", 1, 2),
    "irradiance_wm2": ("poa_wm2", 1, 1),          # Sunny SensorBox: the trusted irradiance
    "pyranometer_wm2": ("pyranometer_wm2", 1, 1),  # reads 5-15 % low (Err 11), kept for reference
    "wind_ms": ("wind_ms", 1, 2),
    "humidity_pct": ("humidity_pct", 1, 2),
    "pressure_hpa": ("air_pressure_pa", 0.01, 2),
    "clearness_index": ("clearness_index", 1, 3),
}


def build_payload(state):
    """tracker_analysis /api/state -> weather push, or None when there's nothing fresh to send."""
    sma = state.get("sma_status") or {}
    live = state.get("live")
    age = sma.get("age_s")
    if not sma.get("connected") or not sma.get("last_read") or age is None or age > MAX_AGE_S or not live:
        return None
    # last_read is desky's local naive time (datetime.now()), so convert on this host.
    payload = {"ts": int(datetime.fromisoformat(sma["last_read"]).timestamp())}
    for key, (src, scale, nd) in MAP.items():
        v = live.get(src)
        payload[key] = round(v * scale, nd) if isinstance(v, (int, float)) else None
    return payload


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def _post(payload):
    req = urllib.request.Request(
        PUSH_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY, "User-Agent": "weather-push/1"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not API_KEY:
        raise SystemExit("WEATHER_API_KEY is not set")
    last_ts = None
    while True:
        try:
            payload = build_payload(_get_json(STATE_URL))
            if payload is None:
                log.info("No fresh Meteostation reading; not pushing")
            else:
                res = _post(payload)
                if payload["ts"] != last_ts:
                    log.info("Pushed reading %s: %s", payload["ts"], res)
                    last_ts = payload["ts"]
        except Exception as exc:
            log.warning("Push failed: %s", exc)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
