# Octagon Comfort

Aircon status page for the Octagon at <https://monitor.mooramoora.org.au/octagon/>: live
state of the Daikin FTXM71WVMA, temperature, heating/cooling and compressor-power
history, and daily use. A copy of Studio Comfort (`../studio_monitor/`), itself a copy
of Dacha Comfort (`../dacha_monitor/`).

## Data flow

```
Daikin unit → noisy (Home Assistant, home LAN)
            → POST every 60 s → pignus /octagon/api/push  (X-API-Key)
            → SQLite (35 days) → /octagon/  +  /octagon/api/{current,history,summary}
```

pignus can't reach noisy, so noisy pushes. The Home Assistant side lives in
`glenmo/octagon_comfort`, `packages/octagon_push.yaml`.

Only units in `UNITS` (in `app.py`) are accepted, stored or served. To add a unit, add
it there and to the `units` map in `octagon_push.yaml`.

**Heating vs resting.** The Daikin integration reports `hvac_action: heating` whenever
the unit is in heat mode, even after it reaches its setpoint and the compressor stops.
noisy also sends `running` (`binary_sensor.octagon_aircon_running`, compressor frequency
above zero). The page and the hours counts use that, so a unit sitting at temperature shows
as "resting" and isn't counted as heating.

## Remote on/off (password)

Each unit card has a Turn on / Turn off button. Pressing it asks for the control
password, then POSTs to `/api/control`. pignus can't reach noisy, so the command waits
in memory until noisy collects it from `/api/command`. Home Assistant checks every 10 s
(`packages/octagon_control.yaml` in glenmo/octagon_comfort), switches
`switch.octagon_aircon_power`, and pushes the new state straight back. A command noisy
hasn't collected after 2 minutes is dropped, so a late pickup can't flip the unit
unexpectedly. On/off only: the unit keeps its last mode and setpoint.

- The password is stored only as a scrypt hash, `OCTAGON_CONTROL_HASH` in
  `/etc/octagon-monitor.env`. Set or change it with `sudo bash set_password.sh`.
  Without the hash, the button is hidden and `/api/control` returns 503.
- After 5 wrong passwords an address is locked out for 15 minutes. After 30 wrong
  passwords from anyone within an hour, control is paused for everyone.
- Every attempt (queued, bad password, locked, collected) is logged to the `controls`
  table in the SQLite file, which is kept for 35 days.

## API

| Endpoint | |
| --- | --- |
| `POST /api/push` | Snapshot from noisy. Header `X-API-Key: $OCTAGON_API_KEY`. |
| `GET /api/current` | Latest snapshot, `age_s`, `stale` (no push for over 5 min). |
| `GET /api/history?hours=24` | Bucketed series (max 720 h, about 360 points): temps, heating/cooling/on fraction, compressor kW. |
| `GET /api/summary?days=7` | Per local day: hours heating/cooling, kWh, inside min/max. |
| `POST /api/control` | `{"unit", "action": "on"\|"off", "password"}` from the page. 202 with a command `id`, 401 wrong password, 429 locked out. |
| `GET /api/control/<id>` | `waiting`, `collected`, `replaced` or `expired`. |
| `GET /api/command` | noisy collects pending commands. Header `X-API-Key: $OCTAGON_API_KEY`. Each is handed out once. |
| `GET /healthz` | Liveness plus age of the last push. |

The page is installable on phones like Lodge Comfort (`/manifest.webmanifest`, icons in
`static/`, network-first `sw.js`). Bump `CACHE` in `sw.js` if cached assets must reach
installed copies immediately.

## Run locally

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
OCTAGON_DB=/tmp/octagon.db OCTAGON_API_KEY=test venv/bin/python app.py --port 8128
```

Flask caches templates when it isn't in debug mode, so restart it after editing
`templates/index.html`.

## Deploy (pignus)

```bash
cd ~/microgrid_remote_monitor && git pull --ff-only
sudo bash octagon_monitor/install.sh            # venv, key in /etc/octagon-monitor.env, systemd unit
sudo bash octagon_monitor/set_password.sh       # password for the on/off button
# Apache: add the /octagon/ block from server/monitor.mooramoora.org.au.conf to the live vhost
sudo apachectl configtest && sudo systemctl reload apache2
```

Updates after that: `git pull --ff-only && sudo systemctl restart octagon-monitor`.
