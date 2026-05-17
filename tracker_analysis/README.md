# tracker_analysis

**Arctech Solar Tracker with Longi Mono and Bifacial Module Analysis** — a
side-by-side performance dashboard for the Smart Energy Lab at Mooramoora.
Four Longi 435 W strings on two Arctech trackers (1P and 2P) feed into the
Solis 50 kW hybrid inverter. The bifacial-vs-mono pairing is identical in
every respect except module type, which makes the wattage delta between the
strings a clean measurement of bifacial gain.

## Experimental setup

| String | Tracker | Module type             | Nameplate                              |
| ------ | ------- | ----------------------- | -------------------------------------- |
| PV1    | 1P      | Bifacial   435 W Longi  | 13 series × 3 parallel = 16,965 W      |
| PV2    | 1P      | Monofacial 435 W Longi  | 13 series × 3 parallel = 16,965 W      |
| PV3    | 2P      | Monofacial 435 W Longi  | 13 series × 3 parallel = 16,965 W      |
| PV4    | 2P      | Bifacial   435 W Longi  | 13 series × 3 parallel = 16,965 W      |

Total nameplate: ~68 kW DC.

The pairs (PV1 with PV2 on the 1P tracker, PV4 with PV3 on the 2P tracker)
share identical orientation and geometry. The only experimental variable is
module type, so any sustained delta in their output is bifacial gain.

## Headline KPIs

```
BG_1P = (PV1 − PV2) / PV2 × 100%     ← bifacial gain on the 1P tracker
BG_2P = (PV4 − PV3) / PV3 × 100%     ← bifacial gain on the 2P tracker
```

Industry-typical values: 1P trackers see 10–18 % bifacial gain, 2P see
4–10 % (lower because the second row of panels shades the rear of the first
from ground-reflected light). This dashboard exists to measure the actual
delta on this site under the trackers' real operating conditions.

## Architecture

```
                              ┌─────────────────────────────┐
                              │   rubberduck.local:5000     │
                              │   /api/solis/data           │
                              │   (Modbus TCP to Solis)     │
                              └──────────────┬──────────────┘
                                             │ HTTP poll (1 s)
                                             ▼
                              ┌──────────────────────────────┐
                              │   desky.local:8901           │
                              │   tracker_analysis (Flask)   │
                              │   + sun position, BG math    │
                              │   + per-string Wh integrator │
                              │   + CSV log (5 s cadence)    │
                              │   + dashboard                │
                              └──────────────┬───────────────┘
                                             │
                                       /home/glen/sma/
                                             │ filesystem watch (10 s)
                                             │
                              ┌──────────────┴─────────────┐
                              │ vsftpd (already on desky)  │
                              └──────────────┬─────────────┘
                                             │ FTP-Push every 10 s
                              ┌──────────────┴─────────────┐
                              │   SMA Sunny WebBox         │
                              │   192.168.55.126           │
                              │     ├─ SENS0700:31621      │
                              │     │   (Sunny Sensorbox)  │
                              │     └─ PYRA0102:158212186  │
                              │         (SMA Pyranometer / │
                              │          Meteo Station)    │
                              └────────────────────────────┘
```

### From the Solis inverter (per string)
- Voltage, current, computed power (V × A)
- Polled by the existing `microgrid_remote_monitor` service on rubberduck;
  this app just reads it via HTTP.

### From the SMA WebBox via FTP-Push (15-minute averages)
The WebBox pushes nested ZIP files to `~/sma/` on desky every ~10 s. Each
push contains:
- `Info.xml` — WebBox metadata header
- `Mean.YYYYMMDD_HHMMSS.xml.zip` — nested zip with `<MeanPublic>` blocks
  containing 15-minute average values
- `Log.YYYYMMDD_HHMMSS.xml.zip` — nested zip with the event log

The reader (`sma_ftp.py`) watches the directory, parses any new outer
ZIPs it hasn't seen, and extracts the latest values for these channels:

| Channel (SMA `<Key>`)           | Mapped to            | Source device                       |
| ------------------------------- | -------------------- | ----------------------------------- |
| `SENS0700:*:IntSolIrr`          | `radiation_wm2`      | Sunny Sensorbox (silicon cell)      |
| `SENS0700:*:TmpAmb C`           | `ambient_temp_c`     | Sunny Sensorbox                     |
| `SENS0700:*:TmpMdul C`          | `module_temp_c`      | Sunny Sensorbox                     |
| `SENS0700:*:WindVel m/s`        | `wind_speed_ms`      | Sunny Sensorbox                     |
| `PYRA0102:*:IntSolIrr`          | `pyranometer_wm2`    | SMA Pyranometer / Meteo Station     |
| `PYRA0102:*:envhmdt`            | `humidity_pct`       | SMA Pyranometer / Meteo Station     |
| `PYRA0102:*:envpress`           | `air_pressure_pa`*   | SMA Pyranometer / Meteo Station     |

\* SMA reports `envpress` in hPa despite the doc-labelled unit "Pa"; the
parser multiplies by 100 so the value is genuinely Pa for downstream maths.

### Why FTP-Push instead of Modbus or RPC

The WebBox on site (firmware 1.53, Modbus profile version 1) **doesn't expose
either** of the obvious data paths:

- **Modbus TCP:** the 34000-range Meteo Station / Sensorbox registers
  exist, but every connected device sits at the default Unit ID 255
  (unaddressable) and the device-assignment table at registers 42109+
  isn't implemented in this profile version, so we can't reassign them
  remotely. The WebBox UI's "Data > Devices > Modbus" page (per the docs)
  doesn't exist in this firmware either.
- **HTTP-RPC at `/rpc`:** the docs describe a JSON-RPC endpoint, but it
  has to be explicitly enabled via "Use RPC" in the UI — which isn't
  present in this firmware's Settings menu.

FTP-Push, by contrast, is already configured and working. The WebBox
uploads to `192.168.55.93:21` (desky's IP) every 10 s with credentials
`glen` / `*****`. We just consume what's already arriving — no firmware
upgrade needed.

### Computed locally
- **Sun azimuth & elevation** (NOAA SPA simplified — see `solar_pos.py`)
- **Tracker tilt** — NS-axis backtracking model, since the Arctech tracker
  doesn't expose tilt on the network
- **Clear-sky GHI** (Haurwitz model — used for clearness-index trending)
- **Bifacial gain** — instantaneous and energy-weighted daily averages
- **Performance Ratio** per string (instantaneous)
- **Specific yield** per string (today's kWh / kWp)

## Dashboard

**Top KPI row:** `1P bifacial gain (now + today avg) · 2P bifacial gain
(now + today avg) · POA irradiance · Sun elevation · Tracker tilt`.

**Per-string strip:** V, A, W, today's kWh, specific yield (kWh/kWp), and
instantaneous Performance Ratio for each of PV1–PV4. Bifacial strings
have orange/red left-border accents; mono strings have grey accents.

**Detail tiles:** Pyranometer · Module Temp · Ambient Temp · Wind ·
Humidity · Air Pressure · Clear-sky GHI · Total PV (now) · Total Today.

**Charts:**
1. **Per-string power today** — 4 lines: PV1 (1P bifacial, solid orange),
   PV2 (1P mono, dashed grey), PV3 (2P mono, dashed dark-grey), PV4 (2P
   bifacial, solid red).
2. **Bifacial gain today (%)** — the headline chart. Two lines, BG_1P and
   BG_2P, computed each tick whenever the mono pair is above 200 W noise
   floor.
3. **Irradiance + temperatures** — POA W/m² (left axis), module °C and
   ambient °C (right axis) — useful for seeing module heating lag the
   irradiance curve, and the temperature crossover at dusk.
4. **Power vs Irradiance scatter** — 4 colours, slope = string efficiency.
   Useful for spot-checking degradation or shading.

## Quick start

```bash
git clone https://github.com/glenmo/microgrid_remote_monitor ~/microgrid_remote_monitor
cd ~/microgrid_remote_monitor/tracker_analysis
sudo bash install.sh
```

Defaults: listens on `0.0.0.0:8901`, polls
`http://rubberduck.local:5000/api/solis/data` every 1 s, watches
`~/sma/` every 10 s for new WebBox push ZIPs, writes CSV to `./data/`.
Open `http://desky.local:8901/`.

The install script auto-detects the invoking user's home directory
(via `getent passwd $SUDO_USER`) when building the SMA watch path, so
`sudo bash install.sh` gives `/home/glen/sma` rather than `/root/sma`.

## CLI options

```
--host                  Flask listen address           (default 0.0.0.0)
--port                  Flask listen port              (default 8901)
--solis-url             Solis upstream                 (default http://rubberduck.local:5000/api/solis/data)
--solis-poll            Solis poll interval (s)        (default 1.0)
--sma-watch-dir         directory to watch for SMA ZIPs (default $HOME/sma)
--sma-poll              SMA watch-dir scan interval (s) (default 10.0)
--csv-interval          CSV log interval (s)           (default 5.0)
--csv-dir               where to write CSVs            (default ./data)
--lat                   site latitude (degrees)        (default -37.4)
--lon                   site longitude (degrees)       (default 144.9)
--string-kwp            nameplate per string (kWp)     (default 16.965)
--site-name             dashboard title text
```

## SMA reader tools

`sma_ftp.py` is the production reader (FTP-Push, parses WebBox ZIPs).

Quick standalone tests:

```bash
# Parse a specific ZIP and print its channels
./venv/bin/python sma_ftp.py --parse ~/sma/wb150006552.20260517-202636.zip

# Watch directory, scan once, print latest sample
./venv/bin/python sma_ftp.py --once --watch-dir ~/sma

# Run continuously (Ctrl-C to stop) — useful for debugging
./venv/bin/python sma_ftp.py --watch-dir ~/sma -v
```

`sma_reader.py` (Modbus) and `sma_http.py` (RPC) approaches are also
present in the repo but **not used by app.py** — they're kept for
reference in case the WebBox firmware is ever upgraded to a profile
version that exposes those interfaces. The Modbus reader has working
`--probe`, `--discover`, and `--set-unit-id` flags for that future work.

## CSV schema

`./data/YYYY-MM-DD.csv` — one row every 5 s, rotates at local midnight:

```
timestamp,
pv1_v, pv1_a, pv1_w,
pv2_v, pv2_a, pv2_w,
pv3_v, pv3_a, pv3_w,
pv4_v, pv4_a, pv4_w,
poa_wm2, pyranometer_wm2,
mod_temp_c, amb_temp_c, wind_ms,
humidity_pct, air_pressure_pa,
sun_elev_deg, sun_azim_deg, tracker_tilt_deg, clear_sky_ghi,
bg_1p_pct, bg_2p_pct,
pr_pv1, pr_pv2, pr_pv3, pr_pv4
```

Weather columns can be `null` (empty) until the WebBox has pushed a Mean
file with that channel since the service started — they fill in within
~10 minutes of startup under normal conditions.

## Notes

- **WebBox push cadence**: every ~10 s the WebBox uploads a ZIP. Most are
  small "heartbeat" uploads (`Info.xml` only); the data-bearing ones with
  `Mean.*.xml.zip` inside arrive at the 15-minute interval boundaries.
  Effective weather sample rate is therefore 15 minutes — fine for
  bifacial gain analysis since that's a slow-moving signal.
- **Per-string PV cadence** is bounded by rubberduck's Solis Modbus poll
  (5 s after today's hardening on the microgrid_remote_monitor side).
- **Performance Ratio** is computed as
  `(P_dc / P_nameplate) / (POA / 1000)`. Values >100 % at low irradiance
  are normal (PR is unreliable below ~50 W/m²); the dashboard suppresses
  PR display when POA < 50 W/m² to avoid the noise.
- **Tracker tilt** is computed from sun position, not measured. The
  Arctech tracker doesn't expose tilt on the network. Tilt code uses a
  configurable backtracking model with `gcr=0.4` and `max_tilt=55°`.
- **PYRA0102 calibration status:** as of installation the pyranometer
  reports `Mode=2 (Warning)` and `Error=11 (WrnMtSensSolIrr)`. Its
  `IntSolIrr` readings are 5–15 % below the SENS0700 silicon-cell reading
  and shouldn't be trusted as the absolute reference until recalibrated.
  Use `radiation_wm2` (from SENS0700) as the primary POA for PR maths;
  `pyranometer_wm2` is logged but currently informational only.
- **Clearness index** = `POA / clear_sky_GHI`. POA differs from GHI by
  sun-angle geometry, so this is approximate, but useful for trending
  sky conditions across days.
- **Pressure unit:** SMA reports `envpress` as hPa even though their doc
  labels it "Pa". The parser converts to actual Pa (×100) so the
  dashboard maths (`/100` for hPa display) reads correctly.
