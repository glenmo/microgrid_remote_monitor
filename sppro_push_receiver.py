#!/usr/bin/env python3
"""SP Pro Push Receiver — holds SP Pro telemetry POSTed by a remote reader.

In the kitty/USB architecture the SP Pro is read locally on `kitty` (cabled
by USB to the Advanced Comms Board) and pushed over the LAN to rubberduck.
rubberduck stays the source of truth: it serves this received data through the
existing /api/sppro/{data,history,status} routes, so the dashboard and flow
page need no changes.

This class exposes the same start/stop/get_data/get_history/get_status
interface as SPProSxReader, plus ingest() which the /api/sppro/ingest route
calls with each pushed payload: {"data": {...}, "status": {...}}.
"""
import threading
from collections import deque
from datetime import datetime


class SPProPushReceiver:
    def __init__(self, stale_after=30):
        # connected -> False if no fresh push arrived within this many seconds
        self.stale_after = stale_after

        self.data = {}
        self._upstream_status = {}
        self._last_ingest_time = None
        self.total_pushes = 0

        self.history_max = 1440
        self.history = {
            "timestamps":      deque(maxlen=self.history_max),
            "battery_soc":     deque(maxlen=self.history_max),
            "battery_w":       deque(maxlen=self.history_max),
            "grid_w":          deque(maxlen=self.history_max),
            "load_w":          deque(maxlen=self.history_max),
            "shunt_w":         deque(maxlen=self.history_max),
            "solarinverter_w": deque(maxlen=self.history_max),
        }
        self._last_history_minute = -1

        self.lock = threading.Lock()

    # --- ingest -----------------------------------------------------------
    def ingest(self, payload):
        """Store one pushed payload {"data": {...}, "status": {...}}."""
        data = dict((payload or {}).get("data") or {})
        status = dict((payload or {}).get("status") or {})
        now = datetime.now()
        with self.lock:
            self.data = data
            self._upstream_status = status
            self._last_ingest_time = now
            self.total_pushes += 1
            # Accumulate one history point per minute, mirroring SPProSxReader.
            if now.minute != self._last_history_minute:
                self._last_history_minute = now.minute
                self.history["timestamps"].append(now.strftime("%H:%M"))
                for key in ("battery_soc", "battery_w", "grid_w",
                            "load_w", "shunt_w", "solarinverter_w"):
                    self.history[key].append(data.get(key, 0))

    # --- reader interface -------------------------------------------------
    def start(self):
        # Nothing to poll — data arrives via ingest().
        pass

    def stop(self):
        pass

    def get_data(self):
        with self.lock:
            return dict(self.data)

    def get_history(self):
        with self.lock:
            return {k: list(v) for k, v in self.history.items()}

    def get_status(self):
        with self.lock:
            last = self._last_ingest_time
            age = (datetime.now() - last).total_seconds() if last else None
            fresh = age is not None and age <= self.stale_after
            # Reflect the upstream reader's own health too: if kitty is pushing
            # but its serial read is down, we are not really connected.
            upstream_ok = bool(self._upstream_status.get("connected", True))
            return {
                "connected":      fresh and upstream_ok,
                "transport":      "push",
                "host":           self._upstream_status.get("host", "kitty (USB)"),
                "stale_after":    self.stale_after,
                "last_push_age_s": round(age, 1) if age is not None else None,
                "total_pushes":   self.total_pushes,
                "last_read":      last.isoformat() if last else None,
                "upstream":       self._upstream_status,
            }
