#!/usr/bin/env python3
"""SP Pro USB pusher — runs on `kitty`.

kitty is cabled by USB directly into the SP Pro's Advanced Comms Board
(CDC-ACM at /dev/ttyACM0). This reads the SP Pro locally via selpi
(SPProSxReader in serial mode) and POSTs each reading to rubberduck's
/api/sppro/ingest, where it is served through the normal SP Pro API.

This replaces the flaky Lantronix Ethernet-to-serial path: the SP Pro is
read over a local USB cable (reliable) and only the resulting JSON crosses
the network, on the fast local LAN to rubberduck.

Config via environment (see sppro-pusher.service):
    INGEST_URL       where to POST          (default http://rubberduck.local:5000/api/sppro/ingest)
    INGEST_TOKEN     optional shared token  (sent as X-Ingest-Token)
    SPPRO_SERIAL     serial device          (default /dev/ttyACM0)
    SPPRO_PASSWORD   selpi handshake string (default "Selectronic SP PRO")
    SPPRO_POLL       seconds between reads   (default 10)
"""
import json
import logging
import os
import sys
import time
import urllib.request

# Import SPProSxReader from the repo root (this file lives in <repo>/kitty/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sppro_sx_reader import SPProSxReader  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sppro_pusher")

INGEST_URL = os.environ.get("INGEST_URL",
                            "http://rubberduck.local:5000/api/sppro/ingest")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
SERIAL_PORT = os.environ.get("SPPRO_SERIAL", "/dev/ttyACM0")
PASSWORD = os.environ.get("SPPRO_PASSWORD", "Selectronic SP PRO")
POLL = int(os.environ.get("SPPRO_POLL", "10"))


def push(payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(INGEST_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if INGEST_TOKEN:
        req.add_header("X-Ingest-Token", INGEST_TOKEN)
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def main():
    log.info("SP Pro pusher: reading %s, posting to %s every %ds",
             SERIAL_PORT, INGEST_URL, POLL)
    reader = SPProSxReader(transport="serial", serial_port=SERIAL_PORT,
                           password=PASSWORD, poll_interval=POLL)
    while True:
        # poll_once() opens the serial port on first use and reconnects on
        # error internally; we drive the cadence here so reads and pushes
        # stay in lockstep.
        reader.poll_once()
        payload = {"data": reader.get_data(), "status": reader.get_status()}
        soc = payload["data"].get("battery_soc")
        connected = payload["status"].get("connected")
        try:
            push(payload)
            log.info("pushed (connected=%s, soc=%s)", connected, soc)
        except Exception as e:  # noqa: BLE001
            log.warning("push failed: %s: %s", type(e).__name__, e)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
