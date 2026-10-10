# Lodge weather page for the kiosk: design spec

## Context

The Lodge kiosk (drongo, 1920×1080, served by `soc_traffic_light` on pignus at `/kiosk`) shows microgrid, hot water, heaters and a 4-day forecast tile. It has no live weather. The site's SMA Meteostation already reports through a Sunny WebBox: the WebBox FTP-pushes 15-min means to **desky**, and `tracker_analysis` (desky:8901, `microgrid_remote_monitor/tracker_analysis/`) decodes them. Its `/api/state` returns `live.amb_temp_c`, `mod_temp_c`, `poa_wm2`, `pyranometer_wm2`, `wind_ms`, `humidity_pct`, `air_pressure_pa`, `clearness_index`, plus `sma_status.{connected,last_read,age_s}`. Nothing moves this data off desky today. (desky:8900 is the unrelated Fox efficiency test, and the Modbus probe on :8910 is idle.)

Goal: a full-screen **Lodge weather** page in the kiosk's 30 s view rotation. It shows live Meteostation readings, a 24 h trend chart, and a 7-day forecast.

Decisions made with the user:
- desky **pushes** to pignus (no inbound access to the LAN).
- Scope: live + 7-day + today's trend chart (so pignus stores history).
- Approach A: a new `weather_monitor` app in `microgrid_remote_monitor`, following the `comfort_monitor` pattern.

This HA repo (`lodge_home_assistant`) is **not changed**.

## Step 0: sync stale checkouts
Local `~/Code/microgrid_remote_monitor` (56 behind, untracked `selpi_probe.py`, `sppro_sx_reader.py`) and `~/Code/soc_traffic_light` (20 behind, uncommitted edits to `app.py`, `install.sh`, `templates/index.html`) are behind GitHub main. Ask the user how to handle the local edits, then `git pull --ff-only` both before editing.

## Data flow
```
WebBox ─FTP 15-min─▶ desky tracker_analysis :8901 /api/state
desky weather_push.py (systemd, 60 s) ─POST /weather/api/push (X-API-Key)─▶
pignus weather_monitor :8130 ── SQLite samples (35 days)
   ├─ Open-Meteo 7-day daily forecast, refreshed 15 min, cached in memory
   └─ GET /weather/ (page), /weather/api/{current,history,forecast}
drongo kiosk: views rotation += "/weather/"
```

## Components

### 1. `tracker_analysis/weather_push.py` (desky), new
- Every 60 s: GET `http://localhost:8901/api/state`.
- Skip the push if `sma_status.connected` is false or `sma_status.age_s > 1800`.
- POST JSON to `WEATHER_PUSH_URL` (default `https://monitor.mooramoora.org.au/weather/api/push`) with header `X-API-Key: $WEATHER_API_KEY`:
  `{"ts": <sma_status.last_read ISO>, "ambient_c", "module_c", "irradiance_wm2" (= poa_wm2, SensorBox, the trusted one), "pyranometer_wm2", "wind_ms", "humidity_pct", "pressure_hpa" (= air_pressure_pa/100), "clearness_index"}`
- Log failures and keep looping. Stdlib only (urllib), Python 3.
- `install_weather_push.sh` writes `/etc/weather-push.env` (mode 600) and `weather-push.service` (`EnvironmentFile`, `Restart=always`).

### 2. `weather_monitor/` (pignus :8130), new, modelled on `comfort_monitor/app.py`
- `WEATHER_API_KEY` from `/etc/weather-monitor.env`. `_authorised()` uses `hmac.compare_digest` (copy from comfort_monitor). `MAX_CONTENT_LENGTH` 16 KB.
- `POST /api/push`: `clean_payload()` coerces the known fields to finite floats and drops the rest. `ts` is parsed and must be within 2 h of now. Updates the in-memory latest and runs `INSERT OR IGNORE` into `samples(ts INTEGER PRIMARY KEY, ambient, module, irradiance, pyranometer, wind, humidity, pressure, clearness)`. Because the key is the reading time, rows are deduped to one per WebBox mean. Prune rows older than 35 days.
- `GET /api/current`: latest reading + `age_s` (now − reading ts) + `stale` (age > 45 min). `Cache-Control: no-store`.
- `GET /api/history?hours=24` (max 168): samples as arrays for charting.
- `GET /api/forecast`: cached Open-Meteo daily for `LAT, LON = -37.72, 145.55` (same as `soc_traffic_light/extras.py` L36). `daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max`, `forecast_days=7`, `timezone=Australia/Melbourne`, default model (`bom_access_global` returns nulls, per the extras.py comment). A background thread refreshes every 15 min and retries every 2 min on error. It keeps the last good result with `fetched_at`/`error` (the `extras.py` `_loop` pattern).
- `GET /`: `templates/weather.html`.
- Python 3.9 compatible. `requirements.txt`: flask (match comfort_monitor). `install.sh`: venv, generated env key, hardened unit (`ProtectSystem=strict`, `ReadWritePaths=data`) as in comfort_monitor.
- `server/monitor.mooramoora.org.au.conf`: `ProxyPass /weather/ http://127.0.0.1:8130/` + reverse, placed **above** the catch-all `ProxyPass /`. Confirm on pignus that :8130 is free.

### 3. `weather_monitor/templates/weather.html`, new
Single-file inline CSS/JS. Copies the kiosk's CSS variables (`--bg --card --card2 --fg --mute --blue --sun …`), system-ui with tabular numbers, inline stroke-SVG icons, and the `fitKiosk()` 1920×1080 scaling plus the narrow/portrait single-column fallback. Uses relative fetches with `cache:"no-store"`.
- **Now panel (left):** big ambient °C. Wind in km/h with a Beaufort word. Humidity %. Pressure hPa. Sunlight W/m², with "% of clear sky" from `clearness_index`. Module temp is small. Footer reads "Meteostation · updated HH:MM". The `.stale` dimming applies past 45 min. Polls `api/current` every 60 s.
- **Trend (right top):** 24 h inline-SVG chart with temperature as a line (left axis) and sunlight as a shaded area (right axis), plus hour ticks. Polls `api/history` every 5 min.
- **7-day row (bottom):** cards with weekday ("Today" first), a weather_code icon and label (reuse the kiosk tile's code→icon mapping from `soc_traffic_light/templates/kiosk.html`), max/min, rain % and mm, and max wind/gust km/h. Shows "forecast as of HH:MM" if the last fetch errored. Polls `api/forecast` every 15 min.
- A full page reload every 900 s, like the kiosk.

### 4. `soc_traffic_light/templates/kiosk.html` (`cycleViews`, ~L707)
Add `views.push(["/weather/", "Lodge weather"])` so every kiosk viewer gets it, not only mesh-token holders. Deployed on pignus with `git pull` + `systemctl restart soc-traffic-light`.

### 5. Docs
- `microgrid_remote_monitor/CLAUDE.md`/README: add the weather_monitor app and the desky pusher.
- `soc_traffic_light` README: the rotation list.
- Save this spec to `microgrid_remote_monitor/docs/superpowers/specs/2026-10-10-lodge-weather-page-design.md` and commit it.

## Error handling summary
- desky down or WebBox stale: no pushes, so the page dims the Now panel and the chart shows a gap.
- Open-Meteo down: the last good forecast is served with an "as of" note.
- pignus restart: latest is reloaded from the newest SQLite row at startup.

## Verification
1. `weather_monitor/tests/`: pytest for `clean_payload` (junk, NaN, unknown keys, old ts), dedupe on repeated ts, history window, forecast parsing with Open-Meteo stubbed, and 401 without or with a wrong key.
2. Run the app locally, POST a sample with curl, check `/api/current` and `/api/history`, and view the page at 1920×1080 in Chrome (claude-in-chrome screenshot), including a stale state and a forecast-error state.
3. Deploy on pignus (batch the SSH commands, since fail2ban is on): install.sh, Apache reload, `curl -s https://monitor.mooramoora.org.au/weather/api/current`.
4. Deploy the pusher on desky, check `journalctl -u weather-push`, and confirm pignus `age_s` matches desky's `sma_status.age_s`.
5. Deploy the kiosk change and watch `/kiosk` cycle to the weather view (or ask the user to confirm on drongo).
