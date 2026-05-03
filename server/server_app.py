#!/usr/bin/env python3
"""
Microgrid Remote Monitor — Server App
======================================
Receives data pushed from the Raspberry Pi and serves a public dashboard.

Runs behind Apache reverse proxy at monitor.mooramoora.org.au.

Endpoints:
    POST /api/push          — Pi pushes latest readings (requires API key)
    GET  /                  — Public dashboard
    GET  /api/data          — Latest Solis data as JSON
    GET  /api/eastron/data  — Latest Eastron data as JSON
    GET  /api/history       — Solis history (last 24h)
    GET  /api/eastron/history — Eastron history (last 24h)
    GET  /api/status        — Connection status and last update time
"""

import argparse
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, render_template, request, abort

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("monitor_server")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# ---------------------------------------------------------------------------
# Data store (in-memory with history)
# ---------------------------------------------------------------------------
MAX_HISTORY = 1440  # 24 hours at 1-minute intervals

data_lock = threading.Lock()

latest_solis = {}
latest_eastron = {}
latest_sppro = {}
solis_history = deque(maxlen=MAX_HISTORY)
eastron_history = deque(maxlen=MAX_HISTORY)
sppro_history = deque(maxlen=MAX_HISTORY)
last_push_time = None
push_count = 0

# API key for authenticating Pi pushes
API_KEY = os.environ.get("MONITOR_API_KEY", "change-me-to-a-secret-key")


def require_api_key(f):
    """Decorator to require API key on push endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if key != API_KEY:
            abort(403, description="Invalid API key")
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Push endpoint — Pi sends data here
# ---------------------------------------------------------------------------
@app.route("/api/push", methods=["POST"])
@require_api_key
def api_push():
    """Receive data from the Raspberry Pi."""
    global latest_solis, latest_eastron, latest_sppro, last_push_time, push_count

    payload = request.get_json(silent=True)
    if not payload:
        abort(400, description="Expected JSON body")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with data_lock:
        if "solis" in payload:
            latest_solis = payload["solis"]
            latest_solis["_received_at"] = now
            # Add to history
            hist_entry = {
                "timestamp": now,
                "soc": latest_solis.get("battery_soc", 0),
                "pv_power": latest_solis.get("pv_total_power", 0),
                "battery_power": latest_solis.get("battery_power", 0),
                "grid_power": latest_solis.get("active_power", 0),
            }
            solis_history.append(hist_entry)

        if "eastron" in payload:
            latest_eastron = payload["eastron"]
            latest_eastron["_received_at"] = now
            # Add to history
            hist_entry = {
                "timestamp": now,
                "total_power": latest_eastron.get("total_power", 0),
                "import_kwh": latest_eastron.get("import_kwh", 0),
                "export_kwh": latest_eastron.get("export_kwh", 0),
                "voltage_avg": latest_eastron.get("voltage_avg", 0),
                "frequency": latest_eastron.get("frequency", 0),
            }
            eastron_history.append(hist_entry)

        if "sppro" in payload:
            latest_sppro = payload["sppro"]
            latest_sppro["_received_at"] = now
            # Add to history
            hist_entry = {
                "timestamp": now,
                "soc": latest_sppro.get("battery_soc", 0),
                "pv_power": latest_sppro.get("pv_power", 0),
                "load_power": latest_sppro.get("load_power", 0),
                "grid_power": latest_sppro.get("grid_power", 0),
                "battery_power": latest_sppro.get("battery_power", 0),
            }
            sppro_history.append(hist_entry)

        last_push_time = now
        push_count += 1

    log.info(f"Push #{push_count} received — solis: {'yes' if 'solis' in payload else 'no'}, "
             f"eastron: {'yes' if 'eastron' in payload else 'no'}, "
             f"sppro: {'yes' if 'sppro' in payload else 'no'}")

    return jsonify({"status": "ok", "received_at": now, "push_count": push_count})


# ---------------------------------------------------------------------------
# Message file — editable text displayed on the dashboard
# ---------------------------------------------------------------------------
MESSAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "message.txt")


@app.route("/api/message")
def api_message():
    """Return the current dashboard message from the text file."""
    try:
        if os.path.exists(MESSAGE_FILE):
            with open(MESSAGE_FILE, "r") as f:
                return jsonify({"message": f.read().strip()})
    except Exception as e:
        log.warning(f"Error reading message file: {e}")
    return jsonify({"message": ""})


# ---------------------------------------------------------------------------
# Public API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/data")
def api_solis_data():
    with data_lock:
        return jsonify(latest_solis)


@app.route("/api/eastron/data")
def api_eastron_data():
    with data_lock:
        return jsonify(latest_eastron)


@app.route("/api/history")
def api_solis_history():
    with data_lock:
        return jsonify(list(solis_history))


@app.route("/api/eastron/history")
def api_eastron_history():
    with data_lock:
        return jsonify(list(eastron_history))


@app.route("/api/sppro/data")
def api_sppro_data():
    with data_lock:
        return jsonify(latest_sppro)


@app.route("/api/sppro/history")
def api_sppro_history():
    with data_lock:
        return jsonify(list(sppro_history))



@app.route("/api/solis/data")
def api_solis_data_alias():
    return api_solis_data()


@app.route("/api/solis/history")
def api_solis_history_alias():
    return api_solis_history()


@app.route("/api/solis/status")
def api_solis_status_alias():
    """Per-inverter status synthesised from push state."""
    with data_lock:
        from datetime import datetime as _dt
        last = last_push_time
        connected = bool(latest_solis and latest_solis.get("battery_soc") is not None)
        if connected and last:
            try:
                age = (_dt.now() - _dt.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
                if age > 300:
                    connected = False
            except Exception:
                pass
        return jsonify({
            "connected": connected,
            "host": "(pushed via mooramoora.org.au)",
            "inverter_ip": "192.168.11.214",
            "last_read": last,
            "total_reads": push_count,
        })


@app.route("/api/sppro/status")
def api_sppro_status_alias():
    with data_lock:
        from datetime import datetime as _dt
        last = last_push_time
        connected = bool(latest_sppro and latest_sppro.get("battery_soc") is not None)
        if connected and last:
            try:
                age = (_dt.now() - _dt.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
                if age > 300:
                    connected = False
            except Exception:
                pass
        return jsonify({
            "connected": connected,
            "host": "(pushed via mooramoora.org.au)",
            "inverter_ip": "192.168.11.240",
            "last_read": last,
            "total_reads": push_count,
        })


@app.route("/api/status")
def api_status():
    with data_lock:
        return jsonify({
            "last_push": last_push_time,
            "push_count": push_count,
            "solis_connected": bool(latest_solis),
            "eastron_connected": bool(latest_eastron),
            "sppro_connected": bool(latest_sppro),
            "solis_history_points": len(solis_history),
            "eastron_history_points": len(eastron_history),
            "sppro_history_points": len(sppro_history),
        })


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    return render_template("combined_v2.html")@app.route("/combined/")
def combined_dashboard():
    return render_template("server_combined_dashboard.html")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Microgrid Remote Monitor — Server"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8100,
                        help="Listen port (default: 8100)")
    parser.add_argument("--api-key", default=None,
                        help="API key for push endpoint (or set MONITOR_API_KEY env var)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode")

    args = parser.parse_args()

    global API_KEY
    if args.api_key:
        API_KEY = args.api_key

    if API_KEY == "change-me-to-a-secret-key":
        log.warning("=== WARNING: Using default API key! Set MONITOR_API_KEY or use --api-key ===")

    log.info(f"Starting server on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
