# Lodge Weather

Full-screen weather page for the Lodge kiosk (`/weather/` on monitor.mooramoora.org.au, shown in
`/kiosk`'s 30 s rotation from the `soc_traffic_light` repo).

- **Live:** SMA Meteostation / Sunny SensorBox readings. The Sunny WebBox FTP-pushes 15-minute means to
  desky, `tracker_analysis` decodes them, and `tracker_analysis/weather_push.py` POSTs them here every
  60 s (`X-API-Key`). One SQLite row per reading time, kept 35 days.
- **Forecast:** Open-Meteo 7-day daily forecast for Mount Toolebewong, refreshed every 15 min. The last
  good forecast is kept if a fetch fails.
- Pressure is the station's absolute pressure (≈ 930 hPa at the lodge), not sea-level.

## API
- `POST /api/push`: `{"ts": epoch, "ambient_c", "module_c", "irradiance_wm2", "pyranometer_wm2", "wind_ms",
  "humidity_pct", "pressure_hpa", "clearness_index"}`. Unknown keys are dropped. Out-of-range values become null.
- `GET /api/current`: latest reading, `age_s`, and `stale` (older than 45 min).
- `GET /api/history?hours=24` (max 168)
- `GET /api/forecast`: `{"days": [...], "fetched_at", "error"}`

## Install (pignus)
    cd ~/microgrid_remote_monitor/weather_monitor && sudo bash install.sh
Then add the Apache block (see the end of install.sh) and give desky the key (see `tracker_analysis/install_weather_push.sh`).

## Tests
    python3 -m venv venv && venv/bin/pip install flask pytest
    venv/bin/python -m pytest tests -q
