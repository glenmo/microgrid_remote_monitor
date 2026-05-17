# tracker_analysis

Bifacial-vs-monofacial PV performance dashboard for the Smart Energy Lab
at Moora Moora. Four Longi 435 W strings on two Arctech trackers go into
the Solis 50 kW hybrid inverter; this app shows the side-by-side gain
the bifacial panels deliver over their identical-conditions mono twins.

## Experimental setup

| String | Tracker | Module type | Nameplate |
| ------ | ------- | ----------- | --------- |
| PV1    | 1P      | Bifacial 435 W Longi   | 13 series × 3 parallel = 16,965 W |
| PV2    | 1P      | Monofacial 435 W Longi | 13 series × 3 parallel = 16,965 W |
| PV3    | 2P      | Monofacial 435 W Longi | 13 series × 3 parallel = 16,965 W |
| PV4    | 2P      | Bifacial 435 W Longi   | 13 series × 3 parallel = 16,965 W |

Total nameplate: ~68 kW DC.

The pairs (PV1 with PV2 on the 1P, PV4 with PV3 on the 2P) share
identical geometry and orientation. The only variable is module type.
Any sustained delta in their output is bifacial gain.

## Headline KPIs

```
BG_1P = (PV1 − PV2) / PV2 × 100%     ← bifacial gain on the 1P tracker
BG_2P = (PV4 − PV3) / PV3 × 100%     ← bifacial gain on the 2P tracker
```

Industry-typical: 1P trackers see 10–18 % gain, 2P see 4–10 % (lower
because the second row shades the rear of the first from ground-reflected
light). This dashboard exists to measure that delta on this site.

## Data sources

```
rubberduck.local:5000/api/solis/data ──HTTP──► tracker_analysis
                                              :8901  (this app)
SMA WebBox 192.168.55.126:502        ──Modbus──┘
   ↳ Sunny Sensor SENS0700:31621:
     · Radiation (POA, W/m²)
     · Module temperature (°C)
     · Ambient temperature (°C)
     · Wind speed (m/s)
```

From the Solis (per string): voltage, current, computed power.
From the SMA WebBox: irradiance + temps + wind, every 5 s.
Computed locally: sun azimuth/elevation, tracker tilt (NS-axis backtracking
model — Arctech tracker doesn't expose tilt on the network), clear-sky
GHI for clearness index.

## Dashboard

Top KPI row:

- **1P bifacial gain** — instantaneous + today's average
- **2P bifacial gain** — instantaneous + today's average
- **POA irradiance** — current W/m²
- **Sun elevation** — current degrees
- **Cumulative BG since install** — kWh-weighted

Per-string strip — V, A, W, today kWh, specific yield (kWh/kWp) for
each of PV1–PV4.

Charts:

1. **Per-string power today** — 4 lines, bifacial pairs in matching
   solid colours, mono pairs in dashed.
2. **Bifacial gain today** — BG_1P and BG_2P over time. The headline
   chart.
3. **POA irradiance + temperatures** — irradiance, ambient temp,
   module temp overlaid.
4. **Power vs irradiance scatter** — 4 colours, slope = string efficiency.
5. **Daily energy bars** — today's kWh per string, bifacial vs mono
   side by side.
6. **(Future)** BG vs sun-elevation heatmap, weekly BG trend.

CSV log at `./data/YYYY-MM-DD.csv`, one row every 5 s, rotating at
local midnight.

## Quick start (on desky)

```bash
git clone https://github.com/glenmo/microgrid_remote_monitor ~/microgrid_remote_monitor
cd ~/microgrid_remote_monitor/tracker_analysis
sudo bash install.sh
```

Defaults: listens on `0.0.0.0:8901`, polls
`http://rubberduck.local:5000/api/solis/data` every 1 s and the SMA
WebBox at `192.168.55.126:502` every 5 s, CSV in `./data/`. Open
`http://desky.local:8901/`.

## CLI options

```
--host                  Flask listen address           (default 0.0.0.0)
--port                  Flask listen port              (default 8901)
--solis-url             Solis upstream                 (default http://rubberduck.local:5000/api/solis/data)
--sma-host              SMA WebBox IP                  (default 192.168.55.126)
--sma-port              SMA WebBox Modbus port         (default 502)
--sma-poll              SMA poll interval (s)          (default 5.0)
--solis-poll            Solis poll interval (s)        (default 1.0)
--csv-interval          CSV log interval (s)           (default 5.0)
--csv-dir               where to write CSVs            (default ./data)
--lat                   site latitude (degrees)        (default -37.4)
--lon                   site longitude (degrees)       (default 144.9)
--string-kwp            nameplate per string (kWp)     (default 16.965)
```

## SMA Modbus register discovery

The Sunny SensorBox values via SMA Modbus profile use 32-bit signed
integers at specific addresses. Without site-specific docs, the
addresses in `sma_reader.py` are best-guess defaults. If a value reads
zero or nonsense, run the probe to scan the register space:

```bash
./venv/bin/python sma_reader.py --probe --sma-host 192.168.55.126
```

This scans 30000–31100 and prints every non-trivial value with its
scaled interpretation. Cross-reference against what the WebBox web UI
shows for that moment, then update `SMA_REGISTERS` at the top of
`sma_reader.py` and restart.

## CSV schema

`./data/YYYY-MM-DD.csv` — one row every 5 s:

```
timestamp,
pv1_v, pv1_a, pv1_w, pv2_v, pv2_a, pv2_w,
pv3_v, pv3_a, pv3_w, pv4_v, pv4_a, pv4_w,
poa_wm2, mod_temp_c, amb_temp_c, wind_ms,
sun_elev_deg, sun_azim_deg, tracker_tilt_deg,
bg_1p_pct, bg_2p_pct,
pr_1p_bifa, pr_1p_mono, pr_2p_mono, pr_2p_bifa
```

## Notes

- **Effective sample rate** is bounded by rubberduck's Solis Modbus poll
  cadence (5 s after today's hardening). The dashboard refreshes every
  1 s but values only change when fresh data arrives.
- **Performance Ratio** is computed as
  `string_kWh / (POA_kWh × string_kWp / 1000)`. Values >100 % are usually
  irradiance sensor under-reading or temperature gain on bifacial.
- **Tracker tilt** is estimated, not measured. If Arctech publishes a
  control interface we'll add a real reading.
- **Clearness index** is `POA / clear_sky_GHI` (rough — POA differs from
  GHI by sun-angle geometry, but useful for sky-condition trending).
