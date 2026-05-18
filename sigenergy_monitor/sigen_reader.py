"""Sigenergy SigenStor Modbus TCP reader.

Polls a Sigen SigenStor EC 10.0 TP at the given host:502, using two
slave addresses on the same TCP socket:

  * slave 247 — "plant address"; system-wide registers (grid sensor,
                plant SOC, total PV, ESS charge/discharge, etc.)
  * slave 1   — hybrid inverter (and DC EV charger) detail registers

Per the Sigenergy Modbus Protocol V2.2 (Sigenergy Technology Co., Ltd.):
  - All registers are read via FC 0x04 (Read Input Registers).
  - Single-frame max is 125 registers.
  - All multi-register integers are big-endian.
  - "Gain" column in the spec = scale-divisor to recover the engineering
    value (e.g. gain 1000 → raw / 1000 = kW).
  - Sentinels: U16 0xFFFF, S16 0x8000, U32 0xFFFFFFFF, S32 0x80000000
    indicate "not available".

Reader design mirrors the hardened Solis reader in microgrid_remote_monitor:
batched reads, bail-on-first-failure per batch, heartbeat log every 30s,
and a watchdog that forces disconnect/reconnect after sustained staleness.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Register definitions
#
# Each tuple: (address, count, name, dtype, gain, unit)
#   dtype ∈ {"U16","S16","U32","S32","U64","STRING"}
#   gain  = the spec's "Gain" column; engineering value = raw / gain
#   STRING values use count = number of registers (2 bytes each, ASCII)
# ---------------------------------------------------------------------------

# Plant-level registers (slave 247) — system-wide info
PLANT_REGS: list[tuple[int, int, str, str, float, str]] = [
    (30000, 2, "system_time_s",         "U32", 1,    "s"),
    (30002, 1, "system_tz_min",         "S16", 1,    "min"),
    (30003, 1, "ems_work_mode",         "U16", 1,    ""),
    (30004, 1, "grid_sensor_status",    "U16", 1,    ""),
    (30005, 2, "grid_power_kw",         "S32", 1000, "kW"),
    (30007, 2, "grid_reactive_kvar",    "S32", 1000, "kVar"),
    (30009, 1, "ongrid_offgrid_state",  "U16", 1,    ""),
    (30010, 2, "max_active_power_kw",   "U32", 1000, "kW"),
    (30014, 1, "ess_soc_pct",           "U16", 10,   "%"),
    (30015, 2, "plant_phase_a_kw",      "S32", 1000, "kW"),
    (30017, 2, "plant_phase_b_kw",      "S32", 1000, "kW"),
    (30019, 2, "plant_phase_c_kw",      "S32", 1000, "kW"),
    (30027, 1, "general_alarm_1",       "U16", 1,    ""),
    (30028, 1, "general_alarm_2",       "U16", 1,    ""),
    (30029, 1, "general_alarm_3",       "U16", 1,    ""),
    (30030, 1, "general_alarm_4",       "U16", 1,    ""),
    (30031, 2, "plant_active_kw",       "S32", 1000, "kW"),
    (30033, 2, "plant_reactive_kvar",   "S32", 1000, "kVar"),
    (30035, 2, "plant_pv_kw",           "S32", 1000, "kW"),
    (30037, 2, "ess_power_kw",          "S32", 1000, "kW"),  # >0 charging
    (30039, 2, "avail_max_active_kw",   "U32", 1000, "kW"),
    (30047, 2, "avail_max_charge_kw",   "U32", 1000, "kW"),
    (30049, 2, "avail_max_discharge_kw","U32", 1000, "kW"),
    (30051, 1, "plant_running_state",   "U16", 1,    ""),
    (30052, 2, "grid_phase_a_kw",       "S32", 1000, "kW"),
    (30054, 2, "grid_phase_b_kw",       "S32", 1000, "kW"),
    (30056, 2, "grid_phase_c_kw",       "S32", 1000, "kW"),
    (30064, 2, "avail_charge_capacity_kwh",    "U32", 100, "kWh"),
    (30066, 2, "avail_discharge_capacity_kwh", "U32", 100, "kWh"),
    (30068, 2, "rated_charge_kw",       "U32", 1000, "kW"),
    (30070, 2, "rated_discharge_kw",    "U32", 1000, "kW"),
    (30072, 1, "general_alarm_5",       "U16", 1,    ""),
]

# Hybrid inverter info + state (slave 1) — first batch
INV_INFO_REGS: list[tuple[int, int, str, str, float, str]] = [
    (30500, 15, "model_type",           "STRING", 1, ""),
    (30515, 10, "serial_number",        "STRING", 1, ""),
    (30525, 15, "firmware_version",     "STRING", 1, ""),
    (30540, 2,  "rated_active_kw",      "U32", 1000, "kW"),
    (30542, 2,  "max_apparent_kva",     "U32", 1000, "kVA"),
    (30544, 2,  "max_active_kw",        "U32", 1000, "kW"),
    (30546, 2,  "max_absorption_kw",    "U32", 1000, "kW"),
    (30548, 2,  "rated_batt_capacity_kwh", "U32", 100,  "kWh"),
    (30550, 2,  "rated_charge_kw",      "U32", 1000, "kW"),
    (30552, 2,  "rated_discharge_kw",   "U32", 1000, "kW"),
]

# Energy counters (slave 1)
INV_ENERGY_REGS: list[tuple[int, int, str, str, float, str]] = [
    (30554, 2, "daily_export_kwh",          "U32", 100, "kWh"),
    (30556, 4, "accum_export_kwh",          "U64", 100, "kWh"),
    (30560, 2, "daily_import_kwh",          "U32", 100, "kWh"),
    (30562, 4, "accum_import_kwh",          "U64", 100, "kWh"),
    (30566, 2, "batt_daily_charge_kwh",     "U32", 100, "kWh"),
    (30568, 4, "batt_accum_charge_kwh",     "U64", 100, "kWh"),
    (30572, 2, "batt_daily_discharge_kwh",  "U32", 100, "kWh"),
    (30574, 4, "batt_accum_discharge_kwh",  "U64", 100, "kWh"),
]

# Inverter + battery operating state (slave 1)
INV_STATE_REGS: list[tuple[int, int, str, str, float, str]] = [
    (30578, 1, "inv_running_state",     "U16", 1,    ""),
    (30579, 2, "max_active_adj_kw",     "S32", 1000, "kW"),
    (30581, 2, "min_active_adj_kw",     "S32", 1000, "kW"),
    (30587, 2, "inv_active_kw",         "S32", 1000, "kW"),
    (30589, 2, "inv_reactive_kvar",     "S32", 1000, "kVar"),
    (30591, 2, "max_batt_charge_kw",    "U32", 1000, "kW"),
    (30593, 2, "max_batt_discharge_kw", "U32", 1000, "kW"),
    (30595, 2, "avail_batt_charge_kwh",    "U32", 100, "kWh"),
    (30597, 2, "avail_batt_discharge_kwh", "U32", 100, "kWh"),
    (30599, 2, "batt_power_kw",         "S32", 1000, "kW"),  # >0 charge
    (30601, 1, "batt_soc_pct",          "U16", 10,   "%"),
    (30602, 1, "batt_soh_pct",          "U16", 10,   "%"),
    (30603, 1, "batt_cell_temp_c",      "S16", 10,   "°C"),
    (30604, 1, "batt_cell_voltage_v",   "U16", 1000, "V"),
    (30605, 1, "alarm_1",               "U16", 1,    ""),
    (30606, 1, "alarm_2",               "U16", 1,    ""),
    (30607, 1, "alarm_3",               "U16", 1,    ""),
    (30608, 1, "alarm_4",               "U16", 1,    ""),
    (30609, 1, "alarm_5",               "U16", 1,    ""),
]

# AC + PV string registers (slave 1)
INV_AC_PV_REGS: list[tuple[int, int, str, str, float, str]] = [
    (31000, 1, "rated_grid_voltage_v",  "U16", 10,   "V"),
    (31001, 1, "rated_grid_freq_hz",    "U16", 100,  "Hz"),
    (31002, 1, "grid_freq_hz",          "U16", 100,  "Hz"),
    (31003, 1, "pcs_internal_temp_c",   "S16", 10,   "°C"),
    (31004, 1, "output_type",           "U16", 1,    ""),  # 2 = L1/L2/L3/N
    (31005, 2, "ac_ab_v",               "U32", 100,  "V"),
    (31007, 2, "ac_bc_v",               "U32", 100,  "V"),
    (31009, 2, "ac_ca_v",               "U32", 100,  "V"),
    (31011, 2, "ac_a_v",                "U32", 100,  "V"),
    (31013, 2, "ac_b_v",                "U32", 100,  "V"),
    (31015, 2, "ac_c_v",                "U32", 100,  "V"),
    (31017, 2, "ac_a_a",                "S32", 100,  "A"),
    (31019, 2, "ac_b_a",                "S32", 100,  "A"),
    (31021, 2, "ac_c_a",                "S32", 100,  "A"),
    (31023, 1, "power_factor",          "U16", 1000, ""),
    (31024, 1, "pack_count",            "U16", 1,    ""),
    (31025, 1, "pv_string_count",       "U16", 1,    ""),
    (31026, 1, "mppt_count",            "U16", 1,    ""),
    (31027, 1, "pv1_v",                 "S16", 10,   "V"),
    (31028, 1, "pv1_a",                 "S16", 100,  "A"),
    (31029, 1, "pv2_v",                 "S16", 10,   "V"),
    (31030, 1, "pv2_a",                 "S16", 100,  "A"),
    (31031, 1, "pv3_v",                 "S16", 10,   "V"),
    (31032, 1, "pv3_a",                 "S16", 100,  "A"),
    (31033, 1, "pv4_v",                 "S16", 10,   "V"),
    (31034, 1, "pv4_a",                 "S16", 100,  "A"),
    (31035, 2, "pv_power_kw",           "S32", 1000, "kW"),
    (31037, 1, "insulation_resistance_mohm", "U16", 1000, "MΩ"),
]

# DC EV Charger (slave 1, registers 31500+)
DC_CHARGER_REGS: list[tuple[int, int, str, str, float, str]] = [
    (31500, 1, "ev_vehicle_v",          "U16", 10,   "V"),
    (31501, 1, "ev_charging_a",         "U16", 10,   "A"),
    (31502, 2, "ev_output_kw",          "S32", 1000, "kW"),
    (31504, 1, "ev_vehicle_soc",        "U16", 10,   "%"),
    (31505, 2, "ev_session_kwh",        "U32", 100,  "kWh"),
    (31507, 2, "ev_session_duration_s", "U32", 1,    "s"),
]


# ---------------------------------------------------------------------------
# Sentinel detection
# ---------------------------------------------------------------------------
def _is_na(raw: int, dtype: str) -> bool:
    if dtype == "U16": return raw == 0xFFFF
    if dtype == "S16": return raw == -0x8000
    if dtype == "U32": return raw == 0xFFFFFFFF
    if dtype == "S32": return raw == -0x80000000
    if dtype == "U64": return raw == 0xFFFFFFFFFFFFFFFF
    return False


def _decode(regs: list[int], dtype: str) -> Optional[Any]:
    """Decode a list of 16-bit registers into the requested data type."""
    if not regs:
        return None
    try:
        if dtype == "U16":
            return regs[0] if not _is_na(regs[0], dtype) else None
        if dtype == "S16":
            v = regs[0]
            if v >= 0x8000: v -= 0x10000
            return v if not _is_na(v, dtype) else None
        if dtype == "U32":
            v = (regs[0] << 16) | regs[1]
            return v if not _is_na(v, dtype) else None
        if dtype == "S32":
            v = (regs[0] << 16) | regs[1]
            if v >= 0x80000000: v -= 0x100000000
            return v if not _is_na(v, dtype) else None
        if dtype == "U64":
            v = ((regs[0] << 48) | (regs[1] << 32)
                 | (regs[2] << 16) | regs[3])
            return v if not _is_na(v, dtype) else None
        if dtype == "STRING":
            chars = []
            for r in regs:
                hi = (r >> 8) & 0xFF
                lo = r & 0xFF
                if hi: chars.append(chr(hi))
                if lo: chars.append(chr(lo))
            return "".join(chars).strip("\x00 \t\r\n")
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Batch builder — group contiguous register reads into single Modbus frames
# ---------------------------------------------------------------------------
def _build_batches(regs: list[tuple[int, int, str, str, float, str]],
                   max_count: int = 100, max_gap: int = 20):
    """Group adjacent register entries into batches readable as one Modbus frame.

    Sigenergy allows up to 125 regs/frame; we use 100 as a safety margin.
    """
    sorted_regs = sorted(regs, key=lambda r: r[0])
    batches: list[list[tuple]] = []
    current: list[tuple] = []
    for reg in sorted_regs:
        addr, count, *_ = reg
        end = addr + count
        if not current:
            current = [reg]
            continue
        first = current[0][0]
        current_end = max(r[0] + r[1] for r in current)
        if end - first <= max_count and addr - current_end <= max_gap:
            current.append(reg)
        else:
            batches.append(current)
            current = [reg]
    if current:
        batches.append(current)
    return [
        (b[0][0], max(r[0] + r[1] for r in b) - b[0][0], b)
        for b in batches
    ]


# ---------------------------------------------------------------------------
# Reader class
# ---------------------------------------------------------------------------
class SigenReader:
    def __init__(self, host: str, port: int = 502,
                 plant_slave: int = 247, inv_slave: int = 1,
                 poll_interval: float = 5.0,
                 inter_frame_sleep: float = 0.05):
        self.host = host
        self.port = port
        self.plant_slave = plant_slave
        self.inv_slave = inv_slave
        self.poll_interval = poll_interval
        self.inter_frame_sleep = inter_frame_sleep

        self.client: Optional[ModbusTcpClient] = None
        self.connected = False
        self.last_read_time: Optional[datetime] = None
        self.total_reads = 0
        self.read_errors = 0

        # Pre-compute batches per slave/group
        self._plant_batches = _build_batches(PLANT_REGS)
        self._inv_info_batches = _build_batches(INV_INFO_REGS)
        self._inv_energy_batches = _build_batches(INV_ENERGY_REGS)
        self._inv_state_batches = _build_batches(INV_STATE_REGS)
        self._inv_ac_pv_batches = _build_batches(INV_AC_PV_REGS)
        self._dc_charger_batches = _build_batches(DC_CHARGER_REGS)
        log.info(
            f"Sigenergy register map split into "
            f"{len(self._plant_batches)} plant + "
            f"{len(self._inv_info_batches) + len(self._inv_energy_batches) + len(self._inv_state_batches) + len(self._inv_ac_pv_batches)} inverter + "
            f"{len(self._dc_charger_batches)} DC-charger batches"
        )

        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- connection ----
    def connect(self):
        if self.client is not None:
            try: self.client.close()
            except Exception: pass
            self.client = None
        try:
            self.client = ModbusTcpClient(host=self.host, port=self.port, timeout=3)
            self.connected = self.client.connect()
            if self.connected:
                log.info(f"Sigenergy: connected to {self.host}:{self.port}")
            else:
                log.warning(f"Sigenergy: failed to connect to {self.host}:{self.port}")
        except Exception as e:
            log.error(f"Sigenergy: connection error: {e}")
            self.connected = False

    def disconnect(self):
        if self.client:
            try: self.client.close()
            except Exception: pass
            self.client = None
            self.connected = False

    # ---- low-level read ----
    def _read_block(self, start: int, count: int, slave: int) -> Optional[list[int]]:
        if not self.connected:
            self.connect()
            if not self.connected:
                return None
        try:
            # Sigenergy: FC 0x04 (Read Input Registers) for all RO data
            try:
                r = self.client.read_input_registers(
                    address=start, count=count, device_id=slave)
            except TypeError:
                r = self.client.read_input_registers(
                    address=start, count=count, slave=slave)
            if isinstance(r, ModbusIOException) or r.isError():
                log.warning(f"Sigenergy: read err at {start} slave={slave}: {r}")
                self.connected = False
                return None
            regs = r.registers
            if len(regs) != count:
                return None
            return regs
        except Exception as e:
            log.error(f"Sigenergy: exception reading {start} slave={slave}: {e}")
            self.connected = False
            return None

    def _read_batches(self, batches, slave: int, out: dict) -> bool:
        """Read all batches for one slave; decode each register's value
        into `out`. Returns True if at least one batch succeeded."""
        any_ok = False
        for batch_start, batch_count, entries in batches:
            block = self._read_block(batch_start, batch_count, slave)
            if block is None:
                continue
            any_ok = True
            for addr, count, name, dtype, gain, _unit in entries:
                offset = addr - batch_start
                slice_ = block[offset:offset + count]
                raw = _decode(slice_, dtype)
                if raw is None:
                    out[name] = None
                elif dtype == "STRING":
                    out[name] = raw
                else:
                    out[name] = raw / gain if gain != 1 else raw
            time.sleep(self.inter_frame_sleep)
        return any_ok

    # ---- one poll cycle ----
    def poll_once(self) -> dict:
        result: dict[str, Any] = {}
        any_ok = False

        # Plant registers
        if self._read_batches(self._plant_batches, self.plant_slave, result):
            any_ok = True

        # Inverter info (slow-changing; could be polled less often, but
        # this register block is small so just include it every cycle).
        if self._read_batches(self._inv_info_batches, self.inv_slave, result):
            any_ok = True

        # Energy counters
        if self._read_batches(self._inv_energy_batches, self.inv_slave, result):
            any_ok = True

        # State + battery + alarms
        if self._read_batches(self._inv_state_batches, self.inv_slave, result):
            any_ok = True

        # AC + PV strings
        if self._read_batches(self._inv_ac_pv_batches, self.inv_slave, result):
            any_ok = True

        # DC EV charger
        if self._read_batches(self._dc_charger_batches, self.inv_slave, result):
            any_ok = True

        # Computed fields
        if "pv1_v" in result and "pv1_a" in result:
            for n in (1, 2, 3, 4):
                v = result.get(f"pv{n}_v"); a = result.get(f"pv{n}_a")
                if v is not None and a is not None:
                    result[f"pv{n}_w"] = round(v * a, 1)
                else:
                    result[f"pv{n}_w"] = None

        if any_ok:
            self.total_reads += 1
            self.last_read_time = datetime.now()
            with self._lock:
                # Merge — keep previous fields that weren't refreshed this cycle
                self._data.update(result)
        else:
            self.read_errors += 1
        return result

    # ---- background loop with watchdog ----
    def _loop(self):
        last_forced = datetime.now()
        last_hb = datetime.now()
        stale_threshold = max(30.0, 3.0 * self.poll_interval)
        cooldown = max(30.0, 2.0 * self.poll_interval)

        log.info(f"Sigenergy poll loop entered (every {self.poll_interval}s)")
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.error(f"Sigenergy poll error: {e}")

            now = datetime.now()
            if (now - last_hb).total_seconds() >= 30:
                age = ((now - self.last_read_time).total_seconds()
                       if self.last_read_time else None)
                age_str = f"{age:.0f}s" if age is not None else "never"
                log.info(
                    f"Sigenergy heartbeat: connected={self.connected}, "
                    f"last_read_age={age_str}, reads={self.total_reads}, "
                    f"errors={self.read_errors}"
                )
                last_hb = now

            if self.last_read_time is not None:
                age_s = (now - self.last_read_time).total_seconds()
                cool_s = (now - last_forced).total_seconds()
                if age_s > stale_threshold and cool_s > cooldown:
                    log.warning(
                        f"Sigenergy watchdog: last_read is {age_s:.0f}s stale "
                        f"(> {stale_threshold:.0f}s) — forcing reconnect"
                    )
                    self.disconnect()
                    self.connect()
                    last_forced = now

            self._stop.wait(self.poll_interval)

    def start(self):
        self.connect()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.disconnect()

    # ---- public API ----
    def get_data(self) -> dict:
        with self._lock:
            return dict(self._data)

    def get_status(self) -> dict:
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "plant_slave": self.plant_slave,
            "inv_slave": self.inv_slave,
            "poll_interval": self.poll_interval,
            "total_reads": self.total_reads,
            "read_errors": self.read_errors,
            "last_read": (self.last_read_time.isoformat()
                          if self.last_read_time else None),
        }


# ---------------------------------------------------------------------------
# CLI for diagnostics
# ---------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--sigen-host", default="192.168.55.131")
    p.add_argument("--sigen-port", type=int, default=502)
    p.add_argument("--plant-slave", type=int, default=247)
    p.add_argument("--inv-slave", type=int, default=1)
    p.add_argument("--once", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    reader = SigenReader(args.sigen_host, args.sigen_port,
                         plant_slave=args.plant_slave,
                         inv_slave=args.inv_slave)
    if args.once:
        reader.connect()
        data = reader.poll_once()
        for k, v in sorted(data.items()):
            print(f"  {k:>35}: {v}")
        reader.disconnect()
    else:
        reader.start()
        try:
            while True:
                time.sleep(15)
                d = reader.get_data()
                # Just print the headline values each tick
                print(f"SOC={d.get('batt_soc_pct')}%  PV={d.get('plant_pv_kw')}kW  "
                      f"Batt={d.get('batt_power_kw')}kW  Grid={d.get('grid_power_kw')}kW  "
                      f"CellT={d.get('batt_cell_temp_c')}°C")
        except KeyboardInterrupt:
            reader.stop()


if __name__ == "__main__":
    main()
