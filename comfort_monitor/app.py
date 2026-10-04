#!/usr/bin/env python3
"""Lodge Comfort: public aircon dashboard for the Moora Moora lodge.

LodgyBox (Home Assistant at the lodge) POSTs a snapshot every minute to
/api/push. This app keeps the latest snapshot in memory, stores a sample per
room in SQLite and serves the public page at / (proxied to /comfort/ on
monitor.mooramoora.org.au).

Only rooms in PUBLIC_ROOMS are accepted or served. The Caretaker's Flat is
someone's home and is deliberately never published.

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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

log = logging.getLogger("comfort")

PUBLIC_ROOMS = {
    "lounge": "Lounge",
    "dining_room": "Dining Room",
}
# Plug-switched heaters: latest state only (shown on the /kiosk/ page), not stored
PUBLIC_HEATERS = {
    "office": "Office",
    "lodge_upstairs": "Lodge Upstairs",
}
RETENTION_DAYS = 35
MAX_HISTORY_HOURS = 24 * 30
SAMPLE_GAP_CAP_S = 300  # don't count gaps longer than this as heating/cooling time

API_KEY = os.environ.get("COMFORT_API_KEY", "")
DB_PATH = os.environ.get("COMFORT_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "comfort.db"))
SITE_TZ = ZoneInfo(os.environ.get("COMFORT_TZ", "Australia/Melbourne"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

_latest = {"received": None, "payload": None}
_lock = threading.Lock()

UNIT_FIELDS = {
    # payload key: (sqlite column, type)
    "hvac_mode": ("hvac_mode", str),
    "hvac_action": ("hvac_action", str),
    "inside_temp": ("inside", float),
    "outside_temp": ("outside", float),
    "humidity": ("humidity", float),
    "target_temp": ("target", float),
    "fan_mode": ("fan", str),
    "swing_mode": ("swing", str),
    "compressor_kw": ("compressor_kw", float),
    "energy_today_kwh": ("energy_today_kwh", float),
    "heat_kwh_last_hour": ("heat_kwh_hr", float),
    "cool_kwh_last_hour": ("cool_kwh_hr", float),
    "managed_by_surplus": ("managed", bool),
    "available": ("available", bool),
}
HEATER_FIELDS = {
    "available": bool,
    "on": bool,
    "power_w": float,
    "managed_by_surplus": bool,
}
SURPLUS_FIELDS = {
    "active": bool,
    "generator_running": bool,
    "solis_soc": float,
    "sppro_soc": float,
    "frequency_hz": float,
    "heating_enabled": bool,
    "setpoint": float,
}


# --------------------------------------------------------------------------- storage

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER NOT NULL, room TEXT NOT NULL,
                hvac_mode TEXT, hvac_action TEXT,
                inside REAL, outside REAL, humidity REAL, target REAL,
                fan TEXT, swing TEXT,
                compressor_kw REAL, energy_today_kwh REAL, heat_kwh_hr REAL, cool_kwh_hr REAL,
                managed INTEGER, available INTEGER,
                PRIMARY KEY (ts, room)
            );
            CREATE TABLE IF NOT EXISTS surplus (
                ts INTEGER PRIMARY KEY,
                active INTEGER, generator INTEGER,
                solis_soc REAL, sppro_soc REAL, freq REAL,
                enabled INTEGER, setpoint REAL
            );
            """
        )


def prune():
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    with db() as conn:
        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM surplus WHERE ts < ?", (cutoff,))


# --------------------------------------------------------------------------- validation

def _coerce(value, kind):
    if value is None:
        return None
    try:
        if kind is bool:
            if isinstance(value, str):
                return value.lower() in ("on", "true", "1", "yes")
            return bool(value)
        if kind is float:
            f = float(value)
            return f if math.isfinite(f) else None
        if kind is str:
            s = str(value)[:32]
            return s if s not in ("unknown", "unavailable", "None", "") else None
    except (TypeError, ValueError):
        return None
    return None


def clean_payload(raw):
    """Keep only public rooms and known fields, coerced to safe types."""
    if not isinstance(raw, dict):
        raise ValueError("payload must be an object")
    units_in = raw.get("units") or {}
    if not isinstance(units_in, dict):
        raise ValueError("units must be an object")
    units = {}
    for room, label in PUBLIC_ROOMS.items():
        u = units_in.get(room)
        if not isinstance(u, dict):
            continue
        units[room] = {"name": label}
        for key, (_, kind) in UNIT_FIELDS.items():
            units[room][key] = _coerce(u.get(key), kind)
    heaters_in = raw.get("heaters") or {}
    heaters = {}
    if isinstance(heaters_in, dict):
        for key, label in PUBLIC_HEATERS.items():
            h = heaters_in.get(key)
            if isinstance(h, dict):
                heaters[key] = {"name": label}
                heaters[key].update({k: _coerce(h.get(k), kind) for k, kind in HEATER_FIELDS.items()})
    s_in = raw.get("surplus") or {}
    surplus = {k: _coerce(s_in.get(k), kind) for k, kind in SURPLUS_FIELDS.items()} if isinstance(s_in, dict) else {}
    rooms_enabled = s_in.get("rooms_enabled") if isinstance(s_in, dict) else None
    if isinstance(rooms_enabled, dict):
        surplus["rooms_enabled"] = {r: _coerce(rooms_enabled.get(r), bool) for r in PUBLIC_ROOMS}
    return {"units": units, "heaters": heaters, "surplus": surplus}


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
    try:
        payload = clean_payload(request.get_json(force=True, silent=False))
    except Exception as exc:  # malformed JSON or shape
        log.warning("Rejected push: %s", exc)
        abort(400)

    now = int(time.time())
    with _lock:
        _latest["received"] = now
        _latest["payload"] = payload

    with db() as conn:
        for room, u in payload["units"].items():
            cols = ["ts", "room"] + [c for c, _ in UNIT_FIELDS.values()]
            vals = [now, room] + [
                (int(u[k]) if isinstance(u[k], bool) else u[k]) for k in UNIT_FIELDS
            ]
            conn.execute(
                "INSERT OR REPLACE INTO samples (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                vals,
            )
        s = payload["surplus"]
        if s:
            conn.execute(
                "INSERT OR REPLACE INTO surplus VALUES (?,?,?,?,?,?,?,?)",
                (
                    now,
                    _b(s.get("active")), _b(s.get("generator_running")),
                    s.get("solis_soc"), s.get("sppro_soc"), s.get("frequency_hz"),
                    _b(s.get("heating_enabled")), s.get("setpoint"),
                ),
            )
    if now % 3600 < 60:
        prune()
    return jsonify({"ok": True, "rooms": sorted(payload["units"])})


def _b(v):
    return None if v is None else int(bool(v))


@app.route("/api/current")
def api_current():
    with _lock:
        received, payload = _latest["received"], _latest["payload"]
    if payload is None:
        # Fall back to the newest stored rows after a restart
        payload, received = _latest_from_db()
    age = None if received is None else int(time.time()) - received
    resp = jsonify({
        "received": received,
        "age_s": age,
        "stale": age is None or age > 300,
        "units": (payload or {}).get("units", {}),
        "surplus": (payload or {}).get("surplus", {}),
        "heaters": (payload or {}).get("heaters", {}),
        "rooms": list(PUBLIC_ROOMS.items()),
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _latest_from_db():
    with db() as conn:
        row = conn.execute("SELECT MAX(ts) AS ts FROM samples").fetchone()
        if not row or row["ts"] is None:
            return None, None
        ts = row["ts"]
        units = {}
        for r in conn.execute("SELECT * FROM samples WHERE ts = ?", (ts,)):
            if r["room"] not in PUBLIC_ROOMS:
                continue
            u = {"name": PUBLIC_ROOMS[r["room"]]}
            for key, (col, kind) in UNIT_FIELDS.items():
                v = r[col]
                u[key] = (bool(v) if v is not None else None) if kind is bool else v
            units[r["room"]] = u
        s = conn.execute("SELECT * FROM surplus WHERE ts = ?", (ts,)).fetchone()
        surplus = {}
        if s:
            surplus = {
                "active": _ob(s["active"]), "generator_running": _ob(s["generator"]),
                "solis_soc": s["solis_soc"], "sppro_soc": s["sppro_soc"], "frequency_hz": s["freq"],
                "heating_enabled": _ob(s["enabled"]), "setpoint": s["setpoint"],
            }
    return {"units": units, "surplus": surplus}, ts


def _ob(v):
    return None if v is None else bool(v)


@app.route("/api/history")
def api_history():
    """Bucketed series for the charts. ?hours=24 (max 720)."""
    try:
        hours = max(1, min(MAX_HISTORY_HOURS, int(request.args.get("hours", 24))))
    except ValueError:
        hours = 24
    end = int(time.time())
    start = end - hours * 3600
    bucket = max(60, (hours * 3600) // 360)

    rooms = {}
    with db() as conn:
        for room in PUBLIC_ROOMS:
            rows = conn.execute(
                """
                SELECT (ts / :b) * :b AS t,
                       AVG(inside) AS inside, AVG(outside) AS outside, AVG(humidity) AS humidity,
                       AVG(CASE WHEN hvac_mode NOT IN ('off') THEN target END) AS target,
                       AVG(CASE WHEN hvac_action = 'heating' THEN 1.0 ELSE 0.0 END) AS heat,
                       AVG(CASE WHEN hvac_action = 'cooling' THEN 1.0 ELSE 0.0 END) AS cool,
                       AVG(CASE WHEN hvac_mode IS NOT NULL AND hvac_mode <> 'off' THEN 1.0 ELSE 0.0 END) AS on_frac,
                       AVG(compressor_kw) AS kw,
                       AVG(COALESCE(managed, 0)) AS managed
                FROM samples WHERE room = :room AND ts >= :start
                GROUP BY t ORDER BY t
                """,
                {"b": bucket, "room": room, "start": start},
            ).fetchall()
            rooms[room] = [
                [r["t"], _r(r["inside"]), _r(r["outside"]), _r(r["humidity"]), _r(r["target"]),
                 _r(r["heat"], 3), _r(r["cool"], 3), _r(r["on_frac"], 3), _r(r["kw"], 3), _r(r["managed"], 3)]
                for r in rows
            ]
        srows = conn.execute(
            """
            SELECT (ts / :b) * :b AS t, AVG(active) AS active, AVG(generator) AS gen,
                   AVG(solis_soc) AS solis, AVG(sppro_soc) AS sppro, MAX(freq) AS freq
            FROM surplus WHERE ts >= :start GROUP BY t ORDER BY t
            """,
            {"b": bucket, "start": start},
        ).fetchall()
    resp = jsonify({
        "start": start, "end": end, "bucket_s": bucket,
        "columns": ["t", "inside", "outside", "humidity", "target", "heat_frac", "cool_frac", "on_frac", "compressor_kw", "managed_frac"],
        "rooms": rooms,
        "surplus_columns": ["t", "active_frac", "generator_frac", "solis_soc", "sppro_soc", "freq_max"],
        "surplus": [[r["t"], _r(r["active"], 3), _r(r["gen"], 3), _r(r["solis"]), _r(r["sppro"]), _r(r["freq"], 2)] for r in srows],
    })
    resp.headers["Cache-Control"] = "public, max-age=55"
    return resp


def _r(v, nd=1):
    return None if v is None else round(v, nd)


@app.route("/api/summary")
def api_summary():
    """Per local day, per room: hours heating/cooling, kWh (where measured), temp range."""
    try:
        days = max(1, min(30, int(request.args.get("days", 7))))
    except ValueError:
        days = 7
    today = datetime.now(SITE_TZ).date()
    first = today - timedelta(days=days - 1)
    start_ts = int(datetime(first.year, first.month, first.day, tzinfo=SITE_TZ).timestamp())

    out = {room: {} for room in PUBLIC_ROOMS}
    with db() as conn:
        for room in PUBLIC_ROOMS:
            rows = conn.execute(
                "SELECT ts, hvac_action, inside, energy_today_kwh FROM samples WHERE room = ? AND ts >= ? ORDER BY ts",
                (room, start_ts),
            ).fetchall()
            prev = None
            for r in rows:
                day = datetime.fromtimestamp(r["ts"], SITE_TZ).date().isoformat()
                d = out[room].setdefault(day, {"heat_h": 0.0, "cool_h": 0.0, "kwh": None, "tmin": None, "tmax": None})
                if prev is not None:
                    gap = min(r["ts"] - prev["ts"], SAMPLE_GAP_CAP_S)
                    if prev["hvac_action"] == "heating":
                        d["heat_h"] += gap / 3600
                    elif prev["hvac_action"] == "cooling":
                        d["cool_h"] += gap / 3600
                if r["energy_today_kwh"] is not None:
                    d["kwh"] = max(d["kwh"] or 0.0, r["energy_today_kwh"])
                if r["inside"] is not None:
                    d["tmin"] = r["inside"] if d["tmin"] is None else min(d["tmin"], r["inside"])
                    d["tmax"] = r["inside"] if d["tmax"] is None else max(d["tmax"], r["inside"])
                prev = r
    days_list = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    result = {
        "days": days_list,
        "rooms": {
            room: [
                {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out[room].get(day, {}).items()} or None
                for day in days_list
            ]
            for room in PUBLIC_ROOMS
        },
    }
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "public, max-age=55"
    return resp


@app.route("/")
def index():
    return render_template("index.html", room_list=list(PUBLIC_ROOMS.items()))


@app.route("/manifest.webmanifest")
def manifest():
    resp = jsonify({
        "name": "Lodge Comfort",
        "short_name": "Lodge Comfort",
        "description": "Live aircon comfort and solar-surplus heating at the Moora Moora lodge.",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": "#0e1116",
        "theme_color": "#0e1116",
        "icons": [
            {"src": "static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            {"src": "static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    })
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


@app.route("/sw.js")
def service_worker():
    # Served from the app root so its scope covers the whole page (/comfort/ behind Apache)
    resp = send_from_directory(os.path.dirname(os.path.abspath(__file__)), "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/healthz")
def healthz():
    with _lock:
        received = _latest["received"]
    return jsonify({"ok": True, "last_push_age_s": None if received is None else int(time.time()) - received})


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8125)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not API_KEY:
        log.warning("COMFORT_API_KEY is not set: pushes will be rejected")
    init_db()
    prune()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
