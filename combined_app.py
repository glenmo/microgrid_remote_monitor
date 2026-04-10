#!/usr/bin/env python3
"""
Microgrid Combined Dashboard — Solis + SP Pro (SwitchDin)
=========================================================
Serves a single dashboard on port 5003 showing both systems side by side.

  - Solis 50kW Hybrid Inverter  — direct Modbus TCP (192.168.11.214:502)
  - Selectronic SP Pro           — via SwitchDin Stormcloud cloud API

Usage:
    python combined_app.py \
        --solis-ip 192.168.11.214 --solis-port 502 --solis-id 1 \
        --switchdin-user user@example.com --switchdin-pass SECRET \
        --port 5003
"""

import argparse
import logging
import os
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, render_template

# Import readers from existing modules
from app import SolisModbusReader
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
# Global reader instances
# ---------------------------------------------------------------------------
solis: SolisModbusReader = None
switchdin: SwitchDinReader = None


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Serve the combined dashboard page."""
    return render_template("combined_dashboard.html")


# ---- Solis API endpoints ----
@app.route("/api/solis/data")
def api_solis_data():
    if solis is None:
        return jsonify({"error": "Solis reader not initialised"}), 503
    return jsonify(solis.get_data())


@app.route("/api/solis/history")
def api_solis_history():
    if solis is None:
        return jsonify({"error": "Solis reader not initialised"}), 503
    return jsonify(solis.get_history())


@app.route("/api/solis/status")
def api_solis_status():
    if solis is None:
        return jsonify({"error": "Solis reader not initialised"}), 503
    return jsonify(solis.get_status())


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
    global solis, switchdin

    parser = argparse.ArgumentParser(
        description="Combined Microgrid Dashboard — Solis + SP Pro (SwitchDin)"
    )

    # Web server
    parser.add_argument("--host", default="0.0.0.0",
                        help="Flask listen address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5003,
                        help="Flask listen port (default: 5003)")

    # Solis inverter
    parser.add_argument("--solis-ip", default="192.168.11.214",
                        help="Solis inverter Modbus TCP IP (default: 192.168.11.214)")
    parser.add_argument("--solis-port", type=int, default=502,
                        help="Solis inverter Modbus TCP port (default: 502)")
    parser.add_argument("--solis-id", type=int, default=1,
                        help="Modbus slave/device ID (default: 1)")
    parser.add_argument("--solis-poll", type=int, default=5,
                        help="Solis poll interval in seconds (default: 5)")
    parser.add_argument("--no-solis", action="store_true",
                        help="Disable the Solis reader")

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

    # Start Solis reader
    if not args.no_solis:
        solis = SolisModbusReader(
            inverter_ip=args.solis_ip,
            inverter_port=args.solis_port,
            slave_id=args.solis_id,
            poll_interval=args.solis_poll,
        )
        solis.start()
    else:
        log.info("Solis reader disabled (--no-solis)")

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
        if solis:
            solis.stop()
        if switchdin:
            switchdin.stop()


if __name__ == "__main__":
    main()
