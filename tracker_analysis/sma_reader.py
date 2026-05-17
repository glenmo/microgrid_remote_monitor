"""SMA WebBox Modbus TCP reader for the Sunny Sensor (SENS0700).

Reads irradiance, module/ambient temperature and wind speed from a SMA
Sunny Sensor connected to a SMA Sunny WebBox over Modbus TCP.

The SMA Modbus profile uses 32-bit signed integers at specific register
addresses. Exact addresses depend on which slots the Sensor Box is
mapped into by the WebBox; they're typically stable per-install but can
shift if WebBox config changes.

Defaults below are best-guess for a typical Sunny WebBox + SensorBox
combination. To verify (or rediscover) the addresses, run:

    python sma_reader.py --probe --sma-host 192.168.55.126

That scans 30000–31100 and prints every non-trivial 32-bit value with a
scaled interpretation guess (×0.01 °C, ×0.1 m/s, W/m² as-is).

Cross-reference the printed values against what the WebBox web UI shows
at that moment, then update SMA_REGISTERS below and restart.
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Register map — defaults, override after probe if needed.
#
# Each entry: (address, name, scale, unit). All are 32-bit (count=2).
# SMA convention is big-endian, signed 32-bit. Special value 0x80000000
# means "not available" → reported as None.
# ---------------------------------------------------------------------------
SMA_REGISTERS = {
    # (address, scale_factor, unit, signed)
    # Per SMA Modbus Profile §§ 5.4.14 (SMA Meteo Station) and 5.4.15
    # (Sunny Sensorbox). Sensorbox adds the external pyranometer at 34623;
    # Meteo Station adds humidity (34617) and air pressure (34619). Reading
    # an unsupported address returns the SMA "not available" sentinel,
    # which we already map to None.
    "ambient_temp_c":  (34609, 0.01, "°C",   True),   # TmpAmb C   S32 TEMP
    "radiation_wm2":   (34613, 1.0,  "W/m²", False),  # IntSolIrr  U32 FIX0
    "wind_speed_ms":   (34615, 0.1,  "m/s",  False),  # WindVel    U32 FIX1
    "humidity_pct":    (34617, 0.01, "%",    False),  # envhmdt    U32 FIX2 (Meteo Station only)
    "air_pressure_pa": (34619, 0.01, "Pa",   False),  # envpress   U32 FIX2 (Meteo Station only)
    "module_temp_c":   (34621, 0.01, "°C",   True),   # TmpMdul C  S32 TEMP
    "pyranometer_wm2": (34623, 1.0,  "W/m²", False),  # ExlSolIrr  U32 FIX0 (Sensorbox / Meteo Station + pyranometer)
}

# SMA "value not available" sentinels
SMA_NA_S32 = -0x80000000   # -2147483648
SMA_NA_U32 = 0xFFFFFFFF    # 4294967295

# Unit IDs 3–247 reach Meteo Station / Sensorbox per the SMA Modbus profile.
# Unit 1 reaches the WebBox itself, which has a different register layout.
DEFAULT_UNIT_ID = 3


@dataclass
class SmaSample:
    ts: datetime
    radiation_wm2: Optional[float] = None      # internal silicon cell (IntSolIrr)
    pyranometer_wm2: Optional[float] = None    # external pyranometer (ExlSolIrr)
    module_temp_c: Optional[float] = None
    ambient_temp_c: Optional[float] = None
    wind_speed_ms: Optional[float] = None
    humidity_pct: Optional[float] = None
    air_pressure_pa: Optional[float] = None
    raw: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "ts": self.ts.isoformat(),
            "radiation_wm2":    self.radiation_wm2,
            "pyranometer_wm2":  self.pyranometer_wm2,
            "module_temp_c":    self.module_temp_c,
            "ambient_temp_c":   self.ambient_temp_c,
            "wind_speed_ms":    self.wind_speed_ms,
            "humidity_pct":     self.humidity_pct,
            "air_pressure_pa":  self.air_pressure_pa,
        }


class SmaReader:
    """Background-polling reader for the SMA WebBox.

    Holds the latest sample under a lock; get_data() returns a copy.
    """
    def __init__(self, host: str, port: int = 502, unit_id: int = DEFAULT_UNIT_ID,
                 poll_interval: float = 5.0):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.poll_interval = poll_interval

        self.client: Optional[ModbusTcpClient] = None
        self.connected = False
        self.last_read_time: Optional[datetime] = None
        self.total_reads = 0
        self.read_errors = 0

        self._latest: Optional[SmaSample] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- connection plumbing ---------------------------------------------
    def connect(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        try:
            self.client = ModbusTcpClient(host=self.host, port=self.port, timeout=3)
            self.connected = self.client.connect()
            if self.connected:
                log.info(f"SMA: connected to WebBox at {self.host}:{self.port}")
            else:
                log.warning(f"SMA: failed to connect to {self.host}:{self.port}")
        except Exception as e:
            log.error(f"SMA: connection error: {e}")
            self.connected = False

    def disconnect(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
            self.connected = False

    # ---- low-level read --------------------------------------------------
    def _read_32(self, address: int, signed: bool = False) -> Optional[int]:
        """Read a 32-bit big-endian value at `address` (2 input regs).

        ``signed`` selects S32 vs U32 interpretation. Returns ``None`` on
        a read error so the caller can keep the previous value.
        """
        if not self.connected:
            self.connect()
            if not self.connected:
                return None
        try:
            # SMA Modbus on Sunny WebBox: spot values are exposed via
            # Read Holding Registers (FC=0x03), not Input Registers.
            try:
                result = self.client.read_holding_registers(
                    address=address, count=2, device_id=self.unit_id
                )
            except TypeError:
                # older pymodbus
                result = self.client.read_holding_registers(
                    address=address, count=2, slave=self.unit_id
                )
            if isinstance(result, ModbusIOException) or result.isError():
                log.warning(f"SMA: read error at {address} unit={self.unit_id}: {result}")
                # Single-register read failures shouldn't blow the whole
                # connection — return None and let the caller keep going.
                return None
            regs = result.registers
            if len(regs) != 2:
                return None
            raw = (regs[0] << 16) | regs[1]
            if signed and raw >= 0x80000000:
                raw -= 0x100000000
            return raw
        except Exception as e:
            log.error(f"SMA: exception reading {address}: {e}")
            self.connected = False
            return None

    @staticmethod
    def _scale(raw: Optional[int], factor: float, signed: bool) -> Optional[float]:
        if raw is None:
            return None
        # SMA "not available" sentinels — different for S32 and U32
        if signed and raw == SMA_NA_S32:
            return None
        if not signed and raw == SMA_NA_U32:
            return None
        return raw * factor

    # ---- poll once -------------------------------------------------------
    def poll_once(self) -> Optional[SmaSample]:
        """Read every configured register independently.

        SMA Modbus addresses are not necessarily contiguous, so a failure
        on one address says nothing about the others. We try them all and
        accept partial samples — only an entire-poll failure counts as an
        error and trips the watchdog.
        """
        sample = SmaSample(ts=datetime.now())
        any_ok = False
        for name, (addr, scale, _unit, signed) in SMA_REGISTERS.items():
            raw = self._read_32(addr, signed=signed)
            sample.raw[name] = raw
            val = self._scale(raw, scale, signed)
            setattr(sample, name, val)
            if raw is not None:
                any_ok = True
        if any_ok:
            self.total_reads += 1
            self.last_read_time = sample.ts
            with self._lock:
                self._latest = sample
            return sample
        else:
            self.read_errors += 1
            return None

    # ---- background loop -------------------------------------------------
    def _loop(self):
        last_forced_reconnect = datetime.now()
        stale_s = max(30.0, 3.0 * self.poll_interval)
        cooldown_s = max(30.0, 2.0 * self.poll_interval)
        last_hb = datetime.now()

        log.info(f"SMA: poll loop entered (poll={self.poll_interval}s, stale_th={stale_s}s)")

        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.error(f"SMA poll error: {e}")

            now = datetime.now()
            if (now - last_hb).total_seconds() >= 30:
                age = ((now - self.last_read_time).total_seconds()
                       if self.last_read_time else None)
                age_str = f"{age:.0f}s" if age is not None else "never"
                log.info(f"SMA heartbeat: connected={self.connected}, "
                         f"last_read_age={age_str}, reads={self.total_reads}, "
                         f"errors={self.read_errors}")
                last_hb = now

            # Watchdog
            if self.last_read_time is not None:
                age_s = (now - self.last_read_time).total_seconds()
                cool = (now - last_forced_reconnect).total_seconds()
                if age_s > stale_s and cool > cooldown_s:
                    log.warning(f"SMA watchdog: {age_s:.0f}s stale — reconnecting")
                    self.disconnect()
                    self.connect()
                    last_forced_reconnect = now

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

    # ---- public API ------------------------------------------------------
    def get_data(self) -> Optional[dict]:
        with self._lock:
            return self._latest.as_dict() if self._latest else None

    def get_status(self) -> dict:
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "unit_id": self.unit_id,
            "poll_interval": self.poll_interval,
            "total_reads": self.total_reads,
            "read_errors": self.read_errors,
            "last_read": (self.last_read_time.isoformat()
                          if self.last_read_time else None),
        }


# ---------------------------------------------------------------------------
# Probe — scan a register range and print decoded values
# ---------------------------------------------------------------------------
def probe(host: str, port: int = 502, unit_id: int = DEFAULT_UNIT_ID,
          start: int = 34600, end: int = 34700):
    """Scan register space; print non-trivial 32-bit values + scaled guesses.

    Compare what's printed against the WebBox web UI to identify the
    addresses for radiation, module temp, ambient temp, wind speed.
    """
    print(f"Probing SMA WebBox at {host}:{port}, unit_id={unit_id}, "
          f"range {start}..{end}")
    client = ModbusTcpClient(host=host, port=port, timeout=3)
    if not client.connect():
        print("  ✗ connection refused")
        return
    print("  ✓ connected")
    print()
    print(f"  {'addr':>6}  {'raw int32':>14}  {'÷1 (W/m²)':>12}  "
          f"{'×0.01 (°C)':>12}  {'×0.1 (m/s)':>12}  {'×0.001 (h)':>12}")
    print("  " + "-" * 84)

    # Step by 1 — SMA can start a 32-bit value at any register address
    for addr in range(start, end):
        try:
            try:
                r = client.read_holding_registers(address=addr, count=2,
                                                  device_id=unit_id)
            except TypeError:
                r = client.read_holding_registers(address=addr, count=2,
                                                  slave=unit_id)
            if isinstance(r, ModbusIOException) or r.isError():
                continue
            regs = r.registers
            if len(regs) != 2:
                continue
            raw = (regs[0] << 16) | regs[1]
            if raw >= 0x80000000:
                raw -= 0x100000000
            # Skip "not available" and trivial zeros
            if raw == SMA_NA_S32 or raw == 0:
                continue
            # Plausibility filters — only show values that look like real
            # measurements in any of the four reasonable scalings.
            wm2 = raw
            cel = raw * 0.01
            ms  = raw * 0.1
            hrs = raw * 0.001
            looks_like = (
                (0 < wm2 < 1500) or
                (-40 < cel < 90) or
                (0 < ms < 50) or
                (1000 < hrs < 200000)   # SMA-h-On style operating-hour counters
            )
            if not looks_like:
                continue
            print(f"  {addr:>6}  {raw:>14}  {wm2:>12.1f}  "
                  f"{cel:>12.2f}  {ms:>12.2f}  {hrs:>12.3f}")
        except Exception:
            continue
        # Light pacing so we don't hammer the WebBox
        time.sleep(0.02)

    client.close()
    print()
    print("  Cross-reference with the WebBox web UI Sunny Sensor values to "
          "identify which address is which. Update SMA_REGISTERS in this "
          "file and restart.")


def _setup_logging(verbose: bool = False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def main():
    p = argparse.ArgumentParser(description="SMA WebBox Modbus reader")
    p.add_argument("--sma-host", default="192.168.55.126")
    p.add_argument("--sma-port", type=int, default=502)
    p.add_argument("--unit-id", type=int, default=DEFAULT_UNIT_ID)
    p.add_argument("--probe", action="store_true",
                   help="Scan register space and print plausible values")
    p.add_argument("--probe-start", type=int, default=34600)
    p.add_argument("--probe-end", type=int, default=34700)
    p.add_argument("--once", action="store_true",
                   help="Single read using configured registers")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    _setup_logging(args.verbose)

    if args.probe:
        probe(args.sma_host, args.sma_port, args.unit_id,
              args.probe_start, args.probe_end)
        return

    if args.once:
        reader = SmaReader(args.sma_host, args.sma_port, args.unit_id)
        reader.connect()
        sample = reader.poll_once()
        if sample:
            print(sample.as_dict())
        else:
            print("No data — try --probe to find the right addresses.")
        reader.disconnect()
        return

    # Otherwise, run continuously and print each sample
    reader = SmaReader(args.sma_host, args.sma_port, args.unit_id)
    reader.start()
    try:
        while True:
            time.sleep(args.unit_id * 0 + 5)  # no-op of arg
            data = reader.get_data()
            if data:
                print(data)
    except KeyboardInterrupt:
        reader.stop()


if __name__ == "__main__":
    main()
