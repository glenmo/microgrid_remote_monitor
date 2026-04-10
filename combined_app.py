#!/usr/bin/env python3
"""
Microgrid Combined Dashboard — Solis + SP Pro (SwitchDin)
=========================================================
Serves a single dashboard on port 5003 showing both systems side by side.

  - Solis 50kW Hybrid Inverter  — proxied from the existing app on port 5000
  - Selectronic SP Pro           — via SwitchDin Stormcloud cloud API

The Solis inverter only accepts one Modbus TCP connection at a time, so
rather than competing with the app on port 5000 we proxy its JSON API.

Usage:
    python combined_app.py \
        --solis-url http://localhost:5000 \
        --switchdin-user user@example.com --switchdin-pass SECRET \
        --port 5003
"""

import argparse
import logging
import os

from flask import Flask, jsonify, render_template, Response
import requests as http_requests

from switchdin_reader import SwitchDinReader

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("combined_monitor")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
switchdin: SwitchDinReader = None
solis_base_url: str = "http://localhost:5000"


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Serve the combined dashboard page."""
    return render_template("combined_dashboard.html")


# ---- Solis API endpoints (proxy from port 5000) ----
def _proxy_solis(path):
    """Forward a request to the Solis app and relay the JSON response."""
    try:
        resp = http_requests.get(f"{solis_base_url}{path}", timeout=5)
        return Response(resp.content, status=resp.status_code,
                        content_type=resp.headers.get("Content-Type", "application/json"))
    except Exception as e:
        return jsonify({"error": f"Solis proxy error: {e}"}), 502


@app.route("/api/solis/data")
def api_solis_data():
    return _proxy_solis("/api/data")


@app.route("/api/solis/history")
def api_solis_history():
    return _proxy_solis("/api/history")


@app.route("/api/solis/status")
def api_solis_status():
    return _proxy_solis("/api/status")


# ---- SP Pro (SwitchDin) API endpoints ----
@app.route("/api/sppro/data")
def api_sppro_data():
    if switchdin is None:
        return jsonify({"error": "SwitchDin reader not initialised"}), 503
    return jsonify(switchdin.get_data())


@app.route("/api/sppro/history")
def api_sppro_history():
    if switchdin is None:
        return jsonify({"error": "SwitchDin reader not initialised"}), 503
    return jsonify(switchdin.get_history())


@app.route("/api/sppro/status")
def api_sppro_status():
    if switchdin is None:
        return jsonify({"error": "SwitchDin reader not initialised"}), 503
    return jsonify(switchdin.get_status())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    global switchdin, solis_base_url

    parser = argparse.ArgumentParser(
        description="Combined Microgrid Dashboard — Solis + SP Pro (SwitchDin)"
    )

    # Web server
    parser.add_argument("--host", default="0.0.0.0",
                        help="Flask listen address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5003,
                        help="Flask listen port (default: 5003)")

    # Solis — proxy from existing app
    parser.add_argument("--solis-url", default="http://localhost:5000",
                        help="Base URL of the existing Solis app (default: http://localhost:5000)")

    # SwitchDin (SP Pro via Stormcloud)
    parser.add_argument("--switchdin-user", default=None,
                        help="SwitchDin login username (email)")
    parser.add_argument("--switchdin-pass", default=None,
                        help="SwitchDin login password")
    parser.add_argument("--switchdin-uuid",
                        default="20cb2a7a-29b5-4f20-b460-0edf9f353e9f",
                        help="SwitchDin unit UUID")
    parser.add_argument("--switchdin-poll", type=int, default=60,
                        help="SwitchDin poll interval in seconds (default: 60)")
    parser.add_argument("--no-switchdin", action="store_true",
                        help="Disable the SwitchDin reader")

    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode")

    args = parser.parse_args()

    # Solis proxy URL
    solis_base_url = args.solis_url.rstrip("/")
    log.info(f"Solis data proxied from {solis_base_url}")

    # Start SwitchDin reader
    if not args.no_switchdin and args.switchdin_user and args.switchdin_pass:
        switchdin = SwitchDinReader(
            username=args.switchdin_user,
            password=args.switchdin_pass,
            unit_uuid=args.switchdin_uuid,
            poll_interval=args.switchdin_poll,
        )
        switchdin.start()
    elif not args.no_switchdin:
        log.warning("SwitchDin: --switchdin-user and --switchdin-pass required (skipping)")
    else:
        log.info("SwitchDin reader disabled (--no-switchdin)")

    log.info(f"Starting combined dashboard on {args.host}:{args.port}")
    try:
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        if switchdin:
            switchdin.stop()


if __name__ == "__main__":
    main()
