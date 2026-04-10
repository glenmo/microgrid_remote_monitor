#!/usr/bin/env python3
"""
Microgrid Remote Monitor — Solis Inverter + Eastron Energy Meter
================================================================
Reads registers from:
  1. Solis 50kW Hybrid Inverter — direct Modbus TCP (function code 0x04)
  2. Eastron SDM630MCT V2 Energy Meter — via Modbus TCP gateway

Each device can have its own IP address or they can share a gateway.

Usage:
    python app.py --host 0.0.0.0 --port 5000 \\
                  --solis-ip 192.168.11.214 --solis-port 502 --solis-id 1 \\
                  --eastron-ip 192.168.1.100 --eastron-port 502 --eastron-id 2
"""

import argparse
import json
import logging
import os
import struct
import threading
import time
from collections import deque
from datetime import datetime

from flask import Flask, jsonify, render_template, request
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException

from eastron_reader import EastronModbusReader
from sppro_reader import SPProModbusReader

# NOTE: pymodbus v3.6+ uses 'device_id' parameter.
# If using an older version (v3.0-3.5), change 'device_id' to 'slave' in
# the read_input_registers() calls in both this file and eastron_reader.py.

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("solis_monitor")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# ---------------------------------------------------------------------------
# Register map — Solis Hybrid Inverter (function code 0x04, input registers)
# Format: (start_register, count, name, data_type, unit, scale_divisor, description)
# For U32/S32 types, count=2 (two consecutive registers, high word first)
# ---------------------------------------------------------------------------
REGISTER_MAP = [
    # Inverter info
    (33000, 1, "model_no",              "U16", "",      1,     "Inverter model number"),
    (33022, 1, "year",                  "U16", "",      1,     "Inverter clock year"),
    (33023, 1, "month",                 "U16", "",      1,     "Inverter clock month"),
    (33024, 1, "day",                   "U16", "",      1,     "Inverter clock day"),
    (33025, 1, "hour",                  "U16", "",      1,     "Inverter clock hour"),
    (33026, 1, "minute",                "U16", "",      1,     "Inverter clock minute"),

    # PV (DC) inputs
    (33049, 1, "pv1_voltage",           "U16", "V",     10,    "PV String 1 Voltage"),
    (33050, 1, "pv1_current",           "U16", "A",     10,    "PV String 1 Current"),
    (33051, 1, "pv2_voltage",           "U16", "V",     10,    "PV String 2 Voltage"),
    (33052, 1, "pv2_current",           "U16", "A",     10,    "PV String 2 Current"),
    (33053, 1, "pv3_voltage",           "U16", "V",     10,    "PV String 3 Voltage"),
    (33054, 1, "pv3_current",           "U16", "A",     10,    "PV String 3 Current"),
    (33055, 1, "pv4_voltage",           "U16", "V",     10,    "PV String 4 Voltage"),
    (33056, 1, "pv4_current",           "U16", "A",     10,    "PV String 4 Current"),
    (33057, 2, "pv_total_power",        "U32", "W",     1,     "Total DC (PV) Power"),

    # PV energy
    (33035, 1, "pv_today_energy",       "U16", "kWh",   10,    "PV Generation Today"),
    (33029, 2, "pv_total_energy",       "U32", "kWh",   1,     "PV Total Energy Generation"),

    # AC grid side
    (33073, 1, "grid_voltage_ab",       "U16", "V",     10,    "Grid Voltage A-B (Phase A for 1PH)"),
    (33074, 1, "grid_voltage_bc",       "U16", "V",     10,    "Grid Voltage B-C"),
    (33075, 1, "grid_voltage_ca",       "U16", "V",     10,    "Grid Voltage C-A"),
    (33076, 1, "grid_current_a",        "U16", "A",     10,    "Grid Current Phase A"),
    (33077, 1, "grid_current_b",        "U16", "A",     10,    "Grid Current Phase B"),
    (33078, 1, "grid_current_c",        "U16", "A",     10,    "Grid Current Phase C"),
    (33079, 2, "active_power",          "S32", "W",     1,     "Active Power (+ export / - import)"),
    (33081, 2, "reactive_power",        "S32", "Var",   1,     "Reactive Power"),
    (33083, 2, "apparent_power",        "S32", "VA",    1,     "Apparent Power"),
    (33094, 1, "grid_frequency",        "U16", "Hz",    100,   "Grid Frequency"),

    # Battery
    (33133, 1, "battery_voltage",       "U16", "V",     10,    "Battery Voltage"),
    (33134, 1, "battery_current",       "S16", "A",     10,    "Battery Current"),
    (33135, 1, "battery_current_dir",   "U16", "",      1,     "Battery Direction (0=charge, 1=discharge)"),
    (33139, 1, "battery_soc",           "U16", "%",     1,     "Battery State of Charge"),
    (33140, 1, "battery_soh",           "U16", "%",     1,     "Battery State of Health"),
    (33141, 1, "bms_battery_voltage",   "U16", "V",     100,   "BMS Battery Voltage"),
    (33142, 1, "bms_battery_current",   "S16", "A",     10,    "BMS Battery Current"),
    (33143, 1, "bms_charge_limit",      "U16", "A",     10,    "BMS Charge Current Limit"),
    (33144, 1, "bms_discharge_limit",   "U16", "A",     10,    "BMS Discharge Current Limit"),

    # Temperatures
    (33093, 1, "inverter_temp",         "S16", "°C",    10,    "Inverter Module Temperature"),
    (33046, 1, "battery_mos_temp",      "S16", "°C",    10,    "Battery MOS Temperature (S6 only)"),

    # Status
    (33091, 1, "working_mode",          "U16", "",      1,     "Standard Working Mode"),
    (33095, 1, "inverter_status",       "U16", "",      1,     "Inverter Current Status (see Appendix 2)"),
    (33121, 1, "operating_status",      "U16", "",      1,     "Operating Status (see Appendix 5)"),
    (33111, 1, "bms_status",            "U16", "",      1,     "Battery BMS Status"),

    # Fault codes
    (33116, 1, "fault_code_01",         "U16", "",      1,     "Fault Code 01"),
    (33117, 1, "fault_code_02",         "U16", "",      1,     "Fault Code 02"),
    (33118, 1, "fault_code_03",         "U16", "",      1,     "Fault Code 03"),
    (33119, 1, "fault_code_04",         "U16", "",      1,     "Fault Code 04"),

    # DC Bus
    (33071, 1, "dc_bus_voltage",        "U16", "V",     10,    "DC Bus Voltage"),

    # Backup output
    (33137, 1, "backup_voltage",        "U16", "V",     10,    "Backup AC Voltage (Phase A)"),
    (33138, 1, "backup_current",        "U16", "A",     10,    "Backup AC Current (Phase A)"),
]

# Lookup table for working mode codes
WORKING_MODES = {
    0: "No response",
    1: "Volt-watt default",
    2: "Volt-var",
    3: "Fixed power factor",
    4: "Fix reactive power",
    5: "Power-PF",
    6: "Rule21 Volt-watt",
    12: "IEEE1547-2018 P-Q",
}

# Lookup table for BMS status
BMS_STATUS = {
    0: "Normal",
    1: "Comms Abnormal",
    2: "BMS Warning",
}


# ---------------------------------------------------------------------------
# Modbus reader class
# ---------------------------------------------------------------------------
class SolisModbusReader:
    """Periodically reads registers from the Solis inverter via Modbus TCP."""

    def __init__(self, inverter_ip, inverter_port=502, slave_id=1, poll_interval=5,
                 shared_client=None, shared_client_lock=None):
        self.inverter_ip = inverter_ip
        self.inverter_port = inverter_port
        self.slave_id = slave_id
        self.poll_interval = poll_interval

        # Shared client support — when multiple devices are on the same gateway,
        # we share one TCP connection and use a lock to prevent simultaneous access
        self._shared_client = shared_client
        self._shared_client_lock = shared_client_lock
        self.client = shared_client
        self.connected = shared_client is not None
        self.last_read_time = None
        self.read_errors = 0
        self.total_reads = 0

        # Current values
        self.data = {}
        self.raw_data = {}

        # History for charts (keep last 24 hours at ~5s intervals = ~17280 points,
        # but we'll downsample to 1-minute averages for the chart = 1440 points)
        self.history_max = 1440
        self.history = {
            "timestamps": deque(maxlen=self.history_max),
            "battery_soc": deque(maxlen=self.history_max),
            "pv_total_power": deque(maxlen=self.history_max),
            "active_power": deque(maxlen=self.history_max),
            "battery_power": deque(maxlen=self.history_max),
            "battery_voltage": deque(maxlen=self.history_max),
            "grid_frequency": deque(maxlen=self.history_max),
        }
        self._last_history_minute = -1

        # Lock for thread safety
        self.lock = threading.Lock()

        # Polling thread
        self._stop_event = threading.Event()
        self._thread = None

    def connect(self):
        """Establish Modbus TCP connection (skipped if using shared client)."""
        if self._shared_client is not None:
            self.client = self._shared_client
            self.connected = self.client.is_socket_open() if hasattr(self.client, 'is_socket_open') else True
            if self.connected:
                log.info(f"Solis: Using shared connection to {self.inverter_ip}:{self.inverter_port}")
            return
        try:
            self.client = ModbusTcpClient(
                host=self.inverter_ip,
                port=self.inverter_port,
                timeout=5,
            )
            self.connected = self.client.connect()
            if self.connected:
                log.info(f"Connected to Solis inverter at {self.inverter_ip}:{self.inverter_port}")
            else:
                log.warning(f"Failed to connect to {self.inverter_ip}:{self.inverter_port}")
        except Exception as e:
            log.error(f"Connection error: {e}")
            self.connected = False

    def disconnect(self):
        """Close the Modbus connection (skipped if using shared client)."""
        if self._shared_client is not None:
            return  # Don't close shared connection
        if self.client:
            self.client.close()
            self.connected = False
            log.info("Disconnected from inverter")

    def _read_registers_batch(self, start, count):
        """Read a batch of input registers (function code 0x04).

        The Solis protocol recommends max 50 registers per frame with
        >300ms between frames.
        """
        if not self.connected:
            self.connect()
            if not self.connected:
                return None
        try:
            result = self.client.read_input_registers(
                address=start,
                count=count,
                device_id=self.slave_id,
            )
            if isinstance(result, ModbusIOException) or result.isError():
                log.warning(f"Modbus read error at register {start}: {result}")
                return None
            return result.registers
        except Exception as e:
            log.error(f"Exception reading registers at {start}: {e}")
            self.connected = False
            return None

    def _decode_value(self, registers, offset, count, data_type, scale):
        """Decode register value(s) according to data type."""
        if offset >= len(registers):
            return None

        if data_type == "U16":
            raw = registers[offset]
            return raw / scale

        elif data_type == "S16":
            raw = registers[offset]
            # Convert unsigned to signed 16-bit
            if raw >= 0x8000:
                raw -= 0x10000
            return raw / scale

        elif data_type == "U32":
            if offset + 1 >= len(registers):
                return None
            # High word first, low word second
            raw = (registers[offset] << 16) | registers[offset + 1]
            return raw / scale

        elif data_type == "S32":
            if offset + 1 >= len(registers):
                return None
            raw = (registers[offset] << 16) | registers[offset + 1]
            # Convert unsigned to signed 32-bit
            if raw >= 0x80000000:
                raw -= 0x100000000
            return raw / scale

        return None

    def poll_once(self):
        """Read all registers from the inverter."""
        new_data = {}
        new_raw = {}
        success = True

        # Read each register entry individually or in small groups
        # The Solis spec recommends max 50 registers per frame with >300ms gap
        sorted_regs = sorted(REGISTER_MAP, key=lambda r: r[0])

        # Acquire the shared client lock if we're sharing a connection.
        # This prevents the Eastron reader from using the TCP socket at the
        # same time (RS485 is half-duplex — only one device talks at a time).
        bus_lock = self._shared_client_lock
        if bus_lock:
            bus_lock.acquire()

        try:
            # Read register by register (or small groups for U32/S32)
            for reg_addr, reg_count, name, dtype, unit, scale, desc in sorted_regs:
                registers = self._read_registers_batch(reg_addr, reg_count)
                if registers is not None and len(registers) == reg_count:
                    value = self._decode_value(registers, 0, reg_count, dtype, scale)
                    if value is not None:
                        new_data[name] = value
                        new_raw[name] = registers[0] if reg_count == 1 else list(registers)
                else:
                    success = False

                # Small delay between reads (Solis needs >300ms between frames)
                time.sleep(0.05)
        finally:
            if bus_lock:
                bus_lock.release()

        if not new_data:
            self.read_errors += 1
            return

        # Calculate battery power (voltage * current, with direction)
        if "battery_voltage" in new_data and "battery_current" in new_data:
            batt_v = new_data["battery_voltage"]
            batt_i = abs(new_data["battery_current"])
            direction = new_data.get("battery_current_dir", 0)
            batt_power = batt_v * batt_i
            if direction == 1:  # discharging
                batt_power = -batt_power
            new_data["battery_power"] = round(batt_power, 1)

        # Add decoded status strings
        wm = new_data.get("working_mode", 0)
        new_data["working_mode_str"] = WORKING_MODES.get(int(wm), f"Unknown ({int(wm)})")

        bms = new_data.get("bms_status", 0)
        new_data["bms_status_str"] = BMS_STATUS.get(int(bms), f"Unknown ({int(bms)})")

        # Add metadata
        now = datetime.now()
        new_data["_timestamp"] = now.isoformat()
        new_data["_read_ok"] = success

        self.total_reads += 1
        if not success:
            self.read_errors += 1

        # Update shared state
        with self.lock:
            self.data = new_data
            self.raw_data = new_raw
            self.last_read_time = now

            # Append to history (once per minute)
            current_minute = now.minute
            if current_minute != self._last_history_minute:
                self._last_history_minute = current_minute
                self.history["timestamps"].append(now.strftime("%H:%M"))
                for key in ["battery_soc", "pv_total_power", "active_power",
                            "battery_power", "battery_voltage", "grid_frequency"]:
                    self.history[key].append(new_data.get(key, 0))

    def _poll_loop(self):
        """Background polling loop."""
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.error(f"Poll error: {e}")
            self._stop_event.wait(self.poll_interval)

    def start(self):
        """Start background polling thread."""
        self.connect()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info(f"Polling started (every {self.poll_interval}s)")

    def stop(self):
        """Stop polling and disconnect."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.disconnect()

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
            "inverter_ip": self.inverter_ip,
            "inverter_port": self.inverter_port,
            "slave_id": self.slave_id,
            "poll_interval": self.poll_interval,
            "total_reads": self.total_reads,
            "read_errors": self.read_errors,
            "last_read": self.last_read_time.isoformat() if self.last_read_time else None,
        }


# ---------------------------------------------------------------------------
# Global reader instances (created in main)
# ---------------------------------------------------------------------------
reader: SolisModbusReader = None
eastron: EastronModbusReader = None
sppro: SPProModbusReader = None


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Serve the dashboard page."""
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    """Return current inverter data as JSON."""
    if reader is None:
        return jsonify({"error": "Reader not initialised"}), 503
    return jsonify(reader.get_data())


@app.route("/api/history")
def api_history():
    """Return historical data for charts."""
    if reader is None:
        return jsonify({"error": "Reader not initialised"}), 503
    return jsonify(reader.get_history())


@app.route("/api/status")
def api_status():
    """Return system/connection status."""
    if reader is None:
        return jsonify({"error": "Reader not initialised"}), 503
    return jsonify(reader.get_status())


# ---------------------------------------------------------------------------
# Eastron SDM630MCT API routes
# ---------------------------------------------------------------------------
@app.route("/api/eastron/data")
def api_eastron_data():
    """Return current Eastron meter data as JSON."""
    if eastron is None:
        return jsonify({"error": "Eastron reader not initialised"}), 503
    return jsonify(eastron.get_data())


@app.route("/api/eastron/history")
def api_eastron_history():
    """Return Eastron historical data for charts."""
    if eastron is None:
        return jsonify({"error": "Eastron reader not initialised"}), 503
    return jsonify(eastron.get_history())


@app.route("/api/eastron/status")
def api_eastron_status():
    """Return Eastron connection/polling status."""
    if eastron is None:
        return jsonify({"error": "Eastron reader not initialised"}), 503
    return jsonify(eastron.get_status())


# ---------------------------------------------------------------------------
# SP Pro API routes
# ---------------------------------------------------------------------------
@app.route("/api/sppro/data")
def api_sppro_data():
    """Return current SP Pro inverter data as JSON."""
    if sppro is None:
        return jsonify({"error": "SP Pro reader not initialised"}), 503
    return jsonify(sppro.get_data())


@app.route("/api/sppro/history")
def api_sppro_history():
    """Return SP Pro historical data for charts."""
    if sppro is None:
        return jsonify({"error": "SP Pro reader not initialised"}), 503
    return jsonify(sppro.get_history())


@app.route("/api/sppro/status")
def api_sppro_status():
    """Return SP Pro connection/polling status."""
    if sppro is None:
        return jsonify({"error": "SP Pro reader not initialised"}), 503
    return jsonify(sppro.get_status())


# ---------------------------------------------------------------------------
# Editable message (read from message.txt in the app directory)
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
# Entry point
# ---------------------------------------------------------------------------
def main():
    global reader, eastron, sppro

    parser = argparse.ArgumentParser(
        description="Microgrid Remote Monitor — Solis Inverter + Eastron Energy Meter"
    )

    # Web server
    parser.add_argument("--host", default="0.0.0.0",
                        help="Flask listen address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Flask listen port (default: 5000)")

    # Solis inverter — direct Modbus TCP connection
    parser.add_argument("--solis-ip", default="192.168.11.214",
                        help="Solis inverter Modbus TCP IP address (default: 192.168.11.214)")
    parser.add_argument("--solis-port", type=int, default=502,
                        help="Solis inverter Modbus TCP port (default: 502)")
    parser.add_argument("--solis-id", type=int, default=1,
                        help="Modbus slave/device ID of the Solis inverter (default: 1)")
    parser.add_argument("--solis-poll", type=int, default=5,
                        help="Solis poll interval in seconds (default: 5)")
    parser.add_argument("--no-solis", action="store_true",
                        help="Disable the Solis inverter reader")

    # Eastron energy meter — via Modbus TCP gateway
    parser.add_argument("--eastron-ip", default="192.168.11.214",
                        help="Eastron meter Modbus TCP gateway IP (default: 192.168.11.214)")
    parser.add_argument("--eastron-port", type=int, default=502,
                        help="Eastron meter Modbus TCP port (default: 502)")
    parser.add_argument("--eastron-id", type=int, default=2,
                        help="Modbus slave/device ID of the Eastron meter (default: 2)")
    parser.add_argument("--eastron-poll", type=int, default=5,
                        help="Eastron poll interval in seconds (default: 5)")
    parser.add_argument("--no-eastron", action="store_true",
                        help="Disable the Eastron meter reader")

    # SP Pro inverter — Modbus TCP
    parser.add_argument("--sppro-ip", default="192.168.11.240",
                        help="SP Pro Modbus TCP IP address (default: 192.168.11.240)")
    parser.add_argument("--sppro-port", type=int, default=502,
                        help="SP Pro Modbus TCP port (default: 502)")
    parser.add_argument("--sppro-poll", type=int, default=5,
                        help="SP Pro poll interval in seconds (default: 5)")
    parser.add_argument("--no-sppro", action="store_true",
                        help="Disable the SP Pro inverter reader")

    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode")

    # Legacy compatibility: support old --gateway-ip / --inverter-ip / --slave-id args
    parser.add_argument("--gateway-ip", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--gateway-port", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--inverter-ip", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--inverter-port", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--slave-id", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--poll-interval", type=int, default=None, help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Legacy arg mapping — old args override new ones if provided
    solis_ip = args.inverter_ip or args.gateway_ip or args.solis_ip
    solis_port = args.inverter_port or args.gateway_port or args.solis_port
    solis_id = args.slave_id or args.solis_id
    solis_poll = args.poll_interval or args.solis_poll
    eastron_ip = args.gateway_ip or args.eastron_ip
    eastron_port = args.gateway_port or args.eastron_port

    # If both devices share the same IP:port, create a single shared Modbus TCP
    # connection.  RS485 is half-duplex so only one reader can use it at a time;
    # the bus_lock ensures this.
    shared_client = None
    bus_lock = None
    both_enabled = not args.no_solis and not args.no_eastron
    same_gateway = (solis_ip == eastron_ip and solis_port == eastron_port)

    if both_enabled and same_gateway:
        log.info(f"Both devices on same gateway {solis_ip}:{solis_port} — sharing connection")
        shared_client = ModbusTcpClient(
            host=solis_ip,
            port=solis_port,
            timeout=10,
        )
        if not shared_client.connect():
            log.error(f"Failed to connect to shared gateway {solis_ip}:{solis_port}")
            shared_client = None
        else:
            log.info(f"Shared Modbus TCP connection established")
        bus_lock = threading.Lock()

    # Start Solis reader
    if not args.no_solis:
        reader = SolisModbusReader(
            inverter_ip=solis_ip,
            inverter_port=solis_port,
            slave_id=solis_id,
            poll_interval=solis_poll,
            shared_client=shared_client,
            shared_client_lock=bus_lock,
        )
        reader.start()

    # Start Eastron reader
    if not args.no_eastron:
        eastron = EastronModbusReader(
            gateway_ip=eastron_ip,
            gateway_port=eastron_port,
            slave_id=args.eastron_id,
            poll_interval=args.eastron_poll,
            shared_client=shared_client,
            shared_client_lock=bus_lock,
        )
        eastron.start()

    # Start SP Pro reader
    if not args.no_sppro:
        sppro = SPProModbusReader(
            ip=args.sppro_ip,
            port=args.sppro_port,
            poll_interval=args.sppro_poll,
        )
        sppro.start()

    log.info(f"Starting web server on {args.host}:{args.port}")
    try:
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        if reader:
            reader.stop()
        if eastron:
            eastron.stop()
        if sppro:
            sppro.stop()
        if shared_client:
            shared_client.close()
            log.info("Shared Modbus connection closed")


if __name__ == "__main__":
    main()
