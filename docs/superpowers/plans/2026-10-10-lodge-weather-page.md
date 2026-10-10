# Lodge Weather Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A full-screen "Lodge weather" page in the Lodge kiosk's 30 s view rotation. It shows live SMA Meteostation readings, a 24 h trend chart and a 7-day forecast.

**Architecture:** On desky, a small stdlib pusher (`tracker_analysis/weather_push.py`) reads `tracker_analysis`'s `/api/state` every 60 s and POSTs the Meteostation subset to a new pignus app, `weather_monitor/` (Flask + SQLite, :8130, Apache `/weather/`), modelled on `comfort_monitor/`. That app stores one row per WebBox reading, fetches the Open-Meteo 7-day forecast in a background thread, and serves the page. `soc_traffic_light`'s kiosk adds `/weather/` to its iframe rotation.

**Tech Stack:** Python 3.9-compatible Flask, sqlite3, urllib (no new deps); single-file HTML/CSS/JS with inline SVG; pytest for tests (dev-only, not in requirements.txt).

**Spec:** `docs/superpowers/specs/2026-10-10-lodge-weather-page-design.md`

## Global Constraints

- Python 3.9 compatible on pignus: no `X | Y` type unions, no `match`, no `zoneinfo`-free assumptions beyond what comfort_monitor already uses.
- `weather_monitor/requirements.txt` is exactly `flask>=2.2` (same as comfort_monitor). The pusher is stdlib only.
- Port `8130`, Apache path `/weather/`, unit `weather-monitor.service`, env file `/etc/weather-monitor.env` with `WEATHER_API_KEY`, DB env `WEATHER_DB`.
- Pusher: unit `weather-push.service` on desky, env file `/etc/weather-push.env` with `WEATHER_API_KEY` and `WEATHER_PUSH_URL` (default `https://monitor.mooramoora.org.au/weather/api/push`).
- Auth header `X-API-Key`, compared with `hmac.compare_digest`, empty server key rejects everything.
- Forecast: Open-Meteo, `LAT, LON = -37.72, 145.55`, `forecast_days=7`, `timezone=Australia/Melbourne`, `wind_speed_unit=kmh`, default model (not `bom_access_global`). Refresh 15 min, retry 2 min on error.
- Retention 35 days. History max 168 h. Live data is stale after 45 min (`STALE_AFTER_S = 2700`). The pusher skips data older than 1800 s.
- The page is laid out at 1920×1080 and uses the kiosk's dark theme variables (copied from `soc_traffic_light/templates/kiosk.html` lines 9–12).
- Pressure is the station's absolute pressure (site ≈ 700 m, so ≈ 930 hPa). Label it "station pressure" and don't convert it to sea level.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- pignus runs fail2ban: batch SSH commands into one connection and stop after a failed connection.
- Pushing to GitHub and deploying (Task 8) are outward-facing. Get the user's go-ahead first.

## Review Focus

1. **Sensor glitches** (for example ambient −200 °C, humidity 101 %, irradiance −3 W/m² at night). The out-of-range values become `None`, small night negatives clamp to 0, and humidity over 100 clamps to 100. The page shows "—" rather than nonsense. Pinned in Task 1 (`test_clean_payload_ranges`).
2. **The same reading pushed every minute** (the WebBox only updates every 15 min). It must store exactly one row, without churning the latest value. Pinned in Task 1 (`test_duplicate_ts_stored_once`).
3. **A push arriving out of order or late** (an older ts after a newer one, for example after a desky backlog). The older reading must not replace the newer latest value. Pinned in Task 1 (`test_older_push_does_not_replace_latest`).
4. **pignus restarts with no push yet**. `/api/current` must serve the newest stored row, not an empty page. Pinned in Task 1 (`test_current_falls_back_to_db`).
5. **Open-Meteo returns partial or null days** (for example a missing `precipitation_sum` value). The parser must not crash, and those fields become `None`. Pinned in Task 3 (`test_parse_forecast_handles_nulls`).

---

## File Structure

**microgrid_remote_monitor** (`~/Code/microgrid_remote_monitor`):
- Create `weather_monitor/app.py`: Flask app with validation, storage, push/current/history/forecast routes, the forecast thread and `main()`.
- Create `weather_monitor/templates/weather.html`: the kiosk page.
- Create `weather_monitor/tests/test_app.py`: pytest for the app.
- Create `weather_monitor/requirements.txt`, `weather_monitor/install.sh`, `weather_monitor/README.md`.
- Create `tracker_analysis/weather_push.py`: the desky pusher.
- Create `tracker_analysis/install_weather_push.sh`: the desky pusher unit.
- Create `tracker_analysis/tests/test_weather_push.py`.
- Modify `server/monitor.mooramoora.org.au.conf`: add the `/weather/` block above the catch-all.
- Modify `.gitignore`: add `weather_monitor/data/`.
- Modify `CLAUDE.md`: the hosts table and a component note.

**soc_traffic_light** (`~/Code/soc_traffic_light`):
- Modify `templates/kiosk.html` (`cycleViews`, around L707–714): add `/weather/` to the rotation.
- Modify `README.md`: the rotation list.

Dev test environment (gitignored `venv/`): `cd ~/Code/microgrid_remote_monitor/weather_monitor && python3 -m venv venv && venv/bin/pip install -q flask pytest`. All `pytest` commands below run as `weather_monitor/venv/bin/python -m pytest …` from the repo root.

---

### Task 1: weather_monitor core: validation, storage, push and current

**Files:**
- Create: `weather_monitor/app.py`
- Create: `weather_monitor/tests/test_app.py`
- Create: `weather_monitor/requirements.txt`
- Modify: `.gitignore`

**Interfaces:**
- Produces:
  - Module globals `API_KEY: str`, `DB_PATH: str` (read at call time, so tests can monkeypatch them), `FIELDS: dict[str, str]` (payload key → sqlite column), `STALE_AFTER_S = 2700`.
  - `clean_payload(raw, now: float) -> dict`: returns `{"ts": int, <each FIELDS key>: float|None}` or raises `ValueError`.
  - `store(payload: dict) -> bool`: True if inserted, False if a duplicate.
  - `init_db() -> None`, `prune(now: float) -> None`.
  - Flask `app`. `POST /api/push` returns `{"ok": true, "stored": bool}`. `GET /api/current` returns `{"ts", "age_s", "stale", "readings": {FIELDS keys}}`. `GET /healthz`.

- [ ] **Step 1: Set up the dev venv and the requirements file**

```bash
cd ~/Code/microgrid_remote_monitor
mkdir -p weather_monitor/tests weather_monitor/templates
printf 'flask>=2.2\n' > weather_monitor/requirements.txt
printf '\n# Lodge Weather runtime data (SQLite)\nweather_monitor/data/\n' >> .gitignore
cd weather_monitor && python3 -m venv venv && venv/bin/pip install -q flask pytest && cd ..
```

- [ ] **Step 2: Write the failing tests**

`weather_monitor/tests/test_app.py`:

```python
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as wm  # noqa: E402

KEY = "test-key"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(wm, "DB_PATH", str(tmp_path / "weather.db"))
    monkeypatch.setattr(wm, "API_KEY", KEY)
    monkeypatch.setitem(wm._latest, "payload", None)
    wm.init_db()
    return wm.app.test_client()


def reading(ts=None, **over):
    p = {
        "ts": int(time.time()) - 600 if ts is None else ts,
        "ambient_c": 13.5, "module_c": 26.3, "irradiance_wm2": 569.3,
        "pyranometer_wm2": 533.8, "wind_ms": 2.1, "humidity_pct": 72.3,
        "pressure_hpa": 930.7, "clearness_index": 0.78,
    }
    p.update(over)
    return p


def push(client, payload, key=KEY):
    return client.post("/api/push", json=payload, headers={"X-API-Key": key})


def test_clean_payload_keeps_known_fields_and_drops_unknown():
    now = time.time()
    out = wm.clean_payload(reading(ts=int(now), extra="x"), now)
    assert set(out) == {"ts"} | set(wm.FIELDS)
    assert out["ambient_c"] == 13.5
    assert "extra" not in out


def test_clean_payload_ranges():
    now = time.time()
    out = wm.clean_payload(reading(ts=int(now), ambient_c=-200, humidity_pct=101.2,
                                   irradiance_wm2=-3, pyranometer_wm2=float("nan"),
                                   wind_ms="junk", pressure_hpa=93066), now)
    assert out["ambient_c"] is None          # out of range
    assert out["humidity_pct"] == 100.0      # clamped
    assert out["irradiance_wm2"] == 0.0      # small night negative clamped
    assert out["pyranometer_wm2"] is None    # NaN
    assert out["wind_ms"] is None            # not a number
    assert out["pressure_hpa"] is None       # Pa sent by mistake, out of range


@pytest.mark.parametrize("bad", [None, [], "x", {"ambient_c": 1}])
def test_clean_payload_rejects_bad_shape(bad):
    with pytest.raises(ValueError):
        wm.clean_payload(bad, time.time())


def test_clean_payload_rejects_old_or_future_ts():
    now = time.time()
    with pytest.raises(ValueError):
        wm.clean_payload(reading(ts=int(now - 3 * 3600)), now)
    with pytest.raises(ValueError):
        wm.clean_payload(reading(ts=int(now + 3 * 3600)), now)


def test_clean_payload_rejects_all_empty():
    now = time.time()
    with pytest.raises(ValueError):
        wm.clean_payload({"ts": int(now)}, now)


def test_push_requires_key(client):
    assert push(client, reading(), key="").status_code == 401
    assert push(client, reading(), key="wrong").status_code == 401


def test_push_rejected_when_server_has_no_key(client, monkeypatch):
    monkeypatch.setattr(wm, "API_KEY", "")
    assert push(client, reading(), key="").status_code == 401


def test_push_bad_payload_is_400(client):
    r = client.post("/api/push", data="not json", headers={"X-API-Key": KEY,
                                                           "Content-Type": "application/json"})
    assert r.status_code == 400


def test_push_then_current(client):
    p = reading()
    r = push(client, p)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "stored": True}
    c = client.get("/api/current")
    assert c.headers["Cache-Control"] == "no-store"
    body = c.get_json()
    assert body["ts"] == p["ts"]
    assert 590 <= body["age_s"] <= 620
    assert body["stale"] is False
    assert body["readings"]["ambient_c"] == 13.5


def test_duplicate_ts_stored_once(client):
    p = reading()
    assert push(client, p).get_json()["stored"] is True
    assert push(client, p).get_json()["stored"] is False
    with wm.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_older_push_does_not_replace_latest(client):
    now = int(time.time())
    push(client, reading(ts=now - 300, ambient_c=15.0))
    push(client, reading(ts=now - 1200, ambient_c=11.0))
    assert client.get("/api/current").get_json()["readings"]["ambient_c"] == 15.0


def test_current_stale_after_45_min(client):
    push(client, reading(ts=int(time.time()) - 50 * 60))
    assert client.get("/api/current").get_json()["stale"] is True


def test_current_with_nothing(client):
    body = client.get("/api/current").get_json()
    assert body["ts"] is None and body["stale"] is True and body["readings"] == {}


def test_current_falls_back_to_db(client):
    p = reading()
    push(client, p)
    wm._latest["payload"] = None  # simulate a restart
    body = client.get("/api/current").get_json()
    assert body["ts"] == p["ts"] and body["readings"]["wind_ms"] == 2.1


def test_prune_drops_old_rows(client):
    now = time.time()
    with wm.db() as conn:
        conn.execute("INSERT INTO samples (ts, ambient) VALUES (?, ?)", (int(now - 40 * 86400), 1.0))
        conn.execute("INSERT INTO samples (ts, ambient) VALUES (?, ?)", (int(now - 60), 2.0))
    wm.prune(now)
    with wm.db() as conn:
        assert [r[0] for r in conn.execute("SELECT ambient FROM samples")] == [2.0]
```

- [ ] **Step 3: Run the tests and confirm they fail**

Run: `weather_monitor/venv/bin/python -m pytest weather_monitor/tests/test_app.py -q`
Expected: collection ERROR, `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 4: Write the implementation**

`weather_monitor/app.py`:

```python
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
import logging
import math
import os
import sqlite3
import threading
import time

from flask import Flask, abort, jsonify, render_template, request

log = logging.getLogger("weather")

# payload key: (sqlite column, min, max). Out-of-range values become None.
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
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `weather_monitor/venv/bin/python -m pytest weather_monitor/tests/test_app.py -q`
Expected: all pass (the parametrised test counts 4).

- [ ] **Step 6: Commit**

```bash
git add .gitignore weather_monitor/app.py weather_monitor/requirements.txt weather_monitor/tests/test_app.py
git commit -m "weather_monitor: accept Meteostation pushes from desky, store one row per reading

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: History endpoint for the trend chart

**Files:**
- Modify: `weather_monitor/app.py` (add a route after `api_current`)
- Test: `weather_monitor/tests/test_app.py` (append)

**Interfaces:**
- Consumes: `db()`, `FIELDS`, `MAX_HISTORY_HOURS` from Task 1.
- Produces: `GET /api/history?hours=N` returns `{"start": int, "end": int, "columns": ["t", <FIELDS keys…>], "rows": [[t, …], …]}`, oldest first. `hours` is clamped to 1..168 and defaults to 24 when invalid. Header `Cache-Control: public, max-age=55`.

- [ ] **Step 1: Write the failing tests** (append to `test_app.py`)

```python
def test_history_window_and_columns(client):
    now = int(time.time())
    for age_min in (30 * 60, 90, 60, 15):  # 30 h ago, then the last 90 min
        with wm.db() as conn:
            conn.execute("INSERT INTO samples (ts, ambient, irradiance) VALUES (?, ?, ?)",
                         (now - age_min * 60, float(age_min), 100.0))
    body = client.get("/api/history?hours=24").get_json()
    assert body["columns"] == ["t"] + list(wm.FIELDS)
    assert [r[0] for r in body["rows"]] == [now - 90 * 60, now - 60 * 60, now - 15 * 60]
    amb = body["columns"].index("ambient_c")
    assert body["rows"][0][amb] == 90.0
    assert body["end"] - body["start"] == 24 * 3600


@pytest.mark.parametrize("arg,hours", [("0", 1), ("9999", 168), ("abc", 24), ("", 24)])
def test_history_hours_clamped(client, arg, hours):
    body = client.get("/api/history?hours=" + arg).get_json()
    assert body["end"] - body["start"] == hours * 3600
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `weather_monitor/venv/bin/python -m pytest weather_monitor/tests/test_app.py -q -k history`
Expected: FAIL. The route returns 404, so `get_json()` is `None` and raises a `TypeError`.

- [ ] **Step 3: Implement** (insert after `api_current` in `app.py`)

```python
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
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `weather_monitor/venv/bin/python -m pytest weather_monitor/tests/test_app.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add weather_monitor/app.py weather_monitor/tests/test_app.py
git commit -m "weather_monitor: /api/history for the 24 h trend chart

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Open-Meteo 7-day forecast

**Files:**
- Modify: `weather_monitor/app.py` (add a forecast section above `# --- routes`, a route, and start the thread in `main()`)
- Test: `weather_monitor/tests/test_app.py` (append)

**Interfaces:**
- Produces:
  - `parse_forecast(js: dict) -> list[dict]`. Each day is `{"date": "YYYY-MM-DD", "code": int|None, "tmax", "tmin", "rain_pct", "rain_mm", "wind_kmh", "gust_kmh": float|None}`.
  - `fetch_forecast() -> list[dict]` (network).
  - `_forecast = {"days": [], "fetched_at": None, "error": None}`.
  - `refresh_forecast(fetch=fetch_forecast) -> bool` runs one attempt, updates `_forecast`, and keeps the old days on error.
  - `forecast_loop()` is the daemon thread body.
  - `GET /api/forecast` returns `{"days": [...], "fetched_at": int|None, "error": str|None}`.

- [ ] **Step 1: Write the failing tests** (append)

```python
OM = {
    "daily": {
        "time": ["2026-10-10", "2026-10-11"],
        "weather_code": [3, 61],
        "temperature_2m_max": [17.2, 14.0],
        "temperature_2m_min": [8.1, 9.4],
        "precipitation_probability_max": [10, 80],
        "precipitation_sum": [0.0, 6.2],
        "wind_speed_10m_max": [18.0, 30.5],
        "wind_gusts_10m_max": [35.0, 61.2],
    }
}


def test_parse_forecast():
    days = wm.parse_forecast(OM)
    assert days[1] == {"date": "2026-10-11", "code": 61, "tmax": 14.0, "tmin": 9.4,
                       "rain_pct": 80.0, "rain_mm": 6.2, "wind_kmh": 30.5, "gust_kmh": 61.2}


def test_parse_forecast_handles_nulls():
    js = {"daily": {"time": ["2026-10-10"], "weather_code": [None],
                    "temperature_2m_max": [16.0]}}  # other arrays missing
    d = wm.parse_forecast(js)[0]
    assert d["code"] is None and d["tmax"] == 16.0 and d["rain_mm"] is None


def test_parse_forecast_rejects_garbage():
    with pytest.raises(ValueError):
        wm.parse_forecast({"error": True, "reason": "bad"})


def test_refresh_keeps_last_good_on_error(monkeypatch):
    monkeypatch.setattr(wm, "_forecast", {"days": [], "fetched_at": None, "error": None})
    assert wm.refresh_forecast(lambda: wm.parse_forecast(OM)) is True
    good = wm._forecast["days"]

    def boom():
        raise OSError("timeout")
    assert wm.refresh_forecast(boom) is False
    assert wm._forecast["days"] == good and "timeout" in wm._forecast["error"]


def test_forecast_route(client, monkeypatch):
    monkeypatch.setattr(wm, "_forecast", {"days": wm.parse_forecast(OM), "fetched_at": 123, "error": None})
    body = client.get("/api/forecast").get_json()
    assert len(body["days"]) == 2 and body["fetched_at"] == 123
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `weather_monitor/venv/bin/python -m pytest weather_monitor/tests/test_app.py -q -k forecast`
Expected: FAIL with `AttributeError: module 'app' has no attribute 'parse_forecast'`

- [ ] **Step 3: Implement**

Add `import json`, `import urllib.parse` and `import urllib.request` to the imports. Then insert this block before `# --- routes`:

```python
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
```

Add this route after `api_history`:

```python
@app.route("/api/forecast")
def api_forecast():
    with _lock:
        snap = dict(_forecast)
    resp = jsonify(snap)
    resp.headers["Cache-Control"] = "public, max-age=55"
    return resp
```

In `main()`, after `prune(time.time())`:

```python
    threading.Thread(target=forecast_loop, name="forecast", daemon=True).start()
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `weather_monitor/venv/bin/python -m pytest weather_monitor/tests/test_app.py -q`
Expected: all pass

- [ ] **Step 5: Do a live smoke test against Open-Meteo**

Run: `cd weather_monitor && venv/bin/python -c "import app; d=app.fetch_forecast(); print(len(d), d[0])"; cd ..`
Expected: `7 {'date': '2026-10-10', 'code': …, 'tmax': …}` with real numbers.

- [ ] **Step 6: Commit**

```bash
git add weather_monitor/app.py weather_monitor/tests/test_app.py
git commit -m "weather_monitor: 7-day Open-Meteo forecast, refreshed every 15 min

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The kiosk page

**Files:**
- Create: `weather_monitor/templates/weather.html`
- Modify: `weather_monitor/app.py` (add the `/` route)
- Test: `weather_monitor/tests/test_app.py` (append) plus a visual check in Chrome

**Interfaces:**
- Consumes: `GET api/current`, `api/history?hours=24` and `api/forecast`, using the shapes from Tasks 1–3. URLs are relative, so they work under `/weather/`.
- Produces: `GET /` renders `weather.html`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Lodge weather" in html and 'fetch(u' in html
    assert '"/api/' not in html  # relative URLs only, so it works under /weather/
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `weather_monitor/venv/bin/python -m pytest weather_monitor/tests/test_app.py -q -k index`
Expected: FAIL, 404

- [ ] **Step 3: Add the route** (after `api_forecast`)

```python
@app.route("/")
def index():
    return render_template("weather.html")
```

- [ ] **Step 4: Write `weather_monitor/templates/weather.html`**

Copy the kiosk's icon helpers **verbatim** from `~/Code/soc_traffic_light/templates/kiosk.html`, lines 431–455 (from `/* ---------------- weather icons ---------------- */` through the `wxIcon` line: `CLOUD_HI`, `CLOUD`, `SUN`, `WX`, `WMO`, `wxIcon`). Paste them at the marked spot below. Everything else is given in full:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Lodge weather</title>
<style>
  /* Theme copied from soc_traffic_light/templates/kiosk.html; keep the two in step. */
  :root { --bg:#0e1116; --card:#171b22; --card2:#1f2530; --fg:#eef1f5; --mute:#8b95a5; --line:#2a313c;
          --green:#22c55e; --orange:#f59e0b; --red:#ef4444; --blue:#60a5fa; --sun:#fbbf24; }
  * { box-sizing:border-box; margin:0; }
  html, body { height:100%; background:var(--bg); color:var(--fg); font-family:system-ui, sans-serif; overflow:hidden; }
  body { display:grid; grid-template-rows:64px 1fr 30px; padding:16px; gap:14px; }
  body.fit { position:absolute; left:0; top:0; width:1920px; height:1080px; transform-origin:0 0; }
  header { display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--line); font-size:30px; }
  main { display:grid; grid-template-columns:620px 1fr; grid-template-rows:minmax(0,1fr) 360px; gap:14px; min-height:0;
         grid-template-areas:"now trend" "days days"; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px 22px;
          overflow:hidden; display:flex; flex-direction:column; min-height:0; }
  .card h2 { font-size:19px; font-weight:500; color:var(--mute); margin-bottom:10px; letter-spacing:.3px;
             display:flex; align-items:center; gap:10px; }
  .num { font-variant-numeric:tabular-nums; }
  .sub { color:var(--mute); font-size:18px; }
  .stale { opacity:.45; }
  svg.i { width:24px; height:24px; flex:none; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }

  #now { grid-area:now; }
  .big { font-size:150px; font-weight:300; line-height:1; margin:8px 0 4px; }
  .big small { font-size:60px; color:var(--mute); }
  .stats { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:auto; }
  .stat { background:var(--card2); border-radius:12px; padding:12px 16px; }
  .stat .v { font-size:38px; }
  .stat .v small { font-size:20px; color:var(--mute); }
  .stat .l { color:var(--mute); font-size:17px; margin-top:2px; }

  #trend { grid-area:trend; }
  #chart { flex:1; width:100%; min-height:0; }
  #chart text { fill:var(--mute); font-size:16px; font-family:system-ui, sans-serif; }
  #chart .grid { stroke:var(--line); stroke-width:1; }
  #chart .temp { fill:none; stroke:var(--orange); stroke-width:3; stroke-linejoin:round; }
  #chart .sunarea { fill:var(--sun); fill-opacity:.18; stroke:var(--sun); stroke-opacity:.6; stroke-width:1.5; }
  .legend { display:flex; gap:22px; font-size:17px; color:var(--mute); }
  .legend b { display:inline-block; width:22px; height:4px; border-radius:2px; vertical-align:middle; margin-right:8px; }

  #days { grid-area:days; }
  .week { flex:1; display:grid; grid-template-columns:repeat(7, 1fr); gap:12px; min-height:0; }
  .day { background:var(--card2); border-radius:12px; padding:12px; display:flex; flex-direction:column;
         align-items:center; justify-content:space-between; text-align:center; }
  .day .name { font-size:24px; }
  .day svg.wx { width:72px; height:72px; }
  .day .desc { color:var(--mute); font-size:17px; min-height:22px; }
  .day .t { font-size:34px; }
  .day .t .lo { color:var(--mute); font-size:26px; }
  .day .rain { color:var(--blue); font-size:18px; }
  .day .wind { color:var(--mute); font-size:17px; }

  footer { display:flex; justify-content:space-between; align-items:center; color:var(--mute); font-size:15px; }

  @media (max-width: 900px), (orientation: portrait) {
    html, body { height:auto; overflow:auto; }
    body { display:block; }
    main { display:flex; flex-direction:column; }
    #chart { height:280px; flex:none; }
    .big { font-size:96px; }
    .week { grid-template-columns:repeat(2, 1fr); }
    .day svg.wx { width:48px; height:48px; }
  }
</style>
</head>
<body>
<header>
  <div>Lodge weather</div>
  <div id="clock" class="num"></div>
</header>
<main>
  <section class="card" id="now">
    <h2><svg class="i" viewBox="0 0 24 24"><path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"/></svg>Right now at Moora Moora</h2>
    <div class="big num" id="amb">—<small>°C</small></div>
    <div class="sub" id="now-sub">Waiting for the Meteostation</div>
    <div class="stats">
      <div class="stat"><div class="v num" id="wind">—</div><div class="l" id="wind-l">Wind</div></div>
      <div class="stat"><div class="v num" id="hum">—</div><div class="l">Humidity</div></div>
      <div class="stat"><div class="v num" id="sun">—</div><div class="l" id="sun-l">Sunlight</div></div>
      <div class="stat"><div class="v num" id="pres">—</div><div class="l">Station pressure</div></div>
    </div>
  </section>
  <section class="card" id="trend">
    <h2><svg class="i" viewBox="0 0 24 24"><path d="M3 17l6-6 4 4 8-8"/></svg>Last 24 hours
      <span class="legend" style="margin-left:auto"><span><b style="background:var(--orange)"></b>Temperature</span><span><b style="background:var(--sun)"></b>Sunlight</span></span></h2>
    <svg id="chart"></svg>
  </section>
  <section class="card" id="days">
    <h2><svg class="i" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>Next 7 days <span class="sub" id="fc-note" style="margin-left:auto"></span></h2>
    <div class="week" id="week"></div>
  </section>
</main>
<footer>
  <div id="upd">Meteostation (SMA Sunny SensorBox), 15-minute readings</div>
  <div>Forecast: Open-Meteo</div>
</footer>
<script>
const $ = id => document.getElementById(id);
const nf = u => fetch(u + (u.includes("?") ? "&" : "?") + "_=" + Date.now(), {cache:"no-store"}).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
const fmt = (v, d=0) => v == null ? "—" : Number(v).toFixed(d);
const hhmm = ts => new Date(ts*1000).toLocaleTimeString("en-AU", {hour:"numeric", minute:"2-digit"});

/* PASTE kiosk.html lines 431-455 here: CLOUD_HI, CLOUD, SUN, WX, WMO, wxIcon */

const BEAUFORT = [[1,"Calm"],[6,"Light air"],[12,"Light breeze"],[20,"Gentle breeze"],[29,"Moderate breeze"],
  [39,"Fresh breeze"],[50,"Strong breeze"],[62,"Near gale"],[75,"Gale"],[89,"Strong gale"],[103,"Storm"],
  [118,"Violent storm"],[Infinity,"Hurricane"]];
const beaufort = kmh => BEAUFORT.find(([lim]) => kmh < lim)[1];

function tickClock(){
  $("clock").textContent = new Date().toLocaleString("en-AU", {weekday:"long", day:"numeric", month:"long", hour:"numeric", minute:"2-digit"});
}

async function pollCurrent(){
  try {
    const c = await nf("api/current"), r = c.readings || {};
    $("now").classList.toggle("stale", !!c.stale);
    $("amb").innerHTML = `${fmt(r.ambient_c, 1)}<small>°C</small>`;
    const kmh = r.wind_ms == null ? null : r.wind_ms * 3.6;
    $("wind").innerHTML = kmh == null ? "—" : `${fmt(kmh)}<small> km/h</small>`;
    $("wind-l").textContent = kmh == null ? "Wind" : `Wind · ${beaufort(kmh)}`;
    $("hum").innerHTML = r.humidity_pct == null ? "—" : `${fmt(r.humidity_pct)}<small> %</small>`;
    $("sun").innerHTML = r.irradiance_wm2 == null ? "—" : `${fmt(r.irradiance_wm2)}<small> W/m²</small>`;
    $("sun-l").textContent = r.clearness_index == null || !r.irradiance_wm2 ? "Sunlight"
      : `Sunlight · ${fmt(Math.min(r.clearness_index, 1) * 100)}% of clear sky`;
    $("pres").innerHTML = r.pressure_hpa == null ? "—" : `${fmt(r.pressure_hpa)}<small> hPa</small>`;
    $("now-sub").textContent = c.ts == null ? "Waiting for the Meteostation"
      : (r.module_c == null ? "" : `Solar panels ${fmt(r.module_c)}°C · `) + `measured ${hhmm(c.ts)}` + (c.stale ? " (old reading)" : "");
    $("upd").textContent = c.ts == null ? "Meteostation: no reading yet" : `Meteostation · updated ${hhmm(c.ts)}`;
  } catch(e){ console.warn(e); $("now").classList.add("stale"); }
}

// Split a series into runs of [t, v], breaking at nulls and gaps over 45 min.
function runs(rows, ti, vi){
  const out = []; let cur = [], prevT = null;
  for (const r of rows){
    const t = r[ti], v = r[vi];
    if (v == null || (prevT != null && t - prevT > 2700)) { if (cur.length) out.push(cur); cur = []; }
    if (v != null) cur.push([t, v]);
    prevT = t;
  }
  if (cur.length) out.push(cur);
  return out;
}

function drawChart(h){
  const svg = $("chart"), W = svg.clientWidth, H = svg.clientHeight;
  const L = 56, R = 70, T = 14, B = 34, pw = W - L - R, ph = H - T - B;
  const ti = h.columns.indexOf("t"), ai = h.columns.indexOf("ambient_c"), si = h.columns.indexOf("irradiance_wm2");
  const temps = h.rows.map(r => r[ai]).filter(v => v != null);
  if (!temps.length){
    svg.innerHTML = `<text x="${W/2}" y="${H/2}" text-anchor="middle">No Meteostation readings in the last 24 hours</text>`;
    return;
  }
  const tLo = Math.floor(Math.min(...temps) - 1), tHi = Math.ceil(Math.max(...temps) + 1);
  const sHi = Math.max(1000, ...h.rows.map(r => r[si] || 0));
  const x = t => L + (t - h.start) / (h.end - h.start) * pw;
  const yT = v => T + (1 - (v - tLo) / (tHi - tLo)) * ph;
  const yS = v => T + (1 - v / sHi) * ph;
  let g = "";
  const step = Math.max(1, Math.ceil((tHi - tLo) / 5));
  for (let v = tLo; v <= tHi; v += step)
    g += `<line class="grid" x1="${L}" x2="${L+pw}" y1="${yT(v)}" y2="${yT(v)}"/><text x="${L-10}" y="${yT(v)+5}" text-anchor="end">${v}°</text>`;
  for (const v of [0, 500, 1000]) if (v <= sHi)
    g += `<text x="${L+pw+10}" y="${yS(v)+5}">${v}</text>`;
  g += `<text x="${L+pw+10}" y="${T+ph+28}">W/m²</text>`;
  for (let t = Math.ceil(h.start/3600)*3600; t <= h.end; t += 3600){
    const hr = new Date(t*1000).getHours();
    if (hr % 3) continue;
    g += `<line class="grid" x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${T+ph}" stroke-dasharray="2 6"/>` +
         `<text x="${x(t)}" y="${T+ph+26}" text-anchor="middle">${new Date(t*1000).toLocaleTimeString("en-AU",{hour:"numeric"})}</text>`;
  }
  for (const run of runs(h.rows, ti, si)){
    const pts = run.map(([t, v]) => `${x(t).toFixed(1)},${yS(v).toFixed(1)}`).join(" L");
    g += `<path class="sunarea" d="M${x(run[0][0]).toFixed(1)},${yS(0)} L${pts} L${x(run[run.length-1][0]).toFixed(1)},${yS(0)} Z"/>`;
  }
  for (const run of runs(h.rows, ti, ai))
    g += `<path class="temp" d="M${run.map(([t, v]) => `${x(t).toFixed(1)},${yT(v).toFixed(1)}`).join(" L")}"/>`;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = g;
}

let lastHistory = null;
async function pollHistory(){
  try { lastHistory = await nf("api/history?hours=24"); drawChart(lastHistory); }
  catch(e){ console.warn(e); }
}

async function pollForecast(){
  try {
    const f = await nf("api/forecast");
    const today = new Date().toLocaleDateString("en-CA");  // YYYY-MM-DD, local
    $("week").innerHTML = (f.days || []).slice(0, 7).map(d => {
      const name = d.date === today ? "Today"
        : new Date(d.date + "T12:00:00").toLocaleDateString("en-AU", {weekday:"short", day:"numeric"});
      const rain = d.rain_pct == null ? "" : `${fmt(d.rain_pct)}%` + (d.rain_mm ? ` · ${fmt(d.rain_mm, d.rain_mm < 10 ? 1 : 0)} mm` : "");
      const wind = d.wind_kmh == null ? "" : `Wind ${fmt(d.wind_kmh)}` + (d.gust_kmh == null ? "" : `, gusts ${fmt(d.gust_kmh)}`) + " km/h";
      return `<div class="day"><div class="name">${name}</div>${wxIcon(d.code)}
        <div class="desc">${(WMO[d.code]||[,""])[1]}</div>
        <div class="t num">${fmt(d.tmax)}° <span class="lo">${fmt(d.tmin)}°</span></div>
        <div class="rain num">${rain || "&nbsp;"}</div><div class="wind num">${wind || "&nbsp;"}</div></div>`;
    }).join("") || `<div class="sub">Forecast not available yet</div>`;
    $("fc-note").textContent = f.error && f.fetched_at ? `forecast as of ${hhmm(f.fetched_at)}` : "";
    $("days").classList.toggle("stale", !!f.error);
  } catch(e){ console.warn(e); $("days").classList.add("stale"); }
}

// Same 1920x1080 scaling as the kiosk (soc_traffic_light/templates/kiosk.html fitKiosk()).
const MOBILE = matchMedia("(max-width: 900px), (orientation: portrait)");
function fitPage(){
  const b = document.body;
  if (MOBILE.matches) { b.classList.remove("fit"); b.style.transform = ""; }
  else {
    const s = Math.min(innerWidth/1920, innerHeight/1080);
    b.classList.add("fit");
    b.style.transform = `translate(${(innerWidth-1920*s)/2}px, ${(innerHeight-1080*s)/2}px) scale(${s})`;
  }
  if (lastHistory) drawChart(lastHistory);
}
addEventListener("resize", fitPage); fitPage();

tickClock(); setInterval(tickClock, 15000);
pollCurrent(); setInterval(pollCurrent, 60000);
pollHistory(); setInterval(pollHistory, 300000);
pollForecast(); setInterval(pollForecast, 900000);
</script>
</body>
</html>
```

After pasting, check that `fetch(u` and `api/` appear only in their relative forms: `grep -n '"/api' weather_monitor/templates/weather.html` should print nothing.

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `weather_monitor/venv/bin/python -m pytest weather_monitor/tests/test_app.py -q`
Expected: all pass

- [ ] **Step 6: Check it visually with local data**

Start the app with a throwaway DB and key, seed 24 h of plausible readings, then view it:

```bash
cd weather_monitor
WEATHER_API_KEY=dev WEATHER_DB=/tmp/wx-dev.db venv/bin/python app.py --port 8130 &
sleep 2
python3 - <<'PY'
import json, math, time, urllib.request
now = int(time.time())
import sqlite3
c = sqlite3.connect("/tmp/wx-dev.db")
for i in range(96, 0, -1):
    t = now - i * 900; hr = time.localtime(t).tm_hour + time.localtime(t).tm_min / 60
    sun = max(0, 900 * math.sin(math.pi * (hr - 6.5) / 12.5)) if 6.5 < hr < 19 else 0
    c.execute("INSERT OR IGNORE INTO samples (ts, ambient, irradiance, wind, humidity, pressure, clearness) VALUES (?,?,?,?,?,?,?)",
              (t, 9 + 6 * math.sin(math.pi * (hr - 9) / 12), sun, 2.5, 70, 931, 0.75))
c.commit()
req = urllib.request.Request("http://127.0.0.1:8130/api/push", method="POST",
    data=json.dumps({"ts": now - 300, "ambient_c": 13.5, "module_c": 26.3, "irradiance_wm2": 569,
                     "wind_ms": 2.1, "humidity_pct": 72, "pressure_hpa": 930.7, "clearness_index": 0.78}).encode(),
    headers={"X-API-Key": "dev", "Content-Type": "application/json"})
print(urllib.request.urlopen(req).read())
PY
cd ..
```

Open `http://127.0.0.1:8130/` in Chrome at a 1920×1080 window (claude-in-chrome: `resize_window` to 1920×1080, then a screenshot). Check:
- the Now panel shows 13.5 °C, a 2.1 m/s ≈ 8 km/h "Light breeze", 72 %, 569 W/m² "78 % of clear sky" and 931 hPa;
- the chart has an orange temperature line and a yellow sunlight area, with 3-hourly labels;
- seven day cards appear (from the real Open-Meteo, so the thread is running);
- nothing overflows its card.

Then resize to 390×844 and confirm the single-column layout scrolls. Push an old reading (`"ts": now - 3600` with a fresh DB) and confirm the Now panel dims and shows "(old reading)". Stop the server with `kill %1` and delete `/tmp/wx-dev.db*`.

- [ ] **Step 7: Commit**

```bash
git add weather_monitor/app.py weather_monitor/templates/weather.html weather_monitor/tests/test_app.py
git commit -m "weather_monitor: kiosk page with now panel, 24 h trend and 7-day forecast

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: pignus install script, Apache route, docs

**Files:**
- Create: `weather_monitor/install.sh`
- Create: `weather_monitor/README.md`
- Modify: `server/monitor.mooramoora.org.au.conf` (insert before line 79, `ProxyPass / http://127.0.0.1:8765/`)
- Modify: `CLAUDE.md` (the hosts table row for pignus, and the component notes near the `comfort_monitor/` bullet)

**Interfaces:**
- Consumes: `app.py` `--host/--port` and the env vars `WEATHER_API_KEY`, `WEATHER_DB`.
- Produces: `weather-monitor.service` on 127.0.0.1:8130 and the public path `/weather/`.

- [ ] **Step 1: Write `weather_monitor/install.sh`**

```bash
#!/usr/bin/env bash
#
# Install Lodge Weather (Meteostation + forecast page for the kiosk) as a systemd service on pignus.
#
# Usage (from this directory, as the deploying user):
#   sudo bash install.sh            # first install: generates the push key
#   sudo bash install.sh --port 8130
#
# The push key lives in /etc/weather-monitor.env (root-only). desky's weather-push
# service needs the same value as WEATHER_API_KEY in /etc/weather-push.env.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="weather-monitor.service"
SERVICE_USER="${SUDO_USER:-$USER}"
ENV_FILE="/etc/weather-monitor.env"
PORT="8130"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo ">> App dir: $APP_DIR"
echo ">> Listen:  127.0.0.1:$PORT (Apache proxies /weather/)"
echo ">> User:    $SERVICE_USER"

echo "[1/4] Python virtual environment"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
sudo -u "$SERVICE_USER" mkdir -p "$APP_DIR/data"

echo "[2/4] Push key ($ENV_FILE)"
if [[ ! -f "$ENV_FILE" ]]; then
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  umask 077
  printf 'WEATHER_API_KEY=%s\n' "$KEY" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "   Generated a new key. Copy it into desky's /etc/weather-push.env:"
  echo "   sudo grep WEATHER_API_KEY $ENV_FILE"
else
  echo "   Keeping existing key."
fi

echo "[3/4] systemd unit"
cat > "/etc/systemd/system/$SERVICE_NAME" <<UNIT
[Unit]
Description=Lodge Weather (Meteostation + forecast) page
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=WEATHER_DB=$APP_DIR/data/weather.db
ExecStart=$APP_DIR/venv/bin/python app.py --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "[4/4] Check"
sleep 2
curl -fsS "http://127.0.0.1:$PORT/healthz" && echo
echo
echo "Done. Apache needs the /weather/ ProxyPass from server/monitor.mooramoora.org.au.conf:"
echo "   sudo cp ../server/monitor.mooramoora.org.au.conf /etc/apache2/sites-available/ && sudo apachectl configtest && sudo systemctl reload apache2"
```

Then run `chmod +x weather_monitor/install.sh && bash -n weather_monitor/install.sh`. Expected: no output.

- [ ] **Step 2: Add the Apache block**

Insert this immediately above `    ProxyPass        /           http://127.0.0.1:8765/` in `server/monitor.mooramoora.org.au.conf`:

```apache
    # Lodge Weather (weather_monitor/app.py, weather-monitor.service :8130; Meteostation data
    # pushed from desky). Must sit above the catch-all "ProxyPass / ..." line.
    RewriteRule ^/weather$ /weather/ [R=301,L]
    ProxyPass        /weather/   http://127.0.0.1:8130/
    ProxyPassReverse /weather/   http://127.0.0.1:8130/
```

Check: `grep -n -E "weather|ProxyPass +/ " server/monitor.mooramoora.org.au.conf`. The `/weather/` lines must come before the `ProxyPass /` line.

- [ ] **Step 3: Write `weather_monitor/README.md`**

```markdown
# Lodge Weather

Full-screen weather page for the Lodge kiosk (`/weather/` on monitor.mooramoora.org.au, shown in
`/kiosk`'s 30 s rotation from the `soc_traffic_light` repo).

- **Live:** SMA Meteostation / Sunny SensorBox readings. The Sunny WebBox FTP-pushes 15-minute means to
  desky, `tracker_analysis` decodes them, and `tracker_analysis/weather_push.py` POSTs them here every
  60 s (`X-API-Key`). One SQLite row per reading time, kept 35 days.
- **Forecast:** Open-Meteo 7-day daily forecast for Mount Toolebewong, refreshed every 15 min. The last
  good forecast is kept if a fetch fails.
- Pressure is the station's absolute pressure (≈ 930 hPa at the lodge), not sea-level.

## API
- `POST /api/push`: `{"ts": epoch, "ambient_c", "module_c", "irradiance_wm2", "pyranometer_wm2", "wind_ms",
  "humidity_pct", "pressure_hpa", "clearness_index"}`. Unknown keys are dropped. Out-of-range values become null.
- `GET /api/current`: latest reading, `age_s`, and `stale` (older than 45 min).
- `GET /api/history?hours=24` (max 168)
- `GET /api/forecast`: `{"days": [...], "fetched_at", "error"}`

## Install (pignus)
    cd ~/microgrid_remote_monitor/weather_monitor && sudo bash install.sh
Then add the Apache block (see the end of install.sh) and give desky the key (see `tracker_analysis/install_weather_push.sh`).

## Tests
    python3 -m venv venv && venv/bin/pip install flask pytest
    venv/bin/python -m pytest tests -q
```

- [ ] **Step 4: Update `CLAUDE.md`**

In the hosts table, append this to the pignus row's unit list: `, weather-monitor.service (weather_monitor/app.py, :8130, /weather/)`. Add a row:

```markdown
| `desky` (home Pi) | `~/microgrid_remote_monitor` | `tracker-analysis.service` (`tracker_analysis/app.py`, :8901), `weather-push.service` (`tracker_analysis/weather_push.py`) |
```

(First check the existing tracker_analysis unit name on desky with `ssh desky systemctl list-units --type=service | grep -i tracker`, and use the real name.)

After the `octagon_monitor/` bullet, add:

```markdown
- `weather_monitor/` is the Lodge weather page (`/weather/`), shown in the `/kiosk` rotation. desky's `tracker_analysis/weather_push.py` pushes the Meteostation readings (from the WebBox FTP feed) every 60 s, and the app fetches a 7-day Open-Meteo forecast itself. Tests: `weather_monitor/venv/bin/python -m pytest weather_monitor/tests tracker_analysis/tests -q`. See its README.
```

- [ ] **Step 5: Commit**

```bash
git add weather_monitor/install.sh weather_monitor/README.md server/monitor.mooramoora.org.au.conf CLAUDE.md
git commit -m "weather_monitor: pignus installer, Apache /weather/ route, docs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: desky pusher

**Files:**
- Create: `tracker_analysis/weather_push.py`
- Create: `tracker_analysis/install_weather_push.sh`
- Create: `tracker_analysis/tests/test_weather_push.py`

**Interfaces:**
- Consumes: tracker_analysis `GET /api/state`, which returns `{"live": {"amb_temp_c", "mod_temp_c", "poa_wm2", "pyranometer_wm2", "wind_ms", "humidity_pct", "air_pressure_pa", "clearness_index", …}, "sma_status": {"connected": bool, "last_read": naive local ISO str|None, "age_s": float|None, …}}`. `last_read` is `datetime.now().isoformat()` on desky, so it's in desky's local time. Parse it with `datetime.fromisoformat(...).timestamp()` on the same host.
- Produces: `build_payload(state: dict) -> dict|None`, in the push shape that Task 1's `clean_payload` accepts.

- [ ] **Step 1: Write the failing tests**

`tracker_analysis/tests/test_weather_push.py`:

```python
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import weather_push as wp  # noqa: E402

LAST = "2026-10-10T10:31:55.668787"
STATE = {
    "live": {"amb_temp_c": 13.534478, "mod_temp_c": 26.271294, "poa_wm2": 569.303483,
             "pyranometer_wm2": 533.80398, "wind_ms": 2.085572, "humidity_pct": 72.272139,
             "air_pressure_pa": 93066.1692, "clearness_index": 0.78, "pv1": {"w": 1}},
    "sma_status": {"connected": True, "last_read": LAST, "age_s": 1156.8},
}


def test_build_payload_maps_fields():
    p = wp.build_payload(STATE)
    assert p == {
        "ts": int(datetime.fromisoformat(LAST).timestamp()),
        "ambient_c": 13.53, "module_c": 26.27, "irradiance_wm2": 569.3,
        "pyranometer_wm2": 533.8, "wind_ms": 2.09, "humidity_pct": 72.27,
        "pressure_hpa": 930.66, "clearness_index": 0.78,
    }


def test_ts_is_stable_across_polls():
    assert wp.build_payload(STATE)["ts"] == wp.build_payload(dict(STATE))["ts"]


def test_skips_when_disconnected_stale_or_unread():
    for sma in ({"connected": False, "last_read": LAST, "age_s": 10},
                {"connected": True, "last_read": LAST, "age_s": 1801},
                {"connected": True, "last_read": None, "age_s": None}):
        assert wp.build_payload(dict(STATE, sma_status=sma)) is None


def test_missing_live_values_become_none():
    p = wp.build_payload(dict(STATE, live={"amb_temp_c": 10.0}))
    assert p["ambient_c"] == 10.0 and p["pressure_hpa"] is None and p["wind_ms"] is None


def test_no_live_block_skips():
    assert wp.build_payload(dict(STATE, live=None)) is None
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `weather_monitor/venv/bin/python -m pytest tracker_analysis/tests/test_weather_push.py -q`
Expected: collection ERROR, `No module named 'weather_push'`

- [ ] **Step 3: Implement `tracker_analysis/weather_push.py`**

```python
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
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `weather_monitor/venv/bin/python -m pytest tracker_analysis/tests/test_weather_push.py -q`
Expected: 5 passed

- [ ] **Step 5: Check it against the live desky feed (read-only)**

Run: `curl -s http://desky.local:8901/api/state | python3 -c "import json,sys; sys.path.insert(0,'tracker_analysis'); import weather_push as w; print(w.build_payload(json.load(sys.stdin)))"`
Expected: a dict with real values and a pressure around 930. (This Mac and desky are both Melbourne time, so `ts` is right here too.)

- [ ] **Step 6: Write `tracker_analysis/install_weather_push.sh`**

```bash
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
```

Run: `chmod +x tracker_analysis/install_weather_push.sh && bash -n tracker_analysis/install_weather_push.sh`. Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add tracker_analysis/weather_push.py tracker_analysis/install_weather_push.sh tracker_analysis/tests/test_weather_push.py
git commit -m "tracker_analysis: weather_push.py sends Meteostation readings to the Lodge weather page

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Kiosk rotation (soc_traffic_light)

**Files:**
- Modify: `~/Code/soc_traffic_light/templates/kiosk.html:707-714` (the comment above `VIEW_MS` and the `views` list in `cycleViews`)
- Modify: `~/Code/soc_traffic_light/README.md` (the "Lodge kiosk" section's rotation description)

**Interfaces:**
- Consumes: the public page `/weather/` (Task 4, served via Task 5's Apache route).

- [ ] **Step 1: Edit `cycleViews`**

Replace:

```js
// Landscape (kiosk) screens rotate every 30 s: this page, the / guide, then /mesh when the URL
// carries drongo's ?mesh=<token>. Views stay loaded in iframes so they keep polling.
const VIEW_MS = 30000;
function cycleViews(){
  const views = [["/", "What the traffic light means"]];
```

with:

```js
// Landscape (kiosk) screens rotate every 30 s: this page, the / guide, /weather/ (weather_monitor in
// microgrid_remote_monitor), then /mesh when the URL carries drongo's ?mesh=<token>. Views stay
// loaded in iframes so they keep polling.
const VIEW_MS = 30000;
function cycleViews(){
  const views = [["/", "What the traffic light means"], ["/weather/", "Lodge weather"]];
```

Also update the CSS comment at about L148 from `/* The / page (traffic light guide) and, on drongo, /mesh, …` to `/* The / page (traffic light guide), /weather/ and, on drongo, /mesh, …`.

- [ ] **Step 2: Update the README**

In `README.md`'s "Lodge kiosk" section, find the sentence describing the rotation (search for `mesh` / `30 s`) and add `/weather/` (the Lodge weather page from `microgrid_remote_monitor/weather_monitor`) between the `/` guide and `/mesh`.

- [ ] **Step 3: Run the existing tests**

Run: `cd ~/Code/soc_traffic_light && (test -x venv/bin/python || python3 -m venv venv && venv/bin/pip install -q -r requirements.txt pytest) && venv/bin/python -m pytest tests/test_mesh.py -q`
Expected: pass (unaffected by this change)

- [ ] **Step 4: Commit**

```bash
cd ~/Code/soc_traffic_light
git add templates/kiosk.html README.md
git commit -m "Kiosk: add the Lodge weather page to the 30 s view rotation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Push and deploy (needs the user's go-ahead)

Ask the user before starting: "Ready to push both repos and deploy to pignus, desky and the kiosk?" Order matters. The page and route must exist before desky pushes or the kiosk links to them.

- [ ] **Step 1: Push microgrid_remote_monitor**

```bash
cd ~/Code/microgrid_remote_monitor && git push origin main
```

- [ ] **Step 2: Deploy on pignus (one SSH session, because of fail2ban)**

```bash
ssh pignus 'set -e
  ss -ltn | grep -q ":8130 " && { echo "PORT 8130 IN USE"; exit 1; } || true
  cd ~/microgrid_remote_monitor && git pull --ff-only
  cd weather_monitor && sudo bash install.sh
  sudo cp ../server/monitor.mooramoora.org.au.conf /etc/apache2/sites-available/
  sudo apachectl configtest && sudo systemctl reload apache2
  sudo grep WEATHER_API_KEY /etc/weather-monitor.env'
```

Expected: `{"latest_ts":null,"ok":true}` from the healthz check, `Syntax OK`, and the key line. If port 8130 is in use, stop and pick a free port with the user. Keep the key out of chat and logs; pass it straight to Step 3.

Before copying the Apache conf, diff it against the live file on pignus (`diff /etc/apache2/sites-available/monitor.mooramoora.org.au.conf ../server/monitor.mooramoora.org.au.conf`). If anything other than the `/weather/` block differs, stop and show the user, because the live file may hold edits that aren't in the repo.

Then check: `curl -s https://monitor.mooramoora.org.au/weather/api/forecast | head -c 300`. Expected: 7 days.

- [ ] **Step 3: Deploy on desky**

```bash
ssh desky 'cd ~/microgrid_remote_monitor && git pull --ff-only && sudo bash tracker_analysis/install_weather_push.sh --key "$(cat)"' <<< "<key from step 2>"
```

(The key goes via stdin so it stays out of shell history on desky; it is briefly visible in desky's process list as an argument to `sudo bash`, which is acceptable on that single-user Pi.) Expected: the journal shows `Pushed reading …: {'ok': True, 'stored': True}`.

Then check: `curl -s https://monitor.mooramoora.org.au/weather/api/current`. Expected: `stale: false`, and `age_s` within a few seconds of desky's `sma_status.age_s`.

- [ ] **Step 4: Push soc_traffic_light and deploy the kiosk**

```bash
cd ~/Code/soc_traffic_light && git push origin main
ssh pignus 'cd ~/soc_traffic_light && git pull --ff-only && sudo systemctl restart soc-traffic-light && sleep 2 && curl -fsS http://127.0.0.1:8765/healthz'
```

- [ ] **Step 5: Do an end-to-end visual check**

Open `https://monitor.mooramoora.org.au/weather/` at 1920×1080 in Chrome and take a screenshot to confirm live data, the chart and seven days. Then open `https://monitor.mooramoora.org.au/kiosk` and wait about 60 s to watch it cycle to the weather view. drongo reloads on its own 900 s refresh. Ask the user to glance at the lodge screen.
