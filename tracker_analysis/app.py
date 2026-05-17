"""tracker_analysis — bifacial vs monofacial PV tracker performance dashboard.

Pulls per-string PV data from rubberduck's Solis monitor and irradiance/
temperature data from the SMA WebBox, computes bifacial gain on the 1P and
2P trackers, and serves a live dashboard.

Run:
    python app.py --host 0.0.0.0 --port 8901
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from flask import Flask, jsonify, render_template, send_from_directory

from sma_reader import SmaReader
from solar_pos import sun_position, tracker_tilt_ns, clear_sky_ghi

log = logging.getLogger("tracker_analysis")


# ---------------------------------------------------------------------------
# Configuration (set in main(); accessed by Engine)
# ---------------------------------------------------------------------------
@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8901
    solis_url: str = "http://rubberduck.local:5000/api/solis/data"
    sma_host: str = "192.168.55.126"
    sma_port: int = 502
    sma_unit: int = 3            # SMA Meteo Station / Sensorbox unit ID (3–247)
    sma_poll: float = 5.0
    solis_poll: float = 1.0
    csv_interval: float = 5.0
    csv_dir: str = "./data"
    lat: float = -37.4
    lon: float = 144.9
    string_kwp: float = 16.965  # 13 × 435W × 3 / 1000
    site_name: str = "Arctech Solar Tracker with Longi Mono and Bifacial Module Analysis"

CONFIG = Config()

# Headline string identities — what each PV input represents.
STRING_INFO = {
    "pv1": {"tracker": "1P", "type": "bifacial",   "label": "PV1 · 1P · Bifacial"},
    "pv2": {"tracker": "1P", "type": "monofacial", "label": "PV2 · 1P · Mono"},
    "pv3": {"tracker": "2P", "type": "monofacial", "label": "PV3 · 2P · Mono"},
    "pv4": {"tracker": "2P", "type": "bifacial",   "label": "PV4 · 2P · Bifacial"},
}


# ---------------------------------------------------------------------------
# Sample container
# ---------------------------------------------------------------------------
@dataclass
class Sample:
    ts: datetime
    # Per-string
    pv1_v: Optional[float] = None
    pv1_a: Optional[float] = None
    pv1_w: Optional[float] = None
    pv2_v: Optional[float] = None
    pv2_a: Optional[float] = None
    pv2_w: Optional[float] = None
    pv3_v: Optional[float] = None
    pv3_a: Optional[float] = None
    pv3_w: Optional[float] = None
    pv4_v: Optional[float] = None
    pv4_a: Optional[float] = None
    pv4_w: Optional[float] = None
    # Weather
    poa_wm2: Optional[float] = None        # internal silicon cell (IntSolIrr)
    pyranometer_wm2: Optional[float] = None  # external pyranometer (ExlSolIrr)
    mod_temp_c: Optional[float] = None
    amb_temp_c: Optional[float] = None
    wind_ms: Optional[float] = None
    humidity_pct: Optional[float] = None
    air_pressure_pa: Optional[float] = None
    # Computed
    sun_elev_deg: float = 0.0
    sun_azim_deg: float = 0.0
    tracker_tilt_deg: float = 0.0
    clear_sky_ghi: float = 0.0
    bg_1p_pct: Optional[float] = None
    bg_2p_pct: Optional[float] = None
    # Per-string PR (instantaneous)
    pr_pv1: Optional[float] = None
    pr_pv2: Optional[float] = None
    pr_pv3: Optional[float] = None
    pr_pv4: Optional[float] = None


# ---------------------------------------------------------------------------
# CSV writer with daily rotation
# ---------------------------------------------------------------------------
class CsvWriter:
    HEADERS = [
        "timestamp",
        "pv1_v", "pv1_a", "pv1_w",
        "pv2_v", "pv2_a", "pv2_w",
        "pv3_v", "pv3_a", "pv3_w",
        "pv4_v", "pv4_a", "pv4_w",
        "poa_wm2", "pyranometer_wm2",
        "mod_temp_c", "amb_temp_c", "wind_ms",
        "humidity_pct", "air_pressure_pa",
        "sun_elev_deg", "sun_azim_deg", "tracker_tilt_deg", "clear_sky_ghi",
        "bg_1p_pct", "bg_2p_pct",
        "pr_pv1", "pr_pv2", "pr_pv3", "pr_pv4",
    ]

    def __init__(self, csv_dir: str):
        self.csv_dir = Path(csv_dir)
        self.csv_dir.mkdir(parents=True, exist_ok=True)
        self._current_date = None
        self._fh = None
        self._writer = None
        self._lock = threading.Lock()

    def _ensure_file(self, now: datetime):
        date_str = now.strftime("%Y-%m-%d")
        if date_str != self._current_date:
            if self._fh:
                self._fh.close()
            path = self.csv_dir / f"{date_str}.csv"
            new_file = not path.exists()
            self._fh = open(path, "a", newline="")
            self._writer = csv.writer(self._fh)
            if new_file:
                self._writer.writerow(self.HEADERS)
                self._fh.flush()
            self._current_date = date_str

    def write(self, sample: Sample):
        with self._lock:
            self._ensure_file(sample.ts)
            row = [
                sample.ts.isoformat(timespec="seconds"),
                sample.pv1_v, sample.pv1_a, sample.pv1_w,
                sample.pv2_v, sample.pv2_a, sample.pv2_w,
                sample.pv3_v, sample.pv3_a, sample.pv3_w,
                sample.pv4_v, sample.pv4_a, sample.pv4_w,
                sample.poa_wm2, sample.pyranometer_wm2,
                sample.mod_temp_c, sample.amb_temp_c, sample.wind_ms,
                sample.humidity_pct, sample.air_pressure_pa,
                round(sample.sun_elev_deg, 2),
                round(sample.sun_azim_deg, 2),
                round(sample.tracker_tilt_deg, 2),
                round(sample.clear_sky_ghi, 1),
                _r(sample.bg_1p_pct, 2),
                _r(sample.bg_2p_pct, 2),
                _r(sample.pr_pv1, 3),
                _r(sample.pr_pv2, 3),
                _r(sample.pr_pv3, 3),
                _r(sample.pr_pv4, 3),
            ]
            self._writer.writerow(row)
            self._fh.flush()


def _r(v, dp=2):
    return None if v is None else round(v, dp)


# ---------------------------------------------------------------------------
# Engine — background poller + KPI integrator
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self):
        self.sma = SmaReader(CONFIG.sma_host, CONFIG.sma_port,
                             unit_id=CONFIG.sma_unit,
                             poll_interval=CONFIG.sma_poll)
        self.csv = CsvWriter(CONFIG.csv_dir)

        self.latest: Optional[Sample] = None
        self.history: deque[Sample] = deque(maxlen=8640)  # ~24h at 10s
        self._history_last_ts: Optional[datetime] = None

        # Per-string Wh integrators (today)
        self.wh = {"pv1": 0.0, "pv2": 0.0, "pv3": 0.0, "pv4": 0.0}
        # POA Wh/m² integrator (today)
        self.poa_wh_m2 = 0.0
        self._prev_sample: Optional[Sample] = None
        self._today_date: Optional[str] = None

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._csv_last_write: Optional[datetime] = None

    # ---- Solis fetch ----
    def _fetch_solis(self) -> dict[str, Any]:
        try:
            r = requests.get(CONFIG.solis_url, timeout=3)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.debug(f"Solis fetch failed: {e}")
            return {}

    # ---- one tick ----
    def _tick(self):
        now = datetime.now()
        s = Sample(ts=now)

        # Per-string V/A/W from Solis
        solis = self._fetch_solis()
        for n in (1, 2, 3, 4):
            v = solis.get(f"pv{n}_voltage")
            a = solis.get(f"pv{n}_current")
            w = solis.get(f"pv{n}_power")
            if w is None and v is not None and a is not None:
                w = v * a
            setattr(s, f"pv{n}_v", v)
            setattr(s, f"pv{n}_a", a)
            setattr(s, f"pv{n}_w", w)

        # Weather from SMA
        sma_data = self.sma.get_data() or {}
        s.poa_wm2          = sma_data.get("radiation_wm2")        # internal silicon cell
        s.pyranometer_wm2  = sma_data.get("pyranometer_wm2")      # external pyranometer
        s.mod_temp_c       = sma_data.get("module_temp_c")
        s.amb_temp_c       = sma_data.get("ambient_temp_c")
        s.wind_ms          = sma_data.get("wind_speed_ms")
        s.humidity_pct     = sma_data.get("humidity_pct")
        s.air_pressure_pa  = sma_data.get("air_pressure_pa")

        # Sun position
        elev, azim = sun_position(now.astimezone(timezone.utc), CONFIG.lat, CONFIG.lon)
        s.sun_elev_deg = elev
        s.sun_azim_deg = azim
        s.tracker_tilt_deg = tracker_tilt_ns(elev, azim)
        s.clear_sky_ghi = clear_sky_ghi(elev)

        # Bifacial gain
        s.bg_1p_pct = _safe_bg(s.pv1_w, s.pv2_w)
        s.bg_2p_pct = _safe_bg(s.pv4_w, s.pv3_w)

        # Per-string instantaneous Performance Ratio
        # PR = (P_dc / P_nameplate) / (POA / 1000)
        if s.poa_wm2 and s.poa_wm2 > 50:
            for n in (1, 2, 3, 4):
                w = getattr(s, f"pv{n}_w")
                if w is not None:
                    setattr(s, f"pr_pv{n}",
                            (w / (CONFIG.string_kwp * 1000.0))
                            / (s.poa_wm2 / 1000.0))

        # Integrate Wh today
        self._integrate(s)

        # Save
        with self._lock:
            self.latest = s
            # Downsample to ~10s for history
            if (self._history_last_ts is None
                    or (now - self._history_last_ts).total_seconds() >= 10):
                self.history.append(s)
                self._history_last_ts = now

        # CSV write?
        if (self._csv_last_write is None
                or (now - self._csv_last_write).total_seconds() >= CONFIG.csv_interval):
            try:
                self.csv.write(s)
            except Exception as e:
                log.warning(f"CSV write failed: {e}")
            self._csv_last_write = now

    def _integrate(self, s: Sample):
        """Trapezoid-rule energy integration since previous sample."""
        prev = self._prev_sample
        # Reset on date roll-over
        date_str = s.ts.strftime("%Y-%m-%d")
        if self._today_date != date_str:
            self.wh = {k: 0.0 for k in self.wh}
            self.poa_wh_m2 = 0.0
            self._today_date = date_str
            prev = None  # don't bridge across midnight
        if prev is not None:
            dt_s = (s.ts - prev.ts).total_seconds()
            if 0 < dt_s < 60:
                hr = dt_s / 3600.0
                for n in (1, 2, 3, 4):
                    a = getattr(s, f"pv{n}_w") or 0.0
                    b = getattr(prev, f"pv{n}_w") or 0.0
                    self.wh[f"pv{n}"] += (a + b) / 2.0 * hr
                if s.poa_wm2 is not None and prev.poa_wm2 is not None:
                    self.poa_wh_m2 += (s.poa_wm2 + prev.poa_wm2) / 2.0 * hr
        self._prev_sample = s

    # ---- loop ----
    def _loop(self):
        log.info(f"Engine started: polling Solis {CONFIG.solis_url} every "
                 f"{CONFIG.solis_poll}s, SMA every {CONFIG.sma_poll}s, "
                 f"CSV every {CONFIG.csv_interval}s")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log.error(f"Tick error: {e}")
            self._stop.wait(CONFIG.solis_poll)

    def start(self):
        self.sma.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.sma.stop()

    # ---- API helpers ----
    def state(self) -> dict:
        with self._lock:
            s = self.latest
            history_age = (datetime.now() - self._history_last_ts).total_seconds() if self._history_last_ts else None
            return {
                "live": _sample_to_dict(s) if s else None,
                "wh_today":   dict(self.wh),
                "kwh_today":  {k: v / 1000.0 for k, v in self.wh.items()},
                "poa_wh_m2_today": self.poa_wh_m2,
                "specific_yield_today": {
                    k: (v / 1000.0) / CONFIG.string_kwp for k, v in self.wh.items()
                },
                # Today's average BG, weighted by mono energy
                "avg_bg_1p_pct": _avg_bg_today(self.wh["pv1"], self.wh["pv2"]),
                "avg_bg_2p_pct": _avg_bg_today(self.wh["pv4"], self.wh["pv3"]),
                "history_age_s": history_age,
                "sma_status":   self.sma.get_status(),
                "string_info":  STRING_INFO,
                "string_kwp":   CONFIG.string_kwp,
            }

    def history_snapshot(self) -> dict:
        with self._lock:
            entries = list(self.history)
        ts = [e.ts.isoformat() for e in entries]
        out = {"timestamps": ts}
        for k in ("pv1_w", "pv2_w", "pv3_w", "pv4_w",
                  "poa_wm2", "mod_temp_c", "amb_temp_c",
                  "bg_1p_pct", "bg_2p_pct",
                  "sun_elev_deg", "tracker_tilt_deg"):
            out[k] = [getattr(e, k) for e in entries]
        return out


def _safe_bg(bifa: Optional[float], mono: Optional[float]) -> Optional[float]:
    """Bifacial gain % = (bifa - mono) / mono * 100, with sanity floors."""
    if bifa is None or mono is None:
        return None
    if mono <= 200:  # noise floor — below this the ratio is meaningless
        return None
    return (bifa - mono) / mono * 100.0


def _avg_bg_today(bifa_wh, mono_wh):
    if mono_wh <= 100:
        return None
    return (bifa_wh - mono_wh) / mono_wh * 100.0


def _sample_to_dict(s: Sample) -> dict:
    return {
        "ts": s.ts.isoformat(),
        "pv1": {"v": s.pv1_v, "a": s.pv1_a, "w": s.pv1_w, "pr": s.pr_pv1},
        "pv2": {"v": s.pv2_v, "a": s.pv2_a, "w": s.pv2_w, "pr": s.pr_pv2},
        "pv3": {"v": s.pv3_v, "a": s.pv3_a, "w": s.pv3_w, "pr": s.pr_pv3},
        "pv4": {"v": s.pv4_v, "a": s.pv4_a, "w": s.pv4_w, "pr": s.pr_pv4},
        "poa_wm2":         s.poa_wm2,
        "pyranometer_wm2": s.pyranometer_wm2,
        "mod_temp_c":      s.mod_temp_c,
        "amb_temp_c":      s.amb_temp_c,
        "wind_ms":         s.wind_ms,
        "humidity_pct":    s.humidity_pct,
        "air_pressure_pa": s.air_pressure_pa,
        "sun_elev_deg":    round(s.sun_elev_deg, 2),
        "sun_azim_deg":    round(s.sun_azim_deg, 2),
        "tracker_tilt_deg": round(s.tracker_tilt_deg, 2),
        "clear_sky_ghi":   round(s.clear_sky_ghi, 1),
        "clearness_index": (round(s.poa_wm2 / s.clear_sky_ghi, 2)
                            if (s.poa_wm2 and s.clear_sky_ghi > 50) else None),
        "bg_1p_pct": _r(s.bg_1p_pct, 2),
        "bg_2p_pct": _r(s.bg_2p_pct, 2),
    }


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
ENGINE: Optional[Engine] = None


@app.route("/")
def dashboard():
    return render_template("index.html",
                           site_name=CONFIG.site_name,
                           string_info=STRING_INFO)


@app.route("/api/state")
def api_state():
    return jsonify(ENGINE.state() if ENGINE else {"live": None})


@app.route("/api/history")
def api_history():
    return jsonify(ENGINE.history_snapshot() if ENGINE else {})


@app.route("/csv/today.csv")
def csv_today():
    date_str = datetime.now().strftime("%Y-%m-%d")
    return send_from_directory(CONFIG.csv_dir, f"{date_str}.csv",
                               as_attachment=False)


@app.route("/csv/")
def csv_index():
    files = sorted(os.listdir(CONFIG.csv_dir)) if os.path.isdir(CONFIG.csv_dir) else []
    csvs = [f for f in files if f.endswith(".csv")]
    html = "<h1>CSV files</h1><ul>" + "".join(
        f'<li><a href="/csv/{f}">{f}</a></li>' for f in csvs) + "</ul>"
    return html


@app.route("/csv/<path:fn>")
def csv_file(fn):
    return send_from_directory(CONFIG.csv_dir, fn, as_attachment=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=CONFIG.host)
    p.add_argument("--port", type=int, default=CONFIG.port)
    p.add_argument("--solis-url", default=CONFIG.solis_url)
    p.add_argument("--sma-host", default=CONFIG.sma_host)
    p.add_argument("--sma-port", type=int, default=CONFIG.sma_port)
    p.add_argument("--sma-unit", type=int, default=CONFIG.sma_unit,
                   help="SMA Modbus unit ID (3–247 reaches Meteo Station / Sensorbox)")
    p.add_argument("--sma-poll", type=float, default=CONFIG.sma_poll)
    p.add_argument("--solis-poll", type=float, default=CONFIG.solis_poll)
    p.add_argument("--csv-interval", type=float, default=CONFIG.csv_interval)
    p.add_argument("--csv-dir", default=CONFIG.csv_dir)
    p.add_argument("--lat", type=float, default=CONFIG.lat)
    p.add_argument("--lon", type=float, default=CONFIG.lon)
    p.add_argument("--string-kwp", type=float, default=CONFIG.string_kwp)
    p.add_argument("--site-name", default=CONFIG.site_name)
    args = p.parse_args()

    for k, v in vars(args).items():
        setattr(CONFIG, k.replace("-", "_"), v)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    global ENGINE
    ENGINE = Engine()
    ENGINE.start()

    log.info(f"Starting tracker_analysis on {CONFIG.host}:{CONFIG.port}")
    app.run(host=CONFIG.host, port=CONFIG.port, debug=False, use_reloader=False,
            threaded=True)


if __name__ == "__main__":
    main()
