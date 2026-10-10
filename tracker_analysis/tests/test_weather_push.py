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
