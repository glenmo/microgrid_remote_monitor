#!/usr/bin/env python3
"""
Eastron SDM630MCT V2 Modbus TCP Reader
=======================================
Reads registers from the Eastron SDM630MCT energy meter via Modbus TCP.

The SDM630MCT uses:
- Function code 0x04 (Read Input Registers)
- IEEE 754 floating-point format (2 registers = 1 float per value)
- Register addresses starting from 0x0000

This module is designed to share a Modbus TCP gateway with the Solis inverter
(same IP, different slave/device ID).
"""

import logging
import struct
import threading
import time
from collections import deque
from datetime import datetime

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException

log = logging.getLogger("eastron_reader")

# ---------------------------------------------------------------------------
# Register map — Eastron SDM630MCT V2 (function code 0x04, input registers)
# All values are IEEE 754 32-bit floats (2 registers each)
# Format: (register_address, name, unit, description)
# ---------------------------------------------------------------------------
EASTRON_REGISTER_MAP = [
    # Phase voltages (line to neutral)
    (0x0000, "voltage_l1",          "V",    "Phase 1 Line to Neutral Voltage"),
    (0x0002, "voltage_l2",          "V",    "Phase 2 Line to Neutral Voltage"),
    (0x0004, "voltage_l3",          "V",    "Phase 3 Line to Neutral Voltage"),

    # Phase currents
    (0x0006, "current_l1",          "A",    "Phase 1 Current"),
    (0x0008, "current_l2",          "A",    "Phase 2 Current"),
    (0x000A, "current_l3",          "A",    "Phase 3 Current"),

    # Phase active power
    (0x000C, "power_l1",            "W",    "Phase 1 Active Power"),
    (0x000E, "power_l2",            "W",    "Phase 2 Active Power"),
    (0x0010, "power_l3",            "W",    "Phase 3 Active Power"),

    # Phase apparent power (VA)
    (0x0012, "va_l1",               "VA",   "Phase 1 Apparent Power"),
    (0x0014, "va_l2",               "VA",   "Phase 2 Apparent Power"),
    (0x0016, "va_l3",               "VA",   "Phase 3 Apparent Power"),

    # Phase reactive power (VAr)
    (0x0018, "var_l1",              "VAr",  "Phase 1 Reactive Power"),
    (0x001A, "var_l2",              "VAr",  "Phase 2 Reactive Power"),
    (0x001C, "var_l3",              "VAr",  "Phase 3 Reactive Power"),

    # Phase power factor
    (0x001E, "pf_l1",              "",      "Phase 1 Power Factor"),
    (0x0020, "pf_l2",              "",      "Phase 2 Power Factor"),
    (0x0022, "pf_l3",              "",      "Phase 3 Power Factor"),

    # Averages and totals
    (0x002A, "voltage_avg",         "V",    "Average Line to Neutral Voltage"),
    (0x002E, "current_avg",         "A",    "Average Line Current"),
    (0x0030, "current_sum",         "A",    "Sum of Line Currents"),
    (0x0034, "total_power",         "W",    "Total System Power"),
    (0x0038, "total_va",            "VA",   "Total System Apparent Power"),
    (0x003C, "total_var",           "VAr",  "Total System Reactive Power"),
    (0x003E, "total_pf",            "",     "Total System Power Factor"),

    # Frequency
    (0x0046, "frequency",           "Hz",   "Line Frequency"),

    # Energy (import = consumed, export = generated/fed-back)
    (0x0048, "import_kwh",          "kWh",  "Total Import Active Energy"),
    (0x004A, "export_kwh",          "kWh",  "Total Export Active Energy"),
    (0x004C, "import_kvarh",        "kVArh","Total Import Reactive Energy"),
    (0x004E, "export_kvarh",        "kVArh","Total Export Reactive Energy"),

    # Line to line voltages
    (0x00C8, "voltage_l1_l2",       "V",    "Line 1 to Line 2 Voltage"),
    (0x00CA, "voltage_l2_l3",       "V",    "Line 2 to Line 3 Voltage"),
    (0x00CC, "voltage_l3_l1",       "V",    "Line 3 to Line 1 Voltage"),
    (0x00CE, "voltage_ll_avg",      "V",    "Average Line to Line Voltage"),

    # Neutral current
    (0x00E0, "neutral_current",     "A",    "Neutral Current"),

    # THD voltages
    (0x00EA, "thd_voltage_l1",      "%",    "Phase 1 Voltage THD"),
    (0x00EC, "thd_voltage_l2",      "%",    "Phase 2 Voltage THD"),
    (0x00EE, "thd_voltage_l3",      "%",    "Phase 3 Voltage THD"),

    # THD currents
    (0x00F0, "thd_current_l1",      "%",    "Phase 1 Current THD"),
    (0x00F2, "thd_current_l2",      "%",    "Phase 2 Current THD"),
    (0x00F4, "thd_current_l3",      "%",    "Phase 3 Current THD"),

    # Demand (max demand since last reset)
    (0x0100, "demand_current_l1",   "A",    "Phase 1 Current Demand"),
    (0x0102, "demand_current_l2",   "A",    "Phase 2 Current Demand"),
    (0x0104, "demand_current_l3",   "A",    "Phase 3 Current Demand"),
    (0x0108, "demand_power_max",    "W",    "Maximum Total System Power Demand"),

    # Total energy (combined import + export)
    (0x0156, "total_kwh",           "kWh",  "Total Active Energy"),
    (0x0158, "total_kvarh",         "kVArh","Total Reactive Energy"),
]


def _decode_ieee754_float(registers):
    """Decode two Modbus registers into an IEEE 754 32-bit float.

    The SDM630MCT sends high word first (big-endian register order).
    Each register is 16 bits; together they form a 32-bit float.
    """
    if len(registers) < 2:
        return None
    # Pack as two unsigned 16-bit values (big-endian), then unpack as float
    raw_bytes = struct.pack(">HH", registers[0], registers[1])
    value = struct.unpack(">f", raw_bytes)[0]
    # Guard against NaN / Inf
    if value != value or abs(value) > 1e9:
        return None
    return round(value, 3)


class EastronModbusReader:
    """Periodically reads registers from the Eastron SDM630MCT via Modbus TCP."""

    def __init__(self, gateway_ip, gateway_port=502, slave_id=2, poll_interval=5,
                 shared_client=None, shared_client_lock=None):
        self.gateway_ip = gateway_ip
        self.gateway_port = gateway_port
        self.slave_id = slave_id
        self.poll_interval = poll_interval

        # Shared client support — share one TCP connection with other readers
        self._shared_client = shared_client
        self._shared_client_lock = shared_client_lock
        self.client = shared_client
        self.connected = shared_client is not None
        self.last_read_time = None
        self.read_errors = 0
        self.total_reads = 0

        # Current values
        self.data = {}

        # History for charts (1-minute samples, 24 hours)
        self.history_max = 1440
        self.history = {
            "timestamps": deque(maxlen=self.history_max),
            "total_power": deque(maxlen=self.history_max),
            "import_kwh": deque(maxlen=self.history_max),
            "export_kwh": deque(maxlen=self.history_max),
            "voltage_avg": deque(maxlen=self.history_max),
            "frequency": deque(maxlen=self.history_max),
        }
        self._last_history_minute = -1

        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    def connect(self):
        """Establish Modbus TCP connection (skipped if using shared client)."""
        if self._shared_client is not None:
            self.client = self._shared_client
            self.connected = self.client.is_socket_open() if hasattr(self.client, 'is_socket_open') else True
            if self.connected:
                log.info(f"Eastron: Using shared connection to {self.gateway_ip}:{self.gateway_port}")
            return
        try:
            self.client = ModbusTcpClient(
                host=self.gateway_ip,
                port=self.gateway_port,
                timeout=5,
            )
            self.connected = self.client.connect()
            if self.connected:
                log.info(f"Eastron: Connected to gateway at {self.gateway_ip}:{self.gateway_port}")
            else:
                log.warning(f"Eastron: Failed to connect to {self.gateway_ip}:{self.gateway_port}")
        except Exception as e:
            log.error(f"Eastron: Connection error: {e}")
            self.connected = False

    def disconnect(self):
        """Close the Modbus connection (skipped if using shared client)."""
        if self._shared_client is not None:
            return  # Don't close shared connection
        if self.client:
            self.client.close()
            self.connected = False
            log.info("Eastron: Disconnected")

    def _read_float_register(self, address):
        """Read a single IEEE 754 float value (2 registers) from the meter."""
        if not self.connected:
            self.connect()
            if not self.connected:
                return None
        try:
            result = self.client.read_input_registers(
                address=address,
                count=2,
                device_id=self.slave_id,
            )
            if isinstance(result, ModbusIOException) or result.isError():
                log.warning(f"Eastron: Read error at register 0x{address:04X}: {result}")
                return None
            return _decode_ieee754_float(result.registers)
        except Exception as e:
            log.error(f"Eastron: Exception reading register 0x{address:04X}: {e}")
            self.connected = False
            return None

    def poll_once(self):
        """Read all registers from the Eastron meter."""
        new_data = {}
        success = True

        # Acquire the shared client lock if sharing a connection.
        # RS485 is half-duplex — only one device can be queried at a time.
        bus_lock = self._shared_client_lock
        if bus_lock:
            bus_lock.acquire()

        try:
            for reg_addr, name, unit, desc in EASTRON_REGISTER_MAP:
                value = self._read_float_register(reg_addr)
                if value is not None:
                    new_data[name] = value
                else:
                    success = False
                # Small delay between reads
                time.sleep(0.02)
        finally:
            if bus_lock:
                bus_lock.release()

        if not new_data:
            self.read_errors += 1
            return

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
            self.last_read_time = now

            # Append to history (once per minute)
            current_minute = now.minute
            if current_minute != self._last_history_minute:
                self._last_history_minute = current_minute
                self.history["timestamps"].append(now.strftime("%H:%M"))
                for key in ["total_power", "import_kwh", "export_kwh",
                            "voltage_avg", "frequency"]:
                    self.history[key].append(new_data.get(key, 0))

    def _poll_loop(self):
        """Background polling loop."""
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.error(f"Eastron: Poll error: {e}")
            self._stop_event.wait(self.poll_interval)

    def start(self):
        """Start background polling thread."""
        self.connect()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info(f"Eastron: Polling started (every {self.poll_interval}s)")

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
            "gateway_ip": self.gateway_ip,
            "gateway_port": self.gateway_port,
            "slave_id": self.slave_id,
            "poll_interval": self.poll_interval,
            "total_reads": self.total_reads,
            "read_errors": self.read_errors,
            "last_read": self.last_read_time.isoformat() if self.last_read_time else None,
        }
