#!/usr/bin/env python3
"""SP Pro Sx Reader — Selectronic SP Pro 2 via Sx protocol over TCP.

Wraps the vendored `selpi` library in the same start/stop/get_data/
get_history/get_status interface as solis_reader.py.

Architecture:
    [SP Pro RS-232] - 57600 8N1 - [Lantronix xDirect @ host:port]
                                          |
                                          v
                               [SPProSxReader (this file)]
                                          |
                                          v
                               selpi.Statistics (auth + decode)
                                          |
                                          v
                               /api/sppro/{data,history,status}
"""
import logging
import os
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime


_SELPI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "vendor", "selpi")
if _SELPI_DIR not in sys.path:
    sys.path.insert(0, _SELPI_DIR)

from statistics import Statistics  # noqa: E402

log = logging.getLogger("sppro_sx_reader")


def _set_selpi_env(host, port, password):
    os.environ["SELPI_CONNECTION_TYPE"]         = "TCP"
    os.environ["SELPI_CONNECTION_TCP_HOSTNAME"] = host
    os.environ["SELPI_CONNECTION_TCP_PORT"]     = str(port)
    os.environ["SELPI_SPPRO_PASSWORD"]          = password or ""


def _set_selpi_env_serial(serial_port, baudrate, password):
    """Configure selpi to read the SP Pro over a local serial port.

    Used on `kitty`, which is cabled by USB directly into the SP Pro's
    Advanced Comms Board (CDC-ACM at /dev/ttyACM0) — no Lantronix, no
    network hop. selpi opens the port with pyserial (DTR/RTS asserted on
    open), which is what the comms board needs to start talking.
    """
    os.environ["SELPI_CONNECTION_TYPE"]            = "Serial"
    os.environ["SELPI_CONNECTION_SERIAL_PORT"]     = serial_port
    os.environ["SELPI_CONNECTION_SERIAL_BAUDRATE"] = str(baudrate)
    os.environ["SELPI_SPPRO_PASSWORD"]             = password or ""
    # Drop any stale TCP vars so selpi can't be misled by a previous config.
    os.environ.pop("SELPI_CONNECTION_TCP_HOSTNAME", None)
    os.environ.pop("SELPI_CONNECTION_TCP_PORT", None)


def _drain_lantronix_buffer(host, port, drain_ms=300):
    """Open a quick TCP socket, read+discard whatever is buffered, close.

    Lantronix xDirect adapters can hold N stale bytes from a previous
    session in their serial-receive buffer. selpi reads expected-length
    chunks, so stale prefix bytes shift the framing and every subsequent
    read fails CRC. Draining via a brief throwaway connection forces the
    Lantronix to flush those bytes (since 'Hard Disconnect: Yes' is set
    on the device).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((host, port))
        s.settimeout(drain_ms / 1000.0)
        total = 0
        try:
            while True:
                data = s.recv(256)
                if not data:
                    break
                total += len(data)
        except socket.timeout:
            pass
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()
        if total:
            log.info(f"SP Pro: drained {total} stale bytes from Lantronix buffer")
    except Exception as e:
        log.debug(f"SP Pro: drain attempt failed (harmless): {e}")


def _close_selpi_socket(stats):
    """Aggressively close any TCP socket held by a selpi Statistics object.

    We don't trust GC to run promptly between a failed login and the next
    reconnect attempt — if the Lantronix sees the new connection while
    the old one is still in its table, it rejects with "Attempted to
    start multiple logins". Probe a few likely attribute names and call
    close() on whatever we find.
    """
    if stats is None:
        return
    for attr in ("close", "disconnect", "stop"):
        fn = getattr(stats, attr, None)
        if callable(fn):
            try:
                fn()
                log.debug(f"SP Pro: selpi {attr}() called on old session")
                return
            except Exception as e:
                log.debug(f"SP Pro: selpi {attr}() raised (ignored): {e}")
    for attr in ("_connection", "_conn", "_socket", "_sock", "connection"):
        obj = getattr(stats, attr, None)
        if obj is not None and hasattr(obj, "close"):
            try:
                obj.close()
                log.debug(f"SP Pro: closed old selpi {attr}")
                return
            except Exception as e:
                log.debug(f"SP Pro: closing old selpi {attr} raised (ignored): {e}")


class SPProSxReader:
    """Polls a Selectronic SP Pro 2 via the Sx protocol over TCP."""

    def __init__(self, host="192.168.11.240", port=10001,
                 password="", poll_interval=10,
                 transport="tcp", serial_port="/dev/ttyACM0", baudrate=57600):
        # transport: "tcp" = via a Lantronix-style serial<->TCP bridge
        #            "serial" = direct local serial (e.g. kitty over USB)
        self.transport = transport
        self.serial_port = serial_port
        self.baudrate = baudrate

        self.host = host
        self.port = port
        self.password = password
        self.poll_interval = poll_interval

        self.connected      = False
        self.last_read_time = None
        self.read_errors    = 0
        self.total_reads    = 0

        self._consecutive_failures = 0
        self._FORCE_RECONNECT_AFTER = 3
        # Pause between tearing down a failed selpi session and opening
        # the next one — gives the Lantronix time to release the
        # previous TCP session from its connection table before we
        # come knocking again.
        self._RECONNECT_GRACE_S = 1.0

        self.data = {}
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
        self._stop_event = threading.Event()
        self._thread = None

        self._selpi_stats = None

    def _open_selpi(self):
        # Make sure any previous Statistics instance's socket is closed
        # *now* — don't wait for GC. If the Lantronix still sees the old
        # connection when we make the new one, it'll reject with
        # "Attempted to start multiple logins".
        if self._selpi_stats is not None:
            _close_selpi_socket(self._selpi_stats)
            self._selpi_stats = None
            # Brief grace period for the Lantronix to release the
            # previous TCP session before we reconnect.
            time.sleep(self._RECONNECT_GRACE_S)

        if self.transport == "serial":
            _set_selpi_env_serial(self.serial_port, self.baudrate, self.password)
            self._selpi_stats = Statistics()
            log.info(f"SP Pro: selpi configured for serial "
                     f"{self.serial_port}@{self.baudrate}")
            return

        _set_selpi_env(self.host, self.port, self.password)
        # Drain any stale bytes the Lantronix has buffered from a previous
        # session before selpi opens its own socket.
        _drain_lantronix_buffer(self.host, self.port)
        self._selpi_stats = Statistics()
        log.info(f"SP Pro: selpi configured for TCP {self.host}:{self.port}")

    def _force_reconnect(self, reason=""):
        if reason:
            log.info(f"SP Pro: forcing reconnect ({reason})")
        # Explicitly tear down the socket — the next poll_once will
        # build a fresh Statistics inside _open_selpi().
        if self._selpi_stats is not None:
            _close_selpi_socket(self._selpi_stats)
            self._selpi_stats = None
        self.connected = False

    def poll_once(self):
        try:
            if self._selpi_stats is None:
                self._open_selpi()
            raw = self._selpi_stats.get_select_emulated()

            # selpi wraps data inside an 'items' sub-dict (matches the
            # select.live cloud JSON shape). Flatten so the dashboard
            # JS can use d.battery_soc rather than d.items.battery_soc.
            new_data = dict(raw.get("items", {}))

            # Selectronic convention: battery_w POSITIVE = discharging.
            # Solis convention (and our dashboard's): POSITIVE = charging.
            # Flip the sign so the dashboard's setBadge / mode logic
            # (positive -> Charging) renders correctly for both inverters.
            if "battery_w" in new_data and new_data["battery_w"] is not None:
                new_data["battery_w"] = -new_data["battery_w"]

            new_data["_timestamp"] = datetime.now().isoformat()
            new_data["_device_name"] = raw.get("name", "SP Pro")

            self.connected = True
            self.total_reads += 1
            self._consecutive_failures = 0

            with self.lock:
                self.data = new_data
                self.last_read_time = datetime.now()
                now = datetime.now()
                if now.minute != self._last_history_minute:
                    self._last_history_minute = now.minute
                    self.history["timestamps"].append(now.strftime("%H:%M"))
                    for key in ("battery_soc", "battery_w", "grid_w",
                                "load_w", "shunt_w", "solarinverter_w"):
                        self.history[key].append(new_data.get(key, 0))
        except Exception as e:
            log.warning(f"SP Pro: poll error: {type(e).__name__}: {e}")
            self.read_errors += 1
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._FORCE_RECONNECT_AFTER:
                self._force_reconnect(
                    reason=f"{self._consecutive_failures} consecutive failures")
                self._consecutive_failures = 0

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.error(f"SP Pro: unexpected poll-loop error: {e}")
            self._stop_event.wait(self.poll_interval)

    def start(self):
        endpoint = (self.serial_port if self.transport == "serial"
                    else f"{self.host}:{self.port}")
        log.info(f"SP Pro: polling started ({self.transport} {endpoint}, "
                 f"every {self.poll_interval}s)")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._force_reconnect(reason="stop()")

    def get_data(self):
        with self.lock:
            return dict(self.data)

    def get_history(self):
        with self.lock:
            return {k: list(v) for k, v in self.history.items()}

    def get_status(self):
        with self.lock:
            endpoint = (self.serial_port if self.transport == "serial"
                        else f"{self.host}:{self.port}")
            return {
                "connected":     self.connected,
                "transport":     self.transport,
                "host":          endpoint,
                "port":          self.port,
                "poll_interval": self.poll_interval,
                "total_reads":   self.total_reads,
                "read_errors":   self.read_errors,
                "last_read":     self.last_read_time.isoformat() if self.last_read_time else None,
            }
