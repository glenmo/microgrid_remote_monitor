#!/usr/bin/env python3
"""Lodge Weather: SMA Meteostation readings and a 7-day forecast for the lodge kiosk.

desky (tracker_analysis, fed by the Sunny WebBox's FTP pushes) POSTs the latest
15-minute Meteostation mean every minute to /api/push. This app keeps one row
per reading in SQLite, fetches the Open-Meteo 7-day forecast in the background
and serves the page at / (proxied to /weather/ on monitor.mooramoora.org.au).
The /kiosk page shows it in its view rotation.

Python 3.9 compatible (pignus).
"""

import argparse
import hmac
import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, abort, jsonify, render_template, request

log = logging.getLogger("weather")

# payload key: sqlite column. Limits are in RANGES; out-of-range values become None.
FIELDS = {
    "ambient_c": "ambient",
    "module_c": "module",
    "irradiance_wm2": "irradiance",
    "pyranometer_wm2": "pyranometer",
    "wind_ms": "wind",
    "humidity_pct": "humidity",
    "pressure_hpa": "pressure",
    "clearness_index": "clearness",
}
RANGES = {
    "ambient_c": (-30.0, 60.0),
    "module_c": (-30.0, 90.0),
    "irradiance_wm2": (-20.0, 1600.0),   # small night negatives clamp to 0
    "pyranometer_wm2": (-20.0, 1600.0),
    "wind_ms": (0.0, 75.0),
    "humidity_pct": (0.0, 105.0),        # sensors read a little over 100 in fog
    "pressure_hpa": (800.0, 1100.0),     # station pressure, ~930 hPa at the lodge
    "clearness_index": (0.0, 1.5),
}
CLAMP = {
    "irradiance_wm2": (0.0, None),
    "pyranometer_wm2": (0.0, None),
    "humidity_pct": (None, 100.0),
}
RETENTION_DAYS = 35
MAX_HISTORY_HOURS = 168
MAX_TS_SKEW_S = 2 * 3600
STALE_AFTER_S = 45 * 60

API_KEY = os.environ.get("WEATHER_API_KEY", "")
DB_PATH = os.environ.get("WEATHER_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "weather.db"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

_latest = {"payload": None}
_lock = threading.Lock()


# --------------------------------------------------------------------------- storage

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    cols = ", ".join("%s REAL" % c for c in FIELDS.values())
    with db() as conn:
        conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, %s);" % cols
        )


def prune(now):
    with db() as conn:
        conn.execute("DELETE FROM samples WHERE ts < ?", (int(now - RETENTION_DAYS * 86400),))


def store(payload):
    """Insert one reading. Returns False if that reading time is already stored."""
    cols = ["ts"] + list(FIELDS.values())
    vals = [payload["ts"]] + [payload[k] for k in FIELDS]
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO samples (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
            vals,
        )
        return cur.rowcount == 1


def _latest_from_db():
    with db() as conn:
        r = conn.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    if r is None:
        return None
    out = {"ts": r["ts"]}
    out.update({k: r[c] for k, c in FIELDS.items()})
    return out


# --------------------------------------------------------------------------- validation

def _num(value, key):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    lo, hi = RANGES[key]
    if not math.isfinite(f) or f < lo or f > hi:
        return None
    clo, chi = CLAMP.get(key, (None, None))
    if clo is not None:
        f = max(clo, f)
    if chi is not None:
        f = min(chi, f)
    return f


def clean_payload(raw, now):
    """Keep only known fields, coerced and range-checked. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("payload must be an object")
    try:
        ts = float(raw.get("ts"))
    except (TypeError, ValueError):
        raise ValueError("ts must be epoch seconds")
    if not math.isfinite(ts) or abs(now - ts) > MAX_TS_SKEW_S:
        raise ValueError("ts too far from now")
    out = {"ts": int(ts)}
    out.update({k: _num(raw.get(k), k) for k in FIELDS})
    if all(out[k] is None for k in FIELDS):
        raise ValueError("no readings")
    return out


# --------------------------------------------------------------------------- forecast

LAT, LON = -37.72, 145.55  # Mount Toolebewong, same as soc_traffic_light/extras.py
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# Open-Meteo daily variable -> our key. Default model blend: bom_access_global returns nulls here.
FORECAST_DAILY = {
    "weather_code": "code",
    "temperature_2m_max": "tmax",
    "temperature_2m_min": "tmin",
    "precipitation_probability_max": "rain_pct",
    "precipitation_sum": "rain_mm",
    "wind_speed_10m_max": "wind_kmh",
    "wind_gusts_10m_max": "gust_kmh",
}
FORECAST_REFRESH_S = 15 * 60
FORECAST_RETRY_S = 2 * 60

_forecast = {"days": [], "fetched_at": None, "error": None}


def parse_forecast(js):
    daily = js.get("daily") if isinstance(js, dict) else None
    if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
        raise ValueError("no daily forecast in response")
    days = []
    for i, date in enumerate(daily["time"]):
        d = {"date": str(date)}
        for src, key in FORECAST_DAILY.items():
            arr = daily.get(src) or []
            v = arr[i] if i < len(arr) else None
            if key == "code":
                d[key] = int(v) if isinstance(v, (int, float)) else None
            else:
                d[key] = float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None
        days.append(d)
    return days


def fetch_forecast():
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON,
        "daily": ",".join(FORECAST_DAILY),
        "forecast_days": 7, "timezone": "Australia/Melbourne", "wind_speed_unit": "kmh",
    })
    req = urllib.request.Request(FORECAST_URL + "?" + q, headers={"User-Agent": "lodge-weather/1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return parse_forecast(json.load(resp))


def refresh_forecast(fetch=fetch_forecast):
    """One fetch attempt. On failure keep the last good days and record the error."""
    global _forecast
    try:
        days = fetch()
    except Exception as exc:
        log.warning("Forecast fetch failed: %s", exc)
        with _lock:
            _forecast = dict(_forecast, error=str(exc)[:200])
        return False
    with _lock:
        _forecast = {"days": days, "fetched_at": int(time.time()), "error": None}
    return True


def forecast_loop():
    while True:
        ok = refresh_forecast()
        time.sleep(FORECAST_REFRESH_S if ok else FORECAST_RETRY_S)


# --------------------------------------------------------------------------- routes

def _authorised():
    if not API_KEY:
        return False
    given = request.headers.get("X-API-Key", "")
    return hmac.compare_digest(given.encode(), API_KEY.encode())


@app.route("/api/push", methods=["POST"])
def api_push():
    if not _authorised():
        abort(401)
    now = time.time()
    try:
        payload = clean_payload(request.get_json(force=True, silent=False), now)
    except Exception as exc:  # malformed JSON or shape
        log.warning("Rejected push: %s", exc)
        abort(400)
    stored = store(payload)
    with _lock:
        cur = _latest["payload"]
        if cur is None or payload["ts"] >= cur["ts"]:
            _latest["payload"] = payload
    if stored and int(now) % 3600 < 60:
        prune(now)
    return jsonify({"ok": True, "stored": stored})


@app.route("/api/current")
def api_current():
    with _lock:
        payload = _latest["payload"]
    if payload is None:
        payload = _latest_from_db()  # after a restart
    ts = payload["ts"] if payload else None
    age = None if ts is None else int(time.time()) - ts
    resp = jsonify({
        "ts": ts,
        "age_s": age,
        "stale": age is None or age > STALE_AFTER_S,
        "readings": {k: payload[k] for k in FIELDS} if payload else {},
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/history")
def api_history():
    """Raw readings for the trend chart (one per WebBox mean). ?hours=24 (max 168)."""
    try:
        hours = max(1, min(MAX_HISTORY_HOURS, int(request.args.get("hours", 24))))
    except ValueError:
        hours = 24
    end = int(time.time())
    start = end - hours * 3600
    cols = list(FIELDS.values())
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, %s FROM samples WHERE ts >= ? ORDER BY ts" % ",".join(cols), (start,)
        ).fetchall()
    resp = jsonify({
        "start": start,
        "end": end,
        "columns": ["t"] + list(FIELDS),
        "rows": [[r["ts"]] + [None if r[c] is None else round(r[c], 2) for c in cols] for r in rows],
    })
    resp.headers["Cache-Control"] = "public, max-age=55"
    return resp


@app.route("/api/forecast")
def api_forecast():
    with _lock:
        snap = dict(_forecast)
    resp = jsonify(snap)
    resp.headers["Cache-Control"] = "public, max-age=55"
    return resp


@app.route("/")
def index():
    return render_template("weather.html")


@app.route("/healthz")
def healthz():
    with _lock:
        payload = _latest["payload"]
    return jsonify({"ok": True, "latest_ts": payload["ts"] if payload else None})


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8130)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not API_KEY:
        log.warning("WEATHER_API_KEY is not set: pushes will be rejected")
    init_db()
    prune(time.time())
    threading.Thread(target=forecast_loop, name="forecast", daemon=True).start()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
