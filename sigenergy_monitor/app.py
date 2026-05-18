"""sigenergy_monitor — Sigenergy SigenStor dashboard.

Polls a SigenStor EC 10.0 TP at 192.168.55.131:502 (configurable) and
serves a comprehensive read-only dashboard. Overlays POA / ambient / wind
/ humidity / pressure from the SMA WebBox FTP-Push pipeline that already
feeds tracker_analysis.

Run:
    python app.py --host 0.0.0.0 --port 8902
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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, render_template, send_from_directory

from sigen_reader import SigenReader
from sma_ftp import SmaFtpReader

log = logging.getLogger("sigenergy_monitor")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8902
    sigen_host: str = "192.168.55.131"
    sigen_port: int = 502
    sigen_plant_slave: int = 247
    sigen_inv_slave: int = 1
    sigen_poll: float = 5.0
    sma_watch_dir: str = "~/sma"
    sma_poll: float = 10.0
    csv_interval: float = 5.0
    csv_dir: str = "./data"
    battery_kwh: float = 32.0
    site_name: str = "Sigenergy SigenStor Monitoring"

CONFIG = Config()


# ---------------------------------------------------------------------------
# Plant running state codes (from Appendix 1)
# ---------------------------------------------------------------------------
RUNNING_STATES = {
    0: "Standby",
    1: "Running",
    2: "Fault",
    3: "Shutdown",
    4: "Initialization",
    5: "Self-check",
    6: "Off-grid charging",
    7: "Off-grid",
    8: "Grid-tied",
    9: "Soft-start",
}

EMS_MODES = {
    0: "Max self-consumption",
    1: "Sigen AI Mode",
    2: "TOU",
    7: "Remote EMS",
}

ONGRID_STATES = {
    0: "On grid",
    1: "Off grid (auto)",
    2: "Off grid (manual)",
}


# ---------------------------------------------------------------------------
# CSV writer with daily rotation
# ---------------------------------------------------------------------------
class CsvWriter:
    HEADERS = [
        "timestamp",
        # Battery
        "battery_soc_pct", "battery_soh_pct",
        "cell_temp_c", "cell_voltage_v",
        # Power flow (W signed)
        "battery_power_w", "pv_power_w", "grid_power_w",
        "plant_active_power_w",
        # PV strings
        "pv1_v", "pv1_a", "pv1_w",
        "pv2_v", "pv2_a", "pv2_w",
        "pv3_v", "pv3_a", "pv3_w",
        "pv4_v", "pv4_a", "pv4_w",
        # AC
        "ac_a_v", "ac_b_v", "ac_c_v",
        "ac_a_a", "ac_b_a", "ac_c_a",
        "grid_freq_hz", "power_factor", "pcs_temp_c",
        # EV
        "ev_vehicle_v", "ev_charging_a", "ev_output_w",
        "ev_vehicle_soc", "ev_session_kwh",
        # Weather
        "poa_wm2", "amb_temp_c", "mod_temp_c", "wind_ms",
        "humidity_pct", "air_pressure_pa",
        # Daily energy
        "daily_pv_kwh", "daily_import_kwh", "daily_export_kwh",
        "batt_daily_charge_kwh", "batt_daily_discharge_kwh",
    ]

    def __init__(self, csv_dir: str):
        self.csv_dir = Path(csv_dir)
        self.csv_dir.mkdir(parents=True, exist_ok=True)
        self._date = None
        self._fh = None
        self._writer = None
        self._lock = threading.Lock()

    def _ensure(self, now: datetime):
        date_str = now.strftime("%Y-%m-%d")
        if date_str != self._date:
            if self._fh: self._fh.close()
            path = self.csv_dir / f"{date_str}.csv"
            new = not path.exists()
            self._fh = open(path, "a", newline="")
            self._writer = csv.writer(self._fh)
            if new:
                self._writer.writerow(self.HEADERS)
                self._fh.flush()
            self._date = date_str

    def write(self, sample: dict, weather: dict, now: datetime):
        with self._lock:
            self._ensure(now)
            r = lambda k, dp=None: (round(sample.get(k), dp) if dp is not None and sample.get(k) is not None
                                    else sample.get(k))
            w = lambda k: weather.get(k) if weather else None
            # power values: kW → W
            def to_w(k):
                v = sample.get(k)
                return None if v is None else int(round(v * 1000))
            row = [
                now.isoformat(timespec="seconds"),
                r("batt_soc_pct", 1), r("batt_soh_pct", 1),
                r("batt_cell_temp_c", 2), r("batt_cell_voltage_v", 3),
                to_w("batt_power_kw"), to_w("plant_pv_kw"), to_w("grid_power_kw"),
                to_w("plant_active_kw"),
                r("pv1_v", 1), r("pv1_a", 2), r("pv1_w", 1),
                r("pv2_v", 1), r("pv2_a", 2), r("pv2_w", 1),
                r("pv3_v", 1), r("pv3_a", 2), r("pv3_w", 1),
                r("pv4_v", 1), r("pv4_a", 2), r("pv4_w", 1),
                r("ac_a_v", 2), r("ac_b_v", 2), r("ac_c_v", 2),
                r("ac_a_a", 2), r("ac_b_a", 2), r("ac_c_a", 2),
                r("grid_freq_hz", 2), r("power_factor", 3), r("pcs_internal_temp_c", 1),
                r("ev_vehicle_v", 1), r("ev_charging_a", 1), to_w("ev_output_kw"),
                r("ev_vehicle_soc", 1), r("ev_session_kwh", 3),
                w("radiation_wm2"), w("ambient_temp_c"), w("module_temp_c"),
                w("wind_speed_ms"), w("humidity_pct"), w("air_pressure_pa"),
                r("daily_pv_kwh", 2) if False else None,  # daily PV computed below
                r("daily_import_kwh", 2),
                r("daily_export_kwh", 2),
                r("batt_daily_charge_kwh", 2),
                r("batt_daily_discharge_kwh", 2),
            ]
            self._writer.writerow(row)
            self._fh.flush()


# ---------------------------------------------------------------------------
# Engine — coordinates Sigenergy + SMA, integrates Wh, exposes state
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self):
        self.sigen = SigenReader(
            CONFIG.sigen_host, CONFIG.sigen_port,
            plant_slave=CONFIG.sigen_plant_slave,
            inv_slave=CONFIG.sigen_inv_slave,
            poll_interval=CONFIG.sigen_poll,
        )
        self.sma = SmaFtpReader(CONFIG.sma_watch_dir,
                                scan_interval=CONFIG.sma_poll)
        self.csv = CsvWriter(CONFIG.csv_dir)

        # Per-sample history for charts — keep ~24h at 30s downsampling
        self.history: deque[dict] = deque(maxlen=2880)
        self._history_last_ts: Optional[datetime] = None
        self._csv_last_write: Optional[datetime] = None
        self._lock = threading.Lock()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.latest: dict[str, Any] = {}

    def start(self):
        self.sigen.start()
        self.sma.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Engine started")

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=5)
        self.sigen.stop()
        self.sma.stop()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log.error(f"Engine tick error: {e}")
            self._stop.wait(1.0)

    def _tick(self):
        now = datetime.now()
        sigen = self.sigen.get_data()
        sma = self.sma.get_data() or {}

        if not sigen:
            return

        with self._lock:
            self.latest = {**sigen}

            # Downsampled history at ~30s for the chart
            if (self._history_last_ts is None
                or (now - self._history_last_ts).total_seconds() >= 30):
                snap = {
                    "ts": now.isoformat(),
                    "battery_soc": sigen.get("batt_soc_pct"),
                    "battery_power_kw": sigen.get("batt_power_kw"),
                    "pv_power_kw": sigen.get("plant_pv_kw"),
                    "grid_power_kw": sigen.get("grid_power_kw"),
                    "cell_temp_c": sigen.get("batt_cell_temp_c"),
                    "pcs_temp_c": sigen.get("pcs_internal_temp_c"),
                    "ac_a_v": sigen.get("ac_a_v"),
                    "ac_b_v": sigen.get("ac_b_v"),
                    "ac_c_v": sigen.get("ac_c_v"),
                    "grid_freq_hz": sigen.get("grid_freq_hz"),
                    "ambient_temp_c": sma.get("ambient_temp_c"),
                    "poa_wm2": sma.get("radiation_wm2"),
                }
                self.history.append(snap)
                self._history_last_ts = now

        # CSV log every 5 s
        if (self._csv_last_write is None
            or (now - self._csv_last_write).total_seconds() >= CONFIG.csv_interval):
            try:
                self.csv.write(sigen, sma, now)
            except Exception as e:
                log.warning(f"CSV write failed: {e}")
            self._csv_last_write = now

    # ---- API helpers ----
    def state(self) -> dict:
        with self._lock:
            sigen = dict(self.latest)
            sma = self.sma.get_data() or {}

        # Decode some enums
        rs = sigen.get("plant_running_state")
        rs_str = RUNNING_STATES.get(rs, f"Code {rs}") if rs is not None else None
        ems = sigen.get("ems_work_mode")
        ems_str = EMS_MODES.get(ems, f"Code {ems}") if ems is not None else None
        og = sigen.get("ongrid_offgrid_state")
        og_str = ONGRID_STATES.get(og, f"Code {og}") if og is not None else None

        # Compute load power: Load = PV + Battery_discharge − Grid_export
        # Easier algebra: Load = PV_power + (-Battery_power) + (-Grid_power_to_export)
        # Sign conventions:
        #   plant_pv_kw   ≥ 0 (always)
        #   ess_power_kw  >0 charging (=power flowing INTO battery)
        #   grid_power_kw >0 importing (=power flowing IN from grid)
        # So:    Load = PV + Grid_import + Batt_discharge
        #             = PV + grid_power_kw + (-ess_power_kw)
        pv  = sigen.get("plant_pv_kw") or 0
        ess = sigen.get("ess_power_kw") or sigen.get("batt_power_kw") or 0
        grid = sigen.get("grid_power_kw") or 0
        load_kw = pv + grid - ess

        ev_active = bool(sigen.get("ev_output_kw") and sigen.get("ev_output_kw") > 0.05)

        return {
            "ts": datetime.now().isoformat(),
            "sigen_status": self.sigen.get_status(),
            "sma_status":   self.sma.get_status(),
            "sigen": sigen,
            "weather": sma,
            "computed": {
                "load_kw": round(load_kw, 3),
                "running_state_str": rs_str,
                "ems_mode_str": ems_str,
                "ongrid_str": og_str,
                "ev_active": ev_active,
            },
        }

    def history_snapshot(self) -> dict:
        with self._lock:
            entries = list(self.history)
        ts = [e["ts"] for e in entries]
        return {
            "timestamps": ts,
            "battery_soc":    [e.get("battery_soc")     for e in entries],
            "battery_power":  [e.get("battery_power_kw") for e in entries],
            "pv_power":       [e.get("pv_power_kw")     for e in entries],
            "grid_power":     [e.get("grid_power_kw")   for e in entries],
            "cell_temp":      [e.get("cell_temp_c")     for e in entries],
            "pcs_temp":       [e.get("pcs_temp_c")      for e in entries],
            "ac_a_v":         [e.get("ac_a_v")          for e in entries],
            "ac_b_v":         [e.get("ac_b_v")          for e in entries],
            "ac_c_v":         [e.get("ac_c_v")          for e in entries],
            "grid_freq":      [e.get("grid_freq_hz")    for e in entries],
            "ambient_temp":   [e.get("ambient_temp_c")  for e in entries],
            "poa_wm2":        [e.get("poa_wm2")         for e in entries],
        }


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__)
ENGINE: Optional[Engine] = None


@app.route("/")
def dashboard():
    return render_template("index.html",
                           site_name=CONFIG.site_name,
                           battery_kwh=CONFIG.battery_kwh)


@app.route("/api/state")
def api_state():
    return jsonify(ENGINE.state() if ENGINE else {})


@app.route("/api/history")
def api_history():
    return jsonify(ENGINE.history_snapshot() if ENGINE else {})


@app.route("/csv/today.csv")
def csv_today():
    return send_from_directory(CONFIG.csv_dir,
                               f"{datetime.now().strftime('%Y-%m-%d')}.csv",
                               as_attachment=False)


@app.route("/csv/")
def csv_index():
    files = sorted(os.listdir(CONFIG.csv_dir)) if os.path.isdir(CONFIG.csv_dir) else []
    csvs = [f for f in files if f.endswith(".csv")]
    return ("<h1>CSV files</h1><ul>"
            + "".join(f'<li><a href="/csv/{f}">{f}</a></li>' for f in csvs)
            + "</ul>")


@app.route("/csv/<path:fn>")
def csv_file(fn):
    return send_from_directory(CONFIG.csv_dir, fn, as_attachment=False)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=CONFIG.host)
    p.add_argument("--port", type=int, default=CONFIG.port)
    p.add_argument("--sigen-host", default=CONFIG.sigen_host)
    p.add_argument("--sigen-port", type=int, default=CONFIG.sigen_port)
    p.add_argument("--sigen-plant-slave", type=int, default=CONFIG.sigen_plant_slave)
    p.add_argument("--sigen-inv-slave", type=int, default=CONFIG.sigen_inv_slave)
    p.add_argument("--sigen-poll", type=float, default=CONFIG.sigen_poll)
    p.add_argument("--sma-watch-dir", default=CONFIG.sma_watch_dir)
    p.add_argument("--sma-poll", type=float, default=CONFIG.sma_poll)
    p.add_argument("--csv-interval", type=float, default=CONFIG.csv_interval)
    p.add_argument("--csv-dir", default=CONFIG.csv_dir)
    p.add_argument("--battery-kwh", type=float, default=CONFIG.battery_kwh)
    p.add_argument("--site-name", default=CONFIG.site_name)
    args = p.parse_args()

    for k, v in vars(args).items():
        setattr(CONFIG, k.replace("-", "_"), v)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    global ENGINE
    ENGINE = Engine()
    ENGINE.start()

    log.info(f"Starting sigenergy_monitor on {CONFIG.host}:{CONFIG.port}")
    app.run(host=CONFIG.host, port=CONFIG.port, debug=False,
            use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
