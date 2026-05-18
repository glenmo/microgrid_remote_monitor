# sigenergy_monitor

**Sigenergy SigenStor Monitoring** — comprehensive Modbus TCP dashboard for
the Sigen SigenStor EC 10.0 TP at home, with 32 kWh battery (4 × 8 kWh
modules) and a 25 kW DC EV charger.

Pulls every available register from the Sigenergy V2.2 Modbus profile,
overlays the on-site weather context (POA irradiance, ambient temp,
wind, humidity, air pressure) from the SMA WebBox Sunny Sensor that's
already streaming to desky via FTP-Push, and logs everything to a daily
CSV.

## What it monitors

### From the Sigenergy SigenStor at 192.168.55.131:502

**Plant-level (Modbus slave 247):**
- ESS state-of-charge
- Plant active/reactive power
- Total PV power
- ESS charge/discharge power
- Grid sensor: import/export per phase (A/B/C)
- On-grid / off-grid status
- EMS work mode (Max self-consumption / Sigen AI / TOU / Remote)
- General alarm flags 1–5

**Hybrid inverter (Modbus slave 1):**
- Battery SOC, SOH, average cell temperature, average cell voltage
- Battery charge/discharge power and max limits
- Available charge / discharge energy
- PV string voltages and currents (PV1–PV4)
- Three-phase AC voltages (line-to-line + phase-to-neutral)
- Three-phase AC currents
- Grid frequency, power factor
- PCS internal temperature
- Active/reactive power and adjustment ranges
- Insulation resistance
- Daily PV / battery-charge / battery-discharge / import / export energy
- Accumulated lifetime energy counters (kWh)
- Running state + alarm registers
- Model, serial number, firmware version

**DC EV Charger (Modbus slave 1, registers 31500+):**
- Vehicle battery voltage
- Charging current
- Output power
- Vehicle SOC
- Session energy & duration

### From the SMA WebBox via FTP-Push (same pipeline as `tracker_analysis`)
- POA irradiance (from Sunny Sensorbox silicon cell + pyranometer)
- Ambient temperature
- Module temperature (sensor box surface)
- Wind speed
- Humidity, air pressure

## Architecture

```
                          ┌──────────────────────────────┐
                          │   Sigenergy SigenStor        │
                          │   192.168.55.131:502         │
                          │   ├─ slave 247 (plant info)  │
                          │   └─ slave 1   (inverter)    │
                          └──────────────┬───────────────┘
                                         │ Modbus TCP, 5 s poll
                                         ▼
                          ┌──────────────────────────────┐
                          │   desky.local:8902           │
                          │   sigenergy_monitor (Flask)  │
                          │   + Wh integrators           │
                          │   + CSV log (5 s cadence)    │
                          │   + dashboard                │
                          └──────────────┬───────────────┘
                                         │ filesystem watch
                                         │
                              /home/glen/sma/  (SMA WebBox FTP-Push)
```

## Dashboard layout

**Top KPI row:**
- Battery SOC (with SOH sub-text)
- Battery Power (signed: + charging / − discharging)
- PV Power
- Grid Power (signed: + import / − export)
- Plant Running State

**Energy-today strip:** PV today · Battery charged today · Battery discharged
today · Grid imported today · Grid exported today · Net flow.

**Battery card:** Average cell temp · Average cell voltage · SOH · Available
charge energy · Available discharge energy · Max charge / discharge power.

**PV strings card:** V, A, calculated W for each of PV1–PV4 · Insulation
resistance · MPPT count.

**AC output card:** Phase A/B/C voltages and currents · Line voltages
(A–B, B–C, C–A) · Grid frequency · Power factor · PCS internal temp.

**EV Charger card** (collapses when idle): Vehicle SOC · Charging power ·
Voltage · Current · Session energy · Session duration.

**Weather card:** POA W/m² · Ambient °C · Wind m/s · Humidity % · Air
pressure hPa.

**Charts:**
1. **Power flow today** — PV, Battery, Grid, computed Load all on one chart
2. **SOC trajectory today**
3. **AC voltages today** — three phases overlaid
4. **Battery & PCS temperature today** — cell temp, PCS temp, ambient

## Quick start (on desky)

```bash
git clone https://github.com/glenmo/microgrid_remote_monitor ~/microgrid_remote_monitor
cd ~/microgrid_remote_monitor/sigenergy_monitor
sudo bash install.sh
```

Defaults: listens on `0.0.0.0:8902`, polls `192.168.55.131:502` every 5 s
(slave 247 + slave 1), watches `~/sma/` for SMA WebBox push files, writes
CSV to `./data/`. Open `http://desky.local:8902/`.

## CLI options

```
--host                 Flask listen address           (default 0.0.0.0)
--port                 Flask listen port              (default 8902)
--sigen-host           SigenStor IP                   (default 192.168.55.131)
--sigen-port           SigenStor Modbus TCP port      (default 502)
--sigen-plant-slave    Plant Modbus slave             (default 247)
--sigen-inv-slave      Inverter Modbus slave          (default 1)
--sigen-poll           Sigenergy poll interval (s)    (default 5.0)
--sma-watch-dir        SMA FTP-Push watch directory   (default $HOME/sma)
--sma-poll             SMA scan interval (s)          (default 10.0)
--csv-interval         CSV log interval (s)           (default 5.0)
--csv-dir              CSV directory                  (default ./data)
--battery-kwh          Battery nameplate capacity     (default 32.0)
```

## CSV schema

`./data/YYYY-MM-DD.csv` — one row every 5 s:

```
timestamp,
battery_soc, battery_soh, cell_temp_c, cell_voltage_v,
battery_power_w, pv_power_w, grid_power_w, plant_active_power_w,
pv1_v, pv1_a, pv2_v, pv2_a, pv3_v, pv3_a, pv4_v, pv4_a,
ac_a_v, ac_b_v, ac_c_v, ac_a_a, ac_b_a, ac_c_a,
grid_freq_hz, power_factor, pcs_temp_c,
ev_charging_w, ev_vehicle_soc, ev_session_kwh,
poa_wm2, amb_temp_c, wind_ms, humidity_pct, air_pressure_pa,
pv_today_kwh, batt_charge_today_kwh, batt_discharge_today_kwh,
grid_import_today_kwh, grid_export_today_kwh
```

## Notes

- **Battery sign convention:** Sigenergy reports `ESS power` (register 30037
  plant / 30599 inverter) with **>0 = charging, <0 = discharging**. The
  dashboard shows this with consistent + / − sign colouring (green charging,
  orange discharging).
- **Grid sign convention:** `Grid sensor active power` (register 30005)
  follows **>0 = buy from grid (import), <0 = sell to grid (export)**.
- **EV charger state:** when no session is active, registers 31500–31508
  read zero or near-zero. The dashboard collapses the EV card to a compact
  "idle" pill at those times.
- **Three-phase**: the EC 10.0 TP is L1/L2/L3/N (output type = 2). All
  per-phase A/B/C registers are valid.
- **Effective sample rate** is bounded by the Sigenergy Modbus poll cadence
  (5 s). The dashboard refreshes display every 1 s but values only change
  when fresh reads land.
- **Weather data** is pulled from the same WebBox push pipeline that feeds
  `tracker_analysis` — no extra config needed since FTP-Push is already
  running into `~/sma/`.
