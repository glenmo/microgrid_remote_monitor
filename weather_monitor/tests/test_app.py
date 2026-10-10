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


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Lodge weather" in html and 'fetch(u' in html
    assert '"/api/' not in html  # relative URLs only, so it works under /weather/
