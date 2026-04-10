#!/usr/bin/env python3
"""
SP Pro Modbus TCP Reader
========================
Reads registers from the Selectronic SP Pro inverter via Modbus TCP.

The SP Pro exposes data across three slave IDs for a 3-phase system:
  - Slave 11: Phase L1 (main unit — includes battery SoC, voltage, frequency)
  - Slave 21: Phase L2
  - Slave 31: Phase L3

Each slave uses the same register address layout (1–11) but only
Slave 11 has the full set (SoC, battery voltage, frequency, etc.).

Register data is Signed 16-bit with scaling factors from the memory map.
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException

log = logging.getLogger("sppro_reader")

# ---------------------------------------------------------------------------
# Register map per slave — (address, name, unit, scaling_factor)
# All registers are Signed 16-bit, size 1
# Scaling: value = raw_register * scaling_factor
# ---------------------------------------------------------------------------

# Slave 11 (L1) — full register set
SLAVE_11_REGISTERS = [
    (1,  "managed_pv_power",       "W",   1.0),   # Managed PV Output Power
    (2,  "source_side_power",      "W",   1.0),   # Source Side Power (grid/gen)
    (3,  "load_side_power",        "W",   1.0),   # Load Side Power
    (4,  "ac_inverter_power",      "W",   1.0),   # AC Inverter Power
    (5,  "battery_soc",            "%",   0.1),   # Battery State of Charge
    (6,  "total_battery_current",  "A",   0.1),   # Total Battery Current — All Inverters
    (7,  "battery_current_l1",     "A",   0.1),   # Battery Current — L1
    (8,  "battery_voltage",        "V",   0.1),   # Battery Voltage
    (9,  "load_side_voltage",      "V",   0.1),   # SP Pro Load Side Voltage
    (10, "generator_side_voltage", "V",   0.1),   # SP Pro Generator Side Voltage
    (11, "inverter_frequency",     "Hz",  0.1),   # SP Pro Inverter Frequency
]

# Slave 21 (L2) — subset of registers
SLAVE_21_REGISTERS = [
    (1,  "managed_pv_power",       "W",   1.0),
    (2,  "source_side_power",      "W",   1.0),
    (3,  "load_side_power",        "W",   1.0),
    (4,  "ac_inverter_power",      "W",   1.0),
    (7,  "battery_current_l2",     "A",   0.1),
    (9,  "load_side_voltage",      "V",   0.1),
    (10, "generator_side_voltage", "V",   0.1),
]

# Slave 31 (L3) — subset of registers
SLAVE_31_REGISTERS = [
    (1,  "managed_pv_power",       "W",   1.0),
    (2,  "source_side_power",      "W",   1.0),
    (3,  "load_side_power",        "W",   1.0),
    (4,  "ac_inverter_power",      "W",   1.0),
    (7,  "battery_current_l3",     "A",   0.1),
    (9,  "load_side_voltage",      "V",   0.1),
    (10, "generator_side_voltage", "V",   0.1),
]


class SPProModbusReader:
    """Periodically reads registers from the SP Pro inverter via Modbus TCP."""

    def __init__(self, ip, port=502, poll_interval=5):
        self.ip = ip
        self.port = port
        self.poll_interval = poll_interval

        self.client = None
        self.connected = False
        self.last_read_time = None
        self.read_errors = 0
        self.total_reads = 0

        # Current processed data (combined from all 3 slaves)
        self.data = {}

        # History for charts (1-minute samples, 24 hours)
        self.history_max = 1440
        self.history = {
            "timestamps":          deque(maxlen=self.history_max),
            "battery_soc":         deque(maxlen=self.history_max),
            "battery_voltage":     deque(maxlen=self.history_max),
            "total_load_power":    deque(maxlen=self.history_max),
            "total_source_power":  deque(maxlen=self.history_max),
            "total_pv_power":      deque(maxlen=self.history_max),
            "total_battery_current": deque(maxlen=self.history_max),
        }
        self._last_history_minute = -1

        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    def connect(self):
        """Establish Modbus TCP connection to the SP Pro."""
        try:
            self.client = ModbusTcpClient(
                host=self.ip,
                port=self.port,
                timeout=5,
            )
            self.connected = self.client.connect()
            if self.connected:
                log.info(f"Connected to SP Pro at {self.ip}:{self.port}")
            else:
                log.warning(f"Failed to connect to SP Pro at {self.ip}:{self.port}")
        except Exception as e:
            log.error(f"SP Pro connection error: {e}")
            self.connected = False

    def disconnect(self):
        """Close the Modbus connection."""
        if self.client:
            self.client.close()
            self.connected = False
            log.info("Disconnected from SP Pro")

    def _read_register(self, address, slave_id):
        """Read a single holding register from the SP Pro.

        The SP Pro uses Modbus function code 0x03 (read holding registers).
        Returns the raw signed 16-bit value, or None on error.
        """
        if not self.connected:
            return None
        try:
            result = self.client.read_holding_registers(
                address=address,
                count=1,
                slave=slave_id,
            )
            if isinstance(result, ModbusIOException) or result.isError():
                log.debug(f"SP Pro read error: slave={slave_id} addr={address}: {result}")
                return None
            raw = result.registers[0]
            # Convert to signed 16-bit
            if raw >= 0x8000:
                raw -= 0x10000
            return raw
        except Exception as e:
            log.error(f"SP Pro exception: slave={slave_id} addr={address}: {e}")
            self.connected = False
            return None

    def _read_slave(self, slave_id, register_map):
        """Read all registers for one slave ID and return a dict of scaled values."""
        readings = {}
        for address, name, unit, scale in register_map:
            raw = self._read_register(address, slave_id)
            if raw is not None:
                readings[name] = round(raw * scale, 2)
            time.sleep(0.05)  # small delay between reads
        return readings

    def poll_once(self):
        """Read all three slaves and combine the data."""
        # Ensure we're connected before attempting reads
        if not self.connected:
            self.connect()
            if not self.connected:
                self.read_errors += 1
                return

        now = datetime.now()

        # Read each slave (L1, L2, L3)
        l1_data = self._read_slave(11, SLAVE_11_REGISTERS)
        l2_data = self._read_slave(21, SLAVE_21_REGISTERS)
        l3_data = self._read_slave(31, SLAVE_31_REGISTERS)

        self.total_reads += 1

        if not l1_data and not l2_data and not l3_data:
            self.read_errors += 1
            return

        # Build combined data dict
        combined = {}

        # L1 data (primary — has SoC, battery voltage, frequency)
        combined["battery_soc"] = l1_data.get("battery_soc", 0)
        combined["battery_voltage"] = l1_data.get("battery_voltage", 0)
        combined["total_battery_current"] = l1_data.get("total_battery_current", 0)
        combined["inverter_frequency"] = l1_data.get("inverter_frequency", 0)

        # Per-phase power values
        combined["l1_pv_power"] = l1_data.get("managed_pv_power", 0)
        combined["l1_source_power"] = l1_data.get("source_side_power", 0)
        combined["l1_load_power"] = l1_data.get("load_side_power", 0)
        combined["l1_inverter_power"] = l1_data.get("ac_inverter_power", 0)
        combined["l1_battery_current"] = l1_data.get("battery_current_l1", 0)
        combined["l1_load_voltage"] = l1_data.get("load_side_voltage", 0)
        combined["l1_gen_voltage"] = l1_data.get("generator_side_voltage", 0)

        combined["l2_pv_power"] = l2_data.get("managed_pv_power", 0)
        combined["l2_source_power"] = l2_data.get("source_side_power", 0)
        combined["l2_load_power"] = l2_data.get("load_side_power", 0)
        combined["l2_inverter_power"] = l2_data.get("ac_inverter_power", 0)
        combined["l2_battery_current"] = l2_data.get("battery_current_l2", 0)
        combined["l2_load_voltage"] = l2_data.get("load_side_voltage", 0)
        combined["l2_gen_voltage"] = l2_data.get("generator_side_voltage", 0)

        combined["l3_pv_power"] = l3_data.get("managed_pv_power", 0)
        combined["l3_source_power"] = l3_data.get("source_side_power", 0)
        combined["l3_load_power"] = l3_data.get("load_side_power", 0)
        combined["l3_inverter_power"] = l3_data.get("ac_inverter_power", 0)
        combined["l3_battery_current"] = l3_data.get("battery_current_l3", 0)
        combined["l3_load_voltage"] = l3_data.get("load_side_voltage", 0)
        combined["l3_gen_voltage"] = l3_data.get("generator_side_voltage", 0)

        # Totals across all 3 phases
        combined["total_pv_power"] = round(
            combined["l1_pv_power"] + combined["l2_pv_power"] + combined["l3_pv_power"], 1
        )
        combined["total_source_power"] = round(
            combined["l1_source_power"] + combined["l2_source_power"] + combined["l3_source_power"], 1
        )
        combined["total_load_power"] = round(
            combined["l1_load_power"] + combined["l2_load_power"] + combined["l3_load_power"], 1
        )
        combined["total_inverter_power"] = round(
            combined["l1_inverter_power"] + combined["l2_inverter_power"] + combined["l3_inverter_power"], 1
        )

        # Battery power estimate (voltage * total current)
        batt_v = combined["battery_voltage"]
        batt_i = combined["total_battery_current"]
        combined["battery_power"] = round(batt_v * batt_i, 1)

        # Metadata
        combined["_timestamp"] = now.isoformat()
        combined["_read_ok"] = bool(l1_data)

        # Update shared state
        with self.lock:
            self.data = combined
            self.last_read_time = now

            # Append to history (once per minute)
            current_minute = now.minute
            if current_minute != self._last_history_minute:
                self._last_history_minute = current_minute
                self.history["timestamps"].append(now.strftime("%H:%M"))
                for key in ["battery_soc", "battery_voltage", "total_load_power",
                            "total_source_power", "total_pv_power", "total_battery_current"]:
                    self.history[key].append(combined.get(key, 0))

    def _poll_loop(self):
        """Background polling loop with backoff on connection failure."""
        retry_delay = self.poll_interval
        while not self._stop_event.is_set():
            try:
                self.poll_once()
                if self.connected:
                    retry_delay = self.poll_interval  # reset on success
                else:
                    # Back off when not connected (max 60s between retries)
                    retry_delay = min(retry_delay * 2, 60)
                    log.info(f"SP Pro not connected, retrying in {retry_delay}s")
            except Exception as e:
                log.error(f"SP Pro poll error: {e}")
                retry_delay = min(retry_delay * 2, 60)
            self._stop_event.wait(retry_delay)

    def start(self):
        """Start background polling thread."""
        self.connect()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info(f"SP Pro polling started (every {self.poll_interval}s)")

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
            "ip": self.ip,
            "port": self.port,
            "poll_interval": self.poll_interval,
            "total_reads": self.total_reads,
            "read_errors": self.read_errors,
            "last_read": self.last_read_time.isoformat() if self.last_read_time else None,
        }
