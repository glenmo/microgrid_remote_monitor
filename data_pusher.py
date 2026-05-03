#!/usr/bin/env python3
"""
Microgrid Data Pusher
=====================
Runs alongside app.py on the Raspberry Pi. Fetches the latest data from
the local Flask API and pushes it to the remote server every 60 seconds.

Usage:
    python data_pusher.py --server-url https://monitor.mooramoora.org.au --api-key YOUR_SECRET_KEY

Environment variable alternative:
    export MONITOR_API_KEY=YOUR_SECRET_KEY
    python data_pusher.py --server-url https://monitor.mooramoora.org.au
"""

import argparse
import json
import logging
import os
import time

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("data_pusher")


def fetch_local(url):
    """Fetch JSON from local Flask API, return dict or None."""
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # Only return if there's actual data (not empty dict)
            if data and len(data) > 1:
                return data
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
    return None


def push_to_server(server_url, api_key, payload):
    """POST JSON payload to the remote server."""
    try:
        r = requests.post(
            f"{server_url}/api/push",
            json=payload,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code == 200:
            result = r.json()
            log.info(f"Push OK — server push #{result.get('push_count', '?')}")
            return True
        else:
            log.warning(f"Push failed — HTTP {r.status_code}: {r.text[:200]}")
    except requests.exceptions.ConnectionError:
        log.warning(f"Push failed — cannot connect to {server_url}")
    except Exception as e:
        log.warning(f"Push failed — {e}")
    return False




def sd_notify_status(msg: str):
    """Update systemd's per-unit Status: string. No-op if systemd-notify
    is unavailable (e.g. running interactively, not under systemd)."""
    import subprocess
    try:
        subprocess.run(
            ["systemd-notify", f"--status={msg}"],
            check=False, timeout=2, capture_output=True,
        )
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Push microgrid data from Pi to remote server"
    )
    parser.add_argument("--server-url", required=True,
                        help="Remote server URL (e.g. https://monitor.mooramoora.org.au)")
    parser.add_argument("--api-key", default=None,
                        help="API key for server (or set MONITOR_API_KEY env var)")
    parser.add_argument("--local-url", default="http://127.0.0.1:5000",
                        help="Local Flask API URL for Solis (default: http://127.0.0.1:5000)")
    parser.add_argument("--sppro-url", default="http://127.0.0.1:5000",
                        help="Local Flask API URL for SP Pro (default: http://127.0.0.1:5000)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Push interval in seconds (default: 60)")

    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("MONITOR_API_KEY", "")
    if not api_key:
        log.error("No API key provided. Use --api-key or set MONITOR_API_KEY env var.")
        return

    server_url = args.server_url.rstrip("/")
    local_url = args.local_url.rstrip("/")
    sppro_url = args.sppro_url.rstrip("/")

    log.info(f"Data pusher started")
    sd_notify_status("Starting up...")
    log.info(f"  Solis API:   {local_url}")
    log.info(f"  SP Pro API:  {sppro_url}")
    log.info(f"  Remote server: {server_url}")
    log.info(f"  Push interval: {args.interval}s")

    consecutive_failures = 0

    while True:
        # Fetch latest data from local Flask app
        payload = {}

        solis_data = fetch_local(f"{local_url}/api/data")
        if solis_data:
            payload["solis"] = solis_data

        eastron_data = fetch_local(f"{local_url}/api/eastron/data")
        if eastron_data:
            payload["eastron"] = eastron_data

        sppro_data = fetch_local(f"{sppro_url}/api/sppro/data")
        if sppro_data:
            payload["sppro"] = sppro_data

        if payload:
            success = push_to_server(server_url, api_key, payload)
            from datetime import datetime as _dt
            now = _dt.now().strftime("%H:%M:%S")
            if success:
                consecutive_failures = 0
                # Build a status string showing what we just pushed
                tags = []
                if "solis" in payload:
                    soc = payload["solis"].get("battery_soc")
                    if soc is not None:
                        tags.append(f"Solis {soc:.0f}%")
                if "sppro" in payload:
                    soc = payload["sppro"].get("battery_soc")
                    if soc is not None:
                        tags.append(f"SP Pro {soc:.0f}%")
                tag_str = ", ".join(tags) if tags else "no soc data"
                sd_notify_status(f"OK at {now}: {tag_str}")
            else:
                consecutive_failures += 1
                sd_notify_status(
                    f"FAIL at {now} (consecutive: {consecutive_failures})"
                )
                if consecutive_failures >= 5:
                    log.warning(f"  {consecutive_failures} consecutive failures")
        else:
            log.info("No data available from local API — skipping push")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
