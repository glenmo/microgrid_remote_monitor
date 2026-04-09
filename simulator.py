#!/usr/bin/env python3
"""
Solis Inverter Modbus TCP Simulator
====================================
Simulates a Solis 50kW hybrid inverter responding to Modbus TCP
function code 0x04 (read input registers) with realistic values.
Useful for testing the dashboard without a real inverter.

Usage:
    python simulator.py --port 5020
    python app.py --inverter-ip 127.0.0.1 --inverter-port 5020
"""

import argparse
import logging
import math
import random
import struct
import threading
import time
from datetime import datetime

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusDeviceContext,
    ModbusServerContext,
)
from pymodbus.server import StartTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("simulator")


def build_initial_store():
    """Build a data store with all zeros for input registers 33000-33250."""
    block_start = 33000
    block_size = 250
    values = [0] * block_size
    return ModbusSequentialDataBlock(block_start, values)


def update_registers(context, slave_id=1):
    """Periodically update the simulated register values."""
    t = 0
    base_soc = 65.0

    while True:
        now = datetime.now()
        store = context[slave_id].store["i"]  # input registers

        # Time of day factor (simulates sun position)
        hour = now.hour + now.minute / 60.0
        sun_factor = max(0, math.sin((hour - 6) / 12 * math.pi)) if 6 <= hour <= 18 else 0

        # --- Inverter info ---
        store.setValues(33000, [0x2073])  # Model: 2073H = S6 3PH HV 50kW Hybrid
        store.setValues(33022, [now.year - 2000])
        store.setValues(33023, [now.month])
        store.setValues(33024, [now.day])
        store.setValues(33025, [now.hour])
        store.setValues(33026, [now.minute])

        # --- PV strings (simulate sun-dependent generation) ---
        pv_base = sun_factor * 12000  # ~12kW per string at peak

        for i, offset in enumerate([(33049, 33050), (33051, 33052), (33053, 33054), (33055, 33056)]):
            variation = random.uniform(0.85, 1.0)
            v = int(sun_factor * (580 + random.uniform(-20, 20)))  # ~580V DC (0.1V units)
            c = int(pv_base * variation / (v * 0.1 + 0.01) * 10) if v > 0 else 0  # 0.1A units
            store.setValues(offset[0], [max(0, v)])
            store.setValues(offset[1], [max(0, c)])

        # Total PV power (U32: high word, low word)
        pv_total = int(pv_base * 4 * random.uniform(0.9, 1.0))
        store.setValues(33057, [(pv_total >> 16) & 0xFFFF, pv_total & 0xFFFF])

        # PV energy today (0.1kWh units)
        pv_today = int(hour * sun_factor * 5 * 10)
        store.setValues(33035, [pv_today])

        # PV total energy (U32, 1kWh)
        pv_total_energy = 125000 + int(t * 0.01)
        store.setValues(33029, [(pv_total_energy >> 16) & 0xFFFF, pv_total_energy & 0xFFFF])

        # --- AC Grid ---
        grid_v_base = 4150  # 415.0V line-line (0.1V units)
        store.setValues(33073, [grid_v_base + random.randint(-20, 20)])
        store.setValues(33074, [grid_v_base + random.randint(-20, 20)])
        store.setValues(33075, [grid_v_base + random.randint(-20, 20)])

        grid_i = int(pv_total / 415 / 1.732 * 10 * random.uniform(0.9, 1.1))
        store.setValues(33076, [max(0, grid_i + random.randint(-5, 5))])
        store.setValues(33077, [max(0, grid_i + random.randint(-5, 5))])
        store.setValues(33078, [max(0, grid_i + random.randint(-5, 5))])

        # Active power (S32): positive = export
        active_pwr = pv_total - 8000 + random.randint(-500, 500)
        if active_pwr < 0:
            active_u32 = (active_pwr + 0x100000000) & 0xFFFFFFFF
        else:
            active_u32 = active_pwr
        store.setValues(33079, [(active_u32 >> 16) & 0xFFFF, active_u32 & 0xFFFF])

        # Reactive power (S32)
        reactive = random.randint(-500, 500)
        reactive_u32 = (reactive + 0x100000000) & 0xFFFFFFFF if reactive < 0 else reactive
        store.setValues(33081, [(reactive_u32 >> 16) & 0xFFFF, reactive_u32 & 0xFFFF])

        # Apparent power (S32)
        apparent = int(math.sqrt(active_pwr**2 + reactive**2))
        store.setValues(33083, [(apparent >> 16) & 0xFFFF, apparent & 0xFFFF])

        # Grid frequency (0.01Hz units)
        store.setValues(33094, [5000 + random.randint(-5, 5)])

        # --- Battery ---
        soc_delta = (pv_total - 8000) * 0.00001
        base_soc = max(10, min(100, base_soc + soc_delta))
        soc = int(base_soc + random.uniform(-0.5, 0.5))
        store.setValues(33139, [max(0, min(100, soc))])
        store.setValues(33140, [97])  # SOH 97%

        # Battery voltage (0.1V units)
        batt_v = 5120 + random.randint(-50, 50)  # ~512V
        store.setValues(33133, [batt_v])

        # Battery current (S16, 0.1A)
        batt_charging = pv_total > 10000
        batt_i = random.randint(50, 200) if batt_charging else random.randint(10, 100)
        if not batt_charging:
            batt_i = (-batt_i) & 0xFFFF
        store.setValues(33134, [batt_i])
        store.setValues(33135, [0 if batt_charging else 1])

        # BMS values
        store.setValues(33141, [batt_v])
        store.setValues(33142, [batt_i])
        store.setValues(33143, [500])
        store.setValues(33144, [500])

        # --- Temperatures ---
        store.setValues(33093, [350 + random.randint(-10, 30)])   # ~35°C
        store.setValues(33046, [280 + random.randint(-5, 15)])    # ~28°C

        # --- Status ---
        store.setValues(33091, [1])
        store.setValues(33095, [0])
        store.setValues(33121, [1])
        store.setValues(33111, [0])
        store.setValues(33116, [0])
        store.setValues(33117, [0])
        store.setValues(33118, [0])
        store.setValues(33119, [0])

        # DC bus voltage
        store.setValues(33071, [7500 + random.randint(-100, 100)])

        # Backup output
        store.setValues(33137, [2400 + random.randint(-10, 10)])
        store.setValues(33138, [random.randint(0, 30)])

        t += 1
        time.sleep(2)


def float_to_regs(value):
    """Convert a Python float to two Modbus registers (IEEE 754, big-endian)."""
    raw = struct.pack(">f", value)
    hi, lo = struct.unpack(">HH", raw)
    return [hi, lo]


def update_eastron_registers(context, slave_id=2):
    """Periodically update the simulated Eastron SDM630MCT register values."""
    t = 0
    import_kwh = 1523.4
    export_kwh = 876.2

    while True:
        store = context[slave_id].store["i"]

        # Simulate 3-phase meter readings
        base_v = 230 + random.uniform(-5, 5)
        load_kw = 4 + random.uniform(-1, 2)  # ~4kW house load

        for i, addr in enumerate([0x0000, 0x0002, 0x0004]):
            v = base_v + random.uniform(-3, 3)
            store.setValues(addr, float_to_regs(v))

        for i, addr in enumerate([0x0006, 0x0008, 0x000A]):
            c = load_kw * 1000 / 3 / base_v + random.uniform(-0.5, 0.5)
            store.setValues(addr, float_to_regs(max(0, c)))

        for i, addr in enumerate([0x000C, 0x000E, 0x0010]):
            p = load_kw * 1000 / 3 + random.uniform(-100, 100)
            store.setValues(addr, float_to_regs(p))

        # VA, VAr per phase
        for i, addr in enumerate([0x0012, 0x0014, 0x0016]):
            store.setValues(addr, float_to_regs(load_kw * 1000 / 3 * 1.02))
        for i, addr in enumerate([0x0018, 0x001A, 0x001C]):
            store.setValues(addr, float_to_regs(random.uniform(-50, 50)))

        # PF per phase
        for addr in [0x001E, 0x0020, 0x0022]:
            store.setValues(addr, float_to_regs(0.98 + random.uniform(-0.02, 0.01)))

        # Averages/totals
        store.setValues(0x002A, float_to_regs(base_v))
        store.setValues(0x002E, float_to_regs(load_kw * 1000 / base_v / 3))
        store.setValues(0x0030, float_to_regs(load_kw * 1000 / base_v))
        store.setValues(0x0034, float_to_regs(load_kw * 1000))
        store.setValues(0x0038, float_to_regs(load_kw * 1000 * 1.02))
        store.setValues(0x003C, float_to_regs(random.uniform(-100, 100)))
        store.setValues(0x003E, float_to_regs(0.98))

        # Frequency
        store.setValues(0x0046, float_to_regs(50.0 + random.uniform(-0.05, 0.05)))

        # Energy counters
        import_kwh += load_kw * 2 / 3600  # increment over 2s
        export_kwh += random.uniform(0, 0.001)
        store.setValues(0x0048, float_to_regs(import_kwh))
        store.setValues(0x004A, float_to_regs(export_kwh))
        store.setValues(0x004C, float_to_regs(12.3))
        store.setValues(0x004E, float_to_regs(5.1))

        # Line-to-line voltages
        store.setValues(0x00C8, float_to_regs(base_v * 1.732))
        store.setValues(0x00CA, float_to_regs(base_v * 1.732))
        store.setValues(0x00CC, float_to_regs(base_v * 1.732))
        store.setValues(0x00CE, float_to_regs(base_v * 1.732))

        # Neutral current
        store.setValues(0x00E0, float_to_regs(random.uniform(0.1, 1.5)))

        # THD
        for addr in [0x00EA, 0x00EC, 0x00EE]:
            store.setValues(addr, float_to_regs(random.uniform(1, 4)))
        for addr in [0x00F0, 0x00F2, 0x00F4]:
            store.setValues(addr, float_to_regs(random.uniform(2, 8)))

        # Demand
        store.setValues(0x0100, float_to_regs(8.5))
        store.setValues(0x0102, float_to_regs(8.3))
        store.setValues(0x0104, float_to_regs(8.1))
        store.setValues(0x0108, float_to_regs(load_kw * 1000 * 1.1))

        # Total energy
        store.setValues(0x0156, float_to_regs(import_kwh + export_kwh))
        store.setValues(0x0158, float_to_regs(17.4))

        t += 1
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(
        description="Microgrid Modbus TCP Simulator (Solis + Eastron)"
    )
    parser.add_argument("--port", type=int, default=5020, help="TCP port (default: 5020)")
    parser.add_argument("--host", default="0.0.0.0", help="Listen address (default: 0.0.0.0)")
    args = parser.parse_args()

    # Solis inverter (slave ID 1): input registers 33000-33250
    solis_ir = build_initial_store()
    solis_ctx = ModbusDeviceContext(
        di=ModbusSequentialDataBlock(0, [0] * 10),
        co=ModbusSequentialDataBlock(0, [0] * 10),
        hr=ModbusSequentialDataBlock(0, [0] * 10),
        ir=solis_ir,
    )

    # Eastron meter (slave ID 2): input registers 0x0000-0x0200 (0-512)
    eastron_ir = ModbusSequentialDataBlock(0, [0] * 600)
    eastron_ctx = ModbusDeviceContext(
        di=ModbusSequentialDataBlock(0, [0] * 10),
        co=ModbusSequentialDataBlock(0, [0] * 10),
        hr=ModbusSequentialDataBlock(0, [0] * 10),
        ir=eastron_ir,
    )

    server_ctx = ModbusServerContext(
        devices={1: solis_ctx, 2: eastron_ctx},
        single=False,
    )

    # Start updater threads
    t1 = threading.Thread(target=update_registers, args=(server_ctx, 1), daemon=True)
    t2 = threading.Thread(target=update_eastron_registers, args=(server_ctx, 2), daemon=True)
    t1.start()
    t2.start()

    log.info(f"Starting Microgrid simulator on {args.host}:{args.port}")
    log.info("  Solis inverter: slave ID 1")
    log.info("  Eastron meter:  slave ID 2")
    log.info("Use: python app.py --gateway-ip 127.0.0.1 --gateway-port %d", args.port)

    StartTcpServer(context=server_ctx, address=(args.host, args.port))


if __name__ == "__main__":
    main()
