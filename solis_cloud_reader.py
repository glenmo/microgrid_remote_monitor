#!/usr/bin/env python3
"""SolisCloud API reader — Solis hybrid inverter telemetry via the cloud.

A drop-in alternative to ``SolisModbusReader`` that pulls the same fields
from the SolisCloud Open API (https://oss.soliscloud.com) instead of local
Modbus. Used when the local RS485/WiFi gateway is unavailable.

Exposes the same interface as the other readers
(start/stop/get_data/get_history/get_status) and produces the same
``/api/solis/data`` shape the dashboard + flow diagram expect.

Auth: HMAC-SHA1 request signing per the SolisCloud Open API spec —
    Authorization: "API <keyId>:<base64(hmacSha1(keySecret, signStr))>"
    signStr = VERB\\nContent-MD5\\nContent-Type\\nDate\\nCanonicalResource

The cloud updates roughly every ~5 min and the API is rate-limited, so
poll no faster than ~60 s.
"""

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime
from email.utils import formatdate

import requests

log = logging.getLogger("solis_cloud_reader")

INVERTER_DETAIL = "/v1/api/inverterDetail"

# Map a SolisCloud unit string to a multiplier that normalises to watts.
_UNIT_W = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0, "VA": 1.0, "kVA": 1000.0}


class SolisCloudReader:
    """Polls one Solis inverter's live detail from the SolisCloud API."""

    def __init__(self, key_id, key_secret, inverter_sn, inverter_id=None,
                 base_url="https://www.soliscloud.com:13333", poll_interval=60):
        self.key_id = key_id
        self.key_secret = key_secret.encode("utf-8") if isinstance(key_secret, str) else key_secret
        self.inverter_sn = inverter_sn
        self.inverter_id = inverter_id
        self.base_url = base_url.rstrip("/")
        self.poll_interval = max(30, int(poll_interval))  # protect the rate limit

        self.connected = False
        self.last_read_time = None
        self.read_errors = 0
        self.total_reads = 0

        self.data = {}
        self.history_max = 1440  # 24h at 1-min cadence
        self.history = {
            "timestamps":      deque(maxlen=self.history_max),
            "battery_soc":     deque(maxlen=self.history_max),
            "pv_total_power":  deque(maxlen=self.history_max),
            "battery_power":   deque(maxlen=self.history_max),
            "active_power":    deque(maxlen=self.history_max),
            "grid_frequency":  deque(maxlen=self.history_max),
            "pv1_power":       deque(maxlen=self.history_max),
            "pv2_power":       deque(maxlen=self.history_max),
            "pv3_power":       deque(maxlen=self.history_max),
            "pv4_power":       deque(maxlen=self.history_max),
        }
        self._last_history_minute = -1

        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    # ---- SolisCloud request signing -------------------------------------
    def _call(self, endpoint, body_dict):
        body = json.dumps(body_dict)
        content_md5 = base64.b64encode(
            hashlib.md5(body.encode("utf-8")).digest()).decode("utf-8")
        content_type = "application/json"
        date = formatdate(timeval=None, localtime=False, usegmt=True)  # GMT
        sign_str = f"POST\n{content_md5}\n{content_type}\n{date}\n{endpoint}"
        sign = base64.b64encode(
            hmac.new(self.key_secret, sign_str.encode("utf-8"),
                     hashlib.sha1).digest()).decode("utf-8")
        headers = {
            "Content-MD5": content_md5,
            "Content-Type": content_type,
            "Date": date,
            "Authorization": f"API {self.key_id}:{sign}",
        }
        r = requests.post(self.base_url + endpoint, data=body,
                          headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()

    # ---- field helpers ---------------------------------------------------
    @staticmethod
    def _watts(d, key):
        """Value at ``key`` normalised to watts using its ``<key>Str`` unit."""
        v = d.get(key)
        if v is None:
            return 0.0
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        unit = d.get(key + "Str") or "W"
        return round(v * _UNIT_W.get(unit, 1.0), 1)

    @staticmethod
    def _num(d, key, default=0.0):
        try:
            return float(d.get(key))
        except (TypeError, ValueError):
            return default

    def _map(self, d):
        """Map a SolisCloud inverterDetail dict to our /api/solis/data shape."""
        # Battery sign: this inverter/firmware returns a SIGNED batteryPower
        # (negative = discharging, positive = charging) — already in the
        # convention the dashboard/flow expect (+ = charging), so use it
        # directly. The batteryDirection flag is NOT a reliable charge/discharge
        # indicator here: it emits codes like 2/3, not the documented
        # 0 (charge) / 1 (discharge), so keying the sign off it inverted a
        # discharging battery into a charging one.
        battery_power = self._watts(d, "batteryPower")
        direction = d.get("batteryDirection")

        # powTotal is often empty on this firmware; sum the PV string powers.
        pv_total = round(sum(self._watts(d, f"pow{i}") for i in range(1, 33)), 1)
        if not pv_total:
            pv_total = self._watts(d, "powTotal")

        out = {
            "battery_soc":     round(self._num(d, "batteryCapacitySoc"), 1),
            "pv_total_power":  pv_total,
            "battery_power":   battery_power,
            # grid/AC power exported to the microgrid bus (+ export / - import)
            "active_power":    self._watts(d, "psum"),
            "ac_output_power": self._watts(d, "pac"),
            "grid_frequency":  round(self._num(d, "fac"), 2),
            "battery_voltage": round(self._num(d, "storageBatteryVoltage"), 1),
            "battery_current": round(self._num(d, "storageBatteryCurrent"), 1),
            "pv1_power":       self._watts(d, "pow1"),
            "pv2_power":       self._watts(d, "pow2"),
            "pv3_power":       self._watts(d, "pow3"),
            "pv4_power":       self._watts(d, "pow4"),
            "pv_today_energy": round(self._num(d, "eToday"), 1),
            # raw fields kept for sign verification / debugging
            "_battery_direction": direction,
            "_source": "soliscloud",
        }
        return out

    # ---- polling ---------------------------------------------------------
    def poll_once(self):
        body = {"sn": self.inverter_sn}
        if self.inverter_id:
            body["id"] = self.inverter_id
        try:
            j = self._call(INVERTER_DETAIL, body)
        except Exception as e:
            self.connected = False
            self.read_errors += 1
            log.warning(f"SolisCloud: request failed: {type(e).__name__}: {e}")
            return

        if not j.get("success") or not isinstance(j.get("data"), dict):
            self.connected = False
            self.read_errors += 1
            log.warning(f"SolisCloud: API error: code={j.get('code')} msg={j.get('msg')}")
            return

        now = datetime.now()
        mapped = self._map(j["data"])
        mapped["_timestamp"] = now.isoformat()

        self.connected = True
        self.total_reads += 1
        with self.lock:
            self.data = mapped
            self.last_read_time = now
            if now.minute != self._last_history_minute:
                self._last_history_minute = now.minute
                self.history["timestamps"].append(now.strftime("%H:%M"))
                for key in ("battery_soc", "pv_total_power", "battery_power",
                            "active_power", "grid_frequency",
                            "pv1_power", "pv2_power", "pv3_power", "pv4_power"):
                    self.history[key].append(mapped.get(key, 0))

    def _poll_loop(self):
        log.info(f"SolisCloud poll loop started (every {self.poll_interval}s, "
                 f"inverter sn={self.inverter_sn})")
        last_heartbeat = datetime.now()
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.error(f"SolisCloud poll error: {e}")
            now = datetime.now()
            if (now - last_heartbeat).total_seconds() >= 60:
                age = (now - self.last_read_time).total_seconds() if self.last_read_time else None
                log.info(f"SolisCloud heartbeat: connected={self.connected}, "
                         f"last_read_age={f'{age:.0f}s' if age is not None else 'never'}, "
                         f"total_reads={self.total_reads}, read_errors={self.read_errors}")
                last_heartbeat = now
            self._stop_event.wait(self.poll_interval)

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info(f"SolisCloud polling started (every {self.poll_interval}s)")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def get_data(self):
        with self.lock:
            return dict(self.data)

    def get_history(self):
        with self.lock:
            return {k: list(v) for k, v in self.history.items()}

    def get_status(self):
        return {
            "connected": self.connected,
            "inverter_ip": "(SolisCloud API)",
            "inverter_sn": self.inverter_sn,
            "poll_interval": self.poll_interval,
            "total_reads": self.total_reads,
            "read_errors": self.read_errors,
            "last_read": self.last_read_time.isoformat() if self.last_read_time else None,
        }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-id", required=True)
    ap.add_argument("--key-secret", required=True)
    ap.add_argument("--sn", required=True)
    ap.add_argument("--id", default=None)
    ap.add_argument("--base-url", default="https://www.soliscloud.com:13333")
    args = ap.parse_args()
    r = SolisCloudReader(args.key_id, args.key_secret, args.sn, args.id, args.base_url)
    r.poll_once()
    print(json.dumps(r.get_data(), indent=2))
