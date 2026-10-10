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
