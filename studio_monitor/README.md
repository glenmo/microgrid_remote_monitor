# Studio Comfort

Aircon status page for the Studio at <https://monitor.mooramoora.org.au/studio/>: live
state of the Daikin FTXM95WVMA, temperature, heating/cooling and compressor-power
history, and daily use. A copy of Dacha Comfort (`../dacha_monitor/`), itself built
from Lodge Comfort without the solar-surplus parts.

## Data flow

```
Daikin unit → noisy (Home Assistant, home LAN)
            → POST every 60 s → pignus /studio/api/push  (X-API-Key)
            → SQLite (35 days) → /studio/  +  /studio/api/{current,history,summary}
```

pignus can't reach noisy, so noisy pushes. The Home Assistant side lives in
`glenmo/studio_comfort`, `packages/studio_push.yaml`.

Only units in `UNITS` (in `app.py`) are accepted, stored or served. To add a unit, add
it there and to the `units` map in `studio_push.yaml`.

**Heating vs resting.** The Daikin integration reports `hvac_action: heating` whenever
the unit is in heat mode, even after it reaches its setpoint and the compressor stops.
noisy also sends `running` (`binary_sensor.studio_aircon_running`, compressor frequency
above zero). The page and the hours counts use that, so a unit sitting at temperature shows
as "resting" and isn't counted as heating.

## API

| Endpoint | |
| --- | --- |
| `POST /api/push` | Snapshot from noisy. Header `X-API-Key: $STUDIO_API_KEY`. |
| `GET /api/current` | Latest snapshot, `age_s`, `stale` (no push for over 5 min). |
| `GET /api/history?hours=24` | Bucketed series (max 720 h, about 360 points): temps, heating/cooling/on fraction, compressor kW. |
| `GET /api/summary?days=7` | Per local day: hours heating/cooling, kWh, inside min/max. |
| `GET /healthz` | Liveness plus age of the last push. |

The page is installable on phones like Lodge Comfort (`/manifest.webmanifest`, icons in
`static/`, network-first `sw.js`). Bump `CACHE` in `sw.js` if cached assets must reach
installed copies immediately.

## Run locally

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
STUDIO_DB=/tmp/studio.db STUDIO_API_KEY=test venv/bin/python app.py --port 8127
```

Flask caches templates when it isn't in debug mode, so restart it after editing
`templates/index.html`.

## Deploy (pignus)

```bash
cd ~/microgrid_remote_monitor && git pull --ff-only
sudo bash studio_monitor/install.sh            # venv, key in /etc/studio-monitor.env, systemd unit
# Apache: add the /studio/ block from server/monitor.mooramoora.org.au.conf to the live vhost
sudo apachectl configtest && sudo systemctl reload apache2
```

Updates after that: `git pull --ff-only && sudo systemctl restart studio-monitor`.
