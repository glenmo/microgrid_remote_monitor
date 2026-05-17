"""SMA WebBox FTP-Push reader.

The SMA Sunny WebBox uploads sensor data to a configured FTP server as
nested ZIPs. Each upload is a ZIP file (outer) named like
``wb150006552.YYYYMMDD-HHMMSS.zip`` containing:

  - ``Info.xml``                              — WebBox metadata (always present)
  - ``Mean.YYYYMMDD_HHMMSS.xml.zip``          — nested ZIP with 15-min averages
  - ``Log.YYYYMMDD_HHMMSS.xml.zip``           — nested ZIP with event log

The Mean ZIP contains a single ``Mean.YYYYMMDD_HHMMSS.xml`` with one
``<MeanPublic>`` element per channel:

    <MeanPublic>
      <Key>SENS0700:31621:TmpAmb C</Key>
      <First>11.43</First><Last>11.73</Last>
      <Min>11.03</Min><Max>12.43</Max>
      <Mean>11.435607</Mean>
      <Base>107</Base>
      <Period>900</Period>
      <TimeStamp>2026-01-05T08:15:30</TimeStamp>
    </MeanPublic>

This reader watches a directory for new outer ZIPs, parses each one's
Mean XML, and exposes the latest reading per channel via ``get_data()``.

Same interface as ``sma_reader.SmaReader`` so it's a drop-in replacement
in ``app.py``.
"""
from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel mapping
# ---------------------------------------------------------------------------
# Maps the SMA <Key> field (Device:Serial:Channel) to the standard
# attribute names that app.py expects. The first match wins, so if both
# SENS0700 and PYRA0102 report TmpAmb C we use whichever is in the dict
# below — PYRA0102 (Meteo Station) values tend to be slightly more
# accurate and we prefer it where both exist.
CHANNEL_MAP = {
    # PYRA0102 first (preferred when both devices report)
    "PYRA0102:*:IntSolIrr":   "pyranometer_wm2",
    "PYRA0102:*:envhmdt":     "humidity_pct",
    "PYRA0102:*:envpress":    "air_pressure_pa",
    # SENS0700 — Sunny Sensorbox (always present)
    "SENS0700:*:IntSolIrr":   "radiation_wm2",
    "SENS0700:*:TmpAmb C":    "ambient_temp_c",
    "SENS0700:*:TmpMdul C":   "module_temp_c",
    "SENS0700:*:WindVel m/s": "wind_speed_ms",
}


@dataclass
class SmaSample:
    ts: datetime
    radiation_wm2: Optional[float] = None
    pyranometer_wm2: Optional[float] = None
    module_temp_c: Optional[float] = None
    ambient_temp_c: Optional[float] = None
    wind_speed_ms: Optional[float] = None
    humidity_pct: Optional[float] = None
    air_pressure_pa: Optional[float] = None
    source_ts: Optional[datetime] = None  # the WebBox <TimeStamp> field
    source_file: Optional[str] = None

    def as_dict(self):
        return {
            "ts":               self.ts.isoformat(),
            "radiation_wm2":    self.radiation_wm2,
            "pyranometer_wm2":  self.pyranometer_wm2,
            "module_temp_c":    self.module_temp_c,
            "ambient_temp_c":   self.ambient_temp_c,
            "wind_speed_ms":    self.wind_speed_ms,
            "humidity_pct":     self.humidity_pct,
            "air_pressure_pa":  self.air_pressure_pa,
            "source_ts":        self.source_ts.isoformat() if self.source_ts else None,
            "source_file":      self.source_file,
        }


def _key_matches(channel_key: str, pattern: str) -> bool:
    """Match 'PYRA0102:*:IntSolIrr' against 'PYRA0102:158212186:IntSolIrr'."""
    p_parts = pattern.split(":")
    k_parts = channel_key.split(":")
    if len(p_parts) != len(k_parts):
        return False
    for p, k in zip(p_parts, k_parts):
        if p == "*":
            continue
        if p != k:
            return False
    return True


def parse_mean_xml(xml_bytes: bytes) -> tuple[dict, Optional[datetime]]:
    """Parse a Mean.YYYYMMDD_HHMMSS.xml document.

    Returns (values_dict, source_timestamp).
    values_dict keys are the mapped attribute names (radiation_wm2, etc.).
    source_timestamp is the <TimeStamp> of the entries (assumed uniform
    across one Mean file — it's the start of the 15-min window).
    """
    values: dict[str, float] = {}
    latest_ts: Optional[datetime] = None

    # The XML doesn't have a namespace, just bare elements. Use a forgiving parser.
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log.warning(f"Mean XML parse error: {e}")
        return values, None

    # Iterate every MeanPublic regardless of depth or namespace
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]  # strip namespace if any
        if tag != "MeanPublic":
            continue
        key_el  = elem.find("Key")
        mean_el = elem.find("Mean")
        ts_el   = elem.find("TimeStamp")
        if key_el is None or mean_el is None or key_el.text is None or mean_el.text is None:
            continue
        channel_key = key_el.text.strip()
        try:
            mean_val = float(mean_el.text)
        except ValueError:
            continue

        # Find which standard name this maps to
        for pattern, std_name in CHANNEL_MAP.items():
            if _key_matches(channel_key, pattern):
                # SMA reports air pressure in hPa despite labelling it 'Pa'.
                # Convert hPa → Pa so the existing dashboard maths (/100 for
                # display) is correct.
                if std_name == "air_pressure_pa":
                    mean_val = mean_val * 100.0
                values.setdefault(std_name, mean_val)
                break

        # Track the latest TimeStamp we've seen
        if ts_el is not None and ts_el.text:
            try:
                ts = datetime.fromisoformat(ts_el.text.strip())
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
            except ValueError:
                pass

    return values, latest_ts


def parse_outer_zip(zip_path: Path) -> Optional[tuple[dict, Optional[datetime]]]:
    """Parse an outer WebBox push ZIP.

    The outer ZIP contains Info.xml, plus zero or more Mean.*.xml.zip
    entries. Each nested Mean.*.xml.zip contains a single Mean.*.xml.

    Returns (values_dict, source_ts) of the LATEST Mean inside, or None
    if no Mean data was present (heartbeat upload).
    """
    if not zip_path.exists():
        return None
    try:
        with zipfile.ZipFile(zip_path, "r") as outer:
            mean_zip_names = sorted(
                [n for n in outer.namelist() if n.startswith("Mean.") and n.endswith(".xml.zip")]
            )
            if not mean_zip_names:
                return None
            best_values: dict = {}
            best_ts: Optional[datetime] = None
            # Process the LAST Mean file (newest timestamp by name sort)
            for mz_name in mean_zip_names[-1:]:
                with outer.open(mz_name) as mz_fh:
                    mz_bytes = mz_fh.read()
                with zipfile.ZipFile(io.BytesIO(mz_bytes), "r") as inner:
                    for xml_name in inner.namelist():
                        if not xml_name.endswith(".xml"):
                            continue
                        xml_bytes = inner.read(xml_name)
                        values, ts = parse_mean_xml(xml_bytes)
                        if values and (best_ts is None or (ts and ts > (best_ts or datetime.min))):
                            best_values = values
                            best_ts = ts
            if not best_values:
                return None
            return best_values, best_ts
    except (zipfile.BadZipFile, OSError) as e:
        log.warning(f"Failed to parse {zip_path.name}: {e}")
        return None


# ---------------------------------------------------------------------------
# Reader — same interface as sma_reader.SmaReader
# ---------------------------------------------------------------------------
class SmaFtpReader:
    """Watches an FTP-push directory for incoming SMA WebBox ZIPs and
    exposes the latest sensor values via ``get_data()`` and ``get_status()``.

    Drop-in replacement for ``SmaReader`` (Modbus) — same public API.
    """
    OUTER_ZIP_RE = re.compile(r"^wb\d+\.\d{8}-\d{6}\.zip$")

    def __init__(self, watch_dir: str, scan_interval: float = 10.0,
                 stale_after_s: float = 1800.0):
        self.watch_dir = Path(watch_dir).expanduser()
        self.scan_interval = scan_interval
        self.stale_after_s = stale_after_s

        # Track processed files so we don't re-parse them
        self._seen: set[str] = set()
        # Cumulative latest values across all parsed files
        self._latest: Optional[SmaSample] = None
        self._latest_lock = threading.Lock()

        self.last_read_time: Optional[datetime] = None
        self.total_reads = 0
        self.read_errors = 0
        self.connected = False  # "connected" means watch dir exists

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- public API (mirrors SmaReader) ---------------------------------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_data(self) -> Optional[dict]:
        with self._latest_lock:
            if self._latest is None:
                return None
            return self._latest.as_dict()

    def get_status(self) -> dict:
        age = None
        if self.last_read_time:
            age = (datetime.now() - self.last_read_time).total_seconds()
        return {
            "transport":     "ftp-push",
            "watch_dir":     str(self.watch_dir),
            "connected":     self.connected,
            "scan_interval": self.scan_interval,
            "total_reads":   self.total_reads,
            "read_errors":   self.read_errors,
            "last_read":     self.last_read_time.isoformat() if self.last_read_time else None,
            "age_s":         age,
            "stale_after_s": self.stale_after_s,
            "files_seen":    len(self._seen),
        }

    # ---- background loop -------------------------------------------------
    def _loop(self):
        log.info(f"SMA FTP reader watching {self.watch_dir}")
        last_heartbeat = datetime.now()
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as e:
                log.error(f"SMA FTP scan error: {e}")
                self.read_errors += 1
            now = datetime.now()
            if (now - last_heartbeat).total_seconds() >= 60:
                age = ((now - self.last_read_time).total_seconds()
                       if self.last_read_time else None)
                age_str = f"{age:.0f}s" if age is not None else "never"
                log.info(
                    f"SMA FTP heartbeat: connected={self.connected}, "
                    f"last_read_age={age_str}, files_seen={len(self._seen)}, "
                    f"total_reads={self.total_reads}, errors={self.read_errors}"
                )
                last_heartbeat = now
            self._stop.wait(self.scan_interval)

    def _scan_once(self):
        if not self.watch_dir.exists():
            self.connected = False
            return
        self.connected = True
        # Newest files last so the latest values win
        try:
            entries = sorted(self.watch_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        except OSError as e:
            log.warning(f"Cannot list {self.watch_dir}: {e}")
            return

        new_files = [p for p in entries
                     if p.is_file()
                     and self.OUTER_ZIP_RE.match(p.name)
                     and p.name not in self._seen]
        if not new_files:
            return

        for path in new_files:
            self._seen.add(path.name)
            parsed = parse_outer_zip(path)
            if parsed is None:
                # Info-only heartbeat — skip silently
                continue
            values, source_ts = parsed
            now = datetime.now()
            sample = SmaSample(
                ts=now,
                source_ts=source_ts,
                source_file=path.name,
                **{k: v for k, v in values.items()
                   if k in {"radiation_wm2", "pyranometer_wm2",
                            "module_temp_c", "ambient_temp_c",
                            "wind_speed_ms", "humidity_pct",
                            "air_pressure_pa"}},
            )
            with self._latest_lock:
                # Merge: keep previous fields that are None in the new sample
                prev = self._latest
                if prev:
                    for fld in ("radiation_wm2", "pyranometer_wm2",
                                "module_temp_c", "ambient_temp_c",
                                "wind_speed_ms", "humidity_pct",
                                "air_pressure_pa"):
                        if getattr(sample, fld) is None:
                            setattr(sample, fld, getattr(prev, fld))
                self._latest = sample
            self.last_read_time = now
            self.total_reads += 1
            log.info(f"SMA: ingested {path.name} (source_ts={source_ts})")


# ---------------------------------------------------------------------------
# CLI for one-shot testing
# ---------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser(description="SMA WebBox FTP-push reader")
    p.add_argument("--watch-dir", default=os.path.expanduser("~/sma"))
    p.add_argument("--once", action="store_true",
                   help="Scan once and print latest sample")
    p.add_argument("--parse", metavar="FILE",
                   help="Parse a specific outer zip and print its contents")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    if args.parse:
        result = parse_outer_zip(Path(args.parse))
        if result is None:
            print("(no Mean data — heartbeat-only zip)")
        else:
            values, ts = result
            print(f"source_timestamp: {ts}")
            for k, v in values.items():
                print(f"  {k:>20}: {v}")
        return

    reader = SmaFtpReader(args.watch_dir)
    reader.start()
    if args.once:
        # Wait briefly for the first scan, then print
        time.sleep(2)
        data = reader.get_data()
        status = reader.get_status()
        print(f"Status: {status}")
        print(f"Data:   {data}")
        reader.stop()
    else:
        try:
            while True:
                time.sleep(15)
                print(reader.get_data())
        except KeyboardInterrupt:
            reader.stop()


if __name__ == "__main__":
    main()
