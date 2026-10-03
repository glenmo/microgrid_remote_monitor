#!/usr/bin/env python3
"""Octagon Comfort: aircon status page for the Octagon.

noisy (Home Assistant) POSTs a snapshot every minute to /api/push. This app
keeps the latest snapshot in memory, stores a sample per unit in SQLite and
serves the page at / (proxied to /octagon/ on monitor.mooramoora.org.au).

Only units in UNITS are accepted or served.

The Daikin integration reports hvac_action "heating" or "cooling" whenever
the unit is in that mode, even with the compressor stopped. `running` (from
binary_sensor.octagon_aircon_running, compressor frequency above zero) says
whether it is really heating or cooling, so history uses that where present.

Remote on/off: the page POSTs to /api/control with a password (scrypt hash in
OCTAGON_CONTROL_HASH; set it with set_password.sh). pignus can't reach noisy,
so the command waits here until noisy collects it from /api/command (polled
every 10 s by Home Assistant, glenmo/octagon_comfort packages/octagon_control.yaml).

Python 3.9 compatible (pignus).
"""

import argparse
import hashlib
import hmac
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

log = logging.getLogger("octagon")

# key sent by Home Assistant: label shown on the page. Add new units here.
UNITS = {
    "octagon": "Octagon",
}
RETENTION_DAYS = 35
MAX_HISTORY_HOURS = 24 * 30
SAMPLE_GAP_CAP_S = 300  # don't count gaps longer than this as heating/cooling time

API_KEY = os.environ.get("OCTAGON_API_KEY", "")
DB_PATH = os.environ.get("OCTAGON_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "octagon.db"))
SITE_TZ = ZoneInfo(os.environ.get("OCTAGON_TZ", "Australia/Melbourne"))
# scrypt:<n>:<r>:<p>:<salt hex>:<hash hex>, written by set_password.sh. Empty disables control.
CONTROL_HASH = os.environ.get("OCTAGON_CONTROL_HASH", "")

COMMAND_TTL_S = 120       # a command noisy hasn't collected by then is dropped
LOCKOUT_FAILS = 5         # wrong passwords from one address before it is locked out
LOCKOUT_S = 15 * 60
GLOBAL_FAILS_PER_H = 30   # wrong passwords from everyone before control is paused

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

_latest = {"received": None, "payload": None}
_lock = threading.Lock()

UNIT_FIELDS = {
    # payload key: (sqlite column, type)
    "hvac_mode": ("hvac_mode", str),
    "hvac_action": ("hvac_action", str),
    "running": ("running", bool),
    "inside_temp": ("inside", float),
    "outside_temp": ("outside", float),
    "humidity": ("humidity", float),
    "target_temp": ("target", float),
    "fan_mode": ("fan", str),
    "swing_mode": ("swing", str),
    "compressor_kw": ("compressor_kw", float),
    "energy_today_kwh": ("energy_today_kwh", float),
    "available": ("available", bool),
}

# Really heating/cooling: the mode says so and the compressor is running
# (or `running` is unknown, e.g. rows pushed before it was sent).
ACTIVE_SQL = "COALESCE(running, 1) = 1 AND hvac_action = '%s'"


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
                ts INTEGER NOT NULL, unit TEXT NOT NULL,
                hvac_mode TEXT, hvac_action TEXT, running INTEGER,
                inside REAL, outside REAL, humidity REAL, target REAL,
                fan TEXT, swing TEXT,
                compressor_kw REAL, energy_today_kwh REAL,
                available INTEGER,
                PRIMARY KEY (ts, unit)
            );
            CREATE TABLE IF NOT EXISTS controls (
                ts INTEGER NOT NULL, ip TEXT, unit TEXT, action TEXT,
                result TEXT NOT NULL, cmd_id TEXT
            );
            """
        )


def prune():
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    with db() as conn:
        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM controls WHERE ts < ?", (cutoff,))


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
    """Keep only known units and fields, coerced to safe types."""
    if not isinstance(raw, dict):
        raise ValueError("payload must be an object")
    units_in = raw.get("units") or {}
    if not isinstance(units_in, dict):
        raise ValueError("units must be an object")
    units = {}
    for key, label in UNITS.items():
        u = units_in.get(key)
        if not isinstance(u, dict):
            continue
        units[key] = {"name": label}
        for field, (_, kind) in UNIT_FIELDS.items():
            units[key][field] = _coerce(u.get(field), kind)
    return {"units": units}


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

    cols = ["ts", "unit"] + [c for c, _ in UNIT_FIELDS.values()]
    with db() as conn:
        for key, u in payload["units"].items():
            vals = [now, key] + [(int(u[f]) if isinstance(u[f], bool) else u[f]) for f in UNIT_FIELDS]
            conn.execute(
                "INSERT OR REPLACE INTO samples (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                vals,
            )
    if now % 3600 < 60:
        prune()
    return jsonify({"ok": True, "units": sorted(payload["units"])})


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
        "unit_list": list(UNITS.items()),
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
            if r["unit"] not in UNITS:
                continue
            u = {"name": UNITS[r["unit"]]}
            for field, (col, kind) in UNIT_FIELDS.items():
                v = r[col]
                u[field] = (bool(v) if v is not None else None) if kind is bool else v
            units[r["unit"]] = u
    return {"units": units}, ts


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

    units = {}
    with db() as conn:
        for key in UNITS:
            rows = conn.execute(
                """
                SELECT (ts / :b) * :b AS t,
                       AVG(inside) AS inside, AVG(outside) AS outside, AVG(humidity) AS humidity,
                       AVG(CASE WHEN hvac_mode NOT IN ('off') THEN target END) AS target,
                       AVG(CASE WHEN %s THEN 1.0 ELSE 0.0 END) AS heat,
                       AVG(CASE WHEN %s THEN 1.0 ELSE 0.0 END) AS cool,
                       AVG(CASE WHEN hvac_mode IS NOT NULL AND hvac_mode <> 'off' THEN 1.0 ELSE 0.0 END) AS on_frac,
                       AVG(compressor_kw) AS kw
                FROM samples WHERE unit = :unit AND ts >= :start
                GROUP BY t ORDER BY t
                """ % (ACTIVE_SQL % "heating", ACTIVE_SQL % "cooling"),
                {"b": bucket, "unit": key, "start": start},
            ).fetchall()
            units[key] = [
                [r["t"], _r(r["inside"]), _r(r["outside"]), _r(r["humidity"]), _r(r["target"]),
                 _r(r["heat"], 3), _r(r["cool"], 3), _r(r["on_frac"], 3), _r(r["kw"], 3)]
                for r in rows
            ]
    resp = jsonify({
        "start": start, "end": end, "bucket_s": bucket,
        "columns": ["t", "inside", "outside", "humidity", "target", "heat_frac", "cool_frac", "on_frac", "compressor_kw"],
        "units": units,
    })
    resp.headers["Cache-Control"] = "public, max-age=55"
    return resp


def _r(v, nd=1):
    return None if v is None else round(v, nd)


@app.route("/api/summary")
def api_summary():
    """Per local day, per unit: hours heating/cooling, kWh, inside temp range."""
    try:
        days = max(1, min(30, int(request.args.get("days", 7))))
    except ValueError:
        days = 7
    today = datetime.now(SITE_TZ).date()
    first = today - timedelta(days=days - 1)
    start_ts = int(datetime(first.year, first.month, first.day, tzinfo=SITE_TZ).timestamp())

    out = {key: {} for key in UNITS}
    with db() as conn:
        for key in UNITS:
            rows = conn.execute(
                "SELECT ts, hvac_action, running, inside, energy_today_kwh FROM samples WHERE unit = ? AND ts >= ? ORDER BY ts",
                (key, start_ts),
            ).fetchall()
            prev = None
            for r in rows:
                day = datetime.fromtimestamp(r["ts"], SITE_TZ).date().isoformat()
                d = out[key].setdefault(day, {"heat_h": 0.0, "cool_h": 0.0, "kwh": None, "tmin": None, "tmax": None})
                if prev is not None and prev["running"] != 0:
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
    resp = jsonify({
        "days": days_list,
        "units": {
            key: [
                {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out[key].get(day, {}).items()} or None
                for day in days_list
            ]
            for key in UNITS
        },
    })
    resp.headers["Cache-Control"] = "public, max-age=55"
    return resp


# --------------------------------------------------------------------------- remote control

_commands = {}   # id -> {"unit", "action", "queued", "collected"}
_fails = {}      # client ip -> [timestamps of wrong passwords]
_ctl_lock = threading.Lock()


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    n, r, p = 2 ** 15, 8, 1
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt:%d:%d:%d:%s:%s" % (n, r, p, salt.hex(), h.hex())


def check_password(password):
    try:
        algo, n, r, p, salt, want = CONTROL_HASH.split(":")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(want) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), want)


def client_ip():
    # Behind Apache on the same host: its mod_proxy appends the real client last
    if request.remote_addr in ("127.0.0.1", "::1"):
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[-1].strip()[:64]
    return (request.remote_addr or "?")[:64]


def _audit(ip, unit, action, result, cmd_id=None):
    with db() as conn:
        conn.execute("INSERT INTO controls (ts, ip, unit, action, result, cmd_id) VALUES (?,?,?,?,?,?)",
                     (int(time.time()), ip, unit, action, result, cmd_id))


def _locked_for(ip, now):
    """Seconds until this address (or everyone) may try again, 0 if not locked."""
    recent = [t for t in _fails.get(ip, []) if now - t < LOCKOUT_S]
    _fails[ip] = recent
    wait = 0
    if len(recent) >= LOCKOUT_FAILS:
        wait = int(recent[-LOCKOUT_FAILS] + LOCKOUT_S - now) + 1
    everyone = sorted(t for ts in _fails.values() for t in ts if now - t < 3600)
    if len(everyone) >= GLOBAL_FAILS_PER_H:
        wait = max(wait, int(everyone[-GLOBAL_FAILS_PER_H] + 3600 - now) + 1)
    return wait


def _expire(now):
    for cid in [c for c, v in _commands.items() if now - v["queued"] > 3600]:
        del _commands[cid]


def _status(cmd, now):
    if cmd["collected"]:
        return "collected"
    if cmd.get("replaced"):
        return "replaced"
    return "expired" if now - cmd["queued"] > COMMAND_TTL_S else "waiting"


@app.route("/api/control", methods=["POST"])
def api_control():
    """Queue an on/off for noisy. Body: {"unit", "action": "on"|"off", "password"}."""
    if not CONTROL_HASH:
        return jsonify({"error": "Remote control isn't set up."}), 503
    if not request.is_json:  # also makes a cross-site form post fail CORS preflight
        abort(415)
    body = request.get_json(silent=True) or {}
    unit, action, password = body.get("unit"), body.get("action"), body.get("password")
    if unit not in UNITS or action not in ("on", "off") or not isinstance(password, str) or len(password) > 256:
        abort(400)
    ip, now = client_ip(), time.time()

    with _ctl_lock:
        wait = _locked_for(ip, now)
    if wait:
        _audit(ip, unit, action, "locked")
        return jsonify({"error": "Too many wrong passwords. Try again in %d min." % math.ceil(wait / 60),
                        "retry_after": wait}), 429

    if not check_password(password):
        with _ctl_lock:
            _fails.setdefault(ip, []).append(now)
        _audit(ip, unit, action, "bad password")
        log.warning("Wrong control password from %s", ip)
        return jsonify({"error": "Wrong password."}), 401

    with _ctl_lock:
        _fails.pop(ip, None)
        _expire(now)
        # A newer command for the unit replaces one noisy hasn't collected yet
        for c in _commands.values():
            if c["unit"] == unit and not c["collected"]:
                c["replaced"] = True
        cid = secrets.token_urlsafe(12)
        _commands[cid] = {"unit": unit, "action": action, "queued": now, "collected": None}
    _audit(ip, unit, action, "queued", cid)
    log.info("Queued %s %s from %s (%s)", unit, action, ip, cid)
    return jsonify({"id": cid, "status": "waiting", "ttl_s": COMMAND_TTL_S}), 202


@app.route("/api/control/<cid>")
def api_control_status(cid):
    now = time.time()
    with _ctl_lock:
        cmd = _commands.get(cid)
        if cmd is None:
            abort(404)
        out = {"id": cid, "unit": cmd["unit"], "action": cmd["action"], "status": _status(cmd, now),
               "age_s": int(now - cmd["queued"])}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/command")
def api_command():
    """noisy collects pending commands here (X-API-Key). Each is handed out once."""
    if not _authorised():
        abort(401)
    now = time.time()
    out = []
    with _ctl_lock:
        for cid, c in _commands.items():
            if _status(c, now) == "waiting":
                c["collected"] = now
                out.append({"id": cid, "unit": c["unit"], "action": c["action"]})
    for c in out:
        _audit("noisy", c["unit"], c["action"], "collected", c["id"])
    resp = jsonify({"commands": out})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index():
    return render_template("index.html", unit_list=list(UNITS.items()), control=bool(CONTROL_HASH))


@app.route("/manifest.webmanifest")
def manifest():
    resp = jsonify({
        "name": "Octagon Comfort",
        "short_name": "Octagon",
        "description": "Live aircon status at the Octagon.",
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
    # Served from the app root so its scope covers the whole page (/octagon/ behind Apache)
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
    p.add_argument("--port", type=int, default=8128)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--hash-password", action="store_true",
                   help="read a password on stdin, print its OCTAGON_CONTROL_HASH value and exit")
    args = p.parse_args()
    if args.hash_password:
        print(hash_password(input().rstrip("\n")))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not API_KEY:
        log.warning("OCTAGON_API_KEY is not set: pushes will be rejected")
    if not CONTROL_HASH:
        log.warning("OCTAGON_CONTROL_HASH is not set: remote on/off is disabled")
    init_db()
    prune()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
