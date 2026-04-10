#!/usr/bin/env python3
"""
SwitchDin Stormcloud API Reader
================================
Fetches live data from a Selectronic SP Pro system via the SwitchDin
Stormcloud cloud platform API.

Authentication:
  POST /api/v1/token/jwt-obtain/  {username, password}  → {access, refresh}

Data:
  GET /api/v2/chartdata/?field=unit_uuid&uuid=<uuid>&metrics=(<csv>)
      &period=minute&fromts=<ms>&tots=<ms>
  Response has Django security prefix  )]}',\\n  before JSON body.

Metric keys (Site Aggregates):
  SWDINPV.ZBAT.SoC.instMag[MX]       Battery State of Charge (%)
  SWDINPV.ZBAT.TotW.instMag[MX]      Battery Power (kW)
  SWDINPV.MMXN1.TotW.instMag[MX]     PV Power (kW)
  SWDINPV.MMXN2.TotW.instMag[MX]     Load Power (kW)
  SWDINPV.MMXN3.TotW.instMag[MX]     Grid Power (kW)
  SWDINPV.MMXN4.TotW.instMag[MX]     Inverter Power (kW)

Per-phase SP Pro metrics use SWDINPV.SPPRO{n}.* where n=0,1,2 for L1,L2,L3.
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import quote

import requests

log = logging.getLogger("switchdin_reader")

# ---------------------------------------------------------------------------
# API configuration
# ---------------------------------------------------------------------------
API_BASE = "https://app.switchdin.com"
LOGIN_URL = f"{API_BASE}/api/v1/token/jwt-obtain/"
CHARTDATA_URL = f"{API_BASE}/api/v2/chartdata/"

# Django REST Framework security prefix on JSON responses
DRF_PREFIX = ")]}',\n"

# ---------------------------------------------------------------------------
# Metric key definitions
# ---------------------------------------------------------------------------

# Site Aggregates — overall system totals
SITE_METRICS = {
    "battery_soc":     "SWDINPV.ZBAT.SoC.instMag[MX]",
    "battery_power":   "SWDINPV.ZBAT.TotW.instMag[MX]",
    "pv_power":        "SWDINPV.MMXN1.TotW.instMag[MX]",
    "load_power":      "SWDINPV.MMXN2.TotW.instMag[MX]",
    "grid_power":      "SWDINPV.MMXN3.TotW.instMag[MX]",
    "inverter_power":  "SWDINPV.MMXN4.TotW.instMag[MX]",
}

# Per-phase metrics from SmartRail X835 3-Phase Meter
# MMXU1 = Phase 1 (L1), MMXU2 = Phase 2 (L2), MMXU3 = Phase 3 (L3)
# MMXN3 = 3-phase totals
PHASE_METRICS = {}
for phase_num, phase_label in enumerate(["l1", "l2", "l3"], start=1):
    pfx = f"SWDINPV.SMART1.MMXU{phase_num}"
    PHASE_METRICS[f"{phase_label}_grid_voltage"]    = f"{pfx}.Vol.instMag[MX]"
    PHASE_METRICS[f"{phase_label}_grid_current"]    = f"{pfx}.Cur.instMag[MX]"
    PHASE_METRICS[f"{phase_label}_grid_power"]      = f"{pfx}.TotW.instMag[MX]"
    PHASE_METRICS[f"{phase_label}_power_factor"]    = f"{pfx}.PF.instMag[MX]"
    PHASE_METRICS[f"{phase_label}_reactive_power"]  = f"{pfx}.VAr.instMag[MX]"

# Grid frequency from SmartRail (single value, same for all phases)
PHASE_METRICS["grid_frequency"] = "SWDINPV.SMART1.MMXN3.Freq.instMag[MX]"

# Total grid power from SmartRail
PHASE_METRICS["smartrail_total_power"] = "SWDINPV.SMART1.MMXN3.TotW.instMag[MX]"

# Combined dict of all metrics we want to fetch
ALL_METRICS = {**SITE_METRICS, **PHASE_METRICS}

# Reverse lookup: API key → our friendly name
_KEY_TO_NAME = {v: k for k, v in ALL_METRICS.items()}


def _strip_drf_prefix(text):
    """Remove the Django REST Framework security prefix from a response."""
    if text.startswith(")]}"):
        return text[text.index("\n") + 1:]
    return text


class SwitchDinReader:
    """Periodically fetches SP Pro data from SwitchDin Stormcloud API."""

    def __init__(self, username, password, unit_uuid, poll_interval=60):
        self.username = username
        self.password = password
        self.unit_uuid = unit_uuid
        self.poll_interval = poll_interval

        # Auth state
        self._access_token = None
        self._token_time = 0
        self._token_lifetime = 300  # re-auth every 5 minutes to be safe

        # Connection / polling stats
        self.connected = False
        self.last_read_time = None
        self.read_errors = 0
        self.total_reads = 0

        # Current processed data
        self.data = {}

        # History for charts (1-minute samples, 24 hours)
        self.history_max = 1440
        self.history = {
            "timestamps":       deque(maxlen=self.history_max),
            "battery_soc":      deque(maxlen=self.history_max),
            "battery_power":    deque(maxlen=self.history_max),
            "pv_power":         deque(maxlen=self.history_max),
            "load_power":       deque(maxlen=self.history_max),
            "grid_power":       deque(maxlen=self.history_max),
            "inverter_power":   deque(maxlen=self.history_max),
        }
        self._last_history_minute = -1

        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

        # Requests session for connection reuse
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def _authenticate(self):
        """Obtain a JWT access token from SwitchDin."""
        try:
            resp = self._session.post(
                LOGIN_URL,
                json={"username": self.username, "password": self.password},
                timeout=15,
            )
            body = _strip_drf_prefix(resp.text)
            if resp.status_code == 200:
                data = __import__("json").loads(body)
                self._access_token = data.get("access")
                self._token_time = time.time()
                self.connected = True
                log.info("SwitchDin: Authenticated successfully")
                return True
            else:
                log.warning(f"SwitchDin auth failed: HTTP {resp.status_code}")
                self.connected = False
                return False
        except Exception as e:
            log.error(f"SwitchDin auth error: {e}")
            self.connected = False
            return False

    def _ensure_token(self):
        """Re-authenticate if the token is missing or expired."""
        if self._access_token and (time.time() - self._token_time) < self._token_lifetime:
            return True
        return self._authenticate()

    def _auth_headers(self):
        return {"Authorization": f"JWT {self._access_token}"}

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    def _fetch_chartdata(self, metric_keys, period="minute", window_ms=300_000):
        """Fetch recent chartdata for a list of metric keys.

        Returns a dict of {metric_key: latest_value} for each metric.
        """
        if not self._ensure_token():
            return {}

        now_ms = int(time.time() * 1000)
        from_ms = now_ms - window_ms

        # Build the metrics parameter — parentheses around CSV, keys URL-encoded
        # Keep commas, curly braces, and dots safe (API expects them literal)
        metrics_csv = ",".join(metric_keys)
        url = (
            f"{CHARTDATA_URL}?field=unit_uuid"
            f"&uuid={self.unit_uuid}"
            f"&metrics=({quote(metrics_csv, safe=',.{}')})"
            f"&period={period}"
            f"&fromts={from_ms}&tots={now_ms}"
        )

        try:
            resp = self._session.get(url, headers=self._auth_headers(), timeout=20)
            body = _strip_drf_prefix(resp.text)

            if resp.status_code == 401:
                # Token expired — re-auth and retry once
                log.info("SwitchDin: Token expired, re-authenticating…")
                self._access_token = None
                if not self._authenticate():
                    return {}
                resp = self._session.get(url, headers=self._auth_headers(), timeout=20)
                body = _strip_drf_prefix(resp.text)

            if resp.status_code != 200:
                log.warning(f"SwitchDin chartdata error: HTTP {resp.status_code}")
                return {}

            data = __import__("json").loads(body)
            # Extract latest value for each metric
            result = {}
            for key, points in data.items():
                if isinstance(points, list) and len(points) > 0:
                    last_point = points[-1]
                    if isinstance(last_point, dict) and "value" in last_point:
                        result[key] = last_point["value"]
            return result

        except Exception as e:
            log.error(f"SwitchDin chartdata error: {e}")
            return {}

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def poll_once(self):
        """Fetch all metrics and update the data dict."""
        # Split into batches of ~10 metrics to avoid URL length issues
        all_keys = list(ALL_METRICS.values())
        batch_size = 10
        raw_values = {}
        for i in range(0, len(all_keys), batch_size):
            batch = all_keys[i:i + batch_size]
            result = self._fetch_chartdata(batch)
            raw_values.update(result)

        self.total_reads += 1

        if not raw_values:
            self.read_errors += 1
            self.connected = False
            return

        self.connected = True
        now = datetime.now()
        log.info(f"SwitchDin: Got {len(raw_values)} metrics")

        # Map API keys back to friendly names
        combined = {}
        for api_key, value in raw_values.items():
            friendly = _KEY_TO_NAME.get(api_key)
            if friendly:
                combined[friendly] = round(value, 3)

        # SwitchDin power values are in kW — convert to W for the dashboard
        for key in list(combined.keys()):
            if ("power" in key or "reactive_power" in key) and "power_factor" not in key:
                combined[key] = round(combined[key] * 1000, 1)  # kW → W

        # Voltage and frequency don't need conversion (already V and Hz)

        # Metadata
        combined["_timestamp"] = now.isoformat()
        combined["_read_ok"] = True
        combined["_source"] = "switchdin"

        # Update shared state
        with self.lock:
            self.data = combined
            self.last_read_time = now

            # Append to history (once per minute)
            current_minute = now.minute
            if current_minute != self._last_history_minute:
                self._last_history_minute = current_minute
                self.history["timestamps"].append(now.strftime("%H:%M"))
                for key in ["battery_soc", "battery_power", "pv_power",
                            "load_power", "grid_power", "inverter_power"]:
                    self.history[key].append(combined.get(key, 0))

    def _poll_loop(self):
        """Background polling loop with backoff on failure."""
        retry_delay = self.poll_interval
        while not self._stop_event.is_set():
            try:
                self.poll_once()
                if self.connected:
                    retry_delay = self.poll_interval
                else:
                    retry_delay = min(retry_delay * 2, 300)
                    log.info(f"SwitchDin: Not connected, retrying in {retry_delay}s")
            except Exception as e:
                log.error(f"SwitchDin poll error: {e}")
                retry_delay = min(retry_delay * 2, 300)
            self._stop_event.wait(retry_delay)

    # ------------------------------------------------------------------
    # Public interface (matches SPProModbusReader API)
    # ------------------------------------------------------------------
    def start(self):
        """Start background polling thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info(f"SwitchDin polling started (every {self.poll_interval}s)")

    def stop(self):
        """Stop polling."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._session.close()

    def connect(self):
        """Authenticate with SwitchDin API."""
        return self._authenticate()

    def disconnect(self):
        """Close the HTTP session."""
        self._session.close()
        self.connected = False

    def get_data(self):
        """Return a copy of the current data."""
        with self.lock:
            return dict(self.data)

    def get_history(self):
        """Return a copy of the history data."""
        with self.lock:
            return {k: list(v) for k, v in self.history.items()}

    def get_status(self):
        """Return connection/polling status."""
        return {
            "connected": self.connected,
            "source": "SwitchDin Stormcloud API",
            "unit_uuid": self.unit_uuid,
            "poll_interval": self.poll_interval,
            "total_reads": self.total_reads,
            "read_errors": self.read_errors,
            "last_read": self.last_read_time.isoformat() if self.last_read_time else None,
        }
